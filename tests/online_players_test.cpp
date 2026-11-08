// Stage 4's pure pieces on the session side (docs/online-4.md sections 4.3 to 4.6): the puppet
// action relay's per-key state machine, S's pose rate check, the roster and the two-player trigger
// replacement, the body slots and the stand-in source order.

#include <chrono>
#include <cstdint>
#include <set>
#include <vector>

#include "online/action_relay.h"
#include "online/body_slots.h"
#include "online/pose_rate.h"
#include "online/roster.h"
#include "test_util.h"

#ifndef REQUIRE_OR_RETURN_VOID
#define REQUIRE_OR_RETURN_VOID(cond)   do {                                   if (!(cond)) {                         CHECK(cond);                         return;                            }                                  } while (0)
#endif

using namespace skylanders::online;
using namespace std::chrono_literals;

namespace {

const TimePoint kT0 = TimePoint{} + 10h;
using Relay = PuppetActionRelay;
using Ev = Relay::Event;

std::vector<Ev> Drain(Relay& r) {
  std::vector<Ev> out;
  Ev e;
  while (r.PollEvent(e)) out.push_back(e);
  return out;
}
int Count(const std::vector<Ev>& v, Ev::Type t, uint32_t seq) {
  int n = 0;
  for (const Ev& e : v) n += (e.type == t && e.press_sequence == seq) ? 1 : 0;
  return n;
}
bool Has(const std::vector<Ev>& v, Ev::Type t, uint32_t seq, Relay::ReleaseWhy why) {
  for (const Ev& e : v) {
    if (e.type == t && e.press_sequence == seq && e.why == why) return true;
  }
  return false;
}

void RelayCases() {
  // A press then its release: one start, the slot held, then clear.
  {
    Relay r;
    r.SetEpoch(1, 3);
    CHECK(r.OnAction(3, 1, 0, false, 0ms, kT0) == Relay::Drop::kNone);
    CHECK(r.Think(kT0) == 0x01);
    CHECK(r.Think(kT0 + 16ms) == 0x01);
    CHECK(r.OnAction(3, 1, 0, true, 0ms, kT0 + 300ms) == Relay::Drop::kNone);
    CHECK(r.Think(kT0 + 316ms) == 0x00);
    const auto ev = Drain(r);
    CHECK(Count(ev, Ev::Type::kStarted, 1) == 1);
    CHECK(Has(ev, Ev::Type::kReleased, 1, Relay::ReleaseWhy::kRelease));
  }
  // A duplicate press and a duplicate release are each dropped alone.
  {
    Relay r;
    r.SetEpoch(1, 3);
    r.OnAction(3, 1, 0, false, 0ms, kT0);
    CHECK(r.OnAction(3, 1, 0, false, 0ms, kT0) == Relay::Drop::kDuplicatePress);
    r.Think(kT0);
    CHECK(r.OnAction(3, 1, 0, true, 0ms, kT0) == Relay::Drop::kNone);
    CHECK(r.OnAction(3, 1, 0, true, 0ms, kT0) == Relay::Drop::kDuplicateRelease);
    CHECK(r.OnAction(3, 1, 0, false, 0ms, kT0) == Relay::Drop::kDuplicatePress);  // after its release
    CHECK(r.HeldSlots() == 0);
    CHECK(Count(Drain(r), Ev::Type::kStarted, 1) == 1);
  }
  // A release after a newer press of another slot ends only its own press.
  {
    Relay r;
    r.SetEpoch(1, 3);
    r.OnAction(3, 1, 0, false, 0ms, kT0);  // slot A
    r.Think(kT0);
    r.OnAction(3, 2, 1, false, 0ms, kT0);  // slot B, newer
    r.Think(kT0);
    CHECK(r.HeldSlots() == 0x03);
    r.OnAction(3, 1, 0, true, 0ms, kT0);  // A's release after B's press
    CHECK(r.Think(kT0) == 0x02);
    r.OnAction(3, 2, 1, true, 0ms, kT0);
    CHECK(r.Think(kT0) == 0x00);
  }
  // Same slot: a late release of the older press keeps the newer press's hold.
  {
    Relay r;
    r.SetEpoch(1, 3);
    r.OnAction(3, 5, 0, false, 0ms, kT0);
    r.Think(kT0);
    r.OnAction(3, 6, 0, false, 0ms, kT0);
    r.Think(kT0);
    r.OnAction(3, 5, 0, true, 0ms, kT0);  // the older press's release, late
    CHECK(r.Think(kT0) == 0x01);           // 6 still holds slot 0
    r.OnAction(3, 6, 0, true, 0ms, kT0);
    CHECK(r.Think(kT0) == 0x00);
    const auto ev = Drain(r);
    CHECK(Count(ev, Ev::Type::kStarted, 5) == 1 && Count(ev, Ev::Type::kStarted, 6) == 1);
    CHECK(Count(ev, Ev::Type::kReleased, 5) == 1 && Count(ev, Ev::Type::kReleased, 6) == 1);
  }
  // A release before the press is taken: in for one think, then ended (release-after).
  {
    Relay r;
    r.SetEpoch(1, 3);
    r.OnAction(3, 1, 2, false, 0ms, kT0);
    r.OnAction(3, 1, 2, true, 0ms, kT0);
    CHECK(r.Think(kT0) == 0x04);
    CHECK(r.Think(kT0 + 16ms) == 0x00);
    const auto ev = Drain(r);
    CHECK(Count(ev, Ev::Type::kStarted, 1) == 1);
    CHECK(Has(ev, Ev::Type::kReleased, 1, Relay::ReleaseWhy::kReleaseAfter));
  }
  // A release of a key never pressed is recorded: its later press is a duplicate and never starts.
  {
    Relay r;
    r.SetEpoch(1, 3);
    CHECK(r.OnAction(3, 9, 0, true, 0ms, kT0) == Relay::Drop::kNone);
    CHECK(r.OnAction(3, 9, 0, false, 0ms, kT0) == Relay::Drop::kDuplicatePress);
    CHECK(r.Think(kT0) == 0x00);
  }
  // A stale press never starts and consumes its later release silently; a second release is a
  // duplicate. A release is never stale.
  {
    Relay r;
    r.SetEpoch(1, 3);
    CHECK(r.OnAction(3, 1, 0, false, 501ms, kT0) == Relay::Drop::kStale);
    CHECK(r.OnAction(3, 1, 0, true, 9000ms, kT0) == Relay::Drop::kNone);
    CHECK(r.OnAction(3, 1, 0, true, 0ms, kT0) == Relay::Drop::kDuplicateRelease);
    CHECK(r.Think(kT0) == 0x00);
    CHECK(Count(Drain(r), Ev::Type::kStarted, 1) == 0);
    CHECK(r.OnAction(3, 2, 0, false, 500ms, kT0) == Relay::Drop::kNone);  // at the bound: pressed
  }
  // Another generation is dropped, press or release.
  {
    Relay r;
    r.SetEpoch(1, 3);
    CHECK(r.OnAction(2, 1, 0, false, 0ms, kT0) == Relay::Drop::kGeneration);
    CHECK(r.OnAction(4, 1, 0, true, 0ms, kT0) == Relay::Drop::kGeneration);
    CHECK(r.Keys() == 0);
  }
  // The 5 s hold timeout.
  {
    Relay r;
    r.SetEpoch(1, 3);
    r.OnAction(3, 1, 0, false, 0ms, kT0);
    CHECK(r.Think(kT0) == 0x01);
    CHECK(r.Think(kT0 + 5000ms) == 0x01);
    CHECK(r.Think(kT0 + 5001ms) == 0x00);
    CHECK(Has(Drain(r), Ev::Type::kReleased, 1, Relay::ReleaseWhy::kTimeout));
  }
  // Teardown at despawn or a body change: every key released, the table kept (a late release is a
  // duplicate); a new generation or peer epoch drops the table.
  {
    Relay r;
    r.SetEpoch(1, 3);
    r.OnAction(3, 1, 0, false, 0ms, kT0);
    r.OnAction(3, 2, 1, false, 0ms, kT0);
    r.Think(kT0);
    r.Teardown();
    CHECK(r.HeldSlots() == 0);
    auto ev = Drain(r);
    CHECK(Has(ev, Ev::Type::kReleased, 1, Relay::ReleaseWhy::kTeardown));
    CHECK(Has(ev, Ev::Type::kReleased, 2, Relay::ReleaseWhy::kTeardown));
    CHECK(r.OnAction(3, 1, 0, true, 0ms, kT0) == Relay::Drop::kDuplicateRelease);
    r.OnAction(3, 3, 0, false, 0ms, kT0);
    r.Think(kT0);
    r.SetEpoch(1, 4);  // generation change
    CHECK(r.Keys() == 0 && r.HeldSlots() == 0);
    CHECK(Has(Drain(r), Ev::Type::kReleased, 3, Relay::ReleaseWhy::kTeardown));
    r.OnAction(4, 1, 0, false, 0ms, kT0);
    r.Think(kT0);
    r.SetEpoch(2, 4);  // peer epoch change
    CHECK(r.Keys() == 0 && r.HeldSlots() == 0);
  }
  // The 32-key bound: released keys go first, oldest first; with none released the oldest pressed
  // key is released and evicted.
  {
    Relay r;
    r.SetEpoch(1, 3);
    for (uint32_t s = 1; s <= 32; ++s) r.OnAction(3, s, 0, true, 0ms, kT0);  // 32 released keys
    CHECK(r.Keys() == 32);
    r.OnAction(3, 100, 1, false, 0ms, kT0);
    CHECK(r.Keys() == 32);
    CHECK(r.OnAction(3, 1, 0, false, 0ms, kT0) == Relay::Drop::kNone);  // key 1 was evicted
    Relay q;
    q.SetEpoch(1, 3);
    for (uint32_t s = 1; s <= 32; ++s) q.OnAction(3, s, uint8_t(s % 3), false, 0ms, kT0);
    q.Think(kT0);
    Drain(q);
    q.OnAction(3, 33, 0, false, 0ms, kT0);
    CHECK(q.Keys() == 32);
    CHECK(Has(Drain(q), Ev::Type::kReleased, 1, Relay::ReleaseWhy::kTeardown));
  }
}

void PoseRateCases() {
  PoseRateCheck c(10.0f);  // 10 units per second
  float v = 0;
  CHECK(c.Check(2, 1, 1, {0, 0, 0}, 0, kT0) == PoseRateCheck::Verdict::kFirst);
  CHECK(c.Check(2, 1, 2, {0.4f, 0, 0}, 0, kT0 + 50ms, &v) == PoseRateCheck::Verdict::kAccepted);
  CHECK(v > 7.9f && v < 8.1f);
  // Too fast: dropped, the last accepted pose stays.
  CHECK(c.Check(2, 1, 3, {5, 0, 0}, 0, kT0 + 100ms, &v) == PoseRateCheck::Verdict::kTooFast);
  std::array<float, 3> last{};
  CHECK(c.Last(2, &last) && last[0] == 0.4f);
  // A burst after a stall: two poses arrive 1 ms apart but were made 50 ms apart (the sequence).
  CHECK(c.Check(2, 1, 4, {0.8f, 0, 0}, 0, kT0 + 300ms) == PoseRateCheck::Verdict::kAccepted);
  CHECK(c.Check(2, 1, 5, {1.2f, 0, 0}, 0, kT0 + 301ms) == PoseRateCheck::Verdict::kAccepted);
  // The teleport counter moved: any jump passes.
  CHECK(c.Check(2, 1, 6, {500, 0, 0}, 1, kT0 + 350ms) == PoseRateCheck::Verdict::kTeleport);
  // A new generation starts again; another peer is its own.
  CHECK(c.Check(2, 2, 1, {-900, 0, 0}, 1, kT0 + 400ms) == PoseRateCheck::Verdict::kFirst);
  CHECK(c.Check(3, 2, 1, {7, 7, 7}, 0, kT0 + 400ms) == PoseRateCheck::Verdict::kFirst);
  c.Forget(2);
  CHECK(!c.Last(2, nullptr));
}

void RosterCases() {
  // In and out by state; the puppet must be live; at most four; the alive answer stays min(2, n).
  {
    Roster r;
    r.Set(1, PlayerState::kPlaying, true);
    r.Set(2, PlayerState::kPlaying, false);
    CHECK(r.Members() == std::vector<uint16_t>{1});
    r.Set(2, PlayerState::kPlaying, true);
    r.Set(3, PlayerState::kSwapping, true);
    r.Set(4, PlayerState::kKo, true);
    CHECK((r.Members() == std::vector<uint16_t>{1, 2}));
    r.Set(3, PlayerState::kPlaying, true);
    r.Set(4, PlayerState::kPlaying, true);
    CHECK(r.Count() == 4 && r.HeroesAliveAnswer() == 2);
    r.SetSynthetic(100, true);
    CHECK(r.Count() == 4 && !r.IsMember(100));  // the cap
    r.Set(4, PlayerState::kLeft, false);
    CHECK(r.IsMember(100) && r.Count() == 4);
    r.Remove(1);
    r.Remove(2);
    r.Remove(3);
    r.SetSynthetic(100, false);
    CHECK(r.Count() == 0 && r.HeroesAliveAnswer() == 0);
  }
  // The trigger: four members, fires once, only with all inside, never at two.
  {
    TwoPlayerTrigger t;
    const std::vector<uint16_t> four{1, 2, 3, 4};
    size_t in = 0;
    CHECK(t.Frame(four, {1, 2, 3, 4}) == TwoPlayerTrigger::Result::kIdle);  // not armed
    t.Arm();
    CHECK(t.Frame(four, {1, 2}, &in) == TwoPlayerTrigger::Result::kWaiting && in == 2);
    CHECK(t.Frame(four, {1, 2, 3}) == TwoPlayerTrigger::Result::kWaiting);
    CHECK(t.Frame(four, {1, 2, 3, 4}, &in) == TwoPlayerTrigger::Result::kFired && in == 4);
    CHECK(t.Frame(four, {1, 2, 3, 4}) == TwoPlayerTrigger::Result::kIdle);  // once per occurrence
    t.Arm();  // the native re-arm
    CHECK(t.Frame(four, {1, 2, 3, 4}) == TwoPlayerTrigger::Result::kFired);
  }
  // Zero members never fire; churn: a member leaving the roster while the rest are inside fires.
  {
    TwoPlayerTrigger t;
    t.Arm();
    CHECK(t.Frame({}, {}) == TwoPlayerTrigger::Result::kIdle);
    CHECK(t.Armed());
    CHECK(t.Frame({1, 2, 3}, {1, 2}) == TwoPlayerTrigger::Result::kWaiting);
    CHECK(t.Frame({1, 2}, {1, 2}) == TwoPlayerTrigger::Result::kFired);  // 3 went KO
    // An inside id that is not a member (a puppet out of the roster) never counts.
    t.Arm();
    CHECK(t.Frame({1, 2}, {1, 3}) == TwoPlayerTrigger::Result::kWaiting);
    // A generation change clears the occurrence.
    t.OnGeneration();
    CHECK(t.Frame({1, 2}, {1, 2}) == TwoPlayerTrigger::Result::kIdle);
  }
}

void BodySlotCases() {
  constexpr CharacterId kA = 0xA, kB = 0xB, kC = 0xC, kD = 0xD;
  // Slot counts and load before unload: a swap acquires the new body before the old is released;
  // the old slot is released only at a count of 0.
  {
    BodySlots s(3, StandInPolicy{});
    CHECK(s.AcquireReal(2, kA) == std::optional<int>(0));
    CHECK(s.AcquireReal(3, kA) == std::optional<int>(0));  // shared package
    CHECK(s.RealUsers(0) == 2);
    s.MarkResident(0);
    CHECK(s.AcquireReal(2, kB) == std::optional<int>(1));  // peer 2 swaps to B
    CHECK(s.RealUsers(0) == 1 && s.RealUsers(1) == 1);
    CHECK(!s.ReleaseSlot(0, nullptr));  // peer 3 still uses A
    s.ReleaseReal(3);
    CHECK(s.ReleaseSlot(0, nullptr));
    CHECK(s.Package(0) == kNoCharacter && s.SlotOf(kA) == -1);
  }
  // No slot with more distinct characters than slots; a stand-in from another resident package,
  // an element match first; an eviction never chooses the evicted package; stand-ins never pin.
  {
    BodySlots s(2, StandInPolicy{});
    s.AcquireReal(2, kA);
    s.MarkResident(0);
    s.SetElement(0, 1);
    s.AcquireReal(3, kB);
    s.MarkResident(1);
    s.SetElement(1, 5);
    CHECK(!s.AcquireReal(4, kC).has_value());
    StandIn in = s.ChooseStandIn(4, 5);
    CHECK(in.source == StandInSource::kRemotePackage && in.slot == 1);  // the element match
    // Peer 3 swaps away from B: B's count is 0 and B is released at once although peer 4's
    // stand-in rests on it; the stand-in moves first, never to B.
    s.ReleaseReal(3);
    CHECK(s.RealUsers(1) == 0);
    std::vector<StandInMove> moves;
    CHECK(s.ReleaseSlot(1, &moves));
    REQUIRE_OR_RETURN_VOID(moves.size() == 1);
    CHECK(moves[0].peer == 4 && moves[0].to.source == StandInSource::kRemotePackage && moves[0].to.slot == 0);
    CHECK(s.StandInOf(4)->slot == 0);
    // The freed slot takes the fifth character; peer 4 upgrades.
    CHECK(s.AcquireReal(4, kC) == std::optional<int>(1));
    s.DropStandIn(4);
    CHECK(!s.StandInOf(4).has_value());
    // A slot with a pending release, or one not resident yet, is no source.
    CHECK(s.ChooseStandIn(5, 0, 0).source == StandInSource::kNone);  // 0 excluded, 1 loading
  }
  // The order past source 1: local package (E2e), pinned fallback (E2d/E2e), hidden (CQ1), none.
  {
    CHECK(BodySlots(0, {true, true, true}).ChooseStandIn(2, 0).source == StandInSource::kLocalPackage);
    CHECK(BodySlots(0, {false, true, true}).ChooseStandIn(2, 0).source == StandInSource::kPinnedFallback);
    CHECK(BodySlots(0, {false, false, true}).ChooseStandIn(2, 0).source == StandInSource::kHidden);
    CHECK(BodySlots(0, {false, false, false}).ChooseStandIn(2, 0).source == StandInSource::kNone);
  }
  // Under B (one slot): the eviction's stand-ins can never rest on the slot being evicted.
  {
    BodySlots s(1, StandInPolicy{false, true, false});
    s.AcquireReal(2, kA);
    s.MarkResident(0);
    s.ChooseStandIn(3, 0);
    CHECK(s.StandInOf(3)->slot == 0);
    s.ReleaseReal(2);
    std::vector<StandInMove> moves;
    CHECK(s.ReleaseSlot(0, &moves));
    REQUIRE_OR_RETURN_VOID(moves.size() == 1);
    CHECK(moves[0].to.source == StandInSource::kPinnedFallback);
    CHECK(s.AcquireReal(3, kD) == std::optional<int>(0));
  }
}

}  // namespace

int main() {
  RelayCases();
  PoseRateCases();
  RosterCases();
  BodySlotCases();
  return Finish("online players");
}
