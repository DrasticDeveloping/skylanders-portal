// Obligations (design sections 3.6 and 5, OB1 to OB13; draft 5's credit account): on Server and
// Client over FakeSession, with a small fake world per client that counts fulfilments. Besides each
// case's own expectations, the stepping helper holds the account's two bounds at every step: the
// client holds at most obligation_credit, and S's `used` for it is never below what it holds.

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "backend/client.h"
#include "backend/fake_network.h"
#include "backend/lanes.h"
#include "backend/obligation.h"
#include "backend/server.h"
#include "backend_lanes_harness.h"
#include "backend_request_harness.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

namespace {

using namespace skylanders::backend;
using namespace std::chrono_literals;
namespace lanes = skylanders::backend::test::lanes;
namespace t = skylanders::backend::test;
using lanes::Ent;
using lanes::Key;
using lanes::Val;

// A session-scope durable event (OB13's session-scope obligation): a badge on a session object.
constexpr KindId kBadge = 9;
inline constexpr KindSpec kTestBadge{
    .id = kBadge, .name = "test.badge", .scope = Scope::kSession, .tier = Tier::kDurableEvent,
    .state_class = StateClass::kEvent, .initial = InitialPolicy::kReadAtSpawn,
    .write = WriteMode::kApplied, .max_value_bytes = 16, .pending_grace_ms = 5000,
    .depends_on = {}, .authored_default = {}};
inline constexpr std::array<KindSpec, 9> kObTable{
    t::kTestToggle, t::kTestGate,       t::kTestScene,   t::kTestProp,  t::kTestEvent,
    t::kTestServerOnly, t::kTestTracker, lanes::kTestCounter, kTestBadge};

// The lanes harness's Rig on this file's table: S, the clients, a fake world per client (ticked
// before its pump), the wire log.
struct ObRig {
  explicit ObRig(FakeSessionOptions options = {})
      : session(kObTable, authorities.set, std::move(options)) {
    session.before_client_pump = [this](uint16_t peer, TimePoint now) {
      auto it = worlds.find(peer);
      if (it != worlds.end()) it->second->Tick(now, session.Generation());
    };
    wire.Install(session);
  }
  lanes::TestWorld& Join(uint16_t peer, bool ready = true) {
    if (session.Has(peer)) session.Leave(peer);
    worlds[peer] = std::make_unique<lanes::TestWorld>(kObTable, session.Faults(), peer);
    session.Join(peer, worlds[peer]->Adapters(), peer == 1 ? PeerRole::kOwner : PeerRole::kGuest);
    if (ready) session.LevelReady(peer);
    return *worlds[peer];
  }
  // Until `peer` is level-subscribed with no baseline running on S, and its client holds the level
  // baseline.
  bool WaitLevel(uint16_t peer, Duration max = 5s) {
    return session.RunUntil(
        [&] {
          const PeerStats st = S().Stats(peer);
          return (st.subscription.scopes & kScopeLevel) != 0 && st.baseline == 0 &&
                 (C(peer).State().Subscribed() & kScopeLevel) != 0;
        },
        max);
  }
  lanes::TestWorld& W(uint16_t peer) { return *worlds.at(peer); }
  Server& S() { return session.S(); }
  Client& C(uint16_t peer) { return session.C(peer); }
  TimePoint Now() const { return session.Now(); }
  Duration Elapsed() const { return session.Now() - lanes::kStart; }

