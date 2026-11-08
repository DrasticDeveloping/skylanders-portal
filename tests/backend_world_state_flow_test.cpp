// Convergence, baselines and the proven prefix at the message level, on WorldState and Replica
// alone (design sections 3.5, 5 and 10; work item 2): backend_world_state_harness.h plays S's
// per-peer send order and an ordered, reliable channel with delay, loss below the lane and kills
// above it. An extra direct-call layer under "WS-" ids: the design's ids (CV, BL, PP1, PP2, PP4,
// PP7, PP8, SR1, SR4) run on backend::Server and backend::Client over MemoryLink and NetplayLink
// in backend_flow_test.cpp.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <map>
#include <vector>

#include "backend/world_state.h"
#include "backend_test.h"
#include "backend_world_state_harness.h"

using namespace skylanders::backend;
using namespace skylanders::backend::test::ws;
using namespace std::chrono_literals;

namespace {

// A script of writes: level props, toggles and the scene, and session trackers, with removes;
// `per_pump` writes each call.
struct Churn {
  Harness& h;
  Rng rng;
  uint32_t generation = 1;
  uint32_t rows = 60;
  uint32_t value = 1;

  RecordKey RandomKey() {
    switch (rng.Below(8)) {
      case 0: return LevelKey(kToggle, 100 + uint32_t(rng.Below(8)), generation);
      case 1: return LevelKey(kScene, 200, generation);
      case 2: return SessionKey(kTracker, 1 + uint32_t(rng.Below(6)));
      case 3: return LevelKey(kServerOnly, 1 + uint32_t(rng.Below(4)), generation);
      default: return LevelKey(kProp, 1 + uint32_t(rng.Below(rows)), generation);
    }
  }
  void Write(int per_pump) {
    for (int i = 0; i < per_pump; ++i) {
      const RecordKey k = RandomKey();
      if (rng.Below(10) == 0) {
        h.s.Remove(k, h.now);
      } else {
        h.s.Put(k, V(value++, 8), h.now);
      }
    }
  }
};

// One peer (1) joined and level-ready on generation 1, with `rows` props in place.
void StartOne(Harness& h, uint32_t rows = 60, uint16_t peer = 1) {
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= rows; ++i) h.s.Put(LevelKey(kProp, i), V(i, 8), h.now);
  h.s.Put(LevelKey(kScene, 200), V(1), h.now);
  for (uint32_t i = 0; i < 8; ++i) h.s.Put(LevelKey(kToggle, 100 + i), V(1), h.now);
  h.Join(peer);
  h.LevelReady(peer);
}

// The revisions a replica holds never go down (section 7.3's invariant), checked every pump.
struct RevisionWatch {
  std::map<RecordKey, uint32_t> seen;
  bool ok = true;
  void Check(const Replica& r) {
    r.ForEach([&](const Record& row) {
      auto [it, added] = seen.try_emplace(row.key, row.revision);
      if (!added) {
        if (row.revision < it->second) ok = false;
        it->second = row.revision;
      }
    });
  }
};

void ConvergeWithLoss(Duration one_way, double loss) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Harness h(TableA(), uint64_t(seed));
    h.SetLink(1, one_way, loss);
    StartOne(h);
    Churn churn{h, Rng{uint64_t(seed) * 7}};
    RevisionWatch watch;
    h.Run(test::Long() ? 60s : 15s, 20ms, [&] {
      churn.Write(2);
      watch.Check(h.cp[1]->c.replica);
    });
    CHECK(h.RunUntilQuiet(30s));
    CHECK(h.Equal(1));
    CHECK(watch.ok);
    CHECK(h.cp[1]->prefix_ok);
    CHECK(h.sp[1].repairs_begun == 0);
    CHECK(h.sp[1].mismatches == 0);
  }
}

}  // namespace

// ---- Convergence after loss ---------------------------------------------------------------------

BACKEND_TEST("WS-CV1", WsConvergeLoss5Rtt300) { ConvergeWithLoss(150ms, 0.05); }

