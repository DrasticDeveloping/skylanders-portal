// Limits (design section 5, LM1 to LM8, LM10, LM10b, LM10c; work items 3 and 4): the bounded
// backlog with a throttled peer, a link that answers kFull, coalescing while held, sizes and row
// limits, the key and lease caps, the paced baseline, and the long churn with its obligation
// credit, on FakeSession over MemoryLink. LM9, LM9b and LM11 (direct calls) are in
// backend_world_state_test.cpp. LM12 (ValidLimits, a direct call; council 2 B10) is the last case
// here.
//
// Seeds: a case whose run depends on the seed (LM1's writes) runs test::Seeds() seeds; the others
// are deterministic on MemoryLink (their faults take no pct and no range), so a second seed would
// repeat the first exactly, and they run once (LM10's family once more under --long, with its long
// form).

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <variant>
#include <vector>

#include "backend/fake_network.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using namespace test::lanes;

struct Rng {
  uint64_t s;
  uint64_t Next() {
    uint64_t z = (s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
  }
  uint32_t Below(uint32_t n) { return n == 0 ? 0 : uint32_t(Next() % n); }
};

// What S reported, counted as it comes (the event list grows long in a churn).
struct Tally {
  std::map<std::pair<ServerEvent::Type, uint16_t>, size_t> by_type;
  std::map<std::pair<uint16_t, BaselineReason>, size_t> baselines;
  std::map<std::pair<uint16_t, uint64_t>, std::pair<ObligationEnd, TimePoint>> ended;
  std::map<std::pair<uint16_t, ObligationEnd>, size_t> ends;
  std::function<void(const ServerEvent&)> also;

  void Install(Rig& r) {
    r.session.on_server_event = [this, &r](const ServerEvent& e) {
      ++by_type[{e.type, e.peer}];
      if (e.type == ServerEvent::Type::kBaselineBegun) ++baselines[{e.peer, e.why}];
      if (e.type == ServerEvent::Type::kObligationEnded) {
        ended.try_emplace({e.peer, e.obligation}, e.obligation_end, r.Now());
        ++ends[{e.peer, e.obligation_end}];
      }
      if (also) also(e);
    };
  }
  size_t Of(ServerEvent::Type t, uint16_t peer) const {
    auto it = by_type.find({t, peer});
    return it == by_type.end() ? 0 : it->second;
  }
  size_t Baselines(uint16_t peer, BaselineReason why) const {
    auto it = baselines.find({peer, why});
    return it == baselines.end() ? 0 : it->second;
  }
  size_t Repairs(uint16_t peer) const {
    return Baselines(peer, BaselineReason::kManifestMismatch) +
           Baselines(peer, BaselineReason::kDigestMismatch) +
           Baselines(peer, BaselineReason::kBelowFloor) + Baselines(peer, BaselineReason::kSilence);
  }
  size_t Ends(uint16_t peer, ObligationEnd end) const {
    auto it = ends.find({peer, end});
    return it == ends.end() ? 0 : it->second;
  }
};

// The bounds LM10 checks at every pump, on S and every replica.
struct Bounds {
  size_t violations = 0;
  void Check(Rig& r) {
    const Limits& l = r.session.Lim();
    const WorldState& s = r.S().State();
    const size_t max_bytes = (l.max_rows_per_level + l.max_rows_per_session) *
                             (size_t(l.max_value_bytes) + 64);
    bool ok = s.Head() < s.Floor() + l.log_retention;
    ok = ok && s.Rows(Scope::kLevel) <= l.max_rows_per_level;
    ok = ok && s.Rows(Scope::kSession) <= l.max_rows_per_session;
    ok = ok && s.Bytes() <= max_bytes;
    for (uint16_t peer : r.session.Peers()) {
      const PeerStats st = r.S().Stats(peer);
      const Replica& c = r.C(peer).State();
      ok = ok && c.Rows() <= l.max_rows_per_level + l.max_rows_per_session;
      ok = ok && c.Bytes() <= max_bytes;
      // Rows of a newer epoch waiting for their marker sit outside Rows() and Bytes() (F4; Sol
      // code review r2, R6): one entry per key, at most max_rows_per_level of them.
      ok = ok && c.FutureRows() <= l.max_rows_per_level;
      ok = ok && c.FutureBytes() <= l.max_rows_per_level * (size_t(l.max_value_bytes) + 64);
      ok = ok && st.obligations_outstanding <= l.max_obligations_per_peer;
      // The client's entries (its newest Ack, as sent) never pass C, and S never counts fewer
      // used than the client holds (Sol r3 F9, draft 5).
      auto a = r.wire.last_sent_ack.find(peer);
      if (a != r.wire.last_sent_ack.end()) {
        ok = ok && a->second.holding <= l.obligation_credit;
        ok = ok && st.credit_used >= a->second.holding;
      }
    }
    if (!ok && violations++ == 0) {
      std::fprintf(stderr, "  bounds broken at %.2f s\n", Seconds(r.Now() - kStart));
    }
  }
  // Once, at the end: no client was ever sent more than C (a defect in S it would report).
  void Final(Rig& r) {
    for (uint16_t peer : r.session.Peers()) {
      if (CountClient(r.session, peer, ClientEvent::Type::kCreditExceeded) != 0) ++violations;
    }
  }
};

// For each write to a peer, whether the peer held it within `bound`.
struct Latency {
  Duration bound{};
  std::map<std::pair<uint16_t, RecordKey>, std::pair<uint32_t, TimePoint>> pending;
  size_t late = 0;
  Duration worst{};

