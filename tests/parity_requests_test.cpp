// The parity suite (stage 2 item 4; design sections 3.9, 9 item 4 and 10; Sol r1 F2): every cell of
// 1B's request table, on 1B's own table and on the generic one at once (tests/parity_twin.h).
//
// The cells are 1B's own bodies, copied by tools/parity_gen_cells.py from
// tests/online_requests_test.cpp at portal 3bbbcc6, 1B's merged commit on the online branch
// (requests.h, requests.cpp and the test file are the same there as at 26ef72d and 9054072, where
// the suite was first written; Sol F10, section 9 item 4): 67 cell functions, recounted there and
// numbered RP1 to RP67 in the order of 1B's main(). The script adds, each
// marked `parity:`, the generic checks of section 10's named cases and a port of Retryable's lines
// (1B's IsRetryable has no generic twin). Beside them: RP0 (the key map), RP-a and RP-b (section
// 3.9's order), RP8b (a pose-0 exit behind a pending enter, Opus F11), RP-c, RP-d and RP-e (the
// design-decided differences), and in tests/backend_parity_test.cpp RP19b (Client::Resend).
//
// D2: this file includes online/ headers (through parity_twin.h). It is test-only and outside
// src/backend and tests/backend*, the files the include rule covers (parity_twin.h's header).

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "backend_test.h"
#include "parity_twin.h"

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

namespace {

using namespace skylanders::online;
using namespace std::chrono_literals;
namespace gb = skylanders::backend;
namespace parity = skylanders::parity;
using H = parity::Twin;
// --- 1B's harness pieces (online_requests_test.cpp, verbatim but for struct H, which is the twin).

using RS = RequestState;
using RR = RefusalReason;
using OT = RequestOutput::Type;

enum class K { kEnter, kExit, kTouch, kTelegram, kSkip };
constexpr K kAllKinds[] = {K::kEnter, K::kExit, K::kTouch, K::kTelegram, K::kSkip};
// The kinds a resend after a retryable refusal exists for (an exit's retry is exit before enter).
constexpr K kResent[] = {K::kEnter, K::kTouch};
constexpr K kNeverResent[] = {K::kTelegram, K::kSkip};
// The kinds that are evaluated on their own once admitted (an exit follows its enter; a touch is
// admitted once its enter applied, which these tests bring first).
constexpr K kOwnChecks[] = {K::kEnter, K::kTouch, K::kTelegram, K::kSkip};

constexpr uint16_t kPeer = 2;
constexpr uint16_t kOther = 3;
constexpr uint32_t kSeen = 1;    // the pose a test sends first
constexpr uint32_t kUnseen = 9;  // a pose no test sends unless it says so

EntityRef Trig(uint32_t gen) { return WorldRef(gen, 2394, NameHash("JumpHint_Trigger"), NameHash("CTrigger")); }

WorldRequest Make(K k, uint32_t gen, uint32_t seq, uint32_t pose, uint16_t peer = kPeer) {
  WorldRequest r;
  r.subject = SelfRef(gen);
  r.pose_sequence = pose;
  switch (k) {
    case K::kEnter:
      r.key = TriggerRequestKey(peer, gen, Trig(gen), TriggerEvent::kEnter, seq);
      r.event_bits = 0x21;
      break;
    case K::kExit:
      r.key = TriggerRequestKey(peer, gen, Trig(gen), TriggerEvent::kExit, seq);
      r.event_bits = 0x22;
      break;
    case K::kTouch:
      r.key = TriggerRequestKey(peer, gen, Trig(gen), TriggerEvent::kTouch, seq);
      r.event_bits = 0x24;
      break;
    case K::kTelegram: {
      TelegramKey t;
      t.messenger = WorldRef(gen, 3846, NameHash("Birdee_A_Flyoff"), NameHash("CEntity"));
      t.list_owner_hash = NameHash("CMessengerComponent+24");
      t.index = 2;
      r.key = TelegramRequestKey(peer, gen, seq, t);
      break;
    }
    case K::kSkip:
      r.key = SkipRequestKey(peer, gen, SceneKey{1, WorldRef(gen, 77, NameHash("Cutscene_01"), 5)}, seq);
      break;
  }
  return r;
}

bool Applied(const WorldDecision& d, bool cached = false) {
  return d.outcome == DecisionOutcome::kApplied && d.final && !d.server_final && d.cached == cached &&
         d.applied_sequence != 0;
}
bool Pending(const WorldDecision& d, bool cached = false) {
  return d.outcome == DecisionOutcome::kPending && !d.final && d.cached == cached;
}
bool Refused(const WorldDecision& d, RR reason, bool final, bool server_final = false, bool cached = false) {
  return d.outcome == DecisionOutcome::kRefused && d.reason == reason && d.final == final &&
         d.server_final == server_final && d.cached == cached;
}
// The only decision since `mark` for `k` (and there is exactly one).
bool One(const H& h, const WorldRequest& r, size_t mark, WorldDecision* out) {
  const auto ds = h.Decisions(r.key, mark);
  if (ds.size() != 1) return false;
  *out = ds[0];
  return true;
}
// The decision since `mark` for `k` after the `pending` a key is answered when it is accepted:
// what its evaluation, a timeout or S made of it (and there is exactly one).
bool Decided(const H& h, const WorldRequest& r, size_t mark, WorldDecision* out) {
  auto ds = h.Decisions(r.key, mark);
  if (!ds.empty() && Pending(ds[0])) ds.erase(ds.begin());
  if (ds.size() != 1) return false;
  *out = ds[0];
  return true;
}

// The four invariants over everything `h` output (docs/online-1B.md section 2):
//   (1) every key sent ends in exactly one final decision (when `all_final`); nothing is decided
//       after it;
//   (2) nothing is applied after a key's final decision, a key is applied at most once and only
//       after its evaluation was asked for, nothing is evaluated after its final decision, and a
//       peer has at most one key evaluating at a time (its keys go in order);
//   (3) every final reaches the client before it closes the key: at each new LevelLoad, every key
//       of an older generation that came before it has its final; at each kPeerClosed (its Bye),
//       every key of that peer has its final;
//   (4) a cached answer repeats the key's last decision.
void CheckInvariants(const H& h, bool all_final = true) {
  std::map<RequestKey, WorldDecision> last;
  std::set<RequestKey> finals;
  std::set<RequestKey> evaluated;
  std::map<RequestKey, int> applies;
  std::map<uint16_t, RequestKey> in_flight;  // per peer: the key evaluating
  for (size_t i = 0; i <= h.log.size(); ++i) {
    for (const H::Load& load : h.level_loads) {
      if (load.at != i) continue;
      for (const H::Sent& s : h.sent) {
        if (s.ordinal < load.ordinal && s.key.generation < load.generation) CHECK(finals.count(s.key) == 1);
      }
    }
    if (i == h.log.size()) break;
    const RequestOutput& o = h.log[i];
    if (o.type == OT::kEvaluate) {
      const RequestKey& k = o.request.key;
      CHECK(o.peer == k.peer);
      CHECK(finals.count(k) == 0);
      CHECK(in_flight.count(k.peer) == 0);
      in_flight[k.peer] = k;
      evaluated.insert(k);
      continue;
    }
    if (o.type == OT::kPeerClosed) {
      for (const H::Sent& s : h.sent) {
        if (s.at <= i && s.key.peer == o.peer) CHECK(finals.count(s.key) == 1);
      }
      continue;
    }
    const WorldDecision& d = o.decision;
    CHECK(o.peer == d.key.peer);
    if (d.cached) {
      auto it = last.find(d.key);
      CHECK(it != last.end());
      if (it != last.end()) {
        WorldDecision repeat = it->second;
        repeat.cached = true;
        CHECK(repeat == d);
      }
      continue;
    }
    CHECK(finals.count(d.key) == 0);
    if (d.final) finals.insert(d.key);
    if (d.outcome == DecisionOutcome::kApplied) {
      CHECK(++applies[d.key] == 1);
      CHECK(evaluated.count(d.key) == 1);
    }
    auto f = in_flight.find(d.key.peer);
    if (f != in_flight.end() && f->second == d.key && d.outcome != DecisionOutcome::kPending) {
      in_flight.erase(f);
    }
    last[d.key] = d;
  }
  if (all_final) {
    for (const H::Sent& s : h.sent) CHECK(finals.count(s.key) == 1);
  }
  h.CheckGeneric(all_final);  // the same invariants over the generic table's log (parity)
}

// Ends the session the way End session does (closing, every peer's closed), then the invariants.
void EndAndCheck(H& h) {
  if (!h.table.Closing()) {
    std::set<uint16_t> peers;
    for (const H::Sent& s : h.sent) peers.insert(s.key.peer);
    h.Close(std::vector<uint16_t>(peers.begin(), peers.end()));
    for (const uint16_t p : peers) h.Closed(p);
  }
  CheckInvariants(h);
}

// What a touch or an exit needs first: its enter, applied (pose kSeen is sent). Nothing for the
// other kinds.
void Prereq(H& h, K k, uint32_t seq) {
  h.Pose(kSeen);
  if (k != K::kTouch && k != K::kExit) return;
  const WorldRequest enter = Make(K::kEnter, h.gen, seq, kSeen);
  if (h.table.StateOf(enter.key) != RS::kNew) return;
  h.verdicts[enter.key] = RR::kNone;
  const bool game = h.game;
  h.game = true;
  h.Send(enter);
  h.game = game;
}

// Puts a key of kind `k` into `state`. Pose kSeen is sent first; kUnseen is not.
WorldRequest Bring(H& h, K k, RS state, uint32_t seq = 1) {
  h.Pose(kSeen);
  switch (state) {
    case RS::kPending: {
      Prereq(h, k, seq);  // an exit or a touch of an applied enter is evaluated on its own
      const WorldRequest r = Make(k, h.gen, seq, kUnseen);
      h.Send(r);
      return r;
    }
    case RS::kRetry: {
      const WorldRequest r = Make(k, h.gen, seq, kSeen);
      if (k == K::kExit) {
        h.Send(r);  // no enter: held, then exit before enter
        h.Wait(kHeldExitWait);
      } else {
        Prereq(h, k, seq);
        h.verdicts[r.key] = RR::kTooFar;
        h.Send(r);
      }
      return r;
    }
    case RS::kApplied: {
      Prereq(h, k, seq);
      const WorldRequest r = Make(k, h.gen, seq, kSeen);
      h.Send(r);
      return r;
    }
    case RS::kRefused: {
      if (k == K::kExit) {
        const WorldRequest enter = Make(K::kEnter, h.gen, seq, kSeen);
        h.verdicts[enter.key] = RR::kUnresolved;
        h.Send(enter);
      } else {
        Prereq(h, k, seq);
      }
      const WorldRequest r = Make(k, h.gen, seq, kSeen);
      if (k != K::kExit) h.verdicts[r.key] = k == K::kSkip ? RR::kSceneEnded : RR::kUnresolved;
      h.Send(r);
      return r;
    }
    case RS::kHeld: {
      const WorldRequest r = Make(K::kExit, h.gen, seq, kSeen);  // no enter
      h.Send(r);
      return r;
    }
    case RS::kTomb: {
      const WorldRequest r = Bring(h, k, RS::kApplied, seq);
      h.NewGeneration();
      return r;
    }
    case RS::kNew:
      break;
  }
  return Make(k, h.gen, seq, kSeen);
}

// Puts a key of kind `k` into pending's evaluating sub-state: its pose seen, its kEvaluate out, the
// game thread's result not in (`game` is left off).
WorldRequest BringEvaluating(H& h, K k, uint32_t seq = 1) {
  Prereq(h, k, seq);
  h.game = false;
  const WorldRequest r = Make(k, h.gen, seq, kSeen);
  h.Send(r);
  CHECK(h.State(r) == RS::kPending && h.Evaluating(r) && h.Evaluations(r.key) == 1);
  return r;
}

// ---------------------------------------------------------------------------------------------
// Row new.

// The two phases: the key is answered `pending` and goes to evaluation; only the game thread's
// result decides it (applied, with the game's apply counter). A second result is dropped.
void NewRequestPassWithPose() {
  for (K k : kOwnChecks) {
    H h;
    Prereq(h, k, 1);
    h.game = false;
    const size_t mark = h.Mark();
    const WorldRequest r = Make(k, h.gen, 1, kSeen);
    h.Send(r);
    CHECK(h.State(r) == RS::kPending && h.Evaluating(r) && h.Evaluations(r.key) == 1);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Pending(d));  // no outcome before the game's
    CHECK(h.Submit(r.key));
    CHECK(h.State(r) == RS::kApplied && !h.Evaluating(r) && h.Applies(r.key) == 1);
    CHECK(Decided(h, r, mark, &d) && Applied(d) && d.applied_sequence == h.next_applied);
    const size_t after = h.Mark();
    CHECK(!h.Submit(r.key));
    CHECK(h.OutputsSince(after) == 0 && h.Applies(r.key) == 1);
    EndAndCheck(h);
  }
}

