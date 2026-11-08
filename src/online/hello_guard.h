#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <list>
#include <map>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "netplay/transport.h"

namespace skylanders::online {

// What an open port must withstand (stage 8 item 4, design 3.4 and D10, the backlog's spoofed
// Hello): the checks a server runs on every Hello before it holds anything for the sender, in the
// design's order, cheapest first. Pure: no sockets, no threads, the clock passed in, the random
// bytes passed in (the caller draws them from the OS random source), so the unit test drives it
// on fake time. The session wiring (a stateless cookie reply before any connection is made, the
// Hello's secret, nonce and cookie fields, the let-in messages) is not here yet.
//
//   1. size: a Hello datagram under kMinHelloBytes (the joiner pads to it) or over the protocol's
//      maximum is dropped, so a cookie reply is never larger than what came in;
//   2. a known session goes to its peer (the caller's, before this gate);
//   3. limits: a global token bucket (16 Hellos per second) and a per-source bucket (2 per second,
//      burst 4) in a table of at most 256 sources, least recently seen evicted first; over either,
//      or from a source blocked for wrong secrets, dropped without a reply;
//   4. the cookie: without a valid one, a stateless reply carrying one (nothing stored);
//   5. the secret: a cookie-valid Hello with the wrong secret is refused "wrong code"; after 4 a
//      source gets no replies for 60 s; 4096 wrong secrets spend the secret's budget and no new
//      joiner is admitted until the owner makes a new code (a new secret);
//   6. let in: a joiner waits as a pending entry (at most 4, each expiring 60 s after it was shown,
//      never a player slot) unless "let anyone with the code in" is on; the owner (a valid owner
//      token, checked by the caller) skips the secret and let in;
//   7. players: at most the session's maximum; one more is refused "full".

using CookieKey = std::array<uint8_t, 16>;

// SipHash-2-4 with a 64-bit output (Aumasson and Bernstein's reference definition).
uint64_t SipHash24(const CookieKey& key, std::span<const uint8_t> data);

constexpr size_t kMinHelloBytes = 96;
constexpr size_t kMaxHelloBytes = 1200;  // netplay kMaxDatagramSize
constexpr auto kCookieBucket = std::chrono::seconds(5);
constexpr auto kCookieKeyLifetime = std::chrono::minutes(10);
constexpr double kGlobalHellosPerSecond = 16;
constexpr double kSourceHellosPerSecond = 2;
constexpr double kSourceHelloBurst = 4;
constexpr size_t kMaxSources = 256;
constexpr int kWrongSecretsPerSource = 4;
constexpr auto kWrongSecretBlock = std::chrono::seconds(60);
constexpr uint32_t kWrongSecretBudget = 4096;
constexpr size_t kMaxPendingJoins = 4;
constexpr auto kPendingJoinExpiry = std::chrono::seconds(60);

// The cookie: SipHash-2-4 under a 128-bit per-session key over (time bucket, source address,
// source port, the Hello's 64-bit client nonce). The bucket is S's own monotonic clock in 5 s steps
// from the jar's start; the current and the previous bucket are accepted (a cookie lives 5 to
// 10 s; nothing about time is taken from the joiner). The key rotates every 10 minutes; the
// previous key is accepted for one bucket after a rotation.
class CookieJar {
 public:
  CookieJar(const CookieKey& key, netplay::TimePoint start);
  // Due once kCookieKeyLifetime has passed since the last key; the caller then calls Rotate with
  // fresh random bytes.
  bool RotationDue(netplay::TimePoint now) const { return now >= next_rotation_; }
  void Rotate(const CookieKey& next_key, netplay::TimePoint now);
  uint64_t Make(const netplay::Endpoint& from, uint64_t client_nonce, netplay::TimePoint now) const;
  bool Verify(uint64_t cookie, const netplay::Endpoint& from, uint64_t client_nonce,
              netplay::TimePoint now) const;

 private:
  uint64_t Bucket(netplay::TimePoint now) const;
  static uint64_t Mac(const CookieKey& key, uint64_t bucket, const netplay::Endpoint& from,
                      uint64_t client_nonce);

  netplay::TimePoint start_;
  netplay::TimePoint next_rotation_;
  CookieKey current_{};
  CookieKey previous_{};
  bool has_previous_ = false;
  uint64_t rotated_at_bucket_ = 0;
};

// A token bucket: `rate` tokens a second up to `burst`; starts full.
class TokenBucket {
 public:
  TokenBucket(double rate, double burst, netplay::TimePoint now)
      : rate_(rate), burst_(burst), tokens_(burst), last_(now) {}
  bool Take(netplay::TimePoint now);

