#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <initializer_list>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "backend/adapter.h"
#include "backend/base.h"
#include "backend/client.h"
#include "backend/compare.h"
#include "backend/entity_id.h"
#include "backend/fake_network.h"
#include "backend/fake_world.h"
#include "backend/fault_injector.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/limits.h"
#include "backend/map_dump.h"
#include "backend/record.h"
#include "backend/request.h"
#include "backend/server.h"
#include "backend/world_state.h"
#include "backend_test.h"

// The converge template (design section 7.3): one kind through a set of fault profiles, graded by
// the compare queries and an invariant checker. A new kind copies one block (the page
// docs/backend/adding-a-kind.md walks test.toggle's):
//
//   ConvergeKind toggle{
//     .spec = kTestToggle, .adapter = MakeFakeToggleAdapter, .authority = MakeToggleAuthority,
//     .script = [](ConvergeScript& s) { s.Spawn(20); s.WriteEvery(50ms, 30s); s.Request(Model::kCommit, 10); },
//   };
//   RunConverge(toggle, {Profile::kPlain, Profile::kLoss5Rtt300, Profile::kLoss30Rtt300,
//                        Profile::kKilledDelta, Profile::kMidBaselineDisconnect, Profile::kThrottledPeer,
//                        Profile::kLateJoiner, Profile::kTeardownDuringApply, Profile::kStreamOut},
//               /*seeds=*/kDefaultSeeds);  // 8; 32 with --long
//
// One run: S and two clients (the owner, peer 1, and a guest, peer 2) on one clock, each with its
// own world holding the script's entities. S's world writes (a native write, then Put), clients
// send requests, the profile's faults play, then the session goes quiet (limits.compare_quiet) and
// is graded: every row of every replicated kind of the table `equal` or `repaired` on every client
// (`unsupported` for a kNever kind), none still pending (the quiet period is past every grace),
// and the invariant checker passes:
//   - one final decision per request key (a second final one with another outcome is a defect);
//   - no obligation fulfilled twice on one peer epoch;
//   - no client told `credit exceeded` (it holds at most obligation_credit; Sol r3 F9, draft 5);
//   - no revision going down on any replica;
//   - no fenced row applied before every dependency row with a first change at or below its LSN
//     has settled, unless a `fence broken` event names the pair in a run with injected or
//     scripted faults (Sol F2, draft 4);
//   - no `completed` before the client holds the request's row at its at_lsn or newer, unless a
//     kill hit the run (Sol F1, PP8);
//   - every injected fault seen (a rule that never fired: the profile's case was not reached);
//   - every row TakeReady handed out answered by OnApplied in the same pump (Replica::HandedOut is
//     0 after every step);
//   - S's `used` for a client never below the client's obligation count (Server::Stats against
//     Client::Obligations, every step).
//
// The profiles:
//   kPlain                 MemoryLink, 10 ms each way
//   kLoss5Rtt300           NetplayLink, 150 ms each way, 5 % of datagrams lost each way (netplay's
//                          lanes resend: loss is lateness and head-of-line blocking)
//   kLoss30Rtt300          the same at 30 % (stage 3's transport gate figures, plan 4)
//   kKilledDelta           message:kill dir=s2c type=delta nth=3 (a bug model above the lane: one
//                          mismatch, one repair baseline)
//   kMidBaselineDisconnect peer:disconnect peer=2 after=baseline_rows:1; the guest rejoins 1 s later
//                          as a new peer epoch
//   kThrottledPeer         link:throttle peer=2 bps=1000
//   kLateJoiner            the guest joins halfway through the writes
//   kTeardownDuringApply   halfway through the writes the level is torn down while rows are in
//                          flight: S begins generation 2, every world drops its entities and loads
//                          the level again, the writes go on in generation 2
//   kStreamOut             one second into the writes the owner streams 3 entities out, and back in
//                          1.5 s later (their rows wait, then apply)
//
// The runs are on fake_network.h's FakeSession (MemoryLink, or NetplayLink over netplay's fake
// network for the loss profiles; the profile's rules through its fault injector). The world is
// fake_world.h's FakeWorld, with a FakeKindAdapter per kind unless the kind brings its own.

