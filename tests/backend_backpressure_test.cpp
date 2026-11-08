// Backpressure, the manifest proof at head 0 and newer-epoch rows (Sol's code review r1 of stage 2,
// F1, F3 and F4; design sections 3.5, 3.6, 3.7 and 5).
//
// F1: a link that answers kFull for half an hour (an hour under --long), S to the client, the
// client to S, and both; at every pump the frames queued above the link and the link's unacked
// ones stay bounded, then the link opens and everything converges: the rows equal, every
// obligation fulfilled exactly once, every request decided. BP1 to BP3 (this file's ids).
// F3: CV5 and CV6 at Head() 0, without a BeginEpoch (FakeSession always begins epoch 1, so these
// run Server and Client by hand): a killed BaselineEnd of the join baseline on an empty log is
// repaired; a healthy peer there is not repaired more than once per silence_baseline, and none
// once the head moved and a manifest matched.
// F4: newer-epoch rows before their marker, under churn: one entry per key, newest revision,
// smallest first_lsn, one epoch, a bounded key count; the right replay at the marker.
//
// Sol's code review r2 of the fixes: R1 (BP5: obligations and their acks are not starved by churn
// that fills the state lane every pump), R2 (BP6: S's waiting decisions are one per key and
// capped while its gameplay lane is held), R3 and R5 (BP7, BP8: the client's waiting resends are
// one per key, a newer lease report goes behind the requests made before it) and R4 (CV5h0 and
// CV5m: a manifest that finds the client staging means the BaselineEnd was lost; the client asks
// for a repair at once, also at an unchanged head a manifest had already matched).
//
// Sol's code review r3, N1 (accepted work was dropped at S's cap): the client admits only what it
// can keep until its final (BP6: max_keys_per_peer open keys, then kKeyBound; one unanswered send
// per key). S reads every frame at every pump and answers everything it reads: past its cap it
// holds a raw sender's requests and answers them once below (BP6b, over MemoryLink and
// NetplayLink), and ends the session of one that sends past what it holds instead of leaving its
// frames in the link (BP6c). A lease report made at the client's bound commits (BP9), and so does
// one S holds at its cap while the lease's renews go on (BP9b). Keys stay open across a generation
// change until their finals come (BP10).

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <vector>

#include "backend/client.h"
#include "backend/fake_network.h"
#include "backend/fault_injector.h"
#include "backend/lanes.h"
#include "backend/server.h"
#include "backend/world_state.h"
#include "backend_lanes_harness.h"
#include "backend_request_harness.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using namespace test::lanes;
namespace req = test::requests;

constexpr uint32_t kEvents = 500;  // the obligations' entities: Ent(1, 500 + i)

// What one direction held above the link and in it, at its worst.
struct Peak {
  size_t s_frames = 0;        // S's queue to the peer, every lane
  size_t s_state_room = 0;    // S's state lane: queued plus the link's unacked
  size_t s_gameplay = 0;      // S's gameplay lane queued
  size_t s_acks_due = 0;
  size_t c_frames = 0;        // the client's queue to S, every lane
  size_t c_unacked = 0;       // the client's link, state and gameplay unacked
  size_t c_waiting = 0;       // the client's unsent requests and reports, its Mismatch
  void Take(Rig& r, uint16_t peer) {
    const PeerStats st = r.S().Stats(peer);
    s_frames = std::max(s_frames, st.frames_queued);
    s_state_room = std::max(s_state_room,
                            st.state_frames_queued +
                                r.session.ServerEnd(peer).UnackedFrames(netplay::Lane::kState));
    s_gameplay = std::max(s_gameplay, st.frames_queued - st.state_frames_queued);
    s_acks_due = std::max(s_acks_due, st.acks_due);
    c_frames = std::max(c_frames, r.C(peer).QueuedFrames());
    c_unacked = std::max(c_unacked,
                         r.session.ClientEnd(peer).UnackedFrames(netplay::Lane::kState) +
                             r.session.ClientEnd(peer).UnackedFrames(netplay::Lane::kGameplay));
    c_waiting = std::max(c_waiting, r.C(peer).Waiting());
  }
  void Print(const char* what) const {
    std::fprintf(stderr,
                 "  %s: S frames %zu (state+unacked %zu, gameplay %zu, acks due %zu); client frames "
                 "%zu, unacked %zu, waiting %zu\n",
                 what, s_frames, s_state_room, s_gameplay, s_acks_due, c_frames, c_unacked,
                 c_waiting);
  }
};

// One hold of `hold` from 5 s. An obligation made at 5 s is the frame S to the client meets the
// full link with (type=obligation), so S has sent an id the client has not seen and the mark is
// due every second. The hold's first `quiet` is a quiet session (the reviewer's case: the cursor
// at the head, a manifest due every 2 s); then ten minutes of churn (a prop a second), an
// obligation every minute and a commit request every five minutes until the end; then the link
// opens and the run converges.
struct HoldResult {
  Peak peak;
  std::vector<uint64_t> obligations;
  std::vector<RequestKey> requests;
  bool mark_due = false;  // S held an id the client had not seen, a minute into the hold
  bool converged = false;
};

HoldResult Hold(const std::string& faults, Duration hold, Duration quiet = 600s) {
  FakeSessionOptions o = WithFaults(faults);
  o.step = 50ms;
  o.limits.max_unacked_state_frames = 2;  // the reviewer's probe
  Rig r(o);
  HoldResult out;
  for (uint32_t n = 1; n <= 20; ++n) r.Everywhere(Ent(1, n), kStart);
  for (uint32_t i = 0; i < 80; ++i) r.Everywhere(Ent(1, kEvents + i), kStart);
  for (uint32_t n = 1; n <= 20; ++n) r.Put(Key(test::kProp, Ent(1, n)), Val(n));
  r.Join(1);
  CHECK(r.session.RunUntil(
      [&] {
        return (r.C(1).State().Subscribed() & kScopeAll) == kScopeAll &&
               r.S().Stats(1).baseline == 0;
      },
      4s));
  auto oblige = [&] {
    const uint32_t i = uint32_t(out.obligations.size());
    const uint64_t id = r.S().Oblige(1, Key(test::kEvent, Ent(1, kEvents + i)), Val(i, 2));
    CHECK(id != 0);
    if (id != 0) out.obligations.push_back(id);
  };
  r.session.Run(kStart + 5s - r.Now());
  const TimePoint begin = r.Now();
  oblige();  // S to the client goes full with this obligation's frame (BP1, BP3)
  const TimePoint end = begin + hold;
  uint32_t value = 100;
  TimePoint next_write = begin + quiet, next_oblige = begin + quiet + 30s,
            next_request = begin + quiet + 150s;
  while (r.Now() < end) {
    if (r.Now() >= next_write && r.Now() < begin + quiet + 600s) {
      r.Put(Key(test::kProp, Ent(1, 1 + value % 20)), Val(value));
      ++value;
      next_write += 1s;
    }
    if (r.Now() >= next_oblige) {
      oblige();
      next_oblige += 60s;
    }
    if (r.Now() >= next_request) {
      const auto k = r.C(1).Send(Commit(test::kProp, Ent(1, 1 + value % 20), Val(value)), r.Now());
      CHECK(k.has_value());
      if (k) out.requests.push_back(*k);
      ++value;
      next_request += 300s;
    }
    r.session.Step();
    out.peak.Take(r, 1);
    if (r.Now() > begin + 60s && r.Now() < begin + 61s) {
      const PeerStats st = r.S().Stats(1);
      out.mark_due = st.client_seen == 0 && st.credit_used >= 1;
    }
  }
  // The link opens: the rows equal, every obligation fulfilled exactly once and ended on S, every
  // request decided, nothing left waiting on either side.
  out.converged = RunUntilEvery(
      r.session,
      [&] {
        if (!Equal(r.session, 1, false)) return false;
        if (r.S().Stats(1).obligations_outstanding != 0) return false;
        if (r.W(1).FulfilledTotal() != out.obligations.size()) return false;
        for (const RequestKey& k : out.requests) {
          if (!FinalFor(r.session, 1, k)) return false;
        }
        return r.C(1).Waiting() == 0 && r.C(1).Obligations().Holding() == 0;
      },
      180s, 500ms);
  if (!out.converged) {
    Equal(r.session, 1);
    const PeerStats st = r.S().Stats(1);
    std::fprintf(stderr,
                 "  not converged: outstanding %zu, fulfilled %zu of %zu, holding %u, waiting %zu\n",
                 st.obligations_outstanding, r.W(1).FulfilledTotal(), out.obligations.size(),
                 unsigned(r.C(1).Obligations().Holding()), r.C(1).Waiting());
    std::fprintf(stderr,
                 "  S: cursor %llu head %llu floor %llu baseline %u frames %zu held %zu unacked %zu "
                 "waiting %zu used %zu seen %llu; client prefix %llu staging %u subscribed %u\n",
                 (unsigned long long)st.cursor, (unsigned long long)r.S().State().Head(),
                 (unsigned long long)r.S().State().Floor(), st.baseline, st.frames_queued,
                 st.frames_held, st.unacked_frames, st.obligations_waiting, st.credit_used,
                 (unsigned long long)st.client_seen,
                 (unsigned long long)r.C(1).State().Prefix(), unsigned(r.C(1).State().Staging()),
                 unsigned(r.C(1).State().Subscribed()));
  }
  CHECK(r.W(1).MaxFulfilledOnce() <= 1);
  CHECK(r.session.Faults()->NotFired().empty());
  return out;
}

