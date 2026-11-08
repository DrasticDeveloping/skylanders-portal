// The Hello gate (stage 8 item 4, `hello-cookie` and `hello-limits` units): SipHash-2-4, the cookie's
// buckets, tuple binding and key rotation, the global and per-source buckets, the bounded source
// table, the wrong-secret budget and the pending let-in entries, all on fake time.
#include <array>
#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

#include "online/hello_guard.h"
#include "test_util.h"

using namespace skylanders::online;
using skylanders::netplay::Endpoint;
using skylanders::netplay::TimePoint;
using std::chrono::milliseconds;
using std::chrono::seconds;

namespace {

CookieKey Key(uint8_t first) {
  CookieKey k{};
  for (size_t i = 0; i < k.size(); ++i) k[i] = uint8_t(first + i);
  return k;
}

HelloFacts Facts(Endpoint from, uint64_t nonce, uint64_t cookie, uint32_t secret) {
  HelloFacts h;
  h.datagram_bytes = kMinHelloBytes;
  h.from = from;
  h.client_nonce = nonce;
  h.cookie = cookie;
  h.secret = secret;
  h.name = "Player";
  return h;
}

// A cookie-valid Hello: asks for the cookie first, then sends it back.
HelloDecision Join(HelloGate& gate, HelloFacts h, int players, TimePoint now) {
  HelloDecision first = gate.Check(h, players, now);
  if (first.verdict != HelloVerdict::kCookieReply) return first;
  h.cookie = first.cookie;
  return gate.Check(h, players, now);
}

}  // namespace

