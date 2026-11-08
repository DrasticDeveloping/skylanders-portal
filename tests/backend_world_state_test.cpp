// World state and the Replica on direct calls (design sections 3.5 and 5; work item 2): the proven
// prefix under a batch cut, the fence with its apply feedback, the minimum first_lsn, manifests,
// the noisy hold. No lanes: the walk's batches are handed to the replica as its receive would.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <map>
#include <set>
#include <vector>

#include "backend/world_state.h"
#include "backend_test.h"
#include "backend_world_state_harness.h"

using namespace skylanders::backend;
using namespace skylanders::backend::test::ws;
using namespace std::chrono_literals;

namespace {

// SR6's log (section 5): the scene row first changes at 5, a toggle at 6, the scene is released
// at 9; the client subscribed at 4.
struct Sr6Setup {
  explicit Sr6Setup(std::span<const KindSpec> table = TableA()) : s(table, {}), c(table) {
    s.BeginEpoch(1);                          // 1
    s.Put(LevelKey(kProp, 10), V(1), kT0);    // 2
    s.Put(LevelKey(kProp, 11), V(1), kT0);    // 3
    s.Put(LevelKey(kProp, 12), V(1), kT0);    // 4
    Subscribe(c.replica, 4, 1);
    sub.scopes = kScopeAll;
    sub.since = {4, 4};
    s.Put(scene, V(1), kT0);                  // 5: open
    s.Put(toggle, V(1), kT0);                 // 6
    s.Put(LevelKey(kProp, 13), V(1), kT0);    // 7
    s.Put(LevelKey(kProp, 14), V(1), kT0);    // 8
    s.Put(scene, V(2), kT0);                  // 9: released
  }
  WorldState s;
  DirectClient c;
  Subscription sub;
  uint64_t cursor = 4;
  RecordKey scene = LevelKey(kScene, 1);
  RecordKey toggle = LevelKey(kToggle, 2);

  // One batch of `cap` walk items to the client, at `now`; the client pumps after it when asked.
  Batch Step(size_t cap, TimePoint now, bool pump, bool through = true) {
    Batch b = WalkBatch(s, cursor, sub, cap);
    Deliver(c, b, through);
    cursor = b.proven;
    if (pump) c.Pump(now);
    return b;
  }
};

// Runs SR6's walk with one message per batch; `seed` decides where the client pumps.
void RunSr6(Sr6Setup& t, uint64_t seed, TimePoint& now) {
  Rng rng{seed};
  bool scene_held_first = false;
  bool released_early = false;
  while (t.cursor < t.s.Head()) {
    const uint64_t before = t.cursor;
    Batch b = t.Step(1, now, rng.Chance(0.5));
    if (before == 4) {
      // The first batch is the scene alone, at revision 2 (LSN 9), first change 5.
      scene_held_first = b.items.size() == 1 && b.items[0].record &&
                         b.items[0].record->key == t.scene && b.items[0].lsn == 9 &&
                         b.items[0].first_lsn == 5 && b.proven == 5;
    }
    if (b.proven < 9 && t.c.AppliedAt(t.scene) >= 0) released_early = true;
    now += 20ms;
  }
  t.c.Pump(now);
  CHECK(scene_held_first);
  CHECK(!released_early);
}

// The apply log reads toggle, then the release, and nothing else of the scene.
bool ToggleThenRelease(const DirectClient& c, const RecordKey& toggle, const RecordKey& scene) {
  const int t = c.AppliedAt(toggle);
  const int s = c.AppliedAt(scene, 2);
  return t >= 0 && s > t && c.AppliedAt(scene, 1) < 0;
}

}  // namespace

// ---- Proven prefix ------------------------------------------------------------------------------

BACKEND_TEST("PP3", ProvenPrefixKilledThrough) {
  Sr6Setup t;
  TimePoint now = kT0;
  // Every batch delivered, but the Through of the batch that proves 9 is killed.
  while (t.cursor < t.s.Head()) {
    Batch b = WalkBatch(t.s, t.cursor, t.sub, 1);
    const bool kill = b.proven == 9;
    Deliver(t.c, b, !kill);
    t.cursor = b.proven;
    t.c.Pump(now);
    now += 20ms;
  }
  CHECK(t.c.replica.Prefix() == 8);  // waits for the next Through
  CHECK(t.c.AppliedAt(t.scene) < 0);  // the release is still held
  CHECK(!t.c.replica.Completes(Scope::kLevel, 9));
  t.s.Put(LevelKey(kProp, 15), V(1), now);  // 10
  t.Step(16, now, true);
  CHECK(t.c.replica.Prefix() == 10);
  CHECK(ToggleThenRelease(t.c, t.toggle, t.scene));
  CHECK(t.c.replica.Completes(Scope::kLevel, 10));
}

