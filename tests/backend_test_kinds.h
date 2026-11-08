#pragma once

#include <array>
#include <cstdint>

#include "backend/kind_table.h"

// The test kinds (design sections 5 and 10, VA13b): a game-free table every backend test can use.
//
//   kind              scope    tier        class    initial    write        deps            grace
//   test.toggle   1   level    structural  current  read      applied      -               5 s
//   test.gate     2   level    structural  current  default   applied      toggle          5 s
//   test.scene    3   level    structural  current  default   applied      toggle, gate    5 s
//   test.prop     4   level    structural  current  read      applied      -               5 s
//   test.event    5   level    durable     event    read      applied      -               5 s
//   test.server_only 6 session structural current  default   server-only  -               -
//   test.tracker  7   session  structural  current  default   mirrored     -               -
//   test.counter  8   session  structural  current  default   mirrored     -               -
//
// test.scene depends on test.toggle (SR6) and on test.gate, which depends on test.toggle (SR7's
// fenced chain); test.toggle and test.gate are applied with a 5 s grace (V12). test.prop is
// unfenced and unrelated to the scene (SR6e), leasable for the owner-run model. test.tracker is a
// replicated session-scope kind (session rows, LM5b, ID8). test.counter is a session-scope kind
// that takes commits (LM5b, LM10). kTestTable is the validator's table (VA13b: the first seven);
// kTestTableFull adds test.counter and is the table the facade tests run on (the lanes, request,
// obligation and limits tests). test.mover is in no table: the page's reader adds it (item 5).

namespace skylanders::backend::test {

constexpr KindId kToggle = 1;
constexpr KindId kGate = 2;
constexpr KindId kScene = 3;
constexpr KindId kProp = 4;
constexpr KindId kEvent = 5;
constexpr KindId kServerOnly = 6;
constexpr KindId kTracker = 7;
constexpr KindId kCounter = 8;

inline constexpr std::array<KindId, 1> kGateDeps{kToggle};
inline constexpr std::array<KindId, 2> kSceneDeps{kToggle, kGate};
inline constexpr std::array<uint8_t, 1> kGateDefault{0};
inline constexpr std::array<uint8_t, 1> kSceneDefault{0};
inline constexpr std::array<uint8_t, 4> kServerOnlyDefault{0, 0, 0, 0};
inline constexpr std::array<uint8_t, 4> kTrackerDefault{0, 0, 0, 0};
inline constexpr std::array<uint8_t, 4> kCounterDefault{0, 0, 0, 0};

inline constexpr KindSpec kTestToggle{
    .id = kToggle, .name = "test.toggle", .scope = Scope::kLevel, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kReadAtSpawn,
    .write = WriteMode::kApplied, .models = kModelCommit | kModelServerRun, .max_value_bytes = 8,
    .pending_grace_ms = 5000, .depends_on = {}, .authored_default = {}};
inline constexpr KindSpec kTestGate{
    .id = kGate, .name = "test.gate", .scope = Scope::kLevel, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kAuthoredDefault,
    .write = WriteMode::kApplied, .models = kModelCommit, .max_value_bytes = 8,
    .pending_grace_ms = 5000, .depends_on = kGateDeps, .authored_default = kGateDefault};
inline constexpr KindSpec kTestScene{
    .id = kScene, .name = "test.scene", .scope = Scope::kLevel, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kAuthoredDefault,
    .write = WriteMode::kApplied, .models = kModelCommit | kModelServerRun, .max_value_bytes = 16,
    .pending_grace_ms = 5000, .depends_on = kSceneDeps, .authored_default = kSceneDefault};
inline constexpr KindSpec kTestProp{
    .id = kProp, .name = "test.prop", .scope = Scope::kLevel, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kReadAtSpawn,
    .write = WriteMode::kApplied, .models = kModelCommit | kModelOwnerRun, .leasable = true,
    .max_value_bytes = 64, .pending_grace_ms = 5000, .depends_on = {}, .authored_default = {}};
inline constexpr KindSpec kTestEvent{
    .id = kEvent, .name = "test.event", .scope = Scope::kLevel, .tier = Tier::kDurableEvent,
    .state_class = StateClass::kEvent, .initial = InitialPolicy::kReadAtSpawn,
    .write = WriteMode::kApplied, .max_value_bytes = 16, .pending_grace_ms = 5000,
    .depends_on = {}, .authored_default = {}};
inline constexpr KindSpec kTestServerOnly{
    .id = kServerOnly, .name = "test.server_only", .scope = Scope::kSession,
    .tier = Tier::kStructural, .state_class = StateClass::kCurrentValue,
    .initial = InitialPolicy::kAuthoredDefault, .write = WriteMode::kServerOnly,
    .max_value_bytes = 8, .depends_on = {}, .authored_default = kServerOnlyDefault};
inline constexpr KindSpec kTestTracker{
    .id = kTracker, .name = "test.tracker", .scope = Scope::kSession, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kAuthoredDefault,
    .write = WriteMode::kMirrored, .max_value_bytes = 32, .depends_on = {},
    .authored_default = kTrackerDefault};

inline constexpr std::array<KindSpec, 7> kTestTable{kTestToggle, kTestGate,       kTestScene,
                                                    kTestProp,   kTestEvent,      kTestServerOnly,
                                                    kTestTracker};

inline constexpr KindSpec kTestCounter{
    .id = kCounter, .name = "test.counter", .scope = Scope::kSession, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kAuthoredDefault,
    .write = WriteMode::kMirrored, .models = kModelCommit, .max_value_bytes = 8,
    .depends_on = {}, .authored_default = kCounterDefault};
inline constexpr std::array<KindSpec, 8> kTestTableFull{
    kTestToggle, kTestGate,       kTestScene,   kTestProp,
    kTestEvent,  kTestServerOnly, kTestTracker, kTestCounter};

}  // namespace skylanders::backend::test
