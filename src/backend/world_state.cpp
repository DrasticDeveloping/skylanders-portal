#include "backend/world_state.h"

#include <algorithm>
#include <deque>
#include <map>
#include <optional>
#include <set>
#include <tuple>
#include <utility>

namespace skylanders::backend {

namespace {

constexpr uint64_t kFnvOffset = 14695981039346656037ull;
constexpr uint64_t kFnvPrime = 1099511628211ull;

// What one row costs in Bytes() besides its value: its key's wire form (kind, entity, slot).
constexpr size_t kRowOverhead = 2 + kEntityIdWireBytes + 2;

uint64_t FnvByte(uint64_t h, uint8_t b) { return (h ^ b) * kFnvPrime; }

uint64_t FnvLe(uint64_t h, uint64_t v, int n) {
  for (int i = 0; i < n; ++i) h = FnvByte(h, uint8_t(v >> (8 * i)));
  return h;
}

// The key's wire layout (record.h EncodeKey): kind u16, entity (form u8, epoch u32, a, b, c u32,
// ordinal u16, incarnation u16), slot u16, all little-endian.
uint64_t FnvKey(uint64_t h, const RecordKey& k) {
  h = FnvLe(h, k.kind, 2);
  h = FnvLe(h, uint8_t(k.entity.form), 1);
  h = FnvLe(h, k.entity.epoch, 4);
  h = FnvLe(h, k.entity.a, 4);
  h = FnvLe(h, k.entity.b, 4);
  h = FnvLe(h, k.entity.c, 4);
  h = FnvLe(h, k.entity.ordinal, 2);
  h = FnvLe(h, k.entity.incarnation, 2);
  h = FnvLe(h, k.slot, 2);
  return h;
}

// One row's contribution to a manifest bucket: FNV-1a 64 over (key, revision, tomb, value digest),
// then a finalizer so that a sum of them mixes well. A bucket is the sum over its rows, so it does
// not depend on the order the rows are visited in (S's map and a replica's map agree).
uint64_t RowHash(const Record& r) {
  uint64_t h = BaselineDigestAdd(kFnvOffset, r);
  h ^= h >> 33;
  h *= 0xff51afd7ed558ccdull;
  h ^= h >> 33;
  return h;
}

size_t Bucket(const RecordKey& k) { return size_t(FnvKey(kFnvOffset, k) % kManifestBuckets); }

bool ValuesEqual(const KindSpec& spec, std::span<const uint8_t> a, std::span<const uint8_t> b) {
  if (spec.equal) return spec.equal(a, b);
  return std::ranges::equal(a, b);
}

// Dependency depth per kind (0: no dependencies) and the transitive dependencies of each kind. The
// validator refuses cycles (V4); a cycle here is cut, never followed forever.
struct KindGraph {
  std::map<KindId, int> depth;
  std::map<KindId, std::vector<KindId>> closure;

  explicit KindGraph(std::span<const KindSpec> table) {
    for (const KindSpec& k : table) {
      std::set<KindId> visiting;
      Depth(table, k.id, visiting);
      std::set<KindId> all;
      Collect(table, k.id, all);
      all.erase(k.id);
      closure[k.id] = std::vector<KindId>(all.begin(), all.end());
    }
  }

  int Depth(std::span<const KindSpec> table, KindId id, std::set<KindId>& visiting) {
    if (auto it = depth.find(id); it != depth.end()) return it->second;
    const KindSpec* spec = FindKind(table, id);
    if (!spec || !visiting.insert(id).second) return 0;
    int d = 0;
    for (KindId dep : spec->depends_on) d = std::max(d, Depth(table, dep, visiting) + 1);
    visiting.erase(id);
    depth[id] = d;
    return d;
  }

  static void Collect(std::span<const KindSpec> table, KindId id, std::set<KindId>& out) {
    const KindSpec* spec = FindKind(table, id);
    if (!spec) return;
    for (KindId dep : spec->depends_on) {
      if (out.insert(dep).second) Collect(table, dep, out);
    }
  }

  int DepthOf(KindId id) const {
    auto it = depth.find(id);
    return it == depth.end() ? 0 : it->second;
  }
  std::span<const KindId> DepsOf(KindId id) const {
    auto it = closure.find(id);
    if (it == closure.end()) return {};
    return it->second;
  }
};

// The first key of a kind in a map ordered by RecordKey (kind first, then the entity, whose
// default is the smallest).
RecordKey FirstKeyOf(KindId kind) {
  RecordKey k;
  k.kind = kind;
  return k;
}

}  // namespace

uint64_t BaselineDigestAdd(uint64_t digest, const Record& r) {
  uint64_t h = FnvKey(digest, r.key);
  h = FnvLe(h, r.revision, 4);
  h = FnvLe(h, r.tomb ? 1 : 0, 1);
  h = FnvLe(h, ValueDigest(r.value), 8);
  return h;
}

// ---------------------------------------------------------------------------------------------
// WorldState
// ---------------------------------------------------------------------------------------------

struct WorldState::Impl {
  struct LogEntry {
    uint64_t lsn = 0;
    EntryType type = EntryType::kRecord;
    bool dropped = false;  // kRecord: a level row an epoch change dropped; every walk passes it
    Scope scope = Scope::kSession;
    RecordKey key;            // kRecord
    uint32_t generation = 0;  // kEpoch
    uint64_t obligation = 0;  // kObligation
  };
  struct Row {
    Record rec;
    TimePoint logged_at{};  // the last logged change (the noisy interval counts from it)
  };