BACKEND_TEST("PP5", ProvenPrefixManifestNotComparable) {
  WorldState s(TableA(), {});
  Replica r(TableA(), {});
  s.BeginEpoch(1);
  s.Put(LevelKey(kProp, 1), V(7), kT0);
  s.Put(SessionKey(kTracker, 1), V(3), kT0);
  CHECK(Subscribe(r, 0, 1));
  Subscription sub{kScopeAll, {0, 0}};
  uint64_t cursor = 0;
  s.Walk(cursor, sub, [&](const WalkItem& it) {
    if (it.type == EntryType::kRecord) r.OnRecord(*it.record, it.first_lsn);
    else r.OnEpoch(it.generation, it.lsn);
    return true;
  }, &cursor);
  r.OnThrough(cursor);
  CHECK(r.Prefix() == 3);
  // A manifest whose LSN is not the prefix: not comparable, no mask, ManifestOk unchanged.
  s.Put(LevelKey(kProp, 2), V(1), kT0);
  uint64_t mask = 99;
  CHECK(r.CheckManifest(s.MakeManifest(kScopeAll), &mask) == Replica::Check::kNotComparable);
  CHECK(mask == 0);
  CHECK(r.ManifestOk() == 0);
  // At the prefix: equal, and ManifestOk moves (the manifest of the state at 3, from a second world
  // with the same writes).
  WorldState s3(TableA(), {});
  s3.BeginEpoch(1);
  s3.Put(LevelKey(kProp, 1), V(7), kT0);
  s3.Put(SessionKey(kTracker, 1), V(3), kT0);
  CHECK(r.CheckManifest(s3.MakeManifest(kScopeAll), &mask) == Replica::Check::kEqual);
  CHECK(r.ManifestOk() == 3);
  // A differing row at the prefix: a mismatch naming its bucket.
  WorldState s3b(TableA(), {});
  s3b.BeginEpoch(1);
  s3b.Put(LevelKey(kProp, 1), V(8), kT0);
  s3b.Put(SessionKey(kTracker, 1), V(3), kT0);
  CHECK(r.CheckManifest(s3b.MakeManifest(kScopeAll), &mask) == Replica::Check::kMismatch);
  CHECK(mask != 0 && (mask & (mask - 1)) == 0);  // one bucket
  CHECK(r.ManifestOk() == 3);
  // While a baseline stages, nothing is comparable, even at the prefix.
  r.OnBaselineBegin(2, 3, 1, kScopeLevel);
  CHECK(r.CheckManifest(s3.MakeManifest(kScopeAll), &mask) == Replica::Check::kNotComparable);
}

BACKEND_TEST("PP6", ProvenPrefixCoalescingCut) {
  // K changed at 5 and 9, other keys at 6, 7, 8; the batch capped so that it ends after the key
  // first changed at 7 (the cut; the held variant is work item 3's, on the FIFO).
  auto build = [](WorldState& s, Replica& r, std::map<uint64_t, std::pair<RecordKey, uint32_t>>& changes) {
    s.BeginEpoch(1);  // 1
    for (uint32_t i = 0; i < 3; ++i) s.Put(LevelKey(kProp, 50 + i), V(1), kT0);  // 2..4
    Subscribe(r, 4, 1);
    const RecordKey k = LevelKey(kProp, 1);
    s.Put(k, V(1), kT0);                     // 5
    s.Put(LevelKey(kProp, 6), V(1), kT0);    // 6
    s.Put(LevelKey(kProp, 7), V(1), kT0);    // 7
    s.Put(LevelKey(kProp, 8), V(1), kT0);    // 8
    s.Put(k, V(2), kT0);                     // 9
    changes[5] = {k, 1};
    changes[6] = {LevelKey(kProp, 6), 1};
    changes[7] = {LevelKey(kProp, 7), 1};
    changes[8] = {LevelKey(kProp, 8), 1};
    changes[9] = {k, 2};
  };
  const RecordKey k = LevelKey(kProp, 1);
  {
    WorldState s(TableA(), {});
    Replica r(TableA(), {});
    std::map<uint64_t, std::pair<RecordKey, uint32_t>> changes;
    build(s, r, changes);
    Subscription sub{kScopeAll, {4, 4}};
    CHECK(!r.Completes(Scope::kLevel, 5));
    Batch b = WalkBatch(s, 4, sub, 3);
    CHECK(b.items.size() == 3);
    CHECK(b.items[0].record->key == k && b.items[0].record->revision == 2 && b.items[0].first_lsn == 5);
    CHECK(b.items[1].first_lsn == 6 && b.items[2].first_lsn == 7);
    CHECK(b.proven == 7);
    for (size_t i = 0; i < b.items.size(); ++i) r.OnRecord(b.records[i], b.items[i].first_lsn);
    r.OnThrough(b.proven);
    CHECK(r.Get(k) && r.Get(k)->revision == 2);
    CHECK(r.Completes(Scope::kLevel, 5));  // a commit at at_lsn 5 completes with K there
  }
  // Every cap: each Through x satisfies "every entry up to x was sent".
  for (size_t cap = 1; cap <= 6; ++cap) {
    WorldState s(TableA(), {});
    Replica r(TableA(), {});
    std::map<uint64_t, std::pair<RecordKey, uint32_t>> changes;
    build(s, r, changes);
    Subscription sub{kScopeAll, {4, 4}};
    uint64_t cursor = 4;
    bool sound = true;
    int rounds = 0;
    while (cursor < s.Head() && rounds++ < 20) {
      Batch b = WalkBatch(s, cursor, sub, cap);
      for (size_t i = 0; i < b.items.size(); ++i) r.OnRecord(b.records[i], b.items[i].first_lsn);
      r.OnThrough(b.proven);
      for (const auto& [lsn, change] : changes) {
        if (lsn > b.proven) continue;
        const Record* got = r.Get(change.first);
        if (!got || got->revision < change.second) sound = false;
      }
      if (r.Completes(Scope::kLevel, 5) && (!r.Get(k))) sound = false;
      cursor = b.proven;
    }
    CHECK(sound);
    CHECK(cursor == s.Head());
  }
}

