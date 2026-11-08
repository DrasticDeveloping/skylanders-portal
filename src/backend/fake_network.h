#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <utility>
#include <vector>

#include "backend/adapter.h"
#include "backend/base.h"
#include "backend/client.h"
#include "backend/fault_injector.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/limits.h"
#include "backend/request.h"
#include "backend/server.h"
#include "netplay/datagram.h"
#include "netplay/fake_network.h"
#include "netplay/lanes.h"

// The session harness (design section 3.12): PeerLink twice, in memory and over netplay, and
// FakeSession, which builds S and N clients on one clock. For tests only; header-only, as netplay's
// fake network. Game-free: the test passes the kind table, the authorities and each client's
// adapters (its fake world).
//
// - MemoryLink passes frames exactly, in order, after a fixed one-way delay (for message-level
//   faults). Its UnackedFrames counts the frames sent and not yet taken by the other side's Receive,
//   plus those taken less than a fixed ack delay ago (design 3.12). A reliable lane answers kFull
//   at netplay's kLaneWindow, as Connection's kWindowFull.
// - NetplayLink runs netplay's Connection over netplay's FakeNetwork, with its loss, delay,
//   jitter, duplicates and reordering. The fake network has no bandwidth model: a throttle is the
//   injector's link decorator.
// - FakeSession: S, its peers' links (each end wrapped by the fault injector when it has rules,
//   then by a tap the test can read), the clients, and one clock that moves only in Step. Helpers:
//   Run(d), RunUntil(done, max), Join, Leave, LevelReady, NewGeneration.

namespace skylanders::backend {

struct MemoryLinkOptions {
  Duration one_way{};  // each frame arrives this long after its Send
  Duration ack_delay = std::chrono::milliseconds(20);  // a taken frame stays unacked this long
};

class MemoryLink final : public PeerLink {
 public:
  // The two ends of one link. `now` is read at every call and must outlive both ends.
  static std::pair<std::unique_ptr<MemoryLink>, std::unique_ptr<MemoryLink>> Pair(
      const TimePoint* now, MemoryLinkOptions options = {}) {
    auto shared = std::make_shared<Shared>();
    shared->now = now;
    shared->options = options;
    return {std::unique_ptr<MemoryLink>(new MemoryLink(shared, 0)),
            std::unique_ptr<MemoryLink>(new MemoryLink(shared, 1))};
  }

  LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) override {
    Shared& s = *shared_;
    if (!s.connected) return LinkSend::kGone;
    if (frame.size() > kMaxFrameBytes) return LinkSend::kTooBig;
    if (lane != netplay::Lane::kFast && UnackedFrames(lane) >= netplay::kLaneWindow) {
      return LinkSend::kFull;
    }
    Direction& d = s.dirs[side_];
    d.frames.push_back(InFlight{lane, std::vector<uint8_t>(frame.begin(), frame.end()),
                                *s.now + s.options.one_way});
    ++d.sent[LaneIndex(lane)];
    d.bytes[LaneIndex(lane)] += frame.size();
    return LinkSend::kSent;
  }

  bool Receive(netplay::Lane& lane, std::vector<uint8_t>& frame) override {
    Shared& s = *shared_;
    if (!s.connected) return false;
    Direction& d = s.dirs[1 - side_];
    if (d.frames.empty() || d.frames.front().due > *s.now) return false;
    InFlight f = std::move(d.frames.front());
    d.frames.pop_front();
    lane = f.lane;
    frame = std::move(f.bytes);
    if (lane != netplay::Lane::kFast) {
      d.acks[LaneIndex(lane)].push_back(*s.now + s.options.ack_delay);
    }
    return true;
  }

  size_t UnackedFrames(netplay::Lane lane) const override {
    if (lane == netplay::Lane::kFast) return 0;  // unreliable: nothing waits for an ack
    Shared& s = *shared_;  // the shared state, not this end's: pruning past acks is no change
    Direction& d = s.dirs[side_];
    auto& acks = d.acks[LaneIndex(lane)];  // in time order
    while (!acks.empty() && acks.front() <= *s.now) acks.pop_front();
    return acks.size() + size_t(std::count_if(d.frames.begin(), d.frames.end(),
                                              [&](const InFlight& f) { return f.lane == lane; }));
  }

