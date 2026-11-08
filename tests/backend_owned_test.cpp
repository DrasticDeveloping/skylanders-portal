// Stage 4's owned-row admission (docs/online-4.md section 4.3): the six checks in order, the forged
// key with an honest writer, the forged owner flag, the delayed delta after leave and rejoin, the
// tombs, the refusal line's once-per-second rule, OwnedDelta's wire form, and the storage bounds
// under churn and floods, and a refused WorldState write (Sol stage 4 F2).

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <variant>
#include <vector>

#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/limits.h"
#include "backend/owned.h"
#include "backend/world_state.h"
#include "backend_test.h"

#ifndef REQUIRE_OR_RETURN_VOID
#define REQUIRE_OR_RETURN_VOID(cond)   do {                                   if (!(cond)) {                         CHECK(cond);                         return;                            }                                  } while (0)
#endif

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;

constexpr KindId kPlayerKind = 40;
constexpr KindId kInfoKind = 41;   // player_info: S-written, a current value
constexpr KindId kLevelOwned = 42;  // a broken table's owned kind at level scope (V15 refuses it;
                                    // admission must still refuse its rows by check 3)

KindSpec Kind(KindId id, std::string_view name, StateClass cls, Scope scope) {
  KindSpec s;
  s.id = id;
  s.name = name;
  s.scope = scope;
  s.tier = Tier::kStructural;
  s.state_class = cls;
  s.initial = InitialPolicy::kReadAtSpawn;
  s.write = WriteMode::kMirrored;
  s.lane = LaneUse::kState;
  s.max_value_bytes = 16;
  return s;
}

const std::array<KindSpec, 3> kTable = {
    Kind(kPlayerKind, "player", StateClass::kOwnedValue, Scope::kSession),
    Kind(kInfoKind, "player_info", StateClass::kCurrentValue, Scope::kSession),
    Kind(kLevelOwned, "level_owned", StateClass::kOwnedValue, Scope::kLevel),
};

Record Row(KindId kind, uint16_t peer, uint32_t peer_epoch, uint32_t revision,
           std::vector<uint8_t> value = {1, 2, 3}) {
  Record r;
  r.key = RecordKey{kind, Player(peer, peer_epoch), 0};
  r.revision = revision;
  r.value = std::move(value);
  return r;
}

// A codec that refuses a value starting with 0xFF (an unknown state enum, say).
bool Schema(KindId, std::span<const uint8_t> value) { return value.empty() || value[0] != 0xFF; }

BACKEND_TEST("OW1", OwnedSixChecksInOrder) {
  OwnedAdmission a(kTable, &Schema);
  // Admitted, then a newer revision admitted.
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 1)) == OwnedVerdict::kAdmitted);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 2)) == OwnedVerdict::kAdmitted);
  // 1: not an owned kind. The forged owner flag: an OwnedDelta of kind player_info.
  CHECK(a.Admit(2, 1, Row(kInfoKind, 2, 1, 1)) == OwnedVerdict::kNotOwned);
  CHECK(a.Admit(2, 1, Row(999, 2, 1, 1)) == OwnedVerdict::kNotOwned);
  // 2: a write from another peer's link for this peer's key, and the forged key with an honest
  // writer (peer 2's link writes a row keyed by peer 3's Player).
  CHECK(a.Admit(3, 1, Row(kPlayerKind, 2, 1, 3)) == OwnedVerdict::kWrongPlayer);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 3, 1, 1)) == OwnedVerdict::kWrongPlayer);
  Record slot = Row(kPlayerKind, 2, 1, 3);
  slot.key.slot = 1;
  CHECK(a.Admit(2, 1, slot) == OwnedVerdict::kWrongPlayer);
  // 3: scope. A level-scope owned kind, a non-zero epoch, a client's tomb.
  CHECK(a.Admit(2, 1, Row(kLevelOwned, 2, 1, 1)) == OwnedVerdict::kScope);
  Record epoch = Row(kPlayerKind, 2, 1, 3);
  epoch.epoch = 4;
  CHECK(a.Admit(2, 1, epoch) == OwnedVerdict::kScope);
  Record tomb = Row(kPlayerKind, 2, 1, 3, {});
  tomb.tomb = true;
  CHECK(a.Admit(2, 1, tomb) == OwnedVerdict::kScope);
  // 4: stale (equal, older, zero).
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 2)) == OwnedVerdict::kStale);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 1)) == OwnedVerdict::kStale);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 4, 0)) == OwnedVerdict::kWrongPlayer);  // 2 before 4
  CHECK(a.Admit(2, 4, Row(kPlayerKind, 2, 4, 0)) == OwnedVerdict::kStale);
  // 5: size, checked after the revision: an oversize stale row is stale.
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 2, std::vector<uint8_t>(17, 1))) == OwnedVerdict::kStale);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 3, std::vector<uint8_t>(17, 1))) == OwnedVerdict::kSize);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 3, std::vector<uint8_t>(16, 1))) == OwnedVerdict::kAdmitted);
  // 6: schema. A refused row does not move the revision: the next one at 4 is admitted.
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 4, {0xFF})) == OwnedVerdict::kSchema);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 4, {0x01})) == OwnedVerdict::kAdmitted);
  CHECK(a.Count(OwnedVerdict::kAdmitted) == 4);
  CHECK(a.Count(OwnedVerdict::kStale) == 4);
}