BACKEND_TEST("PP9", ProvenPrefixEpochEntryAndServerOnly) {
  WorldState s(TableA(), {});
  Replica r(TableA(), {});
  s.BeginEpoch(1);                                     // 1
  CHECK(Subscribe(r, 1, 1));
  Subscription sub{kScopeAll, {1, 1}};
  s.Put(LevelKey(kServerOnly, 1), V(1), kT0);          // 2
  s.Put(SessionKey(kTracker, 1), V(1), kT0);           // 3
  s.Put(LevelKey(kServerOnly, 1), V(2), kT0);          // 4
  s.BeginEpoch(2);                                     // 5
  s.Put(LevelKey(kServerOnly, 1, 2), V(3), kT0);       // 6
  s.Put(LevelKey(kServerOnly, 2, 2), V(3), kT0);       // 7
  // The batch capped at the epoch entry: no Through passes it before EpochBegin goes.
  Batch b1 = WalkBatch(s, 1, sub, 1);
  CHECK(b1.items.size() == 1 && b1.items[0].type == EntryType::kRecord);
  CHECK(b1.proven == 4);  // the server-only change at 4 is passed, the epoch entry is not
  for (size_t i = 0; i < b1.items.size(); ++i) r.OnRecord(b1.records[i], b1.items[i].first_lsn);
  r.OnThrough(b1.proven);
  Batch b2 = WalkBatch(s, b1.proven, sub, 1);
  CHECK(b2.items.size() == 1 && b2.items[0].type == EntryType::kEpoch && b2.items[0].generation == 2);
  CHECK(b2.items[0].lsn == 5);
  CHECK(b2.proven == 7);  // the server-only rows after it never stop Through
  CHECK((sub.scopes & kScopeLevel) == 0);
  r.OnEpoch(2, 5);
  r.OnThrough(b2.proven);
  // Server-only rows never reach the client, and a walk over nothing but them proves the head.
  CHECK(!r.Get(LevelKey(kServerOnly, 1)) && !r.Get(LevelKey(kServerOnly, 1, 2)));
  for (int i = 0; i < 10; ++i) s.Put(LevelKey(kServerOnly, 1, 2), V(10 + i), kT0);
  Subscription level{kScopeAll, {0, 0}};
  Batch b3 = WalkBatch(s, b2.proven, level, 1);
  CHECK(b3.items.empty());
  CHECK(b3.proven == s.Head());
  // Nor are they in a manifest or a baseline (Opus F5).
  CHECK(s.BaselineKeys(kScopeLevel).empty());
  WorldState empty(TableA(), {});
  empty.BeginEpoch(2);
  CHECK(s.MakeManifest(kScopeLevel).digest == empty.MakeManifest(kScopeLevel).digest);
  CHECK(r.Get(SessionKey(kTracker, 1)) != nullptr);
}

// ---- Scene release, on direct calls -------------------------------------------------------------

BACKEND_TEST("SR2", SceneBaselineDependencyOrder) {
  WorldState s(TableA(), {});
  s.BeginEpoch(1);
  const RecordKey scene = LevelKey(kScene, 1);
  s.Put(scene, V(1), kT0);  // open, first
  for (uint32_t i = 0; i < 4; ++i) s.Put(LevelKey(kToggle, 10 + i), V(i + 1), kT0);
  const std::vector<RecordKey> keys = s.BaselineKeys(kScopeLevel);
  CHECK(keys.size() == 5 && keys.back() == scene);  // dependency order on S
  DirectClient c(TableA());
  CHECK(Subscribe(c.replica, 0, 1, kScopeSession));
  // The rows reach the client in any order (the scene first here); the apply order is the
  // dependency order.
  c.replica.OnBaselineBegin(2, s.Head(), 1, kScopeLevel);
  uint64_t digest = kBaselineDigestSeed;
  std::vector<RecordKey> sent(keys.rbegin(), keys.rend());
  for (const RecordKey& k : sent) {
    c.replica.OnBaselineRow(2, *s.Get(k));
    digest = BaselineDigestAdd(digest, *s.Get(k));
  }
  const BaselineEndResult end = c.replica.OnBaselineEnd(2, s.Head(), digest);
  CHECK(end.ok && end.diff.added == 5);
  c.replica.OnThrough(s.Head());  // S's Through right after BaselineEnd
  c.Pump(kT0);
  bool after_all = c.AppliedAt(scene) >= 0;
  for (uint32_t i = 0; i < 4; ++i) after_all &= c.AppliedAt(LevelKey(kToggle, 10 + i)) < c.AppliedAt(scene);
  CHECK(after_all);
}

BACKEND_TEST("SR5", SceneDroppedAtEpoch) {
  Sr6Setup t;
  TimePoint now = kT0;
  RunSr6(t, 1, now);
  CHECK(t.c.replica.Get(t.scene) != nullptr);
  t.s.BeginEpoch(2);
  Batch b = WalkBatch(t.s, t.cursor, t.sub, 16);
  Deliver(t.c, b);
  t.c.Pump(now);
  CHECK(t.c.replica.Get(t.scene) == nullptr);
  CHECK(t.c.replica.Unsettled() == 0);
  CHECK((t.c.replica.Subscribed() & kScopeLevel) == 0);
  CHECK(!t.c.replica.Completes(Scope::kLevel, b.proven));
  t.c.Pump(now + 10s);
  CHECK(t.c.events.empty());
}

BACKEND_TEST("SR6", SceneFenceAcrossBatchCut) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Sr6Setup t;
    TimePoint now = kT0;
    RunSr6(t, uint64_t(seed), now);
    CHECK(ToggleThenRelease(t.c, t.toggle, t.scene));
    CHECK(t.c.events.empty());
    CHECK(t.c.replica.Unsettled() == 0);
    CHECK(t.c.replica.Completes(Scope::kLevel, 9));
  }
}

BACKEND_TEST("SR6b", SceneFencePendingToggle) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Sr6Setup t;
    TimePoint now = kT0;
    t.c.spawn_at[t.toggle] = kT0 + 20ms + 300ms;  // 300 ms after the toggle row arrives
    RunSr6(t, uint64_t(seed), now);
    CHECK(t.c.replica.Prefix() == 9);
    CHECK(t.c.AppliedAt(t.scene) < 0);  // held: the toggle (first change 6) is unsettled
    CHECK(!t.c.replica.Completes(Scope::kLevel, 9));
    bool same_pump = false;
    while (now < kT0 + 1s) {
      const size_t before = t.c.apply_log.size();
      t.c.Pump(now);
      if (t.c.AppliedAt(t.toggle) >= int(before)) same_pump = t.c.AppliedAt(t.scene, 2) >= int(before);
      now += 20ms;
    }
    CHECK(same_pump);
    CHECK(ToggleThenRelease(t.c, t.toggle, t.scene));
    CHECK(t.c.Count(ReplicaEvent::Type::kFenceBroken) == 0);
    CHECK(t.c.replica.Completes(Scope::kLevel, 9));
  }
}