BACKEND_TEST("WS-CV2", WsConvergeLoss30Rtt300) { ConvergeWithLoss(150ms, 0.30); }

BACKEND_TEST("WS-CV3", WsConvergeDuplicatesAndReorder) {
  // The lane hides datagram duplicates and reordering; what can still reach the replica is an
  // older copy of a row (a delta re-delivered after a reconnect, VR1). Each pump, a random older
  // copy goes into the replica again: the revision rule drops it.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Harness h(TableA(), uint64_t(seed));
    h.SetLink(1, 150ms, 0.10);
    StartOne(h);
    Churn churn{h, Rng{uint64_t(seed) * 11}};
    std::vector<std::pair<Record, uint64_t>> history;
    RevisionWatch watch;
    Rng dup{uint64_t(seed) * 13};
    h.Run(15s, 20ms, [&] {
      churn.Write(2);
      Replica& r = h.cp[1]->c.replica;
      r.ForEach([&](const Record& row) {
        if (dup.Chance(0.01)) history.emplace_back(row, row.lsn);
      });
      if (!history.empty() && dup.Chance(0.5)) {
        const auto& [old, first] = history[dup.Below(history.size())];
        r.OnRecord(old, first);
      }
      watch.Check(r);
    });
    CHECK(h.RunUntilQuiet(30s));
    CHECK(h.Equal(1));
    CHECK(watch.ok);
    CHECK(h.sp[1].repairs_begun == 0);
  }
}

BACKEND_TEST("WS-CV4", WsConvergeKilledDelta) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Harness h(TableA(), uint64_t(seed));
    h.SetLink(1, 150ms, 0.05);
    StartOne(h);
    Churn churn{h, Rng{uint64_t(seed) * 17}};
    // The killed delta is of a key the churn never writes, so no later delta heals it.
    const RecordKey k = LevelKey(kProp, 900);
    bool killed = false;
    h.sp[1].kill = [&](const Down& m) {
      if (killed || m.type != Down::Type::kDelta || m.rec.key != k) return false;
      killed = true;
      return true;
    };
    h.Run(10s, 20ms, [&] {
      churn.Write(1);
      if (h.now >= kT0 + 5s && !h.s.Get(k)) h.s.Put(k, V(1, 8), h.now);
    });
    CHECK(killed);
    CHECK(h.RunUntilQuiet(30s));
    CHECK(h.Equal(1));
    CHECK(h.sp[1].mismatches == 1);
    CHECK(h.sp[1].repairs_begun == 1);
  }
}

BACKEND_TEST("WS-CV5", WsConvergeKilledBaselineEnd) {
  Harness h(TableA(), 5);
  StartOne(h);
  Churn churn{h, Rng{55}};
  // The level baseline's end is killed: the client's level staging stays open.
  h.sp[1].kill = [&](const Down& m) {
    return m.type == Down::Type::kBaselineEnd && (h.sp[1].begun.size() == 2) && h.sp[1].repairs_begun == 0;
  };
  bool level_completed_while_staging = false;
  bool session_completed = false;
  bool prefix_moved_while_staging = false;
  uint64_t prefix_at_stage = 0;
  h.Run(75s, 50ms, [&] {
    churn.Write(1);
    const Replica& r = h.cp[1]->c.replica;
    if (r.Staging() & kScopeLevel) {
      if (prefix_at_stage == 0) prefix_at_stage = r.Prefix();
      if (r.Prefix() > prefix_at_stage + 20) prefix_moved_while_staging = true;
      if (r.Completes(Scope::kLevel, r.Prefix())) level_completed_while_staging = true;
      if (r.Completes(Scope::kSession, r.Prefix())) session_completed = true;
    }
  });
  CHECK(prefix_moved_while_staging);  // Through moves the prefix (Opus F1)
  CHECK(!level_completed_while_staging);  // but completes nothing of the staging scope (Opus F8)
  CHECK(session_completed);
  CHECK(h.sp[1].repairs_begun >= 1);  // the silence rule repaired it (no manifest could match)
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1));
  CHECK(h.cp[1]->c.replica.Staging() == 0);
}

