// Requests, decisions and leases (design sections 3.8, 3.9 and 10; work item 4): every request model
// (RM1 to RM11 with RM5b to RM5f) and lease expiry (LE1 to LE8). The table's own cases run on direct
// calls (backend_request_harness.h); the ones that need the client (completion at at_lsn, the
// rejoin) run on FakeSession through the lanes harness's Rig.

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <optional>
#include <vector>

#include "backend/fake_world.h"
#include "backend_lanes_harness.h"
#include "backend_request_harness.h"
#include "backend_test.h"

namespace {

using namespace skylanders::backend;
using namespace skylanders::backend::test::requests;
namespace lanes = skylanders::backend::test::lanes;
using skylanders::backend::test::lanes::Completed;
using skylanders::backend::test::lanes::FinalFor;
using skylanders::backend::test::lanes::kCounter;
using skylanders::backend::test::lanes::Rig;
using skylanders::backend::test::kProp;
using skylanders::backend::test::kToggle;

// The first ServerEvent of `type` for `peer` in the session, from position `from`.
std::optional<ServerEvent> FirstServerEvent(Rig& r, ServerEvent::Type type, uint16_t peer,
                                            size_t from = 0) {
  const auto& events = r.session.ServerEvents();
  for (size_t i = from; i < events.size(); ++i) {
    if (events[i].type == type && events[i].peer == peer) return events[i];
  }
  return std::nullopt;
}

// A joined, level-ready peer 1 and an entity every world has; the session runs until the level is
// subscribed on both sides (S's baseline ended and the client applied it: before StateHello reaches
// the client, Send has nothing to key a request with).
EntityId Ready(Rig& r, uint16_t peer = 1, uint32_t n = 10) {
  r.Join(peer);
  const EntityId e = Ent(r.session.Generation(), n);
  r.Everywhere(e, r.Now());
  r.session.RunUntil(
      [&] { return (r.S().Stats(peer).subscription.scopes & kScopeLevel) != 0 &&
                   r.S().Stats(peer).baseline == 0 &&
                   (r.C(peer).State().Subscribed() & kScopeLevel) != 0; },
      5s);
  return e;
}

// ---------------------------------------------------------------------------------------------
// Every request model.

BACKEND_TEST("RM1", ModelCommit) {
  Rig r;
  const EntityId e = Ready(r);
  const auto key = r.C(1).Send(lanes::Commit(kProp, e, Val(7)), r.Now());
  REQUIRE_OR_RETURN_VOID(key.has_value());
  CHECK(key->peer == 1 && key->peer_epoch == r.C(1).PeerEpoch() && key->sequence == 1);
  CHECK(r.session.RunUntil([&] { return Completed(r.session, 1, *key); }, 2s));
  const auto fin = FinalFor(r.session, 1, *key);
  REQUIRE_OR_RETURN_VOID(fin.has_value());
  CHECK(fin->outcome == Outcome::kApplied);
  CHECK(fin->scope == Scope::kLevel);
  // The decision follows the write: at_lsn is the row's LSN on S.
  const Record* row = r.S().State().Get(Key(kProp, e));
  REQUIRE_OR_RETURN_VOID(row != nullptr);
  CHECK(row->value == Val(7));
  CHECK(fin->at_lsn == row->lsn);
  // Completion at at_lsn: the client holds the row at that revision when it says completed.
  const Record* mine = r.C(1).State().Get(Key(kProp, e));
  CHECK(mine && mine->revision >= row->revision);
  CHECK(r.C(1).State().Completes(Scope::kLevel, fin->at_lsn));
  // `decided` comes before `completed`.
  size_t decided = 0, completed = 0, i = 0;
  for (const ClientEvent& ev : r.session.ClientEvents(1)) {
    ++i;
    if (ev.type == ClientEvent::Type::kDecided && ev.decision.final && ev.key == *key) decided = i;
    if (ev.type == ClientEvent::Type::kCompleted && ev.key == *key) completed = i;
  }
  CHECK(decided != 0 && completed > decided);
}

BACKEND_TEST("RM2", ModelCommitStaleBasis) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  CHECK(t.state.Put(Key(kProp, e), Val(1), t.now) == PutResult::kWritten);
  CHECK(t.state.Put(Key(kProp, e), Val(2), t.now + 1s) == PutResult::kWritten);  // revision 2
  const uint64_t head = t.state.Head();
  const Request r = t.Make(lanes::CommitOn(kProp, e, 1, Val(3)), 1);
  t.Send(r);
  const auto fin = t.Final(r.key);
  REQUIRE_OR_RETURN_VOID(fin.has_value());
  CHECK(fin->outcome == Outcome::kRefused && fin->reason == Reason::kStaleBasis);
  CHECK(fin->revision == 2);  // the row's current revision
  CHECK(t.state.Head() == head);
  CHECK(t.requests.StateOf(r.key) == RequestState::kRefused);  // terminal
  CHECK(t.authority.decides == 0);  // the basis check runs before Decide
  CheckFinals(t);
}

