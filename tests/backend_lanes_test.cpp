// The lanes (design section 3.7; work item 3): the message codec, the batcher's FIFO with its hold
// on kFull and the backlog cap in frames, the pacer, and the two PeerLinks of fake_network.h. These
// have no ids in the design's test list (section 10 names the cases that run over them: LM1 to
// LM3, LM8, PP6, ...); they carry this file's own ids, LN1 to LN10.

#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <functional>
#include <memory>
#include <span>
#include <variant>
#include <vector>

#include "backend/fake_network.h"
#include "backend/lanes.h"
#include "backend/limits.h"
#include "backend/record.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"
#include "netplay/fake_network.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using test::lanes::Ent;
using test::lanes::Key;
using test::lanes::kStart;
using test::lanes::Val;

Record Row(KindId kind, uint32_t n, uint32_t revision, std::vector<uint8_t> value) {
  Record r;
  r.key = Key(kind, Ent(1, n));
  r.revision = revision;
  r.epoch = 1;
  r.lsn = 100 + n;
  r.value = std::move(value);
  return r;
}

// One of every message, with every field set to something other than its default.
std::vector<Message> EveryMessage() {
  std::vector<Message> all;
  all.push_back(msg::StateHello{0x1122334455667788ull, 3, 77, 2, 5});
  all.push_back(msg::EpochBegin{4, 78});
  all.push_back(msg::Delta{Row(test::kProp, 1, 9, Val(7, 8)), 70});
  all.push_back(msg::Tomb{Key(test::kToggle, Ent(1, 2)), 4, 79, 71});
  all.push_back(msg::Through{80});
  msg::Manifest manifest;
  manifest.lsn = 81;
  manifest.scopes = kScopeAll;
  for (size_t i = 0; i < manifest.digests.size(); ++i) manifest.digests[i] = i * 0x0101010101ull;
  all.push_back(manifest);
  all.push_back(msg::BaselineBegin{6, 82, 3, kScopeLevel, 120, 9000, BaselineReason::kDigestMismatch});
  msg::BaselineRows rows;
  rows.id = 6;
  rows.index = 2;
  rows.records = {Row(test::kToggle, 3, 1, Val(1, 1)), Row(test::kGate, 4, 2, Val(2, 1))};
  Record tomb = Row(test::kProp, 5, 3, {});
  tomb.tomb = true;
  rows.records.push_back(tomb);
  all.push_back(rows);
  all.push_back(msg::BaselineEnd{6, 82, 0xABCDEF0123456789ull});
  all.push_back(msg::Obligation{12, 9, Scope::kLevel, 83, Key(test::kEvent, Ent(1, 6)), Val(5, 6)});
  all.push_back(msg::ObligationAck{12, 10, 14});
  all.push_back(msg::Ack{80, 81, 3, 14});
  all.push_back(msg::ObligationDone{12, ObligationEnd::kRefused});
  all.push_back(msg::Mismatch{81, 0x8000000000000001ull});
  msg::Request request;
  request.request.key = RequestKey{0, 0, 3, 17};
  request.request.kind = test::kProp;
  request.request.model = Model::kOwnerRun;
  request.request.subject = Ent(3, 7);
  request.request.basis = Key(test::kProp, Ent(3, 7));
  request.request.basis_revision = 4;
  request.request.parent = RequestKey{0, 0, 3, 16};
  request.request.flags = kClosesParent;
  request.request.precondition = 22;
  request.request.payload = Val(0x01020304, 12);
  all.push_back(request);
  all.push_back(msg::LeaseRenew{31});
  all.push_back(msg::LeaseReport{31, Val(9, 20)});
  msg::Decision decision;
  decision.decision.key = RequestKey{2, 5, 3, 17};
  decision.decision.outcome = Outcome::kRefused;
  decision.decision.reason = Reason::kKind;
  decision.decision.kind_reason = 7;
  decision.decision.final = true;
  decision.decision.cached = true;
  decision.decision.server_final = true;
  decision.decision.at_lsn = 84;
  decision.decision.scope = Scope::kSession;
  decision.decision.lease = 31;
  decision.decision.revision = 6;
  all.push_back(decision);
  all.push_back(msg::LeaseEnd{31, LeaseEndReason::kReportRefused});
  all.push_back(msg::Effect{99, {Row(test::kProp, 8, 1, Val(3, 2))}});
  return all;
}

