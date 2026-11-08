#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <span>
#include <string>

#include "backend/adapter.h"
#include "backend/base.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/lease.h"
#include "backend/limits.h"
#include "backend/obligation.h"
#include "backend/record.h"
#include "backend/request.h"
#include "backend/world_state.h"

// A client's façade (design section 3.10): a Replica, its adapters and one PeerLink to S. Nothing
// is keyed before StateHello, which names this peer and its peer epoch (Opus F9, Sol F3).

namespace skylanders::backend {

// What a client reports. The design lists the event kinds (Client::PollEvent); the struct is the
// header's own.
struct ClientEvent {
  enum class Type : uint8_t {
    kDecided,           // `decision` arrived (its peer epoch is this client's)
    kCompleted,         // `key`'s decision is complete here: Completes(scope, at_lsn)
    kLeaseEnded,        // `lease`, `lease_end`
    kBaselineApplied,   // `baseline`
    kBaselineFailed,    // `baseline`: digest mismatch, Mismatch{at, 0} sent (Sol F6)
    kManifestMismatch,  // `lsn`, `bucket_mask`
    kRowLate,           // `row` stayed waiting or failed past its kind's pending grace;
                        // `row_apply` is its last result (Sol F2, draft 4)
    kFenceBroken,       // `row` (fenced) was released while `dependency` was late, not applied
                        // (Sol F2, draft 4)
    kCreditExceeded,    // S sent an obligation past obligation_credit (a defect in S): it is kept;
                        // the invariant checker fails the run on it (Sol r3 F9, draft 5)
    kHelloRefused,      // StateHello's table digest is not this client's: everything is refused,
                        // nothing applied (VR6; item 4, the header's own)
    kBaselineLost,      // a Manifest (at `lsn`) came while a baseline was still staging: its
                        // BaselineEnd was lost above the lane, Mismatch{lsn, 0} sent (Sol code
                        // review r2, R4; the header's own)
    kInboundLost,       // InboundLost: recoverable state messages were discarded below the
                        // client; staging dropped, Mismatch{`lsn` (the prefix), 0} sent (stage 3)
  };
  Type type = Type::kDecided;
  RequestKey key;
  Decision decision;
  uint32_t lease = 0;
  LeaseEndReason lease_end = LeaseEndReason::kExpired;
  uint32_t baseline = 0;
  uint64_t lsn = 0;
  uint64_t bucket_mask = 0;
  RecordKey row;
  RecordKey dependency;                     // kFenceBroken
  RowApply row_apply = RowApply::kWaiting;  // kRowLate
  std::string text;
};

// Why Client::Send, Client::Resend or Client::Report took nothing: the game's side of admission
// backpressure (Sol code review r3, N1). The client accepts work only while it can keep it: a key
// is assigned and kept until its final (or its completion) arrives, so a request is never taken
// and then lost. The header's own; its policy is a council question (design section 3.9).
enum class SendRefusal : uint8_t {
  kNone = 0,
  kNoHello,      // before StateHello, or after a refused one: nothing can be keyed yet (Sol F3)
  kKeyBound,     // Send: max_keys_per_peer keys are open here; no key assigned, nothing sent
  kKeyOpen,      // Send: an explicit key this client already holds open
  kNotRetry,     // Resend: the key is unknown, final, kNeverResent or not in the retry state
  kUnanswered,   // Resend: the key's last send has no answer yet; Report: the lease's last report
  kNotHeld,      // Report: not a lease this client holds (granted, no LeaseEnd yet)
};

class Client {
 public:
  Client(std::span<const KindSpec> table, AdapterSet& adapters, const Limits& limits);
  ~Client();
  Client(const Client&) = delete;
  Client& operator=(const Client&) = delete;