namespace skylanders::backend::test {

enum class Profile : uint8_t {
  kPlain,
  kLoss5Rtt300,
  kLoss30Rtt300,
  kKilledDelta,
  kMidBaselineDisconnect,
  kThrottledPeer,
  kLateJoiner,
  kTeardownDuringApply,
  kStreamOut,
};

inline std::string_view ProfileName(Profile p) {
  switch (p) {
    case Profile::kPlain: return "plain";
    case Profile::kLoss5Rtt300: return "loss5-rtt300";
    case Profile::kLoss30Rtt300: return "loss30-rtt300";
    case Profile::kKilledDelta: return "killed-delta";
    case Profile::kMidBaselineDisconnect: return "mid-baseline-disconnect";
    case Profile::kThrottledPeer: return "throttled-peer";
    case Profile::kLateJoiner: return "late-joiner";
    case Profile::kTeardownDuringApply: return "teardown-during-apply";
    case Profile::kStreamOut: return "stream-out";
  }
  return "?";
}

inline constexpr std::array<Profile, 9> kEveryProfile{
    Profile::kPlain,         Profile::kLoss5Rtt300,          Profile::kLoss30Rtt300,
    Profile::kKilledDelta,   Profile::kMidBaselineDisconnect, Profile::kThrottledPeer,
    Profile::kLateJoiner,    Profile::kTeardownDuringApply,  Profile::kStreamOut};

// One peer's world is fake_world.h's FakeWorld; the plain adapter its FakeKindAdapter (the kind's
// value is the entity's field; Fulfil counts each obligation id).
inline std::unique_ptr<KindAdapter> MakeFieldAdapter(FakeWorld& world, KindId kind) {
  return std::make_unique<FakeKindAdapter>(world, kind);
}

// The plain authority: a commit writes the payload as the subject's value; server-run hands the
// request to S's game thread (the harness writes its world and the row); owner-run grants a lease
// on the subject, and the report's payload is written.
class FieldAuthority : public Authority {
 public:
  explicit FieldAuthority(KindId kind) : kind_(kind) {}
  Verdict Decide(const Request& request, const DecideContext& context) override {
    Verdict v;
    switch (request.model) {
      case Model::kCommit:
        v.type = Verdict::Type::kCommit;
        v.writes.push_back(Write{RecordKey{kind_, request.subject, 0}, request.payload, false});
        break;
      case Model::kServerRun:
        v.type = Verdict::Type::kServerRun;
        break;
      case Model::kOwnerRun:
        v.type = Verdict::Type::kOwnerRun;
        v.lease.entity = request.subject;
        v.lease.kind = kind_;
        v.lease.holder = request.key.peer;
        v.lease.term = std::chrono::seconds(3);
        break;
    }
    (void)context;
    return v;
  }
  Verdict OnReport(const Lease& lease, std::span<const uint8_t> payload,
                   const DecideContext& /*context*/) override {
    Verdict v;
    v.type = Verdict::Type::kCommit;
    v.writes.push_back(Write{RecordKey{kind_, lease.spec.entity, 0},
                             std::vector<uint8_t>(payload.begin(), payload.end()), false});
    return v;
  }

 private:
  KindId kind_;
};

inline std::unique_ptr<Authority> MakeFieldAuthority(KindId kind) {
  return std::make_unique<FieldAuthority>(kind);
}

// What the kind's script asks for. Times count from the start of the writes.
class ConvergeScript {
 public:
  // n entities of the kind in the level, on every peer's world.
  void Spawn(uint32_t n) { entities = n; }
  // S's world changes a random entity's value every `interval` for `duration` (a native write,
  // then Put).
  void WriteEvery(Duration interval, Duration duration) {
    write_interval = interval;
    write_for = duration;
  }
  // n requests of this model from the clients, spread over the writes, on random entities.
  void Request(Model model, uint32_t n) { requests.push_back({model, n}); }