std::vector<uint8_t> Encoded(const Message& m) {
  std::vector<uint8_t> f;
  CHECK(AppendMessage(m, f));
  return f;
}

// A link a test scripts: what each Send answers, and how many frames count as unacked.
class ScriptLink final : public PeerLink {
 public:
  std::function<LinkSend(netplay::Lane, std::span<const uint8_t>)> answer;
  std::vector<std::pair<netplay::Lane, std::vector<uint8_t>>> sent;
  std::array<size_t, netplay::kLaneCount> unacked{};

  LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) override {
    const LinkSend r = answer ? answer(lane, frame) : LinkSend::kSent;
    if (r == LinkSend::kSent) sent.emplace_back(lane, std::vector<uint8_t>(frame.begin(), frame.end()));
    return r;
  }
  bool Receive(netplay::Lane&, std::vector<uint8_t>&) override { return false; }
  size_t UnackedFrames(netplay::Lane lane) const override { return unacked[size_t(lane)]; }
  std::chrono::microseconds Rtt() const override { return 0us; }
  bool Connected() const override { return true; }
};

// The messages of every frame `link` was sent on `lane`, in order.
std::vector<Message> Sent(const ScriptLink& link, netplay::Lane lane) {
  std::vector<Message> out;
  for (const auto& [l, f] : link.sent) {
    if (l != lane) continue;
    CHECK(DecodeFrame(f, out));
  }
  return out;
}

}  // namespace

BACKEND_TEST("LN1", LanesCodecRoundTrip) {
  // Every message in one frame or more; decoded, each encodes to the same bytes again, and the
  // fields the wire drops come back as the design says (a Request's peer and peer epoch are 0, S
  // stamps them; its parent keeps the generation).
  const std::vector<Message> all = EveryMessage();
  std::vector<std::vector<uint8_t>> frames(1);
  for (const Message& m : all) {
    if (!AppendMessage(m, frames.back())) {
      frames.emplace_back();
      CHECK(AppendMessage(m, frames.back()));
    }
  }
  std::vector<Message> back;
  for (const auto& f : frames) {
    CHECK(f.size() <= kMaxFrameBytes);
    CHECK(DecodeFrame(f, back));
  }
  CHECK(back.size() == all.size());
  for (size_t i = 0; i < back.size() && i < all.size(); ++i) {
    CHECK(TypeOf(back[i]) == TypeOf(all[i]));
    CHECK(Encoded(back[i]) == Encoded(all[i]));
  }
  for (const Message& m : back) {
    if (auto* q = std::get_if<msg::Request>(&m)) {
      CHECK(q->request.key.peer == 0);
      CHECK(q->request.key.peer_epoch == 0);
      CHECK(q->request.key.generation == 3);
      CHECK(q->request.key.sequence == 17);
      CHECK(q->request.parent == (RequestKey{0, 0, 3, 16}));
      CHECK(q->request.subject == Ent(3, 7));
      CHECK(q->request.payload == Val(0x01020304, 12));
    }
    if (auto* d = std::get_if<msg::Decision>(&m)) {
      CHECK(d->decision.key == (RequestKey{2, 5, 3, 17}));
      CHECK(d->decision.final && d->decision.cached && d->decision.server_final);
      CHECK(d->decision.scope == Scope::kSession);
    }
    if (auto* b = std::get_if<msg::BaselineRows>(&m)) {
      CHECK(b->records.size() == 3);
      if (b->records.size() == 3) CHECK(b->records[2].tomb);
    }
  }
  // A request with no subject, no basis and no parent.
  msg::Request bare;
  bare.request.key = RequestKey{0, 0, 1, 1};
  bare.request.kind = test::kToggle;
  std::vector<Message> one;
  CHECK(DecodeFrame(Encoded(bare), one));
  CHECK(one.size() == 1);
  if (one.size() == 1) {
    auto& q = std::get<msg::Request>(one[0]).request;
    CHECK(q.subject.form == IdForm::kNone);
    CHECK(q.basis.kind == 0);
    CHECK(q.parent.sequence == 0);
  }
}

