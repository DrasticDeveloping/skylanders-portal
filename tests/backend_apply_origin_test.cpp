// A queued row's origin (stage 6, Sol F3; world_state.h's ApplyOrigin, Client::ApplyingOrigin): kept
// per key and revision from receipt or installation, not inferred from what the replica looks like
// around a pump. A scripted link feeds the client exactly what S would send, and an adapter records
// the origin of every apply it gets:
//   - a repair whose BaselineBegin, rows and BaselineEnd come in one pump (the level scope already
//     subscribed, nothing staging, no InboundLost) is kBaseline, also for a row whose entity comes
//     a pump later;
//   - in one receive batch, a delta after the BaselineEnd is kDelta (a new key, and a newer revision
//     of a key the baseline carried), and so is a delta read while the scope staged;
//   - the origin survives a kFailed answer and its retry; the same revision re-installed by a later
//     baseline keeps the origin it was first queued with, a new revision brings its own;
//   - the first origin (Client::ApplyingFirstOrigin) is the one the key entered the queue with: a
//     baseline's row that a delta revision replaces before its first successful apply is kDelta
//     per revision and still kBaseline first, also when the delta was read while the scope staged;
//     a settled key starts over.

#include <chrono>
#include <cstdint>
#include <deque>
#include <map>
#include <span>
#include <string>
#include <utility>
#include <vector>

#include "backend/adapter.h"
#include "backend/client.h"
#include "backend/entity_id.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/world_state.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;

const TimePoint kT0 = TimePoint{} + std::chrono::hours(1);
const auto& kTable = test::kTestTableFull;

// S's side of one client, by hand: frames queued for the client; what the client sends is dropped.
class OriginLink final : public PeerLink {
 public:
  LinkSend Send(netplay::Lane, std::span<const uint8_t>) override { return LinkSend::kSent; }
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

  std::deque<std::pair<netplay::Lane, std::vector<uint8_t>>> inbox;
};

// test.prop: level scope, unfenced, applied. Generation 1 throughout.
EntityId PropId(uint32_t n) {
  return Authored(1, n, HashName("prop" + std::to_string(n)), HashName("test"));
}
RecordKey PropKey(uint32_t n) { return RecordKey{test::kProp, PropId(n), 0}; }
Handle PropHandle(uint32_t n) { return 0x1000 + n; }

Record Prop(uint32_t n, uint32_t revision, uint64_t lsn) {
  Record r;
  r.key = PropKey(n);
  r.revision = revision;
  r.lsn = lsn;
  r.epoch = 1;
  r.value = {uint8_t(revision)};
  return r;
}

msg::Delta DeltaOf(const Record& r) { return msg::Delta{r, r.lsn}; }

// A baseline of every scope carrying `rows`, with its digest.
void PushBaseline(OriginLink& link, uint32_t id, uint64_t at, const std::vector<Record>& rows,
                  bool begin = true, bool end = true) {
  if (begin) {
    link.Push(msg::BaselineBegin{id, at, 1, kScopeAll, uint32_t(rows.size()), 0,
                                 BaselineReason::kManifestMismatch});
  }
  if (!rows.empty()) link.Push(msg::BaselineRows{id, 0, rows});
  if (end) {
    uint64_t digest = kBaselineDigestSeed;
    for (const Record& r : rows) digest = BaselineDigestAdd(digest, r);
    link.Push(msg::BaselineEnd{id, at, digest});
  }
}

// Records the row and origin of every Apply, through the Client's Applying/ApplyingOrigin.
class OriginAdapter final : public KindAdapter {
 public:
  struct Call {
    RecordKey key;
    uint32_t revision = 0;
    ApplyOrigin origin = ApplyOrigin::kNone;
    ApplyOrigin first = ApplyOrigin::kNone;  // ApplyingFirstOrigin()
  };

  explicit OriginAdapter(const Client& client) : client_(client) {}

  AdapterResult Read(Handle, uint16_t, std::vector<uint8_t>&) override {
    return AdapterResult::kUnsupported;
  }
  AdapterResult Apply(Handle, uint16_t, std::span<const uint8_t>) override {
    const Record* r = client_.Applying();
    CHECK(r != nullptr);
    if (!r) return AdapterResult::kOk;
    calls.push_back(
        Call{r->key, r->revision, client_.ApplyingOrigin(), client_.ApplyingFirstOrigin()});
    auto f = fail.find(r->key);
    if (f != fail.end() && f->second > 0) {
      --f->second;
      return AdapterResult::kFailed;
    }
    return AdapterResult::kOk;
  }

