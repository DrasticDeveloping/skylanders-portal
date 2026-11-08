#include "online/body_slots.h"

namespace skylanders::online {

int BodySlots::SlotOf(CharacterId character) const {
  if (character == kNoCharacter) return -1;
  for (size_t i = 0; i < slots_.size(); ++i) {
    if (slots_[i].package == character) return int(i);
  }
  return -1;
}

int BodySlots::RealUsers(int slot) const { return Valid(slot) ? int(slots_[size_t(slot)].real.size()) : 0; }
CharacterId BodySlots::Package(int slot) const { return Valid(slot) ? slots_[size_t(slot)].package : kNoCharacter; }
bool BodySlots::PendingRelease(int slot) const { return Valid(slot) && slots_[size_t(slot)].pending_release; }
bool BodySlots::Resident(int slot) const { return Valid(slot) && slots_[size_t(slot)].resident; }

void BodySlots::MarkResident(int slot) {
  if (Valid(slot)) slots_[size_t(slot)].resident = true;
}

void BodySlots::SetElement(int slot, uint8_t element) {
  if (Valid(slot)) slots_[size_t(slot)].element = element;
}

int BodySlots::RealSlotOf(uint16_t peer) const {
  const auto it = real_of_.find(peer);
  return it == real_of_.end() ? -1 : it->second;
}

std::optional<int> BodySlots::AcquireReal(uint16_t peer, CharacterId character) {
  if (character == kNoCharacter) return std::nullopt;
  int slot = SlotOf(character);
  if (slot >= 0 && slots_[size_t(slot)].pending_release) slot = -1;  // being released: not reused
  if (slot < 0) {
    for (size_t i = 0; i < slots_.size(); ++i) {
      if (slots_[i].package == kNoCharacter) {
        slot = int(i);
        slots_[i] = Slot{};
        slots_[i].package = character;
        break;
      }
    }
  }
  if (slot < 0) return std::nullopt;
  const int old = RealSlotOf(peer);
  if (old >= 0 && old != slot) slots_[size_t(old)].real.erase(peer);  // load before unload: the old
                                                                       // stays until ReleaseSlot
  slots_[size_t(slot)].real.insert(peer);
  real_of_[peer] = slot;
  return slot;
}

void BodySlots::ReleaseReal(uint16_t peer) {
  const auto it = real_of_.find(peer);
  if (it == real_of_.end()) return;
  slots_[size_t(it->second)].real.erase(peer);
  real_of_.erase(it);
}

bool BodySlots::ReleaseSlot(int slot, std::vector<StandInMove>* moves) {
  if (!Valid(slot)) return false;
  Slot& s = slots_[size_t(slot)];
  if (!s.real.empty() || s.package == kNoCharacter) return false;
  s.pending_release = true;
  // Every stand-in on the package moves first, in this step; none ever delays the release.
  std::vector<uint16_t> resting;
  for (const auto& [peer, at] : stand_ins_) {
    if (at.source == StandInSource::kRemotePackage && at.slot == slot) resting.push_back(peer);
  }
  for (const uint16_t peer : resting) {
    const auto e = elements_.find(peer);
    StandIn to = ChooseStandIn(peer, e == elements_.end() ? 0 : e->second, slot);
    if (moves) moves->push_back(StandInMove{peer, to});
  }
  s = Slot{};
  return true;
}

StandIn BodySlots::ChooseStandIn(uint16_t peer, uint8_t element, int exclude_slot) {
  StandIn out;
  if (element != 0) elements_[peer] = element;
  // 1. Another resident remote package with no pending release; an element match first.
  int pick = -1;
  for (size_t i = 0; i < slots_.size(); ++i) {
    const Slot& s = slots_[i];
    if (int(i) == exclude_slot || !s.resident || s.pending_release || s.package == kNoCharacter) continue;
    if (pick < 0 || (element != 0 && s.element == element && slots_[size_t(pick)].element != element)) pick = int(i);
  }
  if (pick >= 0) {
    out.source = StandInSource::kRemotePackage;
    out.slot = pick;
  } else if (policy_.local_package_survives) {
    out.source = StandInSource::kLocalPackage;  // 2
  } else if (policy_.pinned_fallback) {
    out.source = StandInSource::kPinnedFallback;  // 3
  } else if (policy_.hiding_approved) {
    out.source = StandInSource::kHidden;  // 4
  }
  // None: the puppet is despawned before the release all the same (the caller's), which counts as
  // no stand-in.
  if (out.source == StandInSource::kNone) stand_ins_.erase(peer);
  else stand_ins_[peer] = out;
  return out;
}

std::optional<StandIn> BodySlots::StandInOf(uint16_t peer) const {
  const auto it = stand_ins_.find(peer);
  if (it == stand_ins_.end()) return std::nullopt;
  return it->second;
}

}  // namespace skylanders::online
