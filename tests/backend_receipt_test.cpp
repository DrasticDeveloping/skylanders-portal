// Receipt versus completion (design section 5, RC1 to RC5; work items 3 and 4): on FakeSession,
// over MemoryLink, with the fault injector playing the delays and kills.

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <optional>
#include <set>
#include <variant>
#include <vector>

#include "backend/fake_network.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using namespace test::lanes;

// The first kObligationEnded for (peer, id), if any.
std::optional<ServerEvent> EndOf(FakeSession& s, uint16_t peer, uint64_t id) {
  for (const ServerEvent& e : s.ServerEvents()) {
    if (e.type == ServerEvent::Type::kObligationEnded && e.peer == peer && e.obligation == id) {
      return e;
    }
  }
  return std::nullopt;
}

}  // namespace

BACKEND_TEST("RC1", ReceiptBeforeCompletion) {
  // A commit while the state lane is 300 ms late against gameplay: the client logs `decided` at
  // once and `completed` only when its prefix reaches at_lsn.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Rig r(WithFaults("message:delay lane=state dir=s2c peer=1 delay=250-350", uint64_t(seed)));
    const EntityId e = Ent(1, 1);
    r.Everywhere(e, kStart);
    r.Join(1);
    r.session.Run(2s);
    std::optional<RequestKey> key;
    TimePoint decided{}, completed{};
    uint64_t at_lsn = 0, prefix_at_decided = 0, prefix_at_completed = 0;
    bool completed_with_decision = false;
    r.session.on_client_event = [&](uint16_t peer, const ClientEvent& ev) {
      if (peer != 1 || !key) return;
      if (ev.type == ClientEvent::Type::kDecided && ev.decision.key == *key && ev.decision.final) {
        decided = r.Now();
        at_lsn = ev.decision.at_lsn;
        prefix_at_decided = r.C(1).State().Prefix();
        CHECK(ev.decision.outcome == Outcome::kApplied);
      }
      if (ev.type == ClientEvent::Type::kCompleted && ev.key == *key) {
        completed = r.Now();
        completed_with_decision = decided != TimePoint{};
        prefix_at_completed = r.C(1).State().Prefix();
      }
    };
    key = r.C(1).Send(Commit(test::kProp, e, Val(5)), r.Now());
    REQUIRE_OR_RETURN_VOID(key.has_value());
    CHECK(r.session.RunUntil([&] { return completed != TimePoint{}; }, 3s));
    CHECK(decided != TimePoint{});
    CHECK(completed_with_decision);
    CHECK(at_lsn > 0);
    CHECK(prefix_at_decided < at_lsn);
    CHECK(prefix_at_completed >= at_lsn);
    CHECK(completed - decided >= 150ms);  // the state lane's delay, less a batch at most
    const Record* row = r.C(1).State().Get(Key(test::kProp, e));
    CHECK(row && row->value == Val(5));
    CHECK(r.session.Faults()->Fired(0) > 0);
  }
}

BACKEND_TEST("RC2", ReceiptPendingThenGenerationEnded) {
  // A server-run request whose evaluation never comes, then a generation change: `pending`, then
  // S's final `stale generation`; a late SubmitEvaluation is dropped; nothing applied.
  Rig r;
  const EntityId e = Ent(1, 1);
  r.Everywhere(e, kStart);
  r.Join(1);
  r.session.Run(1s);
  std::optional<Request> evaluate;
  r.session.on_server_event = [&](const ServerEvent& ev) {
    if (ev.type == ServerEvent::Type::kEvaluate) evaluate = ev.request;
  };
  const auto key = r.C(1).Send(ServerRun(test::kToggle, e), r.Now());
  REQUIRE_OR_RETURN_VOID(key.has_value());
  CHECK(r.session.RunUntil([&] { return evaluate.has_value(); }, 1s));
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *key)) {
          if (d.outcome == Outcome::kPending && !d.final) return true;
        }
        return false;
      },
      1s));
  CHECK(!FinalFor(r.session, 1, *key));
  r.session.NewGeneration();
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *key).has_value(); }, 1s));
  const auto final = FinalFor(r.session, 1, *key);
  CHECK(final && final->outcome == Outcome::kRefused);
  CHECK(final && final->reason == Reason::kStaleGeneration);
  REQUIRE_OR_RETURN_VOID(evaluate.has_value());
  CHECK(evaluate->key == *key);
  CHECK(!r.S().SubmitEvaluation(evaluate->key, EvaluationResult{}, r.Now()));
  r.session.Run(200ms);
  CHECK(CountServer(r.session, ServerEvent::Type::kLateEvaluationDropped) == 1);
  CHECK(r.S().State().Get(Key(test::kToggle, e)) == nullptr);
  CHECK(r.C(1).State().Get(Key(test::kToggle, e)) == nullptr);
}

