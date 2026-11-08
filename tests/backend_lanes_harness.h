#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <map>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <utility>
#include <vector>

#include "backend/adapter.h"
#include "backend/client.h"
#include "backend/entity_id.h"
#include "backend/fake_network.h"
#include "backend/fake_world.h"
#include "backend/fault_injector.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/map_dump.h"
#include "backend/request.h"
#include "backend/server.h"
#include "backend_test.h"
#include "backend_test_kinds.h"

// What the lanes, limits, interleaving, receipt and fault tests share (work item 3; design
// sections 5 and 6): a fake world per client (fake_world.h's FakeWorld: entities that exist from a
// time, a FakeKindAdapter per applied kind that keeps what it wrote and counts fulfilments), a generic authority for the
// three request models, a wire log fed by FakeSession's frame tap, and the "equal" check of section
// 5 (DiffMaps between S's map and the peer's). Game-free: the kinds are the test kinds, plus
// test.counter, a session-scope kind that takes commits (LM5b).

// test_util.h's REQUIRE_OR_RETURN returns 1; a BACKEND_TEST function returns nothing.
#ifndef REQUIRE_OR_RETURN_VOID
#define REQUIRE_OR_RETURN_VOID(cond)                                       \
  do {                                                                     \
    if (!(cond)) {                                                         \
      std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                        \
      return;                                                              \
    }                                                                      \
  } while (0)
#endif

namespace skylanders::backend::test::lanes {

using namespace std::chrono_literals;

// The test kinds plus test.counter (a session-scope kind that takes commits: LM5b, LM10), from
// backend_test_kinds.h, the one table.
using test::kCounter;
using test::kCounterDefault;
using test::kTestCounter;
inline constexpr const std::array<KindSpec, 8>& kLanesTable = test::kTestTableFull;

inline const TimePoint kStart = TimePoint{} + std::chrono::hours(1);

inline EntityId Ent(uint32_t generation, uint32_t n) {
  return Authored(generation, n, HashName("e" + std::to_string(n)), HashName("test"));
}
inline RecordKey Key(KindId kind, const EntityId& entity, uint16_t slot = 0) {
  return RecordKey{kind, entity, slot};
}
inline std::vector<uint8_t> Val(uint32_t v, size_t size = 4) {
  std::vector<uint8_t> out(std::max<size_t>(size, 1), 0);
  for (size_t i = 0; i < out.size() && i < 4; ++i) out[i] = uint8_t(v >> (8 * i));
  return out;
}
inline Duration Ms(int64_t ms) { return std::chrono::duration_cast<Duration>(std::chrono::milliseconds(ms)); }
inline double Seconds(Duration d) { return std::chrono::duration<double>(d).count(); }

// --- The fake world of one client: fake_world.h's FakeWorld, with a FakeKindAdapter per applied
// kind and entities that exist from a time.

using TestAdapter = FakeKindAdapter;

// Entities that exist from a time: Tick spawns each in the FakeWorld (which reports it to the
// client's IdentityMap) once it is due, and again after an epoch dropped it (a level id only in its
// own generation). An adapter per applied kind of the table, wrapped by the injector's apply layer
// when there is one.
class TestWorld {
 public:
  explicit TestWorld(std::span<const KindSpec> table = kLanesTable, FaultInjector* faults = nullptr,
                     uint16_t peer = 0)
      : world_(adapters_.Identity()) {
    for (const KindSpec& spec : table) {
      if (spec.write != WriteMode::kApplied) continue;
      auto adapter = std::make_unique<FakeKindAdapter>(world_, spec.id);
      adapter->SetLog(&apply_log_);
      KindAdapter* registered = adapter.get();
      if (faults) {
        wrapped_.push_back(faults->WrapAdapter(*adapter, peer, spec.id, &adapters_.Identity(),
                                               [this](Handle h) { Despawned(h); }));
        registered = wrapped_.back().get();
      }
      adapters_.Register(spec.id, *registered);
      by_kind_[spec.id] = std::move(adapter);
    }
  }
  TestWorld(const TestWorld&) = delete;
  TestWorld& operator=(const TestWorld&) = delete;

  AdapterSet& Adapters() { return adapters_; }
  IdentityMap& Identity() { return adapters_.Identity(); }
  FakeWorld& World() { return world_; }

