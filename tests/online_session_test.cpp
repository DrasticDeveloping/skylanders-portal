#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "netplay/fake_network.h"
#include "online/join_code.h"
#include "online/session.h"
#include "test_util.h"

using namespace skylanders::online;
using namespace std::chrono_literals;
using skylanders::netplay::Endpoint;
using skylanders::netplay::FakeNetwork;
using skylanders::netplay::LinkConditions;

namespace {

constexpr Endpoint kServerEp{0x0A000001, 7000};  // 10.0.0.1:7000 on the fake network

BuildDigest Digest(uint8_t seed) {
  BuildDigest d{};
  for (size_t i = 0; i < d.size(); ++i) d[i] = uint8_t(seed + i);
  return d;
}

OwnerToken Token(uint8_t seed) {
  OwnerToken t{};
  for (size_t i = 0; i < t.size(); ++i) t[i] = uint8_t(seed * 3 + i + 1);
  return t;
}

ServerSettings Settings() {
  ServerSettings s;
  s.hz = 60;
  s.build_digest = Digest(1);
  s.owner_token = Token(7);
  s.session_id = 0x5F0A1B2C;
  return s;
}

Hello MakeHello(std::string name, bool owner = false, uint8_t digest = 1) {
  Hello h;
  h.build_digest = Digest(digest);
  h.figure_id = 3;
  h.figure_variant = 0x1801;
  h.name = std::move(name);
  if (owner) h.owner_token = Token(7);
  return h;
}

struct Client {
  std::unique_ptr<ClientSession> session = std::make_unique<ClientSession>();
  std::vector<ClientEvent> events;
  bool paused = false;

  size_t Count(ClientEvent::Type t) const {
    return size_t(std::count_if(events.begin(), events.end(),
                                [&](const ClientEvent& e) { return e.type == t; }));
  }
  const ClientEvent* Last(ClientEvent::Type t) const {
    for (auto it = events.rbegin(); it != events.rend(); ++it) {
      if (it->type == t) return &*it;
    }
    return nullptr;
  }
};

// A server and its clients on one fake network, pumped in 5 ms steps of fake time. The network
// is declared first, so it outlives the sockets the sessions hold.
struct World {
  explicit World(uint64_t seed) : net(seed) {}

  FakeNetwork net;
  ServerSession server;
  std::vector<ServerEvent> server_events;
  bool server_paused = false;
  std::vector<std::unique_ptr<Client>> clients;
  uint32_t next_address = 0x0A000010;

  bool StartServer(const ServerSettings& s) { return server.Start(net.Bind(kServerEp), s, net.Now()); }

  Client& Connect(const Hello& h) {
    auto c = std::make_unique<Client>();
    CHECK(c->session->Connect(net.Bind(next_address++, 0), kServerEp, h, net.Now()));
    clients.push_back(std::move(c));
    return *clients.back();
  }

  void Step(std::chrono::milliseconds d = 5ms) {
    if (!server_paused) {
      server.Pump(net.Now());
      ServerEvent e;
      while (server.PollEvent(e)) server_events.push_back(e);
    }
    for (auto& c : clients) {
      if (c->paused) continue;
      c->session->Pump(net.Now());
      ClientEvent e;
      while (c->session->PollEvent(e)) c->events.push_back(e);
    }
    net.Advance(d);
  }

  template <class F>
  bool RunUntil(F done, std::chrono::milliseconds limit = 5000ms) {
    const auto end = net.Now() + limit;
    while (net.Now() < end) {
      Step();
      if (done()) return true;
    }
    return done();
  }
  void Run(std::chrono::milliseconds d) {
    RunUntil([] { return false; }, d);
  }

