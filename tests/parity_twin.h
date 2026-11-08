#pragma once

// The parity suite's twin harness (stage 2 item 4; design sections 3.9 and 10; Sol r1 F2).
//
// Every cell of 1B's request table (tests/online_requests_test.cpp) runs here on two tables at
// once, in lockstep: 1B's own online::RequestTable and the generic backend::RequestTable, fed
// through section 3.9's key mapper. The harness has 1B's harness interface (online_requests_test's
// struct H) field for field, so the cells are 1B's own bodies: their assertions check 1B's outputs
// as they do in 1B, and after every batch of outputs the harness checks that the generic table gave
// the same answers in the same order (each generic key mapped back to its 1B key, each generic
// reason mapped one way to 1B's, section 3.9's last paragraph), that every key is in the same state
// and that the same keys are evaluating. A difference is a failed check, printed with both batches,
// unless it is one of the differences the design decides (below), which the cell must name.
//
// Written against 1B at portal 3bbbcc6 (1B's merged commit on the online branch; Sol F10, both
// reviews' Q2): requests.h, requests.cpp and online_requests_test.cpp are the same there as at
// 26ef72d and 9054072, where the suite was first written, and the cells were recounted there: 67
// cell functions.
//
// D2 (the include rule) is kept: this file and parity_requests_test.cpp include online/ and
// backend/ headers; they are test-only and live outside src/backend and outside tests/backend*, the
// files the rule covers. include_rule.cmake also refuses an include of a parity_ file from those
// files, so 1B's headers cannot reach them through this one. The library never sees 1B's table; the
// test binary already links skylanders_online (netplay is compiled into it, D2).
//
// How 1B's inputs reach the generic table (the stage 4 bridge's job, done here by the adapter):
// - Keys: section 3.9's map. Every 1B key takes the next generic sequence of its (peer, peer epoch,
//   generation) the first time it is seen; an exit or touch allocates its enter's key at once, as
//   its parent. Entries go with their generation's tombs. A key of a generation outside the tomb
//   window (older than the tombs, or one S has not opened) gets a transient sequence, not kept.
// - Poses: 1B's OnPose only records a pose; 1B's table reads it at its next call (Service, a
//   request of that peer, an evaluation's result). Server::OnPrecondition advances the peer's
//   queue at once (section 3.10). So the adapter hands a pose to the generic table at once when the
//   peer has no pending key there (nothing can move), and otherwise at 1B's next reading of it:
//   before Service and SubmitEvaluation, after a request of that peer. A pose of another
//   generation never reaches the generic table (OnPrecondition has no generation; the bridge drops
//   it, as 1B's OnPose does). RP-d holds the generic table's own timing.
// - A peer that left comes back under a new peer epoch (section 3.9, RemovePeer; VR8); a late
//   result names the key its kEvaluate named (Server::SubmitEvaluation, section 3.10).
// - The game thread's results: a pass is a pass; 1B's rate, unresolved and unsupported are the
//   generic reasons; every other reason is kKind with kind_reason = 1B's reason, retryable for too
//   far, rate, not released and not in play (the kind's rule; the generic table makes it terminal
//   for kNeverResent and kClosesParent requests). server_final passes through (1B item 9).
//
// The differences the design decides (each named where it shows):
// - End session: the session keeps End session's drain (section 1 point 3); Server::BeginClose has
//   no peer list and no SessionClosed (section 3.10), so the generic table gives every final at
//   BeginClose, where 1B gives a peer's finals at its SessionClosed or the 2 s gone timeout. The
//   harness holds the generic finals and matches each with 1B's when 1B gives it (RP-e). Tag
//   "close-resend": a resend during closing of a key with no final yet is refused `session ended`
//   by 1B (final, not S's); the generic key has its final already (from BeginClose), so it repeats
//   it, cached (invariants 1 and 4, section 3.9).
// - kClosedByChild: 1B refuses a touch whose exit exists in any state but held; the generic table
//   only when a kClosesParent child was let through (section 3.9's table). Tag
//   "closed-by-child-let-through" (RP-c).

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>
#include <vector>

#include "backend/kind_table.h"
#include "backend/lease.h"
#include "backend/limits.h"
#include "backend/request.h"
#include "backend/world_state.h"
#include "online/requests.h"
#include "test_util.h"