void NewRequestPassWithoutPose() {
  for (K k : kOwnChecks) {
    H h;
    Prereq(h, k, 1);
    const size_t mark = h.Mark();
    const WorldRequest r = Make(k, h.gen, 1, kUnseen);
    h.Send(r);
    CHECK(h.State(r) == RS::kPending && !h.Evaluating(r));
    CHECK(h.Evaluations(r.key) == 0 && h.Applies(r.key) == 0);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Pending(d));
    // Not asked for: a result for it is dropped.
    CHECK(!h.Submit(r.key) && h.State(r) == RS::kPending);
    EndAndCheck(h);
  }
}

void NewRequestRetryableRefusal() {
  for (RR reason : {RR::kTooFar, RR::kRate, RR::kNotReleased, RR::kNotInPlay}) {
    for (K k : kResent) {
      H h;
      Prereq(h, k, 1);
      h.verdict = reason;
      const size_t mark = h.Mark();
      const WorldRequest r = Make(k, h.gen, 1, kSeen);
      h.Send(r);
      CHECK(h.State(r) == RS::kRetry);
      WorldDecision d;
      CHECK(Decided(h, r, mark, &d) && Refused(d, reason, false));
      EndAndCheck(h);
    }
    // A telegram or a skip is never resent: every refusal of one is terminal.
    for (K k : kNeverResent) {
      H h;
      Prereq(h, k, 1);
      h.verdict = reason;
      const WorldRequest r = Make(k, h.gen, 1, kSeen);
      h.Send(r);
      CHECK(h.State(r) == RS::kRefused);
      WorldDecision d;
      CHECK(Decided(h, r, 0, &d) && Refused(d, reason, true));
      EndAndCheck(h);
    }
  }
}

void NewRequestTerminalRefusal() {
  for (RR reason : {RR::kAlreadySent, RR::kUnresolved, RR::kUnsupported, RR::kSceneEnded}) {
    for (K k : kOwnChecks) {
      H h;
      Prereq(h, k, 1);
      h.verdict = reason;
      const size_t mark = h.Mark();
      const WorldRequest r = Make(k, h.gen, 1, kSeen);
      h.Send(r);
      CHECK(h.State(r) == RS::kRefused);
      WorldDecision d;
      CHECK(Decided(h, r, mark, &d) && Refused(d, reason, true));
      EndAndCheck(h);
    }
  }
}

// A first send that was in flight at the change: `stale`, terminal, kept as a tomb; for an exit
// this runs before the exit column (an old-generation exit is never held).
void NewRequestOfALeftGeneration() {
  for (K k : kAllKinds) {
    H h;
    const uint32_t old = h.gen;
    h.NewGeneration();
    const WorldRequest r = Make(k, old, 1, kSeen);
    h.Send(r);
    CHECK(h.State(r) == RS::kTomb);
    CHECK(h.checks == 0 && h.Evaluations(r.key) == 0);
    WorldDecision d;
    CHECK(One(h, r, 0, &d) && Refused(d, RR::kStale, true));
    // A duplicate is answered from the tomb.
    const size_t mark = h.Mark();
    h.Send(r);
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kStale, true, false, true));
    EndAndCheck(h);
  }
}

// Older than the tombstones (two generations back): `stale`, not kept.
void NewRequestOlderThanTheTombs() {
  for (K k : kAllKinds) {
    H h;
    const uint32_t old = h.gen;
    h.NewGeneration();
    h.NewGeneration();
    const WorldRequest r = Make(k, old, 1, kSeen);
    h.Send(r);
    CHECK(h.State(r) == RS::kNew);
    WorldDecision d;
    CHECK(One(h, r, 0, &d) && Refused(d, RR::kStale, true));
    EndAndCheck(h);
  }
  // A generation S has not opened (forged): the same.
  H h;
  const WorldRequest r = Make(K::kEnter, h.gen + 1, 1, kSeen);
  h.Send(r);
  CHECK(h.State(r) == RS::kNew && h.checks == 0);
  WorldDecision d;
  CHECK(One(h, r, 0, &d) && Refused(d, RR::kStale, true));
  EndAndCheck(h);
}

void NewRequestAfterClosing() {
  for (K k : kAllKinds) {
    H h;
    h.Close();
    const WorldRequest r = Make(k, h.gen, 1, kSeen);
    h.Pose(kSeen);
    h.Send(r);
    CHECK(h.State(r) == RS::kRefused && h.checks == 0 && h.Evaluations(r.key) == 0);
    WorldDecision d;
    CHECK(One(h, r, 0, &d) && Refused(d, RR::kSessionEnded, true));
    h.Closed();
    CheckInvariants(h);
  }
}

// Pose sequence 0 means "no pose" only for a skip: a trigger or telegram request with 0 is refused
// `unsupported` (terminal) before anything else, never evaluated; a skip with 0 is evaluated.
void NewRequestPoseZero() {
  for (K k : {K::kEnter, K::kExit, K::kTouch, K::kTelegram}) {
    H h;
    Prereq(h, k, 1);  // an exit or a touch with an applied enter: still refused first
    const size_t mark = h.Mark();
    const WorldRequest r = Make(k, h.gen, 1, 0);
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kUnsupported, true));
    CHECK(h.State(r) == RS::kRefused && h.Evaluations(r.key) == 0);
    const auto g = h.GenericLast(r.key);  // parity: requires_precondition (Opus F11)
    CHECK(g && g->reason == gb::Reason::kUnsupported && g->final);
    EndAndCheck(h);
  }
  {  // a resend of an enter in retry with pose 0: the same
    H h;
    const WorldRequest r = Bring(h, K::kEnter, RS::kRetry);
    WorldRequest zero = r;
    zero.pose_sequence = 0;
    const size_t mark = h.Mark();
    h.Send(zero);
    WorldDecision d;
    CHECK(One(h, zero, mark, &d) && Refused(d, RR::kUnsupported, true) && h.Evaluations(r.key) == 1);
    EndAndCheck(h);
  }
  H h;
  const WorldRequest skip = Make(K::kSkip, h.gen, 1, 0);
  h.Send(skip);
  CHECK(h.Evaluations(skip.key) == 1 && h.State(skip) == RS::kApplied);
  EndAndCheck(h);
}

// Past kMaxKeysPerPeer keys in a generation a peer's new key is refused `rate` (final, terminal for
// any kind) and not stored; the other peers are not touched, and the next generation starts again.
void NewRequestPastTheKeyCap() {
  H h;
  h.Pose(kSeen);
  for (uint32_t i = 1; i <= kMaxKeysPerPeer; ++i) h.Send(Make(K::kTelegram, h.gen, i, kSeen));
  CHECK(h.table.Size() == kMaxKeysPerPeer);
  const WorldRequest over = Make(K::kEnter, h.gen, 1, kSeen);
  h.Send(over);
  WorldDecision d;
  CHECK(One(h, over, 0, &d) && Refused(d, RR::kRate, true));
  CHECK(h.State(over) == RS::kNew && h.table.Size() == kMaxKeysPerPeer && h.Evaluations(over.key) == 0);
  h.Pose(kSeen, kOther);
  const WorldRequest other = Make(K::kEnter, h.gen, 1, kSeen, kOther);
  h.Send(other);
  CHECK(h.State(other) == RS::kApplied);
  h.NewGeneration();
  h.Pose(kSeen);
  const WorldRequest next = Make(K::kEnter, h.gen, 1, kSeen);
  h.Send(next);
  CHECK(h.State(next) == RS::kApplied);
  EndAndCheck(h);
  // Not stored, so a duplicate is answered the same again (not from a state).
  const size_t mark = h.Mark();
  h.table.Reset(1);
  h.gen = 1;
  h.Pose(kSeen);
  for (uint32_t i = 1; i <= kMaxKeysPerPeer; ++i) h.table.OnRequest(Make(K::kTelegram, 1, i, kSeen), h.now);
  h.Take();
  h.table.OnRequest(over, h.now);
  h.table.OnRequest(over, h.now);
  h.Take();
  const auto ds = h.Decisions(over.key, mark);
  REQUIRE_OR_RETURN_VOID(ds.size() == 2);
  CHECK(Refused(ds[0], RR::kRate, true) && ds[1] == ds[0]);
}

// The dashes of row new: a pose, a wait, a generation change with no key do nothing.
void NewNothingElse() {
  H h;
  CHECK(h.Pose(kSeen));
  h.Service();
  h.Wait(kPoseWait + 100ms);
  h.NewGeneration();
  CHECK(h.log.empty() && h.table.Size() == 0);
  EndAndCheck(h);
}

// Row new, the exit column: an exit with no enter is held (answered `pending`) for up to 500 ms,
// then refused `exit before enter` (retry); only S's final at the generation change closes it,
// before the new LevelLoad.
void NewExitWithNoEnter() {
  H h;
  h.Pose(kSeen);
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
  h.Send(exit);
  CHECK(h.State(exit) == RS::kHeld);
  WorldDecision d;
  CHECK(One(h, exit, 0, &d) && Pending(d));
  size_t mark = h.Mark();
  h.Wait(kHeldExitWait - 1ms);
  CHECK(h.OutputsSince(mark) == 0 && h.State(exit) == RS::kHeld);
  h.Wait(1ms);
  CHECK(h.State(exit) == RS::kRetry);
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kExitBeforeEnter, false));
  {  // parity: the generic words (section 3.9): `parent missing`, retryable
    const auto g = h.GenericLast(exit.key);
    CHECK(g && g->reason == gb::Reason::kParentMissing && !g->final);
  }
  mark = h.Mark();
  h.NewGeneration();
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kExitBeforeEnter, true, true));
  CHECK(h.State(exit) == RS::kTomb);
  CheckInvariants(h);
  EndAndCheck(h);
}

// A touch with no enter is held like an exit, then refused `enter refused` (terminal).
void NewTouchWithNoEnter() {
  H h;
  h.Pose(kSeen);
  const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
  h.Send(touch);
  CHECK(h.State(touch) == RS::kHeld);
  WorldDecision d;
  CHECK(One(h, touch, 0, &d) && Pending(d));
  const size_t mark = h.Mark();
  h.Wait(kHeldExitWait - 1ms);
  CHECK(h.OutputsSince(mark) == 0);
  h.Wait(1ms);
  CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true));
  CHECK(h.State(touch) == RS::kRefused && h.Evaluations(touch.key) == 0);
  {  // parity: the same rule with parent_missing_retryable unset: `parent missing`, terminal
    const auto g = h.GenericLast(touch.key);
    CHECK(g && g->reason == gb::Reason::kParentMissing && g->final && !g->server_final);
  }
  EndAndCheck(h);
}

// ---------------------------------------------------------------------------------------------
// Row pending (waiting for its pose, and evaluating on the game thread).

void PendingRequestAgain() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kPending);
    const int checks = h.checks;
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Pending(d, true));
    CHECK(h.checks == checks && h.State(r) == RS::kPending);
    EndAndCheck(h);
  }
  // Evaluating: the same, and no second evaluation.
  for (K k : kOwnChecks) {
    H h;
    const WorldRequest r = BringEvaluating(h, k);
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Pending(d, true));
    CHECK(h.Evaluating(r) && h.Evaluations(r.key) == 1);
    CHECK(h.Submit(r.key) && h.State(r) == RS::kApplied);
    EndAndCheck(h);
  }
}

// Its pose arrives: it goes to evaluation. A pass applies; a retryable refusal is retry for an
// enter or a touch and terminal for the rest; a terminal refusal is refused.
void PendingPoseArrives() {
  for (K k : kAllKinds) {
    for (RR verdict : {RR::kNone, RR::kTooFar, RR::kUnresolved}) {
      H h;
      const WorldRequest r = Bring(h, k, RS::kPending);
      h.verdicts[r.key] = verdict;
      const size_t mark = h.Mark();
      h.Service();  // no pose yet: nothing
      CHECK(h.OutputsSince(mark) == 0);
      CHECK(h.Pose(kUnseen));  // parity: Server::OnPrecondition on the generic table
      h.Service();
      CHECK(h.Evaluations(r.key) == 1);
      WorldDecision d;
      CHECK(One(h, r, mark, &d));
      if (verdict == RR::kNone) {
        CHECK(Applied(d) && h.State(r) == RS::kApplied && h.Applies(r.key) == 1);
      } else if (verdict == RR::kTooFar && (k == K::kEnter || k == K::kTouch)) {
        CHECK(Refused(d, RR::kTooFar, false) && h.State(r) == RS::kRetry);
      } else {
        CHECK(Refused(d, verdict, true) && h.State(r) == RS::kRefused);
      }
      EndAndCheck(h);
    }
  }
}

