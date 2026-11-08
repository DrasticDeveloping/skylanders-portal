#pragma once

#include <cstdint>
#include <map>
#include <set>
#include <span>
#include <string_view>
#include <vector>

#include "backend/base.h"
#include "backend/entity_id.h"
#include "backend/kind_table.h"
#include "backend/lease.h"
#include "backend/limits.h"
#include "backend/record.h"
#include "backend/world_state.h"

// Requests and decisions (design section 3.9). The generic request table keeps 1B's seven states
// (new, pending, retry, applied, refused, held, tomb), its four invariants and its rules for
// generation changes and End session (portal online/requests.h); what was trigger-specific is kind
// data (parent, kClosesParent, kNeverResent, precondition, requires_precondition, parent_wait_ms).
// RequestKey is in record.h, Reason in base.h (Opus F12).
//
// Before Decide the table works in 1B's order (Opus r2 N2; portal online/requests.cpp's Dispatch,
// Admit and Advance):
// 1. on arrival: stale generation, session ended, key cap, requires_precondition (Opus F11), then
//    the parent: one not come is held up to the kind's parent_wait_ms, one pending is held until it
//    leaves pending (no timeout), one refused or closed refuses the child, one applied (or none)
//    lets it go on;
// 2. admission: the request joins the peer's queue, and its precondition clock starts now;
// 3. at the head of the peer's queue (one key evaluating at a time per peer): the precondition wait
//    (limits.precondition_wait from admission, then `precondition missing`), then model allowed by
//    the kind, basis revision, lease held by another peer, then Decide.
// Every refusal goes back to the requester. ApplyAll's results map to refusals: kTooBig, kRowLimit
// to `limit`; kMixedScope, kUnknownKind to `model`; kWrongEpoch to `stale generation`;
// kStaleRevision cannot come from a verdict (Opus r2 N3).

namespace skylanders::backend {

enum class Model : uint8_t { kCommit = 1, kServerRun = 2, kOwnerRun = 3 };
enum RequestFlags : uint8_t { kClosesParent = 1, kNeverResent = 2 };

struct Request {
  RequestKey key;  // on the wire only generation and sequence; S stamps peer and peer epoch from
                   // the link (Sol F3)
  KindId kind = 0;
  Model model = Model::kCommit;
  EntityId subject;
  RecordKey basis;               // kind 0: none
  uint32_t basis_revision = 0;   // the revision the client saw; refused `stale basis` if not current
  RequestKey parent{};           // an exit's enter (sequence 0: none); same peer, epoch, generation
  uint8_t flags = 0;             // RequestFlags
  uint32_t precondition = 0;     // a per-peer sequence S must have seen first (the pose); 0: none
  std::vector<uint8_t> payload;  // at most kMaxRequestPayload
};

enum class Outcome : uint8_t { kApplied = 1, kPending = 2, kRefused = 3, kGranted = 4 };
std::string_view OutcomeName(Outcome outcome);

struct Decision {
  RequestKey key;
  Outcome outcome = Outcome::kPending;
  Reason reason = Reason::kNone;
  uint8_t kind_reason = 0;  // with kKind: set by the kind's Authority, named by its reason_name
  bool final = false, cached = false, server_final = false;
  uint64_t at_lsn = 0;          // applied: complete when Replica::Completes(scope, at_lsn)
  Scope scope = Scope::kLevel;  // the scope of the writes at_lsn covers (Opus F8)
  uint32_t lease = 0;           // granted
  uint32_t revision = 0;        // stale basis: the row's current revision
};

struct Verdict {
  enum class Type : uint8_t { kCommit, kServerRun, kOwnerRun, kRefuse, kWait };
  Type type = Type::kRefuse;
  Reason reason = Reason::kNone;
  uint8_t kind_reason = 0;
  bool retryable = false;
  std::vector<Write> writes;  // kCommit: applied all or nothing (Sol F8); all in one scope (a mixed
                              // set is kMixedScope, refused `model`, RM11; Opus r2 N3)
  LeaseSpec lease;            // kOwnerRun
  Duration wait{};            // kWait: S's own wait, at most this long (1B amendment n)
};

struct DecideContext {
  const WorldState& state;
  const LeaseTable& leases;
  PeerRole peer_role;
  TimePoint now;
  uint64_t tick;
};

// One per kind; pure (D7): reads records, returns a verdict; only the table writes.
class Authority {
 public:
  virtual ~Authority() = default;
  virtual Verdict Decide(const Request& request, const DecideContext& context) = 0;
  // An owner-run kind's report (Sol F7): kCommit with the writes the report earns, or kRefuse.
  virtual Verdict OnReport(const Lease& /*lease*/, std::span<const uint8_t> /*payload*/,
                           const DecideContext& /*context*/) {
    Verdict refused;  // kRefuse
    refused.reason = Reason::kModel;
    return refused;
  }
};

// The authorities S decides with, one per kind that takes requests. Named by the design
// (Server's constructor); the header's own.
class AuthoritySet {
 public:
  void Register(KindId kind, Authority& authority);
  Authority* For(KindId kind);

