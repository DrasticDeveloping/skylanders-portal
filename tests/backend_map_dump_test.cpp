// The map dump, its parser and its diff (design section 7.1; tests MD1 and MD2 of section 10). MD3,
// the Python twin's test, is item 6's (Opus F16).

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <iterator>
#include <optional>
#include <string>
#include <vector>

#include "backend/entity_id.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/map_dump.h"
#include "backend/server.h"
#include "backend/world_state.h"
#include "backend_test.h"

using namespace skylanders::backend;
using namespace std::chrono_literals;

namespace {

const TimePoint kT0 = TimePoint{} + 10h;

// A session-scope mirrored kind, a level-scope applied kind and a server-only one: the three ways a
// row shows in a dump. Their own table, so the golden file never moves with the shared test kinds.
const KindSpec kMdTable[] = {
    {.id = 1,
     .name = "md.gate",
     .scope = Scope::kLevel,
     .tier = Tier::kStructural,
     .initial = InitialPolicy::kReadAtSpawn,
     .write = WriteMode::kApplied,
     .max_value_bytes = 8,
     .pending_grace_ms = 5000,
     .depends_on = {},
     .authored_default = {}},
    {.id = 2,
     .name = "md.count",
     .scope = Scope::kSession,
     .tier = Tier::kStructural,
     .initial = InitialPolicy::kReadAtSpawn,
     .write = WriteMode::kMirrored,
     .max_value_bytes = 8,
     .depends_on = {},
     .authored_default = {}},
    {.id = 3,
     .name = "md.secret",
     .scope = Scope::kLevel,
     .tier = Tier::kStructural,
     .initial = InitialPolicy::kReadAtSpawn,
     .write = WriteMode::kServerOnly,
     .max_value_bytes = 8,
     .depends_on = {},
     .authored_default = {}},
};
constexpr KindId kGate = 1, kCount = 2, kSecret = 3;

EntityId GateA() { return Authored(1, 0x10, HashName("gate_a"), HashName("Gate")); }
EntityId GateB() { return Authored(1, 0x11, HashName("gate_b"), HashName("Gate")); }
EntityId GateC() { return Authored(1, 0x12, HashName("gate_c"), HashName("Gate")); }
EntityId Vault() { return Authored(1, 0x20, HashName("vault"), HashName("Vault")); }

RecordKey Key(KindId kind, const EntityId& entity, uint16_t slot = 0) {
  return RecordKey{kind, entity, slot};
}

// S after one generation of play: LSN 1 the epoch, 2 to 8 the rows (a gate written twice, a gate
// removed, two session slots, a server-only row).
WorldState MakeState() {
  WorldState s(kMdTable, Limits{});
  s.BeginEpoch(1);                                                        // LSN 1
  CHECK(s.Put(Key(kGate, GateA()), std::vector<uint8_t>{0x01}, kT0) == PutResult::kWritten);  // 2
  CHECK(s.Put(Key(kGate, GateB()), std::vector<uint8_t>{0x00}, kT0) == PutResult::kWritten);  // 3
  CHECK(s.Put(Key(kCount, Player(1, 1), 0), std::vector<uint8_t>{0x05, 0x00}, kT0) ==
        PutResult::kWritten);  // 4
  CHECK(s.Put(Key(kCount, Player(1, 1), 1), std::vector<uint8_t>{0x07, 0x00}, kT0) ==
        PutResult::kWritten);  // 5
  CHECK(s.Put(Key(kSecret, Vault()), std::vector<uint8_t>{0xab, 0xcd}, kT0) ==
        PutResult::kWritten);  // 6
  CHECK(s.Put(Key(kGate, GateA()), std::vector<uint8_t>{0x02}, kT0 + 100ms) ==
        PutResult::kWritten);  // 7
  CHECK(s.Remove(Key(kGate, GateB()), kT0 + 200ms) == PutResult::kWritten);  // 8
  return s;
}

// The other lines, through the line helpers, so the golden file pins their format too.
MapDumpOptions GoldenOptions() {
  MapDumpOptions o;
  Obligation ob;
  ob.id = 1;
  ob.recipient = 1;
  ob.key = Key(kGate, GateA());
  ob.lsn = 9;
  ob.scope = Scope::kLevel;
  ob.sent = true;
  o.obligation_lines.push_back(ObligationLine(ob, kMdTable, "sent"));

  Lease lease;
  lease.id = 1;
  lease.spec.entity = GateA();
  lease.spec.kind = kGate;
  lease.spec.holder = 1;
  lease.spec.term = 3s;
  lease.state = LeaseState::kGranted;
  lease.expires = kT0 + 3s;
  o.lease_lines.push_back(LeaseLine(lease, kMdTable, kT0 + 1s));

  PeerStats quiet;
  quiet.cursor = 8;
  quiet.prefix = 8;
  quiet.subscription.scopes = kScopeAll;
  quiet.subscription.since = {4, 1};
  o.peer_lines.push_back(PeerLine(1, quiet));

  PeerStats joining;
  joining.cursor = 3;
  joining.prefix = 2;
  joining.unacked_frames = 4;
  joining.subscription.scopes = kScopeSession;
  joining.subscription.since = {2, 0};
  joining.baseline = 2;
  joining.baseline_why = BaselineReason::kJoin;
  joining.credit_used = 1;
  joining.obligations_waiting = 2;
  o.peer_lines.push_back(PeerLine(2, joining));
  return o;
}

Record WithValue(Record r, std::vector<uint8_t> value) {
  r.value = std::move(value);
  return r;
}

const MapDiff* FindDiff(const std::vector<MapDiff>& diffs, std::string_view kind,
                        const EntityId& entity, uint16_t slot) {
  for (const MapDiff& d : diffs) {
    if (d.kind == kind && d.entity == ToText(entity) && d.slot == slot) return &d;
  }
  return nullptr;
}

}  // namespace

