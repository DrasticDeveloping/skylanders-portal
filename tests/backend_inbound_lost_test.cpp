// Client::InboundLost (stage 3, future/online-3.md section 3.2, "Recoverable overflow on a client"):
// the game's bridge discarded recoverable state messages below the client and its receive reached
// the loss mark. A scripted link feeds the client exactly what S would send, so every step of the
// skip-until-baseline rule is checked: staging dropped, one Mismatch{prefix, 0}, recoverable
// messages discarded (deltas, manifests, an old baseline's begin and rows), control messages read
// (EpochBegin, a Decision), and the first baseline numbered above the last one read before the
// mark taken, after which deltas apply again.

#include <chrono>
#include <cstdint>
#include <deque>
#include <map>
#include <span>
#include <utility>
#include <variant>
#include <vector>

#include "backend/adapter.h"
#include "backend/client.h"
#include "backend/fake_network.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/server.h"
#include "backend/world_state.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

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

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;

const TimePoint kT0 = TimePoint{} + std::chrono::hours(1);

// S's side of one client, by hand: frames queued for the client, frames the client sent.
class ScriptLink final : public PeerLink {
 public:
  LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) override {
    sent.emplace_back(lane, std::vector<uint8_t>(frame.begin(), frame.end()));
    return LinkSend::kSent;
  }
  bool Receive(netplay::Lane& lane, std::vector<uint8_t>& frame) override {
    if (inbox.empty()) return false;
    lane = inbox.front().first;
    frame = std::move(inbox.front().second);
    inbox.pop_front();
    return true;
  }
  size_t UnackedFrames(netplay::Lane) const override { return 0; }
  std::chrono::microseconds Rtt() const override { return 1ms; }
  bool Connected() const override { return true; }

  void Push(const Message& m) {
    std::vector<uint8_t> frame;
    CHECK(AppendMessage(m, frame));
    inbox.emplace_back(LaneOf(TypeOf(m)), std::move(frame));
  }
  // Every Mismatch the client sent so far.
  std::vector<msg::Mismatch> Mismatches() const {
    std::vector<msg::Mismatch> out;
    for (const auto& [lane, frame] : sent) {
      std::vector<Message> ms;
      DecodeFrame(frame, ms);
      for (const Message& m : ms) {
        if (auto* x = std::get_if<msg::Mismatch>(&m)) out.push_back(*x);
      }
    }
    return out;
  }

  std::deque<std::pair<netplay::Lane, std::vector<uint8_t>>> inbox;
  std::vector<std::pair<netplay::Lane, std::vector<uint8_t>>> sent;
};

const auto& kTable = test::kTestTableFull;

RecordKey CounterKey() {
  return RecordKey{test::kCounter, Authored(0, 1, HashName("e1"), HashName("test")), 0};
}

Record Counter(uint32_t revision, uint64_t lsn) {
  Record r;
  r.key = CounterKey();
  r.revision = revision;
  r.lsn = lsn;
  r.value = {uint8_t(revision), 0, 0, 0};
  return r;
}

msg::Delta DeltaOf(uint32_t revision, uint64_t lsn) { return msg::Delta{Counter(revision, lsn), lsn}; }

// StateHello and an empty join baseline of every scope: the client is subscribed, prefix 0.
void Bootstrap(ScriptLink& link) {
  link.Push(msg::StateHello{TableDigest(kTable), 1, 0, 1, 1});
  link.Push(msg::BaselineBegin{1, 0, 1, kScopeAll, 0, 0, BaselineReason::kJoin});
  link.Push(msg::BaselineEnd{1, 0, kBaselineDigestSeed});
}

uint32_t RevisionOf(const Client& c) {
  const Record* r = c.State().Get(CounterKey());
  return r ? r->revision : 0;
}

