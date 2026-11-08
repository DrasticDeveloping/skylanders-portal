// The validator tests (design section 10, VA1 to VA15 with VA12b and VA13b): one crafted table per
// rule V1 to V14, each hitting exactly its rule; the test table; the table digest; Replicated and
// Fenced, and the parts that use them.

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include "backend/kind_table.h"
#include "backend/map_dump.h"
#include "backend/world_state.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;

// A valid kind that touches no rule: level scope, a mirrored current value read at spawn.
KindSpec Spec(KindId id, std::string_view name) {
  KindSpec s;
  s.id = id;
  s.name = name;
  s.scope = Scope::kLevel;
  s.tier = Tier::kStructural;
  s.state_class = StateClass::kCurrentValue;
  s.initial = InitialPolicy::kReadAtSpawn;
  s.write = WriteMode::kMirrored;
  s.lane = LaneUse::kState;
  s.max_value_bytes = 8;
  return s;
}

void Print(const std::vector<ValidationIssue>& issues) {
  for (const ValidationIssue& i : issues) {
    std::fprintf(stderr, "  issue %u %.*s: %s\n", unsigned(i.kind), int(i.rule.size()), i.rule.data(),
                 i.text.c_str());
  }
}

// The table breaks `rule` and nothing else.
bool OnlyRule(std::span<const KindSpec> table, std::string_view rule, const Limits& limits = {}) {
  const std::vector<ValidationIssue> issues = Validate(table, limits);
  bool ok = !issues.empty();
  for (const ValidationIssue& i : issues) ok = ok && i.rule == rule;
  if (!ok) {
    std::fprintf(stderr, "  expected only %.*s, got %zu issues\n", int(rule.size()), rule.data(), issues.size());
    Print(issues);
  }
  return ok;
}

bool Passes(std::span<const KindSpec> table, const Limits& limits = {}) {
  const std::vector<ValidationIssue> issues = Validate(table, limits);
  if (!issues.empty()) Print(issues);
  return issues.empty();
}

constexpr std::array<uint8_t, 1> kOneByte{1};
constexpr std::array<uint8_t, 9> kNineBytes{1, 2, 3, 4, 5, 6, 7, 8, 9};

}  // namespace
}  // namespace skylanders::backend

using namespace skylanders::backend;

BACKEND_TEST("VA1", ValidatorIds) {
  CHECK(Passes(std::array{Spec(1, "a"), Spec(2, "b")}));
  CHECK(OnlyRule(std::array{Spec(0, "a")}, "V1 ids"));
  CHECK(OnlyRule(std::array{Spec(1, "a"), Spec(1, "b")}, "V1 ids"));
  CHECK(OnlyRule(std::array{Spec(1, "")}, "V1 ids"));
  CHECK(OnlyRule(std::array{Spec(1, "a"), Spec(2, "a")}, "V1 ids"));
}

BACKEND_TEST("VA2", ValidatorInitial) {
  KindSpec s = Spec(1, "a");
  s.initial = InitialPolicy::kUnset;
  CHECK(OnlyRule(std::array{s}, "V2 initial"));
  s = Spec(1, "a");
  s.write = WriteMode::kUnset;
  CHECK(OnlyRule(std::array{s}, "V2 initial"));
}

BACKEND_TEST("VA3", ValidatorNever) {
  KindSpec s = Spec(1, "a");
  s.initial = InitialPolicy::kNever;
  s.unsupported = true;
  CHECK(Passes(std::array{s}));  // an unsupported kind, mirrored
  s.write = WriteMode::kApplied;  // `applied` on `never`
  CHECK(OnlyRule(std::array{s}, "V3 never"));
  s = Spec(1, "a");
  s.initial = InitialPolicy::kNever;  // never without unsupported
  CHECK(OnlyRule(std::array{s}, "V3 never"));
  s = Spec(1, "a");
  s.unsupported = true;  // unsupported without never
  CHECK(OnlyRule(std::array{s}, "V3 never"));
}