BACKEND_TEST("SR6c", SceneFenceTogglePastGrace) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Sr6Setup t;
    TimePoint now = kT0;
    t.c.spawn_at[t.toggle] = TimePoint::max();
    RunSr6(t, uint64_t(seed), now);
    bool held_until_late = true, completes_with_late = false;
    while (now < kT0 + 7s) {
      const bool late_before = t.c.Count(ReplicaEvent::Type::kRowLate) > 0;
      const bool completes_before = t.c.replica.Completes(Scope::kLevel, 9);
      t.c.Pump(now);
      const bool late_after = t.c.Count(ReplicaEvent::Type::kRowLate) > 0;
      if (!late_after && (t.c.AppliedAt(t.scene) >= 0 || completes_before)) held_until_late = false;
      if (!late_before && late_after) completes_with_late = !completes_before && t.c.replica.Completes(Scope::kLevel, 9);
      now += 100ms;
    }
    CHECK(held_until_late);
    CHECK(completes_with_late);
    CHECK(t.c.Count(ReplicaEvent::Type::kRowLate) == 1);
    CHECK(t.c.Count(ReplicaEvent::Type::kFenceBroken) == 1);
    for (const ReplicaEvent& e : t.c.events) {
      if (e.type == ReplicaEvent::Type::kRowLate) CHECK(e.row == t.toggle && e.last == RowApply::kWaiting);
      if (e.type == ReplicaEvent::Type::kFenceBroken) CHECK(e.row == t.scene && e.dependency == t.toggle);
    }
    CHECK(t.c.AppliedAt(t.scene, 2) >= 0 && t.c.AppliedAt(t.toggle) < 0);
    CHECK(t.c.replica.Unsettled() == 1);  // the late toggle is still handed out if its entity comes
    t.c.spawn_at.erase(t.toggle);
    t.c.Pump(now);
    CHECK(t.c.AppliedAt(t.toggle) >= 0 && t.c.replica.Unsettled() == 0);
  }
}

BACKEND_TEST("SR6d", SceneFenceToggleApplyFailsOnce) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Sr6Setup t;
    TimePoint now = kT0;
    t.c.fail_next[t.toggle] = 1;
    RunSr6(t, uint64_t(seed), now);
    t.c.Pump(now + 20ms);
    CHECK(ToggleThenRelease(t.c, t.toggle, t.scene));
    CHECK(t.c.events.empty());
    CHECK(t.c.handed >= 3);  // the toggle twice, the scene once
  }
}

BACKEND_TEST("SR6e", SceneFenceUnrelatedWaitingRow) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Sr6Setup t;
    TimePoint now = kT0;
    t.c.spawn_at[LevelKey(kProp, 13)] = TimePoint::max();  // test.prop at 7, never there
    RunSr6(t, uint64_t(seed), now);
    CHECK(ToggleThenRelease(t.c, t.toggle, t.scene));
    CHECK(t.c.replica.Completes(Scope::kLevel, 9));
    CHECK(t.c.Count(ReplicaEvent::Type::kFenceBroken) == 0);
  }
}

BACKEND_TEST("SR7", SceneFencedChain) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Rng rng{uint64_t(seed)};
    WorldState s(TableB(), {});
    DirectClient c(TableB());
    const RecordKey scene = LevelKey(kScene, 1), gate = LevelKey(kGate, 2), toggle = LevelKey(kToggle, 3);
    s.BeginEpoch(1);                                                         // 1
    for (uint32_t i = 0; i < 3; ++i) s.Put(LevelKey(kProp, 20 + i), V(1), kT0);  // 2..4
    Subscribe(c.replica, 4, 1);
    s.Put(scene, V(1), kT0);                   // 5
    s.Put(gate, V(1), kT0);                    // 6
    s.Put(toggle, V(1), kT0);                  // 7
    s.Put(LevelKey(kProp, 30), V(1), kT0);     // 8
    s.Put(scene, V(2), kT0);                   // 9: released
    s.Put(LevelKey(kProp, 31), V(1), kT0);     // 10
    s.Put(LevelKey(kProp, 32), V(1), kT0);     // 11
    s.Put(gate, V(2), kT0);                    // 12
    Subscription sub{kScopeAll, {4, 4}};
    uint64_t cursor = 4;
    TimePoint now = kT0;
    bool early = false;
    while (cursor < s.Head()) {
      Batch b = WalkBatch(s, cursor, sub, 1);
      Deliver(c, b);
      cursor = b.proven;
      if (rng.Chance(0.7)) c.Pump(now);
      if (cursor < 12 && (c.AppliedAt(gate) >= 0 || c.AppliedAt(scene) >= 0)) early = true;
      if (cursor >= 9 && cursor < 12) {
        CHECK(c.replica.FirstLsn(gate) == 6);
        if (c.replica.Completes(Scope::kLevel, 9)) early = true;
      }
      now += 20ms;
    }
    c.Pump(now);
    CHECK(!early);
    const int ti = c.AppliedAt(toggle), gi = c.AppliedAt(gate, 2), si = c.AppliedAt(scene, 2);
    CHECK(ti >= 0 && gi > ti && si > gi);
    CHECK(c.AppliedAt(scene, 1) < 0 && c.AppliedAt(gate, 1) < 0);
    CHECK(c.events.empty());
  }
}

