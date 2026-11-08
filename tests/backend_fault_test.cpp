// The fault injector (design sections 6 and 10, FI1 to FI4; work item 3): the rule grammar, the
// seed, the events, and each of the four layers on its own (FI-link, FI-message, FI-apply,
// FI-pump: this file's own ids, the layers the ordering cases use).

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <tuple>
#include <variant>
#include <vector>

#include "backend/fake_network.h"
#include "backend/fault_injector.h"
#include "backend/lanes.h"
#include "backend_lanes_harness.h"
#include "backend_test.h"

namespace skylanders::backend {
namespace {

using namespace std::chrono_literals;
using test::lanes::Ent;
using test::lanes::Key;
using test::lanes::kLanesTable;
using test::lanes::kStart;
using test::lanes::TestAdapter;
using test::lanes::Val;

std::vector<FaultRule> Rules(std::string_view text) {
  FaultParseError error;
  auto rules = ParseFaultRules(text, &error);
  if (!rules) {
    std::fprintf(stderr, "  parse '%.*s': %s\n", int(text.size()), text.data(), error.text.c_str());
    CHECK(rules.has_value());
    return {};
  }
  return *rules;
}

Record Row(KindId kind, uint32_t n, uint32_t revision) {
  Record r;
  r.key = Key(kind, Ent(1, n));
  r.revision = revision;
  r.epoch = 1;
  r.lsn = n;
  r.value = Val(n, 4);
  return r;
}

std::vector<uint8_t> Frame(std::initializer_list<Message> messages) {
  std::vector<uint8_t> f;
  for (const Message& m : messages) CHECK(AppendMessage(m, f));
  return f;
}

std::vector<Message> Messages(std::span<const uint8_t> frame) {
  std::vector<Message> out;
  CHECK(DecodeFrame(frame, out));
  return out;
}

// A link pair on a test clock, S's end and the client's end wrapped by one injector.
struct Wired {
  explicit Wired(std::string_view rules, uint64_t seed = 1, uint16_t peer = 1)
      : faults(Rules(rules), seed, kLanesTable, kStart) {
    auto [a, b] = MemoryLink::Pair(&now);
    server_raw = std::move(a);
    client_raw = std::move(b);
    server = faults.Wrap(*server_raw, peer, /*server_side=*/true);
    client = faults.Wrap(*client_raw, peer, /*server_side=*/false);
  }
  void Advance(Duration d) {
    now += d;
    faults.Advance(now);
  }
  // Everything the client's end delivers now.
  std::vector<std::pair<netplay::Lane, std::vector<uint8_t>>> Drain() {
    std::vector<std::pair<netplay::Lane, std::vector<uint8_t>>> out;
    netplay::Lane lane;
    std::vector<uint8_t> f;
    while (client->Receive(lane, f)) out.emplace_back(lane, f);
    return out;
  }
  std::vector<FaultEvent> Events() {
    std::vector<FaultEvent> out;
    FaultEvent e;
    while (faults.PollEvent(e)) out.push_back(e);
    return out;
  }

