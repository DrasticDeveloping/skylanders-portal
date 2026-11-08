// Versions (design section 5, VR1 to VR10; work item 4's checkpoint names VR7 to VR9, item 2's VR1
// to VR5 and VR10 on direct calls). VR1 to VR5 run on a Replica alone; VR6 to VR10 on Server and
// Client over FakeSession (the lanes harness's Rig), where the peer epoch, the table digest, the
// request cache and the walk's epoch entries live.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <optional>
#include <vector>

#include "backend/client.h"
#include "backend/fake_network.h"
#include "backend/lanes.h"
#include "backend/server.h"
#include "backend/world_state.h"
#include "backend_lanes_harness.h"
#include "backend_request_harness.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

namespace {

using namespace skylanders::backend;
using namespace std::chrono_literals;
namespace lanes = skylanders::backend::test::lanes;
namespace req = skylanders::backend::test::requests;
using lanes::Ent;
using lanes::Key;
using lanes::kLanesTable;
using lanes::Rig;
using lanes::Val;
using skylanders::backend::test::kProp;
using skylanders::backend::test::kToggle;
using skylanders::backend::test::kTracker;

Record Row(const RecordKey& key, uint32_t revision, uint32_t epoch, uint64_t lsn,
           std::vector<uint8_t> value, bool tomb = false) {
  Record r;
  r.key = key;
  r.revision = revision;
  r.epoch = epoch;
  r.lsn = lsn;
  r.tomb = tomb;
  if (!tomb) r.value = std::move(value);
  return r;
}

// A replica subscribed to both scopes in generation `g` by an empty baseline at LSN `at`.
void Subscribe(Replica& rep, uint32_t id, uint32_t g, uint64_t at) {
  rep.OnBaselineBegin(id, at, g, kScopeAll);
  rep.OnBaselineEnd(id, at, kBaselineDigestSeed);
}

// ---------------------------------------------------------------------------------------------
// On a Replica.

BACKEND_TEST("VR1", VersionsOldRevisionAfterReconnect) {
  Replica rep(kLanesTable, Limits{});
  Subscribe(rep, 1, 1, 0);
  const RecordKey k = Key(kProp, Ent(1, 1));
  CHECK(rep.OnRecord(Row(k, 5, 1, 5, Val(5)), 5) == ApplyResult::kApplied);
  rep.OnThrough(5);
  // A reconnect: a repair baseline carries revision 5 again, then the old revision 4 is delivered.
  const Record r5 = Row(k, 5, 1, 5, Val(5));
  rep.OnBaselineBegin(2, 5, 1, kScopeAll);
  rep.OnBaselineRow(2, r5);
  CHECK(rep.OnBaselineEnd(2, 5, BaselineDigestAdd(kBaselineDigestSeed, r5)).ok);
  CHECK(rep.OnRecord(Row(k, 4, 1, 4, Val(4)), 4) == ApplyResult::kStale);
  const Record* got = rep.Get(k);
  CHECK(got && got->revision == 5 && got->value == Val(5));
}

BACKEND_TEST("VR2", VersionsOldEpochRowDropped) {
  Replica rep(kLanesTable, Limits{});
  Subscribe(rep, 1, 1, 0);
  rep.OnEpoch(2, 10);
  const RecordKey k = Key(kProp, Ent(1, 1));
  CHECK(rep.OnRecord(Row(k, 1, 1, 8, Val(1)), 8) == ApplyResult::kWrongEpoch);
  CHECK(rep.Get(k) == nullptr);
}

BACKEND_TEST("VR3", VersionsNewEpochRowWaitsForMarker) {
  Replica rep(kLanesTable, Limits{});
  Subscribe(rep, 1, 1, 0);
  const RecordKey k = Key(kProp, Ent(2, 1));
  CHECK(rep.OnRecord(Row(k, 1, 2, 12, Val(1)), 12) == ApplyResult::kStaged);
  CHECK(rep.Get(k) == nullptr);
  rep.OnEpoch(2, 11);
  const Record* got = rep.Get(k);
  CHECK(got && got->epoch == 2 && got->value == Val(1));
}

BACKEND_TEST("VR4", VersionsSessionRowsKeptAcrossEpoch) {
  Replica rep(kLanesTable, Limits{});
  Subscribe(rep, 1, 1, 0);
  const RecordKey s = Key(kTracker, SessionObject(1));
  const RecordKey l = Key(kProp, Ent(1, 1));
  CHECK(rep.OnRecord(Row(s, 1, 0, 1, Val(1)), 1) == ApplyResult::kApplied);
  CHECK(rep.OnRecord(Row(l, 1, 1, 2, Val(2)), 2) == ApplyResult::kApplied);
  rep.OnThrough(2);
  rep.OnEpoch(2, 3);
  CHECK(rep.Get(s) && rep.Get(s)->value == Val(1));  // no baseline needed for it
  CHECK(rep.Get(l) == nullptr);
  CHECK((rep.Subscribed() & kScopeSession) != 0);
  CHECK((rep.Subscribed() & kScopeLevel) == 0);
}

BACKEND_TEST("VR5", VersionsTombBeatsOldValue) {
  Replica rep(kLanesTable, Limits{});
  Subscribe(rep, 1, 1, 0);
  const RecordKey k = Key(kProp, Ent(1, 1));
  CHECK(rep.OnRecord(Row(k, 7, 1, 7, {}, /*tomb=*/true), 7) == ApplyResult::kApplied);
  CHECK(rep.OnRecord(Row(k, 6, 1, 6, Val(6)), 6) == ApplyResult::kStale);
  const Record* got = rep.Get(k);
  CHECK(got == nullptr || got->tomb);
}

// ---------------------------------------------------------------------------------------------
// On Server and Client.

BACKEND_TEST("VR6", VersionsTableDigestRefused) {
  // S and the client with different kind tables (the client lacks test.counter).
  TimePoint now = lanes::kStart;
  auto [s_end, c_end] = MemoryLink::Pair(&now);
  AuthoritySet authorities;
  req::ScriptedAuthority authority;
  authorities.Register(kProp, authority);
  Server server(kLanesTable, authorities, Limits{});
  server.BeginEpoch(1, now);
  CHECK(server.State().Put(Key(kProp, Ent(1, 1)), Val(1), now) == PutResult::kWritten);
  CHECK(server.State().Put(Key(kTracker, SessionObject(1)), Val(2), now) == PutResult::kWritten);
  lanes::TestWorld world(skylanders::backend::test::kTestTable);
  Client client(skylanders::backend::test::kTestTable, world.Adapters(), Limits{});
  CHECK(TableDigest(skylanders::backend::test::kTestTable) != TableDigest(kLanesTable));
  server.AddPeer(1, PeerRole::kOwner, *s_end, now);
  client.Attach(*c_end, now);
  server.OnPeerLevelReady(1, 1, now);
  bool refused = false;
  for (int i = 0; i < 200; ++i) {
    server.Pump(now);
    client.Pump(now);
    ClientEvent e;
    while (client.PollEvent(e)) {
      if (e.type == ClientEvent::Type::kHelloRefused) refused = true;
      CHECK(e.type != ClientEvent::Type::kBaselineApplied);
    }
    now += 10ms;
  }
  CHECK(refused);
  CHECK(client.Self() == 0 && client.PeerEpoch() == 0);
  CHECK(client.State().Rows() == 0);  // nothing applied
  CHECK(world.Of(kProp).Applies() == 0);
  CHECK(!client.Send(lanes::Commit(kProp, Ent(1, 1), Val(3)), now).has_value());
}

BACKEND_TEST("VR7", VersionsDuplicateAfterStaleBasisCached) {
  req::TableRig t;
  const EntityId e = Ent(req::kGen, 1);
  CHECK(t.state.Put(Key(kProp, e), Val(1), t.now) == PutResult::kWritten);  // revision 1
  const Request r = t.Make(lanes::CommitOn(kProp, e, 1, Val(2)), 1);
  t.Send(r);
  CHECK(t.Final(r.key) && t.Final(r.key)->outcome == Outcome::kApplied);
  CHECK(t.state.Get(Key(kProp, e))->revision == 2);  // the basis is stale now
  t.Send(r);  // the duplicate
  const auto last = t.Last(r.key);
  CHECK(last && last->cached && last->outcome == Outcome::kApplied);
  CHECK(t.authority.decides == 1);
  req::CheckFinals(t);
}

BACKEND_TEST("VR8", VersionsRejoinNewPeerEpoch) {
  Rig r;
  r.Join(2);
  const EntityId e = Ent(r.session.Generation(), 10);
  r.Everywhere(e, r.Now());
  CHECK(r.session.RunUntil([&] { return r.C(2).PeerEpoch() != 0; }, 1s));
  const uint32_t e1 = r.C(2).PeerEpoch();
  CHECK(e1 == r.S().Stats(2).peer_epoch);
  // A Player row of the first join, on S (session scope).
  const EntityId p1 = Player(2, e1);
  CHECK(r.S().State().Put(Key(kTracker, p1), Val(1), r.Now()) == PutResult::kWritten);
  // A pending key (2, e1, g, 1): server-run, its evaluation never comes.
  const auto old_key = r.C(2).Send(lanes::ServerRun(kToggle, e), r.Now());
  REQUIRE_OR_RETURN_VOID(old_key.has_value());
  CHECK(old_key->peer_epoch == e1 && old_key->sequence == 1);
  r.session.Run(200ms);
  CHECK(r.S().Requests().StateOf(*old_key) == RequestState::kPending);
  // Peer 2 leaves (RemovePeer drops its keys) and comes back as peer 2.
  r.Join(2);
  r.Everywhere(e, r.Now());
  CHECK(r.S().Requests().StateOf(*old_key) == RequestState::kNew);
  CHECK(r.session.RunUntil([&] { return r.C(2).PeerEpoch() != 0; }, 1s));
  const uint32_t e2 = r.C(2).PeerEpoch();
  CHECK(e2 == e1 + 1 && e2 == r.S().Stats(2).peer_epoch);  // told in StateHello
  // Its first request is (2, e2, g, 1), decided fresh: a new evaluation, not the old cache.
  const size_t mark = r.session.ServerEvents().size();
  const auto new_key = r.C(2).Send(lanes::ServerRun(kToggle, e), r.Now());
  REQUIRE_OR_RETURN_VOID(new_key.has_value());
  CHECK(new_key->peer_epoch == e2 && new_key->sequence == 1);
  CHECK(r.session.RunUntil(
      [&] {
        const auto& ev = r.session.ServerEvents();
        for (size_t i = mark; i < ev.size(); ++i) {
          if (ev[i].type == ServerEvent::Type::kEvaluate && ev[i].request.key == *new_key) return true;
        }
        return false;
      },
      1s));
  for (const Decision& d : lanes::DecisionsFor(r.session, 2, *new_key)) CHECK(!d.cached);
  // Its Player id is new: the client builds it from the hello, equal to S's; the old rows never
  // meet it.
  const EntityId p2 = Player(r.C(2).Self(), r.C(2).PeerEpoch());
  CHECK(p2 == Player(2, r.S().Stats(2).peer_epoch));
  CHECK(p2 != p1);
  CHECK(r.S().State().Get(Key(kTracker, p2)) == nullptr);
}

BACKEND_TEST("VR9", VersionsDecisionOfOldPeerEpochDropped) {
  Rig r;
  r.Join(2);
  CHECK(r.session.RunUntil([&] { return r.C(2).PeerEpoch() != 0; }, 1s));
  const uint32_t e1 = r.C(2).PeerEpoch();
  r.Join(2);  // the rejoin
  CHECK(r.session.RunUntil([&] { return r.C(2).PeerEpoch() == e1 + 1; }, 1s));
  // A Decision for (2, e1, g, 1), delayed past the rejoin, arrives on the new link.
  Decision late;
  late.key = RequestKey{2, e1, r.session.Generation(), 1};
  late.outcome = Outcome::kApplied;
  late.final = true;
  std::vector<uint8_t> frame;
  CHECK(AppendMessage(msg::Decision{late}, frame));
  MemoryLink* raw = r.session.MemoryServerEnd(2);
  REQUIRE_OR_RETURN_VOID(raw != nullptr);
  CHECK(raw->Send(skylanders::netplay::Lane::kGameplay, frame) == LinkSend::kSent);
  // And one for its own peer epoch, which it takes.
  Decision mine = late;
  mine.key.peer_epoch = e1 + 1;
  mine.key.sequence = 7;
  frame.clear();
  CHECK(AppendMessage(msg::Decision{mine}, frame));
  CHECK(raw->Send(skylanders::netplay::Lane::kGameplay, frame) == LinkSend::kSent);
  r.session.Run(100ms);
  bool old_seen = false, new_seen = false;
  for (const ClientEvent& ev : r.session.ClientEvents(2)) {
    if (ev.type != ClientEvent::Type::kDecided) continue;
    if (ev.key == late.key) old_seen = true;
    if (ev.key == mine.key) new_seen = true;
  }
  CHECK(!old_seen);
  CHECK(new_seen);
}

BACKEND_TEST("VR10", VersionsEpochBeforeNewRows) {
  FakeSessionOptions options;
  options.limits.max_unacked_state_frames = 2;  // a long delta backlog
  options.memory.one_way = 40ms;
  Rig r(options);
  r.Join(1);
  CHECK(r.session.RunUntil(
      [&] {
        return (r.S().Stats(1).subscription.scopes & kScopeLevel) != 0 &&
               r.S().Stats(1).baseline == 0 && (r.C(1).State().Subscribed() & kScopeLevel) != 0;
      },
      10s));
  // Watch what reaches the client, in order.
  uint64_t epoch_lsn = 0;
  bool epoch_seen = false;
  bool new_row_before_epoch = false, through_past_epoch_early = false;
  r.wire.on_message = [&](uint16_t peer, bool to_server, const Message& m) {
    if (peer != 1 || to_server) return;
    if (auto* eb = std::get_if<msg::EpochBegin>(&m); eb && eb->generation == 2) epoch_seen = true;
    if (auto* d = std::get_if<msg::Delta>(&m); d && d->record.epoch == 2 && !epoch_seen) {
      new_row_before_epoch = true;
    }
    if (auto* t = std::get_if<msg::Through>(&m); t && epoch_lsn != 0 && t->lsn >= epoch_lsn && !epoch_seen) {
      through_past_epoch_early = true;
    }
  };
  // A long backlog of generation 1 rows, then the epoch change in the middle of it, then rows of
  // generation 2 (the peer level-ready again).
  for (uint32_t n = 1; n <= 300; ++n) r.Put(Key(kProp, Ent(1, n)), Val(n, 32));
  r.session.Run(30ms);
  r.session.NewGeneration();
  epoch_lsn = r.S().State().Head();
  r.session.LevelReady(1);
  for (uint32_t n = 1; n <= 100; ++n) r.Put(Key(kProp, Ent(2, n)), Val(n, 32));
  CHECK(r.session.RunUntil([&] { return r.C(1).State().Prefix() >= r.S().State().Head(); }, 30s));
  CHECK(epoch_seen);
  CHECK(!new_row_before_epoch);
  CHECK(!through_past_epoch_early);
  r.wire.on_message = nullptr;
}

}  // namespace