  // Steps for `d`, holding the account's bounds for `peer` at every step.
  void Run(Duration d, uint16_t peer = 1) {
    const TimePoint end = Now() + d;
    while (Now() < end) {
      session.Step();
      Check(peer);
    }
  }
  bool RunUntil(const std::function<bool()>& done, Duration max, uint16_t peer = 1) {
    const TimePoint end = Now() + max;
    while (Now() < end) {
      session.Step();
      Check(peer);
      if (done()) return true;
    }
    return done();
  }
  void Check(uint16_t peer) {
    if (!session.Has(peer)) return;
    const uint32_t holding = C(peer).Obligations().Holding();
    max_holding = std::max<size_t>(max_holding, holding);
    if (holding > session.Lim().obligation_credit) ++over_credit;
    if (S().Stats(peer).credit_used < holding) ++used_below;
  }
  // The end S gave obligation `id` of `peer`, if it ended.
  std::optional<ObligationEnd> Ended(uint64_t id, uint16_t peer = 1) const {
    for (const ServerEvent& e : session.ServerEvents()) {
      if (e.type == ServerEvent::Type::kObligationEnded && e.peer == peer && e.obligation == id) {
        return e.obligation_end;
      }
    }
    return std::nullopt;
  }
  size_t Pauses(uint16_t peer = 1) const {
    return size_t(std::count_if(session.ServerEvents().begin(), session.ServerEvents().end(),
                                [&](const ServerEvent& e) {
                                  return e.type == ServerEvent::Type::kCreditPaused && e.peer == peer;
                                }));
  }
  // The account's bounds held at every checked step.
  void CheckBounds() {
    CHECK(over_credit == 0);
    CHECK(used_below == 0);
  }

