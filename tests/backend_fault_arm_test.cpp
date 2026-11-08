// The fault injector's stage 3 additions (future/online-3.md sections 3.7 and 5.4): rules armed at
// run time (nth counted from arming), the `newer` matcher (only a revision above the one settled at
// arming; repeated handouts of an older revision never consume the match), and `apply:hold`
// (kFailed without an adapter call for its time, then the adapter; cancelled holds never call it
// and give their release line). Parse and round trip too.

#include <chrono>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "backend/adapter.h"
#include "backend/entity_id.h"
#include "backend/fault_injector.h"
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

const TimePoint kT0 = TimePoint{} + std::chrono::hours(2);
const auto& kTable = test::kTestTableFull;

// Counts its calls; answers kOk.
class CountingAdapter final : public KindAdapter {
 public:
  AdapterResult Read(Handle, uint16_t, std::vector<uint8_t>&) override { return AdapterResult::kOk; }
  AdapterResult Apply(Handle, uint16_t, std::span<const uint8_t>) override {
    ++applies;
    return AdapterResult::kOk;
  }
  int applies = 0;
};

EntityId Gate() { return Authored(1, 2614, HashName("TutorialBattleGate_01"), HashName("CEntity")); }
EntityId Other() { return Authored(1, 2607, HashName("TutorialLockedGate"), HashName("CEntity")); }

struct Fixture {
  explicit Fixture(std::string_view rules = "")
      : injector(*ParseFaultRules(rules), 7, kTable, kT0) {
    identity.OnEpoch(1);
    identity.OnSpawn(Gate(), 10, kT0);
    identity.OnSpawn(Other(), 11, kT0);
    wrapped = injector.WrapAdapter(inner, 1, test::kGate, &identity);
    injector.SetApplyRevisionSource([this]() -> std::optional<uint32_t> { return revision; });
  }
  // One handout of `rev` for the gate (handle 10) at `t`.
  AdapterResult Hand(uint32_t rev, TimePoint t, Handle h = 10) {
    injector.Advance(t);
    revision = rev;
    const std::vector<uint8_t> v = {uint8_t(rev)};
    const AdapterResult r = wrapped->Apply(h, 0, v);
    revision.reset();
    return r;
  }
  std::vector<FaultEvent> Events() {
    std::vector<FaultEvent> out;
    FaultEvent e;
    while (injector.PollEvent(e)) out.push_back(e);
    return out;
  }

  IdentityMap identity;
  CountingAdapter inner;
  FaultInjector injector;
  std::unique_ptr<KindAdapter> wrapped;
  std::optional<uint32_t> revision;
};

std::string GateText() { return ToText(Gate()); }

FaultRule Rule(const std::string& text) {
  auto rules = ParseFaultRules(text);
  if (!rules || rules->size() != 1) return FaultRule{};
  return rules->front();
}

}  // namespace

BACKEND_TEST("S3-FA1", FaultArmParseAndRoundTrip) {
  const std::string text = "apply:hold kind=test.gate key=" + GateText() + " newer nth=1 ms=8000";
  FaultParseError err;
  auto rules = ParseFaultRules(text, &err);
  REQUIRE_OR_RETURN_VOID(rules && rules->size() == 1);
  const FaultRule& r = rules->front();
  CHECK(r.layer == FaultLayer::kApply && r.action == FaultAction::kHold);
  CHECK(r.newer && r.nth == 1 && r.amount == 8000);
  CHECK(FormatFaultRule(r) == text);
  auto again = ParseFaultRules(FormatFaultRules(*rules));
  CHECK(again && *again == *rules);
  // `newer` anywhere among the words; canonical after slot=.
  auto moved = ParseFaultRules("apply:fail newer kind=test.gate key=" + GateText() + " slot=2");
  REQUIRE_OR_RETURN_VOID(moved && moved->size() == 1);
  CHECK(FormatFaultRule(moved->front()) ==
        "apply:fail kind=test.gate key=" + GateText() + " slot=2 newer");
  // Refusals: hold without an amount; newer on another layer; newer without kind and key.
  CHECK(!ParseFaultRules("apply:hold kind=test.gate", &err));
  CHECK(!ParseFaultRules("message:kill type=delta newer", &err));
  CHECK(err.text.find("newer") != std::string::npos);
  CHECK(!ParseFaultRules("apply:fail newer kind=test.gate", &err));
  CHECK(!ParseFaultRules("apply:fail newer key=" + GateText(), &err));
  CHECK(FaultArmedLine(2, r) == "Online: backend fault 2 armed " + text);
}

BACKEND_TEST("S3-FA2", FaultArmCountsFromArming) {
  // A fail rule with nth=2 armed after three handouts fires at the second handout after arming,
  // not at the second of the run; its number follows the rules given at start.
  Fixture f("apply:fail kind=test.toggle");
  for (int i = 0; i < 3; ++i) CHECK(f.Hand(1, kT0 + std::chrono::milliseconds(i)) == AdapterResult::kOk);
  CHECK(f.inner.applies == 3);
  auto n = f.injector.Arm(Rule("apply:fail kind=test.gate key=" + GateText() + " nth=2"), std::nullopt);
  REQUIRE_OR_RETURN_VOID(n.has_value());
  CHECK(*n == 2);
  CHECK(f.injector.Matched(1) == 0);
  CHECK(f.Hand(1, kT0 + 10ms) == AdapterResult::kOk);
  CHECK(f.Hand(1, kT0 + 11ms) == AdapterResult::kFailed);
  CHECK(f.Hand(1, kT0 + 12ms) == AdapterResult::kOk);
  CHECK(f.injector.Fired(1) == 1);
  const auto events = f.Events();
  REQUIRE_OR_RETURN_VOID(events.size() == 1);
  CHECK(events[0].rule == 2);
  // A `newer` rule without kind and key cannot be armed.
  FaultRule bad;
  bad.layer = FaultLayer::kApply;
  bad.action = FaultAction::kFail;
  bad.newer = true;
  CHECK(!f.injector.Arm(bad, 3).has_value());
}

