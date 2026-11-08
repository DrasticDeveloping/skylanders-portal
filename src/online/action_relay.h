#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <map>
#include <utility>

#include "online/tick.h"

// One remote player's puppet's attack presentation on a client (stage 4, docs/online-4.md section
// 4.4, D7). S relays every attack press and release it took from a peer as PlayerActionShown; the
// receiver keeps state per action key (peer, peer epoch, generation, press sequence, slot). One
// PuppetActionRelay is one puppet, so peer is fixed and peer epoch and generation are the table's
// (SetEpoch); a key inside the table is (press sequence, slot).
//
//   none --press--> pressed --release--> released (terminal)
//
// - Press and release are deduplicated apart: a press of a key already pressed or released is
//   dropped `duplicate press`; a release of a key already released is `duplicate release`.
// - An action of another generation than the table's is dropped `generation`, press or release.
// - Stale applies to presses only: a press more than kStale of shared time older than the
//   puppet's displayed pose is never pressed; its key goes straight to released (`stale`) and its
//   later release is consumed silently. A release is never stale and never dropped because a newer
//   press came first.
// - A press is taken by the puppet's next think (Think). A release that comes before its press is
//   taken arms a release-after: the press goes in for that one think and ends (S's rule, server
//   attacks.cpp). A release of a key in state none is recorded released, so a press after it is
//   dropped `duplicate press`.
// - The slot's hold: every pressed and taken key holds its slot's bit, so a late release of an older
//   press ends only that press while a newer press of the slot still holds it (S's OtherHolder).
// - A key pressed longer than kMaxHold is released (`timeout`). Teardown (despawn, body change)
//   releases every key (`teardown`); a new peer epoch or generation drops the table after that.
// - At most kMaxKeys keys: released keys are evicted first, oldest first; with none released the
//   oldest pressed key is released (`teardown`) and evicted.
//
// Not thread-safe: the game thread owns it (the receive hands actions over in order).

namespace skylanders::online {

class PuppetActionRelay {
 public:
  static constexpr size_t kMaxKeys = 32;
  static constexpr std::chrono::milliseconds kStale{500};
  static constexpr std::chrono::milliseconds kMaxHold{5000};
  static constexpr uint8_t kSlots = 8;  // slot numbers 0..7 (the game uses 0..2)

  enum class Drop : uint8_t { kNone, kDuplicatePress, kDuplicateRelease, kStale, kGeneration, kSlot };
  enum class ReleaseWhy : uint8_t { kRelease, kReleaseAfter, kTimeout, kTeardown };

  struct Event {
    enum class Type : uint8_t { kStarted, kReleased, kDropped };
    Type type = Type::kStarted;
    uint32_t press_sequence = 0;
    uint8_t slot = 0;
    ReleaseWhy why = ReleaseWhy::kRelease;  // kReleased
    Drop drop = Drop::kNone;                // kDropped
  };

  // The puppet's peer epoch and the client's generation. A change of either tears every key down
  // (released, `teardown`) and drops the table.
  void SetEpoch(uint32_t peer_epoch, uint32_t generation);

  // One PlayerActionShown, in receive order. `press_age` is how much older (shared time) the
  // action's pose is than the puppet's displayed pose; only a press reads it. Returns the drop, or
  // kNone when the action changed state (or a stale press's release was consumed).
  Drop OnAction(uint32_t generation, uint32_t press_sequence, uint8_t slot, bool release,
                std::chrono::milliseconds press_age, TimePoint now);

  // The puppet's think: hold timeouts, then every pressed key not yet taken goes in (kStarted),
  // then the release-afters end after this think. Returns the slot bits to hold in this think (bit
  // n: slot n).
  uint8_t Think(TimePoint now);

  // Despawn, a body change: every key released (`teardown`), the table kept (so a late action of a
  // torn-down key is a duplicate, not a new press).
  void Teardown();

  bool PollEvent(Event& out);
  // The bits a think would hold now (without taking anything).
  uint8_t HeldSlots() const;
  size_t Keys() const { return keys_.size(); }

 private:
  enum class State : uint8_t { kPressed, kReleased };
  struct Key {
    State state = State::kPressed;
    bool taken = false;
    bool release_after = false;
    bool stale = false;  // a stale press's key: its first release is consumed silently
    TimePoint pressed_at{};
    uint64_t order = 0;
  };
  using Id = std::pair<uint32_t, uint8_t>;  // press sequence, slot

  void Release(const Id& id, Key& k, ReleaseWhy why);
  Key& Insert(const Id& id);
  void Emit(Event::Type type, const Id& id, ReleaseWhy why = ReleaseWhy::kRelease, Drop drop = Drop::kNone);

  uint32_t peer_epoch_ = 0;
  uint32_t generation_ = 0;
  uint64_t next_order_ = 0;
  std::map<Id, Key> keys_;
  std::deque<Event> events_;
};

}  // namespace skylanders::online