  std::chrono::microseconds Rtt() const override {
    return std::chrono::duration_cast<std::chrono::microseconds>(2 * shared_->options.one_way +
                                                                 shared_->options.ack_delay);
  }
  bool Connected() const override { return shared_->connected; }

  // Both ends: Connected false, Send kGone, nothing more delivered.
  void Disconnect() { shared_->connected = false; }
  // Frames and bytes this end sent on `lane`.
  uint64_t FramesSent(netplay::Lane lane) const { return shared_->dirs[side_].sent[LaneIndex(lane)]; }
  uint64_t BytesSent(netplay::Lane lane) const { return shared_->dirs[side_].bytes[LaneIndex(lane)]; }
  // Frames on their way to the other end.
  size_t FramesInFlight() const { return shared_->dirs[side_].frames.size(); }

 private:
  struct InFlight {
    netplay::Lane lane = netplay::Lane::kGameplay;
    std::vector<uint8_t> bytes;
    TimePoint due{};
  };
  struct Direction {
    std::deque<InFlight> frames;
    std::array<std::deque<TimePoint>, netplay::kLaneCount> acks;  // when each taken frame is acked
    std::array<uint64_t, netplay::kLaneCount> sent{};
    std::array<uint64_t, netplay::kLaneCount> bytes{};
  };
  struct Shared {
    const TimePoint* now = nullptr;
    MemoryLinkOptions options;
    std::array<Direction, 2> dirs;  // dirs[i]: frames sent by end i
    bool connected = true;
  };
  static size_t LaneIndex(netplay::Lane lane) { return size_t(lane) % netplay::kLaneCount; }

  MemoryLink(std::shared_ptr<Shared> shared, int side) : shared_(std::move(shared)), side_(side) {}

  std::shared_ptr<Shared> shared_;
  int side_;
};

class NetplayLink final : public PeerLink {
 public:
  NetplayLink(netplay::FakeNetwork& net, netplay::Endpoint local, netplay::Endpoint remote)
      : net_(net), socket_(net.Bind(local)), remote_(remote) {
    connection_ = std::make_unique<netplay::Connection>(
        [this](std::span<const uint8_t> datagram) {
          if (socket_) socket_->Send(remote_, datagram);
        },
        net_.Now());
  }

  // Feeds the datagrams that arrived and pumps the connection (resends, acks). The harness calls
  // it every step, before and after the pumps.
  void Service() {
    if (!connected_ || !socket_) return;
    Feed();
    connection_->Pump(net_.Now());
  }

  LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) override {
    if (!connected_ || !socket_) return LinkSend::kGone;
    if (frame.size() > kMaxFrameBytes) return LinkSend::kTooBig;
    switch (connection_->Send(lane, frame, net_.Now())) {
      case netplay::SendResult::kSent: return LinkSend::kSent;
      case netplay::SendResult::kWindowFull: return LinkSend::kFull;
      case netplay::SendResult::kTooBig: return LinkSend::kTooBig;
    }
    return LinkSend::kTooBig;
  }

  bool Receive(netplay::Lane& lane, std::vector<uint8_t>& frame) override {
    if (!connected_ || !socket_) return false;
    Feed();
    netplay::LaneMessage m;
    if (!connection_->Receive(m)) return false;
    lane = m.lane;
    frame = std::move(m.payload);
    return true;
  }

  size_t UnackedFrames(netplay::Lane lane) const override { return connection_->Unacked(lane); }
  std::chrono::microseconds Rtt() const override { return connection_->SmoothedRtt(); }
  bool Connected() const override { return connected_; }

  void Disconnect() {
    connected_ = false;
    socket_.reset();
  }
  const netplay::Connection& Connection() const { return *connection_; }

 private:
  void Feed() {
    std::vector<uint8_t> buf;
    netplay::Endpoint from;
    while (socket_->Receive(buf, from)) {
      if (from != remote_) continue;
      if (auto v = netplay::DecodeDatagram(buf)) connection_->OnDatagram(*v, net_.Now());
    }
  }

  netplay::FakeNetwork& net_;
  std::unique_ptr<netplay::FakeNetwork::Socket> socket_;
  netplay::Endpoint remote_;
  std::unique_ptr<netplay::Connection> connection_;
  bool connected_ = true;
};

