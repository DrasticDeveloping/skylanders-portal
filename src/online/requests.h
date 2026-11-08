#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <map>
#include <set>
#include <utility>
#include <vector>

#include "online/protocol.h"
#include "online/tick.h"

namespace skylanders::online {

// The request state machine of stage 1B (docs/online-1B.md section 2, "The request state machine").
// S keeps one state per request key; every answer S sends follows from the key's state and the
// event, nothing else. What the table decides comes out as outputs (decisions to send, keys to
// evaluate, peers whose drain is done) in the order they are owed.
//
// Threads. The table runs where ServerSession runs: on the thread that pumps it (session_host's
// pump thread), never on the game thread, and it never calls game code. The authority checks and
// the replay need game state, so they run on the game thread in two phases: when a key is ready to
// be evaluated (a new request or a resend whose pose S has seen, a pending key whose pose came, an
// exit or a touch whose enter applied), the table outputs kEvaluate and keeps the key pending (its
// "evaluating" sub-state, Evaluating()); the game thread runs the checks in authority order and, if
// they pass, the replay with g_applying set, then hands the result back (SubmitEvaluation, through
// the pump thread's queue). Only then does the table decide: applied (final), a retryable refusal
// (retry) or a terminal one (refused). The client never hears `applied` before the replay ran. A
// generation change or End session while a key is evaluating gives S's final (generation ended /
// session ended) and drops that key's later SubmitEvaluation. The table's own checks (stale,
// session ended, the pose order, the per-peer key cap) stay here. Not thread-safe: one thread.
//
// Order. Per peer, keys are evaluated in the order they arrived (a resend arrives anew): a key
// waits behind that peer's earlier pending keys (waiting for a pose, or evaluating), so one peer
// has at most one key evaluating, and its applies run in its reliable lane's order. A key whose
// pose S has seen does not time out while it waits for its turn. Held keys do not block the queue.
//
// States: new (not seen), pending (accepted: waiting for its pose, for its turn, or evaluating),
// retry (its last answer was a retryable refusal, no final decision yet), applied and refused
// (final), held (an exit or a touch waiting for its enter's decision), tomb (a key of an older
// generation, kept with its final decision). Every key that enters pending is answered `pending`
// first (a held key was answered so when it was held); its outcome follows.
//
// Exits and touches follow their enter. An exit: held behind a pending enter (or for kHeldExitWait
// with no enter, then `exit before enter`, retry); an enter in retry is made final and the exit
// refused `enter refused`; an applied enter lets the exit through to its own evaluation; a refused
// enter refuses it `enter refused`. A touch: held behind a pending enter (or for kHeldExitWait with
// no enter, then refused `enter refused`, terminal); an applied enter lets it through; an enter in
// retry or refused refuses it `enter refused` (terminal); a touch that arrives after its exit went
// through is refused the same. When an enter is refused, a touch of it still pending gets S's
// final.
//
// Pose sequence 0 means "needs no pose" only for a scene skip; a trigger or telegram request with
// pose 0 is refused `unsupported` (terminal) before anything else.
//
// The invariants the tests hold it to: (1) every key ends in exactly one final decision; (2)
// nothing is applied after a key's final decision; (3) every final decision is output before the
// client closes the key: before the new generation's LevelLoad (OnGenerationChange's outputs),
// within the new generation for a first send in flight at the change, and before the peer's Bye
// (kPeerClosed); (4) a cached answer repeats the key's last decision, it is never a second one.

// How long a request waits for the pose it was made at (design: pose order, "about 500 ms").
constexpr auto kPoseWait = std::chrono::milliseconds(500);
// How long an exit (or a touch) with no enter is held before `exit before enter` (design: row new).
constexpr auto kHeldExitWait = std::chrono::milliseconds(500);
// How long End session waits for a client's SessionClosed before it takes the client as gone.
constexpr auto kCloseWait = std::chrono::seconds(2);
// The keys the table stores for one peer in one generation (current keys and the tombs of first
// sends in flight at the change). A key past it is answered (`rate`, final, for a current one;
// `stale` for an old one) and not stored, so a broken client cannot grow the table. A level's
// route sends a few hundred.
constexpr size_t kMaxKeysPerPeer = 1024;

enum class RequestState : uint8_t {
  kNew,
  kPending,
  kRetry,
  kApplied,
  kRefused,
  kHeld,
  kTomb,
};
const char* RequestStateName(RequestState state);

// A refusal the client may resend after (design: "Retryable and terminal refusals"): too_far,
// rate, not released and not in play for an enter or a touch; exit before enter for an exit.
// Everything else is terminal, and so is every refusal of a telegram or a skip.
bool IsRetryable(const RequestKey& key, RefusalReason reason);

// What the game thread found for one kEvaluate: the authority checks after the table's own, in the
// design's order (already sent, not released, not in play, unresolved, too far, rate, unsupported),
// and, if they passed, the replay. kNone: applied, with the game's apply counter (the sequence of
// the AppliedStep it sent); anything else: refused for that reason (retryable or terminal by
// IsRetryable). A refusal converts implicitly, so SubmitEvaluation(key, RefusalReason::kTooFar)
// reads as one; EvaluationResult::Applied(sequence) is the pass.
//
// `server_final` (stage 1B item 9, amendment l 3.5): S made the refusal final itself, at the end of
// its own wait (its 2 s retry bound on a single-fire trigger): the key is refused with S's final
// (`final <key> refused (<reason>)`) even for a reason that is otherwise retryable, since the client
// never resends a single-fire key. Ignored for a pass.
struct EvaluationResult {
  RefusalReason reason = RefusalReason::kNone;
  uint32_t applied_sequence = 0;
  bool server_final = false;

