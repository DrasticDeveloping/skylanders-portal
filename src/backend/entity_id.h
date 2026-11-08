#pragma once

#include <compare>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "backend/base.h"

// Entity identity (design section 3.2). An EntityId names a thing in the world the same way on
// every peer; IdentityMap says what it names on this peer. The ids the backend uses:
//
//   id            made by            lives               form
//   peer id       the session        session             server 0, owner 1, others 2 to 4
//   peer epoch    backend::Server    session             counts the joins of one peer id, so a
//                                                        returning peer never meets its old rows or
//                                                        keys (1B's ledger rule, amendment n); set
//                                                        at AddPeer, told in StateHello (Opus F9),
//                                                        part of every RequestKey (Sol F3)
//   generation    the session        session             u32 from 1; the epoch of level-scope rows
//   entity id     the adapter,       its scope           EntityId, below
//                 through IdentityMap
//   record key    the kind           the row             (kind, entity id, slot)
//   revision      WorldState         the row, one epoch  u32 from 1, never reused; a tomb is one
//   LSN           WorldState         session             u64 from 1, one counter for every log
//                                                        entry: record changes of both scopes,
//                                                        epoch changes, obligations (Opus F3);
//                                                        effects take none
//   baseline id   backend::Server    per peer            u32
//   request key   the client's       (peer, peer epoch,  (peer, peer epoch, generation, sequence);
//                 sequence; S stamps generation)         record.h (Sol F3)
//                 peer and peer epoch
//                 from the link
//   lease id      LeaseTable         generation          u32
//   obligation id backend::Server    (recipient, peer    u64 from 1, counted per recipient and
//                                    epoch)              peer epoch, so the client's seen and S's
//                                                        credit count are exact (Sol F9, draft 4;
//                                                        Sol r3 F9, draft 5; section 3.6)

