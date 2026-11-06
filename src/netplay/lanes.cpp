#include "netplay/lanes.h"

#include <algorithm>
#include <functional>
#include <utility>

namespace skylanders::netplay {

using std::chrono::duration_cast;
using std::chrono::microseconds;

Connection::Connection(SendFunction send, TimePoint now)
    : send_(std::move(send)), last_receive_(now) {}

SendResult Connection::Send(Lane lane, std::span<const uint8_t> payload, TimePoint now) {
  if (payload.size() > kMaxPayload) {
    ++stats_.refused;
    return SendResult::kTooBig;
  }
  if (!IsReliable(lane)) {
    Transmit(Lane::kFast, 0, fast_.next_sequence++, fast_.highest, fast_.bits, payload);
    ++stats_.sent;
    return SendResult::kSent;
  }
  ReliableLane& l = Reliable(lane);
  // A sliding window: the newest sequence stays within kLaneWindow of the oldest unacked one, so
  // the receiver's buffer always has room for it.
  if (!l.unacked.empty() && l.next_sequence - l.unacked.begin()->first >= kLaneWindow) {
    ++stats_.refused;
    return SendResult::kWindowFull;
  }
  const uint32_t sequence = l.next_sequence++;
  Pending& p = l.unacked[sequence];
  p.payload.assign(payload.begin(), payload.end());
  p.first_sent = p.last_sent = now;
  p.sends = 1;
  TransmitData(lane, sequence, p.payload);
  ++stats_.sent;
  return SendResult::kSent;
}

bool Connection::WasReceived(const ReliableLane& l, uint32_t sequence) {
  return sequence != 0 && (sequence < l.next_deliver || l.buffered.contains(sequence));
}

uint32_t Connection::AckBits(const ReliableLane& l, uint32_t ack) {
  uint32_t bits = 0;
  for (uint32_t i = 0; i < 32 && ack > i + 1; ++i) {
    if (WasReceived(l, ack - 1 - i)) bits |= 1u << i;
  }
  return bits;
}

void Connection::Transmit(Lane lane, uint16_t flags, uint32_t sequence, uint32_t ack,
                          uint32_t ack_bits, std::span<const uint8_t> payload) {
  DatagramHeader h;
  h.lane = lane;
  h.flags = flags;
  h.session_id = session_id_;
  h.sender = local_peer_;
  h.sequence = sequence;
  h.ack = ack;
  h.ack_bits = ack_bits;
  if (EncodeDatagram(h, payload, scratch_) && send_) send_(scratch_);
}

void Connection::TransmitData(Lane lane, uint32_t sequence, std::span<const uint8_t> payload) {
  ReliableLane& l = Reliable(lane);
  Transmit(lane, 0, sequence, l.highest, AckBits(l, l.highest), payload);
  l.ack_due = false;  // this datagram carries the ack
}

void Connection::AddRttSample(microseconds sample) {
  const double us = double(std::max<int64_t>(sample.count(), 0));
  srtt_us_ = rtt_samples_ == 0 ? us : srtt_us_ + (us - srtt_us_) / 8.0;
  ++rtt_samples_;
}

void Connection::HandleAck(ReliableLane& l, uint32_t ack, uint32_t bits, TimePoint now) {
  if (ack == 0 || l.unacked.empty()) return;
  // RTT samples come only from the ack field's own sequence, the one whose arrival made the other
  // side ack; the bits may answer datagrams whose first ack was lost, which would read long.
  auto it = l.unacked.find(ack);
  if (it != l.unacked.end()) {
    if (it->second.sends == 1) {
      AddRttSample(duration_cast<microseconds>(now - it->second.first_sent));
      backoff_ = 1;
    }
    l.unacked.erase(it);
  }
  for (uint32_t i = 0; i < 32 && ack > i + 1; ++i) {
    if (bits & (1u << i)) l.unacked.erase(ack - 1 - i);
  }
}

void Connection::OnDatagram(const DatagramView& v, TimePoint now) {
  last_receive_ = now;
  ++stats_.received;
  const DatagramHeader& h = v.header;

  if (h.lane == Lane::kFast) {
    if (h.flags & kFlagAckOnly) return;
    const uint32_t s = h.sequence;
    if (s > fast_.highest) {
      // Shift the history by the step; the old newest becomes bit d - 1.
      const uint32_t d = s - fast_.highest;
      uint32_t bits = 0;
      if (fast_.highest != 0 && d <= 32) bits = (d == 32 ? 0 : fast_.bits << d) | (1u << (d - 1));
      fast_.bits = bits;
      fast_.highest = s;
      delivered_.push_back(LaneMessage{Lane::kFast, {v.payload.begin(), v.payload.end()}});
      ++stats_.delivered;
    } else {
      if (s < fast_.highest && fast_.highest - s <= 32) fast_.bits |= 1u << (fast_.highest - s - 1);
      ++stats_.stale;
    }
    return;
  }

  ReliableLane& l = Reliable(h.lane);
  HandleAck(l, h.ack, h.ack_bits, now);
  if (h.flags & kFlagAckOnly) return;

  const uint32_t s = h.sequence;
  if (WasReceived(l, s)) {
    // The sender has not seen our ack yet: ack it again.
    ++stats_.duplicates;
    l.ack_due = true;
    l.to_ack.push_back(s);
    return;
  }
  if (s - l.next_deliver >= kLaneWindow) {
    ++stats_.out_of_window;  // not acked, so the sender sends it again later
    return;
  }
  l.buffered.emplace(s, std::vector<uint8_t>(v.payload.begin(), v.payload.end()));
  l.highest = std::max(l.highest, s);
  l.ack_due = true;
  l.to_ack.push_back(s);
  for (auto it = l.buffered.find(l.next_deliver); it != l.buffered.end();
       it = l.buffered.find(l.next_deliver)) {
    delivered_.push_back(LaneMessage{h.lane, std::move(it->second)});
    l.buffered.erase(it);
    ++l.next_deliver;
    ++stats_.delivered;
  }
}

void Connection::Pump(TimePoint now) {
  bool resent = false;
  for (Lane lane : {Lane::kGameplay, Lane::kState}) {
    ReliableLane& l = Reliable(lane);
    for (auto& [sequence, p] : l.unacked) {
      if (now - p.last_sent < TimeoutFor(p)) continue;
      p.last_sent = now;
      ++p.sends;
      TransmitData(lane, sequence, p.payload);
      ++stats_.resent;
      resent = true;
    }
    if (l.ack_due) {
      Transmit(lane, kFlagAckOnly, 0, l.highest, AckBits(l, l.highest), {});
      ++stats_.acks_sent;
      l.ack_due = false;
    }
    if (!l.to_ack.empty()) {
      // The normal ack covers highest and the 32 before it. What arrived below that since the
      // last Pump (a burst of more than 33, a resend filling an old gap) gets ack-only datagrams
      // anchored lower down, each covering its own 33.
      std::sort(l.to_ack.begin(), l.to_ack.end(), std::greater<>());
      uint32_t covered_from = l.highest > 32 ? l.highest - 32 : 1;
      for (uint32_t a : l.to_ack) {
        if (a >= covered_from) continue;
        Transmit(lane, kFlagAckOnly, 0, a, AckBits(l, a), {});
        ++stats_.acks_sent;
        covered_from = a > 32 ? a - 32 : 1;
      }
      l.to_ack.clear();
    }
  }
  if (resent) backoff_ = std::min(backoff_ * 2, kMaxBackoff);
}

bool Connection::Receive(LaneMessage& out) {
  if (delivered_.empty()) return false;
  out = std::move(delivered_.front());
  delivered_.pop_front();
  return true;
}

microseconds Connection::SmoothedRtt() const {
  if (rtt_samples_ == 0) return duration_cast<microseconds>(kInitialRtt);
  return microseconds(int64_t(srtt_us_ + 0.5));
}

microseconds Connection::ResendTimeout() const {
  const auto from_rtt = microseconds(int64_t(1.5 * double(SmoothedRtt().count())));
  return std::max(duration_cast<microseconds>(kMinResend), from_rtt);
}

microseconds Connection::TimeoutFor(const Pending& p) const {
  const microseconds base = ResendTimeout();
  const microseconds cap = std::max(base, duration_cast<microseconds>(kMaxResend));
  microseconds t = base * backoff_;
  for (uint32_t i = 1; i < p.sends && t < cap; ++i) t *= 2;
  return std::min(t, cap);
}

size_t Connection::Unacked(Lane lane) const {
  return IsReliable(lane) ? Reliable(lane).unacked.size() : 0;
}

size_t Connection::UnackedTotal() const {
  return reliable_[0].unacked.size() + reliable_[1].unacked.size();
}

}  // namespace skylanders::netplay
