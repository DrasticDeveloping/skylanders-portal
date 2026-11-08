#pragma once

#include <cstdint>
#include <deque>
#include <map>
#include <string_view>
#include <variant>

#include "backend/base.h"
#include "backend/entity_id.h"
#include "backend/limits.h"
#include "backend/record.h"

// Leases (design section 3.8). Chests, hat boxes, conversations and turrets run on the holder's
// client under a lease; the holder reports and S commits (plan 2.4). A lease that expires commits
// nothing: the row stays as it was before the grant. A report after the expiry of a lease that was
// never reported is refused `lease expired`. The holder hears LeaseEnd every time. An ended lease
// (committed, expired, refused, revoked) stays in the table as a tomb (id, holder, request key,
// final state) until the generation ends or its holder leaves: it holds no entity, counts toward no
// max_leases_per_peer, and is bounded by the request keys (Sol r2 N2).
//
// From report to commit (Sol F7; the order is Sol r2 N2's), in S's request table, on
// LeaseReport{id, payload}: LeaseTable::Report, then by its ReportCheck: kUnknown dropped and
// counted; kNotHolder answered `not holder`, before any replay; kReplay answered from the request
// key's cache (`cached` set), before any expiry check and without any transition; kExpired ends the
// key `lease expired` (terminal, cached) with LeaseEnd{expired}; kEvaluate runs the kind's pure
// Authority::OnReport, which returns kCommit with writes (WorldState::ApplyAll, then Commit and
// LeaseEnd{committed}; not written: refused `limit`, kMixedScope `model`) or kRefuse (RefuseReport,
// LeaseEnd{report refused}). Every final decision goes into the key's cache before it is sent. The
// lease's request key stays pending until one of these ends it, or the lease expires or is revoked.

namespace skylanders::backend {

struct LeaseSpec {
  EntityId entity;
  KindId kind = 0;
  uint16_t holder = 0;
  Duration term{};
};

enum class LeaseState : uint8_t { kGranted, kReported, kCommitted, kExpired, kRevoked };
enum class LeaseEndReason : uint8_t {
  kCommitted,
  kExpired,
  kReportRefused,  // the kind's OnReport refused the report (Sol F7, RM5b)
  kRevokedPeerLeft,
  kRevokedEpoch,
  kRevokedClose,
};
std::string_view LeaseStateName(LeaseState state);
std::string_view LeaseEndReasonName(LeaseEndReason why);

struct Lease {  // (Opus F12)
  uint32_t id = 0;
  LeaseSpec spec;
  RequestKey request;  // the owner-run request that was granted it
  LeaseState state = LeaseState::kGranted;
  TimePoint expires{};
  // Once ended (committed, expired, revoked): why, as LeaseEnd carried it (the tomb's final state;
  // RM5e reads kReportRefused). The header's own (item 4).
  LeaseEndReason why = LeaseEndReason::kCommitted;
};

// A lease that ended outside a report (expired, revoked), for S to answer the request and send
// LeaseEnd. Not named by the design ("expiries come out as events"); the header's own.
struct LeaseEndEvent {
  uint32_t id = 0;
  uint16_t holder = 0;
  RequestKey request;
  LeaseEndReason why = LeaseEndReason::kExpired;
};

// What LeaseTable::Report found (Sol r2 N2). The table acts on it in section 3.8's order.
struct ReportCheck {
  enum class Type : uint8_t {
    kEvaluate,   // a granted lease, its holder, inside its term: now kReported; run OnReport
    kReplay,     // the holder, on a lease that already ended with a terminal decision (committed,
                 // report refused, expired, revoked): answer from `request`'s cache, change nothing
    kNotHolder,  // another peer: nothing changes
    kExpired,    // the holder, a granted lease past its term: now kExpired (terminal, cached)
    kUnknown,    // no lease and no tomb by that id (another generation, a departed holder)
  };
  Type type = Type::kUnknown;
  RequestKey request;  // the lease's request key (every type but kUnknown)
};

class LeaseTable {
 public:
  explicit LeaseTable(const Limits& limits);

  // kLeased when the entity is held, kLimit past limits.max_leases_per_peer.
  std::variant<uint32_t, Reason> Grant(const LeaseSpec& spec, const RequestKey& request,
                                       TimePoint now);
  bool Renew(uint32_t id, uint16_t peer, TimePoint now);
  // Checks only who, whether it already ended, and when, in that order (Sol r2 N2); the payload
  // goes to the kind (section 3.9). Only a granted lease is checked against its term or moved.
  ReportCheck Report(uint32_t id, uint16_t peer, TimePoint now);
  void Commit(uint32_t id);        // kReported -> kCommitted
  void RefuseReport(uint32_t id);  // kReported -> kRevoked, kReportRefused
  // Expiries of granted leases come out as events (PollEnd); a committed or ended lease never
  // expires.
  void Service(TimePoint now);
  void OnPeerLeft(uint16_t peer);     // revokes the peer's leases, drops its tombs
  void OnEpoch(uint32_t generation);  // revokes every lease, drops every tomb
  // End session: revokes every live lease (kRevokedClose, as events); tombs stay so a duplicate
  // report is still answered from its key's cache (LE8). The header's own (item 4).
  void OnClose();
  const Lease* Of(const EntityId& entity) const;  // the live (granted or reported) lease only
  const Lease* Get(uint32_t id) const;            // live or tomb
  bool PollEnd(LeaseEndEvent& out);
  size_t Held(uint16_t peer) const;  // live leases only (tombs count toward no cap)
  size_t Size() const;               // live leases and tombs (LM10: bounded by the keys)

 private:
  Limits limits_;
  uint32_t next_id_ = 1;
  std::map<uint32_t, Lease> leases_;
  std::deque<LeaseEndEvent> ended_;
};

}  // namespace skylanders::backend