int main() {
  const TimePoint t0{};

  // SipHash-2-4: the reference vectors (key 00..0f; messages 00.. of length 0, 8 and 15).
  {
    const CookieKey key = Key(0);
    std::vector<uint8_t> msg;
    CHECK(SipHash24(key, msg) == 0x726fdb47dd0e0e31ULL);
    for (uint8_t i = 0; i < 8; ++i) msg.push_back(i);
    CHECK(SipHash24(key, msg) == 0x93f5f5799a932462ULL);
    for (uint8_t i = 8; i < 15; ++i) msg.push_back(i);
    CHECK(SipHash24(key, msg) == 0xa129ca6149be45e5ULL);
  }

  // The cookie: valid in its bucket and the next, expired at the boundary after that; bound to
  // address, port and nonce; never 0.
  {
    const Endpoint a{0xC0A80114, 50000};
    CookieJar jar(Key(1), t0);
    const TimePoint made = t0 + seconds(12);  // bucket 2 (10 s .. 15 s)
    const uint64_t c = jar.Make(a, 77, made);
    CHECK(c != 0);
    CHECK(jar.Verify(c, a, 77, made));
    CHECK(jar.Verify(c, a, 77, t0 + milliseconds(14999)));  // still bucket 2
    CHECK(jar.Verify(c, a, 77, t0 + seconds(15)));          // bucket 3: the previous bucket
    CHECK(jar.Verify(c, a, 77, t0 + milliseconds(19999)));
    CHECK(!jar.Verify(c, a, 77, t0 + seconds(20)));         // bucket 4: expired at the boundary
    CHECK(!jar.Verify(c, {a.address + 1, a.port}, 77, made));
    CHECK(!jar.Verify(c, {a.address, uint16_t(a.port + 1)}, 77, made));
    CHECK(!jar.Verify(c, a, 78, made));
    CHECK(!jar.Verify(0, a, 77, made));
    // Another session's key never verifies this session's cookie.
    CookieJar other(Key(2), t0);
    CHECK(!other.Verify(c, a, 77, made));
  }

  // Key rotation: due after ten minutes; the previous key is accepted for one bucket after it.
  {
    const Endpoint a{0x0A000002, 40000};
    CookieJar jar(Key(3), t0);
    CHECK(!jar.RotationDue(t0 + seconds(599)));
    CHECK(jar.RotationDue(t0 + seconds(600)));
    const TimePoint before = t0 + seconds(601);  // bucket 120
    const uint64_t old_cookie = jar.Make(a, 5, before);
    jar.Rotate(Key(4), before);
    CHECK(!jar.RotationDue(before + seconds(599)));
    CHECK(jar.Verify(old_cookie, a, 5, before));                 // the rotation's bucket
    CHECK(jar.Verify(old_cookie, a, 5, t0 + seconds(605)));      // one bucket after it
    CHECK(!jar.Verify(old_cookie, a, 5, t0 + seconds(610)));     // two: the old key is gone
    const uint64_t new_cookie = jar.Make(a, 5, before);
    CHECK(new_cookie != old_cookie);
    CHECK(jar.Verify(new_cookie, a, 5, t0 + seconds(605)));
  }

  // Size first: a Hello under the padded minimum or over the datagram maximum gets nothing, so the
  // cookie reply is never larger than what came in; nothing is stored for it.
  {
    HelloGate gate({0x1234, Key(5), false, 4}, t0);
    HelloFacts h = Facts({0x01020304, 1000}, 1, 0, 0x1234);
    h.datagram_bytes = kMinHelloBytes - 1;
    CHECK(gate.Check(h, 0, t0).verdict == HelloVerdict::kDrop);
    h.datagram_bytes = kMaxHelloBytes + 1;
    CHECK(gate.Check(h, 0, t0).verdict == HelloVerdict::kDrop);
    CHECK(gate.Sources().size() == 0);
  }

  // A spoofed Hello holds nothing: the cookie reply is stateless (no pending entry, no slot), and
  // only the cookie's own tuple gets in. A replay from the same tuple is a retransmit.
  {
    HelloGate gate({0x1234, Key(6), false, 4}, t0);
    const Endpoint victim{0x08080808, 3000};
    HelloDecision d = gate.Check(Facts(victim, 9, 0, 0x1234), 0, t0);
    CHECK(d.verdict == HelloVerdict::kCookieReply && d.cookie != 0);
    CHECK(gate.Pending().Pending().empty());
    // The cookie from another port or nonce fails and gets a fresh cookie reply.
    CHECK(gate.Check(Facts({victim.address, 3001}, 9, d.cookie, 0x1234), 0, t0).verdict == HelloVerdict::kCookieReply);
    CHECK(gate.Check(Facts(victim, 10, d.cookie, 0x1234), 0, t0 + milliseconds(600)).verdict ==
          HelloVerdict::kCookieReply);
    // The right tuple waits for let in; the same tuple again is the same entry.
    HelloDecision w = gate.Check(Facts(victim, 9, d.cookie, 0x1234), 0, t0 + seconds(1));
    CHECK(w.verdict == HelloVerdict::kWait && w.pending_id != 0);
    HelloDecision again = gate.Check(Facts(victim, 9, d.cookie, 0x1234), 0, t0 + seconds(2));
    CHECK(again.verdict == HelloVerdict::kWait && again.pending_id == w.pending_id);
    CHECK(gate.Pending().Pending().size() == 1);
  }

  // The global bucket: 16 Hellos a second for the whole server, from many sources.
  {
    HelloGate gate({0x1234, Key(7), false, 4}, t0);
    int answered = 0;
    for (uint32_t i = 0; i < 100; ++i) {
      answered += gate.Check(Facts({0x0B000000 + i, 1000}, i, 0, 0), 0, t0).verdict != HelloVerdict::kDrop;
    }
    CHECK(answered == 16);
    CHECK(gate.Check(Facts({0x0C000000, 1000}, 1, 0, 0), 0, t0 + seconds(1)).verdict == HelloVerdict::kCookieReply);
  }

  // The per-source bucket: burst 4, then 2 a second; a new port does not make a new source.
  {
    HelloGate gate({0x1234, Key(8), false, 4}, t0);
    const uint32_t addr = 0x0D000001;
    int answered = 0;
    for (uint16_t p = 0; p < 10; ++p) {
      answered += gate.Check(Facts({addr, uint16_t(2000 + p)}, p, 0, 0), 0, t0).verdict != HelloVerdict::kDrop;
    }
    CHECK(answered == 4);
    CHECK(gate.Check(Facts({addr, 2000}, 1, 0, 0), 0, t0 + milliseconds(400)).verdict == HelloVerdict::kDrop);
    CHECK(gate.Check(Facts({addr, 2000}, 1, 0, 0), 0, t0 + milliseconds(700)).verdict == HelloVerdict::kCookieReply);
  }

  // One source flooding spends only its own burst of the global bucket: another source still gets
  // its cookie reply in the same instant.
  {
    HelloGate gate({0x1234, Key(14), false, 4}, t0);
    for (uint32_t i = 0; i < 100; ++i) gate.Check(Facts({0x0D000002, 2000}, i, 0, 0), 0, t0);
    CHECK(gate.Check(Facts({0x0D000003, 2000}, 1, 0, 0), 0, t0).verdict == HelloVerdict::kCookieReply);
  }

  // Council 8, Opus B3: a loopback source (the owner's own game) skips the global bucket and keeps
  // its own. A flood of spoofed Hellos from 1,000 addresses empties the global bucket, so another
  // network source is dropped, but the owner's loopback Hello still gets its cookie and gets in;
  // its source bucket still holds it to its burst of 4.
  {
    HelloGate gate({0x1234, Key(18), false, 4}, t0);
    for (uint32_t i = 0; i < 1000; ++i) gate.Check(Facts({0x50000000 + i, 1000}, i, 0, 0), 0, t0);
    CHECK(gate.Check(Facts({0x0D000004, 2000}, 1, 0, 0), 0, t0).verdict == HelloVerdict::kDrop);
    HelloFacts owner = Facts({skylanders::netplay::kLoopback, 5000}, 1, 0, 0);
    owner.owner = true;
    CHECK(Join(gate, owner, 0, t0).verdict == HelloVerdict::kAdmit);
    int answered = 0;
    for (uint64_t n = 2; n < 10; ++n) {
      answered += gate.Check(Facts({skylanders::netplay::kLoopback, 5000}, n, 0, 0), 0, t0).verdict !=
                  HelloVerdict::kDrop;
    }
    CHECK(answered == 2);  // the burst's other two, then dropped
    // Another loopback address (127.0.0.2) is loopback too.
    CHECK(gate.Check(Facts({0x7F000002, 5000}, 1, 0, 0), 0, t0).verdict == HelloVerdict::kCookieReply);
  }

  // The source table stays at 256 entries under 10,000 sources.
  {
    HelloGate gate({0x1234, Key(9), false, 4}, t0);
    TimePoint now = t0;
    for (uint32_t i = 0; i < 10000; ++i) {
      now += milliseconds(70);  // under the global rate
      gate.Check(Facts({0x20000000 + i, 1000}, i, 0, 0), 0, now);
      CHECK(gate.Sources().size() <= kMaxSources);
    }
    CHECK(gate.Sources().size() == kMaxSources);
    CHECK(gate.Sources().Find(0x20000000 + 9999) != nullptr);
    CHECK(gate.Sources().Find(0x20000000) == nullptr);  // the least recently seen went first
  }

  // Wrong secrets: refused "wrong code" only after a valid cookie; after 4 the source gets no
  // replies for 60 s; a spoofer without the cookie cannot spend the budget.
  {
    HelloGate gate({0x1234, Key(10), false, 4}, t0);
    const Endpoint a{0x0E000001, 4000};
    TimePoint now = t0;
    for (int i = 0; i < 3; ++i) {
      now += seconds(1);
      CHECK(Join(gate, Facts(a, 1, 0, 0x9999), 0, now).verdict == HelloVerdict::kWrongCode);
    }
    now += seconds(1);
    CHECK(Join(gate, Facts(a, 1, 0, 0x9999), 0, now).verdict == HelloVerdict::kWrongCode);  // the 4th
    CHECK(gate.Check(Facts(a, 1, 0, 0x1234), 0, now + seconds(1)).verdict == HelloVerdict::kDrop);
    CHECK(gate.Check(Facts(a, 1, 0, 0x1234), 0, now + seconds(59)).verdict == HelloVerdict::kDrop);
    CHECK(Join(gate, Facts(a, 1, 0, 0x1234), 0, now + seconds(61)).verdict == HelloVerdict::kWait);
    CHECK(gate.WrongSecrets() == 4);
    // Without a valid cookie a wrong secret only gets a cookie reply and counts nothing.
    gate.Check(Facts({0x0E000002, 4000}, 1, 0x5555, 0x9999), 0, now + seconds(62));
    CHECK(gate.WrongSecrets() == 4);
  }

  // Council 8, Opus B4: a sender cannot clear its wrong-secret strikes by pushing its own row out
  // of the table. Rows without strikes are evicted first, so after 3 strikes and 512 spoofed
  // sources (twice the table) the striking row is still there with its 3, and its 4th wrong secret
  // blocks it for 60 s.
  {
    HelloGate gate({0x1234, Key(19), false, 4}, t0);
    const Endpoint a{0x0E000010, 4000};
    TimePoint now = t0;
    for (int i = 0; i < 3; ++i) {
      now += seconds(1);
      CHECK(Join(gate, Facts(a, 1, 0, 0x9999), 0, now).verdict == HelloVerdict::kWrongCode);
    }
    for (uint32_t i = 0; i < 2 * kMaxSources; ++i) {
      now += milliseconds(70);  // under the global rate
      gate.Check(Facts({0x21000000 + i, 1000}, i, 0, 0), 0, now);
    }
    CHECK(gate.Sources().size() == kMaxSources);
    const SourceTable::Source* striking = gate.Sources().Find(a.address);
    CHECK(striking != nullptr && striking->wrong_secrets == 3);
    CHECK(gate.Sources().Find(0x21000000) == nullptr);  // a spoofed row went instead
    now += seconds(1);
    CHECK(Join(gate, Facts(a, 1, 0, 0x9999), 0, now).verdict == HelloVerdict::kWrongCode);  // the 4th
    CHECK(gate.Check(Facts(a, 1, 0, 0x1234), 0, now + seconds(1)).verdict == HelloVerdict::kDrop);
    CHECK(gate.Check(Facts(a, 1, 0, 0x1234), 0, now + seconds(59)).verdict == HelloVerdict::kDrop);
  }

  // The per-secret budget: 4096 wrong secrets, then no new joiner (the right secret included)
  // until a new code; a player already in is not this gate's business; the owner still gets in.
  {
    HelloGate gate({0x1234, Key(11), false, 4}, t0);
    TimePoint now = t0;
    uint32_t addr = 0x30000000;
    HelloVerdict last = HelloVerdict::kDrop;
    for (uint32_t i = 0; i < kWrongSecretBudget; ++i) {
      now += milliseconds(150);  // two Hellos a join, under the global rate
      last = Join(gate, Facts({addr++, 1000}, i, 0, 0x9999), 0, now).verdict;
    }
    CHECK(gate.CodeSpent());
    CHECK(last == HelloVerdict::kCodeSpent);
    now += seconds(1);
    CHECK(Join(gate, Facts({addr++, 1000}, 1, 0, 0x1234), 0, now).verdict == HelloVerdict::kCodeSpent);
    HelloFacts owner = Facts({0x7F000001, 5000}, 1, 0, 0);
    owner.owner = true;
    now += seconds(1);
    CHECK(Join(gate, owner, 0, now).verdict == HelloVerdict::kAdmit);
    gate.NewSecret(0x4321);
    CHECK(!gate.CodeSpent());
    now += seconds(1);
    CHECK(Join(gate, Facts({addr++, 1000}, 1, 0, 0x4321), 0, now).verdict == HelloVerdict::kWait);
  }

  // Let in: at most 4 pending, a fifth is busy, none holds a player slot; entries expire 60 s
  // after they were shown; the owner's answer admits or finds the session full.
  {
    HelloGate gate({0x1234, Key(12), false, 4}, t0);
    std::vector<uint32_t> ids;
    for (uint32_t i = 0; i < 4; ++i) {
      HelloDecision d = Join(gate, Facts({0x40000000 + i, 1000}, i, 0, 0x1234), 3, t0 + seconds(i));
      CHECK(d.verdict == HelloVerdict::kWait);
      ids.push_back(d.pending_id);
    }
    CHECK(Join(gate, Facts({0x40000010, 1000}, 9, 0, 0x1234), 3, t0 + seconds(5)).verdict == HelloVerdict::kBusy);
    PendingJoin let{};
    CHECK(gate.LetIn(ids[0], 3, t0 + seconds(5), &let) == HelloVerdict::kAdmit);
    CHECK(let.from.address == 0x40000000);
    CHECK(gate.LetIn(ids[0], 3, t0 + seconds(5), &let) == HelloVerdict::kDrop);  // answered already
    CHECK(gate.LetIn(ids[1], 4, t0 + seconds(5), &let) == HelloVerdict::kFull);
    CHECK(gate.Refuse(ids[2], t0 + seconds(5)));
    // ids[3] was shown at t0 + 3 s: still there at 62 s, gone at 63 s.
    CHECK(gate.ExpirePending(t0 + seconds(62)).empty());
    std::vector<PendingJoin> expired = gate.ExpirePending(t0 + seconds(63));
    CHECK(expired.size() == 1 && expired[0].id == ids[3]);
    CHECK(gate.Pending().Pending().empty());
    // A full session refuses before making a pending entry.
    CHECK(Join(gate, Facts({0x40000020, 1000}, 1, 0, 0x1234), 4, t0 + seconds(70)).verdict == HelloVerdict::kFull);
    CHECK(gate.Pending().Pending().empty());
  }

  // The gate's order around the caller's own checks: limits and the cookie first (an incompatible
  // Hello without a cookie gets only a cookie reply, so no refusal goes to an unproven address),
  // then the caller's checks, before the owner, the secret and let in (no wrong secret is counted
  // and no pending entry made for an incompatible joiner).
  {
    HelloGate gate({0x1234, Key(14), false, 4}, t0);
    HelloFacts h = Facts({0x60000001, 1000}, 7, 0, 0x9999);
    h.compatible = false;
    HelloDecision first = gate.Check(h, 1, t0);
    CHECK(first.verdict == HelloVerdict::kCookieReply);
    h.cookie = first.cookie;
    CHECK(gate.Check(h, 1, t0).verdict == HelloVerdict::kIncompatible);
    CHECK(gate.WrongSecrets() == 0);
    h.owner = true;
    CHECK(gate.Check(h, 1, t0 + milliseconds(600)).verdict == HelloVerdict::kIncompatible);
    h.secret = 0x1234;
    h.owner = false;
    CHECK(gate.Check(h, 1, t0 + milliseconds(1200)).verdict == HelloVerdict::kIncompatible);
    CHECK(gate.Pending().Pending().empty());
    // Undersized: dropped before anything, compatible or not.
    HelloFacts small = h;
    small.datagram_bytes = kMinHelloBytes - 1;
    CHECK(gate.Check(small, 1, t0 + seconds(2)).verdict == HelloVerdict::kDrop);
  }

  // The let-in state machine as the joiner sees it: wait (a new entry once, retransmits after),
  // then the owner's answer on its next Hello: admitted once (a later Hello of the tuple is the
  // connection's), refused ("not let in"), timed out, or full; another nonce from the same address
  // is another join; decisions are kept 30 s and at most 16.
  {
    HelloGate gate({0x1234, Key(15), false, 4}, t0);
    TimePoint now = t0;
    auto ask = [&](uint32_t address, uint64_t nonce, int players) {
      now += milliseconds(600);  // within the per-source rate
      return Join(gate, Facts({address, 1000}, nonce, 0, 0x1234), players, now);
    };
    HelloDecision a = ask(0x70000001, 1, 1);
    CHECK(a.verdict == HelloVerdict::kWait && a.new_pending);
    HelloDecision a2 = ask(0x70000001, 1, 1);
    CHECK(a2.verdict == HelloVerdict::kWait && !a2.new_pending && a2.pending_id == a.pending_id);
    HelloDecision b = ask(0x70000002, 1, 1);
    HelloDecision c = ask(0x70000003, 1, 1);
    HelloDecision e = ask(0x70000004, 1, 3);
    CHECK(b.new_pending && c.new_pending && e.new_pending);
    PendingJoin let{};
    CHECK(gate.LetIn(a.pending_id, 1, now, &let) == HelloVerdict::kAdmit);
    CHECK(gate.Refuse(b.pending_id, now));
    CHECK(!gate.Refuse(b.pending_id, now));  // answered already
    CHECK(gate.LetIn(e.pending_id, 4, now, &let) == HelloVerdict::kFull);
    CHECK(gate.DecidedCount() == 3);
    HelloDecision a3 = ask(0x70000001, 1, 1);
    CHECK(a3.verdict == HelloVerdict::kAdmit);
    CHECK(ask(0x70000001, 1, 1).verdict == HelloVerdict::kWait);  // used: a new entry now
    HelloDecision b2 = ask(0x70000002, 1, 1);
    CHECK(b2.verdict == HelloVerdict::kNotLetIn && !b2.timed_out);
    CHECK(ask(0x70000002, 2, 1).verdict == HelloVerdict::kWait);  // a new nonce: a new join
    CHECK(ask(0x70000004, 1, 1).verdict == HelloVerdict::kFull);
    // c expires 60 s after it was shown; its next Hello hears "timed out".
    now = t0 + seconds(70);
    std::vector<PendingJoin> gone = gate.ExpirePending(now);
    bool c_gone = false;
    for (const PendingJoin& j : gone) c_gone = c_gone || j.id == c.pending_id;
    CHECK(c_gone);
    HelloDecision c2 = ask(0x70000003, 1, 1);
    CHECK(c2.verdict == HelloVerdict::kNotLetIn && c2.timed_out);
    // 30 s later the decisions are gone: the same tuple is a new join.
    now += seconds(31);
    CHECK(ask(0x70000003, 1, 1).verdict == HelloVerdict::kWait);
    // Bounded: 20 refusals keep 16 decisions.
    HelloGate many({0x1234, Key(16), false, 4}, t0);
    TimePoint m = t0;
    for (uint32_t i = 0; i < 20; ++i) {
      m += milliseconds(100);
      HelloDecision w = Join(many, Facts({0x71000000 + i, 1000}, 1, 0, 0x1234), 1, m);
      REQUIRE_OR_RETURN(w.verdict == HelloVerdict::kWait);
      CHECK(many.Refuse(w.pending_id, m));
    }
    CHECK(many.DecidedCount() == kMaxDecided);
  }

  // Sol 8B F1: a join under way keeps the secret it was verified with. After New code (secret B)
  // the tuple still pending under A keeps waiting on A (the same entry, nothing counted wrong, its
  // source never blocked) and is admitted once let in; an already-refused tuple collects its
  // decision under A. Any other Hello with A (another nonce, another address) is a wrong code, so
  // is a tuple's Hello with a third secret, and a decided tuple's A once its decision is gone.
  {
    constexpr uint32_t kA = 0x1234, kB = 0x4321;
    HelloGate gate({kA, Key(17), false, 4}, t0);
    TimePoint now = t0;
    auto ask = [&](uint32_t address, uint64_t nonce, uint32_t secret) {
      now += milliseconds(1100);  // a Join (two Hellos) every 1.1 s holds the per-source rate
      return Join(gate, Facts({address, 1000}, nonce, 0, secret), 1, now);
    };
    const HelloDecision w = ask(0x72000001, 1, kA);  // pending, let in after New code
    const HelloDecision r = ask(0x72000002, 1, kA);  // pending, refused before New code
    REQUIRE_OR_RETURN(w.verdict == HelloVerdict::kWait && r.verdict == HelloVerdict::kWait);
    CHECK(gate.Refuse(r.pending_id, now));
    gate.NewSecret(kB);
    for (int i = 0; i < kWrongSecretsPerSource + 1; ++i) {
      const HelloDecision again = ask(0x72000001, 1, kA);
      CHECK(again.verdict == HelloVerdict::kWait && !again.new_pending &&
            again.pending_id == w.pending_id);
    }
    CHECK(gate.WrongSecrets() == 0);
    CHECK(ask(0x72000002, 1, kA).verdict == HelloVerdict::kNotLetIn);
    PendingJoin let{};
    CHECK(gate.LetIn(w.pending_id, 1, now, &let) == HelloVerdict::kAdmit);
    CHECK(let.secret == kA);
    CHECK(ask(0x72000001, 1, kA).verdict == HelloVerdict::kAdmit);
    CHECK(ask(0x72000001, 2, kA).verdict == HelloVerdict::kWrongCode);      // another nonce
    CHECK(ask(0x72000009, 1, kA).verdict == HelloVerdict::kWrongCode);      // another address
    CHECK(ask(0x72000002, 1, 0x7777).verdict == HelloVerdict::kWrongCode);  // a third secret
    CHECK(gate.WrongSecrets() == 3);
    CHECK(ask(0x7200000A, 1, kB).verdict == HelloVerdict::kWait);  // the new code works
    now += kDecidedLifetime;  // the refusal is gone: its A is an old code now
    CHECK(ask(0x72000002, 1, kA).verdict == HelloVerdict::kWrongCode);
    CHECK(gate.WrongSecrets() == 4);
  }

  // Sol 8B F1: a spent budget strands no join under way either. Entries made while the budget was
  // nearly spent: one let in before it ran out collects its admission after, one still waiting
  // keeps waiting and is admitted once let in; a new joiner with the same (current) secret hears
  // "code spent".
  {
    constexpr uint32_t kA = 0x1234;
    HelloGate gate({kA, Key(19), false, 4}, t0);
    TimePoint now = t0;
    uint32_t n = 0;
    auto wrong = [&] {
      now += milliseconds(150);  // two Hellos a join, under the global rate
      Join(gate, Facts({0x73000000 + n, 1000}, n, 0, 0x9999), 0, now);
      ++n;
    };
    while (gate.WrongSecrets() + 2 < kWrongSecretBudget && n < 2 * kWrongSecretBudget) wrong();
    REQUIRE_OR_RETURN(gate.WrongSecrets() + 2 == kWrongSecretBudget);
    auto join = [&](uint32_t address) {
      now += seconds(1);
      return Join(gate, Facts({address, 1000}, 1, 0, kA), 1, now);
    };
    const HelloDecision p1 = join(0x74000001);
    const HelloDecision p2 = join(0x74000002);
    REQUIRE_OR_RETURN(p1.verdict == HelloVerdict::kWait && p2.verdict == HelloVerdict::kWait);
    PendingJoin let{};
    CHECK(gate.LetIn(p1.pending_id, 1, now, &let) == HelloVerdict::kAdmit);
    wrong();
    wrong();
    REQUIRE_OR_RETURN(gate.CodeSpent());
    CHECK(join(0x74000001).verdict == HelloVerdict::kAdmit);
    CHECK(join(0x74000002).verdict == HelloVerdict::kWait);
    CHECK(gate.LetIn(p2.pending_id, 1, now, &let) == HelloVerdict::kAdmit);
    CHECK(join(0x74000002).verdict == HelloVerdict::kAdmit);
    CHECK(join(0x74000003).verdict == HelloVerdict::kCodeSpent);
    CHECK(gate.WrongSecrets() == kWrongSecretBudget);
  }

  // Sol 8B F2: the gate's capacity check knows the owner's Hello replaces the owner's connection:
  // a full session admits it when a welcomed owner is there (no new place), and still refuses an
  // owner Hello "full" when there is no owner to replace, and every other joiner.
  {
    HelloGate gate({0x1234, Key(18), true, 2}, t0);
    HelloFacts owner = Facts({0x7F000001, 5000}, 1, 0, 0);
    owner.owner = true;
    owner.replaces_owner = true;
    CHECK(Join(gate, owner, 2, t0).verdict == HelloVerdict::kAdmit);
    HelloFacts first = Facts({0x7F000002, 5000}, 1, 0, 0);
    first.owner = true;  // no owner in the session to replace
    CHECK(Join(gate, first, 2, t0 + seconds(1)).verdict == HelloVerdict::kFull);
    CHECK(Join(gate, first, 1, t0 + seconds(2)).verdict == HelloVerdict::kAdmit);
    HelloFacts joiner = Facts({0x7F000003, 5000}, 1, 0, 0x1234);
    joiner.replaces_owner = true;  // read only for an owner Hello
    CHECK(Join(gate, joiner, 2, t0 + seconds(3)).verdict == HelloVerdict::kFull);
    CHECK(Join(gate, joiner, 1, t0 + seconds(4)).verdict == HelloVerdict::kAdmit);
  }

  // "Let anyone with the code in": no pending entry, admitted while there is room.
  {
    HelloGate gate({0x1234, Key(13), true, 4}, t0);
    CHECK(Join(gate, Facts({0x50000001, 1000}, 1, 0, 0x1234), 2, t0).verdict == HelloVerdict::kAdmit);
    CHECK(Join(gate, Facts({0x50000002, 1000}, 1, 0, 0x1234), 4, t0 + seconds(1)).verdict == HelloVerdict::kFull);
    CHECK(Join(gate, Facts({0x50000003, 1000}, 1, 0, 0x9999), 2, t0 + seconds(2)).verdict == HelloVerdict::kWrongCode);
    CHECK(gate.Pending().Pending().empty());
  }

  return Finish("online_hello_guard");
}
