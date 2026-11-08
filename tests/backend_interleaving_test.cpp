// Interleaving (design section 5, IL1 to IL7; work items 3 and 4): requests of two peers on one
// row, server-run writes beside commits, the message-layer delay on one lane at a time, rows
// removed or created during a baseline, S's own wait, and a lease, on FakeSession over MemoryLink.

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <map>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

#include "backend/fake_network.h"
#include "backend/map_dump.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using namespace test::lanes;

// Answers every kEvaluate the way a native that ran would: S writes the subject's row (the value
// is the request's sequence, so a run's writes do not depend on timing), then passes.
void EvaluateByWriting(Rig& r) {
  r.session.on_server_event = [&r](const ServerEvent& ev) {
    if (ev.type != ServerEvent::Type::kEvaluate) return;
    r.S().State().Put(Key(ev.request.kind, ev.request.subject), Val(ev.request.key.sequence),
                      r.Now());
    EvaluationResult pass;
    pass.scope = Scope::kLevel;
    r.S().SubmitEvaluation(ev.request.key, pass, r.Now());
  };
}

std::optional<MapDump> MapOf(const WorldState& state) {
  std::string error;
  auto m = ParseMap(DumpMap(state, MapDumpOptions{}), &error);
  if (!m) std::fprintf(stderr, "  map parse: %s\n", error.c_str());
  return m;
}

// The number of baselines S began for `peer` with reason `why`.
size_t Baselines(FakeSession& s, uint16_t peer, BaselineReason why) {
  size_t n = 0;
  for (const ServerEvent& e : s.ServerEvents()) {
    n += e.type == ServerEvent::Type::kBaselineBegun && e.peer == peer && e.why == why;
  }
  return n;
}

// Rows enough for a paced level baseline of about `seconds` at the default budget.
void FillProps(Rig& r, uint32_t first, uint32_t count) {
  for (uint32_t n = first; n < first + count; ++n) {
    r.Everywhere(Ent(1, n), kStart);
    r.Put(Key(test::kProp, Ent(1, n)), Val(n, 64));
  }
}

}  // namespace

BACKEND_TEST("IL1", InterleaveSameBasisOnePump) {
  // Two peers commit on one row with the same basis revision in one pump: the first in S's receive
  // order applies; the second is refused `stale basis` with the new revision.
  Rig r;
  const EntityId e = Ent(1, 1);
  r.Everywhere(e, kStart);
  r.Join(1);
  r.Join(2);
  const uint32_t rev = r.Put(Key(test::kProp, e), Val(1));
  CHECK(r.session.RunUntil(
      [&] {
        const Record* a = r.C(1).State().Get(Key(test::kProp, e));
        const Record* b = r.C(2).State().Get(Key(test::kProp, e));
        return a && b && a->revision == rev && b->revision == rev;
      },
      2s));
  const auto k1 = r.C(1).Send(CommitOn(test::kProp, e, rev, Val(11)), r.Now());
  const auto k2 = r.C(2).Send(CommitOn(test::kProp, e, rev, Val(22)), r.Now());
  REQUIRE_OR_RETURN_VOID(k1 && k2);
  CHECK(r.session.RunUntil(
      [&] { return FinalFor(r.session, 1, *k1) && FinalFor(r.session, 2, *k2); }, 2s));
  const auto d1 = FinalFor(r.session, 1, *k1);
  const auto d2 = FinalFor(r.session, 2, *k2);
  REQUIRE_OR_RETURN_VOID(d1 && d2);
  const bool first_won = d1->outcome == Outcome::kApplied;
  CHECK(first_won != (d2->outcome == Outcome::kApplied));  // exactly one applies
  const Decision& lost = first_won ? *d2 : *d1;
  CHECK(lost.outcome == Outcome::kRefused);
  CHECK(lost.reason == Reason::kStaleBasis);
  const Record* row = r.S().State().Get(Key(test::kProp, e));
  REQUIRE_OR_RETURN_VOID(row != nullptr);
  CHECK(lost.revision == row->revision);
  CHECK(row->revision == rev + 1);
  CHECK(row->value == (first_won ? Val(11) : Val(22)));
  // S reads its peers in id order, so peer 1's arrives first.
  CHECK(first_won);
  r.session.Run(1s);
  CHECK(Equal(r.session, 1));
  CHECK(Equal(r.session, 2));
}