  struct Ask {
    Model model = Model::kCommit;
    uint32_t n = 0;
  };
  uint32_t entities = 0;
  Duration write_interval{};
  Duration write_for{};
  std::vector<Ask> requests;
};

struct ConvergeKind {
  KindSpec spec;
  std::function<std::unique_ptr<KindAdapter>(FakeWorld&, KindId)> adapter = MakeFieldAdapter;
  std::function<std::unique_ptr<Authority>(KindId)> authority = MakeFieldAuthority;
  std::function<void(ConvergeScript&)> script;
  // The table the kind lives in, when it depends on other kinds (they get the plain adapter and
  // authority); empty: the spec alone.
  std::span<const KindSpec> table = {};
  // The nth value the script writes; empty: n as one byte (a toggle's 0 and 1 when n is even and
  // odd, so a kind of at most one byte sees both).
  std::function<std::vector<uint8_t>(uint32_t n)> value = {};
};

struct ConvergeRun {
  Profile profile = Profile::kPlain;
  uint64_t seed = 0;
  bool ok = false;
  std::vector<std::string> failures;
  size_t rows = 0;           // rows graded on the last compare, every client together
  size_t repaired = 0;
  uint64_t baselines = 0;    // repair and subscription baselines S began
  uint64_t faults = 0;       // faults the injector played
  int64_t sim_ms = 0;
};

namespace converge_detail {

using namespace std::chrono_literals;

struct Rng {
  uint64_t s;
  uint64_t Next() {
    uint64_t z = (s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
  }
  uint64_t Below(uint64_t n) { return n == 0 ? 0 : Next() % n; }
};

inline std::vector<uint8_t> DefaultValue(const KindSpec& spec, uint32_t n) {
  std::vector<uint8_t> v;
  const size_t size = std::clamp<size_t>(spec.max_value_bytes, 1, 4);
  for (size_t i = 0; i < size; ++i) v.push_back(uint8_t(n >> (8 * i)));
  return v;
}

// The profile as FakeSession options: the links and the fault rules.
inline FakeSessionOptions OptionsFor(Profile profile, uint64_t seed) {
  FakeSessionOptions o;
  o.seed = seed;
  o.memory.one_way = 10ms;
  switch (profile) {
    case Profile::kLoss5Rtt300:
    case Profile::kLoss30Rtt300:
      o.netplay = true;
      o.conditions.delay_ms = 150;
      o.conditions.loss_percent = profile == Profile::kLoss5Rtt300 ? 5 : 30;
      break;
    case Profile::kKilledDelta: o.faults = "message:kill dir=s2c type=delta nth=3"; break;
    case Profile::kMidBaselineDisconnect:
      o.faults = "peer:disconnect peer=2 after=baseline_rows:1";
      break;
    case Profile::kThrottledPeer: o.faults = "link:throttle peer=2 bps=1000"; break;
    default: break;
  }
  return o;
}

// One peer's world, adapters and identity. S's has an adapter for every kind (it reads its own
// world); a client's for every applied kind. It outlives the client it serves (a rejoin keeps the
// world and its identity map, as the game would).
struct PeerWorld {
  AdapterSet set;
  FakeWorld world{set.Identity()};
  std::vector<std::unique_ptr<KindAdapter>> adapters;
  std::map<KindId, FakeKindAdapter*> fields;  // the plain adapters, for their counters
};

// What the checker keeps about one client, from its join to its leave.
struct ClientState {
  uint16_t id = 0;
  PeerRole role = PeerRole::kGuest;
  PeerWorld w;
  bool joined = false;
  std::optional<TimePoint> join_at;
  std::map<RecordKey, uint32_t> revisions;          // the newest seen per key (monotonic check)
  std::map<RequestKey, Decision> finals;             // the first final decision per key
  std::map<RequestKey, RecordKey> asked;             // each request's row
  std::map<RequestKey, uint32_t> report_value;       // owner-run: what the holder reports
  std::vector<std::pair<RecordKey, RecordKey>> fence_suspects;  // (row, dependency), this step
  std::vector<std::pair<RecordKey, RecordKey>> fence_broken;    // named by events, this step
  std::vector<std::pair<Model, uint32_t>> to_send;   // requests waiting for StateHello
};

class Runner {
 public:
  Runner(const ConvergeKind& kind, Profile profile, uint64_t seed)
      : kind_(kind),
        profile_(profile),
        seed_(seed),
        table_(kind.table.empty() ? std::span<const KindSpec>(&kind.spec, 1) : kind.table),
        rng_{seed * 0x9E3779B97F4A7C15ull + uint64_t(profile) + 1} {
    if (kind_.script) kind_.script(script_);
    for (const KindSpec& spec : table_) {
      if (spec.models == 0) continue;
      authorities_.push_back(kind_.authority && spec.id == kind_.spec.id ? kind_.authority(spec.id)
                                                                        : MakeFieldAuthority(spec.id));
      authority_set_.Register(spec.id, *authorities_.back());
    }
    for (uint16_t id : {uint16_t(1), uint16_t(2)}) {
      auto c = std::make_unique<ClientState>();
      c->id = id;
      c->role = id == 1 ? PeerRole::kOwner : PeerRole::kGuest;
      clients_.push_back(std::move(c));
    }
    session_ = std::make_unique<FakeSession>(table_, authority_set_, OptionsFor(profile, seed));
    start_ = session_->Now();
    if (!session_->FaultParseErrorText().empty()) {
      Fail("fault rules: " + session_->FaultParseErrorText());
    }
    session_->on_server_event = [this](const ServerEvent& e) { OnServerEvent(e); };
    session_->on_client_event = [this](uint16_t peer, const ClientEvent& e) {
      OnClientEvent(peer, e);
    };
    session_->before_client_pump = [this](uint16_t peer, TimePoint) {
      if (ClientState* c = Find(peer)) c->fence_suspects.clear();
    };
  }

