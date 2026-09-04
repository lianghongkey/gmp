#!/usr/bin/env python
"""在真板上给一段话做 prefill，再逐 token decode，把生成的文字打印出来。

    python host/soc_generate.py "今天天气不错，我们去"
    python host/soc_generate.py --chat "请简单介绍一下你自己。"
    python host/soc_generate.py --file prompt.txt --max-new 32 -v

开跑前查一遍板子，不对的自己修（烧 bit、灌权重）：`--no-auto` 只诊断不动手，`--check-only`
查完就退出，`--dry-run` 只分词不碰板子。流程与排错见 `docs/usage.md`。

分词用 `data/tokenizer.json.gz` 里的词表，编码是本文件里的纯 Python BPE，解码按 GPT-2 的
byte-level 表还原。`--vocab` 指到一份 GGUF 时改用 llama.cpp 的 `llama-tokenize`。
"""
import argparse
import codecs
import contextlib
import io
import json
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import device as DEV                                                # noqa: E402
import runtime as RT                                                # noqa: E402
from hw_params import CPU_PERIPH_BASE, CPU_GP0                      # noqa: E402

DATA = os.path.join(DEV.GMP, "data")
VOCAB_DEFAULT = os.path.join(DATA, "tokenizer.json.gz")
#   缺省 50 MHz 那份，换 66.7 MHz 的用 `--bit`
BIT_DEFAULT = os.path.join(DEV.GMP, "prebuilt", "bit", "top_jtag_p3.bit")
JTAG_DIR = os.path.join(DEV.GMP, "tools")
sys.path.insert(0, JTAG_DIR)
IDCODE_7K480T = 0x03751093          # 低 28 位；bit[31:28] 是版本号


# ══════════════════════════════════════════════════════════════════════════
# 分词
# ══════════════════════════════════════════════════════════════════════════

def gguf_metadata(path):
    """只读 GGUF 头部的 KV 元数据（不碰张量）。返回 {键: 值}。"""
    f = open(path, "rb")

    def rd(fmt):
        return struct.unpack("<" + fmt, f.read(struct.calcsize(fmt)))

    magic, ver, _n_tensor, n_kv = rd("IIQQ")
    if magic != 0x46554747 or ver not in (2, 3):
        raise ValueError(f"{path} 不是 GGUF v2/v3（magic {magic:#x}, ver {ver}）")

    def rstr():
        n, = rd("Q")
        return f.read(n).decode("utf-8", "replace")

    scalar = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}

    def rval(t):
        if t == 8:
            return rstr()
        if t == 9:
            et, = rd("I")
            n, = rd("Q")
            return [rval(et) for _ in range(n)]
        return rd(scalar[t])[0]

    meta = {}
    for _ in range(n_kv):
        k = rstr()
        t, = rd("I")
        meta[k] = rval(t)
    f.close()
    return meta


def _bytes_to_unicode():
    """GPT-2 的 byte → 可打印 unicode 字符表（词表里的 token 串就是这么编的）。"""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))


TOK_NORMAL, TOK_CONTROL, TOK_USER, TOK_UNUSED = 1, 3, 4, 5
# 3 = 控制（<|im_end|> 这类，不打印）；4 = 用户定义（<think> 这类，llama.cpp 也把它当特殊 token 整个切，
# 但打印时原样给）；5 = 没用上的 PAD。


def load_vocab(path):
    """词表两种来源：随包的 `tokenizer.json.gz`（从 GGUF 里摘出来的那几个字段），
    或者一整份 GGUF。字段名两边一样。"""
    if path.endswith(".gguf"):
        return gguf_metadata(path)
    import gzip
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


