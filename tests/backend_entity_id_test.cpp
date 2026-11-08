// The identity tests (design section 10, ID1 to ID10): EntityId's forms, text and wire form,
// HashName, and IdentityMap (what an id names on this peer).

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <iterator>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

#include "backend/entity_id.h"
#include "backend/fake_world.h"
#include "backend/lanes.h"
#include "backend/record.h"
#include "backend/world_state.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

// ID2 links the session library's hash beside the backend's. The backend may include no online/
// header (D2, backend.include_rule), so the one function is declared here; skylanders_backend
// links skylanders_online, which defines it (online/protocol.cpp).
namespace skylanders::online {
uint32_t NameHash(std::string_view name);
}

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using test::kProp;
using test::kTestTable;
using test::kTracker;

const TimePoint kT0 = TimePoint{} + 10s;

std::vector<uint8_t> Wire(const EntityId& id) {
  std::vector<uint8_t> out;
  CHECK(Encode(id, out));
  return out;
}

// Writes the wire form field by field, whatever the id holds (Encode refuses an invalid one).
std::vector<uint8_t> RawWire(const EntityId& id) {
  std::vector<uint8_t> out;
  Writer w(out);
  w.U8(uint8_t(id.form));
  w.U32(id.epoch);
  w.U32(id.a);
  w.U32(id.b);
  w.U32(id.c);
  w.U16(id.ordinal);
  w.U16(id.incarnation);
  return out;
}

std::optional<EntityId> DecodeBytes(const std::vector<uint8_t>& bytes) {
  Reader in(bytes);
  return Decode(in);
}

EntityId Trigger(uint32_t epoch) {
  return Authored(epoch, 2394, HashName("JumpHint_Trigger"), HashName("CTriggerVolume"));
}

}  // namespace
}  // namespace skylanders::backend

using namespace skylanders::backend;

BACKEND_TEST("ID1", IdRoundTrip) {
  const EntityId parent = Trigger(3);
  const EntityId ids[] = {
      Trigger(3),
      Spawned(3, &parent, HashName("wave"), 1, 0),
      Spawned(3, nullptr, HashName("coin"), 2, 1),
      Player(2, 5),
      SessionObject(7),
      ServerIssued(3, 41),
  };
  std::set<std::string> texts;
  for (const EntityId& id : ids) {
    CHECK(Valid(id));
    const std::vector<uint8_t> wire = Wire(id);
    CHECK(wire.size() == kEntityIdWireBytes);
    Reader in(wire);
    const std::optional<EntityId> back = Decode(in);
    CHECK(back.has_value() && *back == id);
    CHECK(in.Ok() && in.Remaining() == 0);
    texts.insert(ToText(id));
  }
  CHECK(texts.size() == std::size(ids));  // every form prints differently

  // The text forms (hashes in hex as 1B's request lines write refs).
  CHECK(ToText(Authored(3, 0x10, 0xabc, 0xdef)) == "a00000010.00000abc.00000def@3");
  CHECK(ToText(Spawned(3, nullptr, 0x1234, 2, 1)) == "s-.00001234#2/1@3");
  {
    const EntityId s = Spawned(4, &parent, 0x99, 1, 0);
    char expected[64];
    std::snprintf(expected, sizeof(expected), "s%08x.00000099#1/0@4", unsigned(ParentDigest(parent)));
    CHECK(ToText(s) == expected);
  }
  CHECK(ToText(Player(2, 5)) == "p2/5");
  CHECK(ToText(SessionObject(7)) == "x7");
  CHECK(ToText(ServerIssued(3, 41)) == "i41@3");
  CHECK(ToText(EntityId{}) == "-");

  // A form with a foreign field set is refused: not Valid, not encoded, not decoded.
  auto with = [](EntityId id, auto change) {
    change(id);
    return id;
  };
  const EntityId foreign[] = {
      EntityId{},                                                      // kNone
      with(Trigger(3), [](EntityId& i) { i.ordinal = 1; }),            // authored with an ordinal
      with(Trigger(3), [](EntityId& i) { i.incarnation = 1; }),        // authored with an incarnation
      with(Trigger(3), [](EntityId& i) { i.epoch = 0; }),              // a level form without epoch
      with(ids[2], [](EntityId& i) { i.c = 1; }),                      // spawned with field c
      with(Player(2, 5), [](EntityId& i) { i.epoch = 1; }),            // player with an epoch
      with(Player(2, 5), [](EntityId& i) { i.c = 9; }),                // player with field c
      with(Player(2, 5), [](EntityId& i) { i.a = 0x10000; }),          // peer id past u16
      with(SessionObject(7), [](EntityId& i) { i.b = 1; }),            // session with field b
      with(SessionObject(7), [](EntityId& i) { i.ordinal = 1; }),      // session with an ordinal
      with(ServerIssued(3, 41), [](EntityId& i) { i.b = 1; }),         // server-issued with field b
      with(ServerIssued(3, 41), [](EntityId& i) { i.incarnation = 2; }),
      ServerIssued(3, 0),                                              // server id 0
      with(Trigger(3), [](EntityId& i) { i.form = IdForm(6); }),       // a form this build lacks
  };
  for (const EntityId& id : foreign) {
    CHECK(!Valid(id));
    std::vector<uint8_t> out{0xAA};
    CHECK(!Encode(id, out));
    CHECK(out.size() == 1);  // nothing appended
    CHECK(!DecodeBytes(RawWire(id)).has_value());
  }

  // A short wire form is refused and fails the reader.
  std::vector<uint8_t> shortened = Wire(ids[0]);
  shortened.pop_back();
  Reader in(shortened);
  CHECK(!Decode(in).has_value());
  CHECK(!in.Ok());
}

