# 在仿真上跑

本文讲手上没有板子时怎么用包里那份整机仿真跑生成：它是什么、机器要满足什么、跑哪条命令、
哪些地方与板上不一样、出错先看什么。板上那条路在 [`usage.md`](usage.md)。

**文档模式**：操作步骤。

## 这份仿真是什么

`prebuilt/sim/VSocCosimTop` 是整颗 SoC 的 Verilator 仿真，跑起来是一个常驻进程：里面是这颗
SoC 的整机 RTL，外加两片 DRAM 模型。主机侧的代码一行都不用改。`host/device.py` 把 read32 与
write32 打成帧经 unix socket 发给它，与经网线打到板上的那两个动作字节等价，所以
`soc_generate.py` 的流程原样跑在它上面。

它起来时从自己所在的目录装入两份数据：`cpu_boot_rom.hex` 是上电引导码，`func_tbl.hex` 是
1D 那些函数用的查找表。三个文件合计 2.4 MB。

与 bitstream 一样，这是编译好的产物，RTL 源码不在包内。可执行文件里链进了 Verilator 5.020
的运行时库，那部分按 LGPL-3.0-only 或 Artistic-2.0 双授权分发。

## 机器要满足什么

| 项 | 要求 |
| - | - |
| 系统 | Linux x86-64；动态库只用到 libstdc++、libm、libgcc_s、libc |
| 内存 | 权重灌完仿真进程占 1.3 GB，机器留 2 GB 富余 |
| CPU | 仿真固定开 8 个线程 |
| Python | 只要 numpy。不用 pyusb、不用 pybind11、不用 udev 规则、不用 setcap、不用 Vivado |

## 跑哪条命令

先自测：

```bash
python host/soc_generate.py --sim --check-only "今天天气不错，我们去"
```

打出“仿真里认出这颗 SoC”就算通了，那一行同时报这台机器每秒打多少拍，以及照这个速率一步
decode 要多久。

再跑生成：

```bash
python host/soc_generate.py --sim "今天天气不错，我们去"
```

喂 prompt 一个 token 一步，生成一个 token 一步，一步 decode 是 1218 万拍。`--max-new` 不给
就一直生成到上下文 128 填满，6 个 token 的 prompt 就是 128 步。

算出来的东西与板上一样：`usage.md` 里板上那趟的 prompt“今天天气不错，我们去”，在仿真上
喂完六个 token 之后片上给出的下一个 token 同样是 102077“公园”，与板上、与 llama.cpp 的
贪心解码逐字相同。

## 跑起来是什么样

一趟实录，机器是一台 16 核 32 线程的 x86，跑的时候上面还有别的负载：

```text
$ python host/soc_generate.py --sim --max-new 1 "今天天气"
── Qwen3 0.6B Instruct：prompt 2 个 token（纯 Python BPE）──
    计划：不满 64，逐个喂 2 个（decode n_seq = 1..2），然后最多生成 1 个（上下文 128）；停在 [151643, 151645]
── 仿真 ──
    仿真起来了：prebuilt/sim/VSocCosimTop（pid 2308902），它打印的东西落在 out/sim_gmp_soc_axi.log
    仿真里认出这颗 SoC，空转 90 k 拍/秒（一步 decode 1218 万拍，这个速率下约 2 分钟）
── 灌整份权重（762 MiB，几秒钟）──
      … 158 MiB（589.42 MB/s，还要约 0 分钟）
      …（每 64 MiB 报一行）
      … 762 MiB（305.90 MB/s，还要约 0 分钟）
    权重 761.7 MiB 灌完，0.0 分钟
    权重抽查 16 处一致
    清零：KV 每层前 128 格 × 56 块 + 中间张量 8 段，共 14.0 MiB，0 s
    CPU 报到，程序版本 1
    逐个喂 2 个：250.5 s
    （吃 prompt 时片上 top1 猜中下一个 token 0/1 次）
── 生成 ──
晴
── 生成 1 个 token（到 --max-new），decode 平均 0.00 s/步 ──
```

最后那个 token 是喂 prompt 最末一轮顺带算出来的，没有再多跑一步 decode，所以“decode 平均”
那栏是 0。

## 与板上不一样的地方

| 项 | 板上 | 仿真 |
| - | - | - |
| 烧 bitstream | 每次板子上电后要烧一遍 | 没有这一步，`--sim` 与 `--program` 不能一起给 |
| 权重 | 灌一次就留在 DDR3 里，后面几趟都跳过 | 每趟都要重灌：DRAM 模型活在仿真进程里，进程退了就没了 |
| 往 DRAM 搬字节 | 经片上互联，762 MiB 要四十几秒 | 走后门直接放进 DRAM 模型，不占仿真拍，几秒钟灌完 |
| 一步 decode | 0.24 秒 | CPU 报的拍数同样是 1218 万，按 50 MHz 折算就是板上那 0.24 秒 |
| 一轮的墙钟超时 | 缺省 120 秒 | 缺省 7200 秒，`--max-wall` 改 |
| DDR 校准 | 板子上电后要等，最多 30 秒 | 起来就是校准完成 |

权重、启动镜像、token 串、还有每轮要清零的那几段，在仿真下都走后门，板上那条经片上互联搬 DRAM 的通路
因此没有被走到；片上算出来的东西仍然是整机 RTL 逐拍跑出来的。

## 出错时先看什么

| 现象 | 先看这个 |
| - | - |
| 仿真起来就退了 | `out/` 下那份 `sim_*.log`，它打印的东西都落在那里，文件名跟着 socket 走，开跑时打印的那一行就是它 |
| 报 socket 路径超出 108 字节 | unix socket 的路径有这个上限，用 `--sim-sock` 或环境变量 `GMP_SIM_SOCK` 指一个短的 |
| 同时跑两份，互相把对方的 socket 删了 | 两份各给一个 `--sim-sock`，日志文件也跟着分开 |
| 跑着跑着进程没了 | 多半是内存不够被系统杀掉，`dmesg` 里能看到 |
| 比自测那行报的速率慢很多 | 机器上有别的进程在抢 CPU |