  ConvergeRun Run() {
    ConvergeRun out;
    out.profile = profile_;
    out.seed = seed_;
    const Duration writes_start = 500ms;
    const Duration writes_end = writes_start + script_.write_for;
    const Duration half = writes_start + script_.write_for / 2;

    // The level loads everywhere (FakeSession began generation 1); S logs every initial value.
    MakeAdapters(server_world_, /*client=*/false, 0);
    LoadLevel(server_world_);
    for (auto& c : clients_) {
      MakeAdapters(c->w, /*client=*/true, c->id);
      LoadLevel(c->w);
    }
    for (auto& c : clients_) {
      if (c->id == 2 && profile_ == Profile::kLateJoiner) {
        c->join_at = start_ + half;
      } else {
        Join(*c);
      }
    }

    // The scripted writes and requests, as times from the start.
    std::vector<Duration> writes;
    if (script_.write_interval > Duration::zero()) {
      for (Duration t = writes_start; t < writes_end; t += script_.write_interval) writes.push_back(t);
    }
    std::vector<std::pair<Duration, Model>> asks;
    for (const ConvergeScript::Ask& a : script_.requests) {
      const Duration span = std::max<Duration>(script_.write_for, 1s);
      for (uint32_t i = 0; i < a.n; ++i) {
        asks.emplace_back(writes_start + span * (i + 1) / (a.n + 1), a.model);
      }
    }
    std::stable_sort(asks.begin(), asks.end(),
                     [](const auto& a, const auto& b) { return a.first < b.first; });
    size_t next_write = 0, next_ask = 0, ask_count = 0;
    bool torn_down = false, streamed_out = false, streamed_in = false;
    std::vector<Handle> stream_handles;

    const Duration last_event = std::max(writes_end, writes_start + 3s);
    const Limits& limits = session_->Lim();
    const TimePoint quiet_from = start_ + last_event + limits.compare_quiet;
    const TimePoint deadline = start_ + last_event + limits.silence_baseline + 15s;
    TimePoint next_grade = quiet_from;
    bool converged = false;

    while (session_->Now() < deadline) {
      const TimePoint now = session_->Now();
      const Duration t = now - start_;

      // Scripted events due now, before the pumps.
      while (next_write < writes.size() && writes[next_write] <= t) {
        ++next_write;
        NativeWrite();
      }
      while (next_ask < asks.size() && asks[next_ask].first <= t) {
        ClientState& c = *clients_[ask_count++ % clients_.size()];
        c.to_send.emplace_back(asks[next_ask].second, value_counter_++);
        ++next_ask;
      }
      if (profile_ == Profile::kTeardownDuringApply && !torn_down && t >= half) {
        torn_down = true;
        Teardown();
      }
      if (profile_ == Profile::kStreamOut && !streamed_out && t >= writes_start + 1s) {
        streamed_out = true;
        ClientState& owner = *clients_[0];
        for (uint32_t i = 0; i < std::min<uint32_t>(3, script_.entities); ++i) {
          if (auto h = owner.w.world.HandleOf(EntityOf(session_->Generation(), i))) {
            owner.w.world.StreamOut(*h, now);
            stream_handles.push_back(*h);
          }
        }
      }
      if (streamed_out && !streamed_in && t >= writes_start + 2500ms) {
        streamed_in = true;
        for (Handle h : stream_handles) clients_[0]->w.world.StreamIn(h, now);
      }
      for (auto& c : clients_) {
        if (!c->joined && c->join_at && now >= *c->join_at) Join(*c);
      }
      SendRequests();

      for (auto& c : clients_) c->fence_broken.clear();
      session_->Step();
      SyncServerWorld();
      AfterStep();

      if (session_->Now() >= next_grade) {
        next_grade = session_->Now() + 500ms;
        std::vector<std::string> why;
        Grade(why, /*final=*/false, out);
        if (why.empty()) {
          converged = true;
          break;
        }
      }
    }

    std::vector<std::string> why;
    Grade(why, /*final=*/true, out);
    if (!converged && why.empty()) why.push_back("never quiet");
    for (std::string& w : why) Fail(std::move(w));
    if (FaultInjector* f = session_->Faults()) {
      for (size_t rule : f->NotFired()) {
        Fail("fault rule " + std::to_string(rule + 1) + " (" + FormatFaultRule(f->Rules()[rule]) +
             ") never fired: case not reached");
      }
    }
    out.failures = failures_;
    out.ok = failures_.empty();
    out.faults = session_->FaultEvents().size();
    out.baselines = baselines_;
    out.sim_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(session_->Now() - start_).count();
    if (!out.ok) DumpForFailure();
    return out;
  }