BACKEND_TEST("VA4", ValidatorCycles) {
  const std::array<KindId, 1> self{1};
  const std::array<KindId, 1> on_one{1};
  const std::array<KindId, 1> on_two{2};
  const std::array<KindId, 1> on_three{3};
  const std::array<KindId, 1> unknown{9};
  KindSpec a = Spec(1, "a");
  KindSpec b = Spec(2, "b");
  KindSpec c = Spec(3, "c");
  // A chain passes.
  b.depends_on = on_one;
  c.depends_on = on_two;
  CHECK(Passes(std::array{a, b, c}));
  // Itself.
  a.depends_on = self;
  CHECK(OnlyRule(std::array{a}, "V4 cycles"));
  // An unknown kind.
  a.depends_on = unknown;
  CHECK(OnlyRule(std::array{a, b}, "V4 cycles"));
  // A cycle through three kinds: each of them named.
  a.depends_on = on_three;
  const std::vector<ValidationIssue> issues = Validate(std::array{a, b, c});
  CHECK(OnlyRule(std::array{a, b, c}, "V4 cycles"));
  std::set<KindId> named;
  for (const ValidationIssue& i : issues) named.insert(i.kind);
  CHECK(named == std::set<KindId>({1, 2, 3}));
}

BACKEND_TEST("VA5", ValidatorScope) {
  const std::array<KindId, 1> on_one{1};
  KindSpec level = Spec(1, "level");
  KindSpec session = Spec(2, "session");
  session.scope = Scope::kSession;
  session.depends_on = on_one;
  CHECK(OnlyRule(std::array{level, session}, "V5 scope"));
  level.scope = Scope::kSession;  // session on session is fine
  CHECK(Passes(std::array{level, session}));
  // A level kind may depend on a session kind.
  KindSpec on_session = Spec(3, "level2");
  on_session.depends_on = on_one;
  CHECK(Passes(std::array{level, on_session}));
}

BACKEND_TEST("VA6", ValidatorEvents) {
  KindSpec s = Spec(1, "a");
  s.tier = Tier::kDurableEvent;  // a durable event that is a current value
  CHECK(OnlyRule(std::array{s}, "V6 events"));
  s = Spec(1, "a");
  s.state_class = StateClass::kEvent;  // an event that is structural
  CHECK(OnlyRule(std::array{s}, "V6 events"));
  s.tier = Tier::kDurableEvent;
  CHECK(Passes(std::array{s}));
}

BACKEND_TEST("VA7", ValidatorLanes) {
  KindSpec s = Spec(1, "a");
  s.lane = LaneUse::kFast;
  CHECK(OnlyRule(std::array{s}, "V7 lanes"));
  s.tier = Tier::kEffect;  // an effect may ride fast
  CHECK(Passes(std::array{s}));
  CHECK(!Replicated(s));   // and is in no baseline and no manifest (VA15 checks the users)
}

BACKEND_TEST("VA8", ValidatorDefault) {
  KindSpec s = Spec(1, "a");
  s.initial = InitialPolicy::kAuthoredDefault;  // no default
  CHECK(OnlyRule(std::array{s}, "V8 default"));
  s.authored_default = kNineBytes;  // longer than max_value_bytes (8)
  CHECK(OnlyRule(std::array{s}, "V8 default"));
  s.authored_default = kOneByte;
  CHECK(Passes(std::array{s}));
  s.initial = InitialPolicy::kReadAtSpawn;  // a default on another policy
  CHECK(OnlyRule(std::array{s}, "V8 default"));
}

BACKEND_TEST("VA9", ValidatorSizes) {
  KindSpec s = Spec(1, "a");
  s.max_value_bytes = 0;
  CHECK(OnlyRule(std::array{s}, "V9 sizes"));
  s.max_value_bytes = uint16_t(kMaxValueBytes + 1);
  CHECK(OnlyRule(std::array{s}, "V9 sizes"));
  s.max_value_bytes = 300;
  CHECK(Passes(std::array{s}));
  Limits small;
  small.max_value_bytes = 256;  // a game's lower limit
  CHECK(OnlyRule(std::array{s}, "V9 sizes", small));
  // The reserved classes: 1 (client-owned value, stage 4) and 2 (merge on S).
  s = Spec(1, "a");
  s.state_class = StateClass(1);
  CHECK(OnlyRule(std::array{s}, "V9 sizes"));
  s.state_class = StateClass(2);
  CHECK(OnlyRule(std::array{s}, "V9 sizes"));
}

