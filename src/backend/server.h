#pragma once

#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <vector>

#include "backend/base.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/lease.h"
#include "backend/limits.h"
#include "backend/obligation.h"
#include "backend/owned.h"
#include "backend/record.h"
#include "backend/request.h"
#include "backend/world_state.h"

// S's façade (design section 3.10): ties WorldState, obligations, leases and the request table to
// one PeerLink per peer. How it sends to one peer every pump, in this order (section 3.5): a
// baseline when one is due; a running baseline's chunks within the pacer's budget, then Through at
// the peer's proven x; deltas from the cursor in first-change order, then Through{proven}, all in
// one FIFO so a held send holds every later Through (Sol F1); a manifest every manifest_interval
// when the cursor is at Head() and no baseline runs; obligations once the cursor is at or past
// their LSN and only while the peer has credit: the newest Ack's holding plus the obligations sent
// above its seen is below limits.obligation_credit (Sol r3 F9, draft 5). At the credit the peer's
// obligations pause, waiting in S's table outside the FIFO, and resume in id order as Acks return
// credit. S answers every ObligationDone with ObligationAck{id, floor, last_sent}, also for an id
// it no longer holds (acked, ignored and counted), and sends the mark ObligationAck{0, floor,
// last_sent} after BaselineEnd of a repair baseline that retired obligations and every
// obligation_report_interval while it has sent an id above the client's seen (section 3.6).
// Nothing but StateHello goes into the state lane's FIFO while it has no room (F1, lanes.h's
// PeerQueue): a due baseline waits, a manifest is not made, obligations, the mark and the
// ObligationAcks owed wait in S's tables. Those one-off messages, once their order is met, take
// the lane's room before the walk does, so churn that fills the lane every pump cannot starve them
// (Sol code review r2, R1). The gameplay lane likewise: while it is blocked, Decisions and
// LeaseEnds wait in S's table, one per key (R2). S reads every frame of every peer at every pump
// and handles and answers every request and lease report it reads (Sol code review r3, N1): the
// link keeps nothing for S, so no transport buffer grows behind it. Past a cap that a peer using
// Client cannot reach, S holds that peer's requests and lease reports unhandled, in order, up to
// what a Client can have unanswered, and handles them once below the cap; one more ends the peer's
// session (kPeerOverrun). Nothing is dropped unanswered and every table stays bounded.

namespace skylanders::backend {

// What S reports. The design lists the event kinds (Server::PollEvent); the struct is the
// header's own.
struct ServerEvent {
  enum class Type : uint8_t {
    kDecision,                // `decision` was sent to `peer`
    kEvaluate,                // the game thread runs `request` (whole key), then SubmitEvaluation
    kBaselineBegun,           // `baseline`, `why`
    kBaselineEnded,           // `baseline`
    kObligationEnded,         // `obligation`, `obligation_end`
    kCreditPaused,            // `peer`'s obligations paused: its used credit reached
                              // obligation_credit (Sol r3 F9, draft 5)
    kLeaseEnded,              // `lease`, `lease_end`
    kLimit,                   // a limit was hit; `text` names it
    kLateEvaluationDropped,   // a SubmitEvaluation whose key the table no longer holds (Sol F3)
    kFault,                   // the injector played a fault; `text` is its rule
    // S ended `peer`'s session (Sol code review r3, N1): past the gameplay cap it held as many of
    // the peer's requests and lease reports as a Client can have unanswered, and one more came (a
    // sender past Client's admission). From then on S reads and drops what the peer sends and
    // sends it nothing; its keys, leases and obligations went as at RemovePeer. The host removes
    // it (RemovePeer) and closes its connection. The header's own (a council question).
    kPeerOverrun,
  };
  Type type = Type::kDecision;
  uint16_t peer = 0;
  Decision decision;
  Request request;
  uint32_t baseline = 0;
  BaselineReason why = BaselineReason::kJoin;
  uint64_t obligation = 0;
  ObligationEnd obligation_end = ObligationEnd::kCompleted;
  uint32_t lease = 0;
  LeaseEndReason lease_end = LeaseEndReason::kExpired;
  std::string text;
};

// One peer as S sees it (the map dump's `peer` line). Named by the design (Server::Stats); the
// fields are the header's own.
struct PeerStats {
  uint32_t peer_epoch = 0;
  uint64_t cursor = 0;
  uint64_t prefix = 0;        // the client's last Ack
  uint64_t manifest_ok = 0;   // the client's last Ack
  size_t unacked_frames = 0;  // the state lane's (Opus F10)
  Subscription subscription;
  uint32_t baseline = 0;      // the running one; 0: none
  BaselineReason baseline_why = BaselineReason::kJoin;
  size_t obligations_outstanding = 0;       // sent and not ended, plus waiting
  uint64_t obligation_floor = 0;            // the lowest id still outstanding, or the next id
  uint64_t obligation_reports_ignored = 0;  // ObligationDone for an id S no longer held (acked)
  // The credit account (Sol r3 F9, draft 5; section 3.6).
  size_t credit_used = 0;          // the newest Ack's holding plus those sent above its seen
  size_t obligations_waiting = 0;  // made and not sent: for the cursor or for credit
  uint64_t credit_pauses = 0;      // kCreditPaused events for this peer
  uint64_t client_seen = 0;        // the newest Ack's seen
  size_t frames_held = 0;     // in the FIFO behind a kFull
  // Every frame queued to the peer, on every lane, and the state lane's (F1: bounded by the
  // producers, lanes.h's PeerQueue), and the ObligationAcks waiting for the state lane's room.
  size_t frames_queued = 0;
  size_t state_frames_queued = 0;
  size_t acks_due = 0;
  // The gameplay lane's (Sol code review r2, R2; r3, N1): the Decisions and LeaseEnds waiting for
  // it to open, one per key; those and the gameplay messages queued, what the cap counts
  // (gameplay_held); the onsets of the cap; the requests and lease reports S holds unhandled
  // meanwhile; whether S ended the peer's session past both bounds (kPeerOverrun).
  size_t decisions_waiting = 0;
  size_t gameplay_held = 0;
  uint64_t cap_onsets = 0;
  size_t requests_held = 0;
  // Lease reports resent while unanswered (S2-L3) that came while a copy already waited in the
  // held requests: dropped, the waiting copy answers them. The header's own.
  uint64_t reports_coalesced = 0;
  bool overrun = false;
  uint64_t baselines = 0, manifests = 0, mismatches = 0;
};

class Server {
 public:
  Server(std::span<const KindSpec> table, AuthoritySet& authorities, const Limits& limits);
  ~Server();
  Server(const Server&) = delete;
  Server& operator=(const Server&) = delete;