 private:
  bool Faulty() const { return profile_ != Profile::kPlain; }

  ClientState* Find(uint16_t peer) {
    for (auto& c : clients_) {
      if (c->id == peer) return c.get();
    }
    return nullptr;
  }

  void Fail(std::string why) {
    const int64_t ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(session_->Now() - start_).count();
    failures_.push_back("converge " + std::string(kind_.spec.name) + " " +
                        std::string(ProfileName(profile_)) + " seed " + std::to_string(seed_) +
                        " at " + std::to_string(ms) + " ms: " + why);
  }

  std::vector<uint8_t> Value(uint32_t n) const {
    return kind_.value ? kind_.value(n) : DefaultValue(kind_.spec, n);
  }
  std::vector<uint8_t> Initial(const KindSpec& spec) const {
    if (spec.initial == InitialPolicy::kAuthoredDefault) {
      return std::vector<uint8_t>(spec.authored_default.begin(), spec.authored_default.end());
    }
    return spec.id == kind_.spec.id ? Value(0) : DefaultValue(spec, 0);
  }
  EntityId EntityOf(uint32_t generation, uint32_t i) const {
    return Authored(generation, 0x100 + i, HashName("converge_" + std::to_string(i)),
                    HashName(kind_.spec.name));
  }
  // Level kinds sit on the level's entities; a session kind on session objects.
  EntityId SubjectOf(const KindSpec& spec, uint32_t i) const {
    return spec.scope == Scope::kSession ? SessionObject(0x100 + i)
                                         : EntityOf(session_->Generation(), i);
  }

  void MakeAdapters(PeerWorld& w, bool client, uint16_t peer) {
    for (const KindSpec& spec : table_) {
      if (client && spec.write != WriteMode::kApplied) continue;
      auto adapter = spec.id == kind_.spec.id && kind_.adapter ? kind_.adapter(w.world, spec.id)
                                                                : MakeFieldAdapter(w.world, spec.id);
      if (auto* f = dynamic_cast<FakeKindAdapter*>(adapter.get())) {
        w.fields[spec.id] = f;
        if (client && Fenced(spec)) {
          const KindId kind = spec.id;
          f->on_apply = [this, &w, kind, peer](Handle h, uint16_t slot) {
            CheckFence(w, peer, kind, h, slot);
          };
        }
      }
      w.set.Register(spec.id, *adapter);
      w.adapters.push_back(std::move(adapter));
    }
  }

  Handle SpawnOnce(PeerWorld& w, const EntityId& id) {
    if (const std::optional<Handle> h = w.world.HandleOf(id)) return *h;
    return w.world.Spawn(id, session_->Now());
  }

  // Every entity of the level (and the session objects once), each kind's field at its initial
  // value. On S each initial value is also logged (read at spawn, or the authored default).
  void LoadLevel(PeerWorld& w) {
    const bool server = &w == &server_world_;
    w.world.NewGeneration(session_->Generation());
    const bool session_too = !session_loaded_.contains(&w);
    session_loaded_.insert(&w);
    for (uint32_t i = 0; i < script_.entities; ++i) {
      for (const KindSpec& spec : table_) {
        if (spec.scope == Scope::kSession && !session_too) continue;
        const EntityId id = SubjectOf(spec, i);
        const Handle h = SpawnOnce(w, id);
        w.world.SetField(h, spec.id, 0, Initial(spec));
        if (server) Put(spec, id, Initial(spec));
      }
    }
  }

  void Put(const KindSpec& spec, const EntityId& id, const std::vector<uint8_t>& value) {
    const PutResult r =
        session_->S().State().Put(RecordKey{spec.id, id, 0}, value, session_->Now());
    if (r != PutResult::kWritten && r != PutResult::kUnchanged) {
      Fail("Put " + std::string(spec.name) + " " + ToText(id) + ": " +
           std::string(PutResultName(r)));
    }
  }

