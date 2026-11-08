#include "backend/fault_injector.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <deque>
#include <map>
#include <set>

#include "netplay/lanes.h"

namespace skylanders::backend {

namespace {

// --- The words each action takes.

enum Word : uint32_t {
  kWordLane = 1u << 0,
  kWordDir = 1u << 1,
  kWordPeer = 1u << 2,
  kWordType = 1u << 3,
  kWordKind = 1u << 4,
  kWordKey = 1u << 5,
  kWordSlot = 1u << 6,
  kWordNth = 1u << 7,
  kWordPct = 1u << 8,
  kWordFrom = 1u << 9,
  kWordTo = 1u << 10,
  kWordAfter = 1u << 11,
  kWordId = 1u << 12,
};
constexpr uint32_t kFrameWords = kWordLane | kWordDir | kWordPeer | kWordType | kWordKind |
                                 kWordKey | kWordSlot | kWordId | kWordNth | kWordPct | kWordFrom |
                                 kWordTo | kWordAfter;
constexpr uint32_t kApplyWords =
    kWordPeer | kWordKind | kWordKey | kWordSlot | kWordNth | kWordPct | kWordFrom | kWordTo;

enum class Amount : uint8_t { kNone, kMs, kDelay, kBps };

struct ActionSpec {
  FaultLayer layer;
  FaultAction action;
  std::string_view name;
  uint32_t words;
  Amount amount;
};
constexpr ActionSpec kActions[] = {
    {FaultLayer::kLink, FaultAction::kDrop, "drop", kFrameWords, Amount::kNone},
    {FaultLayer::kLink, FaultAction::kDelay, "delay", kFrameWords, Amount::kDelay},
    {FaultLayer::kLink, FaultAction::kDuplicate, "duplicate", kFrameWords, Amount::kNone},
    {FaultLayer::kLink, FaultAction::kReorder, "reorder", kFrameWords, Amount::kNone},
    {FaultLayer::kLink, FaultAction::kThrottle, "throttle",
     kWordLane | kWordDir | kWordPeer | kWordFrom | kWordTo, Amount::kBps},
    {FaultLayer::kLink, FaultAction::kFull, "full", kFrameWords, Amount::kMs},
    {FaultLayer::kLink, FaultAction::kDisconnect, "disconnect",
     kWordPeer | kWordFrom | kWordTo | kWordAfter, Amount::kNone},
    {FaultLayer::kPeer, FaultAction::kDisconnect, "disconnect",
     kWordPeer | kWordFrom | kWordTo | kWordAfter, Amount::kNone},
    {FaultLayer::kMessage, FaultAction::kKill, "kill", kFrameWords, Amount::kNone},
    {FaultLayer::kMessage, FaultAction::kDelay, "delay", kFrameWords, Amount::kDelay},
    {FaultLayer::kMessage, FaultAction::kDuplicate, "duplicate", kFrameWords, Amount::kNone},
    {FaultLayer::kApply, FaultAction::kFail, "fail", kApplyWords, Amount::kNone},
    {FaultLayer::kApply, FaultAction::kStuck, "stuck", kApplyWords, Amount::kNone},
    {FaultLayer::kApply, FaultAction::kDespawn, "despawn", kApplyWords, Amount::kNone},
    {FaultLayer::kApply, FaultAction::kLate, "late", kApplyWords, Amount::kMs},
    {FaultLayer::kPump, FaultAction::kStall, "stall",
     kWordPeer | kWordNth | kWordPct | kWordFrom | kWordTo, Amount::kMs},
};

const ActionSpec* FindAction(FaultLayer layer, FaultAction action) {
  for (const ActionSpec& a : kActions) {
    if (a.layer == layer && a.action == action) return &a;
  }
  return nullptr;
}

std::optional<FaultLayer> LayerFromName(std::string_view s) {
  if (s == "link") return FaultLayer::kLink;
  if (s == "peer") return FaultLayer::kPeer;
  if (s == "message") return FaultLayer::kMessage;
  if (s == "apply") return FaultLayer::kApply;
  if (s == "pump") return FaultLayer::kPump;
  return std::nullopt;
}

template <typename T>
std::optional<T> Number(std::string_view s) {
  T v{};
  if (s.empty()) return std::nullopt;
  auto [end, ec] = std::from_chars(s.data(), s.data() + s.size(), v);
  if (ec != std::errc() || end != s.data() + s.size()) return std::nullopt;
  return v;
}

std::string_view DirectionName(FaultDirection d) {
  switch (d) {
    case FaultDirection::kServerToClient: return "s2c";
    case FaultDirection::kClientToServer: return "c2s";
    case FaultDirection::kAny: break;
  }
  return "any";
}

struct Token {
  std::string_view text;
  size_t position;
};

std::vector<Token> Split(std::string_view s, size_t base, char sep) {
  std::vector<Token> out;
  size_t i = 0;
  while (i <= s.size()) {
    size_t j = s.find(sep, i);
    if (j == std::string_view::npos) j = s.size();
    out.push_back({s.substr(i, j - i), base + i});
    i = j + 1;
  }
  return out;
}

std::vector<Token> Words(std::string_view s, size_t base) {
  std::vector<Token> out;
  size_t i = 0;
  while (i < s.size()) {
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
    if (i >= s.size()) break;
    size_t j = i;
    while (j < s.size() && s[j] != ' ' && s[j] != '\t') ++j;
    out.push_back({s.substr(i, j - i), base + i});
    i = j;
  }
  return out;
}

bool Fail(FaultParseError* error, size_t position, std::string text) {
  if (error) {
    error->position = position;
    error->text = std::move(text) + " at " + std::to_string(position);
  }
  return false;
}

// One rule from its words. false with `*error` set.
bool ParseRule(const std::vector<Token>& words, FaultRule& rule, FaultParseError* error) {
  const Token& head = words.front();
  const size_t colon = head.text.find(':');
  if (colon == std::string_view::npos) {
    return Fail(error, head.position, "unknown word '" + std::string(head.text) + "'");
  }
  auto layer = LayerFromName(head.text.substr(0, colon));
  if (!layer) {
    return Fail(error, head.position,
                "unknown layer '" + std::string(head.text.substr(0, colon)) + "'");
  }
  const std::string_view action_name = head.text.substr(colon + 1);
  const ActionSpec* spec = nullptr;
  for (const ActionSpec& a : kActions) {
    if (a.layer == *layer && a.name == action_name) spec = &a;
  }
  if (!spec) {
    return Fail(error, head.position + colon + 1,
                "unknown action '" + std::string(action_name) + "'");
  }
  rule = FaultRule{};
  rule.layer = spec->layer;
  rule.action = spec->action;
  bool have_amount = false;

  auto set_amount = [&](std::string_view v, size_t pos) {
    if (have_amount) return Fail(error, pos, "second amount '" + std::string(v) + "'");
    if (spec->amount == Amount::kNone) {
      return Fail(error, pos, "unknown word '" + std::string(v) + "'");
    }
    const size_t dash = v.find('-');
    if (dash != std::string_view::npos) {
      if (spec->amount != Amount::kDelay) {
        return Fail(error, pos, "a range only for delay '" + std::string(v) + "'");
      }
      auto lo = Number<uint32_t>(v.substr(0, dash));
      auto hi = Number<uint32_t>(v.substr(dash + 1));
      if (!lo || !hi || *hi < *lo) return Fail(error, pos, "bad range '" + std::string(v) + "'");
      rule.amount = *lo;
      rule.amount_max = *hi;
    } else {
      auto n = Number<uint32_t>(v);
      if (!n) return Fail(error, pos, "bad number '" + std::string(v) + "'");
      rule.amount = *n;
      rule.amount_max = *n;
    }
    have_amount = true;
    return true;
  };

  for (size_t w = 1; w < words.size(); ++w) {
    const Token& t = words[w];
    const size_t eq = t.text.find('=');
    if (eq == std::string_view::npos) {
      // A bare amount after the action.
      if (!set_amount(t.text, t.position)) return false;
      continue;
    }
    const std::string_view name = t.text.substr(0, eq);
    const std::string_view value = t.text.substr(eq + 1);
    const size_t vpos = t.position + eq + 1;
    auto need = [&](uint32_t bit) {
      if ((spec->words & bit) == 0) {
        return Fail(error, t.position,
                    "unknown word '" + std::string(name) + "' for " +
                        std::string(FaultLayerName(spec->layer)) + ":" + std::string(spec->name));
      }
      return true;
    };
    auto bad = [&](std::string_view what) {
      return Fail(error, vpos, "bad " + std::string(what) + " '" + std::string(value) + "'");
    };
    if (name == "lane") {
      if (!need(kWordLane)) return false;
      auto lane = LaneFromName(value);
      if (!lane) return bad("lane");
      rule.lane = *lane;
    } else if (name == "dir") {
      if (!need(kWordDir)) return false;
      if (value == "s2c") rule.direction = FaultDirection::kServerToClient;
      else if (value == "c2s") rule.direction = FaultDirection::kClientToServer;
      else return bad("dir");
    } else if (name == "peer") {
      if (!need(kWordPeer)) return false;
      auto n = Number<uint16_t>(value);
      if (!n) return bad("peer");
      rule.peer = *n;
    } else if (name == "type") {
      if (!need(kWordType)) return false;
      auto type = MessageTypeFromName(value);
      if (!type) return bad("type");
      rule.type = *type;
    } else if (name == "kind") {
      if (!need(kWordKind)) return false;
      if (value.empty()) return bad("kind");
      rule.kind = std::string(value);
    } else if (name == "key") {
      if (!need(kWordKey)) return false;
      if (value.empty()) return bad("key");
      rule.key = std::string(value);
    } else if (name == "slot") {
      if (!need(kWordSlot)) return false;
      auto n = Number<uint16_t>(value);
      if (!n) return bad("slot");
      rule.slot = *n;
    } else if (name == "id") {
      if (!need(kWordId)) return false;
      auto n = Number<uint64_t>(value);
      if (!n || *n == 0) return bad("id");
      rule.obligation = *n;
    } else if (name == "nth") {
      if (!need(kWordNth)) return false;
      auto n = Number<uint32_t>(value);
      if (!n || *n == 0) return bad("nth");
      rule.nth = *n;
    } else if (name == "pct") {
      if (!need(kWordPct)) return false;
      auto n = Number<uint32_t>(value);
      if (!n || *n > 100) return bad("pct");
      rule.percent = *n;
    } else if (name == "from") {
      if (!need(kWordFrom)) return false;
      auto n = Number<uint32_t>(value);
      if (!n) return bad("from");
      rule.from_ms = *n;
    } else if (name == "to") {
      if (!need(kWordTo)) return false;
      auto n = Number<uint32_t>(value);
      if (!n) return bad("to");
      rule.to_ms = *n;
    } else if (name == "after") {
      if (!need(kWordAfter)) return false;
      constexpr std::string_view kRows = "baseline_rows:";
      if (!value.starts_with(kRows)) return bad("after");
      auto n = Number<uint32_t>(value.substr(kRows.size()));
      if (!n || *n == 0) return bad("after");
      rule.after_baseline_rows = *n;
    } else if (name == "ms") {
      if (spec->amount != Amount::kMs) {
        return Fail(error, t.position, "unknown word '" + std::string(name) + "'");
      }
      if (value.find('-') != std::string_view::npos) return bad("ms");
      if (!set_amount(value, vpos)) return false;
    } else if (name == "delay") {
      if (spec->amount != Amount::kDelay) {
        return Fail(error, t.position, "unknown word '" + std::string(name) + "'");
      }
      if (!set_amount(value, vpos)) return false;
    } else if (name == "bps") {
      if (spec->amount != Amount::kBps) {
        return Fail(error, t.position, "unknown word '" + std::string(name) + "'");
      }
      if (!set_amount(value, vpos)) return false;
    } else {
      return Fail(error, t.position, "unknown word '" + std::string(name) + "'");
    }
  }
  if (spec->amount != Amount::kNone && !have_amount) {
    return Fail(error, head.position, "no amount for " + std::string(head.text));
  }
  if (spec->amount == Amount::kBps && rule.amount == 0) {
    return Fail(error, head.position, "throttle needs bps above 0");
  }
  if (rule.action == FaultAction::kReorder && rule.lane && *rule.lane != netplay::Lane::kFast) {
    return Fail(error, head.position, "reorder is fast lane only");
  }
  if (rule.from_ms && rule.to_ms && *rule.to_ms < *rule.from_ms) {
    return Fail(error, head.position, "window ends before it starts");
  }
  return true;
}

// splitmix64, as netplay's fake network: the same on every compiler.
uint64_t SplitMix(uint64_t& state) {
  uint64_t z = (state += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}

size_t LaneIndex(netplay::Lane lane) { return size_t(lane) % netplay::kLaneCount; }

size_t CountBaselineRows(const std::vector<Message>& messages) {
  return size_t(std::count_if(messages.begin(), messages.end(), [](const Message& m) {
    return std::holds_alternative<msg::BaselineRows>(m);
  }));
}

// The record keys a message carries, for kind=, key= and slot=.
void KeysOf(const Message& m, std::vector<RecordKey>& out) {
  out.clear();
  if (auto* d = std::get_if<msg::Delta>(&m)) out.push_back(d->record.key);
  else if (auto* t = std::get_if<msg::Tomb>(&m)) out.push_back(t->key);
  else if (auto* o = std::get_if<msg::Obligation>(&m)) out.push_back(o->key);
  else if (auto* b = std::get_if<msg::BaselineRows>(&m)) {
    for (const Record& r : b->records) out.push_back(r.key);
  } else if (auto* e = std::get_if<msg::Effect>(&m)) {
    for (const Record& r : e->records) out.push_back(r.key);
  } else if (auto* q = std::get_if<msg::Request>(&m)) {
    if (q->request.basis.kind != 0) out.push_back(q->request.basis);
    if (q->request.subject.form != IdForm::kNone) {
      out.push_back(RecordKey{q->request.kind, q->request.subject, 0});
    }
  }
}

// The obligation id a message carries; 0: none.
uint64_t ObligationIdOf(const Message& m) {
  if (auto* o = std::get_if<msg::Obligation>(&m)) return o->id;
  if (auto* d = std::get_if<msg::ObligationDone>(&m)) return d->id;
  if (auto* a = std::get_if<msg::ObligationAck>(&m)) return a->id;
  return 0;
}

}  // namespace

std::string_view FaultLayerName(FaultLayer layer) {
  switch (layer) {
    case FaultLayer::kLink: return "link";
    case FaultLayer::kPeer: return "peer";
    case FaultLayer::kMessage: return "message";
    case FaultLayer::kApply: return "apply";
    case FaultLayer::kPump: return "pump";
  }
  return "?";
}

std::string_view FaultActionName(FaultAction action) {
  switch (action) {
    case FaultAction::kDrop: return "drop";
    case FaultAction::kDelay: return "delay";
    case FaultAction::kDuplicate: return "duplicate";
    case FaultAction::kReorder: return "reorder";
    case FaultAction::kThrottle: return "throttle";
    case FaultAction::kFull: return "full";
    case FaultAction::kDisconnect: return "disconnect";
    case FaultAction::kKill: return "kill";
    case FaultAction::kFail: return "fail";
    case FaultAction::kStuck: return "stuck";
    case FaultAction::kDespawn: return "despawn";
    case FaultAction::kLate: return "late";
    case FaultAction::kStall: return "stall";
  }
  return "?";
}

std::optional<std::vector<FaultRule>> ParseFaultRules(std::string_view text,
                                                      FaultParseError* error) {
  std::vector<FaultRule> rules;
  if (text.find(';') != std::string_view::npos) {
    Fail(error, text.find(';'), "unknown word ';'");
    return std::nullopt;
  }
  if (Words(text, 0).empty()) return rules;
  for (const Token& clause : Split(text, 0, '|')) {
    std::vector<Token> words = Words(clause.text, clause.position);
    if (words.empty()) {
      Fail(error, clause.position, "empty rule");
      return std::nullopt;
    }
    FaultRule rule;
    if (!ParseRule(words, rule, error)) return std::nullopt;
    rules.push_back(std::move(rule));
  }
  return rules;
}

std::string FormatFaultRule(const FaultRule& rule) {
  std::string s;
  const ActionSpec* spec = FindAction(rule.layer, rule.action);
  s += FaultLayerName(rule.layer);
  s += ':';
  s += FaultActionName(rule.action);
  auto word = [&](std::string_view name, const std::string& value) {
    s += ' ';
    s += name;
    s += '=';
    s += value;
  };
  if (rule.lane) word("lane", std::string(LaneName(*rule.lane)));
  if (rule.direction != FaultDirection::kAny) word("dir", std::string(DirectionName(rule.direction)));
  if (rule.peer) word("peer", std::to_string(*rule.peer));
  if (rule.type) word("type", std::string(MessageTypeName(*rule.type)));
  if (!rule.kind.empty()) word("kind", rule.kind);
  if (!rule.key.empty()) word("key", rule.key);
  if (rule.slot) word("slot", std::to_string(*rule.slot));
  if (rule.obligation) word("id", std::to_string(*rule.obligation));
  if (rule.nth) word("nth", std::to_string(rule.nth));
  if (rule.percent != 100) word("pct", std::to_string(rule.percent));
  if (rule.from_ms) word("from", std::to_string(*rule.from_ms));
  if (rule.to_ms) word("to", std::to_string(*rule.to_ms));
  if (rule.after_baseline_rows) {
    word("after", "baseline_rows:" + std::to_string(rule.after_baseline_rows));
  }
  if (spec) {
    switch (spec->amount) {
      case Amount::kNone: break;
      case Amount::kMs: word("ms", std::to_string(rule.amount)); break;
      case Amount::kBps: word("bps", std::to_string(rule.amount)); break;
      case Amount::kDelay:
        word("delay", rule.amount_max != rule.amount
                          ? std::to_string(rule.amount) + "-" + std::to_string(rule.amount_max)
                          : std::to_string(rule.amount));
        break;
    }
  }
  return s;
}

std::string FormatFaultRules(std::span<const FaultRule> rules) {
  std::string s;
  for (size_t i = 0; i < rules.size(); ++i) {
    if (i) s += " | ";
    s += FormatFaultRule(rules[i]);
  }
  return s;
}

std::string FaultLine(const FaultEvent& event) {
  std::string s = "Online: backend fault ";
  s += std::to_string(event.rule);
  s += ' ';
  s += FaultLayerName(event.layer);
  s += ':';
  s += FaultActionName(event.action);
  if (!event.what.empty()) {
    s += ' ';
    s += event.what;
  }
  return s;
}

// --- The injector's state, shared by its decorators.

class FaultLink;

struct FaultInjector::Impl {
  std::vector<FaultRule> rules;
  std::vector<uint64_t> matched, fired;
  uint64_t rng = 0;
  std::span<const KindSpec> table;
  TimePoint start{}, now{};
  std::deque<FaultEvent> events;
  std::vector<FaultLink*> links;
  std::map<uint16_t, TimePoint> stall_until;

