#include "backend/request.h"

#include <algorithm>
#include <chrono>
#include <utility>

namespace skylanders::backend {

namespace {

bool Final(RequestState s) {
  return s == RequestState::kApplied || s == RequestState::kRefused || s == RequestState::kTomb;
}

uint8_t ModelBit(Model m) {
  switch (m) {
    case Model::kCommit: return kModelCommit;
    case Model::kServerRun: return kModelServerRun;
    case Model::kOwnerRun: return kModelOwnerRun;
  }
  return 0;
}

// ApplyAll's results as refusals (section 3.9): kTooBig, kRowLimit `limit`; kMixedScope,
// kUnknownKind `model` (Opus r2 N3); kWrongEpoch `stale generation`. kStaleRevision cannot come
// from a verdict (S writes the next revision itself); anything unexpected is a `limit`.
Reason FromPut(PutResult r) {
  switch (r) {
    case PutResult::kMixedScope:
    case PutResult::kUnknownKind:
      return Reason::kModel;
    case PutResult::kWrongEpoch:
      return Reason::kStaleGeneration;
    default:
      return Reason::kLimit;
  }
}

// A verdict's refusal with no reason of its own is the kind's (its kind_reason names it).
Reason VerdictReason(const Verdict& v) { return v.reason == Reason::kNone ? Reason::kKind : v.reason; }

}  // namespace

std::string_view OutcomeName(Outcome outcome) {
  switch (outcome) {
    case Outcome::kApplied: return "applied";
    case Outcome::kPending: return "pending";
    case Outcome::kRefused: return "refused";
    case Outcome::kGranted: return "granted";
  }
  return "?";
}

std::string_view RequestStateName(RequestState state) {
  switch (state) {
    case RequestState::kNew: return "new";
    case RequestState::kPending: return "pending";
    case RequestState::kRetry: return "retry";
    case RequestState::kApplied: return "applied";
    case RequestState::kRefused: return "refused";
    case RequestState::kHeld: return "held";
    case RequestState::kTomb: return "tomb";
  }
  return "?";
}

void AuthoritySet::Register(KindId kind, Authority& authority) { authorities_[kind] = &authority; }

Authority* AuthoritySet::For(KindId kind) {
  auto it = authorities_.find(kind);
  return it == authorities_.end() ? nullptr : it->second;
}

RequestTable::RequestTable(std::span<const KindSpec> table, AuthoritySet& authorities,
                           WorldState& state, LeaseTable& leases, const Limits& limits)
    : table_(table), authorities_(authorities), state_(state), leases_(leases), limits_(limits) {}

void RequestTable::Reset(uint32_t generation) {
  entries_.clear();
  children_.clear();
  queue_.clear();
  held_.clear();
  stored_.clear();
  precondition_.clear();
  generation_ = generation;
  previous_generation_ = generation;
  closing_ = false;
  next_order_ = 0;
  outputs_.clear();
}

std::vector<RequestOutput> RequestTable::TakeOutputs() { return std::exchange(outputs_, {}); }

RequestTable::Entry* RequestTable::Find(const RequestKey& key) {
  auto it = entries_.find(key);
  return it == entries_.end() ? nullptr : &it->second;
}
const RequestTable::Entry* RequestTable::Find(const RequestKey& key) const {
  auto it = entries_.find(key);
  return it == entries_.end() ? nullptr : &it->second;
}

const KindSpec* RequestTable::Spec(KindId kind) const { return FindKind(table_, kind); }

RequestState RequestTable::StateOf(const RequestKey& key) const {
  const Entry* e = Find(key);
  return e ? e->state : RequestState::kNew;
}

bool RequestTable::Evaluating(const RequestKey& key) const {
  const Entry* e = Find(key);
  return e && e->state == RequestState::kPending && e->evaluating;
}

const Decision* RequestTable::LastDecision(const RequestKey& key) const {
  const Entry* e = Find(key);
  return e && e->state != RequestState::kNew ? &e->decision : nullptr;
}

uint32_t RequestTable::Generation() const { return generation_; }
bool RequestTable::Closing() const { return closing_; }
size_t RequestTable::Size() const { return entries_.size(); }

size_t RequestTable::KeysOf(uint16_t peer, uint32_t peer_epoch) const {
  return size_t(std::count_if(entries_.begin(), entries_.end(), [&](const auto& e) {
    return e.first.peer == peer && e.first.peer_epoch == peer_epoch;
  }));
}

size_t RequestTable::KeysOfGeneration(uint32_t generation) const {
  return size_t(std::count_if(entries_.begin(), entries_.end(),
                              [&](const auto& e) { return e.first.generation == generation; }));
}
uint64_t RequestTable::LateEvaluationsDropped() const { return late_evaluations_; }
uint64_t RequestTable::UnknownReports() const { return unknown_reports_; }
uint64_t RequestTable::ImpureVerdicts() const { return impure_; }
uint64_t RequestTable::LeaseEndWritesRefused() const { return end_writes_refused_; }

RequestTable::Entry& RequestTable::Insert(const Request& request, PeerRole role) {
  Entry& e = entries_[request.key];
  e.request = request;
  e.role = role;
  e.arrival = e.order = next_order_++;
  ++stored_[request.key.peer];
  if (request.parent.sequence != 0) children_[request.parent].push_back(request.key);
  return e;
}

std::vector<RequestKey> RequestTable::KeysInOrder(bool (*want)(const Entry&)) const {
  std::vector<Slot> picked;
  for (const auto& [key, e] : entries_) {
    if (want(e)) picked.emplace_back(e.arrival, key);
  }
  std::sort(picked.begin(), picked.end(),
            [](const Slot& a, const Slot& b) { return a.first < b.first; });
  std::vector<RequestKey> keys;
  keys.reserve(picked.size());
  for (auto& p : picked) keys.push_back(p.second);
  return keys;
}

// The children of `parent` that are still in the table, in the order they arrived.
std::vector<RequestKey> RequestTable::ChildrenOf(const RequestKey& parent) const {
  std::vector<RequestKey> out;
  auto it = children_.find(parent);
  if (it == children_.end()) return out;
  for (const RequestKey& k : it->second) {
    if (Find(k)) out.push_back(k);
  }
  return out;
}

bool RequestTable::PreconditionSeen(const Request& request) const {
  // Precondition 0 needs none (a kind with requires_precondition refused it on arrival).
  if (request.precondition == 0) return true;
  if (request.key.generation != generation_) return false;
  auto it = precondition_.find(request.key.peer);
  return it != precondition_.end() && it->second >= request.precondition;
}

// A request the client may send again: not kNeverResent (1B's telegrams and skips) and not
// kClosesParent (1B's exits, which the client never resends).
bool RequestTable::Resendable(const Request& request) const {
  return (request.flags & (kNeverResent | kClosesParent)) == 0;
}

void RequestTable::SetState(Entry& e, RequestState state) {
  const RequestKey& key = e.request.key;
  if (e.state == RequestState::kPending) {
    auto q = queue_.find(key.peer);
    if (q != queue_.end()) {
      q->second.erase(Slot{e.order, key});
      if (q->second.empty()) queue_.erase(q);
    }
  } else if (e.state == RequestState::kHeld) {
    held_.erase(Slot{e.arrival, key});
  }
  if (state != RequestState::kPending) {
    e.evaluating = false;
    e.waiting = false;
  }
  e.state = state;
  if (state == RequestState::kPending && e.lease == 0) {
    queue_[key.peer].insert(Slot{e.order, key});
  } else if (state == RequestState::kHeld) {
    held_.insert(Slot{e.arrival, key});
  }
}

DecideContext RequestTable::Context(PeerRole role, TimePoint now) const {
  return DecideContext{state_, leases_, role, now, 0};
}

// ---------------------------------------------------------------------------------------------
// Answers. Every non-cached answer is stored as the key's decision, so a cached one repeats it.

void RequestTable::Emit(const Decision& decision) {
  RequestOutput o;
  o.type = RequestOutput::Type::kDecision;
  o.peer = decision.key.peer;
  o.decision = decision;
  outputs_.push_back(std::move(o));
}

void RequestTable::Send(Entry& e, Outcome outcome, Reason reason, uint8_t kind_reason, bool final,
                        bool server_final) {
  Decision d;
  d.key = e.request.key;
  d.outcome = outcome;
  d.reason = reason;
  d.kind_reason = kind_reason;
  d.final = final;
  d.server_final = server_final;
  d.lease = e.lease;
  if (const KindSpec* spec = Spec(e.request.kind)) d.scope = spec->scope;
  e.decision = d;
  Emit(d);
}

void RequestTable::SendCached(const Entry& e) {
  Decision d = e.decision;
  d.cached = true;
  Emit(d);
}

void RequestTable::Apply(Entry& e, uint64_t at_lsn, Scope scope) {
  SetState(e, RequestState::kApplied);
  Decision d;
  d.key = e.request.key;
  d.outcome = Outcome::kApplied;
  d.final = true;
  d.at_lsn = at_lsn;
  d.scope = scope;
  d.lease = e.lease;
  e.decision = d;
  Emit(d);
}

// Into its peer's queue, its precondition clock starting now (1B's Admit): answered `pending`
// unless it was answered so when it was held.
void RequestTable::Admit(Entry& e, TimePoint now) {
  const bool answered = e.state == RequestState::kHeld;
  e.since = now;
  SetState(e, RequestState::kPending);
  if (!answered) Send(e, Outcome::kPending, Reason::kNone, 0, false, false);
}

void RequestTable::Hold(Entry& e, TimePoint now) {
  const bool first = e.state != RequestState::kHeld;
  SetState(e, RequestState::kHeld);
  if (!first) return;  // re-held behind its parent: answered `pending` already
  e.since = now;
  Send(e, Outcome::kPending, Reason::kNone, 0, false, false);
}

void RequestTable::Retry(Entry& e, Reason reason, uint8_t kind_reason) {
  SetState(e, RequestState::kRetry);
  e.last_reason = reason;
  e.last_kind_reason = kind_reason;
  Send(e, Outcome::kRefused, reason, kind_reason, false, false);
  // A child whose parent a closing child already closed can never be resent (1B: the touch after
  // its exit went through): it gets S's final at once.
  if (e.request.parent.sequence != 0 && (e.request.flags & kClosesParent) == 0) {
    const Entry* parent = Find(e.request.parent);
    if (parent && parent->closed_by_child) ServerFinal(e, reason, kind_reason);
  }
}

void RequestTable::Refuse(Entry& e, Reason reason, uint8_t kind_reason, uint32_t revision) {
  SetState(e, RequestState::kRefused);
  Decision d;
  d.key = e.request.key;
  d.outcome = Outcome::kRefused;
  d.reason = reason;
  d.kind_reason = kind_reason;
  d.final = true;
  d.revision = revision;
  d.lease = e.lease;
  if (const KindSpec* spec = Spec(e.request.kind)) d.scope = spec->scope;
  e.decision = d;
  Emit(d);
}

void RequestTable::ServerFinal(Entry& e, Reason reason, uint8_t kind_reason) {
  SetState(e, RequestState::kRefused);
  Send(e, Outcome::kRefused, reason, kind_reason, true, true);
}

void RequestTable::RefuseMaybeRetry(Entry& e, Reason reason, uint8_t kind_reason, bool retryable) {
  if (retryable) {
    Retry(e, reason, kind_reason);
  } else {
    Refuse(e, reason, kind_reason);
  }
}

// The precondition did not come within limits.precondition_wait of admission (1B's too_far):
// retryable for a resendable request, terminal otherwise; its children follow.
void RequestTable::PreconditionMissing(Entry& e, TimePoint now) {
  const RequestKey key = e.request.key;
  RefuseMaybeRetry(e, Reason::kPreconditionMissing, 0, Resendable(e.request));
  AfterParent(key, now);
}

void RequestTable::EndLease(uint32_t lease, uint16_t holder, LeaseEndReason why) {
  RequestOutput o;
  o.type = RequestOutput::Type::kLeaseEnd;
  o.peer = holder;
  o.lease = lease;
  o.why = why;
  outputs_.push_back(std::move(o));
}

void RequestTable::EndWrites(const Lease& lease, LeaseEndReason why, TimePoint now) {
  if (why == LeaseEndReason::kCommitted || why == LeaseEndReason::kRevokedEpoch) return;
  Authority* authority = authorities_.For(lease.spec.kind);
  if (!authority) return;
  const Entry* e = Find(lease.request);
  const PeerRole role = e ? e->role : PeerRole::kGuest;
  const uint64_t head = state_.Head();
  const uint64_t count = state_.Writes();
  std::vector<Write> writes = authority->OnLeaseEnd(lease, why, Context(role, now));
  if (state_.Head() != head || state_.Writes() != count) {
    ++impure_;  // OnLeaseEnd wrote (D7, RM9): its own writes are not applied
    return;
  }
  if (writes.empty()) return;
  if (state_.ApplyAll(writes, now).result != PutResult::kWritten) ++end_writes_refused_;
}

// Leases that ended outside a report: an expiry ends its key `lease expired` (final); a revocation
// at the generation change or End session tells the holder (the keys' finals come from those
// paths); a holder that left hears nothing. The kind's end writes go first (S2-L2), for a holder
// that left too.
void RequestTable::DrainLeaseEnds(TimePoint now) {
  LeaseEndEvent ev;
  while (leases_.PollEnd(ev)) {
    EndWrites(ev.lease, ev.why, now);
    if (ev.why == LeaseEndReason::kRevokedPeerLeft) continue;
    if (ev.why == LeaseEndReason::kExpired) {
      Entry* e = Find(ev.request);
      if (e && !Final(e->state)) {
        Refuse(*e, Reason::kLeaseExpired);
        // Its children follow (a parent's end), outside closing.
        const RequestKey key = ev.request;
        AfterParent(key, now);
      }
    }
    EndLease(ev.id, ev.holder, ev.why);
  }
}

// ---------------------------------------------------------------------------------------------
// Transitions.

// A new key or a resend after a retryable refusal, past the generation, closing and key cap
// checks: requires_precondition (Opus F11), then the parent, then admission.
void RequestTable::Dispatch(Entry& e, TimePoint now) {
  const RequestKey key = e.request.key;
  const KindSpec* spec = Spec(e.request.kind);
  if (spec && spec->requires_precondition && e.request.precondition == 0) {
    Refuse(e, Reason::kUnsupported);
    AfterParent(key, now);  // its children follow as after a refused parent
    return;
  }
  if (e.request.parent.sequence != 0) {
    DispatchChild(e, now);
  } else {
    Admit(e, now);
  }
}

// A child follows its parent (1B's exit and touch columns, as data).
void RequestTable::DispatchChild(Entry& child, TimePoint now) {
  Entry* parent = Find(child.request.parent);
  const bool closes = (child.request.flags & kClosesParent) != 0;
  if (!parent || parent->state == RequestState::kPending || parent->state == RequestState::kHeld) {
    // No parent yet: up to the kind's parent_wait_ms; behind a pending parent: until it leaves
    // pending, with no timeout.
    Hold(child, now);
    return;
  }
  const RequestKey parent_key = parent->request.key;
  switch (parent->state) {
    case RequestState::kRetry:
      if (closes) {
        // The closing child makes its parent's retryable refusal final, then the other
        // children's, then is refused itself.
        ServerFinal(*parent, parent->last_reason, parent->last_kind_reason);
        FinalChildren(parent_key, true);
      }
      Refuse(child, Reason::kParentRefused);
      return;
    case RequestState::kApplied:
      if (closes) {
        FinalChildren(parent_key, false);
        parent->closed_by_child = true;
        Admit(child, now);  // evaluated under its own key, after the peer's earlier keys
      } else if (parent->closed_by_child) {
        Refuse(child, Reason::kClosedByChild);
      } else {
        Admit(child, now);
      }
      return;
    default:
      // Refused, or a tomb: the parent will never apply.
      if (closes) FinalChildren(parent_key, true);
      Refuse(child, Reason::kParentRefused);
      return;
  }
}

// The non-closing children of a parent that a closing child closed (or that was refused): one in
// retry gets S's final with its last reason; if the parent is refused, one still pending
// (evaluating included: its result is then dropped) gets S's final `parent refused`, and one held
// is refused `parent refused` (1B's FinalTouch).
void RequestTable::FinalChildren(const RequestKey& parent, bool parent_refused) {
  for (const RequestKey& k : ChildrenOf(parent)) {
    Entry* c = Find(k);
    if (!c || (c->request.flags & kClosesParent) != 0) continue;
    if (c->state == RequestState::kRetry) {
      ServerFinal(*c, c->last_reason, c->last_kind_reason);
    } else if (parent_refused && c->state == RequestState::kPending) {
      ServerFinal(*c, Reason::kParentRefused);
    } else if (parent_refused && c->state == RequestState::kHeld) {
      Refuse(*c, Reason::kParentRefused);
    }
  }
}

// A parent left pending (or was decided at once): its held children follow, in the order they
// came (1B's AfterEnter). Not while closing: then only End session's finals close them.
void RequestTable::AfterParent(const RequestKey& parent_key, TimePoint now) {
  if (closing_) return;
  const Entry* parent = Find(parent_key);
  if (!parent || parent->state == RequestState::kPending) return;
  const bool refused = parent->state == RequestState::kRefused;
  std::vector<Slot> held;
  for (const RequestKey& k : ChildrenOf(parent_key)) {
    Entry* c = Find(k);
    if (!c) continue;
    if (refused && c->state == RequestState::kPending && (c->request.flags & kClosesParent) == 0) {
      ServerFinal(*c, Reason::kParentRefused);
      continue;
    }
    if (c->state == RequestState::kHeld) held.emplace_back(c->arrival, k);
  }
  std::sort(held.begin(), held.end(),
            [](const Slot& a, const Slot& b) { return a.first < b.first; });
  for (const Slot& s : held) {
    Entry* c = Find(s.second);
    if (!c || c->state != RequestState::kHeld) continue;
    DispatchChild(*c, now);
  }
}

// The peer's queue, in order (1B's Advance): one key at a time is evaluating, in S's own wait or
// deciding; the head waits for its precondition up to limits.precondition_wait from admission.
void RequestTable::Advance(uint16_t peer, TimePoint now) {
  if (closing_) return;
  for (;;) {
    auto q = queue_.find(peer);
    if (q == queue_.end() || q->second.empty()) return;
    for (const Slot& s : q->second) {
      const Entry* x = Find(s.second);
      if (x && x->evaluating) return;
      if (x && x->waiting && s != *q->second.begin()) return;
    }
    const RequestKey head = q->second.begin()->second;
    Entry* e = Find(head);
    if (!e) return;  // cannot happen: the queue follows the entries
    if (!e->waiting && !PreconditionSeen(e->request)) {
      if (now - e->since < limits_.precondition_wait) return;
      PreconditionMissing(*e, now);  // it leaves the queue; the next one is looked at
      continue;
    }
    DecideHead(*e, now);
    const Entry* after = Find(head);
    if (after && after->state == RequestState::kPending && after->lease == 0 &&
        (after->evaluating || after->waiting)) {
      return;  // evaluating on the game thread, or in S's own wait: the rest wait behind it
    }
  }
}

// The checks at the head of the queue, then Decide: model allowed by the kind, basis revision,
// lease held by another peer (section 3.9 step 3).
void RequestTable::DecideHead(Entry& e, TimePoint now) {
  const RequestKey key = e.request.key;
  const KindSpec* spec = Spec(e.request.kind);
  Authority* authority = authorities_.For(e.request.kind);
  if (!spec || !authority || (spec->models & ModelBit(e.request.model)) == 0) {
    Refuse(e, Reason::kModel);
    AfterParent(key, now);
    return;
  }
  if (e.request.basis.kind != 0) {
    const Record* row = state_.Get(e.request.basis);
    const uint32_t current = row ? row->revision : 0;
    if (current != e.request.basis_revision) {
      Refuse(e, Reason::kStaleBasis, 0, current);
      AfterParent(key, now);
      return;
    }
  }
  if (const Lease* lease = leases_.Of(e.request.subject); lease && lease->spec.holder != key.peer) {
    Refuse(e, Reason::kLeased);
    AfterParent(key, now);
    return;
  }
  // Head and the write counter: a write that only made a noisy hold takes no LSN (RM9).
  const uint64_t head = state_.Head();
  const uint64_t writes = state_.Writes();
  Verdict verdict = authority->Decide(e.request, Context(e.role, now));
  if (state_.Head() != head || state_.Writes() != writes) {
    // Decide wrote (D7): the write stays in the log (the table cannot undo it), the request is
    // refused and the breach counted, so a test fails on it (RM9).
    ++impure_;
    Refuse(e, Reason::kModel);
    AfterParent(key, now);
    return;
  }
  TakeVerdict(e, std::move(verdict), now);
}

void RequestTable::TakeVerdict(Entry& e, Verdict v, TimePoint now) {
  const RequestKey key = e.request.key;
  const Model model = e.request.model;
  switch (v.type) {
    case Verdict::Type::kWait:
      // S's own wait (1B amendment n): the key stays at the head and is decided again at each
      // Service; at the end of the wait it is S's final with the verdict's reason.
      if (!e.waiting) {
        e.waiting = true;
        e.wait_until = now + v.wait;
      }
      if (now >= e.wait_until) {
        e.waiting = false;
        ServerFinal(e, VerdictReason(v), v.kind_reason);
        AfterParent(key, now);
      }
      return;
    case Verdict::Type::kRefuse:
      e.waiting = false;
      RefuseMaybeRetry(e, VerdictReason(v), v.kind_reason, v.retryable && Resendable(e.request));
      AfterParent(key, now);
      return;
    case Verdict::Type::kCommit:
      e.waiting = false;
      if (model != Model::kCommit) {
        Refuse(e, Reason::kModel);
      } else {
        Commit(e, v.writes, now);
      }
      AfterParent(key, now);
      return;
    case Verdict::Type::kServerRun: {
      e.waiting = false;
      if (model != Model::kServerRun) {
        Refuse(e, Reason::kModel);
        AfterParent(key, now);
        return;
      }
      e.evaluating = true;
      RequestOutput o;
      o.type = RequestOutput::Type::kEvaluate;
      o.peer = key.peer;
      o.request = e.request;
      outputs_.push_back(std::move(o));
      return;
    }
    case Verdict::Type::kOwnerRun: {
      e.waiting = false;
      if (model != Model::kOwnerRun) {
        Refuse(e, Reason::kModel);
        AfterParent(key, now);
        return;
      }
      LeaseSpec spec = v.lease;
      spec.holder = key.peer;
      if (spec.entity.form == IdForm::kNone) spec.entity = e.request.subject;
      if (spec.kind == 0) spec.kind = e.request.kind;
      // S2-L1: the grant's writes go with the grant, all or none. A grant that cannot be made
      // writes nothing (asked first), and a set not written refuses the grant with ApplyAll's
      // mapping (`limit`, `model`, `stale generation`), no lease made.
      if (const Reason busy = leases_.CanGrant(spec); busy != Reason::kNone) {
        Refuse(e, busy);
        AfterParent(key, now);
        return;
      }
      uint64_t grant_lsn = 0;
      Scope grant_scope = Scope::kLevel;
      if (!v.writes.empty()) {
        const WriteResult r = state_.ApplyAll(v.writes, now);
        if (r.result != PutResult::kWritten) {
          Refuse(e, FromPut(r.result));
          AfterParent(key, now);
          return;
        }
        grant_lsn = r.last_lsn;
        if (grant_lsn == 0) {  // nothing changed: complete at the stored changes (as Commit)
          for (const Write& w : v.writes) {
            const Record* row = state_.Get(w.key);
            grant_lsn = std::max(grant_lsn, row ? row->lsn : state_.Head());
          }
        }
        const KindSpec* wspec = Spec(v.writes.front().key.kind);
        grant_scope = wspec ? wspec->scope : Scope::kLevel;
      }
      auto granted = leases_.Grant(spec, key, now);
      if (const Reason* why = std::get_if<Reason>(&granted)) {
        // Not reached: CanGrant answered for the same table on the same thread.
        Refuse(e, *why);
        AfterParent(key, now);
        return;
      }
      // Granted: still pending (not final), out of the queue so the peer's next key goes on; its
      // report, the expiry or a revocation ends it.
      auto q = queue_.find(key.peer);
      if (q != queue_.end()) {
        q->second.erase(Slot{e.order, key});
        if (q->second.empty()) queue_.erase(q);
      }
      e.lease = std::get<uint32_t>(granted);
      Send(e, Outcome::kGranted, Reason::kNone, 0, false, false);
      if (!v.writes.empty()) {
        // The granted decision says where the grant's writes are complete (S2-L1).
        e.decision.at_lsn = grant_lsn;
        e.decision.scope = grant_scope;
        outputs_.back().decision = e.decision;
      }
      return;
    }
  }
}

bool RequestTable::Commit(Entry& e, const std::vector<Write>& writes, TimePoint now) {
  if (writes.empty()) {
    const KindSpec* spec = Spec(e.request.kind);
    Apply(e, state_.Head(), spec ? spec->scope : Scope::kLevel);
    return true;
  }
  const WriteResult r = state_.ApplyAll(writes, now);
  if (r.result != PutResult::kWritten) {
    Refuse(e, FromPut(r.result));
    return false;
  }
  const KindSpec* spec = Spec(writes.front().key.kind);
  // A set that changed nothing (every value already stored) takes no LSN; it is complete where
  // the client holds every written row's stored change, so at_lsn is the newest of those, never 0
  // (an at_lsn of 0 would complete before the earlier delta came).
  uint64_t at_lsn = r.last_lsn;
  if (at_lsn == 0) {
    for (const Write& w : writes) {
      const Record* row = state_.Get(w.key);
      at_lsn = std::max(at_lsn, row ? row->lsn : state_.Head());
    }
  }
  Apply(e, at_lsn, spec ? spec->scope : Scope::kLevel);
  return true;
}

// ---------------------------------------------------------------------------------------------
// Inputs.

void RequestTable::OnRequest(const Request& in, PeerRole role, TimePoint now) {
  Request request = in;
  if (request.parent.sequence != 0) {
    // A parent is the same peer's, in the same peer epoch and generation.
    request.parent.peer = request.key.peer;
    request.parent.peer_epoch = request.key.peer_epoch;
    request.parent.generation = request.key.generation;
  } else {
    request.parent = RequestKey{};
  }
  const RequestKey key = request.key;
  if (Entry* e = Find(key)) {
    if (closing_ && !Final(e->state)) {
      Refuse(*e, Reason::kSessionEnded);
      return;
    }
    if (e->state != RequestState::kRetry) {
      SendCached(*e);  // pending, held: `pending` (or `granted`); final: the final
      return;
    }
    e->request = request;  // the checks again, as for a new key; it arrives anew in its queue
    e->role = role;
    e->order = next_order_++;
    Dispatch(*e, now);
    Advance(key.peer, now);
    return;
  }

  // A new key: stale generation, session ended, key cap, in that order.
  const bool full = stored_[key.peer] >= limits_.max_keys_per_peer;
  if (key.generation != generation_) {
    Decision d;
    d.key = key;
    d.outcome = Outcome::kRefused;
    d.reason = Reason::kStaleGeneration;
    d.final = true;
    if (key.generation < generation_ && key.generation >= previous_generation_ && !full) {
      // A first send in flight at the change: one final, kept as a tomb so a duplicate is
      // answered from it, dropped at the next change.
      Entry& e = Insert(request, role);
      e.state = RequestState::kTomb;
      e.tomb_made_at = generation_;
      e.decision = d;
    }
    Emit(d);
    return;
  }
  if (full) {
    // Past the key cap: `rate` (or `session ended` while closing), final, not stored (a duplicate
    // gets the same answer again).
    Decision d;
    d.key = key;
    d.outcome = Outcome::kRefused;
    d.reason = closing_ ? Reason::kSessionEnded : Reason::kRate;
    d.final = true;
    Emit(d);
    return;
  }
  if (closing_) {
    Entry& e = Insert(request, role);
    Refuse(e, Reason::kSessionEnded);
    return;
  }
  Entry& e = Insert(request, role);
  Dispatch(e, now);
  Advance(key.peer, now);
}

bool RequestTable::OnPrecondition(uint16_t peer, uint32_t sequence, TimePoint now) {
  if (sequence == 0) return false;
  auto [it, inserted] = precondition_.try_emplace(peer, sequence);
  if (!inserted) {
    if (sequence <= it->second) return false;
    it->second = sequence;
  }
  Advance(peer, now);
  return true;
}

bool RequestTable::OnLeaseRenew(uint16_t peer, uint32_t lease, TimePoint now) {
  return leases_.Renew(lease, peer, now);
}

void RequestTable::OnLeaseReport(uint16_t peer, uint32_t peer_epoch, uint32_t lease,
                                 std::span<const uint8_t> payload, PeerRole role, TimePoint now) {
  const ReportCheck check = leases_.Report(lease, peer, now);
  switch (check.type) {
    case ReportCheck::Type::kUnknown:
      ++unknown_reports_;  // no key to answer on
      return;
    case ReportCheck::Type::kNotHolder: {
      Decision d;
      d.key = RequestKey{peer, peer_epoch, generation_, 0};
      d.outcome = Outcome::kRefused;
      d.reason = Reason::kNotHolder;
      d.final = true;
      d.lease = lease;
      Emit(d);
      return;
    }
    case ReportCheck::Type::kReplay:
      if (const Entry* e = Find(check.request)) SendCached(*e);
      return;
    case ReportCheck::Type::kExpired: {
      if (const Lease* ended = leases_.Get(lease)) EndWrites(*ended, LeaseEndReason::kExpired, now);
      Entry* e = Find(check.request);
      if (e && !Final(e->state)) {
        Refuse(*e, Reason::kLeaseExpired);
        AfterParent(check.request, now);
      }
      EndLease(lease, peer, LeaseEndReason::kExpired);
      return;
    }
    case ReportCheck::Type::kEvaluate:
      break;
  }

  const Lease* l = leases_.Get(lease);
  Entry* e = Find(check.request);
  Authority* authority = l ? authorities_.For(l->spec.kind) : nullptr;
  Verdict v;  // kRefuse
  v.reason = Reason::kModel;
  if (authority && l) {
    const uint64_t head = state_.Head();
    const uint64_t writes = state_.Writes();
    v = authority->OnReport(*l, payload, Context(role, now));
    if (state_.Head() != head || state_.Writes() != writes) {
      ++impure_;  // OnReport wrote (D7, RM9)
      v = Verdict{};
      v.reason = Reason::kModel;
    }
  }
  // A refused report ends the lease without a commit: the kind's end writes first (S2-L2).
  auto refuse_report = [&] {
    leases_.RefuseReport(lease);
    if (const Lease* ended = leases_.Get(lease)) {
      EndWrites(*ended, LeaseEndReason::kReportRefused, now);
    }
  };
  if (!e || Final(e->state)) {
    // The key went (it cannot while the lease lives); nothing to commit for.
    refuse_report();
    EndLease(lease, peer, LeaseEndReason::kReportRefused);
    return;
  }
  if (v.type == Verdict::Type::kCommit) {
    if (Commit(*e, v.writes, now)) {
      leases_.Commit(lease);
      EndLease(lease, peer, LeaseEndReason::kCommitted);
    } else {
      refuse_report();  // not written: a limit (or `model`), as a refusal
      EndLease(lease, peer, LeaseEndReason::kReportRefused);
    }
  } else {
    refuse_report();
    const Reason reason = v.type == Verdict::Type::kRefuse ? VerdictReason(v) : Reason::kModel;
    Refuse(*e, reason, v.kind_reason);
    EndLease(lease, peer, LeaseEndReason::kReportRefused);
  }
  AfterParent(check.request, now);
  Advance(check.request.peer, now);
}

void RequestTable::Service(TimePoint now) {
  leases_.Service(now);
  DrainLeaseEnds(now);
  if (closing_) return;
  std::vector<uint16_t> peers;
  peers.reserve(queue_.size());
  for (const auto& [peer, keys] : queue_) peers.push_back(peer);
  for (const uint16_t peer : peers) Advance(peer, now);
  // Held children whose parent never came: after the kind's parent_wait_ms, refused `parent
  // missing`, retryable or terminal as the kind says (1B's exit before enter, enter refused).
  // Held behind a pending parent, a child waits for the parent's own decision.
  const std::vector<Slot> held(held_.begin(), held_.end());
  for (const Slot& s : held) {
    Entry* e = Find(s.second);
    if (!e || e->state != RequestState::kHeld) continue;
    if (Find(e->request.parent)) continue;
    const KindSpec* spec = Spec(e->request.kind);
    const auto wait = std::chrono::milliseconds(spec ? spec->parent_wait_ms : 0);
    if (now - e->since < wait) continue;
    // Retryable as the kind says, but never for a kNeverResent request: every refusal of one is
    // terminal (section 3.9), and a retry key the client can never resend would stay open on both
    // sides until the generation changes, holding one of the client's max_keys_per_peer places
    // (Sol code review r3, N1). kClosesParent keeps the kind's rule: 1B's exit before enter is a
    // retry.
    const bool retryable = spec && spec->parent_missing_retryable &&
                           (e->request.flags & kNeverResent) == 0;
    RefuseMaybeRetry(*e, Reason::kParentMissing, 0, retryable);
  }
}

bool RequestTable::SubmitEvaluation(const RequestKey& key, const EvaluationResult& result,
                                    TimePoint now) {
  Entry* e = Find(key);
  if (closing_ || !e || e->state != RequestState::kPending || !e->evaluating) {
    ++late_evaluations_;
    return false;
  }
  e->evaluating = false;
  if (result.reason == Reason::kNone) {
    Apply(*e, state_.Head(), result.scope);
  } else if (result.server_final) {
    ServerFinal(*e, result.reason, result.kind_reason);
  } else {
    RefuseMaybeRetry(*e, result.reason, result.kind_reason,
                     result.retryable && Resendable(e->request));
  }
  AfterParent(key, now);
  Advance(key.peer, now);
  return true;
}

void RequestTable::OnGenerationChange(uint32_t generation, TimePoint now) {
  if (generation <= generation_) return;
  // Tombs made at an earlier change go now.
  std::erase_if(entries_, [&](const auto& entry) {
    return entry.second.state == RequestState::kTomb && entry.second.tomb_made_at < generation;
  });
  // One final for every key of the older generations, in arrival order. A pending key (evaluating,
  // waiting or granted included) and a held one get S's final `stale generation`, a retry key its
  // last reason; final keys need no message.
  const auto keys = KeysInOrder([](const Entry& e) { return e.state != RequestState::kTomb; });
  for (const RequestKey& key : keys) {
    Entry& e = *Find(key);
    switch (e.state) {
      case RequestState::kPending:
      case RequestState::kHeld:
        ServerFinal(e, Reason::kStaleGeneration);
        break;
      case RequestState::kRetry:
        ServerFinal(e, e.last_reason, e.last_kind_reason);
        break;
      default:
        break;
    }
    SetState(e, RequestState::kTomb);
    e.tomb_made_at = generation;
  }
  // The parent index keeps only keys still in the table.
  for (auto it = children_.begin(); it != children_.end();) {
    std::erase_if(it->second, [&](const RequestKey& k) { return !Find(k); });
    if (it->second.empty() || !Find(it->first)) {
      it = children_.erase(it);
    } else {
      ++it;
    }
  }
  queue_.clear();
  held_.clear();
  stored_.clear();  // the cap counts per generation
  precondition_.clear();
  previous_generation_ = generation_;
  generation_ = generation;
  // Leases live one generation: revoked after the finals, the holders told.
  leases_.OnEpoch(generation);
  DrainLeaseEnds(now);
}

void RequestTable::BeginClose(TimePoint now) {
  if (closing_) return;
  closing_ = true;
  const auto keys = KeysInOrder([](const Entry& e) {
    return e.state == RequestState::kPending || e.state == RequestState::kHeld ||
           e.state == RequestState::kRetry;
  });
  for (const RequestKey& key : keys) {
    Entry& e = *Find(key);
    e.evaluating = false;  // an evaluation in flight is dropped
    if (e.state == RequestState::kRetry) {
      ServerFinal(e, e.last_reason, e.last_kind_reason);
    } else {
      ServerFinal(e, Reason::kSessionEnded);
    }
  }
  queue_.clear();
  held_.clear();
  leases_.OnClose();
  DrainLeaseEnds(now);
}

void RequestTable::ForgetPeer(uint16_t peer, TimePoint now) {
  std::erase_if(entries_, [&](const auto& entry) { return entry.first.peer == peer; });
  for (auto it = children_.begin(); it != children_.end();) {
    if (it->first.peer == peer) {
      it = children_.erase(it);
    } else {
      ++it;
    }
  }
  queue_.erase(peer);
  std::erase_if(held_, [&](const Slot& s) { return s.second.peer == peer; });
  stored_.erase(peer);
  precondition_.erase(peer);
  leases_.OnPeerLeft(peer);
  DrainLeaseEnds(now);
}

}  // namespace skylanders::backend
