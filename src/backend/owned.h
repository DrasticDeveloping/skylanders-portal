#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <span>
#include <string_view>
#include <tuple>
#include <vector>

#include "backend/base.h"
#include "backend/kind_table.h"
#include "backend/record.h"

// Client-owned rows (stage 4, docs/online-4.md sections 3.5 and 4.3): a client writes its own
// player's rows (kind class kOwnedValue) with OwnedDelta on the state lane; S admits each by six
// checks, in order, stamps the writer from the link, never from the message, and relays an admitted
// row as its own write, so every other peer gets it through the normal deltas and baselines. S's
// only write to an owned row is the tomb at the end of the owner's peer epoch.
//
// The six checks (section 4.3):
//   1 owned kind   the kind's class is kOwnedValue                         (not owned)
//   2 key bound    key.entity == Player(link peer, link peer epoch), slot 0 (wrong player)
//   3 scope        the kind is session scope and the record's epoch is 0   (scope)
//   4 revision     above the last admitted one for the key, tombs included dropped as stale,
//                                                                          counted, no line
//   5 size         at most the kind's max_value_bytes                     (size)
//   6 schema       the game's codec decodes it                            (schema)
//
// Revisions. WorldState numbers its own revisions per row (entity_id.h's table), so the client's
// revision (Record::revision in the OwnedDelta) is the writer's own counter: check 4 compares it
// with the last one admitted for the key, never with the WorldState's.

namespace skylanders::backend {

enum class OwnedVerdict : uint8_t {
  kAdmitted,
  kNotOwned,     // check 1
  kWrongPlayer,  // check 2
  kScope,        // check 3
  kStale,        // check 4: dropped and counted, never logged per drop
  kSize,         // check 5
  kSchema,       // check 6
};
// "admitted", "not owned", "wrong player", "scope", "stale", "size", "schema": the word in
// `Online: owned write refused <peer> <kind> (<reason>)`.
std::string_view OwnedVerdictName(OwnedVerdict v);

// The game's codec check for one owned kind's value (check 6): lengths in range, enums known,
// strings terminated, toy ids in the toy table. nullptr: every value decodes (a test table).
using OwnedSchemaCheck = bool (*)(KindId kind, std::span<const uint8_t> value);

// S's admission state. Not thread-safe: S's pump owns it.
class OwnedAdmission {
 public:
  explicit OwnedAdmission(std::span<const KindSpec> table, OwnedSchemaCheck schema = nullptr);

  // The six checks in order for `incoming`, which came on the link of (peer, peer_epoch). On
  // kAdmitted the key's revision is remembered; the caller then writes the value into WorldState.
  OwnedVerdict Admit(uint16_t peer, uint32_t peer_epoch, const Record& incoming);

  // The peer epoch ended (leave, timeout, kick, or a new epoch for the peer): every key of that
  // epoch admitted so far is tombed here (any later write for it is stale) and returned, so S
  // writes a tomb for each. Keys of other epochs are untouched.
  std::vector<RecordKey> EndPeerEpoch(uint16_t peer, uint32_t peer_epoch);

  // Whether a refusal line is printed now: once per (peer, kind, verdict) per second (section
  // 4.3). kStale is never logged. Every refusal is counted whether logged or not.
  bool ShouldLog(uint16_t peer, KindId kind, OwnedVerdict v, TimePoint now);

  uint64_t Count(OwnedVerdict v) const { return counts_[size_t(v)]; }
  void SetSchema(OwnedSchemaCheck schema) { schema_ = schema; }
  size_t KeysHeld() const { return rows_.size(); }

 private:
  struct Row {
    uint32_t revision = 0;
    bool tombed = false;
  };
  std::span<const KindSpec> table_;
  OwnedSchemaCheck schema_;
  std::map<RecordKey, Row> rows_;
  std::map<std::tuple<uint16_t, KindId, uint8_t>, TimePoint> last_logged_;
  uint64_t counts_[7] = {};
};

// The OwnedDelta message's own codec (lanes.h carries it as msg::OwnedDelta): one record in
// record.h's wire form. Encode is false (nothing appended) for a record EncodeRecord refuses.
bool EncodeOwnedDelta(const Record& record, std::vector<uint8_t>& out);
std::optional<Record> DecodeOwnedDelta(Reader& in);

}  // namespace skylanders::backend
