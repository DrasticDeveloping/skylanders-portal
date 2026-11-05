#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <span>
#include <utility>
#include <vector>

#include "netplay/transport.h"

namespace skylanders::netplay {

// What a link does to the datagrams crossing it, one direction.
struct LinkConditions {
  double loss_percent = 0;       // dropped
  double duplicate_percent = 0;  // delivered twice (the copy with its own delay)
  double reorder_percent = 0;    // held back by reorder_ms more than the rest
  int delay_ms = 0;              // one-way delay
  int jitter_ms = 0;             // plus 0 to jitter_ms, random per datagram
  int reorder_ms = 30;
};

// An in-memory network for tests. Sockets bind to made-up addresses and exchange datagrams that
// arrive when the network's own clock reaches their due time; the clock moves only when the test
// moves it (Advance, SetNow). Everything random comes from one seed, so a run repeats exactly.
// Sockets must not outlive their network. Not thread-safe.
class FakeNetwork {
 public:
  class Socket final : public Transport {
   public:
    ~Socket() override { net_->Unbind(local_); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;

    bool Send(const Endpoint& to, std::span<const uint8_t> bytes) override {
      net_->Post(local_, to, bytes);
      return true;
    }
    bool Receive(std::vector<uint8_t>& out, Endpoint& from) override {
      return net_->Take(local_, out, from);
    }
    Endpoint LocalEndpoint() const override { return local_; }

   private:
    friend class FakeNetwork;
    Socket(FakeNetwork* net, Endpoint local) : net_(net), local_(local) {}
    FakeNetwork* net_;
    Endpoint local_;
  };

  struct Counters {
    uint64_t sent = 0;
    uint64_t dropped = 0;
    uint64_t duplicated = 0;
    uint64_t delivered = 0;
    uint64_t unreachable = 0;  // sent to an address nobody has bound
  };

  explicit FakeNetwork(uint64_t seed = 1,
                       TimePoint start = TimePoint{} + std::chrono::hours(1))
      : rng_(seed), now_(start) {}

  TimePoint Now() const { return now_; }
  void Advance(Clock::duration d) { now_ += d; }
  void SetNow(TimePoint t) { now_ = t; }

  void SetDefault(const LinkConditions& c) { default_ = c; }
  // Conditions for datagrams from `from` to `to` (one direction); they replace the default.
  void SetLink(const Endpoint& from, const Endpoint& to, const LinkConditions& c) {
    links_[{from, to}] = c;
  }
  void ClearLink(const Endpoint& from, const Endpoint& to) { links_.erase({from, to}); }

  // Binds `at`; port 0 takes the next free port from 50000. nullptr if the endpoint is taken.
  std::unique_ptr<Socket> Bind(Endpoint at) {
    if (at.port == 0) {
      while (queues_.contains(Endpoint{at.address, next_port_})) ++next_port_;
      at.port = next_port_++;
    }
    if (queues_.contains(at)) return nullptr;
    queues_[at];
    return std::unique_ptr<Socket>(new Socket(this, at));
  }
  std::unique_ptr<Socket> Bind(uint32_t address, uint16_t port = 0) {
    return Bind(Endpoint{address, port});
  }

  const Counters& Stats() const { return counters_; }
  // Datagrams on their way to `to`.
  size_t InFlight(const Endpoint& to) const {
    auto it = queues_.find(to);
    return it == queues_.end() ? 0 : it->second.size();
  }

 private:
  struct Packet {
    Endpoint from;
    std::vector<uint8_t> bytes;
  };
  // Due time, then send order, so equal due times keep the order they were sent in.
  using Queue = std::map<std::pair<TimePoint, uint64_t>, Packet>;

  // splitmix64: small, fast and the same on every compiler (unlike std's distributions).
  uint64_t Next() {
    uint64_t z = (rng_ += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
  }
  bool Chance(double percent) { return double(Next() % 1000000) < percent * 10000.0; }
  int Upto(int n) { return n <= 0 ? 0 : int(Next() % uint64_t(n + 1)); }

  const LinkConditions& ConditionsFor(const Endpoint& from, const Endpoint& to) const {
    auto it = links_.find({from, to});
    return it == links_.end() ? default_ : it->second;
  }

  void Post(const Endpoint& from, const Endpoint& to, std::span<const uint8_t> bytes) {
    ++counters_.sent;
    auto q = queues_.find(to);
    if (q == queues_.end()) {
      ++counters_.unreachable;
      return;
    }
    const LinkConditions& c = ConditionsFor(from, to);
    if (Chance(c.loss_percent)) {
      ++counters_.dropped;
      return;
    }
    const int copies = Chance(c.duplicate_percent) ? 2 : 1;
    if (copies == 2) ++counters_.duplicated;
    for (int i = 0; i < copies; ++i) {
      int ms = c.delay_ms + Upto(c.jitter_ms);
      if (Chance(c.reorder_percent)) ms += c.reorder_ms;
      const TimePoint due = now_ + std::chrono::milliseconds(ms);
      q->second.emplace(std::make_pair(due, order_++),
                        Packet{from, std::vector<uint8_t>(bytes.begin(), bytes.end())});
    }
  }

  bool Take(const Endpoint& at, std::vector<uint8_t>& out, Endpoint& from) {
    auto q = queues_.find(at);
    if (q == queues_.end() || q->second.empty()) return false;
    auto first = q->second.begin();
    if (first->first.first > now_) return false;
    from = first->second.from;
    out = std::move(first->second.bytes);
    q->second.erase(first);
    ++counters_.delivered;
    return true;
  }

  void Unbind(const Endpoint& at) { queues_.erase(at); }

  uint64_t rng_;
  TimePoint now_;
  uint64_t order_ = 0;
  uint16_t next_port_ = 50000;
  LinkConditions default_;
  std::map<std::pair<Endpoint, Endpoint>, LinkConditions> links_;
  std::map<Endpoint, Queue> queues_;
  Counters counters_;
};

}  // namespace skylanders::netplay