BACKEND_TEST("WS-CV6", WsConvergeSilence60s) {
  Harness h(TableA(), 6);
  StartOne(h, 60, 1);
  h.Join(2);
  h.LevelReady(2);
  Churn churn{h, Rng{66}};
  // Peer 1's manifests are killed; its acks flow with an unchanged manifest_ok.
  h.sp[1].kill = [](const Down& m) { return m.type == Down::Type::kManifest; };
  TimePoint repaired_at{};
  h.Run(70s, 50ms, [&] {
    churn.Write(1);
    if (repaired_at == TimePoint{} && h.sp[1].repairs_begun > 0) repaired_at = h.now;
  });
  CHECK(h.sp[1].repairs_begun == 1);
  CHECK(repaired_at >= kT0 + 59s && repaired_at <= kT0 + 62s);
  CHECK(h.sp[2].repairs_begun == 0);  // its manifest_ok moves
  h.sp[1].kill = nullptr;
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1) && h.Equal(2));
}

// ---- Baseline under churn and mid-baseline disconnect ---------------------------------------

BACKEND_TEST("WS-BL1", WsBaselineUnderChurn) {
  Limits limits;
  Harness h(TableA(), 1, limits);
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= 2000; ++i) h.s.Put(LevelKey(kProp, i), V(i, 32), h.now);
  h.Join(1);
  h.LevelReady(1);
  // The pacer: 40 KB/s of about 80-byte rows, 20 ms pumps: 10 rows a pump (a 4 s baseline).
  h.sp[1].rows_per_pump = limits.baseline_bytes_per_second / 80 / 50;
  Rng rng{1};
  uint32_t value = 10000;
  h.Run(6s, 20ms, [&] {
    for (int i = 0; i < 10; ++i) {  // 500 writes/s
      h.s.Put(LevelKey(kProp, 1 + uint32_t(rng.Below(2000))), V(value++, 32), h.now);
    }
  });
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1));
  CHECK(h.cp[1]->c.replica.Rows() == 2000);
  CHECK(h.sp[1].repairs_begun == 0);
  CHECK(h.cp[1]->digest_failed == 0);
}

BACKEND_TEST("WS-BL2", WsBaselineStagingSwap) {
  Harness h(TableA(), 2);
  StartOne(h);
  CHECK(h.RunUntilQuiet(30s));
  // A row the replica has and S does not (never had): the manifest finds it, the repair
  // baseline's swap removes it.
  Record extra;
  extra.key = LevelKey(kProp, 999);
  extra.revision = 3;
  extra.epoch = 1;
  extra.lsn = h.s.Head();
  extra.value = V(1);
  h.cp[1]->c.replica.OnRecord(extra, extra.lsn);
  CHECK(h.cp[1]->c.replica.Get(extra.key) != nullptr);
  h.s.Put(LevelKey(kProp, 1), V(77, 8), h.now);  // so the next manifest has a new head
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.sp[1].repairs_begun == 1);
  CHECK(h.cp[1]->c.replica.Get(extra.key) == nullptr);
  CHECK(h.Equal(1));
}

BACKEND_TEST("WS-BL3", WsBaselineMidDisconnect) {
  Harness h(TableA(), 3);
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= 200; ++i) h.s.Put(LevelKey(kProp, i), V(i, 8), h.now);
  h.Join(1);
  h.sp[1].rows_per_pump = 10;
  h.LevelReady(1);
  // Run until the level baseline has sent 5 chunks, then drop the peer.
  for (int i = 0; i < 200; ++i) {
    h.Pump();
    h.now += 20ms;
    if (h.sp[1].base && h.sp[1].base->scopes == kScopeLevel && h.sp[1].base->next >= 50) break;
  }
  CHECK(h.sp[1].base && h.sp[1].base->next >= 50);
  const uint32_t partial_id = h.sp[1].base->id;
  h.Run(200ms);
  CHECK(h.cp[1]->c.replica.Staging() == kScopeLevel);
  h.Leave(1);
  CHECK(h.cp[1]->c.replica.Rows() == 0 && h.cp[1]->c.replica.Staging() == 0);
  h.Run(1s);
  h.Join(1);
  h.sp[1].rows_per_pump = 10;
  h.LevelReady(1);
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.sp[1].next_id > partial_id + 1);  // the rejoin's baselines have new ids
  CHECK(h.Equal(1));
  CHECK(h.cp[1]->c.replica.Rows() == 200);
}

