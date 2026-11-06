#include "online/requests.h"

#include <algorithm>
#include <utility>

namespace skylanders::online {

namespace {

bool IsTrigger(const RequestKey& k, TriggerEvent event) {
  return k.kind == RequestKind::kTrigger && k.event == event;
}
bool IsEnter(const RequestKey& k) { return IsTrigger(k, TriggerEvent::kEnter); }
bool IsExit(const RequestKey& k) { return IsTrigger(k, TriggerEvent::kExit); }
bool IsTouch(const RequestKey& k) { return IsTrigger(k, TriggerEvent::kTouch); }

// The key of the same enter sequence with another event kind (the enter of an exit, its touch).
RequestKey Sibling(const RequestKey& k, TriggerEvent event) {
  RequestKey s = k;
  s.event = event;
  return s;
}

bool Final(RequestState s) {
  return s == RequestState::kApplied || s == RequestState::kRefused || s == RequestState::kTomb;
}

}  // namespace

const char* RequestStateName(RequestState state) {
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

bool IsRetryable(const RequestKey& key, RefusalReason reason) {
  if (key.kind != RequestKind::kTrigger) return false;  // telegrams and skips are never resent
  if (key.event == TriggerEvent::kExit) return reason == RefusalReason::kExitBeforeEnter;
  switch (reason) {
    case RefusalReason::kTooFar:
    case RefusalReason::kRate:
    case RefusalReason::kNotReleased:
    case RefusalReason::kNotInPlay:
      return true;
    default:
      return false;
  }
}

void RequestTable::Reset(uint32_t generation) {
  entries_.clear();
  queue_.clear();
  held_.clear();
  stored_.clear();
  last_pose_.clear();
  close_wait_.clear();
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

RequestState RequestTable::StateOf(const RequestKey& key) const {
  const Entry* e = Find(key);
  return e ? e->state : RequestState::kNew;
}

bool RequestTable::Evaluating(const RequestKey& key) const {
  const Entry* e = Find(key);
  return e && e->state == RequestState::kPending && e->evaluating;
}

const WorldDecision* RequestTable::LastDecision(const RequestKey& key) const {
  const Entry* e = Find(key);
  return e && e->state != RequestState::kNew ? &e->decision : nullptr;
}

RequestTable::Entry& RequestTable::Insert(const WorldRequest& request) {
  Entry& e = entries_[request.key];
  e.request = request;
  e.arrival = e.order = next_order_++;
  ++stored_[request.key.peer];
  return e;
}

// The keys `want` picks, in the order they first arrived: finals go out in that order, and the
// copy lets a step change other entries.
std::vector<RequestKey> RequestTable::KeysInOrder(bool (*want)(const Entry&)) const {
  std::vector<Slot> picked;
  for (const auto& [key, e] : entries_) {
    if (want(e)) picked.emplace_back(e.arrival, key);
  }
  std::sort(picked.begin(), picked.end(),
            [](const Slot& a, const Slot& b) { return a.first < b.first; });
  std::vector<RequestKey> keys;
  keys.reserve(picked.size());
  for (auto& p : picked) keys.push_back(std::move(p.second));
  return keys;
}

bool RequestTable::PoseSeen(const WorldRequest& request) const {
  // Pose 0 means "needs no pose" only for a skip (a trigger or telegram with 0 is refused first).
  if (request.pose_sequence == 0) return request.key.kind == RequestKind::kSceneSkip;
  if (request.key.generation != generation_) return false;
  auto it = last_pose_.find(request.key.peer);
  return it != last_pose_.end() && it->second >= request.pose_sequence;
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
  if (state != RequestState::kPending) e.evaluating = false;
  e.state = state;
  if (state == RequestState::kPending) {
    queue_[key.peer].insert(Slot{e.order, key});
  } else if (state == RequestState::kHeld) {
    held_.insert(Slot{e.arrival, key});
  }
}

// ---------------------------------------------------------------------------------------------
// Answers. Every non-cached answer is stored as the key's decision, so a cached one repeats it.

void RequestTable::Emit(const WorldDecision& decision) {
  RequestOutput o;
  o.type = RequestOutput::Type::kDecision;
  o.peer = decision.key.peer;
  o.decision = decision;
  outputs_.push_back(std::move(o));
}

void RequestTable::Send(Entry& e, DecisionOutcome outcome, RefusalReason reason, bool final,
                        bool server_final) {
  WorldDecision d;
  d.key = e.request.key;
  d.outcome = outcome;
  d.reason = reason;
  d.final = final;
  d.server_final = server_final;
  d.applied_sequence = 0;
  e.decision = d;
  Emit(d);
}

void RequestTable::SendCached(const Entry& e) {
  WorldDecision d = e.decision;
  d.cached = true;
  Emit(d);
}

void RequestTable::Apply(Entry& e, uint32_t applied_sequence) {
  SetState(e, RequestState::kApplied);
  WorldDecision d;
  d.key = e.request.key;
  d.outcome = DecisionOutcome::kApplied;
  d.final = true;
  d.applied_sequence = applied_sequence;
  e.decision = d;
  Emit(d);
}

// Into its peer's queue: answered `pending` (unless it was answered so when it was held); Advance
// sends it to evaluation when its pose has come and its turn is up.
void RequestTable::Admit(Entry& e, TimePoint now) {
  const bool answered = e.state == RequestState::kHeld;
  e.since = now;
  SetState(e, RequestState::kPending);
  if (!answered) Send(e, DecisionOutcome::kPending, RefusalReason::kNone, false, false);
}

void RequestTable::Hold(Entry& e, TimePoint now) {
  const bool first = e.state != RequestState::kHeld;
  SetState(e, RequestState::kHeld);
  if (!first) return;  // re-held behind its enter: it was answered `pending` already
  e.since = now;
  Send(e, DecisionOutcome::kPending, RefusalReason::kNone, false, false);
}

void RequestTable::Retry(Entry& e, RefusalReason reason) {
  SetState(e, RequestState::kRetry);
  e.last_reason = reason;
  Send(e, DecisionOutcome::kRefused, reason, false, false);
  // A touch whose exit was dispatched already can never be resent (the hero left): it gets S's
  // final at once, as the touches in retry got it when the exit came (the exit column).
  if (IsTouch(e.request.key)) {
    const Entry* exit = Find(Sibling(e.request.key, TriggerEvent::kExit));
    if (exit && exit->state != RequestState::kHeld) ServerFinal(e, reason);
  }
}

void RequestTable::Refuse(Entry& e, RefusalReason reason) {
  SetState(e, RequestState::kRefused);
  Send(e, DecisionOutcome::kRefused, reason, true, false);
}

void RequestTable::ServerFinal(Entry& e, RefusalReason reason) {
  SetState(e, RequestState::kRefused);
  Send(e, DecisionOutcome::kRefused, reason, true, true);
}

// The pose came too late: too_far, retryable for an enter or a touch, terminal for an exit, a
// telegram or a skip (never resent).
void RequestTable::TooFar(Entry& e, TimePoint now) {
  const RequestKey key = e.request.key;
  if (IsRetryable(key, RefusalReason::kTooFar)) {
    Retry(e, RefusalReason::kTooFar);
  } else {
    Refuse(e, RefusalReason::kTooFar);
  }
  if (IsEnter(key)) AfterEnter(key, now);
}

// ---------------------------------------------------------------------------------------------
// Transitions.

// A new key or a resend after a retryable refusal, past the table's own checks.
void RequestTable::Dispatch(Entry& e, TimePoint now) {
  const RequestKey key = e.request.key;
  if (e.request.pose_sequence == 0 && key.kind != RequestKind::kSceneSkip) {
    Refuse(e, RefusalReason::kUnsupported);  // only a skip needs no pose
    if (IsEnter(key)) AfterEnter(key, now);
    return;
  }
  if (IsExit(key)) {
    DispatchExit(e, now);
  } else if (IsTouch(key)) {
    DispatchTouch(e, now);
  } else {
    Admit(e, now);
  }
}

// The exit column: an exit follows its enter's state.
void RequestTable::DispatchExit(Entry& exit, TimePoint now) {
  const RequestKey key = exit.request.key;
  Entry* enter = Find(Sibling(key, TriggerEvent::kEnter));
  if (!enter || enter->state == RequestState::kPending) {
    Hold(exit, now);  // no enter yet: up to kHeldExitWait; behind a pending enter: until it leaves
    return;
  }
  switch (enter->state) {
    case RequestState::kRetry:
      // S makes the enter's retryable refusal final, then its touch's, then refuses the exit.
      ServerFinal(*enter, enter->last_reason);
      FinalTouch(enter->request.key, true);
      Refuse(exit, RefusalReason::kEnterRefused);
      return;
    case RequestState::kApplied:
      FinalTouch(enter->request.key, false);
      Admit(exit, now);  // evaluated under its own key, after the peer's earlier keys
      return;
    default:
      // refused (an enter refused `unresolved` whose ref resolves later included), or a state an
      // enter of this generation cannot be in.
      FinalTouch(enter->request.key, true);
      Refuse(exit, RefusalReason::kEnterRefused);
      return;
  }
}

// A touch is gated on its enter.
void RequestTable::DispatchTouch(Entry& touch, TimePoint now) {
  const RequestKey key = touch.request.key;
  const Entry* enter = Find(Sibling(key, TriggerEvent::kEnter));
  if (!enter || enter->state == RequestState::kPending) {
    Hold(touch, now);  // like an exit: behind its pending enter, or up to kHeldExitWait
    return;
  }
  if (enter->state == RequestState::kApplied) {
    const Entry* exit = Find(Sibling(key, TriggerEvent::kExit));
    if (exit && exit->state != RequestState::kHeld) {
      Refuse(touch, RefusalReason::kEnterRefused);  // its exit went through: the hero left
    } else {
      Admit(touch, now);
    }
    return;
  }
  Refuse(touch, RefusalReason::kEnterRefused);  // the enter is in retry or refused
}

// The touch of an enter whose exit came (or that was refused): one in retry gets S's final with
// its last reason. If the enter is refused, one still pending (evaluating included: its result is
// then dropped) gets S's final `enter refused`, and one held is refused `enter refused`.
void RequestTable::FinalTouch(const RequestKey& enter_key, bool enter_refused) {
  Entry* touch = Find(Sibling(enter_key, TriggerEvent::kTouch));
  if (!touch) return;
  if (touch->state == RequestState::kRetry) {
    ServerFinal(*touch, touch->last_reason);
  } else if (enter_refused && touch->state == RequestState::kPending) {
    ServerFinal(*touch, RefusalReason::kEnterRefused);
  } else if (enter_refused && touch->state == RequestState::kHeld) {
    Refuse(*touch, RefusalReason::kEnterRefused);
  }
}

// An enter left pending (or was decided at once): its held touch and exit follow, in the order
// they came. Not while closing: then only End session's finals close them.
void RequestTable::AfterEnter(const RequestKey& enter_key, TimePoint now) {
  if (closing_) return;
  const Entry* enter = Find(enter_key);
  if (!enter || enter->state == RequestState::kPending) return;
  if (enter->state == RequestState::kRefused) {
    Entry* touch = Find(Sibling(enter_key, TriggerEvent::kTouch));
    if (touch && touch->state == RequestState::kPending) {
      ServerFinal(*touch, RefusalReason::kEnterRefused);
    }
  }
  std::vector<Slot> held;
  for (const TriggerEvent event : {TriggerEvent::kTouch, TriggerEvent::kExit}) {
    const Entry* e = Find(Sibling(enter_key, event));
    if (e && e->state == RequestState::kHeld) held.emplace_back(e->arrival, e->request.key);
  }
  std::sort(held.begin(), held.end(),
            [](const Slot& a, const Slot& b) { return a.first < b.first; });
  for (const Slot& s : held) {
    Entry* e = Find(s.second);
    if (!e || e->state != RequestState::kHeld) continue;
    if (IsExit(s.second)) {
      DispatchExit(*e, now);
    } else {
      DispatchTouch(*e, now);
    }
  }
}

// The peer's queue, in order: the first key whose pose has come goes to evaluation (and the rest
// wait behind it); a first key still waiting for its pose stops the queue until its pose wait ends.
void RequestTable::Advance(uint16_t peer, TimePoint now) {
  if (closing_) return;
  for (;;) {
    auto q = queue_.find(peer);
    if (q == queue_.end() || q->second.empty()) return;
    // One evaluation per peer at a time. It is the first key (a key admitted later with an earlier
    // place, a held exit or touch, is admitted only when its enter's evaluation ended), but the
    // whole queue is looked at so that holds whatever the order.
    for (const Slot& s : q->second) {
      const Entry* x = Find(s.second);
      if (x && x->evaluating) return;
    }
    Entry* e = Find(q->second.begin()->second);
    if (!e) return;  // cannot happen: the queue follows the entries
    if (PoseSeen(e->request)) {
      e->evaluating = true;
      RequestOutput o;
      o.type = RequestOutput::Type::kEvaluate;
      o.peer = peer;
      o.request = e->request;
      outputs_.push_back(std::move(o));
      return;
    }
    if (now - e->since < kPoseWait) return;
    TooFar(*e, now);  // it leaves the queue; the next one is looked at
  }
}

void RequestTable::OnRequest(const WorldRequest& request, TimePoint now) {
  const RequestKey& key = request.key;
  if (Entry* e = Find(key)) {
    if (closing_ && !Final(e->state)) {
      // From End session's closing on, a key with no final decision is refused, in any state.
      Refuse(*e, RefusalReason::kSessionEnded);
      return;
    }
    if (e->state != RequestState::kRetry) {
      SendCached(*e);  // pending (evaluating too), held: `pending`; applied, refused, tomb: final
      return;
    }
    e->request = request;  // the table's checks again, as for new; it arrives anew in its queue
    e->order = next_order_++;
    Dispatch(*e, now);
    Advance(key.peer, now);
    return;
  }

  // Row new. The generation check runs first: an old-generation exit is never held.
  const bool full = stored_[key.peer] >= kMaxKeysPerPeer;
  if (key.generation != generation_) {
    WorldDecision d;
    d.key = key;
    d.outcome = DecisionOutcome::kRefused;
    d.reason = RefusalReason::kStale;
    d.final = true;
    if (key.generation < generation_ && key.generation >= previous_generation_ && !full) {
      // A first send that was in flight at the change: one final decision, kept as a tomb so a
      // duplicate is answered from it, dropped at the next change.
      Entry& e = Insert(request);
      e.state = RequestState::kTomb;
      e.tomb_made_at = generation_;
      e.decision = d;
    }
    // Older than the tombstones (only a forged or broken client sends one), a generation S has
    // not opened (forged), or past the peer's key cap: refused, not kept.
    Emit(d);
    return;
  }
  if (full) {
    // Past the peer's key cap: refused (`rate`, or `session ended` while closing), final, and not
    // stored (a duplicate gets the same answer again).
    WorldDecision d;
    d.key = key;
    d.outcome = DecisionOutcome::kRefused;
    d.reason = closing_ ? RefusalReason::kSessionEnded : RefusalReason::kRate;
    d.final = true;
    Emit(d);
    return;
  }
  Entry& e = Insert(request);
  if (closing_) {
    Refuse(e, RefusalReason::kSessionEnded);
    return;
  }
  Dispatch(e, now);
  Advance(key.peer, now);
}

bool RequestTable::OnPose(uint16_t peer, uint32_t generation, uint32_t pose_sequence) {
  if (generation != generation_ || pose_sequence == 0) return false;
  auto [it, inserted] = last_pose_.try_emplace(peer, pose_sequence);
  if (inserted) return true;
  if (pose_sequence <= it->second) return false;  // older than (or the same as) the last one
  it->second = pose_sequence;
  return true;
}

void RequestTable::Service(TimePoint now) {
  if (closing_) {
    // The queues are cancelled; only the gone timeout runs.
    std::vector<uint16_t> gone;
    for (const auto& [peer, deadline] : close_wait_) {
      if (now >= deadline) gone.push_back(peer);
    }
    for (const uint16_t peer : gone) {
      CloseFinals(peer);
      close_wait_.erase(peer);
      RequestOutput o;
      o.type = RequestOutput::Type::kPeerClosed;
      o.peer = peer;
      o.gone = true;
      outputs_.push_back(std::move(o));
    }
    return;
  }
  std::vector<uint16_t> peers;
  peers.reserve(queue_.size());
  for (const auto& [peer, keys] : queue_) peers.push_back(peer);
  for (const uint16_t peer : peers) Advance(peer, now);
  // Held keys with no enter: up to kHeldExitWait, then an exit is refused `exit before enter`
  // (retry; the client never resends an exit, so only S's final at the generation change or End
  // session closes it) and a touch `enter refused` (terminal). Held behind a pending enter, a key
  // waits for the enter's own decision.
  const std::vector<Slot> held(held_.begin(), held_.end());
  for (const Slot& s : held) {
    Entry* e = Find(s.second);
    if (!e || e->state != RequestState::kHeld) continue;
    if (Find(Sibling(s.second, TriggerEvent::kEnter)) || now - e->since < kHeldExitWait) continue;
    if (IsExit(s.second)) {
      Retry(*e, RefusalReason::kExitBeforeEnter);
    } else {
      Refuse(*e, RefusalReason::kEnterRefused);
    }
  }
}

bool RequestTable::SubmitEvaluation(const RequestKey& key, const EvaluationResult& result,
                                    TimePoint now) {
  Entry* e = Find(key);
  if (!e || e->state != RequestState::kPending || !e->evaluating) return false;
  e->evaluating = false;
  if (result.reason == RefusalReason::kNone) {
    Apply(*e, result.applied_sequence);
  } else if (IsRetryable(key, result.reason)) {
    Retry(*e, result.reason);
  } else {
    Refuse(*e, result.reason);
  }
  if (IsEnter(key)) AfterEnter(key, now);
  Advance(key.peer, now);
  return true;
}

void RequestTable::OnGenerationChange(uint32_t generation) {
  if (generation <= generation_) return;
  // Tombs made at an earlier change go now ("dropped at the next generation change after the one
  // that made it a tomb").
  std::erase_if(entries_, [&](const auto& entry) {
    return entry.second.state == RequestState::kTomb && entry.second.tomb_made_at < generation;
  });
  // One event for every key of the older generations, in arrival order (the last column). A held
  // key is closed by its own cell; its enter's move to tomb does not re-dispatch it. An evaluating
  // key is pending: S's final, and its late result is dropped.
  const auto keys = KeysInOrder([](const Entry& e) { return e.state != RequestState::kTomb; });
  for (const RequestKey& key : keys) {
    Entry& e = *Find(key);
    switch (e.state) {
      case RequestState::kPending:
      case RequestState::kHeld:
        ServerFinal(e, RefusalReason::kGenerationEnded);  // queued work is cancelled
        break;
      case RequestState::kRetry:
        ServerFinal(e, e.last_reason);
        break;
      default:
        break;  // applied, refused: final already, no message
    }
    SetState(e, RequestState::kTomb);
    e.tomb_made_at = generation;
  }
  queue_.clear();
  held_.clear();
  stored_.clear();  // the cap counts per generation; the tombs are bounded by the last one's
  previous_generation_ = generation_;
  generation_ = generation;
  last_pose_.clear();
}

// Tombs stay through End session (the design drops them "at End session"): a duplicate of a
// tombed key during closing must still get its own final decision, not a second, different one
// from row new. The table is Reset at the next Start.
void RequestTable::BeginClose(const std::vector<uint16_t>& peers, TimePoint now) {
  if (closing_) return;
  closing_ = true;
  // Evaluations in flight are dropped: those keys stay pending and get `session ended` with the
  // rest of their peer's finals.
  for (auto& [key, e] : entries_) e.evaluating = false;
  for (const uint16_t peer : peers) close_wait_[peer] = now + kCloseWait;
}

// The finals S owes one peer at End session (the last column): pending (evaluating included) and
// held keys `session ended`, retry keys their last reason. Final keys and tombs answer from their
// state already.
void RequestTable::CloseFinals(uint16_t peer) {
  const auto keys = KeysInOrder([](const Entry& e) {
    return e.state == RequestState::kPending || e.state == RequestState::kHeld ||
           e.state == RequestState::kRetry;
  });
  for (const RequestKey& key : keys) {
    if (key.peer != peer) continue;
    Entry& e = *Find(key);
    ServerFinal(e, e.state == RequestState::kRetry ? e.last_reason : RefusalReason::kSessionEnded);
  }
}

void RequestTable::OnClosed(uint16_t peer) {
  if (!closing_ || !close_wait_.erase(peer)) return;
  CloseFinals(peer);
  RequestOutput o;
  o.type = RequestOutput::Type::kPeerClosed;
  o.peer = peer;
  outputs_.push_back(std::move(o));
}

void RequestTable::ForgetPeer(uint16_t peer) {
  std::erase_if(entries_, [&](const auto& entry) { return entry.first.peer == peer; });
  queue_.erase(peer);
  std::erase_if(held_, [&](const Slot& s) { return s.second.peer == peer; });
  stored_.erase(peer);
  last_pose_.erase(peer);
  close_wait_.erase(peer);
}

}  // namespace skylanders::online
