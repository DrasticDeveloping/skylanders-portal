#include "online/roster.h"

#include <algorithm>

namespace skylanders::online {

void Roster::Set(uint16_t peer, PlayerState state, bool puppet_live) {
  players_[peer] = Player{state, puppet_live};
}

void Roster::SetSynthetic(uint16_t id, bool on) {
  if (on) synthetic_.insert(id);
  else synthetic_.erase(id);
}

std::vector<uint16_t> Roster::Members() const {
  std::vector<uint16_t> out;
  for (const auto& [peer, p] : players_) {
    if (out.size() >= kMaxMembers) break;
    if (p.state == PlayerState::kPlaying && p.puppet_live) out.push_back(peer);
  }
  for (const uint16_t id : synthetic_) {
    if (out.size() >= kMaxMembers) break;
    if (std::find(out.begin(), out.end(), id) == out.end()) out.push_back(id);
  }
  return out;
}

bool Roster::IsMember(uint16_t peer) const {
  const std::vector<uint16_t> m = Members();
  return std::find(m.begin(), m.end(), peer) != m.end();
}

uint32_t Roster::HeroesAliveAnswer() const { return uint32_t(std::min<size_t>(2, Count())); }

TwoPlayerTrigger::Result TwoPlayerTrigger::Frame(const std::vector<uint16_t>& members,
                                                 const std::set<uint16_t>& inside, size_t* inside_count) {
  size_t in = 0;
  for (const uint16_t m : members) in += inside.contains(m) ? 1 : 0;
  if (inside_count) *inside_count = in;
  if (!armed_ || members.empty()) return Result::kIdle;
  if (in < members.size()) return Result::kWaiting;
  armed_ = false;  // once per occurrence
  return Result::kFired;
}

}  // namespace skylanders::online
