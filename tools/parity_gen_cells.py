"""Run from the portal root: python -I tools/parity_gen_cells.py <1B's
tests/online_requests_test.cpp> tests/parity_requests_test.cpp. Writes
tests/parity_requests_test.cpp: 1B's 67 request-table cells, verbatim from the newest 1B
revision's tests/online_requests_test.cpp, on the twin harness (tests/parity_twin.h), with the
generic checks and the parity-only cells added. Standard library only; run with python -I."""

import re
import sys

SRC = sys.argv[1]   # 1B's online_requests_test.cpp (newest revision)
OUT = sys.argv[2]   # tests/parity_requests_test.cpp

text = open(SRC, encoding="utf-8").read().replace("\r\n", "\n")
lines = text.split("\n")


def find(prefix, start=0):
    for i in range(start, len(lines)):
        if lines[i].startswith(prefix):
            return i
    raise SystemExit("not found: " + prefix)


# The parts of 1B's file: aliases and Make (before struct H), the helpers after H, the cells.
ns = find("namespace {")
h_start = find("// A table, fake time, a scripted game thread")
h_end = find("};", find("struct H {")) + 1
retry_start = find("void Retryable() {")
retry_end = find("}", retry_start + 1) + 1
main_start = find("}  // namespace", retry_end)

head_part = "\n".join(lines[ns + 1:h_start])
helpers = "\n".join(lines[h_end:retry_start])
retry_1b = "\n".join(lines[retry_start + 1:retry_end - 1])

# The order of 1B's main(): the cells, numbered RP1 to RPn in that order.
main_body = "\n".join(lines[main_start:])
cells = re.findall(r"^  ([A-Z][A-Za-z0-9]+)\(\);", main_body, re.M)
assert len(cells) == 67, len(cells)


def patch(body, old, new, count=1):
    n = body.count(old)
    if n != count:
        raise SystemExit("patch anchor found %d times (want %d): %r" % (n, count, old))
    return body.replace(old, new)


# CheckInvariants also checks the generic log.
helpers = patch(
    helpers,
    "  if (all_final) {\n    for (const H::Sent& s : h.sent) CHECK(finals.count(s.key) == 1);\n  }\n}",
    "  if (all_final) {\n    for (const H::Sent& s : h.sent) CHECK(finals.count(s.key) == 1);\n  }\n"
    "  h.CheckGeneric(all_final);  // the same invariants over the generic table's log (parity)\n}")

# The generic checks a cell adds (design section 10's named cases), each after one of 1B's lines.
helpers = patch(
    helpers,
    "  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kExitBeforeEnter, false));\n  mark = h.Mark();\n"
    "  h.NewGeneration();\n",
    "  CHECK(One(h, exit, mark, &d) && Refused(d, RR::kExitBeforeEnter, false));\n"
    "  {  // parity: the generic words (section 3.9): `parent missing`, retryable\n"
    "    const auto g = h.GenericLast(exit.key);\n"
    "    CHECK(g && g->reason == gb::Reason::kParentMissing && !g->final);\n"
    "  }\n"
    "  mark = h.Mark();\n  h.NewGeneration();\n")
helpers = patch(
    helpers,
    "  CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true));\n"
    "  CHECK(h.State(touch) == RS::kRefused && h.Evaluations(touch.key) == 0);\n",
    "  CHECK(One(h, touch, mark, &d) && Refused(d, RR::kEnterRefused, true));\n"
    "  CHECK(h.State(touch) == RS::kRefused && h.Evaluations(touch.key) == 0);\n"
    "  {  // parity: the same rule with parent_missing_retryable unset: `parent missing`, terminal\n"
    "    const auto g = h.GenericLast(touch.key);\n"
    "    CHECK(g && g->reason == gb::Reason::kParentMissing && g->final && !g->server_final);\n"
    "  }\n")
helpers = patch(
    helpers,
    "    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true) && h.Evaluations(touch.key) == 0);\n",
    "    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true) && h.Evaluations(touch.key) == 0);\n"
    "    const auto g = h.GenericLast(touch.key);  // parity: kClosedByChild (section 3.9)\n"
    "    CHECK(g && g->reason == gb::Reason::kClosedByChild && g->final);\n")