// The client's end of a real link with the bridge's discard in front: while `drop` holds, a frame
// whose messages are all recoverable state is removed before the client reads it.
class LossyLink final : public PeerLink {
 public:
  explicit LossyLink(PeerLink& inner) : inner_(inner) {}
  LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) override {
    return inner_.Send(lane, frame);
  }
  bool Receive(netplay::Lane& lane, std::vector<uint8_t>& frame) override {
    while (inner_.Receive(lane, frame)) {
      if (!drop || !AllRecoverable(frame)) return true;
      ++dropped;
    }
    return false;
  }
  size_t UnackedFrames(netplay::Lane lane) const override { return inner_.UnackedFrames(lane); }
  std::chrono::microseconds Rtt() const override { return inner_.Rtt(); }
  bool Connected() const override { return inner_.Connected(); }

  bool drop = false;
  size_t dropped = 0;

 private:
  static bool AllRecoverable(std::span<const uint8_t> frame) {
    std::vector<Message> ms;
    if (!DecodeFrame(frame, ms) || ms.empty()) return false;
    for (const Message& m : ms) {
      const bool recoverable =
          std::holds_alternative<msg::Delta>(m) || std::holds_alternative<msg::Tomb>(m) ||
          std::holds_alternative<msg::Through>(m) || std::holds_alternative<msg::Manifest>(m) ||
          std::holds_alternative<msg::BaselineBegin>(m) ||
          std::holds_alternative<msg::BaselineRows>(m) ||
          std::holds_alternative<msg::BaselineEnd>(m);
      if (!recoverable) return false;
    }
    return true;
  }
  PeerLink& inner_;
};

size_t CountEvents(Client& c, ClientEvent::Type type) {
  size_t n = 0;
  ClientEvent e;
  while (c.PollEvent(e)) n += e.type == type ? 1 : 0;
  return n;
}

}  // namespace

BACKEND_TEST("S3-IL1", InboundLostSkipsUntilNewBaseline) {
  AdapterSet adapters;  // no adapters: rows settle as dropped, the replica still holds them
  Limits limits;
  Client c(kTable, adapters, limits);
  ScriptLink link;
  c.Attach(link, kT0);
  Bootstrap(link);
  link.Push(DeltaOf(1, 1));
  link.Push(DeltaOf(2, 2));
  link.Push(msg::Through{2});
  c.Pump(kT0);
  CHECK(c.Self() == 1);
  CHECK(RevisionOf(c) == 2);
  CHECK(c.State().Prefix() == 2);
  CHECK(!c.AwaitingRepairBaseline());

  // A baseline staging when the loss happens (its begin read, its rows and end discarded): the
  // staging goes, the active row stays.
  link.Push(msg::BaselineBegin{2, 2, 1, kScopeAll, 1, 0, BaselineReason::kSilence});
  c.Pump(kT0 + 10ms);
  CHECK(c.State().Staging() == kScopeAll);
  // The bridge removed BaselineRows{2}, BaselineEnd{2} and Delta{3} and put the mark there.
  CHECK(CountEvents(c, ClientEvent::Type::kInboundLost) == 0);
  c.InboundLost(kT0 + 20ms);
  CHECK(c.AwaitingRepairBaseline());
  CHECK(c.State().Staging() == 0);
  CHECK(RevisionOf(c) == 2);
  auto mismatches = link.Mismatches();
  REQUIRE_OR_RETURN_VOID(mismatches.size() == 1);
  CHECK(mismatches[0].lsn == 2 && mismatches[0].bucket_mask == 0);
  CHECK(mismatches[0].lost);  // forced: S never counts a running baseline as its answer (F1)
  CHECK(CountEvents(c, ClientEvent::Type::kInboundLost) == 1);

  // What S sent before it read the Mismatch: discarded (a delta, a Through, a manifest that
  // would mismatch, the rest of baseline 2, a repeated begin of baseline 2), but control is read.
  link.Push(DeltaOf(4, 4));
  link.Push(msg::Through{4});
  msg::Manifest mf;
  mf.lsn = 4;
  mf.scopes = kScopeAll;
  mf.digests.fill(0x1234);
  link.Push(mf);
  link.Push(msg::BaselineRows{2, 0, {Counter(3, 3)}});
  link.Push(msg::BaselineEnd{2, 3, 0});
  link.Push(msg::BaselineBegin{2, 4, 1, kScopeAll, 1, 0, BaselineReason::kSilence});
  link.Push(msg::EpochBegin{2, 5});
  msg::Decision d;
  d.decision.key = RequestKey{1, 1, 1, 7};
  d.decision.outcome = Outcome::kRefused;
  d.decision.final = true;
  link.Push(d);
  c.Pump(kT0 + 30ms);
  CHECK(RevisionOf(c) == 2);
  CHECK(c.State().Prefix() == 2);
  CHECK(c.State().Staging() == 0);
  CHECK(c.State().Epoch() == 2);  // EpochBegin is control: read
  CHECK(c.AwaitingRepairBaseline());
  CHECK(c.DiscardedAfterLoss() == 6);
  CHECK(link.Mismatches().size() == 1);  // the discarded manifest asked for nothing
  {
    size_t decided = 0, mismatch = 0;
    ClientEvent e;
    while (c.PollEvent(e)) {
      decided += e.type == ClientEvent::Type::kDecided ? 1 : 0;
      mismatch += e.type == ClientEvent::Type::kManifestMismatch ? 1 : 0;
    }
    CHECK(decided == 1);  // a Decision is never discarded
    CHECK(mismatch == 0);
  }

  // The repair baseline (id 3, above 2): taken; the row comes back at S's revision, and deltas
  // after it apply again.
  const Record repaired = Counter(5, 5);
  link.Push(msg::BaselineBegin{3, 5, 2, kScopeAll, 1, 0, BaselineReason::kDigestMismatch});
  link.Push(msg::BaselineRows{3, 0, {repaired}});
  link.Push(msg::BaselineEnd{3, 5, BaselineDigestAdd(kBaselineDigestSeed, repaired)});
  link.Push(DeltaOf(6, 6));
  link.Push(msg::Through{6});
  c.Pump(kT0 + 40ms);
  CHECK(!c.AwaitingRepairBaseline());
  CHECK(RevisionOf(c) == 6);
  CHECK(c.State().Prefix() == 6);
  CHECK(CountEvents(c, ClientEvent::Type::kBaselineApplied) == 1);
  CHECK(link.Mismatches().size() == 1);
}

