#include "online/action_relay.h"

namespace skylanders::online {

void PuppetActionRelay::Emit(Event::Type type, const Id& id, ReleaseWhy why, Drop drop) {
  Event e;
  e.type = type;
  e.press_sequence = id.first;
  e.slot = id.second;
  e.why = why;
  e.drop = drop;
  events_.push_back(e);
}

void PuppetActionRelay::Release(const Id& id, Key& k, ReleaseWhy why) {
  if (k.state == State::kReleased) return;
  k.state = State::kReleased;
  k.release_after = false;
  // A key never taken never held its slot: nothing to report as released for the puppet, except
  // that its life ended; the release line is for a key whose slot bit it held.
  if (k.taken) Emit(Event::Type::kReleased, id, why);
}

PuppetActionRelay::Key& PuppetActionRelay::Insert(const Id& id) {
  if (keys_.size() >= kMaxKeys) {
    // Released keys first, oldest first; else the oldest pressed key, released by teardown.
    auto victim = keys_.end();
    for (auto it = keys_.begin(); it != keys_.end(); ++it) {
      if (it->second.state == State::kReleased && (victim == keys_.end() || it->second.order < victim->second.order)) {
        victim = it;
      }
    }
    if (victim == keys_.end()) {
      for (auto it = keys_.begin(); it != keys_.end(); ++it) {
        if (victim == keys_.end() || it->second.order < victim->second.order) victim = it;
      }
      Release(victim->first, victim->second, ReleaseWhy::kTeardown);
    }
    keys_.erase(victim);
  }
  Key& k = keys_[id];
  k.order = ++next_order_;
  return k;
}

void PuppetActionRelay::SetEpoch(uint32_t peer_epoch, uint32_t generation) {
  if (peer_epoch == peer_epoch_ && generation == generation_) return;
  Teardown();
  keys_.clear();
  peer_epoch_ = peer_epoch;
  generation_ = generation;
}

PuppetActionRelay::Drop PuppetActionRelay::OnAction(uint32_t generation, uint32_t press_sequence, uint8_t slot,
                                                    bool release, std::chrono::milliseconds press_age, TimePoint now) {
  const Id id{press_sequence, slot};
  auto drop = [&](Drop d) {
    Emit(Event::Type::kDropped, id, ReleaseWhy::kRelease, d);
    return d;
  };
  if (slot >= kSlots) return drop(Drop::kSlot);
  if (generation != generation_) return drop(Drop::kGeneration);
  auto it = keys_.find(id);
  if (!release) {
    if (it != keys_.end()) return drop(Drop::kDuplicatePress);
    Key& k = Insert(id);
    if (press_age > kStale) {
      k.state = State::kReleased;
      k.stale = true;
      return drop(Drop::kStale);
    }
    k.state = State::kPressed;
    k.pressed_at = now;
    return Drop::kNone;
  }
  // A release.
  if (it == keys_.end()) {
    // Its press was never seen (dropped for generation, or never sent): recorded released, so a
    // press after it is a duplicate.
    Key& k = Insert(id);
    k.state = State::kReleased;
    return Drop::kNone;
  }
  Key& k = it->second;
  if (k.state == State::kReleased) {
    if (k.stale) {
      k.stale = false;  // a stale press's own release: consumed silently, once
      return Drop::kNone;
    }
    return drop(Drop::kDuplicateRelease);
  }
  if (!k.taken) {
    if (k.release_after) return drop(Drop::kDuplicateRelease);
    k.release_after = true;  // the press goes in for one think, then ends
    return Drop::kNone;
  }
  Release(id, k, ReleaseWhy::kRelease);
  return Drop::kNone;
}

uint8_t PuppetActionRelay::Think(TimePoint now) {
  for (auto& [id, k] : keys_) {
    if (k.state == State::kPressed && now - k.pressed_at > kMaxHold) {
      k.taken = true;  // it held (or would have held) its slot: the timeout ends that hold
      Release(id, k, ReleaseWhy::kTimeout);
    }
  }
  for (auto& [id, k] : keys_) {
    if (k.state == State::kPressed && !k.taken) {
      k.taken = true;
      Emit(Event::Type::kStarted, id);
    }
  }
  const uint8_t bits = HeldSlots();
  for (auto& [id, k] : keys_) {
    if (k.state == State::kPressed && k.release_after) Release(id, k, ReleaseWhy::kReleaseAfter);
  }
  return bits;
}

void PuppetActionRelay::Teardown() {
  for (auto& [id, k] : keys_) Release(id, k, ReleaseWhy::kTeardown);
}

bool PuppetActionRelay::PollEvent(Event& out) {
  if (events_.empty()) return false;
  out = events_.front();
  events_.pop_front();
  return true;
}

uint8_t PuppetActionRelay::HeldSlots() const {
  uint8_t bits = 0;
  for (const auto& [id, k] : keys_) {
    if (k.state == State::kPressed && k.taken) bits = uint8_t(bits | (1u << id.second));
  }
  return bits;
}

}  // namespace skylanders::online