  std::span<const KindSpec> table;
  Limits limits;
  KindGraph graph;

  std::map<RecordKey, Row> rows;
  std::map<RecordKey, std::vector<uint8_t>> holds;  // noisy values kept aside (Opus F4)
  std::deque<LogEntry> log;                         // consecutive LSNs from floor
  uint64_t floor = 1;
  uint64_t next_lsn = 1;
  uint32_t epoch = 0;
  std::array<size_t, 2> rows_by_scope{};
  std::map<KindId, size_t> rows_by_kind;
  size_t bytes = 0;
  std::deque<LimitHit> limit_hits;  // for the server's kLimit events
  uint64_t limit_hits_dropped = 0;
  uint64_t writes = 0;  // every stored change, holds included (RM9)

  Impl(std::span<const KindSpec> t, const Limits& l) : table(t), limits(l), graph(t) {}

  Duration NoisyInterval() const {
    if (limits.noisy_hz == 0) return Duration::zero();
    return std::chrono::duration_cast<Duration>(std::chrono::nanoseconds(1'000'000'000ull) /
                                                limits.noisy_hz);
  }

  size_t RowLimit(Scope s) const {
    return s == Scope::kLevel ? limits.max_rows_per_level : limits.max_rows_per_session;
  }

  // Kind known (and not an effect: an effect takes no LSN and is never a row), size, epoch.
  PutResult Check(const RecordKey& key, size_t value_size, const KindSpec** out) const {
    const KindSpec* spec = FindKind(table, key.kind);
    if (!spec || spec->tier == Tier::kEffect) return PutResult::kUnknownKind;
    if (value_size > spec->max_value_bytes || value_size > kMaxValueBytes ||
        value_size > limits.max_value_bytes) {
      return PutResult::kTooBig;
    }
    // A level-form entity resolves only in its generation: an id of another one is stale.
    if (IsLevelForm(key.entity.form) && key.entity.epoch != epoch) return PutResult::kWrongEpoch;
    *out = spec;
    return PutResult::kWritten;
  }

  uint32_t EpochOf(Scope s) const { return s == Scope::kLevel ? epoch : 0; }

  void Hit(PutResult result, const RecordKey& key) {
    if (result != PutResult::kTooBig && result != PutResult::kRowLimit) return;
    if (limit_hits.size() >= 64) {
      ++limit_hits_dropped;
      return;
    }
    const KindSpec* spec = FindKind(table, key.kind);
    limit_hits.push_back(LimitHit{result, key.kind, spec ? spec->scope : Scope::kLevel});
  }

  void Append(LogEntry e) {
    e.lsn = next_lsn++;
    log.push_back(std::move(e));
    // The log's hard bound (LM10). The server trims to its slowest cursor itself; a peer whose
    // cursor falls below the floor is rebaselined (section 3.5 step 1), so dropping the oldest
    // entry here never loses a change, it only moves the floor.
    while (log.size() > limits.log_retention && !log.empty()) {
      log.pop_front();
      floor = log.empty() ? next_lsn : log.front().lsn;
    }
  }

  // Logs one change of `key` (a value, or a tomb when `tomb`). The caller has checked it.
  uint64_t Log(const KindSpec& spec, const RecordKey& key, std::span<const uint8_t> value,
               bool tomb, TimePoint now) {
    holds.erase(key);  // a logged write always drops the key's held value (Opus r2 N1)
    ++writes;
    auto [it, added] = rows.try_emplace(key);
    Row& row = it->second;
    if (added) {
      ++rows_by_scope[size_t(spec.scope)];
      ++rows_by_kind[key.kind];
      bytes += kRowOverhead;
    } else {
      bytes -= row.rec.value.size();
    }
    const uint32_t revision = added ? 1 : row.rec.revision + 1;
    row.rec.key = key;
    row.rec.revision = revision;
    row.rec.epoch = EpochOf(spec.scope);
    row.rec.writer = kServerWriter;
    row.rec.tomb = tomb;
    row.rec.value.assign(value.begin(), value.end());
    bytes += row.rec.value.size();
    row.logged_at = now;
    LogEntry e;
    e.type = EntryType::kRecord;
    e.scope = spec.scope;
    e.key = key;
    Append(std::move(e));
    row.rec.lsn = next_lsn - 1;
    return row.rec.lsn;
  }

  void DropRow(std::map<RecordKey, Row>::iterator it) {
    const KindSpec* spec = FindKind(table, it->first.kind);
    if (spec) --rows_by_scope[size_t(spec->scope)];
    if (auto k = rows_by_kind.find(it->first.kind); k != rows_by_kind.end() && k->second > 0) {
      --k->second;
    }
    bytes -= kRowOverhead + it->second.rec.value.size();
    rows.erase(it);
  }
};

WorldState::WorldState(std::span<const KindSpec> table, const Limits& limits)
    : impl_(std::make_unique<Impl>(table, limits)) {}
WorldState::~WorldState() = default;
WorldState::WorldState(WorldState&&) noexcept = default;
WorldState& WorldState::operator=(WorldState&&) noexcept = default;

PutResult WorldState::Put(const RecordKey& key, std::span<const uint8_t> value, TimePoint now) {
  Impl& s = *impl_;
  const KindSpec* spec = nullptr;
  if (PutResult r = s.Check(key, value.size(), &spec); r != PutResult::kWritten) {
    s.Hit(r, key);
    return r;
  }
  auto it = s.rows.find(key);
  if (it != s.rows.end() && !it->second.rec.tomb &&
      ValuesEqual(*spec, it->second.rec.value, value)) {
    // The newest value is the logged one: a held older value must not be logged over it.
    s.holds.erase(key);
    return PutResult::kUnchanged;
  }
  if (it == s.rows.end() && s.rows_by_scope[size_t(spec->scope)] >= s.RowLimit(spec->scope)) {
    s.Hit(PutResult::kRowLimit, key);
    return PutResult::kRowLimit;
  }
  // A noisy row within its interval: kept aside, the newest value winning; Service logs it.
  if (spec->noisy && it != s.rows.end() && now - it->second.logged_at < s.NoisyInterval()) {
    s.holds[key].assign(value.begin(), value.end());
    ++s.writes;
    return PutResult::kWritten;
  }
  s.Log(*spec, key, value, false, now);
  return PutResult::kWritten;
}

PutResult WorldState::Remove(const RecordKey& key, TimePoint now) {
  Impl& s = *impl_;
  const KindSpec* spec = nullptr;
  if (PutResult r = s.Check(key, 0, &spec); r != PutResult::kWritten) return r;
  s.holds.erase(key);  // (Opus r2 N1) even when nothing is logged: removal is the newest intent
  auto it = s.rows.find(key);
  if (it == s.rows.end() || it->second.rec.tomb) return PutResult::kUnchanged;
  s.Log(*spec, key, {}, true, now);
  return PutResult::kWritten;
}

WriteResult WorldState::ApplyAll(std::span<const Write> writes, TimePoint now) {
  Impl& s = *impl_;
  WriteResult out;
  if (writes.empty()) return out;
  // Check everything first (Sol F8): kind, size, epoch, one scope, then the row limits counting
  // the rows the set adds.
  std::vector<const KindSpec*> specs(writes.size(), nullptr);
  for (size_t i = 0; i < writes.size(); ++i) {
    const Write& w = writes[i];
    PutResult r = s.Check(w.key, w.remove ? 0 : w.value.size(), &specs[i]);
    if (r == PutResult::kWritten && specs[i]->scope != specs[0]->scope) r = PutResult::kMixedScope;
    if (r != PutResult::kWritten) {
      s.Hit(r, w.key);
      out.result = r;
      out.failed = i;
      return out;
    }
  }
  const Scope scope = specs[0]->scope;
  std::set<RecordKey> added;
  for (size_t i = 0; i < writes.size(); ++i) {
    const Write& w = writes[i];
    if (w.remove || s.rows.contains(w.key) || added.contains(w.key)) continue;
    added.insert(w.key);
    if (s.rows_by_scope[size_t(scope)] + added.size() > s.RowLimit(scope)) {
      s.Hit(PutResult::kRowLimit, w.key);
      out.result = PutResult::kRowLimit;
      out.failed = i;
      return out;
    }
  }
  // Then all, with consecutive LSNs (nothing else logs in between). A write that changes nothing
  // (an equal value, a remove of an absent or removed row) takes no LSN; when no write changed
  // anything, first_lsn and last_lsn stay 0.
  for (size_t i = 0; i < writes.size(); ++i) {
    const Write& w = writes[i];
    s.holds.erase(w.key);  // (Opus r2 N1)
    auto it = s.rows.find(w.key);
    uint64_t lsn = 0;
    if (w.remove) {
      if (it == s.rows.end() || it->second.rec.tomb) continue;
      lsn = s.Log(*specs[i], w.key, {}, true, now);
    } else {
      if (it != s.rows.end() && !it->second.rec.tomb &&
          ValuesEqual(*specs[i], it->second.rec.value, w.value)) {
        continue;
      }
      lsn = s.Log(*specs[i], w.key, w.value, false, now);
    }
    if (out.first_lsn == 0) out.first_lsn = lsn;
    out.last_lsn = lsn;
  }
  return out;
}

void WorldState::Service(TimePoint now) {
  Impl& s = *impl_;
  const Duration interval = s.NoisyInterval();
  for (auto it = s.holds.begin(); it != s.holds.end();) {
    auto row = s.rows.find(it->first);
    const KindSpec* spec = FindKind(s.table, it->first.kind);
    if (row == s.rows.end() || !spec) {
      it = s.holds.erase(it);
      continue;
    }
    if (now - row->second.logged_at < interval) {
      ++it;
      continue;
    }
    std::vector<uint8_t> value = std::move(it->second);
    it = s.holds.erase(it);
    if (!row->second.rec.tomb && ValuesEqual(*spec, row->second.rec.value, value)) continue;
    s.Log(*spec, row->first, value, false, now);  // erases nothing more: the hold is gone already
  }
}

const Record* WorldState::Get(const RecordKey& key) const {
  auto it = impl_->rows.find(key);
  return it == impl_->rows.end() ? nullptr : &it->second.rec;
}

void WorldState::BeginEpoch(uint32_t generation) {
  Impl& s = *impl_;
  for (auto it = s.rows.begin(); it != s.rows.end();) {
    const KindSpec* spec = FindKind(s.table, it->first.kind);
    if (spec && spec->scope == Scope::kLevel) {
      auto next = std::next(it);
      s.DropRow(it);
      it = next;
    } else {
      ++it;
    }
  }
  for (auto it = s.holds.begin(); it != s.holds.end();) {
    const KindSpec* spec = FindKind(s.table, it->first.kind);
    it = (!spec || spec->scope == Scope::kLevel) ? s.holds.erase(it) : std::next(it);
  }
  for (Impl::LogEntry& e : s.log) {
    if (e.type == EntryType::kRecord && e.scope == Scope::kLevel) e.dropped = true;
  }
  s.epoch = generation;
  Impl::LogEntry e;
  e.type = EntryType::kEpoch;
  e.generation = generation;
  s.Append(std::move(e));
}

uint64_t WorldState::LogObligation(uint64_t obligation_id) {
  Impl& s = *impl_;
  Impl::LogEntry e;
  e.type = EntryType::kObligation;
  e.obligation = obligation_id;
  s.Append(std::move(e));
  return s.next_lsn - 1;
}

uint32_t WorldState::Epoch() const { return impl_->epoch; }
uint64_t WorldState::Head() const { return impl_->next_lsn - 1; }
uint64_t WorldState::Floor() const { return impl_->log.empty() ? impl_->next_lsn : impl_->floor; }

void WorldState::Walk(uint64_t after, const Subscription& sub,
                      const std::function<bool(const WalkItem&)>& f, uint64_t* proven) const {
  const Impl& s = *impl_;
  uint64_t x = after;
  auto done = [&] {
    if (proven) *proven = x;
  };
  if (after >= Head()) {
    done();
    return;
  }
  // Entries in (after, Floor()) were trimmed: nothing can be proven from here (the server
  // rebaselines a cursor below the floor, section 3.5 step 1).
  if (after + 1 < Floor()) {
    done();
    return;
  }
  std::set<RecordKey> handed;
  for (size_t i = size_t(after + 1 - s.floor); i < s.log.size(); ++i) {
    const Impl::LogEntry& e = s.log[i];
    if (e.type == EntryType::kEpoch) {
      WalkItem item;
      item.type = EntryType::kEpoch;
      item.lsn = e.lsn;
      item.first_lsn = e.lsn;
      item.generation = e.generation;
      if (!f(item)) break;
    } else if (e.type == EntryType::kRecord && !e.dropped &&
               (sub.scopes & MaskOf(e.scope)) != 0 && e.lsn > sub.since[size_t(e.scope)] &&
               !handed.contains(e.key)) {
      const KindSpec* spec = FindKind(s.table, e.key.kind);
      auto row = s.rows.find(e.key);
      if (spec && Replicated(*spec) && row != s.rows.end()) {
        WalkItem item;
        item.type = EntryType::kRecord;
        item.lsn = row->second.rec.lsn;
        item.first_lsn = e.lsn;
        item.record = &row->second.rec;
        if (!f(item)) break;
        handed.insert(e.key);
      }
    }
    // Obligations, unsubscribed scopes, changes at or below their scope's since, non-replicated
    // kinds, dropped rows and later changes of a key already handed (at its latest revision) are
    // passed.
    x = e.lsn;
  }
  done();
}

void WorldState::TrimTo(uint64_t floor) {
  Impl& s = *impl_;
  while (!s.log.empty() && s.log.front().lsn < floor) s.log.pop_front();
  s.floor = s.log.empty() ? s.next_lsn : s.log.front().lsn;
}

WorldState::Manifest WorldState::MakeManifest(ScopeMask scopes) const {
  const Impl& s = *impl_;
  Manifest m;
  m.lsn = Head();
  m.scopes = scopes;
  for (const auto& [key, row] : s.rows) {
    const KindSpec* spec = FindKind(s.table, key.kind);
    if (!spec || !Replicated(*spec) || (scopes & MaskOf(spec->scope)) == 0) continue;
    m.digest[Bucket(key)] += RowHash(row.rec);
  }
  return m;
}

std::vector<RecordKey> WorldState::BaselineKeys(ScopeMask scopes) const {
  const Impl& s = *impl_;
  std::vector<std::pair<int, RecordKey>> keyed;
  for (const auto& [key, row] : s.rows) {
    const KindSpec* spec = FindKind(s.table, key.kind);
    if (!spec || !Replicated(*spec) || (scopes & MaskOf(spec->scope)) == 0) continue;
    keyed.emplace_back(s.graph.DepthOf(key.kind), key);
  }
  std::ranges::sort(keyed);
  std::vector<RecordKey> out;
  out.reserve(keyed.size());
  for (auto& [depth, key] : keyed) out.push_back(key);
  return out;
}

size_t WorldState::Rows() const { return impl_->rows.size(); }
size_t WorldState::Rows(Scope scope) const { return impl_->rows_by_scope[size_t(scope)]; }
size_t WorldState::Rows(KindId kind) const {
  auto it = impl_->rows_by_kind.find(kind);
  return it == impl_->rows_by_kind.end() ? 0 : it->second;
}
size_t WorldState::Bytes() const { return impl_->bytes; }
size_t WorldState::LogEntries() const { return impl_->log.size(); }
std::span<const KindSpec> WorldState::Table() const { return impl_->table; }

bool WorldState::PollLimit(LimitHit& out) {
  Impl& s = *impl_;
  if (s.limit_hits.empty()) return false;
  out = s.limit_hits.front();
  s.limit_hits.pop_front();
  return true;
}

uint64_t WorldState::LimitHitsDropped() const { return impl_->limit_hits_dropped; }

uint64_t WorldState::Writes() const { return impl_->writes; }

void WorldState::ForEach(const std::function<void(const Record&)>& f) const {
  for (const auto& [key, row] : impl_->rows) f(row.rec);
}

// ---------------------------------------------------------------------------------------------
// Replica
// ---------------------------------------------------------------------------------------------

struct Replica::Impl {
  enum class QState : uint8_t { kHeld, kReady, kOut, kWaiting, kFailed };
  // One unsettled key on the apply queue (draft 4): from the moment it is queued until OnApplied
  // answers kApplied or kDropped at its revision.
  struct Entry {
    Record rec;              // the newest received revision
    uint64_t first_lsn = 0;  // the smallest first change while unsettled (Sol r2 N1)
    QState state = QState::kReady;
    bool from_baseline = false;  // queued by a baseline swap: ordered by key, not by LSN
    bool late = false;           // past its grace: the fence no longer waits for it
    bool has_grace = false;      // the grace clock runs from the first kWaiting or kFailed
    TimePoint grace_start{};
    RowApply last = RowApply::kWaiting;
    bool due = false;  // waiting or failed, handed out again at the next TakeReady
  };
  // The order released rows go to the adapters: dependency order, then LSN order (a baseline's
  // rows: key order, ahead of the live ones of the same depth).
  using ReadyKey = std::tuple<int, int, uint64_t, RecordKey>;