  void Exists(const EntityId& id, TimePoint from) { exists_[id] = from; }
  // Destroyed in this epoch: gone, never spawned again.
  void Destroy(const EntityId& id, TimePoint now) {
    exists_.erase(id);
    if (auto it = handles_.find(id); it != handles_.end()) world_.Despawn(it->second, now);
  }
  // The world follows the session's generation (the game's level load: the level entities go and
  // the IdentityMap drops their ids), then spawns what is due.
  void Tick(TimePoint now, uint32_t generation) {
    now_ = now;
    for (auto& [kind, a] : by_kind_) a->now = now;
    if (Identity().Generation() != generation) {
      world_.NewGeneration(generation);
      std::erase_if(handles_, [](const auto& e) { return IsLevelForm(e.first.form); });
    }
    world_.Tick();
    for (const auto& [id, from] : exists_) {
      if (from > now) continue;
      if (IsLevelForm(id.form) && id.epoch != Identity().Generation()) continue;
      if (auto it = handles_.find(id); it != handles_.end() && world_.Live(it->second)) continue;
      handles_[id] = world_.Spawn(id, now);
    }
  }

  FakeKindAdapter& Of(KindId kind) { return *by_kind_.at(kind); }
  std::optional<Handle> HandleOf(const EntityId& id) const {
    auto it = handles_.find(id);
    return it == handles_.end() ? std::nullopt : std::optional<Handle>(it->second);
  }
  const std::vector<uint8_t>* Value(const RecordKey& key) {
    auto it = handles_.find(key.entity);
    if (it == handles_.end() || !by_kind_.contains(key.kind)) return nullptr;
    return by_kind_.at(key.kind)->Value(it->second, key.slot);
  }
  size_t Fulfilled(uint64_t id) const {
    size_t n = 0;
    for (const auto& [kind, a] : by_kind_) n += a->Fulfilled(id);
    return n;
  }
  size_t FulfilledTotal() const {
    size_t n = 0;
    for (const auto& [kind, a] : by_kind_) n += a->FulfilledTotal();
    return n;
  }
  size_t MaxFulfilledOnce() const {
    size_t n = 0;
    for (const auto& [kind, a] : by_kind_) n = std::max(n, a->MaxFulfilledOnce());
    return n;
  }
  const std::vector<KindId>& ApplyLog() const { return apply_log_; }

 private:
  void Despawned(Handle h) {
    for (auto& [id, handle] : handles_) {
      if (handle == h) {
        exists_.erase(id);
        break;
      }
    }
    world_.Despawn(h, now_);
  }

  AdapterSet adapters_;
  FakeWorld world_;
  std::map<KindId, std::unique_ptr<FakeKindAdapter>> by_kind_;
  std::vector<std::unique_ptr<KindAdapter>> wrapped_;
  std::map<EntityId, TimePoint> exists_;
  std::map<EntityId, Handle> handles_;
  TimePoint now_{};
  std::vector<KindId> apply_log_;
};

// --- S's side: one authority for the request models of every test kind.

inline RecordKey TargetOf(const Request& r) {
  return r.basis.kind != 0 ? r.basis : RecordKey{r.kind, r.subject, 0};
}

class TestAuthority final : public Authority {
 public:
  explicit TestAuthority(Duration lease_term = std::chrono::seconds(3)) : term_(lease_term) {}
  // Answers first when it returns a verdict (IL6's wait).
  std::function<std::optional<Verdict>(const Request&, const DecideContext&)> custom;

  Verdict Decide(const Request& r, const DecideContext& c) override {
    ++decides;
    if (custom) {
      if (auto v = custom(r, c)) return *v;
    }
    Verdict v;
    switch (r.model) {
      case Model::kCommit:
        v.type = Verdict::Type::kCommit;
        v.writes.push_back(Write{TargetOf(r), r.payload, false});
        break;
      case Model::kServerRun:
        v.type = Verdict::Type::kServerRun;
        break;
      case Model::kOwnerRun:
        v.type = Verdict::Type::kOwnerRun;
        v.lease = LeaseSpec{r.subject, r.kind, r.key.peer, term_};
        break;
    }
    return v;
  }
  Verdict OnReport(const Lease& lease, std::span<const uint8_t> payload,
                   const DecideContext& /*context*/) override {
    ++reports;
    Verdict v;
    v.type = Verdict::Type::kCommit;
    v.writes.push_back(Write{RecordKey{lease.spec.kind, lease.spec.entity, 0},
                             std::vector<uint8_t>(payload.begin(), payload.end()), false});
    return v;
  }
  uint64_t decides = 0;
  uint64_t reports = 0;  // OnReport calls (S2-L3: one per report, whatever copies arrive)