BACKEND_TEST("VA10", ValidatorModels) {
  KindSpec s = Spec(1, "a");
  s.models = kModelOwnerRun;  // without leasable
  CHECK(OnlyRule(std::array{s}, "V10 models"));
  s.leasable = true;
  CHECK(Passes(std::array{s}));
  s.write = WriteMode::kServerOnly;  // owner-run on a server-only kind
  CHECK(OnlyRule(std::array{s}, "V10 models"));
  s = Spec(1, "a");
  s.parent_wait_ms = 2000;  // on a kind that takes no requests
  CHECK(OnlyRule(std::array{s}, "V10 models"));
  s.models = kModelCommit;
  CHECK(Passes(std::array{s}));
  s = Spec(1, "a");
  s.requires_precondition = true;
  CHECK(OnlyRule(std::array{s}, "V10 models"));
  s.models = kModelServerRun;
  CHECK(Passes(std::array{s}));
}

BACKEND_TEST("VA11", ValidatorNoisy) {
  KindSpec s = Spec(1, "a");
  s.tier = Tier::kDurableEvent;
  s.state_class = StateClass::kEvent;
  s.noisy = true;
  CHECK(OnlyRule(std::array{s}, "V11 noisy"));
  s = Spec(1, "a");
  s.noisy = true;  // a noisy current value
  CHECK(Passes(std::array{s}));
}

BACKEND_TEST("VA12", ValidatorGrace) {
  KindSpec s = Spec(1, "a");
  s.write = WriteMode::kApplied;  // applied, read at spawn, grace 0
  CHECK(OnlyRule(std::array{s}, "V12 grace"));
  s.pending_grace_ms = 5000;
  CHECK(Passes(std::array{s}));
}

BACKEND_TEST("VA12b", ValidatorDependencyGrace) {
  const std::array<KindId, 1> on_one{1};
  KindSpec toggle = Spec(1, "toggle");
  toggle.write = WriteMode::kApplied;
  toggle.initial = InitialPolicy::kAuthoredDefault;  // not read at spawn: only the dependency asks
  toggle.authored_default = kOneByte;
  KindSpec scene = Spec(2, "scene");
  scene.depends_on = on_one;
  CHECK(Passes(std::array{toggle}));  // nothing depends on it yet
  const std::vector<ValidationIssue> issues = Validate(std::array{toggle, scene});
  CHECK(OnlyRule(std::array{toggle, scene}, "V12 grace"));
  CHECK(issues.size() == 1 && issues[0].kind == 1);
  toggle.pending_grace_ms = 5000;
  CHECK(Passes(std::array{toggle, scene}));
}

BACKEND_TEST("VA13", ValidatorReplicatedDeps) {
  const std::array<KindId, 1> on_one{1};
  KindSpec hidden = Spec(1, "hidden");
  hidden.write = WriteMode::kServerOnly;
  KindSpec shown = Spec(2, "shown");
  shown.depends_on = on_one;
  CHECK(OnlyRule(std::array{hidden, shown}, "V13 replicated deps"));
  hidden = Spec(1, "spark");
  hidden.tier = Tier::kEffect;  // an effect is never sent as a row either
  CHECK(OnlyRule(std::array{hidden, shown}, "V13 replicated deps"));
  // A server-only kind may depend on anything.
  KindSpec server = Spec(3, "server");
  server.write = WriteMode::kServerOnly;
  server.depends_on = on_one;
  CHECK(Passes(std::array{hidden, server}));
}

BACKEND_TEST("VA13b", ValidatorTestTable) {
  CHECK(Passes(test::kTestTable));
  const std::set<std::string_view> names = {"test.toggle", "test.gate",  "test.scene",
                                            "test.prop",   "test.event", "test.server_only"};
  size_t found = 0;
  for (const KindSpec& s : test::kTestTable) found += names.contains(s.name);
  CHECK(found == names.size());
  CHECK(FindKind(test::kTestTable, test::kScene) != nullptr);
  CHECK(FindKind(test::kTestTable, 99) == nullptr);
}

namespace {
bool EqualA(std::span<const uint8_t> a, std::span<const uint8_t> b) { return std::ranges::equal(a, b); }
bool EqualB(std::span<const uint8_t> a, std::span<const uint8_t> b) { return std::ranges::equal(a, b); }
std::string_view ReasonA(uint8_t) { return "a"; }
std::string_view ReasonB(uint8_t) { return "b"; }
}  // namespace

