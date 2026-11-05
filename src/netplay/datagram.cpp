#include "netplay/datagram.h"

#include <algorithm>

#include "netplay/bytes.h"

namespace skylanders::netplay {

bool EncodeDatagram(const DatagramHeader& h, std::span<const uint8_t> payload,
                    std::vector<uint8_t>& out) {
  out.clear();
  if (payload.size() > kMaxPayload || uint8_t(h.lane) >= kLaneCount) return false;
  out.reserve(kDatagramHeaderSize + payload.size());
  ByteWriter w(out);
  w.Bytes(kDatagramMagic);
  w.U8(kDatagramVersion);
  w.U8(uint8_t(h.lane));
  w.U16(h.flags);
  w.U32(h.session_id);
  w.U16(h.sender);
  w.U32(h.sequence);
  w.U32(h.ack);
  w.U32(h.ack_bits);
  w.Bytes(payload);
  return true;
}

std::vector<uint8_t> EncodeDatagram(const DatagramHeader& header,
                                    std::span<const uint8_t> payload) {
  std::vector<uint8_t> out;
  EncodeDatagram(header, payload, out);
  return out;
}

std::optional<DatagramView> DecodeDatagram(std::span<const uint8_t> bytes) {
  if (bytes.size() < kDatagramHeaderSize || bytes.size() > kMaxDatagramSize) return std::nullopt;
  if (!std::equal(kDatagramMagic.begin(), kDatagramMagic.end(), bytes.begin())) return std::nullopt;
  ByteReader r(bytes.subspan(4));
  if (r.U8() != kDatagramVersion) return std::nullopt;
  const uint8_t lane = r.U8();
  if (lane >= kLaneCount) return std::nullopt;
  DatagramView v;
  v.header.lane = Lane(lane);
  v.header.flags = r.U16();
  v.header.session_id = r.U32();
  v.header.sender = r.U16();
  v.header.sequence = r.U32();
  v.header.ack = r.U32();
  v.header.ack_bits = r.U32();
  if (!r.Ok()) return std::nullopt;
  v.payload = bytes.subspan(kDatagramHeaderSize);
  if (v.header.flags & kFlagAckOnly) {
    if (v.header.sequence != 0 || !v.payload.empty()) return std::nullopt;
  } else if (v.header.sequence == 0) {
    return std::nullopt;  // sequences start at 1
  }
  return v;
}

}  // namespace skylanders::netplay