  lanes::Authorities authorities;
  FakeSession session;
  lanes::WireLog wire;
  std::map<uint16_t, std::unique_ptr<lanes::TestWorld>> worlds;
  size_t max_holding = 0;
  size_t over_credit = 0;
  size_t used_below = 0;
};

FakeSessionOptions Options(size_t credit = 128, std::string faults = {}) {
  FakeSessionOptions o;
  o.limits.obligation_credit = credit;
  o.faults = std::move(faults);
  return o;
}

RecordKey EventKey(uint32_t generation, uint32_t n) { return Key(t::kEvent, Ent(generation, n)); }

// The messages of `type` the client of `peer` took from S.
uint64_t ToClient(ObRig& r, MessageType type, uint16_t peer = 1) {
  return r.wire.Count(peer, false, type);
}
uint64_t ToServer(ObRig& r, MessageType type, uint16_t peer = 1) {
  return r.wire.Count(peer, true, type);
}

// A Mismatch from the client's end, as a failed manifest check sends it: S starts a repair
// baseline at its next pump.
void AskRepair(ObRig& r, uint16_t peer = 1) {
  std::vector<uint8_t> frame;
  AppendMessage(msg::Mismatch{r.S().State().Head(), 1}, frame);
  r.session.ClientEnd(peer).Send(skylanders::netplay::Lane::kState, frame);
}

// ---------------------------------------------------------------------------------------------

BACKEND_TEST("OB1", ObligationDuplicatedDatagram) {
  ObRig r(Options(128, "message:duplicate type=obligation"));
  r.Join(1);
  const RecordKey k = EventKey(1, 1);
  r.W(1).Exists(k.entity, r.Now());
  CHECK(r.WaitLevel(1));
  const uint64_t id = r.S().Oblige(1, k, Val(3));
  CHECK(id == 1);
  CHECK(r.RunUntil([&] { return r.Ended(id).has_value(); }, 3s));
  r.Run(500ms);
  CHECK(ToClient(r, MessageType::kObligation) >= 2);  // the duplicate came
  CHECK(r.W(1).Fulfilled(id) == 1);
  CHECK(r.Ended(id) == ObligationEnd::kCompleted);
  CHECK(r.C(1).Obligations().Holding() == 0);
  r.CheckBounds();
}

BACKEND_TEST("OB2", ObligationLateJoinerGetsNone) {
  ObRig r;
  r.Join(1);
  const RecordKey k = EventKey(1, 1);
  r.W(1).Exists(k.entity, r.Now());
  CHECK(r.WaitLevel(1));
  const uint64_t id = r.S().Oblige(1, k, Val(3));
  CHECK(id != 0);
  r.Join(2);
  r.W(2).Exists(k.entity, r.Now());
  CHECK(r.WaitLevel(2));
  r.Run(1s);
  CHECK(r.W(1).Fulfilled(id) == 1);
  CHECK(r.W(2).FulfilledTotal() == 0);
  CHECK(r.S().Stats(2).obligations_outstanding == 0);
  CHECK(ToClient(r, MessageType::kObligation, 2) == 0);
  r.CheckBounds();
}

BACKEND_TEST("OB3", ObligationRecipientLeaves) {
  ObRig r(Options(1));
  r.Join(1);  // the entity never comes: the first is held, the second waits for credit
  CHECK(r.WaitLevel(1));
  const uint64_t a = r.S().Oblige(1, EventKey(1, 1), Val(1));
  const uint64_t b = r.S().Oblige(1, EventKey(1, 2), Val(2));
  r.Run(300ms);
  PeerStats st = r.S().Stats(1);
  CHECK(st.obligations_outstanding == 2);
  CHECK(st.obligations_waiting == 1);
  CHECK(st.credit_used == 1);
  CHECK(r.Pauses() == 1);
  r.session.Leave(1);
  CHECK(r.Ended(a) == ObligationEnd::kRetiredPeerLeft);
  CHECK(r.Ended(b) == ObligationEnd::kRetiredPeerLeft);
  // Its return (a new peer epoch) starts with nothing used and gets neither.
  r.Join(1);
  r.W(1).Exists(EventKey(1, 1).entity, r.Now());
  r.W(1).Exists(EventKey(1, 2).entity, r.Now());
  CHECK(r.WaitLevel(1));
  r.Run(1s);
  st = r.S().Stats(1);
  CHECK(st.credit_used == 0 && st.obligations_outstanding == 0);
  CHECK(r.W(1).FulfilledTotal() == 0);
  CHECK(r.C(1).Obligations().Holding() == 0);
}

BACKEND_TEST("OB4", ObligationDroppedAtBaselineEpoch) {
  // OB4 when the epoch reaches the client through BaselineBegin, not EpochBegin: a repair baseline
  // that begins after BeginEpoch moves S's cursor past the epoch entry, so EpochBegin is never
  // sent (VR10's Through covers it). The client changes generation at BaselineBegin's epoch and
  // drops its held level obligation there, unfulfilled and unreported; its next Ack returns the
  // credit.
  ObRig r;
  r.Join(1);
  const RecordKey k = EventKey(1, 1);
  r.W(1).Exists(k.entity, r.Now() + 60s);  // late: held by the client
  CHECK(r.WaitLevel(1));
  const uint64_t id = r.S().Oblige(1, k, Val(1));
  CHECK(r.RunUntil([&] { return r.C(1).Obligations().Holding() == 1; }, 1s));
  const uint64_t epoch_begins = ToClient(r, MessageType::kEpochBegin);
  AskRepair(r);               // read by S at its next pump
  r.session.NewGeneration();  // logged before that pump
  CHECK(r.Ended(id) == ObligationEnd::kRetiredEpoch);
  CHECK(r.RunUntil([&] { return r.S().Stats(1).credit_used == 0; }, 1500ms));
  CHECK(ToClient(r, MessageType::kEpochBegin) == epoch_begins);  // the path under test
  CHECK(r.C(1).Obligations().Holding() == 0);
  CHECK(ToServer(r, MessageType::kObligationDone) == 0);
  CHECK(r.W(1).Fulfilled(id) == 0);
  r.CheckBounds();
}

BACKEND_TEST("OB4", ObligationRetiredAtEpoch) {
  ObRig r;
  r.Join(1);
  const RecordKey k = EventKey(1, 1);
  r.W(1).Exists(k.entity, r.Now() + 60s);  // late: held by the client
  CHECK(r.WaitLevel(1));
  const uint64_t id = r.S().Oblige(1, k, Val(1));
  CHECK(r.RunUntil([&] { return r.C(1).Obligations().Holding() == 1; }, 1s));
  CHECK(r.S().Stats(1).credit_used == 1);
  r.session.NewGeneration();
  CHECK(r.Ended(id) == ObligationEnd::kRetiredEpoch);
  // The client drops its held entry at EpochBegin, unfulfilled and unreported; its next Ack returns
  // the credit.
  CHECK(r.RunUntil([&] { return r.S().Stats(1).credit_used == 0; }, 1500ms));
  CHECK(r.C(1).Obligations().Holding() == 0);
  CHECK(ToServer(r, MessageType::kObligationDone) == 0);
  CHECK(r.W(1).Fulfilled(id) == 0);
  r.CheckBounds();
}

BACKEND_TEST("OB5", ObligationEntityNeverResolves) {
  ObRig r;
  r.Join(1);
  CHECK(r.WaitLevel(1));
  const uint64_t id = r.S().Oblige(1, EventKey(1, 1), Val(1));
  r.Run(4500ms);
  CHECK(!r.Ended(id).has_value());  // inside the grace (5 s)
  CHECK(r.RunUntil([&] { return r.Ended(id).has_value(); }, 2s));
  CHECK(r.Ended(id) == ObligationEnd::kRefused);
  // Reported to S and acked: the entry goes at the ack, the credit with the next Ack.
  CHECK(r.RunUntil([&] { return r.S().Stats(1).credit_used == 0; }, 1500ms));
  CHECK(r.C(1).Obligations().Holding() == 0);
  CHECK(ToServer(r, MessageType::kObligationDone) >= 1);
  CHECK(r.W(1).Fulfilled(id) == 0);
  r.CheckBounds();
}

BACKEND_TEST("OB6", ObligationPerPeerCap) {
  // (a) The client holds all it gets (entities late, inside their grace).
  {
    ObRig r;
    r.Join(1);
    const RecordKey k = EventKey(1, 1);
    r.W(1).Exists(k.entity, r.Now() + 3s);
    CHECK(r.WaitLevel(1));
    const uint64_t baselines = lanes::CountServer(r.session, ServerEvent::Type::kBaselineBegun, 1);
    const size_t limits_before = lanes::CountLimit(r.session, "max_obligations_per_peer");
    size_t made = 0, refused = 0;
    bool first_refusal_at_257 = false;
    for (int i = 1; i <= 300; ++i) {
      const uint64_t id = r.S().Oblige(1, k, Val(uint32_t(i)));
      if (id != 0) {
        ++made;
      } else {
        if (refused == 0) first_refusal_at_257 = i == 257;
        ++refused;
      }
    }
    CHECK(made == 256 && refused == 44);
    CHECK(first_refusal_at_257);
    r.Run(1s);  // the session collects S's events at its next step
    CHECK(lanes::CountLimit(r.session, "max_obligations_per_peer") - limits_before == 44);
    CHECK(r.S().Stats(1).obligations_outstanding <= 256);
    CHECK(r.max_holding == 128);  // 128 go out, then S pauses
    CHECK(r.Pauses() == 1);
    CHECK(r.S().Stats(1).obligations_waiting == 128);
    CHECK(lanes::CountServer(r.session, ServerEvent::Type::kBaselineBegun, 1) == baselines);
    r.CheckBounds();
  }
  // (b) Its state lane held (kFull): nothing goes out; the cap still holds.
  {
    ObRig r(Options(128, "link:full peer=1 dir=s2c lane=state from=1500 ms=3000"));
    r.Join(1);
    const RecordKey k = EventKey(1, 1);
    r.W(1).Exists(k.entity, r.Now());
    CHECK(r.WaitLevel(1, 1400ms));
    r.Run(lanes::kStart + 1600ms - r.Now());
    const uint64_t received = ToClient(r, MessageType::kObligation);
    size_t made = 0, refused = 0;
    for (int i = 1; i <= 300; ++i) {
      if (r.S().Oblige(1, k, Val(uint32_t(i))) != 0) {
        ++made;
      } else {
        ++refused;
      }
    }
    CHECK(made == 256 && refused == 44);
    r.Run(1s);
    CHECK(ToClient(r, MessageType::kObligation) == received);  // nothing went out
    CHECK(r.S().Stats(1).obligations_outstanding <= 256);
    CHECK(r.W(1).FulfilledTotal() == 0);
    r.CheckBounds();
  }
}

BACKEND_TEST("OB7", ObligationDuringRepairBaseline) {
  FakeSessionOptions o = Options();
  o.limits.baseline_bytes_per_second = 2000;  // a repair baseline takes seconds
  ObRig r(std::move(o));
  for (uint32_t n = 100; n < 160; ++n) {
    r.S().State().Put(Key(t::kProp, Ent(1, n)), Val(n, 32), r.Now());
  }
  r.Join(1);
  const RecordKey k = EventKey(1, 1);
  r.W(1).Exists(k.entity, r.Now());
  CHECK(r.WaitLevel(1, 20s));
  AskRepair(r);
  CHECK(r.RunUntil([&] { return r.S().Stats(1).baseline != 0; }, 1s));
  CHECK(r.S().Stats(1).baseline_why == BaselineReason::kManifestMismatch);
  const uint64_t id = r.S().Oblige(1, k, Val(5));
  CHECK(id != 0);
  // Not sent while the baseline runs; not retired; delivered after BaselineEnd.
  while (r.S().Stats(1).baseline != 0 && r.Elapsed() < 60s) {
    r.Run(10ms);
    CHECK(ToClient(r, MessageType::kObligation) == 0);
  }
  CHECK(r.RunUntil([&] { return r.Ended(id).has_value(); }, 3s));
  CHECK(r.Ended(id) == ObligationEnd::kCompleted);
  CHECK(r.W(1).Fulfilled(id) == 1);
  r.CheckBounds();
}

BACKEND_TEST("OB8", ObligationNotMadeForLoadingPeer) {
  ObRig r;
  r.Join(1);
  r.Join(2, /*ready=*/false);
  const RecordKey k = EventKey(1, 1);
  r.W(1).Exists(k.entity, r.Now());
  r.W(2).Exists(k.entity, r.Now());
  CHECK(r.WaitLevel(1));
  CHECK(r.S().Oblige(2, k, Val(1)) == 0);  // not made for it
  CHECK(r.S().Stats(2).obligations_outstanding == 0);
  const uint64_t id = r.S().Oblige(1, k, Val(1));
  CHECK(id != 0);
  r.Run(1s);
  CHECK(r.W(1).Fulfilled(id) == 1);
  CHECK(r.W(2).FulfilledTotal() == 0);
  // A session-scope one is made for it.
  r.W(2).Exists(SessionObject(5), r.Now());
  CHECK(r.S().Oblige(2, Key(kBadge, SessionObject(5)), Val(1)) != 0);
}

BACKEND_TEST("OB9", ObligationAfterItsRow) {
  ObRig r;
  r.Join(1);
  const EntityId chest = Ent(1, 7);
  r.W(1).Exists(chest, r.Now());
  CHECK(r.WaitLevel(1));
  std::vector<std::string> order;
  uint64_t ob_lsn = 0;
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer != 1 || to_server) return;
    if (auto* d = std::get_if<msg::Delta>(&m); d && d->record.key.entity == chest) order.push_back("row");
    if (auto* o = std::get_if<msg::Obligation>(&m)) {
      order.push_back("obligation");
      ob_lsn = o->lsn;
    }
  };
  CHECK(r.S().State().Put(Key(t::kProp, chest), Val(1), r.Now()) == PutResult::kWritten);
  const uint64_t row_lsn = r.S().State().Head();
  const uint64_t id = r.S().Oblige(1, Key(t::kEvent, chest), Val(9));
  CHECK(r.RunUntil([&] { return r.Ended(id).has_value(); }, 2s));
  CHECK(order.size() == 2 && order[0] == "row" && order[1] == "obligation");
  CHECK(ob_lsn == row_lsn + 1);
  CHECK(r.W(1).Fulfilled(id) == 1);
  CHECK(r.C(1).State().Completes(Scope::kLevel, ob_lsn));
  // The row was applied before the fulfilment.
  const auto& log = r.W(1).ApplyLog();
  CHECK(std::find(log.begin(), log.end(), t::kProp) != log.end());
  r.wire.on_message = nullptr;
}