BACKEND_TEST("OW2", OwnedTombAndRejoin) {
  OwnedAdmission a(kTable);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 5)) == OwnedVerdict::kAdmitted);
  CHECK(a.Admit(3, 1, Row(kPlayerKind, 3, 1, 1)) == OwnedVerdict::kAdmitted);
  // Peer 2 leaves: its epoch's row is tombed, peer 3's untouched; a second end tombs nothing.
  const std::vector<RecordKey> tombs = a.EndPeerEpoch(2, 1);
  REQUIRE_OR_RETURN_VOID(tombs.size() == 1);
  CHECK(tombs[0] == (RecordKey{kPlayerKind, Player(2, 1), 0}));
  CHECK(a.EndPeerEpoch(2, 1).empty());
  // The delayed delta after leave and rejoin: the old epoch's key on the new link fails check 2;
  // on any link it would be older than the tomb.
  CHECK(a.Admit(2, 2, Row(kPlayerKind, 2, 1, 6)) == OwnedVerdict::kWrongPlayer);
  CHECK(a.Admit(2, 1, Row(kPlayerKind, 2, 1, 6)) == OwnedVerdict::kStale);
  // The new epoch's own row starts again from revision 1.
  CHECK(a.Admit(2, 2, Row(kPlayerKind, 2, 2, 1)) == OwnedVerdict::kAdmitted);
  CHECK(a.Admit(3, 1, Row(kPlayerKind, 3, 1, 2)) == OwnedVerdict::kAdmitted);
}

BACKEND_TEST("OW3", OwnedRefusalLineOncePerSecond) {
  OwnedAdmission a(kTable);
  const TimePoint t0 = TimePoint{} + 1h;
  CHECK(a.ShouldLog(2, kInfoKind, OwnedVerdict::kNotOwned, t0));
  CHECK(!a.ShouldLog(2, kInfoKind, OwnedVerdict::kNotOwned, t0 + 999ms));
  CHECK(a.ShouldLog(2, kInfoKind, OwnedVerdict::kWrongPlayer, t0 + 999ms));  // another reason
  CHECK(a.ShouldLog(3, kInfoKind, OwnedVerdict::kNotOwned, t0 + 999ms));     // another peer
  CHECK(a.ShouldLog(2, kInfoKind, OwnedVerdict::kNotOwned, t0 + 1s));
  CHECK(!a.ShouldLog(2, kPlayerKind, OwnedVerdict::kStale, t0));  // never logged
}

