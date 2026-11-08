#pragma once

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <span>
#include <vector>

#include "backend/kind_table.h"
#include "backend/lease.h"
#include "backend/limits.h"
#include "backend/record.h"
#include "backend/request.h"
#include "backend/world_state.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"

// The request table on direct calls (work item 4; design sections 3.8 and 3.9): S's WorldState,
// LeaseTable and RequestTable with a scripted authority, on fake time, recording every output in
// order. The cases that need a client (completion at at_lsn, the client's resend and peer epoch)
// run on FakeSession through backend_lanes_harness.h's Rig instead. Game-free: the kinds are the
// lanes harness's table (test.toggle takes commits and server-run, test.prop commits and
// owner-run, test.counter is a session-scope kind that takes commits).

// REQUIRE_OR_RETURN for a test function (which returns nothing): a failure is counted, then the
// test returns.
#ifndef REQUIRE_OR_RETURN_VOID
#define REQUIRE_OR_RETURN_VOID(cond)                                       \
  do {                                                                     \
    if (!(cond)) {                                                         \
      std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                        \
      return;                                                              \
    }                                                                      \
  } while (0)
#endif

namespace skylanders::backend::test::requests {

using namespace std::chrono_literals;
using lanes::Ent;
using lanes::Key;
using lanes::kLanesTable;
using lanes::Ms;
using lanes::Val;

// An authority whose answers a test scripts. Without a script: a commit writes the payload to the
// request's basis (or its subject's row of its kind), server-run asks for the evaluation, owner-run
// asks for a lease on the subject; a report commits its payload to the lease's row.
class ScriptedAuthority final : public Authority {
 public:
  std::function<std::optional<Verdict>(const Request&, const DecideContext&)> decide;
  std::function<std::optional<Verdict>(const Lease&, std::span<const uint8_t>,
                                       const DecideContext&)>
      report;
  Duration lease_term = 3s;
  int decides = 0;
  int reports = 0;

  Verdict Decide(const Request& r, const DecideContext& c) override {
    ++decides;
    if (decide) {
      if (auto v = decide(r, c)) return *v;
    }
    Verdict v;
    switch (r.model) {
      case Model::kCommit:
        v.type = Verdict::Type::kCommit;
        v.writes.push_back(Write{lanes::TargetOf(r), r.payload, false});
        break;
      case Model::kServerRun:
        v.type = Verdict::Type::kServerRun;
        break;
      case Model::kOwnerRun:
        v.type = Verdict::Type::kOwnerRun;
        v.lease = LeaseSpec{r.subject, r.kind, r.key.peer, lease_term};
        break;
    }
    return v;
  }
  Verdict OnReport(const Lease& lease, std::span<const uint8_t> payload,
                   const DecideContext& c) override {
    ++reports;
    if (report) {
      if (auto v = report(lease, payload, c)) return *v;
    }
    Verdict v;
    v.type = Verdict::Type::kCommit;
    v.writes.push_back(Write{RecordKey{lease.spec.kind, lease.spec.entity, 0},
                             std::vector<uint8_t>(payload.begin(), payload.end()), false});
    return v;
  }
};

inline Verdict Refusal(Reason reason, bool retryable = false, uint8_t kind_reason = 0) {
  Verdict v;
  v.type = Verdict::Type::kRefuse;
  v.reason = reason;
  v.retryable = retryable;
  v.kind_reason = kind_reason;
  return v;
}

constexpr uint16_t kPeer = 2;
constexpr uint16_t kOther = 3;
constexpr uint32_t kEpoch = 1;  // the peer epoch the rig's requests carry
constexpr uint32_t kGen = 1;

// S's request machinery on direct calls. Every output is kept in `log`, in order.
struct TableRig {
  explicit TableRig(Limits l = {}, std::span<const KindSpec> table_in = kLanesTable)
      : limits(l), table(table_in), state(table_in, l), leases(l), requests(table_in, set, state, leases, l) {
    for (const KindSpec& spec : table) {
      if (spec.models != 0) set.Register(spec.id, authority);
    }
    state.BeginEpoch(kGen);
    requests.Reset(kGen);
  }

  Limits limits;
  std::span<const KindSpec> table;
  ScriptedAuthority authority;
  AuthoritySet set;
  WorldState state;
  LeaseTable leases;
  RequestTable requests;
  TimePoint now = lanes::kStart;
  uint32_t generation = kGen;
  std::vector<RequestOutput> log;

