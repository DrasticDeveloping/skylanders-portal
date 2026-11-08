#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "backend/limits.h"
#include "backend/record.h"

// The kind table and its validator (design section 3.4). A game's table is a C++ array of KindSpec
// checked by Validate in a unit test; no generator (D9). Plan 3.2 names the fields (tier, state
// class, initial policy, write mode, lanes, dependencies) and three validator rules; the others are
// the design's.

namespace skylanders::backend {

enum class Tier : uint8_t { kStructural, kDurableEvent, kEffect };

// The update semantics: how a new value meets the stored one (Q6, section 12). Draft 2's kCounter
// is gone: while S is the only writer it behaved exactly as a current value (Opus Q6).
enum class StateClass : uint8_t {
  kCurrentValue = 0,  // the newer revision replaces the stored one (a gate, a prop, a count S rewrites)
  kEvent = 3,         // once-only: delivered as an obligation, never replaced (a chest's coins)
  // 1 is reserved for stage 4's client-owned value (PlayerRecord, Q7); 2 for a merge-on-S class
  // (an add) when a kind needs one. The validator refuses both (V9).
};

enum class InitialPolicy : uint8_t { kUnset, kReadAtSpawn, kAuthoredDefault, kNever };

enum class WriteMode : uint8_t {
  kUnset,
  kApplied,     // a client's adapter writes the value into its world
  kMirrored,    // a client keeps it and shows it (a debug menu, the party panel); no world write
  kServerOnly,  // S keeps it; never sent
};

enum class LaneUse : uint8_t { kState, kFast };  // records ride state; an effect may ride fast

// The request models a kind allows (KindSpec::models).
enum ModelBits : uint8_t { kModelCommit = 1, kModelServerRun = 2, kModelOwnerRun = 4 };

struct KindSpec {
  KindId id = 0;
  std::string_view name;            // "prop", "gate", "kill_count"
  uint16_t version = 1;             // bumps with the value's layout
  Scope scope = Scope::kLevel;
  Tier tier = Tier::kStructural;
  StateClass state_class = StateClass::kCurrentValue;
  InitialPolicy initial = InitialPolicy::kUnset;
  WriteMode write = WriteMode::kUnset;
  LaneUse lane = LaneUse::kState;
  uint8_t models = 0;               // request models a request of this kind may ask for (ModelBits)
  bool leasable = false;            // kModelOwnerRun needs it
  bool noisy = false;               // coalesced to limits.noisy_hz per row
  bool unsupported = false;         // listed as unsupported (plan decision 5); needs kNever
  uint16_t max_value_bytes = 0;
  // How long a row may wait for its entity (or retry a failed apply) before it is late; the fence
  // stops waiting then (Sol F2).
  uint32_t pending_grace_ms = 0;
  std::span<const KindId> depends_on;  // applied after these, in a batch and in a baseline
  std::span<const uint8_t> authored_default;
  // null: byte equality.
  bool (*equal)(std::span<const uint8_t>, std::span<const uint8_t>) = nullptr;
  // The kind's own refusal words for its kind_reason byte (D11).
  std::string_view (*reason_name)(uint8_t kind_reason) = nullptr;
  // Requests whose parent never arrives (section 3.9): held this long, then this outcome.
  uint32_t parent_wait_ms = 0;
  bool parent_missing_retryable = false;  // true: refused retryable (1B's exit); false: terminal
                                          // (1B's touch)
  // A request of this kind with precondition 0 is refused `unsupported` by the table right after
  // the key cap, before the parent checks, admission and so the precondition wait (1B: pose 0 on
  // anything but a skip, requests.cpp:245-249; Opus r2 N2). A scene-skip kind leaves it unset
  // (Opus F11).
  bool requires_precondition = false;
};

// Whether a kind's rows leave S at all: not an effect, not server-only. The one predicate the delta
// walk, baselines, manifests and DiffMaps use (Opus F5).
bool Replicated(const KindSpec& spec);
// Whether a client holds a row of this kind until its prefix covers the row's LSN and every earlier
// dependency row is settled (applied, dropped or late): true exactly for a kind with dependencies
// (Sol F2, section 3.5).
bool Fenced(const KindSpec& spec);

// The spec with this id in `table`; nullptr if none.
const KindSpec* FindKind(std::span<const KindSpec> table, KindId id);

// One broken rule. `rule` is the rule's short name ("V1 ids" ... "V13 replicated deps").
//   V1  ids: every id non-zero and unique; every name non-empty and unique
//   V2  initial: `initial` is set
//   V3  never: kNever has no kApplied write mode, and `unsupported` is set; `unsupported` needs
//       kNever
//   V4  cycles: every dependency exists, none is the kind itself, the graph has no cycle
//   V5  scope: a session-scope kind depends only on session-scope kinds
//   V6  events: kDurableEvent if and only if kEvent
//   V7  lanes: kFast only for kEffect; an effect is never in a baseline or a manifest
//   V8  default: kAuthoredDefault has a default of at most max_value_bytes; no other policy has one
//   V9  sizes: 0 < max_value_bytes <= limits.max_value_bytes; the state class is a known value
//       (1 and 2, the reserved classes, are refused)
//   V10 models: kModelOwnerRun needs `leasable`; kServerOnly takes no kModelOwnerRun;
//       parent_wait_ms and requires_precondition only on a kind that takes requests (Opus F11)
//   V11 noisy: `noisy` only on a current value
//   V12 grace: an applied kind read at spawn has pending_grace_ms > 0; so has an applied kind
//       another kind depends on, so a fence never breaks on its first wait (Sol F2, draft 4)
//   V13 replicated deps: a replicated kind depends only on replicated kinds (a fenced row must
//       never wait for a row that is never sent; Sol F2, Opus F5)
struct ValidationIssue {
  KindId kind = 0;
  std::string_view rule;
  std::string text;
};
std::vector<ValidationIssue> Validate(std::span<const KindSpec> table, const Limits& limits = {});

// Goes in StateHello. Hashes every data field and the contents of depends_on and
// authored_default, sorted by id; never a function pointer or a span's address, which differ
// between builds. Whether `equal` or `reason_name` is set is hashed as a bit.
uint64_t TableDigest(std::span<const KindSpec> table);

}  // namespace skylanders::backend
