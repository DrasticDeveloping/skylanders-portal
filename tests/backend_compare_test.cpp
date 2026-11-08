// The compare queries (design section 7.2; tests CQ1 to CQ8 of section 10): CompareLocal on S and
// on a client, then JoinCompare over the union of their keys. CQ1 to CQ5 also pin the three line
// formats in the golden file tests/backend/golden/compare.txt (compared like MD1's, written only
// under --update-golden).

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

#include "backend/adapter.h"
#include "backend/compare.h"
#include "backend/entity_id.h"
#include "backend/kind_table.h"
#include "backend/world_state.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

using namespace skylanders::backend;
using namespace std::chrono_literals;

namespace {

const TimePoint kT0 = TimePoint{} + 10h;

// A kind no peer can read (plan decision 5): listed unsupported, never initialised.
constexpr KindId kNeverKind = 9;
inline constexpr KindSpec kCqNever{
    .id = kNeverKind, .name = "cq.never", .scope = Scope::kLevel, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kNever,
    .write = WriteMode::kMirrored, .unsupported = true, .max_value_bytes = 8,
    .depends_on = {}, .authored_default = {}};
const KindSpec kCqTable[] = {test::kTestToggle, kCqNever};
constexpr KindId kToggle = test::kToggle;

constexpr uint16_t kServerPeer = 0;
constexpr uint16_t kClientPeer = 1;

// One peer's world for one kind: a value per (handle, slot), and the faults a compare must see.
class LiveWorld : public KindAdapter {
 public:
  AdapterResult Read(Handle h, uint16_t slot, std::vector<uint8_t>& value) override {
    if (unsupported) return AdapterResult::kUnsupported;
    if (fail_read) return AdapterResult::kFailed;
    const auto it = values.find({h, slot});
    if (it == values.end()) return AdapterResult::kGone;
    value = it->second;
    return AdapterResult::kOk;
  }
  // `stuck`: the write is taken and the native puts its own value back before the read (the fake
  // world's `stuck`, design section 3.12).
  AdapterResult Apply(Handle h, uint16_t slot, std::span<const uint8_t> value) override {
    if (unsupported) return AdapterResult::kUnsupported;
    ++applies;
    if (!stuck) values[{h, slot}].assign(value.begin(), value.end());
    return AdapterResult::kOk;
  }

