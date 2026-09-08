// host 侧「RLINK 字节流 ⇆ AXI4-Lite」客户端，跑在 rlink::Endpoint 之上。
// Client 组包、分块、按序收响应；Device 把它与 AF_PACKET 传输装在一起。
#pragma once
#include "rlink.h"
#include "raw_sock.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <memory>
#include <functional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace ethaxi {

static constexpr uint8_t  OP_WRITE = 0x11, OP_READ = 0x12, OP_WACK = 0x91, OP_RRESP = 0x92;
static constexpr size_t   MAX_WORDS = 366;                    // (1472 − 8) / 4
static constexpr uint8_t  ERR_AXI = 0x01, ERR_LEN = 0x02;

inline void put32(std::vector<uint8_t>& v, uint32_t x) {
    v.push_back(uint8_t(x)); v.push_back(uint8_t(x >> 8)); v.push_back(uint8_t(x >> 16)); v.push_back(uint8_t(x >> 24));
}
inline uint32_t get32(const uint8_t* p) {
    return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}

class Client {
public:
    using Pump = std::function<void()>;

    size_t inflight = 16;
    uint64_t idle_limit = 5'000'000;

    explicit Client(rlink::Endpoint& e) : ep(e) {}
    void on_response(const uint8_t* p, int len) { resp_q.emplace_back(p, p + len); }

    // 连续地址写 n 个字。
    bool write_words(uint32_t addr, const uint32_t* w, size_t n, Pump pump) {
        std::vector<Chunk> ck = chunks(addr, n);
        size_t sent = 0, got = 0; uint64_t idle = 0;
        last_err_ = 0;
        while (got < ck.size()) {
            while (sent < ck.size() && (sent - got) < inflight) {
                std::vector<uint8_t> m; m.reserve(8 + 4 * ck[sent].n);
                put32(m, (uint32_t(ck[sent].n) << 16) | OP_WRITE); put32(m, ck[sent].addr);
                for (size_t i = 0; i < ck[sent].n; i++) put32(m, w[ck[sent].off + i]);
                ep.send(m.data(), (int)m.size()); sent++;
            }
            pump();
            bool prog = false;
            while (!resp_q.empty()) {
                auto r = std::move(resp_q.front()); resp_q.pop_front();
                if (!check_hdr(r, OP_WACK, ck[got], 8, "WACK")) return false;
                got++; prog = true;
            }
            if (prog) idle = 0;
            else if (++idle > idle_limit) return stall("写", got, ck.size(), sent);
        }
        return true;
    }

    // 连续地址读 n 个字到 out。
    bool read_words(uint32_t addr, uint32_t* out, size_t n, Pump pump) {
        std::vector<Chunk> ck = chunks(addr, n);
        size_t sent = 0, got = 0; uint64_t idle = 0;
        last_err_ = 0;
        while (got < ck.size()) {
            while (sent < ck.size() && (sent - got) < inflight) {
                std::vector<uint8_t> m;
                put32(m, (uint32_t(ck[sent].n) << 16) | OP_READ); put32(m, ck[sent].addr);
                ep.send(m.data(), (int)m.size()); sent++;
            }
            pump();
            bool prog = false;
            while (!resp_q.empty()) {
                auto r = std::move(resp_q.front()); resp_q.pop_front();
                if (!check_hdr(r, OP_RRESP, ck[got], 8 + 4 * ck[got].n, "RRESP")) return false;
                for (size_t i = 0; i < ck[got].n; i++) out[ck[got].off + i] = get32(r.data() + 8 + 4 * i);
                got++; prog = true;
            }
            if (prog) idle = 0;
            else if (++idle > idle_limit) return stall("读", got, ck.size(), sent);
        }
        return true;
    }

