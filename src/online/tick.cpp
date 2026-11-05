#include "online/tick.h"

#include <algorithm>
#include <cmath>

namespace skylanders::online {

using std::chrono::duration;
using std::chrono::duration_cast;
using std::chrono::microseconds;
using std::chrono::nanoseconds;

namespace {
constexpr int64_t kNsPerSecond = 1'000'000'000;
}

ServerTickClock::ServerTickClock(uint32_t hz, TimePoint start)
    : hz_(std::max<uint32_t>(hz, 1)), start_(start) {}

uint64_t ServerTickClock::TickAt(TimePoint now) const {
  const int64_t ns = duration_cast<nanoseconds>(now - start_).count();
  if (ns <= 0) return 0;
  // Split so the product never overflows.
  return uint64_t(ns / kNsPerSecond) * hz_ + uint64_t((ns % kNsPerSecond) * hz_ / kNsPerSecond);
}

double ServerTickClock::TicksAt(TimePoint now) const {
  const double s = duration<double>(now - start_).count();
  return s <= 0 ? 0.0 : s * hz_;
}

TimePoint ServerTickClock::TimeOfTick(uint64_t tick) const {
  // Rounded up, so TickAt(TimeOfTick(t)) == t.
  const uint64_t whole = tick / hz_;
  const uint64_t rest = tick % hz_;
  const int64_t ns = int64_t(whole) * kNsPerSecond + int64_t((rest * kNsPerSecond + hz_ - 1) / hz_);
  return start_ + duration_cast<Clock::duration>(nanoseconds(ns));
}

void ClientTickEstimator::Reset(uint32_t hz) {
  *this = ClientTickEstimator(std::max<uint32_t>(hz, 1));
}

bool ClientTickEstimator::AddSample(TimePoint client_send, TimePoint client_receive,
                                    uint64_t server_tick) {
  const int64_t rtt_us = duration_cast<microseconds>(client_receive - client_send).count();
  if (rtt_us < 0) {
    ++ignored_;
    return false;
  }
  last_rtt_us_ = rtt_us;
  recent_rtts_us_.push_back(rtt_us);
  if (recent_rtts_us_.size() > kBestWindow) recent_rtts_us_.pop_front();
  const int64_t best = *std::min_element(recent_rtts_us_.begin(), recent_rtts_us_.end());
  const int64_t floor_us = duration_cast<microseconds>(kRttFloor).count();
  if (accepted_ > 0 && rtt_us > 3 * std::max(best, floor_us)) {
    ++ignored_;
    return false;
  }

  if (accepted_ == 0) origin_ = client_receive;
  const double half_rtt_ticks = double(rtt_us) / 2e6 * hz_;
  const double client_ticks = duration<double>(client_receive - origin_).count() * hz_;
  const double sample = double(server_tick) + 0.5 + half_rtt_ticks - client_ticks;

  ++accepted_;
  if (accepted_ == 1) {
    offset_ = sample;
    error_ = half_rtt_ticks + 0.5;
    return true;
  }
  const double weight = std::max(1.0 / double(accepted_), 0.1);
  const double deviation = std::abs(sample - offset_);
  offset_ += weight * (sample - offset_);
  error_ += weight * (deviation - error_);
  return true;
}

double ClientTickEstimator::EstimateTicks(TimePoint now) const {
  if (accepted_ == 0) return 0.0;
  return std::max(0.0, offset_ + duration<double>(now - origin_).count() * hz_);
}

uint64_t ClientTickEstimator::EstimateTick(TimePoint now) const {
  return uint64_t(std::floor(EstimateTicks(now)));
}

microseconds ClientTickEstimator::BestRtt() const {
  if (recent_rtts_us_.empty()) return microseconds(0);
  return microseconds(*std::min_element(recent_rtts_us_.begin(), recent_rtts_us_.end()));
}

}  // namespace skylanders::online
