#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "backend/adapter.h"
#include "backend/base.h"
#include "backend/record.h"
#include "backend/world_state.h"

// The compare queries (design section 7.2): the stored record and the live value read back through
// the adapter, on S and on each client, after a quiet period (plan 2.5). Split in two halves, since
// S and each client are separate processes from stage 3: a local compare on each peer, and a join
// across peers. Digest equality alone proves nothing: kEqual needs the live read on the peer as
// well as the joined revision and digest.
//
// The game's lines (from stage 3; LocalLine, CompareLine and ComparedLine below write them):
//   Online: backend local <peer> <kind> <entity> slot <n> rev <r> digest <hex> <equal|repaired|pending|stuck|unsupported>[ pending <ms>][ differs <what>]
//   Online: backend compare <peer> <kind> <entity> <equal|repaired|pending|stuck|unsupported> rev <s>/<r>[ differs <what>]
//   Online: backend compared <peer> <kind> equal <n> repaired <n> pending <n> stuck <n> unsupported <n>

namespace skylanders::backend {

enum class CompareResult : uint8_t { kEqual, kRepaired, kPending, kStuck, kUnsupported };
std::string_view CompareResultName(CompareResult r);  // "equal", "repaired", ...

// Half 1, on one peer: this peer's own copy (S: its WorldState; a client: its Replica) against the
// live value read through its adapter. Repair is one Apply and one Read again.
struct LocalCompare {
  RecordKey key;
  uint32_t revision = 0;  // the stored (S) or replica (client) revision
  uint64_t digest = 0;    // ValueDigest of the stored or replica value
  enum class Live : uint8_t { kEqual, kRepaired, kPending, kStuck, kUnsupported } live = Live::kEqual;
  Duration pending_for{};  // kPending: how long the entity has been missing
  std::string differs;     // the kind's own words for what differs, when it knows them
};
//
// One line per row of `kind` the copy holds (tombs included), in key order. The live result:
//   a kind listed unsupported (kNever), or the adapter's Read answering kUnsupported -> kUnsupported
//   a kind with no adapter: written by no adapter on this peer (mirrored, server-only) -> kEqual,
//     the copy is the value; applied -> kStuck "no adapter"
//   a tomb: its entity not live (gone, pending, unresolved) -> kEqual; live -> kStuck "tomb live",
//     or with `repair` one Retire and one Resolve again -> kRepaired when it is gone
//   the entity pending (not spawned, streamed out) -> kPending, pending_for = now - since; past
//     the kind's pending_grace_ms (Limits' default when the kind has none) -> kStuck
//     "pending <ms>" (section 3.5: pending, then stuck; SR6c)
//   ambiguous, unresolved, gone, or a Read that fails -> kStuck "ambiguous", "unresolved", "gone",
//     "read failed"
//   the live value equal to the copy's (the kind's `equal`, or bytes) -> kEqual; not equal ->
//     kStuck "value <live digest>", or with `repair` one Apply and one Read again -> kRepaired when
//     it is equal then
// `now` (the header's own; the design's signature has none) measures pending_for and the grace;
// the default leaves pending_for 0 and never turns a pending row stuck.
std::vector<LocalCompare> CompareLocal(const WorldState& s, AdapterSet& live, KindId kind,
                                       bool repair, TimePoint now = {});
std::vector<LocalCompare> CompareLocal(const Replica& r, AdapterSet& live, KindId kind,
                                       bool repair, TimePoint now = {});

// Half 2, anywhere: S's lines and one peer's lines, joined over the UNION of their keys (Sol F5).
// After the quiet period:
//   a key on S only                           -> kStuck "replica missing"
//   a key on the client only                  -> kStuck "replica extra"
//   revision or digest differs                -> kStuck "replica behind" or "replica differs"
//   both kUnsupported (a kNever kind)         -> kUnsupported
//   exactly one side kUnsupported             -> kStuck "server: unsupported" or "client:
//                                                unsupported" (Sol r2 N3: the two peers disagree
//                                                on whether the kind can be read at all)
//   else the worse of S's live and the client's live result, in the order
//   kStuck > kPending > kRepaired > kEqual; `differs` names the side ("server: ...", "client: ...").
// So S's adapter reporting stuck makes the row stuck even when the client's live read is equal.
struct RowCompare {
  RecordKey key;
  CompareResult result = CompareResult::kEqual;
  uint32_t stored_revision = 0, replica_revision = 0;
  Duration pending_for{};
  std::string differs;
};
std::vector<RowCompare> JoinCompare(std::span<const LocalCompare> server,
                                    std::span<const LocalCompare> peer);

struct CompareSummary {
  KindId kind = 0;
  size_t equal = 0, repaired = 0, pending = 0, stuck = 0, unsupported = 0;
};
// Counts one kind's joined rows (the `compared` line). The design names the struct only.
CompareSummary Summarize(KindId kind, std::span<const RowCompare> rows);

// The three lines above, without the game's "Online: " prefix (D8: the formats are the library's,
// pinned by tests/backend/golden/compare.txt). A digest is 16 lowercase hex digits, a pending time
// whole milliseconds. The header's own helpers.
std::string LocalLine(uint16_t peer, std::string_view kind_name, const LocalCompare& c);
std::string CompareLine(uint16_t peer, std::string_view kind_name, const RowCompare& r);
std::string ComparedLine(uint16_t peer, std::string_view kind_name, const CompareSummary& s);

}  // namespace skylanders::backend
