#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <vector>

#include "backend/base.h"
#include "backend/kind_table.h"
#include "backend/limits.h"
#include "backend/record.h"

// World state, the change log and baselines (design section 3.5). WorldState is S's rows and the
// one change log with a cursor per peer; Replica is a client's copy with staging, the proven
// prefix, the fence and its apply feedback (OnApplied). How S sends to one peer every pump
// (baseline due, running baseline, deltas and Through, manifest, obligations) is server.h's; this
// header holds the parts it walks.

namespace skylanders::backend {

// The log's entries (Opus F3). Every entry takes the next LSN; an effect takes none (it is never in
// the log, a baseline or a manifest). A record entry whose row an epoch change dropped is passed by
// every walk, as an obligation is.
enum class EntryType : uint8_t {
  kRecord,      // a Put, a Remove (a tomb), one write of an ApplyAll
  kEpoch,       // BeginEpoch(g): reaches every peer as EpochBegin{g, lsn}, in its place
  kObligation,  // Oblige: passed by every walk; S sends it to its recipient alone (section 3.5 step 5)
};

// What one peer is subscribed to (D5; Opus F2): the scopes, and per scope (indexed by Scope) the
// LSN at or below which that scope's changes are already in the peer's baseline of it (the
// baseline's `at`).
struct Subscription {
  ScopeMask scopes = 0;
  std::array<uint64_t, 2> since{};
};

struct WalkItem {
  EntryType type = EntryType::kRecord;  // kRecord or kEpoch (obligations are never handed out)
  uint64_t lsn = 0;                     // the record's latest change, or the epoch entry's own LSN
  uint64_t first_lsn = 0;               // the key's first change after the walk's start; the epoch
                                        // entry's own LSN
  const Record* record = nullptr;       // kRecord
  uint32_t generation = 0;              // kEpoch
};

// One write of an ApplyAll (a verdict's writes, section 3.9).
struct Write {
  RecordKey key;
  std::vector<uint8_t> value;
  bool remove = false;
};
struct WriteResult {
  PutResult result = PutResult::kWritten;
  size_t failed = 0;  // not kWritten: the index of the first write that would not go
  uint64_t first_lsn = 0, last_lsn = 0;
};

// The baseline digest (Sol F6, Opus F14): FNV-1a 64, from kBaselineDigestSeed, continued over each
// BaselineRows record in send order: its key in the wire layout (kind u16, entity form u8, epoch,
// a, b, c u32, ordinal, incarnation u16, slot u16), revision (u32), tomb (u8), ValueDigest of the
// value (u64), all little-endian. S folds each row in as it sends it, the Replica as it receives it
// (OnBaselineRow), so both sides compute it the same way. Not named by the design; the header's own.
constexpr uint64_t kBaselineDigestSeed = 14695981039346656037ull;
uint64_t BaselineDigestAdd(uint64_t digest, const Record& r);

// S's rows and the log.
class WorldState {
 public:
  WorldState(std::span<const KindSpec> table, const Limits& limits);
  ~WorldState();
  WorldState(WorldState&&) noexcept;
  WorldState& operator=(WorldState&&) noexcept;

  // A noisy kind's write that comes within 1/noisy_hz of its row's last logged change is kept
  // aside, the newest value winning, and logged by Service (Opus F4). Get, the walk, baselines and
  // manifests see only logged rows, so the hold never keeps a cursor off the head. Every write that
  // is logged for the key (this Put once its interval passed, a Remove, an ApplyAll write) drops
  // the key's held value: a hold only ever carries a value newer than the row (Opus r2 N1).
  PutResult Put(const RecordKey& key, std::span<const uint8_t> value, TimePoint now);
  // A tomb, kept until its epoch ends. Drops the key's held noisy value (Opus r2 N1).
  PutResult Remove(const RecordKey& key, TimePoint now);
  // All or nothing (Sol F8): every write is checked first (kind known, size, epoch, one scope, the
  // per-level and per-session row limits counting the rows the set adds); then all are logged with
  // consecutive LSNs, or none is and `failed` names the first write that would not go. A set that
  // mixes scopes is kMixedScope (Opus r2 N3; the request table answers it `model`, RM11). Each
  // write drops its key's held noisy value, so Service never logs an older value over it (Opus r2
  // N1).
  WriteResult ApplyAll(std::span<const Write> writes, TimePoint now);
  void Service(TimePoint now);  // logs each held noisy value whose interval passed
  const Record* Get(const RecordKey& key) const;
  // Drops level rows, their tombs and held noisy values; logs a kEpoch entry.
  void BeginEpoch(uint32_t generation);
  uint64_t LogObligation(uint64_t obligation_id);  // a kObligation entry; returns its LSN
  uint32_t Epoch() const;

