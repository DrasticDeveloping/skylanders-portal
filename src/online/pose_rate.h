#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <map>

#include "online/tick.h"

// S's rate check on player poses (stage 4, docs/online-4.md section 4.3, D6; plan 2.1). S drops a
// pose that implies a speed above max_speed unless its teleport counter moved, logs `Online: pose
// dropped <peer> (<speed>)`, and keeps the last accepted pose for proximity and the relay.
//
// The time between two poses is the larger of the arrival gap and the sequence gap times the
// sender's pose interval (50 ms at 20 Hz): two poses that arrive together after a network stall
// were made an interval apart, so a burst never reads as a jump. The first pose of a peer in a
// generation is accepted; a pose of a new generation starts again.
//
// Not thread-safe: S's pump owns it.

namespace skylanders::online {

class PoseRateCheck {
 public:
  enum class Verdict : uint8_t { kAccepted, kFirst, kTeleport, kTooFast };

  PoseRateCheck(float max_speed, std::chrono::milliseconds pose_interval = std::chrono::milliseconds(50))
      : max_speed_(max_speed), interval_(pose_interval) {}

  // One pose of `peer`, already known newer than the last (the session drops older ones).
  // `speed` (if set) gets the implied speed in units per second (0 for kFirst and kTeleport).
  Verdict Check(uint16_t peer, uint32_t generation, uint32_t pose_sequence, const std::array<float, 3>& position,
                uint16_t teleports, TimePoint now, float* speed = nullptr);

  // The peer left: its last pose is forgotten.
  void Forget(uint16_t peer) { last_.erase(peer); }

  // The last accepted position of `peer`; false if none.
  bool Last(uint16_t peer, std::array<float, 3>* position) const;

 private:
  struct Seen {
    uint32_t generation = 0;
    uint32_t sequence = 0;
    std::array<float, 3> position{};
    uint16_t teleports = 0;
    TimePoint at{};
  };
  float max_speed_;
  std::chrono::milliseconds interval_;
  std::map<uint16_t, Seen> last_;
};

}  // namespace skylanders::online