BACKEND_TEST("MD1", MapDumpGolden) {
  CHECK(Validate(kMdTable).empty());
  const WorldState s = MakeState();
  const std::string dump = DumpMap(s, GoldenOptions());
  // Compared, never written here: only --update-golden writes it (Opus F15).
  test::CheckGolden("map.txt", dump);

  // The format's own promises, whatever the golden file says.
  std::string error;
  const std::optional<MapDump> parsed = ParseMap(dump, &error);
  if (!parsed) {
    CHECK(parsed.has_value());
    std::fprintf(stderr, "  %s\n", error.c_str());
    return;
  }
  CHECK(parsed->who == "S");
  CHECK(parsed->head == s.Head());
  CHECK(parsed->prefix == s.Head());
  CHECK(parsed->epoch == 1);
  CHECK(parsed->rows == 5);
  CHECK(parsed->bytes == 7);
  CHECK(parsed->table == TableDigest(kMdTable));
  CHECK(parsed->other_lines.size() == 4);
  // Sorted by scope (session first), kind name, entity text, slot; each line is its row.
  const RecordKey order[] = {Key(kCount, Player(1, 1), 0), Key(kCount, Player(1, 1), 1),
                             Key(kGate, GateA()), Key(kGate, GateB()), Key(kSecret, Vault())};
  CHECK(parsed->row_lines.size() == std::size(order));
  for (size_t i = 0; i < parsed->row_lines.size() && i < std::size(order); ++i) {
    const MapRow& row = parsed->row_lines[i];
    const Record* stored = s.Get(order[i]);
    CHECK(stored != nullptr);
    if (stored == nullptr) continue;
    CHECK(row.entity == ToText(order[i].entity));
    CHECK(row.slot == order[i].slot);
    CHECK(stored->revision == row.revision);
    CHECK(stored->lsn == row.lsn);
    CHECK(stored->epoch == row.epoch);
    CHECK(stored->tomb == row.tomb);
    CHECK(ValueDigest(stored->value) == row.digest);
    CHECK(row.server_only == (order[i].kind == kSecret));
  }
  if (parsed->row_lines.size() == std::size(order)) {
    CHECK(parsed->row_lines[3].value_hex.empty());
    CHECK(parsed->row_lines[4].value_hex == "abcd");
  }
  // The same state dumps the same text.
  CHECK(DumpMap(MakeState(), GoldenOptions()) == dump);
}