  bool Chance(uint32_t percent) {
    if (percent >= 100) return true;
    if (percent == 0) return false;
    return SplitMix(rng) % 100 < percent;
  }
  uint32_t Draw(const FaultRule& r) {
    if (r.amount_max <= r.amount) return r.amount;
    return r.amount + uint32_t(SplitMix(rng) % (uint64_t(r.amount_max - r.amount) + 1));
  }
  bool InWindow(const FaultRule& r) const {
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - start).count();
    if (r.from_ms && ms < int64_t(*r.from_ms)) return false;
    if (r.to_ms && ms >= int64_t(*r.to_ms)) return false;
    return true;
  }
  // The rule's frame conditions: lane, direction, peer, window, after.
  bool Applies(const FaultRule& r, FaultDirection dir, uint16_t peer, netplay::Lane lane,
               uint64_t rows) const {
    if (r.lane && *r.lane != lane) return false;
    if (r.direction != FaultDirection::kAny && r.direction != dir) return false;
    if (r.peer && *r.peer != peer) return false;
    if (r.after_baseline_rows && rows < r.after_baseline_rows) return false;
    return InWindow(r);
  }
  std::string KindName(KindId kind) const {
    const KindSpec* spec = FindKind(table, kind);
    return spec ? std::string(spec->name) : "kind" + std::to_string(kind);
  }
  bool KeyMatches(const FaultRule& r, const RecordKey& key) const {
    if (!r.kind.empty() && KindName(key.kind) != r.kind) return false;
    if (!r.key.empty() && ToText(key.entity) != r.key) return false;
    if (r.slot && *r.slot != key.slot) return false;
    return true;
  }
  bool MessageMatches(const FaultRule& r, const Message& m) const {
    if (r.type && TypeOf(m) != *r.type) return false;
    if (r.obligation && ObligationIdOf(m) != *r.obligation) return false;
    if (r.kind.empty() && r.key.empty() && !r.slot) return true;
    std::vector<RecordKey> keys;
    KeysOf(m, keys);
    return std::any_of(keys.begin(), keys.end(),
                       [&](const RecordKey& k) { return KeyMatches(r, k); });
  }
  bool FrameMatches(const FaultRule& r, const std::vector<Message>& messages) const {
    if (!r.type && r.kind.empty() && r.key.empty() && !r.slot && !r.obligation) return true;
    return std::any_of(messages.begin(), messages.end(),
                       [&](const Message& m) { return MessageMatches(r, m); });
  }
  std::string KeyText(const RecordKey& key) const {
    return KindName(key.kind) + " " + ToText(key.entity) + " slot " + std::to_string(key.slot);
  }
  std::string Describe(const Message& m) const {
    std::string s(MessageTypeName(TypeOf(m)));
    std::vector<RecordKey> keys;
    KeysOf(m, keys);
    if (!keys.empty()) s += " " + KeyText(keys.front());
    return s;
  }
  std::string Describe(const std::vector<Message>& messages) const {
    if (messages.empty()) return "frame";
    std::string s;
    for (size_t i = 0; i < messages.size(); ++i) {
      if (i) s += ',';
      s += MessageTypeName(TypeOf(messages[i]));
    }
    return s;
  }
  // A match of rule i: counted; fires at the nth match only (if nth is set) and then by its chance.
  bool Hit(size_t i, uint16_t peer, std::string what) {
    const FaultRule& r = rules[i];
    ++matched[i];
    if (r.nth && matched[i] != r.nth) return false;
    if (!Chance(r.percent)) return false;
    ++fired[i];
    FaultEvent e;
    e.rule = i + 1;
    e.layer = r.layer;
    e.action = r.action;
    e.peer = peer;
    e.at = now;
    e.what = std::move(what);
    events.push_back(std::move(e));
    return true;
  }
};

// --- The link and message layers.

class FaultLink final : public PeerLink {
 public:
  FaultLink(FaultInjector::Impl& inj, PeerLink& inner, uint16_t peer, bool server_side)
      : inj_(inj), inner_(inner), peer_(peer), server_side_(server_side) {
    inj_.links.push_back(this);
    tokens_last_ = inj_.now;
  }
  ~FaultLink() override { std::erase(inj_.links, this); }

