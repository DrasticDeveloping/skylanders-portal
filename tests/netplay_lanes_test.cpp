#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <vector>

#include "netplay/datagram.h"
#include "netplay/fake_network.h"
#include "netplay/lanes.h"
#include "test_util.h"

using namespace skylanders::netplay;
using namespace std::chrono_literals;

namespace {

constexpr Endpoint kA{0x0A000001, 4000};
constexpr Endpoint kB{0x0A000002, 4000};

std::vector<uint8_t> Index(uint32_t i) {
  return {uint8_t(i), uint8_t(i >> 8), uint8_t(i >> 16), uint8_t(i >> 24)};
}
uint32_t IndexOf(const std::vector<uint8_t>& p) {
  return p.size() < 4 ? 0xFFFFFFFF : uint32_t(p[0] | p[1] << 8 | p[2] << 16 | uint32_t(p[3]) << 24);
}

// Two connections over a fake network, pumped together.
struct Pair {
  explicit Pair(uint64_t seed) : net(seed) {
    sa = net.Bind(kA);
    sb = net.Bind(kB);
    a = std::make_unique<Connection>([this](std::span<const uint8_t> d) { sa->Send(kB, d); },
                                     net.Now());
    b = std::make_unique<Connection>([this](std::span<const uint8_t> d) { sb->Send(kA, d); },
                                     net.Now());
  }
  void Feed() {
    std::vector<uint8_t> buf;
    Endpoint from;
    while (sa->Receive(buf, from)) {
      if (auto v = DecodeDatagram(buf)) a->OnDatagram(*v, net.Now());
    }
    while (sb->Receive(buf, from)) {
      if (auto v = DecodeDatagram(buf)) b->OnDatagram(*v, net.Now());
    }
  }
  void Step(Clock::duration d = 1ms) {
    Feed();
    a->Pump(net.Now());
    b->Pump(net.Now());
    net.Advance(d);
  }
  FakeNetwork net;
  std::unique_ptr<FakeNetwork::Socket> sa, sb;
  std::unique_ptr<Connection> a, b;
};

}  // namespace