BACKEND_TEST("WS-BL4", WsBaselineLateJoin) {
  Harness h(TableA(), 4);
  StartOne(h, 100, 1);
  Churn churn{h, Rng{44}};
  churn.rows = 100;
  bool level_before_ready = false;
  size_t peer1_throughs_during = 0;
  uint64_t p1_through = 0;
  h.Run(3s, 20ms, [&] { churn.Write(2); });
  h.Join(2);
  h.sp[2].rows_per_pump = 8;
  h.Run(2s, 20ms, [&] {
    churn.Write(2);
    h.cp[2]->c.replica.ForEach([&](const Record& r) {
      if (FindKind(TableA(), r.key.kind)->scope == Scope::kLevel) level_before_ready = true;
    });
  });
  h.LevelReady(2);
  h.Run(3s, 20ms, [&] {
    churn.Write(2);  // rows written during its level baseline
    if (h.cp[1]->last_through > p1_through) {
      p1_through = h.cp[1]->last_through;
      ++peer1_throughs_during;
    }
  });
  CHECK(!level_before_ready);
  CHECK(peer1_throughs_during > 50);  // nothing waits on the joiner
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1) && h.Equal(2));
  CHECK(h.sp[1].repairs_begun == 0 && h.sp[2].repairs_begun == 0);
}

BACKEND_TEST("WS-BL5", WsBaselineOneAtATime) {
  Harness h(TableA(), 5);
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= 300; ++i) h.s.Put(LevelKey(kProp, i), V(i, 8), h.now);
  h.Join(1);
  h.LevelReady(1);
  CHECK(h.RunUntilQuiet(30s));
  const int begun = h.sp[1].baselines_begun;
  h.sp[1].rows_per_pump = 5;
  // A repair baseline runs; a mismatch about an LSN before its `at` is answered by it.
  h.AddReason(1, Why::kRepair, h.s.Head());
  h.Pump();
  h.now += 20ms;
  CHECK(h.sp[1].base.has_value());
  const uint64_t at = h.sp[1].base->at;
  h.AddReason(1, Why::kRepair, at - 1);
  h.AddReason(1, Why::kRepair, at);
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.sp[1].baselines_begun == begun + 1);
  // A mismatch after the running baseline's `at` starts a second one, once the first ends.
  h.AddReason(1, Why::kRepair, h.s.Head());
  h.Pump();
  h.now += 20ms;
  const uint64_t at2 = h.sp[1].base->at;
  h.s.Put(LevelKey(kProp, 1), V(5000, 8), h.now);
  h.AddReason(1, Why::kRepair, h.s.Head());
  CHECK(h.s.Head() > at2);
  bool two_at_once = false;
  int begun_while_running = 0;
  for (int i = 0; i < 1000 && (h.sp[1].base || !h.sp[1].reasons.empty()); ++i) {
    const bool running = h.sp[1].base.has_value();
    const int before = h.sp[1].baselines_begun;
    h.Pump();
    h.now += 20ms;
    if (running && h.sp[1].base && h.sp[1].baselines_begun > before) ++begun_while_running;
  }
  (void)two_at_once;
  CHECK(begun_while_running == 0);
  CHECK(h.sp[1].baselines_begun == begun + 3);
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1));
}