BACKEND_TEST("IL2", InterleaveServerRunAndCommit) {
  // A server-run write and a client commit on one row: the commit based on the older revision is
  // refused; the row ends at S's last write on every peer.
  Rig r;
  const EntityId e = Ent(1, 2);
  r.Everywhere(e, kStart);
  r.Join(1);
  r.Join(2);
  EvaluateByWriting(r);
  const RecordKey row = Key(test::kToggle, e);
  // Not Val(1): the server-run writes Val(sequence), and an equal value would change nothing.
  const uint32_t rev = r.Put(row, Val(100));
  CHECK(r.session.RunUntil(
      [&] {
        const Record* b = r.C(2).State().Get(row);
        return b && b->revision == rev;
      },
      2s));
  const auto run = r.C(1).Send(ServerRun(test::kToggle, e), r.Now());
  REQUIRE_OR_RETURN_VOID(run.has_value());
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *run).has_value(); }, 2s));
  const auto applied = FinalFor(r.session, 1, *run);
  CHECK(applied && applied->outcome == Outcome::kApplied);
  const Record* after_run = r.S().State().Get(row);
  REQUIRE_OR_RETURN_VOID(after_run != nullptr);
  CHECK(after_run->revision == rev + 1);
  // Peer 2 still bases its commit on the older revision.
  const auto commit = r.C(2).Send(CommitOn(test::kToggle, e, rev, Val(99)), r.Now());
  REQUIRE_OR_RETURN_VOID(commit.has_value());
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 2, *commit).has_value(); }, 2s));
  const auto refused = FinalFor(r.session, 2, *commit);
  CHECK(refused && refused->outcome == Outcome::kRefused);
  CHECK(refused && refused->reason == Reason::kStaleBasis);
  CHECK(refused && refused->revision == rev + 1);
  r.session.Run(1s);
  const Record* last = r.S().State().Get(row);
  REQUIRE_OR_RETURN_VOID(last != nullptr);
  CHECK(last->value == Val(run->sequence));
  for (uint16_t peer : {1, 2}) {
    const Record* c = r.C(peer).State().Get(row);
    CHECK(c && c->revision == last->revision && c->value == last->value);
    CHECK(Equal(r.session, peer));
  }
}

BACKEND_TEST("IL3", InterleaveDelayOneLaneAtATime) {
  // A scripted run of commits, server-run writes and deltas; per seed a message-layer delay of 0 to
  // 300 ms (uniform, seeded) on one lane at a time, gameplay only, then state only: every peer's map
  // equals S's, and S's equals the undelayed run's (DiffMaps: revisions, values, tombs); each
  // request key's decision sequence equals the undelayed run's.
  struct RunResult {
    std::optional<MapDump> map;
    std::map<std::pair<uint16_t, RequestKey>, std::vector<std::tuple<int, int, bool>>> decisions;
    bool peers_equal = true;
  };
  auto run = [](std::string rules, uint64_t seed) {
    RunResult out;
    Rig r(WithFaults(std::move(rules), seed));
    for (uint32_t n = 1; n <= 30; ++n) r.Everywhere(Ent(1, n), kStart);
    r.Join(1);
    r.Join(2);
    EvaluateByWriting(r);
    r.session.Run(500ms);
    for (int tick = 0; tick < 20; ++tick) {  // every 100 ms for 2 s
      const uint32_t k = uint32_t(tick % 10);
      r.C(1).Send(Commit(test::kProp, Ent(1, 1 + k), Val(uint32_t(tick))), r.Now());
      r.C(2).Send(Commit(test::kProp, Ent(1, 11 + k), Val(uint32_t(100 + tick))), r.Now());
      if (tick % 2 == 0) r.C(1).Send(ServerRun(test::kToggle, Ent(1, 1 + k / 2)), r.Now());
      r.Put(Key(test::kProp, Ent(1, 21 + k)), Val(uint32_t(200 + tick)));
      if (tick == 15) r.S().State().Remove(Key(test::kProp, Ent(1, 25)), r.Now());
      r.session.Run(100ms);
    }
    r.session.Run(3s);
    out.map = MapOf(r.S().State());
    for (uint16_t peer : {1, 2}) {
      out.peers_equal = out.peers_equal && Equal(r.session, peer);
      for (const ClientEvent& e : r.session.ClientEvents(peer)) {
        if (e.type != ClientEvent::Type::kDecided) continue;
        out.decisions[{peer, e.decision.key}].emplace_back(int(e.decision.outcome),
                                                           int(e.decision.reason), e.decision.final);
      }
    }
    return out;
  };
  const RunResult plain = run("", 1);
  REQUIRE_OR_RETURN_VOID(plain.map.has_value());
  CHECK(plain.peers_equal);
  CHECK(plain.decisions.size() == 20 * 2 + 10);
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    for (const char* lane : {"gameplay", "state"}) {
      const RunResult delayed =
          run(std::string("message:delay lane=") + lane + " delay=0-300", uint64_t(seed));
      REQUIRE_OR_RETURN_VOID(delayed.map.has_value());
      CHECK(delayed.peers_equal);
      const auto diffs = DiffMaps(*plain.map, *delayed.map);
      for (size_t i = 0; i < diffs.size() && i < 4; ++i) {
        std::fprintf(stderr, "  seed %d %s: %s\n", seed, lane, diffs[i].text.c_str());
      }
      CHECK(diffs.empty());
      CHECK(delayed.decisions == plain.decisions);
    }
  }
}