BACKEND_TEST("ID2", IdHashMatchesSession) {
  // 1B's test strings (online_protocol_test.cpp) and the FNV-1a vectors.
  const std::string_view names[] = {
      "",          "a",           "JumpHint_Trigger", "CTriggerVolume",   "Birdee_A_Flyoff",
      "CEntity",   "Cutscene_01", "Tutorial_B_Intro", "CLevelEndMessage", "CMessengerComponent+24",
      "conversation",
  };
  for (const std::string_view name : names) CHECK(HashName(name) == skylanders::online::NameHash(name));
  CHECK(HashName("") == 2166136261u);
  CHECK(HashName("a") == 0xE40C292Cu);
}

BACKEND_TEST("ID3", IdAmbiguousNeverPicked) {
  IdentityMap map;
  map.OnEpoch(1);
  const EntityId id = Trigger(1);
  map.OnSpawn(id, 10, kT0);
  CHECK(map.Resolve(id).resolution == Resolution::kResolved);
  map.OnSpawn(id, 11, kT0 + 1s);  // a second live thing claims the same id
  const IdentityMap::Lookup both = map.Resolve(id);
  CHECK(both.resolution == Resolution::kAmbiguous);
  CHECK(both.handle == kNoHandle);  // never picked
  CHECK(both.since == kT0 + 1s);
  CHECK(map.Count(Resolution::kAmbiguous) == 1);
  CHECK(map.Count(Resolution::kResolved) == 0);
  // A third claim stays ambiguous; once only one is left, it resolves to that one.
  map.OnSpawn(id, 12, kT0 + 2s);
  CHECK(map.Resolve(id).resolution == Resolution::kAmbiguous);
  map.OnDespawn(11, /*streamed_out=*/false, kT0 + 3s);
  CHECK(map.Resolve(id).resolution == Resolution::kAmbiguous);
  map.OnDespawn(12, /*streamed_out=*/false, kT0 + 4s);
  const IdentityMap::Lookup one = map.Resolve(id);
  CHECK(one.resolution == Resolution::kResolved && one.handle == 10);
  CHECK(one.since == kT0 + 4s);
  // The request half ("a request naming it is refused `unresolved`") is the request table's: it
  // runs with item 4's RequestTable (TODO in the report), which reads this resolution.
}

BACKEND_TEST("ID4", IdPendingThenResolved) {
  // A row's entity before it spawns: pending since the epoch began (the first time the map sees
  // a clock after OnEpoch), so the waiting row's age, and with it the kind's grace, is measurable.
  IdentityMap map;
  map.OnEpoch(1);
  const EntityId early = Spawned(1, nullptr, HashName("crate"), 1, 0);
  const EntityId never = Spawned(1, nullptr, HashName("crate"), 2, 0);
  const EntityId other = Trigger(1);
  map.OnSpawn(other, 5, kT0);  // the epoch's first event
  IdentityMap::Lookup l = map.Resolve(early);
  CHECK(l.resolution == Resolution::kPending);
  CHECK(l.handle == kNoHandle);
  CHECK(l.since == kT0);
  // The spawn resolves it: the row can be applied.
  map.OnSpawn(early, 6, kT0 + 300ms);
  l = map.Resolve(early);
  CHECK(l.resolution == Resolution::kResolved && l.handle == 6 && l.since == kT0 + 300ms);
  // The other one never comes: past the kind's grace it is late (the Replica raises `row late`
  // from its own clock, AF2; here the identity side of it).
  const auto grace = std::chrono::milliseconds(test::kTestProp.pending_grace_ms);
  const TimePoint later = kT0 + grace + 1ms;
  l = map.Resolve(never);
  CHECK(l.resolution == Resolution::kPending);
  CHECK(later - l.since > grace);
  // Still applied if it comes after all.
  map.OnSpawn(never, 7, later);
  CHECK(map.Resolve(never).resolution == Resolution::kResolved);
}

