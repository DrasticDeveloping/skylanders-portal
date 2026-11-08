#include "backend/obligation.h"

#include <algorithm>
#include <iterator>

namespace skylanders::backend {

std::string_view ObligationEndName(ObligationEnd end) {
  switch (end) {
    case ObligationEnd::kCompleted: return "completed";
    case ObligationEnd::kRetiredBaseline: return "retired baseline";
    case ObligationEnd::kRetiredPeerLeft: return "retired peer left";
    case ObligationEnd::kRetiredEpoch: return "retired epoch";
    case ObligationEnd::kRefused: return "refused";
  }
  return "?";
}

// ---------------------------------------------------------------------------------------------
// S's side.

ObligationLedger::ObligationLedger(const Limits& limits) : limits_(limits) {}

bool ObligationLedger::Full() const {
  return outstanding_.size() >= limits_.max_obligations_per_peer;
}

uint64_t ObligationLedger::NextId() const { return next_id_; }

void ObligationLedger::Add(Obligation o) {
  o.id = next_id_++;
  o.sent = false;
  outstanding_.emplace(o.id, std::move(o));
}

const Obligation* ObligationLedger::NextToSend(uint64_t cursor, bool* credit_bound) const {
  if (credit_bound) *credit_bound = false;
  // Ids and LSNs are both taken in Oblige, so the lowest unsent id is also the lowest LSN; a pause
  // holds the head without skipping it (section 3.6, "Duplicates").
  for (const auto& [id, o] : outstanding_) {
    if (o.sent) continue;
    if (o.lsn > cursor) return nullptr;
    if (Used() >= limits_.obligation_credit) {
      if (credit_bound) *credit_bound = true;
      return nullptr;
    }
    return &o;
  }
  return nullptr;
}

void ObligationLedger::MarkSent(uint64_t id, TimePoint now) {
  auto it = outstanding_.find(id);
  if (it == outstanding_.end() || it->second.sent) return;
  it->second.sent = true;
  last_sent_ = std::max(last_sent_, id);
  if (id > seen_) {
    if (sent_above_seen_.empty()) unseen_since_ = now;
    sent_above_seen_.insert(id);
  }
}

bool ObligationLedger::OnDone(uint64_t id) {
  auto it = outstanding_.find(id);
  // An id S no longer holds, or one it has not sent: the client cannot have received an unsent
  // one, so its report ends nothing (council 2 B11). The obligation stays and goes out in its turn.
  if (it == outstanding_.end() || !it->second.sent) {
    ++ignored_;
    return false;
  }
  outstanding_.erase(it);
  return true;
}

std::vector<uint64_t> ObligationLedger::RetireSent() {
  std::vector<uint64_t> ids;
  for (auto it = outstanding_.begin(); it != outstanding_.end();) {
    if (it->second.sent) {
      ids.push_back(it->first);
      it = outstanding_.erase(it);
    } else {
      ++it;
    }
  }
  return ids;
}

std::vector<uint64_t> ObligationLedger::RetireScope(Scope scope) {
  std::vector<uint64_t> ids;
  for (auto it = outstanding_.begin(); it != outstanding_.end();) {
    if (it->second.scope == scope) {
      ids.push_back(it->first);
      it = outstanding_.erase(it);
    } else {
      ++it;
    }
  }
  return ids;
}

std::vector<uint64_t> ObligationLedger::RetireAll() {
  std::vector<uint64_t> ids;
  ids.reserve(outstanding_.size());
  for (const auto& [id, o] : outstanding_) ids.push_back(id);
  outstanding_.clear();
  return ids;
}

void ObligationLedger::OnAck(uint32_t holding, uint64_t seen) {
  holding_ = holding;
  // The client's seen only rises; an Ack overtaken by a newer one cannot arrive later on the
  // ordered lane, but the maximum keeps the account safe either way.
  seen_ = std::max(seen_, seen);
  sent_above_seen_.erase(sent_above_seen_.begin(), sent_above_seen_.upper_bound(seen_));
}

size_t ObligationLedger::Used() const { return size_t(holding_) + sent_above_seen_.size(); }

uint64_t ObligationLedger::Floor() const {
  return outstanding_.empty() ? next_id_ : outstanding_.begin()->first;
}

uint64_t ObligationLedger::LastSent() const { return last_sent_; }
uint64_t ObligationLedger::ClientSeen() const { return seen_; }
size_t ObligationLedger::Outstanding() const { return outstanding_.size(); }

size_t ObligationLedger::Waiting() const {
  return size_t(std::count_if(outstanding_.begin(), outstanding_.end(),
                              [](const auto& entry) { return !entry.second.sent; }));
}

uint64_t ObligationLedger::Ignored() const { return ignored_; }

bool ObligationLedger::MarkDue(TimePoint now) const {
  if (last_sent_ <= seen_) return false;
  const TimePoint from = std::max(last_mark_, unseen_since_);
  return now - from >= limits_.obligation_report_interval;
}

void ObligationLedger::OnMarkSent(TimePoint now) { last_mark_ = now; }

const Obligation* ObligationLedger::Get(uint64_t id) const {
  auto it = outstanding_.find(id);
  return it == outstanding_.end() ? nullptr : &it->second;
}

const std::map<uint64_t, Obligation>& ObligationLedger::All() const { return outstanding_; }

// ---------------------------------------------------------------------------------------------
// The client's side.

ObligationHolder::ObligationHolder(const Limits& limits) : limits_(limits) {}

ObligationHolder::Received ObligationHolder::OnObligation(const Obligation& o, uint64_t floor) {
  floor_ = std::max(floor_, floor);
  if (o.id <= seen_) return Received::kDuplicate;
  seen_ = o.id;
  Entry e;
  e.obligation = o;
  entries_.emplace(o.id, std::move(e));
  return entries_.size() > limits_.obligation_credit ? Received::kOverCredit : Received::kNew;
}

void ObligationHolder::OnAck(uint64_t id, uint64_t floor, uint64_t last_sent) {
  floor_ = std::max(floor_, floor);
  seen_ = std::max(seen_, last_sent);
  if (id != 0) {
    auto it = entries_.find(id);
    if (it != entries_.end() && it->second.reported) entries_.erase(it);
  }
  // S has ended every id below its floor: a reported one needs no more acks.
  std::erase_if(entries_, [&](const auto& entry) {
    return entry.second.reported && entry.first < floor_;
  });
}

size_t ObligationHolder::OnEpochBegin() {
  return std::erase_if(entries_, [](const auto& entry) {
    return !entry.second.reported && entry.second.obligation.scope == Scope::kLevel;
  });
}

void ObligationHolder::End(uint64_t id, ObligationEnd end, TimePoint now) {
  auto it = entries_.find(id);
  if (it == entries_.end() || it->second.reported) return;
  if (id < floor_) {
    // Retired on S (a repair baseline): one ObligationDone, not repeated; the entry goes now.
    once_.emplace_back(id, end);
    entries_.erase(it);
    return;
  }
  Entry& e = it->second;
  e.reported = true;
  e.end = end;
  e.report_due = true;
  e.last_report = now;
  e.waiting = false;
}

std::vector<std::pair<uint64_t, ObligationEnd>> ObligationHolder::TakeReports(TimePoint now) {
  std::vector<std::pair<uint64_t, ObligationEnd>> out = std::exchange(once_, {});
  for (auto& [id, e] : entries_) {
    if (!e.reported) continue;
    if (e.report_due || now - e.last_report >= limits_.obligation_report_interval) {
      out.emplace_back(id, e.end);
      e.report_due = false;
      e.last_report = now;
    }
  }
  return out;
}

std::vector<uint64_t> ObligationHolder::HeldIds() const {
  std::vector<uint64_t> ids;
  for (const auto& [id, e] : entries_) {
    if (!e.reported) ids.push_back(id);
  }
  return ids;
}

ObligationHolder::Entry* ObligationHolder::Find(uint64_t id) {
  auto it = entries_.find(id);
  return it == entries_.end() ? nullptr : &it->second;
}

const ObligationHolder::Entry* ObligationHolder::Find(uint64_t id) const {
  auto it = entries_.find(id);
  return it == entries_.end() ? nullptr : &it->second;
}

uint32_t ObligationHolder::Holding() const { return uint32_t(entries_.size()); }
uint64_t ObligationHolder::Seen() const { return seen_; }
uint64_t ObligationHolder::Floor() const { return floor_; }

void ObligationHolder::Reset() {
  entries_.clear();
  once_.clear();
  seen_ = 0;
  floor_ = 0;
}

}  // namespace skylanders::backend