 private:
  std::map<KindId, Authority*> authorities_;
};

// What the game thread found for one kEvaluate (the server-run model, 1B's two phases): kNone is a
// pass (the native ran and wrote its records; applied with the head LSN), anything else a refusal.
// Named by the design (Server::SubmitEvaluation); the fields are the header's own, after 1B's
// EvaluationResult.
struct EvaluationResult {
  Reason reason = Reason::kNone;
  uint8_t kind_reason = 0;
  bool retryable = false;
  bool server_final = false;     // S made the refusal final itself (1B item 9); ignored for a pass
  Scope scope = Scope::kLevel;   // a pass: the scope of the records the native wrote
};

// The generic request table (item 4). The design gives its behaviour (above and section 3.9's
// tables), not its methods; item 4 declares them below.

// 1B's seven states (portal online/requests.h): new (not seen), pending (accepted: waiting for its
// precondition, for its turn, in S's own wait, evaluating, or granted a lease), retry (its last
// answer was a retryable refusal, no final decision yet), applied and refused (final), held (a
// child waiting for its parent's decision, or for its parent to come), tomb (a key of an older
// generation, kept with its final decision until the next change).
enum class RequestState : uint8_t { kNew, kPending, kRetry, kApplied, kRefused, kHeld, kTomb };
std::string_view RequestStateName(RequestState state);

// What the table decided, in the order it is owed. Server turns each into a message or an event.
struct RequestOutput {
  enum class Type : uint8_t {
    kDecision,  // send `decision` to `peer` (gameplay lane)
    kEvaluate,  // the game thread runs `request` (server-run), then SubmitEvaluation
    kLeaseEnd,  // send LeaseEnd{lease, why} to `peer` (the holder)
  };
  Type type = Type::kDecision;
  uint16_t peer = 0;
  Decision decision;
  Request request;
  uint32_t lease = 0;
  LeaseEndReason why = LeaseEndReason::kExpired;
};

// The invariants (1B's four): every key ends in exactly one final decision; nothing is applied
// after it; every final is output before the key's generation ends (OnGenerationChange's outputs
// come first) or the session closes; a cached answer repeats the key's last decision, never a
// second one.
//
// Retryable or terminal. A refusal is retryable only when its reason allows it and the request is
// resendable: a request with kNeverResent or kClosesParent (1B's telegrams, skips and exits) gets
// every refusal terminal, except a retryable `parent missing` (1B's `exit before enter`, which S's
// final closes at the generation change). `precondition missing` (1B's `too far`) is retryable for
// a resendable request; a verdict's own refusal is retryable when the verdict says so.
//
// Not thread-safe: one thread (D3). Decide and OnReport run inside the table's calls, on the
// state passed at construction; the table checks that they wrote nothing (D7, RM9).
class RequestTable {
 public:
  RequestTable(std::span<const KindSpec> table, AuthoritySet& authorities, WorldState& state,
               LeaseTable& leases, const Limits& limits);

