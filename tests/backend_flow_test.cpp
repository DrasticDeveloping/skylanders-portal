// Convergence, baselines, the proven prefix and scene release on the real façades (design sections
// 3.5, 5 and 10; work items 3 and 5): backend::Server and backend::Client on fake_network.h's
// FakeSession, over MemoryLink or NetplayLink, with the fault injector playing kills, holds and
// delays. The ids are the design's: CV1 to CV6, BL1 to BL10, PP1, PP2, PP4, PP6 (the held variant),
// PP7, PP8, SR1 to SR7 with SR6b to SR6e (SR6c with its compare step) and LM11. SR7b's two-pump
// timing stays on direct calls (backend_world_state_test.cpp). The same cases on WorldState and
// Replica alone (a direct-call stand-in of S's send) stay in backend_world_state_flow_test.cpp
// under "WS-" ids.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "backend/compare.h"
#include "backend/fake_network.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using namespace test::lanes;

struct FlowRng {
  uint64_t s;
  uint64_t Next() {
    uint64_t z = (s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
  }
  uint64_t Below(uint64_t n) { return n == 0 ? 0 : Next() % n; }
};

// The level's entities (generation 1): props 1..rows, toggles 100..107, the scene 200; and the
// session objects of the trackers (1..6) and the server-only rows (11..14).
constexpr uint32_t kToggles = 100;
constexpr uint32_t kSceneN = 200;
RecordKey Prop(uint32_t n, uint32_t g = 1) { return Key(test::kProp, Ent(g, n)); }
RecordKey Toggle(uint32_t i, uint32_t g = 1) { return Key(test::kToggle, Ent(g, kToggles + i)); }
RecordKey Scene(uint32_t g = 1) { return Key(test::kScene, Ent(g, kSceneN)); }
RecordKey Tracker(uint32_t n) { return Key(test::kTracker, SessionObject(n)); }
RecordKey ServerOnly(uint32_t n) { return Key(test::kServerOnly, SessionObject(10 + n)); }

// Every entity of generation `g` in every world, from the start.
void Entities(Rig& r, uint32_t rows, uint32_t g = 1) {
  for (uint32_t n = 1; n <= rows; ++n) r.Everywhere(Ent(g, n), kStart);
  for (uint32_t i = 0; i < 8; ++i) r.Everywhere(Ent(g, kToggles + i), kStart);
  r.Everywhere(Ent(g, kSceneN), kStart);
  for (uint32_t n = 900; n < 910; ++n) r.Everywhere(Ent(g, n), kStart);
}

// The level in place on S: `rows` props, the scene (closed) and its toggles.
void Level(Rig& r, uint32_t rows) {
  Entities(r, rows);
  for (uint32_t n = 1; n <= rows; ++n) r.Put(Prop(n), Val(n, 8));
  r.Put(Scene(), Val(1));
  for (uint32_t i = 0; i < 8; ++i) r.Put(Toggle(i), Val(1));
}

// S's writes: props, toggles, the scene, session trackers and server-only rows, with removes.
struct Churn {
  Rig& r;
  FlowRng rng;
  uint32_t rows = 60;
  uint32_t value = 1;

  RecordKey RandomKey() {
    switch (rng.Below(8)) {
      case 0: return Toggle(uint32_t(rng.Below(8)));
      case 1: return Scene();
      case 2: return Tracker(1 + uint32_t(rng.Below(6)));
      case 3: return ServerOnly(1 + uint32_t(rng.Below(4)));
      default: return Prop(1 + uint32_t(rng.Below(rows)));
    }
  }
  void Write(int n) {
    for (int i = 0; i < n; ++i) {
      const RecordKey k = RandomKey();
      if (rng.Below(10) == 0) {
        r.S().State().Remove(k, r.Now());
      } else {
        r.S().State().Put(k, Val(value++, 8), r.Now());
      }
    }
  }
};

size_t Begun(Rig& r, uint16_t peer, std::optional<BaselineReason> why = {}) {
  size_t n = 0;
  for (const ServerEvent& e : r.session.ServerEvents()) {
    n += e.type == ServerEvent::Type::kBaselineBegun && e.peer == peer && (!why || e.why == *why);
  }
  return n;
}
size_t Repairs(Rig& r, uint16_t peer) {
  return Begun(r, peer, BaselineReason::kManifestMismatch) +
         Begun(r, peer, BaselineReason::kDigestMismatch) +
         Begun(r, peer, BaselineReason::kBelowFloor) + Begun(r, peer, BaselineReason::kSilence);
}

// Section 5's "quiet" and "equal": no write meanwhile, no baseline staging, a manifest matched at
// the head, and the peer's map equal to S's.
bool Quiet(Rig& r, std::initializer_list<uint16_t> peers, Duration max = 30s) {
  return RunUntilEvery(
      r.session,
      [&] {
        for (uint16_t p : peers) {
          if (!r.session.Has(p)) return false;
          const Replica& c = r.C(p).State();
          if (c.Staging() != 0 || c.ManifestOk() != r.S().State().Head()) return false;
          if (r.S().Stats(p).baseline != 0 || !Equal(r.session, p, false)) return false;
        }
        return true;
      },
      max, 100ms);
}

// Peer `peer` joined and level-ready, with its level baseline applied.
bool Ready(Rig& r, uint16_t peer, Duration max = 10s) {
  r.Join(peer);
  return r.session.RunUntil(
      [&] {
        return (r.C(peer).State().Subscribed() & kScopeLevel) != 0 &&
               r.S().Stats(peer).baseline == 0;
      },
      max);
}

// Peer `peer` joined with its session baseline applied, then level-ready: its level baseline is a
// baseline of its own (Rig::Join with `ready` asks both before the first pump, so S sends one).
bool JoinThenLevel(Rig& r, uint16_t peer, Duration max = 5s) {
  r.Join(peer, /*ready=*/false);
  const bool session = r.session.RunUntil(
      [&] {
        return (r.C(peer).State().Subscribed() & kScopeSession) != 0 &&
               r.S().Stats(peer).baseline == 0;
      },
      max);
  r.session.LevelReady(peer);
  return session;
}

// The revisions a replica holds never go down (section 7.3), checked every step.
struct RevisionWatch {
  std::map<RecordKey, uint32_t> seen;
  bool ok = true;
  void Check(const Replica& c) {
    c.ForEach([&](const Record& row) {
      uint32_t& s = seen[row.key];
      if (row.revision < s) ok = false;
      s = std::max(s, row.revision);
    });
  }
};

// Steps `d`, writing `per_step` churn writes before each step and calling `each` after it.
void ChurnFor(Rig& r, Churn& churn, Duration d, int per_step,
              const std::function<void()>& each = {}) {
  const TimePoint end = r.Now() + d;
  while (r.Now() < end) {
    churn.Write(per_step);
    r.session.Step();
    if (each) each();
  }
}

FakeSessionOptions Lossy(uint64_t seed, int one_way_ms, double loss_percent, std::string faults = {}) {
  FakeSessionOptions o;
  o.seed = seed;
  o.netplay = true;
  o.conditions.delay_ms = one_way_ms;
  o.conditions.loss_percent = loss_percent;
  o.faults = std::move(faults);
  o.step = 20ms;
  return o;
}

void ConvergeWithLoss(int one_way_ms, double loss_percent) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Rig r(Lossy(uint64_t(seed), one_way_ms, loss_percent));
    Level(r, 60);
    CHECK(Ready(r, 1));
    Churn churn{r, FlowRng{uint64_t(seed) * 7}};
    RevisionWatch watch;
    ChurnFor(r, churn, test::Long() ? 60s : 15s, 1, [&] { watch.Check(r.C(1).State()); });
    CHECK(Quiet(r, {1}, 40s));
    CHECK(watch.ok);
    CHECK(Repairs(r, 1) == 0);
    CHECK(r.S().Stats(1).mismatches == 0);
  }
}