  uint64_t Head() const;   // the last LSN
  uint64_t Floor() const;  // the oldest LSN the log still holds
  // The entries after `after` this subscriber must receive (Opus F2, F3, F5): each key of a
  // replicated kind in a subscribed scope changed after max(after, that scope's since), once, at
  // its latest revision; and each epoch entry. All in the order of their FIRST change after the
  // start (`first_lsn`; an epoch entry's is its own LSN). Passed without a call: obligations, a
  // scope not subscribed, a change at or below its scope's since, a non-replicated kind, a row an
  // epoch change has dropped. `f` returns false when it does not take the item (the batch is full):
  // the walk stops before that item, and `*proven` is the highest x such that every entry in
  // (after, x] was passed or taken by f (so Through{x} is sound). A key taken at its first change
  // carries its latest revision, so its later changes up to x are covered by it. A walk from below
  // Floor() - 1 (trimmed entries) hands out nothing and proves nothing: `*proven` is `after`.
  void Walk(uint64_t after, const Subscription& sub,
            const std::function<bool(const WalkItem&)>& f, uint64_t* proven) const;
  // The server trims to the slowest cursor, or past it at log_retention (that peer rebaselines).
  void TrimTo(uint64_t floor);

  struct Manifest {
    uint64_t lsn = 0;
    ScopeMask scopes = 0;
    std::array<uint64_t, kManifestBuckets> digest{};
  };
  // Replicated rows of those scopes at Head() (Opus F5).
  Manifest MakeManifest(ScopeMask scopes) const;
  // Every replicated row of those scopes, in dependency order, then key order.
  std::vector<RecordKey> BaselineKeys(ScopeMask scopes) const;

  size_t Rows() const;
  size_t Rows(Scope scope) const;
  size_t Rows(KindId kind) const;
  // Every row's value bytes plus its key's wire form (25 bytes per row), tombs counted.
  size_t Bytes() const;
  // Entries the log holds now (at most limits.log_retention: past it the oldest goes and the floor
  // moves, LM10). Not named by the design; the header's own.
  size_t LogEntries() const;
  // Every logged row S holds, of every kind (server-only rows and tombs included; no held noisy
  // value), in key order. For the map dump and the compare queries (section 7), which must see rows
  // no baseline carries; the header's own (item 5).
  void ForEach(const std::function<void(const Record&)>& f) const;
  // A write refused at a limit (kTooBig, kRowLimit), from Put or ApplyAll, the request table's
  // commits included: never silent (LM5, LM5b; record.h). The server turns each into a kLimit
  // event naming the limit, the kind and, for a session row, the scope. At most 64 are kept
  // between polls; the rest are counted in LimitHitsDropped. The header's own.
  struct LimitHit {
    PutResult result = PutResult::kRowLimit;
    KindId kind = 0;
    Scope scope = Scope::kLevel;
  };
  bool PollLimit(LimitHit& out);
  uint64_t LimitHitsDropped() const;
  // Counts every call that stored anything: a logged change, and a noisy value kept aside (which
  // takes no LSN yet). The request table compares it around Decide and OnReport, so a write that
  // only made a hold is caught too (D7, RM9). The header's own.
  uint64_t Writes() const;