  // The calls for `key`, in order.
  std::vector<Call> For(const RecordKey& key) const {
    std::vector<Call> out;
    for (const Call& c : calls) {
      if (c.key == key) out.push_back(c);
    }
    return out;
  }

  std::vector<Call> calls;
  std::map<RecordKey, int> fail;  // answers kFailed this many more times

 private:
  const Client& client_;
};

// A client after StateHello and an empty join baseline of every scope (generation 1, subscribed,
// nothing staging), with the prop adapter registered.
struct Rig {
  AdapterSet adapters;
  Limits limits;
  Client client{kTable, adapters, limits};
  OriginLink link;
  OriginAdapter adapter{client};

  Rig() {
    adapters.Register(test::kProp, adapter);
    adapters.Identity().OnEpoch(1);
    client.Attach(link, kT0);
    link.Push(msg::StateHello{TableDigest(kTable), 1, 0, 1, 1});
    link.Push(msg::BaselineBegin{1, 0, 1, kScopeAll, 0, 0, BaselineReason::kJoin});
    link.Push(msg::BaselineEnd{1, 0, kBaselineDigestSeed});
    client.Pump(kT0);
  }
  void Spawn(uint32_t n) { adapters.Identity().OnSpawn(PropId(n), PropHandle(n), kT0); }
};

}  // namespace

BACKEND_TEST("S6-AO1", RepairInOnePumpIsBaseline) {
  Rig rig;
  Client& c = rig.client;
  CHECK((c.State().Subscribed() & kScopeLevel) != 0);
  CHECK(c.State().Staging() == 0);
  CHECK(!c.AwaitingRepairBaseline());
  rig.Spawn(1);  // prop 2's entity is not here yet

  // A manifest repair: BaselineBegin, its rows and BaselineEnd, all read in one pump.
  PushBaseline(rig.link, 2, 5, {Prop(1, 3, 4), Prop(2, 1, 5)});
  c.Pump(kT0 + 10ms);
  CHECK(c.ApplyingOrigin() == ApplyOrigin::kNone);  // outside an adapter call
  auto one = rig.adapter.For(PropKey(1));
  CHECK(one.size() == 1);
  CHECK(!one.empty() && one[0].revision == 3 && one[0].origin == ApplyOrigin::kBaseline);
  // Prop 2 waits for its entity (no adapter call), its origin kept.
  CHECK(rig.adapter.For(PropKey(2)).empty());
  CHECK(c.State().Origin(PropKey(2)) == ApplyOrigin::kBaseline);
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kNone);  // settled

  // The entity comes; the next pump hands the row out again, still the baseline's.
  rig.Spawn(2);
  c.Pump(kT0 + 20ms);
  auto two = rig.adapter.For(PropKey(2));
  CHECK(two.size() == 1);
  CHECK(!two.empty() && two[0].origin == ApplyOrigin::kBaseline);
  CHECK(c.State().Unsettled() == 0);
}