  LinkSend Send(netplay::Lane lane, std::span<const uint8_t> frame) override {
    Release();
    if (disconnected_) return LinkSend::kGone;
    if (frame.size() > kMaxFrameBytes) return LinkSend::kTooBig;
    const size_t li = LaneIndex(lane);
    const FaultDirection dir = SendDirection();
    std::vector<Message> messages;
    DecodeFrame(frame, messages);  // a frame it cannot read matches no type, kind or key
    const std::string where = Where(dir, lane) + " " + inj_.Describe(messages);

    // full: the link answers kFull for the rule's time, from its first match (once).
    if (inj_.now < full_until_[li]) return LinkSend::kFull;
    for (size_t i = 0; i < inj_.rules.size(); ++i) {
      const FaultRule& r = inj_.rules[i];
      if (r.layer != FaultLayer::kLink || r.action != FaultAction::kFull || inj_.fired[i]) continue;
      if (!inj_.Applies(r, dir, peer_, lane, sent_rows_) || !inj_.FrameMatches(r, messages)) {
        continue;
      }
      if (!inj_.Hit(i, peer_, where + " " + std::to_string(r.amount) + " ms")) continue;
      const TimePoint until = inj_.now + std::chrono::milliseconds(r.amount);
      if (r.lane) {
        full_until_[li] = until;
      } else {
        full_until_.fill(until);
      }
      return LinkSend::kFull;
    }
    if (out_[li].size() >= netplay::kLaneWindow) return LinkSend::kFull;

    // throttle: the slowest rule that applies sets the rate. Its bottleneck buffer holds one
    // second of the rate (at least one frame); beyond it the link answers kFull, so the sender's
    // FIFO holds and its changes coalesce (LM1). It fires once per link, when it first applies.
    uint32_t bps = 0;
    size_t throttle_rule = 0;
    for (size_t i = 0; i < inj_.rules.size(); ++i) {
      const FaultRule& r = inj_.rules[i];
      if (r.layer != FaultLayer::kLink || r.action != FaultAction::kThrottle) continue;
      if (!inj_.Applies(r, dir, peer_, lane, sent_rows_)) continue;
      if (bps == 0 || r.amount < bps) {
        bps = r.amount;
        throttle_rule = i;
      }
    }
    const bool throttled = bps > 0;
    if (throttled) {
      bps_ = bps;
      if (throttle_onsets_.insert(throttle_rule).second) {
        inj_.Hit(throttle_rule, peer_, Where(dir, lane) + " " + std::to_string(bps) + " bps");
      }
      const size_t buffer = std::max<size_t>(1, bps / kMaxFrameBytes);
      const size_t held = size_t(std::count_if(out_[li].begin(), out_[li].end(),
                                               [](const Out& f) { return f.throttled; }));
      if (held >= buffer) return LinkSend::kFull;
    }

    bool drop = false, hold_reorder = false;
    int copies = 1;
    Duration delay{};
    for (size_t i = 0; i < inj_.rules.size(); ++i) {
      const FaultRule& r = inj_.rules[i];
      if (r.layer != FaultLayer::kLink) continue;
      if (!inj_.Applies(r, dir, peer_, lane, sent_rows_)) continue;
      switch (r.action) {
        case FaultAction::kDrop:
          if (!drop && inj_.FrameMatches(r, messages) && inj_.Hit(i, peer_, where)) drop = true;
          break;
        case FaultAction::kDuplicate:
          if (copies == 1 && inj_.FrameMatches(r, messages) && inj_.Hit(i, peer_, where)) {
            copies = 2;
          }
          break;
        case FaultAction::kDelay:
          if (inj_.FrameMatches(r, messages)) {
            const uint32_t ms = inj_.Draw(r);
            if (inj_.Hit(i, peer_, where + " " + std::to_string(ms) + " ms")) {
              delay = std::max<Duration>(delay, std::chrono::milliseconds(ms));
            }
          }
          break;
        case FaultAction::kReorder:
          if (lane == netplay::Lane::kFast && !hold_reorder && !reorder_ &&
              inj_.FrameMatches(r, messages) && inj_.Hit(i, peer_, where)) {
            hold_reorder = true;
          }
          break;
        default:
          break;
      }
    }
    sent_rows_ += CountBaselineRows(messages);
    if (drop) return LinkSend::kSent;  // gone below the sender: it never learns
    if (hold_reorder) {
      // Held until the next fast frame has gone (a fast frame older than the newest is dropped
      // by the receiver: netplay's contract, which the reorder plays).
      reorder_.emplace(frame.begin(), frame.end());
      return LinkSend::kSent;
    }
    if (delay > Duration::zero() || throttled || !out_[li].empty()) {
      TimePoint due = inj_.now + delay;
      if (!out_[li].empty()) due = std::max(due, out_[li].back().due);
      for (int c = 0; c < copies; ++c) {
        out_[li].push_back(Out{std::vector<uint8_t>(frame.begin(), frame.end()), due, throttled});
      }
      Release();
      return LinkSend::kSent;
    }
    const LinkSend r = inner_.Send(lane, frame);
    if (r == LinkSend::kSent) {
      if (copies == 2) inner_.Send(lane, frame);
      if (lane == netplay::Lane::kFast && reorder_) {
        inner_.Send(lane, *reorder_);
        reorder_.reset();
      }
    }
    return r;
  }