  // The session baseline now, the level one at level ready. Gives the peer its next peer epoch
  // and sends StateHello{..., peer, peer_epoch} (Opus F9).
  void AddPeer(uint16_t peer, PeerRole role, PeerLink& link, TimePoint now);
  // The owner saw a precondition (in the game: the session's PlayerPose with this sequence).
  // Requests of that peer held on `precondition <= sequence` go on.
  void OnPrecondition(uint16_t peer, uint32_t sequence, TimePoint now);
  // Drops the peer's request keys (pending ones end `session ended` on S's side; nothing is sent),
  // leases (revoked, kRevokedPeerLeft), obligations (sent and waiting ones end kRetiredPeerLeft)
  // with its credit account, cursor and subscription (Opus F9, Sol F3; draft 5).
  void RemovePeer(uint16_t peer, TimePoint now);
  void BeginEpoch(uint32_t generation, TimePoint now);  // finals first, as in 1B
  void OnPeerLevelReady(uint16_t peer, uint32_t generation, TimePoint now);
  void BeginClose(TimePoint now);
  WorldState& State();
  // A durable event for `recipient` (section 3.6); its id counts per recipient and peer epoch from
  // 1. 0 when it cannot be made (the recipient is absent, not level-subscribed for a level kind, or
  // at max_obligations_per_peer, sent and waiting counted: a limit event). It goes out once the
  // cursor is at or past its LSN and the peer has credit; until then it waits in S's table.
  uint64_t Oblige(uint16_t recipient, const RecordKey& key, std::span<const uint8_t> value);
  // `key` is the whole key a kEvaluate event named, peer epoch included; false (dropped, counted)
  // when the table no longer holds it (Sol F3).
  bool SubmitEvaluation(const RequestKey& key, const EvaluationResult& result, TimePoint now);
  void Pump(TimePoint now);  // WorldState::Service, leases, requests, then each peer's send
  // Decisions, kEvaluate, baselines (begun, ended, why), obligation ends, credit pauses, lease
  // ends, limits hit, late evaluations dropped, faults injected, peers' sessions ended past the
  // gameplay cap (kPeerOverrun: the host removes the peer).
  bool PollEvent(ServerEvent& out);
  PeerStats Stats(uint16_t peer) const;
  // Read-only views for tests and the map dump (item 4; the header's own).
  const RequestTable& Requests() const;
  const LeaseTable& Leases() const;
  // Stage 4 (owned.h): the game's codec check for owned rows (admission check 6). Unset: every
  // value decodes. Set once, before the first peer.
  void SetOwnedSchema(OwnedSchemaCheck schema);
  // Stage 4: the admission's counters (owned.h).
  const OwnedAdmission& Owned() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace skylanders::backend
