#!/usr/bin/env python
"""设备访问层：一根 AXI-Lite 读写板子或仿真。

    EthAxiTransport   到板子，经 1GbE
    SimSockTransport  到仿真，经 unix socket，另有直接读写 DRAM 模型的后门
    Soc               架在读写之上：控制寄存器、CPU、批量搬运 DRAM、状态
"""
import atexit
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GMP = os.path.dirname(HERE)                      # 发布包根目录
#   跑出来的东西（读回的原始字节、日志）都落在根目录的 out/ 下，不与发布的内容混在一起
BUILD = os.path.join(GMP, "out")
os.makedirs(BUILD, exist_ok=True)

sys.path.insert(0, HERE)
from hw_params import (WIN_CPU, WIN_XFER, WIN_XFER_BUF, WIN_STAT,  # noqa: E402
                       SOC_MAGIC, STAT_MAGIC, STAT_CYCLES, STAT_CPU_TRACE,  # SOC_MAGIC 转给 soc_generate
                       STAT_CPU_STATE, STAT_BOARD,
                       CPU_PERIPH_BASE, CPU_GO, HOST_PORT, HOST_BEAT)

def _hex(v):
    return f"0x{v & 0xFFFFFFFF:08x}"


# ══════════════════════════════════════════════════════════════════════════
# transport：read32 / write32
# ══════════════════════════════════════════════════════════════════════════

ETHAXI_BUILD = os.path.join(HERE, "ethaxi", "build")


class EthAxiTransport:
    """经 1GbE 读写板子，传输是 `host/ethaxi` 编出来的 ethaxi 模块。"""

    name = "eth"
    load_hint = "约 1 分钟"
    burst_ok = True
    reliable = True

    def __init__(self, ifname, window=16, rto_us=4000, verbose=True):
        sys.path.insert(0, ETHAXI_BUILD)
        try:
            import ethaxi
        except ImportError as e:
            raise IOError(f"导入不了 ethaxi 模块（先 make -C host/ethaxi module）：{e}")
        self.dev = ethaxi.Device()
        self.dev.open(ifname=ifname, window=window, rto_us=rto_us, verbose=verbose)

    def write32(self, addr, data):
        self.dev.write32(addr & 0xFFFFFFFF, data & 0xFFFFFFFF)

    def write32_many(self, items, verify=True):
        self.dev.write_many([(a & 0xFFFFFFFF, d & 0xFFFFFFFF) for a, d in items])

    def write_burst(self, addr, words):
        self.dev.write_burst(addr & 0xFFFFFFFF, [w & 0xFFFFFFFF for w in words])

    def read_burst(self, addr, n):
        return self.dev.read_burst(addr & 0xFFFFFFFF, n)

    def write_bytes(self, addr, data):
        self.dev.write_bytes(addr & 0xFFFFFFFF, data)

    def read_bytes(self, addr, n):
        return self.dev.read_bytes(addr & 0xFFFFFFFF, n)

    def read32(self, addr):
        return self.dev.read32(addr & 0xFFFFFFFF)

    def read32_many(self, addrs, gap=1):
        return self.dev.read_many([a & 0xFFFFFFFF for a in addrs])

    def close(self):
        self.dev.close()


# ══════════════════════════════════════════════════════════════════════════
# transport：仿真（`prebuilt/sim/` 的整机仿真，读写与上面那条同一套帧）
# ══════════════════════════════════════════════════════════════════════════

SIM_BIN = os.path.join(GMP, "prebuilt", "sim", "VSocCosimTop")
#   unix socket 的路径有 108 字节上限，所以放 /tmp 下；同时跑几份仿真时用 GMP_SIM_SOCK
#   给每份各一个，后起的那份会先删掉同名 socket
SIM_SOCK = os.environ.get("GMP_SIM_SOCK", "/tmp/gmp_soc_axi.sock")