BACKEND_TEST("RM3", ModelServerRun) {
  Rig r;
  const EntityId e = Ready(r);
  const size_t mark = r.session.ServerEvents().size();
  const auto key = r.C(1).Send(lanes::ServerRun(kToggle, e), r.Now());
  REQUIRE_OR_RETURN_VOID(key.has_value());
  std::optional<ServerEvent> eval;
  CHECK(r.session.RunUntil(
      [&] { return (eval = FirstServerEvent(r, ServerEvent::Type::kEvaluate, 1, mark)).has_value(); },
      1s));
  REQUIRE_OR_RETURN_VOID(eval.has_value());
  CHECK(eval->request.key == *key);  // the whole key, peer epoch included
  r.session.Run(100ms);
  // Pending, not final, until the evaluation comes.
  const auto decisions = lanes::DecisionsFor(r.session, 1, *key);
  REQUIRE_OR_RETURN_VOID(!decisions.empty());
  CHECK(decisions.front().outcome == Outcome::kPending && !decisions.front().final);
  CHECK(!FinalFor(r.session, 1, *key).has_value());
  CHECK(r.S().Requests().Evaluating(*key));
  // The game thread runs the native, writes its record, hands the result back.
  CHECK(r.S().State().Put(Key(kToggle, e), Val(1), r.Now()) == PutResult::kWritten);
  const uint64_t head = r.S().State().Head();
  EvaluationResult pass;
  pass.scope = Scope::kLevel;
  CHECK(r.S().SubmitEvaluation(*key, pass, r.Now()));
  CHECK(r.session.RunUntil([&] { return Completed(r.session, 1, *key); }, 2s));
  const auto fin = FinalFor(r.session, 1, *key);
  REQUIRE_OR_RETURN_VOID(fin.has_value());
  CHECK(fin->outcome == Outcome::kApplied && fin->at_lsn == head);
  const Record* mine = r.C(1).State().Get(Key(kToggle, e));
  CHECK(mine && mine->value == Val(1));
}

BACKEND_TEST("RM4", ModelServerRunLateResultDropped) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const Request r = t.Make(lanes::ServerRun(kToggle, e), 1);
  t.Send(r);
  CHECK(t.Evaluates(r.key) == 1);
  CHECK(t.requests.Evaluating(r.key));
  const uint64_t head = t.state.Head();
  t.NewGeneration();
  const auto fin = t.Final(r.key);
  REQUIRE_OR_RETURN_VOID(fin.has_value());
  CHECK(fin->outcome == Outcome::kRefused && fin->reason == Reason::kStaleGeneration);
  CHECK(fin->server_final);
  // The late result: dropped and counted; nothing applied.
  CHECK(!t.Submit(r.key));
  CHECK(t.requests.LateEvaluationsDropped() == 1);
  CHECK(t.Finals(r.key) == 1);
  // A result for a key never asked for is dropped the same way.
  CHECK(!t.Submit(RequestKey{kPeer, kEpoch, t.generation, 99}));
  CHECK(t.requests.LateEvaluationsDropped() == 2);
  CHECK(t.state.Head() == head + 1);  // only the epoch entry
  CheckFinals(t);
}

BACKEND_TEST("RM5", ModelOwnerRun) {
  Rig r;
  const EntityId e = Ready(r);
  const auto key = r.C(1).Send(lanes::OwnerRun(kProp, e), r.Now());
  REQUIRE_OR_RETURN_VOID(key.has_value());
  uint32_t lease = 0;
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : lanes::DecisionsFor(r.session, 1, *key)) {
          if (d.outcome == Outcome::kGranted) lease = d.lease;
        }
        return lease != 0;
      },
      1s));
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const Lease* l = r.S().Leases().Get(lease);
  REQUIRE_OR_RETURN_VOID(l != nullptr);
  CHECK(l->state == LeaseState::kGranted && l->spec.holder == 1 && l->request == *key);
  CHECK(!FinalFor(r.session, 1, *key).has_value());  // granted is not final
  // The holder runs its native, then reports; S's OnReport commits.
  r.C(1).Report(lease, Val(42), r.Now());
  CHECK(r.session.RunUntil([&] { return Completed(r.session, 1, *key); }, 2s));
  const auto fin = FinalFor(r.session, 1, *key);
  REQUIRE_OR_RETURN_VOID(fin.has_value());
  CHECK(fin->outcome == Outcome::kApplied && fin->lease == lease);
  const Record* row = r.S().State().Get(Key(kProp, e));
  CHECK(row && row->value == Val(42) && fin->at_lsn == row->lsn);
  CHECK(r.S().Leases().Get(lease)->state == LeaseState::kCommitted);
  CHECK(lanes::CountClient(r.session, 1, ClientEvent::Type::kLeaseEnded) == 1);
  bool committed = false;
  for (const ClientEvent& ev : r.session.ClientEvents(1)) {
    if (ev.type == ClientEvent::Type::kLeaseEnded && ev.lease == lease) {
      committed = ev.lease_end == LeaseEndReason::kCommitted;
    }
  }
  CHECK(committed);
}

// An owner-run request granted on the rig: its key and its lease.
std::pair<RequestKey, uint32_t> Granted(TableRig& t, const EntityId& e, uint32_t sequence,
                                        uint16_t peer = kPeer) {
  const Request r = t.Make(lanes::OwnerRun(kProp, e), sequence, peer);
  t.Send(r);
  return {r.key, t.LeaseOf(r.key)};
}

BACKEND_TEST("RM5b", ModelOwnerRunReportRefused) {
  TableRig t;
  t.authority.report = [](const Lease&, std::span<const uint8_t>, const DecideContext&) {
    return std::optional<Verdict>(Refusal(Reason::kKind, false, 7));
  };
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const uint64_t head = t.state.Head();
  t.Report(lease, Val(5));
  const auto fin = t.Final(key);
  REQUIRE_OR_RETURN_VOID(fin.has_value());
  CHECK(fin->outcome == Outcome::kRefused && fin->reason == Reason::kKind && fin->kind_reason == 7);
  CHECK(t.state.Head() == head);  // nothing written
  CHECK(t.state.Get(Key(kProp, e)) == nullptr);
  CHECK(t.LeaseEnds(lease) == std::vector<LeaseEndReason>{LeaseEndReason::kReportRefused});
  const Lease* l = t.leases.Get(lease);
  CHECK(l && l->state == LeaseState::kRevoked && l->why == LeaseEndReason::kReportRefused);
  CHECK(t.leases.Of(e) == nullptr);
  CheckFinals(t);
}