// The text of an entity for the injector's key= word.
std::string KeyText(const RecordKey& k) { return ToText(k.entity); }

}  // namespace

// ---- Convergence after loss ---------------------------------------------------------------------

BACKEND_TEST("CV1", ConvergeLoss5Rtt300) { ConvergeWithLoss(150, 5); }

BACKEND_TEST("CV2", ConvergeLoss30Rtt300) { ConvergeWithLoss(150, 30); }

BACKEND_TEST("CV3", ConvergeDuplicatesAndReorder) {
  // Datagram duplicates and reordering under netplay's lanes: the lane hides them; the replica
  // ends equal, no revision ever goes down, no repair.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    FakeSessionOptions o = Lossy(uint64_t(seed), 60, 5);
    o.conditions.duplicate_percent = 10;
    o.conditions.reorder_percent = 20;
    o.conditions.jitter_ms = 40;
    Rig r(o);
    Level(r, 60);
    CHECK(Ready(r, 1));
    Churn churn{r, FlowRng{uint64_t(seed) * 11}};
    RevisionWatch watch;
    ChurnFor(r, churn, 15s, 1, [&] { watch.Check(r.C(1).State()); });
    CHECK(Quiet(r, {1}, 40s));
    CHECK(watch.ok);
    CHECK(Repairs(r, 1) == 0);
  }
}

BACKEND_TEST("CV4", ConvergeKilledDelta) {
  // One delta killed above the lane (S cannot see it), of a key nothing writes again: one
  // mismatch, one repair baseline, equal after.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    const RecordKey k = Prop(900);
    Rig r(Lossy(uint64_t(seed), 150, 5,
                "message:kill dir=s2c peer=1 type=delta key=" + KeyText(k) + " nth=1"));
    Level(r, 60);
    CHECK(Ready(r, 1));
    Churn churn{r, FlowRng{uint64_t(seed) * 17}};
    ChurnFor(r, churn, 5s, 1);
    r.Put(k, Val(1, 8));
    ChurnFor(r, churn, 5s, 1);
    CHECK(r.session.Faults()->Fired(0) == 1);
    CHECK(Quiet(r, {1}, 40s));
    CHECK(r.S().Stats(1).mismatches == 1);
    CHECK(Repairs(r, 1) == 1);
  }
}

BACKEND_TEST("CV5", ConvergeKilledBaselineEnd) {
  // The level baseline's BaselineEnd killed: the level stays staging; Through still moves the
  // prefix but completes nothing of the level (Opus F1, F8); the session scope completes; the next
  // manifest finds the client staging, so the end was lost, and the client asks for a repair
  // (Mismatch{lsn, 0}; Sol code review r2, R4) instead of waiting for the silence rule.
  FakeSessionOptions o;
  o.faults = "message:kill dir=s2c peer=1 type=baseline_end nth=2";  // the join's is the first
  o.step = 50ms;
  Rig r(o);
  Level(r, 60);
  CHECK(JoinThenLevel(r, 1));
  Churn churn{r, FlowRng{55}};
  bool level_completed_while_staging = false, session_completed = false;
  bool prefix_moved_while_staging = false;
  uint64_t prefix_at_stage = 0;
  ChurnFor(r, churn, 75s, 1, [&] {
    const Replica& c = r.C(1).State();
    if ((c.Staging() & kScopeLevel) == 0) return;
    if (prefix_at_stage == 0) prefix_at_stage = c.Prefix();
    if (c.Prefix() > prefix_at_stage + 20) prefix_moved_while_staging = true;
    if (c.Completes(Scope::kLevel, c.Prefix())) level_completed_while_staging = true;
    if (c.Completes(Scope::kSession, c.Prefix())) session_completed = true;
  });
  CHECK(r.session.Faults()->Fired(0) == 1);
  CHECK(prefix_moved_while_staging);
  CHECK(!level_completed_while_staging);
  CHECK(session_completed);
  CHECK(Begun(r, 1, BaselineReason::kDigestMismatch) >= 1);
  CHECK(CountClient(r.session, 1, ClientEvent::Type::kBaselineLost) >= 1);
  CHECK(Quiet(r, {1}));
  CHECK(r.C(1).State().Staging() == 0);
}

BACKEND_TEST("CV6", ConvergeSilence60s) {
  // Peer 1's manifests are killed (its acks flow with an unchanged manifest_ok): one silence
  // baseline at about 60 s. Peer 2's manifests match (its manifest_ok moves): none. Then a quiet
  // session of more than 60 s: every manifest matches at the same head, and nothing repairs (the
  // proof at the head stands while the head stands).
  FakeSessionOptions o;
  o.faults = "message:kill dir=s2c peer=1 type=manifest to=72000";
  o.step = 50ms;
  Rig r(o);
  Level(r, 60);
  CHECK(Ready(r, 1));
  CHECK(Ready(r, 2));
  const TimePoint start = r.Now();
  Churn churn{r, FlowRng{66}};
  std::optional<TimePoint> repaired_at;
  ChurnFor(r, churn, 70s, 1, [&] {
    if (!repaired_at && Begun(r, 1, BaselineReason::kSilence) > 0) repaired_at = r.Now();
  });
  CHECK(Begun(r, 1, BaselineReason::kSilence) == 1);
  CHECK(repaired_at && *repaired_at - start >= 55s && *repaired_at - start <= 62s);
  CHECK(Repairs(r, 2) == 0);
  CHECK(Quiet(r, {1, 2}));
  // Quiet for 90 s: no baseline of any kind.
  const size_t b1 = Begun(r, 1), b2 = Begun(r, 2);
  r.session.Run(90s);
  CHECK(Begun(r, 1) == b1);
  CHECK(Begun(r, 2) == b2);
  CHECK(Equal(r.session, 1) && Equal(r.session, 2));
}

// ---- Baseline under churn and mid-baseline disconnect ---------------------------------------

