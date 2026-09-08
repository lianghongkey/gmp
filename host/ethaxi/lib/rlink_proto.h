// RLINK 线上协议的常量与帧结构。
#pragma once
#include <cstdint>
#include <arpa/inet.h>

namespace rlink {

static constexpr uint16_t kEtherType = 0x88B6;
static constexpr uint8_t  kVer       = 1;
enum : uint8_t { kTypeData = 0, kTypeAck = 1 };
static constexpr uint8_t  kFlagSyn   = 0x01;

struct __attribute__((packed)) EthHdr {
    uint8_t  dst[6];
    uint8_t  src[6];
    uint16_t ethertype;     // 大端 = htons(kEtherType)
};
struct __attribute__((packed)) RlinkHdr {
    uint8_t  ver;
    uint8_t  type;
    uint8_t  flags;
    uint8_t  rsvd;
    uint32_t seq;           // 大端
    uint32_t ack;           // 大端
    uint16_t len;           // 大端：payload 真实字节数（区分真 payload vs 以太最小帧填充）
    uint16_t win;           // 大端
};
static_assert(sizeof(EthHdr) == 14, "EthHdr 14B");
static_assert(sizeof(RlinkHdr) == 16, "RlinkHdr 16B");

static constexpr int kEthHdrLen   = 14;
static constexpr int kRlinkHdrLen = 16;
static constexpr int kHdrLen      = 30;             // 以太 + RLINK
static constexpr int kMaxPayload  = 1472;           // ≤ MTU(1500) - RLINK头(16) 留余量

inline uint32_t rl_seq(const RlinkHdr *h) { return ntohl(h->seq); }
inline uint32_t rl_ack(const RlinkHdr *h) { return ntohl(h->ack); }
inline uint16_t rl_len(const RlinkHdr *h) { return ntohs(h->len); }
inline uint16_t rl_win(const RlinkHdr *h) { return ntohs(h->win); }

} // namespace rlink