helpers = patch(
    helpers,
    "    Bring(h, K::kEnter, RS::kRetry);\n    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);\n"
    "    h.Send(touch);\n    WorldDecision d;\n"
    "    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true));\n",
    "    Bring(h, K::kEnter, RS::kRetry);\n    const WorldRequest touch = Make(K::kTouch, h.gen, 1, kSeen);\n"
    "    h.Send(touch);\n    WorldDecision d;\n"
    "    CHECK(One(h, touch, 0, &d) && Refused(d, RR::kEnterRefused, true));\n"
    "    const auto g = h.GenericLast(touch.key);  // parity: `parent refused` (section 3.9)\n"
    "    CHECK(g && g->reason == gb::Reason::kParentRefused && g->final);\n")
helpers = patch(
    helpers,
    "    CHECK(One(h, r, mark, &d) && Refused(d, RR::kUnsupported, true));\n"
    "    CHECK(h.State(r) == RS::kRefused && h.Evaluations(r.key) == 0);\n",
    "    CHECK(One(h, r, mark, &d) && Refused(d, RR::kUnsupported, true));\n"
    "    CHECK(h.State(r) == RS::kRefused && h.Evaluations(r.key) == 0);\n"
    "    const auto g = h.GenericLast(r.key);  // parity: requires_precondition (Opus F11)\n"
    "    CHECK(g && g->reason == gb::Reason::kUnsupported && g->final);\n")
helpers = patch(
    helpers,
    "      CHECK(h.Pose(kUnseen));\n      h.Service();\n      CHECK(h.Evaluations(r.key) == 1);\n",
    "      CHECK(h.Pose(kUnseen));  // parity: Server::OnPrecondition on the generic table\n"
    "      h.Service();\n      CHECK(h.Evaluations(r.key) == 1);\n")

retry_port = """// 1B's lines check IsRetryable, 1B's own function. The generic table has none: a result says
// whether its refusal is retryable and kNeverResent and kClosesParent make every refusal of a
// telegram, a skip or an exit terminal (section 3.9). So the parity part runs each reason the game
// thread can give through an evaluation of each key type on both tables: retry on both where 1B's
// IsRetryable says so, refused on both elsewhere. (An exit refused `exit before enter` by the game
// thread is left out: that reason is the table's own, from the held exit's wait, never a result;
// the game never submits it.)
void Retryable() {
%s
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
}""" % retry_1b

# The design-decided differences each cell shows (parity_twin.h's header): any other difference
# fails, and a cell that stops showing its listed one fails too (the list is kept exact).
# Each cell's design-decided differences, in order: (cell, tag, the operation it shows at).
expected = [
    ('HeldEndSession', 'close-resend', 'Send enter#1(p2 g1) pose 9'),
    ('PendingEndSession', 'close-resend', 'Send telegram#1(p2 g1) pose 1'),
    ('RetryEndSession', 'close-resend', 'Send enter#1(p2 g1) pose 1'),
    ('TouchAfterAnExitNeverLetThrough', 'closed-by-child-let-through', 'Send touch#1(p2 g1) pose 1'),
    ('TouchAfterAnExitNeverLetThrough', 'closed-by-child-let-through', 'Send touch#1(p2 g1) pose 1'),
    ('TouchAfterAnExitNeverLetThrough', 'closed-by-child-let-through', 'Send touch#1(p2 g1) pose 1'),
    ('TouchAfterAnExitNeverLetThrough', 'closed-by-child-let-through', 'Send touch#1(p2 g1) pose 1'),
    ('TouchAfterAnExitNeverLetThrough', 'closed-by-child-let-through', 'Send touch#1(p2 g1) pose 1'),
    ('TouchAfterAnExitNeverLetThrough', 'closed-by-child-let-through', 'Send touch#1(p2 g1) pose 1'),
    ('PreconditionMovesAtOnce', 'precondition-at-once', 'PoseAtOnce 3'),
    ('PreconditionMovesAtOnce', 'precondition-at-once', 'PoseAtOnce 3'),
    ('PreconditionMovesAtOnce', 'precondition-at-once', 'Service'),
]

