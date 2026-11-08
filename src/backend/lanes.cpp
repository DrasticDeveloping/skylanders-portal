#include "backend/lanes.h"

#include <algorithm>
#include <type_traits>

namespace skylanders::backend {

// The variant's order is fixed: TypeOf maps an index to its type number through this table.
static_assert(std::variant_size_v<Message> == 20);
static_assert(std::is_same_v<std::variant_alternative_t<0, Message>, msg::StateHello>);
static_assert(std::is_same_v<std::variant_alternative_t<11, Message>, msg::Ack>);
static_assert(std::is_same_v<std::variant_alternative_t<14, Message>, msg::Request>);
static_assert(std::is_same_v<std::variant_alternative_t<19, Message>, msg::Effect>);

namespace {

constexpr MessageType kTypeOfIndex[] = {
    MessageType::kStateHello,    MessageType::kEpochBegin,     MessageType::kDelta,
    MessageType::kTomb,          MessageType::kThrough,        MessageType::kManifest,
    MessageType::kBaselineBegin, MessageType::kBaselineRows,   MessageType::kBaselineEnd,
    MessageType::kObligation,    MessageType::kObligationAck,  MessageType::kAck,
    MessageType::kObligationDone, MessageType::kMismatch,      MessageType::kRequest,
    MessageType::kLeaseRenew,    MessageType::kLeaseReport,    MessageType::kDecision,
    MessageType::kLeaseEnd,      MessageType::kEffect,
};
static_assert(std::size(kTypeOfIndex) == std::variant_size_v<Message>);

// The message header: type (u16), length (u16).
constexpr size_t kMessageHeader = 4;

struct NamedType {
  MessageType type;
  std::string_view name;
};
constexpr NamedType kTypeNames[] = {
    {MessageType::kStateHello, "state_hello"},
    {MessageType::kEpochBegin, "epoch_begin"},
    {MessageType::kDelta, "delta"},
    {MessageType::kTomb, "tomb"},
    {MessageType::kThrough, "through"},
    {MessageType::kManifest, "manifest"},
    {MessageType::kBaselineBegin, "baseline_begin"},
    {MessageType::kBaselineRows, "baseline_rows"},
    {MessageType::kBaselineEnd, "baseline_end"},
    {MessageType::kObligation, "obligation"},
    {MessageType::kObligationAck, "obligation_ack"},
    {MessageType::kAck, "ack"},
    {MessageType::kObligationDone, "obligation_done"},
    {MessageType::kMismatch, "mismatch"},
    {MessageType::kRequest, "request"},
    {MessageType::kLeaseRenew, "lease_renew"},
    {MessageType::kLeaseReport, "lease_report"},
    {MessageType::kDecision, "decision"},
    {MessageType::kLeaseEnd, "lease_end"},
    {MessageType::kEffect, "effect"},
};

// --- Field writers. Each appends to `out` through `w` (the same vector), failing `w` on a field out
// of bounds, so the caller checks w.Ok() once.

void PutKey(Writer& w, std::vector<uint8_t>& out, const RecordKey& key) {
  if (!w.Ok()) return;
  const size_t before = out.size();
  if (!EncodeKey(key, out)) {
    out.resize(before);
    w.Fail();
  }
}

void PutRecord(Writer& w, std::vector<uint8_t>& out, const Record& record) {
  if (!w.Ok()) return;
  const size_t before = out.size();
  if (!EncodeRecord(record, out)) {
    out.resize(before);
    w.Fail();
  }
}

void PutEntity(Writer& w, std::vector<uint8_t>& out, const EntityId& id) {
  if (!w.Ok()) return;
  const size_t before = out.size();
  if (!Encode(id, out)) {
    out.resize(before);
    w.Fail();
  }
}

void PutBlob(Writer& w, std::span<const uint8_t> bytes, size_t max) {
  if (bytes.size() > max) {
    w.Fail();
    return;
  }
  w.U16(uint16_t(bytes.size()));
  w.Bytes(bytes);
}

void PutRecords(Writer& w, std::vector<uint8_t>& out, const std::vector<Record>& records) {
  if (records.size() > 0xFFFF) {
    w.Fail();
    return;
  }
  w.U16(uint16_t(records.size()));
  for (const Record& r : records) PutRecord(w, out, r);
}

void PutRequestKey(Writer& w, const RequestKey& k) {
  w.U16(k.peer);
  w.U32(k.peer_epoch);
  w.U32(k.generation);
  w.U32(k.sequence);
}

// --- Field readers.

std::vector<uint8_t> GetBlob(Reader& r, size_t max) {
  const uint16_t n = r.U16();
  if (n > max) {
    r.Fail();
    return {};
  }
  auto b = r.Bytes(n);
  return std::vector<uint8_t>(b.begin(), b.end());
}

bool GetRecords(Reader& r, std::vector<Record>& out) {
  const uint16_t n = r.U16();
  if (!r.Ok()) return false;
  out.clear();
  out.reserve(n);
  for (uint16_t i = 0; i < n; ++i) {
    auto rec = DecodeRecord(r);
    if (!rec || !r.Ok()) return false;
    out.push_back(std::move(*rec));
  }
  return true;
}

RequestKey GetRequestKey(Reader& r) {
  RequestKey k;
  k.peer = r.U16();
  k.peer_epoch = r.U32();
  k.generation = r.U32();
  k.sequence = r.U32();
  return k;
}

bool ValidScope(uint8_t v) { return v <= uint8_t(Scope::kLevel); }
bool ValidScopeMask(uint8_t v) { return (v & ~kScopeAll) == 0; }
bool ValidReason(uint8_t v) { return v <= uint8_t(Reason::kKind); }

// --- One message's fields. Each writer appends the fields only (the header is the caller's).

struct FieldWriter {
  Writer& w;
  std::vector<uint8_t>& out;