  TimePoint now = kStart;
  FaultInjector faults;
  std::unique_ptr<MemoryLink> server_raw, client_raw;
  std::unique_ptr<PeerLink> server, client;
};

}  // namespace

BACKEND_TEST("FI1", FaultRulesRoundTrip) {
  // Canonical text round-trips exactly; a rule list round-trips through its text; the bare
  // amounts take their canonical words.
  const std::string_view canonical[] = {
      "message:kill type=delta kind=prop nth=3 | link:throttle peer=3 bps=2000 | peer:disconnect "
      "peer=2 after=baseline_rows:5",
      "message:delay lane=state dir=s2c peer=1 delay=0-300",
      "link:full lane=state peer=1 type=delta key=a1.2.3@1 ms=1000",
      "apply:fail kind=test.toggle nth=1 | apply:late kind=test.toggle ms=300",
      "pump:stall peer=2 from=1000 ms=500",
      "link:drop dir=c2s pct=30 from=100 to=200 | link:reorder lane=fast | link:duplicate",
      "message:duplicate type=obligation | apply:stuck slot=2 | apply:despawn key=x5",
      "link:delay delay=250 | link:disconnect peer=4 | message:kill dir=c2s peer=2 type=ack from=10000",
      "message:kill dir=c2s peer=1 type=obligation_done id=1 nth=1 | link:full lane=state to=500 ms=20",
  };
  for (std::string_view text : canonical) {
    const auto rules = Rules(text);
    CHECK(!rules.empty());
    const std::string again = FormatFaultRules(rules);
    if (again != text) std::fprintf(stderr, "  %s\n  %s\n", std::string(text).c_str(), again.c_str());
    CHECK(again == text);
    CHECK(Rules(again) == rules);
  }
  // The example's three rules, as fields.
  const auto ex = Rules(canonical[0]);
  CHECK(ex.size() == 3);
  if (ex.size() == 3) {
    CHECK(ex[0].layer == FaultLayer::kMessage && ex[0].action == FaultAction::kKill);
    CHECK(ex[0].type == MessageType::kDelta && ex[0].kind == "prop" && ex[0].nth == 3);
    CHECK(ex[1].action == FaultAction::kThrottle && ex[1].peer == 3 && ex[1].amount == 2000);
    CHECK(ex[2].layer == FaultLayer::kPeer && ex[2].action == FaultAction::kDisconnect);
    CHECK(ex[2].peer == 2 && ex[2].after_baseline_rows == 5);
  }
  // Bare amounts, spaces, the range.
  CHECK(FormatFaultRules(Rules("apply:late 300")) == "apply:late ms=300");
  CHECK(FormatFaultRules(Rules("  message:delay 0-300   lane=state ")) ==
        "message:delay lane=state delay=0-300");
  CHECK(FormatFaultRules(Rules("link:throttle 2000 peer=3|pump:stall 5")) ==
        "link:throttle peer=3 bps=2000 | pump:stall ms=5");
  const auto range = Rules("message:delay delay=0-300");
  CHECK(range.size() == 1 && range[0].amount == 0 && range[0].amount_max == 300);
  // No rules: an empty list (the setting is then left out, never passed empty: RULES.md).
  CHECK(Rules("").empty());
  CHECK(Rules("   ").empty());
  CHECK(FormatFaultRules({}).empty());
  // Built in code, formatted, parsed back.
  FaultRule built;
  built.layer = FaultLayer::kMessage;
  built.action = FaultAction::kDelay;
  built.amount = 10;
  built.amount_max = 20;
  built.lane = netplay::Lane::kGameplay;
  built.direction = FaultDirection::kClientToServer;
  built.type = MessageType::kRequest;
  built.slot = 3;
  built.percent = 40;
  built.from_ms = 5;
  built.to_ms = 6;
  const std::vector<FaultRule> list{built};
  CHECK(Rules(FormatFaultRules(list)) == list);
}

BACKEND_TEST("FI2", FaultRulesRefuseUnknownWords) {
  // Each refusal names the word and its position (a byte offset from 0).
  struct Case {
    std::string_view text;
    size_t position;
    std::string_view says;
  };
  const Case cases[] = {
      {"message:kill typ=delta", 13, "unknown word 'typ'"},
      {"link:drop | messag:kill", 12, "unknown layer 'messag'"},
      {"link:explode", 5, "unknown action 'explode'"},
      {"apply:fail type=delta", 11, "unknown word 'type'"},
      {"pump:stall ms=5 lane=state", 16, "unknown word 'lane'"},
      {"link:full peer=1", 0, "no amount"},
      {"link:drop; link:full 5", 9, "unknown word ';'"},
      {"link:reorder lane=state", 0, "fast lane only"},
      {"link:drop pct=101", 14, "bad pct"},
      {"link:drop nth=0", 14, "bad nth"},
      {"link:drop | | pump:stall 5", 11, "empty rule"},  // the empty clause starts after the first |
      {"pump:stall 5 ms=6", 16, "second amount"},
      {"link:throttle bps=0", 0, "above 0"},
      {"link:drop lane=slow", 15, "bad lane"},
      {"message:kill type=deltas", 18, "bad type"},
      {"link:full 300-400", 10, "a range only for delay"},
      {"link:drop 5", 10, "unknown word '5'"},
      {"link:drop after=rows:3", 16, "bad after"},
      {"killall", 0, "unknown word 'killall'"},
  };
  for (const Case& c : cases) {
    FaultParseError error;
    const auto rules = ParseFaultRules(c.text, &error);
    CHECK(!rules.has_value());
    const bool right = error.position == c.position && error.text.find(c.says) != std::string::npos;
    if (!right) {
      std::fprintf(stderr, "  '%.*s': %zu %s\n", int(c.text.size()), c.text.data(), error.position,
                   error.text.c_str());
    }
    CHECK(right);
  }
}

BACKEND_TEST("FI3", FaultSameSeedSameRun) {
  // Everything random comes from the seed: the same seed plays the same faults at the same times
  // and delivers the same frames; another seed plays others.
  constexpr std::string_view kRules =
      "link:drop lane=state pct=50 | message:delay lane=gameplay delay=0-300 | "
      "link:duplicate lane=gameplay pct=25 | message:kill type=through pct=20";
  auto run = [&](uint64_t seed) {
    Wired w(kRules, seed);
    std::vector<std::string> trace;
    for (uint32_t i = 1; i <= 200; ++i) {
      w.server->Send(netplay::Lane::kState, Frame({msg::Delta{Row(test::kProp, i, 1), i}, msg::Through{i}}));
      msg::Decision d;
      d.decision.key = RequestKey{1, 1, 1, i};
      w.server->Send(netplay::Lane::kGameplay, Frame({d}));
      w.Advance(10ms);
      for (const auto& [lane, f] : w.Drain()) {
        trace.push_back(std::to_string((w.now - kStart).count()) + " " +
                        std::string(LaneName(lane)) + " " + std::to_string(f.size()) + " " +
                        std::to_string(ValueDigest(f)));
      }
      for (const FaultEvent& e : w.Events()) {
        trace.push_back(std::to_string((e.at - kStart).count()) + " " + FaultLine(e));
      }
    }
    w.Advance(1s);
    for (const auto& [lane, f] : w.Drain()) trace.push_back(std::string(LaneName(lane)) + " late");
    return trace;
  };
  for (int s = 1; s <= test::Seeds(); ++s) {
    const auto a = run(uint64_t(s));
    const auto b = run(uint64_t(s));
    CHECK(a == b);
    CHECK(!a.empty());
    CHECK(run(uint64_t(s) + 1000) != a);
  }
}

BACKEND_TEST("FI4", FaultEveryFireIsAnEvent) {
  // One rule per layer that fires, one that never matches. Every fire is one event naming its
  // rule (from 1) and what it hit; Fired counts them; NotFired names the rule that never fired (a
  // case "not reached"); the line is `Online: backend fault <rule n> <action> <what>`.
  Wired w("message:kill type=delta kind=test.prop | link:drop lane=gameplay nth=2 | "
          "apply:fail kind=test.toggle | pump:stall peer=1 ms=100 | link:full peer=9 ms=50");
  for (uint32_t i = 1; i <= 3; ++i) {
    w.server->Send(netplay::Lane::kState, Frame({msg::Delta{Row(test::kProp, i, 1), i}, msg::Through{i}}));
    msg::Decision d;
    d.decision.key = RequestKey{1, 1, 1, i};
    w.server->Send(netplay::Lane::kGameplay, Frame({d}));
  }
  w.Advance(10ms);
  const auto frames = w.Drain();
  size_t deltas = 0, throughs = 0, decisions = 0;
  for (const auto& [lane, f] : frames) {
    for (const Message& m : Messages(f)) {
      deltas += std::holds_alternative<msg::Delta>(m);
      throughs += std::holds_alternative<msg::Through>(m);
      decisions += std::holds_alternative<msg::Decision>(m);
    }
  }
  CHECK(deltas == 0);
  CHECK(throughs == 3);
  CHECK(decisions == 2);  // the second one dropped
  IdentityMap fake_identity;
  FakeWorld fake_world(fake_identity);
  const Handle fake_h = fake_world.Spawn(SessionObject(1), kStart);
  TestAdapter toggle(fake_world, test::kToggle);
  auto wrapped = w.faults.WrapAdapter(toggle, 1, test::kToggle, nullptr);
  CHECK(wrapped->Apply(fake_h, 0, Val(1, 1)) == AdapterResult::kFailed);
  CHECK(w.faults.Stalled(1));
  const auto events = w.Events();
  CHECK(events.size() == 3 + 1 + 1 + 1);
  std::vector<uint64_t> per_rule(5, 0);
  for (const FaultEvent& e : events) {
    CHECK(e.rule >= 1 && e.rule <= 5);
    if (e.rule >= 1 && e.rule <= 5) ++per_rule[e.rule - 1];
    CHECK(!e.what.empty());
    CHECK(e.action == w.faults.Rules()[e.rule - 1].action);
  }
  for (size_t i = 0; i < 5; ++i) CHECK(per_rule[i] == w.faults.Fired(i));
  CHECK(w.faults.Fired(0) == 3);
  CHECK(w.faults.Matched(1) == 3);  // three decisions matched; only the second fired
  CHECK(w.faults.Fired(1) == 1);
  CHECK(w.faults.NotFired() == std::vector<size_t>{4});
  for (const FaultEvent& e : events) {
    if (e.rule != 1) continue;
    CHECK(FaultLine(e).starts_with("Online: backend fault 1 message:kill peer 1 s2c state delta test.prop "));
  }
}

BACKEND_TEST("FI-link", FaultLinkLayer) {
  // drop: the sender hears kSent, nothing arrives (below the sender: it never learns).
  {
    Wired w("link:drop type=through nth=1");
    CHECK(w.server->Send(netplay::Lane::kState, Frame({msg::Through{1}})) == LinkSend::kSent);
    CHECK(w.server->Send(netplay::Lane::kState, Frame({msg::Through{2}})) == LinkSend::kSent);
    w.Advance(1ms);
    const auto got = w.Drain();
    CHECK(got.size() == 1);
    if (got.size() == 1) CHECK(std::get<msg::Through>(Messages(got[0].second)[0]).lsn == 2);
  }
  // delay: the lane keeps its order (a later frame never passes a delayed one); another lane is
  // not delayed; delayed frames count as unacked.
  {
    Wired w("link:delay lane=state nth=1 delay=100");
    w.server->Send(netplay::Lane::kState, Frame({msg::Through{1}}));
    w.server->Send(netplay::Lane::kState, Frame({msg::Through{2}}));
    msg::Decision d;
    w.server->Send(netplay::Lane::kGameplay, Frame({d}));
    CHECK(w.server->UnackedFrames(netplay::Lane::kState) == 2);
    w.Advance(1ms);
    auto got = w.Drain();
    CHECK(got.size() == 1);
    if (!got.empty()) CHECK(got[0].first == netplay::Lane::kGameplay);
    w.Advance(100ms);
    got = w.Drain();
    CHECK(got.size() == 2);
    if (got.size() == 2) {
      CHECK(std::get<msg::Through>(Messages(got[0].second)[0]).lsn == 1);
      CHECK(std::get<msg::Through>(Messages(got[1].second)[0]).lsn == 2);
    }
  }
  // duplicate: the frame arrives twice.
  {
    Wired w("link:duplicate type=delta");
    w.server->Send(netplay::Lane::kState, Frame({msg::Delta{Row(test::kProp, 1, 1), 1}}));
    w.Advance(1ms);
    CHECK(w.Drain().size() == 2);
  }
  // reorder (fast lane): the held frame goes after the next one.
  {
    Wired w("link:reorder nth=1");
    w.server->Send(netplay::Lane::kFast, Frame({msg::Effect{1, {Row(test::kProp, 1, 1)}}}));
    w.server->Send(netplay::Lane::kFast, Frame({msg::Effect{2, {Row(test::kProp, 2, 1)}}}));
    w.Advance(1ms);
    const auto got = w.Drain();
    CHECK(got.size() == 2);
    if (got.size() == 2) {
      CHECK(std::get<msg::Effect>(Messages(got[0].second)[0]).tick == 2);
      CHECK(std::get<msg::Effect>(Messages(got[1].second)[0]).tick == 1);
    }
  }
  // throttle: frames leave at the rate through a buffer of one second of it (one frame at 2,000
  // bytes a second); beyond it the link answers kFull; the buffered frame counts as unacked; one
  // event, when it first applies.
  {
    Wired w("link:throttle peer=1 dir=s2c bps=2000");
    std::vector<uint8_t> big;
    for (uint32_t i = 1; AppendMessage(msg::Delta{Row(test::kProp, i, 1), i}, big); ++i) {
    }
    size_t accepted = 0, refused = 0, arrived = 0, max_unacked = 0;
    for (int ms = 0; ms < 12000; ms += 10) {
      const LinkSend r = w.server->Send(netplay::Lane::kState, big);
      accepted += r == LinkSend::kSent;
      refused += r == LinkSend::kFull;
      max_unacked = std::max(max_unacked, w.server->UnackedFrames(netplay::Lane::kState));
      w.Advance(10ms);
      arrived += w.Drain().size();
    }
    // 2,000 bytes a second for 12 s and one frame of burst: about 21 frames of some 1,150 bytes.
    CHECK(arrived >= 19 && arrived <= 23);
    CHECK(refused > 1000);
    CHECK(accepted >= arrived);
    CHECK(accepted <= arrived + 1);  // the buffer holds one frame
    CHECK(max_unacked <= 3);         // the buffered frame and the inner link's
    CHECK(w.faults.Fired(0) == 1);
    // The other direction is not throttled.
    for (int i = 0; i < 5; ++i) CHECK(w.client->Send(netplay::Lane::kState, big) == LinkSend::kSent);
  }
  // full: kFull for the rule's time from its first match, on every lane (no lane named); then the
  // frames go; it fires once.
  {
    Wired w("link:full peer=1 type=delta ms=500");
    const auto f = Frame({msg::Delta{Row(test::kProp, 1, 1), 1}});
    CHECK(w.server->Send(netplay::Lane::kState, Frame({msg::Through{0}})) == LinkSend::kSent);
    CHECK(w.server->Send(netplay::Lane::kState, f) == LinkSend::kFull);
    CHECK(w.server->Send(netplay::Lane::kGameplay, Frame({msg::LeaseRenew{1}})) == LinkSend::kFull);
    w.Advance(499ms);
    CHECK(w.server->Send(netplay::Lane::kState, f) == LinkSend::kFull);
    w.Advance(1ms);
    CHECK(w.server->Send(netplay::Lane::kState, f) == LinkSend::kSent);
    CHECK(w.server->Send(netplay::Lane::kState, f) == LinkSend::kSent);
    CHECK(w.faults.Fired(0) == 1);
  }
  // disconnect after the nth baseline chunk: the frame after chunk 2 finds the link gone.
  {
    Wired w("peer:disconnect peer=1 after=baseline_rows:2");
    msg::BaselineRows rows;
    rows.id = 1;
    rows.records = {Row(test::kProp, 1, 1)};
    CHECK(w.server->Send(netplay::Lane::kState, Frame({rows})) == LinkSend::kSent);
    CHECK(w.server->Connected());
    CHECK(w.server->Send(netplay::Lane::kState, Frame({rows})) == LinkSend::kSent);
    w.Advance(1ms);
    CHECK(!w.server->Connected());
    CHECK(w.server->Send(netplay::Lane::kState, Frame({rows})) == LinkSend::kGone);
    CHECK(w.faults.Fired(0) == 1);
  }
}

BACKEND_TEST("FI-message", FaultMessageLayer) {
  // kill: after the lane delivered the frame, before the backend reads it; the other messages of
  // the frame stay; the sender sees nothing (Sol F1).
  {
    Wired w("message:kill type=delta key=" + ToText(Ent(1, 2)));
    w.server->Send(netplay::Lane::kState,
                   Frame({msg::Delta{Row(test::kProp, 1, 1), 1}, msg::Delta{Row(test::kProp, 2, 1), 2},
                          msg::Through{2}}));
    CHECK(w.server->UnackedFrames(netplay::Lane::kState) == 1);
    w.Advance(1ms);
    const auto got = w.Drain();
    CHECK(got.size() == 1);
    if (got.size() == 1) {
      const auto m = Messages(got[0].second);
      CHECK(m.size() == 2);
      if (m.size() == 2) {
        CHECK(std::get<msg::Delta>(m[0]).first_lsn == 1);
        CHECK(std::holds_alternative<msg::Through>(m[1]));
      }
    }
    w.Advance(1s);
    CHECK(w.server->UnackedFrames(netplay::Lane::kState) == 0);  // acked as delivered
  }
  // delay per lane: the state lane late as a whole, its order kept; gameplay not delayed.
  {
    Wired w("message:delay lane=state dir=s2c delay=200-300", 9);
    for (uint32_t i = 1; i <= 20; ++i) {
      w.server->Send(netplay::Lane::kState, Frame({msg::Through{i}}));
      msg::Decision d;
      d.decision.key.sequence = i;
      w.server->Send(netplay::Lane::kGameplay, Frame({d}));
      w.Advance(5ms);  // all 20 inside 100 ms, before the first state frame is due
      for (const auto& [lane, f] : w.Drain()) CHECK(lane == netplay::Lane::kGameplay);
    }
    w.Advance(200ms);
    uint64_t next = 1;
    for (int i = 0; i < 40; ++i) {
      for (const auto& [lane, f] : w.Drain()) {
        CHECK(lane == netplay::Lane::kState);
        CHECK(std::get<msg::Through>(Messages(f)[0]).lsn == next);
        ++next;
      }
      w.Advance(10ms);
    }
    CHECK(next == 21);
  }
  // duplicate: the message arrives twice, the frame's others once.
  {
    Wired w("message:duplicate type=obligation");
    msg::Obligation o{1, 1, Scope::kLevel, 5, Key(test::kEvent, Ent(1, 1)), Val(1, 2)};
    w.server->Send(netplay::Lane::kState, Frame({o, msg::Through{5}}));
    w.Advance(1ms);
    size_t obligations = 0, throughs = 0;
    for (const auto& [lane, f] : w.Drain()) {
      for (const Message& m : Messages(f)) {
        obligations += std::holds_alternative<msg::Obligation>(m);
        throughs += std::holds_alternative<msg::Through>(m);
      }
    }
    CHECK(obligations == 2);
    CHECK(throughs == 1);
  }
  // Direction: a c2s kill does not touch what S sends.
  {
    Wired w("message:kill dir=c2s type=through");
    w.server->Send(netplay::Lane::kState, Frame({msg::Through{1}}));
    w.Advance(1ms);
    CHECK(w.Drain().size() == 1);
    w.client->Send(netplay::Lane::kState, Frame({msg::Through{1}, msg::Ack{1, 0, 0, 0}}));
    w.Advance(1ms);
    netplay::Lane lane;
    std::vector<uint8_t> f;
    CHECK(w.server->Receive(lane, f));
    const auto m = Messages(f);
    CHECK(m.size() == 1 && std::holds_alternative<msg::Ack>(m[0]));
  }
}

BACKEND_TEST("FI-apply", FaultApplyLayer) {
  TimePoint now = kStart;
  FaultInjector faults(Rules("apply:fail kind=test.toggle nth=1 | apply:stuck kind=test.gate | "
                             "apply:despawn kind=test.scene | apply:late peer=2 kind=test.prop ms=300"),
                       1, kLanesTable, now);
  IdentityMap identity;
  identity.OnEpoch(1);
  FakeWorld world(identity);
  const EntityId e = Ent(1, 1);
  const Handle h7 = world.Spawn(e, now);
  TestAdapter toggle(world, test::kToggle), gate(world, test::kGate), scene(world, test::kScene);
  auto t = faults.WrapAdapter(toggle, 1, test::kToggle, &identity);
  CHECK(t->Apply(h7, 0, Val(1, 1)) == AdapterResult::kFailed);
  CHECK(toggle.Value(h7, 0) == nullptr);
  CHECK(t->Apply(h7, 0, Val(1, 1)) == AdapterResult::kOk);  // nth=1: the second goes through
  CHECK(toggle.Value(h7, 0) != nullptr);
  auto g = faults.WrapAdapter(gate, 1, test::kGate, &identity);
  CHECK(g->Apply(h7, 0, Val(2, 1)) == AdapterResult::kOk);
  CHECK(gate.Value(h7, 0) == nullptr);  // stuck: the native kept its own value
  Handle despawned = 0;
  auto s = faults.WrapAdapter(scene, 1, test::kScene, &identity, [&](Handle h) { despawned = h; });
  CHECK(s->Apply(h7, 0, Val(3, 1)) == AdapterResult::kGone);
  CHECK(despawned == h7);
  CHECK(faults.SpawnDelay(2, Key(test::kProp, e)) == Duration(300ms));
  CHECK(!faults.SpawnDelay(1, Key(test::kProp, e)));
  CHECK(!faults.SpawnDelay(2, Key(test::kToggle, e)));
  CHECK(faults.NotFired().empty());
}

BACKEND_TEST("FI-pump", FaultPumpStall) {
  TimePoint now = kStart;
  FaultInjector faults(Rules("pump:stall peer=2 from=1000 ms=500"), 1, kLanesTable, now);
  size_t stalled = 0;
  for (int ms = 0; ms < 3000; ms += 10) {
    faults.Advance(kStart + std::chrono::milliseconds(ms));
    CHECK(!faults.Stalled(1));
    if (faults.Stalled(2)) {
      ++stalled;
      CHECK(ms >= 1000 && ms < 1500);
    }
  }
  CHECK(stalled == 50);
  CHECK(faults.Fired(0) == 1);
}

}  // namespace skylanders::backend
