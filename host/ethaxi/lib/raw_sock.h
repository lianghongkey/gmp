// AF_PACKET 裸以太网帧收发。需要 CAP_NET_RAW。
#pragma once
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <string>

#include <arpa/inet.h>
#include <fcntl.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

namespace rlink {

inline uint64_t now_ns() {
    timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return uint64_t(ts.tv_sec) * 1000000000ull + uint64_t(ts.tv_nsec);
}

class RawSock {
public:
    RawSock() = default;
    ~RawSock() { close(); }
    RawSock(const RawSock&) = delete;
    RawSock& operator=(const RawSock&) = delete;

    // 打开并绑到网口 ifname，只收 ethertype 的帧。失败返回 false，err() 给原因。
    bool open(const std::string& ifname, uint16_t ethertype = 0x88B6) {
        close();
        fd_ = ::socket(AF_PACKET, SOCK_RAW, htons(ethertype));
        if (fd_ < 0) { err_ = std::string("socket(AF_PACKET) 失败：") + strerror(errno) +
                              "（需要 CAP_NET_RAW：sudo 运行，或 setcap cap_net_raw+ep）"; return false; }

        ifreq ifr{}; std::strncpy(ifr.ifr_name, ifname.c_str(), IFNAMSIZ - 1);
        if (::ioctl(fd_, SIOCGIFINDEX, &ifr) < 0) { err_ = "找不到网口 " + ifname + "：" + strerror(errno); close(); return false; }
        ifindex_ = ifr.ifr_ifindex;
        if (::ioctl(fd_, SIOCGIFHWADDR, &ifr) == 0) std::memcpy(mac_, ifr.ifr_hwaddr.sa_data, 6);
        if (::ioctl(fd_, SIOCGIFFLAGS, &ifr) == 0) up_ = (ifr.ifr_flags & IFF_UP) && (ifr.ifr_flags & IFF_RUNNING);

        sockaddr_ll sll{}; sll.sll_family = AF_PACKET; sll.sll_protocol = htons(ethertype); sll.sll_ifindex = ifindex_;
        if (::bind(fd_, reinterpret_cast<sockaddr*>(&sll), sizeof sll) < 0) { err_ = std::string("bind 失败：") + strerror(errno); close(); return false; }

        packet_mreq mr{}; mr.mr_ifindex = ifindex_; mr.mr_type = PACKET_MR_PROMISC;
        if (::setsockopt(fd_, SOL_PACKET, PACKET_ADD_MEMBERSHIP, &mr, sizeof mr) < 0) {
            err_ = std::string("开混杂失败：") + strerror(errno); close(); return false;
        }
        int sz = 4 << 20;   // 4MB 收发缓冲：一个 GBN 窗口只有几十 KB，留大余量防 pump 间隙丢帧
        ::setsockopt(fd_, SOL_SOCKET, SO_RCVBUF, &sz, sizeof sz);
        ::setsockopt(fd_, SOL_SOCKET, SO_SNDBUF, &sz, sizeof sz);
        int fl = ::fcntl(fd_, F_GETFL, 0);
        ::fcntl(fd_, F_SETFL, fl | O_NONBLOCK);
        ifname_ = ifname;
        return true;
    }

    void close() { if (fd_ >= 0) ::close(fd_); fd_ = -1; }
    bool is_open() const { return fd_ >= 0; }

    // 发一帧（含以太头）。
    bool send(const uint8_t* frame, int len) {
        sockaddr_ll sll{}; sll.sll_family = AF_PACKET; sll.sll_ifindex = ifindex_; sll.sll_halen = 6;
        std::memcpy(sll.sll_addr, frame, 6);
        for (;;) {
            ssize_t n = ::sendto(fd_, frame, len, 0, reinterpret_cast<sockaddr*>(&sll), sizeof sll);
            if (n == len) { tx_++; return true; }
            if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == ENOBUFS)) continue;   // 发送队列满：等
            if (n < 0 && errno == EINTR) continue;
            tx_err_++; return false;
        }
    }

    // 非阻塞收一帧到 buf；没有帧返回 -1。只会收到绑定的 ethertype。
    int recv(uint8_t* buf, int cap) {
        ssize_t n = ::recv(fd_, buf, cap, MSG_DONTWAIT | MSG_TRUNC);
        if (n < 0) return -1;
        rx_++;
        return int(n > cap ? cap : n);
    }

    // 内核侧丢帧计数（累加）。
    uint32_t drops() {
        tpacket_stats st{}; socklen_t l = sizeof st;
        if (::getsockopt(fd_, SOL_PACKET, PACKET_STATISTICS, &st, &l) == 0) drop_ += st.tp_drops;
        return drop_;
    }

    bool link_up() const { return up_; }
    const uint8_t* mac() const { return mac_; }
    const std::string& err() const { return err_; }
    uint64_t rx_frames() const { return rx_; }
    uint64_t tx_frames() const { return tx_; }
    uint64_t tx_errors() const { return tx_err_; }

private:
    int fd_ = -1, ifindex_ = 0;
    bool up_ = false;
    uint8_t mac_[6] = {0};
    std::string ifname_, err_;
    uint64_t rx_ = 0, tx_ = 0, tx_err_ = 0; uint32_t drop_ = 0;
};

} // namespace rlink
