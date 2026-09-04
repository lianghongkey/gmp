"""JTAG TAP 状态机（IEEE 1149.1），建立在 XpcCable.jtag_shift 之上。

只实现例子需要的最小集合：Test-Logic-Reset、Run-Test/Idle、IR/DR 扫描。
所有扫描进出都停在 Run-Test/Idle（RTI）—— 简化状态跟踪，扫描间隔
增加的几个 TCK 对本应用毫无影响。

单器件链假设（本板 JTAG 链上只有一颗 xc7k480t）：IR 长 6 位，DR 前后
无别的器件占位。多器件链需要在扫描前后补 BYPASS 位，此处不做。
"""

MAX_SHIFT_BITS = 8191          # 与 xpc.py 的一次 A6 传输上限一致


try:
    import numpy as np
except ImportError:
    np = None

from math import gcd

from .xpc import MAX_SHIFT_BITS


class Tap:
    def __init__(self, cable):
        self.cable = cable
        self.reset()

    # ── 状态导航 ────────────────────────────────────────────────────────
    def reset(self):
        """TMS=1 ×5 → Test-Logic-Reset，再 TMS=0 → RTI。"""
        self.cable.jtag_shift([1, 1, 1, 1, 1, 0], [0] * 6)

    def run_test(self, n):
        """在 RTI 停留 n 个 TCK（JSTART 之后需要）。"""
        self.cable.jtag_shift([0] * n, [0] * n)

    # ── 扫描原语 ────────────────────────────────────────────────────────
    def _scan(self, to_shift_tms, bits_in, read):
        """进入 Shift-IR/DR → 移位（末位带 TMS=1 → Exit1）→ Update → RTI。

        bits_in: 位列表（LSB 先出）。返回 TDO 位列表（read 时）。
        """
        n = len(bits_in)
        # RTI → Select-DR(-Scan)[→ Select-IR] → Capture → Shift
        self.cable.jtag_shift(to_shift_tms, [0] * len(to_shift_tms))
        # Shift 状态里移 n 位；最后一位 TMS=1 同时退到 Exit1
        tms = [0] * (n - 1) + [1]
        tdo = self.cable.jtag_shift(tms, list(bits_in), read=read)
        # Exit1 → Update → RTI
        self.cable.jtag_shift([1, 0], [0, 0])
        return tdo

    def ir_scan(self, value, nbits=6, read=False):
        """IR 扫描（7 系列单器件 IR=6 位）。返回 IR capture 值（read 时）。"""
        bits = [(value >> i) & 1 for i in range(nbits)]
        tdo = self._scan([1, 1, 0, 0], bits, read)
        if read:
            return sum(b << i for i, b in enumerate(tdo))
        return None

    def dr_scan_many(self, values, nbits, read=False):
        """连续做 len(values) 次 DR 扫描，尽量少的 USB 事务。

        每次扫描的位序列是「导航 3 位 + 移位 nbits 位 + 退出 2 位」，逐位的 TMS/TDI
        由这里自己排；把多帧首尾相接拼成一条长序列，一次 A6 传输打完。逐帧调
        `dr_scan` 是每帧 3 次 USB 往返，这里是每 (8191 // (nbits+5)) 帧 1 次。

        TMS/TDI 直接按大整数拼（每帧几次移位，O(帧数)），再按 nibble 摊进 payload
        （O(位数/4)）—— 不走 `jtag_shift` 的逐位列表。USB 是高速 480 Mbps，一次
        4 KB payload 只要 0.07ms，真正费时的是主机侧这两层 O(位数) 的 Python 循环。

        read 时全程采样，再按位置切出每帧移位段的 TDO，返回整数列表。
        """
        per = nbits + 5
        cap = MAX_SHIFT_BITS // per                # 一次传输装得下几帧
        out = []
        for base in range(0, len(values), cap):
            group = values[base:base + cap]
            # ── TMS / TDI 各拼成一个大整数（LSB 先出）──
            tms_i = tdi_i = 0
            pos = 0
            for v in group:
                tms_i |= 0b001 << pos              # RTI → Select-DR → Capture → Shift
                pos += 3
                tdi_i |= v << pos                  # 移位段的数据
                tms_i |= 1 << (pos + nbits - 1)    # 末位同时退到 Exit1
                pos += nbits
                tms_i |= 0b01 << pos               # Exit1 → Update → RTI
                pos += 2
            n = pos
            nbits_tx = n + 1 if n % 4 == 0 else n  # 4 整倍数要补个不打 TCK 的哑位
            ngrp = (nbits_tx + 3) // 4
            nby = (n + 7) // 8
            tb = tms_i.to_bytes(nby, "little")
            db = tdi_i.to_bytes(nby, "little")
            words = bytearray(2 * ngrp)
            clk = 0xFF if read else 0x0F           # 低 nibble = TCK，高 nibble = 采样 TDO
            for g in range((n + 3) // 4):
                bi, half = divmod(g, 2)
                sh = 4 * half
                words[2 * g] = ((db[bi] >> sh) & 0xF) | (((tb[bi] >> sh) & 0xF) << 4)
                words[2 * g + 1] = clk
            rem = n % 4                            # 末组只对有效位打 TCK —— 多打会
            if rem:                                # 多移几位，把状态机和数据一起带偏
                m = (1 << rem) - 1
                words[2 * (n // 4) + 1] = m | ((m << 4) if read else 0)
            outb = self.cable._shift_raw(nbits_tx, bytes(words), n if read else 0)
            if read:
                bits = [(outb[i // 8] >> (i % 8)) & 1 for i in range(n)]
                for k in range(len(group)):
                    seg = bits[k * per + 3:k * per + 3 + nbits]
                    out.append(sum(b << i for i, b in enumerate(seg)))
        return out if read else None

    def dr_scan_bits(self, bits_in, read=False):
        """DR 扫描，位列表进/出（LSB 先）。"""
        return self._scan([1, 0, 0], list(bits_in), read)

    def dr_scan(self, value, nbits, read=False):
        """DR 扫描，整数进/出。"""
        bits = [(value >> i) & 1 for i in range(nbits)]
        tdo = self.dr_scan_bits(bits, read)
        if read:
            return sum(b << i for i, b in enumerate(tdo))
        return None

    def dr_scan_bytes(self, data, read=False):
        """DR 扫描，bytes 进（每字节 bit0 先出）/ bytes 出。大块数据用。"""
        bits = []
        for byte in data:
            for i in range(8):
                bits.append((byte >> i) & 1)
        tdo = self.dr_scan_bits(bits, read)
        if not read:
            return None
        out = bytearray(len(data))
        for i, b in enumerate(tdo):
            if b:
                out[i // 8] |= 1 << (i % 8)
        return bytes(out)

    # ── 批量 DR 扫描 ────────────────────────────────────────────────────
    #
    # 逐笔调 dr_scan 时，一次扫描被 _scan 拆成三次 jtag_shift（导航 3 位、
    # 移位 N 位、退出 2 位），每次一趟 USB 往返。一帧 66 位的事务因此要 7 次
    # USB 事务，而真正花在 TCK 上的只有 71 拍 —— 6 MHz 下 12 µs，USB 往返却是
    # 每次 ~180 µs。批量把整批帧的 TMS/TDI/采样掩码拼成一条位流，一次传输打完。
    FRAME_OVERHEAD = 5        # 导航 3 位（RTI→Select→Capture→Shift）+ 退出 2 位

    def dr_scan_batch(self, values, nbits, nsample=None):
        """连续做 len(values) 次 DR 扫描，返回每次移出的整数。

        每帧 RTI → Shift-DR → 移 nbits 位 → Update-DR → RTI，与逐笔
        dr_scan 走的状态序列完全一致，区别只在于打包成一次 USB 传输。

        nsample 只采每帧移出的低 nsample 位（默认全采）。采 TDO 每位约
        0.86 µs，不采只要 0.19 µs，所以只取真正要用的位能明显提速 ——
        BscanAxi 的写只关心 DONE/ERR 两位，读只关心低 34 位。

        ⚠ 固件约束：一次传输里采样位总数必须是 16 的倍数，否则末尾那段会
        错位（实测 K=66 时只有 47/66 对得上，K=64 全对，与采样段起始位置无关）。
        本函数据此挑每批帧数；调用方给的 nsample 需能整除出这个条件。
        """
        if nsample is None:
            nsample = nbits
        frame = nbits + self.FRAME_OVERHEAD
        if nsample == 0:
            # 一位都不采：固件走「纯写」快路径（5.21 MHz vs 采样时的 1.16 MHz）。
            # 只要传输里有任何一位采样，整段就按慢路径跑，与采样多少无关 ——
            # 实测每帧采 16 / 48 / 66 位耗时一模一样，所以省采样只有全省才有意义。
            for base in range(0, len(values), max(1, MAX_SHIFT_BITS // frame)):
                chunk = values[base:base + max(1, MAX_SHIFT_BITS // frame)]
                self._dr_batch(list(chunk), nbits, frame, 0)
            return [None] * len(values)
        per = max(1, MAX_SHIFT_BITS // frame)
        step = 16 // gcd(nsample, 16)            # 帧数取 step 的倍数，K 才是 16 的倍数
        per = max(step, (per // step) * step)
        out = []
        for base in range(0, len(values), per):
            chunk = values[base:base + per]
            pad = (-len(chunk)) % step           # 末批补空帧凑齐，结果丢弃
            out.extend(self._dr_batch(list(chunk) + [0] * pad, nbits, frame,
                                      nsample)[:len(chunk)])
        return out

    def _dr_batch(self, chunk, nbits, frame, nsample):
        m = len(chunk)
        total = m * frame
        if np is not None:
            tms = np.zeros(total, dtype=np.uint8)
            tdi = np.zeros(total, dtype=np.uint8)
            smp = np.zeros(total, dtype=np.uint8)
            nbytes = (nbits + 7) // 8
            for k, v in enumerate(chunk):
                o = k * frame
                tms[o] = 1                        # RTI → Select-DR-Scan
                tms[o + 2 + nbits] = 1            # 末位数据同时退到 Exit1-DR
                tms[o + 3 + nbits] = 1            # Exit1 → Update-DR
                smp[o + 3:o + 3 + nsample] = 1    # 只采要用的低 nsample 位
                tdi[o + 3:o + 3 + nbits] = np.unpackbits(
                    np.frombuffer(int(v).to_bytes(nbytes, "little"), dtype=np.uint8),
                    bitorder="little")[:nbits]
        else:
            tms = [0] * total; tdi = [0] * total; smp = [0] * total
            for k, v in enumerate(chunk):
                o = k * frame
                tms[o] = 1
                tms[o + 2 + nbits] = 1
                tms[o + 3 + nbits] = 1
                for i in range(nbits):
                    if i < nsample:
                        smp[o + 3 + i] = 1
                    tdi[o + 3 + i] = (v >> i) & 1

        raw = self.cable.shift_batch(tms, tdi, smp)

        if np is not None:
            bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")
            return [int.from_bytes(
                        np.packbits(bits[k * nsample:(k + 1) * nsample],
                                    bitorder="little").tobytes(), "little")
                    for k in range(m)]
        return [sum(((raw[(k * nsample + i) // 8] >> ((k * nsample + i) % 8)) & 1) << i
                    for i in range(nsample)) for k in range(m)]

    def dr_scan_long(self, data, nbits, nsample=0):
        """一次 DR 扫描移 nbits 位（burst 数据帧用）。

        与 dr_scan_batch 的区别：那个是「多个独立的短帧」，每帧各自
        Capture→Shift→Update；这个是**一个**很长的 DR，中间不退出 Shift-DR。

        data 是紧排的位流 bytes（bit0 先出）。nsample>0 时采低 nsample 位。
        长度超过单次 USB 传输上限时由 shift_batch 自动分块 —— 分块处 TCK 暂停、
        TMS 保持 0，TAP 仍停在 Shift-DR，移位寄存器状态不受影响。
        """
        if nsample == 0 and nbits % 8 == 0 and len(data) * 8 >= nbits:
            # 纯写数据流：绕开位数组，直接从 bytes 生成 A6 payload。
            # 1024 字 burst 的打包从 539 µs 降到 7 µs（见 xpc.shift_dr_stream）。
            self.cable.shift_dr_stream(bytes(data[:nbits // 8]))
            return b""
        total = 3 + nbits + 2
        if np is not None:
            tms = np.zeros(total, dtype=np.uint8)
            tdi = np.zeros(total, dtype=np.uint8)
            smp = np.zeros(total, dtype=np.uint8)
            tms[0] = 1                      # RTI → Select-DR-Scan
            tms[2 + nbits] = 1              # 末位数据同时退到 Exit1-DR
            tms[3 + nbits] = 1              # Exit1 → Update-DR
            src = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="little")
            tdi[3:3 + nbits] = src[:nbits]
            if nsample:
                smp[3:3 + nsample] = 1
        else:
            tms = [0] * total; tdi = [0] * total; smp = [0] * total
            tms[0] = 1; tms[2 + nbits] = 1; tms[3 + nbits] = 1
            for i in range(nbits):
                tdi[3 + i] = (data[i // 8] >> (i % 8)) & 1
            for i in range(nsample):
                smp[3 + i] = 1
        return self.cable.shift_batch(tms, tdi, smp)