BACKEND_TEST("WS-BL6", WsBaselineEpochChange) {
  Harness h(TableA(), 6);
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= 200; ++i) h.s.Put(LevelKey(kProp, i), V(i, 8), h.now);
  h.Join(1);
  h.sp[1].rows_per_pump = 5;
  h.LevelReady(1);
  h.Run(400ms);
  CHECK(h.sp[1].base && h.sp[1].base->scopes == kScopeLevel);
  CHECK(h.cp[1]->c.replica.Staging() == kScopeLevel);
  // The generation changes mid-baseline: S drops it at the epoch entry, the client at EpochBegin.
  h.NewGeneration(2);
  for (uint32_t i = 1; i <= 50; ++i) h.s.Put(LevelKey(kProp, i, 2), V(i, 8), h.now);
  h.Run(500ms);
  CHECK(!h.sp[1].base);
  CHECK(h.cp[1]->c.replica.Staging() == 0);
  CHECK((h.cp[1]->c.replica.Subscribed() & kScopeLevel) == 0);
  CHECK(h.cp[1]->c.replica.Epoch() == 2);
  // The level baseline follows level ready.
  h.LevelReady(1);
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1));
  CHECK(h.cp[1]->c.replica.Rows() == 50);
  CHECK(h.sp[1].repairs_begun == 0);
  CHECK(h.sp[1].begun.size() == 3);  // join, level (dropped), level
}

BACKEND_TEST("WS-BL7", WsBaselineLevelAfterJoin) {
  Harness h(TableA(), 7);
  h.NewGeneration(1);
  h.Join(1);
  bool level_completed = false;
  Rng rng{7};
  uint64_t session_at = 0;
  h.Run(3s, 20ms, [&] {
    h.s.Put(LevelKey(kProp, 1 + uint32_t(rng.Below(40))), V(uint32_t(rng.Next()), 8), h.now);
    if (rng.Chance(0.2)) {
      h.s.Put(SessionKey(kTracker, 1), V(uint32_t(rng.Next())), h.now);
      session_at = h.s.Head();
    }
    const Replica& r = h.cp[1]->c.replica;
    if (r.Completes(Scope::kLevel, std::min(r.Prefix(), h.s.Head()))) level_completed = true;
  });
  CHECK(!level_completed);
  CHECK(h.cp[1]->c.replica.Completes(Scope::kSession, session_at));
  CHECK(h.cp[1]->c.replica.Rows() <= 1);  // the tracker, no level row
  h.LevelReady(1);
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1));
  CHECK(h.cp[1]->c.replica.Completes(Scope::kLevel, h.s.Head()));
  CHECK(h.sp[1].repairs_begun == 0);
}

BACKEND_TEST("WS-BL8", WsBaselineQuietAfter) {
  Harness h(TableA(), 8);
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= 100; ++i) h.s.Put(LevelKey(kProp, i), V(i, 8), h.now);
  h.Join(1);
  h.sp[1].rows_per_pump = 5;
  h.LevelReady(1);
  h.Run(200ms);
  CHECK(h.sp[1].base && h.sp[1].base->scopes == kScopeLevel);
  const uint64_t at = h.sp[1].base->at;
  // A commit's writes in (at, head], then S goes quiet.
  h.s.Put(LevelKey(kProp, 3), V(333, 8), h.now);
  h.s.Put(LevelKey(kProp, 101), V(1, 8), h.now);
  const uint64_t at_lsn = h.s.Head();
  CHECK(at_lsn > at);
  const uint64_t head = h.s.Head();
  while (h.sp[1].base || h.cp[1]->c.replica.Staging() != 0) h.Run(20ms);
  h.Run(1s);
  CHECK(h.s.Head() == head);  // no further write
  CHECK(h.cp[1]->c.replica.Prefix() == head);
  CHECK(h.cp[1]->c.replica.Completes(Scope::kLevel, at_lsn));
  const int equal = h.cp[1]->manifests_equal;
  h.Run(3s);
  CHECK(h.cp[1]->manifests_equal > equal);
  CHECK(h.cp[1]->c.replica.ManifestOk() == head);
}