class Tokenizer:
    def __init__(self, vocab_path, tokenize_bin=None):
        #   llama-tokenize 要的是一份 GGUF，给的是摘出来的词表时它就用不上，走纯 Python BPE
        self.gguf = os.path.abspath(vocab_path) if vocab_path.endswith(".gguf") else None
        m = load_vocab(vocab_path)
        if m.get("tokenizer.ggml.model") != "gpt2":
            raise ValueError(f"只会处理 byte-level BPE（tokenizer.ggml.model = gpt2），"
                             f"这份是 {m.get('tokenizer.ggml.model')!r}")
        self.pre = m.get("tokenizer.ggml.pre", "")
        self.tokens = m["tokenizer.ggml.tokens"]
        self.types = m.get("tokenizer.ggml.token_type") or [TOK_NORMAL] * len(self.tokens)
        self.vocab = {t: i for i, t in enumerate(self.tokens)}
        self.eos = m.get("tokenizer.ggml.eos_token_id")
        self.bos = m.get("tokenizer.ggml.bos_token_id")
        self.add_bos = bool(m.get("tokenizer.ggml.add_bos_token", False))
        self.name = m.get("general.name", "?")
        self.special = {t: i for i, t in enumerate(self.tokens) if self.types[i] in (TOK_CONTROL, TOK_USER)}
        b2u = _bytes_to_unicode()
        self.b2u = b2u
        self.u2b = {u: b for b, u in b2u.items()}
        self._merges = m.get("tokenizer.ggml.merges")
        self._ranks = None
        self.tokenize_bin = self._find_bin(tokenize_bin)

    # ── 编码 ──
    def _find_bin(self, given):
        if self.gguf is None:
            return None
        cands = [given, os.environ.get("LLAMA_TOKENIZE"), shutil.which("llama-tokenize")]
        # GGUF 多半就放在 llama.cpp 的 build/bin 下面
        d = os.path.dirname(os.path.realpath(self.gguf))
        for _ in range(6):
            cands.append(os.path.join(d, "llama-tokenize"))
            d = os.path.dirname(d)
        for c in cands:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return None

    def encode(self, text):
        if self.tokenize_bin:
            return self._encode_bin(text)
        return self._encode_py(text)

    def _encode_bin(self, text):
        cmd = [self.tokenize_bin, "-m", self.gguf, "-p", text, "--ids", "--log-disable", "--no-escape"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"llama-tokenize 失败：{r.stderr.strip()[-400:]}")
        line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.strip().startswith("[")), None)
        if line is None:
            raise RuntimeError(f"llama-tokenize 没输出 id 列表：{r.stdout[-200:]!r}")
        return json.loads(line)

    # 纯 Python 退路。llama.cpp 里 qwen2 那条 pre-tokenizer 正则是
    #   (?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+
    # `re` 没有 \p{L} / \p{N}，用 [^\W\d_] 与 \d 顶替（差别只在 Ⅻ / ½ 这类 Nl / No 字符）。
    _L = r"[^\W\d_]"
    _N = r"\d"
    _PRE = re.compile(
        r"(?i:'s|'t|'re|'ve|'m|'ll|'d)"
        rf"|(?:(?!{_L}|{_N})[^\r\n])?{_L}+"
        rf"|{_N}"
        rf"| ?(?:(?!{_L}|{_N})\S)+[\r\n]*"
        r"|\s*[\r\n]+"
        r"|\s+(?!\S)"
        r"|\s+")

    def _encode_py(self, text):
        if self._ranks is None:
            if not self._merges:
                raise RuntimeError("GGUF 里没有 merges，纯 Python 编码做不了；装 llama.cpp 的 llama-tokenize")
            self._ranks = {tuple(mg.split(" ")): i for i, mg in enumerate(self._merges)}
            sp = sorted(self.special, key=len, reverse=True)
            self._split_special = re.compile("(" + "|".join(re.escape(s) for s in sp) + ")") if sp else None
        out = [self.bos] if self.add_bos and self.bos is not None else []
        pieces = self._split_special.split(text) if self._split_special else [text]
        for piece in pieces:
            if not piece:
                continue
            if piece in self.special:
                out.append(self.special[piece])
                continue
            for word in self._PRE.findall(piece):
                sym = "".join(self.b2u[b] for b in word.encode("utf-8"))
                out.extend(self.vocab[p] for p in self._bpe(sym))
        return out

    def _bpe(self, word):
        parts = list(word)
        while len(parts) > 1:
            best = None
            for i in range(len(parts) - 1):
                r = self._ranks.get((parts[i], parts[i + 1]))
                if r is not None and (best is None or r < best[0]):
                    best = (r, i)
            if best is None:
                break
            i = best[1]
            parts[i:i + 2] = [parts[i] + parts[i + 1]]
        return parts

    # ── 解码 ──
    def is_control(self, tid):
        return 0 <= tid < len(self.tokens) and self.types[tid] in (TOK_CONTROL, TOK_UNUSED)

    def token_bytes(self, tid):
        """普通 token 还原成字节；控制 token 返回 None。"""
        if self.is_control(tid) or not (0 <= tid < len(self.tokens)):
            return None
        s = self.tokens[tid]
        if self.types[tid] == TOK_USER:
            return s.encode("utf-8")          # <think> 这类是明文，不按 byte 表编
        try:
            return bytes(self.u2b[c] for c in s)
        except KeyError:
            return s.encode("utf-8")          # 词表里偶有不按 byte 表编的（PAD 之类），原样返回

    def decode(self, ids):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        out = []
        for t in ids:
            b = self.token_bytes(t)
            out.append(dec.decode(b) if b is not None else self.tokens[t])
        out.append(dec.decode(b"", final=True))
        return "".join(out)