BACKEND_TEST("RM5c", ModelOwnerRunDuplicateReport) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const uint64_t head = t.state.Head();
  t.Report(lease, Val(5));
  t.Report(lease, Val(5));
  t.now += 1s;
  t.Report(lease, Val(6));  // once more, after the commit
  CHECK(t.authority.reports == 1);  // one OnReport, one commit
  CHECK(t.state.Head() == head + 1);
  const auto all = t.For(key);
  std::vector<Decision> finals;
  for (const Decision& d : all) {
    if (d.final) finals.push_back(d);
  }
  REQUIRE_OR_RETURN_VOID(finals.size() == 3);
  CHECK(!finals[0].cached && finals[1].cached && finals[2].cached);
  for (const Decision& d : finals) {
    CHECK(d.outcome == Outcome::kApplied && d.at_lsn == head + 1);
  }
  CHECK(t.state.Get(Key(kProp, e))->value == Val(5));
  CHECK(t.LeaseEnds(lease).size() == 1);
  CheckFinals(t);
}

BACKEND_TEST("RM5d", ModelOwnerRunNotHolder) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const uint64_t head = t.state.Head();
  const size_t before = t.For(key).size();
  t.Report(lease, Val(9), kOther);
  CHECK(t.authority.reports == 0);
  CHECK(t.state.Head() == head);
  // `not holder` goes to the reporter, on no key of the holder's.
  bool told = false;
  for (const RequestOutput& o : t.log) {
    if (o.type == RequestOutput::Type::kDecision && o.peer == kOther) {
      told = o.decision.reason == Reason::kNotHolder && o.decision.final &&
             o.decision.key.sequence == 0 && o.decision.lease == lease;
    }
  }
  CHECK(told);
  CHECK(t.For(key).size() == before);  // the holder heard nothing
  const Lease* l = t.leases.Get(lease);
  CHECK(l && l->state == LeaseState::kGranted);  // untouched
  CHECK(t.LeaseEnds(lease).empty());
  // The holder's own report still commits.
  t.Report(lease, Val(4));
  CHECK(t.Final(key) && t.Final(key)->outcome == Outcome::kApplied);
}

BACKEND_TEST("RM5e", ModelOwnerRunDuplicateAfterRefusal) {
  TableRig t;
  t.authority.report = [](const Lease&, std::span<const uint8_t>, const DecideContext&) {
    return std::optional<Verdict>(Refusal(Reason::kKind, false, 3));
  };
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const uint64_t head = t.state.Head();
  t.Report(lease, Val(5));
  t.now += 500ms;
  t.Report(lease, Val(5));  // the holder's duplicate: kReplay
  CHECK(t.authority.reports == 1);  // OnReport not called again
  const auto all = t.For(key);
  REQUIRE_OR_RETURN_VOID(all.size() >= 2);
  const Decision& first = all[all.size() - 2];
  const Decision& again = all.back();
  CHECK(first.final && !first.cached && first.reason == Reason::kKind && first.kind_reason == 3);
  CHECK(again.final && again.cached && again.reason == Reason::kKind && again.kind_reason == 3);
  const Lease* l = t.leases.Get(lease);
  CHECK(l && l->state == LeaseState::kRevoked && l->why == LeaseEndReason::kReportRefused);
  CHECK(t.LeaseEnds(lease).size() == 1);  // no second LeaseEnd
  CHECK(t.state.Head() == head);
  CheckFinals(t);
}

BACKEND_TEST("RM5f", ModelOwnerRunDuplicateAfterExpiryTime) {
  TableRig t;  // lease term 3 s
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  t.Wait(2s);
  t.Report(lease, Val(5));  // committed 1 s before the term would have ended
  const auto fin = t.Final(key);
  REQUIRE_OR_RETURN_VOID(fin.has_value() && fin->outcome == Outcome::kApplied);
  t.Wait(1500ms);  // past the original expiry: Service raises nothing for a committed lease
  t.Wait(1500ms);
  CHECK(t.LeaseEnds(lease) == std::vector<LeaseEndReason>{LeaseEndReason::kCommitted});
  t.Report(lease, Val(5));  // 2 s after that time
  const auto again = t.Last(key);
  CHECK(again && again->cached && again->outcome == Outcome::kApplied && again->at_lsn == fin->at_lsn);
  CHECK(t.leases.Get(lease)->state == LeaseState::kCommitted);

  // A lease that is never reported: Service expires it; its late report and that report's
  // duplicate both get `lease expired`, from the cache.
  const EntityId e2 = Ent(kGen, 2);
  const auto [key2, lease2] = Granted(t, e2, 2);
  REQUIRE_OR_RETURN_VOID(lease2 != 0);
  t.Wait(3100ms);
  CHECK(t.LeaseEnds(lease2) == std::vector<LeaseEndReason>{LeaseEndReason::kExpired});
  const auto expired = t.Final(key2);
  CHECK(expired && expired->reason == Reason::kLeaseExpired);
  t.Report(lease2, Val(1));
  t.Report(lease2, Val(1));
  const auto all2 = t.For(key2);
  REQUIRE_OR_RETURN_VOID(all2.size() >= 2);
  CHECK(all2.back().cached && all2.back().reason == Reason::kLeaseExpired);
  CHECK(all2[all2.size() - 2].cached && all2[all2.size() - 2].reason == Reason::kLeaseExpired);

  // The same without a Service between the term and the report: the report itself finds the lease
  // past its term (kExpired), ends the key and the lease; its duplicate is answered from the cache.
  const EntityId e3 = Ent(kGen, 3);
  const auto [key3, lease3] = Granted(t, e3, 3);
  REQUIRE_OR_RETURN_VOID(lease3 != 0);
  t.now += 3100ms;
  t.Report(lease3, Val(1));
  t.Report(lease3, Val(1));
  const auto all3 = t.For(key3);
  REQUIRE_OR_RETURN_VOID(all3.size() >= 3);
  CHECK(!all3[all3.size() - 2].cached && all3[all3.size() - 2].reason == Reason::kLeaseExpired);
  CHECK(all3.back().cached && all3.back().reason == Reason::kLeaseExpired);
  CHECK(t.LeaseEnds(lease3) == std::vector<LeaseEndReason>{LeaseEndReason::kExpired});
  CHECK(t.authority.reports == 1);
  CheckFinals(t);
}