BACKEND_TEST("RC3", ReceiptObligationReportKilledOnce) {
  // An obligation delivered and acked by the lane, fulfilled once; its first completion report
  // killed above the lane. It stays outstanding until the client's repeat one
  // obligation_report_interval later, then ends kCompleted and is acked; S never resends it.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    Rig r(WithFaults("message:kill dir=c2s peer=1 type=obligation_done id=1 nth=1", uint64_t(seed)));
    const EntityId e = Ent(1, 9);
    r.Everywhere(e, kStart);
    r.Join(1);
    r.session.Run(1s);
    const uint64_t id = r.S().Oblige(1, Key(test::kEvent, e), Val(3, 2));
    CHECK(id == 1);
    FaultInjector& faults = *r.session.Faults();
    CHECK(r.session.RunUntil([&] { return faults.Fired(0) == 1; }, 2s));
    const TimePoint killed = r.Now();
    CHECK(r.W(1).Fulfilled(1) == 1);
    CHECK(r.S().Stats(1).obligations_outstanding == 1);
    CHECK(!EndOf(r.session, 1, 1));
    CHECK(r.session.RunUntil([&] { return EndOf(r.session, 1, 1).has_value(); }, 2s));
    const auto end = EndOf(r.session, 1, 1);
    CHECK(end && end->obligation_end == ObligationEnd::kCompleted);
    const Duration waited = r.Now() - killed;
    CHECK(waited >= r.session.Lim().obligation_report_interval - 50ms);
    CHECK(waited <= r.session.Lim().obligation_report_interval + 300ms);
    r.session.Run(3s);
    CHECK(r.wire.Count(1, false, MessageType::kObligation) == 1);  // never resent
    CHECK(r.W(1).Fulfilled(1) == 1);
    CHECK(r.wire.Count(1, false, MessageType::kObligationAck) >= 1);
    CHECK(r.S().Stats(1).obligations_outstanding == 0);
    CHECK(r.S().Stats(1).obligation_floor > 1);
    CHECK(r.wire.last_sent_ack[1].holding == 0);  // the ack let the entry go
  }
}