  void Wrote(uint16_t peer, const RecordKey& key, uint32_t revision, TimePoint at) {
    pending[{peer, key}] = {revision, at};
  }
  void Check(Rig& r) {
    for (auto it = pending.begin(); it != pending.end();) {
      const auto& [peer, key] = it->first;
      const auto& [revision, at] = it->second;
      const Record* row = r.C(peer).State().Get(key);
      if (row && row->revision >= revision) {
        worst = std::max(worst, r.Now() - at);
        it = pending.erase(it);
      } else if (r.Now() - at > bound) {
        if (late++ == 0) {
          std::fprintf(stderr, "  peer %u late by %.3f s\n", unsigned(peer), Seconds(r.Now() - at));
        }
        it = pending.erase(it);
      } else {
        ++it;
      }
    }
  }
};

Duration DeltaBound(Rig& r, uint16_t peer) {
  return r.session.Lim().batch_interval +
         std::chrono::duration_cast<Duration>(r.session.ServerEnd(peer).Rtt()) + 50ms + 20ms;
}

}  // namespace

BACKEND_TEST("LM1", LimitThrottledPeerBacklog) {
  // Rows of 32-byte values; peer 3 throttled to 2 KB/s, peers 1 and 2 not; 400 writes a second
  // spread over the rows, then quiet (--long: 1,000 rows for 60 s; default: 250 rows for 15 s).
  // During churn: peer 3's unacked state frames never pass max_unacked_state_frames; one baseline
  // at a time, at most 2 begun for it; peers 1 and 2 see each change within a batch interval, the
  // RTT and 50 ms. After churn: peer 3 equal within 90 s.
  const uint32_t rows = test::Long() ? 1000 : 250;
  const Duration churn = test::Long() ? 60s : 15s;
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Rig r(WithFaults("link:throttle peer=3 dir=s2c bps=2000", uint64_t(seed)));
    for (uint32_t n = 1; n <= rows; ++n) {
      r.Everywhere(Ent(1, n), kStart);
      r.Put(Key(test::kProp, Ent(1, n)), Val(0, 32));
    }
    Tally tally;
    bool in_churn = false, running3 = false, overlap = false;
    size_t begun_in_churn = 0;
    tally.also = [&](const ServerEvent& e) {
      if (e.peer != 3) return;
      if (e.type == ServerEvent::Type::kBaselineBegun) {
        overlap = overlap || running3;
        running3 = true;
        begun_in_churn += in_churn;
      }
      if (e.type == ServerEvent::Type::kBaselineEnded) running3 = false;
    };
    tally.Install(r);
    r.Join(1);
    r.Join(2);
    r.Join(3);
    CHECK(RunUntilEvery(r.session, [&] { return Equal(r.session, 1, false) && Equal(r.session, 2, false); }, 5s));
    Latency latency;
    latency.bound = DeltaBound(r, 1);
    Rng rng{uint64_t(seed) * 7919};
    const size_t cap = r.session.Lim().max_unacked_state_frames;
    size_t max_unacked = 0;
    uint32_t counter = 0;
    in_churn = true;
    const TimePoint end = r.Now() + churn;
    while (r.Now() < end) {
      for (int w = 0; w < 4; ++w) {  // 4 per 10 ms step: 400 a second
        const RecordKey key = Key(test::kProp, Ent(1, 1 + rng.Below(rows)));
        const uint32_t rev = r.Put(key, Val(++counter, 32));
        latency.Wrote(1, key, rev, r.Now());
        latency.Wrote(2, key, rev, r.Now());
      }
      r.session.Step();
      latency.Check(r);
      max_unacked = std::max(max_unacked, r.session.ServerEnd(3).UnackedFrames(netplay::Lane::kState));
      max_unacked = std::max(max_unacked, r.S().Stats(3).unacked_frames);
    }
    in_churn = false;
    CHECK(max_unacked <= cap);
    CHECK(latency.late == 0);
    CHECK(!overlap);
    CHECK(begun_in_churn <= 2);
    CHECK(RunUntilEvery(r.session, [&] { return Equal(r.session, 3, false); }, 90s, 500ms));
    CHECK(Equal(r.session, 3));
    CHECK(Equal(r.session, 1));
    CHECK(Equal(r.session, 2));
    CHECK(r.session.Faults()->Fired(0) >= 1);
  }
}

BACKEND_TEST("LM2", LimitLinkFullTwoSeconds) {
  // The link answers kFull for 2 s: nothing lost; sent after; one `backlog` event.
  Rig r(WithFaults("link:full peer=1 dir=s2c from=1000 ms=2000"));
  for (uint32_t n = 1; n <= 20; ++n) r.Everywhere(Ent(1, n), kStart);
  r.Join(1);
  std::vector<std::pair<TimePoint, uint64_t>> prefixes;
  r.session.before_pump = [&](TimePoint now) { prefixes.emplace_back(now, r.C(1).State().Prefix()); };
  r.session.Run(900ms);
  for (uint32_t i = 0; i < 50; ++i) {  // every 50 ms for 2.5 s, through the hold
    r.Put(Key(test::kProp, Ent(1, 1 + i % 20)), Val(i + 1));
    r.session.Run(50ms);
  }
  CHECK(RunUntilEvery(r.session, [&] { return Equal(r.session, 1, false); }, 5s));
  CHECK(Equal(r.session, 1));
  CHECK(r.session.Faults()->Fired(0) == 1);
  CHECK(CountLimit(r.session, "backlog") == 1);
  REQUIRE_OR_RETURN_VOID(!r.session.FaultEvents().empty());
  // Nothing reached the client while the link was full.
  const TimePoint fired = r.session.FaultEvents().front().at;
  std::optional<uint64_t> held;
  for (const auto& [t, prefix] : prefixes) {
    if (t < fired + 100ms || t > fired + 1900ms) continue;
    if (!held) held = prefix;
    CHECK(prefix == *held);
  }
  CHECK(held.has_value());
  for (uint32_t n = 1; n <= 20; ++n) {
    const Record* s = r.S().State().Get(Key(test::kProp, Ent(1, n)));
    const Record* c = r.C(1).State().Get(Key(test::kProp, Ent(1, n)));
    CHECK(s && c && s->revision == c->revision && s->value == c->value);
  }
}

