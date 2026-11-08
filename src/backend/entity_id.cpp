#include "backend/entity_id.h"

#include <cstdio>
#include <utility>

namespace skylanders::backend {

namespace {

constexpr uint32_t kFnv32Offset = 2166136261u;
constexpr uint32_t kFnv32Prime = 16777619u;

// The wire form, written for any id (ParentDigest hashes it whether or not the parent is Valid).
void WriteWire(const EntityId& id, std::vector<uint8_t>& out) {
  Writer w(out);
  w.U8(uint8_t(id.form));
  w.U32(id.epoch);
  w.U32(id.a);
  w.U32(id.b);
  w.U32(id.c);
  w.U16(id.ordinal);
  w.U16(id.incarnation);
}

}  // namespace

uint32_t HashName(std::string_view name) {
  uint32_t h = kFnv32Offset;
  for (const char c : name) {
    h ^= uint8_t(c);
    h *= kFnv32Prime;
  }
  return h;
}

EntityId Authored(uint32_t epoch, uint32_t mid, uint32_t name_hash, uint32_t class_hash) {
  EntityId id;
  id.form = IdForm::kAuthored;
  id.epoch = epoch;
  id.a = mid;
  id.b = name_hash;
  id.c = class_hash;
  return id;
}

EntityId Spawned(uint32_t epoch, const EntityId* parent, uint32_t key_hash, uint16_t ordinal,
                 uint16_t incarnation) {
  EntityId id;
  id.form = IdForm::kSpawned;
  id.epoch = epoch;
  id.a = parent != nullptr ? ParentDigest(*parent) : 0;
  id.b = key_hash;
  id.ordinal = ordinal;
  id.incarnation = incarnation;
  return id;
}

uint32_t ParentDigest(const EntityId& parent) {
  std::vector<uint8_t> wire;
  wire.reserve(kEntityIdWireBytes);
  WriteWire(parent, wire);
  uint32_t h = kFnv32Offset;
  for (const uint8_t b : wire) {
    h ^= b;
    h *= kFnv32Prime;
  }
  return h;
}

EntityId Player(uint16_t peer, uint32_t peer_epoch) {
  EntityId id;
  id.form = IdForm::kPlayer;
  id.a = peer;
  id.b = peer_epoch;
  return id;
}

EntityId SessionObject(uint32_t object_id) {
  EntityId id;
  id.form = IdForm::kSession;
  id.a = object_id;
  return id;
}

EntityId ServerIssued(uint32_t epoch, uint32_t server_id) {
  EntityId id;
  id.form = IdForm::kServerIssued;
  id.epoch = epoch;
  id.a = server_id;
  return id;
}

bool IsLevelForm(IdForm form) {
  return form == IdForm::kAuthored || form == IdForm::kSpawned || form == IdForm::kServerIssued;
}

bool Valid(const EntityId& id) {
  // A level form resolves in one generation (from 1); the others in none.
  if (IsLevelForm(id.form) ? id.epoch == 0 : id.epoch != 0) return false;
  switch (id.form) {
    case IdForm::kNone:
      return false;
    case IdForm::kAuthored:
      return id.ordinal == 0 && id.incarnation == 0;
    case IdForm::kSpawned:
      // Parent digest 0 is a parentless spawn; the key and the ordinal are what it is keyed by.
      return id.b != 0 && id.c == 0 && id.ordinal >= 1;
    case IdForm::kPlayer:
      return id.a <= 0xFFFF && id.c == 0 && id.ordinal == 0 && id.incarnation == 0;
    case IdForm::kSession:
      return id.b == 0 && id.c == 0 && id.ordinal == 0 && id.incarnation == 0;
    case IdForm::kServerIssued:
      return id.a != 0 && id.b == 0 && id.c == 0 && id.ordinal == 0 && id.incarnation == 0;
  }
  return false;  // a form byte this build does not know
}

std::string ToText(const EntityId& id) {
  char buf[96];
  switch (id.form) {
    case IdForm::kAuthored:
      std::snprintf(buf, sizeof(buf), "a%08x.%08x.%08x@%u", unsigned(id.a), unsigned(id.b),
                    unsigned(id.c), unsigned(id.epoch));
      return buf;
    case IdForm::kSpawned:
      if (id.a == 0) {
        std::snprintf(buf, sizeof(buf), "s-.%08x#%u/%u@%u", unsigned(id.b), unsigned(id.ordinal),
                      unsigned(id.incarnation), unsigned(id.epoch));
      } else {
        std::snprintf(buf, sizeof(buf), "s%08x.%08x#%u/%u@%u", unsigned(id.a), unsigned(id.b),
                      unsigned(id.ordinal), unsigned(id.incarnation), unsigned(id.epoch));
      }
      return buf;
    case IdForm::kPlayer:
      std::snprintf(buf, sizeof(buf), "p%u/%u", unsigned(id.a), unsigned(id.b));
      return buf;
    case IdForm::kSession:
      std::snprintf(buf, sizeof(buf), "x%u", unsigned(id.a));
      return buf;
    case IdForm::kServerIssued:
      std::snprintf(buf, sizeof(buf), "i%u@%u", unsigned(id.a), unsigned(id.epoch));
      return buf;
    case IdForm::kNone:
      break;
  }
  return "-";
}

bool Encode(const EntityId& id, std::vector<uint8_t>& out) {
  if (!Valid(id)) return false;
  WriteWire(id, out);
  return true;
}

std::optional<EntityId> Decode(Reader& in) {
  EntityId id;
  id.form = IdForm(in.U8());
  id.epoch = in.U32();
  id.a = in.U32();
  id.b = in.U32();
  id.c = in.U32();
  id.ordinal = in.U16();
  id.incarnation = in.U16();
  if (!in.Ok() || !Valid(id)) return std::nullopt;
  return id;
}

std::string_view ResolutionName(Resolution r) {
  switch (r) {
    case Resolution::kResolved: return "resolved";
    case Resolution::kPending: return "pending";
    case Resolution::kAmbiguous: return "ambiguous";
    case Resolution::kUnresolved: return "unresolved";
    case Resolution::kGone: return "gone";
  }
  return "unknown";
}

// --- IdentityMap ---
//
// Every reported or bound id has an Entry: its live handles (one: resolved; two or more:
// ambiguous, never picked), and, with none live, whether it was streamed out (pending, may come
// back) or destroyed (gone for the rest of the epoch). `since` is when the entry entered its
// current resolution. OnEpoch has no clock, so the epoch's start time is the first `now` the map
// sees after it (an id never reported is pending since then).

void IdentityMap::OnSpawn(const EntityId& id, Handle h, TimePoint now) {
  if (!Valid(id) || h == kNoHandle) return;
  if (epoch_began_ == TimePoint{}) epoch_began_ = now;
  // A handle reported under a new id without a despawn: the old thing is gone from that handle.
  if (const auto old = handles_.find(h); old != handles_.end()) {
    if (old->second == id) return;  // reported twice
    OnDespawn(h, /*streamed_out=*/false, now);
  }
  Entry& e = ids_[id];
  e.live.push_back(h);
  e.streamed_out = false;
  e.gone = false;
  if (e.live.size() <= 2) e.since = now;  // became resolved, or became ambiguous
  handles_[h] = id;
  if (id.form == IdForm::kAuthored) by_mid_[{id.epoch, id.a}] = id;
}

void IdentityMap::OnDespawn(Handle h, bool streamed_out, TimePoint now) {
  if (h == kNoHandle) return;
  if (epoch_began_ == TimePoint{}) epoch_began_ = now;
  auto leave = [&](const EntityId& id) {
    const auto it = ids_.find(id);
    if (it == ids_.end()) return;
    Entry& e = it->second;
    std::erase(e.live, h);
    if (e.live.empty()) {
      e.streamed_out = streamed_out;
      e.gone = !streamed_out;
      e.since = now;
    } else if (e.live.size() == 1) {
      e.since = now;  // ambiguous no longer: the one left resolves
    }
  };
  if (const auto own = handles_.find(h); own != handles_.end()) {
    leave(own->second);
    handles_.erase(own);
  }
  if (const auto bound = bound_.find(h); bound != bound_.end()) {
    leave(bound->second);
    bound_.erase(bound);
  }
}

void IdentityMap::OnEpoch(uint32_t generation) {
  generation_ = generation;
  epoch_began_ = TimePoint{};
  std::erase_if(ids_, [](const auto& kv) { return IsLevelForm(kv.first.form); });
  std::erase_if(handles_, [](const auto& kv) { return IsLevelForm(kv.second.form); });
  bound_.clear();  // every bound id is server-issued, a level form
  by_mid_.clear();
}

bool IdentityMap::Bind(const EntityId& server_issued, Handle h, TimePoint now) {
  if (server_issued.form != IdForm::kServerIssued || !Valid(server_issued)) return false;
  if (server_issued.epoch != generation_) return false;
  // Only a live handle this peer reported can be bound; a gone or streamed-out one cannot.
  if (h == kNoHandle || !handles_.contains(h)) return false;
  if (epoch_began_ == TimePoint{}) epoch_began_ = now;
  if (const auto bound = bound_.find(h); bound != bound_.end()) {
    // One handle names one server id: a bind to another one is refused, a repeat is a no-op.
    return bound->second == server_issued;
  }
  Entry& e = ids_[server_issued];
  // Accepted even when another live handle holds the id: it is recorded and resolves ambiguous.
  e.live.push_back(h);
  e.streamed_out = false;
  e.gone = false;
  if (e.live.size() <= 2) e.since = now;
  bound_[h] = server_issued;
  return true;
}

IdentityMap::Lookup IdentityMap::Resolve(const EntityId& id) const {
  Lookup out;
  if (!Valid(id) || (IsLevelForm(id.form) && id.epoch != generation_)) {
    out.resolution = Resolution::kUnresolved;
    return out;
  }
  const auto it = ids_.find(id);
  if (it == ids_.end()) {
    // Never reported. An authored id whose mID holds a thing with other hashes names nothing here.
    if (id.form == IdForm::kAuthored) {
      if (const auto m = by_mid_.find({id.epoch, id.a}); m != by_mid_.end() && m->second != id) {
        out.resolution = Resolution::kUnresolved;
        return out;
      }
    }
    out.resolution = Resolution::kPending;
    out.since = epoch_began_;
    return out;
  }
  const Entry& e = it->second;
  out.since = e.since;
  if (e.live.size() == 1) {
    out.resolution = Resolution::kResolved;
    out.handle = e.live.front();
  } else if (e.live.size() > 1) {
    out.resolution = Resolution::kAmbiguous;
  } else {
    out.resolution = e.gone ? Resolution::kGone : Resolution::kPending;
  }
  return out;
}

std::optional<EntityId> IdentityMap::IdOf(Handle h) const {
  const auto it = handles_.find(h);
  if (it == handles_.end()) return std::nullopt;
  return it->second;
}

size_t IdentityMap::Count(Resolution r) const {
  size_t n = 0;
  for (const auto& [id, e] : ids_) {
    Resolution here;
    if (IsLevelForm(id.form) && id.epoch != generation_) {
      here = Resolution::kUnresolved;
    } else if (e.live.size() == 1) {
      here = Resolution::kResolved;
    } else if (e.live.size() > 1) {
      here = Resolution::kAmbiguous;
    } else {
      here = e.gone ? Resolution::kGone : Resolution::kPending;
    }
    n += here == r;
  }
  return n;
}

uint32_t IdentityMap::Generation() const { return generation_; }

void IdentityMap::Reset() {
  generation_ = 0;
  epoch_began_ = TimePoint{};
  ids_.clear();
  handles_.clear();
  bound_.clear();
  by_mid_.clear();
}

}  // namespace skylanders::backend
