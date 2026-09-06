# GMP: an NPU on an FPGA that generates text on the board

[中文](README.md)

This page tells you what this release is, what the board has actually done, what ships in the
package, and which document to read next.

## What this is

GMP is a neural-network accelerator we designed ourselves, synthesized onto a single Kintex-7
(xc7k480t), with the weights of Qwen3 0.6B sitting in DDR3. The host sends a piece of text in
over JTAG; the chip runs the whole network token by token on its own and hands the generated
text back for the host to print.

The operator sequence for the whole network is compiled ahead of time into a program that lives
in DRAM. The chip follows that program once it starts; the host only feeds the prompt and picks
up the result. The RTL is not hand-written Verilog — it is written in pyrilog, which describes
hardware in Python and emits Verilog deterministically.

## What the board has done

![the board, front and back](demo/board_front_back.jpg)

The tail of one real run (2026-09-04; the full transcript is in
[`docs/usage.md`](docs/usage.md), in Chinese):

```text
    权重 604.0 MiB 灌完，17.9 分钟
    灌完再抽查 16 处一致
    清零：64 段，共 14.0 MiB，25 s
    CPU 报到，程序版本 1
    逐个喂 18 个：9.3 s
── 生成 ──
我是AI助手，专注于帮助用户解决问题和提供支持。
── 生成 13 个 token（遇到停止 token），decode 平均 0.49 s/步 ──
```

The same prompts, compared word for word against llama.cpp doing greedy decoding from the same
GGUF (board freshly power-cycled):

| Prompt | Generated on the board | vs. llama.cpp (f32) |
| - | - | - |
| 今天天气不错，我们去 (6 tokens) | 公园散步吧。这句话中，“我们”指的是谁 | identical, all 12 tokens |
| 请用一句话介绍一下你自己。 (chat template) | 我是AI助手，专注于帮助用户解决问题和提供支持。 | identical, stops at the same end-of-text token |
| a 70-token passage (takes the prefill path) | 神经网络被引入，使得模型能够处理更复杂的非线性关系，从而在图像和 | identical, all 20 tokens |

Speed: on the 50 MHz bitstream one decode step takes 0.50 s (2.0 token/s) and a 64-token prefill
round takes 1.3 s; the 66.7 MHz bitstream reaches 2.6 token/s. The full acceptance run (65
rounds, 73 bit-exact checks) passes on the board.

## What ships here

| Path | Contents |
| - | - |
| `prebuilt/bit/` | Two bitstreams: `top_jtag_p3.bit` (50 MHz) and `top_jtag_c66n.bit` (66.7 MHz) |
| `prebuilt/sim/` | The whole-SoC simulator executable and the two data files it loads, for running the same flow without a board |
| `data/weights.npz` | Fixed-point weights; each array is named after the DRAM address it goes to |
| `data/load_image.bin` | The image the board loads itself from at power-up |
| `data/tokenizer.json.gz` | The vocabulary and merges, extracted from the Qwen3-0.6B GGUF |
| `host/` | All host-side code: `soc_generate.py` (entry point), `device.py` (talks to the board or the simulator), `runtime.py` (weights and per-round handshake), `hw_params.py` (constants) |
| `tools/pyjtag/` | A pure-Python JTAG stack; programming and register access need no Vivado |

What ships here are the compiled artifacts plus the host tools that drive them, so that the
results above can be reproduced on the same board. RTL sources, the compiler and the synthesis
scripts are not included, so nothing here can regenerate a bitstream or retarget another model.

## Where to start

The four documents under `docs/` are written in Chinese.

1. [`docs/setup.md`](docs/setup.md) — the board and cable required, what to install, how to grant
   USB permission.
2. [`docs/usage.md`](docs/usage.md) — the full flow from programming to generation, how long each
   step takes, and what to check when the output looks wrong.
3. [`docs/bitstream.md`](docs/bitstream.md) — how fast each of the two bitstreams runs and how
   much of the device it occupies.
4. [`docs/simulation.md`](docs/simulation.md) — how to run the same flow on the simulator when
   you have no board.

Once everything is in place, one command starts a conversation:

```bash
python host/soc_generate.py --chat "请用一句话介绍一下你自己。"
```

Without a board, the same command with `--sim` runs on the simulator instead: much slower,
everything else the same.

Photos and recordings of the board in action are under `demo/`.

## License

MIT, see `LICENSE`. Three third-party pieces keep their own terms: the vocabulary inside
`data/tokenizer.json.gz` comes from Qwen3-0.6B (Apache-2.0), `tools/pyjtag/xusb_*.hex` are
Xilinx cable firmware images distributed with the cable, and `prebuilt/sim/VSocCosimTop` links
in the Verilator 5.020 runtime library (dual-licensed LGPL-3.0-only or Artistic-2.0).
