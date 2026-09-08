// RLINK 端点：Go-Back-N 可靠传输。
#pragma once
#include "rlink_proto.h"
#include <cstdint>
#include <cstring>
#include <vector>
#include <deque>
#include <functional>

namespace rlink {

class Endpoint {
public:
    // tx: 把组好的一帧发出去；deliver: 按序交付一条 payload
    using TxCb      = std::function<void(const uint8_t*, int)>;
    using DeliverCb = std::function<void(const uint8_t*, int)>;

    Endpoint(const uint8_t local_mac[6], const uint8_t peer_mac[6],
             uint32_t window, uint64_t rto_ticks,
             TxCb tx, DeliverCb deliver)
        : W_(window), rto_(rto_ticks), tx_(std::move(tx)), deliver_(std::move(deliver)) {
        memcpy(lmac_, local_mac, 6); memcpy(pmac_, peer_mac, 6);
        slot_.resize(W_);
    }

    // 上层要可靠发一条消息（≤kMaxPayload 字节）。窗口满则排队，poll() 时再发。
    void send(const uint8_t* data, int len) {
        std::vector<uint8_t> p(data, data + len);
        outq_.push_back(std::move(p));
    }

    // 收到一帧（含以太头）。解析、推进 GBN 收端、抽对端 ack。
    void on_rx_frame(const uint8_t* f, int flen) {
        if (flen < kHdrLen) return;
        auto* eth = reinterpret_cast<const EthHdr*>(f);
        if (eth->ethertype != htons(kEtherType)) return;
        auto* h = reinterpret_cast<const RlinkHdr*>(f + kEthHdrLen);
        if (h->ver != kVer) return;
        uint32_t seq = rl_seq(h), ack = rl_ack(h);
        uint16_t len = rl_len(h);
        // 收端：DATA 且按序 → 交付
        if (h->type == kTypeData && seq == rcv_nxt_) {
            const uint8_t* pay = f + kHdrLen;
            if (kHdrLen + len <= flen) deliver_(pay, len);
            rcv_nxt_++;
            ack_pending_ = true;
        } else if (h->type == kTypeData) {
            ack_pending_ = true;   // 乱序：回重复 ACK 提示对端
        }
        // 发端：吸对端累计 ack，滑窗
        if (ack > snd_una_ && ack <= snd_nxt_) {
            while (snd_una_ < ack) { slot_[snd_una_ % W_].acked = true; snd_una_++; }
            last_progress_ = now();
            rt_active_ = false;
        }
    }

    // 周期调用：填窗发新帧、超时重传、必要时发纯 ACK。返回是否还有在途/待发（用于判完成）。
    bool poll() {
        // 1) 把排队消息装进窗口（有空槽）
        while (!outq_.empty() && (snd_nxt_ - snd_una_) < W_) {
            Slot& s = slot_[snd_nxt_ % W_];
            s.payload = std::move(outq_.front()); outq_.pop_front();
            s.acked = false; s.sent = false;
            snd_nxt_++;   // 占位；实际帧在下面发
        }
        // 2) 超时 → GBN 重传 [snd_una, snd_nxt)
        if (snd_una_ != snd_nxt_ && (now() - last_progress_) >= rto_) {
            rt_active_ = true; rt_ptr_ = snd_una_; last_progress_ = now();
        }
        // 3) 发：重传优先，其次新帧，其次纯 ACK
        if (rt_active_ && rt_ptr_ < snd_nxt_) {
            send_frame(rt_ptr_, false); rt_ptr_++;
            if (rt_ptr_ >= snd_nxt_) rt_active_ = false;
        } else {
            // 新帧 = 已占位但未发过的最小 seq
            uint32_t s = snd_una_;
            while (s < snd_nxt_ && slot_[s % W_].sent) s++;
            if (s < snd_nxt_) {
                send_frame(s, false); slot_[s % W_].sent = true;
                if (!sent_initialized_ || s + 1 > snd_max_) snd_max_ = s + 1;
            } else if (ack_pending_) {
                send_frame(snd_nxt_, true);   // 纯 ACK
                ack_pending_ = false;
            }
        }
        return (snd_una_ != snd_nxt_) || !outq_.empty();
    }

    uint32_t rcv_nxt() const { return rcv_nxt_; }
    uint32_t snd_una() const { return snd_una_; }
    uint32_t snd_nxt() const { return snd_nxt_; }

    // 时间源
    std::function<uint64_t()> time_now = [](){ return uint64_t(0); };

private:
    struct Slot { std::vector<uint8_t> payload; bool acked=false, sent=false; };
    uint64_t now() { return time_now(); }

public:
    // 会话起手：两端序号归零。
    void send_reset() {
        snd_una_ = snd_nxt_ = snd_max_ = rcv_nxt_ = 0;
        rt_active_ = false; ack_pending_ = false;
        uint8_t buf[60];
        auto* eth = reinterpret_cast<EthHdr*>(buf);
        memcpy(eth->dst, pmac_, 6); memcpy(eth->src, lmac_, 6);
        eth->ethertype = htons(kEtherType);
        auto* h = reinterpret_cast<RlinkHdr*>(buf + kEthHdrLen);
        h->ver = kVer; h->type = kTypeAck; h->flags = kFlagSyn; h->rsvd = 0;
        h->seq = htonl(0); h->ack = htonl(0);
        h->len = 0; h->win = htons((uint16_t)W_);
        memset(buf + kHdrLen, 0, 60 - kHdrLen);
        tx_(buf, 60);
    }
private:

    void send_frame(uint32_t seq, bool ack_only) {
        uint8_t buf[kHdrLen + kMaxPayload];
        auto* eth = reinterpret_cast<EthHdr*>(buf);
        memcpy(eth->dst, pmac_, 6); memcpy(eth->src, lmac_, 6);
        eth->ethertype = htons(kEtherType);
        auto* h = reinterpret_cast<RlinkHdr*>(buf + kEthHdrLen);
        h->ver = kVer; h->type = ack_only ? kTypeAck : kTypeData; h->flags = 0; h->rsvd = 0;
        h->seq = htonl(seq);
        h->ack = htonl(rcv_nxt_);
        int plen = 0;
        if (!ack_only) { auto& p = slot_[seq % W_].payload; plen = (int)p.size();
                         memcpy(buf + kHdrLen, p.data(), plen); }
        h->len = htons((uint16_t)plen);
        h->win = htons((uint16_t)(W_));
        int flen = kHdrLen + plen;
        if (flen < 60) { memset(buf + flen, 0, 60 - flen); flen = 60; }   // 最小帧（host 自己补；NIC 也会补）
        tx_(buf, flen);
    }

    uint32_t W_;
    uint64_t rto_;
    TxCb tx_;
    DeliverCb deliver_;
    uint8_t lmac_[6], pmac_[6];

    std::vector<Slot>   slot_;
    std::deque<std::vector<uint8_t>> outq_;
    uint32_t snd_una_ = 0, snd_nxt_ = 0, snd_max_ = 0;
    uint32_t rcv_nxt_ = 0;
    uint32_t rt_ptr_ = 0;
    bool     rt_active_ = false, ack_pending_ = false, sent_initialized_ = false;
    uint64_t last_progress_ = 0;
};

} // namespace rlink