BACKEND_TEST("OB10", ObligationRepairBaselineRetiresSentOnly) {
  ObRig r;
  r.Join(1);
  const RecordKey k1 = EventKey(1, 1), k2 = EventKey(1, 2);
  r.W(1).Exists(k2.entity, r.Now());  // k1's entity is late
  CHECK(r.WaitLevel(1));
  const uint64_t sent = r.S().Oblige(1, k1, Val(1));
  CHECK(r.RunUntil([&] { return r.C(1).Obligations().Holding() == 1; }, 1s));
  // The repair begins at the next pump; the second is made just before it, so it is not yet sent.
  AskRepair(r);
  const uint64_t unsent = r.S().Oblige(1, k2, Val(2));
  r.Run(10ms);
  CHECK(r.Ended(sent) == ObligationEnd::kRetiredBaseline);
  CHECK(!r.Ended(unsent).has_value());
  // After BaselineEnd the unsent one goes out (the default credit leaves room) and is fulfilled.
  CHECK(r.RunUntil([&] { return r.Ended(unsent).has_value(); }, 5s));
  CHECK(r.Ended(unsent) == ObligationEnd::kCompleted);
  CHECK(r.W(1).Fulfilled(unsent) == 1);
  // The retired one is still held and costs its credit (the unsent one's entry goes at S's ack).
  CHECK(r.RunUntil([&] { return r.C(1).Obligations().Holding() == 1; }, 1s));
  CHECK(r.S().Stats(1).credit_used >= 1);
  CHECK(r.W(1).Fulfilled(sent) == 0);
  // Its entity comes: fulfilled exactly once; S acks and ignores the report; its end stays.
  r.W(1).Exists(k1.entity, r.Now());
  CHECK(r.RunUntil([&] { return r.W(1).Fulfilled(sent) == 1; }, 2s));
  CHECK(r.RunUntil([&] { return r.S().Stats(1).obligation_reports_ignored == 1; }, 1s));
  r.Run(1500ms);
  CHECK(r.W(1).Fulfilled(sent) == 1);
  CHECK(r.Ended(sent) == ObligationEnd::kRetiredBaseline);
  CHECK(r.C(1).Obligations().Holding() == 0);
  CHECK(r.S().Stats(1).credit_used == 0);
  r.CheckBounds();
}