// The 500 ms pose wait ends: too_far (pose late), retry for an enter or a touch, refused for an
// exit, a telegram or a skip; never evaluated.
void PendingPoseWaitEnds() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kPending);
    const size_t mark = h.Mark();
    h.Wait(kPoseWait - 1ms);
    CHECK(h.OutputsSince(mark) == 0);
    h.Wait(1ms);
    WorldDecision d;
    CHECK(One(h, r, mark, &d));
    if (k == K::kEnter || k == K::kTouch) {
      CHECK(Refused(d, RR::kTooFar, false) && h.State(r) == RS::kRetry);
    } else {
      CHECK(Refused(d, RR::kTooFar, true) && h.State(r) == RS::kRefused);
    }
    CHECK(h.Applies(r.key) == 0 && h.Evaluations(r.key) == 0);
    EndAndCheck(h);
  }
  // An evaluating key has its pose: it does not time out, however long the game takes.
  H h;
  const WorldRequest r = BringEvaluating(h, K::kEnter);
  const size_t mark = h.Mark();
  h.Wait(kPoseWait * 3);
  CHECK(h.OutputsSince(mark) == 0 && h.Evaluating(r));
  CHECK(h.Submit(r.key) && h.State(r) == RS::kApplied);
  EndAndCheck(h);
}

// The exit of a pending enter is held until the enter leaves pending, then follows its state.
void PendingEnterThenItsExit() {
  {  // the enter is applied: the exit is evaluated under its own key, after the enter
    H h;
    const WorldRequest enter = Bring(h, K::kEnter, RS::kPending);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kUnseen);
    h.Send(exit);
    CHECK(h.State(exit) == RS::kHeld);
    WorldDecision d;
    CHECK(One(h, exit, 0, &d) && Pending(d));
    h.Pose(kUnseen);
    const size_t mark = h.Mark();
    h.Service();
    CHECK(h.State(enter) == RS::kApplied && h.State(exit) == RS::kApplied);
    CHECK(One(h, exit, mark, &d) && Applied(d));  // it was answered `pending` when it was held
    CHECK(h.At(OT::kDecision, enter.key, mark) < h.At(OT::kEvaluate, exit.key, mark));
    EndAndCheck(h);
  }
  {  // the enter's pose never comes: too_far (retry), then the exit column of retry
    H h;
    const WorldRequest enter = Bring(h, K::kEnter, RS::kPending);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
    h.Send(exit);
    const size_t mark = h.Mark();
    h.Wait(kPoseWait);
    const auto ds = h.Decisions(enter.key, mark);
    REQUIRE_OR_RETURN_VOID(ds.size() == 2);
    CHECK(Refused(ds[0], RR::kTooFar, false));
    CHECK(Refused(ds[1], RR::kTooFar, true, true));
    WorldDecision d;
    CHECK(One(h, exit, mark, &d) && Refused(d, RR::kEnterRefused, true));
    CHECK(h.State(enter) == RS::kRefused && h.State(exit) == RS::kRefused);
    EndAndCheck(h);
  }
  {  // the enter is evaluating: the exit is held until the game's result
    H h;
    const WorldRequest enter = BringEvaluating(h, K::kEnter);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
    h.Send(exit);
    CHECK(h.State(exit) == RS::kHeld);
    h.Wait(kHeldExitWait * 2);  // behind its enter: no timeout
    CHECK(h.State(exit) == RS::kHeld);
    CHECK(h.Submit(enter.key));
    CHECK(h.State(exit) == RS::kPending && h.Evaluating(exit));
    CHECK(h.Submit(exit.key) && h.State(exit) == RS::kApplied);
    EndAndCheck(h);
  }
}

void PendingGenerationChange() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kPending);
    const size_t mark = h.Mark();
    h.NewGeneration();
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kGenerationEnded, true, true));
    CHECK(h.State(r) == RS::kTomb);
    // The queued work is cancelled: the old pose does nothing.
    h.Pose(kUnseen);
    h.Service();
    CHECK(h.Applies(r.key) == 0 && h.Evaluations(r.key) == 0);
    EndAndCheck(h);
  }
  // Evaluating: S's final `generation ended` before the new LevelLoad; the game's late result
  // (applied or not) is dropped and nothing more is output for it.
  for (K k : kOwnChecks) {
    for (RR late : {RR::kNone, RR::kTooFar}) {
      H h;
      const WorldRequest r = BringEvaluating(h, k);
      const size_t mark = h.Mark();
      h.NewGeneration();
      WorldDecision d;
      CHECK(One(h, r, mark, &d) && Refused(d, RR::kGenerationEnded, true, true));
      CHECK(h.State(r) == RS::kTomb && !h.Evaluating(r));
      const size_t after = h.Mark();
      CHECK(!h.Submit(r.key, late));
      CHECK(h.OutputsSince(after) == 0 && h.Applies(r.key) == 0);
      // A duplicate gets the final, cached.
      h.Send(r);
      CHECK(One(h, r, after, &d) && Refused(d, RR::kGenerationEnded, true, true, true));
      EndAndCheck(h);
    }
  }
}

void PendingEndSession() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kPending);
    h.Close();
    h.Pose(kUnseen);  // the queues are cancelled: nothing is evaluated
    h.Service();
    CHECK(h.Evaluations(r.key) == 0 && h.State(r) == RS::kPending);
    const size_t mark = h.Mark();
    h.Closed();
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kSessionEnded, true, true));
    REQUIRE_OR_RETURN_VOID(!h.log.empty());
    CHECK(h.log.back().type == OT::kPeerClosed && !h.log.back().gone);
    CheckInvariants(h);
  }
  // Evaluating: End session drops the evaluation; the late result is dropped; S's final `session
  // ended` goes with the peer's finals, before its Bye.
  for (K k : kOwnChecks) {
    H h;
    const WorldRequest r = BringEvaluating(h, k);
    size_t mark = h.Mark();
    h.Close();
    CHECK(h.State(r) == RS::kPending && !h.Evaluating(r));
    CHECK(!h.Submit(r.key));
    CHECK(h.OutputsSince(mark) == 0 && h.Applies(r.key) == 0);
    h.Closed();
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kSessionEnded, true, true));
    CHECK(h.log.back().type == OT::kPeerClosed);
    CheckInvariants(h);
  }
  {  // evaluating, and a duplicate during closing: refused `session ended`, nothing owed at closed
    H h;
    const WorldRequest r = BringEvaluating(h, K::kTelegram);
    h.Close();
    size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kSessionEnded, true));
    CHECK(!h.Submit(r.key));
    mark = h.Mark();
    h.Closed();
    CHECK(h.Decisions(r.key, mark).empty());
    CheckInvariants(h);
  }
}

// ---------------------------------------------------------------------------------------------
// Row retry (an enter, a touch, an exit; a telegram or a skip never gets there).

void RetryResendChecksAgain() {
  for (K k : kResent) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kRetry);
    h.verdicts.erase(r.key);
    const int checks = h.checks;
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(h.checks == checks + 1);
    CHECK(Decided(h, r, mark, &d) && Applied(d) && h.Applies(r.key) == 1);
    EndAndCheck(h);
  }
  {  // still too far: retry again, not cached
    H h;
    const WorldRequest r = Bring(h, K::kEnter, RS::kRetry);
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(Decided(h, r, mark, &d) && Refused(d, RR::kTooFar, false) && h.State(r) == RS::kRetry);
    EndAndCheck(h);
  }
  {  // an exit refused `exit before enter`, sent again (a broken client): the exit column again
    H h;
    const WorldRequest exit = Bring(h, K::kExit, RS::kRetry);
    const size_t mark = h.Mark();
    h.Send(exit);
    WorldDecision d;
    CHECK(One(h, exit, mark, &d) && Pending(d) && h.State(exit) == RS::kHeld);
    EndAndCheck(h);
  }
}

void RetryNothingOnPoseOrWait() {
  for (K k : {K::kEnter, K::kTouch, K::kExit}) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kRetry);
    const size_t mark = h.Mark();
    h.Pose(kUnseen);
    h.Service();
    h.Wait(kPoseWait * 3);
    CHECK(h.OutputsSince(mark) == 0 && h.State(r) == RS::kRetry);
    EndAndCheck(h);
  }
}

// The exit of an enter in retry: S's final for the enter (its last reason), then the exit refused
// `enter refused`. A touch of an enter in retry never gets to retry itself: it is refused `enter
// refused` when it comes (the touch gate), so the exit has no touch to close.
void RetryEnterThenItsExit() {
  H h;
  const WorldRequest enter = Bring(h, K::kEnter, RS::kRetry);
  const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
  size_t mark = h.Mark();
  h.Send(touch);
  WorldDecision d;
  CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true) && h.Evaluations(touch.key) == 0);
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
  mark = h.Mark();
  h.Send(exit);
  REQUIRE_OR_RETURN_VOID(h.OutputsSince(mark) == 2);
  CHECK(h.log[mark].decision.key == enter.key && Refused(h.log[mark].decision, RR::kTooFar, true, true));
  CHECK(h.log[mark + 1].decision.key == exit.key && Refused(h.log[mark + 1].decision, RR::kEnterRefused, true));
  CHECK(h.State(enter) == RS::kRefused && h.State(touch) == RS::kRefused && h.State(exit) == RS::kRefused);
  CHECK(h.Applies(enter.key) == 0 && h.Applies(exit.key) == 0);
  EndAndCheck(h);
}

// A touch in retry when the exit of its (applied) enter comes: S's final for the touch.
void RetryTouchThenTheExit() {
  H h;
  const WorldRequest enter = Bring(h, K::kEnter, RS::kApplied);
  const WorldRequest touch = Bring(h, K::kTouch, RS::kRetry);
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
  const size_t mark = h.Mark();
  h.Send(exit);
  WorldDecision d;
  CHECK(One(h, touch, mark, &d) && Refused(d, RR::kTooFar, true, true));
  CHECK(Decided(h, exit, mark, &d) && Applied(d));
  CHECK(h.State(enter) == RS::kApplied);
  // A touch that turns retry after its exit was dispatched (it was pending then, the exit behind
  // it in the peer's queue) gets S's final at once: the hero left, so nothing would ever resend it.
  // The exit is evaluated only after it.
  H g;
  g.Send(Make(K::kEnter, g.gen, 2, kSeen));  // pose kSeen not sent yet: pending
  const WorldRequest late_touch = Make(K::kTouch, g.gen, 2, kUnseen);
  g.Send(late_touch);  // held behind its pending enter
  CHECK(g.State(late_touch) == RS::kHeld);
  g.Pose(kSeen);
  g.Service();  // the enter applies; the touch is admitted and waits for its pose
  CHECK(g.State(late_touch) == RS::kPending);
  const WorldRequest exit2 = Make(K::kExit, g.gen, 2, kSeen);
  g.Send(exit2);
  CHECK(g.State(exit2) == RS::kPending && !g.Evaluating(exit2));  // behind the touch
  const size_t m2 = g.Mark();
  g.Wait(kPoseWait);  // the touch's pose never came
  const auto ds = g.Decisions(late_touch.key, m2);
  REQUIRE_OR_RETURN_VOID(ds.size() == 2);
  CHECK(Refused(ds[0], RR::kTooFar, false) && Refused(ds[1], RR::kTooFar, true, true));
  CHECK(g.State(exit2) == RS::kApplied);
  CHECK(g.At(OT::kDecision, late_touch.key, m2) < g.At(OT::kEvaluate, exit2.key, m2));
  EndAndCheck(h);
  EndAndCheck(g);
}

void RetryGenerationChange() {
  for (K k : {K::kEnter, K::kTouch, K::kExit}) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kRetry);
    const RR last = k == K::kExit ? RR::kExitBeforeEnter : RR::kTooFar;
    const size_t mark = h.Mark();
    h.NewGeneration();
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, last, true, true) && h.State(r) == RS::kTomb);
    EndAndCheck(h);
  }
}

