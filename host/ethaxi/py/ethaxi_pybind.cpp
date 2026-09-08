// python 模块 ethaxi：一个 Device，读写 AXI4-Lite。出错抛 RuntimeError，收发期间放开 GIL。
#include "rlink_axi.h"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;
using ethaxi::Device;
using ethaxi::DeviceConfig;

static void set_mac(uint8_t (&dst)[6], const py::bytes& b, const char* what) {
    std::string s = b;
    if (s.size() != 6) throw std::invalid_argument(std::string(what) + " 必须是 6 字节");
    std::memcpy(dst, s.data(), 6);
}

PYBIND11_MODULE(ethaxi, m) {
    m.doc() = "host 经 1GbE（RLINK）读写 AXI4-Lite 的设备库（lib/ 的 python 封装）";
    py::class_<Device>(m, "Device")
        .def(py::init<>())
        .def("open",
             [](Device& self, const std::string& ifname, uint32_t window, uint32_t rto_us, bool verbose,
                py::object host_mac, py::object fpga_mac) {
                 DeviceConfig cfg; cfg.ifname = ifname; cfg.window = window; cfg.rto_us = rto_us; cfg.verbose = verbose;
                 if (!host_mac.is_none()) set_mac(cfg.host_mac, host_mac.cast<py::bytes>(), "host_mac");
                 if (!fpga_mac.is_none()) set_mac(cfg.fpga_mac, fpga_mac.cast<py::bytes>(), "fpga_mac");
                 py::gil_scoped_release nogil;
                 self.open(cfg);
             },
             py::arg("ifname"), py::arg("window") = 16, py::arg("rto_us") = 4000, py::arg("verbose") = false,
             py::arg("host_mac") = py::none(), py::arg("fpga_mac") = py::none(),
             "打开网口（AF_PACKET）并做会话复位。window < 32（FPGA 回程窗口）。")
        .def("close", &Device::close)
        .def("is_open", &Device::is_open)
        .def("write32", [](Device& self, uint32_t addr, uint32_t v) { py::gil_scoped_release nogil; self.write32(addr, v); },
             py::arg("addr"), py::arg("data"))
        .def("read32", [](Device& self, uint32_t addr) { py::gil_scoped_release nogil; return self.read32(addr); }, py::arg("addr"))
        .def("write_burst",
             [](Device& self, uint32_t addr, const std::vector<uint32_t>& words) {
                 py::gil_scoped_release nogil; self.write_words(addr, words.data(), words.size());
             }, py::arg("addr"), py::arg("words"), "连续地址写一串 32 位字。")
        .def("read_burst",
             [](Device& self, uint32_t addr, size_t n) {
                 std::vector<uint32_t> out(n);
                 { py::gil_scoped_release nogil; self.read_words(addr, out.data(), n); }
                 return out;
             }, py::arg("addr"), py::arg("n"), "连续地址读 n 个 32 位字，返回 list。")
        .def("write_bytes",
             [](Device& self, uint32_t addr, py::buffer data) {
                 py::buffer_info info = data.request(false);
                 size_t n = size_t(info.size) * size_t(info.itemsize);
                 if (n % 4) throw std::invalid_argument("write_bytes：长度须是 4 的倍数");
                 std::vector<uint32_t> w(n / 4);
                 std::memcpy(w.data(), info.ptr, n);          // 小端主机：字节序与线上一致
                 py::gil_scoped_release nogil;
                 self.write_words(addr, w.data(), w.size());
             }, py::arg("addr"), py::arg("data"), "连续地址写一段字节（bytes/bytearray/memoryview，长度 4 对齐），免去字列表转换。")
        .def("read_bytes",
             [](Device& self, uint32_t addr, size_t n) {
                 if (n % 4) throw std::invalid_argument("read_bytes：长度须是 4 的倍数");
                 PyObject* obj = PyBytes_FromStringAndSize(nullptr, (Py_ssize_t)n);
                 if (!obj) throw py::error_already_set();
                 uint32_t* p = reinterpret_cast<uint32_t*>(PyBytes_AS_STRING(obj));
                 { py::gil_scoped_release nogil; self.read_words(addr, p, n / 4); }
                 return py::reinterpret_steal<py::bytes>(obj);
             }, py::arg("addr"), py::arg("n"), "连续地址读 n 字节（4 对齐），返回 bytes。")
        .def("write_many",
             [](Device& self, const std::vector<std::pair<uint32_t, uint32_t>>& pairs) {
                 py::gil_scoped_release nogil;
                 self.write_many(pairs);
             }, py::arg("pairs"), "写 [(addr, data), ...]：每条一笔消息、多条在飞，次序保持。")
        .def("read_many",
             [](Device& self, const std::vector<uint32_t>& addrs) {
                 py::gil_scoped_release nogil;
                 return self.read_many(addrs);
             }, py::arg("addrs"), "读 [addr, ...]：每个地址一笔消息、多条在飞，返回等长 list。")
        .def("last_err", &Device::last_err, "最近一次失败的 err 位（bit0 AXI 错误，bit1 长度不符）。")
        .def("__enter__", [](Device& self) -> Device& { return self; })
        .def("__exit__", [](Device& self, py::object, py::object, py::object) { self.close(); return false; });

}
