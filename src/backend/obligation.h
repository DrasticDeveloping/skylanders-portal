#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <set>
#include <string_view>
#include <utility>
#include <vector>

#include "backend/base.h"
#include "backend/limits.h"
#include "backend/record.h"

// Obligations (design section 3.6): durable events per recipient, and how each one ends. An
// obligation exists only for the peers present when it is made (a level-scope one only for peers
// subscribed to the level scope then). It takes a log entry (WorldState::LogObligation), which
// every walk passes, and goes to its recipient only once that peer's cursor is at or past its LSN
// and the peer has credit. Server::Oblige makes one (server.h).
//
// One account, kept in the client's terms (Sol r3 F9, draft 5; it replaces draft 4's completion
// window and its 2W bound). Every obligation a client holds, in any state, costs one credit of
// C = limits.obligation_credit. The client keeps one entry per obligation it received and has not
// let go:
// - held: waiting for Completes(scope, lsn) or for its entity, including one a repair baseline
//   retired on S while the client held it (Opus r2 N4);
// - reported: ended by the client (fulfilled, or refused after the kind's grace); its
//   ObligationDone{id, end} is repeated every limits.obligation_report_interval until S answers
//   ObligationAck{id, ...}.
// An entry goes, and its credit with it: a reported one at its ack, or once the newest floor passes
// it (RC4); a held one below the newest floor (retired on S) once fulfilled or refused, with one
// ObligationDone, not repeated, which S acks and ignores (OB10); a held level-scope one at
// EpochBegin of a later generation, unfulfilled and unreported (OB4); every one at a rejoin (a new
// peer epoch).
// The client sends Ack{..., holding, seen}: its entry count and the highest id it has received in
// this peer epoch, read at the end of the same pump, on a change and at least every
// obligation_report_interval. S counts used = the newest Ack's holding plus the obligations it has
// sent above that Ack's seen, and sends the peer's next obligation only while used < C. At C it
// pauses the peer (a credit event): its obligations wait in S's table, unsent, not retired by a
// repair baseline, and go out once, in id order, as Acks return credit. Only the client's word
// returns credit, so the client holds at most C in every sequence of events, repeated repair
// baselines included. S holds at most limits.max_obligations_per_peer per peer, sent and not ended
// plus waiting; at it a further Oblige for the peer fails with a limit event.
// Ids count per recipient and peer epoch from 1 and arrive in rising order on the ordered lane, so
// the client drops as done any Obligation whose id is at or below its seen. The recipient's floor
// (the lowest id still outstanding for it on S, or the next id to be made when none is; each
// Obligation and each ObligationAck carries it; floors only rise) bounds nothing: it lets the
// client drop a reported entry whose acks were all lost once S has ended it (RC4). The mark,
// ObligationAck{0, floor, last_sent}, goes after BaselineEnd of a repair baseline that retired
// obligations and every obligation_report_interval while S has sent an id above the client's seen;
// the client raises its seen to last_sent, which also returns the credit of a killed Obligation
// (OB13). A client that gets more than C (a defect in S) keeps it and raises credit exceeded.

namespace skylanders::backend {

struct Obligation {
  uint64_t id = 0;  // from 1, per recipient and peer epoch
  uint16_t recipient = 0;
  RecordKey key;
  std::vector<uint8_t> value;
  uint64_t lsn = 0;
  Scope scope = Scope::kLevel;
  bool sent = false;  // S: gone out on the recipient's FIFO (a repair baseline retires only these)
};

enum class ObligationEnd : uint8_t {
  kCompleted,        // the recipient's adapter fulfilled it and reported so
  kRetiredBaseline,  // sent and not ended when a repair baseline began: never resent (no second
                     // spill); an unsent one, waiting for its cursor or for credit, goes out after
                     // BaselineEnd (D6, Opus F7); a client that holds it still fulfils it once, and
                     // its credit returns only then (Opus r2 N4, draft 5)
  kRetiredPeerLeft,  // the recipient left (sent and waiting ones); its return gets nothing
  kRetiredEpoch,     // a level-scope obligation still outstanding at the generation change, sent
                     // or waiting; the client drops its held copy at EpochBegin (draft 5)
  kRefused,          // the recipient's adapter could not resolve its entity in the kind's grace
};
// "completed", "retired baseline", ... (the map dump's and the event lines' words).
std::string_view ObligationEndName(ObligationEnd end);

// S's obligations for one recipient in one peer epoch, and the credit account it keeps for that
// client (section 3.6). Server keeps one per present peer and drops it at the leave. The header's
// own (item 4): the design gives the rules, not the class.
class ObligationLedger {
 public:
  explicit ObligationLedger(const Limits& limits);