BACKEND_TEST("BL1", BaselineUnderChurn) {
  // 2,000 rows of 32 bytes, 500 writes a second while the paced level baseline runs (about 4 s at
  // 40 KB/s): equal once quiet, no repair, no digest failure.
  FakeSessionOptions o;
  o.step = 20ms;
  Rig r(o);
  for (uint32_t n = 1; n <= 2000; ++n) {
    r.Everywhere(Ent(1, n), kStart);
    r.Put(Prop(n), Val(n, 32));
  }
  r.Join(1);
  FlowRng rng{1};
  uint32_t value = 10000;
  const TimePoint end = r.Now() + 6s;
  while (r.Now() < end) {
    for (int i = 0; i < 10; ++i) r.Put(Prop(1 + uint32_t(rng.Below(2000))), Val(value++, 32));
    r.session.Step();
  }
  CHECK(Quiet(r, {1}));
  CHECK(r.C(1).State().Rows() == 2000);
  CHECK(Repairs(r, 1) == 0);
  CHECK(CountClient(r.session, 1, ClientEvent::Type::kBaselineFailed) == 0);
}

BACKEND_TEST("BL2", BaselineStagingSwap) {
  // A row the replica has and S never had: the manifest finds it, the repair baseline's swap
  // removes it.
  Rig r;
  Level(r, 60);
  CHECK(Ready(r, 1));
  CHECK(Quiet(r, {1}));
  Record extra;
  extra.key = Prop(999);
  extra.revision = 3;
  extra.epoch = 1;
  extra.lsn = r.S().State().Head();
  extra.value = Val(1);
  const_cast<Replica&>(r.C(1).State()).OnRecord(extra, extra.lsn);  // a bug's leftover
  CHECK(r.C(1).State().Get(extra.key) != nullptr);
  r.Put(Prop(1), Val(77, 8));  // the next manifest has a new head
  CHECK(Quiet(r, {1}));
  CHECK(Repairs(r, 1) == 1);
  CHECK(r.C(1).State().Get(extra.key) == nullptr);
}

BACKEND_TEST("BL3", BaselineMidDisconnect) {
  // The peer drops after the level baseline's fifth chunk: nothing of the partial one stays; the
  // rejoin gets new baseline ids (S never reuses one for a peer id).
  FakeSessionOptions o;
  o.faults = "peer:disconnect peer=1 after=baseline_rows:5";
  o.limits.baseline_bytes_per_second = 4000;  // a few chunks a second
  Rig r(o);
  for (uint32_t n = 1; n <= 300; ++n) {
    r.Everywhere(Ent(1, n), kStart);
    r.Put(Prop(n), Val(n, 32));
  }
  r.Put(Tracker(1), Val(1));
  r.Join(1);
  uint32_t partial = 0;
  r.session.on_server_event = [&](const ServerEvent& e) {
    if (e.type == ServerEvent::Type::kBaselineBegun && e.peer == 1) partial = e.baseline;
  };
  CHECK(r.session.RunUntil([&] { return !r.session.Has(1); }, 30s));
  CHECK(r.session.Faults()->Fired(0) == 1);
  CHECK(partial != 0);
  r.session.on_server_event = nullptr;
  r.session.Run(1s);
  std::vector<uint32_t> rejoin_ids;
  r.session.on_server_event = [&](const ServerEvent& e) {
    if (e.type == ServerEvent::Type::kBaselineBegun && e.peer == 1) rejoin_ids.push_back(e.baseline);
  };
  r.Join(1);
  CHECK(r.C(1).State().Rows() == 0);  // a fresh client: nothing of the partial baseline
  CHECK(Quiet(r, {1}, 120s));
  CHECK(!rejoin_ids.empty());
  for (uint32_t id : rejoin_ids) CHECK(id > partial);
  CHECK(r.C(1).State().Rows() == 301);
}

BACKEND_TEST("BL4", BaselineLateJoin) {
  // Peer 2 joins late, level-ready 2 s after: no level row before its level ready, nothing waits on
  // it (peer 1's prefix keeps moving), rows written during its level baseline arrive, no repair.
  FakeSessionOptions o;
  o.limits.baseline_bytes_per_second = 8000;
  Rig r(o);
  Level(r, 100);
  CHECK(Ready(r, 1));
  Churn churn{r, FlowRng{44}};
  churn.rows = 100;
  ChurnFor(r, churn, 3s, 1);
  r.Join(2, /*ready=*/false);
  bool level_before_ready = false;
  ChurnFor(r, churn, 2s, 1, [&] {
    r.C(2).State().ForEach([&](const Record& row) {
      if (FindKind(kLanesTable, row.key.kind)->scope == Scope::kLevel) level_before_ready = true;
    });
  });
  r.session.LevelReady(2);
  uint64_t p1 = r.C(1).State().Prefix();
  size_t p1_moves = 0;
  ChurnFor(r, churn, 3s, 1, [&] {
    if (r.C(1).State().Prefix() > p1) {
      p1 = r.C(1).State().Prefix();
      ++p1_moves;
    }
  });
  CHECK(!level_before_ready);
  CHECK(p1_moves > 20);
  CHECK(Quiet(r, {1, 2}));
  CHECK(Repairs(r, 1) == 0 && Repairs(r, 2) == 0);
}

BACKEND_TEST("BL5", BaselineOneAtATime) {
  // A mismatch whose LSN is at or before the running baseline's `at` starts no second one; one
  // after `at` starts a second one once the first ends; never two at once.
  FakeSessionOptions o;
  o.limits.baseline_bytes_per_second = 4000;
  Rig r(o);
  Level(r, 300);
  CHECK(Ready(r, 1, 60s));
  CHECK(Quiet(r, {1}, 60s));
  const size_t begun = Begun(r, 1);
  bool overlap = false;
  bool running = false;
  r.session.on_server_event = [&](const ServerEvent& e) {
    if (e.peer != 1) return;
    if (e.type == ServerEvent::Type::kBaselineBegun) {
      overlap = overlap || running;
      running = true;
    }
    if (e.type == ServerEvent::Type::kBaselineEnded) running = false;
  };
  auto mismatch = [&](uint64_t lsn) {
    std::vector<uint8_t> frame;
    AppendMessage(msg::Mismatch{lsn, 1}, frame);
    r.session.ClientEnd(1).Send(netplay::Lane::kState, frame);
  };
  mismatch(r.S().State().Head());
  CHECK(r.session.RunUntil([&] { return r.S().Stats(1).baseline != 0; }, 1s));
  const uint64_t at = r.S().State().Head();
  mismatch(at - 1);
  mismatch(at);
  CHECK(Quiet(r, {1}, 60s));
  CHECK(Begun(r, 1) == begun + 1);
  // After `at`: a second one, once the first ends.
  mismatch(r.S().State().Head());
  CHECK(r.session.RunUntil([&] { return r.S().Stats(1).baseline != 0; }, 1s));
  r.Put(Prop(1), Val(5000, 8));
  mismatch(r.S().State().Head());
  CHECK(Quiet(r, {1}, 60s));
  CHECK(Begun(r, 1) == begun + 3);
  CHECK(!overlap);
}

