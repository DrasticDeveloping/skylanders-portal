// The converge template on test.toggle (design section 7.3; test CT1 of section 10). The page
// docs/backend/adding-a-kind.md walks this file: the kind's table row (backend_test_kinds.h), its
// adapter, its authority and its converge block.

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <span>
#include <vector>

#include "backend/adapter.h"
#include "backend/request.h"
#include "backend_converge.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

using namespace skylanders::backend;
using namespace skylanders::backend::test;
using namespace std::chrono_literals;

namespace {

// test.toggle's adapter: the toggle is one byte, 0 or 1, in the entity's field. A value of any
// other length or byte is refused (kFailed), so a bad row shows as a failed apply, never as a
// silent write.
class FakeToggleAdapter : public FakeKindAdapter {
 public:
  using FakeKindAdapter::FakeKindAdapter;
  AdapterResult Apply(Handle h, uint16_t slot, std::span<const uint8_t> value) override {
    if (value.size() != 1 || value[0] > 1) return AdapterResult::kFailed;
    return FakeKindAdapter::Apply(h, slot, value);
  }
};

std::unique_ptr<KindAdapter> MakeFakeToggleAdapter(FakeWorld& world, KindId kind) {
  return std::make_unique<FakeToggleAdapter>(world, kind);
}

// test.toggle's authority: a commit flips the toggle to the requested state; server-run hands it to
// S's game thread. It reads only the request (Decide is pure, D7).
class ToggleAuthority : public FieldAuthority {
 public:
  using FieldAuthority::FieldAuthority;
  Verdict Decide(const Request& request, const DecideContext& context) override {
    if (request.payload.size() != 1 || request.payload[0] > 1) {
      Verdict refused;  // kRefuse
      refused.reason = Reason::kModel;
      return refused;
    }
    return FieldAuthority::Decide(request, context);
  }
};

std::unique_ptr<Authority> MakeToggleAuthority(KindId kind) {
  return std::make_unique<ToggleAuthority>(kind);
}

}  // namespace

BACKEND_TEST("CT1", ConvergeTemplateToggle) {
  ConvergeKind toggle{
      .spec = kTestToggle, .adapter = MakeFakeToggleAdapter, .authority = MakeToggleAuthority,
      .script = [](ConvergeScript& s) { s.Spawn(20); s.WriteEvery(50ms, 3s); s.Request(Model::kCommit, 10); },
  };
  toggle.value = [](uint32_t n) { return std::vector<uint8_t>{uint8_t(n & 1)}; };
  const std::vector<ConvergeRun> runs =
      RunConverge(toggle, {Profile::kPlain, Profile::kLoss5Rtt300, Profile::kLoss30Rtt300,
                           Profile::kKilledDelta, Profile::kMidBaselineDisconnect,
                           Profile::kThrottledPeer, Profile::kLateJoiner,
                           Profile::kTeardownDuringApply, Profile::kStreamOut},
                  /*seeds=*/kDefaultSeeds);  // 8; 32 with --long
  CHECK(runs.size() == kEveryProfile.size() * size_t(Seeds()));
  // Every profile graded something, and the faulty ones did what they say.
  for (const ConvergeRun& run : runs) {
    CHECK(run.rows > 0);
    if (run.profile == Profile::kKilledDelta || run.profile == Profile::kThrottledPeer ||
        run.profile == Profile::kMidBaselineDisconnect) {
      CHECK(run.faults > 0);
    }
  }
}