BACKEND_TEST("RM6", ModelRefusedRetryable) {
  TableRig t;
  int calls = 0;
  t.authority.decide = [&](const Request&, const DecideContext&) -> std::optional<Verdict> {
    if (++calls == 1) return Refusal(Reason::kKind, /*retryable=*/true, 2);
    return std::nullopt;  // the default commit
  };
  const EntityId e = Ent(kGen, 1);
  const Request r = t.Make(lanes::Commit(kProp, e, Val(8)), 1);
  t.Send(r);
  auto last = t.Last(r.key);
  REQUIRE_OR_RETURN_VOID(last.has_value());
  CHECK(last->outcome == Outcome::kRefused && !last->final && last->kind_reason == 2);
  CHECK(t.requests.StateOf(r.key) == RequestState::kRetry);
  // A resend is checked again, not answered from the cache.
  t.Send(r);
  CHECK(t.authority.decides == 2);
  last = t.Last(r.key);
  CHECK(last && last->outcome == Outcome::kApplied && last->final && !last->cached);
  CheckFinals(t);
}

BACKEND_TEST("RM7", ModelRefusedTerminalCached) {
  TableRig t;
  t.authority.decide = [](const Request&, const DecideContext&) {
    return std::optional<Verdict>(Refusal(Reason::kKind, /*retryable=*/false, 4));
  };
  const Request r = t.Make(lanes::Commit(kProp, Ent(kGen, 1), Val(8)), 1);
  t.Send(r);
  t.Send(r);
  t.Send(r);
  CHECK(t.authority.decides == 1);
  const auto all = t.For(r.key);
  size_t cached = 0;
  for (const Decision& d : all) {
    if (d.cached) {
      ++cached;
      CHECK(d.final && d.reason == Reason::kKind && d.kind_reason == 4);
    }
  }
  CHECK(cached == 2);
  CHECK(t.requests.StateOf(r.key) == RequestState::kRefused);
  CheckFinals(t);
}

BACKEND_TEST("RM8", ModelNotAllowedForKind) {
  TableRig t;
  // test.toggle takes commit and server-run, not owner-run.
  const Request r = t.Make(lanes::OwnerRun(kToggle, Ent(kGen, 1)), 1);
  t.Send(r);
  const auto fin = t.Final(r.key);
  CHECK(fin && fin->reason == Reason::kModel && fin->outcome == Outcome::kRefused);
  CHECK(t.authority.decides == 0);
  // A kind with no authority (test.tracker takes no requests) is refused the same.
  const Request r2 = t.Make(lanes::Commit(test::kTracker, SessionObject(1), Val(1)), 2);
  t.Send(r2);
  CHECK(t.Final(r2.key) && t.Final(r2.key)->reason == Reason::kModel);
  // A verdict of another model than the request's is refused `model` too.
  t.authority.decide = [](const Request&, const DecideContext&) {
    Verdict v;
    v.type = Verdict::Type::kServerRun;
    return std::optional<Verdict>(v);
  };
  const Request r3 = t.Make(lanes::Commit(kProp, Ent(kGen, 2), Val(1)), 3);
  t.Send(r3);
  CHECK(t.Final(r3.key) && t.Final(r3.key)->reason == Reason::kModel);
  CheckFinals(t);
}

BACKEND_TEST("RM9", ModelDecidePure) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  // An authority that writes behind the table's back (D7): caught, refused, counted.
  t.authority.decide = [&](const Request&, const DecideContext& c) -> std::optional<Verdict> {
    const_cast<WorldState&>(c.state).Put(Key(kProp, Ent(kGen, 50)), Val(1), c.now);
    return std::nullopt;
  };
  const Request r = t.Make(lanes::Commit(kProp, e, Val(2)), 1);
  t.Send(r);
  CHECK(t.requests.ImpureVerdicts() == 1);
  CHECK(t.Final(r.key) && t.Final(r.key)->reason == Reason::kModel);
  CHECK(t.state.Get(Key(kProp, e)) == nullptr);  // the verdict's own writes never went
  // The same in OnReport.
  t.authority.decide = nullptr;
  t.authority.report = [&](const Lease&, std::span<const uint8_t>,
                           const DecideContext& c) -> std::optional<Verdict> {
    const_cast<WorldState&>(c.state).Put(Key(kProp, Ent(kGen, 51)), Val(1), c.now);
    return std::nullopt;
  };
  const auto [key, lease] = Granted(t, Ent(kGen, 2), 2);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  t.Report(lease, Val(3));
  CHECK(t.requests.ImpureVerdicts() == 2);
  CHECK(t.Final(key) && t.Final(key)->reason == Reason::kModel);
  CHECK(t.state.Get(Key(kProp, Ent(kGen, 2))) == nullptr);
  // A pure authority is never counted.
  t.authority.report = nullptr;
  const Request ok = t.Make(lanes::Commit(kProp, Ent(kGen, 3), Val(2)), 3);
  t.Send(ok);
  CHECK(t.requests.ImpureVerdicts() == 2);
  CHECK(t.Final(ok.key) && t.Final(ok.key)->outcome == Outcome::kApplied);
}