BACKEND_TEST("IL4", InterleaveRemovedWhileChunkWaits) {
  // A row removed while its baseline chunk is waiting: absent on the client after the baseline.
  Rig r;
  FillProps(r, 1, 600);  // about 66 KB: some 1.6 s at the pacer's 40 KB/s
  r.Join(1);
  CHECK(RunUntilEvery(r.session, [&] { return Equal(r.session, 1, false); }, 5s));
  r.Join(2, /*ready=*/false);
  r.session.Run(300ms);
  r.session.LevelReady(2);
  const size_t ended_before = CountServer(r.session, ServerEvent::Type::kBaselineEnded, 2);
  CHECK(r.session.RunUntil(
      [&] { return Baselines(r.session, 2, BaselineReason::kLevelReady) == 1; }, 1s));
  // The last key in baseline order (key order for one unfenced kind): its chunk goes last.
  const RecordKey last = Key(test::kProp, Ent(1, 600));
  CHECK(r.S().State().Remove(last, r.Now()) == PutResult::kWritten);
  CHECK(r.session.RunUntil(
      [&] { return CountServer(r.session, ServerEvent::Type::kBaselineEnded, 2) > ended_before; },
      5s));
  r.session.Run(300ms);
  const Record* row = r.C(2).State().Get(last);
  CHECK(row == nullptr || row->tomb);
  CHECK(r.W(2).Value(last) == nullptr);
  CHECK(Equal(r.session, 2));
}

BACKEND_TEST("IL5", InterleaveCreatedDuringBaseline) {
  // A row created during a baseline, for a level subscription baseline and for a repair baseline:
  // present after it (a delta after `at`, into staging); no second baseline (Opus F2).
  {
    Rig r;
    FillProps(r, 1, 600);
    r.Join(1);
    r.Join(2, /*ready=*/false);
    r.session.Run(500ms);
    r.session.LevelReady(2);
    CHECK(r.session.RunUntil(
        [&] { return Baselines(r.session, 2, BaselineReason::kLevelReady) == 1; }, 1s));
    const RecordKey fresh = Key(test::kProp, Ent(1, 1000));
    r.Everywhere(Ent(1, 1000), kStart);
    CHECK(r.Put(fresh, Val(5, 64)) == 1);
    CHECK(r.session.RunUntil(
        [&] { return CountServer(r.session, ServerEvent::Type::kBaselineEnded, 2) >= 2; }, 5s));
    r.session.Run(500ms);
    const Record* row = r.C(2).State().Get(fresh);
    CHECK(row && !row->tomb && row->value == Val(5, 64));
    CHECK(Equal(r.session, 2));
    r.session.Run(3s);
    CHECK(CountServer(r.session, ServerEvent::Type::kBaselineBegun, 2) == 2);  // join, level
  }
  {
    FakeSessionOptions o = WithFaults("message:kill dir=s2c peer=2 type=delta kind=test.toggle nth=1");
    o.limits.manifest_interval = 500ms;
    Rig r(o);
    FillProps(r, 1, 600);
    r.Everywhere(Ent(1, 2000), kStart);
    r.Join(1);
    r.Join(2);
    CHECK(RunUntilEvery(r.session, [&] { return Equal(r.session, 2, false); }, 5s));
    r.Put(Key(test::kToggle, Ent(1, 2000)), Val(1, 1));  // its delta killed: a mismatch follows
    CHECK(r.session.RunUntil(
        [&] { return Baselines(r.session, 2, BaselineReason::kManifestMismatch) == 1; }, 5s));
    const size_t ended = CountServer(r.session, ServerEvent::Type::kBaselineEnded, 2);
    const RecordKey fresh = Key(test::kProp, Ent(1, 1001));
    r.Everywhere(Ent(1, 1001), kStart);
    CHECK(r.Put(fresh, Val(6, 64)) == 1);
    CHECK(r.session.RunUntil(
        [&] { return CountServer(r.session, ServerEvent::Type::kBaselineEnded, 2) > ended; }, 5s));
    r.session.Run(500ms);
    const Record* row = r.C(2).State().Get(fresh);
    CHECK(row && !row->tomb && row->value == Val(6, 64));
    r.session.Run(3s);
    CHECK(Baselines(r.session, 2, BaselineReason::kManifestMismatch) == 1);
    CHECK(Equal(r.session, 2));
  }
}