void RetryEndSession() {
  for (K k : {K::kEnter, K::kTouch, K::kExit}) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kRetry);
    const RR last = k == K::kExit ? RR::kExitBeforeEnter : RR::kTooFar;
    h.Close();
    const size_t mark = h.Mark();
    h.Closed();
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, last, true, true));
    CheckInvariants(h);
  }
  {  // a resend during closing: refused `session ended`, and no final is owed at closed
    H h;
    const WorldRequest r = Bring(h, K::kEnter, RS::kRetry);
    h.verdicts.erase(r.key);
    h.Close();
    size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kSessionEnded, true) && h.Applies(r.key) == 0);
    mark = h.Mark();
    h.Closed();
    CHECK(h.Decisions(r.key, mark).empty());
    CheckInvariants(h);
  }
}

// ---------------------------------------------------------------------------------------------
// Row applied.

void AppliedRequestAgain() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kApplied);
    const WorldDecision first = h.Decisions(r.key).back();
    h.verdict = RR::kUnresolved;  // the other checks would now fail: the cache answers first
    h.verdicts.clear();
    const int checks = h.checks;
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Applied(d, true));
    CHECK(d.applied_sequence == first.applied_sequence && h.checks == checks && h.Applies(r.key) == 1);
    EndAndCheck(h);
  }
}

void AppliedNothingOnPoseOrWait() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kApplied);
    const size_t mark = h.Mark();
    h.Pose(kUnseen);
    h.Service();
    h.Wait(kPoseWait * 2);
    CHECK(h.OutputsSince(mark) == 0 && h.State(r) == RS::kApplied);
    EndAndCheck(h);
  }
}

// The exit of an applied enter goes to evaluation under its own key.
void AppliedEnterThenItsExit() {
  for (RR verdict : {RR::kNone, RR::kTooFar, RR::kUnresolved}) {
    H h;
    const WorldRequest enter = Bring(h, K::kEnter, RS::kApplied);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
    h.verdicts[exit.key] = verdict;
    h.Send(exit);
    WorldDecision d;
    CHECK(Decided(h, exit, 0, &d));
    if (verdict == RR::kNone) {
      CHECK(Applied(d) && h.Applies(exit.key) == 1);
    } else {
      CHECK(Refused(d, verdict, true));  // too_far is terminal for an exit
    }
    CHECK(h.State(enter) == RS::kApplied && h.Evaluations(exit.key) == 1);
    EndAndCheck(h);
  }
  {  // its pose not seen yet: pending under its own key
    H h;
    Bring(h, K::kEnter, RS::kApplied);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kUnseen);
    h.Send(exit);
    CHECK(h.State(exit) == RS::kPending && h.Evaluations(exit.key) == 0);
    EndAndCheck(h);
  }
}

void AppliedGenerationChange() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kApplied);
    const size_t mark = h.Mark();
    h.NewGeneration();
    CHECK(h.OutputsSince(mark) == 0 && h.State(r) == RS::kTomb);
    EndAndCheck(h);
  }
}

void AppliedEndSession() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kApplied);
    h.Close();
    const size_t mark = h.Mark();
    h.Send(r);  // a duplicate during closing: from its state
    h.Closed();
    REQUIRE_OR_RETURN_VOID(h.OutputsSince(mark) == 2);
    CHECK(Applied(h.log[mark].decision, true));
    CHECK(h.log[mark + 1].type == OT::kPeerClosed);
    CheckInvariants(h);
  }
}

// ---------------------------------------------------------------------------------------------
// Row refused.

void RefusedRequestAgain() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kRefused);
    const WorldDecision first = h.Decisions(r.key).back();
    h.verdicts.clear();  // it would pass now: no new evaluation
    const int checks = h.checks;
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, first.reason, true, false, true));
    CHECK(h.checks == checks && h.Applies(r.key) == 0);
    EndAndCheck(h);
  }
}

void RefusedNothingOnPoseOrWait() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kRefused);
    const size_t mark = h.Mark();
    h.Pose(kUnseen);
    h.Service();
    h.Wait(kPoseWait * 2);
    CHECK(h.OutputsSince(mark) == 0 && h.State(r) == RS::kRefused);
    EndAndCheck(h);
  }
}

// The exit (and a touch) of a refused enter: refused `enter refused`, never evaluated.
void RefusedEnterThenItsExit() {
  H h;
  const WorldRequest enter = Bring(h, K::kEnter, RS::kRefused);
  h.verdicts.clear();
  const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
  h.Send(touch);
  WorldDecision d;
  CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true));
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
  h.Send(exit);
  CHECK(One(h, exit, 0, &d) && Refused(d, RR::kEnterRefused, true));
  CHECK(h.Applies(exit.key) == 0 && h.Evaluations(exit.key) == 0 && h.Evaluations(touch.key) == 0);
  CHECK(h.State(enter) == RS::kRefused);
  EndAndCheck(h);
}

void RefusedGenerationChangeAndEndSession() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kRefused);
    size_t mark = h.Mark();
    h.NewGeneration();
    CHECK(h.OutputsSince(mark) == 0 && h.State(r) == RS::kTomb);
    EndAndCheck(h);
    H e;
    const WorldRequest s = Bring(e, k, RS::kRefused);
    e.Close();
    mark = e.Mark();
    e.Closed();
    CHECK(e.Decisions(s.key, mark).empty());
    CheckInvariants(e);
  }
}

// ---------------------------------------------------------------------------------------------
// Row held (an exit, or a touch behind its enter).

void HeldRequestAgain() {
  H h;
  const WorldRequest exit = Bring(h, K::kExit, RS::kHeld);
  const size_t mark = h.Mark();
  h.Send(exit);
  WorldDecision d;
  CHECK(One(h, exit, mark, &d) && Pending(d, true) && h.State(exit) == RS::kHeld);
  EndAndCheck(h);
  // Held behind a pending enter: the same, for an exit and a touch.
  for (K k : {K::kExit, K::kTouch}) {
    H g;
    Bring(g, K::kEnter, RS::kPending);
    const WorldRequest behind = Make(k, g.gen, 1, kSeen);
    g.Send(behind);
    const size_t m2 = g.Mark();
    g.Send(behind);
    CHECK(One(g, behind, m2, &d) && Pending(d, true) && g.State(behind) == RS::kHeld);
    EndAndCheck(g);
  }
}

void HeldNothingOnPose() {
  H h;
  const WorldRequest exit = Bring(h, K::kExit, RS::kHeld);
  const size_t mark = h.Mark();
  h.Pose(kUnseen);
  h.Service();
  CHECK(h.OutputsSince(mark) == 0 && h.State(exit) == RS::kHeld);
  EndAndCheck(h);
}

// The wait ends with no enter: `exit before enter` (retry). Behind a pending enter, the exit does
// not time out by itself: it waits for its enter's own decision.
void HeldWaitEnds() {
  H h;
  const WorldRequest exit = Bring(h, K::kExit, RS::kHeld);
  const size_t mark = h.Mark();
  h.Wait(kHeldExitWait);
  WorldDecision d;
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kExitBeforeEnter, false) && h.State(exit) == RS::kRetry);
  EndAndCheck(h);
  H g;
  const WorldRequest behind = Make(K::kExit, g.gen, 1, kSeen);
  g.Send(behind);
  g.Wait(kHeldExitWait - 100ms);
  const WorldRequest enter = Make(K::kEnter, g.gen, 1, kUnseen);
  g.Send(enter);  // pending; the exit is now behind it
  g.Wait(200ms);  // past the exit's own 500 ms: still held behind the enter
  CHECK(g.State(behind) == RS::kHeld && g.State(enter) == RS::kPending);
  EndAndCheck(g);
}

void HeldGenerationChange() {
  H h;
  const WorldRequest enter = Bring(h, K::kEnter, RS::kPending);
  const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
  h.Send(touch);
  h.Send(exit);
  const size_t mark = h.Mark();
  h.NewGeneration();
  // One final each; the enter's move to tomb does not re-dispatch the held ones.
  WorldDecision d;
  CHECK(One(h, enter, mark, &d) && Refused(d, RR::kGenerationEnded, true, true));
  CHECK(One(h, touch, mark, &d) && Refused(d, RR::kGenerationEnded, true, true));
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kGenerationEnded, true, true));
  CHECK(h.State(enter) == RS::kTomb && h.State(touch) == RS::kTomb && h.State(exit) == RS::kTomb);
  EndAndCheck(h);
  H g;
  const WorldRequest alone = Bring(g, K::kExit, RS::kHeld);
  const size_t m2 = g.Mark();
  g.NewGeneration();
  CHECK(One(g, alone, m2, &d) && Refused(d, RR::kGenerationEnded, true, true));
  EndAndCheck(g);
}

void HeldEndSession() {
  H h;
  const WorldRequest exit = Bring(h, K::kExit, RS::kHeld);
  h.Close();
  h.Wait(kHeldExitWait);  // closing: the hold does not time out into a retry
  CHECK(h.State(exit) == RS::kHeld);
  const size_t mark = h.Mark();
  h.Closed();
  WorldDecision d;
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kSessionEnded, true, true));
  CheckInvariants(h);
  // A touch held behind a pending enter: the same; the enter refused during closing does not
  // dispatch it.
  H g;
  const WorldRequest enter = Bring(g, K::kEnter, RS::kPending);
  const WorldRequest touch = Make(K::kTouch, g.gen, 1, kSeen);
  g.Send(touch);
  g.Close();
  g.Send(enter);  // refused `session ended`
  CHECK(g.State(touch) == RS::kHeld);
  const size_t m2 = g.Mark();
  g.Closed();
  CHECK(One(g, touch, m2, &d) && Refused(d, RR::kSessionEnded, true, true));
  CheckInvariants(g);
}

// ---------------------------------------------------------------------------------------------
// Row tomb.

void TombRequestAgain() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kTomb);
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Applied(d, true) && h.State(r) == RS::kTomb);
    EndAndCheck(h);
  }
  {  // a tomb made by S's final repeats that final
    H h;
    const WorldRequest r = Bring(h, K::kEnter, RS::kPending);
    h.NewGeneration();
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kGenerationEnded, true, true, true));
    EndAndCheck(h);
  }
  {  // during End session's closing too: a tomb has its final decision, so it answers from it
    H h;
    const WorldRequest r = Bring(h, K::kEnter, RS::kTomb);
    h.Close();
    const size_t mark = h.Mark();
    h.Send(r);
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Applied(d, true));
    h.Closed();
    CheckInvariants(h);
  }
}

void TombNothingOnPoseOrWait() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kTomb);
    const size_t mark = h.Mark();
    h.Pose(kUnseen);
    h.Service();
    h.Wait(kPoseWait * 2);
    CHECK(h.OutputsSince(mark) == 0 && h.State(r) == RS::kTomb);
    EndAndCheck(h);
  }
}

// Dropped at the next generation change after the one that made it a tomb.
void TombDroppedNextGeneration() {
  for (K k : kAllKinds) {
    H h;
    const WorldRequest r = Bring(h, k, RS::kTomb);
    const size_t mark = h.Mark();
    h.NewGeneration();
    CHECK(h.OutputsSince(mark) == 0 && h.State(r) == RS::kNew && h.table.Size() == 0);
    EndAndCheck(h);
  }
}

// ---------------------------------------------------------------------------------------------
// The touch gate.

// A touch behind a pending enter is held; it follows the enter's decision: applied lets it through
// to its own evaluation (after the enter's), a refusal or a retry refuses it `enter refused`.
void TouchFollowsItsEnter() {
  {  // the enter applies after its pose: the touch is evaluated next
    H h;
    const WorldRequest enter = Bring(h, K::kEnter, RS::kPending);
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kUnseen);
    h.Send(touch);
    CHECK(h.State(touch) == RS::kHeld);
    h.Pose(kUnseen);
    const size_t mark = h.Mark();
    h.Service();
    WorldDecision d;
    CHECK(One(h, touch, mark, &d) && Applied(d));
    CHECK(h.At(OT::kDecision, enter.key, mark) < h.At(OT::kEvaluate, touch.key, mark));
    EndAndCheck(h);
  }
  {  // the enter is evaluating
    H h;
    const WorldRequest enter = BringEvaluating(h, K::kEnter);
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(touch);
    CHECK(h.State(touch) == RS::kHeld && h.Evaluations(touch.key) == 0);
    CHECK(h.Submit(enter.key));
    CHECK(h.State(touch) == RS::kPending && h.Evaluating(touch));
    CHECK(h.Submit(touch.key) && h.State(touch) == RS::kApplied);
    EndAndCheck(h);
  }
  for (RR verdict : {RR::kUnresolved, RR::kNotInPlay}) {  // the enter is refused, or retry
    H h;
    const WorldRequest enter = BringEvaluating(h, K::kEnter);
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(touch);
    const size_t mark = h.Mark();
    CHECK(h.Submit(enter.key, verdict));
    CHECK(h.State(enter) == (verdict == RR::kUnresolved ? RS::kRefused : RS::kRetry));
    WorldDecision d;
    CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true));
    CHECK(h.Evaluations(touch.key) == 0);
    EndAndCheck(h);
  }
  {  // the enter's pose never comes: too_far (retry) refuses the touch
    H h;
    const WorldRequest enter = Bring(h, K::kEnter, RS::kPending);
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kUnseen);
    h.Send(touch);
    const size_t mark = h.Mark();
    h.Wait(kPoseWait);
    WorldDecision d;
    CHECK(h.State(enter) == RS::kRetry);
    CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true));
    EndAndCheck(h);
  }
}