// The hold: half an hour, an hour under --long (the reviewer's probe ran 600 s and 3,600 s).
Duration HoldTime() { return test::Long() ? Duration(3600s) : Duration(1800s); }
std::string HoldMs() {
  return std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(HoldTime()).count());
}

BACKEND_TEST("BP1", BackpressureServerToClientFull) {
  // S to the client answers kFull (F1: the reviewer measured 150 held frames after 600 s and 900
  // after 3,600 s, manifests at the head and the mark). S queues nothing new into the held state
  // lane: no manifest, no mark, no obligation, no due baseline, no ObligationAck; its gameplay
  // lane holds the decisions of the client's requests and nothing periodic.
  HoldResult h =
      Hold("link:full peer=1 dir=s2c type=obligation from=5000 ms=" + HoldMs(), HoldTime());
  h.peak.Print("BP1");
  CHECK(h.mark_due);
  CHECK(h.peak.s_state_room <= 2 + 2);  // max_unacked_state_frames, plus a pump's two past it
  CHECK(h.peak.s_gameplay <= 1);
  CHECK(h.peak.s_frames <= 4);
  CHECK(h.peak.c_frames <= 1);
  CHECK(h.converged);
}

BACKEND_TEST("BP2", BackpressureClientToServerFull) {
  // The client to S answers kFull: the client makes no Ack, no ObligationDone and no renew while
  // its lanes are held; its requests wait in its request table, its reports in the holder; its
  // queue holds what it held at the onset. S, which reads nothing from it, stays bounded too.
  HoldResult h = Hold("link:full peer=1 dir=c2s from=5000 ms=" + HoldMs(), HoldTime());
  h.peak.Print("BP2");
  CHECK(h.peak.c_frames <= 2);
  CHECK(h.peak.c_unacked <= 2);
  CHECK(h.peak.c_waiting <= h.requests.size());
  CHECK(h.peak.s_state_room <= 2 + 2);
  CHECK(h.converged);
}

BACKEND_TEST("BP3", BackpressureBothDirectionsFull) {
  // Both directions answer kFull: both bounds at once, then convergence.
  HoldResult h = Hold("link:full peer=1 dir=s2c type=obligation from=5000 ms=" + HoldMs() +
                          " | link:full peer=1 dir=c2s from=5000 ms=" + HoldMs(),
                      HoldTime());
  h.peak.Print("BP3");
  CHECK(h.mark_due);
  CHECK(h.peak.s_state_room <= 2 + 2);
  CHECK(h.peak.s_frames <= 3);  // the client's requests never reached S: no decision queued
  CHECK(h.peak.c_frames <= 2);
  CHECK(h.peak.c_waiting <= h.requests.size());
  CHECK(h.converged);
}

BACKEND_TEST("BP4", BackpressureAcksOwedWaitInTable) {
  // S's ObligationAcks owed while its state lane is held wait in its table, one per id (at most
  // obligation_credit), and all go when it opens: the client's reported entries all go.
  FakeSessionOptions o = WithFaults("link:full peer=1 dir=s2c from=3000 ms=20000");
  o.step = 50ms;
  Rig r(o);
  // The entities come at 4 s: the obligations, sent at 2 s, are fulfilled after the hold began.
  for (uint32_t i = 0; i < 40; ++i) r.Everywhere(Ent(1, kEvents + i), kStart + 4s);
  r.Join(1);
  r.session.Run(2s);
  std::vector<uint64_t> ids;
  for (uint32_t i = 0; i < 40; ++i) {
    ids.push_back(r.S().Oblige(1, Key(test::kEvent, Ent(1, kEvents + i)), Val(i, 2)));
  }
  r.session.RunUntil([&] { return r.Now() >= kStart + 3500ms; }, 3s);  // sent and the link held
  size_t max_due = 0;
  r.session.before_pump = [&](TimePoint) { max_due = std::max(max_due, r.S().Stats(1).acks_due); };
  r.session.Run(15s);  // the client fulfils them and repeats its reports every second
  CHECK(r.W(1).FulfilledTotal() == 40);
  CHECK(r.S().Stats(1).obligations_outstanding == 0);  // its reports reached S
  CHECK(max_due == 40);
  CHECK(r.C(1).Obligations().Holding() == 40);  // no ack reached the client
  CHECK(r.session.RunUntil([&] { return r.C(1).Obligations().Holding() == 0; }, 10s));
  r.session.Run(1s);  // an ack with a floor past every id may come before the last owed ones
  CHECK(r.S().Stats(1).acks_due == 0);
  CHECK(r.C(1).Obligations().Holding() == 0);
  CHECK(r.W(1).MaxFulfilledOnce() == 1);
}

BACKEND_TEST("BP5", BackpressureObligationsThroughSaturatingChurn) {
  // R1 (the reviewer's PB3): S to the client throttled to 4,000 B/s, cap 2, 80 writes a second
  // over 400 keys for 120 s, so the walk could fill the state lane at every pump; 8 obligations
  // made in the first 75 s. Before R1 none was fulfilled until the churn stopped (the walk took
  // all the room every pump and the obligations, their acks and the mark waited behind it). Now
  // each goes once the cursor passed it: all 8 are fulfilled, ended on S and acked to the client
  // while the churn lasts, and the state lane stays within the cap plus a pump's two frames.
  FakeSessionOptions o = WithFaults("link:throttle peer=1 dir=s2c bps=4000");
  o.step = 50ms;
  o.limits.max_unacked_state_frames = 2;
  Rig r(o);
  for (uint32_t i = 0; i < 8; ++i) r.Everywhere(Ent(1, kEvents + i), kStart);
  for (uint32_t n = 1; n <= 400; ++n) r.Everywhere(Ent(1, n), kStart);
  r.Join(1);
  r.session.Run(3s);
  std::vector<uint64_t> ids;
  uint32_t v = 1000;
  const TimePoint churn_end = r.Now() + 120s;
  TimePoint next_ob = r.Now() + 5s;
  size_t max_state = 0;
  bool behind = false;  // the cursor was well off the head: the churn outran the link
  while (r.Now() < churn_end) {
    for (int k = 0; k < 4; ++k) {
      r.Put(Key(test::kProp, Ent(1, 1 + v % 400)), Val(v, 40));
      ++v;
    }
    if (r.Now() >= next_ob && ids.size() < 8) {
      const uint64_t id =
          r.S().Oblige(1, Key(test::kEvent, Ent(1, kEvents + uint32_t(ids.size()))), Val(1, 2));
      CHECK(id != 0);
      ids.push_back(id);
      next_ob += 10s;
    }
    r.session.Step();
    const PeerStats st = r.S().Stats(1);
    const size_t unacked = r.session.ServerEnd(1).UnackedFrames(netplay::Lane::kState);
    max_state = std::max(max_state, st.state_frames_queued + unacked);
    behind = behind || st.cursor + 100 < r.S().State().Head();
  }
  const PeerStats st = r.S().Stats(1);
  std::fprintf(stderr,
               "  BP5: fulfilled %zu of %zu during the churn; outstanding %zu, acks due %zu, "
               "client holding %u; cursor %llu head %llu; state lane at most %zu\n",
               r.W(1).FulfilledTotal(), ids.size(), st.obligations_outstanding, st.acks_due,
               unsigned(r.C(1).Obligations().Holding()), (unsigned long long)st.cursor,
               (unsigned long long)r.S().State().Head(), max_state);
  CHECK(ids.size() == 8);
  CHECK(behind);
  CHECK(r.W(1).FulfilledTotal() == 8);
  CHECK(st.obligations_outstanding == 0);
  CHECK(st.acks_due == 0);
  CHECK(r.C(1).Obligations().Holding() == 0);
  CHECK(max_state <= 2 + 2);
  CHECK(r.W(1).MaxFulfilledOnce() == 1);
}