BACKEND_TEST("LM3", LimitCoalesceWhileFull) {
  // 100 writes to one row while the link is full: one delta when it opens (the walk stops while
  // the FIFO is held, so the row coalesces in the log; LM3).
  Rig r(WithFaults("link:full peer=1 dir=s2c lane=state type=delta ms=2000"));
  const EntityId a = Ent(1, 1), b = Ent(1, 2);
  r.Everywhere(a, kStart);
  r.Everywhere(b, kStart);
  r.Join(1);
  r.session.Run(1s);
  const RecordKey bkey = Key(test::kProp, b);
  uint64_t b_deltas = 0;
  uint32_t b_revision = 0;
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer != 1 || to_server) return;
    if (auto* d = std::get_if<msg::Delta>(&m); d && d->record.key == bkey) {
      ++b_deltas;
      b_revision = d->record.revision;
    }
  };
  r.Put(Key(test::kProp, a), Val(1));  // its frame meets the full link and is held
  FaultInjector& faults = *r.session.Faults();
  CHECK(r.session.RunUntil([&] { return faults.Fired(0) == 1; }, 1s));
  const TimePoint fired = r.Now();
  for (uint32_t i = 1; i <= 100; ++i) {
    CHECK(r.Put(bkey, Val(i)) == i);
    r.session.Step();
  }
  CHECK(r.Now() < fired + 2s);
  CHECK(b_deltas == 0);
  CHECK(RunUntilEvery(r.session, [&] { return Equal(r.session, 1, false); }, 5s));
  CHECK(b_deltas == 1);
  CHECK(b_revision == 100);
  CHECK(CountLimit(r.session, "backlog") == 1);
  const Record* row = r.C(1).State().Get(bkey);
  CHECK(row && row->revision == 100 && row->value == Val(100));
}

BACKEND_TEST("LM4", LimitValueTooBig) {
  // A value over its kind's maximum: kTooBig, never sent (a Put, a commit, an obligation).
  Rig r;
  const EntityId e = Ent(1, 1);
  r.Everywhere(e, kStart);
  r.Join(1);
  r.session.Run(500ms);
  const RecordKey key = Key(test::kProp, e);  // test.prop: at most 64 bytes
  uint64_t deltas = 0;
  r.wire.on_message = [&](uint16_t, bool to_server, const Message& m) {
    if (auto* d = std::get_if<msg::Delta>(&m); d && !to_server && d->record.key == key) ++deltas;
  };
  const uint64_t head = r.S().State().Head();
  CHECK(r.S().State().Put(key, std::vector<uint8_t>(65, 1), r.Now()) == PutResult::kTooBig);
  CHECK(r.S().State().Head() == head);
  CHECK(r.S().State().Get(key) == nullptr);
  const auto k = r.C(1).Send(Commit(test::kProp, e, std::vector<uint8_t>(65, 2)), r.Now());
  REQUIRE_OR_RETURN_VOID(k.has_value());
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *k).has_value(); }, 1s));
  const auto d = FinalFor(r.session, 1, *k);
  CHECK(d && d->outcome == Outcome::kRefused && d->reason == Reason::kLimit);
  CHECK(r.S().Oblige(1, Key(test::kEvent, e), std::vector<uint8_t>(17, 3)) == 0);  // at most 16
  r.session.Run(1s);  // the session collects S's events at its next step
  CHECK(CountLimit(r.session, "test.event") == 1);
  CHECK(deltas == 0);
  CHECK(r.C(1).State().Get(key) == nullptr);
  CHECK(r.wire.Count(1, false, MessageType::kObligation) == 0);
}

BACKEND_TEST("LM5", LimitLevelRows) {
  // Rows past max_rows_per_level: kRowLimit, an event naming the kind; never silent.
  FakeSessionOptions o;
  o.limits.max_rows_per_level = 8;
  Rig r(o);
  r.Join(1);
  r.session.Run(300ms);
  for (uint32_t n = 1; n <= 8; ++n) {
    CHECK(r.S().State().Put(Key(test::kProp, Ent(1, n)), Val(n), r.Now()) == PutResult::kWritten);
  }
  CHECK(r.S().State().Put(Key(test::kProp, Ent(1, 9)), Val(9), r.Now()) == PutResult::kRowLimit);
  CHECK(r.S().State().Rows(Scope::kLevel) == 8);
  CHECK(r.S().State().Get(Key(test::kProp, Ent(1, 9))) == nullptr);
  // The request path: refused `limit`, and S says which kind.
  const auto k = r.C(1).Send(Commit(test::kProp, Ent(1, 10), Val(10)), r.Now());
  REQUIRE_OR_RETURN_VOID(k.has_value());
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *k).has_value(); }, 1s));
  const auto d = FinalFor(r.session, 1, *k);
  CHECK(d && d->outcome == Outcome::kRefused && d->reason == Reason::kLimit);
  CHECK(CountLimit(r.session, "test.prop") >= 1);
  CHECK(r.S().State().Rows(Scope::kLevel) == 8);
}

