"""7 系列 FPGA 的 JTAG 操作：读 IDCODE、烧 bitstream。

配置流程是公开文档（UG470 第 10 章「JTAG 配置」）+ libxpc/xc3sprog 的
既有实现，无任何逆向成分：

    TLR 复位 → IR=JPROGRAM（清配置）→ 等 INIT → IR=CFG_IN
    → DR 移入整个 .bit 配置数据（每字节位反转，配置口按 MSB 先收）
    → IR=JSTART → RTI 跑几十个 TCK 起动 → TLR，IR capture 查 DONE。

.bit 文件格式：13 字节魔数头 + 'a'~'d' 段（2 字节长度，描述/器件/时间戳）
+ 'e' 段（4 字节大端长度 + 配置数据本体）。
"""

import time

# 7 系列 IR 操作码（BSDL/UG470，6 位）
IR_IDCODE = 0x09
IR_JPROGRAM = 0x0B
IR_CFG_IN = 0x05
IR_JSTART = 0x0C
IR_BYPASS = 0x3F

_BITREV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))

_BIT_MAGIC = bytes((0x00, 0x09, 0x0F, 0xF0, 0x0F, 0xF0,
                    0x0F, 0xF0, 0x0F, 0xF0, 0x00, 0x00, 0x01))


class BitFileError(RuntimeError):
    pass


def parse_bit(path):
    """解析 .bit → (info dict, 配置数据 bytes)。"""
    with open(path, "rb") as f:
        raw = f.read()
    if not raw.startswith(_BIT_MAGIC):
        raise BitFileError(f"{path}: 不是 Xilinx .bit 文件（魔数头不符）")
    info = {}
    pos = len(_BIT_MAGIC)
    data = None
    while pos < len(raw):
        sec = raw[pos:pos + 1].decode()
        pos += 1
        if sec == "e":
            size = int.from_bytes(raw[pos:pos + 4], "big")
            pos += 4
            data = raw[pos:pos + size]
            break
        size = int.from_bytes(raw[pos:pos + 2], "big")
        pos += 2
        info[{"a": "design", "b": "part", "c": "date", "d": "time"}
             .get(sec, sec)] = raw[pos:pos + size].rstrip(b"\0").decode()
        pos += size
    if data is None:
        raise BitFileError(f"{path}: 没有 'e' 配置数据段")
    return info, data


def read_idcode(tap):
    """IR=IDCODE 后读 32 位 DR。（TLR 后直接读 DR 也行，这里走显式 IR。）"""
    tap.reset()
    tap.ir_scan(IR_IDCODE)
    return tap.dr_scan(0, 32, read=True)


def program_bit(cable, tap, path, progress=None):
    """烧 .bit（易失）。progress: 可选回调 f(done_bytes, total_bytes)。"""
    info, data = parse_bit(path)
    cfg = data.translate(_BITREV)      # 每字节位反转：配置口 MSB 先收

    tap.reset()
    tap.ir_scan(IR_JPROGRAM)           # 清掉现有配置，重新起配置逻辑
    # 等配置存储器清空：轮询 IR capture 的 INIT_B 位（bit4）。大器件要几十 ms，
    # 等不够就开灌会丢掉开头的数据（含同步字），最后 DONE=0（xc3sprog 同款轮询）。
    for _ in range(200):
        ircap = tap.ir_scan(IR_CFG_IN, read=True)
        if ircap & 0x10:
            break
        time.sleep(0.01)
    else:
        raise BitFileError(f"JPROGRAM 后 INIT 一直为 0（IR capture=0x{ircap:02X}）")
    # 进 Shift-DR，流式移入全部配置数据（最后 1 位带 TMS=1 → Exit1）
    cable.jtag_shift([1, 0, 0], [0, 0, 0])          # RTI → Shift-DR
    total = len(cfg)
    BLOCK = 64 * 1024
    for base in range(0, total - 1, BLOCK):
        cable.shift_bulk_tdi(cfg[base:min(base + BLOCK, total - 1)])
        if progress:
            progress(min(base + BLOCK, total - 1), total)
    last_byte = cfg[-1]
    tms = [0] * 7 + [1]
    tdi = [(last_byte >> i) & 1 for i in range(8)]
    cable.jtag_shift(tms, tdi)                      # 末字节，末位退 Exit1
    cable.jtag_shift([1, 0], [0, 0])                # Update-DR → RTI

    tap.ir_scan(IR_JSTART)
    tap.run_test(2048)                 # 起动序列的自由 TCK（宁多勿少）

    # DONE 检查：IR capture 的 bit5 = DONE（7 系列 IR capture 低 2 位恒 01）。
    # 启动等外部条件（如 DCM 锁定）时 DONE 可能晚到，轮询几次。
    done = False
    ircap = 0
    for _ in range(50):
        ircap = tap.ir_scan(IR_BYPASS, read=True)
        done = bool((ircap >> 5) & 1)
        if done:
            break
        tap.run_test(1000)
        time.sleep(0.01)
    tap.reset()
    return info, done, ircap