BACKEND_TEST("VA14", TableDigestStable) {
  const std::vector<KindSpec> base(test::kTestTable.begin(), test::kTestTable.end());
  const uint64_t digest = TableDigest(base);
  CHECK(digest == TableDigest(test::kTestTable));

  // Not the order of declaration.
  std::vector<KindSpec> reversed(base.rbegin(), base.rend());
  CHECK(TableDigest(reversed) == digest);
  // Not the address of a span: the same contents elsewhere.
  const std::vector<KindId> deps_copy(test::kSceneDeps.begin(), test::kSceneDeps.end());
  const std::vector<uint8_t> default_copy(test::kGateDefault.begin(), test::kGateDefault.end());
  std::vector<KindSpec> moved = base;
  moved[test::kScene - 1].depends_on = deps_copy;
  moved[test::kGate - 1].authored_default = default_copy;
  CHECK(TableDigest(moved) == digest);
  // The dependencies are a set: their order is not part of the table.
  const std::vector<KindId> deps_reversed(test::kSceneDeps.rbegin(), test::kSceneDeps.rend());
  moved[test::kScene - 1].depends_on = deps_reversed;
  CHECK(TableDigest(moved) == digest);
  // Not the address of a function: only whether one is set.
  std::vector<KindSpec> fa = base, fb = base;
  fa[0].equal = &EqualA;
  fb[0].equal = &EqualB;
  fa[1].reason_name = &ReasonA;
  fb[1].reason_name = &ReasonB;
  CHECK(TableDigest(fa) == TableDigest(fb));
  CHECK(TableDigest(fa) != digest);

  // Every data field changes it.
  const std::vector<KindId> fewer_deps{test::kToggle};
  const std::vector<uint8_t> other_default{2};
  const std::vector<std::function<void(KindSpec&)>> changes = {
      [](KindSpec& s) { s.id = 40; },
      [](KindSpec& s) { s.name = "test.renamed"; },
      [](KindSpec& s) { s.version = 2; },
      [](KindSpec& s) { s.scope = Scope::kSession; },
      [](KindSpec& s) { s.tier = Tier::kEffect; },
      [](KindSpec& s) { s.state_class = StateClass::kEvent; },
      [](KindSpec& s) { s.initial = InitialPolicy::kNever; },
      [](KindSpec& s) { s.write = WriteMode::kMirrored; },
      [](KindSpec& s) { s.lane = LaneUse::kFast; },
      [](KindSpec& s) { s.models = kModelCommit; },
      [](KindSpec& s) { s.leasable = true; },
      [](KindSpec& s) { s.noisy = true; },
      [](KindSpec& s) { s.unsupported = true; },
      [](KindSpec& s) { s.max_value_bytes = 9; },
      [](KindSpec& s) { s.pending_grace_ms = 4000; },
      [&](KindSpec& s) { s.depends_on = fewer_deps; },
      [&](KindSpec& s) { s.authored_default = other_default; },
      [](KindSpec& s) { s.equal = &EqualA; },
      [](KindSpec& s) { s.reason_name = &ReasonA; },
      [](KindSpec& s) { s.parent_wait_ms = 2000; },
      [](KindSpec& s) { s.parent_missing_retryable = true; },
      [](KindSpec& s) { s.requires_precondition = true; },
      [](KindSpec& s) { s.urgent = true; },  // stage 5's urgent flush (the flag byte's last bit)
  };
  std::set<uint64_t> seen{digest};
  for (size_t i = 0; i < changes.size(); ++i) {
    std::vector<KindSpec> changed = base;
    changes[i](changed[test::kScene - 1]);  // the scene: deps, a default, models 3
    const uint64_t d = TableDigest(changed);
    if (d == digest) std::fprintf(stderr, "  change %zu left the digest unchanged\n", i);
    CHECK(d != digest);
    seen.insert(d);
  }
  CHECK(seen.size() == changes.size() + 1);  // and each change differently
  // A kind more or less.
  std::vector<KindSpec> fewer = base;
  fewer.pop_back();
  CHECK(TableDigest(fewer) != digest);
}

