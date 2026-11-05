#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>

#include "netplay/transport.h"

namespace skylanders::online {

using netplay::Clock;
using netplay::TimePoint;

// The server's tick: whole ticks at `hz` since `start` on a steady clock (tick 0 is the start).
class ServerTickClock {
 public:
  explicit ServerTickClock(uint32_t hz = 60, TimePoint start = Clock::now());

  void Restart(TimePoint start) { start_ = start; }
  uint32_t Hz() const { return hz_; }
  TimePoint Start() const { return start_; }
  // 0 before the start.
  uint64_t TickAt(TimePoint now = Clock::now()) const;
  // The same with the fraction of the running tick.
  double TicksAt(TimePoint now) const;
  // The first moment TickAt returns `tick`.
  TimePoint TimeOfTick(uint64_t tick) const;

 private:
  uint32_t hz_;
  TimePoint start_;
};

// A client's estimate of the server's tick, from pongs. A pong answers a ping sent at
// `client_send` and arrives at `client_receive` with the server's tick; the server read that tick
// about half the round trip ago, so the tick now is server_tick + (rtt / 2) x hz (plus half a tick:
// the server sent a whole tick and is on average halfway through it). The estimator keeps the
// difference between that and its own clock, averaged (the plain mean over the first 10 samples,
// then an exponential average weighting each new one 1/10).
//
// A sample whose round trip is more than 3x the best one is ignored: a long trip is usually long in
// one direction, which the half-RTT rule gets wrong. "The best" is the shortest of the last 32
// round trips (so a path that got slower for good is accepted again after a while), and never
// below 5 ms (on loopback the trips are a frame or two, and 3x a lucky 0.2 ms would refuse all).
class ClientTickEstimator {
 public:
  static constexpr size_t kBestWindow = 32;
  static constexpr auto kRttFloor = std::chrono::milliseconds(5);

  explicit ClientTickEstimator(uint32_t hz = 60) : hz_(hz) {}
  // Forgets every sample; `hz` is the server's tick rate (from its Welcome).
  void Reset(uint32_t hz);

  // false: the sample was ignored (a negative or too long round trip).
  bool AddSample(TimePoint client_send, TimePoint client_receive, uint64_t server_tick);

  bool HasEstimate() const { return accepted_ > 0; }
  // The server's tick at `now` on this client's clock, with the fraction (0 without an estimate).
  double EstimateTicks(TimePoint now) const;
  uint64_t EstimateTick(TimePoint now) const;
  // How far off the estimate is likely to be, in ticks: the average distance of the samples from
  // the estimate (after the first sample: half its round trip plus half a tick).
  double ErrorTicks() const { return error_; }
  uint32_t Hz() const { return hz_; }
  std::chrono::microseconds BestRtt() const;
  std::chrono::microseconds LastRtt() const { return std::chrono::microseconds(last_rtt_us_); }
  uint64_t Accepted() const { return accepted_; }
  uint64_t Ignored() const { return ignored_; }

 private:
  uint32_t hz_;
  TimePoint origin_{};   // the first accepted sample's receive time
  double offset_ = 0;    // server ticks at origin_
  double error_ = 0;
  uint64_t accepted_ = 0;
  uint64_t ignored_ = 0;
  int64_t last_rtt_us_ = 0;
  std::deque<int64_t> recent_rtts_us_;
};

}  // namespace skylanders::online