BACKEND_TEST("WS-BL9", WsBaselineDigestUnderChurn) {
  Harness h(TableA(), 9);
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= 100; ++i) h.s.Put(LevelKey(kProp, i), V(i, 8), h.now);
  h.Join(1);
  h.sp[1].rows_per_pump = 10;
  h.LevelReady(1);
  for (int i = 0; i < 200 && !(h.sp[1].base && h.sp[1].base->scopes == kScopeLevel); ++i) h.Run(20ms);
  CHECK(h.sp[1].base.has_value());
  h.Run(60ms);  // some chunks out
  const auto& keys = h.sp[1].base->keys;
  const size_t next = h.sp[1].base->next;
  CHECK(next > 0 && next < keys.size());
  const RecordKey before_chunk = keys.back();  // changed before its chunk goes: in the digest
  const RecordKey after_chunk = keys.front();  // changed after its chunk went: a delta
  h.s.Put(before_chunk, V(9001, 8), h.now);
  h.s.Put(after_chunk, V(9002, 8), h.now);
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.cp[1]->digest_failed == 0);
  CHECK(h.sp[1].repairs_begun == 0);
  CHECK(h.cp[1]->c.replica.Get(before_chunk)->value == V(9001, 8));
  CHECK(h.cp[1]->c.replica.Get(after_chunk)->value == V(9002, 8));
  CHECK(h.Equal(1));
}

BACKEND_TEST("WS-BL10", WsBaselineDigestKilledRow) {
  Harness h(TableA(), 10);
  h.NewGeneration(1);
  for (uint32_t i = 1; i <= 50; ++i) h.s.Put(LevelKey(kProp, i), V(i, 8), h.now);
  h.s.Put(SessionKey(kTracker, 1), V(1), h.now);
  h.Join(1);
  int level_rows = 0;
  h.sp[1].kill = [&](const Down& m) {
    if (m.type != Down::Type::kBaselineRow || h.sp[1].begun.size() != 2) return false;
    return ++level_rows == 3;
  };
  h.sp[1].rows_per_pump = 10;
  h.LevelReady(1);
  // Snapshot the active replica before each client pump; the pump that fails the digest must
  // leave it as it was.
  bool unchanged = false;
  bool failed = false;
  for (int i = 0; i < 500 && !failed; ++i) {
    std::vector<Record> before;
    h.cp[1]->c.replica.ForEach([&](const Record& r) { before.push_back(r); });
    const size_t unsettled = h.cp[1]->c.replica.Unsettled();
    h.Pump();
    h.now += 20ms;
    if (h.cp[1]->digest_failed == 1) {
      failed = true;
      std::vector<Record> after;
      h.cp[1]->c.replica.ForEach([&](const Record& r) { after.push_back(r); });
      unchanged = before == after && h.cp[1]->c.replica.Unsettled() == unsettled &&
                  h.cp[1]->c.replica.Staging() == 0;
    }
  }
  CHECK(failed);
  CHECK(unchanged);
  h.sp[1].kill = nullptr;
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.sp[1].repairs_begun == 1);  // Mismatch{at, 0}
  CHECK(h.Equal(1));
}

// ---- Proven prefix at the message level ------------------------------------------------------

BACKEND_TEST("WS-PP1", WsProvenPrefixLossOnlyAtThrough) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Harness h(TableA(), uint64_t(seed));
    h.SetLink(1, 50ms, 0.30);
    StartOne(h);
    h.sp[1].batch_cap = 7;
    Churn churn{h, Rng{uint64_t(seed) * 3}};
    bool acks_ok = true;
    h.Run(10s, 20ms, [&] {
      churn.Write(3);
      if (h.cp[1]->acked_prefix > std::max(h.cp[1]->last_through, h.cp[1]->last_covering_at)) {
        acks_ok = false;
      }
    });
    CHECK(h.cp[1]->prefix_ok);
    CHECK(acks_ok);
    CHECK(h.RunUntilQuiet(30s));
    CHECK(h.Equal(1));
  }
}