BACKEND_TEST("OB11", ObligationDuplicateAfterDone) {
  ObRig r;
  r.Join(1);
  const RecordKey k = EventKey(1, 1);
  r.W(1).Exists(k.entity, r.Now());
  CHECK(r.WaitLevel(1));
  const uint64_t id = r.S().Oblige(1, k, Val(4));
  CHECK(r.RunUntil([&] { return r.W(1).Fulfilled(id) == 1; }, 2s));
  msg::Obligation dup;
  dup.id = id;
  dup.floor = 1;
  dup.scope = Scope::kLevel;
  dup.lsn = 1;
  dup.key = k;
  dup.value = Val(4);
  std::vector<uint8_t> frame;
  CHECK(AppendMessage(dup, frame));
  MemoryLink* raw = r.session.MemoryServerEnd(1);
  REQUIRE_OR_RETURN_VOID(raw != nullptr);
  // (a) After the client fulfilled it, before the floor passed it.
  CHECK(raw->Send(skylanders::netplay::Lane::kState, frame) == LinkSend::kSent);
  r.Run(200ms);
  CHECK(r.W(1).Fulfilled(id) == 1);
  // (b) After S's floor moved past it.
  CHECK(r.RunUntil([&] { return r.S().Stats(1).obligation_floor > id; }, 2s));
  CHECK(raw->Send(skylanders::netplay::Lane::kState, frame) == LinkSend::kSent);
  r.Run(500ms);
  CHECK(r.W(1).Fulfilled(id) == 1);
  CHECK(r.C(1).Obligations().Holding() == 0);
  CHECK(r.Ended(id) == ObligationEnd::kCompleted);
}