    // 不连续地址的一组单字写，次序保持。
    bool write_scatter(const std::pair<uint32_t, uint32_t>* items, size_t n, Pump pump) {
        size_t sent = 0, got = 0; uint64_t idle = 0;
        last_err_ = 0;
        while (got < n) {
            while (sent < n && (sent - got) < inflight) {
                std::vector<uint8_t> m; m.reserve(12);
                put32(m, (1u << 16) | OP_WRITE); put32(m, items[sent].first); put32(m, items[sent].second);
                ep.send(m.data(), (int)m.size()); sent++;
            }
            pump();
            bool prog = false;
            while (!resp_q.empty()) {
                auto r = std::move(resp_q.front()); resp_q.pop_front();
                Chunk c{items[got].first, 0, 1};
                if (!check_hdr(r, OP_WACK, c, 8, "WACK")) return false;
                got++; prog = true;
            }
            if (prog) idle = 0;
            else if (++idle > idle_limit) return stall("散写", got, n, sent);
        }
        return true;
    }

    // 不连续地址的一组单字读。
    bool read_scatter(const uint32_t* addrs, uint32_t* out, size_t n, Pump pump) {
        size_t sent = 0, got = 0; uint64_t idle = 0;
        last_err_ = 0;
        while (got < n) {
            while (sent < n && (sent - got) < inflight) {
                std::vector<uint8_t> m; m.reserve(8);
                put32(m, (1u << 16) | OP_READ); put32(m, addrs[sent]);
                ep.send(m.data(), (int)m.size()); sent++;
            }
            pump();
            bool prog = false;
            while (!resp_q.empty()) {
                auto r = std::move(resp_q.front()); resp_q.pop_front();
                Chunk c{addrs[got], 0, 1};
                if (!check_hdr(r, OP_RRESP, c, 12, "RRESP")) return false;
                out[got] = get32(r.data() + 8);
                got++; prog = true;
            }
            if (prog) idle = 0;
            else if (++idle > idle_limit) return stall("散读", got, n, sent);
        }
        return true;
    }

    uint8_t last_err() const { return last_err_; }
    const std::string& last_msg() const { return last_msg_; }

private:
    struct Chunk { uint32_t addr; size_t off; size_t n; };
    static std::vector<Chunk> chunks(uint32_t addr, size_t n) {
        std::vector<Chunk> v;
        if (n == 0) { v.push_back({addr, 0, 0}); return v; }      // n=0 也发一条（探链路 / 空写）
        for (size_t off = 0; off < n; ) {
            size_t k = std::min(MAX_WORDS, n - off);
            v.push_back({uint32_t(addr + 4 * off), off, k}); off += k;
        }
        return v;
    }
    bool check_hdr(const std::vector<uint8_t>& r, uint8_t op, const Chunk& c, size_t len, const char* what) {
        if (r.size() != len || (r[0] != op) || (get32(r.data()) >> 16) != c.n || get32(r.data() + 4) != c.addr) {
            char buf[200];
            std::snprintf(buf, sizeof buf, "%s 不符：期望 op=%02x n=%zu addr=0x%08x len=%zu，收到 op=%02x n=%u addr=0x%08x len=%zu",
                          what, op, c.n, c.addr, len, r.empty() ? 0 : r[0], r.size() >= 4 ? get32(r.data()) >> 16 : 0,
                          r.size() >= 8 ? get32(r.data() + 4) : 0, r.size());
            last_msg_ = buf; return false;
        }
        uint8_t err = r[1];
        if (err) {
            last_err_ = err;
            char buf[120];
            std::snprintf(buf, sizeof buf, "%s err=0x%02x @0x%08x（bit0=AXI SLVERR/DECERR，bit1=消息长度不符）", what, err, c.addr);
            last_msg_ = buf; return false;
        }
        return true;
    }
    bool stall(const char* what, size_t got, size_t total, size_t sent) {
        char buf[160];
        std::snprintf(buf, sizeof buf, "%s卡死 @块 %zu/%zu sent=%zu（无新响应；链路 / FPGA 停了）", what, got, total, sent);
        last_msg_ = buf; return false;
    }

    rlink::Endpoint& ep;
    std::deque<std::vector<uint8_t>> resp_q;
    uint8_t last_err_ = 0;
    std::string last_msg_;
};