BACKEND_TEST("LM5b", LimitSessionRows) {
  // Session rows past max_rows_per_session: kRowLimit, an event naming the kind and the scope.
  FakeSessionOptions o;
  o.limits.max_rows_per_session = 4;
  Rig r(o);
  r.Join(1);
  r.session.Run(300ms);
  for (uint32_t n = 1; n <= 4; ++n) {
    CHECK(r.S().State().Put(Key(kCounter, SessionObject(n)), Val(n), r.Now()) == PutResult::kWritten);
  }
  CHECK(r.S().State().Put(Key(kCounter, SessionObject(5)), Val(5), r.Now()) == PutResult::kRowLimit);
  // A tomb is still a row (tombs counted, Sol F9).
  CHECK(r.S().State().Remove(Key(kCounter, SessionObject(1)), r.Now()) == PutResult::kWritten);
  CHECK(r.S().State().Put(Key(kCounter, SessionObject(6)), Val(6), r.Now()) == PutResult::kRowLimit);
  CHECK(r.S().State().Rows(Scope::kSession) == 4);
  const auto k = r.C(1).Send(Commit(kCounter, SessionObject(7), Val(7)), r.Now());
  REQUIRE_OR_RETURN_VOID(k.has_value());
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *k).has_value(); }, 1s));
  const auto d = FinalFor(r.session, 1, *k);
  CHECK(d && d->outcome == Outcome::kRefused && d->reason == Reason::kLimit);
  bool named = false;
  for (const ServerEvent& e : r.session.ServerEvents()) {
    named = named || (e.type == ServerEvent::Type::kLimit &&
                      e.text.find("test.counter") != std::string::npos &&
                      e.text.find("session") != std::string::npos);
  }
  CHECK(named);
}

BACKEND_TEST("LM6", LimitOpenKeys) {
  // 1,025 open keys from one peer. The client admits max_keys_per_peer open keys and refuses the
  // next itself: nullopt, kKeyBound, no key assigned, nothing sent (Sol code review r3, N1). S
  // keeps 1B's rule for a sender that bypasses Client: a raw 1,025th key is refused `rate`, final
  // (requests.h kMaxKeysPerPeer).
  Rig r;
  const EntityId e = Ent(1, 1);
  r.Everywhere(e, kStart);
  r.Join(1);
  r.session.Run(300ms);
  const size_t cap = r.session.Lim().max_keys_per_peer;
  std::vector<RequestKey> keys;
  for (size_t i = 0; i < cap; ++i) {  // never evaluated: every key stays open
    const auto k = r.C(1).Send(ServerRun(test::kToggle, e), r.Now());
    REQUIRE_OR_RETURN_VOID(k.has_value());
    keys.push_back(*k);
  }
  SendRefusal why = SendRefusal::kNone;
  CHECK(!r.C(1).Send(ServerRun(test::kToggle, e), r.Now(), &why).has_value());
  CHECK(why == SendRefusal::kKeyBound);
  CHECK(r.C(1).OpenKeys() == cap);
  r.session.Run(300ms);  // the client's frames are acked: the link has room
  // The raw 1,025th key, past the client's admission.
  Request q = ServerRun(test::kToggle, e);
  const RequestKey raw{1, r.C(1).PeerEpoch(), r.session.Generation(), uint32_t(cap + 100)};
  q.key = raw;
  std::vector<uint8_t> frame;
  CHECK(AppendMessage(msg::Request{q}, frame));
  CHECK(r.session.ClientEnd(1).Send(netplay::Lane::kGameplay, frame) == LinkSend::kSent);
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, raw).has_value(); }, 2s));
  const auto last = FinalFor(r.session, 1, raw);
  CHECK(last && last->outcome == Outcome::kRefused && last->reason == Reason::kRate && last->final);
  size_t rate_refused = 0;
  for (size_t i = 0; i < cap; ++i) {
    const auto d = FinalFor(r.session, 1, keys[i]);
    rate_refused += d && d->reason == Reason::kRate;
  }
  CHECK(rate_refused == 0);
  // S holds the client's keys only: the refused Send never reached it, the raw key is not stored.
  CHECK(r.S().Requests().KeysOf(1, r.C(1).PeerEpoch()) == cap);
}

BACKEND_TEST("LM7", LimitFifthLease) {
  // A fifth lease for one peer: refused `limit`.
  Rig r;
  for (uint32_t n = 1; n <= 5; ++n) r.Everywhere(Ent(1, n), kStart);
  r.Join(1);
  r.session.Run(300ms);
  std::vector<RequestKey> keys;
  for (uint32_t n = 1; n <= 5; ++n) {
    const auto k = r.C(1).Send(OwnerRun(test::kProp, Ent(1, n)), r.Now());
    REQUIRE_OR_RETURN_VOID(k.has_value());
    keys.push_back(*k);
  }
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, keys[4]).has_value(); }, 2s));
  size_t granted = 0;
  for (size_t i = 0; i < 4; ++i) {
    for (const Decision& d : DecisionsFor(r.session, 1, keys[i])) granted += d.outcome == Outcome::kGranted;
  }
  CHECK(granted == 4);
  const auto fifth = FinalFor(r.session, 1, keys[4]);
  CHECK(fifth && fifth->outcome == Outcome::kRefused && fifth->reason == Reason::kLimit);
}

