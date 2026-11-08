#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <span>
#include <vector>

#include "netplay/datagram.h"
#include "netplay/transport.h"

namespace skylanders::netplay {

// How many datagrams a reliable lane holds unacked, and how far ahead of the next one to deliver
// the receiver buffers.
constexpr size_t kLaneWindow = 1024;
// The resend timeout: max(kMinResend, 1.5 x smoothed RTT). It doubles for every resend of the same
// datagram, and (Karn's algorithm) doubles for the whole connection after every Pump that had to
// resend, up to kMaxBackoff times, until a datagram sent only once is acked; the result is capped
// at max(the plain timeout, kMaxResend). The RTT is sampled only from datagrams sent once (which
// send a resent one's ack answers is unknown); the connection backoff is what lets the estimate
// catch up when the path gets slower than the timeout. Before the first sample the RTT is taken as
// kInitialRtt.
constexpr auto kMinResend = std::chrono::milliseconds(50);
constexpr auto kMaxResend = std::chrono::milliseconds(1000);
constexpr auto kInitialRtt = std::chrono::milliseconds(100);
constexpr uint32_t kMaxBackoff = 16;

enum class SendResult {
  kSent,
  kWindowFull,  // a reliable lane already holds kLaneWindow unacked datagrams; nothing was queued
  kTooBig,      // longer than kMaxPayload
};

struct LaneMessage {
  Lane lane = Lane::kGameplay;
  std::vector<uint8_t> payload;
};

// The bytes of one lane, whole datagrams as they go on the wire (the kDatagramHeaderSize header
// included), first sends and resends apart (design 1B section 5, "Traffic").
// Bytes per lane, whole datagrams (header and payload).
struct LaneBytes {
  uint64_t sent = 0;      // bytes of data datagrams, first sends
  uint64_t resent = 0;    // bytes of data datagrams sent again
  uint64_t acks = 0;      // bytes of ack-only datagrams
  uint64_t received = 0;  // bytes fed in (data and ack-only, duplicates and stale ones too)
};

struct ConnectionStats {
  uint64_t sent = 0;          // data datagrams, first sends
  uint64_t resent = 0;        // data datagrams sent again
  uint64_t acks_sent = 0;     // ack-only datagrams
  uint64_t received = 0;      // datagrams fed in
  uint64_t delivered = 0;     // messages handed to Receive
  uint64_t duplicates = 0;    // reliable datagrams received twice
  uint64_t stale = 0;         // fast datagrams older than the newest, dropped
  uint64_t out_of_window = 0; // reliable datagrams too far ahead, dropped
  uint64_t refused = 0;       // Send calls that returned kWindowFull or kTooBig
  std::array<LaneBytes, kLaneCount> lane_bytes{};  // by Lane value

  const LaneBytes& Bytes(Lane lane) const { return lane_bytes[size_t(lane) % kLaneCount]; }
  // Every lane: first sends, resends and acks.
  uint64_t BytesSent() const {
    uint64_t n = 0;
    for (const LaneBytes& b : lane_bytes) n += b.sent + b.resent + b.acks;
    return n;
  }
  uint64_t BytesReceived() const {
    uint64_t n = 0;
    for (const LaneBytes& b : lane_bytes) n += b.received;
    return n;
  }
};

// The lanes to one remote peer, over a send function the owner supplies. No threads and no
// clock of its own: the owner feeds received datagrams (OnDatagram), calls Pump often (every frame)
// for resends and acks, and takes delivered messages with Receive. `now` defaults to
// std::chrono::steady_clock; tests pass their own time points. Not thread-safe: one thread at a time.
//
// The gameplay and state lanes are reliable and ordered: every message arrives once, in the order
// sent. The fast lane is unreliable: a datagram older than the newest one received is dropped.
//
// Acks: each datagram carries, for its own lane, the highest sequence received from the other side
// and a bit for each of the 32 before it. When a reliable lane received something and has nothing
// to send, Pump sends an ack-only datagram. What arrived more than 32 below the highest (a burst of
// more than 33 between two Pumps, a resend that fills an old gap) is acked by ack-only datagrams
// whose ack field is such a sequence instead of the highest, so nothing goes unacked.
class Connection {
 public:
  using SendFunction = std::function<void(std::span<const uint8_t> datagram)>;