BACKEND_TEST("BL6", BaselineEpochChange) {
  // A generation change mid-baseline drops it (S at the epoch, the client at EpochBegin); the
  // level baseline follows the next level ready.
  FakeSessionOptions o;
  o.limits.baseline_bytes_per_second = 2000;
  Rig r(o);
  Level(r, 200);
  CHECK(JoinThenLevel(r, 1));
  CHECK(r.session.RunUntil([&] { return (r.C(1).State().Staging() & kScopeLevel) != 0; }, 5s));
  r.session.NewGeneration();
  Entities(r, 50, 2);
  for (uint32_t n = 1; n <= 50; ++n) r.Put(Prop(n, 2), Val(n, 8));
  r.session.Run(500ms);
  CHECK(r.S().Stats(1).baseline == 0);
  CHECK(r.C(1).State().Staging() == 0);
  CHECK((r.C(1).State().Subscribed() & kScopeLevel) == 0);
  CHECK(r.C(1).State().Epoch() == 2);
  r.session.LevelReady(1);
  CHECK(Quiet(r, {1}, 60s));
  CHECK(r.C(1).State().Rows() == 50);
  CHECK(Repairs(r, 1) == 0);
  CHECK(Begun(r, 1, BaselineReason::kLevelReady) == 2);  // the dropped one, then the new one
}

BACKEND_TEST("BL7", BaselineLevelAfterJoin) {
  // Level rows written between the join and level ready: no level decision completes meanwhile
  // (the level scope is not subscribed); the level baseline carries them; equal, no repair.
  Rig r;
  Entities(r, 40);
  r.Join(1, /*ready=*/false);
  FlowRng rng{7};
  bool level_completed = false;
  uint64_t session_at = 0;
  const TimePoint end = r.Now() + 3s;
  while (r.Now() < end) {
    r.Put(Prop(1 + uint32_t(rng.Below(40))), Val(uint32_t(rng.Next()), 8));
    if (rng.Below(5) == 0) {
      r.Put(Tracker(1), Val(uint32_t(rng.Next())));
      session_at = r.S().State().Head();
    }
    r.session.Step();
    const Replica& c = r.C(1).State();
    if (c.Completes(Scope::kLevel, std::min(c.Prefix(), r.S().State().Head()))) level_completed = true;
  }
  r.session.Run(500ms);
  CHECK(!level_completed);
  CHECK(r.C(1).State().Completes(Scope::kSession, session_at));
  CHECK(r.C(1).State().Rows() <= 1);  // the tracker, no level row
  r.session.LevelReady(1);
  CHECK(Quiet(r, {1}));
  CHECK(r.C(1).State().Completes(Scope::kLevel, r.S().State().Head()));
  CHECK(Repairs(r, 1) == 0);
}

BACKEND_TEST("BL8", BaselineQuietAfter) {
  // A baseline ends while S is quiet, with writes in (at, head]: the Through after BaselineEnd
  // brings the prefix to the head with no further write; the commit's at_lsn completes; the next
  // manifest is comparable and equal.
  FakeSessionOptions o;
  o.limits.baseline_bytes_per_second = 2000;
  Rig r(o);
  Level(r, 100);
  CHECK(JoinThenLevel(r, 1));
  CHECK(r.session.RunUntil([&] { return (r.C(1).State().Staging() & kScopeLevel) != 0; }, 5s));
  r.Put(Prop(3), Val(333, 8));
  r.Put(Prop(101), Val(1, 8));
  const uint64_t head = r.S().State().Head();
  CHECK(r.session.RunUntil([&] { return r.C(1).State().Staging() == 0; }, 30s));
  r.session.Run(1s);
  CHECK(r.S().State().Head() == head);
  CHECK(r.C(1).State().Prefix() == head);
  CHECK(r.C(1).State().Completes(Scope::kLevel, head));
  r.session.Run(3s);
  CHECK(r.C(1).State().ManifestOk() == head);
  CHECK(Equal(r.session, 1));
}

BACKEND_TEST("BL9", BaselineDigestUnderChurn) {
  // One row changed before its chunk goes (sent at the new revision, in the digest) and one after
  // its chunk went (a delta into staging): the digest matches; both end at their newest revision.
  FakeSessionOptions o;
  o.limits.baseline_bytes_per_second = 2000;
  Rig r(o);
  Level(r, 100);
  CHECK(JoinThenLevel(r, 1));
  CHECK(r.session.RunUntil([&] { return (r.C(1).State().Staging() & kScopeLevel) != 0; }, 5s));
  r.session.Run(1500ms);  // some chunks out (about 1,150 bytes each at 2,000 bytes a second)
  // Baseline order is dependency order, then key order: the toggles (depth 0) and props go first,
  // the scene last; the first prop's chunk went, the scene's has not.
  const RecordKey before_chunk = Scene();
  const RecordKey after_chunk = Toggle(0);
  CHECK(r.C(1).State().Staging() != 0);
  r.Put(before_chunk, Val(9001, 8));
  r.Put(after_chunk, Val(9002, 8));
  CHECK(Quiet(r, {1}, 60s));
  CHECK(CountClient(r.session, 1, ClientEvent::Type::kBaselineFailed) == 0);
  CHECK(Repairs(r, 1) == 0);
  CHECK(r.C(1).State().Get(before_chunk)->value == Val(9001, 8));
  CHECK(r.C(1).State().Get(after_chunk)->value == Val(9002, 8));
}

BACKEND_TEST("BL10", BaselineDigestKilledRow) {
  // One BaselineRows message of the level baseline killed: the end digest fails, staging is
  // dropped, the active replica and the apply queue are unchanged (the dump before and after the
  // failing pump is identical); Mismatch{at, 0} starts a new baseline; equal after.
  FakeSessionOptions o;
  o.limits.baseline_bytes_per_second = 4000;
  // The join's baseline (the session tracker, one chunk) is the first BaselineRows; the level's
  // third chunk is killed.
  o.faults = "message:kill dir=s2c peer=1 type=baseline_rows nth=4";
  Rig r(o);
  Level(r, 120);
  r.Put(Tracker(1), Val(1));
  r.Join(1, /*ready=*/false);
  CHECK(r.session.RunUntil([&] { return (r.C(1).State().Subscribed() & kScopeSession) != 0; }, 5s));
  r.session.LevelReady(1);
  bool failed = false, unchanged = false;
  for (int i = 0; i < 3000 && !failed; ++i) {
    MapDumpOptions co;
    co.who = "peer 1";
    const std::string before = DumpMap(r.C(1).State(), co);
    const size_t unsettled = r.C(1).State().Unsettled();
    r.session.Step();
    if (CountClient(r.session, 1, ClientEvent::Type::kBaselineFailed) == 1) {
      failed = true;
      unchanged = DumpMap(r.C(1).State(), co) == before &&
                  r.C(1).State().Unsettled() == unsettled && r.C(1).State().Staging() == 0;
    }
  }
  CHECK(r.session.Faults()->Fired(0) == 1);
  CHECK(failed);
  CHECK(unchanged);
  CHECK(Quiet(r, {1}, 60s));
  CHECK(Begun(r, 1, BaselineReason::kDigestMismatch) == 1);
}