  // S's world changes one entity of the kind by itself.
  void NativeWrite() {
    if (script_.entities == 0) return;
    const EntityId id = SubjectOf(kind_.spec, uint32_t(rng_.Below(script_.entities)));
    const std::vector<uint8_t> value = Value(value_counter_++);
    if (auto h = server_world_.world.HandleOf(id)) {
      server_world_.world.SetField(*h, kind_.spec.id, 0, value);
    }
    Put(kind_.spec, id, value);
  }

  // S's game applies what S's table committed (the rows S's world does not hold yet).
  void SyncServerWorld() {
    session_->S().State().ForEach([&](const Record& r) {
      uint32_t& synced = synced_[r.key];
      if (r.revision <= synced) return;
      synced = r.revision;
      if (r.tomb) return;
      if (auto h = server_world_.world.HandleOf(r.key.entity)) {
        server_world_.world.SetField(*h, r.key.kind, r.key.slot, r.value);
      }
    });
  }

  void Join(ClientState& c) {
    c.revisions.clear();
    c.finals.clear();
    c.asked.clear();
    c.report_value.clear();
    for (auto& [kind, f] : c.w.fields) f->fulfilled.clear();
    session_->Join(c.id, c.w.set, c.role);
    session_->LevelReady(c.id);
    c.joined = true;
    c.join_at.reset();
  }

  // The level is torn down while rows are in flight, and loaded again in the next generation.
  void Teardown() {
    session_->NewGeneration();
    LoadLevel(server_world_);
    for (auto& c : clients_) {
      LoadLevel(c->w);
      if (c->joined) session_->LevelReady(c->id);
    }
  }

  void SendRequests() {
    for (auto& c : clients_) {
      if (!c->joined || !session_->Has(c->id)) continue;
      Client& client = session_->C(c->id);
      for (auto it = c->to_send.begin(); it != c->to_send.end();) {
        if (client.Self() == 0) break;  // not keyed before StateHello (Sol F3)
        Request req;
        req.kind = kind_.spec.id;
        req.model = it->first;
        req.subject =
            SubjectOf(kind_.spec, uint32_t(rng_.Below(std::max<uint32_t>(script_.entities, 1))));
        req.payload = Value(it->second);
        if (const std::optional<RequestKey> key = client.Send(req, session_->Now())) {
          c->asked[*key] = RecordKey{kind_.spec.id, req.subject, 0};
          c->report_value[*key] = it->second + 1;
        }
        it = c->to_send.erase(it);
      }
    }
  }

  void OnServerEvent(const ServerEvent& e) {
    switch (e.type) {
      case ServerEvent::Type::kEvaluate: {
        // S's game thread runs the request: its world, then the row.
        const KindSpec* spec = FindKind(table_, e.request.kind);
        EvaluationResult result;
        if (spec != nullptr) {
          if (auto h = server_world_.world.HandleOf(e.request.subject)) {
            server_world_.world.SetField(*h, spec->id, 0, e.request.payload);
          }
          Put(*spec, e.request.subject, e.request.payload);
          result.scope = spec->scope;
        }
        session_->S().SubmitEvaluation(e.request.key, result, session_->Now());
        break;
      }
      case ServerEvent::Type::kBaselineBegun: ++baselines_; break;
      default: break;
    }
  }

  void OnClientEvent(uint16_t peer, const ClientEvent& e) {
    ClientState* c = Find(peer);
    if (c == nullptr) return;
    switch (e.type) {
      case ClientEvent::Type::kDecided: {
        const Decision& d = e.decision;
        if (d.outcome == Outcome::kGranted && session_->Has(peer)) {
          const auto v = c->report_value.find(d.key);
          session_->C(peer).Report(d.lease, Value(v == c->report_value.end() ? 1 : v->second),
                                   session_->Now());
        }
        if (!d.final) break;
        const auto [it, first] = c->finals.try_emplace(d.key, d);
        if (!first && (it->second.outcome != d.outcome || it->second.reason != d.reason)) {
          Fail("peer " + std::to_string(peer) + " request " + std::to_string(d.key.sequence) +
               ": a second final decision (" + std::string(OutcomeName(it->second.outcome)) +
               ", then " + std::string(OutcomeName(d.outcome)) + ")");
        }
        break;
      }
      case ClientEvent::Type::kCompleted: CheckCompleted(*c, e.key); break;
      case ClientEvent::Type::kCreditExceeded:
        Fail("peer " + std::to_string(peer) + ": credit exceeded");
        break;
      case ClientEvent::Type::kBaselineLost:
        // Only a message killed above the lane loses a BaselineEnd (Sol code review r2, R4).
        if (!Faulty()) Fail("peer " + std::to_string(peer) + ": a baseline end lost with no fault");
        break;
      case ClientEvent::Type::kFenceBroken:
        c->fence_broken.emplace_back(e.row, e.dependency);
        if (!Faulty()) {
          Fail("peer " + std::to_string(peer) + ": fence broken with no fault (" +
               ToText(e.row.entity) + " on " + ToText(e.dependency.entity) + ")");
        }
        break;
      default: break;
    }
  }