BACKEND_TEST("WS-PP2", WsProvenPrefixKilledDelta) {
  Harness h(TableA(), 2);
  StartOne(h);
  CHECK(h.RunUntilQuiet(30s));
  const RecordKey k = LevelKey(kProp, 5);
  h.sp[1].kill = [&](const Down& m) { return m.type == Down::Type::kDelta && m.rec.key == k; };
  h.s.Put(k, V(4242, 8), h.now);
  const uint64_t lsn = h.s.Head();
  h.s.Put(LevelKey(kProp, 6), V(4243, 8), h.now);
  h.Run(500ms);
  h.sp[1].kill = nullptr;
  CHECK(h.cp[1]->c.replica.Prefix() >= lsn);  // the Through came: S cannot see a kill
  CHECK(h.cp[1]->c.replica.Get(k)->value != V(4242, 8));
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.sp[1].mismatches == 1);
  CHECK(h.sp[1].repairs_begun == 1);
  CHECK(h.Equal(1));
}

BACKEND_TEST("WS-PP4", WsProvenPrefixManifestOnlyAtHead) {
  Harness h(TableA(), 4);
  StartOne(h);
  CHECK(h.RunUntilQuiet(30s));
  h.sp[1].batch_cap = 2;
  // A burst of 300 new rows the walk needs 3 s for: no manifest while the cursor is behind,
  // though one falls due after 2 s.
  for (uint32_t i = 0; i < 300; ++i) h.s.Put(LevelKey(kProp, 1000 + i), V(i, 8), h.now);
  const int sent = h.sp[1].manifests_sent;
  bool behind_manifest = false;
  bool sent_at_head = false;
  int pumps = 0;
  while (h.sp[1].cursor < h.s.Head() && pumps++ < 1000) {
    const int before = h.sp[1].manifests_sent;
    h.Pump();
    h.now += 20ms;
    if (h.sp[1].manifests_sent > before && h.sp[1].cursor != h.s.Head()) behind_manifest = true;
    if (h.sp[1].cursor == h.s.Head()) sent_at_head = h.sp[1].manifests_sent == before + 1;
  }
  CHECK(pumps > 100);
  CHECK(!behind_manifest);
  CHECK(h.sp[1].manifests_sent == sent + 1);
  CHECK(sent_at_head);  // at the first pump with the cursor at the head
}

BACKEND_TEST("WS-PP7", WsProvenPrefixManifestsUnderChurn) {
  Harness h(TableA(), 7);
  StartOne(h, 200);
  CHECK(h.RunUntilQuiet(30s));
  Rng rng{77};
  bool killed = false;
  TimePoint killed_at{}, found_at{};
  // The killed delta is of a key the churn never writes, so no later delta heals it.
  const RecordKey k = LevelKey(kProp, 900);
  h.sp[1].kill = [&](const Down& m) {
    if (killed || m.type != Down::Type::kDelta || m.rec.key != k) return false;
    killed = true;
    killed_at = h.now;
    return true;
  };
  const TimePoint start = h.now;
  uint32_t value = 1;
  h.Run(20s, 20ms, [&] {
    for (int i = 0; i < 8; ++i) h.s.Put(LevelKey(kProp, 1 + uint32_t(rng.Below(200))), V(value++, 8), h.now);
    if (h.now >= start + 5s && !h.s.Get(k)) h.s.Put(k, V(1, 8), h.now);
    if (killed && found_at == TimePoint{} && h.cp[1]->mismatches_sent > 0) found_at = h.now;
  });
  CHECK(killed);
  CHECK(found_at != TimePoint{} && found_at - killed_at <= 2 * h.limits.manifest_interval);
  // A manifest reaches the peer at least every two intervals throughout (a repair baseline's run
  // aside, when none goes).
  bool gaps_ok = true;
  const auto& times = h.sp[1].manifest_times;
  for (size_t i = 1; i < times.size(); ++i) {
    if (times[i] < start) continue;
    if (times[i] - times[i - 1] > 2 * h.limits.manifest_interval + 500ms) gaps_ok = false;
  }
  CHECK(gaps_ok);
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.Equal(1));
}

