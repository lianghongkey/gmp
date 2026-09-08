# 构建 python 模块 `ethaxi`（ethaxi::Device 的 pybind11 封装：host 经 1GbE RLINK 读写 FPGA 侧 AXI4-Lite，AF_PACKET 传输）。
#   需要 pybind11（pip install pybind11）。编译：make module（.so 进 build/）。
from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

# 静态链接 libstdc++/libgcc：把 C++ 运行时打进 .so，不依赖运行用的 python 自带哪个 libstdc++。
ext = Pybind11Extension(
    "ethaxi",
    sources=["py/ethaxi_pybind.cpp"],
    extra_compile_args=["-O3", "-Ilib"],
    extra_link_args=["-static-libstdc++", "-static-libgcc"],
    cxx_std=20,
)

setup(
    name="ethaxi",
    version="0.1.0",
    description="host 经 1GbE（RLINK）读写 FPGA 侧 AXI4-Lite 的设备库（pybind11）",
    ext_modules=[ext],
    cmdclass={"build_ext": build_ext},
)