BACKEND_TEST("OB12", ObligationCreditPause) {
  ObRig r(Options(4));
  r.Join(1);
  std::array<RecordKey, 5> keys;
  for (uint32_t i = 0; i < 5; ++i) keys[i] = EventKey(1, i + 1);
  r.W(1).Exists(keys[0].entity, r.Now() + 60s);  // set below: 2 s after the obligations
  for (uint32_t i = 1; i < 5; ++i) r.W(1).Exists(keys[i].entity, r.Now());
  CHECK(r.WaitLevel(1));
  r.W(1).Exists(keys[0].entity, r.Now() + 2s);
  std::vector<std::string> order;
  bool one_outstanding_when_five_came = false;
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer != 1 || to_server) return;
    if (auto* d = std::get_if<msg::Delta>(&m); d && d->record.key.kind == t::kProp) order.push_back("delta");
    if (auto* o = std::get_if<msg::Obligation>(&m)) {
      order.push_back("ob" + std::to_string(o->id));
      if (o->id == 5) one_outstanding_when_five_came = r.W(1).Fulfilled(1) == 0;
    }
  };
  std::array<uint64_t, 5> ids{};
  for (uint32_t i = 0; i < 5; ++i) ids[i] = r.S().Oblige(1, keys[i], Val(i));
  CHECK(ids[0] == 1 && ids[4] == 5);
  r.S().State().Put(Key(t::kProp, Ent(1, 40)), Val(1), r.Now());  // a delta after 5
  r.Run(10ms);
  CHECK(r.Pauses() == 1);
  CHECK(r.S().Stats(1).obligations_waiting == 1);
  CHECK(r.RunUntil([&] { return r.Ended(5).has_value(); }, 2s));  // while 1 is outstanding
  CHECK(!r.Ended(1).has_value());
  CHECK(one_outstanding_when_five_came);
  // The delta went out in the pump that paused 5.
  const auto delta_at = std::find(order.begin(), order.end(), "delta");
  const auto five_at = std::find(order.begin(), order.end(), "ob5");
  CHECK(delta_at != order.end() && five_at != order.end() && delta_at < five_at);
  CHECK(r.RunUntil([&] { return r.Ended(1).has_value(); }, 3s));
  for (const uint64_t id : ids) {
    CHECK(r.Ended(id) == ObligationEnd::kCompleted);
    CHECK(r.W(1).Fulfilled(id) == 1);
  }
  CHECK(r.max_holding <= 4);
  r.CheckBounds();
  r.wire.on_message = nullptr;
}

