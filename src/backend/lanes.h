#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <span>
#include <string_view>
#include <variant>
#include <vector>

#include "backend/base.h"
#include "backend/lease.h"
#include "backend/limits.h"
#include "backend/obligation.h"
#include "backend/record.h"
#include "backend/request.h"
#include "netplay/datagram.h"

// Lanes on a transport contract (design section 3.7): PeerLink, the backend's messages and their
// codec. The batcher (one FIFO per peer and lane; a frame the link answers kFull stays at the head,
// never dropped, Sol F1) and the pacer (a token bucket per peer for baseline bytes; deltas stop at
// limits.max_unacked_state_frames, Opus F10) are PeerQueue and Pacer below; the Server owns one of
// each per peer (item 3).
//
// Request, Decision, ObligationEnd and LeaseEndReason are carried on the wire, so this header
// includes request.h, obligation.h and lease.h (the design's table 3.1 lists record only).

namespace skylanders::backend {

enum class LinkSend : uint8_t { kSent, kFull, kTooBig, kGone };

// One remote peer. Gameplay and state are reliable and ordered; fast keeps only the newest
// (netplay's contract, lanes.h). Implementations: MemoryLink (tests), NetplayLink (tests, on
// netplay::Connection over netplay::FakeNetwork), the session's (stage 3), and the injector's
// decorator around any of them. One Send is one frame is one datagram. Server::Pump and
// Client::Pump call Receive until it returns false, every pump: the backend never leaves frames in
// the link, because netplay's Connection acks a datagram on arrival and keeps what was not taken
// without a bound, and stage 3's backend frames share the session's lanes (Sol code review r3, N1).
class PeerLink {
 public:
  virtual ~PeerLink() = default;
  virtual LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) = 0;  // <= kMaxFrameBytes
  virtual bool Receive(netplay::Lane& lane, std::vector<uint8_t>& frame) = 0;
  // Frames sent on `lane` and not yet acked: netplay's Connection::Unacked, which counts datagrams
  // (Opus F10).
  virtual size_t UnackedFrames(netplay::Lane lane) const = 0;
  virtual std::chrono::microseconds Rtt() const = 0;
  virtual bool Connected() const = 0;
};

// Why a baseline runs (BaselineBegin's reason; section 3.5 step 1, D5).
enum class BaselineReason : uint8_t {
  kJoin,              // the session scope at join
  kLevelReady,        // the level scope at level ready
  kManifestMismatch,  // repair
  kDigestMismatch,    // repair: a failed baseline digest (Mismatch with bucket mask 0; Sol F6)
  kBelowFloor,        // repair: the cursor is below the log's floor
  kSilence,           // repair: no baseline ended and no manifest matched in silence_baseline
};
std::string_view BaselineReasonName(BaselineReason why);

// The messages. A frame holds one or more, in the session's convention: type (u16), length (u16),
// fields little-endian; a reader skips unknown types by their length and ignores bytes past the
// fields it knows (protocol.h). A client refuses every state message before StateHello and drops a
// Decision whose peer epoch is not its own (Sol F3).
enum class MessageType : uint16_t {
  // State lane, S to client.
  kStateHello = 1,
  kEpochBegin = 2,
  kDelta = 3,
  kTomb = 4,
  kThrough = 5,
  kManifest = 6,
  kBaselineBegin = 7,
  kBaselineRows = 8,
  kBaselineEnd = 9,
  kObligation = 10,
  kObligationAck = 11,
  // State lane, client to S.
  kAck = 20,
  kObligationDone = 21,
  kMismatch = 22,
  // Gameplay lane, client to S.
  kRequest = 30,
  kLeaseRenew = 31,
  kLeaseReport = 32,
  // Gameplay lane, S to client.
  kDecision = 40,
  kLeaseEnd = 41,
  // Fast lane, S to client.
  kEffect = 50,
};