BACKEND_TEST("SR7b", SceneFencedChainTwoPumps) {
  WorldState s(TableB(), {});
  DirectClient c(TableB());
  const RecordKey scene = LevelKey(kScene, 1), gate = LevelKey(kGate, 2), toggle = LevelKey(kToggle, 3);
  c.spawn_at[gate] = kT0 + 1000ms;  // apply:late 1000, inside the gate's 5 s grace
  s.BeginEpoch(1);                          // 1
  s.Put(LevelKey(kProp, 20), V(1), kT0);    // 2
  s.Put(LevelKey(kProp, 21), V(1), kT0);    // 3
  Subscribe(c.replica, 3, 1);
  Subscription sub{kScopeAll, {3, 3}};
  s.Put(toggle, V(1), kT0);                 // 4
  s.Put(LevelKey(kProp, 22), V(1), kT0);    // 5
  s.Put(gate, V(1), kT0);                   // 6
  // Pump 1: the toggle and the gate, then Through{6}.
  Batch b1 = WalkBatch(s, 3, sub, 100);
  CHECK(b1.proven == 6);
  Deliver(c, b1);
  c.Pump(kT0);
  CHECK(c.AppliedAt(toggle) >= 0);
  CHECK(c.AppliedAt(gate) < 0 && c.replica.FirstLsn(gate) == 6);  // released, answered kWaiting
  // Then the scene is released at 9 (its only change) and the gate changes again at 12.
  s.Put(LevelKey(kProp, 23), V(1), kT0);    // 7
  s.Put(LevelKey(kProp, 24), V(1), kT0);    // 8
  s.Put(scene, V(2), kT0);                  // 9
  s.Put(LevelKey(kProp, 25), V(1), kT0);    // 10
  s.Put(LevelKey(kProp, 26), V(1), kT0);    // 11
  s.Put(gate, V(2), kT0);                   // 12
  Batch b2 = WalkBatch(s, b1.proven, sub, 100);
  CHECK(b2.proven == 12);
  bool gate_first_12 = false;
  for (const WalkItem& it : b2.items) {
    if (it.record && it.record->key == gate) gate_first_12 = it.first_lsn == 12;
  }
  CHECK(gate_first_12);  // first change 12 in that walk
  Deliver(c, b2);
  CHECK(c.replica.FirstLsn(gate) == 6);  // the replacement keeps the waiting entry's 6
  TimePoint now = kT0 + 100ms;
  c.Pump(now);
  CHECK(c.AppliedAt(scene) < 0);  // held behind the unsettled gate (6 <= 9)
  CHECK(!c.replica.Completes(Scope::kLevel, 9));
  bool same_pump = false;
  while (now < kT0 + 2s) {
    now += 100ms;
    const size_t before = c.apply_log.size();
    c.Pump(now);
    if (c.AppliedAt(gate) >= int(before)) same_pump = c.AppliedAt(scene) >= int(before);
  }
  CHECK(same_pump);
  const int ti = c.AppliedAt(toggle), gi = c.AppliedAt(gate), si = c.AppliedAt(scene);
  CHECK(ti >= 0 && gi > ti && si > gi);
  CHECK(c.AppliedAt(gate, 2) == gi && c.AppliedAt(gate, 1) < 0);  // at its newest revision
  CHECK(c.events.empty());
}

// ---- The apply feedback -------------------------------------------------------------------------

BACKEND_TEST("AF1", ReplicaSettleOnApplied) {
  Replica r(TableA(), {});
  CHECK(Subscribe(r, 0, 1));
  auto row = [](uint32_t n, uint32_t rev, uint64_t lsn) {
    Record rec;
    rec.key = LevelKey(kProp, n);
    rec.revision = rev;
    rec.epoch = 1;
    rec.lsn = lsn;
    rec.value = V(rev);
    return rec;
  };
  Record out;
  // kApplied settles.
  CHECK(r.OnRecord(row(1, 1, 1), 1) == ApplyResult::kApplied);
  CHECK(r.Unsettled() == 1);
  CHECK(r.TakeReady(out) && out.key == LevelKey(kProp, 1));
  CHECK(r.Unsettled() == 1);  // handed out, not answered
  CHECK(!r.TakeReady(out));
  r.OnApplied(out.key, out.revision, RowApply::kApplied, kT0);
  CHECK(r.Unsettled() == 0);
  // kDropped settles.
  r.OnRecord(row(2, 1, 2), 2);
  CHECK(r.TakeReady(out));
  r.OnApplied(out.key, out.revision, RowApply::kDropped, kT0);
  CHECK(r.Unsettled() == 0);
  // kWaiting and kFailed hand it out again after the next Service, and only then.
  r.OnRecord(row(3, 1, 3), 3);
  CHECK(r.TakeReady(out));
  r.OnApplied(out.key, out.revision, RowApply::kWaiting, kT0);
  CHECK(r.Unsettled() == 1 && !r.TakeReady(out));
  r.Service(kT0 + 10ms);
  CHECK(r.TakeReady(out) && out.key == LevelKey(kProp, 3));
  r.OnApplied(out.key, out.revision, RowApply::kFailed, kT0 + 10ms);
  CHECK(!r.TakeReady(out));
  r.Service(kT0 + 20ms);
  CHECK(r.TakeReady(out));
  CHECK(!r.TakeReady(out));  // once per Service
  r.OnApplied(out.key, out.revision, RowApply::kApplied, kT0 + 20ms);
  CHECK(r.Unsettled() == 0);
  // A result for a replaced older revision settles nothing.
  r.OnRecord(row(4, 1, 4), 4);
  CHECK(r.TakeReady(out) && out.revision == 1);
  CHECK(r.OnRecord(row(4, 2, 5), 5) == ApplyResult::kApplied);
  r.OnApplied(LevelKey(kProp, 4), 1, RowApply::kApplied, kT0);
  CHECK(r.Unsettled() == 1);
  CHECK(r.TakeReady(out) && out.revision == 2);
  r.OnApplied(out.key, 2, RowApply::kApplied, kT0);
  CHECK(r.Unsettled() == 0);
  // A result for a key never handed out changes nothing.
  r.OnRecord(row(5, 1, 6), 6);
  r.OnApplied(LevelKey(kProp, 5), 1, RowApply::kApplied, kT0);
  CHECK(r.Unsettled() == 1);
}