  std::map<std::pair<Handle, uint16_t>, std::vector<uint8_t>> values;
  bool stuck = false;
  bool unsupported = false;
  bool fail_read = false;
  int applies = 0;
};

struct Peer {
  Peer() {
    set.Register(kToggle, world);
    set.Register(kNeverKind, world);
    set.Identity().OnEpoch(1);
  }
  void Spawn(const EntityId& id, Handle h, std::vector<uint8_t> value, TimePoint now = kT0) {
    set.Identity().OnSpawn(id, h, now);
    world.values[{h, 0}] = std::move(value);
  }
  LiveWorld world;
  AdapterSet set;
};

EntityId ToggleA() { return Authored(1, 0x30, HashName("toggle_a"), HashName("Toggle")); }
EntityId ToggleB() { return Authored(1, 0x31, HashName("toggle_b"), HashName("Toggle")); }
EntityId ToggleC() { return Authored(1, 0x32, HashName("toggle_c"), HashName("Toggle")); }

RecordKey Key(KindId kind, const EntityId& id) { return RecordKey{kind, id, 0}; }

// S with one row of `kind` on ToggleA, value {1}; the client's replica with S's record.
struct Session {
  explicit Session(KindId kind = kToggle) : s(kCqTable, Limits{}), r(kCqTable, Limits{}) {
    s.BeginEpoch(1);
    CHECK(s.Put(Key(kind, ToggleA()), std::vector<uint8_t>{1}, kT0) == PutResult::kWritten);
    r.OnEpoch(1, 1);
    Give(Key(kind, ToggleA()));
  }
  // The client receives S's record of `key` as it stands.
  void Give(const RecordKey& key) {
    const Record* rec = s.Get(key);
    CHECK(rec != nullptr);
    if (rec != nullptr) CHECK(r.OnRecord(*rec, rec->lsn) == ApplyResult::kApplied);
  }
  WorldState s;
  Replica r;
  Peer server, client;
};

struct Halves {
  std::vector<LocalCompare> server, client;
  std::vector<RowCompare> joined;
};

Halves Compare(Session& x, KindId kind, bool repair, TimePoint now = kT0 + 1s) {
  Halves h;
  h.server = CompareLocal(x.s, x.server.set, kind, repair, now);
  h.client = CompareLocal(x.r, x.client.set, kind, repair, now);
  h.joined = JoinCompare(h.server, h.client);
  return h;
}

// The section a scenario writes into compare.txt.
std::string Lines(std::string_view title, KindId kind, const Halves& h) {
  const std::string_view name = FindKind(kCqTable, kind)->name;
  std::string out = "# " + std::string(title) + "\n";
  for (const LocalCompare& c : h.server) out += LocalLine(kServerPeer, name, c) + "\n";
  for (const LocalCompare& c : h.client) out += LocalLine(kClientPeer, name, c) + "\n";
  for (const RowCompare& r : h.joined) out += CompareLine(kClientPeer, name, r) + "\n";
  out += ComparedLine(kClientPeer, name, Summarize(kind, h.joined)) + "\n";
  return out;
}

// The five results, one scenario each; every scenario checks itself and returns its lines.
std::string Equal() {
  Session x;
  x.server.Spawn(ToggleA(), 100, {1});
  x.client.Spawn(ToggleA(), 200, {1});
  const Halves h = Compare(x, kToggle, true);
  CHECK(h.server.size() == 1 && h.client.size() == 1 && h.joined.size() == 1);
  if (h.joined.size() != 1 || h.server.size() != 1 || h.client.size() != 1) return {};
  CHECK(h.server[0].live == LocalCompare::Live::kEqual);
  CHECK(h.client[0].live == LocalCompare::Live::kEqual);
  CHECK(h.server[0].digest == ValueDigest(std::vector<uint8_t>{1}));
  CHECK(h.joined[0].result == CompareResult::kEqual);
  CHECK(h.joined[0].differs.empty());
  CHECK(h.joined[0].stored_revision == 1 && h.joined[0].replica_revision == 1);
  CHECK(x.server.world.applies == 0 && x.client.world.applies == 0);
  return Lines("CQ1 CompareEqual", kToggle, h);
}

std::string Repaired() {
  Session x;
  x.server.Spawn(ToggleA(), 100, {1});
  x.client.Spawn(ToggleA(), 200, {0});  // the client's world lost the write
  // Without repair the difference is reported, nothing is written.
  const Halves seen = Compare(x, kToggle, false);
  CHECK(seen.client.size() == 1 && seen.client[0].live == LocalCompare::Live::kStuck);
  CHECK(x.client.world.applies == 0);
  const Halves h = Compare(x, kToggle, true);
  CHECK(h.client.size() == 1 && h.joined.size() == 1);
  if (h.client.size() != 1 || h.joined.size() != 1) return {};
  CHECK(h.client[0].live == LocalCompare::Live::kRepaired);
  CHECK(x.client.world.applies == 1);  // one Apply, one Read again
  CHECK((x.client.world.values[{200, 0}] == std::vector<uint8_t>{1}));
  CHECK(h.joined[0].result == CompareResult::kRepaired);
  CHECK(h.joined[0].differs == "client: repaired");
  // Repaired once, equal after.
  const Halves again = Compare(x, kToggle, true);
  CHECK(again.joined.size() == 1 && again.joined[0].result == CompareResult::kEqual);
  return Lines("CQ2 CompareRepaired", kToggle, h);
}

std::string Pending() {
  Session x;
  x.server.Spawn(ToggleA(), 100, {1});
  x.client.Spawn(ToggleA(), 200, {1});
  x.client.set.Identity().OnDespawn(200, /*streamed_out=*/true, kT0 + 1s);
  const Halves h = Compare(x, kToggle, true, kT0 + 4s);
  CHECK(h.client.size() == 1 && h.joined.size() == 1);
  if (h.client.size() != 1 || h.joined.size() != 1) return {};
  CHECK(h.client[0].live == LocalCompare::Live::kPending);
  CHECK(h.client[0].pending_for == 3s);
  CHECK(x.client.world.applies == 0);  // nothing to write to
  CHECK(h.joined[0].result == CompareResult::kPending);
  CHECK(h.joined[0].pending_for == 3s);
  CHECK(h.joined[0].differs == "client: pending");
  // Without a time, pending_for stays 0 (the design's signature).
  const std::vector<LocalCompare> untimed = CompareLocal(x.r, x.client.set, kToggle, false);
  CHECK(untimed.size() == 1 && untimed[0].pending_for == Duration::zero());
  return Lines("CQ3 ComparePending", kToggle, h);
}

std::string Stuck() {
  Session x;
  x.server.Spawn(ToggleA(), 100, {1});
  x.client.Spawn(ToggleA(), 200, {0});
  x.client.world.stuck = true;  // the native writes its value back
  const Halves h = Compare(x, kToggle, true);
  CHECK(h.client.size() == 1 && h.joined.size() == 1);
  if (h.client.size() != 1 || h.joined.size() != 1) return {};
  CHECK(h.client[0].live == LocalCompare::Live::kStuck);
  CHECK(x.client.world.applies == 1);  // repair is one Apply and one Read again, never a loop
  CHECK(h.client[0].differs.rfind("value ", 0) == 0);
  CHECK(h.joined[0].result == CompareResult::kStuck);
  CHECK(h.joined[0].differs == "client: " + h.client[0].differs);
  return Lines("CQ4 CompareStuck", kToggle, h);
}

std::string Unsupported() {
  Session x(kNeverKind);
  x.server.Spawn(ToggleA(), 100, {1});
  x.client.Spawn(ToggleA(), 200, {1});
  const Halves h = Compare(x, kNeverKind, true);
  CHECK(h.server.size() == 1 && h.client.size() == 1 && h.joined.size() == 1);
  if (h.joined.size() != 1 || h.server.size() != 1 || h.client.size() != 1) return {};
  CHECK(h.server[0].live == LocalCompare::Live::kUnsupported);
  CHECK(h.client[0].live == LocalCompare::Live::kUnsupported);
  CHECK(x.server.world.applies == 0 && x.client.world.applies == 0);
  CHECK(h.joined[0].result == CompareResult::kUnsupported);
  const CompareSummary sum = Summarize(kNeverKind, h.joined);
  CHECK(sum.kind == kNeverKind && sum.unsupported == 1);
  CHECK(sum.equal + sum.repaired + sum.pending + sum.stuck == 0);
  return Lines("CQ5 CompareUnsupported", kNeverKind, h);
}

}  // namespace

