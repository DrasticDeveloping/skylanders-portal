#include <cstdint>
#include <vector>

#include "netplay/datagram.h"
#include "netplay/transport.h"
#include "test_util.h"

using namespace skylanders::netplay;

int main() {
  // A round trip keeps every field and the payload; the layout is the documented one.
  {
    DatagramHeader h;
    h.lane = Lane::kState;
    h.flags = 0x8000;
    h.session_id = 0xA1B2C3D4;
    h.sender = 3;
    h.sequence = 0x01020304;
    h.ack = 0x11223344;
    h.ack_bits = 0x80000001;
    const std::vector<uint8_t> payload = {1, 2, 3, 4, 5};
    std::vector<uint8_t> bytes = EncodeDatagram(h, payload);
    REQUIRE_OR_RETURN(bytes.size() == kDatagramHeaderSize + payload.size());
    CHECK(bytes[0] == 'S' && bytes[1] == 'F' && bytes[2] == 'O' && bytes[3] == '1');
    CHECK(bytes[4] == kDatagramVersion);
    CHECK(bytes[5] == 1);                      // lane
    CHECK(bytes[6] == 0x00 && bytes[7] == 0x80);  // flags, little-endian
    CHECK(bytes[8] == 0xD4 && bytes[11] == 0xA1);  // session id
    CHECK(bytes[12] == 3 && bytes[13] == 0);       // sender
    CHECK(bytes[14] == 0x04 && bytes[17] == 0x01);  // sequence
    CHECK(bytes[18] == 0x44 && bytes[21] == 0x11);  // ack
    CHECK(bytes[22] == 0x01 && bytes[25] == 0x80);  // ack bits
    CHECK(bytes[26] == 1 && bytes.back() == 5);

    auto v = DecodeDatagram(bytes);
    REQUIRE_OR_RETURN(v.has_value());
    CHECK(v->header == h);
    CHECK(std::vector<uint8_t>(v->payload.begin(), v->payload.end()) == payload);
  }

  // An empty payload and the largest one both pass; one byte more is refused by the encoder.
  {
    DatagramHeader h;
    h.sequence = 1;
    CHECK(DecodeDatagram(EncodeDatagram(h, {})).has_value());
    std::vector<uint8_t> big(kMaxPayload, 0xAB);
    auto bytes = EncodeDatagram(h, big);
    CHECK(bytes.size() == kMaxDatagramSize);
    CHECK(DecodeDatagram(bytes).has_value());
    big.push_back(0);
    std::vector<uint8_t> out;
    CHECK(!EncodeDatagram(h, big, out));
    CHECK(out.empty());
  }

  // Rejects: short, wrong magic, wrong version, unknown lane, too long, sequence 0, a malformed
  // ack-only datagram.
  {
    DatagramHeader h;
    h.sequence = 7;
    const std::vector<uint8_t> good = EncodeDatagram(h, std::vector<uint8_t>{9, 9});
    CHECK(DecodeDatagram(good).has_value());

    for (size_t n = 0; n < kDatagramHeaderSize; ++n) {
      CHECK(!DecodeDatagram(std::span<const uint8_t>(good.data(), n)).has_value());
    }
    auto bad = good;
    bad[0] = 'X';
    CHECK(!DecodeDatagram(bad).has_value());
    bad = good;
    bad[3] = '2';
    CHECK(!DecodeDatagram(bad).has_value());
    bad = good;
    bad[4] = 2;
    CHECK(!DecodeDatagram(bad).has_value());
    bad = good;
    bad[5] = 3;
    CHECK(!DecodeDatagram(bad).has_value());
    bad = good;
    bad.resize(kMaxDatagramSize + 1);
    CHECK(!DecodeDatagram(bad).has_value());

    DatagramHeader zero;
    zero.sequence = 0;
    CHECK(!DecodeDatagram(EncodeDatagram(zero, {})).has_value());

    DatagramHeader ack;
    ack.flags = kFlagAckOnly;
    ack.ack = 5;
    CHECK(DecodeDatagram(EncodeDatagram(ack, {})).has_value());
    ack.sequence = 1;
    CHECK(!DecodeDatagram(EncodeDatagram(ack, {})).has_value());
    ack.sequence = 0;
    CHECK(!DecodeDatagram(EncodeDatagram(ack, std::vector<uint8_t>{1})).has_value());

    // The encoder refuses an unknown lane.
    DatagramHeader lane;
    lane.lane = Lane(7);
    lane.sequence = 1;
    std::vector<uint8_t> out;
    CHECK(!EncodeDatagram(lane, {}, out));
  }

  // Endpoints and IPv4 text.
  {
    CHECK(ParseIpv4("127.0.0.1") == kLoopback);
    CHECK(ParseIpv4("0.0.0.0") == 0u);
    CHECK(ParseIpv4("192.168.1.20") == 0xC0A80114u);
    CHECK(!ParseIpv4("256.0.0.1"));
    CHECK(!ParseIpv4("1.2.3"));
    CHECK(!ParseIpv4("1.2.3.4.5"));
    CHECK(!ParseIpv4("1.2.3.4 "));
    CHECK(!ParseIpv4("localhost"));
    CHECK(!ParseIpv4("1..2.3"));
    CHECK(!ParseIpv4("0001.2.3.4"));
    CHECK(FormatIpv4(0xC0A80114u) == "192.168.1.20");
    CHECK((Endpoint{kLoopback, 7000}.ToString() == "127.0.0.1:7000"));
    CHECK(IsLoopback(kLoopback) && !IsLoopback(0xC0A80114u));
  }

  return Finish("netplay_datagram");
}