// A request of the client's peer and peer epoch sent as a raw frame on the gameplay lane, past the
// client's admission (a sender that bypasses Client).
bool SendRaw(Rig& r, uint16_t peer, Request q, uint32_t sequence) {
  q.key = RequestKey{peer, r.C(peer).PeerEpoch(), r.session.Generation(), sequence};
  std::vector<uint8_t> frame;
  if (!AppendMessage(msg::Request{q}, frame)) return false;
  return r.session.ClientEnd(peer).Send(netplay::Lane::kGameplay, frame) == LinkSend::kSent;
}

size_t FinalsFor(FakeSession& s, uint16_t peer, const RequestKey& key) {
  size_t n = 0;
  for (const Decision& d : DecisionsFor(s, peer, key)) n += d.final;
  return n;
}

// The finals of a lease's key that are not cached repeats; every cached final repeats the first
// one (CHECKed). A report the client resent while unanswered (S2-L3) may meet the key's end on S
// and be answered from its cache, so a lease's key can see a cached repeat of its final.
size_t FreshFinalsFor(FakeSession& s, uint16_t peer, const RequestKey& key) {
  size_t n = 0;
  std::optional<Decision> first;
  for (const Decision& d : DecisionsFor(s, peer, key)) {
    if (!d.final) continue;
    if (!first) first = d;
    if (d.cached) {
      CHECK(d.outcome == first->outcome && d.reason == first->reason);
    } else {
      ++n;
    }
  }
  return n;
}

BACKEND_TEST("BP6", BackpressureClientAdmitsWhatItCanKeep) {
  // N1 (Sol code review r3) on R2's case (the reviewer's PB1): S to the client's gameplay lane held
  // for 120 s. First the client resends a key in the retry state at every pump: one resend is
  // taken, the rest are refused while it has no answer (kUnanswered). Then it tries a new request
  // every 100 ms, every other one kNeverResent. It takes them while it has fewer than
  // max_keys_per_peer open keys and refuses the rest itself (nullopt, kKeyBound, no key assigned).
  // S's waiting decisions stay at one per open key, and S never reaches its cap (nothing held).
  // When the lane opens every request taken gets exactly one final, kNeverResent ones included
  // (before N1, S dropped 837 of 900 and 63 got one); every key closes, and the client takes new
  // ones.
  FakeSessionOptions o = WithFaults("link:full peer=1 dir=s2c lane=gameplay from=5000 ms=120000");
  o.step = 50ms;
  o.limits.max_keys_per_peer = 32;
  Rig r(o);
  const EntityId e = Ent(1, 10);
  r.Everywhere(e, kStart);
  r.Join(1);
  CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
  int calls = 0;
  r.authorities.authority.custom = [&](const Request&,
                                       const DecideContext&) -> std::optional<Verdict> {
    if (++calls == 1) return req::Refusal(Reason::kKind, true, 1);
    return std::nullopt;
  };
  const auto retry = r.C(1).Send(Commit(test::kProp, e, Val(3)), r.Now());
  REQUIRE_OR_RETURN_VOID(retry.has_value());
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *retry)) {
          if (d.outcome == Outcome::kRefused && !d.final) return true;
        }
        return false;
      },
      2s));
  r.authorities.authority.custom = nullptr;
  r.session.RunUntil([&] { return r.Now() >= kStart + 5500ms; }, 6s);
  const size_t bound = r.session.Lim().max_keys_per_peer;
  size_t peak_frames = 0, peak_waiting = 0, peak_held = 0, peak_open = 0, resends = 0,
         unanswered = 0;
  auto take = [&] {
    const PeerStats st = r.S().Stats(1);
    peak_frames = std::max(peak_frames, st.frames_queued - st.state_frames_queued);
    peak_waiting = std::max(peak_waiting, st.decisions_waiting);
    peak_held = std::max(peak_held, st.gameplay_held);
    peak_open = std::max(peak_open, r.C(1).OpenKeys());
  };
  while (r.Now() < kStart + 30s) {  // phase 1: resends of one key
    SendRefusal why = SendRefusal::kNone;
    if (r.C(1).Resend(*retry, r.Now(), &why)) {
      ++resends;
    } else {
      unanswered += why == SendRefusal::kUnanswered;
    }
    r.session.Step();
    take();
  }
  CHECK(resends == 1);
  CHECK(unanswered > 400);
  // The resend's `pending` and `applied` are in the frame that met kFull: nothing more waits.
  CHECK(r.S().Stats(1).decisions_waiting == 0);
  std::vector<RequestKey> keys;
  std::set<RequestKey> never;
  size_t tries = 0, bound_refusals = 0, other_refusals = 0;
  uint32_t v = 10;
  TimePoint next = r.Now();
  while (r.Now() < kStart + 120s) {  // phase 2: a new request every 100 ms
    if (r.Now() >= next) {
      Request q = Commit(test::kProp, e, Val(v));
      if (v % 2 == 0) q.flags = kNeverResent;
      ++v;
      ++tries;
      const size_t open = r.C(1).OpenKeys();
      SendRefusal why = SendRefusal::kNone;
      if (auto k = r.C(1).Send(q, r.Now(), &why)) {
        keys.push_back(*k);
        if (q.flags & kNeverResent) never.insert(*k);
      } else if (why == SendRefusal::kKeyBound && open == bound) {
        ++bound_refusals;
      } else {
        ++other_refusals;
      }
      next += 100ms;
    }
    r.session.Step();
    take();
  }
  const PeerStats held = r.S().Stats(1);
  std::fprintf(stderr,
               "  BP6: %zu resend taken, %zu refused unanswered; %zu requests tried, %zu taken, "
               "%zu refused at the bound; client open keys at most %zu; S gameplay frames at most "
               "%zu, waiting at most %zu, gameplay messages held at most %zu, cap onsets %llu\n",
               resends, unanswered, tries, keys.size(), bound_refusals, peak_open, peak_frames,
               peak_waiting, peak_held, (unsigned long long)held.cap_onsets);
  CHECK(keys.size() == bound - 1);  // the retry key holds one place
  CHECK(bound_refusals == tries - keys.size());
  CHECK(other_refusals == 0);
  CHECK(!never.empty());
  CHECK(peak_open == bound);
  CHECK(peak_frames <= 1);
  CHECK(peak_waiting <= bound);   // one per open key
  CHECK(peak_held <= 4 * bound);  // server.cpp, PlayCap: what a peer using Client makes S hold
  CHECK(held.cap_onsets == 0);
  CHECK(held.requests_held == 0);
  CHECK(CountLimit(r.session, "decisions_waiting") == 0);
  // The lane opens: what waited goes; every request taken gets exactly one final and closes.
  CHECK(r.session.RunUntil(
      [&] { return r.S().Stats(1).decisions_waiting == 0 && r.C(1).OpenKeys() == 0; }, 30s));
  r.session.Run(2s);
  size_t one_final = 0, never_final = 0;
  for (const RequestKey& k : keys) {
    const size_t n = FinalsFor(r.session, 1, k);
    one_final += n == 1;
    if (n == 1 && never.contains(k)) ++never_final;
  }
  CHECK(one_final == keys.size());
  CHECK(never_final == never.size());
  const auto retry_final = FinalFor(r.session, 1, *retry);
  CHECK(retry_final && retry_final->outcome == Outcome::kApplied);
  // `pending` and the refusal; then the one resend's `pending` and `applied`; no cached repeat.
  size_t cached = 0;
  for (const Decision& d : DecisionsFor(r.session, 1, *retry)) cached += d.cached;
  CHECK(DecisionsFor(r.session, 1, *retry).size() == 4);
  CHECK(cached == 0);
  // With its keys closed the client takes new ones.
  const auto after = r.C(1).Send(Commit(test::kProp, e, Val(1000)), r.Now());
  REQUIRE_OR_RETURN_VOID(after.has_value());
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *after).has_value(); }, 2s));
}