namespace msg {

struct StateHello {  // (Opus F9, Sol F3)
  uint64_t table_digest = 0;
  uint32_t epoch = 0;
  uint64_t head = 0;
  uint16_t peer = 0;
  uint32_t peer_epoch = 0;
};
struct EpochBegin {
  uint32_t generation = 0;
  uint64_t lsn = 0;
};
struct Delta {
  Record record;
  uint64_t first_lsn = 0;  // the key's first change after the cursor, for the fence (Sol F2)
};
struct Tomb {
  RecordKey key;
  uint32_t revision = 0;
  uint64_t lsn = 0;
  uint64_t first_lsn = 0;  // (Sol F2)
};
struct Through {
  uint64_t lsn = 0;
};
struct Manifest {
  uint64_t lsn = 0;
  ScopeMask scopes = 0;
  std::array<uint64_t, kManifestBuckets> digests{};
};
struct BaselineBegin {
  uint32_t id = 0;
  uint64_t at = 0;
  uint32_t epoch = 0;
  ScopeMask scopes = 0;
  uint32_t rows = 0;
  uint32_t bytes = 0;
  BaselineReason reason = BaselineReason::kJoin;
};
struct BaselineRows {
  uint32_t id = 0;
  uint32_t index = 0;
  std::vector<Record> records;
};
struct BaselineEnd {  // (Sol F6)
  uint32_t id = 0;
  uint64_t at = 0;
  uint64_t digest = 0;  // FNV-1a 64 over (key, revision, tomb, value digest) of the rows as sent
};
struct Obligation {  // (Sol F9)
  uint64_t id = 0;
  uint64_t floor = 0;  // the lowest id still outstanding for the recipient on S, or the next id to
                       // be made when none is; only rises; bounds nothing (draft 5): it lets the
                       // client drop a reported entry whose acks were all lost (RC4)
  Scope scope = Scope::kLevel;
  uint64_t lsn = 0;
  RecordKey key;
  std::vector<uint8_t> value;
};
// S's answer to every ObligationDone it reads, also for an id it no longer holds (ended, retired;
// Sol F9, draft 4). Id 0 is the mark: sent after BaselineEnd of a repair baseline that retired
// obligations, and every limits.obligation_report_interval while S has sent an id above the
// client's seen; on the ordered lane it arrives after every obligation up to last_sent, so the
// client raises its seen to last_sent (Sol r3 F9, draft 5).
struct ObligationAck {
  uint64_t id = 0;         // the ObligationDone's id; 0: the mark
  uint64_t floor = 0;      // the recipient's floor when the ack is sent
  uint64_t last_sent = 0;  // the highest id S has sent this peer in this peer epoch (draft 5)
};
struct Ack {  // (Opus F6; holding and seen: Sol r3 F9, draft 5)
  uint64_t prefix = 0;
  uint64_t manifest_ok = 0;  // the LSN of the last manifest the client found equal
  // The credit account (section 3.6), both read at the end of the same pump; sent on a change and
  // at least every limits.obligation_report_interval. S's used = holding + those sent above seen.
  uint32_t holding = 0;  // obligation entries the client holds (held and reported)
  uint64_t seen = 0;     // the highest obligation id the client has received in this peer epoch
};
// Repeated every limits.obligation_report_interval until S answers ObligationAck{id, ...} or the
// newest floor passes the id; sent once, not repeated, for a held obligation a repair baseline
// retired (Sol F9, Opus r2 N4; draft 4). The entry's credit returns with the next Ack (draft 5).
struct ObligationDone {
  uint64_t id = 0;
  ObligationEnd end = ObligationEnd::kCompleted;
};
struct Mismatch {
  uint64_t lsn = 0;
  uint64_t bucket_mask = 0;  // 0: a failed baseline digest
};
struct Request {  // on the wire: no peer, no peer epoch (S stamps both from the link; Sol F3)
  backend::Request request;
};
struct LeaseRenew {
  uint32_t id = 0;
};
struct LeaseReport {
  uint32_t id = 0;
  std::vector<uint8_t> payload;
};
struct Decision {  // carries the whole key and the scope (Opus F8)
  backend::Decision decision;
};
struct LeaseEnd {
  uint32_t id = 0;
  LeaseEndReason why = LeaseEndReason::kExpired;
};
struct Effect {  // effect tier only; several in one datagram; best effort, never replayed (Opus Q9)
  uint64_t tick = 0;
  std::vector<Record> records;
};

}  // namespace msg

using Message = std::variant<msg::StateHello, msg::EpochBegin, msg::Delta, msg::Tomb, msg::Through,
                             msg::Manifest, msg::BaselineBegin, msg::BaselineRows,
                             msg::BaselineEnd, msg::Obligation, msg::ObligationAck, msg::Ack,
                             msg::ObligationDone, msg::Mismatch, msg::Request, msg::LeaseRenew,
                             msg::LeaseReport, msg::Decision, msg::LeaseEnd, msg::Effect>;

MessageType TypeOf(const Message& m);
// The lane a message rides (state, gameplay or fast).
netplay::Lane LaneOf(MessageType type);

// Appends one message (type, length, fields) to `frame`; false (nothing appended) if it would make
// the frame longer than `limit` or a field is out of bounds (a value over kMaxValueBytes, a payload
// over kMaxRequestPayload).
bool AppendMessage(const Message& m, std::vector<uint8_t>& frame, size_t limit = kMaxFrameBytes);
// Reads every message of one frame. Unknown types are skipped by their length; a short or broken
// message ends the frame (false; the messages before it stay in `out`). A Request comes out with
// peer and peer epoch 0: S stamps them from the link it came on.
bool DecodeFrame(std::span<const uint8_t> frame, std::vector<Message>& out);

// "state_hello", "delta", "obligation_done", ...: the fault rules' `type=` words and the event
// lines' (D8). Not named by the design; the header's own.
std::string_view MessageTypeName(MessageType type);
std::optional<MessageType> MessageTypeFromName(std::string_view name);
// "gameplay", "state", "fast".
std::string_view LaneName(netplay::Lane lane);
std::optional<netplay::Lane> LaneFromName(std::string_view name);