BACKEND_TEST("ID5", IdStreamOutVersusDestroy) {
  IdentityMap map;
  map.OnEpoch(2);
  const EntityId id = Spawned(2, nullptr, HashName("chompy"), 3, 0);
  map.OnSpawn(id, 100, kT0);
  CHECK(map.IdOf(100) == id);

  // Streamed out: pending, no handle; it may come back, under another handle.
  map.OnDespawn(100, /*streamed_out=*/true, kT0 + 1s);
  IdentityMap::Lookup l = map.Resolve(id);
  CHECK(l.resolution == Resolution::kPending && l.handle == kNoHandle && l.since == kT0 + 1s);
  CHECK(map.Count(Resolution::kPending) == 1);
  CHECK(!map.IdOf(100).has_value());
  map.OnSpawn(id, 101, kT0 + 2s);
  l = map.Resolve(id);
  CHECK(l.resolution == Resolution::kResolved && l.handle == 101);  // never the stale 100

  // The old handle reused by another thing does not bring the id back to it.
  const EntityId stranger = Spawned(2, nullptr, HashName("chompy"), 4, 0);
  map.OnSpawn(stranger, 100, kT0 + 3s);
  CHECK(map.Resolve(id).handle == 101);
  CHECK(map.IdOf(100) == stranger);

  // Destroyed: gone for the rest of the epoch; no handle, so no write reaches one.
  map.OnDespawn(101, /*streamed_out=*/false, kT0 + 4s);
  l = map.Resolve(id);
  CHECK(l.resolution == Resolution::kGone && l.handle == kNoHandle && l.since == kT0 + 4s);
  CHECK(map.Count(Resolution::kGone) == 1);
  // A handle reported under a new id without a despawn: the old id is gone from it.
  const EntityId third = Spawned(2, nullptr, HashName("chompy"), 5, 0);
  map.OnSpawn(third, 100, kT0 + 5s);
  CHECK(map.Resolve(stranger).resolution == Resolution::kGone);
  CHECK(map.Resolve(third).handle == 100);
}

BACKEND_TEST("ID6", IdEpochScope) {
  IdentityMap map;
  map.OnEpoch(1);
  const EntityId authored = Trigger(1);
  const EntityId spawned = Spawned(1, &authored, HashName("wave"), 1, 0);
  const EntityId player = Player(1, 1);
  const EntityId session = SessionObject(3);
  map.OnSpawn(authored, 1, kT0);
  map.OnSpawn(spawned, 2, kT0);
  map.OnSpawn(player, 3, kT0);
  map.OnSpawn(session, 4, kT0);
  CHECK(map.Bind(ServerIssued(1, 9), 2, kT0));
  CHECK(map.Count(Resolution::kResolved) == 5);

  map.OnEpoch(2);
  CHECK(map.Generation() == 2);
  // Level ids drop: the old ones name nothing (the wrong epoch), the same tuple in the new epoch
  // waits for its own spawn.
  CHECK(map.Resolve(authored).resolution == Resolution::kUnresolved);
  CHECK(map.Resolve(spawned).resolution == Resolution::kUnresolved);
  CHECK(map.Resolve(ServerIssued(1, 9)).resolution == Resolution::kUnresolved);
  CHECK(map.Resolve(Trigger(2)).resolution == Resolution::kPending);
  CHECK(!map.IdOf(1).has_value() && !map.IdOf(2).has_value());
  // Player and session ids stay.
  IdentityMap::Lookup l = map.Resolve(player);
  CHECK(l.resolution == Resolution::kResolved && l.handle == 3);
  l = map.Resolve(session);
  CHECK(l.resolution == Resolution::kResolved && l.handle == 4);
  CHECK(map.Count(Resolution::kResolved) == 2);
  CHECK(map.IdOf(3) == player);

  map.Reset();
  CHECK(map.Count(Resolution::kResolved) == 0);
  CHECK(map.Generation() == 0);
}