namespace skylanders::parity {

namespace ob = skylanders::online;
namespace gb = skylanders::backend;
using namespace std::chrono_literals;

inline constexpr const char* kOneBRevision = "3bbbcc6";  // 1B's merged commit (Sol F10)

// --- The parity kinds: 1B's five key types as generic kinds (section 3.9).

constexpr gb::KindId kEnterKind = 20, kExitKind = 21, kTouchKind = 22, kTelegramKind = 23,
                     kSkipKind = 24;

constexpr gb::KindSpec ParityKind(gb::KindId id, std::string_view name, bool precondition,
                                  uint32_t parent_wait_ms, bool retryable_missing) {
  gb::KindSpec k;
  k.id = id;
  k.name = name;
  k.scope = gb::Scope::kLevel;
  k.tier = gb::Tier::kStructural;
  k.state_class = gb::StateClass::kCurrentValue;
  k.initial = gb::InitialPolicy::kReadAtSpawn;
  k.write = gb::WriteMode::kApplied;
  k.models = gb::kModelServerRun;
  k.max_value_bytes = 16;
  k.pending_grace_ms = 5000;
  k.parent_wait_ms = parent_wait_ms;
  k.parent_missing_retryable = retryable_missing;
  k.requires_precondition = precondition;
  return k;
}

// Enter, exit, touch and telegram need a pose (pose 0 refused `unsupported`, 1B's Dispatch); the
// exit and the touch wait kHeldExitWait for their enter, the exit's `parent missing` retryable
// (1B's exit before enter), the touch's terminal (1B's enter refused). Every 1B key is evaluated on
// the game thread: every kind is server-run.
inline constexpr std::array<gb::KindSpec, 5> kParityTable{
    ParityKind(kEnterKind, "parity.enter", true, 0, false),
    ParityKind(kExitKind, "parity.exit", true, 500, true),
    ParityKind(kTouchKind, "parity.touch", true, 500, false),
    ParityKind(kTelegramKind, "parity.telegram", true, 0, false),
    ParityKind(kSkipKind, "parity.skip", false, 0, false),
};

enum class KeyType : uint8_t { kEnter, kExit, kTouch, kTelegram, kSkip };

inline KeyType TypeOf(const ob::RequestKey& k) {
  if (k.kind == ob::RequestKind::kTelegram) return KeyType::kTelegram;
  if (k.kind == ob::RequestKind::kSceneSkip) return KeyType::kSkip;
  if (k.event == ob::TriggerEvent::kExit) return KeyType::kExit;
  if (k.event == ob::TriggerEvent::kTouch) return KeyType::kTouch;
  return KeyType::kEnter;
}

inline gb::KindId KindOf(KeyType t) {
  switch (t) {
    case KeyType::kEnter: return kEnterKind;
    case KeyType::kExit: return kExitKind;
    case KeyType::kTouch: return kTouchKind;
    case KeyType::kTelegram: return kTelegramKind;
    case KeyType::kSkip: return kSkipKind;
  }
  return 0;
}

inline std::string KeyText(const ob::RequestKey& k) {
  char buf[96];
  switch (TypeOf(k)) {
    case KeyType::kEnter:
    case KeyType::kExit:
    case KeyType::kTouch: {
      const char* ev = TypeOf(k) == KeyType::kEnter  ? "enter"
                       : TypeOf(k) == KeyType::kExit ? "exit"
                                                     : "touch";
      std::snprintf(buf, sizeof buf, "%s#%u(p%u g%u)", ev, k.enter_sequence, k.peer, k.generation);
      break;
    }
    case KeyType::kTelegram:
      std::snprintf(buf, sizeof buf, "telegram#%u(p%u g%u)", k.request_sequence, k.peer,
                    k.generation);
      break;
    case KeyType::kSkip:
      std::snprintf(buf, sizeof buf, "skip#%u(p%u g%u)", k.skip_sequence, k.peer, k.generation);
      break;
  }
  return buf;
}

// --- The key mapper (section 3.9): one collision-free map per (peer, peer epoch, generation).

class KeyMap {
 public:
  void Reset(uint32_t generation) {
    maps_.clear();
    transient_.clear();
    reverse_.clear();
    previous_ = current_ = generation;
  }
  // The generation change: the window moves; entries older than the tombs go with them.
  void OnGeneration(uint32_t generation) {
    previous_ = current_;
    current_ = generation;
    std::erase_if(maps_, [&](const auto& e) { return std::get<2>(e.first) < previous_; });
    transient_.clear();
    std::erase_if(reverse_, [&](const auto& e) { return e.first.generation < previous_; });
  }
  // A peer left: its old peer epoch's entries go (it comes back under a new one).
  void ForgetPeer(uint16_t peer, uint32_t peer_epoch) {
    std::erase_if(maps_, [&](const auto& e) {
      return std::get<0>(e.first) == peer && std::get<1>(e.first) == peer_epoch;
    });
  }
  // The generic key of a 1B key: the same entry for the same content (Sol F4).
  gb::RequestKey Map(const ob::RequestKey& k, uint32_t peer_epoch) {
    const bool in_window = k.generation >= previous_ && k.generation <= current_;
    auto& m = in_window ? maps_[{k.peer, peer_epoch, k.generation}]
                        : transient_[{k.peer, peer_epoch, k.generation}];
    const uint32_t next = uint32_t(m.size() + 1);
    auto [it, inserted] = m.try_emplace(k, next);
    const gb::RequestKey g{k.peer, peer_epoch, k.generation, it->second};
    if (inserted) reverse_[g] = k;
    return g;
  }
  std::optional<gb::RequestKey> Find(const ob::RequestKey& k, uint32_t peer_epoch) const {
    for (const auto* maps : {&maps_, &transient_}) {
      auto m = maps->find({k.peer, peer_epoch, k.generation});
      if (m == maps->end()) continue;
      auto it = m->second.find(k);
      if (it != m->second.end()) {
        return gb::RequestKey{k.peer, peer_epoch, k.generation, it->second};
      }
    }
    return std::nullopt;
  }
  std::optional<ob::RequestKey> Reverse(const gb::RequestKey& g) const {
    auto it = reverse_.find(g);
    if (it == reverse_.end()) return std::nullopt;
    return it->second;
  }
  // Entries kept (transient ones aside): LM10's "no key of an ended generation".
  size_t Size() const {
    size_t n = 0;
    for (const auto& [k, m] : maps_) n += m.size();
    return n;
  }
  size_t SizeOfGeneration(uint32_t generation) const {
    size_t n = 0;
    for (const auto& [k, m] : maps_) n += std::get<2>(k) == generation ? m.size() : 0;
    return n;
  }