// ── Device：传输 + Endpoint + Client。失败抛 std::runtime_error。──
struct DeviceConfig {
    std::string ifname;                 // 内核网口名
    uint32_t    window = 16;            // host GBN 窗口（< FPGA 回程窗口 32）
    uint32_t    rto_us = 4000;          // 重传超时
    bool        verbose = false;
    uint8_t     host_mac[6] = {0x02,0x00,0x00,0x00,0x00,0x01};
    uint8_t     fpga_mac[6] = {0x02,0x00,0x00,0x00,0x00,0x02};
};

class Device {
public:
    Device() = default;
    ~Device() { close(); }
    Device(const Device&) = delete;
    Device& operator=(const Device&) = delete;

    void open(const DeviceConfig& cfg) {
        if (opened_) fail("设备已打开（先 close）");
        cfg_ = cfg;
        if (!sock_.open(cfg.ifname)) fail(sock_.err());
        if (cfg.verbose) {
            const uint8_t* m = sock_.mac();
            std::printf("[ethaxi] 网口 %s（%02x:%02x:%02x:%02x:%02x:%02x）link %s\n", cfg.ifname.c_str(),
                        m[0], m[1], m[2], m[3], m[4], m[5], sock_.link_up() ? "UP" : "DOWN（RX 必为 0，查链路）");
        }
        auto tx_cb = [this](const uint8_t* f, int len) { sock_.send(f, len); };
        auto deliver_cb = [this](const uint8_t* p, int len) { client_->on_response(p, len); };
        ep_ = std::make_unique<rlink::Endpoint>(cfg.host_mac, cfg.fpga_mac, cfg.window, uint64_t(cfg.rto_us) * 1000, tx_cb, deliver_cb);
        ep_->time_now = [] { return rlink::now_ns(); };
        client_ = std::make_unique<Client>(*ep_);
        client_->inflight = cfg.window;
        // 会话复位
        for (int i = 0; i < 8; i++) {
            ep_->send_reset(); usleep(2000);
            uint8_t buf[2048]; while (sock_.recv(buf, sizeof buf) >= 0) {}
        }
        opened_ = true;
    }
    void close() { if (!opened_) return; sock_.close(); client_.reset(); ep_.reset(); opened_ = false; }
    bool is_open() const { return opened_; }

    void write_words(uint32_t addr, const uint32_t* w, size_t n) {
        need_open();
        if (!client_->write_words(addr, w, n, pump())) fail("write_words 失败：" + client_->last_msg());
    }
    void read_words(uint32_t addr, uint32_t* out, size_t n) {
        need_open();
        if (!client_->read_words(addr, out, n, pump())) fail("read_words 失败：" + client_->last_msg());
    }
    void write32(uint32_t addr, uint32_t v) { write_words(addr, &v, 1); }
    void write_many(const std::vector<std::pair<uint32_t, uint32_t>>& items) {
        need_open();
        if (!client_->write_scatter(items.data(), items.size(), pump())) fail("write_many 失败：" + client_->last_msg());
    }
    uint32_t read32(uint32_t addr) { uint32_t v = 0; read_words(addr, &v, 1); return v; }
    std::vector<uint32_t> read_many(const std::vector<uint32_t>& addrs) {
        need_open();
        std::vector<uint32_t> out(addrs.size());
        if (!client_->read_scatter(addrs.data(), out.data(), addrs.size(), pump())) fail("read_many 失败：" + client_->last_msg());
        return out;
    }
    uint8_t last_err() const { return client_ ? client_->last_err() : 0; }

private:
    [[noreturn]] static void fail(const std::string& m) { throw std::runtime_error("ethaxi::Device: " + m); }
    void need_open() const { if (!opened_) fail("设备未打开"); }
    Client::Pump pump() {
        return [this] {
            uint8_t buf[2048];
            for (int i = 0; i < 64; i++) { int n = sock_.recv(buf, sizeof buf); if (n < 0) break; ep_->on_rx_frame(buf, n); }
            for (int i = 0; i < 64; i++) ep_->poll();
        };
    }

    DeviceConfig cfg_;
    bool opened_ = false;
    rlink::RawSock sock_;
    std::unique_ptr<rlink::Endpoint> ep_;
    std::unique_ptr<Client> client_;
};

} // namespace ethaxi