  bool Receive(netplay::Lane& lane, std::vector<uint8_t>& frame) override {
    Release();
    if (disconnected_) return false;
    netplay::Lane l = netplay::Lane::kGameplay;
    std::vector<uint8_t> f;
    while (inner_.Receive(l, f)) Inbound(l, std::move(f));
    for (auto it = in_.begin(); it != in_.end(); ++it) {
      if (it->due > inj_.now) continue;
      lane = it->lane;
      frame = std::move(it->frame);
      in_.erase(it);
      return true;
    }
    return false;
  }

  size_t UnackedFrames(netplay::Lane lane) const override {
    size_t n = inner_.UnackedFrames(lane) + out_[LaneIndex(lane)].size();
    if (lane == netplay::Lane::kFast && reorder_) ++n;
    return n;
  }
  std::chrono::microseconds Rtt() const override { return inner_.Rtt(); }
  bool Connected() const override { return !disconnected_ && inner_.Connected(); }

  // Due frames out, and a disconnect whose condition holds. Called by Advance and by every Send
  // and Receive.
  void Release() {
    if (!disconnected_) CheckDisconnect();
    if (disconnected_) return;
    // The throttle's bucket: one batch of 100 ms at the rate, at least one frame.
    if (bps_ > 0 && inj_.now > tokens_last_) {
      const double cap = std::max(double(kMaxFrameBytes), double(bps_) / 10.0);
      tokens_ = std::min(
          cap, tokens_ + double(bps_) * std::chrono::duration<double>(inj_.now - tokens_last_).count());
    }
    tokens_last_ = inj_.now;
    for (size_t li = 0; li < netplay::kLaneCount; ++li) {
      auto& q = out_[li];
      while (!q.empty()) {
        Out& f = q.front();
        if (f.due > inj_.now) break;
        if (f.throttled) {
          const double cap = std::max(double(kMaxFrameBytes), double(bps_) / 10.0);
          if (tokens_ + 1e-9 < std::min(double(f.bytes.size()), cap)) break;
        }
        const LinkSend r = inner_.Send(netplay::Lane(li), f.bytes);
        if (r == LinkSend::kFull) break;
        if (f.throttled) tokens_ = std::max(0.0, tokens_ - double(f.bytes.size()));
        q.pop_front();
        if (r == LinkSend::kGone) break;
      }
    }
  }