 private:
  using MapKey = std::tuple<uint16_t, uint32_t, uint32_t>;
  std::map<MapKey, std::map<ob::RequestKey, uint32_t>> maps_, transient_;
  std::map<gb::RequestKey, ob::RequestKey> reverse_;
  uint32_t previous_ = 0, current_ = 0;
};

// --- What one output says, in 1B's words.

// A generic reason 1B has no word for: never equal to one of 1B's.
inline constexpr auto kUnmapped = static_cast<ob::RefusalReason>(0xFF);

struct Norm {
  enum class Type : uint8_t { kDecision, kEvaluate, kOther };
  Type type = Type::kOther;
  gb::RequestKey key;
  uint8_t outcome = 0;  // 1 applied, 2 pending, 3 refused (1B's DecisionOutcome values)
  ob::RefusalReason reason = ob::RefusalReason::kNone;
  bool final = false, server_final = false, cached = false;
  bool operator==(const Norm&) const = default;
};

// The generic reason as 1B's (section 3.9's one-way map). `type` is the request's key type.
inline ob::RefusalReason OneBReason(gb::Reason reason, uint8_t kind_reason, bool server_final,
                                    KeyType type) {
  using RR = ob::RefusalReason;
  switch (reason) {
    case gb::Reason::kNone: return RR::kNone;
    case gb::Reason::kStaleGeneration: return server_final ? RR::kGenerationEnded : RR::kStale;
    case gb::Reason::kSessionEnded: return RR::kSessionEnded;
    case gb::Reason::kRate: return RR::kRate;
    case gb::Reason::kParentRefused:
    case gb::Reason::kClosedByChild: return RR::kEnterRefused;
    case gb::Reason::kParentMissing:
      // retryable (the exit kind's parent_missing_retryable): exit before enter; terminal (the
      // touch's): enter refused
      return type == KeyType::kExit ? RR::kExitBeforeEnter : RR::kEnterRefused;
    case gb::Reason::kPreconditionMissing: return RR::kTooFar;
    case gb::Reason::kUnresolved: return RR::kUnresolved;
    case gb::Reason::kUnsupported: return RR::kUnsupported;
    case gb::Reason::kKind: return static_cast<RR>(kind_reason);
    default: return kUnmapped;
  }
}

// The game thread's result for the generic table (the bridge's conversion, header comment).
inline gb::EvaluationResult GenericResult(const ob::EvaluationResult& r) {
  using RR = ob::RefusalReason;
  gb::EvaluationResult g;
  g.server_final = r.server_final;
  switch (r.reason) {
    case RR::kNone: break;
    case RR::kRate:
      g.reason = gb::Reason::kRate;
      g.retryable = true;
      break;
    case RR::kUnresolved: g.reason = gb::Reason::kUnresolved; break;
    case RR::kUnsupported: g.reason = gb::Reason::kUnsupported; break;
    default:
      g.reason = gb::Reason::kKind;
      g.kind_reason = static_cast<uint8_t>(r.reason);
      g.retryable =
          r.reason == RR::kTooFar || r.reason == RR::kNotReleased || r.reason == RR::kNotInPlay;
      break;
  }
  return g;
}

// --- The differences a cell shows, by tag (the design-decided ones; anything else fails).

inline std::string& CurrentCell() {
  static std::string cell;
  return cell;
}
// Every difference noted in the running cell, in order: its tag, the operation it showed at and
// what differed. RunCell compares the (tag, operation) list with the cell's expected one, so a
// region that shows a difference at another operation, or more or fewer of them, fails (Sol code
// review r2, R7).
struct CellDiff {
  std::string tag, op, detail;
};
inline std::vector<CellDiff>& CellDiffs() {
  static std::vector<CellDiff> diffs;
  return diffs;
}
inline void NoteDiff(const std::string& tag, const std::string& op, const std::string& detail) {
  CellDiffs().push_back({tag, op, detail});
}

// Every generic request is server-run: Decide asks for the evaluation, as 1B's Advance does.
class ServerRunAuthority final : public gb::Authority {
 public:
  gb::Verdict Decide(const gb::Request&, const gb::DecideContext&) override {
    gb::Verdict v;
    v.type = gb::Verdict::Type::kServerRun;
    return v;
  }
};

// --- The twin: 1B's H (online_requests_test.cpp:91-216) on both tables.

class Twin {
 public:
  using RR = ob::RefusalReason;
  using RS = ob::RequestState;
  using OT = ob::RequestOutput::Type;
  struct Sent {
    size_t at;
    uint64_t ordinal;
    ob::RequestKey key;
  };
  struct Load {
    size_t at;
    uint64_t ordinal;
    uint32_t generation;
  };

