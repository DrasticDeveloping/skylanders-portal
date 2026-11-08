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
    CHECK(gate.LetIn(ids[0], 3, &let) == HelloVerdict::kAdmit);
    CHECK(let.from.address == 0x40000000);
    CHECK(gate.LetIn(ids[0], 3, &let) == HelloVerdict::kDrop);  // answered already
    CHECK(gate.LetIn(ids[1], 4, &let) == HelloVerdict::kFull);
    CHECK(gate.Refuse(ids[2]));
    // ids[3] was shown at t0 + 3 s: still there at 62 s, gone at 63 s.
    CHECK(gate.ExpirePending(t0 + seconds(62)).empty());
    std::vector<PendingJoin> expired = gate.ExpirePending(t0 + seconds(63));
    CHECK(expired.size() == 1 && expired[0].id == ids[3]);
    CHECK(gate.Pending().Pending().empty());
    // A full session refuses before making a pending entry.
    CHECK(Join(gate, Facts({0x40000020, 1000}, 1, 0, 0x1234), 4, t0 + seconds(70)).verdict == HelloVerdict::kFull);
    CHECK(gate.Pending().Pending().empty());
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
