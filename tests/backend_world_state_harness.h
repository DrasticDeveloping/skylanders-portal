#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "backend/kind_table.h"
#include "backend/limits.h"
#include "backend/record.h"
#include "backend/world_state.h"
#include "backend_test.h"

// A direct-call harness for the world state tests (design section 3.5; work item 2): S's per-peer
// send order (a baseline when due, a running baseline's rows, deltas from the cursor in
// first-change order and Through{proven}, a manifest at the head) written over WorldState alone,
// and a client over Replica alone, joined by an ordered, reliable message channel with a delay, a
// loss model below the lane (a lost datagram costs a retransmission, never the order) and a kill
// hook above it (a bug model: S cannot see it). It has no codec, no PeerLink and no obligations:
// the design's ids run on backend::Server and backend::Client over MemoryLink and NetplayLink in
// backend_flow_test.cpp; this layer's are "WS-" ids. DirectClient drives a Replica on direct calls
// with per-key spawn times and failures, below any entity or adapter, so it keeps its own record
// map rather than fake_world.h; its tables (TableA without test.gate for SR6, TableB with the
// chain for SR7, and test.mover for LM9) are local to these direct-call tests. Swap Force is not
// in it: the kinds are the test kinds.

namespace skylanders::backend::test::ws {

using namespace std::chrono_literals;

inline const TimePoint kT0 = TimePoint{} + std::chrono::hours(10);

struct Rng {
  uint64_t s;
  uint64_t Next() {
    uint64_t z = (s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
  }
  // Uniform in [0, n).
  uint64_t Below(uint64_t n) { return n == 0 ? 0 : Next() % n; }
  bool Chance(double p) { return double(Next() >> 11) / double(1ull << 53) < p; }
};

// ---------------------------------------------------------------------------------------------
// The test kinds. test.toggle, test.gate, test.scene and test.prop as section 5 names them;
// test.server_only (Opus F5), test.tracker (a session-scope value) and test.mover (a noisy
// current value, LM9).
// ---------------------------------------------------------------------------------------------

constexpr KindId kToggle = 1;
constexpr KindId kGate = 2;
constexpr KindId kScene = 3;
constexpr KindId kProp = 4;
constexpr KindId kServerOnly = 5;
constexpr KindId kTracker = 6;
constexpr KindId kMover = 7;

inline constexpr KindId kOnToggle[] = {kToggle};
inline constexpr KindId kOnGate[] = {kGate};

inline KindSpec LevelKind(KindId id, std::string_view name) {
  KindSpec k;
  k.id = id;
  k.name = name;
  k.scope = Scope::kLevel;
  k.tier = Tier::kStructural;
  k.state_class = StateClass::kCurrentValue;
  k.initial = InitialPolicy::kReadAtSpawn;
  k.write = WriteMode::kApplied;
  k.max_value_bytes = 64;
  k.pending_grace_ms = 5000;
  return k;
}

inline std::vector<KindSpec> MakeTable(bool gate_chain) {
  std::vector<KindSpec> t;
  t.push_back(LevelKind(kToggle, "test.toggle"));
  if (gate_chain) {
    KindSpec gate = LevelKind(kGate, "test.gate");
    gate.depends_on = kOnToggle;
    t.push_back(gate);
  }
  KindSpec scene = LevelKind(kScene, "test.scene");
  scene.depends_on = gate_chain ? std::span<const KindId>(kOnGate) : std::span<const KindId>(kOnToggle);
  t.push_back(scene);
  t.push_back(LevelKind(kProp, "test.prop"));
  KindSpec server_only = LevelKind(kServerOnly, "test.server_only");
  server_only.write = WriteMode::kServerOnly;
  server_only.initial = InitialPolicy::kNever;
  server_only.unsupported = true;
  t.push_back(server_only);
  KindSpec tracker = LevelKind(kTracker, "test.tracker");
  tracker.scope = Scope::kSession;
  tracker.write = WriteMode::kMirrored;
  tracker.initial = InitialPolicy::kAuthoredDefault;
  t.push_back(tracker);
  KindSpec mover = LevelKind(kMover, "test.mover");
  mover.noisy = true;
  t.push_back(mover);
  return t;
}

// test.scene on test.toggle (SR6 family).
inline std::span<const KindSpec> TableA() {
  static const std::vector<KindSpec> t = MakeTable(false);
  return t;
}
// test.scene on test.gate on test.toggle (SR7 family).
inline std::span<const KindSpec> TableB() {
  static const std::vector<KindSpec> t = MakeTable(true);
  return t;
}

// A level row's key: an authored entity (mID n) in generation g.
inline RecordKey LevelKey(KindId kind, uint32_t n, uint32_t g = 1) {
  return RecordKey{kind, Authored(g, n, 0x1000 + n, 0x2000 + kind), 0};
}
inline RecordKey SessionKey(KindId kind, uint32_t n) {
  return RecordKey{kind, SessionObject(n), 0};
}
inline std::vector<uint8_t> V(uint32_t v, size_t size = 4) {
  std::vector<uint8_t> out(size, 0);
  for (size_t i = 0; i < size && i < 4; ++i) out[i] = uint8_t(v >> (8 * i));
  return out;
}

// Subscribes a replica to the given scopes through an empty baseline at `at` (the join's).
inline bool Subscribe(Replica& r, uint64_t at, uint32_t epoch, ScopeMask scopes = kScopeAll,
                      uint32_t id = 1) {
  r.OnBaselineBegin(id, at, epoch, scopes);
  return r.OnBaselineEnd(id, at, kBaselineDigestSeed).ok;
}

// ---------------------------------------------------------------------------------------------
// A client on direct calls: the replica, a fake world it applies into, and the apply log.
// ---------------------------------------------------------------------------------------------

struct DirectClient {
  explicit DirectClient(std::span<const KindSpec> table, const Limits& limits = {})
      : replica(table, limits) {}

