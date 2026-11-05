#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <vector>

namespace skylanders::netplay {

// The wire header every datagram starts with (little-endian):
//
//   offset size
//    0      4   magic "SFO1"
//    4      1   version (kDatagramVersion)
//    5      1   lane (Lane)
//    6      2   flags (kFlag...)
//    8      4   session id (0 until the server's Welcome names it)
//   12      2   sender peer id (server 0, clients 1 to 4, kUnknownPeer before the Welcome)
//   14      4   sequence on this lane (from 1; 0 in an ack-only datagram)
//   18      4   ack: a sequence received on this lane from the other side, normally the highest
//   22      4   ack bits: bit i set = sequence (ack - 1 - i) was received too
//   26          payload
constexpr std::array<uint8_t, 4> kDatagramMagic = {'S', 'F', 'O', '1'};
constexpr uint8_t kDatagramVersion = 1;
constexpr size_t kDatagramHeaderSize = 26;
// Small enough for any path's MTU without IP fragments.
constexpr size_t kMaxDatagramSize = 1200;
constexpr size_t kMaxPayload = kMaxDatagramSize - kDatagramHeaderSize;
constexpr uint16_t kUnknownPeer = 0xFFFF;

enum class Lane : uint8_t {
  kGameplay = 0,  // reliable, ordered
  kState = 1,     // reliable, ordered
  kFast = 2,      // unreliable, newest only
};
constexpr size_t kLaneCount = 3;

// The datagram carries only acks; its sequence is 0 and its payload empty.
constexpr uint16_t kFlagAckOnly = 0x0001;

struct DatagramHeader {
  Lane lane = Lane::kGameplay;
  uint16_t flags = 0;
  uint32_t session_id = 0;
  uint16_t sender = kUnknownPeer;
  uint32_t sequence = 0;
  uint32_t ack = 0;
  uint32_t ack_bits = 0;

  bool operator==(const DatagramHeader&) const = default;
};

struct DatagramView {
  DatagramHeader header;
  std::span<const uint8_t> payload;  // points into the decoded buffer
};

// Writes the header and the payload to `out` (replacing what it held). false if the payload is
// longer than kMaxPayload or the lane is not one of the three.
bool EncodeDatagram(const DatagramHeader& header, std::span<const uint8_t> payload,
                    std::vector<uint8_t>& out);
std::vector<uint8_t> EncodeDatagram(const DatagramHeader& header,
                                    std::span<const uint8_t> payload);

// nullopt for anything that is not one of ours: shorter than the header, longer than
// kMaxDatagramSize, a wrong magic or version, an unknown lane, an ack-only datagram with a
// sequence or payload.
std::optional<DatagramView> DecodeDatagram(std::span<const uint8_t> bytes);

}  // namespace skylanders::netplay
