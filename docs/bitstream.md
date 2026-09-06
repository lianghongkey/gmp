# bitstream 与实测

本文给出两份 bitstream 各跑多快、在片上占多少资源，用来判断一次运行的耗时是否合理。

**文档模式**：实测数。

## 时钟与两份 bitstream

板上的 50 MHz 晶振接进 MMCM，分出功能时钟与它的两倍频。

| bitstream | clk / clk2x | decode 一步 | 吞吐 | 板上验证 |
| - | - | -: | -: | - |
| `top_jtag_p3.bit` | 50 / 100 MHz | 24,312,970 拍，0.50 s | 2.0 token/s | 整份判据 73 项全对 |
| `top_jtag_c66n.bit` | 66.7 / 133 MHz | — | 2.6 token/s | 64 步 decode 判据全对 |

prefill 一轮（64 个 token）在 50 MHz 那份上是 64,000,909 拍，1.3 秒。

`top_jtag_p3.bit` 布局布线之后的用量：LUT 242,767（81.30%）、FF 146,086、BRAM 382.5 块、DSP 980，
布线后最差裕量 +0.127 ns。这片器件一共 298,600 个 LUT、955 块 RAMB36、1920 个 DSP。

## 片上占用

![按单元着色的布线后布局](placement.png)

图是一次布线后的布局，每个 cell 按所属模块着色，圆圈是 DSP48 与 RAMB36。整片器件的可编程
逻辑用掉八成。这张图不是发布的那两份 bitstream 的布局。