BACKEND_TEST("RM10", ModelServerRunLateAcrossRejoin) {
  Rig r;
  const EntityId e = Ready(r, 2);
  const uint32_t e1 = r.C(2).PeerEpoch();
  size_t mark = r.session.ServerEvents().size();
  const auto old_key = r.C(2).Send(lanes::ServerRun(kToggle, e), r.Now());
  REQUIRE_OR_RETURN_VOID(old_key.has_value());
  CHECK(r.session.RunUntil(
      [&] { return FirstServerEvent(r, ServerEvent::Type::kEvaluate, 2, mark).has_value(); }, 1s));
  // Peer 2 leaves and rejoins under a new peer epoch, and sends the same sequence.
  r.Join(2);
  r.Everywhere(e, r.Now());
  CHECK(r.session.RunUntil([&] { return r.C(2).PeerEpoch() != 0; }, 1s));
  const uint32_t e2 = r.C(2).PeerEpoch();
  CHECK(e2 == e1 + 1);
  mark = r.session.ServerEvents().size();
  const auto new_key = r.C(2).Send(lanes::ServerRun(kToggle, e), r.Now());
  REQUIRE_OR_RETURN_VOID(new_key.has_value());
  CHECK(new_key->peer == old_key->peer && new_key->generation == old_key->generation &&
        new_key->sequence == old_key->sequence && new_key->peer_epoch == e2);
  CHECK(r.session.RunUntil(
      [&] { return FirstServerEvent(r, ServerEvent::Type::kEvaluate, 2, mark).has_value(); }, 1s));
  // The old evaluation arrives: dropped and counted; the new key is not touched.
  const uint64_t head = r.S().State().Head();
  CHECK(!r.S().SubmitEvaluation(*old_key, EvaluationResult{}, r.Now()));
  r.session.Run(100ms);  // the session collects S's events at its next step
  CHECK(lanes::CountServer(r.session, ServerEvent::Type::kLateEvaluationDropped, 2) >= 1);
  CHECK(r.S().Requests().StateOf(*new_key) == RequestState::kPending);
  CHECK(r.S().Requests().Evaluating(*new_key));
  CHECK(!FinalFor(r.session, 2, *new_key).has_value());
  CHECK(r.S().State().Head() == head);
  // Its own result decides it.
  CHECK(r.S().SubmitEvaluation(*new_key, EvaluationResult{}, r.Now()));
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 2, *new_key).has_value(); }, 1s));
  CHECK(FinalFor(r.session, 2, *new_key)->outcome == Outcome::kApplied);
}

BACKEND_TEST("RM11", ModelCommitAllOrNothing) {
  Limits limits;
  limits.max_rows_per_level = 4;
  TableRig t(limits);
  const EntityId a = Ent(kGen, 1), b = Ent(kGen, 2);
  // Two writes, the second over its kind's size: nothing written, `limit`.
  t.authority.decide = [&](const Request&, const DecideContext&) {
    Verdict v;
    v.type = Verdict::Type::kCommit;
    v.writes.push_back(Write{Key(kProp, a), Val(1), false});
    v.writes.push_back(Write{Key(kProp, b), std::vector<uint8_t>(65, 1), false});  // max 64
    return std::optional<Verdict>(v);
  };
  uint64_t head = t.state.Head();
  const Request r1 = t.Make(lanes::Commit(kProp, a, Val(1)), 1);
  t.Send(r1);
  CHECK(t.state.Head() == head);
  CHECK(t.Final(r1.key) && t.Final(r1.key)->reason == Reason::kLimit);
  CHECK(t.state.Get(Key(kProp, a)) == nullptr);
  // A set whose new rows pass max_rows_per_level by one: the same.
  for (uint32_t n = 10; n < 13; ++n) {
    CHECK(t.state.Put(Key(kProp, Ent(kGen, n)), Val(n), t.now) == PutResult::kWritten);
  }
  head = t.state.Head();
  t.authority.decide = [&](const Request&, const DecideContext&) {
    Verdict v;
    v.type = Verdict::Type::kCommit;
    v.writes.push_back(Write{Key(kProp, a), Val(1), false});
    v.writes.push_back(Write{Key(kProp, b), Val(2), false});
    return std::optional<Verdict>(v);
  };
  const Request r2 = t.Make(lanes::Commit(kProp, a, Val(1)), 2);
  t.Send(r2);
  CHECK(t.state.Head() == head);
  CHECK(t.Final(r2.key) && t.Final(r2.key)->reason == Reason::kLimit);
  // A set mixing scopes: kMixedScope, nothing written, `model` (Opus r2 N3).
  t.authority.decide = [&](const Request&, const DecideContext&) {
    Verdict v;
    v.type = Verdict::Type::kCommit;
    v.writes.push_back(Write{Key(kProp, Ent(kGen, 10)), Val(5), false});
    v.writes.push_back(Write{Key(kCounter, SessionObject(1)), Val(5), false});
    return std::optional<Verdict>(v);
  };
  const Request r3 = t.Make(lanes::Commit(kProp, Ent(kGen, 10), Val(5)), 3);
  t.Send(r3);
  CHECK(t.state.Head() == head);
  CHECK(t.Final(r3.key) && t.Final(r3.key)->reason == Reason::kModel);
  CHECK(t.state.Get(Key(kProp, Ent(kGen, 10)))->value == Val(10));
  CheckFinals(t);
}