BACKEND_TEST("S3-IL2", InboundLostTwiceKeepsFloor) {
  // A second loss while the first is still being repaired: one more Mismatch, the same id floor,
  // so a baseline S began after the first Mismatch still ends the discarding.
  AdapterSet adapters;
  Limits limits;
  Client c(kTable, adapters, limits);
  ScriptLink link;
  c.Attach(link, kT0);
  Bootstrap(link);
  link.Push(DeltaOf(1, 1));
  link.Push(msg::Through{1});
  c.Pump(kT0);
  c.InboundLost(kT0 + 1ms);
  link.Push(DeltaOf(2, 2));
  c.Pump(kT0 + 2ms);
  c.InboundLost(kT0 + 3ms);
  CHECK(link.Mismatches().size() == 2);
  CHECK(c.AwaitingRepairBaseline());
  const Record repaired = Counter(3, 3);
  link.Push(msg::BaselineBegin{2, 3, 1, kScopeAll, 1, 0, BaselineReason::kDigestMismatch});
  link.Push(msg::BaselineRows{2, 0, {repaired}});
  link.Push(msg::BaselineEnd{2, 3, BaselineDigestAdd(kBaselineDigestSeed, repaired)});
  c.Pump(kT0 + 4ms);
  CHECK(!c.AwaitingRepairBaseline());
  CHECK(RevisionOf(c) == 3);
}