  struct Staging {
    uint32_t id = 0;
    uint64_t at = 0;
    uint32_t epoch = 0;
    ScopeMask scopes = 0;
    uint64_t digest = kBaselineDigestSeed;
    std::map<RecordKey, std::pair<Record, uint64_t>> rows;  // record, first_lsn
  };

  std::span<const KindSpec> table;
  Limits limits;
  KindGraph graph;

  std::map<RecordKey, Record> rows;
  std::map<RecordKey, Entry> queue;
  std::set<ReadyKey> ready;
  std::set<RecordKey> held;
  std::deque<RecordKey> due;
  std::optional<Staging> staging;
  // Rows of a newer epoch that came before its EpochBegin (VR3), waiting for that marker (F4):
  // one entry per key, the newest revision with the smallest first_lsn (as an unsettled entry
  // keeps it, Sol r2 N1), and one epoch only, the newest seen. The lane is ordered, so a row of
  // generation g proves that every marker below g has passed: rows of an older newer-epoch can
  // never be replayed (their marker is gone, and a later marker drops them) and go when a row of
  // a later generation comes. At most limits.max_rows_per_level keys: S holds no more level rows
  // (tombs counted) in one epoch, so a key past it is a defect in S, dropped and counted.
  uint32_t future_epoch = 0;
  std::map<RecordKey, std::pair<Record, uint64_t>> future;
  size_t future_bytes = 0;
  uint64_t future_dropped = 0;
  std::deque<ReplicaEvent> events;
  uint64_t prefix = 0;
  uint64_t manifest_ok = 0;
  uint32_t epoch = 0;
  ScopeMask subscribed = 0;
  size_t bytes = 0;