BACKEND_TEST("OW4", OwnedDeltaWire) {
  msg::OwnedDelta m;
  m.record = Row(kPlayerKind, 2, 1, 7, {9, 8, 7, 6});
  std::vector<uint8_t> frame;
  REQUIRE_OR_RETURN_VOID(AppendMessage(m, frame));
  CHECK(LaneOf(MessageType::kOwnedDelta) == netplay::Lane::kState);
  CHECK(MessageTypeName(MessageType::kOwnedDelta) == "owned_delta");
  std::vector<Message> out;
  REQUIRE_OR_RETURN_VOID(DecodeFrame(frame, out));
  REQUIRE_OR_RETURN_VOID(out.size() == 1 && std::holds_alternative<msg::OwnedDelta>(out[0]));
  CHECK(std::get<msg::OwnedDelta>(out[0]).record == m.record);
  // A broken record ends the frame.
  frame.resize(frame.size() - 2);
  out.clear();
  CHECK(!DecodeFrame(frame, out));
}

// Churn: 8 peers rejoin 2,000 times each, every epoch writing its row twice. Admission holds the
// live epochs' keys only (one owned session kind: one key per live peer) and one ended epoch per
// peer; a delayed write of any ended epoch is still stale.
BACKEND_TEST("OW5", OwnedChurnBounded) {
  OwnedAdmission a(kTable);
  bool admitted = true, live_bound = true, tombed = true, emptied = true;
  for (uint32_t epoch = 1; epoch <= 2000; ++epoch) {
    for (uint16_t peer = 2; peer <= 9; ++peer) {
      admitted &= a.Admit(peer, epoch, Row(kPlayerKind, peer, epoch, 1)) == OwnedVerdict::kAdmitted;
      admitted &= a.Admit(peer, epoch, Row(kPlayerKind, peer, epoch, 2)) == OwnedVerdict::kAdmitted;
    }
    live_bound &= a.KeysHeld() == 8;
    for (uint16_t peer = 2; peer <= 9; ++peer) tombed &= a.EndPeerEpoch(peer, epoch).size() == 1;
    emptied &= a.KeysHeld() == 0 && a.RetiredPeersHeld() == 8;
  }
  CHECK(admitted);
  CHECK(live_bound);
  CHECK(tombed);
  CHECK(emptied);
  CHECK(a.Count(OwnedVerdict::kAdmitted) == 2000u * 8u * 2u);
  CHECK(a.Admit(5, 2000, Row(kPlayerKind, 5, 2000, 3)) == OwnedVerdict::kStale);  // the last epoch
  CHECK(a.Admit(5, 1, Row(kPlayerKind, 5, 1, 3)) == OwnedVerdict::kStale);        // the first
  CHECK(a.EndPeerEpoch(5, 1).empty());
  CHECK(a.KeysHeld() == 0 && a.RetiredPeersHeld() == 8);
  // The next epoch starts again from revision 1.
  CHECK(a.Admit(5, 2001, Row(kPlayerKind, 5, 2001, 1)) == OwnedVerdict::kAdmitted);
  CHECK(a.KeysHeld() == 1);
}

