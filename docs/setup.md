# 环境搭建

本文列出把这份包跑起来要准备的硬件、软件与权限，做完最后一节的三条自检就可以进
[`usage.md`](usage.md) 开始跑。手上没有板子时改看 [`simulation.md`](simulation.md)，那条路
不用板子、线缆、网口和它们的权限，下面“软件”一节里只要 numpy。

**文档模式**：操作步骤。

## 硬件

主机与板子之间有两条线：网线走数据，JTAG 线缆只用来烧 bitstream。

| 要什么 | 说明 |
| - | - |
| FPGA 板 | 下表这块 Kintex-7 加速卡 |
| 转以太网的转接硬件 | 插进板子的 PCIe 金手指，把 lane 7 的 GTX 引到一个 SFP 笼，并给板子送 125 MHz 参考时钟 |
| 铜口 SFP 模块与一根网线 | 从 SFP 笼直连主机的千兆网口，中间不经交换机 |
| JTAG 线缆 | Xilinx Platform Cable USB II（DLC10），接板上的 JTAG 头 |
| 主机 | Linux，一个 USB 口给线缆，一个千兆网口给板子 |

### 板卡

发布的 bitstream 是按这块板子的引脚约束综合的，换板子要重新综合。

| 项 | 值 |
| - | - |
| FPGA | Kintex-7 `xc7k480tffg1156-2L`（298,600 LUT、955 块 RAMB36、1920 个 DSP） |
| 板卡型号 | `ypcb003381p1`，PCIe x8 插卡 |
| DDR3 | 两通道，每通道 72 位（64 数据 + 8 ECC）、2 GB，颗粒 MT41K256M8，DDR3-1066，合计 4 GB |
| 系统时钟 | 50 MHz 单端，AA28；复位 R28 |
| DDR 参考时钟 | 200 MHz 差分，通道 0 在 AH27 / AH28，通道 1 在 G25 / G26 |
| 以太网 | PCIe 金手指 lane 7 的 GTX（`GTXE2_CHANNEL_X0Y16`），参考时钟 125 MHz 从 PCIe 的 J8 / J7 进 |
| LED | M30 绿（心跳）、N30 黄（AXI 有活动）、P30 红（出错或 DDR 未校准） |
| 配置 | JTAG 易失烧录（本包用的就是这条路）；板上另有 BPI flash `mt28gu512aax1e` |

这颗 SoC 用到 50 MHz 时钟、两路 DDR3、PCIe lane 7 的 GTX、JTAG 与三个 LED。卡上其余的 PCIe
通道与温度传感器都没接进来。

板子要一直上电：DDR3 里的权重掉电就没了，重新灌一次要四十几秒。

## 软件

Python 3.9 以上，三个第三方包：

```bash
pip install numpy pyusb pybind11
```

`numpy` 读权重；`pyusb` 给烧录用的 JTAG 栈；`pybind11` 编下面那个走网口的模块。

网口那条通路是一小段 C++，随包发源码，编一次：

```bash
make -C host/ethaxi          # 启动器 ethaxi_run
make -C host/ethaxi module   # python 模块 ethaxi
```

产物都进 `host/ethaxi/build/`。

烧录走 `tools/pyjtag/`（随包，纯 Python），不需要 Vivado。

分词要用的东西包里都有（`data/tokenizer.json.gz` 加包内的纯 Python BPE），不需要 `tokenizers` 之类的包。

一样可选的：**Qwen3-0.6B 的 GGUF 与 llama.cpp 的 `llama-tokenize`**，想拿板上结果与
llama.cpp 逐字对照时才需要，`--vocab <那份 gguf>` 把编码交给它。两条路径在常规中英文上
给出同一串 token。

## 权限

两处，各授一次。

**网口收发裸帧**要 CAP_NET_RAW。这个权限只挂得到可执行文件上，挂不到 `.so` 与脚本，所以
包里有一个专门的启动器 `host/ethaxi/build/ethaxi_run`：给它授权，它把权限传给随后启动的
python。`soc_generate.py` 发现自己没有这个权限时会自己经它重跑一遍，所以平时的命令不用加前缀。

```bash
sudo setcap cap_net_raw+eip host/ethaxi/build/ethaxi_run
```

`ethaxi_run` 的源码不变、也不用重编，所以这一条只授这一次；python 模块随便重编都不受影响。
它只肯启动两类目标：发布包目录之下的可执行文件，以及“python 加一个包内脚本”。

**线缆**默认只有 root 能打开，装一条 udev 规则：

```bash
sudo cp tools/pyjtag/60-xilinx-usb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

规则只在设备接入时生效，已经插着的线缆要拔下来重插一次。

线缆第一次打开时 pyjtag 会把固件（`tools/pyjtag/xusb_xp2.hex`）灌进去，灌完线缆会重新枚举成
另一个 PID。固件常驻在线缆的 RAM 里，断电才会丢。

## 网口

板子与主机是直连的一段链路，两端不用配 IP：帧里带的是自定义协议，不经内核的网络栈。主机
这一侧只要网口是 up 的：

```bash
sudo ip link set <网口名> up
```

`soc_generate.py` 不给 `--nic` 时，会在本机挑一块 link up 的实体网口；主机上不止一块时报错并
列出候选，这时用 `--nic <网口名>` 指定，或者把它写进环境变量 `GMP_NIC`。

## 数据落位

`prebuilt/` 与 `data/` 加起来约 820 MB，按下面的位置放好（从发布页单独下载时按 `SHA256SUMS`
校验）：

| 路径 | 大小 | 是什么 |
| - | -: | - |
| `prebuilt/bit/top_eth.bit` | 14 MB | bitstream |
| `prebuilt/sim/VSocCosimTop` | 2.3 MB | 整机仿真，没有板子时走它 |
| `prebuilt/sim/cpu_boot_rom.hex`、`func_tbl.hex` | 21 KB | 仿真起来时装入的引导码与函数表 |
| `data/weights.npz` | 799 MB | 定点权重，每个数组的名字就是它在 DRAM 里的地址 |
| `data/load_image.bin` | 52 KB | 启动镜像，板子上电后照着它把程序装进片上 |
| `data/tokenizer.json.gz` | 1.4 MB | 分词用的词表与 merges |

校验：

```bash
cd data && sha256sum -c SHA256SUMS
cd ../prebuilt && sha256sum -c SHA256SUMS
```

## 自检

三条自检。第一条不碰硬件，后两条要接上线缆与网线、板子上电：

```bash
# 1. 依赖与路径：分词跑通、权重与 bit 找得到
python host/soc_generate.py --dry-run "今天天气不错，我们去"

# 2. 线缆认得出来（插上线缆、板子上电）
python -c "from sys import path; path.insert(0, 'tools'); \
           from pyjtag import XpcCable; c = XpcCable(); \
           print(f'firmware 0x{c.firmware_version:04x}')"

# 3. 板子与权重的完整体检（会按需烧 bit、灌权重，见 usage.md）
python host/soc_generate.py --check-only "今天天气不错，我们去"
```

第一条打出分词结果与生成计划就算通了。第二条报 `Resource busy` 是有别的进程占着线缆（多半是
残留的 `hw_server`），杀掉再来。