BACKEND_TEST("ID7", IdSpawnedIncarnation) {
  const EntityId wave = Trigger(1);
  const EntityId first = Spawned(1, &wave, HashName("enemy"), 2, 0);
  const EntityId again = Spawned(1, &wave, HashName("enemy"), 2, 1);  // the same ordinal after a restart
  CHECK(Valid(first) && Valid(again));
  CHECK(first != again);
  CHECK(ToText(first) != ToText(again));
  const RecordKey old_key{kProp, first, 0};
  const RecordKey new_key{kProp, again, 0};
  CHECK(old_key != new_key);

  // On S: the old incarnation's row becomes a tomb; the new one starts its own row.
  WorldState s(kTestTable, Limits{});
  s.BeginEpoch(1);
  const std::vector<uint8_t> value{1, 2, 3};
  CHECK(s.Put(old_key, value, kT0) == PutResult::kWritten);
  CHECK(s.Remove(old_key, kT0 + 1s) == PutResult::kWritten);
  const Record* old_row = s.Get(old_key);
  CHECK(old_row != nullptr && old_row->tomb);
  // A late copy of the old incarnation's value never brings its row back.
  if (old_row != nullptr) {
    Record late;
    late.key = old_key;
    late.revision = old_row->revision - 1;
    late.epoch = 1;
    late.value = value;
    CHECK(!Newer(late, old_row, 1));
  }
  CHECK(s.Get(new_key) == nullptr);
  CHECK(s.Put(new_key, value, kT0 + 2s) == PutResult::kWritten);
  const Record* new_row = s.Get(new_key);
  CHECK(new_row != nullptr && !new_row->tomb && new_row->revision == 1);

  // On a client: the restarted thing is a new id; the old one is gone.
  IdentityMap map;
  map.OnEpoch(1);
  map.OnSpawn(first, 20, kT0);
  map.OnDespawn(20, /*streamed_out=*/false, kT0 + 1s);
  map.OnSpawn(again, 21, kT0 + 2s);
  CHECK(map.Resolve(first).resolution == Resolution::kGone);
  CHECK(map.Resolve(again).handle == 21);
}

BACKEND_TEST("ID8", IdPeerEpoch) {
  // S's ids for peer 2's first and second join.
  const EntityId first = Player(2, 1);
  const EntityId second = Player(2, 2);
  CHECK(first != second);

  // The client learns its peer and peer epoch from StateHello and builds the same id.
  msg::StateHello hello;
  hello.peer = 2;
  hello.peer_epoch = 2;
  hello.epoch = 1;
  hello.head = 40;
  std::vector<uint8_t> frame;
  CHECK(AppendMessage(Message{hello}, frame));
  std::vector<Message> messages;
  CHECK(DecodeFrame(frame, messages));
  CHECK(messages.size() == 1);
  if (messages.size() == 1 && std::holds_alternative<msg::StateHello>(messages[0])) {
    const msg::StateHello& got = std::get<msg::StateHello>(messages[0]);
    CHECK(Player(got.peer, got.peer_epoch) == second);
    CHECK(Player(got.peer, got.peer_epoch) != first);
  } else {
    CHECK(false);
  }

  // The returning peer never meets its old rows: they are another key.
  WorldState s(kTestTable, Limits{});
  const RecordKey old_key{kTracker, first, 0};
  const RecordKey new_key{kTracker, second, 0};
  const std::vector<uint8_t> value{7, 0, 0, 0};
  CHECK(s.Put(old_key, value, kT0) == PutResult::kWritten);
  CHECK(s.Get(new_key) == nullptr);
  CHECK(s.Put(new_key, std::vector<uint8_t>{1, 0, 0, 0}, kT0 + 1s) == PutResult::kWritten);
  const Record* row = s.Get(new_key);
  CHECK(row != nullptr && row->revision == 1 && !row->value.empty() && row->value[0] == 1);
}

