#pragma once

#include <algorithm>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <span>
#include <tuple>
#include <utility>
#include <vector>

#include "backend/adapter.h"
#include "backend/base.h"
#include "backend/entity_id.h"
#include "backend/obligation.h"
#include "backend/record.h"

// The fake world (design section 3.12): what the tests use where a game would be. Entities with an
// EntityId, a handle, fields per (kind, slot), alive or streamed out; Spawn, Despawn, StreamOut,
// StreamIn and NativeWrite (the game changing a value by itself, as S's world does), each reported
// to the peer's IdentityMap as a game's adapters would; and per kind a FakeKindAdapter whose value
// is the entity's field. The faults it can play: a write that fails (fail_next), a write the native
// overwrites at the next tick (Stick, then Tick), a despawn inside Apply (despawn_in_apply), and
// two entities under one id (a second Spawn of a live id: the IdentityMap answers kAmbiguous).
// Header-only, like fake_network.h; game-free.

namespace skylanders::backend {

class FakeWorld {
 public:
  explicit FakeWorld(IdentityMap& identity) : identity_(identity) {}
  FakeWorld(const FakeWorld&) = delete;
  FakeWorld& operator=(const FakeWorld&) = delete;

  // A new entity under `id`. A second live entity under the same id is the ambiguous fault: both
  // stay, and the IdentityMap resolves neither until one goes.
  Handle Spawn(const EntityId& id, TimePoint now) {
    const Handle h = next_++;
    entities_[h].id = id;
    identity_.OnSpawn(id, h, now);
    return h;
  }
  // Destroyed in this epoch: gone (rows for it are dropped, never applied).
  void Despawn(Handle h, TimePoint now) {
    if (Entity* e = Find(h); e != nullptr && e->live) {
      e->live = false;
      e->streamed_out = false;
      identity_.OnDespawn(h, /*streamed_out=*/false, now);
    }
  }
  // Streamed out: its rows wait (pending) until StreamIn.
  void StreamOut(Handle h, TimePoint now) {
    if (Entity* e = Find(h); e != nullptr && e->live) {
      e->live = false;
      e->streamed_out = true;
      identity_.OnDespawn(h, /*streamed_out=*/true, now);
    }
  }
  void StreamIn(Handle h, TimePoint now) {
    if (Entity* e = Find(h); e != nullptr && e->streamed_out) {
      e->live = true;
      e->streamed_out = false;
      identity_.OnSpawn(e->id, h, now);
    }
  }
  // The level is unloaded: every level entity goes, with no events (the IdentityMap drops level
  // ids at the epoch itself); session objects and players stay.
  void NewGeneration(uint32_t generation) {
    std::erase_if(entities_, [](const auto& e) { return IsLevelForm(e.second.id.form); });
    identity_.OnEpoch(generation);
  }

  bool Live(Handle h) const {
    const auto it = entities_.find(h);
    return it != entities_.end() && it->second.live;
  }
  bool StreamedOut(Handle h) const {
    const auto it = entities_.find(h);
    return it != entities_.end() && it->second.streamed_out;
  }
  // The game changing a value by itself (S's world before its Put; a client's native).
  void NativeWrite(Handle h, KindId kind, uint16_t slot, std::span<const uint8_t> value) {
    SetField(h, kind, slot, value);
  }
  // An adapter's write (and NativeWrite's).
  void SetField(Handle h, KindId kind, uint16_t slot, std::span<const uint8_t> value) {
    if (Entity* e = Find(h)) e->fields[{kind, slot}].assign(value.begin(), value.end());
  }
  void EraseField(Handle h, KindId kind, uint16_t slot) {
    if (Entity* e = Find(h)) e->fields.erase({kind, slot});
  }
  // The field of a live entity (empty when never written); nullptr: no such entity or not live.
  const std::vector<uint8_t>* Field(Handle h, KindId kind, uint16_t slot) const {
    const auto it = entities_.find(h);
    if (it == entities_.end() || !it->second.live) return nullptr;
    static const std::vector<uint8_t> kEmpty;
    const auto f = it->second.fields.find({kind, slot});
    return f == it->second.fields.end() ? &kEmpty : &f->second;
  }
  // Whether the field was ever written (and not erased), live or not.
  bool HasField(Handle h, KindId kind, uint16_t slot) const {
    const auto it = entities_.find(h);
    return it != entities_.end() && it->second.fields.contains({kind, slot});
  }
  // The first entity ever made under `id` in this generation, whatever its state now.
  std::optional<Handle> HandleOf(const EntityId& id) const {
    for (const auto& [h, e] : entities_) {
      if (e.id == id) return h;
    }
    return std::nullopt;
  }
  size_t Entities() const { return entities_.size(); }
  IdentityMap& Identity() { return identity_; }