  // What the cells call on h.table: each call goes to both tables.
  class Table {
   public:
    explicit Table(Twin& t) : t_(t) {}
    void Reset(uint32_t generation) { t_.ResetBoth(generation); }
    void OnRequest(const ob::WorldRequest& r, ob::TimePoint now) { t_.RequestBoth(r, now); }
    bool OnPose(uint16_t peer, uint32_t generation, uint32_t seq) {
      return t_.PoseBoth(peer, generation, seq);
    }
    bool SubmitEvaluation(const ob::RequestKey& k, const ob::EvaluationResult& r,
                          ob::TimePoint now) {
      return t_.SubmitBoth(k, r, now);
    }
    void ForgetPeer(uint16_t peer) { t_.ForgetBoth(peer); }
    RS StateOf(const ob::RequestKey& k) const { return t_.one_.StateOf(k); }
    bool Evaluating(const ob::RequestKey& k) const { return t_.one_.Evaluating(k); }
    size_t Size() const {
      if (t_.one_.Size() != t_.generic_.Size()) {
        t_.Mismatch("Size: 1B " + std::to_string(t_.one_.Size()) + ", generic " +
                    std::to_string(t_.generic_.Size()));
      }
      return t_.one_.Size();
    }
    bool Closing() const { return t_.one_.Closing(); }
    // End session's per-peer wait is the session's (section 1 point 3): 1B's only.
    bool AllClosed() const { return t_.one_.AllClosed(); }

   private:
    Twin& t_;
  };

  Twin()
      : state_(kParityTable, limits_),
        leases_(limits_),
        generic_(kParityTable, set_, state_, leases_, limits_),
        table(*this) {
    for (const gb::KindSpec& k : kParityTable) set_.Register(k.id, authority_);
    one_.Reset(gen);
    state_.BeginEpoch(gen);
    generic_.Reset(gen);
    map_.Reset(gen);
  }
  ~Twin() { Finish(); }
  Twin(const Twin&) = delete;
  Twin& operator=(const Twin&) = delete;

 private:
  // Declared before `table` (initialised in this order).
  ob::RequestTable one_;
  gb::Limits limits_{};
  gb::WorldState state_;
  gb::LeaseTable leases_;
  ServerRunAuthority authority_;
  gb::AuthoritySet set_;
  gb::RequestTable generic_;

 public:
  Table table;
  ob::TimePoint now = ob::TimePoint{} + std::chrono::hours(1);
  uint32_t gen = 1;
  RR verdict = RR::kNone;
  std::map<ob::RequestKey, RR> verdicts;
  int checks = 0;
  bool game = true;
  uint32_t next_applied = 0;
  std::vector<ob::RequestKey> asked;
  std::vector<ob::RequestOutput> log;        // 1B's outputs (the cells read these)
  std::vector<gb::RequestOutput> glog;       // the generic table's
  std::vector<Sent> sent;
  std::vector<Load> level_loads;
  uint64_t ordinal = 0;

  RR VerdictFor(const ob::RequestKey& k) const {
    auto it = verdicts.find(k);
    return it == verdicts.end() ? verdict : it->second;
  }
  bool Answer(const ob::RequestKey& k, std::optional<RR> v) {
    ++checks;
    ob::EvaluationResult r;
    r.reason = v ? *v : VerdictFor(k);
    if (r.reason == RR::kNone) r.applied_sequence = ++next_applied;
    return SubmitBoth(k, r, now);
  }
  void Take() {
    for (;;) {
      std::vector<ob::RequestOutput> o1 = one_.TakeOutputs();
      std::vector<gb::RequestOutput> o2 = generic_.TakeOutputs();
      CompareBatch(o1, o2);
      for (gb::RequestOutput& o : o2) glog.push_back(std::move(o));
      if (o1.empty() && o2.empty()) break;
      for (ob::RequestOutput& o : o1) {
        if (o.type == OT::kEvaluate) {
          asked.push_back(o.request.key);
          last_eval_[o.request.key] = map_.Map(o.request.key, Epoch(o.request.key.peer));
        }
        log.push_back(std::move(o));
      }
      if (!game) break;
      for (const ob::RequestKey& k : std::exchange(asked, {})) Answer(k, std::nullopt);
    }
    CompareStates();
    op_sends_.clear();
  }
  bool Submit(const ob::RequestKey& k, std::optional<RR> v = std::nullopt) {
    Op("Submit " + KeyText(k));
    std::erase(asked, k);
    const bool taken = Answer(k, v);
    Take();
    return taken;
  }
  void Send(const ob::WorldRequest& r) {
    Op("Send " + KeyText(r.key) + " pose " + std::to_string(r.pose_sequence));
    sent.push_back(Sent{log.size(), ordinal++, r.key});
    RequestBoth(r, now);
    Take();
  }
  bool Pose(uint32_t seq, uint16_t peer = 2) { return PoseBoth(peer, gen, seq); }
  void Service() {
    Op("Service");
    DeliverPoses(std::nullopt);
    one_.Service(now);
    generic_.Service(now);
    Take();
  }
  void Wait(std::chrono::milliseconds d) {
    now += d;
    Service();
  }
  void NewGeneration() {
    Op("NewGeneration");
    ++gen;
    pending_poses_.clear();  // 1B drops the old generation's poses at the change, unread
    one_.OnGenerationChange(gen);
    generic_.OnGenerationChange(gen, now);
    state_.BeginEpoch(gen);
    map_.OnGeneration(gen);
    std::erase_if(last_eval_, [&](const auto& e) { return e.first.generation + 1 < gen; });
    Take();
    level_loads.push_back(Load{log.size(), ordinal++, gen});
  }
  void Close(std::vector<uint16_t> peers = {2}) {
    Op("Close");
    one_.BeginClose(peers, now);
    generic_.BeginClose(now);
    closing_ = true;
    in_begin_close_ = true;
    Take();
    in_begin_close_ = false;
    DeliverPoses(std::nullopt);  // closing: nothing advances on either table
  }
  void Closed(uint16_t peer = 2) {
    Op("Closed p" + std::to_string(peer));
    one_.OnClosed(peer);
    Take();
  }
  RS State(const ob::WorldRequest& r) const { return one_.StateOf(r.key); }
  bool Evaluating(const ob::WorldRequest& r) const { return one_.Evaluating(r.key); }