BACKEND_TEST("LM8", LimitPacedBaseline) {
  // A 300 KB baseline (peer 3's session baseline: about 3,900 rows of 32 bytes of the mirrored
  // test.tracker): paced at baseline_bytes_per_second within one batch; the other peers' deltas
  // keep their latency meanwhile.
  FakeSessionOptions o;
  o.limits.max_rows_per_session = 8192;
  Rig r(o);
  const EntityId t = Ent(1, 1);
  r.Everywhere(t, kStart);
  r.Join(1);
  r.Join(2);
  constexpr uint32_t kRows = 3900;
  for (uint32_t n = 1; n <= kRows; ++n) {
    CHECK(r.S().State().Put(Key(test::kTracker, SessionObject(n)), Val(n, 32), r.Now()) ==
          PutResult::kWritten);
  }
  CHECK(RunUntilEvery(r.session, [&] { return Equal(r.session, 1, false) && Equal(r.session, 2, false); }, 30s, 500ms));
  // Peer 3's baseline frames, by time.
  std::optional<TimePoint> begun, ended;
  Tally tally;
  tally.also = [&](const ServerEvent& e) {
    if (e.peer != 3 || e.why != BaselineReason::kJoin) return;
    if (e.type == ServerEvent::Type::kBaselineBegun && !begun) begun = r.Now();
  };
  tally.Install(r);
  std::vector<std::pair<TimePoint, size_t>> chunks;
  auto tap = r.session.on_frame;
  r.session.on_frame = [&, tap](uint16_t peer, bool to_server, netplay::Lane lane,
                                std::span<const uint8_t> frame) {
    if (tap) tap(peer, to_server, lane, frame);
    if (peer != 3 || to_server) return;
    std::vector<Message> m;
    DecodeFrame(frame, m);
    size_t rows_bytes = 0;
    for (const Message& x : m) {
      if (std::holds_alternative<msg::BaselineRows>(x)) rows_bytes = frame.size();
      if (std::holds_alternative<msg::BaselineEnd>(x) && !ended) ended = r.Now();
    }
    if (rows_bytes) chunks.emplace_back(r.Now(), rows_bytes);
  };
  r.Join(3, /*ready=*/false);
  Latency latency;
  latency.bound = DeltaBound(r, 1);
  uint32_t v = 0;
  // A toggle every 100 ms on the other peers' level scope while the baseline runs.
  const TimePoint stop = r.Now() + 20s;
  TimePoint next_write = r.Now();
  while (r.Now() < stop && !ended) {
    if (r.Now() >= next_write) {
      const uint32_t rev = r.Put(Key(test::kToggle, t), Val(++v, 1));
      latency.Wrote(1, Key(test::kToggle, t), rev, r.Now());
      latency.Wrote(2, Key(test::kToggle, t), rev, r.Now());
      next_write += 100ms;
    }
    r.session.Step();
    latency.Check(r);
  }
  REQUIRE_OR_RETURN_VOID(begun && ended);
  size_t total = 0;
  const Limits& l = r.session.Lim();
  const double rate = double(l.baseline_bytes_per_second);
  const double slack = rate * std::chrono::duration<double>(l.batch_interval).count() + 2.0 * kMaxFrameBytes;
  for (const auto& [at, bytes] : chunks) {
    total += bytes;
    CHECK(double(total) <= rate * Seconds(at - *begun) + slack);
  }
  CHECK(total >= 290 * 1024);
  const double took = Seconds(*ended - *begun);
  CHECK(took >= double(total) / rate - 0.5);
  CHECK(took <= double(total) / rate + 1.5);
  CHECK(latency.late == 0);
  r.session.Run(1s);
  CHECK(Equal(r.session, 3));
}

namespace {

// LM10's churn: level and session rows, an obligation for each peer every second from 5 s (each
// fulfilled), a commit every second, peer 3 rejoining at every `rejoin_every`; the bounds checked
// at every pump.
struct ChurnPlan {
  Duration length = 60s;
  Duration rejoin_every = 30s;
  bool rejoin = true;
};
struct ChurnResult {
  size_t violations = 0;
  std::map<uint16_t, size_t> made;  // obligations made per peer (peer 1 and 2: one peer epoch)
  std::vector<uint32_t> epochs3;    // every peer epoch peer 3 had, in order
};

ChurnResult Churn(Rig& r, const ChurnPlan& plan) {
  ChurnResult out;
  for (uint32_t n = 1; n <= 50; ++n) r.Everywhere(Ent(1, n), kStart);
  for (uint32_t n = 500; n < 520; ++n) r.Everywhere(Ent(1, n), kStart);
  r.Join(1);
  r.Join(2);
  r.Join(3);
  Bounds bounds;
  Rng rng{17};
  const TimePoint start = r.Now();
  TimePoint next_second = start + 1s, next_rejoin = start + plan.rejoin_every;
  uint32_t second = 0, v = 0;
  while (r.Now() < start + plan.length) {
    r.Put(Key(test::kProp, Ent(1, 1 + rng.Below(50))), Val(++v, 16));
    if (r.Now() >= next_second) {
      ++second;
      next_second += 1s;
      for (uint32_t n = 1; n <= 4; ++n) r.Put(Key(kCounter, SessionObject(n)), Val(second * 10 + n));
      if (second >= 5) {
        for (uint16_t peer : r.session.Peers()) {
          if (r.S().Oblige(peer, Key(test::kEvent, Ent(1, 500 + second % 20)), Val(second, 2))) {
            ++out.made[peer];
          }
        }
      }
      r.C(2).Send(Commit(test::kProp, Ent(1, 1 + second % 50), Val(1000 + second, 16)), r.Now());
      // Peer 3's keys too, so a departed peer epoch had some to drop.
      if (r.session.Has(3)) {
        r.C(3).Send(Commit(test::kProp, Ent(1, 1 + (second + 7) % 50), Val(2000 + second, 16)),
                    r.Now());
      }
    }
    if (r.session.Has(3) && r.C(3).PeerEpoch() != 0 &&
        (out.epochs3.empty() || out.epochs3.back() != r.C(3).PeerEpoch())) {
      out.epochs3.push_back(r.C(3).PeerEpoch());
    }
    if (plan.rejoin && r.Now() >= next_rejoin) {
      next_rejoin += plan.rejoin_every;
      r.wire.last_sent_ack.erase(3);
      r.Join(3);  // a new peer epoch: nothing of the old one carries over
    }
    r.session.Step();
    bounds.Check(r);
  }
  r.session.Run(3s);
  bounds.Check(r);
  bounds.Final(r);
  out.violations = bounds.violations;
  return out;
}

}  // namespace