BACKEND_TEST("RC4", ReceiptEveryReportKilledThenRebaseline) {
  // RC3 with every completion report of it killed, then the peer rebaselined (a killed delta, so the
  // manifest mismatches): the obligation ends kRetiredBaseline; the mark ObligationAck{0, floor,
  // last_sent} after BaselineEnd puts it below the floor, the client drops the entry, stops
  // repeating, and its next Ack returns the credit (draft 5). Fulfilled exactly once.
  for (int seed = 1; seed <= test::Seeds(); ++seed) {
    FakeSessionOptions o = WithFaults(
        "message:kill dir=c2s peer=1 type=obligation_done id=1 | "
        "message:kill dir=s2c peer=1 type=delta kind=test.toggle nth=1",
        uint64_t(seed));
    o.limits.manifest_interval = 500ms;
    Rig r(o);
    const EntityId e = Ent(1, 9), t = Ent(1, 10);
    r.Everywhere(e, kStart);
    r.Everywhere(t, kStart);
    r.Join(1);
    r.session.Run(1s);
    CHECK(r.S().Oblige(1, Key(test::kEvent, e), Val(3, 2)) == 1);
    FaultInjector& faults = *r.session.Faults();
    CHECK(r.session.RunUntil([&] { return faults.Fired(0) >= 2; }, 3s));  // reported, repeated
    CHECK(r.W(1).Fulfilled(1) == 1);
    CHECK(!EndOf(r.session, 1, 1));
    CHECK(r.S().Stats(1).credit_used >= 1);
    // The repair: a toggle written, its delta killed above the lane.
    r.Put(Key(test::kToggle, t), Val(1, 1));
    CHECK(r.session.RunUntil([&] { return EndOf(r.session, 1, 1).has_value(); }, 5s));
    const auto end = EndOf(r.session, 1, 1);
    CHECK(end && end->obligation_end == ObligationEnd::kRetiredBaseline);
    CHECK(faults.Fired(1) == 1);
    size_t repairs = 0;
    for (const ServerEvent& ev : r.session.ServerEvents()) {
      repairs += ev.type == ServerEvent::Type::kBaselineBegun && ev.peer == 1 &&
                 ev.why == BaselineReason::kManifestMismatch;
    }
    CHECK(repairs == 1);
    CHECK(r.session.RunUntil([&] { return r.S().Stats(1).credit_used == 0; }, 3s));
    CHECK(r.wire.last_sent_ack[1].holding == 0);
    CHECK(r.S().Stats(1).obligation_floor > 1);
    // It stops repeating: no report of id 1 after the mark.
    const uint64_t reports = faults.Fired(0);
    r.session.Run(3s);
    CHECK(faults.Fired(0) == reports);
    CHECK(r.W(1).Fulfilled(1) == 1);
    CHECK(r.wire.Count(1, false, MessageType::kObligation) == 1);
    CHECK(Equal(r.session, 1));
  }
}

BACKEND_TEST("RC5", ReceiptLeaseExpiresWithoutReport) {
  // Owner-run: the lease granted, no report ever sent (and, as a holder that went quiet, no renew:
  // the client renews by itself, so the renews are killed): LeaseEnd{expired} after the term; the
  // row unchanged.
  Rig r(WithFaults("message:kill dir=c2s peer=1 type=lease_renew"));
  const EntityId e = Ent(1, 4);
  r.Everywhere(e, kStart);
  r.Join(1);
  r.session.Run(1s);
  const uint32_t before = r.Put(Key(test::kProp, e), Val(7, 4));
  CHECK(before == 1);
  r.session.Run(500ms);
  const auto key = r.C(1).Send(OwnerRun(test::kProp, e), r.Now());
  REQUIRE_OR_RETURN_VOID(key.has_value());
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *key)) {
          if (d.outcome == Outcome::kGranted) return true;
        }
        return false;
      },
      1s));
  const TimePoint granted = r.Now();
  uint32_t lease = 0;
  for (const Decision& d : DecisionsFor(r.session, 1, *key)) {
    if (d.outcome == Outcome::kGranted) lease = d.lease;
  }
  CHECK(lease != 0);
  std::optional<ClientEvent> ended;
  CHECK(r.session.RunUntil(
      [&] {
        for (const ClientEvent& ev : r.session.ClientEvents(1)) {
          if (ev.type == ClientEvent::Type::kLeaseEnded && ev.lease == lease) {
            ended = ev;
            return true;
          }
        }
        return false;
      },
      5s));
  CHECK(ended && ended->lease_end == LeaseEndReason::kExpired);
  const Duration took = r.Now() - granted;
  CHECK(took >= r.session.Lim().lease_term - 100ms);
  CHECK(took <= r.session.Lim().lease_term + 500ms);
  const Record* row = r.S().State().Get(Key(test::kProp, e));
  CHECK(row && row->revision == before && row->value == Val(7, 4));
  CHECK(r.session.Faults()->Fired(0) >= 1);
  r.session.Run(500ms);
  CHECK(Equal(r.session, 1));
}

}  // namespace skylanders::backend
