"""主机侧读写板子要用到的编号，值固化在发布的 bitstream 里，只读不改。"""

# ══ AXI-Lite 窗口（addr[31:24] 选窗口，addr[23:0] 是窗口内偏移）══
WIN_CPU = 0x02                  # CPU
WIN_XFER = 0x03                 # 批量搬运的命令
WIN_XFER_BUF = 0x04             # 批量搬运的数据缓冲
WIN_STAT = 0x20                 # 只读状态

# ══ 状态窗口的寄存器号 ══
SOC_MAGIC = 0x534F4331          # 读 STAT_MAGIC 得到它才说明板上烧的是这颗 SoC

STAT_MAGIC = 0
STAT_CYCLES = 1                 # 拍计数
STAT_CPU_TRACE = 4              # 4 到 7
STAT_CPU_STATE = 8
STAT_BOARD = 9                  # bit0 = DDR 两通道校准完成

# ══ CPU ══
CPU_PERIPH_BASE = 0x0020_0000   # 外设区基址，下面两个是区内的寄存器号
CPU_GO = 0x7C0                  # 写 0 再写 1，让 CPU 开始跑
CPU_GP0 = 0x7D0                 # 与 CPU 逐轮通讯的那组寄存器，从这里起

# ══ 批量搬运 ══
HOST_PORT = 3                   # 主机在片上互联上的端口号
IMAGE_BASE = 0x0310_0000        # 启动镜像在 DRAM 里的位置，板子上电后从这里把程序搬进片上