  Impl(std::span<const KindSpec> t, const Limits& l) : table(t), limits(l), graph(t) {}

  ReadyKey KeyOf(const Entry& e) const {
    return {graph.DepthOf(e.rec.key.kind), e.from_baseline ? 0 : 1,
            e.from_baseline ? 0 : e.rec.lsn, e.rec.key};
  }

  void Leave(Entry& e) {
    if (e.state == QState::kReady) ready.erase(KeyOf(e));
    if (e.state == QState::kHeld) held.erase(e.rec.key);
  }
  void Enter(Entry& e, QState st) {
    e.state = st;
    e.due = false;
    if (st == QState::kReady) ready.insert(KeyOf(e));
    if (st == QState::kHeld) held.insert(e.rec.key);
  }
  void Erase(std::map<RecordKey, Entry>::iterator it) {
    Leave(it->second);
    queue.erase(it);
  }

  uint32_t GraceMs(KindId kind) const {
    const KindSpec* spec = FindKind(table, kind);
    if (spec && spec->pending_grace_ms != 0) return spec->pending_grace_ms;
    return limits.pending_grace_ms;
  }

  void SetRow(const Record& r) {
    auto [it, added] = rows.try_emplace(r.key);
    if (added) {
      bytes += kRowOverhead;
    } else {
      bytes -= it->second.value.size();
    }
    it->second = r;
    bytes += r.value.size();
  }
  void DropRow(std::map<RecordKey, Record>::iterator it) {
    bytes -= kRowOverhead + it->second.value.size();
    rows.erase(it);
  }