BACKEND_TEST("ID9", IdServerIssuedBind) {
  // On the fake world (fake_world.h): its spawns, despawns and streaming feed the client's
  // IdentityMap as a game's adapters would; the client binds S's ids to the handles it knows.
  IdentityMap map;
  FakeWorld world(map);
  world.NewGeneration(1);
  const EntityId local_a = Spawned(1, nullptr, HashName("gem"), 1, 0);
  const EntityId local_b = Spawned(1, nullptr, HashName("gem"), 2, 0);
  const Handle a = world.Spawn(local_a, kT0);
  const Handle b = world.Spawn(local_b, kT0);
  const EntityId sid = ServerIssued(1, 100);
  CHECK(map.Resolve(sid).resolution == Resolution::kPending);  // not bound yet

  CHECK(map.Bind(sid, a, kT0 + 1s));
  IdentityMap::Lookup l = map.Resolve(sid);
  CHECK(l.resolution == Resolution::kResolved && l.handle == a && l.since == kT0 + 1s);
  CHECK(map.Bind(sid, a, kT0 + 1s));       // a repeat changes nothing
  CHECK(map.IdOf(a) == local_a);           // the handle keeps its own id
  // A second live handle under the same server id: ambiguous, never picked.
  CHECK(map.Bind(sid, b, kT0 + 2s));
  l = map.Resolve(sid);
  CHECK(l.resolution == Resolution::kAmbiguous && l.handle == kNoHandle);
  world.Despawn(b, kT0 + 3s);
  CHECK(map.Resolve(sid).handle == a);
  // One handle names one server id.
  CHECK(!map.Bind(ServerIssued(1, 101), a, kT0 + 3s));

  // A bind to a gone, a streamed-out or an unknown handle is refused.
  CHECK(!map.Bind(ServerIssued(1, 102), b, kT0 + 4s));  // destroyed above
  const Handle c = world.Spawn(Spawned(1, nullptr, HashName("gem"), 3, 0), kT0 + 4s);
  world.StreamOut(c, kT0 + 5s);
  CHECK(!map.Bind(ServerIssued(1, 103), c, kT0 + 5s));
  CHECK(!map.Bind(ServerIssued(1, 104), 99, kT0 + 5s));
  // Only a valid server-issued id of this epoch binds.
  CHECK(!map.Bind(local_a, a, kT0 + 5s));
  CHECK(!map.Bind(ServerIssued(2, 105), a, kT0 + 5s));
  CHECK(!map.Bind(ServerIssued(1, 0), a, kT0 + 5s));

  // The bound handle streaming out takes the binding with it; back in, it binds only by Bind.
  world.StreamOut(a, kT0 + 6s);
  CHECK(map.Resolve(sid).resolution == Resolution::kPending);
  world.StreamIn(a, kT0 + 7s);
  CHECK(map.Resolve(sid).resolution == Resolution::kPending);
  CHECK(map.Bind(sid, a, kT0 + 7s));
  CHECK(map.Resolve(sid).handle == a);

  // The epoch (the fake world's level unload) drops binds.
  world.NewGeneration(2);
  CHECK(world.Entities() == 0);
  CHECK(map.Resolve(sid).resolution == Resolution::kUnresolved);
  CHECK(map.Resolve(ServerIssued(2, 100)).resolution == Resolution::kPending);
  CHECK(map.Count(Resolution::kResolved) == 0);
}

BACKEND_TEST("ID10", IdSpawnedParentForms) {
  const EntityId authored = Trigger(5);
  const EntityId spawned_parent = Spawned(5, nullptr, HashName("spawner"), 1, 0);
  const EntityId issued = ServerIssued(5, 77);
  const EntityId parents[] = {authored, spawned_parent, issued};
  std::set<uint32_t> digests;
  for (const EntityId& parent : parents) {
    const EntityId child = Spawned(5, &parent, HashName("minion"), 1, 0);
    CHECK(Valid(child));
    CHECK(child.a == ParentDigest(parent));
    CHECK(child.a != 0);
    digests.insert(child.a);
    // The digest is FNV-1a 32 over the parent's wire form.
    uint32_t h = 2166136261u;
    for (const uint8_t b : Wire(parent)) {
      h ^= b;
      h *= 16777619u;
    }
    CHECK(ParentDigest(parent) == h);
    // The same parent gives the same child on every peer.
    CHECK(Spawned(5, &parent, HashName("minion"), 1, 0) == child);
  }
  CHECK(digests.size() == 3);

  // Parentless: digest 0, keyed by spawn key and ordinal alone.
  const EntityId orphan = Spawned(5, nullptr, HashName("minion"), 1, 0);
  CHECK(Valid(orphan) && orphan.a == 0);
  CHECK(ToText(orphan).starts_with("s-."));
  CHECK(Spawned(5, nullptr, HashName("minion"), 1, 0) == orphan);
  CHECK(Spawned(5, nullptr, HashName("minion"), 2, 0) != orphan);
  CHECK(Spawned(5, nullptr, HashName("other"), 1, 0) != orphan);

  // Valid() refuses key hash 0 or ordinal 0; incarnation 0 is the first one.
  CHECK(!Valid(Spawned(5, nullptr, 0, 1, 0)));
  CHECK(!Valid(Spawned(5, &authored, HashName("minion"), 0, 0)));
  CHECK(Valid(Spawned(5, &authored, HashName("minion"), 1, 0)));
}
