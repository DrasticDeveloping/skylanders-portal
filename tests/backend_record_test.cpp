// record.h: the revision rule, value digests and the record's wire form (no design id: the parts
// the version cases stand on; ids REC1 to REC3 are this file's own).

#include <cstdint>
#include <optional>
#include <vector>

#include "backend/limits.h"
#include "backend/record.h"
#include "backend_test.h"

using namespace skylanders::backend;

namespace {

Record Row(uint32_t revision, uint32_t epoch, bool tomb = false) {
  Record r;
  r.key = RecordKey{4, Authored(epoch == 0 ? 1 : epoch, 10, 20, 30), 1};
  r.revision = revision;
  r.epoch = epoch;
  r.tomb = tomb;
  r.lsn = 100 + revision;
  if (!tomb) r.value = {uint8_t(revision), 2, 3};
  return r;
}

}  // namespace

BACKEND_TEST("REC1", RevisionRule) {
  const Record stored = Row(3, 2);
  CHECK(Newer(Row(1, 2), nullptr, 2));         // nothing stored
  CHECK(Newer(Row(4, 2), &stored, 2));         // above the stored revision
  CHECK(!Newer(Row(3, 2), &stored, 2));        // the same revision
  CHECK(!Newer(Row(2, 2), &stored, 2));        // a late old value
  CHECK(!Newer(Row(4, 1), &stored, 2));        // another epoch than the scope's
  CHECK(!Newer(Row(4, 3), &stored, 2));
  CHECK(!Newer(Row(0, 2), nullptr, 2));        // revisions count from 1
  // A tomb is a revision too: a late old value never brings a removed row back.
  const Record tomb = Row(4, 2, true);
  CHECK(!Newer(Row(3, 2), &tomb, 2));
  CHECK(Newer(Row(5, 2), &tomb, 2));
  // A stored row of an older epoch (dropped by the epoch change) does not hold a new one back.
  const Record old_epoch = Row(9, 1);
  CHECK(Newer(Row(1, 2), &old_epoch, 2));
  // Session scope: epoch 0.
  CHECK(Newer(Row(1, 0), nullptr, 0));
  CHECK(!Newer(Row(1, 0), nullptr, 1));
}

BACKEND_TEST("REC2", ValueDigestFnv64) {
  CHECK(ValueDigest({}) == 14695981039346656037ull);
  const uint8_t a[] = {'a'};
  CHECK(ValueDigest(a) == 0xAF63DC4C8601EC8Cull);  // FNV-1a 64 test vector
  const uint8_t x[] = {1, 2, 3};
  const uint8_t y[] = {1, 2, 4};
  CHECK(ValueDigest(x) != ValueDigest(y));
}

BACKEND_TEST("REC3", RecordWireForm) {
  const Record rows[] = {Row(1, 1), Row(7, 3, true), Row(2, 0)};
  for (Record r : rows) {
    if (r.epoch == 0) r.key.entity = SessionObject(5);
    std::vector<uint8_t> wire;
    CHECK(EncodeRecord(r, wire));
    CHECK(wire.size() == 2 + kEntityIdWireBytes + 2 + 4 + 4 + 2 + 1 + 8 + 2 + r.value.size());
    Reader in(wire);
    const std::optional<Record> back = DecodeRecord(in);
    CHECK(back.has_value() && *back == r);
    CHECK(in.Ok() && in.Remaining() == 0);
  }

  // A key alone.
  const RecordKey key{7, Player(2, 3), 4};
  std::vector<uint8_t> wire;
  CHECK(EncodeKey(key, wire));
  CHECK(wire.size() == 2 + kEntityIdWireBytes + 2);
  Reader in(wire);
  CHECK(DecodeKey(in) == key);

  // Refused, nothing appended: kind 0, an invalid entity, a value past kMaxValueBytes, a tomb
  // with a value.
  Record bad = Row(1, 1);
  bad.key.kind = 0;
  std::vector<uint8_t> out;
  CHECK(!EncodeRecord(bad, out) && out.empty());
  CHECK(!EncodeKey(bad.key, out) && out.empty());
  bad = Row(1, 1);
  bad.key.entity.ordinal = 3;  // a foreign field on an authored id
  CHECK(!EncodeRecord(bad, out) && out.empty());
  bad = Row(1, 1);
  bad.value.assign(kMaxValueBytes + 1, 0);
  CHECK(!EncodeRecord(bad, out) && out.empty());
  bad.value.assign(kMaxValueBytes, 0);
  CHECK(EncodeRecord(bad, out));  // exactly the bound
  out.clear();
  bad = Row(1, 1, true);
  bad.value = {1};
  CHECK(!EncodeRecord(bad, out) && out.empty());

  // A short or unknown-flagged record does not decode.
  std::vector<uint8_t> good;
  CHECK(EncodeRecord(Row(1, 1), good));
  std::vector<uint8_t> shortened(good.begin(), good.end() - 1);
  Reader s(shortened);
  CHECK(!DecodeRecord(s).has_value());
  std::vector<uint8_t> flagged = good;
  flagged[2 + kEntityIdWireBytes + 2 + 4 + 4 + 2] |= 0x80;  // the flags byte
  Reader f(flagged);
  CHECK(!DecodeRecord(f).has_value());

  CHECK(ScopeName(Scope::kSession) == "session" && ScopeName(Scope::kLevel) == "level");
  CHECK(MaskOf(Scope::kSession) == kScopeSession && MaskOf(Scope::kLevel) == kScopeLevel);
  CHECK(PutResultName(PutResult::kMixedScope) == "mixed scope");
}