  Replica replica;
  std::map<RecordKey, Record> world;  // what the adapters wrote (a tomb removes nothing here)
  std::vector<std::pair<RecordKey, uint32_t>> apply_log;
  std::vector<ReplicaEvent> events;
  // An entity's spawn time; absent: there from the start. TimePoint::max(): never comes.
  std::map<RecordKey, TimePoint> spawn_at;
  // Applies of a key that fail before one goes (apply:fail nth=1).
  std::map<RecordKey, int> fail_next;
  size_t handed = 0;

  RowApply Apply(const Record& r, TimePoint now) {
    if (auto it = spawn_at.find(r.key); it != spawn_at.end() && now < it->second) {
      return RowApply::kWaiting;
    }
    if (auto it = fail_next.find(r.key); it != fail_next.end() && it->second > 0) {
      --it->second;
      return RowApply::kFailed;
    }
    world[r.key] = r;
    apply_log.emplace_back(r.key, r.revision);
    return RowApply::kApplied;
  }

  // Client::Pump's apply part: Service, then every ready row answered in the same pump.
  void Pump(TimePoint now) {
    replica.Service(now);
    Record r;
    while (replica.TakeReady(r)) {
      ++handed;
      replica.OnApplied(r.key, r.revision, Apply(r, now), now);
    }
    ReplicaEvent e;
    while (replica.PollEvent(e)) events.push_back(e);
  }