// ---------------------------------------------------------------------------------------------
// Lease expiry.

BACKEND_TEST("LE1", LeaseGrantReportCommit) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  LeaseState seen_in_report = LeaseState::kGranted;
  t.authority.report = [&](const Lease& l, std::span<const uint8_t>,
                           const DecideContext& c) -> std::optional<Verdict> {
    seen_in_report = c.leases.Get(l.id)->state;
    return std::nullopt;
  };
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  CHECK(t.leases.Get(lease)->state == LeaseState::kGranted);
  CHECK(t.leases.Of(e) && t.leases.Of(e)->id == lease);
  CHECK(t.requests.StateOf(key) == RequestState::kPending);
  t.Report(lease, Val(3));
  CHECK(seen_in_report == LeaseState::kReported);
  CHECK(t.leases.Get(lease)->state == LeaseState::kCommitted);
  CHECK(t.leases.Of(e) == nullptr);
  CHECK(t.LeaseEnds(lease) == std::vector<LeaseEndReason>{LeaseEndReason::kCommitted});
  CHECK(t.Final(key) && t.Final(key)->outcome == Outcome::kApplied);
  // The decision is output before the LeaseEnd.
  const auto d = t.IndexOf([&](const RequestOutput& o) {
    return o.type == RequestOutput::Type::kDecision && o.decision.key == key && o.decision.final;
  });
  const auto end = t.IndexOf([&](const RequestOutput& o) {
    return o.type == RequestOutput::Type::kLeaseEnd && o.lease == lease;
  });
  CHECK(d && end && *d < *end);
}

BACKEND_TEST("LE2", LeaseBusyRefused) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  // Another peer asks for the same entity: `leased`.
  const Request r = t.Make(lanes::OwnerRun(kProp, e), 1, kOther);
  t.Send(r);
  CHECK(t.Final(r.key) && t.Final(r.key)->reason == Reason::kLeased);
  // And a commit on it from another peer: `leased` too (IL7).
  const Request c = t.Make(lanes::Commit(kProp, e, Val(1)), 2, kOther);
  t.Send(c);
  CHECK(t.Final(c.key) && t.Final(c.key)->reason == Reason::kLeased);
  CHECK(t.leases.Of(e)->id == lease);
  // Past max_leases_per_peer (4): `limit` (LM7).
  for (uint32_t n = 2; n <= 4; ++n) CHECK(Granted(t, Ent(kGen, n), n).second != 0);
  const auto [fifth, none] = Granted(t, Ent(kGen, 5), 5);
  CHECK(none == 0);
  CHECK(t.Final(fifth) && t.Final(fifth)->reason == Reason::kLimit);
}

BACKEND_TEST("LE3", LeaseExpiresWithoutReport) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const uint64_t head = t.state.Head();
  t.Wait(2900ms);
  CHECK(t.LeaseEnds(lease).empty());
  t.Wait(200ms);
  CHECK(t.LeaseEnds(lease) == std::vector<LeaseEndReason>{LeaseEndReason::kExpired});
  CHECK(t.leases.Get(lease)->state == LeaseState::kExpired);
  const auto fin = t.Final(key);
  CHECK(fin && fin->reason == Reason::kLeaseExpired && fin->outcome == Outcome::kRefused);
  CHECK(t.state.Head() == head);  // nothing committed
  // Another peer can take it now.
  const auto [key2, lease2] = Granted(t, e, 1, kOther);
  CHECK(lease2 != 0 && lease2 != lease);
  CheckFinals(t, false);
}

BACKEND_TEST("LE4", LeaseReportAfterExpiry) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const uint64_t head = t.state.Head();
  t.now += 3s;  // at the term, no Service in between
  t.Report(lease, Val(9));
  CHECK(t.authority.reports == 0);
  CHECK(t.state.Head() == head);
  CHECK(t.Final(key) && t.Final(key)->reason == Reason::kLeaseExpired);
  CHECK(t.leases.Get(lease)->state == LeaseState::kExpired);
  CHECK(t.LeaseEnds(lease) == std::vector<LeaseEndReason>{LeaseEndReason::kExpired});
  CheckFinals(t);
}

BACKEND_TEST("LE5", LeaseRenewKeepsIt) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  t.Wait(2s);
  CHECK(t.requests.OnLeaseRenew(kPeer, lease, t.now));
  CHECK(!t.requests.OnLeaseRenew(kOther, lease, t.now));  // only the holder renews
  t.Wait(2s);
  CHECK(t.requests.OnLeaseRenew(kPeer, lease, t.now));
  t.Wait(2500ms);  // 6.5 s after the grant, 2.5 s after the last renew
  CHECK(t.LeaseEnds(lease).empty());
  t.Report(lease, Val(1));
  CHECK(t.Final(key) && t.Final(key)->outcome == Outcome::kApplied);
  // Without renews it expires a term after the last one.
  const auto [key2, lease2] = Granted(t, Ent(kGen, 2), 2);
  t.Wait(1s);
  CHECK(t.requests.OnLeaseRenew(kPeer, lease2, t.now));
  t.Wait(3100ms);
  CHECK(t.LeaseEnds(lease2) == std::vector<LeaseEndReason>{LeaseEndReason::kExpired});
  CHECK(!t.requests.OnLeaseRenew(kPeer, lease2, t.now));  // too late
}

