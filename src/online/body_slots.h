#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <vector>

// A client's remote body slots and stand-ins (stage 4, docs/online-4.md section 4.5, D8). Pure
// bookkeeping: the game's loader and puppet code act on what it answers; which strategy (A, B, C)
// and K come from the capacity experiments (E2a to E2f), passed in.
//
// A body slot holds one resident character package, the count of puppets whose real body it is,
// and a separate list of the stand-ins resting on it. A stand-in never pins a package: the count is
// real users only, and before any package is released every stand-in on it is moved, in that step,
// to a source independent of it, in this order:
//   1. another resident remote package with no pending release (one matching the element first);
//   2. the local hero's package, only if E2e showed a stand-in on it survives a local swap;
//   3. a pinned fallback body, only if E2d/E2e proved one fits and is safe;
//   4. hidden, only if the council approved hiding (CQ1);
//   otherwise none: the puppet is despawned before the release all the same.
// The same order picks a puppet's first stand-in while its real body is not resident.
//
// Not thread-safe: the game thread owns it.

namespace skylanders::online {

using CharacterId = uint32_t;  // the character's name hash
constexpr CharacterId kNoCharacter = 0;

enum class StandInSource : uint8_t { kRemotePackage, kLocalPackage, kPinnedFallback, kHidden, kNone };

struct StandInPolicy {
  bool local_package_survives = false;  // E2e
  bool pinned_fallback = false;         // E2d and E2e
  bool hiding_approved = false;         // CQ1
};

struct StandIn {
  StandInSource source = StandInSource::kNone;
  int slot = -1;  // kRemotePackage: the slot whose package it rests on
};

// One stand-in that had to move because its package is being released.
struct StandInMove {
  uint16_t peer = 0;
  StandIn to;
};

class BodySlots {
 public:
  BodySlots(size_t slots, StandInPolicy policy) : slots_(slots), policy_(policy) {}

  size_t SlotCount() const { return slots_.size(); }
  // The slot holding `character` (resident or loading), or -1.
  int SlotOf(CharacterId character) const;
  int RealUsers(int slot) const;
  CharacterId Package(int slot) const;
  bool PendingRelease(int slot) const;

  // A real body of `character` for `peer` (its previous real body, if another, is released first
  // through ReleaseReal). Returns the slot: one already holding the character (the count goes up),
  // or a free one (the caller loads the package into it, then MarkResident). nullopt: no slot (only
  // with more distinct characters than slots); the caller takes a stand-in and, under pressure,
  // releases a slot whose count reached 0.
  std::optional<int> AcquireReal(uint16_t peer, CharacterId character);
  void MarkResident(int slot);
  bool Resident(int slot) const;
  // `peer`'s real body is gone (a swap away, a leave, a despawn). Its slot's count goes down; at 0
  // the slot is releasable (ReleaseSlot), never released here.
  void ReleaseReal(uint16_t peer);
  // The slot `peer`'s real body is on, or -1.
  int RealSlotOf(uint16_t peer) const;

  // Releases a slot whose count is 0: first every stand-in resting on it moves (returned, in peer
  // order), then the package is free. false (nothing done) while real users remain.
  bool ReleaseSlot(int slot, std::vector<StandInMove>* moves);

  // A stand-in for `peer` (element: the remote player's element, to prefer a matching package),
  // never resting on `exclude_slot` or a slot with a pending release. Recorded as where `peer`'s
  // stand-in rests.
  StandIn ChooseStandIn(uint16_t peer, uint8_t element, int exclude_slot = -1);
  void DropStandIn(uint16_t peer) { stand_ins_.erase(peer); }
  std::optional<StandIn> StandInOf(uint16_t peer) const;

  // The local hero's own swap needs no call here: source 2 is chosen only when E2e showed a
  // stand-in on the local package survives a local swap in place, so nothing ever moves for it.

  // The element of the character a slot holds (for the element match); set by the caller.
  void SetElement(int slot, uint8_t element);

 private:
  struct Slot {
    CharacterId package = kNoCharacter;
    bool resident = false;
    bool pending_release = false;
    uint8_t element = 0;
    std::set<uint16_t> real;  // the puppets whose real body this is
  };
  bool Valid(int slot) const { return slot >= 0 && size_t(slot) < slots_.size(); }
  std::vector<Slot> slots_;
  StandInPolicy policy_;
  std::map<uint16_t, int> real_of_;        // peer -> slot of its real body
  std::map<uint16_t, StandIn> stand_ins_;  // peer -> where its stand-in rests
  std::map<uint16_t, uint8_t> elements_;   // peer -> the element it last asked a stand-in for
};

}  // namespace skylanders::online