// The outermost decorator FakeSession puts on each end: calls the session's frame tap for every
// frame the backend takes (after the injector's message layer), so a test can count messages.
class TapLink final : public PeerLink {
 public:
  using Tap = std::function<void(netplay::Lane, std::span<const uint8_t>)>;
  TapLink(PeerLink& inner, Tap on_receive, Tap on_send)
      : inner_(inner), on_receive_(std::move(on_receive)), on_send_(std::move(on_send)) {}

  LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) override {
    const LinkSend r = inner_.Send(lane, frame);
    if (r == LinkSend::kSent && on_send_) on_send_(lane, frame);
    return r;
  }
  bool Receive(netplay::Lane& lane, std::vector<uint8_t>& frame) override {
    if (!inner_.Receive(lane, frame)) return false;
    if (on_receive_) on_receive_(lane, frame);
    return true;
  }
  size_t UnackedFrames(netplay::Lane lane) const override { return inner_.UnackedFrames(lane); }
  std::chrono::microseconds Rtt() const override { return inner_.Rtt(); }
  bool Connected() const override { return inner_.Connected(); }

 private:
  PeerLink& inner_;
  Tap on_receive_, on_send_;
};

struct FakeSessionOptions {
  Limits limits;
  bool netplay = false;  // NetplayLink over FakeNetwork; MemoryLink otherwise
  MemoryLinkOptions memory;
  netplay::LinkConditions conditions;  // netplay: both directions of every peer's link
  uint64_t seed = 1;                   // the fake network's and the injector's
  Duration step = std::chrono::milliseconds(10);
  TimePoint start = TimePoint{} + std::chrono::hours(1);
  std::string faults;  // rule text (ParseFaultRules); empty: no injector
  bool leave_on_disconnect = true;  // a link the injector disconnected: the peer leaves
};

class FakeSession {
 public:
  // One frame as the backend took it: `to_server` true for a frame S read from `peer`.
  using FrameTap = std::function<void(uint16_t peer, bool to_server, netplay::Lane lane,
                                      std::span<const uint8_t> frame)>;

  FakeSession(std::span<const KindSpec> table, AuthoritySet& authorities,
              FakeSessionOptions options = {})
      : table_(table), options_(std::move(options)), now_(options_.start) {
    if (options_.netplay) {
      net_ = std::make_unique<netplay::FakeNetwork>(options_.seed, options_.start);
      net_->SetDefault(options_.conditions);
    }
    if (!options_.faults.empty()) {
      FaultParseError error;
      auto rules = ParseFaultRules(options_.faults, &error);
      parse_error_ = rules ? std::string() : error.text;
      faults_ = std::make_unique<FaultInjector>(rules ? std::move(*rules) : std::vector<FaultRule>{},
                                                options_.seed, table_, now_);
    }
    server_ = std::make_unique<Server>(table_, authorities, options_.limits);
    server_->BeginEpoch(generation_, now_);
  }
  ~FakeSession() {
    // The clients first (they hold their ends), then S's view of each peer, then the links.
    for (auto& [peer, p] : peers_) p->client.reset();
    server_.reset();
    peers_.clear();
  }
  FakeSession(const FakeSession&) = delete;
  FakeSession& operator=(const FakeSession&) = delete;

  Server& S() { return *server_; }
  Client& C(uint16_t peer) { return *peers_.at(peer)->client; }
  bool Has(uint16_t peer) const { return peers_.contains(peer); }
  std::vector<uint16_t> Peers() const {
    std::vector<uint16_t> out;
    for (const auto& [peer, p] : peers_) out.push_back(peer);
    return out;
  }
  // The rule text's parse error; empty when it parsed (or there were none).
  const std::string& FaultParseErrorText() const { return parse_error_; }
  FaultInjector* Faults() { return faults_.get(); }
  TimePoint Now() const { return now_; }
  uint32_t Generation() const { return generation_; }
  const Limits& Lim() const { return options_.limits; }