BACKEND_TEST("LM10", LimitLongChurnBounded) {
  // 10 minutes of churn in fake time (--long; the default run 60 s): the log, the rows and the
  // bytes within their bounds on S and every replica; the first obligation's completion report
  // killed once at 5 s, reported again within obligation_report_interval, ended kCompleted, the
  // floor past it; S within max_obligations_per_peer, each client within C, S's used never below
  // a client's count.
  FakeSessionOptions o = WithFaults("message:kill dir=c2s peer=1 type=obligation_done id=1 nth=1");
  o.step = 20ms;
  Rig r(o);
  Tally tally;
  tally.Install(r);
  ChurnPlan plan;
  plan.length = test::Long() ? 600s : 60s;
  plan.rejoin_every = test::Long() ? 60s : 30s;
  const ChurnResult result = Churn(r, plan);
  CHECK(result.violations == 0);
  FaultInjector& faults = *r.session.Faults();
  CHECK(faults.Fired(0) == 1);
  REQUIRE_OR_RETURN_VOID(!r.session.FaultEvents().empty());
  const TimePoint killed = r.session.FaultEvents().front().at;
  auto it = tally.ended.find({1, 1});
  REQUIRE_OR_RETURN_VOID(it != tally.ended.end());
  CHECK(it->second.first == ObligationEnd::kCompleted);
  CHECK(it->second.second - killed <= r.session.Lim().obligation_report_interval + 200ms);
  CHECK(r.S().Stats(1).obligation_floor > 1);
  for (const auto& [peer, w] : r.worlds) CHECK(w->MaxFulfilledOnce() <= 1);
  CHECK(r.W(1).FulfilledTotal() == result.made.at(1));
  CHECK(r.wire.max_holding[1] <= r.session.Lim().obligation_credit);
  // The request table holds no key of a departed peer epoch (every rejoin of peer 3 dropped its
  // keys, Opus F9), and no lease or lease tomb is left (none was asked for). No generation ends in
  // this churn, so no key of an ended generation can be held: generation 0 never had one.
  CHECK(result.epochs3.size() >= (test::Long() ? 10u : 2u));
  for (size_t i = 0; i + 1 < result.epochs3.size(); ++i) {
    CHECK(r.S().Requests().KeysOf(3, result.epochs3[i]) == 0);
  }
  CHECK(r.S().Requests().KeysOfGeneration(0) == 0);
  CHECK(r.S().Leases().Size() == 0);
}