  std::vector<ob::WorldDecision> Decisions(const ob::RequestKey& k, size_t from = 0) const {
    std::vector<ob::WorldDecision> out;
    for (size_t i = from; i < log.size(); ++i) {
      if (log[i].type == OT::kDecision && log[i].decision.key == k) out.push_back(log[i].decision);
    }
    return out;
  }
  int Applies(const ob::RequestKey& k) const {
    int n = 0;
    for (const ob::RequestOutput& o : log) {
      n += o.type == OT::kDecision && o.decision.key == k && !o.decision.cached &&
           o.decision.outcome == ob::DecisionOutcome::kApplied;
    }
    return n;
  }
  int Evaluations(const ob::RequestKey& k) const {
    int n = 0;
    for (const ob::RequestOutput& o : log) n += o.type == OT::kEvaluate && o.request.key == k;
    return n;
  }
  size_t At(OT type, const ob::RequestKey& k, size_t from = 0) const {
    for (size_t i = from; i < log.size(); ++i) {
      const ob::RequestKey& key = type == OT::kEvaluate ? log[i].request.key : log[i].decision.key;
      if (log[i].type == type && key == k) return i;
    }
    return log.size();
  }
  size_t Mark() const { return log.size(); }
  size_t OutputsSince(size_t mark) const { return log.size() - mark; }

  // --- The generic side, for the cells' own generic checks.

  // The generic key of a 1B key (current peer epoch); nullopt if the map no longer holds it.
  std::optional<gb::RequestKey> GenericKey(const ob::RequestKey& k) const {
    return map_.Find(k, Epoch(k.peer));
  }
  std::vector<gb::Decision> GenericDecisions(const ob::RequestKey& k) const {
    std::vector<gb::Decision> out;
    const auto g = GenericKey(k);
    if (!g) return out;
    for (const gb::RequestOutput& o : glog) {
      if (o.type == gb::RequestOutput::Type::kDecision && o.decision.key == *g) {
        out.push_back(o.decision);
      }
    }
    return out;
  }
  std::optional<gb::Decision> GenericLast(const ob::RequestKey& k) const {
    const auto all = GenericDecisions(k);
    if (all.empty()) return std::nullopt;
    return all.back();
  }
  // A cell that shows a design-decided difference: from here to EndDifference, a difference is
  // noted under `tag` with its operation (the cell's expected list names each one, in order)
  // instead of failing.
  void ExpectDifference(std::string tag) { expect_tag_ = std::move(tag); }
  void EndDifference() { expect_tag_.clear(); }
  // A pose handed to the generic table at once, as Server::OnPrecondition does (RP-d).
  bool PoseAtOnce(uint32_t seq, uint16_t peer = 2) {
    Op("PoseAtOnce " + std::to_string(seq));
    const bool a = one_.OnPose(peer, gen, seq);
    const bool b = generic_.OnPrecondition(peer, seq, now);
    if (a != b) Mismatch("OnPose: 1B and generic disagree on newer");
    Take();
    return a;
  }
  gb::RequestTable& Generic() { return generic_; }
  const KeyMap& Map() const { return map_; }
  int Mismatches() const { return mismatches_; }

  // 1B's invariants 1, 2 and 4 over the generic log (CheckInvariants runs 1B's over 1B's log):
  // nothing after a key's final, at most one final, a cached answer repeats the last decision, and
  // with `all_final` every key sent has its one final.
  void CheckGeneric(bool all_final) const {
    std::map<gb::RequestKey, gb::Decision> last;
    std::map<gb::RequestKey, int> finals;
    for (const gb::RequestOutput& o : glog) {
      if (o.type != gb::RequestOutput::Type::kDecision) continue;
      const gb::Decision& d = o.decision;
      if (d.cached) {
        auto it = last.find(d.key);
        CHECK(it != last.end());
        if (it != last.end()) CHECK(SameDecision(it->second, d));
        continue;
      }
      CHECK(finals[d.key] == 0);
      if (d.final) ++finals[d.key];
      last[d.key] = d;
    }
    if (all_final) {
      for (const gb::RequestKey& k : gsent_) CHECK(finals[k] == 1);
    }
  }

 private:
  struct PendingPose {
    uint16_t peer;
    uint32_t seq;
    bool expected;  // 1B's answer
  };
  struct CloseFinal {
    Norm norm;
    size_t index = 0;  // its place among the generic table's BeginClose finals
    bool matched = false;
  };

  static bool SameDecision(const gb::Decision& a, const gb::Decision& b) {
    return a.key == b.key && a.outcome == b.outcome && a.reason == b.reason &&
           a.kind_reason == b.kind_reason && a.final == b.final &&
           a.server_final == b.server_final &&
           a.at_lsn == b.at_lsn && a.lease == b.lease && a.revision == b.revision;
  }