// ---- Proven prefix on the façades ---------------------------------------------------------------

BACKEND_TEST("PP1", ProvenPrefixLossOnlyAtThrough) {
  // Deltas only, 30 % datagram loss: the prefix moves only at Through, and an Ack never says more
  // than the last Through the client received.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Rig r(Lossy(uint64_t(seed), 50, 30));
    Level(r, 60);
    CHECK(Ready(r, 1));
    uint64_t last_through = r.C(1).State().Prefix();
    bool prefix_ok = true, acks_ok = true;
    r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
      if (peer != 1) return;
      if (!to_server) {
        if (auto* t = std::get_if<msg::Through>(&m)) last_through = std::max(last_through, t->lsn);
        if (auto* e = std::get_if<msg::BaselineEnd>(&m)) last_through = std::max(last_through, e->at);
      }
    };
    r.wire.on_client_sent = [&](uint16_t peer, const Message& m) {
      if (auto* a = std::get_if<msg::Ack>(&m); peer == 1 && a && a->prefix > last_through) {
        acks_ok = false;
      }
    };
    Churn churn{r, FlowRng{uint64_t(seed) * 3}};
    ChurnFor(r, churn, 10s, 2, [&] {
      if (r.C(1).State().Prefix() > last_through) prefix_ok = false;
    });
    CHECK(prefix_ok);
    CHECK(acks_ok);
    CHECK(Quiet(r, {1}, 40s));
  }
}

BACKEND_TEST("PP2", ProvenPrefixKilledDelta) {
  // A delta killed: the prefix still moves (S cannot see the kill), the next manifest mismatches,
  // one repair baseline, one mismatch, equal after quiet.
  const RecordKey k = Prop(5);
  Rig r(WithFaults("message:kill dir=s2c peer=1 type=delta key=" + KeyText(k)));
  Level(r, 60);
  CHECK(Ready(r, 1));
  CHECK(Quiet(r, {1}));
  r.Put(k, Val(4242, 8));
  const uint64_t lsn = r.S().State().Head();
  r.Put(Prop(6), Val(4243, 8));
  CHECK(r.session.RunUntil([&] { return r.C(1).State().Prefix() >= lsn; }, 1s));
  CHECK(r.C(1).State().Get(k)->value != Val(4242, 8));
  CHECK(Quiet(r, {1}));
  CHECK(r.S().Stats(1).mismatches == 1);
  CHECK(Repairs(r, 1) == 1);
  CHECK(r.C(1).State().Get(k)->value == Val(4242, 8));  // the repair baseline carries it
}

BACKEND_TEST("PP4", ProvenPrefixManifestOnlyAtHead) {
  // A burst the walk needs seconds for (two state frames in flight at most, 60 ms each way): no
  // manifest while the cursor is behind, though one falls due; one at the first pump with the
  // cursor at the head.
  FakeSessionOptions o;
  o.limits.max_unacked_state_frames = 2;
  o.memory.one_way = 60ms;
  Rig r(o);
  Level(r, 20);
  CHECK(Ready(r, 1, 30s));
  CHECK(Quiet(r, {1}, 30s));
  for (uint32_t i = 0; i < 600; ++i) {
    r.Everywhere(Ent(1, 1000 + i), kStart);
    r.Put(Prop(1000 + i), Val(i, 64));
  }
  const uint64_t manifests = r.S().Stats(1).manifests;
  bool behind_manifest = false;
  int steps = 0;
  while (r.S().Stats(1).cursor < r.S().State().Head() && steps++ < 5000) {
    const uint64_t before = r.S().Stats(1).manifests;
    r.session.Step();
    if (r.S().Stats(1).manifests > before && r.S().Stats(1).cursor != r.S().State().Head()) {
      behind_manifest = true;
    }
  }


  CHECK(steps > 200);  // more than one manifest interval behind
  CHECK(!behind_manifest);
  CHECK(r.S().Stats(1).manifests == manifests + 1);  // at the pump the cursor reached the head
}

BACKEND_TEST("PP6", ProvenPrefixHeldFrame) {
  // PP6 (b): key K changed at LSN x and again after keys first changed at x+1..x+3; K's frame is
  // held (the link answers kFull for 1 s, Sol F1). No Through goes while it is held, so the
  // client's prefix stays below x and Completes(level, x) is false; it turns true only once K's
  // frame and its Through went out, with K at its newest revision.
  const RecordKey k = Prop(1);
  Rig r(WithFaults("link:full peer=1 dir=s2c lane=state from=3000 ms=1000"));
  Level(r, 10);
  CHECK(Ready(r, 1));
  r.session.Run(kStart + 3000ms - r.Now());
  r.Put(k, Val(501, 8));
  const uint64_t k_first = r.S().State().Head();
  r.Put(Prop(6), Val(1, 8));
  r.Put(Prop(7), Val(1, 8));
  r.Put(Prop(8), Val(1, 8));
  r.Put(k, Val(502, 8));
  const uint32_t k_revision = r.S().State().Get(k)->revision;
  bool through_while_held = false;
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer == 1 && !to_server && std::holds_alternative<msg::Through>(m) &&
        r.Now() < kStart + 3950ms) {
      through_while_held = through_while_held || std::get<msg::Through>(m).lsn >= k_first;
    }
  };
  bool early = false;
  CHECK(r.session.RunUntil(
      [&] {
        const Replica& c = r.C(1).State();
        if (r.Now() < kStart + 3950ms && c.Prefix() >= k_first) early = true;
        if (!c.Completes(Scope::kLevel, k_first)) return false;
        const Record* held = c.Get(k);
        early = early || !held || held->revision != k_revision || r.Now() < kStart + 4000ms;
        return true;
      },
      3s));
  CHECK(r.session.Faults()->Fired(0) == 1);
  CHECK(!through_while_held);
  CHECK(!early);
  CHECK(Equal(r.session, 1));
}

