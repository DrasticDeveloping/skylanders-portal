#include "online/pose_rate.h"

#include <algorithm>
#include <cmath>

namespace skylanders::online {

PoseRateCheck::Verdict PoseRateCheck::Check(uint16_t peer, uint32_t generation, uint32_t pose_sequence,
                                            const std::array<float, 3>& position, uint16_t teleports, TimePoint now,
                                            float* speed) {
  if (speed) *speed = 0;
  auto accept = [&](Verdict v) {
    last_[peer] = Seen{generation, pose_sequence, position, teleports, now};
    return v;
  };
  const auto it = last_.find(peer);
  if (it == last_.end() || it->second.generation != generation) return accept(Verdict::kFirst);
  const Seen& l = it->second;
  if (teleports != l.teleports) return accept(Verdict::kTeleport);
  const double dx = double(position[0]) - l.position[0];
  const double dy = double(position[1]) - l.position[1];
  const double dz = double(position[2]) - l.position[2];
  const double distance = std::sqrt(dx * dx + dy * dy + dz * dz);
  const uint32_t gap = pose_sequence > l.sequence ? pose_sequence - l.sequence : 1;
  const double arrival = std::chrono::duration<double>(now - l.at).count();
  const double made = std::chrono::duration<double>(interval_).count() * gap;
  const double seconds = std::max({arrival, made, 1e-3});
  const double v = distance / seconds;
  if (speed) *speed = float(v);
  if (v > max_speed_) return Verdict::kTooFast;  // dropped: the last accepted pose stays
  return accept(Verdict::kAccepted);
}

bool PoseRateCheck::Last(uint16_t peer, std::array<float, 3>* position) const {
  const auto it = last_.find(peer);
  if (it == last_.end()) return false;
  if (position) *position = it->second.position;
  return true;
}

}  // namespace skylanders::online