 private:
  struct Out {
    std::vector<uint8_t> bytes;
    TimePoint due{};
    bool throttled = false;
  };
  struct In {
    netplay::Lane lane = netplay::Lane::kGameplay;
    std::vector<uint8_t> frame;
    TimePoint due{};
  };

  FaultDirection SendDirection() const {
    return server_side_ ? FaultDirection::kServerToClient : FaultDirection::kClientToServer;
  }
  FaultDirection ReceiveDirection() const {
    return server_side_ ? FaultDirection::kClientToServer : FaultDirection::kServerToClient;
  }
  std::string Where(FaultDirection dir, netplay::Lane lane) const {
    return "peer " + std::to_string(peer_) + " " + std::string(DirectionName(dir)) + " " +
           std::string(LaneName(lane));
  }

  void CheckDisconnect() {
    for (size_t i = 0; i < inj_.rules.size(); ++i) {
      const FaultRule& r = inj_.rules[i];
      if (r.action != FaultAction::kDisconnect || inj_.fired[i]) continue;
      if (r.peer && *r.peer != peer_) continue;
      if (r.after_baseline_rows && std::max(sent_rows_, received_rows_) < r.after_baseline_rows) {
        continue;
      }
      if (!inj_.InWindow(r)) continue;
      if (!inj_.Hit(i, peer_, "peer " + std::to_string(peer_))) continue;
      disconnected_ = true;
      for (auto& q : out_) q.clear();
      in_.clear();
      reorder_.reset();
      return;
    }
  }