BACKEND_TEST("PP7", ProvenPrefixManifestsUnderChurn) {
  // 400 writes a second for 20 s, one unthrottled peer, one delta killed at 5 s: a manifest
  // reaches the peer at least every two intervals throughout; the mismatch is found within two
  // intervals of the kill.
  const RecordKey k = Prop(900);
  Rig r(WithFaults("message:kill dir=s2c peer=1 type=delta key=" + KeyText(k) + " nth=1"));
  Level(r, 200);
  CHECK(Ready(r, 1));
  CHECK(Quiet(r, {1}));
  std::vector<TimePoint> manifests;
  std::optional<TimePoint> mismatch_at;
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer != 1) return;
    if (!to_server && std::holds_alternative<msg::Manifest>(m)) manifests.push_back(r.Now());
    if (to_server && std::holds_alternative<msg::Mismatch>(m) && !mismatch_at) mismatch_at = r.Now();
  };
  FlowRng rng{77};
  uint32_t value = 1;
  const TimePoint start = r.Now();
  while (r.Now() < start + 20s) {
    for (int i = 0; i < 4; ++i) r.Put(Prop(1 + uint32_t(rng.Below(200))), Val(value++, 8));
    if (r.Now() >= start + 5s && !r.S().State().Get(k)) r.Put(k, Val(1, 8));
    r.session.Step();
  }
  REQUIRE_OR_RETURN_VOID(!r.session.FaultEvents().empty());
  const TimePoint killed_at = r.session.FaultEvents().front().at;
  const Duration interval = r.session.Lim().manifest_interval;
  CHECK(mismatch_at && *mismatch_at - killed_at <= 2 * interval + 200ms);
  bool gaps_ok = true;
  TimePoint last = start;
  for (const TimePoint t : manifests) {
    // A repair baseline's run aside (no manifest while it runs).
    if (mismatch_at && t > *mismatch_at && t < *mismatch_at + 3s) {
      last = t;
      continue;
    }
    if (t - last > 2 * interval + 200ms) gaps_ok = false;
    last = t;
  }
  CHECK(gaps_ok);
  CHECK(Quiet(r, {1}));
}

BACKEND_TEST("PP8", ProvenPrefixKilledCoalescedKey) {
  // K killed after the lane delivered it (every delta of K: its second change lies after the
  // first batch's Through, so the next walk sends it again and one kill alone would be healed):
  // the prefix passes K's first change and Completes(level, it) turns true without K ("completed
  // on a killed row", expected here only); the manifest finds it within two intervals; a repair
  // baseline; equal after quiet.
  const RecordKey k = Prop(1);
  Rig r(WithFaults("message:kill dir=s2c peer=1 type=delta key=" + KeyText(k) + " from=2500"));
  Level(r, 10);
  CHECK(Ready(r, 1));
  r.session.Run(kStart + 3000ms - r.Now());
  const Record before = *r.S().State().Get(k);
  r.Put(k, Val(501, 8));
  const uint64_t k_first = r.S().State().Head();
  r.Put(Prop(6), Val(1, 8));
  r.Put(Prop(7), Val(1, 8));
  r.Put(Prop(8), Val(1, 8));
  r.Put(k, Val(502, 8));
  bool completed_without_k = false;
  std::optional<TimePoint> mismatch_at;
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer == 1 && to_server && std::holds_alternative<msg::Mismatch>(m) && !mismatch_at) {
      mismatch_at = r.Now();
    }
  };
  const TimePoint written = r.Now();
  r.session.RunUntil(
      [&] {
        const Replica& c = r.C(1).State();
        const Record* held = c.Get(k);
        if (c.Completes(Scope::kLevel, k_first) && held && held->revision == before.revision) {
          completed_without_k = true;
        }
        return mismatch_at.has_value();
      },
      6s);
  CHECK(r.session.Faults()->Fired(0) >= 1);
  CHECK(completed_without_k);
  CHECK(mismatch_at && *mismatch_at - written <= 2 * r.session.Lim().manifest_interval + 200ms);
  CHECK(Repairs(r, 1) == 1);
  // The repair baseline carries K (baseline rows are not deltas: the rule kills deltas only).
  CHECK(Quiet(r, {1}));
}

// ---- Scene release on the façades ---------------------------------------------------------------

BACKEND_TEST("SR1", SceneReleaseAfterToggles) {
  // Toggles written, then the scene released, the state lane slowed and cut one frame at a time:
  // the client applies the release only after every toggle before it (one log, one order).
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    FakeSessionOptions o = WithFaults("message:delay lane=state dir=s2c peer=1 delay=100-300",
                                      uint64_t(seed));
    o.limits.max_unacked_state_frames = 1;
    Rig r(o);
    Level(r, 10);
    CHECK(Ready(r, 1));
    CHECK(Quiet(r, {1}));
    const size_t log_start = r.W(1).ApplyLog().size();
    for (uint32_t i = 0; i < 8; ++i) r.Put(Toggle(i), Val(uint32_t(seed) * 100 + i));
    r.Put(Scene(), Val(2));  // released
    CHECK(Quiet(r, {1}));

    const auto& log = r.W(1).ApplyLog();
    size_t toggles = 0;
    bool scene_after_all = false;
    for (size_t i = log_start; i < log.size(); ++i) {
      if (log[i] == test::kToggle) ++toggles;
      if (log[i] == test::kScene) scene_after_all = toggles == 8;
    }
    CHECK(toggles == 8);
    CHECK(scene_after_all);
    CHECK(CountClient(r.session, 1, ClientEvent::Type::kFenceBroken) == 0);
  }
}

// test.scene's words for a refused skip: the generic table refuses a skip on an older revision
// `stale basis` with the current revision; the scene kind reads that as `scene ended` (the scene
// was ended by another peer's skip).
static std::string SceneRefusal(const Decision& d) {
  return d.reason == Reason::kStaleBasis ? "scene ended" : std::string(ReasonName(d.reason));
}

BACKEND_TEST("SR3", SceneTwoSkipsOnePump) {
  // Two peers skip in one pump (a commit on the scene row on the same basis revision): the first
  // in S's receive order ends the scene; the other is refused, which test.scene maps to `scene
  // ended`; both see the same end revision.
  Rig r;
  Level(r, 10);
  CHECK(Ready(r, 1));
  CHECK(Ready(r, 2));
  CHECK(Quiet(r, {1, 2}));
  const uint32_t basis = r.S().State().Get(Scene())->revision;
  CHECK(r.C(1).State().Get(Scene())->revision == basis);
  CHECK(r.C(2).State().Get(Scene())->revision == basis);
  const auto a = r.C(1).Send(CommitOn(test::kScene, Ent(1, kSceneN), basis, Val(9)), r.Now());
  const auto b = r.C(2).Send(CommitOn(test::kScene, Ent(1, kSceneN), basis, Val(9)), r.Now());
  REQUIRE_OR_RETURN_VOID(a && b);
  CHECK(r.session.RunUntil(
      [&] { return FinalFor(r.session, 1, *a).has_value() && FinalFor(r.session, 2, *b).has_value(); },
      2s));
  const auto da = FinalFor(r.session, 1, *a);
  const auto db = FinalFor(r.session, 2, *b);
  REQUIRE_OR_RETURN_VOID(da && db);
  // S reads peers in id order: peer 1's arrives first.
  CHECK(da->outcome == Outcome::kApplied);
  CHECK(db->outcome == Outcome::kRefused && SceneRefusal(*db) == "scene ended");
  CHECK(Quiet(r, {1, 2}));
  const uint32_t end_revision = r.S().State().Get(Scene())->revision;
  CHECK(end_revision == basis + 1);
  CHECK(db->revision == end_revision);  // the refusal names the end revision
  CHECK(r.C(1).State().Get(Scene())->revision == end_revision);
  CHECK(r.C(2).State().Get(Scene())->revision == end_revision);
}