BACKEND_TEST("LM10b", LimitCreditThroughRepairs) {
  // (a) Sol r3's case: C = 8, max_obligations_per_peer 32, the event kind's grace 5 s. 8 sent with
  // their entities due at 4 s; repair baselines (a killed delta, so the manifest mismatches) near
  // 1 s, 2.5 s and 3.5 s; 8 more made at 1.5 s and 30 more at 3 s, their entities there at once.
  {
    FakeSessionOptions o = WithFaults("message:kill dir=s2c peer=1 type=delta kind=test.toggle");
    o.limits.obligation_credit = 8;
    o.limits.max_obligations_per_peer = 32;
    o.limits.manifest_interval = 200ms;
    Rig r(o);
    Tally tally;
    tally.Install(r);
    const EntityId toggle = Ent(1, 900);
    r.Everywhere(toggle, kStart);
    for (uint32_t n = 200; n < 238; ++n) r.Everywhere(Ent(1, n), kStart);
    r.Join(1);
    r.session.Run(500ms);
    const TimePoint t0 = r.Now();
    for (uint32_t n = 100; n < 108; ++n) r.W(1).Exists(Ent(1, n), t0 + 4s);  // late, inside 5 s
    std::vector<uint64_t> ids;
    std::set<uint64_t> received;
    uint64_t last_received = 0;
    bool rising = true;
    r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
      if (peer != 1 || to_server) return;
      if (auto* ob = std::get_if<msg::Obligation>(&m)) {
        rising = rising && ob->id > last_received;
        last_received = ob->id;
        received.insert(ob->id);
      }
    };
    for (uint32_t n = 100; n < 108; ++n) ids.push_back(r.S().Oblige(1, Key(test::kEvent, Ent(1, n)), Val(n, 2)));
    CHECK(std::count(ids.begin(), ids.end(), 0) == 0);
    size_t refused_at_3s = 0, obligations_before_4s = 0, pauses_before_4s = 0, repairs_before_4s = 0;
    Bounds bounds;
    bool kill1 = false, kill2 = false, kill3 = false, made15 = false, made3 = false, at4 = false;
    uint32_t v = 0;
    while (r.Now() < t0 + 15s) {
      const Duration t = r.Now() - t0;
      auto kill_at = [&](bool& done, Duration when) {
        if (!done && t >= when) {
          done = true;
          r.Put(Key(test::kToggle, toggle), Val(++v, 1));  // its delta is killed: a repair follows
        }
      };
      kill_at(kill1, 700ms);
      kill_at(kill2, 2200ms);
      kill_at(kill3, 3200ms);
      if (!made15 && t >= 1500ms) {
        made15 = true;
        for (uint32_t n = 200; n < 208; ++n) CHECK(r.S().Oblige(1, Key(test::kEvent, Ent(1, n)), Val(n, 2)) != 0);
      }
      if (!made3 && t >= 3s) {
        made3 = true;
        for (uint32_t n = 208; n < 238; ++n) {
          refused_at_3s += r.S().Oblige(1, Key(test::kEvent, Ent(1, n)), Val(n, 2)) == 0;
        }
      }
      if (!at4 && t >= 4s) {
        at4 = true;
        obligations_before_4s = r.wire.Count(1, false, MessageType::kObligation);
        pauses_before_4s = tally.Of(ServerEvent::Type::kCreditPaused, 1);
        repairs_before_4s = tally.Baselines(1, BaselineReason::kManifestMismatch);
      }
      r.session.Step();
      bounds.Check(r);
      CHECK(r.S().Stats(1).obligations_outstanding <= 32);
    }
    bounds.Final(r);
    CHECK(bounds.violations == 0);
    CHECK(r.wire.max_holding[1] <= 8);
    CHECK(obligations_before_4s == 8);
    CHECK(pauses_before_4s == 1);
    CHECK(repairs_before_4s >= 2);
    CHECK(tally.Ends(1, ObligationEnd::kRetiredBaseline) == 8);  // the first repair's; later ones none
    CHECK(refused_at_3s == 6);
    CHECK(CountLimit(r.session, "max_obligations_per_peer peer 1") == 6);
    CHECK(received.size() == 8 + 8 + 24);
    CHECK(rising);  // in id order, none sent twice
    CHECK(r.W(1).FulfilledTotal() == 40);
    CHECK(r.W(1).MaxFulfilledOnce() == 1);
    CHECK(r.S().Stats(1).obligation_reports_ignored == 8);
    CHECK(r.S().Stats(1).obligations_outstanding == 0);
  }
  // (b) Draft 4's case: LM10's churn with every completion report of id 1 killed and no rejoin of
  // that peer: id 1 stays reported on the client and outstanding on S, costing one credit; the
  // others flow on the other C - 1, with no stall and no rebaseline.
  {
    FakeSessionOptions o = WithFaults("message:kill dir=c2s peer=1 type=obligation_done id=1");
    o.step = 20ms;
    Rig r(o);
    Tally tally;
    tally.Install(r);
    ChurnPlan plan;
    plan.length = test::Long() ? 600s : 60s;
    plan.rejoin_every = test::Long() ? 60s : 30s;  // peer 3 rejoins; peer 1 never
    const ChurnResult result = Churn(r, plan);
    CHECK(result.violations == 0);
    CHECK(!tally.ended.contains({1, 1}));
    CHECK(r.S().Stats(1).obligation_floor == 1);
    CHECK(r.S().Stats(1).credit_used >= 1);
    CHECK(r.W(1).Fulfilled(1) == 1);
    CHECK(r.W(1).MaxFulfilledOnce() == 1);
    CHECK(tally.Ends(1, ObligationEnd::kCompleted) + 1 == result.made.at(1));
    CHECK(tally.Repairs(1) == 0);
    CHECK(r.wire.max_holding[1] <= r.session.Lim().obligation_credit);
    CHECK(r.session.Faults()->Fired(0) >= 50);  // repeated every second, killed every time
  }
}

