#include "backend/owned.h"

#include <chrono>

namespace skylanders::backend {

std::string_view OwnedVerdictName(OwnedVerdict v) {
  switch (v) {
    case OwnedVerdict::kAdmitted: return "admitted";
    case OwnedVerdict::kNotOwned: return "not owned";
    case OwnedVerdict::kWrongPlayer: return "wrong player";
    case OwnedVerdict::kScope: return "scope";
    case OwnedVerdict::kStale: return "stale";
    case OwnedVerdict::kSize: return "size";
    case OwnedVerdict::kSchema: return "schema";
    case OwnedVerdict::kLimit: return "limit";
  }
  return "unknown";
}

namespace {
// The refusal limiter's kind for every kind outside the table: above KindId's range, so it never
// meets a real kind.
constexpr uint32_t kUnknownKind = 0x10000;
}  // namespace

OwnedAdmission::OwnedAdmission(std::span<const KindSpec> table, OwnedSchemaCheck schema)
    : table_(table), schema_(schema) {}

OwnedVerdict OwnedAdmission::Admit(uint16_t peer, uint32_t peer_epoch, const Record& incoming,
                                   const std::function<bool()>& write) {
  auto verdict = [&](OwnedVerdict v) {
    ++counts_[size_t(v)];
    return v;
  };
  // 1. Owned kind. An unknown kind is not owned either.
  const KindSpec* spec = FindKind(table_, incoming.key.kind);
  if (spec == nullptr || spec->state_class != StateClass::kOwnedValue) return verdict(OwnedVerdict::kNotOwned);
  // 2. The key is the link's own player, slot 0: a forged key with an honest writer, another
  // peer's row and an old epoch's row on a new link all end here.
  if (incoming.key.entity != Player(peer, peer_epoch) || incoming.key.slot != 0) {
    return verdict(OwnedVerdict::kWrongPlayer);
  }
  // 3. Scope: session rows only, epoch 0. A tomb from a client is refused here too: only S tombs.
  if (spec->scope != Scope::kSession || incoming.epoch != 0 || incoming.tomb) return verdict(OwnedVerdict::kScope);
  // 4. Revision: above the last admitted one; an ended epoch (tombed) takes nothing more.
  const auto retired = retired_.find(peer);
  if (retired != retired_.end() && peer_epoch <= retired->second) return verdict(OwnedVerdict::kStale);
  const auto it = rows_.find(incoming.key);
  if (it != rows_.end() && incoming.revision <= it->second) return verdict(OwnedVerdict::kStale);
  if (incoming.revision == 0) return verdict(OwnedVerdict::kStale);  // revisions count from 1
  // 5. Size.
  if (incoming.value.size() > spec->max_value_bytes || incoming.value.size() > kMaxValueBytes) {
    return verdict(OwnedVerdict::kSize);
  }
  // 6. Schema.
  if (schema_ != nullptr && !schema_(incoming.key.kind, incoming.value)) return verdict(OwnedVerdict::kSchema);
  // S's own write first: a refused write (the row limit) leaves nothing remembered.
  if (write && !write()) return verdict(OwnedVerdict::kLimit);
  rows_[incoming.key] = incoming.revision;
  return verdict(OwnedVerdict::kAdmitted);
}

std::vector<RecordKey> OwnedAdmission::EndPeerEpoch(uint16_t peer, uint32_t peer_epoch) {
  std::vector<RecordKey> out;
  const EntityId who = Player(peer, peer_epoch);
  for (auto it = rows_.begin(); it != rows_.end();) {
    if (it->first.entity == who) {
      out.push_back(it->first);
      it = rows_.erase(it);
    } else {
      ++it;
    }
  }
  uint32_t& ended = retired_[peer];
  if (peer_epoch > ended) ended = peer_epoch;
  return out;
}

bool OwnedAdmission::ShouldLog(uint16_t peer, KindId kind, OwnedVerdict v, TimePoint now) {
  if (v == OwnedVerdict::kAdmitted || v == OwnedVerdict::kStale || v == OwnedVerdict::kLimit) return false;
  // Every kind outside the table is one kind here: a client sending each id makes one line a
  // second, not one entry per id.
  const uint32_t kind_key = FindKind(table_, kind) != nullptr ? uint32_t(kind) : kUnknownKind;
  const auto key = std::make_tuple(peer, kind_key, uint8_t(v));
  const auto it = last_logged_.find(key);
  if (it != last_logged_.end()) {
    if (now - it->second < std::chrono::seconds(1)) return false;
    it->second = now;
    return true;
  }
  if (last_logged_.size() >= kMaxRefusalKeys) {
    // An entry a second old says nothing any more; with every entry younger, this line is dropped
    // (the refusal is counted) until one ages out.
    std::erase_if(last_logged_, [&](const auto& e) { return now - e.second >= std::chrono::seconds(1); });
    if (last_logged_.size() >= kMaxRefusalKeys) return false;
  }
  last_logged_.emplace(key, now);
  return true;
}

bool EncodeOwnedDelta(const Record& record, std::vector<uint8_t>& out) { return EncodeRecord(record, out); }

std::optional<Record> DecodeOwnedDelta(Reader& in) { return DecodeRecord(in); }

}  // namespace skylanders::backend