BACKEND_TEST("SR4", SceneJoinDuringOpenScene) {
  // A peer joins while the scene is open: nothing waits for it; its baseline has the scene row.
  Rig r;
  Level(r, 10);
  CHECK(Ready(r, 1));
  r.Put(Scene(), Val(7));  // open
  CHECK(Quiet(r, {1}));
  const uint64_t p1 = r.C(1).State().Prefix();
  r.Join(2);
  r.Put(Toggle(0), Val(9));
  r.session.Run(200ms);
  CHECK(r.C(1).State().Prefix() > p1);
  CHECK(Quiet(r, {1, 2}));
  const Record* scene = r.C(2).State().Get(Scene());
  CHECK(scene && scene->value == Val(7));
  const auto& log = r.W(2).ApplyLog();
  CHECK(std::find(log.begin(), log.end(), test::kScene) != log.end());
}

namespace {

// The positions of `kind`'s applies in the apply log from `from`.
std::vector<size_t> AppliesOf(const std::vector<KindId>& log, KindId kind, size_t from) {
  std::vector<size_t> out;
  for (size_t i = from; i < log.size(); ++i) {
    if (log[i] == kind) out.push_back(i);
  }
  return out;
}

// A session cut to one message per batch (one state frame in flight, the walk takes one item
// while the FIFO has room), peer 1 level-ready and quiet: the setting of the SR6 family.
struct SceneRig {
  explicit SceneRig(std::string faults = {}) : r(Options(std::move(faults))) {
    Level(r, 10);
    ok = Ready(r, 1) && Quiet(r, {1});
    from = r.W(1).ApplyLog().size();
  }
  static FakeSessionOptions Options(std::string faults) {
    FakeSessionOptions o;
    o.faults = std::move(faults);
    o.limits.max_unacked_state_frames = 1;
    o.memory.one_way = 20ms;
    return o;
  }
  const std::vector<KindId>& Log() { return r.W(1).ApplyLog(); }
  Rig r;
  bool ok = false;
  size_t from = 0;
};

}  // namespace

BACKEND_TEST("SR2", SceneBaselineDependencyOrder) {
  // A baseline taken while the scene is open (peer 2 joins): its scene row applies after the
  // toggle rows (dependency order).
  Rig r;
  Level(r, 10);
  CHECK(Ready(r, 1));
  r.Put(Scene(), Val(7));  // open
  CHECK(Quiet(r, {1}));
  CHECK(Ready(r, 2));
  CHECK(Quiet(r, {1, 2}));
  const auto& log = r.W(2).ApplyLog();
  const auto scenes = AppliesOf(log, test::kScene, 0);
  const auto toggles = AppliesOf(log, test::kToggle, 0);
  REQUIRE_OR_RETURN_VOID(scenes.size() == 1 && toggles.size() == 8);
  CHECK(toggles.back() < scenes.front());
}

BACKEND_TEST("SR5", SceneDroppedAtEpoch) {
  // A generation change while the scene is open: the client drops the row at EpochBegin.
  Rig r;
  Level(r, 10);
  CHECK(Ready(r, 1));
  r.Put(Scene(), Val(7));
  CHECK(Quiet(r, {1}));
  CHECK(r.C(1).State().Get(Scene()) != nullptr);
  r.session.NewGeneration();
  CHECK(r.session.RunUntil([&] { return r.C(1).State().Epoch() == 2; }, 1s));
  CHECK(r.C(1).State().Get(Scene()) == nullptr);
  CHECK(CountClient(r.session, 1, ClientEvent::Type::kRowLate) == 0);
  CHECK(CountClient(r.session, 1, ClientEvent::Type::kFenceBroken) == 0);
}

BACKEND_TEST("SR6", SceneFenceAcrossBatchCut) {
  // The scene row first changes, then a toggle, then the scene is released; one message per batch,
  // so the scene (at its newest revision) arrives before the toggle. It is held (fenced) until the
  // prefix covers its change and the toggle settled: apply log toggle, then release.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    SceneRig s;
    CHECK(s.ok);
    s.r.Put(Scene(), Val(2));
    s.r.Put(Toggle(0), Val(uint32_t(seed) + 10));
    s.r.Put(Scene(), Val(3));  // released
    CHECK(Quiet(s.r, {1}));
    const auto scenes = AppliesOf(s.Log(), test::kScene, s.from);
    const auto toggles = AppliesOf(s.Log(), test::kToggle, s.from);
    REQUIRE_OR_RETURN_VOID(scenes.size() == 1 && toggles.size() == 1);
    CHECK(toggles.front() < scenes.front());
    CHECK(CountClient(s.r.session, 1, ClientEvent::Type::kFenceBroken) == 0);
  }
}

BACKEND_TEST("SR6b", SceneFencePendingToggle) {
  // SR6 with the toggle's entity spawning after its row and after the Through that covers the
  // release (2 s here, inside its 5 s grace; the design's 300 ms is shorter than this cut session
  // needs to deliver the three rows): the toggle waits, the scene stays held behind it, and the
  // release follows the toggle's apply; no fence broken.
  SceneRig s;
  CHECK(s.ok);
  const EntityId late = Ent(1, 130);
  s.r.W(1).Exists(late, s.r.Now() + 2s);
  const RecordKey toggle = Key(test::kToggle, late);
  s.r.Put(Scene(), Val(2));
  s.r.Put(toggle, Val(1));
  s.r.Put(Scene(), Val(3));
  CHECK(s.r.session.RunUntil([&] { return s.r.C(1).State().Prefix() == s.r.S().State().Head(); },
                             1900ms));
  CHECK(AppliesOf(s.Log(), test::kScene, s.from).empty());  // held: the toggle is unsettled
  CHECK(Quiet(s.r, {1}));
  const auto scenes = AppliesOf(s.Log(), test::kScene, s.from);
  const auto toggles = AppliesOf(s.Log(), test::kToggle, s.from);
  REQUIRE_OR_RETURN_VOID(scenes.size() == 1 && toggles.size() == 1);
  CHECK(toggles.front() < scenes.front());
  CHECK(CountClient(s.r.session, 1, ClientEvent::Type::kFenceBroken) == 0);
}

BACKEND_TEST("SR6d", SceneFenceToggleApplyFailsOnce) {
  // SR6 with the toggle's first Apply failing (apply:fail nth=1): handed out again at the next
  // pump, it applies; the release follows; no fence broken.
  SceneRig s("apply:fail peer=1 kind=test.toggle nth=1");
  CHECK(s.ok);
  s.r.Put(Scene(), Val(2));
  s.r.Put(Toggle(0), Val(42));
  s.r.Put(Scene(), Val(3));
  CHECK(Quiet(s.r, {1}));
  CHECK(s.r.session.Faults()->Fired(0) == 1);
  const auto scenes = AppliesOf(s.Log(), test::kScene, s.from);
  const auto toggles = AppliesOf(s.Log(), test::kToggle, s.from);
  REQUIRE_OR_RETURN_VOID(scenes.size() == 1 && toggles.size() == 1);
  CHECK(toggles.front() < scenes.front());
  CHECK(CountClient(s.r.session, 1, ClientEvent::Type::kFenceBroken) == 0);
}