  // The message layer: kill and duplicate per message, delay per frame (the lane as a whole late,
  // its order kept).
  void Inbound(netplay::Lane lane, std::vector<uint8_t> frame) {
    const size_t li = LaneIndex(lane);
    const FaultDirection dir = ReceiveDirection();
    std::vector<Message> messages;
    const bool readable = DecodeFrame(frame, messages);
    std::vector<std::vector<uint8_t>> frames;
    Duration delay{};
    if (!readable) {
      frames.push_back(std::move(frame));  // not ours to judge; the backend drops it
    } else {
      std::vector<Message> kept;
      bool changed = false;
      for (const Message& m : messages) {
        bool killed = false, duplicated = false;
        for (size_t i = 0; i < inj_.rules.size(); ++i) {
          const FaultRule& r = inj_.rules[i];
          if (r.layer != FaultLayer::kMessage) continue;
          if (r.action != FaultAction::kKill && r.action != FaultAction::kDuplicate) continue;
          if (!inj_.Applies(r, dir, peer_, lane, received_rows_) || !inj_.MessageMatches(r, m)) {
            continue;
          }
          const std::string what = Where(dir, lane) + " " + inj_.Describe(m);
          if (r.action == FaultAction::kKill && !killed) killed = inj_.Hit(i, peer_, what);
          if (r.action == FaultAction::kDuplicate && !duplicated) {
            duplicated = inj_.Hit(i, peer_, what);
          }
        }
        if (std::holds_alternative<msg::BaselineRows>(m)) ++received_rows_;
        if (!killed) kept.push_back(m);
        if (!killed && duplicated) kept.push_back(m);
        changed = changed || killed || duplicated;
      }
      for (size_t i = 0; i < inj_.rules.size(); ++i) {
        const FaultRule& r = inj_.rules[i];
        if (r.layer != FaultLayer::kMessage || r.action != FaultAction::kDelay) continue;
        if (!inj_.Applies(r, dir, peer_, lane, received_rows_) ||
            !inj_.FrameMatches(r, messages)) {
          continue;
        }
        const uint32_t ms = inj_.Draw(r);
        if (inj_.Hit(i, peer_, Where(dir, lane) + " " + inj_.Describe(messages) + " " +
                                   std::to_string(ms) + " ms")) {
          delay = std::max<Duration>(delay, std::chrono::milliseconds(ms));
        }
      }
      if (!changed) {
        frames.push_back(std::move(frame));
      } else {
        // The rest re-framed (a duplicate may need a second frame).
        std::vector<uint8_t> f;
        for (const Message& m : kept) {
          if (AppendMessage(m, f)) continue;
          if (!f.empty()) frames.push_back(std::move(f));
          f.clear();
          if (AppendMessage(m, f)) continue;
          f.clear();  // cannot happen: it came in one frame
        }
        if (!f.empty()) frames.push_back(std::move(f));
      }
    }
    TimePoint due = std::max(inj_.now + delay, last_in_due_[li]);
    last_in_due_[li] = due;
    for (auto& f : frames) in_.push_back(In{lane, std::move(f), due});
  }

