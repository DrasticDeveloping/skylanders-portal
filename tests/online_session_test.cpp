#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "netplay/fake_network.h"
#include "online/join_code.h"
#include "online/session.h"
#include "test_util.h"

// REQUIRE_OR_RETURN for a function that returns nothing: a failure is counted, then it returns.
#define REQUIRE_OR_RETURN_VOID(cond)                                       \
  do {                                                                     \
    if (!(cond)) {                                                         \
      std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                        \
      return;                                                              \
    }                                                                      \
  } while (0)

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
  // The server's game thread: it answers each kEvaluateRequest on its next frame (here, right after
  // the Pump that reported it) with `verdict`, and drops one whose generation is no longer current
  // or that comes while End session's drain is on, as the game must.
  bool game = true;
  RefusalReason verdict = RefusalReason::kNone;
  uint32_t applied = 0;

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
      std::vector<ServerEvent> evaluate;
      while (server.PollEvent(e)) {
        if (e.type == ServerEvent::Type::kEvaluateRequest) evaluate.push_back(e);
        server_events.push_back(e);
      }
      for (const ServerEvent& x : evaluate) {
        if (!game || x.generation != server.Generation() || server.Closing()) continue;
        EvaluationResult r;
        r.reason = verdict;
        if (verdict == RefusalReason::kNone) r.applied_sequence = ++applied;
        server.SubmitEvaluation(x.request.key, r, net.Now());
      }
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

bool Pending(const WorldDecision& d) { return d.outcome == DecisionOutcome::kPending && !d.final; }

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

// 5000 empty datagrams (what a socket delivers for one too long to take, or an error report) from
// 5000 addresses: one Pump takes exactly kMaxReceivePerPump of them and opens no connection; a
// real client still gets in afterwards.
void FakeNetworkReceiveBudget() {
  World w(11);
  CHECK(w.StartServer(Settings()));
  constexpr size_t kFlood = 5000;
  for (uint32_t i = 0; i < kFlood; ++i) {
    auto s = w.net.Bind(0x0B000000 + i, 4000);
    CHECK(s && s->Send(kServerEp, {}));
  }
  CHECK(w.net.InFlight(kServerEp) == kFlood);
  const uint64_t delivered = w.net.Stats().delivered;
  w.server.Pump(w.net.Now());
  CHECK(w.net.Stats().delivered - delivered == uint64_t(kMaxReceivePerPump));
  CHECK(w.net.InFlight(kServerEp) == kFlood - size_t(kMaxReceivePerPump));
  CHECK(w.server.ConnectionCount() == 0);
  w.server.Pump(w.net.Now());
  CHECK(w.net.InFlight(kServerEp) == 0);
  ServerEvent none;
  CHECK(w.server.ConnectionCount() == 0 && !w.server.PollEvent(none));
  Client& a = w.Connect(MakeHello("A", true));
  CHECK(w.RunUntil([&] { return a.session->Connected(); }));
  CHECK(w.server.PeerCount() == 1 && w.server.ConnectionCount() == 1);
}