  EvaluationResult() = default;
  // NOLINTNEXTLINE(google-explicit-constructor): implicit on purpose
  EvaluationResult(RefusalReason refused) : reason(refused) {}
  static EvaluationResult Applied(uint32_t sequence) {
    EvaluationResult r;
    r.applied_sequence = sequence;
    return r;
  }
  static EvaluationResult ServerFinal(RefusalReason refused) {
    EvaluationResult r(refused);
    r.server_final = true;
    return r;
  }
};

struct RequestOutput {
  enum class Type {
    kDecision,    // send `decision` to `peer`
    kEvaluate,    // the game thread evaluates `request` (checks, then the replay), then
                  // SubmitEvaluation; the key stays pending until then
    kPeerClosed,  // End session's drain is done for `peer`: its finals went out; send its Bye
  };
  Type type = Type::kDecision;
  uint16_t peer = 0;
  WorldDecision decision;
  WorldRequest request;
  bool gone = false;  // kPeerClosed: its SessionClosed did not come within kCloseWait
};

class RequestTable {
 public:
  RequestTable() = default;

  // Forgets everything (a new session); `generation` is the current one.
  void Reset(uint32_t generation = 0);

  // A request or a resend of a key (its peer already set to the sender).
  void OnRequest(const WorldRequest& request, TimePoint now);
  // A pose from `peer`, taken at once. true if it is newer than the last one from that peer in the
  // current generation (it is kept and pending keys see it at the next Service); false if it is
  // dropped.
  bool OnPose(uint16_t peer, uint32_t generation, uint32_t pose_sequence);
  // Stage 4: receiving and accepting a pose are two steps (S's rate check sits between them).
  // OnPoseReceived only drops duplicates: true if the pose is newer than the last one received
  // from that peer in the current generation. Pending keys do not see it. OnPoseAccepted is the
  // host's accept of a pose OnPoseReceived passed: pending keys see it at the next Service. false
  // if it is of another generation, was never received (or its peer left since), or is not newer
  // than the last one accepted. A pose that is received but never accepted (rate-dropped) meets no
  // key's pose precondition.
  bool OnPoseReceived(uint16_t peer, uint32_t generation, uint32_t pose_sequence);
  bool OnPoseAccepted(uint16_t peer, uint32_t generation, uint32_t pose_sequence);
  // Once per server frame: each peer's queue (pending keys whose pose came go to evaluation, keys
  // whose pose wait ended are refused too_far), held keys whose enter never came, and End
  // session's gone timeout.
  void Service(TimePoint now);
  // The game thread's result for a kEvaluate of `key`. true if it was taken (the decision and
  // whatever follows are output now); false if it is dropped: the key is no longer evaluating
  // (its generation ended, End session began, its peer left, or it was never asked for).
  bool SubmitEvaluation(const RequestKey& key, const EvaluationResult& result, TimePoint now);
  // The server opened `generation` (one event for every key of the older ones). The finals owed
  // are output now; the caller sends them before the new generation's LevelLoad.
  void OnGenerationChange(uint32_t generation);
  // End session: from now on nothing is applied or evaluated (evaluations in flight are dropped)
  // and every key without a final decision is refused `session ended`. `peers` are the clients
  // whose SessionClosed is awaited.
  void BeginClose(const std::vector<uint16_t>& peers, TimePoint now);
  // A client's SessionClosed: its finals, then kPeerClosed.
  void OnClosed(uint16_t peer);
  // A client left the session (Bye, timeout, replaced, closed): its keys, poses and close wait go.
  // Its id may come back with fresh sequences, which must not meet the old keys.
  void ForgetPeer(uint16_t peer);