BACKEND_TEST("WS-PP8", WsProvenPrefixKilledCoalescedKey) {
  // PP6's cut with K killed after the lane delivered it (Sol F1): the prefix passes K's first
  // change; a commit at that LSN may complete without K (expected here only); the manifest finds
  // it within two intervals; a repair baseline; equal after quiet.
  Harness h(TableA(), 8);
  StartOne(h);
  CHECK(h.RunUntilQuiet(30s));
  h.sp[1].batch_cap = 3;
  const RecordKey k = LevelKey(kProp, 1);
  h.s.Put(k, V(501, 8), h.now);
  const uint64_t k_first = h.s.Head();
  h.s.Put(LevelKey(kProp, 6), V(1, 8), h.now);
  h.s.Put(LevelKey(kProp, 7), V(1, 8), h.now);
  h.s.Put(LevelKey(kProp, 8), V(1, 8), h.now);
  h.s.Put(k, V(502, 8), h.now);
  // Every delta of K is killed: K's change at its second LSN lies after the cut's Through, so the
  // next walk sends K again, and one kill alone would be healed by it (no mismatch to find).
  bool killed = false;
  h.sp[1].kill = [&](const Down& m) {
    if (m.type != Down::Type::kDelta || m.rec.key != k) return false;
    killed = true;
    return true;
  };
  bool completed_without_k = false;
  TimePoint killed_at = h.now, found_at{};
  h.Run(6s, 20ms, [&] {
    const Replica& r = h.cp[1]->c.replica;
    if (r.Completes(Scope::kLevel, k_first) && r.Get(k)->value == V(k.entity.a, 8)) {
      completed_without_k = true;  // "completed on a killed row"
    }
    if (found_at == TimePoint{} && h.cp[1]->mismatches_sent > 0) found_at = h.now;
  });
  CHECK(killed);
  CHECK(completed_without_k);
  CHECK(found_at != TimePoint{} && found_at - killed_at <= 2 * h.limits.manifest_interval + 100ms);
  h.sp[1].kill = nullptr;
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.sp[1].repairs_begun == 1);
  CHECK(h.Equal(1));
}

// ---- Scene release at the message level ------------------------------------------------------

BACKEND_TEST("WS-SR1", WsSceneReleaseAfterToggles) {
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Harness h(TableA(), uint64_t(seed));
    StartOne(h);
    CHECK(h.RunUntilQuiet(30s));
    h.SetLink(1, 300ms, 0.1);  // the state lane slowed
    h.sp[1].batch_cap = 2;
    const RecordKey scene = LevelKey(kScene, 200);
    std::vector<std::pair<RecordKey, uint32_t>> toggles;
    for (uint32_t i = 0; i < 8; ++i) {
      const RecordKey t = LevelKey(kToggle, 100 + i);
      h.s.Put(t, V(seed * 100 + i), h.now);
      toggles.emplace_back(t, h.s.Get(t)->revision);
    }
    h.s.Put(scene, V(2), h.now);  // released
    const uint32_t released = h.s.Get(scene)->revision;
    CHECK(h.RunUntilQuiet(30s));
    const DirectClient& c = h.cp[1]->c;
    const int si = c.AppliedAt(scene, released);
    bool after_all = si >= 0;
    for (const auto& [t, rev] : toggles) after_all &= c.AppliedAt(t, rev) >= 0 && c.AppliedAt(t, rev) < si;
    CHECK(after_all);
    CHECK(c.events.empty());
  }
}

BACKEND_TEST("WS-SR4", WsSceneJoinDuringOpenScene) {
  Harness h(TableA(), 4);
  StartOne(h);
  const RecordKey scene = LevelKey(kScene, 200);
  h.s.Put(scene, V(7), h.now);  // open
  CHECK(h.RunUntilQuiet(30s));
  const uint64_t p1_prefix = h.cp[1]->c.replica.Prefix();
  h.Join(2);
  h.LevelReady(2);
  h.s.Put(LevelKey(kToggle, 100), V(9), h.now);
  h.Run(200ms);
  CHECK(h.cp[1]->c.replica.Prefix() > p1_prefix);  // nothing waits for the joiner
  CHECK(h.RunUntilQuiet(30s));
  CHECK(h.cp[2]->c.replica.Get(scene) && h.cp[2]->c.replica.Get(scene)->value == V(7));
  CHECK(h.cp[2]->c.AppliedAt(scene) >= 0);
  CHECK(h.Equal(1) && h.Equal(2));
}