BACKEND_TEST("LN2", LanesCodecSkipsUnknownAndTrailing) {
  // An unknown type is skipped by its length; bytes past the fields a reader knows are ignored
  // (the session's convention, protocol.h).
  std::vector<uint8_t> f = Encoded(msg::Through{5});
  f.insert(f.end(), {0xE7, 0x03, 3, 0, 1, 2, 3});  // type 999, length 3
  std::vector<uint8_t> longer = Encoded(msg::EpochBegin{2, 6});
  longer[2] = uint8_t(longer[2] + 2);  // the length, two bytes more
  longer.push_back(0xAA);
  longer.push_back(0xBB);
  f.insert(f.end(), longer.begin(), longer.end());
  std::vector<Message> out;
  CHECK(DecodeFrame(f, out));
  CHECK(out.size() == 2);
  if (out.size() == 2) {
    CHECK(std::get<msg::Through>(out[0]).lsn == 5);
    CHECK(std::get<msg::EpochBegin>(out[1]).generation == 2);
    CHECK(std::get<msg::EpochBegin>(out[1]).lsn == 6);
  }
}

BACKEND_TEST("LN3", LanesCodecBrokenEndsFrame) {
  // A short or broken message ends the frame (false); the messages before it stay.
  std::vector<uint8_t> f = Encoded(msg::Through{5});
  std::vector<uint8_t> ack = Encoded(msg::Ack{1, 2, 3, 4});
  f.insert(f.end(), ack.begin(), ack.end() - 3);  // cut inside the length it claims
  std::vector<Message> out;
  CHECK(!DecodeFrame(f, out));
  CHECK(out.size() == 1);
  // A field out of range: an ObligationDone whose end is not one of the five.
  std::vector<uint8_t> done = Encoded(msg::ObligationDone{3, ObligationEnd::kCompleted});
  done.back() = 9;
  out.clear();
  CHECK(!DecodeFrame(done, out));
  CHECK(out.empty());
  // A header with no room for its length.
  const std::vector<uint8_t> stub{5, 0};
  out.clear();
  CHECK(!DecodeFrame(stub, out));
}

BACKEND_TEST("LN4", LanesCodecBounds) {
  // A value over kMaxValueBytes, a payload over kMaxRequestPayload: refused, nothing appended.
  std::vector<uint8_t> f = Encoded(msg::Through{1});
  const size_t before = f.size();
  CHECK(!AppendMessage(msg::Delta{Row(test::kProp, 1, 1, std::vector<uint8_t>(kMaxValueBytes + 1)), 1}, f));
  msg::LeaseReport report{1, std::vector<uint8_t>(kMaxRequestPayload + 1)};
  CHECK(!AppendMessage(report, f));
  CHECK(f.size() == before);
  // At the frame's limit: a message that would pass it is refused whole.
  std::vector<uint8_t> frame;
  size_t n = 0;
  while (AppendMessage(msg::Delta{Row(test::kProp, uint32_t(n + 1), 1, Val(1, 64)), 1}, frame)) ++n;
  CHECK(n > 1);
  CHECK(frame.size() <= kMaxFrameBytes);
  std::vector<Message> out;
  CHECK(DecodeFrame(frame, out));
  CHECK(out.size() == n);
  // A value of exactly kMaxValueBytes fits one frame (limits.h: one delta with its key).
  std::vector<uint8_t> big;
  CHECK(AppendMessage(msg::Delta{Row(test::kProp, 1, 1, std::vector<uint8_t>(kMaxValueBytes)), 1}, big));
  // Lanes and names.
  CHECK(LaneOf(MessageType::kDelta) == netplay::Lane::kState);
  CHECK(LaneOf(MessageType::kAck) == netplay::Lane::kState);
  CHECK(LaneOf(MessageType::kRequest) == netplay::Lane::kGameplay);
  CHECK(LaneOf(MessageType::kDecision) == netplay::Lane::kGameplay);
  CHECK(LaneOf(MessageType::kLeaseEnd) == netplay::Lane::kGameplay);
  CHECK(LaneOf(MessageType::kEffect) == netplay::Lane::kFast);
  for (const Message& m : EveryMessage()) {
    const MessageType t = TypeOf(m);
    CHECK(MessageTypeFromName(MessageTypeName(t)) == t);
  }
  CHECK(!MessageTypeFromName("deltas"));
  CHECK(LaneFromName(LaneName(netplay::Lane::kFast)) == netplay::Lane::kFast);
  CHECK(BaselineReasonName(BaselineReason::kLevelReady) == "level ready");
}