int main() {
  // Reliable lanes through 30 % loss, duplicates and reordering, both directions at once: every
  // message arrives exactly once and in order, on each lane separately.
  {
    Pair p(12345);
    LinkConditions bad;
    bad.loss_percent = 30;
    bad.duplicate_percent = 10;
    bad.reorder_percent = 20;
    bad.reorder_ms = 25;
    bad.delay_ms = 20;
    bad.jitter_ms = 15;
    p.net.SetDefault(bad);

    constexpr uint32_t kCount = 3000;
    uint32_t sent_gameplay = 0, sent_state = 0, sent_back = 0;
    std::vector<uint32_t> got_gameplay, got_state, got_back;
    for (int step = 0; step < 300000; ++step) {
      // 8 per lane per millisecond, until the window says wait.
      for (int k = 0; k < 8 && sent_gameplay < kCount; ++k) {
        if (p.a->Send(Lane::kGameplay, Index(sent_gameplay), p.net.Now()) != SendResult::kSent) break;
        ++sent_gameplay;
      }
      for (int k = 0; k < 8 && sent_state < kCount; ++k) {
        if (p.a->Send(Lane::kState, Index(sent_state), p.net.Now()) != SendResult::kSent) break;
        ++sent_state;
      }
      for (int k = 0; k < 2 && sent_back < kCount / 4; ++k) {
        if (p.b->Send(Lane::kGameplay, Index(sent_back), p.net.Now()) != SendResult::kSent) break;
        ++sent_back;
      }
      p.Step();
      LaneMessage m;
      while (p.b->Receive(m)) {
        (m.lane == Lane::kGameplay ? got_gameplay : got_state).push_back(IndexOf(m.payload));
        CHECK(m.lane != Lane::kFast);
      }
      while (p.a->Receive(m)) got_back.push_back(IndexOf(m.payload));
      if (got_gameplay.size() == kCount && got_state.size() == kCount &&
          got_back.size() == kCount / 4 && p.a->UnackedTotal() == 0 && p.b->UnackedTotal() == 0) {
        break;
      }
    }
    CHECK(got_gameplay.size() == kCount);
    CHECK(got_state.size() == kCount);
    CHECK(got_back.size() == kCount / 4);
    bool in_order = true;
    for (uint32_t i = 0; i < got_gameplay.size(); ++i) in_order &= got_gameplay[i] == i;
    for (uint32_t i = 0; i < got_state.size(); ++i) in_order &= got_state[i] == i;
    for (uint32_t i = 0; i < got_back.size(); ++i) in_order &= got_back[i] == i;
    CHECK(in_order);
    CHECK(p.a->UnackedTotal() == 0);
    CHECK(p.b->UnackedTotal() == 0);
    // The bad link really was bad.
    CHECK(p.net.Stats().dropped > 1000);
    CHECK(p.net.Stats().duplicated > 300);
    CHECK(p.a->Stats().resent > 0);
    CHECK(p.b->Stats().duplicates > 0);
    // RTT about 2 x (20 ms + up to 15 ms jitter + some reordering).
    const auto rtt = p.a->SmoothedRtt();
    CHECK(p.a->HasRttSample());
    CHECK(rtt >= 35ms && rtt <= 90ms);
    std::printf("lossy link: %llu dropped, %llu resent by A, rtt %lld us\n",
                (unsigned long long)p.net.Stats().dropped,
                (unsigned long long)p.a->Stats().resent, (long long)rtt.count());
  }

  // The fast lane: newest only. Under reordering and duplication, what arrives is strictly
  // newer each time; under loss some never arrive and nothing is resent.
  {
    Pair p(777);
    LinkConditions c;
    c.loss_percent = 20;
    c.duplicate_percent = 20;
    c.reorder_percent = 30;
    c.reorder_ms = 10;
    c.delay_ms = 5;
    c.jitter_ms = 8;
    p.net.SetDefault(c);
    std::vector<uint32_t> got;
    for (uint32_t i = 0; i < 1000; ++i) {
      CHECK(p.a->Send(Lane::kFast, Index(i), p.net.Now()) == SendResult::kSent);
      p.Step();
      LaneMessage m;
      while (p.b->Receive(m)) {
        CHECK(m.lane == Lane::kFast);
        got.push_back(IndexOf(m.payload));
      }
    }
    for (int i = 0; i < 100; ++i) p.Step();
    LaneMessage m;
    while (p.b->Receive(m)) got.push_back(IndexOf(m.payload));
    bool increasing = true;
    for (size_t i = 1; i < got.size(); ++i) increasing &= got[i] > got[i - 1];
    CHECK(increasing);
    CHECK(got.size() > 300 && got.size() < 1000);
    CHECK(p.b->Stats().stale > 0);
    CHECK(p.a->Stats().resent == 0);
    CHECK(p.a->Unacked(Lane::kFast) == 0);
  }

  // RTT from acks on a clean link with 20 ms each way.
  {
    Pair p(1);
    LinkConditions c;
    c.delay_ms = 20;
    p.net.SetDefault(c);
    CHECK(!p.a->HasRttSample());
    CHECK(p.a->SmoothedRtt() == std::chrono::duration_cast<std::chrono::microseconds>(kInitialRtt));
    for (int i = 0; i < 2000; ++i) {
      if (i % 10 == 0) p.a->Send(Lane::kGameplay, Index(i), p.net.Now());
      p.Step();
    }
    const auto rtt = p.a->SmoothedRtt();
    CHECK(p.a->HasRttSample());
    CHECK(rtt >= 39ms && rtt <= 43ms);
    CHECK(p.a->Stats().resent == 0);
    CHECK(p.a->ResendTimeout() >= 59ms && p.a->ResendTimeout() <= 65ms);
    // A fast link: the timeout never goes under 50 ms.
    Pair q(2);
    for (int i = 0; i < 200; ++i) {
      q.a->Send(Lane::kState, Index(i), q.net.Now());
      q.Step();
    }
    CHECK(q.a->SmoothedRtt() <= 2ms);
    CHECK(q.a->ResendTimeout() == std::chrono::duration_cast<std::chrono::microseconds>(kMinResend));

    // The path gets slower (20 ms to 150 ms each way): the estimate follows, although every
    // datagram is first resent before its ack can come back.
    LinkConditions slow;
    slow.delay_ms = 150;
    p.net.SetDefault(slow);
    for (int i = 0; i < 20000; ++i) {
      if (i % 10 == 0) p.a->Send(Lane::kGameplay, Index(i), p.net.Now());
      p.Step();
    }
    std::printf("slower path: rtt %lld us\n", (long long)p.a->SmoothedRtt().count());
    CHECK(p.a->SmoothedRtt() >= 250ms && p.a->SmoothedRtt() <= 330ms);
  }

  // The window: 1024 unacked on a lane, then kWindowFull (nothing queued), counted; the other
  // lanes are not affected; once acks come back, sending works again.
  {
    Pair p(3);
    LinkConditions down;
    down.loss_percent = 100;
    p.net.SetLink(kA, kB, down);
    for (uint32_t i = 0; i < kLaneWindow; ++i) {
      CHECK(p.a->Send(Lane::kGameplay, Index(i), p.net.Now()) == SendResult::kSent);
    }
    CHECK(p.a->Unacked(Lane::kGameplay) == kLaneWindow);
    CHECK(p.a->Send(Lane::kGameplay, Index(9999), p.net.Now()) == SendResult::kWindowFull);
    CHECK(p.a->Stats().refused == 1);
    CHECK(p.a->Unacked(Lane::kGameplay) == kLaneWindow);
    CHECK(p.a->Send(Lane::kState, Index(0), p.net.Now()) == SendResult::kSent);
    CHECK(p.a->Send(Lane::kFast, Index(0), p.net.Now()) == SendResult::kSent);
    std::vector<uint8_t> big(kMaxPayload + 1, 1);
    CHECK(p.a->Send(Lane::kGameplay, big, p.net.Now()) == SendResult::kTooBig);
    CHECK(p.a->Stats().refused == 2);

    p.net.ClearLink(kA, kB);
    for (int i = 0; i < 3000 && p.a->UnackedTotal() > 0; ++i) p.Step();
    CHECK(p.a->UnackedTotal() == 0);
    CHECK(p.a->Send(Lane::kGameplay, Index(kLaneWindow), p.net.Now()) == SendResult::kSent);
    uint32_t delivered = 0;
    bool in_order = true;
    for (int i = 0; i < 100; ++i) p.Step();
    LaneMessage m;
    while (p.b->Receive(m)) {
      if (m.lane != Lane::kGameplay) continue;
      in_order &= IndexOf(m.payload) == delivered;
      ++delivered;
    }
    CHECK(delivered == kLaneWindow + 1);
    CHECK(in_order);
  }

  // A resend far below the newest (here 200 below) is still acked, so it is not sent forever.
  {
    Pair p(4);
    LinkConditions down;
    down.loss_percent = 100;
    p.net.SetLink(kA, kB, down);
    p.a->Send(Lane::kGameplay, Index(0), p.net.Now());  // lost
    p.net.ClearLink(kA, kB);
    for (uint32_t i = 1; i <= 200; ++i) p.a->Send(Lane::kGameplay, Index(i), p.net.Now());
    for (int i = 0; i < 2000 && p.a->UnackedTotal() > 0; ++i) p.Step();
    CHECK(p.a->UnackedTotal() == 0);
    const uint64_t resent = p.a->Stats().resent;
    for (int i = 0; i < 2000; ++i) p.Step();
    CHECK(p.a->Stats().resent == resent);  // nothing more to resend
    uint32_t delivered = 0;
    LaneMessage m;
    while (p.b->Receive(m)) delivered += IndexOf(m.payload) == delivered;
    CHECK(delivered == 201);
  }

  return Finish("netplay_lanes");
}