class SimSockTransport:
    """把 read32 / write32 发给仿真进程，帧走 unix socket。

    仿真进程由本类拉起（工作目录是它自己那个目录，两份 `.hex` 从那里装入），`close`
    时收掉，它打印的东西落在 `out/` 下，文件名跟着 socket 走。

    除了板上那两个动作，它另有一条后门：直接读写 DRAM 模型里的存储，不占仿真拍。灌
    权重、清残留都走后门（那些字节要是一笔一笔经片上互联搬，一趟跑不完）。
    """

    name = "sim"
    load_hint = "几秒钟"                # 灌权重那句提示里的时间：后门不占仿真拍，只受磁盘与内存限制

    CHUNK = 1 << 20                     # 后门一帧最多这么多字节
    BATCH = 2048                        # 批量读写一次发这么多笔：请求全塞进缓冲会与响应互等

    def __init__(self, bin_path=None, sock_path=None, timeout=30.0):
        self.bin = os.path.abspath(bin_path or SIM_BIN)
        if not os.path.isfile(self.bin):
            raise IOError(f"没有仿真可执行文件 {self.bin}")
        self.sock_path = sock_path or SIM_SOCK
        if len(self.sock_path.encode()) > 100:
            raise IOError(f"socket 路径 {self.sock_path} 超出 unix socket 的 108 字节上限，"
                          "用 GMP_SIM_SOCK 换一个短的")
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        #   日志名跟着 socket 走：同时跑两份时两边的输出才不会写进同一个文件
        base = os.path.basename(self.sock_path)
        self.log = os.path.join(BUILD, "sim_" + (base[:-5] if base.endswith(".sock") else base) + ".log")
        self._logf = open(self.log, "wb")
        self.proc = subprocess.Popen([self.bin, self.sock_path], cwd=os.path.dirname(self.bin),
                                     stdout=self._logf, stderr=subprocess.STDOUT)
        atexit.register(self.close)
        t0 = time.time()
        while not os.path.exists(self.sock_path):
            if self.proc.poll() is not None:
                raise IOError(f"仿真起来就退了（exit {self.proc.returncode}），看 {self.log}")
            if time.time() - t0 > timeout:
                self.close()
                raise IOError(f"仿真 {timeout:.0f} 秒还没把 socket 建起来，看 {self.log}")
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)

    # ── 帧：请求 9 字节 [op][addr LE][data LE]，响应 4 字节；批量就是把请求接起来发 ──
    @staticmethod
    def _req(op, addr, data=0):
        return (bytes([ord(op)]) + (addr & 0xFFFFFFFF).to_bytes(4, "little")
                + (data & 0xFFFFFFFF).to_bytes(4, "little"))

    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                gone = "" if self.proc.poll() is None else f"（它已经退了，exit {self.proc.returncode}）"
                raise IOError(f"仿真那头把 socket 关了{gone}，看 {self.log}")
            buf += chunk
        return bytes(buf)

    def _words(self, n):
        b = self._recv(4 * n)
        return [int.from_bytes(b[4 * i:4 * i + 4], "little") for i in range(n)]

    def write32(self, addr, data):
        self.sock.sendall(self._req("W", addr, data))
        self._recv(4)

    def read32(self, addr):
        self.sock.sendall(self._req("R", addr))
        return self._words(1)[0]

    def write32_many(self, items, verify=True):
        items = list(items)
        for i in range(0, len(items), self.BATCH):
            part = items[i:i + self.BATCH]
            self.sock.sendall(b"".join(self._req("W", a, d) for a, d in part))
            self._recv(4 * len(part))

    def read32_many(self, addrs, gap=1):
        addrs = list(addrs)
        out = []
        for i in range(0, len(addrs), self.BATCH):
            part = addrs[i:i + self.BATCH]
            self.sock.sendall(b"".join(self._req("R", a) for a in part))
            out += self._words(len(part))
        return out

    # ── 后门：直接读写 DRAM 模型的存储，不占仿真拍（板上没有这条） ──
    def dram_preload(self, addr, data):
        """把 bytes 放进 DRAM 模型，地址是系统字节地址，长度与对齐随意。"""
        for off in range(0, len(data), self.CHUNK):
            part = data[off:off + self.CHUNK]
            self.sock.sendall(self._req("P", addr + off, len(part)) + part)
            if self._words(1)[0] != len(part):
                raise IOError("DRAM 后门写：回的字节数对不上")

    def dram_peek(self, addr, nbytes):
        out = bytearray()
        for off in range(0, nbytes, self.CHUNK):
            n = min(self.CHUNK, nbytes - off)
            self.sock.sendall(self._req("G", addr + off, n))
            if self._words(1)[0] != n:
                raise IOError("DRAM 后门读：回的字节数对不上")
            out += self._recv(n)
        return bytes(out)

    def cycles(self):
        """仿真打到第几拍（板上那条是从 STAT 窗口读的拍计数）。"""
        self.sock.sendall(self._req("T", 0) + self._req("U", 0))
        lo, hi = self._words(2)
        return lo | (hi << 32)

    def close(self):
        """关 socket、收掉仿真进程。重复调用没有副作用（退出时 atexit 还会调一次）。"""
        sock = getattr(self, "sock", None)
        if sock is not None:
            self.sock = None
            try:
                sock.close()
            except OSError:
                pass
        proc = getattr(self, "proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        logf = getattr(self, "_logf", None)
        if logf is not None and not logf.closed:
            logf.close()
        path = getattr(self, "sock_path", None)
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


# ══════════════════════════════════════════════════════════════════════════
# 设备层
# ══════════════════════════════════════════════════════════════════════════

class Soc:
    """几个窗口 + DRAM 批量搬运。"""

    def __init__(self, tr):
        self.tr = tr
        self.lock = threading.Lock()

    # ── 窗口 ──
    def cpu_wr(self, addr, val):
        self.tr.write32(WIN_CPU << 24 | (addr & 0xFFFFFF), val)

    def cpu_rd(self, addr):
        return self.tr.read32(WIN_CPU << 24 | (addr & 0xFFFFFF))

    def go(self, val):
        """让 CPU 开始跑（先写 0 再写 1，它要的是释放沿）。"""
        self.cpu_wr(CPU_PERIPH_BASE + CPU_GO * 4, val)

    def stat_rd(self, num):
        return self.tr.read32(WIN_STAT << 24 | (num << 2))

    def hp_wr(self, num, val):
        self.tr.write32(WIN_XFER << 24 | (num << 2), val)

    def hp_rd(self, num):
        return self.tr.read32(WIN_XFER << 24 | (num << 2))

    def buf_wr(self, widx, val):
        self.tr.write32(WIN_XFER_BUF << 24 | (widx << 2), val)

    def buf_fill(self, words):
        """把 words 写进数据缓冲的 0..len-1。"""
        many = getattr(self.tr, "write32_many", None)
        if many is None:
            for i, v in enumerate(words):
                self.buf_wr(i, v)
            return
        # 缓冲是一段连续地址，通路支持整段写时一次灌完
        burst = getattr(self.tr, "write_burst", None) if getattr(self.tr, "burst_ok", False) else None
        if burst is not None:
            burst(WIN_XFER_BUF << 24, list(words))
            return
        many([(WIN_XFER_BUF << 24 | (i << 2), v) for i, v in enumerate(words)])

    def buf_rd(self, widx):
        return self.tr.read32(WIN_XFER_BUF << 24 | (widx << 2))

    # ── DRAM 批量搬运 ──
    #   数据缓冲 16 KiB，按 beat（`HOST_BEAT`）计格；一条命令最多 255 beat
    BUF_BEATS = (16 << 10) // HOST_BEAT
    CMD_BEATS = min(255, BUF_BEATS)

    @staticmethod
    def _row_beats(addr, nbytes):
        return ((addr % HOST_BEAT) + nbytes + HOST_BEAT - 1) // HOST_BEAT

    @staticmethod
    def _split(addr, nbytes):
        """把 [addr, addr+nbytes) 切成每条 ≤ CMD_BEATS beat 的子命令。"""
        subs, off, a, left = [], 0, addr, nbytes
        while left > 0:
            room = Soc.CMD_BEATS * HOST_BEAT - (a % HOST_BEAT)   # 这条从 a 起最多能覆盖的字节数
            n = min(left, room)
            beats = Soc._row_beats(a, n)
            subs.append((a, n, beats, off))
            off += beats
            a += n
            left -= n
        assert off <= Soc.BUF_BEATS, f"跨度 {nbytes} B @{_hex(addr)} 超出 16 KiB 缓冲"
        return subs

    def _cmd_items(self, direction, addr, nbytes, off, rows=1, stride=0):
        nb = rows * self._row_beats(addr, nbytes)
        assert 0 < nb <= self.CMD_BEATS
        return [(1, addr & 0xFFFFFFFF),
                (2, (addr >> 32) & 0xFFFF),
                (3, nbytes),
                (4, rows),
                (5, stride),
                (6, (nb & 0xFF) << 24 | (off & 0xFF) << 16 | (0 << 8)
                    | (direction & 1) << 4 | HOST_PORT),
                (7, 1)]                                    # PUSH

    def _cmds_go(self, direction, subs):
        """清上一趟的完成位 + 若干条命令 + go，一批发出去。"""
        regs = [(0, 2)]
        for a, n, _beats, off in subs:
            regs += self._cmd_items(direction, a, n, off)
        regs.append((0, 1))                                # go
        many = getattr(self.tr, "write32_many", None)
        if many is None:
            for n, v in regs:
                self.hp_wr(n, v)
            return
        many([(WIN_XFER << 24 | (n << 2), v) for n, v in regs], verify=False)

    def _run(self, limit=4000, sent_go=False):
        """发 go 再等完成。"""
        st_addr = WIN_XFER << 24 | 0
        many = getattr(self.tr, "read32_many", None)
        if not sent_go:
            self.hp_wr(0, 1)                               # go
        if many is None:
            for _ in range(limit):
                if self.hp_rd(0) & 2:                      # done
                    return True
            self.hp_wr(0, 2)
            return False
        n = 0
        while n < limit:
            k = min(8, limit - n)
            if any(v & 2 for v in many([st_addr] * k)):
                return True
            n += k
        self.hp_wr(0, 2)
        return False

    _DIRTY = b"".join((0xDEAD0000 + i).to_bytes(4, "little") for i in range(4096))

    def dram_write(self, addr, data):
        """data 是 bytes（4 字节对齐，≤ 16 KiB），起始地址 `HOST_BEAT` 字节对齐。"""
        assert len(data) % 4 == 0 and len(data) <= 16 << 10
        assert addr % HOST_BEAT == 0, f"dram_write 的起始地址要 {HOST_BEAT} 字节对齐，给的是 {_hex(addr)}"
        wb = getattr(self.tr, "write_bytes", None)
        if wb is not None:
            wb(WIN_XFER_BUF << 24, data)
        else:
            self.buf_fill([int.from_bytes(data[4 * i:4 * i + 4], "little")
                           for i in range(len(data) // 4)])
        self._cmds_go(1, self._split(addr, len(data)))
        if not self._run(sent_go=True):
            raise IOError(f"DRAM 写 {_hex(addr)} 超时")

    def dram_read(self, addr, nbytes):
        """读回 [addr, addr+nbytes)，起始地址不必对齐。"""
        assert nbytes % 4 == 0 and nbytes <= 16 << 10
        subs = self._split(addr, nbytes)
        nwords = (subs[-1][3] + subs[-1][2]) * (HOST_BEAT // 4)   # 缓冲里实际占到的字数
        wb = getattr(self.tr, "write_bytes", None)
        rb = getattr(self.tr, "read_bytes", None)
        if wb is not None:
            wb(WIN_XFER_BUF << 24, self._DIRTY[:4 * nwords])
        else:
            self.buf_fill([0xDEAD0000 + i for i in range(nwords)])
        self._cmds_go(0, subs)
        if not self._run(sent_go=True):
            raise IOError(f"DRAM 读 {_hex(addr)} 超时")
        if rb is not None:
            raw = rb(WIN_XFER_BUF << 24, 4 * nwords)
        else:
            burst = getattr(self.tr, "read_burst", None) if getattr(self.tr, "burst_ok", False) else None
            if burst is not None:
                vals = burst(WIN_XFER_BUF << 24, nwords)
            else:
                many = getattr(self.tr, "read32_many", None)
                if many is None:
                    vals = [self.buf_rd(i) for i in range(nwords)]
                else:
                    vals = many([WIN_XFER_BUF << 24 | (i << 2) for i in range(nwords)])
            raw = b"".join(v.to_bytes(4, "little") for v in vals)
        out = b""
        for a, k, _beats, off in subs:
            start = off * HOST_BEAT + (a % HOST_BEAT)
            out += raw[start:start + k]
        return out

    # ── 状态 ──
    def snapshot(self):
        return {"magic": _hex(self.stat_rd(STAT_MAGIC)), "tick": self.stat_rd(STAT_CYCLES),
                "calib": self.stat_rd(STAT_BOARD) & 1,
                "cpu_state": self.stat_rd(STAT_CPU_STATE),
                "trace": [_hex(self.stat_rd(STAT_CPU_TRACE + i)) for i in range(4)]}