// The batcher and the ordered queue of one peer (section 3.7; Sol F1, Opus F10). One FIFO per
// lane; every message to the peer goes through it, so a Through queued behind the deltas it covers
// never leaves before them. Messages are packed into frames of at most kMaxFrameBytes. A frame
// that is full goes at the next Flush; the last, partly filled frame of the state lane goes once its
// first message is limits.batch_interval old ("record deltas every 100 ms or on a full batch",
// plan 5.5), the gameplay and fast lanes' at every Flush (a decision is not held back for a batch).
// A frame the link answers kFull stays at the head of its FIFO and nothing behind it goes first; it
// is never dropped. The state lane also stops while the link has limits.max_unacked_state_frames
// frames unacked (the backlog cap; frames, not bytes: Opus F10). The Server owns one per peer
// (server.h); the walk asks Room() before it queues more deltas, so a held or capped peer's
// changes coalesce in the log instead of piling up here (LM3). Not named by the design's API (it
// names the batcher, its FIFO and the cap); the header's own.
//
// Push takes whatever it is given, so the bound is the producers' (Sol code review r1, F1): no
// producer pushes into a lane that is blocked. A periodic message (a manifest, the obligation
// mark, the client's Ack, a repeated ObligationDone, a lease renew) is not made while its lane is
// blocked, and the next one made when it opens carries the newest state, so a newer one replaces
// every older one that was never queued; a one-off message waits in the table that owns it (a due
// baseline, the obligation ledger, S's pending ObligationAcks, S's waiting Decisions and
// LeaseEnds (one per key, Sol code review r2 R2), the client's unsent requests (one per key, R3)
// and lease reports) and is queued when the lane opens. S's state lane so holds at most
// limits.max_unacked_state_frames frames with the link's unacked ones, plus the two frames one
// pump can add past the room (a Through behind the walk, a baseline's end); a client's lanes hold
// at most one pump's messages while they are held.
class PeerQueue {
 public:
  explicit PeerQueue(const Limits& limits);

  // Appends `m` to its lane's FIFO (LaneOf). false, nothing queued, for a message that cannot be
  // framed (a field out of bounds; AppendMessage on an empty frame fails).
  bool Push(const Message& m, TimePoint now);

  struct FlushResult {
    size_t frames_sent = 0;
    bool held = false;            // a lane's head was answered kFull and stays queued
    bool capped = false;          // the state lane stopped at the backlog cap
    bool backlog_began = false;   // a lane went from flowing to held or capped in this Flush: the
                                  // Server raises one `backlog` limit event (LM2)
    bool gone = false;            // the link answered kGone: the queue was cleared
  };
  // Sends what is due on every lane, in FIFO order (see above). `now_all` sends the partly filled
  // frames too (End session's drain, a test).
  FlushResult Flush(PeerLink& link, TimePoint now, bool now_all = false);

  // Frames the state lane may still take before the backlog cap: limits.max_unacked_state_frames
  // less the link's unacked state frames and the frames queued here; 0 while the state lane's head
  // is held (kFull). The Server walks only while this is above 0.
  size_t Room(const PeerLink& link) const;
  // Frames queued on `lane` (full and partly filled).
  size_t Frames(netplay::Lane lane) const;
  // Frames queued on every lane: what this direction of the peer holds above the link (F1).
  size_t Frames() const;
  size_t Messages(netplay::Lane lane) const;
  size_t Bytes(netplay::Lane lane) const;
  // Frames queued behind a kFull on any lane (the map dump's and PeerStats' frames_held).
  size_t Held() const;
  bool Blocked(netplay::Lane lane) const;  // the last Flush stopped this lane (held or capped)
  uint64_t BacklogEvents() const;          // backlog_began, counted
  void Clear();

 private:
  struct Frame {
    std::vector<uint8_t> bytes;
    size_t messages = 0;
    TimePoint opened{};
  };
  struct LaneQueue {
    std::deque<Frame> frames;
    bool held = false;    // the head was answered kFull
    bool capped = false;  // the state lane stopped at the cap
  };
  LaneQueue& Of(netplay::Lane lane);
  const LaneQueue& Of(netplay::Lane lane) const;

  Limits limits_;
  std::array<LaneQueue, netplay::kLaneCount> lanes_;
  uint64_t backlog_events_ = 0;
  std::vector<uint8_t> scratch_;
};

// The pacer's token bucket for one peer's baseline bytes (section 3.7): it fills at
// limits.baseline_bytes_per_second up to one batch interval's worth (at least one frame), so a
// paced baseline keeps to its rate within one batch (LM8). Deltas are not paced here: they stop at
// the backlog cap (PeerQueue). Not named by the design's API; the header's own.
class Pacer {
 public:
  Pacer(const Limits& limits, TimePoint now);
  // Adds what the time since the last refill earned, up to the bucket's size.
  void Refill(TimePoint now);
  // Takes `bytes` from the bucket if it holds them; false (nothing taken) otherwise. A chunk
  // larger than the bucket goes when the bucket is full.
  bool Take(size_t bytes);
  size_t Available() const;
  size_t Capacity() const;
  // A new baseline starts with a full bucket.
  void Reset(TimePoint now);

 private:
  double rate_ = 0;  // bytes per second
  double capacity_ = 0;
  double tokens_ = 0;
  TimePoint last_{};
};

}  // namespace skylanders::backend