BACKEND_TEST("AF2", ReplicaGraceMakesLate) {
  Replica r(TableA(), {});
  CHECK(Subscribe(r, 0, 1));
  Record rec;
  rec.key = LevelKey(kProp, 1);
  rec.revision = 1;
  rec.epoch = 1;
  rec.lsn = 1;
  rec.value = V(1);
  r.OnRecord(rec, 1);
  Record out;
  CHECK(r.TakeReady(out));
  r.OnApplied(out.key, 1, RowApply::kWaiting, kT0);
  ReplicaEvent e;
  r.Service(kT0 + 3s);
  CHECK(!r.PollEvent(e));
  // A replacement at 3 s keeps the grace clock that began at 0 (Sol r2 N1).
  rec.revision = 2;
  rec.lsn = 2;
  rec.value = V(2);
  r.OnRecord(rec, 2);
  CHECK(r.FirstLsn(rec.key) == 1);
  CHECK(r.TakeReady(out) && out.revision == 2);
  r.OnApplied(out.key, 2, RowApply::kWaiting, kT0 + 3s);
  r.Service(kT0 + 4999ms);
  CHECK(!r.PollEvent(e));
  CHECK(r.TakeReady(out));
  r.OnApplied(out.key, 2, RowApply::kWaiting, kT0 + 4999ms);
  r.Service(kT0 + 5000ms);
  CHECK(r.PollEvent(e) && e.type == ReplicaEvent::Type::kRowLate && e.row == rec.key &&
        e.last == RowApply::kWaiting);
  CHECK(!r.PollEvent(e));
  // Late, but still handed out: a later spawn applies it.
  CHECK(r.TakeReady(out) && out.revision == 2);
  r.OnApplied(out.key, 2, RowApply::kApplied, kT0 + 6s);
  CHECK(r.Unsettled() == 0);
  r.Service(kT0 + 20s);
  CHECK(!r.PollEvent(e));
  // kFailed counts the same way.
  rec.key = LevelKey(kProp, 2);
  rec.revision = 1;
  rec.lsn = 3;
  r.OnRecord(rec, 3);
  CHECK(r.TakeReady(out));
  r.OnApplied(out.key, 1, RowApply::kFailed, kT0 + 20s);
  r.Service(kT0 + 25s);
  CHECK(r.PollEvent(e) && e.type == ReplicaEvent::Type::kRowLate && e.last == RowApply::kFailed);
}

BACKEND_TEST("AF2b", ReplicaGraceUnderContinuousReplacement) {
  // AF2 answers each replacement before the next Service, so the key is waiting whenever Service
  // looks. The client's pump receives first, then services, then applies (Client::Pump): a
  // dependency replaced before every pump is ready, not waiting or failed, at each Service, and
  // its grace clock from the first kWaiting or kFailed must still run out at the retained deadline
  // (council B1). The toggle first answers at 0; the gate (first change after the toggle's) is
  // held behind it; a newer toggle arrives before every pump through and past the grace while its
  // entity stays pending (or every apply fails); then the entity comes.
  for (const RowApply cause : {RowApply::kWaiting, RowApply::kFailed}) {
    WorldState s(TableB(), {});
    DirectClient c(TableB());
    const RecordKey toggle = LevelKey(kToggle, 1), gate = LevelKey(kGate, 2);
    const auto grace = std::chrono::milliseconds(FindKind(TableB(), kToggle)->pending_grace_ms);
    if (cause == RowApply::kWaiting) {
      c.spawn_at[toggle] = TimePoint::max();
    } else {
      c.fail_next[toggle] = 1000000;
    }
    s.BeginEpoch(1);                 // 1
    CHECK(Subscribe(c.replica, 1, 1));
    Subscription sub{kScopeAll, {1, 1}};
    s.Put(toggle, V(1), kT0);        // 2
    s.Put(gate, V(1), kT0);          // 3: on the toggle, first change 2 <= 3
    Batch b = WalkBatch(s, 1, sub, 100);
    Deliver(c, b);
    uint64_t cursor = b.proven;
    c.Pump(kT0);  // the toggle's first kWaiting or kFailed: its grace clock starts at 0
    CHECK(c.handed == 1 && c.AppliedAt(toggle) < 0);
    CHECK(c.AppliedAt(gate) < 0 && c.replica.FirstLsn(gate) == 3);
    CHECK(!c.replica.Completes(Scope::kLevel, 3));
    uint32_t value = 1;
    TimePoint late_at = TimePoint::max(), released_at = TimePoint::max();
    bool kept_clock = true, completes_early = false;
    for (TimePoint now = kT0 + 100ms; now <= kT0 + grace + 2s; now += 100ms) {
      // Receive: a newer toggle and its Through, so the entry is ready again when Service runs.
      s.Put(toggle, V(++value), now);
      b = WalkBatch(s, cursor, sub, 100);
      Deliver(c, b);
      cursor = b.proven;
      if (c.replica.FirstLsn(toggle) != 2) kept_clock = false;
      // Service, then ApplyReady.
      c.Pump(now);
      if (late_at == TimePoint::max() && c.Count(ReplicaEvent::Type::kRowLate) > 0) late_at = now;
      if (released_at == TimePoint::max() && c.AppliedAt(gate) >= 0) released_at = now;
      if (now < kT0 + grace && c.replica.Completes(Scope::kLevel, 3)) completes_early = true;
    }
    CHECK(kept_clock);  // the replacements kept the first change (and the clock) of 0
    CHECK(late_at == kT0 + grace);
    CHECK(released_at == kT0 + grace);
    CHECK(!completes_early && c.replica.Completes(Scope::kLevel, 3));
    // One row-late and one fence-broken event, however many Services followed.
    CHECK(c.Count(ReplicaEvent::Type::kRowLate) == 1);
    CHECK(c.Count(ReplicaEvent::Type::kFenceBroken) == 1);
    for (const ReplicaEvent& e : c.events) {
      if (e.type == ReplicaEvent::Type::kRowLate) CHECK(e.row == toggle && e.last == cause);
      if (e.type == ReplicaEvent::Type::kFenceBroken) {
        CHECK(e.row == gate && e.dependency == toggle && e.last == cause);
      }
    }
    CHECK(c.AppliedAt(gate, 1) >= 0 && c.AppliedAt(toggle) < 0);
    CHECK(c.replica.Unsettled() == 1);  // the late toggle, still handed out
    // The toggle's entity comes (or its apply goes): one more replacement, applied at its newest
    // revision, and nothing is left unsettled; no further event.
    c.spawn_at.erase(toggle);
    c.fail_next.erase(toggle);
    const TimePoint end = kT0 + grace + 2s + 100ms;
    s.Put(toggle, V(++value), end);
    b = WalkBatch(s, cursor, sub, 100);
    Deliver(c, b);
    c.Pump(end);
    const Record* last = s.Get(toggle);
    CHECK(last && c.AppliedAt(toggle) >= 0 && c.AppliedAt(toggle) == c.AppliedAt(toggle, last->revision));
    CHECK(c.world.contains(toggle) && c.world[toggle].value == V(value));
    CHECK(c.replica.Unsettled() == 0);
    CHECK(c.Count(ReplicaEvent::Type::kRowLate) == 1);
    CHECK(c.Count(ReplicaEvent::Type::kFenceBroken) == 1);
  }
}