  std::span<const KindSpec> Table() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

// What Replica::OnRecord did with a row. Not named by the design (its signature only); the header's
// own.
enum class ApplyResult : uint8_t {
  kApplied,      // into the replica and onto the apply queue (an unfenced kind)
  kHeld,         // into the replica, held on the apply queue until releasable (a fenced kind)
  kStaged,       // a scope being baselined, or a newer epoch waiting for its EpochBegin
  kStale,        // the revision rule dropped it (not newer)
  kWrongEpoch,   // an older epoch's row
  kUnknownKind,  // not in the table, or not replicated
  kTooBig,       // over the kind's max_value_bytes
};

// What a baseline's swap changed in the replica (BaselineEndResult). Not defined by the design;
// the header's own.
struct BaselineDiff {
  size_t added = 0;      // rows the replica did not hold
  size_t changed = 0;    // rows at another revision
  size_t removed = 0;    // rows of the baselined scopes the baseline did not carry
  size_t unchanged = 0;
};
struct BaselineEndResult {
  bool ok = false;  // !ok: digest mismatch (Sol F6): staging dropped, replica unchanged
  BaselineDiff diff;
};

// What the client's adapters did with a row TakeReady handed out (Sol F2, draft 4).
enum class RowApply : uint8_t {
  kApplied,  // the adapter wrote it (AdapterResult::kOk): settled
  kDropped,  // nothing to write and nothing to wait for (the entity is gone in this epoch, the kind
             // is unsupported here, a tomb with no entity): settled
  kWaiting,  // the entity is not here yet (pending, streamed out): unsettled, handed out again
  kFailed,   // the adapter's write failed (AdapterResult::kFailed): unsettled, handed out again
};

// What the Replica reports to the client's event stream (draft 4).
struct ReplicaEvent {
  enum class Type : uint8_t {
    kRowLate,      // `row` stayed waiting or failed past its kind's pending_grace_ms
    kFenceBroken,  // `row` (fenced) was released while `dependency` was late, not applied
  };
  Type type = Type::kRowLate;
  RecordKey row, dependency;
  RowApply last = RowApply::kWaiting;  // kRowLate: why it is late
};

// A client's copy. Nothing in it outlives the session (plan decision 13).
class Replica {
 public:
  Replica(std::span<const KindSpec> table, const Limits& limits);
  ~Replica();
  Replica(Replica&&) noexcept;
  Replica& operator=(Replica&&) noexcept;

