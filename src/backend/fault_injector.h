#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "backend/adapter.h"
#include "backend/base.h"
#include "backend/entity_id.h"
#include "backend/kind_table.h"
#include "backend/lanes.h"
#include "backend/record.h"
#include "netplay/datagram.h"

// The fault injector (design section 6): one rule list, deterministic from a seed. The tests use it
// directly; stage 3 wires the same rules to a dev setting on real traffic. Every fault it plays is
// an event (and, in the game, a line), so a case can require it: a rule that never fired makes the
// case "not reached" (NotFired).
//
//   layer    actions                                            where
//   link     drop, delay <ms>, duplicate, reorder (fast lane     a PeerLink decorator (Wrap), on
//            only), throttle <bytes/s>, full <ms>, disconnect    what its end sends
//   message  kill, delay <ms or min-max> (the lane's order is    the same decorator, on what its end
//            kept, the lane as a whole is late against the        receives: after the lane delivered
//            others), duplicate                                   a frame, before the backend reads
//                                                                 it, so S never sees a kill (Sol F1)
//   apply    fail, stuck, despawn (inside Apply), late <ms>      a KindAdapter decorator
//            (spawn after the row)                                (WrapAdapter); late is asked by the
//                                                                 harness that spawns (SpawnDelay)
//   pump     stall <ms> (a peer that stops pumping: a loading     the harness asks Stalled
//            screen)
//
// A held send is the link layer's `full`: the link answers kFull and the sender's FIFO holds.
//
// Text form: one rule per `;`-free clause, joined by `|` (the game's input script splits at `;`):
//
//   message:kill type=delta kind=prop nth=3 | link:throttle peer=3 bps=2000 | peer:disconnect peer=2 after=baseline_rows:5
//
// A rule is `<layer>:<action>` and words. `peer:` is the spelling the design's example gives the
// link layer's disconnect; it takes only `disconnect`. Matching words (a word left out matches
// everything): lane=gameplay|state|fast, dir=s2c|c2s, peer=<n>, type=<message type>
// (MessageTypeName), kind=<kind name>, key=<entity text> (ToText), slot=<n>, id=<n> (an
// obligation's id: Obligation, ObligationDone, ObligationAck; the header's own word, so a case can
// kill the reports of one obligation, RC3, RC4, LM10b), nth=<n> (fire at the
// nth match only), pct=<0-100> (the seeded chance a match fires), from=<ms> and to=<ms> (a window
// from the injector's start), after=baseline_rows:<n> (once the link's end carried n BaselineRows
// messages in the rule's direction). The amount: ms=<n> (full, late, stall), delay=<n> or
// delay=<min>-<max> (delay), bps=<n> (throttle); a bare number after the action is the same amount
// (`apply:late 300`). FormatFaultRules writes the canonical form: the matching words in the order
// above, then the amount word. The parser refuses an unknown word, a word the action does not take
// and a missing amount, with the word's position; a rule list round-trips through its text (FI1,
// FI2).
//
// `full`, `disconnect` and `stall` fire once (their onset); `throttle` fires once per link, when it
// first applies; every other action fires once per match. A throttled link passes its frames at
// the rate through a bottleneck buffer of one second of the rate (at least one frame) and answers
// kFull beyond it, so the sender's FIFO holds and its changes coalesce in the log (LM1). The game's line, from stage 3:
// `Online: backend fault <rule n> <action> <what>` (FaultLine).