// OB13's setup: C as given; two level-scope obligations sent and held (their entities late), then a
// session-scope one, which waits for credit. Returns the three ids.
std::array<uint64_t, 3> Ob13Setup(ObRig& r, bool level_entities_ever) {
  r.Join(1);
  const EntityId badge = SessionObject(77);
  r.W(1).Exists(badge, r.Now());
  if (level_entities_ever) {
    r.W(1).Exists(EventKey(1, 1).entity, r.Now() + 60s);
    r.W(1).Exists(EventKey(1, 2).entity, r.Now() + 60s);
  }
  r.WaitLevel(1, 1400ms);
  r.Run(lanes::kStart + 1500ms - r.Now());
  std::array<uint64_t, 3> ids{};
  ids[0] = r.S().Oblige(1, EventKey(1, 1), Val(1));
  ids[1] = r.S().Oblige(1, EventKey(1, 2), Val(2));
  r.Run(300ms);
  ids[2] = r.S().Oblige(1, Key(kBadge, badge), Val(3));
  r.Run(lanes::kStart + 2000ms - r.Now());
  return ids;
}

BACKEND_TEST("OB13", ObligationCreditReturnedByDrop) {
  // (a) A generation change.
  {
    ObRig r(Options(2));
    const auto ids = Ob13Setup(r, true);
    CHECK(r.C(1).Obligations().Holding() == 2);
    CHECK(r.S().Stats(1).obligations_waiting == 1);
    CHECK(!r.Ended(ids[2]).has_value());
    r.session.NewGeneration();
    CHECK(r.Ended(ids[0]) == ObligationEnd::kRetiredEpoch);
    CHECK(r.Ended(ids[1]) == ObligationEnd::kRetiredEpoch);
    CHECK(r.RunUntil([&] { return r.Ended(ids[2]).has_value(); }, 2s));
    CHECK(r.Ended(ids[2]) == ObligationEnd::kCompleted);
    CHECK(ToServer(r, MessageType::kObligationDone) == 1);  // only the session one's
    CHECK(r.W(1).Fulfilled(ids[0]) == 0 && r.W(1).Fulfilled(ids[1]) == 0);
    r.CheckBounds();
  }
  // (b) No generation change: the two are refused at their kind's grace.
  {
    ObRig r(Options(2));
    const auto ids = Ob13Setup(r, false);
    CHECK(r.RunUntil([&] { return r.Ended(ids[0]).has_value() && r.Ended(ids[1]).has_value(); }, 6s));
    CHECK(r.Ended(ids[0]) == ObligationEnd::kRefused);
    CHECK(r.Ended(ids[1]) == ObligationEnd::kRefused);
    CHECK(r.RunUntil([&] { return r.Ended(ids[2]).has_value(); }, 2s));
    CHECK(r.Ended(ids[2]) == ObligationEnd::kCompleted);
    CHECK(r.W(1).Fulfilled(ids[2]) == 1);
    r.CheckBounds();
  }
  // (c) (a) with the client's first Ack after the drop killed: the periodic Ack returns the credit
  // at most one obligation_report_interval later.
  {
    ObRig r(Options(2, "message:kill type=ack dir=c2s from=2000 to=2150"));
    const auto ids = Ob13Setup(r, true);
    r.session.NewGeneration();
    const TimePoint drop = r.Now();
    CHECK(r.RunUntil([&] { return r.Ended(ids[2]).has_value(); }, 2s));
    CHECK(r.Now() - drop <= 1300ms);
    CHECK(r.Ended(ids[2]) == ObligationEnd::kCompleted);
    CHECK(r.session.Faults() && r.session.Faults()->Fired(0) >= 1);
    r.CheckBounds();
  }
  // (d) C = 1: a session-scope Obligation killed above the lane, then a second made.
  {
    ObRig r(Options(1, "message:kill type=obligation nth=1"));
    r.Join(1);
    const EntityId badge = SessionObject(77);
    r.W(1).Exists(badge, r.Now());
    CHECK(r.WaitLevel(1));
    const uint64_t killed = r.S().Oblige(1, Key(kBadge, badge), Val(1));
    r.Run(100ms);
    const uint64_t second = r.S().Oblige(1, Key(kBadge, badge), Val(2));
    r.Run(100ms);
    CHECK(r.S().Stats(1).credit_used == 1);  // the killed one counts as used
    CHECK(r.S().Stats(1).obligations_waiting == 1);
    // The mark raises the client's seen to 1; its next Ack returns the credit.
    CHECK(r.RunUntil([&] { return r.Ended(second).has_value(); }, 1500ms));
    CHECK(r.C(1).Obligations().Seen() >= killed);
    CHECK(r.Ended(second) == ObligationEnd::kCompleted);
    CHECK(r.W(1).Fulfilled(second) == 1);
    r.Run(2s);
    CHECK(r.W(1).Fulfilled(killed) == 0);  // never resent, never fulfilled
    CHECK(ToClient(r, MessageType::kObligation) == 1);
    r.CheckBounds();
  }
}