  void Attach(PeerLink& link, TimePoint now);  // self and peer epoch come in StateHello
  // The sequence is filled in unless set; peer and peer epoch are the hello's. nullopt, with
  // `why` set, when nothing is taken: before StateHello (Sol F3), or while max_keys_per_peer keys
  // are open here (N1: the check comes before a key is assigned). A key is open from Send until its
  // final decision arrives, or its `applied` completes here; a key of an ended generation stays
  // open until S's final for it comes (S sends one for every key it holds at the change). The
  // decisions S has not delivered are all for open keys, so this bound also bounds what S keeps
  // waiting for this peer (server.cpp, PlayCap).
  std::optional<RequestKey> Send(Request request, TimePoint now, SendRefusal* why = nullptr);
  // Sends a key in the retry state again, unchanged. Refused locally (false) for a key that is
  // final, unknown or kNeverResent, and while the key's last send has no answer yet (N1: at most
  // one send of a key is ever unanswered).
  bool Resend(const RequestKey& key, TimePoint now, SendRefusal* why = nullptr);
  // While the gameplay lane is held, a request or resend waits, in order, in the client's request
  // table and goes when the lane goes again (F1); a key already waiting gets no second entry, so
  // repeated resends of it go once (Sol code review r2, R3). A lease report waits too, and a newer
  // report of the same lease replaces the waiting one and moves behind what was made before it
  // (the design names no rule for it: the newest wins; R5). Refused locally (false, `why` set)
  // for a lease this client does not hold: before StateHello, or not granted to it, or ended (its
  // LeaseEnd came; the key has its final then), and while a report of it that went to S has no
  // answer yet (kUnanswered; one waiting for the lane is replaced instead, as above). A report
  // taken is kept until it is sent (or its LeaseEnd comes first: the key has its final), and S
  // answers it (N1).
  bool Report(uint32_t lease, std::span<const uint8_t> payload, TimePoint now,
              SendRefusal* why = nullptr);
  // Receive; Replica::Service; take every ready row, resolve its entity, call its adapter and
  // answer it with Replica::OnApplied (a release that answer causes is taken in the same pump, so a
  // dependency and its dependent can apply in one pump; Sol F2, draft 4); fulfil releasable
  // obligations (held level-scope ones are dropped, unreported, at EpochBegin of a later
  // generation); repeat unacked ObligationDone every obligation_report_interval (Sol F9); ack
  // (Ack{prefix, manifest_ok, holding, seen} on a change and at least every
  // obligation_report_interval; Sol r3 F9, draft 5); check manifests.
  void Pump(TimePoint now);
  // Stage 3 (future/online-3.md section 3.2, "Recoverable overflow on a client"): the game's bridge
  // discarded recoverable state messages from S (Delta, Tomb, Through, Manifest, BaselineBegin,
  // BaselineRows, BaselineEnd) because its inbound budget was spent, and its receive reached the
  // loss mark: every message before the mark has been read, none after it. Drops staging as a
  // failed baseline digest does (the active replica and the apply queue untouched), sends
  // Mismatch{prefix, 0, lost} (lost: S answers with a fresh full baseline even while one whose
  // `at` covers the prefix runs, since its rest is discarded here; Sol code review r1 F1), and
  // from now on discards recoverable state messages until the
  // BaselineBegin of a baseline with an id above the last one read before the mark (a baseline S
  // began after the loss; S numbers each peer's baselines upward). Control messages (StateHello,
  // EpochBegin, Obligation, ObligationAck, Decision, LeaseEnd) are never discarded here. Safe on
  // the ordered lane: every delta S sent before it read the Mismatch has an LSN at or below the new
  // baseline's `at`, and every later one comes after its BaselineBegin. Nothing before StateHello
  // (nothing is read then). Called between pumps, never from inside the link's Receive. A second
  // loss while discarding sends another Mismatch and keeps the same id floor.
  void InboundLost(TimePoint now);
  // True from InboundLost until the repair baseline's BaselineBegin is read (for tests and the
  // debug window).
  bool AwaitingRepairBaseline() const;
  // Recoverable state messages discarded since the last InboundLost (for the bridge's line).
  uint64_t DiscardedAfterLoss() const;
  // The row whose adapter Apply or Retire call is in progress on this thread (the fault injector's
  // `newer` matcher reads its revision; stage 3 section 3.7); nullptr outside such a call (an
  // effect's apply included).
  const Record* Applying() const;
  const Replica& State() const;
  // The client's obligation entries and its side of the credit account (item 4; for tests and
  // the invariant checker).
  const ObligationHolder& Obligations() const;
  // Frames queued to S on every lane, and the messages waiting in the client's tables for a held
  // lane (unsent requests, resends and lease reports; the newest Mismatch). A held lane gets
  // nothing new: periodic messages are not made, one-off ones wait (F1; lanes.h's PeerQueue). The
  // header's own, for tests.
  size_t QueuedFrames() const;
  size_t Waiting() const;
  size_t OpenKeys() const;      // keys taken and not ended here: at most max_keys_per_peer (N1)
  uint16_t Self() const;        // 0 before StateHello
  uint32_t PeerEpoch() const;   // 0 before StateHello
  // Decided, completed (Completes(scope, at_lsn)), lease ended, baseline applied or failed,
  // manifest mismatch, row late, fence broken (row and dependency; draft 4), credit exceeded
  // (draft 5).
  bool PollEvent(ClientEvent& out);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace skylanders::backend
