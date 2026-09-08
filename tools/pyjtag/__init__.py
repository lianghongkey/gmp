"""pyjtag — 纯 Python 的 Xilinx JTAG 栈（Platform Cable USB II）。

    XpcCable  (xpc.py)    pyusb 线缆驱动：FX2 固件加载 + XPC 逆向协议移位
    Tap       (tap.py)    JTAG TAP 状态机：reset / IR 扫描 / DR 扫描
    program_bit (fpga.py) 7 系列 JTAG 配置：JPROGRAM → CFG_IN → JSTART

运行时依赖：pyusb。线缆固件 xusb_xp2.hex 随本包入库（仅线缆上电后第一次
打开时灌入；固件常驻线缆 RAM 直到拔电）。不依赖 Vivado / hw_server。
"""

from .xpc import XpcCable
from .tap import Tap
from .fpga import program_bit, read_idcode

__all__ = ["XpcCable", "Tap", "program_bit", "read_idcode"]