 private:
  double rate_;
  double burst_;
  double tokens_;
  netplay::TimePoint last_;
};

// Per-source state in a fixed-size table (kMaxSources), keyed by the source address (a port is
// free for a sender to change). A new source evicts the least recently seen one, preferring one
// that is not blocked, so many addresses cannot grow memory.
class SourceTable {
 public:
  struct Source {
    TokenBucket bucket;
    int wrong_secrets = 0;
    netplay::TimePoint blocked_until{};
  };
  // The entry for `address`, made (and an old one evicted) if needed; marks it seen at `now`.
  Source& Touch(uint32_t address, netplay::TimePoint now);
  const Source* Find(uint32_t address) const;
  size_t size() const { return entries_.size(); }

 private:
  struct Entry {
    Source source;
    std::list<uint32_t>::iterator lru;  // position in lru_ (front: most recently seen)
  };
  std::map<uint32_t, Entry> entries_;
  std::list<uint32_t> lru_;
};

// A joiner waiting for the owner's let in. Never a player slot.
struct PendingJoin {
  uint32_t id = 0;
  netplay::Endpoint from;
  uint64_t client_nonce = 0;
  std::string name;
  uint16_t figure_id = 0;
  uint16_t figure_variant = 0;
  netplay::TimePoint shown{};
};

class LetInTable {
 public:
  enum class Add { kAdded, kRetransmit, kBusy };
  // A new pending entry for (from, nonce), or the existing one for the same tuple (a retransmit:
  // no second entry), or kBusy when kMaxPendingJoins are waiting. `id` gets the entry's id.
  Add Offer(const PendingJoin& join, netplay::TimePoint now, uint32_t* id);
  // The owner's decision takes the entry out (nullopt: no such entry, or it expired).
  std::optional<PendingJoin> Take(uint32_t id);
  // Entries shown kPendingJoinExpiry ago or more, taken out ("not let in: timed out").
  std::vector<PendingJoin> Expire(netplay::TimePoint now);
  const std::vector<PendingJoin>& Pending() const { return pending_; }

 private:
  std::vector<PendingJoin> pending_;
  uint32_t next_id_ = 1;
};

// What a Hello carries for the gate (from the datagram and the decoded Hello).
struct HelloFacts {
  size_t datagram_bytes = 0;
  netplay::Endpoint from;
  uint64_t client_nonce = 0;
  uint64_t cookie = 0;  // 0: none
  uint32_t secret = 0;
  bool owner = false;  // the Hello carries the session's owner token (the caller compared it)
  std::string name;
  uint16_t figure_id = 0;
  uint16_t figure_variant = 0;
};

enum class HelloVerdict {
  kDrop,          // no reply at all (size, limits, a blocked source)
  kCookieReply,   // a stateless reply carrying `cookie`
  kWrongCode,     // refused "wrong code"
  kCodeSpent,     // the secret's wrong-secret budget is spent: no new joiner until a new code
  kWait,          // a pending entry (new or retransmitted): wait for the owner's let in
  kBusy,          // "the host is busy, try again": every pending place is taken
  kAdmit,         // welcome it now (the owner, or "let anyone with the code in")
  kFull,          // "full"
};
const char* HelloVerdictName(HelloVerdict verdict);

struct HelloDecision {
  HelloVerdict verdict = HelloVerdict::kDrop;
  uint64_t cookie = 0;       // kCookieReply
  uint32_t pending_id = 0;   // kWait
};

struct HelloGateSettings {
  uint32_t secret = 0;          // the join code's secret (never 0)
  CookieKey cookie_key{};       // random, per session
  bool let_anyone_in = false;   // the session option "let anyone with the code in" (off: D15)
  int max_players = 4;
};

class HelloGate {
 public:
  HelloGate(const HelloGateSettings& settings, netplay::TimePoint now);
  // A Hello from a sender that is not a known session; `players` is how many are in now.
  HelloDecision Check(const HelloFacts& hello, int players, netplay::TimePoint now);
  // The owner let pending entry `id` in: kAdmit (with the entry in `out`), kFull, or kDrop (no
  // such entry: it expired or was answered).
  HelloVerdict LetIn(uint32_t id, int players, PendingJoin* out);
  // The owner refused it: true if it was waiting.
  bool Refuse(uint32_t id) { return pending_.Take(id).has_value(); }
  std::vector<PendingJoin> ExpirePending(netplay::TimePoint now) { return pending_.Expire(now); }
  // A new code (a new secret): the wrong-secret budget starts again.
  void NewSecret(uint32_t secret);
  // Key rotation, with fresh random bytes from the caller.
  bool CookieRotationDue(netplay::TimePoint now) const { return cookies_.RotationDue(now); }
  void RotateCookieKey(const CookieKey& key, netplay::TimePoint now) { cookies_.Rotate(key, now); }

  bool CodeSpent() const { return wrong_secrets_ >= kWrongSecretBudget; }
  uint32_t WrongSecrets() const { return wrong_secrets_; }
  const LetInTable& Pending() const { return pending_; }
  const SourceTable& Sources() const { return sources_; }

 private:
  HelloGateSettings settings_;
  CookieJar cookies_;
  TokenBucket global_;
  SourceTable sources_;
  LetInTable pending_;
  uint32_t wrong_secrets_ = 0;
};

}  // namespace skylanders::online
