#include "online/protocol.h"

#include <algorithm>

#include "netplay/bytes.h"

namespace skylanders::online {

using netplay::ByteReader;
using netplay::ByteWriter;

// The variant's order is the type numbering (Hello = 1 ...), which TypeOf relies on.
static_assert(std::variant_size_v<Message> == 11);
static_assert(std::is_same_v<std::variant_alternative_t<0, Message>, Hello>);
static_assert(std::is_same_v<std::variant_alternative_t<10, Message>, PlayerList>);

bool IsNone(const OwnerToken& token) {
  return std::all_of(token.begin(), token.end(), [](uint8_t b) { return b == 0; });
}

const char* RefuseReasonName(RefuseReason reason) {
  switch (reason) {
    case RefuseReason::kVersion: return "version";
    case RefuseReason::kDigest: return "digest";
    case RefuseReason::kFull: return "full";
    case RefuseReason::kNotOwner: return "not owner";
    case RefuseReason::kOther: break;
  }
  return "other";
}

MessageType TypeOf(const Message& message) { return MessageType(message.index() + 1); }

bool IsValidUtf8(std::string_view s) {
  size_t i = 0;
  while (i < s.size()) {
    const uint8_t c = uint8_t(s[i]);
    if (c == 0) return false;
    if (c < 0x80) {
      ++i;
      continue;
    }
    size_t n;
    uint32_t cp;
    if ((c & 0xE0) == 0xC0) {
      n = 1;
      cp = c & 0x1F;
    } else if ((c & 0xF0) == 0xE0) {
      n = 2;
      cp = c & 0x0F;
    } else if ((c & 0xF8) == 0xF0) {
      n = 3;
      cp = c & 0x07;
    } else {
      return false;
    }
    if (s.size() - i <= n) return false;
    for (size_t k = 1; k <= n; ++k) {
      const uint8_t cc = uint8_t(s[i + k]);
      if ((cc & 0xC0) != 0x80) return false;
      cp = (cp << 6) | (cc & 0x3F);
    }
    const bool overlong = (n == 1 && cp < 0x80) || (n == 2 && cp < 0x800) || (n == 3 && cp < 0x10000);
    if (overlong || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return false;
    i += n + 1;
  }
  return true;
}

std::string TruncateUtf8(std::string_view text, size_t max_bytes) {
  if (text.size() <= max_bytes) return std::string(text);
  size_t cut = max_bytes;
  while (cut > 0 && (uint8_t(text[cut]) & 0xC0) == 0x80) --cut;
  return std::string(text.substr(0, cut));
}

namespace {

bool PutString(ByteWriter& w, std::string_view s, size_t max) {
  if (s.size() > max || !IsValidUtf8(s)) return false;
  w.U8(uint8_t(s.size()));
  w.Bytes(s);
  return true;
}

bool GetString(ByteReader& r, size_t max, std::string& out) {
  const size_t n = r.U8();
  if (!r.Ok() || n > max) return false;
  out = r.String(n);
  return r.Ok() && IsValidUtf8(out);
}

bool Fields(ByteWriter& w, const Hello& m) {
  w.U16(m.protocol_version);
  w.Bytes(m.build_digest);
  w.U16(m.figure_id);
  w.U16(m.figure_variant);
  if (!PutString(w, m.name, kMaxNameBytes)) return false;
  w.Bytes(m.owner_token);
  return true;
}
bool Fields(ByteWriter& w, const Welcome& m) {
  w.U16(m.peer_id);
  w.U32(m.session_id);
  w.U16(m.tick_hz);
  w.U64(m.server_tick);
  w.U32(m.level_generation);
  return PutString(w, m.level_zone, kMaxZoneBytes);
}
bool Fields(ByteWriter& w, const Refuse& m) {
  w.U8(uint8_t(m.reason));
  return PutString(w, m.text, kMaxRefuseTextBytes);
}
bool Fields(ByteWriter&, const Bye&) { return true; }
bool Fields(ByteWriter& w, const Ping& m) {
  w.U64(m.client_time_us);
  return true;
}
bool Fields(ByteWriter& w, const Pong& m) {
  w.U64(m.client_time_us);
  w.U64(m.server_tick);
  return true;
}
bool Fields(ByteWriter& w, const LevelRequest& m) { return PutString(w, m.zone, kMaxZoneBytes); }
bool Fields(ByteWriter& w, const LevelLoad& m) {
  w.U32(m.generation);
  return PutString(w, m.zone, kMaxZoneBytes);
}
bool Fields(ByteWriter& w, const LevelLoaded& m) {
  w.U32(m.generation);
  return true;
}
bool Fields(ByteWriter& w, const BarrierGo& m) {
  w.U32(m.generation);
  w.U64(m.start_tick);
  return true;
}
bool Fields(ByteWriter& w, const PlayerList& m) {
  if (m.players.size() > kMaxPlayerEntries) return false;
  w.U8(uint8_t(m.players.size()));
  for (const PlayerEntry& e : m.players) {
    w.U16(e.peer_id);
    if (!PutString(w, e.name, kMaxNameBytes)) return false;
  }
  return true;
}

template <size_t N>
void GetArray(ByteReader& r, std::array<uint8_t, N>& out) {
  auto b = r.Bytes(N);
  if (r.Ok()) std::copy(b.begin(), b.end(), out.begin());
}

std::optional<Message> DecodeKnown(MessageType type, std::span<const uint8_t> body) {
  ByteReader r(body);
  bool ok = true;
  Message result;
  switch (type) {
    case MessageType::kHello: {
      Hello m;
      m.protocol_version = r.U16();
      GetArray(r, m.build_digest);
      m.figure_id = r.U16();
      m.figure_variant = r.U16();
      ok = GetString(r, kMaxNameBytes, m.name);
      GetArray(r, m.owner_token);
      result = std::move(m);
      break;
    }
    case MessageType::kWelcome: {
      Welcome m;
      m.peer_id = r.U16();
      m.session_id = r.U32();
      m.tick_hz = r.U16();
      m.server_tick = r.U64();
      m.level_generation = r.U32();
      ok = GetString(r, kMaxZoneBytes, m.level_zone);
      result = std::move(m);
      break;
    }
    case MessageType::kRefuse: {
      Refuse m;
      const uint8_t reason = r.U8();
      m.reason = reason >= 1 && reason <= 5 ? RefuseReason(reason) : RefuseReason::kOther;
      ok = GetString(r, kMaxRefuseTextBytes, m.text);
      result = std::move(m);
      break;
    }
    case MessageType::kBye:
      result = Bye{};
      break;
    case MessageType::kPing:
      result = Ping{r.U64()};
      break;
    case MessageType::kPong: {
      Pong m;
      m.client_time_us = r.U64();
      m.server_tick = r.U64();
      result = m;
      break;
    }
    case MessageType::kLevelRequest: {
      LevelRequest m;
      ok = GetString(r, kMaxZoneBytes, m.zone);
      result = std::move(m);
      break;
    }
    case MessageType::kLevelLoad: {
      LevelLoad m;
      m.generation = r.U32();
      ok = GetString(r, kMaxZoneBytes, m.zone);
      result = std::move(m);
      break;
    }
    case MessageType::kLevelLoaded:
      result = LevelLoaded{r.U32()};
      break;
    case MessageType::kBarrierGo: {
      BarrierGo m;
      m.generation = r.U32();
      m.start_tick = r.U64();
      result = m;
      break;
    }
    case MessageType::kPlayerList: {
      PlayerList m;
      const size_t count = r.U8();
      if (count > kMaxPlayerEntries) return std::nullopt;
      for (size_t i = 0; ok && i < count; ++i) {
        PlayerEntry e;
        e.peer_id = r.U16();
        ok = GetString(r, kMaxNameBytes, e.name);
        m.players.push_back(std::move(e));
      }
      result = std::move(m);
      break;
    }
    default:
      return std::nullopt;
  }
  if (!ok || !r.Ok()) return std::nullopt;
  return result;
}

bool IsKnown(uint16_t type) { return type >= 1 && type <= 11; }

}  // namespace

bool EncodeMessage(const Message& message, std::vector<uint8_t>& out) {
  std::vector<uint8_t> body;
  ByteWriter w(body);
  const bool ok = std::visit([&](const auto& m) { return Fields(w, m); }, message);
  if (!ok || body.size() > 0xFFFF) return false;
  ByteWriter o(out);
  o.U16(uint16_t(TypeOf(message)));
  o.U16(uint16_t(body.size()));
  o.Bytes(body);
  return true;
}

std::vector<uint8_t> EncodeMessage(const Message& message) {
  std::vector<uint8_t> out;
  if (!EncodeMessage(message, out)) out.clear();
  return out;
}

std::optional<std::vector<Message>> DecodeMessages(std::span<const uint8_t> bytes,
                                                   size_t* skipped) {
  std::vector<Message> messages;
  size_t unknown = 0;
  ByteReader r(bytes);
  while (r.Remaining() > 0) {
    const uint16_t type = r.U16();
    const uint16_t length = r.U16();
    auto body = r.Bytes(length);
    if (!r.Ok()) return std::nullopt;
    if (!IsKnown(type)) {
      ++unknown;
      continue;
    }
    auto m = DecodeKnown(MessageType(type), body);
    if (!m) return std::nullopt;
    messages.push_back(std::move(*m));
  }
  if (skipped) *skipped = unknown;
  return messages;
}

}  // namespace skylanders::online