  // After every step: a peer the injector disconnected left (FakeSession); it rejoins 1 s later
  // as a new peer epoch. Then the per-step invariants.
  void AfterStep() {
    for (auto& c : clients_) {
      if (c->joined && !session_->Has(c->id)) {
        c->joined = false;
        c->join_at = session_->Now() + 1s;
      }
      if (!c->joined) continue;
      for (const auto& suspect : c->fence_suspects) {
        if (std::find(c->fence_broken.begin(), c->fence_broken.end(), suspect) ==
            c->fence_broken.end()) {
          Fail("peer " + std::to_string(c->id) + ": fenced " + ToText(suspect.first.entity) +
               " applied before its dependency " + ToText(suspect.second.entity) + " settled");
        }
      }
      // No revision goes down on a replica.
      session_->C(c->id).State().ForEach([&](const Record& r) {
        uint32_t& seen = c->revisions[r.key];
        if (r.revision < seen) {
          Fail("peer " + std::to_string(c->id) + ": " + ToText(r.key.entity) + " went from rev " +
               std::to_string(seen) + " to " + std::to_string(r.revision));
        }
        seen = std::max(seen, r.revision);
      });
      // Every row TakeReady handed out was answered by OnApplied in the same pump (Sol F2).
      if (const size_t out = session_->C(c->id).State().HandedOut(); out != 0) {
        Fail("peer " + std::to_string(c->id) + ": " + std::to_string(out) +
             " rows handed out and not answered in the pump");
      }
      // S never counts fewer obligations used than the client holds (Sol r3 F9, draft 5).
      const size_t used = session_->S().Stats(c->id).credit_used;
      const size_t holding = session_->C(c->id).Obligations().Holding();
      if (used < holding) {
        Fail("peer " + std::to_string(c->id) + ": S counts " + std::to_string(used) +
             " obligations used, the client holds " + std::to_string(holding));
      }
      // No obligation fulfilled twice.
      for (const auto& [kind, f] : c->w.fields) {
        for (const auto& [id, times] : f->fulfilled) {
          if (times > 1) {
            Fail("peer " + std::to_string(c->id) + ": obligation " + std::to_string(id) +
                 " fulfilled " + std::to_string(times) + " times");
          }
        }
      }
    }
  }

  // A fenced row is being applied: every dependency row at or below its LSN must have settled
  // (its value in the world, or its entity gone), or a fence broken event must name the pair.
  void CheckFence(PeerWorld& w, uint16_t peer, KindId kind, Handle h, uint16_t slot) {
    ClientState* c = Find(peer);
    const KindSpec* spec = FindKind(table_, kind);
    if (c == nullptr || spec == nullptr || !session_->Has(peer)) return;
    const std::optional<EntityId> id = w.set.Identity().IdOf(h);
    if (!id) return;
    const Replica& replica = session_->C(peer).State();
    const Record* row = replica.Get(RecordKey{kind, *id, slot});
    if (row == nullptr) return;
    const RecordKey row_key = row->key;
    const uint64_t row_lsn = row->lsn;
    for (const KindId dep : spec->depends_on) {
      replica.ForEach([&](const Record& d) {
        if (d.key.kind != dep || d.lsn > row_lsn || d.tomb) return;
        const IdentityMap::Lookup found = w.set.Identity().Resolve(d.key.entity);
        if (found.resolution != Resolution::kResolved && found.resolution != Resolution::kPending) {
          return;  // gone or unresolved: dropped, settled
        }
        const std::vector<uint8_t>* live =
            found.resolution == Resolution::kResolved ? w.world.Field(found.handle, dep, d.key.slot)
                                                      : nullptr;
        if (live == nullptr || *live != d.value) c->fence_suspects.emplace_back(row_key, d.key);
      });
    }
  }