BACKEND_TEST("S6-AO2", DeltaAfterBaselineEndSameBatchIsDelta) {
  Rig rig;
  Client& c = rig.client;
  rig.Spawn(1);
  rig.Spawn(3);
  rig.Spawn(4);  // prop 2's entity comes later

  // A repair begins in one pump (the pump after it starts with the level scope staging) ...
  PushBaseline(rig.link, 2, 5, {}, /*begin=*/true, /*end=*/false);
  c.Pump(kT0 + 10ms);
  CHECK(c.State().Staging() == kScopeAll);

  // ... and in the next receive batch: a delta read while staging (prop 4), the rows (props 1 and
  // 3), BaselineEnd, then deltas after the end: a new key (prop 2) and a newer revision of prop 1.
  rig.link.Push(DeltaOf(Prop(4, 2, 6)));
  PushBaseline(rig.link, 2, 5, {Prop(1, 1, 3), Prop(3, 1, 4)}, /*begin=*/false, /*end=*/true);
  rig.link.Push(DeltaOf(Prop(2, 1, 7)));
  rig.link.Push(DeltaOf(Prop(1, 2, 8)));
  c.Pump(kT0 + 20ms);

  auto one = rig.adapter.For(PropKey(1));
  CHECK(one.size() == 1);
  CHECK(!one.empty() && one[0].revision == 2 && one[0].origin == ApplyOrigin::kDelta);
  auto three = rig.adapter.For(PropKey(3));
  CHECK(three.size() == 1);
  CHECK(!three.empty() && three[0].origin == ApplyOrigin::kBaseline);
  auto four = rig.adapter.For(PropKey(4));
  CHECK(four.size() == 1);
  CHECK(!four.empty() && four[0].revision == 2 && four[0].origin == ApplyOrigin::kDelta);
  // The new key's entity is not here: unbound, kept as a delta for its retry.
  CHECK(rig.adapter.For(PropKey(2)).empty());
  CHECK(c.State().Origin(PropKey(2)) == ApplyOrigin::kDelta);

  rig.Spawn(2);
  c.Pump(kT0 + 30ms);
  auto two = rig.adapter.For(PropKey(2));
  CHECK(two.size() == 1);
  CHECK(!two.empty() && two[0].origin == ApplyOrigin::kDelta);
  CHECK(c.State().Unsettled() == 0);
}

BACKEND_TEST("S6-AO3", OriginSurvivesFailedRetry) {
  Rig rig;
  Client& c = rig.client;
  rig.Spawn(1);
  rig.Spawn(2);
  rig.adapter.fail[PropKey(1)] = 2;  // the baseline row fails twice
  rig.adapter.fail[PropKey(2)] = 1;  // the delta row once

  PushBaseline(rig.link, 2, 5, {Prop(1, 1, 5)});
  rig.link.Push(DeltaOf(Prop(2, 1, 6)));
  c.Pump(kT0 + 10ms);
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kBaseline);
  CHECK(c.State().Origin(PropKey(2)) == ApplyOrigin::kDelta);
  c.Pump(kT0 + 20ms);  // retried once each: prop 2 applies, prop 1 fails again
  c.Pump(kT0 + 30ms);  // prop 1 applies

  auto one = rig.adapter.For(PropKey(1));
  CHECK(one.size() == 3);
  for (const auto& call : one) CHECK(call.revision == 1 && call.origin == ApplyOrigin::kBaseline);
  auto two = rig.adapter.For(PropKey(2));
  CHECK(two.size() == 2);
  for (const auto& call : two) CHECK(call.origin == ApplyOrigin::kDelta);
  CHECK(c.State().Unsettled() == 0);
}

BACKEND_TEST("S6-AO4", SameRevisionKeepsFirstOrigin) {
  // A delta waiting for its entity, then a repair carrying the same revision: still the delta's
  // (the revision was received that way). A repair carrying a newer revision brings kBaseline.
  Rig rig;
  Client& c = rig.client;
  rig.link.Push(DeltaOf(Prop(1, 1, 1)));
  c.Pump(kT0 + 10ms);
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kDelta);
  PushBaseline(rig.link, 2, 2, {Prop(1, 1, 1)});
  c.Pump(kT0 + 20ms);
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kDelta);
  PushBaseline(rig.link, 3, 3, {Prop(1, 2, 3)});
  c.Pump(kT0 + 30ms);
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kBaseline);
  rig.Spawn(1);
  c.Pump(kT0 + 40ms);
  auto one = rig.adapter.For(PropKey(1));
  CHECK(one.size() == 1);
  CHECK(!one.empty() && one[0].revision == 2 && one[0].origin == ApplyOrigin::kBaseline);
}