BACKEND_TEST("LN5", PeerQueueHoldsOnFull) {
  // A frame the link answers kFull stays at the head; nothing behind it goes first; it is never
  // dropped; one backlog event for the whole hold (Sol F1, LM2).
  Limits limits;
  PeerQueue q(limits);
  ScriptLink link;
  bool full = true;
  link.answer = [&](netplay::Lane, std::span<const uint8_t>) {
    return full ? LinkSend::kFull : LinkSend::kSent;
  };
  TimePoint now = kStart;
  for (uint32_t i = 1; i <= 40; ++i) {
    CHECK(q.Push(msg::Delta{Row(test::kProp, i, 1, Val(i, 64)), i}, now));
  }
  CHECK(q.Push(msg::Through{40}, now));
  CHECK(q.Frames(netplay::Lane::kState) > 1);
  uint64_t began = 0;
  for (int i = 0; i < 20; ++i) {
    now += 100ms;
    auto r = q.Flush(link, now);
    CHECK(r.frames_sent == 0);
    CHECK(r.held);
    began += r.backlog_began;
    CHECK(q.Room(link) == 0);
  }
  CHECK(began == 1);
  CHECK(q.BacklogEvents() == 1);
  CHECK(q.Held() == q.Frames(netplay::Lane::kState));
  full = false;
  now += 100ms;
  auto r = q.Flush(link, now);
  CHECK(!r.held);
  CHECK(q.Frames(netplay::Lane::kState) == 0);
  const std::vector<Message> got = Sent(link, netplay::Lane::kState);
  CHECK(got.size() == 41);
  for (size_t i = 0; i + 1 < got.size(); ++i) {
    CHECK(std::get<msg::Delta>(got[i]).first_lsn == i + 1);
  }
  if (!got.empty()) CHECK(std::holds_alternative<msg::Through>(got.back()));
}

BACKEND_TEST("LN6", PeerQueueBatchInterval) {
  // The state lane's last, partly filled frame goes once it is a batch interval old; a full frame
  // at the next Flush; the gameplay lane at every Flush.
  Limits limits;
  PeerQueue q(limits);
  ScriptLink link;
  TimePoint now = kStart;
  CHECK(q.Push(msg::Through{1}, now));
  msg::Decision d;
  d.decision.key = RequestKey{1, 1, 1, 1};
  d.decision.outcome = Outcome::kApplied;
  CHECK(q.Push(d, now));
  auto r = q.Flush(link, now);
  CHECK(r.frames_sent == 1);  // the decision
  CHECK(q.Frames(netplay::Lane::kState) == 1);
  r = q.Flush(link, now + limits.batch_interval - 1ms);
  CHECK(r.frames_sent == 0);
  r = q.Flush(link, now + limits.batch_interval);
  CHECK(r.frames_sent == 1);
  // Enough deltas for two frames: the first (full) goes at once, the second waits.
  now += 1s;
  // (Pushed until a second frame opens: how many fit in one depends on the codec's row size.)
  for (uint32_t i = 1; i <= 40 && q.Frames(netplay::Lane::kState) < 2; ++i) {
    CHECK(q.Push(msg::Delta{Row(test::kProp, i, 1, Val(i, 64)), i}, now));
  }
  CHECK(q.Frames(netplay::Lane::kState) == 2);
  r = q.Flush(link, now);
  CHECK(r.frames_sent == 1);
  CHECK(q.Frames(netplay::Lane::kState) == 1);
  r = q.Flush(link, now, /*now_all=*/true);
  CHECK(r.frames_sent == 1);
  CHECK(q.Messages(netplay::Lane::kState) == 0);
}