 private:
  Duration term_;
};

struct Authorities {
  explicit Authorities(Duration lease_term = std::chrono::seconds(3)) : authority(lease_term) {
    for (const KindSpec& spec : kLanesTable) {
      if (spec.models != 0) set.Register(spec.id, authority);
    }
  }
  TestAuthority authority;
  AuthoritySet set;
};

inline Request Commit(KindId kind, const EntityId& entity, std::vector<uint8_t> value) {
  Request r;
  r.kind = kind;
  r.model = Model::kCommit;
  r.subject = entity;
  r.payload = std::move(value);
  return r;
}
// A commit based on the revision the client saw (refused `stale basis` when it is not current).
inline Request CommitOn(KindId kind, const EntityId& entity, uint32_t revision,
                        std::vector<uint8_t> value) {
  Request r = Commit(kind, entity, std::move(value));
  r.basis = RecordKey{kind, entity, 0};
  r.basis_revision = revision;
  return r;
}
inline Request ServerRun(KindId kind, const EntityId& entity) {
  Request r;
  r.kind = kind;
  r.model = Model::kServerRun;
  r.subject = entity;
  return r;
}
inline Request OwnerRun(KindId kind, const EntityId& entity) {
  Request r;
  r.kind = kind;
  r.model = Model::kOwnerRun;
  r.subject = entity;
  return r;
}

// --- The wire, as the backend took it (FakeSession's frame tap).

struct WireLog {
  // Messages taken, by (peer, to S) and type.
  std::map<std::pair<uint16_t, bool>, std::map<MessageType, uint64_t>> counts;
  std::map<uint16_t, msg::Ack> last_ack;  // the newest Ack S read from each peer
  // From what each client sent (before faults): its newest Ack, its highest `holding` (the
  // client's obligation entries), and its messages by type.
  std::map<uint16_t, msg::Ack> last_sent_ack;
  std::map<uint16_t, uint32_t> max_holding;
  std::map<uint16_t, std::map<MessageType, uint64_t>> client_sent;
  std::function<void(uint16_t peer, bool to_server, const Message&)> on_message;
  std::function<void(uint16_t peer, const Message&)> on_client_sent;