// The golden file holds the five scenarios' lines in order; CQ1 checks it, so a filtered run of
// one CQ test checks only that test's results.
BACKEND_TEST("CQ1", CompareEqual) {
  CHECK(Validate(kCqTable).empty());
  const std::string lines = Equal() + Repaired() + Pending() + Stuck() + Unsupported();
  test::CheckGolden("compare.txt", lines);
}

BACKEND_TEST("CQ2", CompareRepaired) { Repaired(); }

BACKEND_TEST("CQ3", ComparePending) { Pending(); }

BACKEND_TEST("CQ4", CompareStuck) { Stuck(); }

BACKEND_TEST("CQ5", CompareUnsupported) {
  Unsupported();
  CHECK(CompareResultName(CompareResult::kEqual) == "equal");
  CHECK(CompareResultName(CompareResult::kRepaired) == "repaired");
  CHECK(CompareResultName(CompareResult::kPending) == "pending");
  CHECK(CompareResultName(CompareResult::kStuck) == "stuck");
  CHECK(CompareResultName(CompareResult::kUnsupported) == "unsupported");
}

// Sol F5: S's adapter is stuck, the client's live read is equal, revisions and digests match: the
// row is stuck, and the join names S's side.
BACKEND_TEST("CQ6", CompareServerStuckClientEqual) {
  Session x;
  x.server.Spawn(ToggleA(), 100, {0});
  x.server.world.stuck = true;
  x.client.Spawn(ToggleA(), 200, {1});
  const Halves h = Compare(x, kToggle, true);
  CHECK(h.server.size() == 1 && h.client.size() == 1 && h.joined.size() == 1);
  if (h.joined.size() != 1 || h.server.size() != 1 || h.client.size() != 1) return;
  CHECK(h.server[0].live == LocalCompare::Live::kStuck);
  CHECK(h.client[0].live == LocalCompare::Live::kEqual);
  CHECK(h.server[0].revision == h.client[0].revision);
  CHECK(h.server[0].digest == h.client[0].digest);
  CHECK(h.joined[0].result == CompareResult::kStuck);
  CHECK(h.joined[0].differs.rfind("server: ", 0) == 0);
  CHECK(h.joined[0].differs == "server: " + h.server[0].differs);
  const CompareSummary sum = Summarize(kToggle, h.joined);
  CHECK(sum.stuck == 1 && sum.equal == 0);
}

