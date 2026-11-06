#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

namespace skylanders::online {

// The session messages. On the wire each one is: type (u16), length of what follows (u16), the
// fields, all little-endian; a string is a length byte and that many UTF-8 bytes. A datagram's
// payload holds one or more messages. A reader skips message types it does not know by their
// length, and ignores bytes past the fields it knows inside a message (a later version may add
// fields at the end).
constexpr uint16_t kProtocolVersion = 1;
constexpr size_t kMaxNameBytes = 32;
constexpr size_t kMaxZoneBytes = 128;
constexpr size_t kMaxRefuseTextBytes = 200;
constexpr size_t kMaxPlayerEntries = 8;
constexpr size_t kMessageHeaderSize = 4;

using BuildDigest = std::array<uint8_t, 32>;
using OwnerToken = std::array<uint8_t, 16>;  // all zero: none
bool IsNone(const OwnerToken& token);

enum class MessageType : uint16_t {
  kHello = 1,
  kWelcome = 2,
  kRefuse = 3,
  kBye = 4,
  kPing = 5,
  kPong = 6,
  kLevelRequest = 7,
  kLevelLoad = 8,
  kLevelLoaded = 9,
  kBarrierGo = 10,
  kPlayerList = 11,
};

enum class RefuseReason : uint8_t {
  kVersion = 1,   // another protocol version
  kDigest = 2,    // another build of the game
  kFull = 3,      // every place is taken
  kNotOwner = 4,  // a wrong owner token, or a request only the owner may make
  kOther = 5,     // also what an unknown reason reads as
};
const char* RefuseReasonName(RefuseReason reason);

// Client to server, first: who is joining.
struct Hello {
  uint16_t protocol_version = kProtocolVersion;
  BuildDigest build_digest{};
  uint16_t figure_id = 0;
  uint16_t figure_variant = 0;
  std::string name;        // UTF-8, at most kMaxNameBytes
  OwnerToken owner_token{};
  bool operator==(const Hello&) const = default;
};

// Server to client: accepted. tick_hz is the rate of the server's tick (the client's tick
// estimate needs it). level_generation 0: no level yet.
struct Welcome {
  uint16_t peer_id = 0;
  uint32_t session_id = 0;
  uint16_t tick_hz = 0;
  uint64_t server_tick = 0;
  uint32_t level_generation = 0;
  std::string level_zone;  // at most kMaxZoneBytes
  bool operator==(const Welcome&) const = default;
};

// Server to client: not accepted (before a Welcome), or a request turned down (after it).
struct Refuse {
  RefuseReason reason = RefuseReason::kOther;
  std::string text;  // at most kMaxRefuseTextBytes
  bool operator==(const Refuse&) const = default;
};

// Either side: leaving.
struct Bye {
  bool operator==(const Bye&) const = default;
};

// Client to server, every second: clock sync and liveness.
struct Ping {
  uint64_t client_time_us = 0;
  bool operator==(const Ping&) const = default;
};

struct Pong {
  uint64_t client_time_us = 0;  // the Ping's, echoed
  uint64_t server_tick = 0;
  bool operator==(const Pong&) const = default;
};

// The owner to the server: change the level.
struct LevelRequest {
  std::string zone;
  bool operator==(const LevelRequest&) const = default;
};

// Server to clients: load this level; it is generation `generation`.
struct LevelLoad {
  uint32_t generation = 0;
  std::string zone;
  bool operator==(const LevelLoad&) const = default;
};

// Client to server: the level of this generation is up.
struct LevelLoaded {
  uint32_t generation = 0;
  bool operator==(const LevelLoaded&) const = default;
};

// Server to clients: everyone has the level; it starts at this server tick.
struct BarrierGo {
  uint32_t generation = 0;
  uint64_t start_tick = 0;
  bool operator==(const BarrierGo&) const = default;
};

struct PlayerEntry {
  uint16_t peer_id = 0;
  std::string name;
  bool operator==(const PlayerEntry&) const = default;
};

// Server to clients: who is in the session (sent when it changes).
struct PlayerList {
  std::vector<PlayerEntry> players;  // at most kMaxPlayerEntries
  bool operator==(const PlayerList&) const = default;
};

using Message = std::variant<Hello, Welcome, Refuse, Bye, Ping, Pong, LevelRequest, LevelLoad,
                             LevelLoaded, BarrierGo, PlayerList>;
MessageType TypeOf(const Message& message);

// Appends one message to `out`. false (nothing appended) if a string is over its limit or not
// UTF-8, or a list is too long.
bool EncodeMessage(const Message& message, std::vector<uint8_t>& out);
// The message alone; empty if it cannot be encoded.
std::vector<uint8_t> EncodeMessage(const Message& message);

// Every message in `bytes`, in order. nullopt if anything is cut short (a header, or a message
// whose length runs past the end), or a known message's fields are too short or invalid. Unknown
// types are skipped and counted in `skipped`.
std::optional<std::vector<Message>> DecodeMessages(std::span<const uint8_t> bytes,
                                                   size_t* skipped = nullptr);

// Well-formed UTF-8 without NUL bytes.
bool IsValidUtf8(std::string_view text);
// The longest prefix of `text` of at most `max_bytes` that does not cut a character in two.
std::string TruncateUtf8(std::string_view text, size_t max_bytes);

}  // namespace skylanders::online