// The gameplay cap and what S holds past it, as server.cpp computes them (PlayCap, HeldCap).
size_t PlayCapOf(const Limits& l) { return 5 * l.max_keys_per_peer + l.max_leases_per_peer; }
size_t HeldCapOf(const Limits& l) { return l.max_keys_per_peer + l.max_leases_per_peer; }

// The requests S took from `peer`'s link (the wire log counts every message S read).
uint64_t RequestsTaken(Rig& r, uint16_t peer) {
  return r.wire.counts[{peer, true}][MessageType::kRequest];
}

BACKEND_TEST("BP6b", BackpressureRawSenderHeldThenAnswered) {
  // N1, the cap's other side: a sender of raw frames that bypasses Client's admission, under LM6's
  // 1B rule on S (keys past max_keys_per_peer (K) in a generation are refused `rate`, final, one
  // decision each). S to the client's gameplay lane held for 40 s. A raw burst brings what S holds
  // for the peer (queued in the FIFO or waiting) to PlayCap: the K stored keys make `pending` and
  // `applied`, the rest one `rate` each, so after PlayCap - K requests. The next HeldCap requests
  // are held unhandled, in order: one limit event, nothing past the cap, the session not ended. S
  // reads every frame meanwhile, so the link keeps nothing, over NetplayLink too (netplay acks a
  // datagram on arrival). When the lane opens S handles what it held, and every raw request has
  // exactly one final: K applied, the rest `rate`. Before N1 S dropped every request past its cap
  // unanswered; the first N1 draft stopped reading the link at the cap instead.
  for (const bool np : {false, true}) {
    FakeSessionOptions o = WithFaults("link:full peer=1 dir=s2c lane=gameplay from=3000 ms=40000");
    o.netplay = np;
    o.step = 50ms;
    o.limits.max_keys_per_peer = 16;
    Rig r(o);
    const EntityId e = Ent(1, 10);
    r.Everywhere(e, kStart);
    r.Join(1);
    CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
    r.session.RunUntil([&] { return r.Now() >= kStart + 3500ms; }, 4s);
    const Limits& lim = r.session.Lim();
    const size_t bound = lim.max_keys_per_peer;
    const size_t cap = PlayCapOf(lim), held_cap = HeldCapOf(lim);
    const size_t n = cap - bound + held_cap;  // to the cap, then exactly HeldCap held
    std::vector<RequestKey> raw;
    for (uint32_t i = 0; i < n; ++i) {
      CHECK(SendRaw(r, 1, Commit(test::kProp, e, Val(i)), 1000 + i));
      raw.push_back(RequestKey{1, r.C(1).PeerEpoch(), r.session.Generation(), 1000 + i});
    }
    size_t peak = 0, peak_held = 0;
    bool all_read = false;
    while (r.Now() < kStart + 40s) {
      r.session.Step();
      const PeerStats st = r.S().Stats(1);
      peak = std::max(peak, st.gameplay_held);
      peak_held = std::max(peak_held, st.requests_held);
      all_read = all_read || RequestsTaken(r, 1) == n;
    }
    const PeerStats held = r.S().Stats(1);
    size_t decided = 0;
    for (const RequestKey& k : raw) decided += !DecisionsFor(r.session, 1, k).empty();
    std::fprintf(stderr,
                 "  BP6b %s: %zu raw requests, all read while held %d; S gameplay messages at most "
                 "%zu (cap %zu), requests held at most %zu (bound %zu), cap onsets %llu, %zu "
                 "decided while held\n",
                 np ? "netplay" : "memory", n, int(all_read), peak, cap, peak_held, held_cap,
                 (unsigned long long)held.cap_onsets, decided);
    CHECK(all_read);
    CHECK(peak >= cap && peak <= cap + 1);  // a commit's `pending` and `applied`
    CHECK(peak_held == held_cap);
    CHECK(held.requests_held == held_cap);
    CHECK(held.cap_onsets == 1);
    CHECK(!held.overrun);
    CHECK(CountLimit(r.session, "decisions_waiting") == 1);
    CHECK(CountServer(r.session, ServerEvent::Type::kPeerOverrun) == 0);
    CHECK(decided == 0);
    // The lane opens: S handles what it held; every raw request has exactly one final.
    CHECK(r.session.RunUntil(
        [&] {
          for (const RequestKey& k : raw) {
            if (!FinalFor(r.session, 1, k)) return false;
          }
          return true;
        },
        30s));
    r.session.Run(1s);
    size_t applied = 0, rate = 0, one = 0;
    for (const RequestKey& k : raw) {
      const auto d = FinalFor(r.session, 1, k);
      applied += d && d->outcome == Outcome::kApplied;
      rate += d && d->outcome == Outcome::kRefused && d->reason == Reason::kRate;
      one += FinalsFor(r.session, 1, k) == 1;
    }
    CHECK(applied == bound);
    CHECK(rate == raw.size() - bound);
    CHECK(one == raw.size());
    CHECK(r.S().Stats(1).requests_held == 0);
  }
}

BACKEND_TEST("BP6c", BackpressureRawFloodEndsSession) {
  // N1: a raw sender that keeps sending past what S holds at its cap, 40 frames a pump for 6.5 s,
  // over MemoryLink and NetplayLink. S cannot hold more without a bound and must not drop a
  // request unanswered, so it ends the peer's session at the first one past HeldCap: one
  // kPeerOverrun, the peer's keys gone from the request table, nothing held or queued for it, no
  // obligation made for it; from then on what it sends is read and dropped, so the link keeps
  // nothing and the sender is never refused. Before N1 S dropped the requests unanswered; the first
  // N1 draft stopped reading the link, which over NetplayLink left every frame in netplay's receive
  // queue, below the PeerLink and without a bound (a probe: 28,112 frames in 36 s). The host
  // removes the peer; its rejoin gets a new peer epoch and works.
  for (const bool np : {false, true}) {
    FakeSessionOptions o = WithFaults("link:full peer=1 dir=s2c lane=gameplay from=3000 ms=12000");
    o.netplay = np;
    o.step = 50ms;
    o.limits.max_keys_per_peer = 16;
    Rig r(o);
    const EntityId e = Ent(1, 10);
    r.Everywhere(e, kStart);
    r.Join(1);
    CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
    r.session.RunUntil([&] { return r.Now() >= kStart + 3500ms; }, 4s);
    const Limits& lim = r.session.Lim();
    const uint32_t epoch = r.C(1).PeerEpoch();
    uint32_t seq = 1000;
    uint64_t sent = 0;
    size_t peak = 0, peak_held = 0;
    TimePoint overrun_at{};
    while (r.Now() < kStart + 10s) {
      for (int i = 0; i < 40; ++i, ++seq) {
        sent += SendRaw(r, 1, Commit(test::kProp, e, Val(seq)), seq) ? 1 : 0;
      }
      r.session.Step();
      const PeerStats st = r.S().Stats(1);
      peak = std::max(peak, st.gameplay_held);
      peak_held = std::max(peak_held, st.requests_held);
      if (st.overrun && overrun_at == TimePoint{}) overrun_at = r.Now();
    }
    r.session.Run(200ms);  // the last frames arrive
    const PeerStats st = r.S().Stats(1);
    const uint64_t taken = RequestsTaken(r, 1);
    std::fprintf(stderr,
                 "  BP6c %s: %u raw frames tried, %llu sent, %llu read by S; ended at %lld ms; S "
                 "gameplay messages at most %zu (cap %zu), requests held at most %zu after a pump "
                 "(bound %zu)\n",
                 np ? "netplay" : "memory", seq - 1000, (unsigned long long)sent,
                 (unsigned long long)taken,
                 (long long)std::chrono::duration_cast<std::chrono::milliseconds>(overrun_at -
                                                                                  kStart)
                     .count(),
                 peak, PlayCapOf(lim), peak_held, HeldCapOf(lim));
    CHECK(sent == uint64_t(seq - 1000));  // the link never refused the sender
    CHECK(taken == sent);                 // every frame read: the link keeps nothing
    CHECK(st.overrun);
    CHECK(CountServer(r.session, ServerEvent::Type::kPeerOverrun, 1) == 1);
    CHECK(overrun_at != TimePoint{} && overrun_at < kStart + 4s);
    CHECK(peak <= PlayCapOf(lim) + 1);
    CHECK(peak_held <= HeldCapOf(lim));  // sampled after each pump; the end came within one
    // S decided exactly the raw keys it handled before its cap (K applied, the rest `rate`), held
    // the next HeldCap and ended the session at the one after: none of those was decided.
    std::set<RequestKey> decided;
    for (const ServerEvent& ev : r.session.ServerEvents()) {
      if (ev.type == ServerEvent::Type::kDecision && ev.peer == 1 &&
          ev.decision.key.sequence >= 1000) {
        decided.insert(ev.decision.key);
      }
    }
    CHECK(decided.size() == PlayCapOf(lim) - lim.max_keys_per_peer);
    CHECK(st.gameplay_held == 0 && st.requests_held == 0 && st.decisions_waiting == 0);
    CHECK(st.frames_queued == 0);
    CHECK(r.S().Requests().KeysOf(1, epoch) == 0);
    CHECK(r.S().Oblige(1, Key(test::kProp, e), Val(1)) == 0);
    // The host removes it; the rejoin gets a new peer epoch and works once the lane is open.
    r.session.RunUntil([&] { return r.Now() >= kStart + 16s; }, 7s);
    r.Join(1);
    CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() > epoch; }, 1s));
    CHECK(!r.S().Stats(1).overrun);
    const auto k = r.C(1).Send(Commit(test::kProp, e, Val(7)), r.Now());
    REQUIRE_OR_RETURN_VOID(k.has_value());
    CHECK(r.session.RunUntil(
        [&] {
          const auto d = FinalFor(r.session, 1, *k);
          return d && d->outcome == Outcome::kApplied;
        },
        2s));
  }
}