BACKEND_TEST("AF3", ReplicaEpochDropsUnsettled) {
  Sr6Setup t;
  t.c.spawn_at[t.toggle] = TimePoint::max();
  TimePoint now = kT0;
  RunSr6(t, 3, now);
  CHECK(t.c.replica.Unsettled() == 2);  // the waiting toggle and the held scene
  t.s.BeginEpoch(2);
  Batch b = WalkBatch(t.s, t.cursor, t.sub, 16);
  Deliver(t.c, b);
  CHECK(t.c.replica.Unsettled() == 0);
  for (int i = 0; i < 100; ++i) {
    now += 100ms;
    t.c.Pump(now);
  }
  CHECK(t.c.events.empty());  // no row late, no fence broken
  CHECK(t.c.AppliedAt(t.scene) < 0);
}

// ---- Limits: the noisy hold ---------------------------------------------------------------------

BACKEND_TEST("LM9", LimitsNoisyRow) {
  Limits limits;
  WorldState s(TableA(), limits);
  DirectClient c(TableA(), limits);
  s.BeginEpoch(1);
  CHECK(Subscribe(c.replica, 1, 1));
  Subscription sub{kScopeAll, {1, 1}};
  uint64_t cursor = 1;
  const RecordKey mover = LevelKey(kMover, 1);
  std::vector<TimePoint> logged;  // when the mover's row took an LSN
  std::vector<TimePoint> deltas;  // when a delta of it went out
  std::vector<TimePoint> matched;
  uint64_t last_lsn = 0;
  bool cursor_at_head = true;
  TimePoint now = kT0, last_manifest = kT0;
  uint32_t last_value = 0;
  for (int tick = 0; tick < 60 * 10; ++tick) {  // every tick at 60 Hz, 10 s
    last_value = uint32_t(tick + 1);
    s.Put(mover, V(last_value), now);
    if (tick % 6 == 0) s.Put(LevelKey(kProp, uint32_t(tick % 50)), V(uint32_t(tick)), now);
    s.Service(now);
    if (const Record* r = s.Get(mover); r && r->lsn != last_lsn) {
      last_lsn = r->lsn;
      logged.push_back(now);
    }
    Batch b = WalkBatch(s, cursor, sub, 1000000);
    for (const WalkItem& it : b.items) {
      if (it.record && it.record->key == mover) deltas.push_back(now);
    }
    Deliver(c, b);
    cursor = b.proven;
    if (cursor != s.Head()) cursor_at_head = false;
    c.Pump(now);
    if (now - last_manifest >= limits.manifest_interval) {
      last_manifest = now;
      uint64_t mask = 0;
      if (c.replica.CheckManifest(s.MakeManifest(kScopeAll), &mask) == Replica::Check::kEqual) {
        matched.push_back(now);
      }
    }
    now += std::chrono::microseconds(16667);
  }
  auto at_most_per_second = [&](const std::vector<TimePoint>& times) {
    for (size_t i = 0; i < times.size(); ++i) {
      size_t n = 0;
      for (size_t j = i; j < times.size() && times[j] - times[i] < 1s; ++j) ++n;
      if (n > limits.noisy_hz) return false;
    }
    return true;
  };
  CHECK(logged.size() >= 15);
  CHECK(at_most_per_second(logged));
  CHECK(at_most_per_second(deltas));
  CHECK(cursor_at_head);
  CHECK(matched.size() >= 4);
  bool gaps_ok = true;
  for (size_t i = 1; i < matched.size(); ++i) gaps_ok &= matched[i] - matched[i - 1] <= 2 * limits.manifest_interval;
  CHECK(gaps_ok);
  // The last value wins once its interval passes.
  now += 1s;
  s.Service(now);
  CHECK(s.Get(mover) && s.Get(mover)->value == V(last_value));
  Batch b = WalkBatch(s, cursor, sub, 1000000);
  Deliver(c, b);
  c.Pump(now);
  CHECK(c.world.contains(mover) && c.world[mover].value == V(last_value));
}