  // The position of a key's first apply in the log, or -1.
  int AppliedAt(const RecordKey& k, uint32_t revision = 0) const {
    for (size_t i = 0; i < apply_log.size(); ++i) {
      if (apply_log[i].first == k && (revision == 0 || apply_log[i].second == revision)) {
        return int(i);
      }
    }
    return -1;
  }
  size_t Count(ReplicaEvent::Type t) const {
    return size_t(std::ranges::count_if(events, [&](const ReplicaEvent& e) { return e.type == t; }));
  }
};

// One delta batch from S's walk: the items f took (at most `cap`) and the Through it proves.
struct Batch {
  std::vector<WalkItem> items;
  std::vector<Record> records;  // copies, as the wire carries them
  uint64_t proven = 0;
};
inline Batch WalkBatch(const WorldState& s, uint64_t after, Subscription& sub, size_t cap) {
  Batch b;
  s.Walk(
      after, sub,
      [&](const WalkItem& it) {
        if (b.items.size() >= cap) return false;
        b.items.push_back(it);
        b.records.push_back(it.record ? *it.record : Record{});
        if (it.type == EntryType::kEpoch) sub.scopes = ScopeMask(sub.scopes & ~kScopeLevel);
        return true;
      },
      &b.proven);
  return b;
}
// Hands one batch to a direct client, as its receive would: the deltas, then the Through.
inline void Deliver(DirectClient& c, const Batch& b, bool through = true) {
  for (size_t i = 0; i < b.items.size(); ++i) {
    const WalkItem& it = b.items[i];
    if (it.type == EntryType::kEpoch) {
      c.replica.OnEpoch(it.generation, it.lsn);
    } else {
      c.replica.OnRecord(b.records[i], it.first_lsn);
    }
  }
  if (through) c.replica.OnThrough(b.proven);
}

// ---------------------------------------------------------------------------------------------
// The message-level harness: S's per-peer send order over WorldState, clients over Replica.
// ---------------------------------------------------------------------------------------------

struct Down {
  enum class Type : uint8_t {
    kEpoch, kDelta, kThrough, kManifest, kBaselineBegin, kBaselineRow, kBaselineEnd,
  };
  Type type = Type::kDelta;
  Record rec;
  uint64_t first_lsn = 0;
  uint64_t lsn = 0;  // kEpoch, kThrough
  uint32_t generation = 0;
  WorldState::Manifest manifest;
  uint32_t id = 0;
  uint64_t at = 0;
  uint32_t epoch = 0;
  ScopeMask scopes = 0;
  uint64_t digest = 0;
};
struct Up {
  enum class Type : uint8_t { kAck, kMismatch };
  Type type = Type::kAck;
  uint64_t prefix = 0, manifest_ok = 0;
  uint64_t lsn = 0, mask = 0;  // kMismatch

  static Up Ack(uint64_t prefix, uint64_t manifest_ok) {
    Up u;
    u.prefix = prefix;
    u.manifest_ok = manifest_ok;
    return u;
  }
  static Up Mismatch(uint64_t lsn, uint64_t mask) {
    Up u;
    u.type = Type::kMismatch;
    u.lsn = lsn;
    u.mask = mask;
    return u;
  }
};

// Reliable and ordered, as the state lane: a message is delayed by the one-way time, plus one
// round trip per loss of its datagram below the lane; never reordered, never duplicated.
template <class M>
struct Channel {
  struct Item {
    TimePoint at;
    M m;
  };
  std::deque<Item> q;
  TimePoint last{};
  Duration one_way = 10ms;
  double loss = 0;
  Rng* rng = nullptr;

  void Send(M m, TimePoint now) {
    Duration extra{};
    while (rng && loss > 0 && rng->Chance(loss) && extra < 10s) extra += 2 * one_way + 20ms;
    TimePoint at = std::max(last, now + one_way + extra);
    last = at;
    q.push_back({at, std::move(m)});
  }
  bool Take(TimePoint now, M& out) {
    if (q.empty() || q.front().at > now) return false;
    out = std::move(q.front().m);
    q.pop_front();
    return true;
  }
  void Clear() {
    q.clear();
    last = {};
  }
};

enum class Why : uint8_t { kJoin, kLevel, kRepair };

struct ServerPeer {
  struct Base {
    uint32_t id = 0;
    uint64_t at = 0;
    ScopeMask scopes = 0;
    Why why = Why::kJoin;
    std::vector<RecordKey> keys;
    size_t next = 0;
    uint64_t digest = kBaselineDigestSeed;
  };
  bool present = false;
  bool level_ready = false;
  uint64_t cursor = 0;
  Subscription sub;
  std::optional<Base> base;
  std::deque<std::pair<Why, uint64_t>> reasons;  // why, and the LSN the reason is about
  uint32_t next_id = 1;
  TimePoint last_manifest{};
  TimePoint last_proof{};
  uint64_t manifest_ok = 0;
  // Counters the tests read.
  int baselines_begun = 0;
  int repairs_begun = 0;
  int mismatches = 0;
  int manifests_sent = 0;
  std::vector<std::pair<Why, uint64_t>> begun;  // why, at
  std::vector<TimePoint> manifest_times;
  // Faults: a message killed above the lane after it was "delivered" (S counts it sent).
  std::function<bool(const Down&)> kill;
  size_t batch_cap = 1000000;  // walk items per pump
  size_t rows_per_pump = 64;   // the pacer's baseline budget per pump
};

struct ClientPeer {
  ClientPeer(std::span<const KindSpec> table, const Limits& limits) : c(table, limits) {}
  DirectClient c;
  uint64_t last_through = 0;
  uint64_t last_covering_at = 0;  // the `at` of the last baseline that covered every held scope
  bool prefix_ok = true;          // the prefix never passed what a Through or a baseline proved
  int mismatches_sent = 0;
  int digest_failed = 0;
  int manifests_equal = 0;
  int manifests_not_comparable = 0;
  TimePoint last_ack{};
  uint64_t acked_prefix = 0, acked_ok = 0;
  std::function<bool(const Up&)> kill;
  std::vector<TimePoint> manifest_ok_times;
};

struct Harness {
  explicit Harness(std::span<const KindSpec> t, uint64_t seed = 1, Limits l = {})
      : table(t), limits(l), s(t, l), rng{seed} {
    for (size_t p = 0; p < kPeers; ++p) {
      down[p].rng = &rng;
      up[p].rng = &rng;
      cp[p] = std::make_unique<ClientPeer>(table, limits);
    }
  }