  void Take() {
    for (RequestOutput& o : requests.TakeOutputs()) log.push_back(std::move(o));
  }
  // A request of `peer` (stamped as S stamps it from the link).
  Request Make(Request r, uint32_t sequence, uint16_t peer = kPeer, uint32_t peer_epoch = kEpoch) {
    r.key = RequestKey{peer, peer_epoch, generation, sequence};
    return r;
  }
  void Send(const Request& r, PeerRole role = PeerRole::kGuest) {
    requests.OnRequest(r, role, now);
    Take();
  }
  void Wait(Duration d) {
    now += d;
    requests.Service(now);
    Take();
  }
  bool Submit(const RequestKey& key, EvaluationResult result = {}) {
    const bool taken = requests.SubmitEvaluation(key, result, now);
    Take();
    return taken;
  }
  void Report(uint32_t lease, std::vector<uint8_t> payload, uint16_t peer = kPeer) {
    requests.OnLeaseReport(peer, kEpoch, lease, payload, PeerRole::kGuest, now);
    Take();
  }
  void NewGeneration() {
    ++generation;
    requests.OnGenerationChange(generation, now);
    Take();
    state.BeginEpoch(generation);
  }

  // Every decision for `key`, in order.
  std::vector<Decision> For(const RequestKey& key) const {
    std::vector<Decision> out;
    for (const RequestOutput& o : log) {
      if (o.type == RequestOutput::Type::kDecision && o.decision.key == key) out.push_back(o.decision);
    }
    return out;
  }
  std::optional<Decision> Last(const RequestKey& key) const {
    const auto all = For(key);
    if (all.empty()) return std::nullopt;
    return all.back();
  }
  std::optional<Decision> Final(const RequestKey& key) const {
    for (const Decision& d : For(key)) {
      if (d.final) return d;
    }
    return std::nullopt;
  }
  size_t Finals(const RequestKey& key) const {
    size_t n = 0;
    for (const Decision& d : For(key)) n += d.final && !d.cached ? 1 : 0;
    return n;
  }
  size_t Evaluates(const RequestKey& key) const {
    return size_t(std::count_if(log.begin(), log.end(), [&](const RequestOutput& o) {
      return o.type == RequestOutput::Type::kEvaluate && o.request.key == key;
    }));
  }
  // LeaseEnd outputs for `lease`.
  std::vector<LeaseEndReason> LeaseEnds(uint32_t lease) const {
    std::vector<LeaseEndReason> out;
    for (const RequestOutput& o : log) {
      if (o.type == RequestOutput::Type::kLeaseEnd && o.lease == lease) out.push_back(o.why);
    }
    return out;
  }
  // The position in the log of the first output that matches.
  std::optional<size_t> IndexOf(const std::function<bool(const RequestOutput&)>& match) const {
    for (size_t i = 0; i < log.size(); ++i) {
      if (match(log[i])) return i;
    }
    return std::nullopt;
  }
  // The lease a granted decision of `key` carries (0: none).
  uint32_t LeaseOf(const RequestKey& key) const {
    for (const Decision& d : For(key)) {
      if (d.outcome == Outcome::kGranted) return d.lease;
    }
    return 0;
  }
};

// Every key the log names ended in exactly one final decision (cached repeats aside), and no
// non-cached decision follows its final (1B's invariants 1, 2 and 4).
inline void CheckFinals(const TableRig& rig, bool all_final = true) {
  std::map<RequestKey, int> finals;
  std::map<RequestKey, bool> closed;
  for (const RequestOutput& o : rig.log) {
    if (o.type != RequestOutput::Type::kDecision || o.decision.key.sequence == 0) continue;
    const Decision& d = o.decision;
    if (d.cached) {
      if (closed[d.key]) CHECK(d.final);  // a cached answer after the final repeats the final
      continue;
    }
    CHECK(!closed[d.key]);  // nothing after the final
    if (d.final) {
      ++finals[d.key];
      closed[d.key] = true;
    } else {
      finals.try_emplace(d.key, 0);
    }
  }
  for (const auto& [key, n] : finals) {
    CHECK(n <= 1);
    if (all_final) CHECK(n == 1);
  }
}

}  // namespace skylanders::backend::test::requests
