#include "online/protocol.h"

#include <algorithm>
#include <bit>
#include <cmath>

#include "netplay/bytes.h"

namespace skylanders::online {

using netplay::ByteReader;
using netplay::ByteWriter;

// The variant's order is the type numbering (Hello = 1 ...), which TypeOf relies on, for the
// contiguous run 1..kLastType; the number-block types after it (stage 1C's OwnerFlags = 50) are
// mapped explicitly (TypeOf, IsKnown).
static_assert(std::variant_size_v<Message> == 27);
static_assert(std::is_same_v<std::variant_alternative_t<0, Message>, Hello>);
static_assert(std::is_same_v<std::variant_alternative_t<10, Message>, PlayerList>);
static_assert(std::is_same_v<std::variant_alternative_t<11, Message>, PlayerPose>);
static_assert(std::is_same_v<std::variant_alternative_t<14, Message>, WorldDecision>);
static_assert(std::is_same_v<std::variant_alternative_t<19, Message>, SessionClosed>);
static_assert(std::is_same_v<std::variant_alternative_t<20, Message>, SceneBarrier>);
static_assert(std::is_same_v<std::variant_alternative_t<21, Message>, BackendFrame>);
static_assert(std::is_same_v<std::variant_alternative_t<23, Message>, PlayerPoseShown>);
static_assert(std::is_same_v<std::variant_alternative_t<24, Message>, OccurrenceAccept>);
static_assert(std::is_same_v<std::variant_alternative_t<25, Message>, AttackPress>);
constexpr uint16_t kLastType = uint16_t(MessageType::kAttackPress);
static_assert(std::is_same_v<std::variant_alternative_t<26, Message>, OwnerFlags>);
constexpr size_t kBlockTypes = 1;  // OwnerFlags
static_assert(kLastType + kBlockTypes == std::variant_size_v<Message>);

bool IsNone(const OwnerToken& token) {
  return std::all_of(token.begin(), token.end(), [](uint8_t b) { return b == 0; });
}

const char* RefuseReasonName(RefuseReason reason) {
  switch (reason) {
    case RefuseReason::kVersion: return "version";
    case RefuseReason::kDigest: return "digest";
    case RefuseReason::kFull: return "full";
    case RefuseReason::kNotOwner: return "not owner";
    case RefuseReason::kOther: break;
  }
  return "other";
}

const char* RefusalReasonName(RefusalReason reason) {
  switch (reason) {
    case RefusalReason::kNone: return "none";
    case RefusalReason::kTooFar: return "too_far";
    case RefusalReason::kRate: return "rate";
    case RefusalReason::kNotReleased: return "not released";
    case RefusalReason::kNotInPlay: return "not in play";
    case RefusalReason::kExitBeforeEnter: return "exit before enter";
    case RefusalReason::kStale: return "stale";
    case RefusalReason::kAlreadySent: return "already sent";
    case RefusalReason::kUnresolved: return "unresolved";
    case RefusalReason::kUnsupported: return "unsupported";
    case RefusalReason::kEnterRefused: return "enter refused";
    case RefusalReason::kSceneEnded: return "scene ended";
    case RefusalReason::kGenerationEnded: return "generation ended";
    case RefusalReason::kSessionEnded: return "session ended";
    case RefusalReason::kAlreadyFired: return "already fired";
    case RefusalReason::kNotActivated: return "not activated";
    case RefusalReason::kOther: break;
  }
  return "other";
}

bool ValidElements(uint16_t elements) {
  if (elements & ~kElementsUsed) return false;
  return elements == 0 || (elements & kElementsKnown) != 0;
}

const char* TriggerChecksName(TriggerChecks checks) {
  switch (checks) {
    case TriggerChecks::kPassed: return "passed";
    case TriggerChecks::kCooldown: return "cooldown";
    case TriggerChecks::kAccept: return "accept";
    case TriggerChecks::kAct: return "act";
    case TriggerChecks::kTouchFlag: return "touch flag";
    case TriggerChecks::kNotRun: return "not run";
  }
  return "other";
}

const char* SceneEndReasonName(SceneEndReason reason) {
  switch (reason) {
    case SceneEndReason::kNone: return "none";
    case SceneEndReason::kNatural: return "natural";
    case SceneEndReason::kStop: return "stop";
    case SceneEndReason::kLevelChange: return "level change";
    case SceneEndReason::kOther: break;
  }
  return "other";
}

uint32_t NameHash(std::string_view name) {
  uint32_t h = 2166136261u;
  for (const char c : name) {
    h ^= uint8_t(c);
    h *= 16777619u;
  }
  return h;
}

EntityRef SelfRef(uint32_t generation) {
  EntityRef r;
  r.kind = RefKind::kSelf;
  r.generation = generation;
  return r;
}

EntityRef WorldRef(uint32_t generation, uint32_t mid, uint32_t name_hash, uint32_t class_hash) {
  return EntityRef{RefKind::kWorld, generation, mid, name_hash, class_hash};
}

RequestKey TriggerRequestKey(uint16_t peer, uint32_t generation, const EntityRef& trigger,
                             TriggerEvent event, uint32_t enter_sequence) {
  RequestKey k;
  k.peer = peer;
  k.generation = generation;
  k.kind = RequestKind::kTrigger;
  k.trigger = trigger;
  k.event = event;
  k.enter_sequence = enter_sequence;
  return k;
}

RequestKey TelegramRequestKey(uint16_t peer, uint32_t generation, uint32_t request_sequence,
                              const TelegramKey& telegram) {
  RequestKey k;
  k.peer = peer;
  k.generation = generation;
  k.kind = RequestKind::kTelegram;
  k.request_sequence = request_sequence;
  k.telegram = telegram;
  return k;
}

RequestKey SkipRequestKey(uint16_t peer, uint32_t generation, const SceneKey& scene,
                          uint32_t skip_sequence) {
  RequestKey k;
  k.peer = peer;
  k.generation = generation;
  k.kind = RequestKind::kSceneSkip;
  k.scene = scene;
  k.skip_sequence = skip_sequence;
  return k;
}

MessageType TypeOf(const Message& message) {
  if (std::holds_alternative<OwnerFlags>(message)) return MessageType::kOwnerFlags;
  return MessageType(message.index() + 1);
}

bool IsValidUtf8(std::string_view s) {
  size_t i = 0;
  while (i < s.size()) {
    const uint8_t c = uint8_t(s[i]);
    if (c == 0) return false;
    if (c < 0x80) {
      ++i;
      continue;
    }
    size_t n;
    uint32_t cp;
    if ((c & 0xE0) == 0xC0) {
      n = 1;
      cp = c & 0x1F;
    } else if ((c & 0xF0) == 0xE0) {
      n = 2;
      cp = c & 0x0F;
    } else if ((c & 0xF8) == 0xF0) {
      n = 3;
      cp = c & 0x07;
    } else {
      return false;
    }
    if (s.size() - i <= n) return false;
    for (size_t k = 1; k <= n; ++k) {
      const uint8_t cc = uint8_t(s[i + k]);
      if ((cc & 0xC0) != 0x80) return false;
      cp = (cp << 6) | (cc & 0x3F);
    }
    const bool overlong = (n == 1 && cp < 0x80) || (n == 2 && cp < 0x800) || (n == 3 && cp < 0x10000);
    if (overlong || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return false;
    i += n + 1;
  }
  return true;
}

std::string TruncateUtf8(std::string_view text, size_t max_bytes) {
  if (text.size() <= max_bytes) return std::string(text);
  size_t cut = max_bytes;
  while (cut > 0 && (uint8_t(text[cut]) & 0xC0) == 0x80) --cut;
  return std::string(text.substr(0, cut));
}

namespace {

bool PutString(ByteWriter& w, std::string_view s, size_t max) {
  if (s.size() > max || !IsValidUtf8(s)) return false;
  w.U8(uint8_t(s.size()));
  w.Bytes(s);
  return true;
}

bool GetString(ByteReader& r, size_t max, std::string& out) {
  const size_t n = r.U8();
  if (!r.Ok() || n > max) return false;
  out = r.String(n);
  return r.Ok() && IsValidUtf8(out);
}

// Version 2 pieces. Each Put checks what its Get checks, so whatever encodes also decodes.

bool ValidRefKind(uint8_t kind) { return kind <= uint8_t(RefKind::kWorld); }
bool ValidEvent(uint8_t event) {
  return event == uint8_t(TriggerEvent::kEnter) || event == uint8_t(TriggerEvent::kExit) ||
         event == uint8_t(TriggerEvent::kTouch);
}

void PutFloat(ByteWriter& w, float f) { w.U32(std::bit_cast<uint32_t>(f)); }
bool PutFloats(ByteWriter& w, std::span<const float> fs) {
  for (const float f : fs) {
    if (!std::isfinite(f)) return false;
    PutFloat(w, f);
  }
  return true;
}
bool GetFloats(ByteReader& r, std::span<float> out) {
  for (float& f : out) {
    f = std::bit_cast<float>(r.U32());
    if (!std::isfinite(f)) return false;
  }
  return true;
}

// For kNone and kSelf only the generation means anything; the other fields must be 0, so two refs
// (and the keys that hold them) compare equal exactly when the design's tuples do.
bool RefFieldsUsed(const EntityRef& ref) {
  return ref.kind == RefKind::kWorld || (ref.mid == 0 && ref.name_hash == 0 && ref.class_hash == 0);
}

// kind u8, generation u32, mid u32, name hash u32, class hash u32: 17 bytes.
bool PutRef(ByteWriter& w, const EntityRef& ref) {
  if (!ValidRefKind(uint8_t(ref.kind)) || !RefFieldsUsed(ref)) return false;
  w.U8(uint8_t(ref.kind));
  w.U32(ref.generation);
  w.U32(ref.mid);
  w.U32(ref.name_hash);
  w.U32(ref.class_hash);
  return true;
}
bool GetRef(ByteReader& r, EntityRef& ref) {
  const uint8_t kind = r.U8();
  ref.generation = r.U32();
  ref.mid = r.U32();
  ref.name_hash = r.U32();
  ref.class_hash = r.U32();
  if (!ValidRefKind(kind)) return false;
  ref.kind = RefKind(kind);
  return RefFieldsUsed(ref);
}

// The messenger's ref, list owner hash u32, index u16: 23 bytes.
bool PutTelegram(ByteWriter& w, const TelegramKey& t) {
  if (!PutRef(w, t.messenger)) return false;
  w.U32(t.list_owner_hash);
  w.U16(t.index);
  return true;
}
bool GetTelegram(ByteReader& r, TelegramKey& t) {
  const bool ok = GetRef(r, t.messenger);
  t.list_owner_hash = r.U32();
  t.index = r.U16();
  return ok;
}

// A scene occurrence: sequence u32, the recipient's ref, name hash u32: 25 bytes.
bool PutScene(ByteWriter& w, const SceneKey& s) {
  w.U32(s.scene_sequence);
  if (!PutRef(w, s.recipient)) return false;
  w.U32(s.name_hash);
  return true;
}
bool GetScene(ByteReader& r, SceneKey& s) {
  s.scene_sequence = r.U32();
  const bool ok = GetRef(r, s.recipient);
  s.name_hash = r.U32();
  return ok;
}

// peer u16, generation u32, kind u8, then the kind's fields (at most 29 more bytes: a skip's).
bool PutKey(ByteWriter& w, const RequestKey& k) {
  w.U16(k.peer);
  w.U32(k.generation);
  w.U8(uint8_t(k.kind));
  switch (k.kind) {
    case RequestKind::kTrigger:
      if (!ValidEvent(uint8_t(k.event)) || !PutRef(w, k.trigger)) return false;
      w.U8(uint8_t(k.event));
      w.U32(k.enter_sequence);
      return true;
    case RequestKind::kTelegram:
      w.U32(k.request_sequence);
      return PutTelegram(w, k.telegram);
    case RequestKind::kSceneSkip:
      if (!PutScene(w, k.scene)) return false;
      w.U32(k.skip_sequence);
      return true;
  }
  return false;
}
bool GetKey(ByteReader& r, RequestKey& k) {
  k = RequestKey{};
  k.peer = r.U16();
  k.generation = r.U32();
  const uint8_t kind = r.U8();
  switch (kind) {
    case uint8_t(RequestKind::kTrigger): {
      k.kind = RequestKind::kTrigger;
      const bool ref_ok = GetRef(r, k.trigger);
      const uint8_t event = r.U8();
      k.enter_sequence = r.U32();
      if (!ref_ok || !ValidEvent(event)) return false;
      k.event = TriggerEvent(event);
      return true;
    }
    case uint8_t(RequestKind::kTelegram):
      k.kind = RequestKind::kTelegram;
      k.request_sequence = r.U32();
      return GetTelegram(r, k.telegram);
    case uint8_t(RequestKind::kSceneSkip): {
      k.kind = RequestKind::kSceneSkip;
      const bool scene_ok = GetScene(r, k.scene);
      k.skip_sequence = r.U32();
      return scene_ok;
    }
    default:
      return false;
  }
}

// A decision's outcome, reason and flags agree (the design's "applied: final; pending: not
// final; only a refusal has a reason; S's own final is final").
constexpr uint8_t kDecisionCached = 0x01;
constexpr uint8_t kDecisionFinal = 0x02;
// AppliedStep's trailing flags byte (amendment w).
constexpr uint8_t kAppliedLateStep = 0x01;
constexpr uint8_t kDecisionServerFinal = 0x04;
bool ConsistentDecision(const WorldDecision& d) {
  switch (d.outcome) {
    case DecisionOutcome::kApplied:
      return d.final && !d.server_final && d.reason == RefusalReason::kNone;
    case DecisionOutcome::kPending:
      return !d.final && !d.server_final && d.reason == RefusalReason::kNone;
    case DecisionOutcome::kRefused:
      return d.reason != RefusalReason::kNone && (d.final || !d.server_final);
  }
  return false;
}

bool ValidStepKind(uint8_t kind) {
  return kind == uint8_t(StepKind::kTriggerEnter) || kind == uint8_t(StepKind::kTelegram);
}
bool ValidReaction(uint8_t outcome) {
  return outcome >= uint8_t(ReactionOutcome::kLanded) && outcome <= uint8_t(ReactionOutcome::kMissed);
}
bool ValidScenePhase(uint8_t phase) {
  return phase >= uint8_t(ScenePhase::kOpen) && phase <= uint8_t(ScenePhase::kEnd);
}
bool ValidSceneKind(uint8_t kind) {
  return kind == uint8_t(SceneKind::kCutscene) || kind == uint8_t(SceneKind::kConversation);
}
// Only kEnd carries how it ended; every other phase has kNone and no skips, so two messages compare
// equal exactly when they say the same.
bool ConsistentScene(const SceneBarrier& m) {
  if (m.phase == ScenePhase::kEnd) return m.end != SceneEndReason::kNone;
  return m.end == SceneEndReason::kNone && m.skips == 0;
}

// A pose's fields, through the trailing ones (elements: item 11; teleports: stage 4); false if
// short or invalid. Used for PlayerPose and for PlayerPoseShown, whose pose ends the message.
bool ReadPose(ByteReader& r, PlayerPose& m) {
  m.pose_sequence = r.U32();
  m.generation = r.U32();
  bool ok = GetFloats(r, m.position) && GetFloats(r, m.rotation);
  m.figure.figure_id = r.U16();
  m.figure.figure_variant = r.U16();
  m.figure.top_id = r.U16();
  m.figure.top_variant = r.U16();
  m.figure.bottom_id = r.U16();
  m.figure.bottom_variant = r.U16();
  if (ok && r.Ok() && r.Remaining() > 0) {
    // Item 11's elements (absent in an earlier version 2 pose: 0, not read).
    m.figure.elements = r.U16();
    if (r.Ok() && !ValidElements(m.figure.elements)) return false;
  }
  if (ok && r.Ok() && r.Remaining() > 0) m.teleports = r.U16();  // stage 4 (absent before: 0)
  return ok && r.Ok();
}

bool Fields(ByteWriter& w, const Hello& m) {
  w.U16(m.protocol_version);
  w.Bytes(m.build_digest);
  w.U16(m.figure_id);
  w.U16(m.figure_variant);
  if (!PutString(w, m.name, kMaxNameBytes)) return false;
  w.Bytes(m.owner_token);
  if ((m.flags & ~kHelloFlagsKnown) != 0) return false;
  w.U8(m.flags);  // stage 4: a trailing byte, so a Hello without it reads as 0
  return true;
}
bool Fields(ByteWriter& w, const Welcome& m) {
  w.U16(m.peer_id);
  w.U32(m.session_id);
  w.U16(m.tick_hz);
  w.U64(m.server_tick);
  w.U32(m.level_generation);
  if (!PutString(w, m.level_zone, kMaxZoneBytes)) return false;
  if ((m.backend_mode & ~kBackendModeKnown) != 0) return false;
  w.U8(m.backend_mode);
  w.U64(m.backend_table_digest);
  return true;
}
bool Fields(ByteWriter& w, const Refuse& m) {
  w.U8(uint8_t(m.reason));
  return PutString(w, m.text, kMaxRefuseTextBytes);
}
bool Fields(ByteWriter&, const Bye&) { return true; }
bool Fields(ByteWriter& w, const Ping& m) {
  w.U64(m.client_time_us);
  return true;
}
bool Fields(ByteWriter& w, const Pong& m) {
  w.U64(m.client_time_us);
  w.U64(m.server_tick);
  return true;
}
bool Fields(ByteWriter& w, const LevelRequest& m) { return PutString(w, m.zone, kMaxZoneBytes); }
bool Fields(ByteWriter& w, const LevelLoad& m) {
  w.U32(m.generation);
  return PutString(w, m.zone, kMaxZoneBytes);
}
bool Fields(ByteWriter& w, const LevelLoaded& m) {
  w.U32(m.generation);
  return true;
}
bool Fields(ByteWriter& w, const BarrierGo& m) {
  w.U32(m.generation);
  w.U64(m.start_tick);
  return true;
}
bool Fields(ByteWriter& w, const PlayerList& m) {
  if (m.players.size() > kMaxPlayerEntries) return false;
  w.U8(uint8_t(m.players.size()));
  for (const PlayerEntry& e : m.players) {
    w.U16(e.peer_id);
    if (!PutString(w, e.name, kMaxNameBytes)) return false;
  }
  return true;
}

bool Fields(ByteWriter& w, const PlayerPose& m) {
  if (!ValidElements(m.figure.elements)) return false;
  w.U32(m.pose_sequence);
  w.U32(m.generation);
  if (!PutFloats(w, m.position) || !PutFloats(w, m.rotation)) return false;
  w.U16(m.figure.figure_id);
  w.U16(m.figure.figure_variant);
  w.U16(m.figure.top_id);
  w.U16(m.figure.top_variant);
  w.U16(m.figure.bottom_id);
  w.U16(m.figure.bottom_variant);
  w.U16(m.figure.elements);  // item 11: a trailing field, so a pose without it reads as 0 (not read)
  w.U16(m.teleports);        // stage 4: trailing too, so a pose without it reads as 0
  return true;
}
bool Fields(ByteWriter& w, const PlayerActionShown& m) {
  w.U16(m.peer);
  w.U32(m.peer_epoch);
  w.U32(m.generation);
  w.U32(m.press_sequence);
  w.U32(m.attack_id);
  w.U32(m.pose_sequence);
  return true;
}
bool Fields(ByteWriter& w, const PlayerPoseShown& m) {
  w.U16(m.peer);
  return Fields(w, m.pose);
}
bool Fields(ByteWriter& w, const OccurrenceAccept& m) {
  if (m.keys.empty() || m.keys.size() > kMaxOccurrenceKeys) return false;
  w.U32(m.generation);
  w.U8(uint8_t(m.keys.size()));
  for (const OccurrenceKeyBytes& k : m.keys) w.Bytes(k);
  return true;
}
bool Fields(ByteWriter& w, const AttackPress& m) {
  if (m.slot > kMaxAttackSlot) return false;
  w.U32(m.press_sequence);
  w.U32(m.generation);
  w.U8(m.slot);
  w.U8(m.release ? 1 : 0);
  w.U32(m.pose_sequence);
  w.U64(m.press_tick);
  return true;
}
bool Fields(ByteWriter& w, const PlayerAction& m) {
  w.U32(m.press_sequence);
  w.U32(m.generation);
  w.U32(m.attack_id);
  w.U32(m.pose_sequence);
  return true;
}
bool Fields(ByteWriter& w, const WorldRequest& m) {
  if (uint8_t(m.checks) > uint8_t(TriggerChecks::kNotRun)) return false;
  if (!PutKey(w, m.key) || !PutRef(w, m.subject)) return false;
  w.U32(m.event_bits);
  w.U32(m.pose_sequence);
  w.U8(uint8_t(m.checks));  // item 9: a trailing byte, so a request without it reads as kPassed
  return true;
}
bool Fields(ByteWriter& w, const WorldDecision& m) {
  if (!ConsistentDecision(m) || !PutKey(w, m.key)) return false;
  w.U8(uint8_t(m.outcome));
  w.U8(uint8_t(m.reason));
  w.U8(uint8_t((m.cached ? kDecisionCached : 0) | (m.final ? kDecisionFinal : 0) |
               (m.server_final ? kDecisionServerFinal : 0)));
  w.U32(m.applied_sequence);
  return true;
}
bool Fields(ByteWriter& w, const AppliedStep& m) {
  if (m.steps.size() > kMaxAppliedSteps || !PutKey(w, m.key)) return false;
  w.U32(m.applied_sequence);
  w.U8(uint8_t(m.steps.size()));
  for (const AppliedStepEntry& e : m.steps) {
    if (!ValidStepKind(uint8_t(e.kind)) || (e.targets & ~kTargetMask) != 0) return false;
    w.U8(uint8_t(e.kind));
    w.U8(e.targets);
    if (e.kind == StepKind::kTriggerEnter) {
      if (!PutRef(w, e.trigger)) return false;
      w.U32(e.event_bits);
    } else if (!PutTelegram(w, e.telegram)) {
      return false;
    }
  }
  w.U8(m.late_step ? kAppliedLateStep : 0);  // amendment w: a trailing byte, so a step without it reads as false
  return true;
}
bool Fields(ByteWriter& w, const LevelEnd& m) {
  w.U32(m.generation);
  w.U32(m.class_hash);
  return PutRef(w, m.recipient);
}
bool Fields(ByteWriter& w, const Reaction& m) {
  if (!ValidReaction(uint8_t(m.outcome))) return false;
  w.U32(m.press_sequence);
  w.U32(m.generation);
  if (!PutRef(w, m.victim)) return false;
  w.U8(uint8_t(m.outcome));
  return true;
}
bool Fields(ByteWriter&, const SessionClosing&) { return true; }
bool Fields(ByteWriter&, const SessionClosed&) { return true; }
bool Fields(ByteWriter& w, const BackendFrame& m) {
  if (m.bytes.empty() || m.bytes.size() > kMaxBackendFrameBytes) return false;
  w.Bytes(m.bytes);
  return true;
}
// generation u32, revision u32, flags u8, died, damage taken, defeated (i32 each): 21 bytes.
constexpr uint8_t kOwnerFlagDamage = 0x01;
constexpr uint8_t kOwnerFlagSwapped = 0x02;
bool Fields(ByteWriter& w, const OwnerFlags& m) {
  if (m.generation == 0 || m.revision == 0) return false;
  w.U32(m.generation);
  w.U32(m.revision);
  w.U8(uint8_t((m.has_taken_damage ? kOwnerFlagDamage : 0) | (m.has_swapped ? kOwnerFlagSwapped : 0)));
  w.U32(uint32_t(m.player_died));
  w.U32(uint32_t(m.damage_taken));
  w.U32(uint32_t(m.defeated));
  return true;
}
// phase u8, generation u32, the scene (25 bytes), kind u8, end u8, skips u8: 33 bytes.
bool Fields(ByteWriter& w, const SceneBarrier& m) {
  if (!ValidScenePhase(uint8_t(m.phase)) || !ValidSceneKind(uint8_t(m.kind)) ||
      uint8_t(m.end) > uint8_t(SceneEndReason::kOther) || !ConsistentScene(m)) {
    return false;
  }
  w.U8(uint8_t(m.phase));
  w.U32(m.generation);
  if (!PutScene(w, m.scene)) return false;
  w.U8(uint8_t(m.kind));
  w.U8(uint8_t(m.end));
  w.U8(m.skips);
  return true;
}

template <size_t N>
void GetArray(ByteReader& r, std::array<uint8_t, N>& out) {
  auto b = r.Bytes(N);
  if (r.Ok()) std::copy(b.begin(), b.end(), out.begin());
}

std::optional<Message> DecodeKnown(MessageType type, std::span<const uint8_t> body) {
  ByteReader r(body);
  bool ok = true;
  Message result;
  switch (type) {
    case MessageType::kHello: {
      Hello m;
      m.protocol_version = r.U16();
      GetArray(r, m.build_digest);
      m.figure_id = r.U16();
      m.figure_variant = r.U16();
      ok = GetString(r, kMaxNameBytes, m.name);
      GetArray(r, m.owner_token);
      if (ok && r.Ok() && r.Remaining() > 0) {  // stage 4's flags (absent before: 0)
        m.flags = r.U8();
        if (r.Ok() && (m.flags & ~kHelloFlagsKnown) != 0) return std::nullopt;
      }
      result = std::move(m);
      break;
    }
    case MessageType::kWelcome: {
      Welcome m;
      m.peer_id = r.U16();
      m.session_id = r.U32();
      m.tick_hz = r.U16();
      m.server_tick = r.U64();
      m.level_generation = r.U32();
      ok = GetString(r, kMaxZoneBytes, m.level_zone);
      // Version 3's two trailing fields. Only a version 3 client is ever welcomed (a version 2
      // Hello is refused kVersion), so a Welcome that ends before them is cut short.
      m.backend_mode = r.U8();
      m.backend_table_digest = r.U64();
      ok = ok && (m.backend_mode & ~kBackendModeKnown) == 0;
      result = std::move(m);
      break;
    }
    case MessageType::kRefuse: {
      Refuse m;
      const uint8_t reason = r.U8();
      m.reason = reason >= 1 && reason <= 5 ? RefuseReason(reason) : RefuseReason::kOther;
      ok = GetString(r, kMaxRefuseTextBytes, m.text);
      result = std::move(m);
      break;
    }
    case MessageType::kBye:
      result = Bye{};
      break;
    case MessageType::kPing:
      result = Ping{r.U64()};
      break;
    case MessageType::kPong: {
      Pong m;
      m.client_time_us = r.U64();
      m.server_tick = r.U64();
      result = m;
      break;
    }
    case MessageType::kLevelRequest: {
      LevelRequest m;
      ok = GetString(r, kMaxZoneBytes, m.zone);
      result = std::move(m);
      break;
    }
    case MessageType::kLevelLoad: {
      LevelLoad m;
      m.generation = r.U32();
      ok = GetString(r, kMaxZoneBytes, m.zone);
      result = std::move(m);
      break;
    }
    case MessageType::kLevelLoaded:
      result = LevelLoaded{r.U32()};
      break;
    case MessageType::kBarrierGo: {
      BarrierGo m;
      m.generation = r.U32();
      m.start_tick = r.U64();
      result = m;
      break;
    }
    case MessageType::kPlayerList: {
      PlayerList m;
      const size_t count = r.U8();
      if (count > kMaxPlayerEntries) return std::nullopt;
      for (size_t i = 0; ok && i < count; ++i) {
        PlayerEntry e;
        e.peer_id = r.U16();
        ok = GetString(r, kMaxNameBytes, e.name);
        m.players.push_back(std::move(e));
      }
      result = std::move(m);
      break;
    }
    case MessageType::kPlayerPose: {
      PlayerPose m;
      ok = ReadPose(r, m);
      result = m;
      break;
    }
    case MessageType::kPlayerAction: {
      PlayerAction m;
      m.press_sequence = r.U32();
      m.generation = r.U32();
      m.attack_id = r.U32();
      m.pose_sequence = r.U32();
      result = m;
      break;
    }
    case MessageType::kWorldRequest: {
      WorldRequest m;
      ok = GetKey(r, m.key) && GetRef(r, m.subject);
      m.event_bits = r.U32();
      m.pose_sequence = r.U32();
      if (ok && r.Ok() && r.Remaining() > 0) {
        // Item 9's client verdict (absent in an earlier version 2 request: kPassed).
        const uint8_t checks = r.U8();
        if (checks > uint8_t(TriggerChecks::kNotRun)) return std::nullopt;
        m.checks = TriggerChecks(checks);
      }
      result = m;
      break;
    }
    case MessageType::kWorldDecision: {
      WorldDecision m;
      ok = GetKey(r, m.key);
      const uint8_t outcome = r.U8();
      const uint8_t reason = r.U8();
      const uint8_t flags = r.U8();
      m.applied_sequence = r.U32();
      if (outcome < uint8_t(DecisionOutcome::kApplied) || outcome > uint8_t(DecisionOutcome::kRefused)) {
        return std::nullopt;
      }
      m.outcome = DecisionOutcome(outcome);
      // An unknown reason reads as "other" (terminal), as RefuseReason does; other flag bits are
      // a later version's.
      m.reason = reason <= uint8_t(RefusalReason::kNotActivated) ? RefusalReason(reason) : RefusalReason::kOther;
      m.cached = flags & kDecisionCached;
      m.final = flags & kDecisionFinal;
      m.server_final = flags & kDecisionServerFinal;
      ok = ok && ConsistentDecision(m);
      result = m;
      break;
    }
    case MessageType::kAppliedStep: {
      AppliedStep m;
      ok = GetKey(r, m.key);
      m.applied_sequence = r.U32();
      const size_t count = r.U8();
      if (count > kMaxAppliedSteps) return std::nullopt;
      for (size_t i = 0; ok && i < count; ++i) {
        AppliedStepEntry e;
        const uint8_t kind = r.U8();
        e.targets = r.U8();
        if (!ValidStepKind(kind) || (e.targets & ~kTargetMask) != 0) return std::nullopt;
        e.kind = StepKind(kind);
        if (e.kind == StepKind::kTriggerEnter) {
          ok = GetRef(r, e.trigger);
          e.event_bits = r.U32();
        } else {
          ok = GetTelegram(r, e.telegram);
        }
        m.steps.push_back(e);
      }
      if (ok && r.Ok() && r.Remaining() > 0) {
        // Amendment w's flags (absent in an earlier version 2 step: not a late step); the other bits
        // are a later version's.
        m.late_step = (r.U8() & kAppliedLateStep) != 0;
      }
      result = std::move(m);
      break;
    }
    case MessageType::kLevelEnd: {
      LevelEnd m;
      m.generation = r.U32();
      m.class_hash = r.U32();
      ok = GetRef(r, m.recipient);
      result = m;
      break;
    }
    case MessageType::kReaction: {
      Reaction m;
      m.press_sequence = r.U32();
      m.generation = r.U32();
      ok = GetRef(r, m.victim);
      const uint8_t outcome = r.U8();
      ok = ok && ValidReaction(outcome);
      m.outcome = ReactionOutcome(outcome);
      result = m;
      break;
    }
    case MessageType::kSessionClosing:
      result = SessionClosing{};
      break;
    case MessageType::kSessionClosed:
      result = SessionClosed{};
      break;
    case MessageType::kSceneBarrier: {
      SceneBarrier m;
      const uint8_t phase = r.U8();
      m.generation = r.U32();
      ok = GetScene(r, m.scene);
      const uint8_t kind = r.U8();
      const uint8_t end = r.U8();
      m.skips = r.U8();
      if (!ValidScenePhase(phase) || !ValidSceneKind(kind)) return std::nullopt;
      m.phase = ScenePhase(phase);
      m.kind = SceneKind(kind);
      // An unknown end reason reads as "other", as an unknown refusal reason does.
      m.end = end <= uint8_t(SceneEndReason::kOther) ? SceneEndReason(end) : SceneEndReason::kOther;
      ok = ok && ConsistentScene(m);
      result = m;
      break;
    }
    case MessageType::kPlayerActionShown: {
      PlayerActionShown m;
      m.peer = r.U16();
      m.peer_epoch = r.U32();
      m.generation = r.U32();
      m.press_sequence = r.U32();
      m.attack_id = r.U32();
      m.pose_sequence = r.U32();
      result = m;
      break;
    }
    case MessageType::kPlayerPoseShown: {
      PlayerPoseShown m;
      m.peer = r.U16();
      ok = r.Ok() && ReadPose(r, m.pose);
      result = m;
      break;
    }
    case MessageType::kOccurrenceAccept: {
      OccurrenceAccept m;
      m.generation = r.U32();
      const uint8_t n = r.U8();
      if (!r.Ok() || n == 0 || n > kMaxOccurrenceKeys) return std::nullopt;
      m.keys.resize(n);
      for (OccurrenceKeyBytes& k : m.keys) GetArray(r, k);
      result = std::move(m);
      break;
    }
    case MessageType::kAttackPress: {
      AttackPress m;
      m.press_sequence = r.U32();
      m.generation = r.U32();
      m.slot = r.U8();
      const uint8_t release = r.U8();
      m.pose_sequence = r.U32();
      m.press_tick = r.U64();
      if (r.Ok() && (m.slot > kMaxAttackSlot || release > 1)) return std::nullopt;
      m.release = release != 0;
      result = m;
      break;
    }
    case MessageType::kBackendFrame: {
      // The rest of the message is the frame (the session never reads it).
      if (body.empty() || body.size() > kMaxBackendFrameBytes) return std::nullopt;
      BackendFrame m;
      m.bytes.assign(body.begin(), body.end());
      result = std::move(m);
      break;
    }
    case MessageType::kOwnerFlags: {
      OwnerFlags m;
      m.generation = r.U32();
      m.revision = r.U32();
      const uint8_t flags = r.U8();
      m.has_taken_damage = (flags & kOwnerFlagDamage) != 0;
      m.has_swapped = (flags & kOwnerFlagSwapped) != 0;
      m.player_died = int32_t(r.U32());
      m.damage_taken = int32_t(r.U32());
      m.defeated = int32_t(r.U32());
      ok = m.generation != 0 && m.revision != 0;
      result = m;
      break;
    }
    default:
      return std::nullopt;
  }
  if (!ok || !r.Ok()) return std::nullopt;
  return result;
}

bool IsKnown(uint16_t type) {
  return (type >= 1 && type <= kLastType) || type == uint16_t(MessageType::kOwnerFlags);
}

}  // namespace

bool EncodeMessage(const Message& message, std::vector<uint8_t>& out) {
  std::vector<uint8_t> body;
  ByteWriter w(body);
  const bool ok = std::visit([&](const auto& m) { return Fields(w, m); }, message);
  if (!ok || body.size() > 0xFFFF) return false;
  ByteWriter o(out);
  o.U16(uint16_t(TypeOf(message)));
  o.U16(uint16_t(body.size()));
  o.Bytes(body);
  return true;
}

std::vector<uint8_t> EncodeMessage(const Message& message) {
  std::vector<uint8_t> out;
  if (!EncodeMessage(message, out)) out.clear();
  return out;
}

std::optional<std::vector<Message>> DecodeMessages(std::span<const uint8_t> bytes,
                                                   size_t* skipped) {
  std::vector<Message> messages;
  size_t unknown = 0;
  ByteReader r(bytes);
  while (r.Remaining() > 0) {
    const uint16_t type = r.U16();
    const uint16_t length = r.U16();
    auto body = r.Bytes(length);
    if (!r.Ok()) return std::nullopt;
    if (!IsKnown(type)) {
      ++unknown;
      continue;
    }
    auto m = DecodeKnown(MessageType(type), body);
    if (!m) return std::nullopt;
    messages.push_back(std::move(*m));
  }
  if (skipped) *skipped = unknown;
  return messages;
}

}  // namespace skylanders::online