  FaultInjector::Impl& inj_;
  PeerLink& inner_;
  uint16_t peer_;
  bool server_side_;
  std::array<std::deque<Out>, netplay::kLaneCount> out_;
  std::optional<std::vector<uint8_t>> reorder_;
  std::deque<In> in_;
  std::array<TimePoint, netplay::kLaneCount> full_until_{};
  std::array<TimePoint, netplay::kLaneCount> last_in_due_{};
  bool disconnected_ = false;
  std::set<size_t> throttle_onsets_;  // the throttle rules that fired on this link
  uint32_t bps_ = 0;
  double tokens_ = double(kMaxFrameBytes);
  TimePoint tokens_last_{};
  uint64_t sent_rows_ = 0, received_rows_ = 0;
};

// --- The apply layer.

class FaultAdapter final : public KindAdapter {
 public:
  FaultAdapter(FaultInjector::Impl& inj, KindAdapter& inner, uint16_t peer, KindId kind,
               const IdentityMap* identity, std::function<void(Handle)> despawn)
      : inj_(inj),
        inner_(inner),
        peer_(peer),
        kind_(kind),
        identity_(identity),
        despawn_(std::move(despawn)) {}

  AdapterResult Read(Handle h, uint16_t slot, std::vector<uint8_t>& value) override {
    return inner_.Read(h, slot, value);
  }
  AdapterResult Apply(Handle h, uint16_t slot, std::span<const uint8_t> value) override {
    RecordKey key{kind_, {}, slot};
    if (identity_) {
      if (auto id = identity_->IdOf(h)) key.entity = *id;
    }
    for (size_t i = 0; i < inj_.rules.size(); ++i) {
      const FaultRule& r = inj_.rules[i];
      if (r.layer != FaultLayer::kApply || r.action == FaultAction::kLate) continue;
      if (r.peer && *r.peer != peer_) continue;
      if (!inj_.InWindow(r) || !inj_.KeyMatches(r, key)) continue;
      if (!inj_.Hit(i, peer_, "peer " + std::to_string(peer_) + " " + inj_.KeyText(key))) {
        continue;
      }
      switch (r.action) {
        case FaultAction::kFail: return AdapterResult::kFailed;
        case FaultAction::kStuck: return AdapterResult::kOk;  // the native keeps its own value
        case FaultAction::kDespawn:
          if (despawn_) despawn_(h);
          return AdapterResult::kGone;
        default: break;
      }
    }
    return inner_.Apply(h, slot, value);
  }
  AdapterResult Retire(Handle h, uint16_t slot) override { return inner_.Retire(h, slot); }
  AdapterResult Fulfil(Handle h, const Obligation& o) override { return inner_.Fulfil(h, o); }

