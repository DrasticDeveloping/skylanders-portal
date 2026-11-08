#pragma once

#include <compare>
#include <cstdint>
#include <optional>
#include <span>
#include <string_view>
#include <vector>

#include "backend/entity_id.h"

// Records (design section 3.3): a record is key, value, revision, under an epoch (plan 2.5).

namespace skylanders::backend {

using KindId = uint16_t;

enum class Scope : uint8_t { kSession = 0, kLevel = 1 };
std::string_view ScopeName(Scope scope);  // "session", "level"

// Bit 0 session, bit 1 level: what a peer is subscribed to (D5), what a manifest or baseline covers.
using ScopeMask = uint8_t;
constexpr ScopeMask kScopeSession = 1;
constexpr ScopeMask kScopeLevel = 2;
constexpr ScopeMask kScopeAll = kScopeSession | kScopeLevel;
constexpr ScopeMask MaskOf(Scope scope) { return ScopeMask(1u << uint8_t(scope)); }

// The only writer in stage 2; the field is reserved for stage 4's client-owned rows (Q7) and
// carried on the wire.
constexpr uint16_t kServerWriter = 0;

struct RecordKey {
  KindId kind = 0;
  EntityId entity;
  uint16_t slot = 0;  // a second row on one entity (a per-player tracker)
  auto operator<=>(const RecordKey&) const = default;
};

// A request's identity (design sections 3.2, 3.9). peer and peer_epoch are S's, stamped from the
// link the request came on, never read from the client's bytes; a client's own copy takes them from
// StateHello. Sequences count per (peer, peer epoch, generation) from 1, so a returning peer's keys
// never meet its old ones, and a late evaluation for the old ones finds nothing (Sol F3, Opus F9).
// Declared here, beside RecordKey, so lease.h needs no request.h.
struct RequestKey {
  uint16_t peer = 0;
  uint32_t peer_epoch = 0;
  uint32_t generation = 0;
  uint32_t sequence = 0;  // 0: none (a Request's parent)
  auto operator<=>(const RequestKey&) const = default;
};

struct Record {
  RecordKey key;
  uint32_t revision = 0;
  uint32_t epoch = 0;  // 0 for session scope
  uint16_t writer = kServerWriter;
  bool tomb = false;
  uint64_t lsn = 0;            // the change that made this revision
  std::vector<uint8_t> value;  // at most the kind's max_value_bytes (and kMaxValueBytes); empty
                               // for a tomb
  bool operator==(const Record&) const = default;
};

enum class PutResult : uint8_t {
  kWritten,
  kUnchanged,       // the value equals the stored one (the kind's `equal`, or bytes): no revision
  kStaleRevision,
  kWrongEpoch,
  kTooBig,          // over the kind's max_value_bytes: never stored, never sent (LM4)
  kRowLimit,        // past limits.max_rows_per_level or max_rows_per_session (tombs counted):
                    // never silent, S raises an event (LM5, LM5b; Sol F9)
  kUnknownKind,
  kMixedScope,      // ApplyAll only: the set mixes scopes; the request table answers it `model`
                    // (Opus r2 N3)
};
std::string_view PutResultName(PutResult r);

// The revision rule, on S and on every replica: a row of another epoch than its scope's current
// one is dropped; a revision not above the stored one is dropped (a tomb is a revision too, so a
// late old value never brings a removed row back). `current_epoch` is the epoch of the incoming
// row's scope (0 for session scope).
bool Newer(const Record& incoming, const Record* stored, uint32_t current_epoch);

// FNV-1a, 64 bits, over the value's bytes.
uint64_t ValueDigest(std::span<const uint8_t> value);

// Appends a key's wire form: kind (u16), entity (kEntityIdWireBytes), slot (u16).
bool EncodeKey(const RecordKey& key, std::vector<uint8_t>& out);
std::optional<RecordKey> DecodeKey(Reader& in);
// Appends a record's wire form: key, revision (u32), epoch (u32), writer (u16), flags (u8: bit 0
// tomb), lsn (u64), value length (u16), value. false (nothing appended) if the value is longer
// than kMaxValueBytes or the key is not valid.
bool EncodeRecord(const Record& record, std::vector<uint8_t>& out);
std::optional<Record> DecodeRecord(Reader& in);

}  // namespace skylanders::backend