  uint32_t Epoch(uint16_t peer) const {
    auto it = epoch_.find(peer);
    return it == epoch_.end() ? 1 : it->second;
  }

  void Op(std::string what) { op_ = std::move(what); }

  void Mismatch(const std::string& what) const {
    if (!expect_tag_.empty()) {
      NoteDiff(expect_tag_, op_, what);
      return;
    }
    ++mismatches_;
    ++g_failures;
    std::fprintf(stderr, "PARITY MISMATCH [%s] at %s: %s\n", CurrentCell().c_str(), op_.c_str(),
                 what.c_str());
  }

  std::string Text(const Norm& n) const {
    std::string s;
    const auto k = map_.Reverse(n.key);
    const std::string key = (k ? KeyText(*k) : "?") + "=" + std::to_string(n.key.sequence) + "/e" +
                            std::to_string(n.key.peer_epoch);
    switch (n.type) {
      case Norm::Type::kEvaluate: return "evaluate " + key;
      case Norm::Type::kOther: return "other " + key;
      case Norm::Type::kDecision: break;
    }
    s = "decision " + key + " ";
    s += n.outcome == 1 ? "applied" : n.outcome == 2 ? "pending" : "refused";
    if (n.reason != RR::kNone) {
      s += " (";
      s += n.reason == kUnmapped ? "UNMAPPED" : ob::RefusalReasonName(n.reason);
      s += ")";
    }
    if (n.final) s += " final";
    if (n.server_final) s += " S";
    if (n.cached) s += " cached";
    return s;
  }
  std::string Text(const std::vector<Norm>& v) const {
    std::string s = "[";
    for (size_t i = 0; i < v.size(); ++i) s += (i ? "; " : "") + Text(v[i]);
    return s + "]";
  }

  Norm FromOneB(const ob::RequestOutput& o) {
    Norm n;
    if (o.type == OT::kEvaluate) {
      n.type = Norm::Type::kEvaluate;
      n.key = Lookup(o.request.key);
      return n;
    }
    n.type = Norm::Type::kDecision;
    n.key = Lookup(o.decision.key);
    n.outcome = static_cast<uint8_t>(o.decision.outcome);
    n.reason = o.decision.reason;
    n.final = o.decision.final;
    n.server_final = o.decision.server_final;
    n.cached = o.decision.cached;
    return n;
  }
  Norm FromGeneric(const gb::RequestOutput& o) const {
    Norm n;
    if (o.type == gb::RequestOutput::Type::kEvaluate) {
      n.type = Norm::Type::kEvaluate;
      n.key = o.request.key;
      return n;
    }
    if (o.type != gb::RequestOutput::Type::kDecision) {
      n.type = Norm::Type::kOther;  // a LeaseEnd: 1B has none
      return n;
    }
    const gb::Decision& d = o.decision;
    n.type = Norm::Type::kDecision;
    n.key = d.key;
    switch (d.outcome) {
      case gb::Outcome::kApplied: n.outcome = 1; break;
      case gb::Outcome::kPending: n.outcome = 2; break;
      case gb::Outcome::kRefused: n.outcome = 3; break;
      case gb::Outcome::kGranted: n.outcome = 4; break;
    }
    auto t = types_.find(d.key);
    n.reason = OneBReason(d.reason, d.kind_reason, d.server_final,
                          t == types_.end() ? KeyType::kEnter : t->second);
    n.final = d.final;
    n.server_final = d.server_final;
    n.cached = d.cached;
    return n;
  }
  gb::RequestKey Lookup(const ob::RequestKey& k) {
    if (auto g = map_.Find(k, Epoch(k.peer))) return *g;
    return map_.Map(k, Epoch(k.peer));
  }

  gb::Request GenericRequest(const ob::WorldRequest& r) {
    const KeyType t = TypeOf(r.key);
    const uint32_t ep = Epoch(r.key.peer);
    gb::Request g;
    g.key = map_.Map(r.key, ep);
    g.kind = KindOf(t);
    g.model = gb::Model::kServerRun;
    g.precondition = r.pose_sequence;
    types_[g.key] = t;
    if (t == KeyType::kExit || t == KeyType::kTouch) {
      ob::RequestKey enter = r.key;
      enter.event = ob::TriggerEvent::kEnter;
      g.parent = map_.Map(enter, ep);
      types_[g.parent] = KeyType::kEnter;
      if (t == KeyType::kExit) g.flags = gb::kClosesParent;
    } else if (t == KeyType::kTelegram || t == KeyType::kSkip) {
      g.flags = gb::kNeverResent;
    }
    return g;
  }

  void RequestBoth(const ob::WorldRequest& r, ob::TimePoint at) {
    one_.OnRequest(r, at);
    const gb::Request g = GenericRequest(r);
    gsent_.insert(g.key);
    sent_keys_.insert(r.key);
    op_sends_.insert(g.key);
    generic_.OnRequest(g, gb::PeerRole::kGuest, at);
    DeliverPoses(r.key.peer);  // 1B reads that peer's pose in this call's Advance
  }