  static constexpr size_t kPeers = 5;
  std::span<const KindSpec> table;
  Limits limits;
  WorldState s;
  Rng rng;
  TimePoint now = kT0;
  std::array<ServerPeer, kPeers> sp;
  std::array<std::unique_ptr<ClientPeer>, kPeers> cp;
  std::array<Channel<Down>, kPeers> down;
  std::array<Channel<Up>, kPeers> up;

  void SetLink(uint16_t p, Duration one_way, double loss) {
    down[p].one_way = up[p].one_way = one_way;
    down[p].loss = up[p].loss = loss;
  }

  void Join(uint16_t p) {
    const uint32_t next = sp[p].next_id;
    sp[p] = ServerPeer{};
    sp[p].next_id = next;
    sp[p].present = true;
    sp[p].last_proof = now;
    sp[p].last_manifest = now;
    cp[p] = std::make_unique<ClientPeer>(table, limits);
    down[p].Clear();
    up[p].Clear();
    AddReason(p, Why::kJoin, s.Head());
  }
  void Leave(uint16_t p) {
    const uint32_t next = sp[p].next_id;
    sp[p] = ServerPeer{};
    sp[p].next_id = next;
    down[p].Clear();
    up[p].Clear();
    cp[p]->c.replica.Reset();
  }
  void LevelReady(uint16_t p) {
    sp[p].level_ready = true;
    AddReason(p, Why::kLevel, s.Head());
  }
  void NewGeneration(uint32_t g) {
    s.BeginEpoch(g);
    for (ServerPeer& q : sp) q.level_ready = false;
  }

  // BL5: a repair reason about an LSN the running baseline's `at` covers is answered by it.
  void AddReason(uint16_t p, Why why, uint64_t lsn) {
    ServerPeer& q = sp[p];
    if (why == Why::kRepair) {
      if (q.base && lsn <= q.base->at) return;
      for (const auto& r : q.reasons) {
        if (r.first == Why::kRepair) return;
      }
    }
    q.reasons.emplace_back(why, lsn);
  }

  void SendDown(uint16_t p, Down m) {
    if (sp[p].kill && sp[p].kill(m)) return;
    down[p].Send(std::move(m), now);
  }

  void Begin(uint16_t p, Why why) {
    ServerPeer& q = sp[p];
    ScopeMask scopes = why == Why::kJoin ? kScopeSession : why == Why::kLevel ? kScopeLevel : q.sub.scopes;
    if (why == Why::kLevel && !q.level_ready) return;
    if (scopes == 0) return;
    ServerPeer::Base b;
    b.id = q.next_id++;
    b.at = s.Head();
    b.scopes = scopes;
    b.why = why;
    b.keys = s.BaselineKeys(scopes);
    const bool covers = (scopes & q.sub.scopes) == q.sub.scopes;
    for (Scope sc : {Scope::kSession, Scope::kLevel}) {
      if (scopes & MaskOf(sc)) q.sub.since[size_t(sc)] = b.at;
    }
    q.sub.scopes = ScopeMask(q.sub.scopes | scopes);
    if (covers) q.cursor = std::max(q.cursor, b.at);
    ++q.baselines_begun;
    if (why == Why::kRepair) ++q.repairs_begun;
    q.begun.emplace_back(why, b.at);
    Down m;
    m.type = Down::Type::kBaselineBegin;
    m.id = b.id;
    m.at = b.at;
    m.epoch = s.Epoch();
    m.scopes = scopes;
    q.base = std::move(b);
    SendDown(p, m);
  }