BACKEND_TEST("LM10c", LimitPeerStopsAcking) {
  // A peer that stops acking: obligations for peers 1 and 2 every 100 ms, fulfilled at once; C =
  // 16 and max_obligations_per_peer 64; from 10 s every state message from peer 2 to S killed (Ack,
  // ObligationDone, Mismatch); at 80 s (a) the kills stop, or (b) peer 2 leaves.
  for (const bool leaves : {false, true}) {
    FakeSessionOptions o = WithFaults(leaves ? "message:kill dir=c2s peer=2 lane=state from=10000"
                                             : "message:kill dir=c2s peer=2 lane=state from=10000 to=80000");
    o.limits.obligation_credit = 16;
    o.limits.max_obligations_per_peer = 64;
    o.step = 20ms;
    Rig r(o);
    Tally tally;
    tally.Install(r);
    for (uint32_t n = 1; n <= 20; ++n) r.Everywhere(Ent(1, n), kStart);
    r.Join(1);
    r.Join(2);
    std::map<uint16_t, uint64_t> last_id;
    bool rising = true;
    r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
      if (to_server) return;
      if (auto* ob = std::get_if<msg::Obligation>(&m)) {
        rising = rising && ob->id > last_id[peer];
        last_id[peer] = ob->id;
      }
    };
    Bounds bounds;
    size_t refused2 = 0, outstanding_at_20 = 0, outstanding_at_79 = 0, pauses_at_10 = 0,
           pauses_at_80 = 0, completed1_at_20 = 0, completed1_at_80 = 0, dones_at_40 = 0,
           dones_at_50 = 0, used_after_silence = 0, sent_at_60 = 0;
    uint32_t k = 0;
    TimePoint next = kStart + 1s;
    auto t = [&] { return r.Now() - kStart; };
    auto mark = [&](Duration when, auto&& f) {
      if (t() >= when && t() < when + o.step) f();
    };
    while (t() < 80s) {
      if (r.Now() >= next) {
        next += 100ms;
        ++k;
        r.S().Oblige(1, Key(test::kEvent, Ent(1, 1 + k % 20)), Val(k, 2));
        refused2 += r.S().Oblige(2, Key(test::kEvent, Ent(1, 1 + k % 20)), Val(k, 2)) == 0;
      }
      mark(10s, [&] { pauses_at_10 = r.S().Stats(2).credit_pauses; });
      mark(20s, [&] {
        outstanding_at_20 = r.S().Stats(2).obligations_outstanding;
        completed1_at_20 = tally.Ends(1, ObligationEnd::kCompleted);
      });
      mark(40s, [&] { dones_at_40 = r.wire.Sent(2, MessageType::kObligationDone); });
      mark(50s, [&] { dones_at_50 = r.wire.Sent(2, MessageType::kObligationDone); });
      mark(79s, [&] { outstanding_at_79 = r.S().Stats(2).obligations_outstanding; });
      mark(60s, [&] {  // paused since about 10 s, before the silence baseline (about 70 s)
        const PeerStats st = r.S().Stats(2);
        sent_at_60 = st.obligations_outstanding - st.obligations_waiting;
      });
      r.session.Step();
      bounds.Check(r);
      if (tally.Baselines(2, BaselineReason::kSilence) > 0 && t() > 75s) {
        used_after_silence = std::max(used_after_silence, r.S().Stats(2).credit_used);
      }
    }
    pauses_at_80 = r.S().Stats(2).credit_pauses;
    completed1_at_80 = tally.Ends(1, ObligationEnd::kCompleted);
    bounds.Final(r);
    CHECK(bounds.violations == 0);
    CHECK(pauses_at_80 - pauses_at_10 == 1);  // one credit event
    CHECK(r.wire.max_holding[2] <= 16);
    CHECK(outstanding_at_20 == 64);  // S's table for peer 2 full, and staying so
    CHECK(outstanding_at_79 == 64);
    CHECK(refused2 > 0);
    CHECK(CountLimit(r.session, "max_obligations_per_peer peer 2") == refused2);
    CHECK(tally.Baselines(2, BaselineReason::kSilence) >= 1);  // about 70 s
    // The silence baseline retires the sent ones: up to 16 (the newest Ack S read may count an
    // entry whose ObligationDone S had already taken, so `used` can be one above them).
    CHECK(tally.Ends(2, ObligationEnd::kRetiredBaseline) == sent_at_60);
    CHECK(sent_at_60 >= 15 && sent_at_60 <= 16);
    CHECK(used_after_silence >= 16);  // a baseline returns no credit
    CHECK(dones_at_50 - dones_at_40 <= 16 * 10 + 16);  // the reports repeat, bounded
    CHECK(completed1_at_80 - completed1_at_20 >= 500);  // peer 1 flows unchanged (10 a second)
    if (!leaves) {
      // The kills stop: the Acks return the credit; the waiting go out in id order, each once.
      r.session.Run(20s);
      CHECK(r.S().Stats(2).obligations_outstanding == 0);
      CHECK(r.S().Stats(2).credit_used == 0);
      CHECK(r.W(2).MaxFulfilledOnce() == 1);
      CHECK(rising);
    } else {
      const size_t outstanding = r.S().Stats(2).obligations_outstanding;
      r.session.Leave(2);
      r.session.Run(100ms);
      CHECK(tally.Ends(2, ObligationEnd::kRetiredPeerLeft) == outstanding);
      CHECK(r.S().Stats(2).obligations_outstanding == 0);
      CHECK(r.S().Stats(2).credit_used == 0);
    }
  }
}

// LM12 (council 2 B10): ValidLimits, a direct call. The defaults keep the ranges; a credit of 0 or
// above max_obligations_per_peer fails, and so does a state-lane bound above netplay's window; the
// edges themselves pass.
BACKEND_TEST("LM12", LimitRanges) {
  CHECK(ValidLimits(Limits{}));
  Limits l;
  l.obligation_credit = 0;
  CHECK(!ValidLimits(l));
  l.obligation_credit = 1;
  CHECK(ValidLimits(l));
  l.obligation_credit = l.max_obligations_per_peer;
  CHECK(ValidLimits(l));
  l.obligation_credit = l.max_obligations_per_peer + 1;
  CHECK(!ValidLimits(l));
  l = Limits{};
  l.max_unacked_state_frames = netplay::kLaneWindow;
  CHECK(ValidLimits(l));
  l.max_unacked_state_frames = netplay::kLaneWindow + 1;
  CHECK(!ValidLimits(l));
}

}  // namespace skylanders::backend