  // Onto the apply queue: held for a fenced kind, ready otherwise. A row replacing an unsettled
  // entry keeps the smaller first_lsn and the entry's grace clock (and so its lateness) until the
  // entry settles (Sol r2 N1).
  void Enqueue(const Record& r, uint64_t first_lsn, bool from_baseline) {
    const KindSpec* spec = FindKind(table, r.key.kind);
    const bool fenced = spec && Fenced(*spec);
    auto [it, added] = queue.try_emplace(r.key);
    Entry& e = it->second;
    if (!added) {
      Leave(e);
      e.first_lsn = std::min(e.first_lsn, first_lsn);
    } else {
      e.first_lsn = first_lsn;
    }
    e.rec = r;
    e.from_baseline = from_baseline;
    Enter(e, fenced ? QState::kHeld : QState::kReady);
  }

  // Releases every held row that is now releasable: the prefix covers its LSN and no unsettled,
  // not late row of a kind it depends on (transitively) has a first change at or below its LSN.
  // A release past a late dependency raises kFenceBroken for the pair (draft 4).
  void TryRelease() {
    std::vector<RecordKey> candidates(held.begin(), held.end());
    for (const RecordKey& key : candidates) {
      auto it = queue.find(key);
      if (it == queue.end() || it->second.state != QState::kHeld) continue;
      Entry& e = it->second;
      if (prefix < e.rec.lsn) continue;
      bool blocked = false;
      std::vector<RecordKey> broken;
      for (KindId dep : graph.DepsOf(key.kind)) {
        for (auto u = queue.lower_bound(FirstKeyOf(dep)); u != queue.end() && u->first.kind == dep;
             ++u) {
          if (u->second.first_lsn > e.rec.lsn) continue;
          if (u->second.late) {
            broken.push_back(u->first);
          } else {
            blocked = true;
            break;
          }
        }
        if (blocked) break;
      }
      if (blocked) continue;
      Leave(e);
      Enter(e, QState::kReady);
      for (const RecordKey& dep : broken) {
        ReplicaEvent ev;
        ev.type = ReplicaEvent::Type::kFenceBroken;
        ev.row = key;
        ev.dependency = dep;
        ev.last = queue.at(dep).last;
        events.push_back(ev);
      }
    }
  }