  std::vector<RequestOutput> TakeOutputs();

  RequestState StateOf(const RequestKey& key) const;
  // The key is pending and its kEvaluate is out, awaiting SubmitEvaluation.
  bool Evaluating(const RequestKey& key) const;
  // The key's last decision (the one a cached answer repeats); nullptr if the key has no state.
  const WorldDecision* LastDecision(const RequestKey& key) const;
  uint32_t Generation() const { return generation_; }
  bool Closing() const { return closing_; }
  // End session started and every awaited client is closed (or gone, or left).
  bool AllClosed() const { return closing_ && close_wait_.empty(); }
  size_t Size() const { return entries_.size(); }

 private:
  struct Entry {
    RequestState state = RequestState::kNew;
    WorldRequest request;            // the last one that came with this key
    RefusalReason last_reason = RefusalReason::kNone;  // retry: the retryable reason
    WorldDecision decision;          // the last decision sent (what a cached answer repeats)
    TimePoint since{};               // pending: its pose wait; held: its hold
    uint64_t arrival = 0;            // order of first arrival (finals go out in this order)
    uint64_t order = 0;              // its place in its peer's queue (a resend arrives anew)
    uint32_t tomb_made_at = 0;       // tomb: the generation whose change made it one
    bool evaluating = false;         // pending: its kEvaluate is out
  };
  using Slot = std::pair<uint64_t, RequestKey>;

  Entry* Find(const RequestKey& key);
  const Entry* Find(const RequestKey& key) const;
  Entry& Insert(const WorldRequest& request);
  std::vector<RequestKey> KeysInOrder(bool (*want)(const Entry&)) const;
  bool PoseSeen(const WorldRequest& request) const;
  // Every state change goes through here: it keeps the per-peer queues and the held set.
  void SetState(Entry& e, RequestState state);

  void Emit(const WorldDecision& decision);
  void Send(Entry& e, DecisionOutcome outcome, RefusalReason reason, bool final, bool server_final);
  void SendCached(const Entry& e);
  void Apply(Entry& e, uint32_t applied_sequence);
  void Admit(Entry& e, TimePoint now);
  void Hold(Entry& e, TimePoint now);
  void Retry(Entry& e, RefusalReason reason);
  void Refuse(Entry& e, RefusalReason reason);
  void ServerFinal(Entry& e, RefusalReason reason);
  void TooFar(Entry& e, TimePoint now);

  void Dispatch(Entry& e, TimePoint now);
  void DispatchExit(Entry& exit, TimePoint now);
  void DispatchTouch(Entry& touch, TimePoint now);
  void FinalTouch(const RequestKey& enter_key, bool enter_refused);
  void AfterEnter(const RequestKey& enter_key, TimePoint now);
  void Advance(uint16_t peer, TimePoint now);
  void CloseFinals(uint16_t peer);

  std::map<RequestKey, Entry> entries_;
  std::map<uint16_t, std::set<Slot>> queue_;  // per peer: its pending keys, by order
  std::set<Slot> held_;                       // held keys, by arrival
  std::map<uint16_t, size_t> stored_;         // per peer: keys stored in this generation
  std::map<uint16_t, uint32_t> last_pose_;    // per peer, in the current generation (accepted)
  std::map<uint16_t, uint32_t> last_received_;  // per peer, in the current generation (stage 4)
  std::map<uint16_t, TimePoint> close_wait_;  // End session: the peers awaited, by deadline
  uint32_t generation_ = 0;
  uint32_t previous_generation_ = 0;  // the generation before the last change (the tomb window)
  bool closing_ = false;
  uint64_t next_order_ = 0;
  std::vector<RequestOutput> outputs_;
};

}  // namespace skylanders::online