BACKEND_TEST("LM9b", LimitsNoisyHoldDroppedByRemoveAndApplyAll) {
  // (a) A held value, then the row removed before Service: still a tomb, no log entry for it.
  {
    WorldState s(TableA(), {});
    s.BeginEpoch(1);
    const RecordKey mover = LevelKey(kMover, 1);
    CHECK(s.Put(mover, V(1), kT0) == PutResult::kWritten);
    const uint64_t head = s.Head();
    CHECK(s.Put(mover, V(2), kT0 + 10ms) == PutResult::kWritten);  // held
    CHECK(s.Head() == head && s.Get(mover)->value == V(1));
    CHECK(s.Remove(mover, kT0 + 20ms) == PutResult::kWritten);
    const uint64_t after_remove = s.Head();
    for (int i = 1; i <= 10; ++i) s.Service(kT0 + i * 1s);
    CHECK(s.Get(mover) && s.Get(mover)->tomb);
    CHECK(s.Head() == after_remove);
    Replica r(TableA(), {});
    Subscribe(r, 1, 1);
    Subscription sub{kScopeAll, {1, 1}};
    bool brought_back = false;
    s.Walk(1, sub, [&](const WalkItem& it) {
      if (it.record && it.record->key == mover && !it.record->tomb) brought_back = true;
      return true;
    }, nullptr);
    CHECK(!brought_back);
  }
  // (b) A held value, then an ApplyAll write: Service leaves the ApplyAll value at its revision.
  {
    WorldState s(TableA(), {});
    s.BeginEpoch(1);
    const RecordKey mover = LevelKey(kMover, 1);
    s.Put(mover, V(1), kT0);
    s.Put(mover, V(2), kT0 + 10ms);  // held
    const Write w[] = {{mover, V(9), false}, {LevelKey(kProp, 1), V(1), false}};
    const WriteResult res = s.ApplyAll(w, kT0 + 20ms);
    CHECK(res.result == PutResult::kWritten && res.last_lsn == res.first_lsn + 1);
    const uint32_t rev = s.Get(mover)->revision;
    const uint64_t head = s.Head();
    for (int i = 1; i <= 10; ++i) s.Service(kT0 + i * 1s);
    CHECK(s.Get(mover)->value == V(9) && s.Get(mover)->revision == rev);
    CHECK(s.Head() == head);
  }
}

// ---- S2-L4: the grace clocks stand still while the world is stopped -----------------------------

BACKEND_TEST("S2L4a", ReplicaGraceStandsStillWhileWorldStopped) {
  // A row waiting from 0 with a 5 s grace; the world stops at 1 s and runs again at 40 s: the row
  // is not late at 30 s nor at 43.999 s (4.999 s of world-runnable time), late at 44 s. A row whose
  // first wait comes inside a stop starts its clock at the resume. A repeated report changes
  // nothing.
  Replica r(TableA(), {});
  CHECK(Subscribe(r, 0, 1));
  auto row = [](uint32_t n, uint64_t lsn) {
    Record rec;
    rec.key = LevelKey(kProp, n);
    rec.revision = 1;
    rec.epoch = 1;
    rec.lsn = lsn;
    rec.value = V(1);
    return rec;
  };
  Record out;
  r.OnRecord(row(1, 1), 1);
  CHECK(r.TakeReady(out));
  r.OnApplied(out.key, 1, RowApply::kWaiting, kT0);
  CHECK(!r.WorldStopped());
  CHECK(r.WorldTime(kT0 + 1s) == kT0 + 1s);
  r.SetWorldStopped(true, kT0 + 1s);
  CHECK(r.WorldStopped());
  CHECK(r.WorldTime(kT0 + 30s) == kT0 + 1s);
  ReplicaEvent e;
  r.Service(kT0 + 30s);
  CHECK(!r.PollEvent(e));
  r.SetWorldStopped(true, kT0 + 31s);  // repeated: the stop still counts from 1 s
  r.SetWorldStopped(false, kT0 + 40s);
  CHECK(!r.WorldStopped());
  CHECK(r.WorldTime(kT0 + 40s) == kT0 + 1s);
  r.Service(kT0 + 43999ms);
  CHECK(!r.PollEvent(e));
  r.Service(kT0 + 44s);
  CHECK(r.PollEvent(e) && e.type == ReplicaEvent::Type::kRowLate && e.row == LevelKey(kProp, 1));
  CHECK(!r.PollEvent(e));
  // The second row's first wait comes during a stop (from 50 s to 70 s): its clock starts at 70 s.
  r.SetWorldStopped(true, kT0 + 50s);
  r.OnRecord(row(2, 2), 2);
  CHECK(r.TakeReady(out) && out.key == LevelKey(kProp, 2));
  r.OnApplied(out.key, 1, RowApply::kFailed, kT0 + 55s);
  r.Service(kT0 + 69s);
  CHECK(!r.PollEvent(e));
  r.SetWorldStopped(false, kT0 + 70s);
  r.Service(kT0 + 74999ms);
  CHECK(!r.PollEvent(e));
  r.Service(kT0 + 75s);
  CHECK(r.PollEvent(e) && e.type == ReplicaEvent::Type::kRowLate && e.row == LevelKey(kProp, 2) &&
        e.last == RowApply::kFailed);
  // Reset keeps the report (the game's state, not the session's).
  r.SetWorldStopped(true, kT0 + 80s);
  r.Reset();
  CHECK(r.WorldStopped());
}