// A touch whose enter is in retry, or that comes after its exit went through: refused `enter
// refused` at once.
void TouchRefusedAtOnce() {
  {
    H h;
    Bring(h, K::kEnter, RS::kRetry);
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(touch);
    WorldDecision d;
    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true));
    const auto g = h.GenericLast(touch.key);  // parity: `parent refused` (section 3.9)
    CHECK(g && g->reason == gb::Reason::kParentRefused && g->final);
    EndAndCheck(h);
  }
  {
    H h;
    Bring(h, K::kExit, RS::kApplied);  // its enter applied, then it
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(touch);
    WorldDecision d;
    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true) && h.Evaluations(touch.key) == 0);
    const auto g = h.GenericLast(touch.key);  // parity: kClosedByChild (section 3.9)
    CHECK(g && g->reason == gb::Reason::kClosedByChild && g->final);
    EndAndCheck(h);
  }
}

// ---------------------------------------------------------------------------------------------
// The per-peer evaluation order.

// One peer's keys are evaluated in the order they came: a later key whose pose is seen waits
// behind an earlier one that waits for its pose or is evaluating; another peer is not held up.
void EvaluationOrderPerPeer() {
  H h;
  h.game = false;
  const WorldRequest enter = Make(K::kEnter, h.gen, 1, 10);
  const WorldRequest tele = Make(K::kTelegram, h.gen, 1, 12);
  h.Send(enter);
  h.Send(tele);
  CHECK(h.Evaluations(enter.key) == 0 && h.Evaluations(tele.key) == 0);
  h.Pose(12);  // covers both
  h.Service();
  CHECK(h.Evaluating(enter) && !h.Evaluating(tele) && h.State(tele) == RS::kPending);
  h.Pose(1, kOther);
  const WorldRequest other = Make(K::kTelegram, h.gen, 1, 1, kOther);
  h.Send(other);
  CHECK(h.Evaluating(other));  // its own queue
  const size_t mark = h.Mark();
  CHECK(h.Submit(enter.key));
  CHECK(h.State(enter) == RS::kApplied && h.Evaluating(tele));
  CHECK(h.At(OT::kDecision, enter.key, mark) < h.At(OT::kEvaluate, tele.key, mark));
  CHECK(h.Submit(tele.key) && h.Submit(other.key));
  EndAndCheck(h);
}

// A key whose pose has come does not time out while it waits for its turn: when the key ahead of
// it is refused too_far at its own 500 ms, it is evaluated.
void WaitingForItsTurnDoesNotTimeOut() {
  H h;
  h.Pose(1);
  const WorldRequest enter = Make(K::kEnter, h.gen, 1, 10);  // pose 10 never comes
  const WorldRequest tele = Make(K::kTelegram, h.gen, 1, 1);  // its pose is seen
  h.Send(enter);
  h.Send(tele);
  h.Wait(kPoseWait - 1ms);
  CHECK(h.State(tele) == RS::kPending && h.Evaluations(tele.key) == 0);
  h.Wait(1ms);
  CHECK(h.State(enter) == RS::kRetry && h.State(tele) == RS::kApplied);
  EndAndCheck(h);
}

// ---------------------------------------------------------------------------------------------
// The orderings the council named.

// A resend in flight, then the exit: the resend is decided first; one final for the enter,
// whichever way it went.
void ResendInFlightThenExit() {
  {  // the pose caught up: the resend applies, then the exit is evaluated
    H h;
    const WorldRequest enter = Bring(h, K::kEnter, RS::kRetry);
    h.verdicts.erase(enter.key);
    h.Send(enter);
    h.Send(Make(K::kExit, h.gen, 1, kSeen));
    CHECK(h.State(enter) == RS::kApplied && h.Applies(enter.key) == 1);
    EndAndCheck(h);
  }
  {  // still too far: the resend is refused again, then the exit makes it final
    H h;
    const WorldRequest enter = Bring(h, K::kEnter, RS::kRetry);
    h.Send(enter);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
    h.Send(exit);
    const auto ds = h.Decisions(enter.key);
    REQUIRE_OR_RETURN_VOID(ds.size() == 5);  // pending, too_far, pending, too_far, S's final
    CHECK(Refused(ds[4], RR::kTooFar, true, true));
    WorldDecision d;
    CHECK(One(h, exit, 0, &d) && Refused(d, RR::kEnterRefused, true));
    EndAndCheck(h);
  }
}

// A resend in flight across a generation change: S's final went before the new LevelLoad; the
// resend arriving after it gets that final again, cached.
void ResendInFlightAcrossGeneration() {
  H h;
  const WorldRequest enter = Bring(h, K::kEnter, RS::kRetry);
  h.NewGeneration();
  const size_t mark = h.Mark();
  h.Send(enter);
  WorldDecision d;
  CHECK(One(h, enter, mark, &d) && Refused(d, RR::kTooFar, true, true, true));
  // Its exit, sent before the client saw the LevelLoad, is a first send of a left generation.
  const WorldRequest exit = Make(K::kExit, h.gen - 1, 1, kSeen);
  h.Send(exit);
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kStale, true));
  EndAndCheck(h);
}

// A first send in flight across a generation change: `stale` after the new LevelLoad, one final.
void FirstSendInFlightAcrossGeneration() {
  for (K k : kAllKinds) {
    H h;
    h.Pose(kSeen);
    const WorldRequest r = Make(k, h.gen, 1, kSeen);  // sent in the old generation...
    h.NewGeneration();                                // ...S changes before it lands
    h.Send(r);
    const auto ds = h.Decisions(r.key);
    REQUIRE_OR_RETURN_VOID(ds.size() == 1);
    CHECK(Refused(ds[0], RR::kStale, true));
    // It came after the LevelLoad: that is the tomb window, where invariant 3 lets it be answered.
    CHECK(h.sent.back().ordinal > h.level_loads.back().ordinal);
    EndAndCheck(h);
  }
}

// A first send in flight across End session: answered before closed (lane order), `session ended`
// once closing began, or from its state if it came before.
void FirstSendInFlightAcrossEndSession() {
  H h;
  h.Pose(kSeen);
  const WorldRequest before = Make(K::kEnter, h.gen, 1, kUnseen);
  h.Send(before);  // pending
  h.Close();
  const WorldRequest during = Make(K::kEnter, h.gen, 2, kSeen);
  h.Send(during);
  WorldDecision d;
  CHECK(One(h, during, 0, &d) && Refused(d, RR::kSessionEnded, true) && h.Applies(during.key) == 0);
  const size_t mark = h.Mark();
  h.Closed();
  CHECK(One(h, before, mark, &d) && Refused(d, RR::kSessionEnded, true, true));
  CHECK(h.log.back().type == OT::kPeerClosed);
  CheckInvariants(h);
}

// A client whose closed never comes: after kCloseWait S takes it as gone, sends the finals it
// owes for the keys it has seen, then its Bye (kPeerClosed with gone).
void ClosedNeverComes() {
  H h;
  h.Pose(kSeen);
  h.Pose(kSeen, kOther);
  const WorldRequest mine = Make(K::kTelegram, h.gen, 1, kUnseen);
  const WorldRequest theirs = Make(K::kEnter, h.gen, 1, kUnseen, kOther);
  h.Send(mine);
  h.Send(theirs);
  h.Close({kPeer, kOther});
  size_t mark = h.Mark();
  h.Closed(kPeer);
  WorldDecision d;
  CHECK(One(h, mine, mark, &d) && Refused(d, RR::kSessionEnded, true, true));
  CHECK(!h.table.AllClosed());
  mark = h.Mark();
  h.Wait(kCloseWait - 1ms);
  CHECK(h.OutputsSince(mark) == 0);
  h.Wait(1ms);
  REQUIRE_OR_RETURN_VOID(h.OutputsSince(mark) == 2);
  CHECK(h.log[mark].decision.key == theirs.key && Refused(h.log[mark].decision, RR::kSessionEnded, true, true));
  CHECK(h.log[mark + 1].type == OT::kPeerClosed && h.log[mark + 1].peer == kOther && h.log[mark + 1].gone);
  CHECK(h.table.AllClosed());
  // A late closed changes nothing.
  mark = h.Mark();
  h.Closed(kOther);
  CHECK(h.OutputsSince(mark) == 0);
  CheckInvariants(h);
}

// A pending telegram at End session: never applied, S's final `session ended` before the Bye.
void PendingTelegramAtEndSession() {
  H h;
  const WorldRequest t = Make(K::kTelegram, h.gen, 1, kUnseen);
  h.Send(t);
  h.Close();
  h.Pose(kUnseen);
  h.Wait(kPoseWait * 2);
  CHECK(h.Applies(t.key) == 0 && h.State(t) == RS::kPending);
  const size_t mark = h.Mark();
  h.Closed();
  REQUIRE_OR_RETURN_VOID(h.OutputsSince(mark) == 2);
  CHECK(Refused(h.log[mark].decision, RR::kSessionEnded, true, true));
  CHECK(h.log[mark + 1].type == OT::kPeerClosed);
  CheckInvariants(h);
}

// An enter refused `unresolved` whose ref resolves before its exit: the exit is still refused
// `enter refused`, nothing is applied, a duplicate of the enter gets the cached refusal.
void EnterUnresolvedRefResolvesBeforeExit() {
  H h;
  h.verdict = RR::kUnresolved;
  h.Pose(kSeen);
  const WorldRequest enter = Make(K::kEnter, h.gen, 1, kSeen);
  h.Send(enter);
  h.verdict = RR::kNone;  // the ref resolves now
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
  h.Send(exit);
  h.Send(enter);
  const auto ds = h.Decisions(enter.key);
  REQUIRE_OR_RETURN_VOID(ds.size() == 3);
  CHECK(Pending(ds[0]) && Refused(ds[1], RR::kUnresolved, true) &&
        Refused(ds[2], RR::kUnresolved, true, false, true));
  WorldDecision d;
  CHECK(One(h, exit, 0, &d) && Refused(d, RR::kEnterRefused, true));
  CHECK(h.Applies(enter.key) == 0 && h.Applies(exit.key) == 0);
  EndAndCheck(h);
}

// A telegram refused while its gate is shut, duplicated after the gate is released: the cached
// refusal, not an apply, no new evaluation.
void RefusedTelegramDuplicatedAfterRelease() {
  H h;
  h.verdict = RR::kNotReleased;
  h.Pose(kSeen);
  const WorldRequest t = Make(K::kTelegram, h.gen, 1, kSeen);
  h.Send(t);
  h.verdict = RR::kNone;
  const int checks = h.checks;
  h.Send(t);
  const auto ds = h.Decisions(t.key);
  REQUIRE_OR_RETURN_VOID(ds.size() == 3);
  CHECK(Refused(ds[1], RR::kNotReleased, true) && Refused(ds[2], RR::kNotReleased, true, false, true));
  CHECK(h.checks == checks && h.Applies(t.key) == 0);
  // A new request of the same telegram (the next request sequence) is evaluated, and applies.
  const WorldRequest next = Make(K::kTelegram, h.gen, 2, kSeen);
  h.Send(next);
  CHECK(h.Applies(next.key) == 1);
  EndAndCheck(h);
}

// ---------------------------------------------------------------------------------------------
// Section 5's cases that are the table's (the rest are in online_session_test).

// A request that arrives before its pose, then the pose: evaluated at the next Service.
void RequestBeforeItsPose() {
  H h;
  const WorldRequest r = Make(K::kEnter, h.gen, 1, 3);
  h.Pose(1);
  h.Send(r);
  CHECK(h.State(r) == RS::kPending);
  h.Pose(2);
  h.Service();
  CHECK(h.State(r) == RS::kPending);
  h.Pose(3);
  CHECK(h.State(r) == RS::kPending && h.Evaluations(r.key) == 0);  // serviced per frame, not on the pose
  h.Service();
  CHECK(h.State(r) == RS::kApplied);
  EndAndCheck(h);
}