BACKEND_TEST("MD2", MapDumpParseDiff) {
  WorldState s = MakeState();
  CHECK(s.Put(Key(kCount, Player(2, 1)), std::vector<uint8_t>{0x01}, kT0 + 300ms) ==
        PutResult::kWritten);  // 9

  // A client that missed some of it: count slot 1 missing, gate A one revision behind, gate B not
  // removed, gate C extra, player 2's count at the right revision with another value.
  Replica r(kMdTable, Limits{});
  r.OnEpoch(1, 1);
  const Record count0 = *s.Get(Key(kCount, Player(1, 1), 0));
  CHECK(r.OnRecord(count0, count0.lsn) == ApplyResult::kApplied);
  Record gate_a_old = *s.Get(Key(kGate, GateA()));
  gate_a_old.revision = 1;
  gate_a_old.lsn = 2;
  gate_a_old.value = {0x01};
  CHECK(r.OnRecord(gate_a_old, 2) == ApplyResult::kApplied);
  Record gate_b_live = *s.Get(Key(kGate, GateB()));
  gate_b_live.tomb = false;
  gate_b_live.value = {0x00};
  CHECK(r.OnRecord(gate_b_live, gate_b_live.lsn) == ApplyResult::kApplied);
  Record gate_c;
  gate_c.key = Key(kGate, GateC());
  gate_c.revision = 1;
  gate_c.epoch = 1;
  gate_c.lsn = 3;
  gate_c.value = {0x01};
  CHECK(r.OnRecord(gate_c, 3) == ApplyResult::kApplied);
  const Record p2 = *s.Get(Key(kCount, Player(2, 1)));
  CHECK(r.OnRecord(WithValue(p2, {0x09}), p2.lsn) == ApplyResult::kApplied);
  // A server-only row never reaches a replica.
  CHECK(r.OnRecord(*s.Get(Key(kSecret, Vault())), 6) == ApplyResult::kUnknownKind);

  MapDumpOptions peer;
  peer.who = "peer 1";
  const std::string a_text = DumpMap(s, MapDumpOptions{});
  const std::string b_text = DumpMap(r, peer);
  CHECK(b_text.find("server-only") == std::string::npos);
  CHECK(b_text.rfind("map peer 1 head 9 prefix 0 epoch 1 rows 5 ", 0) == 0);

  std::string error;
  const std::optional<MapDump> a = ParseMap(a_text, &error);
  const std::optional<MapDump> b = ParseMap(b_text, &error);
  CHECK(a.has_value() && b.has_value());
  if (!a || !b) return;
  CHECK(a->rows == 6 && b->rows == 5);
  CHECK(b->who == "peer 1");

  // Nothing differs from itself; the server-only row is never a difference.
  CHECK(DiffMaps(*a, *a).empty());
  MapDump a_only_secret = *a;
  a_only_secret.row_lines.clear();
  for (const MapRow& row : a->row_lines) {
    if (row.server_only) a_only_secret.row_lines.push_back(row);
  }
  MapDump empty = *a;
  empty.row_lines.clear();
  CHECK(DiffMaps(a_only_secret, empty).empty());

  const std::vector<MapDiff> diffs = DiffMaps(*a, *b);
  CHECK(diffs.size() == 5);
  const MapDiff* d = FindDiff(diffs, "md.count", Player(1, 1), 1);
  CHECK(d != nullptr && d->what == MapDiff::What::kOnlyA);
  if (d) CHECK(d->text == "md.count p1/1 slot 1 only-a a rev 1 b -");
  d = FindDiff(diffs, "md.count", Player(2, 1), 0);
  CHECK(d != nullptr && d->what == MapDiff::What::kValue);
  if (d) {
    CHECK(d->text.rfind("md.count p2/1 slot 0 value a ", 0) == 0);
    CHECK(d->text.find(" b ") != std::string::npos);
  }
  d = FindDiff(diffs, "md.gate", GateA(), 0);
  CHECK(d != nullptr && d->what == MapDiff::What::kRevision);
  if (d) CHECK(d->text == "md.gate " + ToText(GateA()) + " slot 0 revision a 2 b 1");
  d = FindDiff(diffs, "md.gate", GateB(), 0);
  CHECK(d != nullptr && d->what == MapDiff::What::kTomb);
  if (d) CHECK(d->text == "md.gate " + ToText(GateB()) + " slot 0 tomb a tomb b live");
  d = FindDiff(diffs, "md.gate", GateC(), 0);
  CHECK(d != nullptr && d->what == MapDiff::What::kOnlyB);
  if (d) CHECK(d->text == "md.gate " + ToText(GateC()) + " slot 0 only-b a - b rev 1");
  // In (kind, entity, slot) order.
  if (diffs.size() == 5) {
    CHECK(diffs[0].kind == "md.count" && diffs[1].kind == "md.count");
    CHECK(diffs[2].kind == "md.gate" && diffs[4].kind == "md.gate");
  }
  // lsn, epoch and writer are not compared.
  MapDump moved = *a;
  for (MapRow& row : moved.row_lines) {
    row.lsn += 100;
    row.writer = 7;
  }
  CHECK(DiffMaps(*a, moved).empty());

  // The parser refuses what it does not know, with the line.
  const auto refuse = [](std::string_view text, std::string_view expected) {
    std::string why;
    const bool refused = !ParseMap(text, &why).has_value();
    CHECK(refused);
    if (refused && why.rfind(expected, 0) != 0) {
      std::fprintf(stderr, "  error was '%s', expected '%.*s...'\n", why.c_str(),
                   int(expected.size()), expected.data());
      CHECK(why.rfind(expected, 0) == 0);
    }
  };
  const std::string header = "map S head 1 prefix 1 epoch 0 rows 0 bytes 0 table 0000000000000000\n";
  refuse(header + "bogus 1 2 3\n", "line 2: unknown line 'bogus'");
  refuse("row md.gate x1 slot 0 rev 1 lsn 1 epoch 0 writer 0 value 01 digest 0\n",
         "line 1: a line before the map header");
  refuse("map Q head 1\n", "line 1: who");
  refuse("map S head x prefix 1 epoch 0 rows 0 bytes 0 table 0\n", "line 1: head");
  refuse(header + header, "line 2: a second map header");
  // A digest that is not the value's.
  refuse("map S head 1 prefix 1 epoch 0 rows 1 bytes 1 table 0000000000000000\n"
         "row md.count x1 slot 0 rev 1 lsn 1 epoch 0 writer 0 value 01 digest 0000000000000001\n",
         "line 2: digest is not the value's");
  // A row count that is not the header's.
  refuse("map S head 1 prefix 1 epoch 0 rows 2 bytes 0 table 0000000000000000\n", "line ");
  refuse("", "line ");
  // CRLF line ends and blank lines are read.
  std::string crlf;
  for (const char c : a_text) {
    if (c == '\n') crlf += "\r\n\r\n";
    else crlf += c;
  }
  const std::optional<MapDump> again = ParseMap(crlf, &error);
  CHECK(again.has_value());
  if (again) CHECK(DiffMaps(*a, *again).empty() && again->rows == a->rows);
}