BACKEND_TEST("LN7", PeerQueueBacklogCap) {
  // The state lane stops while the link has max_unacked_state_frames frames unacked (frames, not
  // bytes: Opus F10); Room() says how many more the walk may queue.
  Limits limits;
  limits.max_unacked_state_frames = 4;
  PeerQueue q(limits);
  ScriptLink link;
  link.answer = [&](netplay::Lane lane, std::span<const uint8_t>) {
    ++link.unacked[size_t(lane)];
    return LinkSend::kSent;
  };
  TimePoint now = kStart;
  CHECK(q.Room(link) == 4);
  for (uint32_t i = 1; i <= 120; ++i) q.Push(msg::Delta{Row(test::kProp, i, 1, Val(i, 64)), i}, now);
  const size_t frames = q.Frames(netplay::Lane::kState);
  CHECK(frames > 4);
  CHECK(q.Room(link) == 0);
  auto r = q.Flush(link, now, true);
  CHECK(r.frames_sent == 4);
  CHECK(r.capped);
  CHECK(r.backlog_began);
  CHECK(link.unacked[size_t(netplay::Lane::kState)] == 4);
  r = q.Flush(link, now, true);
  CHECK(r.frames_sent == 0);
  CHECK(!r.backlog_began);  // still capped: no second event
  link.unacked[size_t(netplay::Lane::kState)] = 1;  // three acked
  r = q.Flush(link, now, true);
  CHECK(r.frames_sent == 3);
  CHECK(q.Frames(netplay::Lane::kState) == frames - 7);
  // kGone clears the queue.
  link.answer = [](netplay::Lane, std::span<const uint8_t>) { return LinkSend::kGone; };
  link.unacked.fill(0);
  r = q.Flush(link, now, true);
  CHECK(r.gone);
  CHECK(q.Frames(netplay::Lane::kState) == 0);
}

BACKEND_TEST("LN8", PacerKeepsTheRate) {
  // Baseline bytes at baseline_bytes_per_second within one batch (LM8).
  Limits limits;
  TimePoint now = kStart;
  Pacer pacer(limits, now);
  CHECK(pacer.Capacity() >= kMaxFrameBytes);
  size_t taken = 0;
  const TimePoint end = now + 5s;
  while (now < end) {
    pacer.Refill(now);
    while (pacer.Take(kMaxFrameBytes)) taken += kMaxFrameBytes;
    now += 10ms;
    const double t = std::chrono::duration<double>(now - kStart).count();
    CHECK(double(taken) <= limits.baseline_bytes_per_second * t + pacer.Capacity() + kMaxFrameBytes);
  }
  const double rate = double(taken) / 5.0;
  CHECK(rate <= limits.baseline_bytes_per_second * 1.05 + pacer.Capacity() / 5.0);
  CHECK(rate >= limits.baseline_bytes_per_second * 0.9);
  pacer.Reset(now);
  CHECK(pacer.Available() == pacer.Capacity());
}