  void operator()(const msg::StateHello& m) {
    w.U64(m.table_digest);
    w.U32(m.epoch);
    w.U64(m.head);
    w.U16(m.peer);
    w.U32(m.peer_epoch);
  }
  void operator()(const msg::EpochBegin& m) {
    w.U32(m.generation);
    w.U64(m.lsn);
  }
  void operator()(const msg::Delta& m) {
    PutRecord(w, out, m.record);
    w.U64(m.first_lsn);
  }
  void operator()(const msg::Tomb& m) {
    PutKey(w, out, m.key);
    w.U32(m.revision);
    w.U64(m.lsn);
    w.U64(m.first_lsn);
  }
  void operator()(const msg::Through& m) { w.U64(m.lsn); }
  void operator()(const msg::Manifest& m) {
    w.U64(m.lsn);
    w.U8(m.scopes);
    for (uint64_t d : m.digests) w.U64(d);
  }
  void operator()(const msg::BaselineBegin& m) {
    w.U32(m.id);
    w.U64(m.at);
    w.U32(m.epoch);
    w.U8(m.scopes);
    w.U32(m.rows);
    w.U32(m.bytes);
    w.U8(uint8_t(m.reason));
  }
  void operator()(const msg::BaselineRows& m) {
    w.U32(m.id);
    w.U32(m.index);
    PutRecords(w, out, m.records);
  }
  void operator()(const msg::BaselineEnd& m) {
    w.U32(m.id);
    w.U64(m.at);
    w.U64(m.digest);
  }
  void operator()(const msg::Obligation& m) {
    w.U64(m.id);
    w.U64(m.floor);
    w.U8(uint8_t(m.scope));
    w.U64(m.lsn);
    PutKey(w, out, m.key);
    PutBlob(w, m.value, kMaxValueBytes);
  }
  void operator()(const msg::ObligationAck& m) {
    w.U64(m.id);
    w.U64(m.floor);
    w.U64(m.last_sent);
  }
  void operator()(const msg::Ack& m) {
    w.U64(m.prefix);
    w.U64(m.manifest_ok);
    w.U32(m.holding);
    w.U64(m.seen);
  }
  void operator()(const msg::ObligationDone& m) {
    w.U64(m.id);
    w.U8(uint8_t(m.end));
  }
  void operator()(const msg::Mismatch& m) {
    w.U64(m.lsn);
    w.U64(m.bucket_mask);
    if (m.lost) w.U8(1);
  }
  // On the wire: generation and sequence only; S stamps peer and peer epoch from the link (Sol F3).
  // The parent is the same peer, peer epoch and generation, so only its sequence goes.
  void operator()(const msg::Request& m) {
    const backend::Request& q = m.request;
    w.U32(q.key.generation);
    w.U32(q.key.sequence);
    w.U16(q.kind);
    w.U8(uint8_t(q.model));
    w.U8(q.flags);
    const bool has_subject = q.subject.form != IdForm::kNone;
    const bool has_basis = q.basis.kind != 0;
    w.U8(uint8_t((has_subject ? 1 : 0) | (has_basis ? 2 : 0)));
    if (has_subject) PutEntity(w, out, q.subject);
    if (has_basis) PutKey(w, out, q.basis);
    w.U32(q.basis_revision);
    w.U32(q.parent.sequence);
    w.U32(q.precondition);
    PutBlob(w, q.payload, kMaxRequestPayload);
  }
  void operator()(const msg::LeaseRenew& m) { w.U32(m.id); }
  void operator()(const msg::LeaseReport& m) {
    w.U32(m.id);
    PutBlob(w, m.payload, kMaxRequestPayload);
  }
  void operator()(const msg::Decision& m) {
    const backend::Decision& d = m.decision;
    PutRequestKey(w, d.key);
    w.U8(uint8_t(d.outcome));
    w.U8(uint8_t(d.reason));
    w.U8(d.kind_reason);
    w.U8(uint8_t((d.final ? 1 : 0) | (d.cached ? 2 : 0) | (d.server_final ? 4 : 0)));
    w.U64(d.at_lsn);
    w.U8(uint8_t(d.scope));
    w.U32(d.lease);
    w.U32(d.revision);
  }
  void operator()(const msg::LeaseEnd& m) {
    w.U32(m.id);
    w.U8(uint8_t(m.why));
  }
  void operator()(const msg::Effect& m) {
    w.U64(m.tick);
    PutRecords(w, out, m.records);
  }
};

// Reads one message's fields from `r` (exactly the message's bytes). nullopt: short or broken.
std::optional<Message> ReadFields(MessageType type, Reader& r) {
  switch (type) {
    case MessageType::kStateHello: {
      msg::StateHello m;
      m.table_digest = r.U64();
      m.epoch = r.U32();
      m.head = r.U64();
      m.peer = r.U16();
      m.peer_epoch = r.U32();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kEpochBegin: {
      msg::EpochBegin m;
      m.generation = r.U32();
      m.lsn = r.U64();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kDelta: {
      msg::Delta m;
      auto rec = DecodeRecord(r);
      if (!rec) return std::nullopt;
      m.record = std::move(*rec);
      m.first_lsn = r.U64();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kTomb: {
      msg::Tomb m;
      auto key = DecodeKey(r);
      if (!key) return std::nullopt;
      m.key = *key;
      m.revision = r.U32();
      m.lsn = r.U64();
      m.first_lsn = r.U64();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kThrough: {
      msg::Through m;
      m.lsn = r.U64();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kManifest: {
      msg::Manifest m;
      m.lsn = r.U64();
      const uint8_t scopes = r.U8();
      for (uint64_t& d : m.digests) d = r.U64();
      if (!r.Ok() || !ValidScopeMask(scopes)) return std::nullopt;
      m.scopes = scopes;
      return m;
    }
    case MessageType::kBaselineBegin: {
      msg::BaselineBegin m;
      m.id = r.U32();
      m.at = r.U64();
      m.epoch = r.U32();
      const uint8_t scopes = r.U8();
      m.rows = r.U32();
      m.bytes = r.U32();
      const uint8_t reason = r.U8();
      if (!r.Ok() || !ValidScopeMask(scopes) || reason > uint8_t(BaselineReason::kSilence)) {
        return std::nullopt;
      }
      m.scopes = scopes;
      m.reason = BaselineReason(reason);
      return m;
    }
    case MessageType::kBaselineRows: {
      msg::BaselineRows m;
      m.id = r.U32();
      m.index = r.U32();
      if (!r.Ok() || !GetRecords(r, m.records)) return std::nullopt;
      return m;
    }
    case MessageType::kBaselineEnd: {
      msg::BaselineEnd m;
      m.id = r.U32();
      m.at = r.U64();
      m.digest = r.U64();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kObligation: {
      msg::Obligation m;
      m.id = r.U64();
      m.floor = r.U64();
      const uint8_t scope = r.U8();
      m.lsn = r.U64();
      if (!r.Ok() || !ValidScope(scope)) return std::nullopt;
      m.scope = Scope(scope);
      auto key = DecodeKey(r);
      if (!key) return std::nullopt;
      m.key = *key;
      m.value = GetBlob(r, kMaxValueBytes);
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kObligationAck: {
      msg::ObligationAck m;
      m.id = r.U64();
      m.floor = r.U64();
      m.last_sent = r.U64();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kAck: {
      msg::Ack m;
      m.prefix = r.U64();
      m.manifest_ok = r.U64();
      m.holding = r.U32();
      m.seen = r.U64();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kObligationDone: {
      msg::ObligationDone m;
      m.id = r.U64();
      const uint8_t end = r.U8();
      if (!r.Ok() || end > uint8_t(ObligationEnd::kRefused)) return std::nullopt;
      m.end = ObligationEnd(end);
      return m;
    }
    case MessageType::kMismatch: {
      msg::Mismatch m;
      m.lsn = r.U64();
      m.bucket_mask = r.U64();
      if (r.Ok() && r.Remaining() > 0) m.lost = r.U8() != 0;
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kRequest: {
      msg::Request m;
      backend::Request& q = m.request;
      q.key.generation = r.U32();
      q.key.sequence = r.U32();
      q.kind = r.U16();
      const uint8_t model = r.U8();
      q.flags = r.U8();
      const uint8_t present = r.U8();
      if (!r.Ok() || model < uint8_t(Model::kCommit) || model > uint8_t(Model::kOwnerRun) ||
          (present & ~3u) != 0) {
        return std::nullopt;
      }
      q.model = Model(model);
      if (present & 1) {
        auto id = Decode(r);
        if (!id) return std::nullopt;
        q.subject = *id;
      }
      if (present & 2) {
        auto key = DecodeKey(r);
        if (!key) return std::nullopt;
        q.basis = *key;
      }
      q.basis_revision = r.U32();
      const uint32_t parent = r.U32();
      q.precondition = r.U32();
      q.payload = GetBlob(r, kMaxRequestPayload);
      if (!r.Ok()) return std::nullopt;
      if (parent != 0) q.parent = RequestKey{0, 0, q.key.generation, parent};
      return m;
    }
    case MessageType::kLeaseRenew: {
      msg::LeaseRenew m;
      m.id = r.U32();
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kLeaseReport: {
      msg::LeaseReport m;
      m.id = r.U32();
      m.payload = GetBlob(r, kMaxRequestPayload);
      if (!r.Ok()) return std::nullopt;
      return m;
    }
    case MessageType::kDecision: {
      msg::Decision m;
      backend::Decision& d = m.decision;
      d.key = GetRequestKey(r);
      const uint8_t outcome = r.U8();
      const uint8_t reason = r.U8();
      d.kind_reason = r.U8();
      const uint8_t bits = r.U8();
      d.at_lsn = r.U64();
      const uint8_t scope = r.U8();
      d.lease = r.U32();
      d.revision = r.U32();
      if (!r.Ok() || outcome < uint8_t(Outcome::kApplied) || outcome > uint8_t(Outcome::kGranted) ||
          !ValidReason(reason) || (bits & ~7u) != 0 || !ValidScope(scope)) {
        return std::nullopt;
      }
      d.outcome = Outcome(outcome);
      d.reason = Reason(reason);
      d.final = bits & 1;
      d.cached = bits & 2;
      d.server_final = bits & 4;
      d.scope = Scope(scope);
      return m;
    }
    case MessageType::kLeaseEnd: {
      msg::LeaseEnd m;
      m.id = r.U32();
      const uint8_t why = r.U8();
      if (!r.Ok() || why > uint8_t(LeaseEndReason::kRevokedClose)) return std::nullopt;
      m.why = LeaseEndReason(why);
      return m;
    }
    case MessageType::kEffect: {
      msg::Effect m;
      m.tick = r.U64();
      if (!r.Ok() || !GetRecords(r, m.records)) return std::nullopt;
      return m;
    }
  }
  return std::nullopt;
}

bool KnownType(uint16_t type) {
  for (const NamedType& t : kTypeNames) {
    if (uint16_t(t.type) == type) return true;
  }
  return false;
}

}  // namespace

std::string_view BaselineReasonName(BaselineReason why) {
  switch (why) {
    case BaselineReason::kJoin: return "join";
    case BaselineReason::kLevelReady: return "level ready";
    case BaselineReason::kManifestMismatch: return "manifest mismatch";
    case BaselineReason::kDigestMismatch: return "digest mismatch";
    case BaselineReason::kBelowFloor: return "below floor";
    case BaselineReason::kSilence: return "silence";
  }
  return "?";
}

MessageType TypeOf(const Message& m) { return kTypeOfIndex[m.index()]; }

netplay::Lane LaneOf(MessageType type) {
  switch (type) {
    case MessageType::kRequest:
    case MessageType::kLeaseRenew:
    case MessageType::kLeaseReport:
    case MessageType::kDecision:
    case MessageType::kLeaseEnd:
      return netplay::Lane::kGameplay;
    case MessageType::kEffect:
      return netplay::Lane::kFast;
    default:
      return netplay::Lane::kState;
  }
}

bool AppendMessage(const Message& m, std::vector<uint8_t>& frame, size_t limit) {
  const size_t start = frame.size();
  Writer w(frame, std::min(limit, start + kMessageHeader + 0xFFFF));
  w.U16(uint16_t(TypeOf(m)));
  w.U16(0);  // the length, patched below
  if (w.Ok()) std::visit(FieldWriter{w, frame}, m);
  // A record or key appended past the limit by its own encoder fails here too.
  if (!w.Ok() || frame.size() > limit) {
    frame.resize(start);
    return false;
  }
  const size_t length = frame.size() - start - kMessageHeader;
  w.PatchU16(start + 2, uint16_t(length));
  if (!w.Ok()) {
    frame.resize(start);
    return false;
  }
  return true;
}

bool DecodeFrame(std::span<const uint8_t> frame, std::vector<Message>& out) {
  Reader r(frame);
  while (r.Remaining() > 0) {
    if (r.Remaining() < kMessageHeader) return false;
    const uint16_t type = r.U16();
    const uint16_t length = r.U16();
    if (length > r.Remaining()) return false;
    std::span<const uint8_t> body = r.Bytes(length);
    if (!r.Ok()) return false;
    if (!KnownType(type)) continue;  // skipped by its length
    Reader fields(body);
    auto m = ReadFields(MessageType(type), fields);
    if (!m) return false;
    out.push_back(std::move(*m));  // bytes past the fields it knows are ignored
  }
  return true;
}

std::string_view MessageTypeName(MessageType type) {
  for (const NamedType& t : kTypeNames) {
    if (t.type == type) return t.name;
  }
  return "?";
}

std::optional<MessageType> MessageTypeFromName(std::string_view name) {
  for (const NamedType& t : kTypeNames) {
    if (t.name == name) return t.type;
  }
  return std::nullopt;
}

std::string_view LaneName(netplay::Lane lane) {
  switch (lane) {
    case netplay::Lane::kGameplay: return "gameplay";
    case netplay::Lane::kState: return "state";
    case netplay::Lane::kFast: return "fast";
  }
  return "?";
}

std::optional<netplay::Lane> LaneFromName(std::string_view name) {
  if (name == "gameplay") return netplay::Lane::kGameplay;
  if (name == "state") return netplay::Lane::kState;
  if (name == "fast") return netplay::Lane::kFast;
  return std::nullopt;
}

// --- PeerQueue ---

PeerQueue::PeerQueue(const Limits& limits) : limits_(limits) {}

PeerQueue::LaneQueue& PeerQueue::Of(netplay::Lane lane) {
  return lanes_[size_t(lane) % netplay::kLaneCount];
}
const PeerQueue::LaneQueue& PeerQueue::Of(netplay::Lane lane) const {
  return lanes_[size_t(lane) % netplay::kLaneCount];
}

bool PeerQueue::Push(const Message& m, TimePoint now) {
  LaneQueue& q = Of(LaneOf(TypeOf(m)));
  if (!q.frames.empty() && AppendMessage(m, q.frames.back().bytes)) {
    ++q.frames.back().messages;
    return true;
  }
  // The last frame is full (or there is none): a new one, unless the message fits no frame.
  Frame f;
  f.opened = now;
  if (!AppendMessage(m, f.bytes)) return false;
  f.messages = 1;
  q.frames.push_back(std::move(f));
  return true;
}

PeerQueue::FlushResult PeerQueue::Flush(PeerLink& link, TimePoint now, bool now_all) {
  FlushResult result;
  for (size_t i = 0; i < netplay::kLaneCount; ++i) {
    const auto lane = netplay::Lane(i);
    LaneQueue& q = lanes_[i];
    const bool was_blocked = q.held || q.capped;
    q.held = false;
    q.capped = false;
    while (!q.frames.empty()) {
      Frame& head = q.frames.front();
      // The last frame of the state lane is still filling: it goes once it is a batch old.
      if (lane == netplay::Lane::kState && !now_all && q.frames.size() == 1 &&
          now - head.opened < limits_.batch_interval) {
        break;
      }
      if (lane == netplay::Lane::kState &&
          link.UnackedFrames(lane) >= limits_.max_unacked_state_frames) {
        q.capped = true;
        result.capped = true;
        break;
      }
      const LinkSend sent = link.Send(lane, head.bytes);
      if (sent == LinkSend::kSent) {
        ++result.frames_sent;
        q.frames.pop_front();
        continue;
      }
      if (sent == LinkSend::kFull) {
        q.held = true;
        result.held = true;
        break;
      }
      if (sent == LinkSend::kTooBig) {
        // Never: Push keeps frames at kMaxFrameBytes. Dropping it is the only way past it.
        q.frames.pop_front();
        continue;
      }
      // kGone: the peer is gone; nothing here will ever go.
      Clear();
      result.gone = true;
      return result;
    }
    if (!was_blocked && (q.held || q.capped)) {
      result.backlog_began = true;
    }
  }
  if (result.backlog_began) ++backlog_events_;
  return result;
}

size_t PeerQueue::Room(const PeerLink& link) const {
  const LaneQueue& q = Of(netplay::Lane::kState);
  if (q.held) return 0;
  const size_t used = link.UnackedFrames(netplay::Lane::kState) + q.frames.size();
  return used >= limits_.max_unacked_state_frames ? 0 : limits_.max_unacked_state_frames - used;
}

size_t PeerQueue::Frames(netplay::Lane lane) const { return Of(lane).frames.size(); }

size_t PeerQueue::Frames() const {
  size_t n = 0;
  for (const LaneQueue& q : lanes_) n += q.frames.size();
  return n;
}

size_t PeerQueue::Messages(netplay::Lane lane) const {
  size_t n = 0;
  for (const Frame& f : Of(lane).frames) n += f.messages;
  return n;
}

size_t PeerQueue::Bytes(netplay::Lane lane) const {
  size_t n = 0;
  for (const Frame& f : Of(lane).frames) n += f.bytes.size();
  return n;
}

size_t PeerQueue::Held() const {
  size_t n = 0;
  for (const LaneQueue& q : lanes_) {
    if (q.held) n += q.frames.size();
  }
  return n;
}

bool PeerQueue::Blocked(netplay::Lane lane) const {
  const LaneQueue& q = Of(lane);
  return q.held || q.capped;
}

uint64_t PeerQueue::BacklogEvents() const { return backlog_events_; }

void PeerQueue::Clear() {
  for (LaneQueue& q : lanes_) {
    q.frames.clear();
    q.held = false;
    q.capped = false;
  }
}

// --- Pacer ---

Pacer::Pacer(const Limits& limits, TimePoint now)
    : rate_(double(limits.baseline_bytes_per_second)),
      capacity_(std::max(double(kMaxFrameBytes),
                         double(limits.baseline_bytes_per_second) *
                             std::chrono::duration<double>(limits.batch_interval).count())),
      tokens_(capacity_),
      last_(now) {}

void Pacer::Refill(TimePoint now) {
  if (now <= last_) return;
  tokens_ = std::min(capacity_, tokens_ + rate_ * std::chrono::duration<double>(now - last_).count());
  last_ = now;
}

bool Pacer::Take(size_t bytes) {
  // A chunk larger than the bucket goes when the bucket is full, and leaves it empty.
  const double need = std::min(double(bytes), capacity_);
  if (tokens_ + 1e-9 < need) return false;
  tokens_ = std::max(0.0, tokens_ - double(bytes));
  return true;
}

size_t Pacer::Available() const { return size_t(tokens_); }

size_t Pacer::Capacity() const { return size_t(capacity_); }

void Pacer::Reset(TimePoint now) {
  tokens_ = capacity_;
  last_ = now;
}

}  // namespace skylanders::backend
