# 环境搭建

本文列出把这份包跑起来要准备的硬件、软件与权限，做完最后一节的三条自检就可以进
[`usage.md`](usage.md) 开始跑。

**文档模式**：操作步骤。

## 硬件

| 要什么 | 说明 |
| - | - |
| FPGA 板 | 下表这块 Kintex-7 加速卡 |
| JTAG 线缆 | Xilinx Platform Cable USB II（DLC10），接板上的 JTAG 头 |
| 主机 | Linux，一个 USB 口给线缆 |

### 板卡

发布的 bitstream 是按这块板子的引脚约束综合的，换板子要重新综合。

| 项 | 值 |
| - | - |
| FPGA | Kintex-7 `xc7k480tffg1156-2L`（298,600 LUT、955 块 RAMB36、1920 个 DSP） |
| 板卡型号 | `ypcb003381p1`，PCIe x8 插卡 |
| DDR3 | 两通道，每通道 72 位（64 数据 + 8 ECC）、2 GB，颗粒 MT41K256M8，DDR3-1066，合计 4 GB |
| 系统时钟 | 50 MHz 单端，AA28；复位 R28 |
| DDR 参考时钟 | 200 MHz 差分，通道 0 在 AH27 / AH28，通道 1 在 G25 / G26 |
| LED | M30 绿（心跳）、N30 黄（AXI 有活动）、P30 红（出错或 DDR 未校准） |
| 配置 | JTAG 易失烧录（本包用的就是这条路）；板上另有 BPI flash `mt28gu512aax1e` |

这颗 SoC 只用到 50 MHz 时钟、两路 DDR3、JTAG 与三个 LED。卡上的 PCIe、GTX 收发器、温度
传感器都没接进来。

板子要一直上电：DDR3 里的权重掉电就没了，重新灌一次要十几分钟。

## 软件

Python 3.9 以上，两个第三方包：

```bash
pip install numpy pyusb
```

烧录与读写走 `tools/pyjtag/`（随包，纯 Python），不需要 Vivado，也不需要 `hw_server`。

分词器随包带全（`data/tokenizer.json.gz` 加包内的纯 Python BPE），不需要 `tokenizers` 之类的包。

两样可选的：

* **Qwen3-0.6B 的 GGUF 与 llama.cpp 的 `llama-tokenize`**：想拿板上结果与 llama.cpp 逐字
  对照时才需要，`--vocab <那份 gguf>` 把编码交给它。两条路径在常规中英文上给出同一串 token。
* **Vivado**：只在 pyjtag 烧录后 DONE 没拉起来时作兜底（`--prog-tool vivado`，走
  `tools/program.tcl`）。正常不需要。

## USB 权限

线缆默认只有 root 能打开，装一条 udev 规则：

```bash
sudo cp tools/pyjtag/60-xilinx-usb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

规则只在设备接入时生效，已经插着的线缆要拔插一次。

线缆第一次打开时 pyjtag 会把固件（`tools/pyjtag/xusb_xp2.hex`）灌进去，灌完线缆会重新枚举成
另一个 PID。固件常驻线缆的 RAM，拔电才丢。

## 数据落位

`prebuilt/` 与 `data/` 加起来约 630 MB，按下面的位置放好（从发布页单独下载时按 `SHA256SUMS`
校验）：

| 路径 | 大小 | 是什么 |
| - | -: | - |
| `prebuilt/bit/top_jtag_p3.bit` | 14 MB | 50 MHz 的 bitstream |
| `prebuilt/bit/top_jtag_c66n.bit` | 14 MB | 66.7 MHz 的 bitstream |
| `data/weights.npz` | 604 MB | 定点权重，每个数组的名字就是它在 DRAM 里的地址 |
| `data/load_image.bin` | 52 KB | 启动镜像，板子上电后照着它把程序装进片上 |
| `data/tokenizer.json.gz` | 1.4 MB | 分词用的词表与 merges |

校验：

```bash
cd data && sha256sum -c SHA256SUMS
cd ../prebuilt && sha256sum -c SHA256SUMS
```

## 自检

三条自检。第一条不碰板子，后两条要接上线缆、板子上电：

```bash
# 1. 依赖与路径：分词跑通、权重与 bit 找得到
python host/soc_generate.py --dry-run "今天天气不错，我们去"

# 2. 线缆认得出来（插上线缆、板子上电）
python -c "from sys import path; path.insert(0, 'tools'); \
           from pyjtag import XpcCable; c = XpcCable(); \
           print(f'firmware 0x{c.firmware_version:04x}')"

# 3. 板子与权重的完整体检（会按需烧 bit、灌权重，见 usage.md）
python host/soc_generate.py --check-only
```

第一条打出分词结果与生成计划就算通了。第二条报 `Resource busy` 是有别的进程占着线缆（多半是
残留的 `hw_server`），杀掉再来。