BACKEND_TEST("LE6", LeaseHolderLeaves) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  const size_t outputs = t.log.size();
  t.requests.ForgetPeer(kPeer, t.now);
  t.Take();
  CHECK(t.log.size() == outputs);  // nothing is sent to a peer that left
  CHECK(t.leases.Get(lease) == nullptr);
  CHECK(t.leases.Of(e) == nullptr);
  CHECK(t.requests.StateOf(key) == RequestState::kNew);
  // Revoked at once: another peer can take it now, no expiry waited for.
  const auto [key2, lease2] = Granted(t, e, 1, kOther);
  CHECK(lease2 != 0);
}

BACKEND_TEST("LE7", LeaseGenerationChange) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  t.NewGeneration();
  const auto fin = t.Final(key);
  CHECK(fin && fin->reason == Reason::kStaleGeneration && fin->server_final);
  CHECK(t.LeaseEnds(lease) == std::vector<LeaseEndReason>{LeaseEndReason::kRevokedEpoch});
  // Its final before the new level's load: output before the LeaseEnd, and before anything of the
  // new generation (the caller sends both before the epoch entry).
  const auto d = t.IndexOf([&](const RequestOutput& o) {
    return o.type == RequestOutput::Type::kDecision && o.decision.key == key && o.decision.final;
  });
  const auto end = t.IndexOf([&](const RequestOutput& o) {
    return o.type == RequestOutput::Type::kLeaseEnd && o.lease == lease;
  });
  CHECK(d && end && *d < *end);
  CHECK(t.leases.Get(lease) == nullptr);  // lease ids live one generation
  CHECK(t.leases.Held(kPeer) == 0);
  // A late report finds nothing.
  t.Report(lease, Val(1));
  CHECK(t.requests.UnknownReports() == 1);
  CheckFinals(t);
}

BACKEND_TEST("LE8", LeaseEndSession) {
  TableRig t;
  const EntityId e = Ent(kGen, 1);
  const auto [key, lease] = Granted(t, e, 1);
  REQUIRE_OR_RETURN_VOID(lease != 0);
  t.requests.BeginClose(t.now);
  t.Take();
  const auto fin = t.Final(key);
  CHECK(fin && fin->reason == Reason::kSessionEnded && fin->server_final);
  CHECK(t.LeaseEnds(lease) == std::vector<LeaseEndReason>{LeaseEndReason::kRevokedClose});
  const Lease* l = t.leases.Get(lease);
  CHECK(l && l->state == LeaseState::kRevoked && l->why == LeaseEndReason::kRevokedClose);
  CHECK(t.leases.Of(e) == nullptr);
  // A report during the drain: answered from the key's cache, nothing written.
  const uint64_t head = t.state.Head();
  t.Report(lease, Val(1));
  CHECK(t.Last(key) && t.Last(key)->cached && t.Last(key)->reason == Reason::kSessionEnded);
  CHECK(t.state.Head() == head);
  CHECK(t.authority.reports == 0);
  // A new request is refused `session ended`.
  const Request r = t.Make(lanes::Commit(kProp, Ent(kGen, 2), Val(1)), 2);
  t.Send(r);
  CHECK(t.Final(r.key) && t.Final(r.key)->reason == Reason::kSessionEnded);
  CheckFinals(t);
}

// ---------------------------------------------------------------------------------------------
// The review's additions: an impure write that only makes a noisy hold, a commit that changes
// nothing, and ID3's request half.

BACKEND_TEST("RM9", ModelDecidePureNoisyHold) {
  // A noisy kind's Put inside its interval stores only a hold and takes no LSN: Head() does not
  // move, but the write counter does, so the breach is still caught (D7).
  static const std::array<uint8_t, 0> kNoDefault{};
  static const KindSpec kNoisy{
      .id = 20, .name = "test.noisy", .scope = Scope::kLevel, .tier = Tier::kStructural,
      .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kReadAtSpawn,
      .write = WriteMode::kMirrored, .models = kModelCommit, .noisy = true, .max_value_bytes = 8,
      .depends_on = {}, .authored_default = kNoDefault};
  std::vector<KindSpec> table(kLanesTable.begin(), kLanesTable.end());
  table.push_back(kNoisy);
  TableRig t(Limits{}, table);
  const RecordKey noisy{20, Ent(kGen, 9), 0};
  CHECK(t.state.Put(noisy, Val(1), t.now) == PutResult::kWritten);  // logged: its interval starts
  t.authority.decide = [&](const Request&, const DecideContext& c) -> std::optional<Verdict> {
    const uint64_t head = c.state.Head();
    CHECK(const_cast<WorldState&>(c.state).Put(noisy, Val(2), c.now) == PutResult::kWritten);
    CHECK(c.state.Head() == head);  // a hold: no LSN
    return std::nullopt;
  };
  const Request r = t.Make(lanes::Commit(kProp, Ent(kGen, 1), Val(2)), 1);
  t.Send(r);
  CHECK(t.requests.ImpureVerdicts() == 1);
  CHECK(t.Final(r.key) && t.Final(r.key)->reason == Reason::kModel);
  CHECK(t.state.Get(Key(kProp, Ent(kGen, 1))) == nullptr);
}