  // The level scope ends: its rows, its queue entries (no late event: their epoch ended), its
  // subscription, and a staging map that covers it (BL6).
  void ChangeEpoch(uint32_t generation) {
    epoch = generation;
    for (auto it = rows.begin(); it != rows.end();) {
      const KindSpec* spec = FindKind(table, it->first.kind);
      auto next = std::next(it);
      if (!spec || spec->scope == Scope::kLevel) DropRow(it);
      it = next;
    }
    for (auto it = queue.begin(); it != queue.end();) {
      const KindSpec* spec = FindKind(table, it->first.kind);
      auto next = std::next(it);
      if (!spec || spec->scope == Scope::kLevel) Erase(it);
      it = next;
    }
    subscribed = ScopeMask(subscribed & ~kScopeLevel);
    if (staging && (staging->scopes & kScopeLevel) != 0) staging.reset();
    // The waiting rows of this generation now go in (in key order: the apply queue orders them
    // by dependency, then LSN); a later generation's keep waiting; an older one's are dead.
    if (future_epoch <= generation) {
      std::map<RecordKey, std::pair<Record, uint64_t>> now;
      if (future_epoch == generation) now = std::move(future);
      ClearFuture();
      for (auto& [key, entry] : now) Receive(entry.first, entry.second);
    }
    TryRelease();
  }

  void ClearFuture() {
    future.clear();
    future_bytes = 0;
    future_epoch = 0;
  }

  // A row of a newer epoch than the replica's, before its marker (F4; see `future`).
  ApplyResult Wait(const Record& r, uint64_t first_lsn) {
    if (r.epoch < future_epoch) return ApplyResult::kWrongEpoch;  // its marker can no longer come
    if (r.epoch > future_epoch) {
      ClearFuture();
      future_epoch = r.epoch;
    }
    auto it = future.find(r.key);
    if (it == future.end()) {
      if (future.size() >= limits.max_rows_per_level) {
        ++future_dropped;
        return ApplyResult::kWrongEpoch;
      }
      future.emplace(r.key, std::make_pair(r, first_lsn));
      future_bytes += kRowOverhead + r.value.size();
      return ApplyResult::kStaged;
    }
    auto& [stored, first] = it->second;
    first = std::min(first, first_lsn);
    if (!Newer(r, &stored, r.epoch)) return ApplyResult::kStale;
    future_bytes = future_bytes - stored.value.size() + r.value.size();
    stored = r;
    return ApplyResult::kStaged;
  }

  void Stage(const Record& r, uint64_t first_lsn) {
    auto [it, added] = staging->rows.try_emplace(r.key, r, first_lsn);
    if (added) return;
    auto& [stored, first] = it->second;
    first = std::min(first, first_lsn);
    if (Newer(r, &stored, r.epoch)) stored = r;
  }

  ApplyResult Receive(const Record& r, uint64_t first_lsn) {
    const KindSpec* spec = FindKind(table, r.key.kind);
    if (!spec || !Replicated(*spec)) return ApplyResult::kUnknownKind;
    if (r.value.size() > spec->max_value_bytes || r.value.size() > kMaxValueBytes) {
      return ApplyResult::kTooBig;
    }
    if (spec->scope == Scope::kLevel) {
      if (r.epoch < epoch) return ApplyResult::kWrongEpoch;
      if (r.epoch > epoch) return Wait(r, first_lsn);
    } else if (r.epoch != 0) {
      return ApplyResult::kWrongEpoch;
    }
    if (staging && (staging->scopes & MaskOf(spec->scope)) != 0) {
      Stage(r, first_lsn);
      return ApplyResult::kStaged;
    }
    auto stored = rows.find(r.key);
    if (!Newer(r, stored == rows.end() ? nullptr : &stored->second, r.epoch)) {
      return ApplyResult::kStale;
    }
    SetRow(r);
    Enqueue(r, first_lsn, false);
    if (Fenced(*spec)) {
      TryRelease();
      return queue.at(r.key).state == QState::kHeld ? ApplyResult::kHeld : ApplyResult::kApplied;
    }
    return ApplyResult::kApplied;
  }

