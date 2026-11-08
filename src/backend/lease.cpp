#include "backend/lease.h"

#include <iterator>

namespace skylanders::backend {

namespace {

bool Live(const Lease& l) {
  return l.state == LeaseState::kGranted || l.state == LeaseState::kReported;
}

}  // namespace

std::string_view LeaseStateName(LeaseState state) {
  switch (state) {
    case LeaseState::kGranted: return "granted";
    case LeaseState::kReported: return "reported";
    case LeaseState::kCommitted: return "committed";
    case LeaseState::kExpired: return "expired";
    case LeaseState::kRevoked: return "revoked";
  }
  return "?";
}

std::string_view LeaseEndReasonName(LeaseEndReason why) {
  switch (why) {
    case LeaseEndReason::kCommitted: return "committed";
    case LeaseEndReason::kExpired: return "expired";
    case LeaseEndReason::kReportRefused: return "report refused";
    case LeaseEndReason::kRevokedPeerLeft: return "revoked peer left";
    case LeaseEndReason::kRevokedEpoch: return "revoked epoch";
    case LeaseEndReason::kRevokedClose: return "revoked close";
  }
  return "?";
}

LeaseTable::LeaseTable(const Limits& limits) : limits_(limits) {}

Reason LeaseTable::CanGrant(const LeaseSpec& spec) const {
  if (Of(spec.entity) != nullptr) return Reason::kLeased;
  if (Held(spec.holder) >= limits_.max_leases_per_peer) return Reason::kLimit;
  return Reason::kNone;
}

std::variant<uint32_t, Reason> LeaseTable::Grant(const LeaseSpec& spec, const RequestKey& request,
                                                 TimePoint now) {
  if (const Reason why = CanGrant(spec); why != Reason::kNone) return why;
  Lease l;
  l.id = next_id_++;
  l.spec = spec;
  if (l.spec.term <= Duration::zero()) l.spec.term = limits_.lease_term;
  l.request = request;
  l.state = LeaseState::kGranted;
  l.expires = now + l.spec.term;
  const uint32_t id = l.id;
  leases_.emplace(id, std::move(l));
  return id;
}

bool LeaseTable::Renew(uint32_t id, uint16_t peer, TimePoint now) {
  auto it = leases_.find(id);
  if (it == leases_.end()) return false;
  Lease& l = it->second;
  if (l.spec.holder != peer || l.state != LeaseState::kGranted) return false;
  // A renew after the term is too late: the lease is expired at the next Service (or the report).
  if (now >= l.expires) return false;
  l.expires = now + l.spec.term;
  return true;
}

ReportCheck LeaseTable::Report(uint32_t id, uint16_t peer, TimePoint now) {
  ReportCheck check;
  auto it = leases_.find(id);
  if (it == leases_.end()) return check;  // kUnknown
  Lease& l = it->second;
  check.request = l.request;
  // Who first: a non-holder never learns the holder's decision (RM5d).
  if (l.spec.holder != peer) {
    check.type = ReportCheck::Type::kNotHolder;
    return check;
  }
  // Already ended (or reported and being decided): the key's cache answers, nothing moves, and
  // the term is not looked at (Sol r2 N2: a committed lease's duplicate after its old expiry gets
  // `applied`, RM5f).
  if (l.state != LeaseState::kGranted) {
    check.type = ReportCheck::Type::kReplay;
    return check;
  }
  // When: only a granted lease is checked against its term.
  if (now >= l.expires) {
    l.state = LeaseState::kExpired;
    l.why = LeaseEndReason::kExpired;
    check.type = ReportCheck::Type::kExpired;
    return check;
  }
  l.state = LeaseState::kReported;
  check.type = ReportCheck::Type::kEvaluate;
  return check;
}

void LeaseTable::Commit(uint32_t id) {
  auto it = leases_.find(id);
  if (it == leases_.end() || it->second.state != LeaseState::kReported) return;
  it->second.state = LeaseState::kCommitted;
  it->second.why = LeaseEndReason::kCommitted;
}

void LeaseTable::RefuseReport(uint32_t id) {
  auto it = leases_.find(id);
  if (it == leases_.end() || it->second.state != LeaseState::kReported) return;
  it->second.state = LeaseState::kRevoked;
  it->second.why = LeaseEndReason::kReportRefused;
}

void LeaseTable::Service(TimePoint now) {
  for (auto& [id, l] : leases_) {
    if (l.state != LeaseState::kGranted || now < l.expires) continue;
    l.state = LeaseState::kExpired;
    l.why = LeaseEndReason::kExpired;
    ended_.push_back(LeaseEndEvent{id, l.spec.holder, l.request, LeaseEndReason::kExpired, l});
  }
}

void LeaseTable::OnPeerLeft(uint16_t peer) {
  for (auto it = leases_.begin(); it != leases_.end();) {
    Lease& l = it->second;
    if (l.spec.holder != peer) {
      ++it;
      continue;
    }
    if (Live(l)) {
      Lease ended = l;
      ended.state = LeaseState::kRevoked;
      ended.why = LeaseEndReason::kRevokedPeerLeft;
      ended_.push_back(
          LeaseEndEvent{l.id, l.spec.holder, l.request, LeaseEndReason::kRevokedPeerLeft, ended});
    }
    it = leases_.erase(it);  // its tombs go with it
  }
}

void LeaseTable::OnEpoch(uint32_t /*generation*/) {
  for (auto& [id, l] : leases_) {
    if (Live(l)) {
      Lease ended = l;
      ended.state = LeaseState::kRevoked;
      ended.why = LeaseEndReason::kRevokedEpoch;
      ended_.push_back(
          LeaseEndEvent{id, l.spec.holder, l.request, LeaseEndReason::kRevokedEpoch, ended});
    }
  }
  leases_.clear();  // lease ids live one generation; every tomb goes
}

void LeaseTable::OnClose() {
  for (auto& [id, l] : leases_) {
    if (!Live(l)) continue;
    l.state = LeaseState::kRevoked;
    l.why = LeaseEndReason::kRevokedClose;
    ended_.push_back(LeaseEndEvent{id, l.spec.holder, l.request, LeaseEndReason::kRevokedClose, l});
  }
}

const Lease* LeaseTable::Of(const EntityId& entity) const {
  for (const auto& [id, l] : leases_) {
    if (Live(l) && l.spec.entity == entity) return &l;
  }
  return nullptr;
}

const Lease* LeaseTable::Get(uint32_t id) const {
  auto it = leases_.find(id);
  return it == leases_.end() ? nullptr : &it->second;
}

bool LeaseTable::PollEnd(LeaseEndEvent& out) {
  if (ended_.empty()) return false;
  out = ended_.front();
  ended_.pop_front();
  return true;
}

size_t LeaseTable::Held(uint16_t peer) const {
  size_t n = 0;
  for (const auto& [id, l] : leases_) {
    if (Live(l) && l.spec.holder == peer) ++n;
  }
  return n;
}

size_t LeaseTable::Size() const { return leases_.size(); }

}  // namespace skylanders::backend