// The refusal limiter under floods: one live client sends every kind id outside the table within
// one second (each counted, one line, one entry); 3,000 peers refused within one second fill at
// most kMaxRefusalKeys entries; a second later old entries make room again.
BACKEND_TEST("OW6", OwnedRefusalFloodBounded) {
  OwnedAdmission a(kTable);
  const TimePoint t0 = TimePoint{} + 1h;
  int lines = 0;
  bool not_owned = true;
  for (uint32_t kind = 100; kind <= 0xFFFF; ++kind) {
    const OwnedVerdict v = a.Admit(2, 1, Row(KindId(kind), 2, 1, 1));
    not_owned &= v == OwnedVerdict::kNotOwned;
    if (a.ShouldLog(2, KindId(kind), v, t0)) ++lines;
  }
  CHECK(not_owned);
  CHECK(lines == 1);
  CHECK(a.RefusalKeysHeld() == 1);
  CHECK(a.Count(OwnedVerdict::kNotOwned) == 0x10000u - 100u);
  CHECK(a.KeysHeld() == 0);
  // A second later the unknown kinds' entry prints once more, whichever id comes.
  CHECK(a.ShouldLog(2, 7777, OwnedVerdict::kNotOwned, t0 + 1s));
  CHECK(!a.ShouldLog(2, 100, OwnedVerdict::kNotOwned, t0 + 1s));
  // A known kind keeps its own entry.
  CHECK(a.ShouldLog(2, kInfoKind, OwnedVerdict::kNotOwned, t0 + 1s));
  CHECK(a.RefusalKeysHeld() == 2);
  // Many peers within one second: never more than kMaxRefusalKeys entries, lines stop at the cap.
  lines = 0;
  size_t most = 0;
  for (uint16_t peer = 100; peer < 3100; ++peer) {
    if (a.ShouldLog(peer, kPlayerKind, OwnedVerdict::kWrongPlayer, t0 + 1500ms)) ++lines;
    most = std::max(most, a.RefusalKeysHeld());
  }
  CHECK(most == OwnedAdmission::kMaxRefusalKeys);
  CHECK(lines == int(OwnedAdmission::kMaxRefusalKeys) - 2);  // the two t0 + 1s entries are younger
  CHECK(!a.ShouldLog(3099, kPlayerKind, OwnedVerdict::kWrongPlayer, t0 + 1999ms));
  // Once they are a second old, room again: the next new key prints and the old entries go.
  CHECK(a.ShouldLog(3099, kPlayerKind, OwnedVerdict::kWrongPlayer, t0 + 2500ms));
  CHECK(a.RefusalKeysHeld() == 1);
  CHECK(!a.ShouldLog(2, kPlayerKind, OwnedVerdict::kLimit, t0 + 9s));  // WorldState's line
  CHECK(a.RefusalKeysHeld() == 1);
}

// A write S's WorldState refuses (the session row limit) is kLimit and remembers nothing: the same
// revision is checked again, not stale; the write is never called when a check refuses.
BACKEND_TEST("OW7", OwnedRefusedWriteNotRemembered) {
  OwnedAdmission a(kTable);
  Limits limits;
  limits.max_rows_per_session = 1;
  WorldState state(kTable, limits);
  const TimePoint now = TimePoint{} + 1h;
  int writes = 0;
  auto put = [&](const Record& r) {
    return [&state, &writes, &r, now] {
      ++writes;
      const PutResult w = state.Put(r.key, r.value, now);
      return w == PutResult::kWritten || w == PutResult::kUnchanged;
    };
  };
  const Record two = Row(kPlayerKind, 2, 1, 1);
  CHECK(a.Admit(2, 1, two, put(two)) == OwnedVerdict::kAdmitted);
  const Record three = Row(kPlayerKind, 3, 1, 1);
  CHECK(a.Admit(3, 1, three, put(three)) == OwnedVerdict::kLimit);  // past the row limit
  CHECK(a.Admit(3, 1, three, put(three)) == OwnedVerdict::kLimit);  // not stale: not remembered
  CHECK(a.KeysHeld() == 1 && a.Count(OwnedVerdict::kLimit) == 2);
  CHECK(writes == 3);
  // The same value at a newer revision: WorldState answers kUnchanged, which took it.
  const Record same = Row(kPlayerKind, 2, 1, 2);
  CHECK(a.Admit(2, 1, same, put(same)) == OwnedVerdict::kAdmitted);
  CHECK(a.Admit(2, 1, same, put(same)) == OwnedVerdict::kStale);
  const Record wrong = Row(kPlayerKind, 3, 1, 4);
  CHECK(a.Admit(2, 1, wrong, put(wrong)) == OwnedVerdict::kWrongPlayer);
  CHECK(writes == 4);  // neither the stale nor the wrong-player row reached WorldState
}

}  // namespace
}  // namespace skylanders::backend