namespace skylanders::backend {

// FNV-1a, 32 bits: equal to online::NameHash (protocol.cpp), pinned by a test that links both (ID2).
uint32_t HashName(std::string_view name);

enum class IdForm : uint8_t {
  kNone = 0,
  kAuthored = 1,      // placed in the level: (mID, name hash, class hash), as 1B's world EntityRef
  kSpawned = 2,       // made at run time: (parent digest, spawn key hash, ordinal, incarnation)
  kPlayer = 3,        // a player's own rows: (peer id, peer epoch)
  kSession = 4,       // a session object (a tracker, difficulty): (object id)
  kServerIssued = 5,  // S's own number for an entity, bound on a client by IdentityMap::Bind
                      // (Q5's alternative; reserved and tested now so item 7 is a choice, not a
                      // redesign)
};

// What each field means depends on the form; fields a form does not use are 0, so ids compare
// equal exactly when their tuples do (1B's RequestKey rule, protocol.h).
//
//   form           epoch        a              b             c            ordinal  incarnation
//   kAuthored      generation   mID            name hash     class hash   0        0
//   kSpawned       generation   parent digest  key hash      0            n >= 1   m
//   kPlayer        0            peer id        peer epoch    0            0        0
//   kSession       0            object id      0             0            0        0
//   kServerIssued  generation   server id      0             0            0        0
struct EntityId {
  IdForm form = IdForm::kNone;
  uint32_t epoch = 0;  // the generation it resolves in; 0 for kPlayer and kSession
  uint32_t a = 0, b = 0, c = 0;
  uint16_t ordinal = 0;      // kSpawned: the nth spawn of (parent, key) in the epoch, from 1
  uint16_t incarnation = 0;  // kSpawned: how often that ordinal came back (restart, wave reset)
  auto operator<=>(const EntityId&) const = default;
};

// The wire form: form (u8), epoch (u32), a, b, c (u32), ordinal, incarnation (u16), every form the
// same length, so a reader can skip one without knowing the form.
constexpr size_t kEntityIdWireBytes = 21;

EntityId Authored(uint32_t epoch, uint32_t mid, uint32_t name_hash, uint32_t class_hash);
// parent: the parent's own EntityId, any form; its digest (32 bits, FNV-1a over its wire form) goes
// in field a. A parentless spawn passes nullptr: digest 0, keyed by spawn key and ordinal alone.
EntityId Spawned(uint32_t epoch, const EntityId* parent, uint32_t key_hash, uint16_t ordinal,
                 uint16_t incarnation);
uint32_t ParentDigest(const EntityId& parent);
EntityId Player(uint16_t peer, uint32_t peer_epoch);
EntityId SessionObject(uint32_t id);
EntityId ServerIssued(uint32_t epoch, uint32_t server_id);

// The unused fields 0; epoch set only for level forms (authored, spawned, server-issued).
// kSpawned: key hash non-zero, ordinal >= 1; parent digest 0 is allowed (parentless).
// kServerIssued: server id non-zero.
bool Valid(const EntityId& id);
// A level form (authored, spawned, server-issued): dropped at each epoch.
bool IsLevelForm(IdForm form);

// a<mid>.<name>.<class>@<g>, s<parent|->.<key>#<ord>/<inc>@<g>, p<peer>/<epoch>, x<id>,
// i<server id>@<g>; hashes in hex as 1B's request lines write refs (the map dump's entity text).
std::string ToText(const EntityId& id);
// Appends the wire form; false (nothing appended) for an id that is not Valid.
bool Encode(const EntityId& id, std::vector<uint8_t>& out);
// Reads one wire form; nullopt if it is short or not Valid (a form with a foreign field set).
std::optional<EntityId> Decode(Reader& in);

enum class Resolution : uint8_t {
  kResolved,
  kPending,     // not here yet (not spawned, streamed out); may come
  kAmbiguous,   // two live things claim it: refused, never picked
  kUnresolved,  // the wrong epoch, or the hashes do not match what is under the mID
  kGone,        // destroyed in this epoch; a write to it is dropped
};
std::string_view ResolutionName(Resolution r);

// The adapter's own: a guest address in the game, an index in the fake world. 0 is no handle.
using Handle = uint64_t;
constexpr Handle kNoHandle = 0;

// What each id names on this peer. The adapter reports spawns and despawns; nothing here guesses.
// An id never reported resolves kPending since the epoch began.
class IdentityMap {
 public:
  void OnSpawn(const EntityId& id, Handle h, TimePoint now);
  void OnDespawn(Handle h, bool streamed_out, TimePoint now);  // streamed out: pending; else gone
  void OnEpoch(uint32_t generation);                          // drops every level-form id
  // A client binds S's id to a handle it already knows (by its own local id). A second bind of
  // the same server id to another live handle is ambiguous; a bind to a gone handle is refused.
  bool Bind(const EntityId& server_issued, Handle h, TimePoint now);

  struct Lookup {
    Resolution resolution = Resolution::kPending;
    Handle handle = kNoHandle;  // kResolved only
    TimePoint since{};          // when it entered this resolution (pending: how long it waits)
  };
  Lookup Resolve(const EntityId& id) const;
  // The id this peer reported for a live handle (its own local id, not a bound server id).
  std::optional<EntityId> IdOf(Handle h) const;
  // Ids known in each resolution (pending counts only ids that were seen and streamed out).
  size_t Count(Resolution r) const;
  uint32_t Generation() const;
  void Reset();

 private:
  struct Entry {
    std::vector<Handle> live;  // two or more: ambiguous
    bool streamed_out = false;
    bool gone = false;
    TimePoint since{};
  };
  uint32_t generation_ = 0;
  TimePoint epoch_began_{};
  std::map<EntityId, Entry> ids_;          // reported and bound ids
  std::map<Handle, EntityId> handles_;     // each live handle's own id
  std::map<Handle, EntityId> bound_;       // each bound handle's server-issued id
  // Authored ids by (epoch, mID): an id whose hashes differ from the one under its mID resolves
  // kUnresolved.
  std::map<std::pair<uint32_t, uint32_t>, EntityId> by_mid_;
};

}  // namespace skylanders::backend