// A pose older than the last from that peer and generation is dropped; another generation's too.
void PoseOrder() {
  H h;
  CHECK(h.Pose(5));
  CHECK(!h.Pose(5));
  CHECK(!h.Pose(4));
  CHECK(h.Pose(6));
  CHECK(h.Pose(1, kOther));  // per peer
  CHECK(!h.table.OnPose(kPeer, h.gen + 1, 100));
  CHECK(!h.table.OnPose(kPeer, h.gen - 1, 100));
  CHECK(!h.Pose(0));
  h.NewGeneration();
  CHECK(h.Pose(1));  // a new generation starts its poses again
  EndAndCheck(h);
}

// A retried enter: one apply, then "already applied"; a lost decision and its retry the same.
void RetriedEnterOneApply() {
  H h;
  h.Pose(kSeen);
  const WorldRequest r = Make(K::kEnter, h.gen, 1, kSeen);
  for (int i = 0; i < 4; ++i) h.Send(r);
  CHECK(h.Applies(r.key) == 1 && h.Evaluations(r.key) == 1);
  const auto ds = h.Decisions(r.key);
  REQUIRE_OR_RETURN_VOID(ds.size() == 5);
  CHECK(Pending(ds[0]) && Applied(ds[1]) && Applied(ds[2], true) && Applied(ds[4], true));
  EndAndCheck(h);
}

// An enter and its exit are two keys, each with one final decision.
void EnterAndExitTwoKeys() {
  H h;
  h.Pose(kSeen);
  const WorldRequest enter = Make(K::kEnter, h.gen, 7, kSeen);
  const WorldRequest exit = Make(K::kExit, h.gen, 7, kSeen);
  CHECK(enter.key != exit.key);
  h.Send(enter);
  h.Send(exit);
  CHECK(h.Applies(enter.key) == 1 && h.Applies(exit.key) == 1);
  // The next enter of the same trigger is a new sequence, a new key.
  const WorldRequest again = Make(K::kEnter, h.gen, 8, kSeen);
  h.Send(again);
  CHECK(h.Applies(again.key) == 1);
  EndAndCheck(h);
}

// A pending enter whose pose never arrives: retry with too_far, then a resend evaluated as new.
void PendingEnterPoseNeverArrives() {
  H h;
  const WorldRequest r = Make(K::kEnter, h.gen, 1, kUnseen);
  h.Send(r);
  h.Wait(kPoseWait);
  CHECK(h.State(r) == RS::kRetry);
  const int checks = h.checks;
  WorldRequest resend = r;
  resend.pose_sequence = kSeen;  // the client resends at the pose it has now
  h.Pose(kSeen);
  h.Send(resend);
  CHECK(h.checks == checks + 1 && h.State(r) == RS::kApplied);
  EndAndCheck(h);
}

// already sent: terminal within the late window (the game's check), evaluated normally after it.
void AlreadySentThenAfterTheWindow() {
  H h;
  h.Pose(kSeen);
  const WorldRequest within = Make(K::kTelegram, h.gen, 1, kSeen);
  h.verdicts[within.key] = RR::kAlreadySent;
  h.Send(within);
  CHECK(h.State(within) == RS::kRefused);
  const WorldRequest after = Make(K::kTelegram, h.gen, 2, kSeen);
  h.Send(after);
  CHECK(h.Applies(after.key) == 1);
  EndAndCheck(h);
}

// A duplicate scene skip: one apply. A skip of an ended scene: `scene ended`, then cached.
void SceneSkips() {
  H h;
  const WorldRequest skip = Make(K::kSkip, h.gen, 1, 0);  // a skip needs no pose
  h.Send(skip);
  h.Send(skip);
  CHECK(h.Applies(skip.key) == 1);
  const WorldRequest second = Make(K::kSkip, h.gen, 2, 0);  // a second press is a new key
  h.verdicts[second.key] = RR::kSceneEnded;
  h.Send(second);
  h.Send(second);
  const auto ds = h.Decisions(second.key);
  REQUIRE_OR_RETURN_VOID(ds.size() == 3);
  CHECK(Refused(ds[1], RR::kSceneEnded, true) && Refused(ds[2], RR::kSceneEnded, true, false, true));
  // The scene's name hash is the key's check field (amendment g): the same sequence, recipient and
  // skip sequence with another name is its own key, evaluated on its own.
  WorldRequest renamed = skip;
  renamed.key.scene.name_hash ^= 1;
  CHECK(renamed.key != skip.key);
  h.Send(renamed);
  CHECK(h.Applies(renamed.key) == 1 && h.Applies(skip.key) == 1);
  EndAndCheck(h);
}

// A new generation sends the finals owed in arrival order and turns the old keys into tombs,
// dropped one generation later.
void GenerationFinalsInOrder() {
  H h;
  const WorldRequest a = Make(K::kTelegram, h.gen, 1, kUnseen);
  const WorldRequest b = Make(K::kEnter, h.gen, 1, kUnseen);
  const WorldRequest c = Bring(h, K::kExit, RS::kHeld, 5);
  h.Send(a);
  h.Send(b);
  const size_t mark = h.Mark();
  h.NewGeneration();
  REQUIRE_OR_RETURN_VOID(h.OutputsSince(mark) == 3);
  CHECK(h.log[mark].decision.key == c.key);  // it came first
  CHECK(h.log[mark + 1].decision.key == a.key);
  CHECK(h.log[mark + 2].decision.key == b.key);
  CHECK(h.table.Size() == 3);
  h.NewGeneration();
  CHECK(h.table.Size() == 0);
  EndAndCheck(h);
}

// A peer that leaves: its keys go (an evaluating one's late result is dropped), and the same id
// with the same sequences starts fresh.
void ForgetPeer() {
  H h;
  h.Pose(kSeen);
  const WorldRequest r = Make(K::kEnter, h.gen, 1, kSeen);
  h.Send(r);
  const WorldRequest t = BringEvaluating(h, K::kTelegram);
  h.table.ForgetPeer(kPeer);
  CHECK(h.State(r) == RS::kNew && h.State(t) == RS::kNew && h.table.Size() == 0);
  const size_t mark = h.Mark();
  CHECK(!h.Submit(t.key) && h.OutputsSince(mark) == 0);
  CHECK(h.Pose(kSeen));  // its poses went too
  H g;  // a fresh table for the invariants (the forgotten peer's keys were never closed)
  g.Pose(kSeen);
  g.Send(r);
  CHECK(g.Applies(r.key) == 1);
  EndAndCheck(g);
}

// Stage 1B item 9, amendment l (docs/online-1B-amendment-l.md 3.9): the two terminal refusals it
// adds, and S's own waits. S's waits (the lock, a deferral, `not released` for an exit or a
// single-fire key, S's own retry on a single-fire trigger) keep the key evaluating: the game thread
// holds its kEvaluate and submits only the key's one decision, so the table sees one long
// evaluation (server/requests.cpp in the game). These cells check what the table does around it.

// already fired and not activated are terminal for every kind; an enter refused either way refuses
// its exit `enter refused` (never `already fired`), and its touch the same.
void AmendmentLTerminalRefusals() {
  for (RR reason : {RR::kAlreadyFired, RR::kNotActivated}) {
    for (K k : kAllKinds) CHECK(!IsRetryable(Make(k, 1, 1, 1).key, reason));
    H h;
    h.Pose(kSeen);
    const WorldRequest enter = Make(K::kEnter, 1, 1, kSeen);
    h.verdicts[enter.key] = reason;
    size_t mark = h.Mark();
    h.Send(enter);
    WorldDecision d;
    CHECK(Decided(h, enter, mark, &d) && Refused(d, reason, true));
    CHECK(h.State(enter) == RS::kRefused);
    const WorldRequest touch = Make(K::kTouch, 1, 1, kSeen);
    mark = h.Mark();
    h.Send(touch);
    CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true));
    const WorldRequest exit = Make(K::kExit, 1, 1, kSeen);
    mark = h.Mark();
    h.Send(exit);
    CHECK(One(h, exit, mark, &d) && Refused(d, RR::kEnterRefused, true));
    CHECK(h.Evaluations(exit.key) == 0 && h.Evaluations(touch.key) == 0);
    // A duplicate gets the cached refusal, no second evaluation.
    mark = h.Mark();
    h.Send(enter);
    CHECK(One(h, enter, mark, &d) && Refused(d, reason, true, false, true));
    CHECK(h.Evaluations(enter.key) == 1);
    EndAndCheck(h);
  }
  // Two peers against one single-fire trigger: the first applied, the second `already fired`.
  H h;
  h.Pose(kSeen);
  h.Pose(kSeen, kOther);
  const WorldRequest first = Make(K::kEnter, 1, 1, kSeen);
  const WorldRequest second = Make(K::kEnter, 1, 1, kSeen, kOther);
  h.verdicts[second.key] = RR::kAlreadyFired;
  h.Send(first);
  h.Send(second);
  CHECK(h.State(first) == RS::kApplied && h.State(second) == RS::kRefused);
  EndAndCheck(h);
}

// S holds an enter's evaluation (a lock wait, then the replay): the key stays pending with no pose
// timeout, the client's resends are answered `pending` from the cache, the same peer's later key
// waits its turn and is not refused too_far meanwhile, and the one decision comes when S submits.
void SOwnWaitHoldsTheEvaluation() {
  H h;
  h.game = false;
  h.Pose(kSeen);
  const WorldRequest enter = Make(K::kEnter, 1, 1, kSeen);
  h.Send(enter);
  CHECK(h.Evaluating(enter));
  const WorldRequest other = Make(K::kEnter, 1, 2, kSeen);  // another enter sequence of the same peer
  h.Send(other);
  CHECK(h.State(other) == RS::kPending && !h.Evaluating(other));
  for (int i = 0; i < 30; ++i) h.Wait(100ms);  // 3 s: longer than the pose and held-exit waits
  CHECK(h.Evaluating(enter) && h.State(other) == RS::kPending);
  size_t mark = h.Mark();
  h.Send(enter);  // the client's resend while S waits
  WorldDecision d;
  CHECK(One(h, enter, mark, &d) && Pending(d, true));
  // Its exit arrives meanwhile: held behind the pending enter.
  const WorldRequest exit = Make(K::kExit, 1, 1, kSeen);
  h.Send(exit);
  CHECK(h.State(exit) == RS::kHeld);
  for (int i = 0; i < 10; ++i) h.Wait(100ms);
  CHECK(h.State(exit) == RS::kHeld);  // not `exit before enter`: its enter exists
  mark = h.Mark();
  CHECK(h.Submit(enter.key));
  CHECK(Decided(h, enter, mark, &d) && Applied(d));
  // The exit goes to its own evaluation after the enter, then the other key.
  CHECK(h.Evaluating(exit) || h.Evaluating(other));
  while (!h.asked.empty()) h.Submit(h.asked.front());
  CHECK(h.State(exit) == RS::kApplied && h.State(other) == RS::kApplied);
  EndAndCheck(h);
}

// A held evaluation across a generation change, and across End session: S's final (generation
// ended, session ended), and S's late submit is dropped (one final decision). For an enter and for
// an exit (an exit is never lost to a wait, but the generation's end closes it).
void SOwnWaitAcrossGenerationAndEndSession() {
  for (K k : {K::kEnter, K::kExit}) {
    H h;
    h.game = false;
    Prereq(h, k, 1);
    while (!h.asked.empty()) h.Submit(h.asked.front());
    const WorldRequest r = Make(k, 1, 1, kSeen);
    h.Send(r);
    REQUIRE_OR_RETURN_VOID(h.Evaluating(r));
    for (int i = 0; i < 20; ++i) h.Wait(100ms);
    const size_t mark = h.Mark();
    h.NewGeneration();
    WorldDecision d;
    CHECK(One(h, r, mark, &d) && Refused(d, RR::kGenerationEnded, true, true));
    CHECK(!h.Submit(r.key));  // the replay that came too late is dropped
    CHECK(h.Applies(r.key) == 0);
    EndAndCheck(h);
  }
  for (K k : {K::kEnter, K::kExit}) {
    H h;
    h.game = false;
    Prereq(h, k, 1);
    while (!h.asked.empty()) h.Submit(h.asked.front());
    const WorldRequest r = Make(k, 1, 1, kSeen);
    h.Send(r);
    REQUIRE_OR_RETURN_VOID(h.Evaluating(r));
    h.Close();
    CHECK(!h.Submit(r.key));
    h.Closed();
    WorldDecision d;
    const auto ds = h.Decisions(r.key);
    REQUIRE_OR_RETURN_VOID(!ds.empty());
    d = ds.back();
    CHECK(Refused(d, RR::kSessionEnded, true, true));
    CheckInvariants(h);
  }
}