// Sol F5: the join runs over the union of keys. A row on the client that S does not have (no row,
// no tomb) is stuck `replica extra`; the other union rules beside it.
BACKEND_TEST("CQ7", CompareClientExtraRow) {
  Session x;
  x.server.Spawn(ToggleA(), 100, {1});
  x.client.Spawn(ToggleA(), 200, {1});
  // Extra on the client: a record S never wrote.
  Record extra;
  extra.key = Key(kToggle, ToggleB());
  extra.revision = 1;
  extra.epoch = 1;
  extra.lsn = 50;
  extra.value = {1};
  CHECK(x.r.OnRecord(extra, extra.lsn) == ApplyResult::kApplied);
  x.client.Spawn(ToggleB(), 201, {1});
  {
    const Halves h = Compare(x, kToggle, false);
    CHECK(h.joined.size() == 2);
    if (h.joined.size() != 2) return;
    CHECK(h.joined[0].key == Key(kToggle, ToggleA()));
    CHECK(h.joined[0].result == CompareResult::kEqual);
    CHECK(h.joined[1].key == Key(kToggle, ToggleB()));
    CHECK(h.joined[1].result == CompareResult::kStuck);
    CHECK(h.joined[1].differs == "replica extra");
    CHECK(h.joined[1].stored_revision == 0 && h.joined[1].replica_revision == 1);
  }

  // Missing on the client: S wrote ToggleC, the client never got it.
  CHECK(x.s.Put(Key(kToggle, ToggleC()), std::vector<uint8_t>{1}, kT0) == PutResult::kWritten);
  x.server.Spawn(ToggleC(), 102, {1});
  // Behind: S wrote ToggleA again, the client holds revision 1.
  CHECK(x.s.Put(Key(kToggle, ToggleA()), std::vector<uint8_t>{0}, kT0 + 100ms) ==
        PutResult::kWritten);
  x.server.world.values[{100, 0}] = {0};
  {
    const Halves h = Compare(x, kToggle, false);
    CHECK(h.joined.size() == 3);
    if (h.joined.size() != 3) return;
    CHECK(h.joined[0].result == CompareResult::kStuck);
    CHECK(h.joined[0].differs == "replica behind");
    CHECK(h.joined[0].stored_revision == 2 && h.joined[0].replica_revision == 1);
    CHECK(h.joined[1].differs == "replica extra");
    CHECK(h.joined[2].key == Key(kToggle, ToggleC()));
    CHECK(h.joined[2].differs == "replica missing");
    CHECK(h.joined[2].stored_revision == 1 && h.joined[2].replica_revision == 0);
    const CompareSummary sum = Summarize(kToggle, h.joined);
    CHECK(sum.stuck == 3);
  }

  // Differs: the same revision with another value (a replica bug, not lateness).
  std::vector<LocalCompare> server = CompareLocal(x.s, x.server.set, kToggle, false);
  std::vector<LocalCompare> client = server;
  for (LocalCompare& c : client) c.digest ^= 1;
  const std::vector<RowCompare> joined = JoinCompare(server, client);
  CHECK(!joined.empty());
  for (const RowCompare& r : joined) CHECK(r.differs == "replica differs");
  // A replica ahead of S is no lateness either.
  for (LocalCompare& c : client) {
    c.digest ^= 1;
    ++c.revision;
  }
  for (const RowCompare& r : JoinCompare(server, client)) CHECK(r.differs == "replica differs");
  CHECK(JoinCompare({}, {}).empty());
}

// Sol r2 N3: revisions and digests equal; exactly one side unsupported is stuck, naming the side;
// both unsupported is unsupported.
BACKEND_TEST("CQ8", CompareOneSideUnsupported) {
  const auto run = [](bool server_unsupported, bool client_unsupported) {
    Session x;
    x.server.Spawn(ToggleA(), 100, {1});
    x.client.Spawn(ToggleA(), 200, {1});
    x.server.world.unsupported = server_unsupported;
    x.client.world.unsupported = client_unsupported;
    const Halves h = Compare(x, kToggle, true);
    CHECK(h.server.size() == 1 && h.client.size() == 1 && h.joined.size() == 1);
    if (h.server.size() == 1 && h.client.size() == 1) {
      CHECK(h.server[0].revision == h.client[0].revision);
      CHECK(h.server[0].digest == h.client[0].digest);
      CHECK((h.server[0].live == LocalCompare::Live::kUnsupported) == server_unsupported);
      CHECK((h.client[0].live == LocalCompare::Live::kUnsupported) == client_unsupported);
    }
    return h.joined.size() == 1 ? h.joined[0] : RowCompare{};
  };
  const RowCompare a = run(true, false);
  CHECK(a.result == CompareResult::kStuck);
  CHECK(a.differs == "server: unsupported");
  const RowCompare b = run(false, true);
  CHECK(b.result == CompareResult::kStuck);
  CHECK(b.differs == "client: unsupported");
  const RowCompare c = run(true, true);
  CHECK(c.result == CompareResult::kUnsupported);
  CHECK(c.differs.empty());
}