  // Forgets every key (a new session); `generation` is the current one.
  void Reset(uint32_t generation);

  // A request or a resend of a key; its key's peer and peer epoch already stamped by S from the
  // link (and its parent's too), never read from the client's bytes (Sol F3).
  void OnRequest(const Request& request, PeerRole role, TimePoint now);
  // The owner saw `peer`'s precondition `sequence` (the session's PlayerPose). Requests of that
  // peer with precondition <= sequence go on. false if it is not newer than the last one.
  bool OnPrecondition(uint16_t peer, uint32_t sequence, TimePoint now);
  // LeaseReport{lease, payload} from `peer` (under `peer_epoch`), in section 3.8's order. A
  // non-holder is answered `not holder` on the key (peer, peer_epoch, generation, 0) with `lease`
  // set: it never learns the holder's decision (RM5d).
  void OnLeaseReport(uint16_t peer, uint32_t peer_epoch, uint32_t lease,
                     std::span<const uint8_t> payload, PeerRole role, TimePoint now);
  // LeaseRenew{lease} from `peer`. false: not its live lease.
  bool OnLeaseRenew(uint16_t peer, uint32_t lease, TimePoint now);
  // Once per pump: lease expiries, each peer's queue (precondition waits, S's own waits), held
  // children whose parent never came.
  void Service(TimePoint now);
  // The game thread's result for a kEvaluate of `key` (the whole key, peer epoch included). false
  // (dropped, counted) when the table no longer holds it evaluating: another peer epoch, another
  // generation, already final, End session began (Sol F3, RM4, RM10).
  bool SubmitEvaluation(const RequestKey& key, const EvaluationResult& result, TimePoint now);
  // The server opened `generation`: the finals owed are output now (before the new level's
  // records), leases are revoked (kRevokedEpoch), older tombs go.
  void OnGenerationChange(uint32_t generation, TimePoint now);
  // End session: every key without a final is refused `session ended` (S's final), evaluations in
  // flight are dropped, leases revoked (kRevokedClose); from now on every request is refused so.
  void BeginClose(TimePoint now);
  // The peer left: its keys, preconditions and leases go, nothing is sent (Opus F9). Its id may
  // come back under a new peer epoch, whose keys never meet the old ones.
  void ForgetPeer(uint16_t peer, TimePoint now);

  std::vector<RequestOutput> TakeOutputs();

  RequestState StateOf(const RequestKey& key) const;
  bool Evaluating(const RequestKey& key) const;  // pending, its kEvaluate out
  // The key's last decision (what a cached answer repeats); nullptr if the key has no state.
  const Decision* LastDecision(const RequestKey& key) const;
  uint32_t Generation() const;
  bool Closing() const;
  size_t Size() const;
  // Keys held (any state, tombs included) of one peer under one peer epoch, and of one generation:
  // LM10's "no key of a departed peer epoch or an ended generation".
  size_t KeysOf(uint16_t peer, uint32_t peer_epoch) const;
  size_t KeysOfGeneration(uint32_t generation) const;

  // Counters (the server's events and stats read them).
  uint64_t LateEvaluationsDropped() const;  // SubmitEvaluation for a key not evaluating
  uint64_t UnknownReports() const;          // LeaseReport for no lease and no tomb
  uint64_t ImpureVerdicts() const;          // Decide or OnReport changed the state (RM9)