BACKEND_TEST("S3-FA3", FaultNewerSkipsOlderRevisions) {
  // Armed with revision 4 settled: handouts of 3 and 4 (a baseline's apply, repeated kFailed
  // handouts of an older revision) never consume the nth=1 match; revision 5 does.
  Fixture f;
  auto n = f.injector.Arm(Rule("apply:fail kind=test.gate key=" + GateText() + " newer nth=1"), 4);
  REQUIRE_OR_RETURN_VOID(n.has_value());
  CHECK(f.Hand(3, kT0 + 1ms) == AdapterResult::kOk);
  CHECK(f.Hand(4, kT0 + 2ms) == AdapterResult::kOk);
  CHECK(f.Hand(4, kT0 + 3ms) == AdapterResult::kOk);
  CHECK(f.injector.Matched(0) == 0);
  CHECK(f.Hand(5, kT0 + 4ms) == AdapterResult::kFailed);
  CHECK(f.injector.Fired(0) == 1);
  CHECK(f.Hand(6, kT0 + 5ms) == AdapterResult::kOk);  // nth=1: once
  // Another entity never matches; nor does a handout with no revision source answer.
  CHECK(f.Hand(9, kT0 + 6ms, 11) == AdapterResult::kOk);
  Fixture g;
  g.injector.SetApplyRevisionSource({});
  g.injector.Arm(Rule("apply:fail kind=test.gate key=" + GateText() + " newer"), std::nullopt);
  CHECK(g.Hand(9, kT0 + 1ms) == AdapterResult::kOk);
  CHECK(g.injector.Matched(0) == 0);
  // Nothing settled at arming: any revision is newer.
  Fixture h;
  h.injector.Arm(Rule("apply:fail kind=test.gate key=" + GateText() + " newer nth=1"), std::nullopt);
  CHECK(h.Hand(1, kT0 + 1ms) == AdapterResult::kFailed);
}

BACKEND_TEST("S3-FA4", FaultHoldThenApply) {
  // apply:hold ms=500: the first matching handout starts the hold; handouts during it answer
  // kFailed without an adapter call and without counting as matches; after it the adapter runs.
  Fixture f("apply:hold kind=test.gate key=" + GateText() + " ms=500");
  CHECK(f.Hand(2, kT0 + 0ms) == AdapterResult::kFailed);
  CHECK(f.injector.ActiveHolds() == 1);
  CHECK(f.Hand(2, kT0 + 100ms) == AdapterResult::kFailed);
  CHECK(f.Hand(3, kT0 + 499ms) == AdapterResult::kFailed);  // a newer value waits too
  CHECK(f.inner.applies == 0);
  CHECK(f.injector.Matched(0) == 1);
  CHECK(f.Hand(3, kT0 + 500ms) == AdapterResult::kOk);
  CHECK(f.inner.applies == 1);
  CHECK(f.injector.ActiveHolds() == 0);
  const auto events = f.Events();
  REQUIRE_OR_RETURN_VOID(events.size() == 1);
  CHECK(events[0].action == FaultAction::kHold);
  CHECK(FaultLine(events[0]).find("apply:hold peer 1 test.gate") != std::string::npos);
  CHECK(FaultLine(events[0]).find("500 ms") != std::string::npos);
}

BACKEND_TEST("S3-FA5", FaultHoldCancelledNeverApplies) {
  // EpochBegin cancels the hold: no adapter call ever follows for it; the release line comes when
  // its time is up. Detach releases at once.
  Fixture f;
  f.injector.Arm(Rule("apply:hold kind=test.gate key=" + GateText() + " newer nth=1 ms=8000"), 1);
  CHECK(f.Hand(2, kT0 + 0ms) == AdapterResult::kFailed);
  CHECK(f.injector.ActiveHolds() == 1);
  f.Events();
  f.injector.CancelHolds("epoch", false);
  CHECK(f.injector.ActiveHolds() == 0);
  CHECK(f.injector.CancelledHolds() == 1);
  f.injector.Advance(kT0 + 7999ms);
  CHECK(f.Events().empty());
  f.injector.Advance(kT0 + 8000ms);
  const auto events = f.Events();
  REQUIRE_OR_RETURN_VOID(events.size() == 1);
  CHECK(FaultLine(events[0]).find("released after cancel (no apply)") != std::string::npos);
  CHECK(FaultLine(events[0]).find("(epoch)") != std::string::npos);
  CHECK(f.inner.applies == 0);
  CHECK(f.injector.CancelledHolds() == 0);
  // A later handout of the key (a new generation's row) is a fresh call: the nth=1 rule is spent.
  CHECK(f.Hand(3, kT0 + 9000ms) == AdapterResult::kOk);
  CHECK(f.inner.applies == 1);

  Fixture d;
  d.injector.Arm(Rule("apply:hold kind=test.gate key=" + GateText() + " newer nth=1 ms=600000"), 1);
  CHECK(d.Hand(2, kT0) == AdapterResult::kFailed);
  d.Events();
  d.injector.CancelHolds("detach", true);
  const auto at_once = d.Events();
  REQUIRE_OR_RETURN_VOID(at_once.size() == 1);
  CHECK(FaultLine(at_once[0]).find("(detach)") != std::string::npos);
  CHECK(d.inner.applies == 0);
}

}  // namespace skylanders::backend
