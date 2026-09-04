#!/usr/bin/env python
"""跑这份产物要的三样：读权重、在 DRAM 上搬字节、与板上 CPU 逐轮通讯。

产物是 `data/` 下的两个文件：`weights.npz`（每个数组的名字就是它落在 DRAM 的地址）与
`load_image.bin`（板子上电后照着自装载的镜像）。地址与轮次口径写在下面这组常量里。
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hw_params import CPU_PERIPH_BASE, CPU_GP0, IMAGE_BASE          # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
WEIGHTS_NPZ = os.path.join(DATA, "weights.npz")
IMAGE_BIN = os.path.join(DATA, "load_image.bin")

# ══ 这份产物在 DRAM 上的落位 ══════════════════════════════════════════════
WEIGHTS_LO, WEIGHTS_HI = 0x0005000000, 0x002AC06000     # 权重区
WEIGHTS_BYTES = WEIGHTS_HI - WEIGHTS_LO                 # 灌权重时按这个数算进度
TOKENS_BASE = 0x0031C06000                              # token 串，每格 4 字节

# KV cache：28 层，每层 K 与 V 各一块
KV_BASE, KV_LAYERS = 0x002AC06000, 28
KV_LAYER_STRIDE, KV_KV_GAP = 0x400000, 0x200000
KV_SLOT, KV_MAX_CTX = 2048, 1024                        # 一格的字节数、最多几格

# 中间张量里每轮要清掉的几块：(地址, 字节数)
ARENA = [(0x0032006000, 2048), (0x0032006800, 2048), (0x0032007000, 4096),
         (0x0032007800, 6144), (0x0032008000, 2048), (0x0032008800, 2048),
         (0x0032009000, 4096), (0x003200A800, 6144)]

# ══ 与 CPU 的口径 ═════════════════════════════════════════════════════════
SID_STOP, SID_PREFILL, SID_DECODE = 0, 1, 2
PREFILL_SEQ = 64                # prefill 一轮固定处理这么多 token
CTX_MAX = 128                   # decode 的 n_seq 上限，也是 prompt 加生成的总上限

GP_CMD, GP_NSEQ, GP_STAT, GP_VER, GP_CYC = 0, 1, 16, 17, 18
ST_IDLE, ST_BUSY, ST_ERR = 1, 2, 0x80


def _hex(v):
    return f"0x{v:08x}"


def load_image(path=IMAGE_BIN):
    """启动镜像：(装进 DRAM 的地址, 字节)。"""
    return IMAGE_BASE, open(path, "rb").read()


def weights(path=WEIGHTS_NPZ):
    """打开权重包，`.files` 是全部条目名。"""
    return np.load(path)


def entry_addr(name):
    """条目名 `in_0x0005000000` → 它在 DRAM 里的地址。"""
    return int(name.split("_", 1)[1], 16)


def kv_blocks():
    """KV cache 每层 K 与 V 各一块：[(起始地址, 一格字节数), ...]。"""
    out = []
    for i in range(KV_LAYERS):
        base = KV_BASE + i * KV_LAYER_STRIDE
        out += [(base, KV_SLOT), (base + KV_KV_GAP, KV_SLOT)]
    return out


_CHUNK = 8 << 10


def dram_put(tr, soc, addr, data):
    if addr % 64 or len(data) % 64:
        # 搬运按 64 字节整块走，写不进块内的一截：把首尾那两个不完整的块先读回来、
        # 改掉要改的几个字节、再整块写回去。
        lo = addr & ~0x3F
        hi = (addr + len(data) + 63) & ~0x3F
        buf = bytearray(dram_get(tr, soc, lo, hi - lo))
        buf[addr - lo:addr - lo + len(data)] = data
        addr, data = lo, bytes(buf)
    pad = (-len(data)) % 4
    if pad:
        data = data + b"\0" * pad
    t0, done = time.time(), 0
    for off in range(0, len(data), _CHUNK):
        soc.dram_write(addr + off, data[off:off + _CHUNK])
        done += min(_CHUNK, len(data) - off)
        if done % (1 << 20) < _CHUNK and done >= 1 << 20:
            print(f"    … 已灌 {done >> 20} MiB（{done / 1024 / max(time.time() - t0, 1e-9):.0f} KB/s）")


def dram_get(tr, soc, addr, nbytes):
    n4 = nbytes + ((-nbytes) % 4)
    out = bytearray()
    for off in range(0, n4, _CHUNK):
        out += soc.dram_read(addr + off, min(_CHUNK, n4 - off))
    return bytes(out[:nbytes])


class Host:
    """与板上 CPU 逐轮通讯。"""

    def __init__(self, soc, max_cycles, wall_limit=None):
        self.soc = soc
        self.tr = soc.tr
        self.max_cycles = max_cycles
        self.wall_limit = wall_limit          # 秒，一轮超过这么久就算超时
        self.rnd = 0

    def _clk(self):
        return time.time() if self.wall_limit is not None else self.tr.cycles()

    def _expired(self, t0):
        if self.wall_limit is not None:
            return time.time() - t0 > self.wall_limit
        return self.tr.cycles() - t0 > self.max_cycles

    def gp_wr(self, i, v):
        self.soc.cpu_wr(CPU_PERIPH_BASE + (CPU_GP0 + i) * 4, v)

    def gp_rd(self, i):
        return self.soc.cpu_rd(CPU_PERIPH_BASE + (CPU_GP0 + i) * 4)

    def wait_ready(self):
        t0 = self._clk()
        while True:
            st = self.gp_rd(GP_STAT)
            if st != 0:
                break
            if self._expired(t0):
                raise TimeoutError("等 CPU 报到超时（gp16 一直是 0）")
        return st, self.gp_rd(GP_VER), self._clk() - t0

    def round(self, sid, n_seq=0):
        """一轮：返回 (出错位, CPU 报的拍数, 保留位, 墙钟秒)。"""
        self.rnd = (self.rnd + 1) & 0xFFFF
        rnd = self.rnd
        self.gp_wr(GP_NSEQ, n_seq)
        c0, w0 = self._clk(), time.time()
        self.gp_wr(GP_CMD, (sid & 0xFFFF) | (rnd << 16))
        while True:
            st = self.gp_rd(GP_STAT)
            if (st >> 16) == rnd and (st & 0x7F) == ST_IDLE:
                break
            if self._expired(c0):
                try:
                    print(f"    超时快照：{self.soc.snapshot()}")
                except Exception as e:
                    print(f"    超时快照失败：{e}")
                raise TimeoutError(f"session {sid} 第 {rnd} 轮超时：gp16 = {_hex(st)}")
        sim = 0 if self.wall_limit is not None else self.tr.cycles() - c0
        return st & ST_ERR, self.gp_rd(GP_CYC), sim, time.time() - w0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