 private:
  FaultInjector::Impl& inj_;
  KindAdapter& inner_;
  uint16_t peer_;
  KindId kind_;
  const IdentityMap* identity_;
  std::function<void(Handle)> despawn_;
};

// --- FaultInjector ---

FaultInjector::FaultInjector(std::vector<FaultRule> rules, uint64_t seed,
                             std::span<const KindSpec> table, TimePoint start)
    : impl_(std::make_unique<Impl>()) {
  impl_->matched.assign(rules.size(), 0);
  impl_->fired.assign(rules.size(), 0);
  impl_->rules = std::move(rules);
  impl_->rng = seed;
  impl_->table = table;
  impl_->start = start;
  impl_->now = start;
}

FaultInjector::~FaultInjector() = default;

void FaultInjector::Advance(TimePoint now) {
  if (now > impl_->now) impl_->now = now;
  for (FaultLink* link : impl_->links) link->Release();
}

TimePoint FaultInjector::Now() const { return impl_->now; }

std::unique_ptr<PeerLink> FaultInjector::Wrap(PeerLink& inner, uint16_t peer, bool server_side) {
  return std::make_unique<FaultLink>(*impl_, inner, peer, server_side);
}

std::unique_ptr<KindAdapter> FaultInjector::WrapAdapter(KindAdapter& inner, uint16_t peer,
                                                        KindId kind, const IdentityMap* identity,
                                                        std::function<void(Handle)> despawn) {
  return std::make_unique<FaultAdapter>(*impl_, inner, peer, kind, identity, std::move(despawn));
}

std::optional<Duration> FaultInjector::SpawnDelay(uint16_t peer, const RecordKey& key) {
  Impl& s = *impl_;
  for (size_t i = 0; i < s.rules.size(); ++i) {
    const FaultRule& r = s.rules[i];
    if (r.layer != FaultLayer::kApply || r.action != FaultAction::kLate) continue;
    if (r.peer && *r.peer != peer) continue;
    if (!s.InWindow(r) || !s.KeyMatches(r, key)) continue;
    if (!s.Hit(i, peer, "peer " + std::to_string(peer) + " " + s.KeyText(key) + " " +
                            std::to_string(r.amount) + " ms")) {
      continue;
    }
    return std::chrono::duration_cast<Duration>(std::chrono::milliseconds(r.amount));
  }
  return std::nullopt;
}

bool FaultInjector::Stalled(uint16_t peer) {
  Impl& s = *impl_;
  auto it = s.stall_until.find(peer);
  if (it != s.stall_until.end() && s.now < it->second) return true;
  for (size_t i = 0; i < s.rules.size(); ++i) {
    const FaultRule& r = s.rules[i];
    if (r.layer != FaultLayer::kPump || r.action != FaultAction::kStall || s.fired[i]) continue;
    if (r.peer && *r.peer != peer) continue;
    if (!s.InWindow(r)) continue;
    if (!s.Hit(i, peer, "peer " + std::to_string(peer) + " " + std::to_string(r.amount) + " ms")) {
      continue;
    }
    s.stall_until[peer] = s.now + std::chrono::milliseconds(r.amount);
    return true;
  }
  return false;
}

bool FaultInjector::PollEvent(FaultEvent& out) {
  if (impl_->events.empty()) return false;
  out = std::move(impl_->events.front());
  impl_->events.pop_front();
  return true;
}

const std::vector<FaultRule>& FaultInjector::Rules() const { return impl_->rules; }

uint64_t FaultInjector::Matched(size_t rule) const {
  return rule < impl_->matched.size() ? impl_->matched[rule] : 0;
}

uint64_t FaultInjector::Fired(size_t rule) const {
  return rule < impl_->fired.size() ? impl_->fired[rule] : 0;
}

std::vector<size_t> FaultInjector::NotFired() const {
  std::vector<size_t> out;
  for (size_t i = 0; i < impl_->fired.size(); ++i) {
    if (impl_->fired[i] == 0) out.push_back(i);
  }
  return out;
}

}  // namespace skylanders::backend