 private:
  struct Entry {
    RequestState state = RequestState::kNew;
    Request request;               // the last one that came with this key
    PeerRole role = PeerRole::kGuest;
    Reason last_reason = Reason::kNone;  // retry: the retryable reason
    uint8_t last_kind_reason = 0;
    Decision decision;             // the last decision sent (what a cached answer repeats)
    TimePoint since{};             // pending: its precondition clock (from admission); held: its hold
    uint64_t arrival = 0;          // order of first arrival (finals go out in this order)
    uint64_t order = 0;            // its place in its peer's queue (a resend arrives anew)
    uint32_t tomb_made_at = 0;     // tomb: the generation whose change made it one
    bool evaluating = false;       // pending: its kEvaluate is out
    bool waiting = false;          // pending: in S's own wait (Verdict::kWait)
    TimePoint wait_until{};        // waiting: when the wait ends
    uint32_t lease = 0;            // pending: granted this lease (out of the queue)
    bool closed_by_child = false;  // a kClosesParent child was let through (kClosedByChild)
  };
  using Slot = std::pair<uint64_t, RequestKey>;

  Entry* Find(const RequestKey& key);
  const Entry* Find(const RequestKey& key) const;
  Entry& Insert(const Request& request, PeerRole role);
  const KindSpec* Spec(KindId kind) const;
  std::vector<RequestKey> KeysInOrder(bool (*want)(const Entry&)) const;
  std::vector<RequestKey> ChildrenOf(const RequestKey& parent) const;
  bool PreconditionSeen(const Request& request) const;
  bool Resendable(const Request& request) const;
  void SetState(Entry& e, RequestState state);
  DecideContext Context(PeerRole role, TimePoint now) const;

  void Emit(const Decision& decision);
  void Send(Entry& e, Outcome outcome, Reason reason, uint8_t kind_reason, bool final,
            bool server_final);
  void SendCached(const Entry& e);
  void Apply(Entry& e, uint64_t at_lsn, Scope scope);
  void Admit(Entry& e, TimePoint now);
  void Hold(Entry& e, TimePoint now);
  void Retry(Entry& e, Reason reason, uint8_t kind_reason);
  void Refuse(Entry& e, Reason reason, uint8_t kind_reason = 0, uint32_t revision = 0);
  void ServerFinal(Entry& e, Reason reason, uint8_t kind_reason = 0);
  void RefuseMaybeRetry(Entry& e, Reason reason, uint8_t kind_reason, bool retryable);
  void PreconditionMissing(Entry& e, TimePoint now);
  void EndLease(uint32_t lease, uint16_t holder, LeaseEndReason why);

  void Dispatch(Entry& e, TimePoint now);
  void DispatchChild(Entry& child, TimePoint now);
  void FinalChildren(const RequestKey& parent, bool parent_refused);
  void AfterParent(const RequestKey& parent, TimePoint now);
  void Advance(uint16_t peer, TimePoint now);
  void DecideHead(Entry& e, TimePoint now);
  void TakeVerdict(Entry& e, Verdict verdict, TimePoint now);
  // ApplyAll the writes and answer `applied`; false (refused, `limit` or `model`) if not written.
  bool Commit(Entry& e, const std::vector<Write>& writes, TimePoint now);
  void DrainLeaseEnds(TimePoint now);

  std::span<const KindSpec> table_;
  AuthoritySet& authorities_;
  WorldState& state_;
  LeaseTable& leases_;
  Limits limits_;

  std::map<RequestKey, Entry> entries_;
  std::map<RequestKey, std::vector<RequestKey>> children_;  // parent -> children, by arrival
  std::map<uint16_t, std::set<Slot>> queue_;  // per peer: its queued pending keys, by order
  std::set<Slot> held_;                       // held keys, by arrival
  std::map<uint16_t, size_t> stored_;         // per peer: keys stored in this generation
  std::map<uint16_t, uint32_t> precondition_; // per peer, in the current generation
  uint32_t generation_ = 0;
  uint32_t previous_generation_ = 0;  // the generation before the last change (the tomb window)
  bool closing_ = false;
  uint64_t next_order_ = 0;
  uint64_t late_evaluations_ = 0, unknown_reports_ = 0, impure_ = 0;
  std::vector<RequestOutput> outputs_;
};

}  // namespace skylanders::backend