  explicit Connection(SendFunction send, TimePoint now = Clock::now());

  // The header fields of what this side sends from now on (resends included).
  void SetSessionId(uint32_t id) { session_id_ = id; }
  void SetLocalPeer(uint16_t id) { local_peer_ = id; }
  uint32_t SessionId() const { return session_id_; }

  // Sends `payload` on `lane` at once. A reliable lane keeps it until it is acked.
  SendResult Send(Lane lane, std::span<const uint8_t> payload, TimePoint now = Clock::now());
  // Feeds a datagram from this peer (decoded with DecodeDatagram).
  void OnDatagram(const DatagramView& datagram, TimePoint now = Clock::now());
  // Resends what is due and sends the acks owed.
  void Pump(TimePoint now = Clock::now());
  // Takes the next delivered message; false when there is none.
  bool Receive(LaneMessage& out);

  // The smoothed round trip from acks of datagrams sent once (kInitialRtt before the first).
  std::chrono::microseconds SmoothedRtt() const;
  bool HasRttSample() const { return rtt_samples_ > 0; }
  std::chrono::microseconds ResendTimeout() const;
  size_t Unacked(Lane lane) const;
  size_t UnackedTotal() const;
  // The last time a datagram from this peer was fed in (the construction time before that).
  TimePoint LastReceive() const { return last_receive_; }
  void SetLastReceive(TimePoint t) { last_receive_ = t; }
  const ConnectionStats& Stats() const { return stats_; }

 private:
  struct Pending {
    std::vector<uint8_t> payload;
    TimePoint first_sent;
    TimePoint last_sent;
    uint32_t sends = 0;
  };
  struct ReliableLane {
    // Sending side.
    uint32_t next_sequence = 1;
    std::map<uint32_t, Pending> unacked;
    // Receiving side: everything below next_deliver has been delivered.
    uint32_t next_deliver = 1;
    uint32_t highest = 0;
    std::map<uint32_t, std::vector<uint8_t>> buffered;
    bool ack_due = false;
    std::vector<uint32_t> to_ack;  // received since the last Pump
  };
  struct FastLane {
    uint32_t next_sequence = 1;
    uint32_t highest = 0;
    uint32_t bits = 0;  // the 32 before `highest`, for the ack fields
  };

  static bool IsReliable(Lane lane) { return lane != Lane::kFast; }
  ReliableLane& Reliable(Lane lane) { return reliable_[lane == Lane::kState ? 1 : 0]; }
  const ReliableLane& Reliable(Lane lane) const { return reliable_[lane == Lane::kState ? 1 : 0]; }
  static bool WasReceived(const ReliableLane& l, uint32_t sequence);
  static uint32_t AckBits(const ReliableLane& l, uint32_t ack);
  // Both return the datagram's size (0 if it could not be encoded).
  size_t Transmit(Lane lane, uint16_t flags, uint32_t sequence, uint32_t ack, uint32_t ack_bits,
                  std::span<const uint8_t> payload);
  size_t TransmitData(Lane lane, uint32_t sequence, std::span<const uint8_t> payload);
  LaneBytes& BytesOf(Lane lane) { return stats_.lane_bytes[size_t(lane) % kLaneCount]; }
  void HandleAck(ReliableLane& l, uint32_t ack, uint32_t bits, TimePoint now);
  void AddRttSample(std::chrono::microseconds sample);
  std::chrono::microseconds TimeoutFor(const Pending& p) const;

  SendFunction send_;
  uint32_t session_id_ = 0;
  uint16_t local_peer_ = kUnknownPeer;
  std::array<ReliableLane, 2> reliable_;
  FastLane fast_;
  std::deque<LaneMessage> delivered_;
  double srtt_us_ = 0;
  uint64_t rtt_samples_ = 0;
  uint32_t backoff_ = 1;
  TimePoint last_receive_;
  ConnectionStats stats_;
  std::vector<uint8_t> scratch_;
};

}  // namespace skylanders::netplay
