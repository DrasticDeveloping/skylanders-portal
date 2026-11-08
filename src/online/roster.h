#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <set>
#include <vector>

// S's roster and the two-player trigger replacement (stage 4, docs/online-4.md section 4.6, D12,
// D14). Pure: the game's census and trigger hooks feed it and act on its answers.
//
// The roster: the players whose `player` row says `playing` and whose puppet on S is live, at most
// kMaxMembers. A player swapping, KO'd or gone is out: not counted alive, not listed, not
// targetable. getNumHeroesAlive keeps 1B's answer, min(2, members), until E11 reads its readers.
//
// The trigger replacement, one per trigger marked two-player:
// - Membership is S's own containment test of roster members' puppets each S frame (the caller's
//   `inside` set), never a request, so synthetic and placed members count.
// - An occurrence runs from the trigger's arm (level start, or its native re-arm) to its firing;
//   it fires once: when the roster is non-empty and every member is inside. An empty roster never
//   fires. A member leaving the roster is dropped from the count at once, so the others firing then
//   is a firing. A generation change ends the occurrence until the next arm.

namespace skylanders::online {

enum class PlayerState : uint8_t { kPlaying, kSwapping, kKo, kLeft };

class Roster {
 public:
  static constexpr size_t kMaxMembers = 4;

  // The `player` row's state for `peer` and whether its puppet on S is live.
  void Set(uint16_t peer, PlayerState state, bool puppet_live);
  void Remove(uint16_t peer) { players_.erase(peer); }
  // Dev only (E11, dev_roster_synthetic): a placed puppet counted as a member.
  void SetSynthetic(uint16_t id, bool on);

  // The members, by peer id (synthetic ids after the players), at most kMaxMembers.
  std::vector<uint16_t> Members() const;
  bool IsMember(uint16_t peer) const;
  size_t Count() const { return Members().size(); }
  // 1B's answer for getNumHeroesAlive until E11: min(2, members).
  uint32_t HeroesAliveAnswer() const;

 private:
  struct Player {
    PlayerState state = PlayerState::kLeft;
    bool puppet_live = false;
  };
  std::map<uint16_t, Player> players_;
  std::set<uint16_t> synthetic_;
};

class TwoPlayerTrigger {
 public:
  enum class Result : uint8_t { kIdle, kWaiting, kFired };

  // Level start or the trigger's native re-arm: a new occurrence.
  void Arm() { armed_ = true; }
  // A generation change: no occurrence until the next Arm.
  void OnGeneration() { armed_ = false; }
  bool Armed() const { return armed_; }

  // One S frame: `members` (Roster::Members) and the members whose puppets are inside the volume.
  // kFired once per occurrence, when every member is inside; kWaiting while armed and not all are;
  // kIdle when not armed or the roster is empty. `inside_count` (if set) gets how many members are
  // inside, for `all entered (<n> of <n>)`.
  Result Frame(const std::vector<uint16_t>& members, const std::set<uint16_t>& inside,
               size_t* inside_count = nullptr);

 private:
  bool armed_ = false;
};

}  // namespace skylanders::online