out = []
out.append("""// The parity suite (stage 2 item 4; design sections 3.9, 9 item 4 and 10; Sol r1 F2): every cell of
// 1B's request table, on 1B's own table and on the generic one at once (tests/parity_twin.h).
//
// PROVISIONAL (Sol F10). The cells are 1B's own bodies, copied by tools/parity_gen_cells.py from
// tests/online_requests_test.cpp at portal 26ef72d (work/1B-route, the newest 1B revision;
// requests.h, requests.cpp and the test file are unchanged there since 9054072): 67 cell functions,
// numbered RP1 to RP67 in the order of 1B's main(). Before item 4 merges the file is written again
// from 1B's merged commit, the cells recounted, in a commit of its own. The script adds, each
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
#define REQUIRE_OR_RETURN_VOID(cond)                                       \\
  do {                                                                     \\
    if (!(cond)) {                                                         \\
      std::fprintf(stderr, "FAIL %s:%d: %s\\n", __FILE__, __LINE__, #cond); \\
      ++g_failures;                                                        \\
      return;                                                              \\
    }                                                                      \\
  } while (0)
#endif

namespace {

using namespace skylanders::online;
using namespace std::chrono_literals;
namespace gb = skylanders::backend;
namespace parity = skylanders::parity;
using H = parity::Twin;
""")
out.append("// --- 1B's harness pieces (online_requests_test.cpp, verbatim but for struct H, which is the twin).\n")
out.append(head_part)
out.append(helpers)
out.append(retry_port)
out.append("""
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
""")
by_cell = {}
for cell, tag, op in expected:
    by_cell.setdefault(cell, []).append((tag, op))
for cell in sorted(by_cell):
    out.append('      {"%s",\n       {%s}},\n' %
               (cell, ",\n        ".join('{"%s", "%s"}' % d for d in by_cell[cell])))
out.append("""  };
  return diffs;
}

void RunCell(const char* name, void (*fn)()) {
  parity::CurrentCell() = name;
  parity::CellDiffs().clear();
  fn();
  DiffList seen;
  for (const parity::CellDiff& d : parity::CellDiffs()) {
    seen.emplace_back(d.tag, d.op);
    std::printf("  parity %s: design-decided difference `%s` at %s: %s\\n", name, d.tag.c_str(),
                d.op.c_str(), d.detail.c_str());
  }
  auto it = ExpectedDiffs().find(name);
  const DiffList want = it == ExpectedDiffs().end() ? DiffList{} : it->second;
  if (seen != want) {
    std::fprintf(stderr, "FAIL parity %s: its design-decided differences changed\\n", name);
    ++g_failures;
  }
}

}  // namespace

#define PARITY_CELL(id, fn) \\
  BACKEND_TEST(id, Parity##fn) { RunCell(#fn, &fn); }

PARITY_CELL("RP0", KeyMapCollisionFree)
PARITY_CELL("RP-a", ExitNoPoseNoEnter)
PARITY_CELL("RP-b", ExitNoPoseBehindSlowEnter)
""")
for i, cell in enumerate(cells, 1):
    out.append('PARITY_CELL("RP%d", %s)\n' % (i, cell))
    if cell == "NewRequestPoseZero":
        out.append('PARITY_CELL("RP8b", PoseZeroExitBehindPendingEnter)\n')
out.append("""PARITY_CELL("RP-c", TouchAfterAnExitNeverLetThrough)
PARITY_CELL("RP-d", PreconditionMovesAtOnce)
PARITY_CELL("RP-e", CloseFinalsAtBeginClose)
PARITY_CELL("RP-f", HeldExitAndTouchEnterRefused)
""")

open(OUT, "w", encoding="utf-8", newline="\n").write("\n".join(o.rstrip("\n") for o in out) + "\n")
print("cells:", len(cells))
for i, c in enumerate(cells, 1):
    print("RP%d %s" % (i, c))
