#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>

// The backend's limits (D10). Two parts: the codec's bounds, constexpr and the same for every game
// because the wire depends on them; and struct Limits, generic defaults a game replaces with its own
// (Swap Force's: the game's src/online/backend_limits.h, from stage 2 item 7, each value with the
// census run it came from). Every constructor that needs a limit takes a Limits. Until item 7 the
// fields marked "census" are provisional.

namespace skylanders::backend {

// One backend frame, at most: netplay's payload (1,174, netplay/datagram.h) less the session message
// header (4) is 1,170, rounded down to a multiple of 64 to leave room for stage 3's framing.
constexpr size_t kMaxFrameBytes = 1152;
// One record value, at most, for every kind: one delta with its key fits in one frame.
constexpr size_t kMaxValueBytes = 512;
// One request's payload, at most.
constexpr size_t kMaxRequestPayload = 256;
// Digests in one manifest (one frame).
constexpr size_t kManifestBuckets = 64;

struct Limits {
  // A kind's max_value_bytes may not exceed this (validator rule V9); at most kMaxValueBytes.
  // Frozen by: census sizes.
  uint16_t max_value_bytes = 512;
  // The batcher flushes at least this often (plan 5.5, "record deltas every 100 ms or on a full
  // batch"). Frozen by: thresholds.
  std::chrono::milliseconds batch_interval{100};
  // A noisy kind's row is sent at most this often per second, the last value winning (plan 5.5).
  // Frozen by: thresholds.
  uint32_t noisy_hz = 2;
  // A manifest goes to a peer this often, when its cursor is at the head (plan 2.5).
  std::chrono::milliseconds manifest_interval{2000};
  // A peer with no baseline ended and no manifest matched for this long is rebaselined (plan 2.5).
  // Silence is about proof, not bytes: a peer that keeps acking under churn is still silent.
  std::chrono::milliseconds silence_baseline{60000};
  // The pacer's budget for baseline bytes, per peer (inside the 30 to 50 KB/s baseline peak of plan
  // 5.5; KB read as 1,024 bytes). Frozen by: census baseline sizes.
  uint32_t baseline_bytes_per_second = 40 * 1024;
  // Changes the log holds at most; a peer whose cursor falls below the floor is rebaselined.
  // Frozen by: census burst rates.
  size_t log_retention = 16384;
  // Deltas to one peer stop while its state lane has this many frames unacked (about 256 KB at
  // kMaxFrameBytes). Frames, not bytes (Opus F10): netplay's Connection::Unacked(lane) counts
  // datagrams and reports no per-datagram ack (netplay/lanes.h), the state lane carries only backend
  // frames and one frame is one datagram, so the count is exact. At most netplay's kLaneWindow
  // (1,024); a kWindowFull is the link's kFull. Frozen by: census burst rates.
  size_t max_unacked_state_frames = 224;
  // Level-scope rows at most (Put answers kRowLimit past it). A generic default; Swap Force's comes
  // from its entity table (2,048 slots) and the census, in the game's file. Frozen by: census rows.
  size_t max_rows_per_level = 16384;
  // Session-scope rows at most, tombs counted (Put and ApplyAll answer kRowLimit past it; Sol F9,
  // LM5b). Frozen by: census rows.
  size_t max_rows_per_session = 4096;
  // Obligations one peer has on S at most: sent and not ended, plus waiting (for the cursor or for
  // credit). At it a further Oblige for that peer fails with a limit event until it is under (OB6,
  // LM10c). Draft 5: no longer a window and no longer a rebaseline reason (a baseline returns no
  // credit; Sol r3 F9, section 3.6). Frozen by: census.
  size_t max_obligations_per_peer = 256;
  // The obligations a client holds at most, in any state (held, retired but held, reported and not
  // acked): S sends a peer's next one only while the credit it counts as used is below this, pauses
  // that peer's obligations at it and resumes them as Acks return credit (Sol r3 F9, draft 5;
  // section 3.6). From 1 to max_obligations_per_peer; half of it by default, about 72 KB per client
  // at most. Frozen by: census, twice the p99 of obligations in flight per peer.
  size_t obligation_credit = 128;
  // The client repeats an unacked ObligationDone this often, until ObligationAck, and sends Ack at
  // least this often; S sends the mark ObligationAck{0, floor, last_sent} this often while it has
  // sent an id above the client's seen (Sol F9, draft 4; Sol r3 F9, draft 5). Frozen by: code.
  std::chrono::milliseconds obligation_report_interval{1000};
  // Leases one peer holds at most; one more is refused `limit` (LM7). Frozen by: stage 6.
  size_t max_leases_per_peer = 4;
  // A lease without a renew for this long expires; the holder renews every lease_renew_interval.
  // Frozen by: stage 6.
  std::chrono::milliseconds lease_term{3000};
  std::chrono::milliseconds lease_renew_interval{1000};
  // How long a request waits for its precondition (1B's pose wait, requests.h kPoseWait).
  std::chrono::milliseconds precondition_wait{500};
  // The grace a row waits for its entity before it is late, for a kind whose own pending_grace_ms
  // is 0. Frozen by: census identity settle p95 plus a margin.
  uint32_t pending_grace_ms = 5000;
  // The quiet period before a compare (two manifests and 1 s). Frozen by: census settle.
  std::chrono::milliseconds compare_quiet{5000};
  // Request keys one peer holds in one generation (1B's kMaxKeysPerPeer); one more is refused
  // `rate`, final (LM6).
  size_t max_keys_per_peer = 1024;
};

}  // namespace skylanders::backend