// 200 addresses send Hellos the server refuses at once (another protocol version). A refused
// connection lingers kCloseLinger, so it counts against kMaxUnadmitted: the server never holds more
// than kMaxClients + kMaxUnadmitted connections, refuses at most kMaxUnadmitted per linger, keeps
// the owner, and lets real clients in once the flood stops; a full session still says kFull.
void FakeNetworkHelloFlood() {
  World w(77);
  CHECK(w.StartServer(Settings()));
  Client& owner = w.Connect(MakeHello("Owner", true));
  CHECK(w.RunUntil([&] { return owner.session->Connected(); }));
  const size_t refusals_before = w.Count(SE::kRefusalSent);

  Hello bad = MakeHello("Flood");
  bad.protocol_version = kProtocolVersion + 1;
  std::vector<Client*> flood;
  for (int i = 0; i < 200; ++i) flood.push_back(&w.Connect(bad));
  const size_t cap = size_t(kMaxClients) + kMaxUnadmitted;
  const auto start = w.net.Now();
  bool bounded = true;
  size_t most = 0;
  size_t early = 0;  // refusals in the first 100 ms
  while (w.net.Now() < start + 3s) {
    w.Step();
    bounded &= w.server.ConnectionCount() <= cap;
    most = std::max(most, w.server.ConnectionCount());
    if (w.net.Now() <= start + 100ms) early = w.Count(SE::kRefusalSent) - refusals_before;
  }
  const size_t refused = w.Count(SE::kRefusalSent) - refusals_before;
  std::printf("hello flood: at most %zu connections, %zu refusals in 100 ms, %zu in 3 s\n", most,
              early, refused);
  CHECK(bounded);
  CHECK(early >= 1 && early <= kMaxUnadmitted);
  // Two waves at most in 3 s: the first at once, the next when it has lingered kCloseLinger.
  CHECK(refused <= 2 * kMaxUnadmitted);
  CHECK(owner.session->Connected() && w.server.PeerCount() == 1);
  CHECK(w.Count(SE::kPeerLeft) == 0);

  // The flood stops: a good client is welcomed within kPeerTimeout, then two more fill the session
  // and a fifth is refused as full.
  for (Client* c : flood) c->paused = true;
  Client& good = w.Connect(MakeHello("Good"));
  CHECK(w.RunUntil([&] { return good.session->Connected(); }, kPeerTimeout));
  CHECK(good.session->PeerId() == 2);
  Client& c3 = w.Connect(MakeHello("Three"));
  Client& c4 = w.Connect(MakeHello("Four"));
  CHECK(w.RunUntil([&] { return c3.session->Connected() && c4.session->Connected(); }));
  CHECK(w.server.PeerCount() == size_t(kMaxClients));
  Client& fifth = w.Connect(MakeHello("Five"));
  CHECK(w.RunUntil([&] { return fifth.session->State() == ClientState::kRefused; }));
  CHECK(fifth.Last(CE::kRefused) && fifth.Last(CE::kRefused)->refuse_reason == RefuseReason::kFull);
  CHECK(owner.session->Connected() && w.server.ConnectionCount() <= cap);
}

// ---------------------------------------------------------------------------------------------
// Version 2: requests through ServerSession's request table, on a lossy, reordering fake network
// (docs/online-1B.md section 2, "The request state machine"; requests.h has the table's own
// cell-by-cell tests).

EntityRef TestTrigger(uint32_t generation) {
  return WorldRef(generation, 2394, NameHash("JumpHint_Trigger"), NameHash("CTrigger"));
}

WorldRequest EnterRequest(uint32_t generation, uint32_t enter_sequence, uint32_t pose) {
  WorldRequest r;
  r.key = TriggerRequestKey(0, generation, TestTrigger(generation), TriggerEvent::kEnter, enter_sequence);
  r.subject = SelfRef(generation);
  r.event_bits = 0x21;
  r.pose_sequence = pose;
  return r;
}

// The client's view of a key: every decision for it, and where each sits in its events.
std::vector<std::pair<size_t, WorldDecision>> DecisionsFor(const Client& c, const RequestKey& key) {
  std::vector<std::pair<size_t, WorldDecision>> out;
  for (size_t i = 0; i < c.events.size(); ++i) {
    const ClientEvent& e = c.events[i];
    if (e.type == CE::kWorldDecision && e.decision.key == key) out.emplace_back(i, e.decision);
  }
  return out;
}

size_t IndexOf(const Client& c, CE type, uint32_t generation) {
  for (size_t i = 0; i < c.events.size(); ++i) {
    if (c.events[i].type == type && c.events[i].generation == generation) return i;
  }
  return c.events.size();
}