  void CheckCompleted(ClientState& c, const RequestKey& key) {
    const auto asked = c.asked.find(key);
    const auto final = c.finals.find(key);
    if (asked == c.asked.end() || final == c.finals.end() || !session_->Has(c.id)) return;
    const Decision& d = final->second;
    if (d.outcome != Outcome::kApplied || d.key.generation != session_->Generation()) return;
    const Record* row = session_->C(c.id).State().Get(asked->second);
    const bool killed = profile_ == Profile::kKilledDelta;
    if ((row == nullptr || row->lsn < d.at_lsn) && !killed) {
      Fail("peer " + std::to_string(c.id) + " request " + std::to_string(key.sequence) +
           ": completed before the row at lsn " + std::to_string(d.at_lsn) + " was held");
    }
  }

  // The compare: S against each joined client, for every replicated kind. A row not equal or
  // repaired (unsupported for a kNever kind) is not converged; at the end, long past every grace,
  // a failure. `final` repairs on the clients and lists every reason.
  void Grade(std::vector<std::string>& why, bool final, ConvergeRun& out) {
    out.rows = 0;
    out.repaired = 0;
    const TimePoint now = session_->Now();
    for (auto& c : clients_) {
      if (!c->joined || !session_->Has(c->id)) {
        why.push_back("peer " + std::to_string(c->id) + " not joined");
        if (!final) return;
        continue;
      }
      for (const KindSpec& spec : table_) {
        if (!Replicated(spec)) continue;
        const std::vector<LocalCompare> s =
            CompareLocal(session_->S().State(), server_world_.set, spec.id, false, now);
        const std::vector<LocalCompare> l =
            CompareLocal(session_->C(c->id).State(), c->w.set, spec.id, /*repair=*/final, now);
        for (const RowCompare& r : JoinCompare(s, l)) {
          ++out.rows;
          const bool ok = spec.initial == InitialPolicy::kNever
                              ? r.result == CompareResult::kUnsupported
                              : r.result == CompareResult::kEqual ||
                                    r.result == CompareResult::kRepaired;
          if (r.result == CompareResult::kRepaired) ++out.repaired;
          if (!ok) {
            why.push_back(CompareLine(c->id, spec.name, r));
            if (!final) return;
          }
        }
      }
    }
  }

  void DumpForFailure() {
    const auto a = ParseMap(DumpMap(session_->S().State(), MapDumpOptions{}), nullptr);
    for (auto& c : clients_) {
      if (!a || !session_->Has(c->id)) continue;
      MapDumpOptions peer;
      peer.who = "peer " + std::to_string(c->id);
      const auto b = ParseMap(DumpMap(session_->C(c->id).State(), peer), nullptr);
      if (!b) continue;
      size_t shown = 0;
      for (const MapDiff& d : DiffMaps(*a, *b)) {
        if (++shown > 8) break;
        std::fprintf(stderr, "    diff S peer %u: %s\n", unsigned(c->id), d.text.c_str());
      }
    }
  }

  const ConvergeKind& kind_;
  Profile profile_;
  uint64_t seed_;
  std::span<const KindSpec> table_;
  ConvergeScript script_;
  Rng rng_;
  TimePoint start_{};
  uint32_t value_counter_ = 1;
  std::vector<std::unique_ptr<Authority>> authorities_;
  AuthoritySet authority_set_;
  PeerWorld server_world_;
  std::vector<std::unique_ptr<ClientState>> clients_;
  std::set<const PeerWorld*> session_loaded_;
  std::map<RecordKey, uint32_t> synced_;
  std::vector<std::string> failures_;
  uint64_t baselines_ = 0;
  // Last, so it goes first: its clients hold the worlds' adapter sets, its S the authorities.
  std::unique_ptr<FakeSession> session_;
};

}  // namespace converge_detail

// Runs `kind` through every profile on `seeds` seeds each (kDefaultSeeds: the run's own count, 8,
// or 32 with --long; any other number: exactly that many). A failed run is a failed check, printed
// with its profile, seed, reasons and the map diff of S against each client.
inline std::vector<ConvergeRun> RunConverge(const ConvergeKind& kind,
                                            std::initializer_list<Profile> profiles,
                                            int seeds = kDefaultSeeds) {
  const int n = seeds == kDefaultSeeds ? Seeds() : seeds;
  std::vector<ConvergeRun> runs;
  for (const Profile p : profiles) {
    for (int s = 1; s <= n; ++s) {
      converge_detail::Runner runner(kind, p, uint64_t(s));
      ConvergeRun run = runner.Run();
      if (!run.ok) {
        ++g_failures;
        for (size_t i = 0; i < run.failures.size() && i < 8; ++i) {
          std::fprintf(stderr, "FAIL %s\n", run.failures[i].c_str());
        }
      }
      runs.push_back(std::move(run));
    }
  }
  return runs;
}

}  // namespace skylanders::backend::test