BACKEND_TEST("S3-IL4", InboundLostDuringRunningBaselineRealServer) {
  // Sol code review r1 F1, with a real Server: the loss comes while S's full baseline runs (its
  // BaselineBegin read, its rest discarded), the head never moves after it and a manifest proof
  // at that head keeps S's silence rule quiet. Before the fix S counted the running baseline as
  // the answer to the Mismatch (its `at` and scopes cover the prefix), the client discarded its
  // rest and every manifest while waiting for a later baseline, and stayed in discard mode for
  // good. Now the Mismatch is forced: S begins a fresh full baseline after the running one.
  namespace tl = test::lanes;
  TimePoint now = tl::kStart;
  tl::Authorities authorities;
  tl::TestWorld world;
  Server server(tl::kLanesTable, authorities.set, Limits{});
  Client client(tl::kLanesTable, world.Adapters(), Limits{});
  auto [s_end, c_end] = MemoryLink::Pair(&now);
  LossyLink lossy(*c_end);
  // Enough session rows that one baseline spans many pumps at the pacer's 40 KiB/s (about 2 s).
  constexpr uint32_t kRows = 1000;
  for (uint32_t n = 1; n <= kRows; ++n) {
    CHECK(server.State().Put(tl::Key(test::kTracker, SessionObject(n)), tl::Val(n, 32), now) ==
          PutResult::kWritten);
  }
  server.AddPeer(1, PeerRole::kOwner, *s_end, now);
  client.Attach(lossy, now);
  std::map<BaselineReason, size_t> begun;
  size_t ended = 0;
  auto step = [&] {
    server.Pump(now);
    client.Pump(now);
    ServerEvent e;
    while (server.PollEvent(e)) {
      if (e.type == ServerEvent::Type::kBaselineBegun) ++begun[e.why];
      if (e.type == ServerEvent::Type::kBaselineEnded) ++ended;
    }
    ClientEvent ce;
    while (client.PollEvent(ce)) {
    }
    now += 50ms;
  };
  auto run = [&](Duration d) {
    const TimePoint until = now + d;
    while (now < until) step();
  };

  // Joined, and the head proven by a manifest: nothing repairs in a quiet session from here.
  run(15s);
  const uint64_t head = server.State().Head();
  CHECK(head == kRows);
  CHECK(begun[BaselineReason::kJoin] == 1);
  CHECK(client.State().Staging() == 0);
  CHECK(client.State().Completes(Scope::kSession, head));
  CHECK(server.Stats(1).manifest_ok == head);

  // A full repair baseline (a first InboundLost with nothing lost asks for one): its BaselineBegin
  // read, the client stages it and has left discard mode.
  client.InboundLost(now);
  for (int i = 0; i < 40 && (client.AwaitingRepairBaseline() || client.State().Staging() == 0);
       ++i) {
    step();
  }
  REQUIRE_OR_RETURN_VOID(!client.AwaitingRepairBaseline());
  CHECK(client.State().Staging() == kScopeSession);
  CHECK(begun[BaselineReason::kDigestMismatch] == 1);
  const size_t ended_before = ended;

  // The loss: the rest of that baseline (rows, end) is discarded below the client and the mark is
  // read. S reads the Mismatch while the baseline still runs.
  lossy.drop = true;
  client.InboundLost(now);
  CHECK(client.AwaitingRepairBaseline());
  step();
  CHECK(ended == ended_before);  // the baseline was still running when S read the Mismatch
  for (int i = 0; i < 200 && ended == ended_before; ++i) step();
  CHECK(ended == ended_before + 1);
  CHECK(lossy.dropped > 0);
  lossy.drop = false;

  // A fresh full baseline after it: the client takes it, leaves discard mode and holds S's rows.
  run(10s);
  CHECK(server.State().Head() == head);
  CHECK(begun[BaselineReason::kDigestMismatch] == 2);
  CHECK(!client.AwaitingRepairBaseline());
  CHECK(client.State().Staging() == 0);
  CHECK(client.State().Completes(Scope::kSession, head));
  const Record* row = client.State().Get(tl::Key(test::kTracker, SessionObject(kRows)));
  CHECK(row && row->value == tl::Val(kRows, 32));
}

BACKEND_TEST("S3-IL3", InboundLostBeforeHelloDoesNothing) {
  // Before StateHello nothing is read, so a loss mark there asks for nothing.
  AdapterSet adapters;
  Limits limits;
  Client c(kTable, adapters, limits);
  ScriptLink link;
  c.Attach(link, kT0);
  c.InboundLost(kT0);
  CHECK(!c.AwaitingRepairBaseline());
  CHECK(link.Mismatches().empty());
}

}  // namespace skylanders::backend