BACKEND_TEST("BP7", BackpressureClientResendOncePerKey) {
  // R3 (the reviewer's PB2), with N1's admission: the client to S's gameplay lane held for 55 s, a
  // key in the retry state resent at every pump. The first resend is taken and makes the frame
  // that meets kFull; every later one is refused while it has no answer (kUnanswered), so nothing
  // waits in the client's table and S answers the key once (before R3: 1,093 decisions for it).
  FakeSessionOptions o = WithFaults("link:full peer=1 dir=c2s lane=gameplay from=5000 ms=55000");
  o.step = 50ms;
  Rig r(o);
  const EntityId e = Ent(1, 10);
  r.Everywhere(e, kStart);
  r.Join(1);
  CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
  int calls = 0;
  r.authorities.authority.custom = [&](const Request&,
                                       const DecideContext&) -> std::optional<Verdict> {
    if (++calls == 1) return req::Refusal(Reason::kKind, true, 1);
    return std::nullopt;
  };
  const auto key = r.C(1).Send(Commit(test::kProp, e, Val(3)), r.Now());
  REQUIRE_OR_RETURN_VOID(key.has_value());
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *key)) {
          if (d.outcome == Outcome::kRefused && !d.final) return true;
        }
        return false;
      },
      2s));
  r.session.RunUntil([&] { return r.Now() >= kStart + 5500ms; }, 6s);
  size_t peak = 0, ok = 0, unanswered = 0;
  while (r.Now() < kStart + 60s) {
    SendRefusal why = SendRefusal::kNone;
    if (r.C(1).Resend(*key, r.Now(), &why)) {
      ++ok;
    } else {
      unanswered += why == SendRefusal::kUnanswered;
    }
    r.session.Step();
    peak = std::max(peak, r.C(1).Waiting());
  }
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *key).has_value(); }, 10s));
  r.session.Run(2s);
  std::fprintf(stderr,
               "  BP7: resends taken %zu, refused unanswered %zu, waiting at most %zu, decisions "
               "%zu\n",
               ok, unanswered, peak, DecisionsFor(r.session, 1, *key).size());
  CHECK(ok == 1);
  CHECK(unanswered > 1000);
  CHECK(peak == 0);
  CHECK(calls == 2);
  // `pending` and the refusal; the resend queued as the lane met kFull: `pending`, `applied`.
  size_t cached = 0;
  for (const Decision& d : DecisionsFor(r.session, 1, *key)) cached += d.cached;
  CHECK(DecisionsFor(r.session, 1, *key).size() == 4);
  CHECK(cached == 0);
  CHECK(r.C(1).Waiting() == 0);
  CHECK(r.C(1).OpenKeys() == 0);
  r.authorities.authority.custom = nullptr;
}

BACKEND_TEST("BP8", BackpressureNewerReportGoesBehind) {
  // R5: while the client to S's gameplay lane is held, a report of its lease, then a request, then
  // a newer report of the lease. One report goes, the newer one, and after the request (before R5
  // it took the old report's place, ahead of the request). The lease is granted before the hold
  // (N1: Report takes only a lease the client holds), and the hold is shorter than its term.
  FakeSessionOptions o = WithFaults("link:full peer=1 dir=c2s lane=gameplay from=3000 ms=2000");
  o.step = 50ms;
  Rig r(o, 10s);  // no renew goes while the lane is held
  const EntityId e = Ent(1, 10), owned = Ent(1, 11);
  r.Everywhere(e, kStart);
  r.Everywhere(owned, kStart);
  r.Join(1);
  CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
  const auto owner_key = r.C(1).Send(OwnerRun(test::kProp, owned), r.Now());
  REQUIRE_OR_RETURN_VOID(owner_key.has_value());
  uint32_t lease = 0;
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *owner_key)) {
          if (d.outcome == Outcome::kGranted) lease = d.lease;
        }
        return lease != 0;
      },
      2s));
  REQUIRE_OR_RETURN_VOID(lease != 0);
  std::vector<std::pair<MessageType, uint8_t>> order;  // what S read: type, report payload
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer != 1 || !to_server) return;
    if (auto* rep = std::get_if<msg::LeaseReport>(&m)) {
      order.emplace_back(MessageType::kLeaseReport, rep->payload.empty() ? 0 : rep->payload[0]);
    } else if (std::holds_alternative<msg::Request>(m)) {
      order.emplace_back(MessageType::kRequest, 0);
    }
  };
  r.session.RunUntil([&] { return r.Now() >= kStart + 3500ms; }, 4s);
  // The lane is held once a frame met it: a lease renew or a first request makes that frame.
  REQUIRE_OR_RETURN_VOID(r.C(1).Send(Commit(test::kProp, e, Val(1)), r.Now()).has_value());
  r.session.Step();
  r.session.Step();
  const size_t before = r.C(1).Waiting();  // 1 when a renew met kFull first
  const std::vector<uint8_t> v1{1}, v2{2};
  CHECK(r.C(1).Report(lease, v1, r.Now()));
  REQUIRE_OR_RETURN_VOID(r.C(1).Send(Commit(test::kProp, e, Val(2)), r.Now()).has_value());
  CHECK(r.C(1).Report(lease, v2, r.Now()));
  CHECK(r.C(1).Waiting() == before + 2);
  CHECK(r.session.RunUntil([&] { return r.C(1).Waiting() == 0 && order.size() >= 3; }, 10s));
  r.session.Run(1s);
  REQUIRE_OR_RETURN_VOID(order.size() == 3);
  CHECK(order[0].first == MessageType::kRequest);
  CHECK(order[1].first == MessageType::kRequest);
  CHECK(order[2].first == MessageType::kLeaseReport && order[2].second == 2);
  const auto fin = FinalFor(r.session, 1, *owner_key);
  CHECK(fin && fin->outcome == Outcome::kApplied && fin->lease == lease);
  const Record* row = r.S().State().Get(Key(test::kProp, owned));
  CHECK(row && row->value == v2);
  // A lease the client does not hold is refused at once, nothing sent.
  SendRefusal why = SendRefusal::kNone;
  CHECK(!r.C(1).Report(lease + 100, v1, r.Now(), &why));
  CHECK(why == SendRefusal::kNotHeld);
}

