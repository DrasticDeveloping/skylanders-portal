#pragma once

#include <cstdint>
#include <functional>
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
//
// Bounds (Sol stage 4 F2). Admission keeps a revision only for keys of live peer epochs: a key
// is Player(peer, epoch) slot 0 of an owned session kind, so at most (live peer epochs) x (owned
// kinds). EndPeerEpoch drops the epoch's keys; what stays is one number per peer id, the highest
// ended epoch (S numbers a peer's epochs upwards), so a delayed write of an ended epoch is stale
// however many epochs came and went. The refusal limiter holds at most kMaxRefusalKeys entries,
// every unknown kind of a peer shares one, and an entry a second old is dropped when room is
// needed.

namespace skylanders::backend {

enum class OwnedVerdict : uint8_t {
  kAdmitted,
  kNotOwned,     // check 1
  kWrongPlayer,  // check 2
  kScope,        // check 3
  kStale,        // check 4: dropped and counted, never logged per drop
  kSize,         // check 5
  kSchema,       // check 6
  kLimit,        // the six checks passed but S's own write was refused (WorldState's row limit,
                 // reported by WorldState's own limit event): nothing remembered
};
// "admitted", "not owned", "wrong player", "scope", "stale", "size", "schema", "limit": the word
// in `Online: owned write refused <peer> <kind> (<reason>)`.
std::string_view OwnedVerdictName(OwnedVerdict v);

// The game's codec check for one owned kind's value (check 6): lengths in range, enums known,
// strings terminated, toy ids in the toy table. nullptr: every value decodes (a test table).
using OwnedSchemaCheck = bool (*)(KindId kind, std::span<const uint8_t> value);

// S's admission state. Not thread-safe: S's pump owns it.
class OwnedAdmission {
 public:
  // The refusal limiter's size: past it, entries a second old are dropped; if none is, the line
  // is not printed (the refusal is still counted).
  static constexpr size_t kMaxRefusalKeys = 256;

  explicit OwnedAdmission(std::span<const KindSpec> table, OwnedSchemaCheck schema = nullptr);

  // The six checks in order for `incoming`, which came on the link of (peer, peer_epoch), then
  // `write`, S's own write into WorldState. The key's revision is remembered, and kAdmitted
  // returned, only when `write` returns true; false is kLimit and nothing is remembered, so the
  // client's next revision is checked against the last one S stored. An empty `write` succeeds
  // (tests). `write` is not called when a check refuses.
  OwnedVerdict Admit(uint16_t peer, uint32_t peer_epoch, const Record& incoming,
                     const std::function<bool()>& write = {});

  // The peer epoch ended (leave, timeout, kick, or a new epoch for the peer): every key of that
  // epoch admitted so far is returned, so S writes a tomb for each, and forgotten; any later write
  // of that epoch (or an older one of the peer) is stale. Keys of other epochs are untouched.
  std::vector<RecordKey> EndPeerEpoch(uint16_t peer, uint32_t peer_epoch);

  // Whether a refusal line is printed now: once per (peer, kind, verdict) per second (section
  // 4.3), every kind outside the table counting as one kind. kStale and kLimit are never logged
  // here (kLimit has WorldState's own line). Every refusal is counted whether logged or not.
  bool ShouldLog(uint16_t peer, KindId kind, OwnedVerdict v, TimePoint now);

  uint64_t Count(OwnedVerdict v) const { return counts_[size_t(v)]; }
  void SetSchema(OwnedSchemaCheck schema) { schema_ = schema; }
  size_t KeysHeld() const { return rows_.size(); }
  size_t RetiredPeersHeld() const { return retired_.size(); }
  size_t RefusalKeysHeld() const { return last_logged_.size(); }

 private:
  std::span<const KindSpec> table_;
  OwnedSchemaCheck schema_;
  std::map<RecordKey, uint32_t> rows_;  // live epochs' keys: the last admitted revision
  std::map<uint16_t, uint32_t> retired_;  // peer: its highest ended epoch
  // (peer, kind or kUnknownKind, verdict): when its line was last printed.
  std::map<std::tuple<uint16_t, uint32_t, uint8_t>, TimePoint> last_logged_;
  uint64_t counts_[size_t(OwnedVerdict::kLimit) + 1] = {};
};

// The OwnedDelta message's own codec (lanes.h carries it as msg::OwnedDelta): one record in
// record.h's wire form. Encode is false (nothing appended) for a record EncodeRecord refuses.
bool EncodeOwnedDelta(const Record& record, std::vector<uint8_t>& out);
std::optional<Record> DecodeOwnedDelta(Reader& in);

}  // namespace skylanders::backend