BACKEND_TEST("S6-AO5", FirstOriginOutlivesALaterDelta) {
  // A baseline installs K at revision 3; a delta at revision 4 comes before K's first successful
  // apply (another player's skip on a scene that stays open). Per revision K is now a delta; its
  // first origin stays the baseline's until K settles. A settled K starts over.
  Rig rig;
  Client& c = rig.client;
  PushBaseline(rig.link, 2, 5, {Prop(1, 3, 4)});
  c.Pump(kT0 + 10ms);  // K's entity is not here: waiting, no adapter call
  CHECK(rig.adapter.For(PropKey(1)).empty());
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kBaseline);
  CHECK(c.State().FirstOrigin(PropKey(1)) == ApplyOrigin::kBaseline);

  rig.link.Push(DeltaOf(Prop(1, 4, 6)));
  c.Pump(kT0 + 20ms);
  CHECK(rig.adapter.For(PropKey(1)).empty());
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kDelta);
  CHECK(c.State().FirstOrigin(PropKey(1)) == ApplyOrigin::kBaseline);

  rig.Spawn(1);
  c.Pump(kT0 + 30ms);
  CHECK(c.ApplyingFirstOrigin() == ApplyOrigin::kNone);  // outside an adapter call
  auto one = rig.adapter.For(PropKey(1));
  CHECK(one.size() == 1);
  CHECK(!one.empty() && one[0].revision == 4 && one[0].origin == ApplyOrigin::kDelta &&
        one[0].first == ApplyOrigin::kBaseline);
  CHECK(c.State().Unsettled() == 0);
  CHECK(c.State().FirstOrigin(PropKey(1)) == ApplyOrigin::kNone);  // settled

  // A fresh K from a delta: first kDelta, kept when a repair then brings revision 6 (per revision
  // kBaseline) after the delta's apply failed.
  rig.adapter.fail[PropKey(1)] = 1;
  rig.link.Push(DeltaOf(Prop(1, 5, 7)));
  c.Pump(kT0 + 40ms);
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kDelta);
  CHECK(c.State().FirstOrigin(PropKey(1)) == ApplyOrigin::kDelta);
  PushBaseline(rig.link, 3, 8, {Prop(1, 6, 8)});
  c.Pump(kT0 + 50ms);
  one = rig.adapter.For(PropKey(1));
  CHECK(one.size() == 3);
  if (one.size() == 3) {
    CHECK(one[1].revision == 5 && one[1].origin == ApplyOrigin::kDelta &&
          one[1].first == ApplyOrigin::kDelta);
    CHECK(one[2].revision == 6 && one[2].origin == ApplyOrigin::kBaseline &&
          one[2].first == ApplyOrigin::kDelta);
  }
  CHECK(c.State().Unsettled() == 0);
}

BACKEND_TEST("S6-AO6", StagedDeltaKeepsTheBaselinesFirstOrigin) {
  // The same while the scope stages: a delta at revision 4 read during the baseline, whose rows
  // then carry K at revision 3. The swap queues revision 4 (per revision kDelta) with first origin
  // kBaseline, since the baseline's snapshot held K. A key only a staged delta brought is kDelta
  // both ways.
  Rig rig;
  Client& c = rig.client;
  PushBaseline(rig.link, 2, 5, {}, /*begin=*/true, /*end=*/false);
  c.Pump(kT0 + 10ms);
  CHECK(c.State().Staging() == kScopeAll);

  rig.link.Push(DeltaOf(Prop(1, 4, 6)));
  rig.link.Push(DeltaOf(Prop(2, 1, 7)));
  PushBaseline(rig.link, 2, 5, {Prop(1, 3, 4)}, /*begin=*/false, /*end=*/true);
  c.Pump(kT0 + 20ms);
  CHECK(c.State().Staging() == 0);
  CHECK(c.State().Origin(PropKey(1)) == ApplyOrigin::kDelta);
  CHECK(c.State().FirstOrigin(PropKey(1)) == ApplyOrigin::kBaseline);
  CHECK(c.State().Origin(PropKey(2)) == ApplyOrigin::kDelta);
  CHECK(c.State().FirstOrigin(PropKey(2)) == ApplyOrigin::kDelta);

  rig.Spawn(1);
  rig.Spawn(2);
  c.Pump(kT0 + 30ms);
  auto one = rig.adapter.For(PropKey(1));
  CHECK(one.size() == 1);
  CHECK(!one.empty() && one[0].revision == 4 && one[0].origin == ApplyOrigin::kDelta &&
        one[0].first == ApplyOrigin::kBaseline);
  auto two = rig.adapter.For(PropKey(2));
  CHECK(two.size() == 1);
  CHECK(!two.empty() && two[0].origin == ApplyOrigin::kDelta && two[0].first == ApplyOrigin::kDelta);
  CHECK(c.State().Unsettled() == 0);
}

}  // namespace skylanders::backend