BACKEND_TEST("BP9", BackpressureReportAtTheBoundCommits) {
  // N1: a lease report made while the client is at its bound and S to the client's gameplay lane
  // is held. Before N1, S stopped handling the peer's requests and reports at its cap: the report
  // was dropped, nothing sent it again, and the lease never committed. Now the client takes no
  // request past max_keys_per_peer open keys (six times that are tried, past any cap of S's),
  // the report is taken and S handles it while the lane is still held; once the lane opens the key
  // has `applied`, the lease its LeaseEnd `committed`, and every request taken its one final.
  FakeSessionOptions o = WithFaults("link:full peer=1 dir=s2c lane=gameplay from=5000 ms=60000");
  o.step = 50ms;
  o.limits.max_keys_per_peer = 16;
  Rig r(o);
  const EntityId owned = Ent(1, 10), other = Ent(1, 11);
  r.Everywhere(owned, kStart);
  r.Everywhere(other, kStart);
  r.Join(1);
  CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
  const auto owner_key = r.C(1).Send(OwnerRun(test::kProp, owned), r.Now());
  REQUIRE_OR_RETURN_VOID(owner_key.has_value());
  uint32_t lease = 0;
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *owner_key)) {
          if (d.outcome == Outcome::kGranted) lease = d.lease;
        }
        return lease != 0;
      },
      2s));
  REQUIRE_OR_RETURN_VOID(lease != 0);
  r.session.RunUntil([&] { return r.Now() >= kStart + 5500ms; }, 6s);
  const size_t bound = r.session.Lim().max_keys_per_peer;
  std::vector<RequestKey> keys;
  size_t refused = 0;
  for (uint32_t i = 0; i < 6 * bound; ++i) {  // past S's cap too (5 K), had the client taken them
    SendRefusal why = SendRefusal::kNone;
    if (auto k = r.C(1).Send(Commit(test::kProp, other, Val(i)), r.Now(), &why)) {
      keys.push_back(*k);
    } else {
      refused += why == SendRefusal::kKeyBound;
    }
    r.session.Step();
  }
  CHECK(keys.size() == bound - 1);  // the owner-run key holds one place
  CHECK(refused == 6 * bound - keys.size());
  CHECK(r.C(1).OpenKeys() == bound);
  SendRefusal why = SendRefusal::kNone;
  CHECK(r.C(1).Report(lease, Val(42), r.Now(), &why));
  CHECK(why == SendRefusal::kNone);
  // It went to S and has no answer yet: a second report waits for it.
  CHECK(!r.C(1).Report(lease, Val(43), r.Now(), &why));
  CHECK(why == SendRefusal::kUnanswered);
  r.session.Run(5s);  // the lane still held: S handles the report meanwhile
  const Lease* l = r.S().Leases().Get(lease);
  CHECK(l && l->state == LeaseState::kCommitted);
  CHECK(r.S().Stats(1).cap_onsets == 0);
  CHECK(!FinalFor(r.session, 1, *owner_key).has_value());  // nothing delivered yet
  // The lane opens.
  CHECK(r.session.RunUntil([&] { return r.C(1).OpenKeys() == 0; }, 70s));
  r.session.Run(1s);
  const auto fin = FinalFor(r.session, 1, *owner_key);
  CHECK(fin && fin->outcome == Outcome::kApplied && fin->lease == lease);
  CHECK(FreshFinalsFor(r.session, 1, *owner_key) == 1);  // cached repeats aside (S2-L3)
  const Record* row = r.S().State().Get(Key(test::kProp, owned));
  CHECK(row && row->value == Val(42));
  bool committed = false;
  for (const ClientEvent& ev : r.session.ClientEvents(1)) {
    if (ev.type == ClientEvent::Type::kLeaseEnded && ev.lease == lease) {
      committed = ev.lease_end == LeaseEndReason::kCommitted;
    }
  }
  CHECK(committed);
  size_t one = 0;
  for (const RequestKey& k : keys) one += FinalsFor(r.session, 1, k) == 1;
  CHECK(one == keys.size());
}

BACKEND_TEST("BP9b", BackpressureReportHeldAtCapCommits) {
  // N1, a lease report at S's cap: S to the client's gameplay lane held for 20 s, and a raw sender
  // on the client's link brings what S holds for the peer to PlayCap, one raw request past it
  // held. The client's report of its lease comes behind it: S holds it too, in order, and handles
  // the lease's renews at once, so the lease outlives its 3 s term through a 6 s wait. When the
  // lane opens S handles what it held: the report commits (the key `applied` with the lease, its
  // LeaseEnd `committed`, the row the report's value) and every raw request has one final. Before
  // N1, S dropped the report at its cap and the lease never committed; the first N1 draft stopped
  // reading the link, so the renews waited too and the lease expired.
  FakeSessionOptions o = WithFaults("link:full peer=1 dir=s2c lane=gameplay from=5000 ms=20000");
  o.step = 50ms;
  o.limits.max_keys_per_peer = 16;
  Rig r(o);
  const EntityId owned = Ent(1, 10), other = Ent(1, 11);
  r.Everywhere(owned, kStart);
  r.Everywhere(other, kStart);
  r.Join(1);
  CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
  const auto owner_key = r.C(1).Send(OwnerRun(test::kProp, owned), r.Now());
  REQUIRE_OR_RETURN_VOID(owner_key.has_value());
  uint32_t lease = 0;
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : DecisionsFor(r.session, 1, *owner_key)) {
          if (d.outcome == Outcome::kGranted) lease = d.lease;
        }
        return lease != 0;
      },
      2s));
  REQUIRE_OR_RETURN_VOID(lease != 0);
  r.session.RunUntil([&] { return r.Now() >= kStart + 5500ms; }, 6s);
  // The owner-run key holds one of the K stored keys: K - 1 raw commits make `pending` and
  // `applied`, the rest one `rate` each, so PlayCap comes after PlayCap - K + 1 raw requests.
  const Limits& lim = r.session.Lim();
  const size_t n = PlayCapOf(lim) - (lim.max_keys_per_peer - 1) + 1;
  std::vector<RequestKey> raw;
  for (uint32_t i = 0; i < n; ++i) {
    CHECK(SendRaw(r, 1, Commit(test::kProp, other, Val(i)), 1000 + i));
    raw.push_back(RequestKey{1, r.C(1).PeerEpoch(), r.session.Generation(), 1000 + i});
  }
  r.session.Step();
  CHECK(r.S().Stats(1).requests_held == 1);
  SendRefusal why = SendRefusal::kNone;
  CHECK(r.C(1).Report(lease, Val(42), r.Now(), &why));
  r.session.Step();
  CHECK(r.S().Stats(1).requests_held == 2);  // the report, behind the raw request
  r.session.Run(6s);                         // twice the lease's term, the lane still held
  const Lease* l = r.S().Leases().Get(lease);
  CHECK(l && l->state == LeaseState::kGranted);  // renewed; the report not handled yet
  CHECK(r.S().Stats(1).requests_held == 2);
  CHECK(!FinalFor(r.session, 1, *owner_key).has_value());
  // The lane opens.
  CHECK(r.session.RunUntil(
      [&] { return r.C(1).OpenKeys() == 0 && r.S().Stats(1).requests_held == 0; }, 30s));
  r.session.Run(1s);
  const auto fin = FinalFor(r.session, 1, *owner_key);
  CHECK(fin && fin->outcome == Outcome::kApplied && fin->lease == lease);
  CHECK(FreshFinalsFor(r.session, 1, *owner_key) == 1);  // cached repeats aside (S2-L3)
  const Record* row = r.S().State().Get(Key(test::kProp, owned));
  CHECK(row && row->value == Val(42));
  bool committed = false;
  for (const ClientEvent& ev : r.session.ClientEvents(1)) {
    if (ev.type == ClientEvent::Type::kLeaseEnded && ev.lease == lease) {
      committed = ev.lease_end == LeaseEndReason::kCommitted;
    }
  }
  CHECK(committed);
  size_t one = 0;
  for (const RequestKey& k : raw) one += FinalsFor(r.session, 1, k) == 1;
  CHECK(one == raw.size());
  CHECK(r.S().Stats(1).cap_onsets == 1);
  CHECK(CountServer(r.session, ServerEvent::Type::kPeerOverrun) == 0);
}

