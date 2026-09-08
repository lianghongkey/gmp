"""Platform Cable USB II（DLC10）的纯 Python 驱动（pyusb）。

协议来源：该线缆的 USB 协议没有公开文档，本实现移植自两份逆向工程的
开源代码 —— UrJTAG/xc3sprog 的 xpc 驱动（Kolja Waschk 逆向，GPL）与
hexalinq/libxpc。移植的是协议事实（USB 请求常量与数据格式），代码为重写。

固件存 FX2 RAM，由主机灌入，拔电即失；五份 hex 随包放在本目录。
"""

import os
import time

import atexit
import signal
import usb.core
import usb.util

try:
    import numpy as np
except ImportError:                  # 没有 numpy 就退回逐位打包（慢 20 倍，但能跑）
    np = None

XPC_VID = 0x03FD
XPC_PID_FW = 0x0008          # 固件已加载
XPC_PID_RAW = (0x0013, 0x000D, 0x000F, 0x0009, 0x0007, 0x0015)  # 裸态候选

# 裸态 PID → 首选固件。同一个 PID 在不同批次的线缆上未必用同一份固件，首选不成立时
# 按 FIRMWARE_ORDER 逐个试，以「能走一次空移位」为准。
FIRMWARE_BY_PID = {0x0007: "xusb_xup.hex", 0x0009: "xusb_xup.hex",
                   0x000D: "xusb_xp2.hex", 0x000F: "xusb_xlp.hex",
                   0x0013: "xusb_xp2.hex", 0x0015: "xusb_xse.hex"}
FIRMWARE_ORDER = ("xusb_xp2.hex", "xusb_emb.hex", "xusb_xse.hex", "xusb_xup.hex")

# GPIO 位（写 0x30 / 读 0x38）
GPIO_TDI = 1 << 0
GPIO_TMS = 1 << 1
GPIO_TCK = 1 << 2
GPIO_PROG = 1 << 3
GPIO_TDO = 1 << 0

MAX_SHIFT_BITS = 8191        # 采样传输的单次上限
MAX_WRITE_BITS = 49151       # 纯写传输的单次上限

# TCK 档位：0x0028 请求的 index。12 MHz 档采 TDO 会错、纯写是好的，所以烧 bitstream
# 那条不采样的通道走 12 MHz，其余一律 6 MHz。
TCK_MODE_FAST = 0x10         # 6 MHz：读写都零错
TCK_MODE_WRITE_ONLY = 0x20   # 12 MHz：只在不采 TDO 时可用
TCK_MODE_DEFAULT = TCK_MODE_FAST

# 线缆固件：随例子入库的本地拷贝（--firmware 可覆盖）。
FIRMWARE = os.path.join(os.path.dirname(__file__), "xusb_xp2.hex")


class XpcError(RuntimeError):
    pass


def _parse_ihex(path):
    """解析 Intel HEX → [(addr, bytes), ...]（type 00 数据记录）。"""
    chunks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith(":"):
                continue
            raw = bytes.fromhex(line[1:])
            count, addr, rectype = raw[0], (raw[1] << 8) | raw[2], raw[3]
            if rectype == 0:
                chunks.append((addr, raw[4:4 + count]))
            elif rectype == 1:
                break
    return chunks


def _fx2_load_firmware(dev, hex_path):
    """FX2 固件加载（fxload 等价物）：CPUCS 拉复位 → 0xA0 写 RAM → 放复位。"""
    CPUCS = 0xE600
    def wr(addr, data):
        dev.ctrl_transfer(0x40, 0xA0, addr, 0, data, timeout=1000)
    wr(CPUCS, b"\x01")                      # CPU 复位
    for addr, data in _parse_ihex(hex_path):
        wr(addr, data)
    wr(CPUCS, b"\x00")                      # 放复位 → 固件跑起来，重新枚举