  // At limits.max_obligations_per_peer, sent and not ended plus waiting: Oblige fails (OB6).
  bool Full() const;
  // The id the next Add takes (ids count from 1 per recipient and peer epoch).
  uint64_t NextId() const;
  // A new obligation; its id is NextId() and its LSN the log entry Server took for it.
  void Add(Obligation o);
  // The next obligation step 5 may send, in id order: the lowest-id unsent one, once `cursor` is
  // at or past its LSN and Used() is below limits.obligation_credit. nullptr when none may go;
  // `*credit_bound` (if given) is then true when only the credit holds the head back.
  const Obligation* NextToSend(uint64_t cursor, bool* credit_bound = nullptr) const;
  void MarkSent(uint64_t id, TimePoint now);
  // ObligationDone{id}: true when S still held it, sent (it ends now with the client's end); false
  // when it had ended already (retired, completed) or was never sent (it stays, unsent; council 2
  // B11), which is acked, ignored and counted (Sol F9).
  bool OnDone(uint64_t id);
  // A repair baseline began: the sent ones end kRetiredBaseline, never resent (D6); unsent ones
  // stay. Returns the ids retired.
  std::vector<uint64_t> RetireSent();
  // A generation change: every obligation of `scope` (sent or waiting) ends kRetiredEpoch.
  std::vector<uint64_t> RetireScope(Scope scope);
  // The recipient left: every one ends kRetiredPeerLeft.
  std::vector<uint64_t> RetireAll();
  // Ack{..., holding, seen}: the client's own count. Only this returns credit.
  void OnAck(uint32_t holding, uint64_t seen);

  // The newest Ack's holding plus the obligations sent above its seen: never below what the
  // client holds (section 3.6, "What S counts").
  size_t Used() const;
  // The lowest id still outstanding, or the next id to be made when none is; only rises.
  uint64_t Floor() const;
  uint64_t LastSent() const;    // the highest id sent in this peer epoch
  uint64_t ClientSeen() const;  // the newest Ack's seen
  size_t Outstanding() const;   // sent and not ended, plus waiting
  size_t Waiting() const;       // made and not sent
  uint64_t Ignored() const;     // ObligationDone for an id no longer held or not yet sent
  // The mark ObligationAck{0, floor, last_sent} is due: S has sent an id above the client's seen,
  // and limits.obligation_report_interval passed since the last mark and since that id went.
  bool MarkDue(TimePoint now) const;
  void OnMarkSent(TimePoint now);
  const Obligation* Get(uint64_t id) const;
  const std::map<uint64_t, Obligation>& All() const;

 private:
  Limits limits_;
  uint64_t next_id_ = 1;
  uint64_t last_sent_ = 0;
  std::map<uint64_t, Obligation> outstanding_;  // sent and not ended, plus waiting
  std::set<uint64_t> sent_above_seen_;          // sent ids above the newest Ack's seen
  uint32_t holding_ = 0;
  uint64_t seen_ = 0;
  uint64_t ignored_ = 0;
  TimePoint last_mark_{};
  TimePoint unseen_since_{};  // when sent_above_seen_ last became non-empty
};

// A client's obligation entries and its side of the credit account (section 3.6). The header's
// own (item 4).
class ObligationHolder {
 public:
  explicit ObligationHolder(const Limits& limits);

  struct Entry {
    Obligation obligation;
    bool reported = false;  // ended by the client; its ObligationDone repeats until acked
    ObligationEnd end = ObligationEnd::kCompleted;
    TimePoint last_report{};
    bool report_due = false;
    bool waiting = false;  // held, its entity not here (or its fulfil failed) since `waiting_since`
    TimePoint waiting_since{};
  };
  enum class Received : uint8_t {
    kNew,         // held now
    kDuplicate,   // its id is at or below seen: dropped as done (OB1, OB11)
    kOverCredit,  // held now, but the client holds more than limits.obligation_credit (a defect
                  // in S): credit exceeded
  };
  Received OnObligation(const Obligation& o, uint64_t floor);
  // ObligationAck{id, floor, last_sent}: the reported entry `id` goes (id 0: the mark); floor and
  // seen rise (seen to last_sent: on the ordered lane every id up to it came or was killed); a
  // reported entry below the floor goes too (RC4).
  void OnAck(uint64_t id, uint64_t floor, uint64_t last_sent);
  // EpochBegin of a later generation: held level-scope entries go, unfulfilled and unreported
  // (OB4, OB13). Returns how many went.
  size_t OnEpochBegin();
  // The client ended a held entry (fulfilled or refused). Below the floor (retired on S) one
  // ObligationDone is sent, not repeated, and the entry goes (OB10); otherwise it is reported and
  // repeated every limits.obligation_report_interval until acked.
  void End(uint64_t id, ObligationEnd end, TimePoint now);
  // The ObligationDone messages due now: new ends, once-only ends, and repeats.
  std::vector<std::pair<uint64_t, ObligationEnd>> TakeReports(TimePoint now);
  // Held (not reported) ids in id order: the ones the client still tries to fulfil.
  std::vector<uint64_t> HeldIds() const;
  Entry* Find(uint64_t id);
  const Entry* Find(uint64_t id) const;

  uint32_t Holding() const;  // every entry, held and reported
  uint64_t Seen() const;
  uint64_t Floor() const;
  // A rejoin: a new peer epoch starts with nothing.
  void Reset();

 private:
  Limits limits_;
  std::map<uint64_t, Entry> entries_;
  std::vector<std::pair<uint64_t, ObligationEnd>> once_;  // ends below the floor, sent once
  uint64_t seen_ = 0;
  uint64_t floor_ = 0;
};

}  // namespace skylanders::backend
