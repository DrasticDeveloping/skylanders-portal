#include "online/hello_guard.h"

#include <algorithm>

namespace skylanders::online {

namespace {

uint64_t Rotl(uint64_t x, int b) { return (x << b) | (x >> (64 - b)); }

uint64_t Load64(const uint8_t* p) {
  uint64_t v = 0;
  for (int i = 7; i >= 0; --i) v = (v << 8) | p[i];
  return v;
}

void Round(uint64_t& v0, uint64_t& v1, uint64_t& v2, uint64_t& v3) {
  v0 += v1;
  v1 = Rotl(v1, 13);
  v1 ^= v0;
  v0 = Rotl(v0, 32);
  v2 += v3;
  v3 = Rotl(v3, 16);
  v3 ^= v2;
  v0 += v3;
  v3 = Rotl(v3, 21);
  v3 ^= v0;
  v2 += v1;
  v1 = Rotl(v1, 17);
  v1 ^= v2;
  v2 = Rotl(v2, 32);
}

void Put64(std::vector<uint8_t>& out, uint64_t v) {
  for (int i = 0; i < 8; ++i) out.push_back(uint8_t(v >> (8 * i)));
}

}  // namespace

uint64_t SipHash24(const CookieKey& key, std::span<const uint8_t> data) {
  const uint64_t k0 = Load64(key.data());
  const uint64_t k1 = Load64(key.data() + 8);
  uint64_t v0 = 0x736f6d6570736575ULL ^ k0;
  uint64_t v1 = 0x646f72616e646f6dULL ^ k1;
  uint64_t v2 = 0x6c7967656e657261ULL ^ k0;
  uint64_t v3 = 0x7465646279746573ULL ^ k1;
  const size_t n = data.size();
  const size_t whole = n - n % 8;
  for (size_t i = 0; i < whole; i += 8) {
    const uint64_t m = Load64(data.data() + i);
    v3 ^= m;
    Round(v0, v1, v2, v3);
    Round(v0, v1, v2, v3);
    v0 ^= m;
  }
  uint64_t last = uint64_t(n & 0xFF) << 56;
  for (size_t i = 0; i < n % 8; ++i) last |= uint64_t(data[whole + i]) << (8 * i);
  v3 ^= last;
  Round(v0, v1, v2, v3);
  Round(v0, v1, v2, v3);
  v0 ^= last;
  v2 ^= 0xFF;
  for (int i = 0; i < 4; ++i) Round(v0, v1, v2, v3);
  return v0 ^ v1 ^ v2 ^ v3;
}

// ---------------------------------------------------------------------------------------------

CookieJar::CookieJar(const CookieKey& key, netplay::TimePoint start)
    : start_(start), next_rotation_(start + kCookieKeyLifetime), current_(key) {}

void CookieJar::Rotate(const CookieKey& next_key, netplay::TimePoint now) {
  previous_ = current_;
  current_ = next_key;
  has_previous_ = true;
  rotated_at_bucket_ = Bucket(now);
  next_rotation_ = now + kCookieKeyLifetime;
}

uint64_t CookieJar::Bucket(netplay::TimePoint now) const {
  if (now <= start_) return 0;
  return uint64_t((now - start_) / kCookieBucket);
}

uint64_t CookieJar::Mac(const CookieKey& key, uint64_t bucket, const netplay::Endpoint& from,
                        uint64_t client_nonce) {
  std::vector<uint8_t> bytes;
  bytes.reserve(22);
  Put64(bytes, bucket);
  for (int i = 0; i < 4; ++i) bytes.push_back(uint8_t(from.address >> (8 * i)));
  bytes.push_back(uint8_t(from.port));
  bytes.push_back(uint8_t(from.port >> 8));
  Put64(bytes, client_nonce);
  const uint64_t mac = SipHash24(key, bytes);
  return mac == 0 ? 1 : mac;  // 0 means "no cookie" on the wire
}

uint64_t CookieJar::Make(const netplay::Endpoint& from, uint64_t client_nonce,
                         netplay::TimePoint now) const {
  return Mac(current_, Bucket(now), from, client_nonce);
}

bool CookieJar::Verify(uint64_t cookie, const netplay::Endpoint& from, uint64_t client_nonce,
                       netplay::TimePoint now) const {
  if (cookie == 0) return false;
  const uint64_t bucket = Bucket(now);
  auto matches = [&](const CookieKey& key) {
    if (Mac(key, bucket, from, client_nonce) == cookie) return true;
    return bucket > 0 && Mac(key, bucket - 1, from, client_nonce) == cookie;
  };
  if (matches(current_)) return true;
  // The previous key, for one bucket after the rotation.
  return has_previous_ && bucket <= rotated_at_bucket_ + 1 && matches(previous_);
}

// ---------------------------------------------------------------------------------------------

bool TokenBucket::Take(netplay::TimePoint now) {
  if (now > last_) {
    const double seconds = std::chrono::duration<double>(now - last_).count();
    tokens_ = std::min(burst_, tokens_ + seconds * rate_);
    last_ = now;
  }
  if (tokens_ < 1.0) return false;
  tokens_ -= 1.0;
  return true;
}

SourceTable::Source& SourceTable::Touch(uint32_t address, netplay::TimePoint now) {
  if (auto it = entries_.find(address); it != entries_.end()) {
    lru_.splice(lru_.begin(), lru_, it->second.lru);
    return it->second.source;
  }
  if (entries_.size() >= kMaxSources) {
    // The least recently seen source that is not blocked; if every one is blocked, the least
    // recently seen of all.
    auto victim = std::prev(lru_.end());
    for (auto it = lru_.rbegin(); it != lru_.rend(); ++it) {
      if (entries_.at(*it).source.blocked_until <= now) {
        victim = std::prev(it.base());
        break;
      }
    }
    entries_.erase(*victim);
    lru_.erase(victim);
  }
  lru_.push_front(address);
  auto [it, inserted] = entries_.emplace(
      address, Entry{Source{TokenBucket(kSourceHellosPerSecond, kSourceHelloBurst, now)}, lru_.begin()});
  return it->second.source;
}

const SourceTable::Source* SourceTable::Find(uint32_t address) const {
  auto it = entries_.find(address);
  return it == entries_.end() ? nullptr : &it->second.source;
}

// ---------------------------------------------------------------------------------------------

LetInTable::Add LetInTable::Offer(const PendingJoin& join, netplay::TimePoint now, uint32_t* id) {
  for (const PendingJoin& p : pending_) {
    if (p.from == join.from && p.client_nonce == join.client_nonce) {
      if (id) *id = p.id;
      return Add::kRetransmit;
    }
  }
  if (pending_.size() >= kMaxPendingJoins) return Add::kBusy;
  PendingJoin entry = join;
  entry.id = next_id_++;
  if (next_id_ == 0) next_id_ = 1;
  entry.shown = now;
  pending_.push_back(entry);
  if (id) *id = entry.id;
  return Add::kAdded;
}

std::optional<PendingJoin> LetInTable::Take(uint32_t id) {
  auto it = std::find_if(pending_.begin(), pending_.end(), [id](const PendingJoin& p) { return p.id == id; });
  if (it == pending_.end()) return std::nullopt;
  PendingJoin out = *it;
  pending_.erase(it);
  return out;
}

std::vector<PendingJoin> LetInTable::Expire(netplay::TimePoint now) {
  std::vector<PendingJoin> expired;
  auto keep = std::stable_partition(pending_.begin(), pending_.end(), [now](const PendingJoin& p) {
    return now - p.shown < kPendingJoinExpiry;
  });
  expired.assign(keep, pending_.end());
  pending_.erase(keep, pending_.end());
  return expired;
}

// ---------------------------------------------------------------------------------------------

const char* HelloVerdictName(HelloVerdict verdict) {
  switch (verdict) {
    case HelloVerdict::kDrop: return "dropped";
    case HelloVerdict::kCookieReply: return "cookie reply";
    case HelloVerdict::kWrongCode: return "wrong code";
    case HelloVerdict::kCodeSpent: return "code spent";
    case HelloVerdict::kWait: return "waiting for let in";
    case HelloVerdict::kBusy: return "the host is busy, try again";
    case HelloVerdict::kAdmit: return "admitted";
    case HelloVerdict::kFull: return "full";
  }
  return "dropped";
}

HelloGate::HelloGate(const HelloGateSettings& settings, netplay::TimePoint now)
    : settings_(settings),
      cookies_(settings.cookie_key, now),
      global_(kGlobalHellosPerSecond, kGlobalHellosPerSecond, now) {}

void HelloGate::NewSecret(uint32_t secret) {
  settings_.secret = secret;
  wrong_secrets_ = 0;
}

HelloDecision HelloGate::Check(const HelloFacts& hello, int players, netplay::TimePoint now) {
  HelloDecision d;
  // 1. Size.
  if (hello.datagram_bytes < kMinHelloBytes || hello.datagram_bytes > kMaxHelloBytes) return d;
  // 3. Limits: a blocked source first (no token spent on it), then the global and source buckets.
  if (const SourceTable::Source* s = sources_.Find(hello.from.address); s && now < s->blocked_until) {
    return d;
  }
  if (!global_.Take(now)) return d;
  SourceTable::Source& source = sources_.Touch(hello.from.address, now);
  if (!source.bucket.Take(now)) return d;
  // 4. The cookie.
  if (!cookies_.Verify(hello.cookie, hello.from, hello.client_nonce, now)) {
    d.verdict = HelloVerdict::kCookieReply;
    d.cookie = cookies_.Make(hello.from, hello.client_nonce, now);
    return d;
  }
  // The owner (its token checked by the caller) skips the secret and let in.
  if (hello.owner) {
    d.verdict = players < settings_.max_players ? HelloVerdict::kAdmit : HelloVerdict::kFull;
    return d;
  }
  // 5. The secret; only cookie-valid attempts count, so a spoofer cannot spend them.
  if (CodeSpent()) {
    d.verdict = HelloVerdict::kCodeSpent;
    return d;
  }
  if (hello.secret != settings_.secret || settings_.secret == 0) {
    ++wrong_secrets_;
    if (++source.wrong_secrets >= kWrongSecretsPerSource) {
      source.blocked_until = now + kWrongSecretBlock;
      source.wrong_secrets = 0;
    }
    d.verdict = CodeSpent() ? HelloVerdict::kCodeSpent : HelloVerdict::kWrongCode;
    return d;
  }
  // 6. Let in, and 7. players.
  if (settings_.let_anyone_in) {
    d.verdict = players < settings_.max_players ? HelloVerdict::kAdmit : HelloVerdict::kFull;
    return d;
  }
  if (players >= settings_.max_players) {
    d.verdict = HelloVerdict::kFull;
    return d;
  }
  PendingJoin join;
  join.from = hello.from;
  join.client_nonce = hello.client_nonce;
  join.name = hello.name;
  join.figure_id = hello.figure_id;
  join.figure_variant = hello.figure_variant;
  switch (pending_.Offer(join, now, &d.pending_id)) {
    case LetInTable::Add::kAdded:
    case LetInTable::Add::kRetransmit:
      d.verdict = HelloVerdict::kWait;
      break;
    case LetInTable::Add::kBusy:
      d.verdict = HelloVerdict::kBusy;
      break;
  }
  return d;
}

HelloVerdict HelloGate::LetIn(uint32_t id, int players, PendingJoin* out) {
  std::optional<PendingJoin> join = pending_.Take(id);
  if (!join) return HelloVerdict::kDrop;
  if (players >= settings_.max_players) return HelloVerdict::kFull;
  if (out) *out = *join;
  return HelloVerdict::kAdmit;
}

}  // namespace skylanders::online
