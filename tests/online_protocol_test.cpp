#include <cstdint>
#include <string>
#include <vector>

#include "online/protocol.h"
#include "test_util.h"

using namespace skylanders::online;

namespace {

bool RoundTrip(const Message& m) {
  const std::vector<uint8_t> bytes = EncodeMessage(m);
  if (bytes.size() < kMessageHeaderSize) return false;
  auto back = DecodeMessages(bytes);
  return back && back->size() == 1 && (*back)[0] == m;
}

Hello SampleHello() {
  Hello h;
  for (size_t i = 0; i < h.build_digest.size(); ++i) h.build_digest[i] = uint8_t(i * 7 + 1);
  h.figure_id = 0x1234;
  h.figure_variant = 0x3805;
  h.name = "Spyro \xE2\x98\x85";  // with a UTF-8 star
  for (size_t i = 0; i < h.owner_token.size(); ++i) h.owner_token[i] = uint8_t(0xF0 + i);
  return h;
}

}  // namespace

int main() {
  // Every message survives a round trip.
  {
    CHECK(RoundTrip(SampleHello()));
    CHECK(RoundTrip(Hello{}));
    CHECK(RoundTrip(Welcome{3, 0xDEADBEEF, 60, 0x0102030405060708ull, 7, "levels/zone_hub"}));
    CHECK(RoundTrip(Refuse{RefuseReason::kFull, "the session is full"}));
    CHECK(RoundTrip(Refuse{RefuseReason::kNotOwner, ""}));
    CHECK(RoundTrip(Bye{}));
    CHECK(RoundTrip(Ping{0xFFFFFFFFFFFFFFFFull}));
    CHECK(RoundTrip(Pong{123456789, 987654321}));
    CHECK(RoundTrip(LevelRequest{"a/b/c"}));
    CHECK(RoundTrip(LevelLoad{42, "z"}));
    CHECK(RoundTrip(LevelLoaded{42}));
    CHECK(RoundTrip(BarrierGo{42, 1'000'000}));
    CHECK(RoundTrip(PlayerList{}));
    CHECK(RoundTrip(PlayerList{{{1, "Owner"}, {2, "Two"}, {4, ""}}}));
    CHECK((TypeOf(Message{Hello{}}) == MessageType::kHello));
    CHECK((TypeOf(Message{PlayerList{}}) == MessageType::kPlayerList));
  }

  // The wire layout of one message: type, length, fields, little-endian.
  {
    auto b = EncodeMessage(LevelLoaded{0x01020304});
    const std::vector<uint8_t> want = {9, 0, 4, 0, 4, 3, 2, 1};
    CHECK(b == want);
    auto h = EncodeMessage(SampleHello());
    CHECK(h.size() == kMessageHeaderSize + 2 + 32 + 2 + 2 + 1 + SampleHello().name.size() + 16);
  }

  // Limits: a name over 32 bytes, a zone over 128, bad UTF-8, too many players: not encoded.
  {
    Hello h;
    h.name = std::string(kMaxNameBytes, 'x');
    CHECK(RoundTrip(h));
    h.name.push_back('x');
    CHECK(EncodeMessage(h).empty());
    std::vector<uint8_t> out = {0xAA};
    CHECK(!EncodeMessage(h, out));
    CHECK(out.size() == 1);  // nothing appended
    CHECK(RoundTrip(LevelLoad{1, std::string(kMaxZoneBytes, 'z')}));
    CHECK(EncodeMessage(LevelLoad{1, std::string(kMaxZoneBytes + 1, 'z')}).empty());
    CHECK(EncodeMessage(LevelRequest{"\xC3"}).empty());  // cut in the middle of a character
    CHECK(EncodeMessage(LevelRequest{std::string("a\0b", 3)}).empty());
    PlayerList many;
    for (uint16_t i = 0; i <= kMaxPlayerEntries; ++i) many.players.push_back({i, "p"});
    CHECK(EncodeMessage(many).empty());
  }

  // Several messages in one buffer come back in order.
  {
    std::vector<uint8_t> b;
    CHECK(EncodeMessage(Ping{1}, b));
    CHECK(EncodeMessage(LevelLoad{2, "two"}, b));
    CHECK(EncodeMessage(Bye{}, b));
    auto back = DecodeMessages(b);
    REQUIRE_OR_RETURN(back && back->size() == 3);
    CHECK(((*back)[0] == Message{Ping{1}}));
    CHECK(((*back)[1] == Message{LevelLoad{2, "two"}}));
    CHECK(((*back)[2] == Message{Bye{}}));
  }

  // Truncated input is rejected at every length short of the whole.
  {
    std::vector<uint8_t> b;
    EncodeMessage(SampleHello(), b);
    EncodeMessage(Welcome{1, 2, 60, 4, 5, "zone"}, b);
    EncodeMessage(PlayerList{{{1, "a"}, {2, "bb"}}}, b);
    CHECK(DecodeMessages(b).has_value());
    int accepted_short = 0;
    for (size_t n = 1; n < b.size(); ++n) {
      auto r = DecodeMessages(std::span<const uint8_t>(b.data(), n));
      // A cut exactly between two messages is a valid, shorter buffer.
      if (r) ++accepted_short;
    }
    CHECK(accepted_short == 2);
    CHECK(DecodeMessages({}).has_value());
    CHECK(DecodeMessages({})->empty());
  }

  // A known message whose fields are shorter than its type needs is rejected even when the length
  // field agrees; extra bytes after the known fields are ignored (a later version's additions).
  {
    std::vector<uint8_t> short_pong = {6, 0, 8, 0, 1, 2, 3, 4, 5, 6, 7, 8};  // Pong needs 16
    CHECK(!DecodeMessages(short_pong).has_value());
    std::vector<uint8_t> long_loaded = {9, 0, 6, 0, 7, 0, 0, 0, 0xEE, 0xEE};
    auto r = DecodeMessages(long_loaded);
    REQUIRE_OR_RETURN(r && r->size() == 1);
    CHECK(((*r)[0] == Message{LevelLoaded{7}}));
    // A string length that runs past its message.
    std::vector<uint8_t> bad_zone = {7, 0, 3, 0, 5, 'a', 'b'};
    CHECK(!DecodeMessages(bad_zone).has_value());
    // A name over the limit, though the bytes are there.
    std::vector<uint8_t> long_name;
    Hello h;
    EncodeMessage(h, long_name);
    // The name length byte sits after type, length, version, digest, figure, variant.
    const size_t at = kMessageHeaderSize + 2 + 32 + 2 + 2;
    CHECK(long_name[at] == 0);
    long_name[at] = 33;
    long_name.insert(long_name.begin() + at + 1, 33, 'n');
    long_name[2] = uint8_t(long_name.size() - kMessageHeaderSize);
    CHECK(!DecodeMessages(long_name).has_value());
    // An unknown refuse reason reads as "other".
    std::vector<uint8_t> odd_reason = {3, 0, 2, 0, 99, 0};
    auto o = DecodeMessages(odd_reason);
    REQUIRE_OR_RETURN(o && o->size() == 1);
    CHECK(std::get<Refuse>((*o)[0]).reason == RefuseReason::kOther);
  }

  // Unknown types are skipped by their length, between known ones.
  {
    std::vector<uint8_t> b;
    EncodeMessage(Ping{5}, b);
    const std::vector<uint8_t> unknown = {0x34, 0x12, 3, 0, 0xAA, 0xBB, 0xCC};
    b.insert(b.end(), unknown.begin(), unknown.end());
    const std::vector<uint8_t> empty_unknown = {0, 0, 0, 0};  // type 0, no body
    b.insert(b.end(), empty_unknown.begin(), empty_unknown.end());
    EncodeMessage(LevelLoaded{6}, b);
    size_t skipped = 0;
    auto r = DecodeMessages(b, &skipped);
    REQUIRE_OR_RETURN(r && r->size() == 2);
    CHECK(skipped == 2);
    CHECK(((*r)[0] == Message{Ping{5}}));
    CHECK(((*r)[1] == Message{LevelLoaded{6}}));
    // An unknown type whose length runs past the end is still a cut buffer.
    const std::vector<uint8_t> cut = {0x34, 0x12, 9, 0, 1};
    CHECK(!DecodeMessages(cut).has_value());
  }

  // UTF-8 helpers.
  {
    CHECK(IsValidUtf8("plain"));
    CHECK(IsValidUtf8("\xE2\x98\x85"));
    CHECK(IsValidUtf8("\xF0\x9F\x98\x80"));
    CHECK(!IsValidUtf8("\xC0\x80"));          // overlong NUL
    CHECK(!IsValidUtf8("\xED\xA0\x80"));      // a surrogate
    CHECK(!IsValidUtf8("\xF4\x90\x80\x80"));  // above U+10FFFF
    CHECK(!IsValidUtf8("\x80"));
    CHECK(TruncateUtf8("abc", 5) == "abc");
    CHECK(TruncateUtf8("ab\xE2\x98\x85", 4) == "ab");
    CHECK(TruncateUtf8("ab\xE2\x98\x85", 5) == "ab\xE2\x98\x85");
    CHECK(IsNone(OwnerToken{}));
    CHECK(!IsNone(SampleHello().owner_token));
    CHECK(std::string(RefuseReasonName(RefuseReason::kDigest)) == "digest");
  }

  return Finish("online_protocol");
}