  size_t Count(ServerEvent::Type t) const {
    return size_t(std::count_if(server_events.begin(), server_events.end(),
                                [&](const ServerEvent& e) { return e.type == t; }));
  }
  const ServerEvent* Last(ServerEvent::Type t) const {
    for (auto it = server_events.rbegin(); it != server_events.rend(); ++it) {
      if (it->type == t) return &*it;
    }
    return nullptr;
  }
};

using SE = ServerEvent::Type;
using CE = ClientEvent::Type;

void FakeNetworkSession() {
  World w(2026);
  LinkConditions c;
  c.loss_percent = 10;
  c.duplicate_percent = 5;
  c.reorder_percent = 10;
  c.reorder_ms = 15;
  c.delay_ms = 10;
  c.jitter_ms = 5;
  w.net.SetDefault(c);
  if (!w.StartServer(Settings())) {
    CHECK(false);
    return;
  }
  CHECK(w.server.Running());
  CHECK(w.server.Port() == 7000);
  CHECK(DecodeJoinCode(w.server.JoinCode()) == kServerEp);

  // Handshake: the owner gets 1, the others 2 and 3, in the order they join.
  Client& owner = w.Connect(MakeHello("Owner", true));
  CHECK(w.RunUntil([&] { return owner.session->Connected(); }));
  Client& b = w.Connect(MakeHello("Bee"));
  CHECK(w.RunUntil([&] { return b.session->Connected(); }));
  Client& cc = w.Connect(MakeHello("Cee"));
  CHECK(w.RunUntil([&] { return cc.session->Connected(); }));
  CHECK(owner.session->PeerId() == 1 && owner.session->IsOwner());
  CHECK(b.session->PeerId() == 2 && !b.session->IsOwner());
  CHECK(cc.session->PeerId() == 3);
  CHECK(owner.session->SessionId() == Settings().session_id);
  CHECK(w.Count(SE::kPeerJoined) == 3);
  const ServerEvent* joined = w.Last(SE::kPeerJoined);
  CHECK(joined && joined->peer_id == 3 && joined->name == "Cee" && !joined->owner &&
        joined->figure_variant == 0x1801);
  CHECK(w.server_events[0].owner && w.server_events[0].peer_id == 1);
  const ClientEvent* connected = owner.Last(CE::kConnected);
  CHECK(connected && connected->peer_id == 1 && connected->generation == 0 && connected->zone.empty());
  CHECK(w.server.PeerCount() == 3);
  // Everyone ends up with the same list of three.
  CHECK(w.RunUntil([&] {
    for (Client* x : {&owner, &b, &cc}) {
      if (x->session->Players().size() != 3) return false;
    }
    return true;
  }));
  CHECK(cc.session->Players()[0].name == "Owner" && cc.session->Players()[2].peer_id == 3);

  // Refusals: another build, another protocol version, a wrong owner token.
  Client& d = w.Connect(MakeHello("Dee", false, 9));
  CHECK(w.RunUntil([&] { return d.session->State() == ClientState::kRefused; }));
  CHECK(d.Last(CE::kRefused) && d.Last(CE::kRefused)->refuse_reason == RefuseReason::kDigest);
  CHECK(w.Last(SE::kRefusalSent) && w.Last(SE::kRefusalSent)->refuse_reason == RefuseReason::kDigest &&
        w.Last(SE::kRefusalSent)->name == "Dee");
  Hello old = MakeHello("Old");
  old.protocol_version = kProtocolVersion + 1;
  Client& v = w.Connect(old);
  CHECK(w.RunUntil([&] { return v.session->State() == ClientState::kRefused; }));
  CHECK(v.Last(CE::kRefused) && v.Last(CE::kRefused)->refuse_reason == RefuseReason::kVersion);
  Hello thief = MakeHello("Thief");
  thief.owner_token = Token(8);
  Client& t = w.Connect(thief);
  CHECK(w.RunUntil([&] { return t.session->State() == ClientState::kRefused; }));
  CHECK(t.Last(CE::kRefused) && t.Last(CE::kRefused)->refuse_reason == RefuseReason::kNotOwner);
  CHECK(w.server.PeerCount() == 3);
  CHECK(w.Count(SE::kRefusalSent) == 3);

  // A fourth client gets 4; a fifth is refused as full; the fourth leaves again.
  Client& e = w.Connect(MakeHello("Eee"));
  CHECK(w.RunUntil([&] { return e.session->Connected(); }));
  CHECK(e.session->PeerId() == 4);
  Client& f = w.Connect(MakeHello("Eff"));
  CHECK(w.RunUntil([&] { return f.session->State() == ClientState::kRefused; }));
  CHECK(f.Last(CE::kRefused) && f.Last(CE::kRefused)->refuse_reason == RefuseReason::kFull);
  e.session->Disconnect(w.net.Now());
  CHECK(w.RunUntil([&] { return e.session->State() == ClientState::kDisconnected; }));
  CHECK(e.Last(CE::kDisconnected) &&
        e.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kLocal);
  CHECK(w.RunUntil([&] { return w.server.PeerCount() == 3; }));
  CHECK(w.Last(SE::kPeerLeft) && w.Last(SE::kPeerLeft)->peer_id == 4 &&
        w.Last(SE::kPeerLeft)->leave_reason == LeaveReason::kBye);

  // The owner's level request opens barrier 1; it releases only after the server and all three
  // clients report loaded.
  CHECK(!b.session->SendLevelRequest("levels/nope", w.net.Now()));  // owner only
  CHECK(owner.session->SendLevelRequest("levels/zone_a", w.net.Now()));
  CHECK(w.RunUntil([&] { return w.Count(SE::kLevelRequest) == 1; }));
  const ServerEvent* request = w.Last(SE::kLevelRequest);
  CHECK(request && request->zone == "levels/zone_a" && request->generation == 1 && request->peer_id == 1);
  CHECK(w.server.BarrierOpen() && w.server.Generation() == 1);
  CHECK(w.RunUntil([&] {
    return owner.Count(CE::kLevelLoad) == 1 && b.Count(CE::kLevelLoad) == 1 &&
           cc.Count(CE::kLevelLoad) == 1;
  }));
  CHECK(b.Last(CE::kLevelLoad)->zone == "levels/zone_a" && b.Last(CE::kLevelLoad)->generation == 1);
  w.server.MarkServerLoaded(1, w.net.Now());
  CHECK(owner.session->SendLevelLoaded(1, w.net.Now()));
  CHECK(b.session->SendLevelLoaded(1, w.net.Now()));
  w.Run(500ms);
  CHECK(w.server.BarrierOpen());
  CHECK(w.server.BarrierWaitingFor() == std::vector<uint16_t>{3});
  CHECK(w.Count(SE::kBarrierReleased) == 0);
  CHECK(cc.session->SendLevelLoaded(1, w.net.Now()));
  CHECK(w.RunUntil([&] { return w.Count(SE::kBarrierReleased) == 1; }));
  CHECK(!w.server.BarrierOpen());
  const ServerEvent* released = w.Last(SE::kBarrierReleased);
  CHECK(released && released->generation == 1 && released->tick > 0);
  CHECK(w.RunUntil([&] {
    return owner.Count(CE::kBarrierGo) == 1 && b.Count(CE::kBarrierGo) == 1 &&
           cc.Count(CE::kBarrierGo) == 1;
  }));
  CHECK(cc.Last(CE::kBarrierGo)->generation == 1 && cc.Last(CE::kBarrierGo)->tick == released->tick);

  // Barrier 2 (the server's own request): a client that leaves mid-barrier does not block it.
  const uint32_t g2 = w.server.RequestLevel("levels/zone_b", w.net.Now());
  CHECK(g2 == 2);
  CHECK(w.RunUntil([&] { return cc.Count(CE::kLevelLoad) == 2; }));
  w.server.MarkServerLoaded(2, w.net.Now());
  owner.session->SendLevelLoaded(2, w.net.Now());
  b.session->SendLevelLoaded(2, w.net.Now());
  w.Run(500ms);
  CHECK(w.server.BarrierOpen());
  cc.session->Disconnect(w.net.Now());
  CHECK(w.RunUntil([&] { return w.Count(SE::kBarrierReleased) == 2; }));
  CHECK(w.Last(SE::kBarrierReleased)->generation == 2);
  CHECK(w.Last(SE::kPeerLeft)->peer_id == 3);
  CHECK(w.RunUntil([&] { return b.Count(CE::kBarrierGo) == 2; }));

  // A client that joins while barrier 3 is open learns the level from its Welcome and is not
  // waited for.
  const uint32_t g3 = w.server.RequestLevel("levels/zone_c", w.net.Now());
  Client& g = w.Connect(MakeHello("Gee"));
  CHECK(w.RunUntil([&] { return g.session->Connected(); }));
  CHECK(g.session->PeerId() == 3);  // the lowest free one
  CHECK(g.Last(CE::kConnected)->generation == g3 && g.Last(CE::kConnected)->zone == "levels/zone_c");
  CHECK(g.session->Zone() == "levels/zone_c");
  CHECK(w.server.BarrierWaitingFor() == (std::vector<uint16_t>{1, 2}));
  w.server.MarkServerLoaded(g3, w.net.Now());
  owner.session->SendLevelLoaded(g3, w.net.Now());
  b.session->SendLevelLoaded(g3, w.net.Now());
  CHECK(w.RunUntil([&] { return w.Count(SE::kBarrierReleased) == 3; }));
  CHECK(w.RunUntil([&] { return g.Count(CE::kBarrierGo) == 1; }));
  CHECK(g.Count(CE::kLevelLoad) == 0);
  // A stale LevelLoaded changes nothing.
  CHECK(g.session->SendLevelLoaded(1, w.net.Now()));
  w.Run(200ms);
  CHECK(w.Count(SE::kBarrierReleased) == 3);

  // The tick: after a few pings the owner's estimate is within a couple of ticks of the server.
  w.Run(5000ms);
  const auto& est = owner.session->TickEstimator();
  CHECK(est.HasEstimate() && est.Accepted() >= 3);
  const double off = est.EstimateTicks(w.net.Now()) - double(w.server.Tick(w.net.Now()));
  std::printf("fake network: owner's tick estimate off by %.2f ticks (error estimate %.2f), rtt %lld us\n",
              off, est.ErrorTicks(), (long long)owner.session->Rtt().count());
  CHECK(off > -2.0 && off < 2.0);

  // Timeout: B goes silent (its game hangs) and is dropped 10 s after the last datagram the server
  // had from it (B pings once a second, so that is up to a second or two before it went silent),
  // not before.
  b.paused = true;
  w.Run(100ms);  // what B sent last has landed
  auto b_info = [&] {
    for (const PeerInfo& p : w.server.Peers()) {
      if (p.peer_id == 2) return p;
    }
    return PeerInfo{};
  };
  const PeerInfo silent = b_info();
  CHECK(silent.peer_id == 2 && silent.name == "Bee");
  const auto last_heard = silent.last_receive;
  CHECK(w.net.Now() - last_heard < 2500ms);
  CHECK(w.RunUntil([&] { return w.net.Now() >= last_heard + 9900ms; }, 11000ms));
  CHECK(w.server.PeerCount() == 3);
  CHECK(w.RunUntil([&] { return w.server.PeerCount() == 2; }, 1000ms));
  const auto took = w.net.Now() - last_heard;
  CHECK(took > 10s && took <= 10010ms);
  CHECK(w.Last(SE::kPeerLeft)->peer_id == 2 &&
        w.Last(SE::kPeerLeft)->leave_reason == LeaveReason::kTimeout);
  // B comes back: the server no longer knows it, so B times out too.
  b.paused = false;
  CHECK(w.RunUntil([&] { return b.session->State() == ClientState::kDisconnected; }, 12000ms));
  CHECK(b.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kTimeout);

  // The owner connects again from another address: the new one is the owner, the old one is told
  // Bye.
  Client& owner2 = w.Connect(MakeHello("Owner", true));
  CHECK(w.RunUntil([&] { return owner2.session->Connected(); }));
  CHECK(owner2.session->PeerId() == 1);
  CHECK(w.RunUntil([&] { return owner.session->State() == ClientState::kDisconnected; }));
  CHECK(owner.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kBye);
  bool replaced = false;
  for (const ServerEvent& x : w.server_events) {
    replaced |= x.type == SE::kPeerLeft && x.peer_id == 1 && x.leave_reason == LeaveReason::kReplaced;
  }
  CHECK(replaced);

  // The server hangs: its clients time out.
  w.server_paused = true;
  CHECK(w.RunUntil([&] {
    return owner2.session->State() == ClientState::kDisconnected &&
           g.session->State() == ClientState::kDisconnected;
  }, 11000ms));
  CHECK(g.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kTimeout);
}

void FakeNetworkStopAndQuiet() {
  World w(5);
  CHECK(w.StartServer(Settings()));
  Client& a = w.Connect(MakeHello("A", true));
  CHECK(w.RunUntil([&] { return a.session->Connected(); }));
  // Nothing happens on a quiet session for a minute: pings keep it alive.
  w.Run(60000ms);
  CHECK(a.session->Connected() && w.server.PeerCount() == 1);
  // A connect to nobody times out after 10 s.
  Client& lost = *w.clients.emplace_back(std::make_unique<Client>());
  CHECK(lost.session->Connect(w.net.Bind(0x0A0000F0, 0), Endpoint{0x0A0000FE, 1}, MakeHello("L"),
                              w.net.Now()));
  CHECK(w.RunUntil([&] { return lost.session->State() == ClientState::kDisconnected; }, 11000ms));
  CHECK(lost.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kTimeout);
  // Stop says Bye.
  w.server.Stop();
  CHECK(!w.server.Running());
  CHECK(w.RunUntil([&] { return a.session->State() == ClientState::kDisconnected; }, 1000ms));
  CHECK(a.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kBye);
}

// Real UDP sockets on 127.0.0.1 (port 0: any free one), real time.
void LoopbackSession() {
  ServerSession server;
  if (!server.Start("127.0.0.1", 0, Settings())) {
    std::fprintf(stderr, "loopback server: %s\n", server.Error().c_str());
    CHECK(false);
    return;
  }
  const uint16_t port = server.Port();
  CHECK(port != 0);
  CHECK(server.LocalEndpoint().address == skylanders::netplay::kLoopback);
  const auto code = DecodeJoinCode(server.JoinCode());
  CHECK(code && code->address == skylanders::netplay::kLoopback && code->port == port);

  ClientSession owner, other;
  CHECK(owner.Connect("127.0.0.1", port, MakeHello("Owner", true)));
  CHECK(other.Connect("127.0.0.1", port, MakeHello("Other")));
  CHECK(owner.LocalEndpoint().address == skylanders::netplay::kLoopback);

  std::vector<ServerEvent> server_events;
  std::vector<ClientEvent> owner_events, other_events;
  auto pump = [&] {
    server.Pump();
    owner.Pump();
    other.Pump();
    ServerEvent se;
    while (server.PollEvent(se)) server_events.push_back(se);
    ClientEvent ce;
    while (owner.PollEvent(ce)) owner_events.push_back(ce);
    while (other.PollEvent(ce)) other_events.push_back(ce);
  };
  auto run_until = [&](auto done) {
    const auto end = std::chrono::steady_clock::now() + 5s;
    while (std::chrono::steady_clock::now() < end) {
      pump();
      if (done()) return true;
      std::this_thread::sleep_for(1ms);
    }
    return false;
  };
  auto has = [](const std::vector<ClientEvent>& v, CE t) {
    return std::any_of(v.begin(), v.end(), [&](const ClientEvent& e) { return e.type == t; });
  };

  CHECK(run_until([&] { return owner.Connected() && other.Connected(); }));
  CHECK(owner.PeerId() == 1 && other.PeerId() == 2);
  CHECK(owner.SendLevelRequest("levels/loopback"));
  CHECK(run_until([&] { return has(owner_events, CE::kLevelLoad) && has(other_events, CE::kLevelLoad); }));
  CHECK(server.BarrierOpen() && server.Zone() == "levels/loopback");
  server.MarkServerLoaded(1);
  owner.SendLevelLoaded(1);
  other.SendLevelLoaded(1);
  CHECK(run_until([&] { return has(owner_events, CE::kBarrierGo) && has(other_events, CE::kBarrierGo); }));
  CHECK(!server.BarrierOpen());
  // The first ping went right after the Welcome.
  CHECK(run_until([&] { return owner.TickEstimator().HasEstimate(); }));
  const auto now = std::chrono::steady_clock::now();
  const double off = owner.TickEstimator().EstimateTicks(now) - double(server.Tick(now));
  std::printf("loopback: port %u, tick estimate off by %.2f ticks, rtt %lld us\n", port, off,
              (long long)owner.Rtt().count());
  CHECK(off > -2.0 && off < 2.0);

  other.Disconnect();
  CHECK(run_until([&] { return other.State() == ClientState::kDisconnected && server.PeerCount() == 1; }));
  server.Stop();
  CHECK(run_until([&] { return owner.State() == ClientState::kDisconnected; }));
}

}  // namespace

int main() {
  FakeNetworkSession();
  FakeNetworkStopAndQuiet();
  LoopbackSession();
  return Finish("online_session");
}