void FakeNetworkRequests() {
  World w(1234);
  LinkConditions c;
  c.loss_percent = 10;
  c.duplicate_percent = 5;
  c.reorder_percent = 10;
  c.reorder_ms = 15;
  c.delay_ms = 10;
  c.jitter_ms = 5;
  w.net.SetDefault(c);
  REQUIRE_OR_RETURN_VOID(w.StartServer(Settings()));
  Client& owner = w.Connect(MakeHello("Owner", true));
  CHECK(w.RunUntil([&] { return owner.session->Connected(); }));
  Client& b = w.Connect(MakeHello("Bee"));
  CHECK(w.RunUntil([&] { return b.session->Connected(); }));
  const uint16_t bee = b.session->PeerId();
  const uint32_t g1 = w.server.RequestLevel("levels/zone_a", w.net.Now());
  CHECK(w.RunUntil([&] { return b.session->Generation() == g1 && owner.session->Generation() == g1; }));

  auto keyed = [&](WorldRequest r) {
    r.key.peer = bee;  // what the client and the server set it to
    return r.key;
  };
  auto server_count = [&](SE type, const RequestKey& key) {
    return size_t(std::count_if(w.server_events.begin(), w.server_events.end(), [&](const ServerEvent& e) {
      return e.type == type && e.request.key == key;
    }));
  };

  // A request that arrives before its pose: pending; applied once the pose comes (the fast lane
  // loses some poses, so the client keeps sending the same one, as its 20 Hz stream would).
  const WorldRequest first = EnterRequest(g1, 1, 1);
  const RequestKey first_key = keyed(first);
  CHECK(b.session->SendWorldRequest(first, w.net.Now()));
  CHECK(w.RunUntil([&] { return server_count(SE::kRequestReceived, first_key) == 1; }));
  PlayerPose pose;
  pose.pose_sequence = 1;
  pose.generation = g1;
  pose.figure.figure_id = 3;
  CHECK(w.RunUntil([&] {
    b.session->SendPlayerPose(pose, w.net.Now());
    return DecisionsFor(b, first_key).size() >= 2;
  }));
  auto ds = DecisionsFor(b, first_key);
  REQUIRE_OR_RETURN_VOID(ds.size() == 2);
  CHECK(Pending(ds[0].second));
  CHECK(ds[1].second.outcome == DecisionOutcome::kApplied && ds[1].second.final &&
        ds[1].second.applied_sequence == w.applied);
  CHECK(server_count(SE::kEvaluateRequest, first_key) == 1);
  CHECK(w.Count(SE::kPlayerPose) == 1);  // the same pose again is dropped
  CHECK(w.Last(SE::kPlayerPose)->pose == pose && w.Last(SE::kPlayerPose)->peer_id == bee);
  // The server set the key's peer: a client cannot speak for another.
  CHECK(w.Last(SE::kRequestReceived)->request.key.peer == bee);

  // A retryable refusal, a resend with the same key that applies, and a retry of the applied key
  // answered from the cache: one apply. Each evaluation is answered `pending` first; the second
  // resend lands while the first is evaluating (a cached `pending`) or after it applied (a cached
  // `applied`), whichever the network made it.
  w.verdict = RefusalReason::kTooFar;
  const WorldRequest second = EnterRequest(g1, 2, 1);
  const RequestKey second_key = keyed(second);
  CHECK(b.session->SendWorldRequest(second, w.net.Now()));
  CHECK(w.RunUntil([&] { return DecisionsFor(b, second_key).size() == 2; }));
  ds = DecisionsFor(b, second_key);
  CHECK(Pending(ds[0].second));
  CHECK(ds[1].second.outcome == DecisionOutcome::kRefused && ds[1].second.reason == RefusalReason::kTooFar &&
        !ds[1].second.final);
  w.verdict = RefusalReason::kNone;
  CHECK(b.session->SendWorldRequest(second, w.net.Now()));
  CHECK(b.session->SendWorldRequest(second, w.net.Now()));
  CHECK(w.RunUntil([&] { return DecisionsFor(b, second_key).size() == 5; }));
  w.Run(200ms);
  ds = DecisionsFor(b, second_key);
  REQUIRE_OR_RETURN_VOID(ds.size() == 5);
  CHECK(Pending(ds[2].second) && !ds[2].second.cached);
  WorldDecision applied_second;
  int applied_count = 0;
  for (size_t i = 3; i < ds.size(); ++i) {
    const WorldDecision& d = ds[i].second;
    if (d.outcome == DecisionOutcome::kApplied && !d.cached) {
      applied_second = d;
      ++applied_count;
    }
  }
  CHECK(applied_count == 1);
  // The cached one repeats the decision before it.
  const WorldDecision& extra = ds[3].second.cached ? ds[3].second : ds[4].second;
  WorldDecision repeat = ds[3].second.cached ? ds[2].second : ds[3].second;
  repeat.cached = true;
  CHECK(extra.cached && extra == repeat);
  CHECK(server_count(SE::kEvaluateRequest, second_key) == 2);

  // The applied steps go to every client; a level end and a reaction to the one named.
  AppliedStep step;
  step.key = second_key;
  step.applied_sequence = applied_second.applied_sequence;
  AppliedStepEntry entry;
  entry.kind = StepKind::kTriggerEnter;
  entry.targets = kTargetActivator | kTargetOther;
  entry.trigger = TestTrigger(g1);
  entry.event_bits = 0x21;
  step.steps.push_back(entry);
  CHECK(w.server.SendAppliedStep(step, w.net.Now()));
  CHECK(w.server.SendLevelEnd(owner.session->PeerId(), LevelEnd{g1, NameHash("CLevelEnd"), SelfRef(g1)}, w.net.Now()));
  CHECK(w.server.SendReaction(bee, Reaction{1, g1, WorldRef(g1, 9, 8, 7), ReactionOutcome::kLanded}, w.net.Now()));
  CHECK(!w.server.SendReaction(4, Reaction{}, w.net.Now()));  // nobody is peer 4
  CHECK(w.RunUntil([&] {
    return owner.Count(CE::kAppliedStep) == 1 && b.Count(CE::kAppliedStep) == 1 &&
           owner.Count(CE::kLevelEnd) == 1 && b.Count(CE::kReaction) == 1;
  }));
  CHECK(owner.Last(CE::kAppliedStep)->applied == step && b.Last(CE::kAppliedStep)->applied == step);
  CHECK(b.Count(CE::kLevelEnd) == 0 && owner.Count(CE::kReaction) == 0);
  CHECK(b.Last(CE::kReaction)->reaction.outcome == ReactionOutcome::kLanded);

  // A generation change: S's final for a key in retry goes before the new LevelLoad; a first send
  // that was in flight at the change is refused `stale` after it, with one final decision.
  w.verdict = RefusalReason::kTooFar;
  const WorldRequest third = EnterRequest(g1, 3, 1);
  const RequestKey third_key = keyed(third);
  CHECK(b.session->SendWorldRequest(third, w.net.Now()));
  CHECK(w.RunUntil([&] { return DecisionsFor(b, third_key).size() == 2; }));
  const WorldRequest in_flight = EnterRequest(g1, 4, 1);
  const RequestKey in_flight_key = keyed(in_flight);
  CHECK(b.session->SendWorldRequest(in_flight, w.net.Now()));
  const uint32_t g2 = w.server.RequestLevel("levels/zone_b", w.net.Now());  // before it lands
  CHECK(w.RunUntil([&] { return DecisionsFor(b, in_flight_key).size() == 1 && b.Count(CE::kLevelLoad) >= 2; }));
  const auto third_ds = DecisionsFor(b, third_key);
  REQUIRE_OR_RETURN_VOID(third_ds.size() == 3);
  CHECK(third_ds[2].second.final && third_ds[2].second.server_final &&
        third_ds[2].second.reason == RefusalReason::kTooFar);
  const size_t load = IndexOf(b, CE::kLevelLoad, g2);
  CHECK(third_ds[2].first < load);
  const auto stale = DecisionsFor(b, in_flight_key);
  CHECK(stale[0].first > load && stale[0].second.reason == RefusalReason::kStale && stale[0].second.final);
  // The resend the client had in flight for the key in retry: the same final, cached.
  CHECK(b.session->SendWorldRequest(third, w.net.Now()));
  CHECK(w.RunUntil([&] { return DecisionsFor(b, third_key).size() == 4; }));
  CHECK(DecisionsFor(b, third_key)[3].second.cached);

  // The per-lane byte counters, per peer on the server and on each client.
  using skylanders::netplay::Lane;
  for (const PeerInfo& p : w.server.Peers()) {
    CHECK(p.stats.Bytes(Lane::kGameplay).sent > 0);
    CHECK(p.stats.Bytes(Lane::kGameplay).received > 0);
    CHECK(p.stats.Bytes(Lane::kFast).received > 0);  // pings, and B's poses
    CHECK(p.stats.BytesSent() > 0 && p.stats.BytesReceived() > 0);
  }
  const auto b_stats = b.session->Stats();
  CHECK(b_stats.Bytes(Lane::kFast).sent > 0);
  CHECK(b_stats.Bytes(Lane::kGameplay).sent > 0);
  CHECK(b_stats.Bytes(Lane::kGameplay).received > 0);

  // End session: a pending telegram gets S's final `session ended`, then the Bye; nothing more
  // leaves a client after SessionClosing.
  w.verdict = RefusalReason::kNone;
  WorldRequest tele;
  TelegramKey tk;
  tk.messenger = WorldRef(g2, 3846, NameHash("Birdee_A_Flyoff"), NameHash("CEntity"));
  tk.index = 2;
  tele.key = TelegramRequestKey(0, g2, 1, tk);
  tele.subject = SelfRef(g2);
  tele.pose_sequence = 50;  // never sent
  const RequestKey tele_key = keyed(tele);
  CHECK(b.session->SendWorldRequest(tele, w.net.Now()));
  CHECK(w.RunUntil([&] { return DecisionsFor(b, tele_key).size() == 1; }));
  w.server.EndSession(w.net.Now());
  CHECK(w.server.Closing() && !w.server.CloseComplete());
  CHECK(w.RunUntil([&] { return b.session->State() == ClientState::kDisconnected &&
                                owner.session->State() == ClientState::kDisconnected; }));
  CHECK(w.server.CloseComplete());
  const auto tele_ds = DecisionsFor(b, tele_key);
  REQUIRE_OR_RETURN_VOID(tele_ds.size() == 2);
  CHECK(tele_ds[1].second.final && tele_ds[1].second.server_final &&
        tele_ds[1].second.reason == RefusalReason::kSessionEnded);
  const size_t closing = IndexOf(b, CE::kSessionClosing, 0);
  CHECK(closing < tele_ds[1].first);
  CHECK(b.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kBye);
  CHECK(server_count(SE::kEvaluateRequest, tele_key) == 0);
  size_t closed = 0, left_closed = 0;
  for (const ServerEvent& e : w.server_events) {
    closed += e.type == SE::kPeerClosed && !e.gone;
    left_closed += e.type == SE::kPeerLeft && e.leave_reason == LeaveReason::kClosed;
    if (e.type == SE::kDecisionSent) CHECK(e.sent);  // every answer went out
  }
  CHECK(closed == 2 && left_closed == 2);
  CHECK(w.server.PeerCount() == 0);
  CHECK(!b.session->SendWorldRequest(tele, w.net.Now()));
}