BACKEND_TEST("BP10", BackpressureKeysOpenAcrossGenerations) {
  // N1: S to the client's gameplay lane held while the generation changes. EpochBegin comes on the
  // state lane, the old keys' finals wait on S for the gameplay lane: the client keeps those keys
  // open, so it still refuses new ones (before, it forgot them at EpochBegin and could take
  // max_keys_per_peer more per generation while S still held the old finals). When the lane opens
  // each old key gets its one final and closes, an old key's `applied` too (it completes nothing
  // in the new generation), and the client takes a key of the new generation.
  FakeSessionOptions o = WithFaults("link:full peer=1 dir=s2c lane=gameplay from=3000 ms=20000");
  o.step = 50ms;
  o.limits.max_keys_per_peer = 8;
  Rig r(o);
  const EntityId e = Ent(1, 10);
  r.Everywhere(e, kStart);
  r.Join(1);
  CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
  r.session.RunUntil([&] { return r.Now() >= kStart + 3500ms; }, 4s);
  const size_t bound = r.session.Lim().max_keys_per_peer;
  // Half commits (applied at once, their `applied` waiting on S), then half server-run ones
  // nobody evaluates (pending, one evaluating at a time: S's final at the change).
  std::vector<RequestKey> commits, runs;
  for (size_t i = 0; i < bound; ++i) {
    const bool commit = i < bound / 2;
    const auto k = commit ? r.C(1).Send(Commit(test::kProp, e, Val(uint32_t(i))), r.Now())
                          : r.C(1).Send(ServerRun(test::kToggle, e), r.Now());
    REQUIRE_OR_RETURN_VOID(k.has_value());
    (commit ? commits : runs).push_back(*k);
    r.session.Step();
  }
  r.session.Run(1s);
  const uint32_t old_generation = r.session.Generation();
  r.session.NewGeneration();
  r.session.Run(2s);  // EpochBegin reaches the client on the state lane
  SendRefusal why = SendRefusal::kNone;
  CHECK(!r.C(1).Send(Commit(test::kProp, e, Val(99)), r.Now(), &why).has_value());
  CHECK(why == SendRefusal::kKeyBound);
  CHECK(r.C(1).OpenKeys() == bound);
  // The lane opens.
  CHECK(r.session.RunUntil([&] { return r.C(1).OpenKeys() == 0; }, 30s));
  r.session.Run(1s);
  size_t applied = 0, stale = 0;
  for (const RequestKey& k : commits) {
    const auto d = FinalFor(r.session, 1, k);
    applied += d && d->outcome == Outcome::kApplied && FinalsFor(r.session, 1, k) == 1;
  }
  for (const RequestKey& k : runs) {
    const auto d = FinalFor(r.session, 1, k);
    stale += d && d->reason == Reason::kStaleGeneration && FinalsFor(r.session, 1, k) == 1;
  }
  CHECK(applied == commits.size());
  CHECK(stale == runs.size());
  const auto fresh = r.C(1).Send(Commit(test::kProp, e, Val(100)), r.Now());
  REQUIRE_OR_RETURN_VOID(fresh.has_value());
  CHECK(fresh->generation == old_generation + 1);
  CHECK(r.session.RunUntil([&] { return FinalFor(r.session, 1, *fresh).has_value(); }, 2s));
}

// ---- F3: the manifest proof at Head() 0 --------------------------------------------------------

// Server and Client by hand on a MemoryLink, with no BeginEpoch: S's log stays at 0 until a write.
struct HeadZero {
  explicit HeadZero(const std::string& rules = {})
      : server(kLanesTable, authorities.set, Limits{}),
        client(kLanesTable, world.Adapters(), Limits{}) {
    auto [s, c] = MemoryLink::Pair(&now);
    s_mem = std::move(s);
    c_mem = std::move(c);
    PeerLink* s_end = s_mem.get();
    PeerLink* c_end = c_mem.get();
    if (!rules.empty()) {
      auto parsed = ParseFaultRules(rules);
      CHECK(parsed.has_value());
      faults = std::make_unique<FaultInjector>(parsed ? *parsed : std::vector<FaultRule>{}, 1,
                                               std::span<const KindSpec>(kLanesTable), now);
      s_fault = faults->Wrap(*s_mem, 1, true);
      c_fault = faults->Wrap(*c_mem, 1, false);
      s_end = s_fault.get();
      c_end = c_fault.get();
    }
    server.AddPeer(1, PeerRole::kOwner, *s_end, now);
    client.Attach(*c_end, now);
  }
  void Step() {
    if (faults) faults->Advance(now);
    server.Pump(now);
    client.Pump(now);
    ServerEvent e;
    while (server.PollEvent(e)) {
      if (e.type == ServerEvent::Type::kBaselineBegun) ++begun[e.why];
    }
    ClientEvent ce;
    while (client.PollEvent(ce)) {
      if (ce.type == ClientEvent::Type::kBaselineLost) ++lost;
    }
    now += 50ms;
  }
  void Run(Duration d) {
    const TimePoint until = now + d;
    while (now < until) Step();
  }
  size_t Lost() const { return lost; }
  size_t Begun(BaselineReason why) const {
    auto it = begun.find(why);
    return it == begun.end() ? 0 : it->second;
  }

  TimePoint now = kStart;
  Authorities authorities;
  TestWorld world;
  Server server;
  Client client;
  std::unique_ptr<MemoryLink> s_mem, c_mem;
  std::unique_ptr<FaultInjector> faults;
  std::unique_ptr<PeerLink> s_fault, c_fault;
  std::map<BaselineReason, size_t> begun;
  size_t lost = 0;
};

BACKEND_TEST("CV5h0", ConvergeKilledJoinEndAtHeadZero) {
  // A fresh empty S (no BeginEpoch, Head() 0): the join baseline's BaselineEnd killed. Before F3
  // the initial manifest_ok 0 read as a match at the head and the client stayed staging forever;
  // F3 left it to the silence rule (60 s). Now the first manifest finds the client staging, so
  // the end was lost, and the client asks for a repair (R4): repaired within a few seconds.
  HeadZero h("message:kill dir=s2c peer=1 type=baseline_end nth=1");
  h.Run(1s);
  CHECK(h.faults->Fired(0) == 1);
  CHECK(h.client.State().Staging() == kScopeSession);
  CHECK(h.client.State().Subscribed() == 0);
  CHECK(!h.client.State().Completes(Scope::kSession, 0));
  h.Run(5s);
  CHECK(h.server.State().Head() == 0);
  CHECK(h.Lost() == 1);
  CHECK(h.Begun(BaselineReason::kDigestMismatch) == 1);
  CHECK(h.Begun(BaselineReason::kSilence) == 0);
  CHECK(h.client.State().Staging() == 0);
  CHECK(h.client.State().Subscribed() == kScopeSession);
  CHECK(h.client.State().Completes(Scope::kSession, 0));
  h.Run(130s);  // the silence rule still runs at head 0 (CV6h0); no further repair for a lost end
  CHECK(h.Lost() == 1);
  CHECK(h.Begun(BaselineReason::kDigestMismatch) == 1);
}

BACKEND_TEST("CV5m", ConvergeKilledLevelEndAtMatchedHead) {
  // R4's probe (PB4): a manifest matched at head 2, then the level-ready baseline begins at the
  // same head and its BaselineEnd is killed. S's proof at the head stands (the matched manifest_ok
  // is the head), so the silence rule never runs; before R4 the client staged the level for good.
  // The next manifest finds it staging and it asks for a repair, which covers both scopes.
  FakeSessionOptions o = WithFaults("message:kill dir=s2c peer=1 type=baseline_end nth=2");
  o.step = 50ms;
  Rig r(o);
  r.Put(Key(test::kProp, Ent(1, 1)), Val(1));
  r.Everywhere(Ent(1, 1), kStart);
  r.Join(1, /*ready=*/false);
  CHECK(r.session.RunUntil(
      [&] {
        const PeerStats st = r.S().Stats(1);
        return st.manifest_ok == r.S().State().Head() && st.manifest_ok != 0 && st.baseline == 0;
      },
      20s));
  const uint64_t head = r.S().State().Head();
  r.session.LevelReady(1);
  CHECK(r.session.RunUntil(
      [&] {
        return r.C(1).State().Subscribed() == kScopeAll && r.C(1).State().Staging() == 0 &&
               r.S().Stats(1).baseline == 0;
      },
      10s));
  CHECK(r.session.Faults()->Fired(0) == 1);
  CHECK(r.S().State().Head() == head);
  CHECK(CountClient(r.session, 1, ClientEvent::Type::kBaselineLost) == 1);
  size_t digest = 0, silence = 0;
  for (const ServerEvent& e : r.session.ServerEvents()) {
    if (e.type != ServerEvent::Type::kBaselineBegun) continue;
    digest += e.why == BaselineReason::kDigestMismatch;
    silence += e.why == BaselineReason::kSilence;
  }
  CHECK(digest == 1);
  CHECK(silence == 0);
  const Record* row = r.C(1).State().Get(Key(test::kProp, Ent(1, 1)));
  CHECK(row && row->value == Val(1));
  CHECK(r.C(1).State().Completes(Scope::kLevel, head));
}