def chat_prompt(user, system=None, think=False):
    """Qwen3 的对话模板。缺省把 think 段留空（`<think>\\n\\n</think>`）。"""
    s = ""
    if system:
        s += f"<|im_start|>system\n{system}<|im_end|>\n"
    s += f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"
    if not think:
        s += "<think>\n\n</think>\n\n"
    return s


# ══════════════════════════════════════════════════════════════════════════
# 板子
# ══════════════════════════════════════════════════════════════════════════

def _hex(v):
    return f"0x{v:010x}"


class Board:
    """把要一路传下去的几件东西收拢成一个对象。"""

    def __init__(self, tr):
        self.tr = tr
        self.soc = DEV.Soc(tr)

    def put(self, addr, data):
        RT.dram_put(self.tr, self.soc, addr, data)

    def get(self, addr, nbytes):
        return RT.dram_get(self.tr, self.soc, addr, nbytes)

    def put_i32(self, addr, v):
        self.put(addr, int(v).to_bytes(4, "little", signed=True))

    def get_i32(self, addr):
        return int.from_bytes(self.get(addr, 4), "little", signed=True)


def check_weights(bd, w, n, seed, verbose):
    """抽 n 条权重各比 1 KiB。返回不一致的条目名。"""
    rng = random.Random(seed)
    bad = []
    for name in rng.sample(list(w.files), min(n, len(w.files))):
        arr = np.ascontiguousarray(w[name]).tobytes()
        addr = RT.entry_addr(name)
        win = min(1024, len(arr))
        off = rng.randrange(0, len(arr) - win + 1) & ~3
        got = bd.get(addr + off, win)
        same = got == arr[off:off + win]
        if verbose or not same:
            print(f"    {'ok  ' if same else 'DIFF'} {name} +{off:#x} {win} B")
        if not same:
            bad.append(name)
    return bad


def load_weights(bd, w, stop=None):
    total = RT.WEIGHTS_BYTES
    tot = 0
    t0 = time.time()
    for name in w.files:
        if stop is not None and stop.flag:
            raise BoardNotReady(f"Ctrl-C：权重只灌了 {tot / 2**20:.0f} MiB，没灌完")
        data = np.ascontiguousarray(w[name]).tobytes()
        with contextlib.redirect_stdout(io.StringIO()):     # dram_put 每 MiB 打一行，这里不要
            bd.put(RT.entry_addr(name), data)
        tot += len(data)
        if tot % (64 << 20) < len(data):
            rate = tot / 2**20 / max(time.time() - t0, 1e-9)
            print(f"      … {tot / 2**20:.0f} MiB（{rate:.2f} MB/s，还要约 {(total - tot) / 2**20 / rate / 60:.0f} 分钟）")
    print(f"    权重 {tot / 2**20:.1f} MiB 灌完，{(time.time() - t0) / 60:.1f} 分钟")