  // AddPeer on S (session baseline, StateHello), then a client attached to the other end.
  // `adapters` must outlive the client (until Leave or the session's end).
  Client& Join(uint16_t peer, AdapterSet& adapters, PeerRole role = PeerRole::kGuest) {
    if (Has(peer)) Leave(peer);
    auto p = std::make_unique<Peer>();
    if (options_.netplay) {
      const netplay::Endpoint s_end{0x0A000001, uint16_t(5000 + peer)};
      const netplay::Endpoint c_end{0x0A000100u + peer, 4000};
      p->net_server = std::make_unique<NetplayLink>(*net_, s_end, c_end);
      p->net_client = std::make_unique<NetplayLink>(*net_, c_end, s_end);
      p->raw_server = p->net_server.get();
      p->raw_client = p->net_client.get();
    } else {
      auto [a, b] = MemoryLink::Pair(&now_, options_.memory);
      p->mem_server = std::move(a);
      p->mem_client = std::move(b);
      p->raw_server = p->mem_server.get();
      p->raw_client = p->mem_client.get();
    }
    PeerLink* server_inner = p->raw_server;
    PeerLink* client_inner = p->raw_client;
    if (faults_) {
      p->fault_server = faults_->Wrap(*p->raw_server, peer, /*server_side=*/true);
      p->fault_client = faults_->Wrap(*p->raw_client, peer, /*server_side=*/false);
      server_inner = p->fault_server.get();
      client_inner = p->fault_client.get();
    }
    p->tap_server = std::make_unique<TapLink>(
        *server_inner,
        [this, peer](netplay::Lane lane, std::span<const uint8_t> f) {
          if (on_frame) on_frame(peer, true, lane, f);
        },
        [this, peer](netplay::Lane lane, std::span<const uint8_t> f) {
          if (on_send) on_send(peer, false, lane, f);
        });
    p->tap_client = std::make_unique<TapLink>(
        *client_inner,
        [this, peer](netplay::Lane lane, std::span<const uint8_t> f) {
          if (on_frame) on_frame(peer, false, lane, f);
        },
        [this, peer](netplay::Lane lane, std::span<const uint8_t> f) {
          if (on_send) on_send(peer, true, lane, f);
        });
    p->client = std::make_unique<Client>(table_, adapters, options_.limits);
    Peer& ref = *p;
    peers_[peer] = std::move(p);
    server_->AddPeer(peer, role, *ref.tap_server, now_);
    ref.client->Attach(*ref.tap_client, now_);
    return *ref.client;
  }

  // RemovePeer on S, then the client and both ends go. The events it raised (obligations retired,
  // leases revoked) are collected at once, with the client's last ones.
  void Leave(uint16_t peer) {
    auto it = peers_.find(peer);
    if (it == peers_.end()) return;
    server_->RemovePeer(peer, now_);
    Collect();
    if (it->second->mem_server) it->second->mem_server->Disconnect();
    it->second->client.reset();
    peers_.erase(it);
  }

  void LevelReady(uint16_t peer) { server_->OnPeerLevelReady(peer, generation_, now_); }
  // BeginEpoch on S; its events (finals, obligations retired) are collected at once.
  uint32_t NewGeneration() {
    ++generation_;
    server_->BeginEpoch(generation_, now_);
    Collect();
    return generation_;
  }