BACKEND_TEST("IL6", InterleaveSecondWaitsBehindServerWait) {
  // Two requests of one peer, the first in S's own wait (kWait): the second waits behind it;
  // decisions in arrival order (1B amendment n).
  Rig r;
  const EntityId a = Ent(1, 1), b = Ent(1, 2);
  r.Everywhere(a, kStart);
  r.Everywhere(b, kStart);
  r.Join(1);
  r.session.Run(500ms);
  const TimePoint release = r.Now() + 300ms;
  // Pure: the verdict depends on the request and the time only.
  r.authorities.authority.custom = [release](const Request& q,
                                             const DecideContext& c) -> std::optional<Verdict> {
    if (q.payload.empty() || q.payload[0] != 0xEE || c.now >= release) return std::nullopt;
    Verdict v;
    v.type = Verdict::Type::kWait;
    v.wait = 500ms;
    return v;
  };
  std::vector<std::vector<uint8_t>> payload{{0xEE, 0, 0, 0}};
  const auto first = r.C(1).Send(Commit(test::kProp, a, payload[0]), r.Now());
  const auto second = r.C(1).Send(Commit(test::kProp, b, Val(2)), r.Now());
  REQUIRE_OR_RETURN_VOID(first && second);
  CHECK(r.session.RunUntil(
      [&] { return FinalFor(r.session, 1, *first) && FinalFor(r.session, 1, *second); }, 3s));
  size_t first_at = 0, second_at = 0, i = 0;
  for (const ClientEvent& e : r.session.ClientEvents(1)) {
    ++i;
    if (e.type != ClientEvent::Type::kDecided || !e.decision.final) continue;
    if (e.decision.key == *first) first_at = i;
    if (e.decision.key == *second) second_at = i;
  }
  CHECK(first_at != 0 && second_at != 0);
  CHECK(first_at < second_at);
  const auto d1 = FinalFor(r.session, 1, *first);
  const auto d2 = FinalFor(r.session, 1, *second);
  CHECK(d1 && d1->outcome == Outcome::kApplied);
  CHECK(d2 && d2->outcome == Outcome::kApplied);
  // The second's write comes after the first's in the log.
  const Record* ra = r.S().State().Get(Key(test::kProp, a));
  const Record* rb = r.S().State().Get(Key(test::kProp, b));
  CHECK(ra && rb && ra->lsn < rb->lsn);
}

BACKEND_TEST("IL7", InterleaveLeasedEntity) {
  // A lease held by peer 1; peer 2 commits on its entity: refused `leased`.
  Rig r;
  const EntityId e = Ent(1, 3);
  r.Everywhere(e, kStart);
  r.Join(1);
  r.Join(2);
  r.session.Run(500ms);
  const auto grant = r.C(1).Send(OwnerRun(test::kProp, e), r.Now());
  REQUIRE_OR_RETURN_VOID(grant.has_value());
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *grant)) {
          if (d.outcome == Outcome::kGranted) return true;
        }
        return false;
      },
      1s));
  const auto commit = r.C(2).Send(Commit(test::kProp, e, Val(4)), r.Now());
  REQUIRE_OR_RETURN_VOID(commit.has_value());
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 2, *commit).has_value(); }, 1s));
  const auto d = FinalFor(r.session, 2, *commit);
  CHECK(d && d->outcome == Outcome::kRefused);
  CHECK(d && d->reason == Reason::kLeased);
  CHECK(r.S().State().Get(Key(test::kProp, e)) == nullptr);
}

}  // namespace skylanders::backend