  // The generic table has a pending key of `peer` (a pose could move it).
  bool GenericPending(uint16_t peer) const {
    for (const gb::RequestKey& k : gsent_) {
      if (k.peer == peer && k.peer_epoch == Epoch(peer) && k.generation == gen &&
          generic_.StateOf(k) == gb::RequestState::kPending) {
        return true;
      }
    }
    return false;
  }

  bool PoseBoth(uint16_t peer, uint32_t generation, uint32_t seq) {
    const bool a = one_.OnPose(peer, generation, seq);
    if (generation != gen) {
      // Another generation's pose: the bridge drops it (1B's OnPose does too).
      if (a) Mismatch("1B took a pose of another generation");
      return a;
    }
    if (closing_ || !GenericPending(peer)) {
      const bool b = generic_.OnPrecondition(peer, seq, now);
      if (a != b) Mismatch("OnPose " + std::to_string(seq) + ": 1B " + (a ? "newer" : "dropped") +
                           ", generic " + (b ? "newer" : "dropped"));
      std::vector<gb::RequestOutput> o2 = generic_.TakeOutputs();
      if (!o2.empty()) {
        std::vector<Norm> n2;
        for (const gb::RequestOutput& o : o2) n2.push_back(FromGeneric(o));
        Mismatch("a pose moved the generic table with no pending key: " + Text(n2));
        for (gb::RequestOutput& o : o2) glog.push_back(std::move(o));
      }
    } else {
      pending_poses_.push_back(PendingPose{peer, seq, a});
    }
    return a;
  }

  void DeliverPoses(std::optional<uint16_t> only) {
    for (auto it = pending_poses_.begin(); it != pending_poses_.end();) {
      if (only && it->peer != *only) {
        ++it;
        continue;
      }
      const bool b = generic_.OnPrecondition(it->peer, it->seq, now);
      if (b != it->expected) {
        Mismatch("OnPose " + std::to_string(it->seq) + " (read later): 1B " +
                 (it->expected ? "newer" : "dropped") + ", generic " + (b ? "newer" : "dropped"));
      }
      it = pending_poses_.erase(it);
    }
  }

  bool SubmitBoth(const ob::RequestKey& k, const ob::EvaluationResult& r, ob::TimePoint at) {
    DeliverPoses(std::nullopt);
    const bool a = one_.SubmitEvaluation(k, r, at);
    auto e = last_eval_.find(k);
    const gb::RequestKey g = e != last_eval_.end() ? e->second : Lookup(k);
    const bool b = generic_.SubmitEvaluation(g, GenericResult(r), at);
    if (a != b) {
      Mismatch("SubmitEvaluation " + KeyText(k) + ": 1B " + (a ? "taken" : "dropped") +
               ", generic " + (b ? "taken" : "dropped"));
    }
    return a;
  }

  void ForgetBoth(uint16_t peer) {
    Op("ForgetPeer p" + std::to_string(peer));
    one_.ForgetPeer(peer);
    generic_.ForgetPeer(peer, now);
    map_.ForgetPeer(peer, Epoch(peer));
    epoch_[peer] = Epoch(peer) + 1;  // it comes back under a new peer epoch (section 3.9, VR8)
    std::erase_if(pending_poses_, [&](const PendingPose& p) { return p.peer == peer; });
    std::erase_if(sent_keys_, [&](const ob::RequestKey& k) { return k.peer == peer; });
    std::erase_if(close_buf_, [&](const auto& e) { return e.first.peer == peer; });
    if (!one_.TakeOutputs().empty() || !generic_.TakeOutputs().empty()) {
      Mismatch("ForgetPeer output something");
    }
    CompareStates();
  }

  void ResetBoth(uint32_t generation) {
    Op("Reset");
    one_.Reset(generation);
    generic_.Reset(generation);
    map_.Reset(generation);
    pending_poses_.clear();
    close_buf_.clear();
    close_resend_.clear();
    last_eval_.clear();
    epoch_.clear();
    sent_keys_.clear();
    closing_ = false;
  }

  // One batch of outputs from each table: the same answers in the same order. While closing, the
  // generic finals given at BeginClose are matched with 1B's as 1B gives them.
  void CompareBatch(const std::vector<ob::RequestOutput>& o1,
                    const std::vector<gb::RequestOutput>& o2) {
    std::vector<Norm> n1, n2;
    std::vector<uint16_t> closed;
    for (const ob::RequestOutput& o : o1) {
      if (o.type == OT::kPeerClosed) {
        closed.push_back(o.peer);
      } else {
        n1.push_back(FromOneB(o));
      }
    }
    for (const gb::RequestOutput& o : o2) n2.push_back(FromGeneric(o));
    if (in_begin_close_) {
      if (!n1.empty()) Mismatch("1B output at BeginClose: " + Text(n1));
      for (const Norm& n : n2) {
        if (n.type != Norm::Type::kDecision || !n.final || n.cached) {
          Mismatch("generic BeginClose output that is not a final: " + Text(n));
          continue;
        }
        close_buf_[n.key] = CloseFinal{n, close_buf_.size(), false};
      }
      return;
    }
    if (closing_) Reconcile(n1, n2);
    if (n1 != n2) {
      Mismatch("outputs differ\n    1B:      " + Text(n1) + "\n    generic: " + Text(n2));
    }
    for (const uint16_t peer : closed) {
      for (const auto& [k, f] : close_buf_) {
        if (k.peer == peer && !f.matched) {
          Mismatch("1B closed p" + std::to_string(peer) +
                   " without the final the generic table gave at BeginClose: " + Text(f.norm));
        }
      }
    }
  }