  // The `stuck` fault: the native holds this field at `native`; every write to it is overwritten
  // at the next Tick (the compare query then reports the row stuck).
  void Stick(Handle h, KindId kind, uint16_t slot, std::vector<uint8_t> native) {
    stuck_[{h, kind, slot}] = std::move(native);
  }
  void Unstick(Handle h, KindId kind, uint16_t slot) { stuck_.erase({h, kind, slot}); }
  // One game tick: stuck natives win.
  void Tick() {
    for (const auto& [where, native] : stuck_) {
      const auto& [h, kind, slot] = where;
      if (Live(h)) SetField(h, kind, slot, native);
    }
  }

 private:
  struct Entity {
    EntityId id;
    bool live = true;
    bool streamed_out = false;
    std::map<std::pair<KindId, uint16_t>, std::vector<uint8_t>> fields;
  };
  Entity* Find(Handle h) {
    const auto it = entities_.find(h);
    return it == entities_.end() ? nullptr : &it->second;
  }

  IdentityMap& identity_;
  Handle next_ = 1;
  std::map<Handle, Entity> entities_;
  std::map<std::tuple<Handle, KindId, uint16_t>, std::vector<uint8_t>> stuck_;
};

// The plain adapter of one kind: its value is the entity's field. Counts applies and fulfilments,
// can keep an apply log (the kinds in apply order), and plays the write faults.
class FakeKindAdapter : public KindAdapter {
 public:
  FakeKindAdapter(FakeWorld& world, KindId kind) : world_(world), kind_(kind) {}

  AdapterResult Read(Handle h, uint16_t slot, std::vector<uint8_t>& value) override {
    const std::vector<uint8_t>* f = world_.Field(h, kind_, slot);
    if (f == nullptr) return AdapterResult::kGone;
    value = *f;
    return AdapterResult::kOk;
  }
  AdapterResult Apply(Handle h, uint16_t slot, std::span<const uint8_t> value) override {
    if (!world_.Live(h)) return AdapterResult::kGone;
    if (despawn_in_apply) {  // the entity goes inside the call (a game despawn on this tick)
      despawn_in_apply = false;
      world_.Despawn(h, now);
      return AdapterResult::kGone;
    }
    if (fail_next > 0) {
      --fail_next;
      return AdapterResult::kFailed;
    }
    if (on_apply) on_apply(h, slot);
    world_.SetField(h, kind_, slot, value);
    ++applies;
    if (log != nullptr) log->push_back(kind_);
    return AdapterResult::kOk;
  }
  AdapterResult Retire(Handle h, uint16_t slot) override {
    if (!world_.Live(h)) return AdapterResult::kGone;
    world_.EraseField(h, kind_, slot);
    return AdapterResult::kOk;
  }
  AdapterResult Fulfil(Handle h, const Obligation& o) override {
    if (!world_.Live(h)) return AdapterResult::kGone;
    ++fulfilled[o.id];
    world_.SetField(h, kind_, o.key.slot, o.value);
    return AdapterResult::kOk;
  }

  // What the adapter wrote to a field; nullptr when it was never written.
  const std::vector<uint8_t>* Value(Handle h, uint16_t slot) const {
    if (!world_.HasField(h, kind_, slot)) return nullptr;
    return world_.Live(h) ? world_.Field(h, kind_, slot) : nullptr;
  }
  size_t Fulfilled(uint64_t id) const {
    auto it = fulfilled.find(id);
    return it == fulfilled.end() ? 0 : size_t(it->second);
  }
  size_t FulfilledTotal() const {
    size_t n = 0;
    for (const auto& [id, count] : fulfilled) n += size_t(count);
    return n;
  }
  size_t MaxFulfilledOnce() const {
    size_t n = 0;
    for (const auto& [id, count] : fulfilled) n = std::max(n, size_t(count));
    return n;
  }
  uint64_t Applies() const { return applies; }
  void SetLog(std::vector<KindId>* l) { log = l; }
  KindId Kind() const { return kind_; }

  std::map<uint64_t, int> fulfilled;               // obligation id -> times fulfilled
  uint64_t applies = 0;                            // writes that went through
  std::vector<KindId>* log = nullptr;              // the apply log, when set
  std::function<void(Handle, uint16_t)> on_apply;  // a harness's check, before the write
  int fail_next = 0;                               // the next n Apply calls fail
  bool despawn_in_apply = false;                   // the next Apply despawns its entity
  TimePoint now{};                                 // the clock a despawn inside Apply uses

 protected:
  FakeWorld& world_;
  KindId kind_;
};

}  // namespace skylanders::backend