BACKEND_TEST("RM1b", ModelCommitUnchangedCompletesAtStoredChange) {
  // A commit whose writes change nothing (S already holds the value) takes no LSN; its at_lsn is the
  // stored change's, so the client completes it only once that change reached it, never before.
  for (int seed = 1; seed <= 4; ++seed) {
    Rig r(lanes::WithFaults("message:delay lane=state dir=s2c peer=1 delay=400", uint64_t(seed)));
    const EntityId e = Ready(r);
    const RecordKey row = Key(kToggle, e);
    CHECK(r.S().State().Put(row, Val(1), r.Now()) == PutResult::kWritten);
    const Record* stored = r.S().State().Get(row);
    REQUIRE_OR_RETURN_VOID(stored != nullptr);
    const uint32_t revision = stored->revision;
    const uint64_t lsn = stored->lsn;
    const auto key = r.C(1).Send(lanes::Commit(kToggle, e, Val(1)), r.Now());
    REQUIRE_OR_RETURN_VOID(key.has_value());
    bool early = false;
    std::optional<Decision> decided;
    r.session.on_client_event = [&](uint16_t peer, const ClientEvent& ev) {
      if (peer != 1) return;
      if (ev.type == ClientEvent::Type::kDecided && ev.decision.key == *key) decided = ev.decision;
      if (ev.type == ClientEvent::Type::kCompleted && ev.key == *key) {
        const Record* held = r.C(1).State().Get(row);
        early = early || !held || held->revision < revision;
      }
    };
    CHECK(r.session.RunUntil([&] { return Completed(r.session, 1, *key); }, 3s));
    CHECK(decided && decided->outcome == Outcome::kApplied && decided->at_lsn == lsn);
    CHECK(!early);
    CHECK(r.S().State().Get(row)->revision == revision);  // nothing written by the commit
  }
}

BACKEND_TEST("ID3", IdAmbiguousRequestRefused) {
  // ID3's request half: S's authority resolves the request's subject through S's own IdentityMap
  // (the game's adapters feed it, here the fake world). Two live entities under one id resolve
  // kAmbiguous: never picked, the request is refused `unresolved` (terminal). Once one goes, the id
  // resolves and the same request is applied. The table passes the kind's refusal through as is.
  TableRig t;
  IdentityMap identity;
  identity.OnEpoch(kGen);
  FakeWorld world(identity);
  const EntityId e = Ent(kGen, 7);
  const Handle first = world.Spawn(e, t.now);
  world.Spawn(e, t.now);  // a second entity under the same id
  CHECK(identity.Resolve(e).resolution == Resolution::kAmbiguous);
  t.authority.decide = [&](const Request& req, const DecideContext&) -> std::optional<Verdict> {
    if (identity.Resolve(req.subject).resolution != Resolution::kResolved) {
      return Refusal(Reason::kUnresolved);
    }
    return std::nullopt;
  };
  const Request a = t.Make(lanes::Commit(kProp, e, Val(1)), 1);
  t.Send(a);
  const auto fa = t.Final(a.key);
  CHECK(fa && fa->outcome == Outcome::kRefused && fa->reason == Reason::kUnresolved);
  CHECK(t.state.Get(Key(kProp, e)) == nullptr);
  world.Despawn(first, t.now);
  CHECK(identity.Resolve(e).resolution == Resolution::kResolved);
  const Request b = t.Make(lanes::Commit(kProp, e, Val(1)), 2);
  t.Send(b);
  const auto fb = t.Final(b.key);
  CHECK(fb && fb->outcome == Outcome::kApplied);
  CheckFinals(t);
}

BACKEND_TEST("RM12", ModelNeverResentParentMissingFinal) {
  // A held child whose parent never came, of a kind whose parent_missing_retryable is set: a
  // kNeverResent one is refused `parent missing`, final (every refusal of a kNeverResent request is
  // terminal, section 3.9), not left in the retry state that Client::Resend refuses to leave: there
  // it would hold one of the client's max_keys_per_peer places until the generation changes, with
  // no final (Sol code review r3, N1). A child without the flag keeps the kind's retryable refusal
  // (1B's exit before enter).
  std::array<KindSpec, kLanesTable.size()> table = kLanesTable;
  for (KindSpec& spec : table) {
    if (spec.id != kProp) continue;
    spec.parent_wait_ms = 100;
    spec.parent_missing_retryable = true;
  }
  TableRig t({}, table);
  const EntityId e = Ent(kGen, 1);
  Request never = t.Make(lanes::Commit(kProp, e, Val(1)), 2);
  never.parent = RequestKey{kPeer, kEpoch, kGen, 1};  // never sent
  never.flags = kNeverResent;
  t.Send(never);
  Request plain = t.Make(lanes::Commit(kProp, e, Val(2)), 4);
  plain.parent = RequestKey{kPeer, kEpoch, kGen, 3};  // never sent
  t.Send(plain);
  CHECK(t.requests.StateOf(never.key) == RequestState::kHeld);
  CHECK(t.requests.StateOf(plain.key) == RequestState::kHeld);
  t.Wait(150ms);
  const auto fin = t.Last(never.key);
  CHECK(fin && fin->outcome == Outcome::kRefused && fin->reason == Reason::kParentMissing &&
        fin->final);
  CHECK(t.requests.StateOf(never.key) == RequestState::kRefused);
  const auto retry = t.Last(plain.key);
  CHECK(retry && retry->outcome == Outcome::kRefused && retry->reason == Reason::kParentMissing &&
        !retry->final);
  CHECK(t.requests.StateOf(plain.key) == RequestState::kRetry);
  CheckFinals(t, /*all_final=*/false);
}

}  // namespace