  void Reconcile(std::vector<Norm>& n1, std::vector<Norm>& n2) {
    std::vector<Norm> kept;
    size_t last_index = 0;
    bool any = false;
    for (const Norm& n : n1) {
      if (n.type == Norm::Type::kDecision && n.final && !n.cached) {
        auto it = close_buf_.find(n.key);
        if (it != close_buf_.end() && !it->second.matched) {
          it->second.matched = true;
          // 1B gives a peer's finals in arrival order, as the generic BeginClose gives them all.
          if (any && it->second.index < last_index) {
            Mismatch("close finals in another order: " + Text(n));
          }
          last_index = it->second.index;
          any = true;
          if (n == it->second.norm) continue;  // the same final, given at BeginClose
          if (n.reason == RR::kSessionEnded && !n.server_final && op_sends_.contains(n.key)) {
            auto c = std::find_if(n2.begin(), n2.end(), [&](const Norm& g) {
              return g.type == Norm::Type::kDecision && g.cached && g.key == n.key;
            });
            if (c != n2.end()) {
              Norm repeat = it->second.norm;
              repeat.cached = true;
              if (*c == repeat) {
                NoteDiff("close-resend", op_, "1B: " + Text(n) + "; generic: " + Text(*c));
                close_resend_[n.key] = {n, it->second.norm};
                n2.erase(c);
                continue;
              }
            }
          }
        }
      }
      if (n.type == Norm::Type::kDecision && n.cached) {
        // A later repeat of a close-resend key: each table repeats its own final.
        auto r = close_resend_.find(n.key);
        if (r != close_resend_.end()) {
          Norm one = r->second.first, two = r->second.second;
          one.cached = two.cached = true;
          auto c = std::find(n2.begin(), n2.end(), two);
          if (n == one && c != n2.end()) {
            n2.erase(c);
            continue;
          }
        }
      }
      kept.push_back(n);
    }
    n1 = std::move(kept);
  }

  // After each batch: every key the cells sent is in the same state on both tables (while closing,
  // a key whose generic final came at BeginClose may still wait for 1B's), the same keys evaluate,
  // and both hold as many keys.
  void CompareStates() {
    for (const ob::RequestKey& k : sent_keys_) {
      const auto g = map_.Find(k, Epoch(k.peer));
      const RS s1 = one_.StateOf(k);
      if (!g) {
        if (s1 != RS::kNew) {
          Mismatch(KeyText(k) + ": no generic key, 1B state " + ob::RequestStateName(s1));
        }
        continue;
      }
      const auto s2 = generic_.StateOf(*g);
      auto cb = close_buf_.find(*g);
      const bool waiting_close = cb != close_buf_.end() && !cb->second.matched;
      if (waiting_close) {
        if (s2 != gb::RequestState::kRefused ||
            (s1 != RS::kPending && s1 != RS::kHeld && s1 != RS::kRetry)) {
          Mismatch(KeyText(k) + " while closing: 1B " + ob::RequestStateName(s1) + ", generic " +
                   std::string(gb::RequestStateName(s2)));
        }
      } else if (static_cast<int>(s1) != static_cast<int>(s2)) {
        Mismatch(KeyText(k) + ": 1B " + ob::RequestStateName(s1) + ", generic " +
                 std::string(gb::RequestStateName(s2)));
      }
      if (one_.Evaluating(k) != generic_.Evaluating(*g)) {
        Mismatch(KeyText(k) + ": evaluating on 1B " + (one_.Evaluating(k) ? "yes" : "no") +
                 ", generic " + (generic_.Evaluating(*g) ? "yes" : "no"));
      }
    }
    if (one_.Size() != generic_.Size()) {
      Mismatch("Size: 1B " + std::to_string(one_.Size()) + ", generic " +
               std::to_string(generic_.Size()));
    }
    if (one_.Closing() != generic_.Closing()) Mismatch("Closing differs");
  }

  void Finish() {
    if (!one_.AllClosed()) return;
    for (const auto& [k, f] : close_buf_) {
      if (!f.matched) Mismatch("a generic BeginClose final 1B never gave: " + Text(f.norm));
    }
  }

  KeyMap map_;
  std::map<uint16_t, uint32_t> epoch_;                    // per peer: its peer epoch (from 1)
  std::map<gb::RequestKey, KeyType> types_;               // generic key -> 1B key type
  std::map<ob::RequestKey, gb::RequestKey> last_eval_;    // the generic key each kEvaluate named
  std::set<gb::RequestKey> gsent_;                        // generic keys of every request sent
  std::set<ob::RequestKey> sent_keys_;                    // 1B keys sent (state comparison)
  std::set<gb::RequestKey> op_sends_;                     // requests sent since the last batch
  std::vector<PendingPose> pending_poses_;
  std::map<gb::RequestKey, CloseFinal> close_buf_;        // generic finals given at BeginClose
  std::map<gb::RequestKey, std::pair<Norm, Norm>> close_resend_;  // 1B's final, the generic one
  bool closing_ = false;
  bool in_begin_close_ = false;
  std::string op_ = "start";
  std::string expect_tag_;
  mutable int mismatches_ = 0;
};

}  // namespace skylanders::parity