BACKEND_TEST("LN9", MemoryLinkCountsUnacked) {
  // Frames sent and not yet taken, plus those taken less than the ack delay ago (design 3.12);
  // kFull at netplay's window; kGone after a disconnect.
  TimePoint now = kStart;
  MemoryLinkOptions o;
  o.one_way = 50ms;
  o.ack_delay = 20ms;
  auto [s, c] = MemoryLink::Pair(&now, o);
  const std::vector<uint8_t> frame = Encoded(msg::Through{1});
  for (int i = 0; i < 3; ++i) CHECK(s->Send(netplay::Lane::kState, frame) == LinkSend::kSent);
  CHECK(s->UnackedFrames(netplay::Lane::kState) == 3);
  CHECK(s->UnackedFrames(netplay::Lane::kGameplay) == 0);
  netplay::Lane lane;
  std::vector<uint8_t> got;
  CHECK(!c->Receive(lane, got));  // not due yet
  now += 50ms;
  CHECK(c->Receive(lane, got));
  CHECK(lane == netplay::Lane::kState);
  CHECK(got == frame);
  CHECK(s->UnackedFrames(netplay::Lane::kState) == 3);  // taken, not acked yet
  now += 20ms;
  CHECK(s->UnackedFrames(netplay::Lane::kState) == 2);
  CHECK(s->Rtt() == std::chrono::microseconds(120000));
  for (size_t i = 0; i < netplay::kLaneWindow; ++i) s->Send(netplay::Lane::kGameplay, frame);
  CHECK(s->Send(netplay::Lane::kGameplay, frame) == LinkSend::kFull);
  CHECK(s->Send(netplay::Lane::kState, std::vector<uint8_t>(kMaxFrameBytes + 1)) == LinkSend::kTooBig);
  s->Disconnect();
  CHECK(!c->Connected());
  CHECK(c->Send(netplay::Lane::kState, frame) == LinkSend::kGone);
  CHECK(!c->Receive(lane, got));
}

BACKEND_TEST("LN10", NetplayLinkOrderedThroughLoss) {
  // NetplayLink on netplay's Connection over its FakeNetwork: 30 % loss, duplicates and
  // reordering; the state lane delivers every frame once, in order, and UnackedFrames is the
  // Connection's (Opus F10).
  netplay::FakeNetwork net(7, kStart);
  netplay::LinkConditions bad;
  bad.loss_percent = 30;
  bad.duplicate_percent = 10;
  bad.reorder_percent = 20;
  bad.delay_ms = 40;
  bad.jitter_ms = 20;
  net.SetDefault(bad);
  const netplay::Endpoint a{0x0A000001, 5001}, b{0x0A000101, 4000};
  NetplayLink s(net, a, b), c(net, b, a);
  constexpr uint64_t kCount = 300;
  uint64_t sent = 0, next = 1;
  size_t max_unacked = 0;
  for (int step = 0; step < 20000 && next <= kCount; ++step) {
    if (sent < kCount && s.Send(netplay::Lane::kState, Encoded(msg::Through{sent + 1})) == LinkSend::kSent) {
      ++sent;
    }
    max_unacked = std::max(max_unacked, s.UnackedFrames(netplay::Lane::kState));
    s.Service();
    c.Service();
    netplay::Lane lane;
    std::vector<uint8_t> frame;
    while (c.Receive(lane, frame)) {
      std::vector<Message> m;
      CHECK(DecodeFrame(frame, m));
      CHECK(m.size() == 1);
      if (m.size() == 1) {
        CHECK(std::get<msg::Through>(m[0]).lsn == next);
        ++next;
      }
    }
    net.Advance(1ms);
  }
  CHECK(next == kCount + 1);
  CHECK(max_unacked > 0);
  for (int step = 0; step < 5000 && s.UnackedFrames(netplay::Lane::kState) > 0; ++step) {
    s.Service();
    c.Service();
    net.Advance(1ms);
  }
  CHECK(s.UnackedFrames(netplay::Lane::kState) == 0);
  CHECK(s.Rtt() > 0us);
}

}  // namespace skylanders::backend
