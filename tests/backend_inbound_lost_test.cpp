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
#include <span>
#include <utility>
#include <variant>
#include <vector>

#include "backend/adapter.h"
#include "backend/client.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/world_state.h"
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