class XpcCable:
    """Platform Cable USB II。打开即完成外部 JTAG 链模式的初始化。"""

    def __init__(self, firmware=None, verbose=False, tck_mode=TCK_MODE_DEFAULT):
        self.verbose = verbose
        self.tck_mode = tck_mode
        self.max_shift_bits = MAX_SHIFT_BITS   # 采样路径上限（实例级，测速脚本可调）
        self.max_write_bits = MAX_WRITE_BITS   # 纯写路径上限
        dev, pid = self._find_any()
        if dev is None:
            raise XpcError(
                "没找到 Xilinx 线缆（VID 03fd）。检查 USB 连接与权限"
                "（udev 规则见本目录 60-xilinx-usb.rules）。")
        self._attach(dev)
        if not self._probe():
            self._load_firmware(firmware, pid)
        self._init_external()

    # ── 打开/初始化 ─────────────────────────────────────────────────────
    @staticmethod
    def _find_any():
        """按「已加载优先」的顺序找一颗 03fd 设备，返回 (dev, pid)。"""
        for pid in (XPC_PID_FW,) + XPC_PID_RAW:
            dev = usb.core.find(idVendor=XPC_VID, idProduct=pid)
            if dev is not None:
                return dev, pid
        return None, None

    def _attach(self, dev):
        """配置 + claim + 切到带批量端点的 altsetting + 排空 EP6 残包。"""
        self.dev = dev
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass                             # 已配置过（如 hw_server 用过）
        usb.util.claim_interface(dev, 0)
        for cfg in dev:
            for intf in cfg:
                eps = [e.bEndpointAddress for e in intf]
                if 0x02 in eps and 0x86 in eps:
                    if intf.bAlternateSetting:
                        dev.set_interface_altsetting(
                            interface=intf.bInterfaceNumber,
                            alternate_setting=intf.bAlternateSetting)
                    self._drain_ep6()
                    return
        raise XpcError("没有同时提供 EP2(OUT) 与 EP6(IN) 的 altsetting")

    def _drain_ep6(self):
        """把 EP6 里的 TDO 残包读干净。

        上一次进程崩在移位中途（或换固件重试）时，FX2 的 EP6 FIFO 里会留着
        没取走的采样数据。下一次 `_shift_raw` 请求 4 字节却收到残包的头，
        libusb 报 `[Errno 75] Overflow`，症状是「IDCODE 忽然读不出来」，
        拔插线缆就好 —— 实测残留过 304 字节。
        """
        try:
            self.dev.clear_halt(0x86)
        except usb.core.USBError:
            pass
        try:
            while self.dev.read(0x86, 512, timeout=50):
                pass
        except usb.core.USBError:
            pass                             # 读空即超时，正常出口

    def _probe(self):
        """固件在不在：0xB0 读到非零版本，且能走一次 2 位空移位。

        只读版本不够 —— 灌错的固件也会应答版本，但一移位就超时。
        """
        try:
            self._ctrl_out(0x0028, 0x11)
            fw = self._ctrl_in(0x0050, 0x0000, 2)
            if not (fw[0] | (fw[1] << 8)):
                return False
            self._shift_raw(2, bytes(2), 0)
        except usb.core.USBError:
            return False
        return True

    def _load_firmware(self, firmware, pid):
        """逐个试固件，直到 _probe 通过。灌完 PID 可能变 0008，也可能不变。"""
        if firmware:
            cands = [firmware]
        else:
            first = FIRMWARE_BY_PID.get(pid)
            cands = [os.path.join(os.path.dirname(__file__), n)
                     for n in ([first] if first else [])
                     + [n for n in FIRMWARE_ORDER if n != first]]
        for fw in cands:
            if not os.path.exists(fw):
                continue
            if self.verbose:
                print(f"[xpc] 灌固件 {os.path.basename(fw)} …")
            try:
                _fx2_load_firmware(self.dev, fw)
            except usb.core.USBError as e:
                if self.verbose:
                    print(f"[xpc]   写 RAM 失败: {e}")
                continue
            usb.util.dispose_resources(self.dev)
            for _ in range(40):              # 固件起来最多等 4s（可能重枚举）
                time.sleep(0.1)
                dev, _ = self._find_any()
                if dev is None:
                    continue
                try:
                    self._attach(dev)
                except (usb.core.USBError, XpcError):
                    continue
                break
            else:
                raise XpcError("灌固件后设备不见了 —— 拔插线缆重试")
            if self._probe():
                if self.verbose:
                    print(f"[xpc]   OK（{os.path.basename(fw)}，"
                          f"PID {self.dev.idProduct:04x}）")
                return
        raise XpcError(
            "试遍所有固件都没能让线缆应答。可能原因：这颗要一份仓库里没有的固件；"
            "线缆是只模拟装载协议的克隆件；或者插在 hub 上信号不稳（换主板直连口）。")

    def _ctrl_out(self, value, index=0):
        self.dev.ctrl_transfer(0x40, 0xB0, value, index, None, timeout=1000)

    def _ctrl_in(self, value, index, length):
        return bytes(self.dev.ctrl_transfer(0xC0, 0xB0, value, index, length,
                                            timeout=1000))

    def _init_external(self):
        """外部链模式初始化（照 xc3sprog IOXPC::Init 的非 internal 分支）。"""
        self._ctrl_out(0x0028, 0x11)
        self._ctrl_out(0x0030, GPIO_PROG)    # PROG 拉高（不复位目标）
        fw = self._ctrl_in(0x0050, 0x0000, 2)
        cpld = self._ctrl_in(0x0050, 0x0001, 2)
        self.firmware_version = fw[0] | (fw[1] << 8)
        self.cpld_version = cpld[0] | (cpld[1] << 8)
        if self.verbose:
            print(f"[xpc] firmware=0x{self.firmware_version:04x} "
                  f"cpld=0x{self.cpld_version:04x}")
        if self.firmware_version == 0:
            raise XpcError("固件版本读到 0 —— 拔插线缆重试")
        self._ctrl_out(0x0010)               # output disable
        self._ctrl_out(0x0028, 0x11)
        self._ctrl_out(0x0018)               # output enable
        self._shift_raw(2, bytes(2), 0)      # 2 个空位（初始化惯例）
        self._ctrl_out(0x0028, self.tck_mode)   # TCK 档位，见 TCK_MODE_FAST

    def close(self):
        try:
            self._ctrl_out(0x0010)           # output disable
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError:
            pass

    # ── 位级移位 ────────────────────────────────────────────────────────
    def _shift_raw(self, nbits, payload, out_bits):
        """一次 A6 传输：nbits 个位组已按协议打包在 payload 里。"""
        self._ctrl_out(0x00A6, nbits)
        self.dev.write(0x02, payload, timeout=1000)
        if out_bits <= 0:
            return b""
        out_len = 2 * (out_bits >> 4)
        if out_bits & 15:
            out_len += 2
        data = self.dev.read(0x86, out_len, timeout=1000)
        return self._realign_tdo(bytes(data), out_bits)

    @staticmethod
    def _realign_tdo(buf, out_bits):
        """TDO 重对齐（照 xc3sprog xpcu_do_ext_transfer 的算法）。

        整 32-bit 字直接可用；末个不满 32 位的字，有效位靠 16-bit 字的
        高位对齐，需要移回来。返回按位紧排的小端 bytes。
        """
        out = bytearray((out_bits + 7) // 8)
        aligned_bytes = (out_bits // 32) * 4
        out[:aligned_bytes] = buf[:aligned_bytes]
        if out_bits % 32:
            shift = out_bits % 16
            if shift:
                shift = 16 - shift
            for i in range(aligned_bytes * 8, out_bits):
                bit_num = i + shift
                if buf[bit_num // 8] & (1 << (bit_num % 8)):
                    out[i // 8] |= 1 << (i % 8)
        return bytes(out)

    def jtag_shift(self, tms_bits, tdi_bits, read=False):
        """打 len(tms_bits) 个 TCK：逐位给定 TMS/TDI；read 时返回 TDO 位列表。

        自动按 8191 位上限分块、按「不能 4 整倍数」怪癖补哑位。
        """
        assert len(tms_bits) == len(tdi_bits)
        tdo = []
        pos = 0
        total = len(tms_bits)
        while pos < total:
            n = min(total - pos, MAX_SHIFT_BITS)
            chunk_tms = tms_bits[pos:pos + n]
            chunk_tdi = tdi_bits[pos:pos + n]
            pos += n
            nbits = n
            pad = 0
            if nbits % 4 == 0:               # 补 1 个不打 TCK 的哑位
                nbits += 1
                pad = 1
            words = bytearray(2 * ((nbits + 3) // 4))
            for i in range(n):
                g, b = divmod(i, 4)
                if chunk_tdi[i]:
                    words[2 * g] |= 0x01 << b
                if chunk_tms[i]:
                    words[2 * g] |= 0x10 << b
                words[2 * g + 1] |= 0x01 << b          # TCK 脉冲
                if read:
                    words[2 * g + 1] |= 0x10 << b      # 采样 TDO
            # pad 位：words 里对应组保持全 0（无 TCK、无采样）即可
            out = self._shift_raw(nbits, bytes(words), n if read else 0)
            if read:
                for i in range(n):
                    tdo.append((out[i // 8] >> (i % 8)) & 1)
        return tdo if read else None

    def set_tck(self, mode):
        """切 TCK 档位（见 TCK_MODE_*）。一次 control transfer，约 250 µs。"""
        self._ctrl_out(0x0028, mode)
        self.tck_mode = mode

    # ── 批量移位：一次 USB 传输打完多帧，主机侧批量的地基 ──────────────
    _NIB_W = None

    @classmethod
    def _nib(cls, arr, g):
        """位数组 → 每 4 位一组的 nibble 数组（长度 g）。

        走 packbits 而不是「reshape(g,4) 乘权重再 sum」：后者的 sum 会把 uint8
        提升成 int64，中间数组是结果的 8 倍大，1024 字 burst 打一次要 539 µs，
        packbits 版 31 µs。打包曾是 6 MHz 下仅次于移位本身的第二大开销。
        """
        a = np.zeros(4 * g, dtype=np.uint8)
        a[:len(arr)] = arr
        b = np.packbits(a, bitorder="little")     # 每字节装两个 nibble
        out = np.empty(g, dtype=np.uint8)
        lo, hi = len(out[0::2]), len(out[1::2])   # g 为奇数时两者差一
        out[0::2] = b[:lo] & 0x0F
        out[1::2] = b[:hi] >> 4
        return out

    @classmethod
    def _pack_batch(cls, tms, tdi, sample, nbits, n_tck):
        """打成 A6 的 payload：nbits 含哑位，前 n_tck 位打 TCK。"""
        g = (nbits + 3) // 4
        if np is None:
            w = bytearray(2 * g)
            for i in range(n_tck):
                q, b = divmod(i, 4)
                if tdi[i]:    w[2 * q]     |= 0x01 << b
                if tms[i]:    w[2 * q]     |= 0x10 << b
                w[2 * q + 1] |= 0x01 << b
                if sample[i]: w[2 * q + 1] |= 0x10 << b
            return bytes(w)
        tck = np.zeros(n_tck, dtype=np.uint8) + 1
        out = np.empty(2 * g, dtype=np.uint8)
        out[0::2] = cls._nib(tdi, g) | (cls._nib(tms, g) << 4)
        out[1::2] = cls._nib(tck, g) | (cls._nib(sample, g) << 4)
        return out.tobytes()

    def shift_batch(self, tms, tdi, sample):
        """打 len(tms) 个 TCK，逐位给定 TMS / TDI / 是否采样。

        与 `jtag_shift` 的区别是打包走 numpy（8191 位从 ~1.3 ms 降到 ~70 µs），
        以及调用方可以只取自己要的位。返回采样位紧排成的 bytes（bit0 先出）。

        ⚠ 硬件上一律全位采样，回来再按 sample 掩码挑。固件那个「末个不满 32 位
        的字要重对齐」的怪癖（见 _realign_tdo）只在 out_bits == 总移位位数时
        行为已知；一旦关掉某些位的采样，_realign_tdo 就会跟固件实际返回的字节数
        对不上，症状是 66 位帧读出错位、更长的批次直接 IndexError。多读进来的
        那点导航位（71 位一帧里占 5 位）远比赌固件的对齐规则划算。
        """
        n = len(tms)
        chunks, pos = [], 0
        while pos < n:
            m = min(n - pos, MAX_SHIFT_BITS)
            nbits = m + 1 if m % 4 == 0 else m       # 避开「4 的整倍数」怪癖
            seg = slice(pos, pos + m)
            seg_s = sample[seg]
            any_s = (int(np.count_nonzero(seg_s)) if np is not None and hasattr(seg_s, "dtype")
                     else any(seg_s))
            if not any_s:
                # 一位都不采：固件走纯写快路径（5.21 MHz，采样时只有 1.16 MHz）。
                zeros = (np.zeros(m, dtype=np.uint8) if np is not None else [0] * m)
                self._shift_raw(nbits, self._pack_batch(tms[seg], tdi[seg], zeros, nbits, m), 0)
                pos += m
                continue
            ones = (np.ones(m, dtype=np.uint8) if np is not None else [1] * m)
            payload = self._pack_batch(tms[seg], tdi[seg], ones, nbits, m)
            raw = self._shift_raw(nbits, payload, m)
            if np is not None:
                bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8),
                                     bitorder="little")[:m]
                chunks.append(bits[np.asarray(sample[seg], dtype=bool)])
            else:
                chunks.append([(raw[i // 8] >> (i % 8)) & 1
                               for i in range(m) if sample[pos + i]])
            pos += m
        if not chunks:
            return b""
        if np is not None:
            return np.packbits(np.concatenate(chunks) if len(chunks) > 1 else chunks[0],
                               bitorder="little").tobytes()
        flat = [b for ch in chunks for b in ch]
        out = bytearray((len(flat) + 7) // 8)
        for i, v in enumerate(flat):
            if v:
                out[i // 8] |= 1 << (i % 8)
        return bytes(out)

    # ── DR 数据流快通道：burst 数据帧专用 ──────────────────────────────
    #
    # burst 数据帧的本质是「TMS 恒 0、一位不采、TDI 就是一串字节」，走
    # shift_batch 的通用位数组路径纯属浪费：1024 字要先摊成 32 K 个 uint8
    # 再打包回去，539 µs。这里直接从 bytes 生成 payload（7 µs，78 倍）。
    #
    # 能这么做的前提是让数据从 nibble 边界开始：TAP 导航 RTI→Select-DR→
    # Capture-DR→Shift-DR 本来是 3 个 TCK，这里在 RTI 多停一拍凑成 4 位
    # （TMS=0,1,0,0），正好一个 nibble 组，其后每 4 位数据严格对齐一组。
    #
    # 退出也不占数据组：数据段整段 TMS=0（结束时仍停在 Shift-DR），随后单发
    # 一组「TMS=1,1」退出。这会多移一位进桥的移位寄存器 —— 无害，桥的 burst
    # 计数到 0 之后本就不再写 AXI。
    _NAV4 = bytes((0x20, 0x0F))      # TDI=0000 TMS=0100(LSB先) / TCK=1111 采样=0000
    _EXIT2 = bytes((0x30, 0x03))     # TDI=00 TMS=11 / TCK=11 采样=00

    @staticmethod
    def _tdi_groups(buf):
        """bytes → A6 payload 片段：每字节 2 组，TMS=0、TCK 全打、不采样。"""
        b = np.frombuffer(buf, dtype=np.uint8)
        out = np.empty(4 * len(b), dtype=np.uint8)
        out[0::4] = b & 0x0F         # 低 nibble 作 TDI，高 nibble（TMS）留 0
        out[1::4] = 0x0F             # 4 个 TCK 脉冲，采样位全 0
        out[2::4] = b >> 4
        out[3::4] = 0x0F
        return out.tobytes()

    def shift_dr_stream(self, data):
        """一次 DR 扫描移入 len(data)*8 位纯数据（RTI 进、RTI 出，不采 TDO）。

        分块时中间不插导航、TMS 保持 0，TAP 停在 Shift-DR，移位寄存器不受影响；
        导航只在首块之前发一次，退出只在末块之后发一次。
        """
        if np is None:
            return self._shift_dr_stream_slow(data)
        # 每块的数据字节数：留出首块导航 / 末块退出那一组，并保证是整字节
        per = (self.max_write_bits - 8) // 8
        n = len(data)
        pos = 0
        while pos < n:
            m = min(n - pos, per)
            head = self._NAV4 if pos == 0 else b""
            tail = self._EXIT2 if pos + m >= n else b""
            payload = head + self._tdi_groups(data[pos:pos + m]) + tail
            nb = (4 if head else 0) + m * 8 + (2 if tail else 0)
            if nb % 4 == 0:                      # 避开「4 的整倍数」怪癖
                payload += b"\x00\x00"
                nb += 1
            self._shift_raw(nb, payload, 0)
            pos += m

    def _shift_dr_stream_slow(self, data):
        """没有 numpy 时的等价实现（慢 20 倍，但语义一致）。"""
        bits_ = []
        for byte in data:
            for i in range(8):
                bits_.append((byte >> i) & 1)
        n = len(bits_)
        tms = [0, 1, 0, 0] + [0] * n + [1, 1]
        tdi = [0, 0, 0, 0] + bits_ + [0, 0]
        self.jtag_shift(tms, tdi)

    def shift_bulk_tdi(self, data):
        """大块 TDI 快速通道：移 len(data)*8 位，TMS 恒 0、不采 TDO。

        烧 bitstream 用（1500 万+ 位，逐位列表太慢）。每字节 bit0 先出。
        分块 1023 字节 = 8184 位/次（补 1 哑位避开 4 整倍数怪癖）。

        不采 TDO，所以整段切到 12 MHz（0.96 MB/s，比 6 MHz 快 65%）；
        两次切档共约 500 µs，摊到整个 bitstream 上可以忽略。
        """
        prev = self.tck_mode
        if prev != TCK_MODE_WRITE_ONLY:
            self.set_tck(TCK_MODE_WRITE_ONLY)
        try:
            self._shift_bulk_tdi(data)
        finally:
            if prev != TCK_MODE_WRITE_ONLY:
                self.set_tck(prev)

    def _shift_bulk_tdi(self, data):
        """TMS 恒 0 的裸移位（TAP 已在 Shift-DR 里），用于灌 bitstream。

        打包与分块都跟 shift_dr_stream 同款：numpy 直通取代逐字节 Python 循环，
        分块按 max_shift_bits 而不是写死的 1023 字节 —— 1.84 MB 的 bitstream
        原先要切 1800 块，每块两次 USB 事务。
        """
        per = self.max_write_bits // 8
        for base in range(0, len(data), per):
            chunk = data[base:base + per]
            n = len(chunk) * 8
            nbits = n + 1 if n % 4 == 0 else n     # 字节整数倍必是 4 的倍数
            payload = self._tdi_groups(chunk)
            if nbits != n:
                payload += b"\x00\x00"
            self._shift_raw(nbits, payload, 0)
