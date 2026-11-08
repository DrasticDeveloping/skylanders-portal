#include "backend/base.h"

namespace skylanders::backend {

std::string_view PeerRoleName(PeerRole role) {
  switch (role) {
    case PeerRole::kServer: return "server";
    case PeerRole::kOwner: return "owner";
    case PeerRole::kGuest: return "guest";
  }
  return "unknown";
}

std::string_view ReasonName(Reason reason) {
  switch (reason) {
    case Reason::kNone: return "none";
    case Reason::kStaleGeneration: return "stale generation";
    case Reason::kSessionEnded: return "session ended";
    case Reason::kRate: return "rate";
    case Reason::kLimit: return "limit";
    case Reason::kStaleBasis: return "stale basis";
    case Reason::kLeased: return "leased";
    case Reason::kLeaseExpired: return "lease expired";
    case Reason::kNotHolder: return "not holder";
    case Reason::kModel: return "model";
    case Reason::kParentRefused: return "parent refused";
    case Reason::kParentMissing: return "parent missing";
    case Reason::kClosedByChild: return "closed by child";
    case Reason::kPreconditionMissing: return "precondition missing";
    case Reason::kUnresolved: return "unresolved";
    case Reason::kUnsupported: return "unsupported";
    case Reason::kKind: return "kind";
  }
  return "unknown";
}

// --- Writer ---

Writer::Writer(std::vector<uint8_t>& out, size_t limit) : out_(out), limit_(limit) {
  // A vector already past the limit leaves no room: the first write fails.
  if (out_.size() > limit_) failed_ = true;
}

void Writer::Le(uint64_t v, int n) {
  if (failed_ || Room() < size_t(n)) {
    failed_ = true;
    return;
  }
  for (int i = 0; i < n; ++i) out_.push_back(uint8_t(v >> (8 * i)));
}

void Writer::U8(uint8_t v) { Le(v, 1); }
void Writer::U16(uint16_t v) { Le(v, 2); }
void Writer::U32(uint32_t v) { Le(v, 4); }
void Writer::U64(uint64_t v) { Le(v, 8); }

void Writer::Bytes(std::span<const uint8_t> b) {
  if (failed_ || Room() < b.size()) {
    failed_ = true;
    return;
  }
  out_.insert(out_.end(), b.begin(), b.end());
}

void Writer::Bytes(std::string_view s) {
  Bytes(std::span<const uint8_t>(reinterpret_cast<const uint8_t*>(s.data()), s.size()));
}

void Writer::PatchU16(size_t offset, uint16_t v) {
  if (failed_ || offset + 2 > out_.size()) {
    failed_ = true;
    return;
  }
  out_[offset] = uint8_t(v);
  out_[offset + 1] = uint8_t(v >> 8);
}

size_t Writer::Size() const { return out_.size(); }

size_t Writer::Room() const { return out_.size() >= limit_ ? 0 : limit_ - out_.size(); }

bool Writer::Ok() const { return !failed_; }

void Writer::Fail() { failed_ = true; }

// --- Reader ---

Reader::Reader(std::span<const uint8_t> in) : in_(in) {}

uint64_t Reader::Le(int n) {
  if (failed_ || Remaining() < size_t(n)) {
    failed_ = true;
    return 0;
  }
  uint64_t v = 0;
  for (int i = 0; i < n; ++i) v |= uint64_t(in_[pos_ + size_t(i)]) << (8 * i);
  pos_ += size_t(n);
  return v;
}

uint8_t Reader::U8() { return uint8_t(Le(1)); }
uint16_t Reader::U16() { return uint16_t(Le(2)); }
uint32_t Reader::U32() { return uint32_t(Le(4)); }
uint64_t Reader::U64() { return Le(8); }

std::span<const uint8_t> Reader::Bytes(size_t n) {
  if (failed_ || Remaining() < n) {
    failed_ = true;
    return {};
  }
  std::span<const uint8_t> out = in_.subspan(pos_, n);
  pos_ += n;
  return out;
}

void Reader::Skip(size_t n) {
  if (failed_ || Remaining() < n) {
    failed_ = true;
    pos_ = in_.size();
    return;
  }
  pos_ += n;
}

size_t Reader::Remaining() const { return in_.size() - pos_; }

size_t Reader::Position() const { return pos_; }

bool Reader::Ok() const { return !failed_; }

void Reader::Fail() { failed_ = true; }

}  // namespace skylanders::backend