  void Install(FakeSession& session) {
    session.on_frame = [this](uint16_t peer, bool to_server, netplay::Lane,
                              std::span<const uint8_t> frame) {
      std::vector<Message> messages;
      DecodeFrame(frame, messages);
      for (const Message& m : messages) {
        ++counts[{peer, to_server}][TypeOf(m)];
        if (auto* a = std::get_if<msg::Ack>(&m); a && to_server) last_ack[peer] = *a;
        if (on_message) on_message(peer, to_server, m);
      }
    };
    session.on_send = [this](uint16_t peer, bool to_server, netplay::Lane,
                             std::span<const uint8_t> frame) {
      if (!to_server) return;
      std::vector<Message> messages;
      DecodeFrame(frame, messages);
      for (const Message& m : messages) {
        ++client_sent[peer][TypeOf(m)];
        if (auto* a = std::get_if<msg::Ack>(&m)) {
          last_sent_ack[peer] = *a;
          max_holding[peer] = std::max(max_holding[peer], a->holding);
        }
        if (on_client_sent) on_client_sent(peer, m);
      }
    };
  }
  uint64_t Sent(uint16_t peer, MessageType type) const {
    auto it = client_sent.find(peer);
    if (it == client_sent.end()) return 0;
    auto jt = it->second.find(type);
    return jt == it->second.end() ? 0 : jt->second;
  }
  uint64_t Count(uint16_t peer, bool to_server, MessageType type) const {
    auto it = counts.find({peer, to_server});
    if (it == counts.end()) return 0;
    auto jt = it->second.find(type);
    return jt == it->second.end() ? 0 : jt->second;
  }
};

// --- Checks.

// Section 5's "equal": DiffMaps finds no row difference between S's map and the peer's, over the
// replicated rows of the peer's subscribed scopes (Opus F5: a peer that is not level-ready holds no
// level row). A failure prints the differences.
inline bool Equal(FakeSession& s, uint16_t peer, bool print = true) {
  std::string error;
  MapDumpOptions so;
  MapDumpOptions co;
  co.who = "peer " + std::to_string(peer);
  auto a = ParseMap(DumpMap(s.S().State(), so), &error);
  auto b = ParseMap(DumpMap(s.C(peer).State(), co), &error);
  if (!a || !b) {
    if (print) std::fprintf(stderr, "  map parse: %s\n", error.c_str());
    return false;
  }
  const ScopeMask subscribed = s.S().Stats(peer).subscription.scopes;  // as S subscribed it
  std::erase_if(a->row_lines, [&](const MapRow& row) {
    for (const KindSpec& spec : kLanesTable) {
      if (spec.name == row.kind) return (MaskOf(spec.scope) & subscribed) == 0;
    }
    return false;
  });
  const auto diffs = DiffMaps(*a, *b);
  if (print) {
    for (size_t i = 0; i < diffs.size() && i < 8; ++i) {
      std::fprintf(stderr, "  peer %u: %s\n", unsigned(peer), diffs[i].text.c_str());
    }
  }
  return diffs.empty();
}

// Steps until `done` holds, checking it only every `every` (for a costly check, such as Equal).
inline bool RunUntilEvery(FakeSession& s, const std::function<bool()>& done, Duration max,
                          Duration every = std::chrono::milliseconds(100)) {
  const TimePoint end = s.Now() + max;
  while (s.Now() < end) {
    s.Run(every);
    if (done()) return true;
  }
  return done();
}

inline size_t CountServer(FakeSession& s, ServerEvent::Type type, std::optional<uint16_t> peer = {}) {
  return size_t(std::count_if(s.ServerEvents().begin(), s.ServerEvents().end(),
                              [&](const ServerEvent& e) {
                                return e.type == type && (!peer || e.peer == *peer);
                              }));
}
inline size_t CountClient(FakeSession& s, uint16_t peer, ClientEvent::Type type) {
  const auto& events = s.ClientEvents(peer);
  return size_t(std::count_if(events.begin(), events.end(),
                              [&](const ClientEvent& e) { return e.type == type; }));
}
inline size_t CountLimit(FakeSession& s, std::string_view word) {
  return size_t(std::count_if(s.ServerEvents().begin(), s.ServerEvents().end(),
                              [&](const ServerEvent& e) {
                                return e.type == ServerEvent::Type::kLimit &&
                                       e.text.find(word) != std::string::npos;
                              }));
}
// The client's decisions for `key`, in arrival order.
inline std::vector<Decision> DecisionsFor(FakeSession& s, uint16_t peer, const RequestKey& key) {
  std::vector<Decision> out;
  for (const ClientEvent& e : s.ClientEvents(peer)) {
    if (e.type == ClientEvent::Type::kDecided && e.decision.key == key) out.push_back(e.decision);
  }
  return out;
}
inline std::optional<Decision> FinalFor(FakeSession& s, uint16_t peer, const RequestKey& key) {
  for (const Decision& d : DecisionsFor(s, peer, key)) {
    if (d.final) return d;
  }
  return std::nullopt;
}
inline bool Completed(FakeSession& s, uint16_t peer, const RequestKey& key) {
  for (const ClientEvent& e : s.ClientEvents(peer)) {
    if (e.type == ClientEvent::Type::kCompleted && e.key == key) return true;
  }
  return false;
}

// A session with every peer's world ticked before its pump, joined and level-ready.
struct Rig {
  explicit Rig(FakeSessionOptions options = {}, Duration lease_term = std::chrono::seconds(3))
      : authorities(lease_term), session(kLanesTable, authorities.set, std::move(options)) {
    session.before_client_pump = [this](uint16_t peer, TimePoint now) {
      auto it = worlds.find(peer);
      if (it != worlds.end()) it->second->Tick(now, session.Generation());
    };
    wire.Install(session);
  }
  // A new world for `peer` (a rejoin gets a fresh one), joined; the level too when `ready`.
  TestWorld& Join(uint16_t peer, bool ready = true) {
    if (session.Has(peer)) session.Leave(peer);  // the old client goes before its world
    worlds[peer] = std::make_unique<TestWorld>(kLanesTable, session.Faults(), peer);
    for (const auto& [id, from] : everywhere) worlds[peer]->Exists(id, from);
    session.Join(peer, worlds[peer]->Adapters(), peer == 1 ? PeerRole::kOwner : PeerRole::kGuest);
    if (ready) session.LevelReady(peer);
    return *worlds[peer];
  }
  // An entity every joined world has from `from` on.
  void Everywhere(const EntityId& id, TimePoint from) {
    everywhere[id] = from;
    for (auto& [peer, w] : worlds) w->Exists(id, from);
  }
  TestWorld& W(uint16_t peer) { return *worlds.at(peer); }
  Server& S() { return session.S(); }
  Client& C(uint16_t peer) { return session.C(peer); }
  TimePoint Now() const { return session.Now(); }
  // Puts on S, returning the revision written (0 if not written).
  uint32_t Put(const RecordKey& key, std::vector<uint8_t> value) {
    if (session.S().State().Put(key, value, session.Now()) != PutResult::kWritten) return 0;
    const Record* r = session.S().State().Get(key);
    return r ? r->revision : 0;
  }

  // The worlds outlive the session (its clients hold their adapters); the session outlives the
  // wire log its taps feed.
  Authorities authorities;
  std::map<uint16_t, std::unique_ptr<TestWorld>> worlds;
  std::map<EntityId, TimePoint> everywhere;  // given to every world, a rejoin's too
  FakeSession session;
  WireLog wire;
};

inline FakeSessionOptions WithFaults(std::string rules, uint64_t seed = 1) {
  FakeSessionOptions o;
  o.faults = std::move(rules);
  o.seed = seed;
  return o;
}

}  // namespace skylanders::backend::test::lanes