BACKEND_TEST("SR6e", SceneFenceUnrelatedWaitingRow) {
  // SR6b with the waiting row on test.prop (unfenced, not a scene dependency): the toggle applies
  // at once and the release is not held by the waiting prop.
  SceneRig s;
  CHECK(s.ok);
  const EntityId late = Ent(1, 131);
  s.r.W(1).Exists(late, s.r.Now() + 1500ms);
  s.r.Put(Scene(), Val(2));
  s.r.Put(Key(test::kProp, late), Val(1, 8));
  s.r.Put(Toggle(0), Val(43));
  s.r.Put(Scene(), Val(3));
  CHECK(s.r.session.RunUntil(
      [&] { return !AppliesOf(s.Log(), test::kScene, s.from).empty(); }, 1400ms));
  CHECK(AppliesOf(s.Log(), test::kProp, s.from).empty());  // the prop still waits
  CHECK(Quiet(s.r, {1}));
  CHECK(AppliesOf(s.Log(), test::kProp, s.from).size() == 1);
}

BACKEND_TEST("SR7", SceneFencedChain) {
  // test.scene on test.gate on test.toggle: the gate first changes, the scene is released, the gate
  // changes again; apply order toggle, gate, scene.
  SceneRig s;
  CHECK(s.ok);
  const RecordKey gate = Key(test::kGate, Ent(1, kToggles));
  s.r.Put(Toggle(0), Val(44));
  s.r.Put(gate, Val(1));
  s.r.Put(Scene(), Val(3));
  s.r.Put(gate, Val(2));
  CHECK(Quiet(s.r, {1}));
  const auto scenes = AppliesOf(s.Log(), test::kScene, s.from);
  const auto gates = AppliesOf(s.Log(), test::kGate, s.from);
  const auto toggles = AppliesOf(s.Log(), test::kToggle, s.from);
  REQUIRE_OR_RETURN_VOID(scenes.size() == 1 && !gates.empty() && toggles.size() == 1);
  CHECK(toggles.front() < gates.front());
  CHECK(gates.back() < scenes.front());
  CHECK(CountClient(s.r.session, 1, ClientEvent::Type::kFenceBroken) == 0);
}

BACKEND_TEST("SR6c", SceneFenceTogglePastGraceCompare) {
  // SR6c on the façades: the toggle's entity never comes on the client. The scene's release stays
  // held behind it until the toggle's grace: then `row late` (the toggle) and `fence broken`
  // (scene, toggle), and the release applies. The compare query reports the toggle `pending`
  // inside its grace, then `stuck` past it.
  Rig r;
  Level(r, 10);
  const EntityId missing = Ent(1, 950);  // in no world
  CHECK(Ready(r, 1));
  CHECK(Quiet(r, {1}));
  const size_t from = r.W(1).ApplyLog().size();
  const RecordKey toggle = Key(test::kToggle, missing);
  r.Put(Scene(), Val(2));
  r.Put(toggle, Val(1));
  r.Put(Scene(), Val(3));
  const TimePoint written = r.Now();
  CHECK(r.session.RunUntil([&] { return r.C(1).State().Get(toggle) != nullptr; }, 1s));
  CHECK(r.session.RunUntil(
      [&] { return !AppliesOf(r.W(1).ApplyLog(), test::kScene, from).empty(); }, 7s));
  CHECK(r.Now() - written >= 4900ms);  // held until the toggle's 5 s grace
  CHECK(CountClient(r.session, 1, ClientEvent::Type::kRowLate) == 1);
  bool named = false;
  for (const ClientEvent& e : r.session.ClientEvents(1)) {
    named = named || (e.type == ClientEvent::Type::kFenceBroken && e.row == Scene() &&
                      e.dependency == toggle);
  }
  CHECK(named);
  auto result = [&]() -> std::optional<LocalCompare::Live> {
    for (const LocalCompare& c :
         CompareLocal(r.C(1).State(), r.W(1).Adapters(), test::kToggle, false, r.Now())) {
      if (c.key == toggle) return c.live;
    }
    return std::nullopt;
  };
  // The compare's pending time counts from the epoch's start on the client (the entity was never
  // reported): past the grace by now.
  CHECK(result() == LocalCompare::Live::kStuck);
}

BACKEND_TEST("SR6c", SceneFenceTogglePendingThenStuck) {
  // The compare step alone, from a fresh epoch: `pending` inside the grace, `stuck` past it.
  Rig r;
  Level(r, 10);
  CHECK(Ready(r, 1));
  const RecordKey toggle = Key(test::kToggle, Ent(1, 951));  // in no world
  r.Put(toggle, Val(1));
  CHECK(r.session.RunUntil([&] { return r.C(1).State().Get(toggle) != nullptr; }, 1s));
  auto result = [&]() -> std::optional<LocalCompare::Live> {
    for (const LocalCompare& c :
         CompareLocal(r.C(1).State(), r.W(1).Adapters(), test::kToggle, false, r.Now())) {
      if (c.key == toggle) return c.live;
    }
    return std::nullopt;
  };
  r.session.Run(2s);
  CHECK(result() == LocalCompare::Live::kPending);
  r.session.Run(4s);  // past the 5 s grace since the epoch began
  CHECK(result() == LocalCompare::Live::kStuck);
}

// ---- Limits on the façades ---------------------------------------------------------------------

BACKEND_TEST("LM11", LimitServerOnlyEveryPump) {
  // A server-only row written every pump for 30 s (Opus F5): never in a delta, a baseline or a
  // manifest; no mismatch, no repair.
  Rig r;
  Level(r, 10);
  CHECK(Ready(r, 1));
  bool on_wire = false;
  r.wire.on_message = [&](uint16_t, bool to_server, const Message& m) {
    if (to_server) return;
    if (auto* d = std::get_if<msg::Delta>(&m); d && d->record.key.kind == test::kServerOnly) {
      on_wire = true;
    }
    if (auto* b = std::get_if<msg::BaselineRows>(&m)) {
      for (const Record& rec : b->records) on_wire = on_wire || rec.key.kind == test::kServerOnly;
    }
  };
  uint32_t v = 0;
  const TimePoint end = r.Now() + 30s;
  bool rejoined = false;
  while (r.Now() < end) {
    r.S().State().Put(ServerOnly(1), Val(++v, 8), r.Now());
    if (!rejoined && r.Now() >= end - 10s) {  // a baseline with the row present
      rejoined = true;
      r.Join(2);
    }
    r.session.Step();
  }
  CHECK(!on_wire);
  CHECK(r.C(1).State().Get(ServerOnly(1)) == nullptr);
  CHECK(r.S().Stats(1).mismatches == 0);
  CHECK(Repairs(r, 1) == 0);
  CHECK(r.S().Stats(1).manifests >= 10);
  CHECK(Quiet(r, {1, 2}));
}

}  // namespace skylanders::backend
