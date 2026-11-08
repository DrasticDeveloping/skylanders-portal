// base.h: the bounded codec and the names (no design id: the parts the design's tests stand on;
// ids BASE1 to BASE3 are this file's own).

#include <cstdint>
#include <set>
#include <string_view>
#include <vector>

#include "backend/base.h"
#include "backend_test.h"

using namespace skylanders::backend;

BACKEND_TEST("BASE1", CodecRoundTrip) {
  std::vector<uint8_t> out;
  Writer w(out);
  w.U8(0x12);
  w.U16(0x3456);
  w.U32(0x789ABCDE);
  w.U64(0x0102030405060708ull);
  const uint8_t bytes[] = {9, 8, 7};
  w.Bytes(bytes);
  w.Bytes(std::string_view("hi"));
  CHECK(w.Ok());
  CHECK(w.Size() == 1 + 2 + 4 + 8 + 3 + 2);
  // Little endian.
  CHECK(out[1] == 0x56 && out[2] == 0x34);
  CHECK(out[3] == 0xDE && out[6] == 0x78);
  CHECK(out[7] == 0x08 && out[14] == 0x01);

  Reader r(out);
  CHECK(r.U8() == 0x12);
  CHECK(r.U16() == 0x3456);
  CHECK(r.U32() == 0x789ABCDE);
  CHECK(r.U64() == 0x0102030405060708ull);
  const std::span<const uint8_t> got = r.Bytes(3);
  CHECK(got.size() == 3 && got[0] == 9 && got[2] == 7);
  CHECK(r.Position() == 18 && r.Remaining() == 2);
  r.Skip(2);
  CHECK(r.Ok() && r.Remaining() == 0);

  // A length patched in afterwards.
  std::vector<uint8_t> msg;
  Writer m(msg);
  m.U16(0);
  m.U32(7);
  m.PatchU16(0, uint16_t(m.Size() - 2));
  CHECK(m.Ok() && msg[0] == 4 && msg[1] == 0);
  m.PatchU16(5, 1);  // past the end
  CHECK(!m.Ok());
}

BACKEND_TEST("BASE2", CodecBounds) {
  // The writer never passes its limit; a refused write writes nothing and fails the writer.
  std::vector<uint8_t> out;
  Writer w(out, 6);
  w.U32(1);
  CHECK(w.Ok() && w.Room() == 2);
  w.U32(2);
  CHECK(!w.Ok());
  CHECK(out.size() == 4);
  w.U8(3);  // stays failed
  CHECK(!w.Ok() && out.size() == 4);
  std::vector<uint8_t> bytes;
  Writer b(bytes, 2);
  const uint8_t three[] = {1, 2, 3};
  b.Bytes(three);
  CHECK(!b.Ok() && bytes.empty());
  Writer f(bytes);
  f.Fail();
  CHECK(!f.Ok());

  // The reader: a read past the end returns 0 or an empty span and fails it.
  const uint8_t in[] = {1, 2, 3};
  Reader r(in);
  CHECK(r.U16() == 0x0201);
  CHECK(r.U16() == 0);
  CHECK(!r.Ok());
  Reader s(in);
  CHECK(s.Bytes(4).empty() && !s.Ok());
  Reader k(in);
  k.Skip(4);
  CHECK(!k.Ok());
  Reader e(std::span<const uint8_t>{});
  CHECK(e.U8() == 0 && !e.Ok());
}

BACKEND_TEST("BASE3", BaseNames) {
  CHECK(PeerRoleName(PeerRole::kServer) == "server");
  CHECK(PeerRoleName(PeerRole::kOwner) == "owner");
  CHECK(PeerRoleName(PeerRole::kGuest) == "guest");
  CHECK(ReasonName(Reason::kNone) == "none");
  CHECK(ReasonName(Reason::kStaleGeneration) == "stale generation");
  CHECK(ReasonName(Reason::kParentMissing) == "parent missing");
  CHECK(ReasonName(Reason::kPreconditionMissing) == "precondition missing");
  // Every reason has its own words.
  std::set<std::string_view> names;
  for (int i = 0; i <= int(Reason::kKind); ++i) names.insert(ReasonName(Reason(i)));
  CHECK(names.size() == size_t(Reason::kKind) + 1);
  CHECK(!names.contains("unknown"));
}