// S's 2 s retry bound on a single-fire key (amendment l 3.5): S submits the last retryable reason as
// its own final; the key is refused for good (no retry state, no resend expected), its exit
// `enter refused`.
void SOwnRetryBoundIsFinal() {
  H h;
  h.game = false;
  h.Pose(kSeen);
  const WorldRequest enter = Make(K::kEnter, 1, 1, kSeen);
  h.Send(enter);
  REQUIRE_OR_RETURN_VOID(h.Evaluating(enter));
  for (int i = 0; i < 20; ++i) h.Wait(100ms);
  size_t mark = h.Mark();
  h.asked.clear();
  CHECK(h.table.SubmitEvaluation(enter.key, EvaluationResult::ServerFinal(RR::kTooFar), h.now));
  h.Take();
  WorldDecision d;
  CHECK(One(h, enter, mark, &d) && Refused(d, RR::kTooFar, true, true));
  CHECK(h.State(enter) == RS::kRefused);
  const WorldRequest exit = Make(K::kExit, 1, 1, kSeen);
  mark = h.Mark();
  h.Send(exit);
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kEnterRefused, true));
  // A pass ignores the flag.
  const WorldRequest other = Make(K::kEnter, 1, 2, kSeen);
  h.Send(other);
  EvaluationResult pass = EvaluationResult::Applied(7);
  pass.server_final = true;
  mark = h.Mark();
  h.asked.clear();
  CHECK(h.table.SubmitEvaluation(other.key, pass, h.now));
  h.Take();
  CHECK(One(h, other, mark, &d) && Applied(d));
  EndAndCheck(h);
}
// 1B's lines check IsRetryable, 1B's own function. The generic table has none: a result says
// whether its refusal is retryable and kNeverResent and kClosesParent make every refusal of a
// telegram, a skip or an exit terminal (section 3.9). So the parity part runs each reason the game
// thread can give through an evaluation of each key type on both tables: retry on both where 1B's
// IsRetryable says so, refused on both elsewhere. (An exit refused `exit before enter` by the game
// thread is left out: that reason is the table's own, from the held exit's wait, never a result;
// the game never submits it.)
void Retryable() {
  const RequestKey enter = Make(K::kEnter, 1, 1, 1).key;
  const RequestKey exit = Make(K::kExit, 1, 1, 1).key;
  const RequestKey touch = Make(K::kTouch, 1, 1, 1).key;
  for (RR r : {RR::kTooFar, RR::kRate, RR::kNotReleased, RR::kNotInPlay}) {
    CHECK(IsRetryable(enter, r) && IsRetryable(touch, r) && !IsRetryable(exit, r));
    for (K k : kNeverResent) CHECK(!IsRetryable(Make(k, 1, 1, 1).key, r));
  }
  CHECK(IsRetryable(exit, RR::kExitBeforeEnter) && !IsRetryable(enter, RR::kExitBeforeEnter));
  for (RR r : {RR::kStale, RR::kAlreadySent, RR::kUnresolved, RR::kUnsupported, RR::kEnterRefused,
               RR::kSceneEnded, RR::kGenerationEnded, RR::kSessionEnded, RR::kOther}) {
    for (K k : kAllKinds) CHECK(!IsRetryable(Make(k, 1, 1, 1).key, r));
  }
  CHECK(std::string_view(RequestStateName(RS::kTomb)) == "tomb");
  for (RR r : {RR::kTooFar, RR::kRate, RR::kNotReleased, RR::kNotInPlay, RR::kExitBeforeEnter,
               RR::kStale, RR::kAlreadySent, RR::kUnresolved, RR::kUnsupported, RR::kEnterRefused,
               RR::kSceneEnded, RR::kGenerationEnded, RR::kSessionEnded, RR::kOther,
               RR::kAlreadyFired, RR::kNotActivated}) {
    for (K k : kAllKinds) {
      if (k == K::kExit && r == RR::kExitBeforeEnter) continue;
      H h;
      Prereq(h, k, 1);
      const WorldRequest req = Make(k, h.gen, 1, kSeen);
      h.verdicts[req.key] = r;
      h.Send(req);
      CHECK(h.Evaluations(req.key) == 1);
      CHECK(h.State(req) == (IsRetryable(req.key, r) ? RS::kRetry : RS::kRefused));
      EndAndCheck(h);
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Parity-only cells.

// RP0 (Sol F4): a trigger enter, a telegram and a skip whose 1B sequences are all 1, in one
// generation: three generic keys, three decisions, and a resend of each finds its own cache entry.
// The limits the cells lean on are 1B's numbers.
void KeyMapCollisionFree() {
  CHECK(gb::Limits{}.precondition_wait == kPoseWait);
  CHECK(gb::Limits{}.max_keys_per_peer == kMaxKeysPerPeer);
  CHECK(parity::kParityTable[1].parent_wait_ms == uint32_t(kHeldExitWait.count()));
  CHECK(parity::kParityTable[2].parent_wait_ms == uint32_t(kHeldExitWait.count()));
  H h;
  h.Pose(kSeen);
  const WorldRequest enter = Make(K::kEnter, h.gen, 1, kSeen);
  const WorldRequest tele = Make(K::kTelegram, h.gen, 1, kSeen);
  const WorldRequest skip = Make(K::kSkip, h.gen, 1, 0);
  h.Send(enter);
  h.Send(tele);
  h.Send(skip);
  const auto ge = h.GenericKey(enter.key), gt = h.GenericKey(tele.key), gs = h.GenericKey(skip.key);
  REQUIRE_OR_RETURN_VOID(ge && gt && gs);
  CHECK(ge->sequence == 1 && gt->sequence == 2 && gs->sequence == 3);
  for (const WorldRequest* r : {&enter, &tele, &skip}) {
    CHECK(h.State(*r) == RS::kApplied && h.Applies(r->key) == 1);
    const size_t mark = h.Mark();
    h.Send(*r);
    WorldDecision d;
    CHECK(One(h, *r, mark, &d) && Applied(d, true));
    const auto g = h.GenericLast(r->key);
    CHECK(g && g->cached && g->outcome == gb::Outcome::kApplied && g->key == *h.GenericKey(r->key));
  }
  CHECK(h.Map().Size() == 3);
  // The map is bounded with the tombs: a generation's entries go one change after its keys did.
  h.NewGeneration();
  CHECK(h.Map().Size() == 3);
  h.NewGeneration();
  CHECK(h.Map().Size() == 0);
  EndAndCheck(h);
}

// RP-a (Opus r2 N2), on both tables: an exit whose pose never comes and whose enter never comes is
// refused `parent missing` (retryable, 1B's exit before enter) at parent_wait_ms, never
// `precondition missing`; only S's final at the generation change closes it.
void ExitNoPoseNoEnter() {
  H h;
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kUnseen);
  h.Send(exit);
  CHECK(h.State(exit) == RS::kHeld);
  h.Wait(kHeldExitWait - 1ms);
  CHECK(h.State(exit) == RS::kHeld);
  h.Wait(1ms);
  CHECK(h.State(exit) == RS::kRetry);
  WorldDecision d;
  CHECK(Decided(h, exit, 0, &d) && Refused(d, RR::kExitBeforeEnter, false));
  for (const gb::Decision& g : h.GenericDecisions(exit.key)) {
    CHECK(g.reason != gb::Reason::kPreconditionMissing);
  }
  const auto last = h.GenericLast(exit.key);
  CHECK(last && last->reason == gb::Reason::kParentMissing && !last->final);
  h.Wait(2000ms);  // no pose wait runs for it
  CHECK(h.State(exit) == RS::kRetry);
  const size_t mark = h.Mark();
  h.NewGeneration();
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kExitBeforeEnter, true, true));
  const auto fin = h.GenericLast(exit.key);
  CHECK(fin && fin->reason == gb::Reason::kParentMissing && fin->final && fin->server_final);
  EndAndCheck(h);
}

// RP-b (Opus r2 N2), on both tables: an exit with no pose behind an enter that stays evaluating for
// 2 s is still held at 600 ms and at 2 s; when the enter applies it is admitted, its pose clock
// starts then, and it ends too far (`precondition missing`, terminal: an exit is never resent)
// 500 ms later.
void ExitNoPoseBehindSlowEnter() {
  H h;
  h.game = false;
  h.Pose(kSeen);
  const WorldRequest enter = Make(K::kEnter, h.gen, 1, kSeen);
  h.Send(enter);
  CHECK(h.Evaluating(enter));
  const WorldRequest exit = Make(K::kExit, h.gen, 1, kUnseen);
  h.Send(exit);
  CHECK(h.State(exit) == RS::kHeld);
  h.Wait(600ms);
  CHECK(h.State(exit) == RS::kHeld);
  h.Wait(1400ms);
  CHECK(h.State(exit) == RS::kHeld);
  CHECK(h.Submit(enter.key));  // the enter applies at 2 s
  CHECK(h.State(exit) == RS::kPending && !h.Evaluating(exit));
  h.Wait(kPoseWait - 1ms);
  CHECK(h.State(exit) == RS::kPending);
  const size_t mark = h.Mark();
  h.Wait(1ms);
  WorldDecision d;
  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kTooFar, true));
  const auto fin = h.GenericLast(exit.key);
  CHECK(fin && fin->reason == gb::Reason::kPreconditionMissing && fin->final && !fin->server_final);
  CHECK(h.Evaluations(exit.key) == 0);
  EndAndCheck(h);
}

// RP8b (Opus F11), on both tables: pose 0 on anything but a skip is refused `unsupported` before
// the parent logic: a pose-0 exit behind a pending enter is refused at once, not held; an enter
// refused so refuses its held touch (its children follow).
void PoseZeroExitBehindPendingEnter() {
  {
    H h;
    const WorldRequest enter = Make(K::kEnter, h.gen, 1, kUnseen);  // pending: its pose never comes
    h.Send(enter);
    CHECK(h.State(enter) == RS::kPending);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, 0);
    h.Send(exit);
    WorldDecision d;
    CHECK(One(h, exit, 0, &d) && Refused(d, RR::kUnsupported, true));
    CHECK(h.State(exit) == RS::kRefused && h.Evaluations(exit.key) == 0);
    const auto g = h.GenericLast(exit.key);
    CHECK(g && g->reason == gb::Reason::kUnsupported);
    EndAndCheck(h);
  }
  {
    H h;
    h.Pose(kSeen);
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(touch);
    CHECK(h.State(touch) == RS::kHeld);
    const WorldRequest enter = Make(K::kEnter, h.gen, 1, 0);
    h.Send(enter);
    WorldDecision d;
    CHECK(Decided(h, enter, 0, &d) && Refused(d, RR::kUnsupported, true));
    CHECK(Decided(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true));
    const auto g = h.GenericLast(touch.key);
    CHECK(g && g->reason == gb::Reason::kParentRefused);
    EndAndCheck(h);
  }
}

// RP-c: a design-decided difference (section 3.9's table, kClosedByChild: "a child with
// kClosesParent that was let through closes the parent"). 1B refuses a touch whose exit exists in any state but
// held (requests.cpp DispatchTouch and Retry: `exit->state != kHeld`), so also after an exit that
// was never let through: one refused `exit before enter` before its enter came, or refused
// `unsupported` (pose 0). The generic table admits that touch. Both tables are run; the difference
// is noted, not failed.
void TouchAfterAnExitNeverLetThrough() {
  {  // the exit came first and timed out (retry); then the enter, applied; then the touch
    H h;
    h.Pose(kSeen);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
    h.Send(exit);
    h.Wait(kHeldExitWait);
    CHECK(h.State(exit) == RS::kRetry);
    const WorldRequest enter = Make(K::kEnter, h.gen, 1, kSeen);
    h.Send(enter);
    CHECK(h.State(enter) == RS::kApplied);
    h.ExpectDifference("closed-by-child-let-through");
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(touch);
    h.EndDifference();
    WorldDecision d;
    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true));
    CHECK(h.Evaluations(touch.key) == 0);
    // The generic table admitted it and asked for its evaluation (the game here answers 1B's asks).
    const auto g = h.GenericLast(touch.key);
    CHECK(g && g->outcome == gb::Outcome::kPending && !g->final);
    CHECK(h.GenericKey(touch.key) && h.Generic().Evaluating(*h.GenericKey(touch.key)));
  }
  {  // the exit refused `unsupported` (pose 0) after its enter applied; then the touch
    H h;
    h.Pose(kSeen);
    const WorldRequest enter = Make(K::kEnter, h.gen, 1, kSeen);
    h.Send(enter);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, 0);
    h.Send(exit);
    CHECK(h.State(exit) == RS::kRefused);
    h.ExpectDifference("closed-by-child-let-through");
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(touch);
    h.EndDifference();
    WorldDecision d;
    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true));
    // The generic table admitted it and asked for its evaluation (the game here answers 1B's asks).
    const auto g = h.GenericLast(touch.key);
    CHECK(g && g->outcome == gb::Outcome::kPending && !g->final);
    CHECK(h.GenericKey(touch.key) && h.Generic().Evaluating(*h.GenericKey(touch.key)));
  }
}