  void PumpServer(uint16_t p) {
    ServerPeer& q = sp[p];
    if (!q.present) return;
    // Upstream first.
    Up u;
    while (up[p].Take(now, u)) {
      if (u.type == Up::Type::kAck) {
        if (u.manifest_ok > q.manifest_ok) {
          q.manifest_ok = u.manifest_ok;
          q.last_proof = now;
        }
      } else {
        ++q.mismatches;
        AddReason(p, Why::kRepair, u.lsn);
      }
    }
    // Step 1: a baseline is due.
    if (q.sub.scopes != 0 && now - q.last_proof >= limits.silence_baseline) {
      AddReason(p, Why::kRepair, s.Head());
      q.last_proof = now;
    }
    if (q.sub.scopes != 0 && q.cursor + 1 < s.Floor()) AddReason(p, Why::kRepair, s.Head());
    while (!q.base && !q.reasons.empty()) {
      const Why why = q.reasons.front().first;
      q.reasons.pop_front();
      Begin(p, why);
    }
    // Step 2: a running baseline's rows.
    if (q.base) {
      ServerPeer::Base& b = *q.base;
      size_t sent = 0;
      while (b.next < b.keys.size() && sent < q.rows_per_pump) {
        const Record* r = s.Get(b.keys[b.next++]);
        if (!r) continue;
        Down m;
        m.type = Down::Type::kBaselineRow;
        m.id = b.id;
        m.rec = *r;
        b.digest = BaselineDigestAdd(b.digest, *r);
        SendDown(p, m);
        ++sent;
      }
      if (b.next >= b.keys.size()) {
        Down end;
        end.type = Down::Type::kBaselineEnd;
        end.id = b.id;
        end.at = b.at;
        end.digest = b.digest;
        SendDown(p, end);
        q.base.reset();
        q.last_proof = now;
        Down th;
        th.type = Down::Type::kThrough;
        th.lsn = q.cursor;
        SendDown(p, th);
      }
    }
    // Step 3: deltas, then Through{proven}.
    if (q.cursor < s.Head()) {
      uint64_t proven = q.cursor;
      size_t taken = 0;
      s.Walk(
          q.cursor, q.sub,
          [&](const WalkItem& it) {
            if (taken >= q.batch_cap) return false;
            ++taken;
            Down m;
            if (it.type == EntryType::kEpoch) {
              m.type = Down::Type::kEpoch;
              m.generation = it.generation;
              m.lsn = it.lsn;
              // The level subscription ends at the epoch entry; a level baseline goes with it.
              q.sub.scopes = ScopeMask(q.sub.scopes & ~kScopeLevel);
              if (q.base && (q.base->scopes & kScopeLevel)) q.base.reset();
            } else {
              m.type = Down::Type::kDelta;
              m.rec = *it.record;
              m.first_lsn = it.first_lsn;
            }
            SendDown(p, m);
            return true;
          },
          &proven);
      if (proven > q.cursor) {
        q.cursor = proven;
        Down th;
        th.type = Down::Type::kThrough;
        th.lsn = proven;
        SendDown(p, th);
      }
    }
    // Step 4: a manifest, only at the head and with no baseline running.
    if (!q.base && q.sub.scopes != 0 && q.cursor == s.Head() &&
        now - q.last_manifest >= limits.manifest_interval) {
      Down m;
      m.type = Down::Type::kManifest;
      m.manifest = s.MakeManifest(q.sub.scopes);
      q.last_manifest = now;
      ++q.manifests_sent;
      q.manifest_times.push_back(now);
      SendDown(p, m);
    }
  }

  void SendUp(uint16_t p, Up m) {
    if (cp[p]->kill && cp[p]->kill(m)) return;
    up[p].Send(m, now);
  }