BACKEND_TEST("VA15", ReplicatedPredicate) {
  // Replicated: false for effects and server-only kinds, true otherwise.
  KindSpec s = Spec(1, "a");
  CHECK(Replicated(s));
  s.write = WriteMode::kApplied;
  CHECK(Replicated(s));
  s.write = WriteMode::kServerOnly;
  CHECK(!Replicated(s));
  s = Spec(1, "a");
  s.tier = Tier::kEffect;
  CHECK(!Replicated(s));
  s.tier = Tier::kDurableEvent;
  CHECK(Replicated(s));
  // Fenced: exactly the kinds with dependencies.
  for (const KindSpec& k : test::kTestTable) {
    CHECK(Fenced(k) == !k.depends_on.empty());
    CHECK(Replicated(k) == (k.id != test::kServerOnly));
  }
  CHECK(Fenced(test::kTestScene) && Fenced(test::kTestGate) && !Fenced(test::kTestToggle));

  // The users leave a server-only row out: BaselineKeys, MakeManifest, Walk, DiffMaps.
  const TimePoint t0 = TimePoint{} + 1s;
  const RecordKey tracker{test::kTracker, SessionObject(1), 0};
  const RecordKey hidden{test::kServerOnly, SessionObject(1), 0};
  const std::vector<uint8_t> value{1, 2, 3, 4};
  WorldState with(test::kTestTable, Limits{});
  WorldState without(test::kTestTable, Limits{});
  CHECK(with.Put(tracker, value, t0) == PutResult::kWritten);
  CHECK(without.Put(tracker, value, t0) == PutResult::kWritten);
  CHECK(with.Put(hidden, value, t0) == PutResult::kWritten);
  CHECK(with.Get(hidden) != nullptr);  // S keeps it

  const std::vector<RecordKey> keys = with.BaselineKeys(kScopeAll);
  CHECK(std::ranges::find(keys, hidden) == keys.end());
  CHECK(std::ranges::find(keys, tracker) != keys.end());
  CHECK(with.MakeManifest(kScopeAll).digest == without.MakeManifest(kScopeAll).digest);

  Subscription sub;
  sub.scopes = kScopeAll;
  std::vector<RecordKey> walked;
  uint64_t proven = 0;
  with.Walk(0, sub,
            [&](const WalkItem& item) {
              if (item.type == EntryType::kRecord && item.record != nullptr) walked.push_back(item.record->key);
              return true;
            },
            &proven);
  CHECK(std::ranges::find(walked, hidden) == walked.end());
  CHECK(std::ranges::find(walked, tracker) != walked.end());
  CHECK(proven == with.Head());  // passed, not withheld: the prefix still reaches the head

  MapDump a, b;
  a.who = "S";
  b.who = "peer 1";
  MapRow shared;
  shared.kind = "test.tracker";
  shared.entity = ToText(SessionObject(1));
  shared.revision = 1;
  shared.value_hex = "01020304";
  shared.digest = ValueDigest(value);
  MapRow server_only = shared;
  server_only.kind = "test.server_only";
  server_only.server_only = true;
  a.row_lines = {shared, server_only};
  b.row_lines = {shared};
  CHECK(DiffMaps(a, b).empty());
}

BACKEND_TEST("S2U3", ValidatorUrgent) {
  // V14: `urgent` only on a replicated kind. An effect (fast lane, flushed at every pump) and a
  // server-only kind (never sent) are refused; a replicated state-lane kind passes.
  KindSpec shown = Spec(1, "attack");
  shown.urgent = true;
  CHECK(Passes(std::array{shown}));
  KindSpec hidden = Spec(2, "hidden");
  hidden.write = WriteMode::kServerOnly;
  hidden.urgent = true;
  CHECK(OnlyRule(std::array{hidden}, "V14 urgent"));
  KindSpec spark = Spec(3, "spark");
  spark.tier = Tier::kEffect;
  spark.lane = LaneUse::kFast;
  spark.urgent = true;
  CHECK(OnlyRule(std::array{spark}, "V14 urgent"));
  // A table without the flag keeps the digest it had before the field: the flag takes a bit of
  // its own, and only when set.
  std::vector<KindSpec> table(test::kTestTable.begin(), test::kTestTable.end());
  const uint64_t before = TableDigest(table);
  table[test::kProp - 1].urgent = true;
  CHECK(TableDigest(table) != before);
  table[test::kProp - 1].urgent = false;
  CHECK(TableDigest(table) == before);
}