// RP-d: the pose precondition through Server::OnPrecondition (section 3.10: "requests of that peer
// held on precondition <= sequence go on"): the generic table moves the peer's queue at once, 1B's
// table at its next Service (RequestBeforeItsPose, RP54: "serviced per frame, not on the pose").
// Same decisions; the generic one up to a frame earlier. The other cells hand the pose over where
// 1B reads it (parity_twin.h's header), so this is the one cell that shows it.
void PreconditionMovesAtOnce() {
  H h;
  h.Pose(1);
  const WorldRequest r = Make(K::kEnter, h.gen, 1, 3);
  h.Send(r);
  CHECK(h.State(r) == RS::kPending);
  h.ExpectDifference("precondition-at-once");
  h.PoseAtOnce(3);  // Server::OnPrecondition's timing
  const auto g = h.GenericLast(r.key);
  CHECK(g && g->outcome == gb::Outcome::kPending);  // asked for: the evaluation is out
  CHECK(h.GenericKey(r.key) && h.Generic().Evaluating(*h.GenericKey(r.key)));
  CHECK(h.State(r) == RS::kPending && h.Evaluations(r.key) == 0);  // 1B: at its next Service
  h.Service();
  h.EndDifference();
  CHECK(h.State(r) == RS::kApplied);
  const auto fin = h.GenericLast(r.key);
  CHECK(fin && fin->outcome == gb::Outcome::kApplied);
  EndAndCheck(h);
}

// RP-e: End session (section 1 point 3: the session keeps End session's drain; section 3.10:
// BeginClose has no peer list and no SessionClosed). The generic table gives every final at
// BeginClose; 1B gives a peer's at its SessionClosed. The same finals, before the Bye on both.
void CloseFinalsAtBeginClose() {
  H h;
  h.Pose(kSeen);
  const WorldRequest pending = Make(K::kEnter, h.gen, 1, kUnseen);
  const WorldRequest retry = Bring(h, K::kExit, RS::kRetry, 2);
  h.Send(pending);
  h.Close();
  CHECK(h.Decisions(pending.key, 0).size() == 1);  // 1B: only its `pending` so far
  const auto gp = h.GenericLast(pending.key);
  CHECK(gp && gp->reason == gb::Reason::kSessionEnded && gp->final && gp->server_final);
  const auto gr = h.GenericLast(retry.key);
  CHECK(gr && gr->reason == gb::Reason::kParentMissing && gr->final && gr->server_final);
  const size_t mark = h.Mark();
  h.Closed();
  WorldDecision d;
  CHECK(One(h, pending, mark, &d) && Refused(d, RR::kSessionEnded, true, true));
  CHECK(One(h, retry, mark, &d) && Refused(d, RR::kExitBeforeEnter, true, true));
  CheckInvariants(h);
}

// RP-f: an ordering 1B's cells leave out (the held branch of FinalTouch / FinalChildren): an exit
// and then a touch held behind a pending enter, and the enter refused (unresolved) or turned retry
// (too far). The exit, dispatched first, refuses the touch before itself; with the enter in retry,
// S's final for the enter comes first.
void HeldExitAndTouchEnterRefused() {
  for (RR verdict : {RR::kUnresolved, RR::kTooFar}) {
    H h;
    const WorldRequest enter = BringEvaluating(h, K::kEnter);
    const WorldRequest exit = Make(K::kExit, h.gen, 1, kSeen);
    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);
    h.Send(exit);
    h.Send(touch);
    CHECK(h.State(exit) == RS::kHeld && h.State(touch) == RS::kHeld);
    const size_t mark = h.Mark();
    CHECK(h.Submit(enter.key, verdict));
    const size_t t = h.At(OT::kDecision, touch.key, mark), x = h.At(OT::kDecision, exit.key, mark);
    CHECK(t < x && x < h.log.size());
    WorldDecision d;
    CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true));
    CHECK(One(h, exit, mark, &d) && Refused(d, RR::kEnterRefused, true));
    if (verdict == RR::kTooFar) {
      const auto ds = h.Decisions(enter.key, mark);
      REQUIRE_OR_RETURN_VOID(ds.size() == 2);
      CHECK(Refused(ds[0], RR::kTooFar, false) && Refused(ds[1], RR::kTooFar, true, true));
      CHECK(h.At(OT::kDecision, enter.key, mark) < t);
    }
    EndAndCheck(h);
  }
}

// --- Running a cell: its design-decided differences must be exactly the listed ones, each tag with
// the operation it showed at, in order (Sol code review r2, R7).

using DiffList = std::vector<std::pair<std::string, std::string>>;  // (tag, operation)

const std::map<std::string, DiffList>& ExpectedDiffs() {
  static const std::map<std::string, DiffList> diffs = {
      {"HeldEndSession",
       {{"close-resend", "Send enter#1(p2 g1) pose 9"}}},
      {"PendingEndSession",
       {{"close-resend", "Send telegram#1(p2 g1) pose 1"}}},
      {"PreconditionMovesAtOnce",
       {{"precondition-at-once", "PoseAtOnce 3"},
        {"precondition-at-once", "PoseAtOnce 3"},
        {"precondition-at-once", "Service"}}},
      {"RetryEndSession",
       {{"close-resend", "Send enter#1(p2 g1) pose 1"}}},
      {"TouchAfterAnExitNeverLetThrough",
       {{"closed-by-child-let-through", "Send touch#1(p2 g1) pose 1"},
        {"closed-by-child-let-through", "Send touch#1(p2 g1) pose 1"},
        {"closed-by-child-let-through", "Send touch#1(p2 g1) pose 1"},
        {"closed-by-child-let-through", "Send touch#1(p2 g1) pose 1"},
        {"closed-by-child-let-through", "Send touch#1(p2 g1) pose 1"},
        {"closed-by-child-let-through", "Send touch#1(p2 g1) pose 1"}}},
  };
  return diffs;
}

void RunCell(const char* name, void (*fn)()) {
  parity::CurrentCell() = name;
  parity::CellDiffs().clear();
  fn();
  DiffList seen;
  for (const parity::CellDiff& d : parity::CellDiffs()) {
    seen.emplace_back(d.tag, d.op);
    std::printf("  parity %s: design-decided difference `%s` at %s: %s\n", name, d.tag.c_str(),
                d.op.c_str(), d.detail.c_str());
  }
  auto it = ExpectedDiffs().find(name);
  const DiffList want = it == ExpectedDiffs().end() ? DiffList{} : it->second;
  if (seen != want) {
    std::fprintf(stderr, "FAIL parity %s: its design-decided differences changed\n", name);
    ++g_failures;
  }
}

}  // namespace

#define PARITY_CELL(id, fn) \
  BACKEND_TEST(id, Parity##fn) { RunCell(#fn, &fn); }

PARITY_CELL("RP0", KeyMapCollisionFree)
PARITY_CELL("RP-a", ExitNoPoseNoEnter)
PARITY_CELL("RP-b", ExitNoPoseBehindSlowEnter)
PARITY_CELL("RP1", NewRequestPassWithPose)
PARITY_CELL("RP2", NewRequestPassWithoutPose)
PARITY_CELL("RP3", NewRequestRetryableRefusal)
PARITY_CELL("RP4", NewRequestTerminalRefusal)
PARITY_CELL("RP5", NewRequestOfALeftGeneration)
PARITY_CELL("RP6", NewRequestOlderThanTheTombs)
PARITY_CELL("RP7", NewRequestAfterClosing)
PARITY_CELL("RP8", NewRequestPoseZero)
PARITY_CELL("RP8b", PoseZeroExitBehindPendingEnter)
PARITY_CELL("RP9", NewRequestPastTheKeyCap)
PARITY_CELL("RP10", NewNothingElse)
PARITY_CELL("RP11", NewExitWithNoEnter)
PARITY_CELL("RP12", NewTouchWithNoEnter)
PARITY_CELL("RP13", PendingRequestAgain)
PARITY_CELL("RP14", PendingPoseArrives)
PARITY_CELL("RP15", PendingPoseWaitEnds)
PARITY_CELL("RP16", PendingEnterThenItsExit)
PARITY_CELL("RP17", PendingGenerationChange)
PARITY_CELL("RP18", PendingEndSession)
PARITY_CELL("RP19", RetryResendChecksAgain)
PARITY_CELL("RP20", RetryNothingOnPoseOrWait)
PARITY_CELL("RP21", RetryEnterThenItsExit)
PARITY_CELL("RP22", RetryTouchThenTheExit)
PARITY_CELL("RP23", RetryGenerationChange)
PARITY_CELL("RP24", RetryEndSession)
PARITY_CELL("RP25", AppliedRequestAgain)
PARITY_CELL("RP26", AppliedNothingOnPoseOrWait)
PARITY_CELL("RP27", AppliedEnterThenItsExit)
PARITY_CELL("RP28", AppliedGenerationChange)
PARITY_CELL("RP29", AppliedEndSession)
PARITY_CELL("RP30", RefusedRequestAgain)
PARITY_CELL("RP31", RefusedNothingOnPoseOrWait)
PARITY_CELL("RP32", RefusedEnterThenItsExit)
PARITY_CELL("RP33", RefusedGenerationChangeAndEndSession)
PARITY_CELL("RP34", HeldRequestAgain)
PARITY_CELL("RP35", HeldNothingOnPose)
PARITY_CELL("RP36", HeldWaitEnds)
PARITY_CELL("RP37", HeldGenerationChange)
PARITY_CELL("RP38", HeldEndSession)
PARITY_CELL("RP39", TombRequestAgain)
PARITY_CELL("RP40", TombNothingOnPoseOrWait)
PARITY_CELL("RP41", TombDroppedNextGeneration)
PARITY_CELL("RP42", TouchFollowsItsEnter)
PARITY_CELL("RP43", TouchRefusedAtOnce)
PARITY_CELL("RP44", EvaluationOrderPerPeer)
PARITY_CELL("RP45", WaitingForItsTurnDoesNotTimeOut)
PARITY_CELL("RP46", ResendInFlightThenExit)
PARITY_CELL("RP47", ResendInFlightAcrossGeneration)
PARITY_CELL("RP48", FirstSendInFlightAcrossGeneration)
PARITY_CELL("RP49", FirstSendInFlightAcrossEndSession)
PARITY_CELL("RP50", ClosedNeverComes)
PARITY_CELL("RP51", PendingTelegramAtEndSession)
PARITY_CELL("RP52", EnterUnresolvedRefResolvesBeforeExit)
PARITY_CELL("RP53", RefusedTelegramDuplicatedAfterRelease)
PARITY_CELL("RP54", RequestBeforeItsPose)
PARITY_CELL("RP55", PoseOrder)
PARITY_CELL("RP56", RetriedEnterOneApply)
PARITY_CELL("RP57", EnterAndExitTwoKeys)
PARITY_CELL("RP58", PendingEnterPoseNeverArrives)
PARITY_CELL("RP59", AlreadySentThenAfterTheWindow)
PARITY_CELL("RP60", SceneSkips)
PARITY_CELL("RP61", GenerationFinalsInOrder)
PARITY_CELL("RP62", ForgetPeer)
PARITY_CELL("RP63", Retryable)
PARITY_CELL("RP64", AmendmentLTerminalRefusals)
PARITY_CELL("RP65", SOwnWaitHoldsTheEvaluation)
PARITY_CELL("RP66", SOwnWaitAcrossGenerationAndEndSession)
PARITY_CELL("RP67", SOwnRetryBoundIsFinal)
PARITY_CELL("RP-c", TouchAfterAnExitNeverLetThrough)
PARITY_CELL("RP-d", PreconditionMovesAtOnce)
PARITY_CELL("RP-e", CloseFinalsAtBeginClose)
PARITY_CELL("RP-f", HeldExitAndTouchEnterRefused)