  std::array<uint64_t, kManifestBuckets> Digests(ScopeMask scopes) const {
    std::array<uint64_t, kManifestBuckets> d{};
    for (const auto& [key, r] : rows) {
      const KindSpec* spec = FindKind(table, key.kind);
      if (!spec || (scopes & MaskOf(spec->scope)) == 0) continue;
      d[Bucket(key)] += RowHash(r);
    }
    return d;
  }
};

Replica::Replica(std::span<const KindSpec> table, const Limits& limits)
    : impl_(std::make_unique<Impl>(table, limits)) {}
Replica::~Replica() = default;
Replica::Replica(Replica&&) noexcept = default;
Replica& Replica::operator=(Replica&&) noexcept = default;

ApplyResult Replica::OnRecord(const Record& r, uint64_t first_lsn) {
  return impl_->Receive(r, first_lsn);
}

void Replica::OnThrough(uint64_t lsn) {
  Impl& s = *impl_;
  if (lsn <= s.prefix) return;
  s.prefix = lsn;
  s.TryRelease();
}

void Replica::OnEpoch(uint32_t generation, uint64_t /*lsn*/) {
  // The marker's LSN proves nothing by itself (only Through moves the prefix); the generation is
  // what the replica changes on. A repeated or older marker changes nothing.
  Impl& s = *impl_;
  if (generation <= s.epoch) return;
  s.ChangeEpoch(generation);
}

void Replica::OnBaselineBegin(uint32_t id, uint64_t at, uint32_t epoch, ScopeMask scopes) {
  Impl& s = *impl_;
  s.staging.reset();
  // A baseline of a later generation than the replica knows (its EpochBegin still behind it, or
  // lost): the generation changes first, so the staged level rows are of the right one.
  if (epoch > s.epoch) s.ChangeEpoch(epoch);
  Impl::Staging st;
  st.id = id;
  st.at = at;
  st.epoch = epoch;
  st.scopes = scopes;
  s.staging = std::move(st);
}

void Replica::OnBaselineRow(uint32_t id, const Record& r) {
  Impl& s = *impl_;
  if (!s.staging || s.staging->id != id) return;
  // The digest covers every row as received, whatever the replica then does with it (Sol F6).
  s.staging->digest = BaselineDigestAdd(s.staging->digest, r);
  const KindSpec* spec = FindKind(s.table, r.key.kind);
  if (!spec || !Replicated(*spec) || (s.staging->scopes & MaskOf(spec->scope)) == 0) return;
  if (r.value.size() > spec->max_value_bytes || r.value.size() > kMaxValueBytes) return;
  if (r.epoch != (spec->scope == Scope::kLevel ? s.epoch : 0)) return;
  // A baseline row's first change is its own LSN (section 3.5, "Replacing an unsettled entry").
  s.Stage(r, r.lsn);
}

BaselineEndResult Replica::OnBaselineEnd(uint32_t id, uint64_t at, uint64_t digest) {
  Impl& s = *impl_;
  BaselineEndResult out;
  if (!s.staging || s.staging->id != id || s.staging->digest != digest) {
    // A failed check (or an end whose begin never came): staging goes, the replica and the apply
    // queue stay as they were, and the client asks for a repair (Mismatch{at, 0}; Sol F6).
    s.staging.reset();
    return out;
  }
  Impl::Staging st = std::move(*s.staging);
  s.staging.reset();
  out.ok = true;
  // Rows of the baselined scopes the baseline did not carry go, with their queue entries.
  for (auto it = s.rows.begin(); it != s.rows.end();) {
    const KindSpec* spec = FindKind(s.table, it->first.kind);
    auto next = std::next(it);
    if (spec && (st.scopes & MaskOf(spec->scope)) != 0 && !st.rows.contains(it->first)) {
      ++out.diff.removed;
      if (auto q = s.queue.find(it->first); q != s.queue.end()) s.Erase(q);
      s.DropRow(it);
    }
    it = next;
  }
  // Every staged row replaces its key and goes on the apply queue, in dependency order, then key
  // order (Enqueue keeps an unsettled entry's smaller first_lsn and its grace clock).
  std::vector<std::pair<int, RecordKey>> order;
  order.reserve(st.rows.size());
  for (const auto& [key, v] : st.rows) order.emplace_back(s.graph.DepthOf(key.kind), key);
  std::ranges::sort(order);
  for (const auto& [depth, key] : order) {
    const auto& [r, first] = st.rows.at(key);
    auto old = s.rows.find(key);
    if (old == s.rows.end()) {
      ++out.diff.added;
    } else if (old->second.revision != r.revision || old->second.tomb != r.tomb) {
      ++out.diff.changed;
    } else {
      ++out.diff.unchanged;
    }
    s.SetRow(r);
    s.Enqueue(r, first, true);
  }
  // A baseline that covers every scope the peer holds (the join's, a repair) proves `at`.
  const ScopeMask before = s.subscribed;
  s.subscribed = ScopeMask(s.subscribed | st.scopes);
  if ((st.scopes & before) == before) s.prefix = std::max(s.prefix, at);
  s.TryRelease();
  return out;
}

bool Replica::TakeReady(Record& out) {
  Impl& s = *impl_;
  if (!s.ready.empty()) {
    const RecordKey key = std::get<3>(*s.ready.begin());
    Impl::Entry& e = s.queue.at(key);
    s.Leave(e);
    s.Enter(e, Impl::QState::kOut);
    out = e.rec;
    return true;
  }
  while (!s.due.empty()) {
    const RecordKey key = s.due.front();
    s.due.pop_front();
    auto it = s.queue.find(key);
    if (it == s.queue.end() || !it->second.due) continue;
    Impl::Entry& e = it->second;
    if (e.state != Impl::QState::kWaiting && e.state != Impl::QState::kFailed) continue;
    s.Enter(e, Impl::QState::kOut);
    out = e.rec;
    return true;
  }
  return false;
}

void Replica::OnApplied(const RecordKey& key, uint32_t revision, RowApply result, TimePoint now) {
  Impl& s = *impl_;
  auto it = s.queue.find(key);
  if (it == s.queue.end()) return;
  Impl::Entry& e = it->second;
  // Only an answer to the revision handed out counts; a replaced older revision settles nothing.
  if (e.state != Impl::QState::kOut || e.rec.revision != revision) return;
  switch (result) {
    case RowApply::kApplied:
    case RowApply::kDropped:
      s.Erase(it);
      s.TryRelease();
      return;
    case RowApply::kWaiting:
    case RowApply::kFailed:
      e.last = result;
      if (!e.has_grace) {
        e.has_grace = true;
        e.grace_start = now;
      }
      s.Enter(e, result == RowApply::kWaiting ? Impl::QState::kWaiting : Impl::QState::kFailed);
      return;
  }
}

void Replica::Service(TimePoint now) {
  Impl& s = *impl_;
  bool any_late = false;
  for (auto& [key, e] : s.queue) {
    if (e.state != Impl::QState::kWaiting && e.state != Impl::QState::kFailed) continue;
    if (e.has_grace && !e.late &&
        now - e.grace_start >= std::chrono::milliseconds(s.GraceMs(key.kind))) {
      e.late = true;
      any_late = true;
      ReplicaEvent ev;
      ev.type = ReplicaEvent::Type::kRowLate;
      ev.row = key;
      ev.last = e.last;
      s.events.push_back(ev);
    }
    if (!e.due) {
      e.due = true;
      s.due.push_back(key);
    }
  }
  if (any_late) s.TryRelease();
}

bool Replica::PollEvent(ReplicaEvent& out) {
  Impl& s = *impl_;
  if (s.events.empty()) return false;
  out = s.events.front();
  s.events.pop_front();
  return true;
}

ScopeMask Replica::Subscribed() const { return impl_->subscribed; }
ScopeMask Replica::Staging() const { return impl_->staging ? impl_->staging->scopes : 0; }

bool Replica::Completes(Scope scope, uint64_t at_lsn) const {
  const Impl& s = *impl_;
  const ScopeMask m = MaskOf(scope);
  if ((s.subscribed & m) == 0 || (Staging() & m) != 0 || s.prefix < at_lsn) return false;
  for (const RecordKey& key : s.held) {
    if (s.queue.at(key).first_lsn <= at_lsn) return false;
  }
  return true;
}

size_t Replica::Unsettled() const { return impl_->queue.size(); }

size_t Replica::HandedOut() const {
  size_t n = 0;
  for (const auto& [key, e] : impl_->queue) n += e.state == Impl::QState::kOut ? 1 : 0;
  return n;
}

Replica::Check Replica::CheckManifest(const WorldState::Manifest& m, uint64_t* bucket_mask) {
  Impl& s = *impl_;
  if (bucket_mask) *bucket_mask = 0;
  if (s.prefix != m.lsn || Staging() != 0) return Check::kNotComparable;
  const auto mine = s.Digests(m.scopes);
  uint64_t mask = 0;
  for (size_t i = 0; i < kManifestBuckets; ++i) {
    if (mine[i] != m.digest[i]) mask |= uint64_t(1) << i;
  }
  if (bucket_mask) *bucket_mask = mask;
  if (mask != 0) return Check::kMismatch;
  s.manifest_ok = m.lsn;
  return Check::kEqual;
}

uint64_t Replica::Prefix() const { return impl_->prefix; }
uint64_t Replica::ManifestOk() const { return impl_->manifest_ok; }

const Record* Replica::Get(const RecordKey& key) const {
  auto it = impl_->rows.find(key);
  return it == impl_->rows.end() ? nullptr : &it->second;
}

size_t Replica::Rows() const { return impl_->rows.size(); }
size_t Replica::Bytes() const { return impl_->bytes; }
size_t Replica::FutureRows() const { return impl_->future.size(); }
size_t Replica::FutureBytes() const { return impl_->future_bytes; }
uint32_t Replica::FutureEpoch() const { return impl_->future_epoch; }
uint64_t Replica::FutureDropped() const { return impl_->future_dropped; }
uint32_t Replica::Epoch() const { return impl_->epoch; }

std::optional<uint64_t> Replica::FirstLsn(const RecordKey& key) const {
  auto it = impl_->queue.find(key);
  if (it == impl_->queue.end()) return std::nullopt;
  return it->second.first_lsn;
}

void Replica::ForEach(const std::function<void(const Record&)>& f) const {
  for (const auto& [key, r] : impl_->rows) f(r);
}

void Replica::Reset() {
  Impl& s = *impl_;
  s.rows.clear();
  s.queue.clear();
  s.ready.clear();
  s.held.clear();
  s.due.clear();
  s.staging.reset();
  s.ClearFuture();
  s.future_dropped = 0;
  s.events.clear();
  s.prefix = 0;
  s.manifest_ok = 0;
  s.epoch = 0;
  s.subscribed = 0;
  s.bytes = 0;
}

std::span<const KindSpec> Replica::Table() const { return impl_->table; }

}  // namespace skylanders::backend
