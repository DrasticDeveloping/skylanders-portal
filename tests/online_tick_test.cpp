#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>

#include "online/tick.h"
#include "test_util.h"

using namespace skylanders::online;
using namespace std::chrono_literals;

namespace {

struct Rng {
  uint64_t s;
  uint64_t Next() {
    uint64_t z = (s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
  }
  // Uniform in [lo, hi] microseconds.
  std::chrono::microseconds Us(int64_t lo, int64_t hi) {
    return std::chrono::microseconds(lo + int64_t(Next() % uint64_t(hi - lo + 1)));
  }
};

const TimePoint kT0 = TimePoint{} + 10h;

}  // namespace

int main() {
  // The server's tick at 60 and 30 Hz.
  {
    ServerTickClock c(60, kT0);
    CHECK(c.Hz() == 60);
    CHECK(c.TickAt(kT0) == 0);
    CHECK(c.TickAt(kT0 - 1s) == 0);
    CHECK(c.TickAt(kT0 + 16ms) == 0);
    CHECK(c.TickAt(kT0 + 17ms) == 1);
    CHECK(c.TickAt(kT0 + 1s) == 60);
    CHECK(c.TickAt(kT0 + 1h) == 216000);
    CHECK(c.TickAt(kT0 + 24h * 365) == 60ull * 3600 * 24 * 365);
    CHECK(std::abs(c.TicksAt(kT0 + 1500ms) - 90.0) < 1e-6);
    ServerTickClock h(30, kT0);
    CHECK(h.TickAt(kT0 + 33ms) == 0);
    CHECK(h.TickAt(kT0 + 34ms) == 1);
    CHECK(h.TickAt(kT0 + 1s) == 30);
    CHECK(h.TickAt(kT0 + 10min) == 18000);
    // TimeOfTick is the first moment of each tick.
    bool exact = true;
    for (uint64_t t = 0; t < 2000; ++t) {
      for (const ServerTickClock* k : {&c, &h}) {
        const TimePoint at = k->TimeOfTick(t);
        exact &= k->TickAt(at) == t;
        if (t > 0) exact &= k->TickAt(at - 1ns) == t - 1;
      }
    }
    CHECK(exact);
    c.Restart(kT0 + 1s);
    CHECK(c.TickAt(kT0 + 2s) == 60);
  }

  // The estimator converges to within one tick with jittery round trips (5 to 40 ms each way,
  // drawn separately, so the trips are lopsided), and every tenth trip stalls for 400 ms more in
  // one direction, which it ignores.
  for (uint32_t hz : {60u, 30u}) {
    const ServerTickClock server(hz, kT0 - 37'345ms);  // started long before the client
    ClientTickEstimator est(hz);
    CHECK(!est.HasEstimate());
    Rng rng{hz};
    TimePoint t = kT0;
    double worst_after_20 = 0;
    for (int i = 0; i < 120; ++i) {
      auto up = rng.Us(5'000, 40'000);
      auto down = rng.Us(5'000, 40'000);
      if (i % 10 == 9) up += 400ms;
      const uint64_t tick = server.TickAt(t + up);
      const TimePoint received = t + up + down;
      est.AddSample(t, received, tick);
      if (i >= 20) {
        const TimePoint now = received + 300ms;
        worst_after_20 = std::max(worst_after_20,
                                  std::abs(est.EstimateTicks(now) - server.TicksAt(now)));
      }
      t += 1s;
    }
    const double error = std::abs(est.EstimateTicks(t) - server.TicksAt(t));
    std::printf("%u Hz: off by %.3f ticks at the end, %.3f at worst after 20 samples, "
                "error estimate %.3f, %llu accepted, %llu ignored\n",
                hz, error, worst_after_20, est.ErrorTicks(),
                (unsigned long long)est.Accepted(), (unsigned long long)est.Ignored());
    CHECK(est.HasEstimate());
    CHECK(error <= 1.0);
    CHECK(worst_after_20 <= 1.0);
    CHECK(est.Ignored() >= 12);  // the stalls, at least
    CHECK(est.ErrorTicks() > 0.0 && est.ErrorTicks() < 1.5);
    CHECK(est.BestRtt() >= 10ms && est.BestRtt() <= 30ms);
    const auto tick_now = est.EstimateTick(t);
    CHECK(tick_now + 1 >= server.TickAt(t) && tick_now <= server.TickAt(t) + 1);
  }

  // Symmetric trips: the estimate is exact up to the half-tick of the whole-tick pongs.
  {
    const ServerTickClock server(60, kT0 - 5s);
    ClientTickEstimator est(60);
    TimePoint t = kT0;
    for (int i = 0; i < 30; ++i) {
      est.AddSample(t, t + 40ms, server.TickAt(t + 20ms));
      t += 1s;
    }
    CHECK(std::abs(est.EstimateTicks(t) - server.TicksAt(t)) <= 0.5);
  }

  // A negative round trip is ignored; a path that got slower for good is accepted again once the
  // fast trips have left the window of the last 32.
  {
    const ServerTickClock server(60, kT0);
    ClientTickEstimator est(60);
    CHECK(!est.AddSample(kT0 + 1s, kT0, 0));
    CHECK(est.Ignored() == 1 && !est.HasEstimate());
    TimePoint t = kT0;
    for (int i = 0; i < 40; ++i, t += 1s) est.AddSample(t, t + 6ms, server.TickAt(t + 3ms));
    const uint64_t accepted = est.Accepted();
    CHECK(accepted == 40);
    int refused_in_a_row = 0;
    for (int i = 0; i < 80; ++i, t += 1s) {
      if (!est.AddSample(t, t + 200ms, server.TickAt(t + 100ms))) ++refused_in_a_row;
    }
    CHECK(refused_in_a_row >= 30 && refused_in_a_row <= 32);
    CHECK(est.Accepted() > accepted);
    CHECK(std::abs(est.EstimateTicks(t) - server.TicksAt(t)) <= 1.0);
    // Reset forgets everything and takes the new rate.
    est.Reset(30);
    CHECK(!est.HasEstimate() && est.Hz() == 30 && est.Accepted() == 0);
  }

  return Finish("online_tick");
}