# ══════════════════════════════════════════════════════════════════════════
# 开跑前的板子检查：线缆 → IDCODE → 魔数 → 读 burst → DDR 校准；不对的自己修
# ══════════════════════════════════════════════════════════════════════════

class BoardNotReady(SystemExit):
    pass


def _procs():
    out = subprocess.run(["ps", "-eo", "pid,comm"], capture_output=True, text=True).stdout
    res = []
    for ln in out.splitlines()[1:]:
        f = ln.split(None, 1)
        if len(f) == 2:
            res.append((int(f[0]), f[1].strip()))
    return res


def _open_cable():
    """打开线缆。报 `Resource busy` 时先清掉占着 USB 的残留 hw_server；vivado 进程
    本身还在跑就不动它。"""
    import usb.core
    from pyjtag.xpc import XpcError
    for attempt in (1, 2):
        try:
            return DEV.BscanAxiTransport()
        except usb.core.USBError as e:
            busy = getattr(e, "errno", None) == 16 or "busy" in str(e).lower()
            if not busy or attempt == 2:
                raise BoardNotReady(f"线缆打不开：{e}")
            hw = [p for p, c in _procs() if c == "hw_server"]
            viv = [p for p, c in _procs() if c.startswith("vivado")]
            if viv:
                raise BoardNotReady(f"线缆被占用，vivado（pid {viv}）还在跑，等它退出或关掉再来")
            if not hw:
                raise BoardNotReady(f"线缆被占用（{e}），但没看到 hw_server —— 看看谁在用 03fd 设备")
            print(f"    线缆被残留的 hw_server（pid {hw}）占着，kill -9 后重试")
            for p in hw:
                try:
                    os.kill(p, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(2)
        except XpcError as e:
            raise BoardNotReady(f"线缆不正常：{e}")


def _reselect_user1(tr):
    """读 IDCODE 或烧录之后要重新选一次 JTAG 用户链。"""
    from pyjtag import BscanAxi
    tr._axi = BscanAxi(tr._axi.tap)


def _magic_ok(soc):
    from pyjtag.bscan import BscanAxiError
    try:
        return soc.stat_rd(DEV.STAT_MAGIC) == DEV.SOC_MAGIC
    except BscanAxiError:
        return False


def _rburst_ok(tr):
    """读 burst 能读回逐笔写进去的图案 = 板上这条通路支持它。探针写在搬运缓冲上。"""
    from pyjtag.bscan import BscanAxiError
    pat = [0x5A5A0000 + i * 0x01010101 for i in range(8)]
    try:
        tr.write32_many([(DEV.WIN_XFER_BUF << 24 | (i << 2), v) for i, v in enumerate(pat)])
        one = [tr.read32(DEV.WIN_XFER_BUF << 24 | (i << 2)) for i in range(8)]
        many = tr.read_burst(DEV.WIN_XFER_BUF << 24, 8)
        return one == pat and list(many) == pat
    except BscanAxiError:
        return False


def _program_pyjtag(tr, bit):
    from pyjtag.fpga import program_bit
    last = [-1]

    def progress(done, total):
        pct = done * 100 // total
        if pct // 20 != last[0]:
            last[0] = pct // 20
            print(f"      … {pct}%")

    t0 = time.time()
    info, done, ircap = program_bit(tr.cable, tr._axi.tap, bit, progress=progress)
    print(f"    pyjtag 烧 {os.path.basename(bit)}（{info.get('design')} / {info.get('part')} "
          f"{info.get('date')} {info.get('time')}）：DONE={int(done)}，{time.time() - t0:.0f} s")
    _reselect_user1(tr)
    return done


def _program_vivado(tr, bit):
    """走 `tools/program.tcl` 烧录：vivado 期间独占 JTAG，先把线缆放掉，它退出后清掉
    残留的 hw_server。返回重新打开的 transport。"""
    tr.close()
    t0 = time.time()
    env = dict(os.environ, BIT=os.path.abspath(bit))
    r = subprocess.run(["vivado", "-mode", "batch", "-notrace", "-source",
                        os.path.join(DEV.GMP, "tools", "program.tcl"),
                        "-log", os.path.join(DEV.BUILD, "prog.log"),
                        "-journal", os.path.join(DEV.BUILD, "prog.jou")],
                       capture_output=True, text=True, timeout=600, env=env)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    print(f"    vivado 烧 {os.path.basename(bit)}：exit {r.returncode}，{time.time() - t0:.0f} s"
          + ("" if r.returncode == 0 else "\n      " + "\n      ".join(tail)))
    for p, c in _procs():
        if c == "hw_server":
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(3)
    if r.returncode:
        raise BoardNotReady("vivado 烧录失败")
    return _open_cable()


def open_board(a):
    """按阶梯查一遍并修；返回 (transport, 这次有没有重烧)。"""
    from pyjtag.fpga import read_idcode
    tr = _open_cable()
    cab = tr.cable
    print(f"    线缆：firmware 0x{cab.firmware_version:04x}，cpld 0x{cab.cpld_version:04x}")
    idc = read_idcode(tr._axi.tap)
    _reselect_user1(tr)
    if (idc & 0x0FFFFFFF) != IDCODE_7K480T:
        tr.close()
        raise BoardNotReady(f"IDCODE 读到 0x{idc:08x}，不是 xc7k480t：板子没上电？JTAG 排线 / VREF？"
                            "（软件修不了）")
    soc = DEV.Soc(tr)
    why = None
    if a.program:
        why = "--program"
    elif not _magic_ok(soc):
        why = "STAT 魔数读不到或不是 SOC1（没烧，或烧的不是这个 SoC）"
    elif not a.no_burst and not _rburst_ok(tr):
        why = "桥没有读 burst（旧 bit）"
    programmed = False
    if why:
        print(f"    要重烧：{why} → {a.bit}")
        if a.no_auto:
            tr.close()
            raise BoardNotReady(f"--no-auto：不动手。去掉它会烧 {a.bit}")
        if not os.path.isfile(a.bit):
            tr.close()
            raise BoardNotReady(f"找不到 {a.bit} —— 先 make bit-board")
        ok = False
        if a.prog_tool == "pyjtag":
            ok = _program_pyjtag(tr, a.bit)
            if not ok:
                print("    pyjtag 烧完 DONE=0，改用 vivado 再烧一次")
        if not ok:
            tr = _program_vivado(tr, a.bit)
            soc = DEV.Soc(tr)
        programmed = True
        for _ in range(50):
            if _magic_ok(soc):
                break
            time.sleep(0.1)
        else:
            tr.close()
            raise BoardNotReady("烧完 STAT 魔数还是不对：这份 bit 不是 gmp soc？")
        print("    板上认出这颗 SoC")
    else:
        print("    板上认出这颗 SoC" + ("" if a.no_burst else "，读写通路一致"))
    t0 = time.time()
    while not soc.stat_rd(DEV.STAT_BOARD) & 1:
        if time.time() - t0 > 30:
            tr.close()
            raise BoardNotReady("DDR 两通道校准 30 秒没完成（STAT_BOARD bit0 = 0）")
        time.sleep(0.5)
    print("    DDR 校准完成" + (f"（等了 {time.time() - t0:.1f} s）" if time.time() - t0 > 0.5 else ""))
    return tr, programmed


def ensure_weights(a, bd, w, programmed, stop=None):
    if a.load_weights:
        print("── --load-weights：灌整份权重 ──")
        load_weights(bd, w, stop)
    if not a.check_weights:
        return
    bad = check_weights(bd, w, a.check_weights, a.seed, a.verbose)
    if not bad:
        print(f"    权重抽查 {a.check_weights} 处一致")
        return
    print(f"    权重抽查 {len(bad)}/{a.check_weights} 处不一致"
          + ("（刚重烧过，DRAM 随 MIG 复位丢了）" if programmed else "（DRAM 里不是这份权重）"))
    if a.no_auto:
        raise BoardNotReady("--no-auto：不灌。去掉它会整份重灌（18 分钟）")
    print(f"── 整份重灌权重（{RT.WEIGHTS_BYTES / 2**20:.0f} MiB，约 18 分钟）──")
    load_weights(bd, w, stop)
    bad = check_weights(bd, w, a.check_weights, a.seed + 1, a.verbose)
    if bad:
        raise BoardNotReady(f"灌完抽查仍有 {len(bad)} 处不一致：JTAG 数据通路有问题，别往下跑")
    print(f"    灌完再抽查 {a.check_weights} 处一致")


class Stopper:
    """第一次 Ctrl-C 只做个标记，跑完当前这一步再停；第二次才交回缺省行为。"""

    def __init__(self):
        self.flag = False
        self._old = signal.signal(signal.SIGINT, self._on)

    def _on(self, sig, frame):
        if self.flag:
            signal.signal(signal.SIGINT, self._old)
            raise KeyboardInterrupt
        self.flag = True
        print("\n    （收到 Ctrl-C：跑完这一步就停。再按一次强制退出 —— 会卡住线缆）", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("prompt", nargs="?", help="要 prefill 的那段话（或用 --file）")
    ap.add_argument("--file", help="从文件读 prompt")
    ap.add_argument("--chat", action="store_true", help="按 Qwen3 对话模板包一层（缺省不思考）")
    ap.add_argument("--system", default=None, help="--chat 时的 system 消息")
    ap.add_argument("--think", action="store_true", help="--chat 时不封掉 think 段")
    ap.add_argument("--vocab", default=VOCAB_DEFAULT,
                    help="词表：随包的 tokenizer.json.gz，或一份 GGUF（给 llama-tokenize 用）")
    ap.add_argument("--tokenize-bin", default=None, help="llama-tokenize 的路径（缺省自己找，找不到用纯 Python）")
    ap.add_argument("--max-new", type=int, default=0, help="最多生成几个 token（0 = 把 128 的上下文用满）")
    ap.add_argument("--no-stop", action="store_true", help="遇到 <|im_end|> / <|endoftext|> 也不停")
    ap.add_argument("--feed-only", action="store_true",
                    help="调试：满 64 也不走 prefill，整段 prompt 逐个用 decode 喂（慢 64 倍，"
                         "用来把 prefill 路径与 decode 路径在同一块板上对照）")
    ap.add_argument("--no-zero", action="store_true", help="不清上一趟的残留")
    ap.add_argument("--check-weights", type=int, default=16, help="抽查几处权重（0 = 不查）")
    ap.add_argument("--load-weights", action="store_true", help="不抽查，直接把整份权重灌进 DRAM（18 分钟）")
    ap.add_argument("--bit", default=BIT_DEFAULT,
                    help="要烧的 bit（板上没烧 / 不是这个 SoC / 旧桥时）；缺省 prebuilt/bit/top_jtag_p3.bit")
    ap.add_argument("--program", action="store_true", help="不管板上是什么，先烧一遍 --bit")
    ap.add_argument("--prog-tool", choices=("pyjtag", "vivado"), default="pyjtag",
                    help="烧录走 pyjtag 的 program_bit（缺省）还是 vivado 的 tools/program.tcl")
    ap.add_argument("--no-auto", action="store_true", help="板子不对时只诊断，不烧 bit、不灌权重")
    ap.add_argument("--check-only", action="store_true", help="板子与权重查完（该修的修完）就退出，不生成")
    ap.add_argument("--max-wall", type=float, default=120.0, help="每轮的墙钟超时（秒）")
    ap.add_argument("--no-burst", action="store_true", help="不用 JTAG burst")
    ap.add_argument("--show-special", action="store_true", help="控制 token 也打印出来（放在〈〉里）")
    ap.add_argument("--dry-run", action="store_true", help="只分词、只算计划，不碰板子")
    ap.add_argument("--seed", type=int, default=0, help="抽查权重用的随机种子")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.file:
        text = open(a.file, encoding="utf-8").read()
    elif a.prompt is not None:
        text = a.prompt
    else:
        ap.error("要给一段话（位置参数或 --file）")
    if a.chat:
        text = chat_prompt(text, a.system, a.think)

    n_pre, n_ctx, c_base = RT.PREFILL_SEQ, RT.CTX_MAX, RT.TOKENS_BASE
    w = RT.weights()

    # ── 分词 ──
    tk = Tokenizer(a.vocab, a.tokenize_bin)
    ids = tk.encode(text)
    P = len(ids)
    how = f"llama-tokenize（{tk.tokenize_bin}）" if tk.tokenize_bin else "纯 Python BPE"
    print(f"── {tk.name}：prompt {P} 个 token（{how}）──")
    if a.verbose:
        print(f"    {ids}")
    if P == 0:
        sys.exit("prompt 分出来是空的")
    if P > n_ctx:
        sys.exit(f"prompt {P} 个 token，上下文上限 {n_ctx}（decode 的 n_seq_hi）")
    use_pre = P >= n_pre and not a.feed_only
    feed = list(range(n_pre + 1, P + 1)) if use_pre else list(range(1, P + 1))
    # decode(k) 写第 k 格：喂 prompt 时 k 走到 P 为止（decode(P) 算出来的才是第一个生成的）
    room = n_ctx - P + 1                 # decode(k) 最大到 k = n_ctx，写第 n_ctx 格
    n_new = room if a.max_new <= 0 else min(a.max_new, room)
    stops = set() if a.no_stop else {t for t in (tk.eos, tk.bos) if t is not None}
    plan = (f"prefill({n_pre}) + 逐个喂 {len(feed)} 个（decode n_seq = {n_pre + 1}..{P}）" if use_pre and feed
            else f"prefill({n_pre})" if use_pre
            else f"{'--feed-only' if a.feed_only else f'不满 {n_pre}'}，逐个喂 {P} 个（decode n_seq = 1..{P}）")
    print(f"    计划：{plan}，然后最多生成 {n_new} 个（上下文 {n_ctx}）；"
          f"停在 {sorted(stops) if stops else '不停'}")
    if a.dry_run:
        print("    --dry-run：到此为止")
        print(f"    还原：{tk.decode(ids)!r}")
        return 0

    # ── 板子 ──
    if a.no_burst:
        os.environ.pop("JTAG_BURST", None)
        os.environ.pop("JTAG_RBURST", None)
    else:
        os.environ.setdefault("JTAG_BURST", "1")
        os.environ.setdefault("JTAG_RBURST", "1")
    stop = Stopper()
    print("── 板子 ──")
    tr, programmed = open_board(a)
    bd = Board(tr)
    soc = bd.soc
    ensure_weights(a, bd, w, programmed, stop)
    if a.check_only:
        print("    --check-only：到此为止")
        tr.close()
        return 0

    # 启动镜像
    addr, img = RT.load_image()
    bd.put(addr, img)
    if bd.get(addr, len(img)) != img:
        sys.exit("装载镜像写进去读回来不一样")

    # token 串：prompt 放 [0, P)，后面到 n_ctx 清零
    tok_bytes = b"".join(int(t).to_bytes(4, "little") for t in ids)
    bd.put(c_base, tok_bytes + bytes(4 * (n_ctx - P)))
    if bd.get(c_base, 4 * P) != tok_bytes:
        sys.exit("token 串写进去读回来不一样")

    if not a.no_zero:
        t0 = time.time()
        blocks = RT.kv_blocks()
        n_slot = min(n_ctx, RT.KV_MAX_CTX)
        tot = 0
        for base, slot in blocks:
            bd.put(base, bytes(slot * n_slot))
            tot += slot * n_slot
        for base, nb in RT.ARENA:
            bd.put(base, bytes(nb))
            tot += nb
        print(f"    清零：{len(blocks) + len(RT.ARENA)} 段，"
              f"共 {tot / 2**20:.1f} MiB，{time.time() - t0:.0f} s")

    # ── 让 CPU 开始跑 ──
    host = RT.Host(soc, 0, wall_limit=a.max_wall)
    soc.cpu_wr(CPU_PERIPH_BASE + CPU_GP0 * 4, 0)      # 命令字不随核复位，清掉上一趟的
    soc.go(0)
    soc.go(1)
    st, ver, _ = host.wait_ready()
    if (st & 0x7F) != RT.ST_IDLE:
        sys.exit(f"CPU 报到状态不对：{st:#010x}")
    print(f"    CPU 报到，程序版本 {ver}")

    dec_out = codecs.getincrementaldecoder("utf-8")("replace")

    def emit(t):
        """流式打印一个 token。-v 时不流式（每步一行已经带了它），最后整段打一次。"""
        if a.verbose:
            return
        bts = tk.token_bytes(t)
        if bts is None:
            if a.show_special:
                sys.stdout.write(f"〈{tk.tokens[t]}〉")
        else:
            sys.stdout.write(dec_out.decode(bts))
        sys.stdout.flush()

    def step(sid, k, what):
        err, cyc, _, wall = host.round(sid, k)
        if err:
            raise RuntimeError(f"{what}：CPU 拒了这一轮（n_seq = {k}）")
        t = bd.get_i32(c_base + 4 * k)
        if a.verbose:
            print(f"    {what}：n_seq = {k} → token[{k}] = {t} {tk.decode([t])!r}（{cyc} 拍，{wall:.2f} s）")
        return t, wall

    # 1. 把 prompt 送进去。每一轮都把算出的 token 写到串上第 k 格，k < P 时那一格本该是
    #    prompt 自己的第 k 个 token，被盖掉了要立刻写回去，下一轮读的才是 prompt。
    t_feed0 = time.time()
    hits = guesses = 0
    pre_wall = 0.0
    nxt = None

    def took(k, got):
        nonlocal hits, guesses, nxt
        if k < P:
            guesses += 1
            hits += got == ids[k]
            bd.put_i32(c_base + 4 * k, ids[k])
        else:
            nxt = got

    if use_pre:
        got, pre_wall = step(RT.SID_PREFILL, n_pre, "prefill")
        took(n_pre, got)
        print(f"    prefill {n_pre} 个 token：{pre_wall:.2f} s")
    for k in feed:
        if stop.flag:
            break
        got, _ = step(RT.SID_DECODE, k, "喂 prompt" if k < P else "喂 prompt 末尾")
        took(k, got)
    if feed and not stop.flag:
        print(f"    逐个喂 {len(feed)} 个：{time.time() - t_feed0 - pre_wall:.1f} s")
    if guesses:
        print(f"    （吃 prompt 时片上 top1 猜中下一个 token {hits}/{guesses} 次）")

    # 2. 生成
    print("── 生成 ──")
    out_ids = []
    walls = []
    k = P
    if nxt is not None and not stop.flag:
        out_ids.append(nxt)
        emit(nxt)
    while not stop.flag and len(out_ids) < n_new and out_ids and out_ids[-1] not in stops and k < n_ctx:
        k += 1
        t, wall = step(RT.SID_DECODE, k, "decode")
        walls.append(wall)
        out_ids.append(t)
        emit(t)
    sys.stdout.write(dec_out.decode(b"", final=True))
    if a.verbose:
        sys.stdout.write(tk.decode([t for t in out_ids if a.show_special or not tk.is_control(t)]))
    print()
    if out_ids and out_ids[-1] in stops:
        why = "遇到停止 token"
    elif stop.flag:
        why = "Ctrl-C"
    elif a.max_new and len(out_ids) >= a.max_new:
        why = "到 --max-new"
    else:
        why = f"上下文 {n_ctx} 用满"
    print(f"── 生成 {len(out_ids)} 个 token（{why}），decode 平均 "
          f"{(sum(walls) / len(walls)) if walls else 0:.2f} s/步 ──")
    if a.verbose:
        print(f"    {out_ids}")

    # 3. 收工
    host.round(RT.SID_STOP)
    tr.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