BACKEND_TEST("CV6h0", ConvergeQuietAtHeadZero) {
  // A healthy peer on an empty log (Head() 0): a manifest matched at 0 leaves manifest_ok at its
  // initial 0, so S cannot see the match and the silence rule runs, at most once per
  // silence_baseline (an empty baseline). Once a write moves the head and a manifest matches, the
  // proof at the head stands and nothing repairs in a quiet session.
  HeadZero h;
  h.Run(200s);
  CHECK(h.server.State().Head() == 0);
  CHECK(h.client.State().Subscribed() == kScopeSession);
  CHECK(h.client.State().Staging() == 0);
  const size_t silence = h.Begun(BaselineReason::kSilence);
  CHECK(silence >= 1 && silence <= 3);
  CHECK(h.server.State().Put(Key(test::kTracker, SessionObject(1)), Val(7), h.now) ==
        PutResult::kWritten);
  h.Run(10s);
  CHECK(h.server.Stats(1).manifest_ok == 1);
  const size_t before = h.Begun(BaselineReason::kSilence);
  h.Run(150s);
  CHECK(h.Begun(BaselineReason::kSilence) == before);
  const Record* row = h.client.State().Get(Key(test::kTracker, SessionObject(1)));
  CHECK(row && row->value == Val(7));
}

// ---- F4: newer-epoch rows before their marker --------------------------------------------------

Record Row(const RecordKey& key, uint32_t revision, uint32_t epoch, uint64_t lsn,
           std::vector<uint8_t> value, bool tomb = false) {
  Record r;
  r.key = key;
  r.revision = revision;
  r.epoch = epoch;
  r.lsn = lsn;
  r.tomb = tomb;
  if (!tomb) r.value = std::move(value);
  return r;
}

void Subscribe(Replica& rep, uint32_t g) {
  rep.OnBaselineBegin(1, 0, g, kScopeAll);
  rep.OnBaselineEnd(1, 0, kBaselineDigestSeed);
}

BACKEND_TEST("VR3b", VersionsFutureChurnCoalesced) {
  // 10,000 revisions over 8 keys of generation 2 (and duplicates) before EpochBegin{2}: one
  // entry per key, its newest revision, the smallest first_lsn; bytes do not grow. At the marker
  // each key comes in at its newest revision, ordered and unsettled with the first first_lsn.
  Replica rep(kLanesTable, Limits{});
  Subscribe(rep, 1);
  uint64_t lsn = 10;
  std::map<RecordKey, uint32_t> newest;
  std::map<RecordKey, uint64_t> first;
  size_t max_rows = 0, max_bytes = 0;
  for (uint32_t rev = 1; rev <= 1250; ++rev) {
    for (uint32_t n = 1; n <= 8; ++n) {
      const RecordKey k = Key(test::kProp, Ent(2, n));
      ++lsn;
      first.try_emplace(k, lsn);
      CHECK(rep.OnRecord(Row(k, rev, 2, lsn, Val(rev)), lsn) == ApplyResult::kStaged);
      // A duplicate of an older revision is stale and changes nothing but nothing grows.
      if (rev > 1) CHECK(rep.OnRecord(Row(k, rev - 1, 2, lsn, Val(rev - 1)), lsn) == ApplyResult::kStale);
      newest[k] = rev;
      max_rows = std::max(max_rows, rep.FutureRows());
      max_bytes = std::max(max_bytes, rep.FutureBytes());
    }
  }
  CHECK(max_rows == 8);
  CHECK(rep.FutureRows() == 8);
  CHECK(rep.FutureEpoch() == 2);
  CHECK(max_bytes == rep.FutureBytes());
  CHECK(rep.Rows() == 0 + 0);  // nothing of generation 2 is in the replica yet
  rep.OnEpoch(2, 5);
  CHECK(rep.FutureRows() == 0 && rep.FutureBytes() == 0 && rep.FutureEpoch() == 0);
  for (const auto& [k, rev] : newest) {
    const Record* got = rep.Get(k);
    CHECK(got && got->revision == rev && got->value == Val(rev) && got->epoch == 2);
    CHECK(rep.FirstLsn(k) == first.at(k));
  }
  // The adapters get each key once, at its newest revision.
  Record out;
  size_t taken = 0;
  while (rep.TakeReady(out)) {
    ++taken;
    CHECK(out.revision == newest.at(out.key));
    rep.OnApplied(out.key, out.revision, RowApply::kApplied, kStart);
  }
  CHECK(taken == 8);
}

BACKEND_TEST("VR3c", VersionsFutureEpochsBounded) {
  // Rows of generations 2, 3 and 4 before any marker (their EpochBegins killed): only the newest
  // generation waits (on the ordered lane a row of 4 proves the markers of 2 and 3 have passed),
  // and an older one's rows that come after it are dropped. Keys past max_rows_per_level are
  // dropped and counted. The marker of 4 replays exactly 4's rows.
  Limits limits;
  limits.max_rows_per_level = 16;
  Replica rep(kLanesTable, limits);
  Subscribe(rep, 1);
  uint64_t lsn = 10;
  for (uint32_t n = 1; n <= 10; ++n) {
    ++lsn;
    CHECK(rep.OnRecord(Row(Key(test::kProp, Ent(2, n)), 1, 2, lsn, Val(n)), lsn) ==
          ApplyResult::kStaged);
  }
  CHECK(rep.FutureRows() == 10 && rep.FutureEpoch() == 2);
  for (uint32_t n = 1; n <= 4; ++n) {
    ++lsn;
    CHECK(rep.OnRecord(Row(Key(test::kProp, Ent(3, n)), 1, 3, lsn, Val(n)), lsn) ==
          ApplyResult::kStaged);
  }
  CHECK(rep.FutureRows() == 4 && rep.FutureEpoch() == 3);
  for (uint32_t n = 1; n <= 20; ++n) {
    ++lsn;
    rep.OnRecord(Row(Key(test::kProp, Ent(4, n)), 1, 4, lsn, Val(100 + n)), lsn);
  }
  CHECK(rep.FutureRows() == 16 && rep.FutureEpoch() == 4);
  CHECK(rep.FutureDropped() == 4);
  // A row of generation 3 after one of 4: its marker can no longer come.
  ++lsn;
  CHECK(rep.OnRecord(Row(Key(test::kProp, Ent(3, 9)), 1, 3, lsn, Val(9)), lsn) ==
        ApplyResult::kWrongEpoch);
  CHECK(rep.FutureRows() == 16);
  rep.OnEpoch(4, 5);
  CHECK(rep.Rows() == 16);
  CHECK(rep.FutureRows() == 0);
  for (uint32_t n = 1; n <= 16; ++n) {
    const Record* got = rep.Get(Key(test::kProp, Ent(4, n)));
    CHECK(got && got->value == Val(100 + n));
  }
  CHECK(rep.Get(Key(test::kProp, Ent(4, 17))) == nullptr);
  CHECK(rep.Get(Key(test::kProp, Ent(2, 1))) == nullptr);
}

BACKEND_TEST("VR3d", VersionsFutureWaitsThroughEarlierMarker) {
  // Rows of generation 3 wait through the marker of 2 (a later generation keeps waiting) and go
  // in at the marker of 3.
  Replica rep(kLanesTable, Limits{});
  Subscribe(rep, 1);
  const RecordKey k = Key(test::kProp, Ent(3, 1));
  CHECK(rep.OnRecord(Row(k, 4, 3, 40, Val(4)), 40) == ApplyResult::kStaged);
  rep.OnEpoch(2, 30);
  CHECK(rep.FutureRows() == 1 && rep.Get(k) == nullptr);
  rep.OnEpoch(3, 35);
  const Record* got = rep.Get(k);
  CHECK(got && got->revision == 4);
  CHECK(rep.FutureRows() == 0);
}

}  // namespace
}  // namespace skylanders::backend
