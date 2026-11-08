#include "backend/record.h"

#include "backend/limits.h"

namespace skylanders::backend {

namespace {

constexpr uint8_t kFlagTomb = 1;

// A key the wire carries: a kind (ids start at 1, validator rule V1) and a Valid entity.
bool KeyValid(const RecordKey& key) { return key.kind != 0 && Valid(key.entity); }

}  // namespace

std::string_view ScopeName(Scope scope) {
  switch (scope) {
    case Scope::kSession: return "session";
    case Scope::kLevel: return "level";
  }
  return "unknown";
}

std::string_view PutResultName(PutResult r) {
  switch (r) {
    case PutResult::kWritten: return "written";
    case PutResult::kUnchanged: return "unchanged";
    case PutResult::kStaleRevision: return "stale revision";
    case PutResult::kWrongEpoch: return "wrong epoch";
    case PutResult::kTooBig: return "too big";
    case PutResult::kRowLimit: return "row limit";
    case PutResult::kUnknownKind: return "unknown kind";
    case PutResult::kMixedScope: return "mixed scope";
  }
  return "unknown";
}

bool Newer(const Record& incoming, const Record* stored, uint32_t current_epoch) {
  if (incoming.epoch != current_epoch) return false;  // another epoch than its scope's
  if (incoming.revision == 0) return false;           // revisions count from 1
  if (stored == nullptr) return true;
  // Revisions count within one epoch; a stored row of another epoch is already dead.
  if (stored->epoch != incoming.epoch) return true;
  // Not above the stored one (a tomb counts): dropped, so a late old value never comes back.
  return incoming.revision > stored->revision;
}

uint64_t ValueDigest(std::span<const uint8_t> value) {
  uint64_t h = 14695981039346656037ull;
  for (const uint8_t b : value) {
    h ^= b;
    h *= 1099511628211ull;
  }
  return h;
}

bool EncodeKey(const RecordKey& key, std::vector<uint8_t>& out) {
  if (!KeyValid(key)) return false;
  Writer w(out);
  w.U16(key.kind);
  Encode(key.entity, out);
  w.U16(key.slot);
  return true;
}

std::optional<RecordKey> DecodeKey(Reader& in) {
  RecordKey key;
  key.kind = in.U16();
  const std::optional<EntityId> entity = Decode(in);
  key.slot = in.U16();
  if (!in.Ok() || !entity || key.kind == 0) return std::nullopt;
  key.entity = *entity;
  return key;
}

bool EncodeRecord(const Record& record, std::vector<uint8_t>& out) {
  if (!KeyValid(record.key) || record.value.size() > kMaxValueBytes) return false;
  if (record.tomb && !record.value.empty()) return false;  // a tomb carries no value
  EncodeKey(record.key, out);
  Writer w(out);
  w.U32(record.revision);
  w.U32(record.epoch);
  w.U16(record.writer);
  w.U8(record.tomb ? kFlagTomb : 0);
  w.U64(record.lsn);
  w.U16(uint16_t(record.value.size()));
  w.Bytes(record.value);
  return true;
}

std::optional<Record> DecodeRecord(Reader& in) {
  Record r;
  const std::optional<RecordKey> key = DecodeKey(in);
  r.revision = in.U32();
  r.epoch = in.U32();
  r.writer = in.U16();
  const uint8_t flags = in.U8();
  r.lsn = in.U64();
  const uint16_t length = in.U16();
  if (!in.Ok() || !key || (flags & ~kFlagTomb) != 0 || length > kMaxValueBytes) return std::nullopt;
  r.tomb = (flags & kFlagTomb) != 0;
  if (r.tomb && length != 0) return std::nullopt;
  const std::span<const uint8_t> value = in.Bytes(length);
  if (!in.Ok()) return std::nullopt;
  r.key = *key;
  r.value.assign(value.begin(), value.end());
  return r;
}

}  // namespace skylanders::backend
