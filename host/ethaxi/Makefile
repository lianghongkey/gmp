# ethaxi —— host 经 1GbE 读写板子的那条通路。
#   lib/  链路与 AXI 客户端（头文件）   py/  python 模块 ethaxi   cli/  ethaxi_run
#
#   make          编 ethaxi_run
#   make module   编 python 模块 ethaxi
#   产物进 build/。AF_PACKET 要 CAP_NET_RAW，一次性：
#       sudo setcap cap_net_raw+eip build/ethaxi_run
#   之后 soc_generate.py 自己经它重跑一遍拿权限，平时的命令不用加前缀。

CXX     ?= g++
CC      ?= gcc
PYTHON  ?= python
BUILD   := build
LIBH    := $(wildcard lib/*.h)

.DEFAULT_GOAL := all
$(BUILD):
	@mkdir -p $(BUILD)

all: $(BUILD)/ethaxi_run

# 真实文件目标：只在源码比二进制新时才重编 —— 重编会抹掉授过的 capability。
$(BUILD)/ethaxi_run: cli/ethaxi_run.c | $(BUILD)
	$(CC) -O2 -Wall -Wextra cli/ethaxi_run.c -o $@
	@echo "── $@ 已编译；一次性授权：sudo setcap cap_net_raw+eip $@ ──"

module: py/ethaxi_pybind.cpp $(LIBH) setup.py | $(BUILD)
	$(PYTHON) setup.py build_ext --build-lib $(BUILD)
	@echo "── 已编译 python 模块 ethaxi → $(BUILD)/ ──"

clean:
	rm -rf $(BUILD) *.egg-info __pycache__

.PHONY: all module clean