// OB14 (council 2 B11): an ObligationDone for an id S made and has not sent (no client can have
// received it) ends nothing: it is ignored and counted like one for an ended id, and the obligation
// stays, goes out in its turn, and ends at the report that follows its send. On the ledger itself,
// as Server::Handle calls it (it answers every ObligationDone with an ObligationAck either way).
BACKEND_TEST("OB14", ObligationDoneForAnUnsentId) {
  ObligationLedger ledger{Limits{}};
  Obligation o;
  o.recipient = 1;
  o.key = EventKey(1, 1);
  o.value = Val(1);
  o.lsn = 5;
  ledger.Add(o);
  const uint64_t id = ledger.NextId() - 1;
  CHECK(!ledger.OnDone(id));
  CHECK(ledger.Ignored() == 1);
  CHECK(ledger.Get(id) != nullptr && !ledger.Get(id)->sent);
  CHECK(ledger.Waiting() == 1 && ledger.Outstanding() == 1);
  CHECK(ledger.Floor() == id);
  const Obligation* next = ledger.NextToSend(5);
  CHECK(next != nullptr && next->id == id);
  ledger.MarkSent(id, lanes::kStart);
  CHECK(ledger.OnDone(id));
  CHECK(ledger.Get(id) == nullptr && ledger.Outstanding() == 0);
  CHECK(ledger.Ignored() == 1);
  CHECK(!ledger.OnDone(id));  // ended already: ignored again
  CHECK(ledger.Ignored() == 2);
}

}  // namespace