  void PumpClient(uint16_t p) {
    if (!sp[p].present) return;
    ClientPeer& c = *cp[p];
    Replica& r = c.c.replica;
    Down m;
    while (down[p].Take(now, m)) {
      switch (m.type) {
        case Down::Type::kEpoch: r.OnEpoch(m.generation, m.lsn); break;
        case Down::Type::kDelta: r.OnRecord(m.rec, m.first_lsn); break;
        case Down::Type::kThrough:
          c.last_through = std::max(c.last_through, m.lsn);
          r.OnThrough(m.lsn);
          break;
        case Down::Type::kManifest: {
          uint64_t mask = 0;
          const auto check = r.CheckManifest(m.manifest, &mask);
          if (check == Replica::Check::kMismatch) {
            ++c.mismatches_sent;
            SendUp(p, Up::Mismatch(m.manifest.lsn, mask));
          } else if (check == Replica::Check::kEqual) {
            ++c.manifests_equal;
            c.manifest_ok_times.push_back(now);
          } else {
            ++c.manifests_not_comparable;
          }
          break;
        }
        case Down::Type::kBaselineBegin: r.OnBaselineBegin(m.id, m.at, m.epoch, m.scopes); break;
        case Down::Type::kBaselineRow: r.OnBaselineRow(m.id, m.rec); break;
        case Down::Type::kBaselineEnd: {
          const ScopeMask held = r.Subscribed();
          const ScopeMask scopes = r.Staging();
          const BaselineEndResult res = r.OnBaselineEnd(m.id, m.at, m.digest);
          if (!res.ok) {
            ++c.digest_failed;
            SendUp(p, Up::Mismatch(m.at, 0));
          } else if ((scopes & held) == held) {
            c.last_covering_at = std::max(c.last_covering_at, m.at);
          }
          break;
        }
      }
    }
    c.c.Pump(now);
    if (r.Prefix() > std::max(c.last_through, c.last_covering_at)) c.prefix_ok = false;
    if (r.Prefix() != c.acked_prefix || r.ManifestOk() != c.acked_ok ||
        now - c.last_ack >= limits.obligation_report_interval) {
      c.acked_prefix = r.Prefix();
      c.acked_ok = r.ManifestOk();
      c.last_ack = now;
      SendUp(p, Up::Ack(c.acked_prefix, c.acked_ok));
    }
  }

  void Pump() {
    s.Service(now);
    for (uint16_t p = 0; p < kPeers; ++p) PumpServer(p);
    for (uint16_t p = 0; p < kPeers; ++p) PumpClient(p);
  }

  // Runs for `d` in steps of `step`; `each` runs before every pump (the script's writes).
  void Run(Duration d, Duration step = 20ms, const std::function<void()>& each = {}) {
    const TimePoint end = now + d;
    while (now < end) {
      if (each) each();
      Pump();
      now += step;
    }
  }

  bool QuietFor(uint16_t p) const {
    const ServerPeer& q = sp[p];
    const ClientPeer& c = *cp[p];
    return !q.base && q.reasons.empty() && q.cursor == s.Head() && down[p].q.empty() &&
           c.c.replica.Staging() == 0 && c.c.replica.Unsettled() == 0 &&
           c.c.replica.ManifestOk() == s.Head();
  }
  // Runs with no writes until every present peer is quiet (section 5's "quiet": a manifest at the
  // head matched), at most `max`. True if it got there.
  bool RunUntilQuiet(Duration max = 30s, Duration step = 20ms) {
    const TimePoint end = now + max;
    while (now < end) {
      Pump();
      now += step;
      bool all = true;
      for (uint16_t p = 0; p < kPeers; ++p) {
        if (sp[p].present && sp[p].sub.scopes != 0 && !QuietFor(p)) all = false;
      }
      if (all) return true;
    }
    return false;
  }

  // Section 5's "equal" on direct calls: the replicated rows of the peer's subscribed scopes on S
  // and in the replica, both ways, at the same revision, tomb and value; and every replica row
  // applied into the fake world at its revision.
  bool Equal(uint16_t p) const {
    const ScopeMask scopes = sp[p].sub.scopes;
    const Replica& r = cp[p]->c.replica;
    bool ok = true;
    s.ForEach([&](const Record& row) {
      const KindSpec* spec = FindKind(table, row.key.kind);
      if (!spec || !Replicated(*spec) || (scopes & MaskOf(spec->scope)) == 0) return;
      const Record* mine = r.Get(row.key);
      if (!mine || mine->revision != row.revision || mine->tomb != row.tomb ||
          mine->value != row.value) {
        ok = false;
      }
    });
    r.ForEach([&](const Record& row) {
      const Record* theirs = s.Get(row.key);
      if (!theirs) ok = false;
      auto w = cp[p]->c.world.find(row.key);
      if (w == cp[p]->c.world.end() || w->second.revision != row.revision) ok = false;
    });
    return ok;
  }
};

}  // namespace skylanders::backend::test::ws
