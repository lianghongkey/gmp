"""JTAG TAP 状态机（IEEE 1149.1），建立在 XpcCable.jtag_shift 之上。

只实现例子需要的最小集合：Test-Logic-Reset、Run-Test/Idle、IR/DR 扫描。
所有扫描进出都停在 Run-Test/Idle（RTI）—— 简化状态跟踪，扫描间隔
增加的几个 TCK 对本应用毫无影响。

单器件链假设（本板 JTAG 链上只有一颗 xc7k480t）：IR 长 6 位，DR 前后
无别的器件占位。多器件链需要在扫描前后补 BYPASS 位，此处不做。
"""


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