// A client whose SessionClosed never comes (its game hangs): after kCloseWait the server takes it
// as gone, sends its finals and its Bye; the owner closes normally.
void FakeNetworkCloseGone() {
  World w(4321);
  REQUIRE_OR_RETURN_VOID(w.StartServer(Settings()));
  Client& owner = w.Connect(MakeHello("Owner", true));
  Client& b = w.Connect(MakeHello("Bee"));
  CHECK(w.RunUntil([&] { return owner.session->Connected() && b.session->Connected(); }));
  // Barrier 1 stays open (nobody loads): the drain's leave path stops it waiting for each client.
  const uint32_t g1 = w.server.RequestLevel("levels/zone_a", w.net.Now());
  CHECK(w.RunUntil([&] { return b.session->Generation() == g1 && owner.session->Generation() == g1; }));
  const WorldRequest pending = EnterRequest(g1, 1, 7);  // pose 7 never comes
  CHECK(b.session->SendWorldRequest(pending, w.net.Now()));
  CHECK(w.RunUntil([&] { return b.Count(CE::kWorldDecision) == 1; }));
  CHECK(w.server.BarrierWaitingFor().size() == 2);
  b.paused = true;
  const auto start = w.net.Now();
  w.server.EndSession(start);
  // While the drain is on: no new generation (the server's own call, or the owner's LevelRequest,
  // sent here before its SessionClosing lands), and no Hello is welcomed.
  CHECK(w.server.RequestLevel("levels/zone_b", w.net.Now()) == 0 && w.server.Generation() == g1);
  CHECK(owner.session->SendLevelRequest("levels/zone_c", w.net.Now()));
  Client& late = w.Connect(MakeHello("Late"));
  CHECK(w.RunUntil([&] { return w.server.CloseComplete(); }, 3000ms));
  const auto took = w.net.Now() - start;
  CHECK(took >= kCloseWait && took < kCloseWait + 100ms);
  CHECK(w.RunUntil([&] { return late.session->State() == ClientState::kRefused; }));
  CHECK(late.Last(CE::kRefused) && late.Last(CE::kRefused)->refuse_reason == RefuseReason::kOther &&
        late.Last(CE::kRefused)->text == "the session is ending");
  CHECK(w.Count(SE::kLevelRequest) == 0 && w.server.Generation() == g1);
  CHECK(w.Count(SE::kPeerJoined) == 2);
  bool owner_closed = false, bee_gone = false;
  for (const ServerEvent& e : w.server_events) {
    if (e.type != SE::kPeerClosed) continue;
    owner_closed |= e.peer_id == kOwnerPeerId && !e.gone;
    bee_gone |= e.peer_id == b.session->PeerId() && e.gone;
  }
  CHECK(owner_closed && bee_gone);
  // Each drained client left by the leave path: kPeerClosed, then kPeerLeft (closed); the barrier
  // waits for neither, and neither is connected any more.
  for (const uint16_t id : {kOwnerPeerId, b.session->PeerId()}) {
    size_t closed_at = w.server_events.size(), left_at = w.server_events.size();
    for (size_t i = 0; i < w.server_events.size(); ++i) {
      const ServerEvent& e = w.server_events[i];
      if (e.peer_id != id) continue;
      if (e.type == SE::kPeerClosed) closed_at = std::min(closed_at, i);
      if (e.type == SE::kPeerLeft && e.leave_reason == LeaveReason::kClosed) left_at = std::min(left_at, i);
    }
    CHECK(closed_at < left_at && left_at < w.server_events.size());
  }
  CHECK(w.server.BarrierWaitingFor().empty() && w.server.PeerCount() == 0);
  CHECK(w.RunUntil([&] { return owner.session->State() == ClientState::kDisconnected; }));
  CHECK(owner.Last(CE::kDisconnected)->disconnect_reason == DisconnectReason::kBye);
  // The final S owed the gone client went out (it reaches the client if it ever reads again).
  bool final_sent = false;
  for (const ServerEvent& e : w.server_events) {
    final_sent |= e.type == SE::kDecisionSent && e.peer_id == b.session->PeerId() && e.decision.final &&
                  e.decision.reason == RefusalReason::kSessionEnded && e.sent;
  }
  CHECK(final_sent);
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
  FakeNetworkReceiveBudget();
  FakeNetworkHelloFlood();
  FakeNetworkRequests();
  FakeNetworkCloseGone();
  LoopbackSession();
  return Finish("online_session");
}