  // One pump of everything, then the clock moves one step: the injector's clock, the links'
  // service, before_pump, S's pump, each client's (unless stalled; before_client_pump first), the
  // links again, the events collected, departed peers removed.
  void Step() {
    if (faults_) faults_->Advance(now_);
    ServiceLinks();
    if (before_pump) before_pump(now_);
    server_->Pump(now_);
    for (auto& [peer, p] : peers_) {
      if (faults_ && faults_->Stalled(peer)) continue;
      if (before_client_pump) before_client_pump(peer, now_);
      p->client->Pump(now_);
    }
    ServiceLinks();
    Collect();
    if (options_.leave_on_disconnect) {
      std::vector<uint16_t> gone;
      for (auto& [peer, p] : peers_) {
        if (!p->tap_server->Connected() || !p->tap_client->Connected()) gone.push_back(peer);
      }
      for (uint16_t peer : gone) Leave(peer);
    }
    now_ += options_.step;
    if (net_) net_->SetNow(now_);
  }
  void Run(Duration d) {
    const TimePoint end = now_ + d;
    while (now_ < end) Step();
  }
  // Steps until `done` holds (checked after each step) or `max` passed; whether it held.
  bool RunUntil(const std::function<bool()>& done, Duration max) {
    const TimePoint end = now_ + max;
    while (now_ < end) {
      Step();
      if (done()) return true;
    }
    return done();
  }

  // S's end and the client's end of a peer's link, as the backend sees them (tapped, faulted).
  PeerLink& ServerEnd(uint16_t peer) { return *peers_.at(peer)->tap_server; }
  PeerLink& ClientEnd(uint16_t peer) { return *peers_.at(peer)->tap_client; }
  MemoryLink* MemoryServerEnd(uint16_t peer) { return peers_.at(peer)->mem_server.get(); }
  netplay::FakeNetwork* Network() { return net_.get(); }

  const std::vector<ServerEvent>& ServerEvents() const { return server_events_; }
  const std::vector<ClientEvent>& ClientEvents(uint16_t peer) {
    return client_events_[peer];
  }
  const std::vector<FaultEvent>& FaultEvents() const { return fault_events_; }

  std::function<void(TimePoint)> before_pump;
  std::function<void(uint16_t peer, TimePoint)> before_client_pump;
  std::function<void(const ServerEvent&)> on_server_event;
  std::function<void(uint16_t peer, const ClientEvent&)> on_client_event;
  // Every frame the backend took (after the injector's message layer).
  FrameTap on_frame;
  // Every frame the backend handed its link and the link accepted (before the injector): S's,
  // to_server false; a client's, to_server true. What a client really sent, even when a fault
  // kills it on the way.
  FrameTap on_send;

 private:
  struct Peer {
    std::unique_ptr<MemoryLink> mem_server, mem_client;
    std::unique_ptr<NetplayLink> net_server, net_client;
    PeerLink* raw_server = nullptr;
    PeerLink* raw_client = nullptr;
    std::unique_ptr<PeerLink> fault_server, fault_client;
    std::unique_ptr<TapLink> tap_server, tap_client;
    std::unique_ptr<Client> client;  // last: destroyed first
  };

  void ServiceLinks() {
    if (!net_) return;
    for (auto& [peer, p] : peers_) {
      if (p->net_server) p->net_server->Service();
      if (p->net_client) p->net_client->Service();
    }
  }

  void Collect() {
    ServerEvent se;
    while (server_->PollEvent(se)) {
      if (on_server_event) on_server_event(se);
      server_events_.push_back(std::move(se));
      se = ServerEvent{};
    }
    for (auto& [peer, p] : peers_) {
      ClientEvent ce;
      while (p->client->PollEvent(ce)) {
        if (on_client_event) on_client_event(peer, ce);
        client_events_[peer].push_back(std::move(ce));
        ce = ClientEvent{};
      }
    }
    if (faults_) {
      FaultEvent fe;
      while (faults_->PollEvent(fe)) fault_events_.push_back(std::move(fe));
    }
  }

  std::span<const KindSpec> table_;
  FakeSessionOptions options_;
  TimePoint now_;
  uint32_t generation_ = 1;
  std::string parse_error_;
  std::unique_ptr<netplay::FakeNetwork> net_;
  std::unique_ptr<FaultInjector> faults_;
  std::unique_ptr<Server> server_;
  std::map<uint16_t, std::unique_ptr<Peer>> peers_;
  std::vector<ServerEvent> server_events_;
  std::map<uint16_t, std::vector<ClientEvent>> client_events_;
  std::vector<FaultEvent> fault_events_;
};

}  // namespace skylanders::backend