namespace skylanders::backend {

enum class FaultLayer : uint8_t { kLink, kPeer, kMessage, kApply, kPump };
enum class FaultAction : uint8_t {
  kDrop,        // link
  kDelay,       // link, message
  kDuplicate,   // link, message
  kReorder,     // link, fast lane only
  kThrottle,    // link
  kFull,        // link
  kDisconnect,  // link, peer
  kKill,        // message
  kFail,        // apply
  kStuck,       // apply
  kDespawn,     // apply
  kLate,        // apply
  kStall,       // pump
};
std::string_view FaultLayerName(FaultLayer layer);     // "link", "peer", "message", ...
std::string_view FaultActionName(FaultAction action);  // "drop", "delay", ...

// Which way a frame goes: S to a client, or a client to S. dir=s2c|c2s.
enum class FaultDirection : uint8_t { kAny, kServerToClient, kClientToServer };

struct FaultRule {
  FaultLayer layer = FaultLayer::kLink;
  FaultAction action = FaultAction::kDrop;
  // ms for delay, full, late and stall; bytes per second for throttle. A delay range
  // (delay=0-300) is amount to amount_max, drawn uniformly per frame from the seed; amount_max ==
  // amount otherwise.
  uint32_t amount = 0;
  uint32_t amount_max = 0;
  std::optional<netplay::Lane> lane;
  FaultDirection direction = FaultDirection::kAny;
  std::optional<uint16_t> peer;
  std::optional<MessageType> type;
  std::string kind;  // a kind's name; empty: any
  std::string key;   // an entity's text (ToText); empty: any
  std::optional<uint16_t> slot;
  std::optional<uint64_t> obligation;  // id=
  uint32_t nth = 0;        // 0: every match
  uint32_t percent = 100;  // the chance a match fires
  std::optional<uint32_t> from_ms, to_ms;
  uint32_t after_baseline_rows = 0;  // 0: no such condition
  bool operator==(const FaultRule&) const = default;
};

struct FaultParseError {
  size_t position = 0;  // the byte offset of the word, from 0
  std::string text;     // "unknown word 'x' at 12", ...
};
// nullopt with `*error` set for text it refuses. An empty text is an empty list.
std::optional<std::vector<FaultRule>> ParseFaultRules(std::string_view text,
                                                      FaultParseError* error = nullptr);
std::string FormatFaultRule(const FaultRule& rule);
std::string FormatFaultRules(std::span<const FaultRule> rules);  // joined by " | "

struct FaultEvent {
  size_t rule = 0;  // from 1, as the line writes it
  FaultLayer layer = FaultLayer::kLink;
  FaultAction action = FaultAction::kDrop;
  uint16_t peer = 0;
  TimePoint at{};
  std::string what;  // "peer 1 s2c state delta test.prop a1.2.3@1 slot 0", "peer 3 2000 bps", ...
};
// "Online: backend fault <rule n> <layer>:<action> <what>".
std::string FaultLine(const FaultEvent& event);

class FaultInjector {
 public:
  // `table` names kinds for kind= (it must outlive the injector); `start` is the window's origin.
  FaultInjector(std::vector<FaultRule> rules, uint64_t seed, std::span<const KindSpec> table,
                TimePoint start);
  ~FaultInjector();
  FaultInjector(const FaultInjector&) = delete;
  FaultInjector& operator=(const FaultInjector&) = delete;

  // The owner's clock (D3; the injector has none): call before every pump. Sends the delayed and
  // throttled frames that are due on every wrapped link, and fires a disconnect whose condition
  // holds.
  void Advance(TimePoint now);
  TimePoint Now() const;

  // The link and message layers around `inner`, the end of the link to `peer` on this side:
  // `server_side`, S's end (it sends s2c and receives c2s), or the client's. The decorator must not
  // outlive the injector. Its UnackedFrames counts the frames it holds (delayed, throttled) as
  // unacked, so the sender's backlog cap counts them (LM1).
  std::unique_ptr<PeerLink> Wrap(PeerLink& inner, uint16_t peer, bool server_side);
  // The apply layer around `inner`, the adapter of `kind` on `peer`'s client. `identity` names the
  // entity behind a handle (for key=); `despawn` is how the world removes an entity inside Apply
  // (apply:despawn); without it, despawn only answers kGone.
  std::unique_ptr<KindAdapter> WrapAdapter(KindAdapter& inner, uint16_t peer, KindId kind,
                                           const IdentityMap* identity,
                                           std::function<void(Handle)> despawn = {});
  // apply:late: the harness asks once, when a row or obligation for an entity first reaches
  // `peer`, how long to hold the entity's spawn; nullopt: spawn as usual.
  std::optional<Duration> SpawnDelay(uint16_t peer, const RecordKey& key);
  // pump:stall: whether `peer` skips its pump now.
  bool Stalled(uint16_t peer);

  bool PollEvent(FaultEvent& out);
  const std::vector<FaultRule>& Rules() const;
  uint64_t Matched(size_t rule) const;  // index from 0
  uint64_t Fired(size_t rule) const;
  // Rules that never fired (index from 0): the case they belong to was not reached.
  std::vector<size_t> NotFired() const;

  struct Impl;

 private:
  std::unique_ptr<Impl> impl_;
};

}  // namespace skylanders::backend
