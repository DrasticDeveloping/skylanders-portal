#pragma once

#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "backend/kind_table.h"
#include "backend/lease.h"
#include "backend/obligation.h"
#include "backend/world_state.h"

// The map dump (design section 7.1): one header and one line per row, sorted by scope, kind name,
// entity text and slot, so two dumps diff line by line. The format is fixed here and pinned by the
// golden dump tests/backend/golden/map.txt (compared, never rewritten, except under
// skylanders_backend_test --update-golden; Opus F15):
//
//   map <S|peer n> head <lsn> prefix <lsn> epoch <g> rows <n> bytes <b> table <digest>
//   row <kind> <entity> slot <n> rev <r> lsn <l> epoch <g> writer <w> [tomb] [server-only] value <hex> digest <hex>
//   obligation <id> to <peer> <kind> <entity> lsn <l> <state>
//   lease <id> <kind> <entity> holder <peer> <state> left <ms>
//   peer <n> cursor <lsn> prefix <lsn> unacked <frames> baseline <id|-> <why|-> since <session lsn|-> <level lsn|-> credit <used> waiting <n>
//
// Only replicated rows are dumped for a peer; S's dump marks a server-only row `server-only` and
// DiffMaps leaves it out (Opus F5). The function names are the design's; the option and result
// structs and the line helpers are the header's own.
//
// The header's own choices: digests are 16 lowercase hex digits; a value is lowercase hex, `-` when
// empty (a tomb); `rows` and `bytes` count the row lines and their value bytes as dumped; a word of
// more than one (a state, a baseline reason) is written with `-` for its spaces, so every field is
// one token. A kind not in the table is written `#<id>`.

namespace skylanders::backend {

struct PeerStats;  // server.h

// The lines a dump carries besides the rows; the caller (the façade or a test) fills them, with the
// line helpers below.
struct MapDumpOptions {
  std::string who = "S";  // "S" or "peer <n>"
  // S writes its Head(). A client: 0 writes the replica's Prefix().
  uint64_t prefix = 0;
  std::vector<std::string> obligation_lines;  // "obligation ..." lines, without the newline
  std::vector<std::string> lease_lines;       // "lease ..." lines
  std::vector<std::string> peer_lines;        // "peer ..." lines
};

// S: head is Head(), epoch WorldState::Epoch(). A client: head is the highest LSN among the rows
// it holds, epoch Replica::Epoch().
std::string DumpMap(const WorldState& state, const MapDumpOptions& options);
std::string DumpMap(const Replica& replica, const MapDumpOptions& options);

// The other lines, in the dump's words. `state` is the obligation's state as the caller knows it
// ("waiting", "sent", or an ObligationEndName).
std::string ObligationLine(const Obligation& o, std::span<const KindSpec> table,
                           std::string_view state);
std::string LeaseLine(const Lease& lease, std::span<const KindSpec> table, TimePoint now);
std::string PeerLine(uint16_t peer, const PeerStats& stats);

struct MapRow {
  std::string kind;
  std::string entity;
  uint16_t slot = 0;
  uint32_t revision = 0;
  uint64_t lsn = 0;
  uint32_t epoch = 0;
  uint16_t writer = 0;
  bool tomb = false;
  bool server_only = false;
  std::string value_hex;  // "" for an empty value (written `-`)
  uint64_t digest = 0;
};
struct MapDump {
  std::string who;
  uint64_t head = 0, prefix = 0;
  uint32_t epoch = 0;
  size_t rows = 0, bytes = 0;
  uint64_t table = 0;
  std::vector<MapRow> row_lines;
  std::vector<std::string> other_lines;  // obligation, lease and peer lines, as written
};
// nullopt with `*error` set ("line <n>: <what>") for a line it does not know, a field it cannot
// read, a digest that is not its value's, or a row count that is not the header's.
std::optional<MapDump> ParseMap(std::string_view text, std::string* error);

struct MapDiff {
  enum class What : uint8_t { kOnlyA, kOnlyB, kRevision, kTomb, kValue };
  What what = What::kValue;
  std::string kind;
  std::string entity;
  uint16_t slot = 0;
  std::string text;  // one line: "<kind> <entity> slot <n> <what> a <...> b <...>"
};
// "only-a", "only-b", "revision", "tomb", "value" (the <what> of a diff line).
std::string_view MapDiffName(MapDiff::What what);
// Row differences between two dumps, one per key at most (tomb, then revision, then value), in
// (kind, entity, slot) order (lsn, epoch and writer are not compared: two peers' logs differ
// there). Server-only rows are left out.
std::vector<MapDiff> DiffMaps(const MapDump& a, const MapDump& b);

}  // namespace skylanders::backend
