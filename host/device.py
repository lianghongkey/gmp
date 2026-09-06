#!/usr/bin/env python
"""设备访问层：一根 AXI-Lite 读写板子或仿真。

    BscanAxiTransport   到板子的通路，经 JTAG，只有 read32 / write32 两个动作
    SimSockTransport    到仿真的通路，经 unix socket，同样那两个动作，另有一条直接
                        读写 DRAM 模型的后门
    Soc                 架在那对读写之上：读写控制寄存器、读写 CPU、批量搬运 DRAM、
                        读状态

编号在 `hw_params.py`。
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
                       CPU_PERIPH_BASE, CPU_GO, HOST_PORT)

def _hex(v):
    return f"0x{v & 0xFFFFFFFF:08x}"


# ══════════════════════════════════════════════════════════════════════════
# transport：read32 / write32
# ══════════════════════════════════════════════════════════════════════════

class BscanAxiTransport:
    """经 `tools/pyjtag` 读写板子。"""

    name = "xpc"
    load_hint = "约 18 分钟"            # 灌权重那句提示里的时间，按线缆实测 0.55 MB/s 算

    TCK_MODE = None                     # None = 用 pyjtag 缺省档

    def __init__(self, firmware=None, tck_mode=None):
        jtag_dir = os.path.join(GMP, "tools")
        sys.path.insert(0, os.path.abspath(jtag_dir))
        from pyjtag import XpcCable, Tap, BscanAxi
        mode = tck_mode if tck_mode is not None else self.TCK_MODE
        kw = {} if mode is None else {"tck_mode": mode}
        self.cable = XpcCable(firmware=firmware, **kw)
        self._axi = BscanAxi(Tap(self.cable))

    def write32(self, addr, data):
        self._axi.write32(addr, data)

    def write32_many(self, items, verify=True):
        self._axi.write32_many(items, verify=verify)

    def write_burst(self, addr, words):
        """连续写一段连续地址。"""
        self._axi.write_burst(addr, words)

    def read_burst(self, addr, n):
        """连续读一段连续地址。"""
        return self._axi.read_burst(addr, n)

    def read32(self, addr):
        return self._axi.read32(addr)

    def read32_many(self, addrs, gap=1):
        """`gap` 参数不再用，排帧由下面这层自己做。"""
        return self._axi.read32_many(addrs)

    def close(self):
        self.cable.close()


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
        """`verify` 是 JTAG 那条要的参数，仿真这边不写完再读一遍。"""
        items = list(items)
        for i in range(0, len(items), self.BATCH):
            part = items[i:i + self.BATCH]
            self.sock.sendall(b"".join(self._req("W", a, d) for a, d in part))
            self._recv(4 * len(part))

    def read32_many(self, addrs, gap=1):
        """`gap` 同样是 JTAG 那条的参数，这里没有它的事。"""
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
        """把 words 写进数据缓冲的 0..len-1，打成一批发出去。"""
        many = getattr(self.tr, "write32_many", None)
        if many is None:
            for i, v in enumerate(words):
                self.buf_wr(i, v)
            return
        # 通路支持 burst 时一次灌完，用环境变量显式启用
        burst = getattr(self.tr, "write_burst", None) if os.environ.get("JTAG_BURST") else None
        if burst is not None:
            burst(WIN_XFER_BUF << 24, list(words))
            return
        many([(WIN_XFER_BUF << 24 | (i << 2), v) for i, v in enumerate(words)])

    def buf_rd(self, widx):
        return self.tr.read32(WIN_XFER_BUF << 24 | (widx << 2))

    # ── DRAM 批量搬运 ──
    @staticmethod
    def _row_beats(addr, nbytes):
        return ((addr & 0x3F) + nbytes + 63) // 64

    def _cmd(self, direction, addr, nbytes, rows=1, stride=0, off=0, go=False):
        """下一条搬运命令，`go=True` 时把启动也并进同一批。"""
        nb = rows * self._row_beats(addr, nbytes)
        regs = [(1, addr & 0xFFFFFFFF),
                (2, (addr >> 32) & 0xFFFF),
                (3, nbytes),
                (4, rows),
                (5, stride),
                (6, (nb & 0xFF) << 24 | (off & 0xFF) << 16 | (0 << 8)
                    | (direction & 1) << 4 | HOST_PORT),
                (7, 1)]                                    # PUSH
        many = getattr(self.tr, "write32_many", None)
        if many is None:
            for n, v in regs:
                self.hp_wr(n, v)
            if go:
                self.hp_wr(0, 1)
            return
        items = [(WIN_XFER << 24 | (n << 2), v) for n, v in regs]
        if go:
            items.append((WIN_XFER << 24 | 0, 1))
        many(items, verify=False)

    def _run(self, limit=4000, sent_go=False):
        """发 go 再等完成（`sent_go=True` 表示 go 已经并在命令批里发过了）。"""
        st_addr = WIN_XFER << 24 | 0
        many = getattr(self.tr, "read32_many", None)
        if many is None:
            if not sent_go:
                self.hp_wr(0, 1)                           # go
            for _ in range(limit):
                if self.hp_rd(0) & 2:                      # done
                    self.hp_wr(0, 2)                       # clear
                    return True
            self.hp_wr(0, 2)
            return False
        if not sent_go:
            self.hp_wr(0, 1)
        n = 0
        while n < limit:
            k = min(8, limit - n)
            if any(v & 2 for v in many([st_addr] * k)):
                self.hp_wr(0, 2)
                return True
            n += k
        self.hp_wr(0, 2)
        return False

    def dram_write(self, addr, data):
        """data 是 bytes（4 字节对齐，≤ 16 KiB）。搬运按 64 字节整块走，所以起始
        地址必须 64 字节对齐。
        """
        assert len(data) % 4 == 0 and len(data) <= 16 << 10
        assert addr % 64 == 0, f"dram_write 的起始地址要 64 字节对齐，给的是 {_hex(addr)}"
        self.buf_fill([int.from_bytes(data[4 * i:4 * i + 4], "little")
                       for i in range(len(data) // 4)])
        self._cmd(1, addr, len(data), go=True)
        if not self._run(sent_go=True):
            raise IOError(f"DRAM 写 {_hex(addr)} 超时")

    def dram_read(self, addr, nbytes):
        """读回 [addr, addr+nbytes)，起始地址不必 64 对齐，缓冲开头多出来的字节由
        本函数跳过。
        """
        assert nbytes % 4 == 0 and nbytes <= 16 << 10
        head = addr & 0x3F                      # 缓冲开头要跳过这么多
        span = head + nbytes                    # 缓冲里实际要取的范围
        assert span <= 16 << 10, "非对齐读的跨度超出缓冲"
        nwords = (span + 3) // 4
        # 先在缓冲里填一串无关的值，读回来才分得清哪些是真取到的数据
        self.buf_fill([0xDEAD0000 + i for i in range(nwords)])
        self._cmd(0, addr, nbytes, go=True)
        if not self._run(sent_go=True):
            raise IOError(f"DRAM 读 {_hex(addr)} 超时")
        n = nwords
        # 读 burst 与写 burst 各有各的开关
        rburst = getattr(self.tr, "read_burst", None) if os.environ.get("JTAG_RBURST") else None
        if rburst is not None:
            vals = rburst(WIN_XFER_BUF << 24, n)
            return b"".join(v.to_bytes(4, "little") for v in vals)[head:head + nbytes]
        many = getattr(self.tr, "read32_many", None)
        if many is None:
            return b"".join(self.buf_rd(i).to_bytes(4, "little")
                            for i in range(n))[head:head + nbytes]
        vals = many([WIN_XFER_BUF << 24 | (i << 2) for i in range(n)])
        return b"".join(v.to_bytes(4, "little") for v in vals)[head:head + nbytes]

    # ── 状态 ──
    def snapshot(self):
        return {"magic": _hex(self.stat_rd(STAT_MAGIC)), "tick": self.stat_rd(STAT_CYCLES),
                "calib": self.stat_rd(STAT_BOARD) & 1,
                "cpu_state": self.stat_rd(STAT_CPU_STATE),
                "trace": [_hex(self.stat_rd(STAT_CPU_TRACE + i)) for i in range(4)]}