  // The revision rule. A row of a scope being baselined goes to staging. Any other row goes into
  // the replica at once (so manifests compare the received rows) and onto the apply queue: at
  // once for an unfenced kind, held for a fenced one until it is releasable (Sol F2): the prefix is
  // at or past the row's LSN, and no row of a kind it depends on (directly or through others) with
  // a first_lsn at or below the row's LSN is unsettled (held, queued, handed out without a result,
  // waiting or failed; draft 4). A row that replaces an unsettled entry of its key keeps the
  // smaller first_lsn of the two and the entry's grace clock until the entry settles (Sol r2 N1).
  ApplyResult OnRecord(const Record& r, uint64_t first_lsn);
  // Always moves the prefix, staging or not (Opus F1); releases held rows it now covers.
  void OnThrough(uint64_t lsn);
  // Drops level rows, held and unsettled level rows (no late event: their epoch ended) and the
  // level subscription; rows of a newer epoch that came first wait for this marker (FutureRows,
  // coalesced per key and bounded, F4) and go in now.
  void OnEpoch(uint32_t generation, uint64_t lsn);
  // A fresh staging map for those scopes; an older one is dropped.
  void OnBaselineBegin(uint32_t id, uint64_t at, uint32_t epoch, ScopeMask scopes);
  // Into staging and into the running digest (Sol F6): FNV-1a 64 over (key, revision, tomb, value
  // digest) of each BaselineRows record in the order received.
  void OnBaselineRow(uint32_t id, const Record& r);
  // Digest equal: staging replaces the rows of its scopes, they are subscribed, and every staged
  // row goes on the apply queue in dependency order, then key order. A baseline that covers every
  // scope the peer holds (join, repair) also moves the prefix to at least `at`. Digest not equal:
  // staging is dropped, the replica and the apply queue are unchanged, and the client asks S for a
  // repair (Mismatch{at, 0}; Sol F6).
  BaselineEndResult OnBaselineEnd(uint32_t id, uint64_t at, uint64_t digest);
  // The next row for the adapters, in order: released rows (in dependency order, then LSN order),
  // then waiting and failed rows due again (once per Service). Every row handed out is unsettled
  // until OnApplied answers it; the client answers each one in the same pump (Sol F2, draft 4).
  bool TakeReady(Record& out);
  // The apply result of a row TakeReady handed out, at that revision (an older revision's result,
  // for a key that a newer row replaced meanwhile, settles nothing). kApplied and kDropped settle
  // the key and may release fenced rows it held; kWaiting and kFailed keep it unsettled.
  void OnApplied(const RecordKey& key, uint32_t revision, RowApply result, TimePoint now);
  // Grace clocks: a key unsettled past its kind's pending_grace_ms (from its first kWaiting or
  // kFailed) becomes late: the fence stops waiting for it (kRowLate, and kFenceBroken for each
  // dependent it held); a late row is still handed out and applied if its entity comes.
  void Service(TimePoint now);
  bool PollEvent(ReplicaEvent& out);
  ScopeMask Subscribed() const;
  ScopeMask Staging() const;
  // A decision whose writes are in `scope` is complete here: the scope is subscribed and not
  // staging, the prefix is at or past at_lsn, and no row held by the fence has first_lsn <= at_lsn
  // (Opus F8, Sol F2). Receipt plus the fence, not application: an unfenced row waiting for its
  // entity does not hold a completion; a fenced row held behind an unsettled dependency does, up to
  // the grace.
  bool Completes(Scope scope, uint64_t at_lsn) const;
  size_t Unsettled() const;  // held, queued, handed out, waiting or failed
  // Rows TakeReady handed out that OnApplied has not answered yet. The client answers every one in
  // the pump that took it, so this is 0 after each Client::Pump (section 7.3's invariant). The
  // header's own.
  size_t HandedOut() const;

  enum class Check : uint8_t { kEqual, kMismatch, kNotComparable };
  // Comparable only when Prefix() == m.lsn and nothing stages; an equal check sets ManifestOk().
  // kMismatch sets `*bucket_mask` to the buckets that differ.
  Check CheckManifest(const WorldState::Manifest& m, uint64_t* bucket_mask);
  uint64_t Prefix() const;
  uint64_t ManifestOk() const;  // the LSN of the last equal manifest (Opus F6)
  const Record* Get(const RecordKey& key) const;
  size_t Rows() const;
  size_t Bytes() const;
  // Rows of a newer epoch waiting for its EpochBegin (VR3), not in Rows() or Bytes() (F4; the
  // header's own): one per key, the newest revision with the smallest first_lsn, of one epoch
  // only (the newest seen: on the ordered lane an older one's marker can no longer come), at most
  // limits.max_rows_per_level keys (S holds no more level rows in one epoch; a key past it is
  // dropped and counted). Their bytes count kRowOverhead per row as Bytes() does.
  size_t FutureRows() const;
  size_t FutureBytes() const;
  uint32_t FutureEpoch() const;   // 0: none waits
  uint64_t FutureDropped() const;
  // Every row of the active replica (tombs included; not staging), in key order. For the map dump
  // and the compare queries (section 7); the header's own (item 5).
  void ForEach(const std::function<void(const Record&)>& f) const;
  void Reset();
  // Not named by the design; the header's own. The generation the replica's level rows are of.
  uint32_t Epoch() const;
  // An unsettled key's first change (the smaller one kept across replacements, Sol r2 N1; SR7b
  // reads it); nullopt for a settled key.
  std::optional<uint64_t> FirstLsn(const RecordKey& key) const;

  std::span<const KindSpec> Table() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace skylanders::backend
