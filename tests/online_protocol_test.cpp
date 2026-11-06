#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <span>
#include <string>
#include <vector>

#include "netplay/datagram.h"
#include "online/protocol.h"
#include "test_util.h"

// REQUIRE_OR_RETURN for a function that returns nothing: a failure is counted, then it returns.
#define REQUIRE_OR_RETURN_VOID(cond)                                       \
  do {                                                                     \
    if (!(cond)) {                                                         \
      std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                        \
      return;                                                              \
    }                                                                      \
  } while (0)

using namespace skylanders::online;

namespace {

bool RoundTrip(const Message& m) {
  const std::vector<uint8_t> bytes = EncodeMessage(m);
  if (bytes.size() < kMessageHeaderSize) return false;
  auto back = DecodeMessages(bytes);
  return back && back->size() == 1 && (*back)[0] == m;
}

Hello SampleHello() {
  Hello h;
  for (size_t i = 0; i < h.build_digest.size(); ++i) h.build_digest[i] = uint8_t(i * 7 + 1);
  h.figure_id = 0x1234;
  h.figure_variant = 0x3805;
  h.name = "Spyro \xE2\x98\x85";  // with a UTF-8 star
  for (size_t i = 0; i < h.owner_token.size(); ++i) h.owner_token[i] = uint8_t(0xF0 + i);
  return h;
}

EntityRef Trigger() { return WorldRef(3, 2394, NameHash("JumpHint_Trigger"), NameHash("CTriggerVolume")); }

TelegramKey Telegram() {
  TelegramKey t;
  t.messenger = WorldRef(3, 3846, NameHash("Birdee_A_Flyoff"), NameHash("CEntity"));
  t.list_owner_hash = NameHash("CMessengerComponent+24");
  t.index = 2;
  return t;
}

SceneKey Scene() {
  return SceneKey{1, WorldRef(3, 77, NameHash("Cutscene_01"), NameHash("CEntity")), NameHash("Tutorial_B_Intro")};
}

std::vector<WorldRequest> SampleRequests() {
  std::vector<WorldRequest> out;
  for (TriggerEvent ev : {TriggerEvent::kEnter, TriggerEvent::kExit, TriggerEvent::kTouch}) {
    WorldRequest r;
    r.key = TriggerRequestKey(2, 3, Trigger(), ev, 17);
    r.subject = SelfRef(3);
    r.event_bits = uint32_t(ev) | 0x20;
    r.pose_sequence = 41;
    out.push_back(r);
  }
  WorldRequest t;
  t.key = TelegramRequestKey(4, 3, 9, Telegram());
  t.subject = SelfRef(3);
  t.pose_sequence = 0xFFFFFFFF;
  out.push_back(t);
  WorldRequest k;
  k.key = SkipRequestKey(1, 3, Scene(), 2);
  out.push_back(k);
  return out;
}

void Version2RoundTrips() {
  PlayerPose pose;
  pose.pose_sequence = 1234;
  pose.generation = 3;
  pose.position = {1.5f, -20.25f, 1e6f};
  pose.rotation = {0.0f, 0.70710677f, 0.0f, 0.70710677f};
  pose.figure = CurrentFigure{0x0BB8, 0x1801, 0x0BB9, 0x1802, 0x0BBA, 0x1803};
  CHECK(RoundTrip(pose));
  CHECK(RoundTrip(PlayerPose{}));
  CHECK(RoundTrip(PlayerAction{7, 3, 0xA77AC, 41}));
  for (const WorldRequest& r : SampleRequests()) CHECK(RoundTrip(r));

  // Every legal outcome and flag combination of a decision.
  WorldDecision applied;
  applied.key = SampleRequests()[0].key;
  applied.outcome = DecisionOutcome::kApplied;
  applied.final = true;
  applied.applied_sequence = 99;
  CHECK(RoundTrip(applied));
  applied.cached = true;
  CHECK(RoundTrip(applied));
  WorldDecision pending;
  pending.key = SampleRequests()[3].key;
  CHECK(RoundTrip(pending));
  WorldDecision retry;
  retry.key = SampleRequests()[0].key;
  retry.outcome = DecisionOutcome::kRefused;
  retry.reason = RefusalReason::kTooFar;
  CHECK(RoundTrip(retry));
  WorldDecision server_final = retry;
  server_final.final = true;
  server_final.server_final = true;
  CHECK(RoundTrip(server_final));
  for (uint8_t r = 1; r <= uint8_t(RefusalReason::kOther); ++r) {
    WorldDecision d;
    d.key = SampleRequests()[4].key;
    d.outcome = DecisionOutcome::kRefused;
    d.reason = RefusalReason(r);
    d.final = true;
    CHECK(RoundTrip(d));
  }

  AppliedStep step;
  step.key = SampleRequests()[0].key;
  step.applied_sequence = 5;
  AppliedStepEntry enter;
  enter.kind = StepKind::kTriggerEnter;
  enter.targets = kTargetActivator;
  enter.trigger = Trigger();
  enter.event_bits = 0x21;
  step.steps.push_back(enter);
  AppliedStepEntry tele;
  tele.kind = StepKind::kTelegram;
  tele.targets = kTargetHeroes | kTargetOther;  // a mixed telegram
  tele.telegram = Telegram();
  step.steps.push_back(tele);
  CHECK(RoundTrip(step));
  CHECK(RoundTrip(AppliedStep{SampleRequests()[3].key, 1, {}}));
  CHECK(EncodeMessage(AppliedStep{}).empty());  // a default key has no event kind
  AppliedStep full = step;
  full.steps.assign(kMaxAppliedSteps, tele);
  CHECK(RoundTrip(full));
  // The longest AppliedStep (the longest key, a skip's, and 32 telegram steps) fits one datagram.
  AppliedStep longest = full;
  longest.key = SampleRequests()[4].key;
  CHECK(RoundTrip(longest));
  CHECK(EncodeMessage(longest).size() <= skylanders::netplay::kMaxPayload);

  CHECK(RoundTrip(LevelEnd{3, NameHash("CLevelEndMessage"), WorldRef(3, 5, 6, 7)}));
  CHECK(RoundTrip(Reaction{7, 3, WorldRef(3, 400, 1, 2), ReactionOutcome::kLanded}));
  CHECK(RoundTrip(Reaction{8, 3, EntityRef{}, ReactionOutcome::kMissed}));
  CHECK(RoundTrip(SessionClosing{}));
  CHECK(RoundTrip(SessionClosed{}));
  CHECK((TypeOf(Message{SessionClosed{}}) == MessageType::kSessionClosed));
  CHECK((TypeOf(Message{PlayerPose{}}) == MessageType::kPlayerPose));
  CHECK((TypeOf(Message{WorldDecision{}}) == MessageType::kWorldDecision));

  // The wire layout of the empty ones.
  CHECK((EncodeMessage(SessionClosing{}) == std::vector<uint8_t>{19, 0, 0, 0}));
  CHECK((EncodeMessage(SessionClosed{}) == std::vector<uint8_t>{20, 0, 0, 0}));
  // A trigger key: peer, generation, kind, ref (kind, generation, mid, name, class), event, sequence.
  {
    WorldRequest r;
    r.key = TriggerRequestKey(0x0102, 3, WorldRef(3, 0x0A0B0C0D, 0, 0), TriggerEvent::kExit, 0x11);
    const auto b = EncodeMessage(r);
    REQUIRE_OR_RETURN_VOID(b.size() == kMessageHeaderSize + 2 + 4 + 1 + 17 + 1 + 4 + 17 + 4 + 4 + 1);
    CHECK(b[0] == 14 && b[4] == 0x02 && b[5] == 0x01 && b[10] == 1 && b[11] == 2);
    CHECK(b[16] == 0x0D && b[19] == 0x0A);       // the mid, little-endian
    CHECK(b[28] == 2 && b[29] == 0x11);          // the event kind, then the enter sequence
    CHECK(b.back() == 0);                        // item 9: the client's verdict, kPassed
  }

  // The key builders fill only their kind's fields.
  const RequestKey t = TelegramRequestKey(2, 3, 9, Telegram());
  CHECK(t.trigger == EntityRef{} && t.event == TriggerEvent::kNone && t.skip_sequence == 0);
  CHECK(TriggerRequestKey(2, 3, Trigger(), TriggerEvent::kEnter, 1) !=
        TriggerRequestKey(2, 3, Trigger(), TriggerEvent::kExit, 1));
  CHECK(NameHash("") == 2166136261u);
  CHECK(NameHash("a") == 0xE40C292Cu);  // FNV-1a test vector
  CHECK(std::string(RefusalReasonName(RefusalReason::kTooFar)) == "too_far");
  CHECK(std::string(RefusalReasonName(RefusalReason::kExitBeforeEnter)) == "exit before enter");
}

// Stage 1B item 9: the client's verdict on a trigger request (amendment l 3.3), and the two refusals
// amendment l adds.
void Version2Item9() {
  for (TriggerChecks c : {TriggerChecks::kPassed, TriggerChecks::kCooldown, TriggerChecks::kAccept,
                          TriggerChecks::kAct, TriggerChecks::kTouchFlag, TriggerChecks::kNotRun}) {
    WorldRequest r = SampleRequests()[0];
    r.checks = c;
    CHECK(RoundTrip(r));
  }
  CHECK(std::string(TriggerChecksName(TriggerChecks::kTouchFlag)) == "touch flag");
  CHECK(std::string(TriggerChecksName(TriggerChecks::kNotRun)) == "not run");
  // A request written before item 9 (no trailing byte) reads as kPassed.
  {
    WorldRequest r = SampleRequests()[1];
    r.checks = TriggerChecks::kPassed;
    std::vector<uint8_t> b = EncodeMessage(r);
    REQUIRE_OR_RETURN_VOID(b.size() > kMessageHeaderSize);
    b.pop_back();
    b[2] = uint8_t(b.size() - kMessageHeaderSize);  // the length (under 256 here)
    b[3] = 0;
    auto back = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(back && back->size() == 1);
    const auto* got = std::get_if<WorldRequest>(&(*back)[0]);
    REQUIRE_OR_RETURN_VOID(got != nullptr);
    CHECK(*got == r);
  }
  // A verdict past kNotRun is neither encoded nor decoded.
  {
    WorldRequest r = SampleRequests()[0];
    r.checks = TriggerChecks(6);
    CHECK(EncodeMessage(r).empty());
    r.checks = TriggerChecks::kNotRun;
    std::vector<uint8_t> b = EncodeMessage(r);
    REQUIRE_OR_RETURN_VOID(!b.empty());
    b.back() = 6;
    CHECK(!DecodeMessages(b));
  }
  // A later field after the verdict is ignored, as for any message.
  {
    WorldRequest r = SampleRequests()[2];
    r.checks = TriggerChecks::kAct;
    std::vector<uint8_t> b = EncodeMessage(r);
    b.push_back(0x5A);
    b[2] = uint8_t(b.size() - kMessageHeaderSize);
    auto back = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(back && back->size() == 1);
    CHECK((*back)[0] == Message{r});
  }
  CHECK(std::string(RefusalReasonName(RefusalReason::kAlreadyFired)) == "already fired");
  CHECK(std::string(RefusalReasonName(RefusalReason::kNotActivated)) == "not activated");
  for (RefusalReason reason : {RefusalReason::kAlreadyFired, RefusalReason::kNotActivated}) {
    WorldDecision d;
    d.key = SampleRequests()[0].key;
    d.outcome = DecisionOutcome::kRefused;
    d.reason = reason;
    d.final = true;
    CHECK(RoundTrip(d));
  }
  // A reason past the ones this version knows still reads as "other".
  {
    WorldDecision d;
    d.key = SampleRequests()[0].key;
    d.outcome = DecisionOutcome::kRefused;
    d.reason = RefusalReason::kNotActivated;
    d.final = true;
    std::vector<uint8_t> b = EncodeMessage(d);
    REQUIRE_OR_RETURN_VOID(b.size() > 8);
    // outcome, reason, flags, then the applied sequence (u32) end the message.
    b[b.size() - 6] = 17;
    auto back = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(back && back->size() == 1);
    const auto* got = std::get_if<WorldDecision>(&(*back)[0]);
    REQUIRE_OR_RETURN_VOID(got != nullptr);
    CHECK(got->reason == RefusalReason::kOther);
  }
}

void Version2Limits() {
  // Not encoded: a non-finite float, an unknown ref kind, a trigger key with no event kind, an
  // inconsistent decision, too many steps, an unknown step target, an unknown reaction outcome.
  PlayerPose nan_pose;
  nan_pose.position[1] = std::numeric_limits<float>::quiet_NaN();
  CHECK(EncodeMessage(nan_pose).empty());
  PlayerPose inf_pose;
  inf_pose.rotation[3] = std::numeric_limits<float>::infinity();
  CHECK(EncodeMessage(inf_pose).empty());
  WorldRequest bad_ref = SampleRequests()[0];
  bad_ref.subject.kind = RefKind(7);
  CHECK(EncodeMessage(bad_ref).empty());
  WorldRequest no_event = SampleRequests()[0];
  no_event.key.event = TriggerEvent::kNone;
  CHECK(EncodeMessage(no_event).empty());
  WorldRequest bad_kind = SampleRequests()[0];
  bad_kind.key.kind = RequestKind(9);
  CHECK(EncodeMessage(bad_kind).empty());
  WorldDecision d;
  d.key = SampleRequests()[0].key;
  d.outcome = DecisionOutcome::kApplied;  // applied but not final
  CHECK(EncodeMessage(d).empty());
  d.final = true;
  d.reason = RefusalReason::kRate;  // applied with a reason
  CHECK(EncodeMessage(d).empty());
  d.outcome = DecisionOutcome::kPending;  // pending and final
  d.reason = RefusalReason::kNone;
  CHECK(EncodeMessage(d).empty());
  d.outcome = DecisionOutcome::kRefused;  // refused without a reason
  CHECK(EncodeMessage(d).empty());
  d.reason = RefusalReason::kTooFar;
  d.final = false;
  d.server_final = true;  // S's final that is not final
  CHECK(EncodeMessage(d).empty());
  AppliedStep many;
  many.key = SampleRequests()[0].key;
  many.steps.resize(kMaxAppliedSteps + 1);
  CHECK(EncodeMessage(many).empty());
  AppliedStep odd_target;
  odd_target.key = SampleRequests()[0].key;
  odd_target.steps.resize(1);
  odd_target.steps[0].targets = 0x01;
  CHECK(EncodeMessage(odd_target).empty());
  CHECK(EncodeMessage(Reaction{1, 1, EntityRef{}, ReactionOutcome(0)}).empty());
  // A kSelf or kNone ref with a world field set (only a kWorld ref has them): two such refs would
  // be one design key but two table keys.
  WorldRequest self_mid = SampleRequests()[0];
  self_mid.subject.mid = 5;
  CHECK(EncodeMessage(self_mid).empty());
  WorldRequest none_hash = SampleRequests()[4];
  none_hash.key.scene.recipient = EntityRef{};
  none_hash.key.scene.recipient.class_hash = 1;
  CHECK(EncodeMessage(none_hash).empty());
  {
    auto b = EncodeMessage(SampleRequests()[0]);
    const size_t subject = kMessageHeaderSize + 2 + 4 + 1 + 17 + 1 + 4;  // the subject ref
    REQUIRE_OR_RETURN_VOID(b.size() > subject + 17);
    CHECK(b[subject] == uint8_t(RefKind::kSelf));
    CHECK(DecodeMessages(b).has_value());
    b[subject + 1 + 4] = 1;  // its mid
    CHECK(!DecodeMessages(b).has_value());
    b[subject + 1 + 4] = 0;
    b[subject + 1 + 4 + 4] = 1;  // its name hash
    CHECK(!DecodeMessages(b).has_value());
  }

  // Decoded with the same checks: patch valid bytes into invalid ones.
  {
    auto b = EncodeMessage(SampleRequests()[0]);
    const size_t ref_kind = kMessageHeaderSize + 2 + 4 + 1;  // the trigger ref's kind byte
    CHECK(b[ref_kind] == uint8_t(RefKind::kWorld));
    b[ref_kind] = 3;
    CHECK(!DecodeMessages(b).has_value());
    b[ref_kind] = uint8_t(RefKind::kWorld);
    const size_t event = ref_kind + 17;
    b[event] = 3;  // not one bit of enter, exit or touch
    CHECK(!DecodeMessages(b).has_value());
    b[event] = 1;
    b[kMessageHeaderSize + 6] = 4;  // request kind 4
    CHECK(!DecodeMessages(b).has_value());
  }
  {
    WorldDecision ok;
    ok.key = SampleRequests()[3].key;  // a telegram key: 2 + 4 + 1 + 4 + 23 bytes
    auto b = EncodeMessage(ok);
    const size_t outcome = kMessageHeaderSize + 2 + 4 + 1 + 4 + 23;
    CHECK(b[outcome] == uint8_t(DecisionOutcome::kPending));
    b[outcome] = 0;
    CHECK(!DecodeMessages(b).has_value());
    b[outcome] = uint8_t(DecisionOutcome::kApplied);  // applied without the final flag
    CHECK(!DecodeMessages(b).has_value());
    b[outcome] = uint8_t(DecisionOutcome::kRefused);
    b[outcome + 1] = 200;  // an unknown reason reads as other
    auto r = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(r && r->size() == 1);
    CHECK(std::get<WorldDecision>((*r)[0]).reason == RefusalReason::kOther);
    b[outcome + 2] = 0x80 | 0x02;  // an unknown flag bit is a later version's: ignored
    r = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(r && r->size() == 1);
    CHECK(std::get<WorldDecision>((*r)[0]).final);
  }
  {
    PlayerPose p;
    p.pose_sequence = 1;
    auto b = EncodeMessage(p);
    const size_t x = kMessageHeaderSize + 8;
    b[x] = 0x00; b[x + 1] = 0x00; b[x + 2] = 0x80; b[x + 3] = 0x7F;  // +infinity
    CHECK(!DecodeMessages(b).has_value());
  }
  {
    AppliedStep st;
    st.key = SampleRequests()[3].key;
    st.steps.resize(1);
    st.steps[0].targets = kTargetOther;
    st.steps[0].telegram = Telegram();
    auto b = EncodeMessage(st);
    const size_t count = kMessageHeaderSize + 2 + 4 + 1 + 4 + 23 + 4;
    CHECK(b[count] == 1);
    b[count] = uint8_t(kMaxAppliedSteps + 1);
    CHECK(!DecodeMessages(b).has_value());
    b[count] = 1;
    b[count + 1] = 3;  // step kind 3
    CHECK(!DecodeMessages(b).has_value());
    b[count + 1] = uint8_t(StepKind::kTelegram);
    b[count + 2] = 0x04;  // a target bit that is none of 0x2, 0x8, 0x80
    CHECK(!DecodeMessages(b).has_value());
  }

  // Every version 2 message is rejected at every cut short of the whole, and a buffer of all of
  // them comes back whole and in order.
  {
    std::vector<uint8_t> b;
    std::vector<Message> all;
    PlayerPose pose;
    pose.pose_sequence = 3;
    pose.position = {1, 2, 3};
    all.push_back(pose);
    all.push_back(PlayerAction{1, 2, 3, 4});
    for (const WorldRequest& r : SampleRequests()) all.push_back(r);
    WorldDecision dec;
    dec.key = SampleRequests()[4].key;
    dec.outcome = DecisionOutcome::kRefused;
    dec.reason = RefusalReason::kSceneEnded;
    dec.final = true;
    all.push_back(dec);
    AppliedStep st;
    st.key = SampleRequests()[1].key;
    st.steps.resize(2);
    st.steps[0].kind = StepKind::kTriggerEnter;
    st.steps[0].trigger = Trigger();
    st.steps[1].telegram = Telegram();
    all.push_back(st);
    all.push_back(LevelEnd{1, 2, WorldRef(1, 2, 3, 4)});
    all.push_back(Reaction{1, 2, SelfRef(2), ReactionOutcome::kRefused});
    all.push_back(SessionClosing{});
    all.push_back(SessionClosed{});
    all.push_back(SceneBarrier{ScenePhase::kOpen, 2, Scene(), SceneKind::kConversation});
    all.push_back(SceneBarrier{ScenePhase::kEnd, 2, Scene(), SceneKind::kCutscene, SceneEndReason::kNatural, 3});
    std::vector<size_t> ends;
    for (const Message& m : all) {
      CHECK(EncodeMessage(m, b));
      ends.push_back(b.size());
    }
    auto back = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(back && back->size() == all.size());
    for (size_t i = 0; i < all.size(); ++i) CHECK(((*back)[i] == all[i]));
    for (size_t n = 1; n < b.size(); ++n) {
      const bool at_end = std::find(ends.begin(), ends.end(), n) != ends.end();
      CHECK(DecodeMessages(std::span<const uint8_t>(b.data(), n)).has_value() == at_end);
    }
  }
}

// The scene barrier (item 12) and the skip key's check field (amendment g).
void Version2Scenes() {
  // Every phase and kind survives a round trip; a level-load scene has no recipient (kNone).
  const SceneKey loaded{1, EntityRef{RefKind::kNone, 2}, NameHash("Tutorial_B_Intro")};
  for (ScenePhase p : {ScenePhase::kOpen, ScenePhase::kAck, ScenePhase::kRelease}) {
    CHECK(RoundTrip(SceneBarrier{p, 2, loaded, SceneKind::kCutscene}));
    CHECK(RoundTrip(SceneBarrier{p, 2, Scene(), SceneKind::kConversation}));
  }
  for (SceneEndReason e : {SceneEndReason::kNatural, SceneEndReason::kStop, SceneEndReason::kLevelChange,
                           SceneEndReason::kOther}) {
    CHECK(RoundTrip(SceneBarrier{ScenePhase::kEnd, 2, loaded, SceneKind::kCutscene, e, 0}));
  }
  CHECK(RoundTrip(SceneBarrier{ScenePhase::kEnd, 2, Scene(), SceneKind::kConversation, SceneEndReason::kNatural, 255}));
  CHECK((TypeOf(Message{SceneBarrier{}}) == MessageType::kSceneBarrier));
  CHECK(std::string(SceneEndReasonName(SceneEndReason::kLevelChange)) == "level change");

  // The wire layout: type 21, phase, generation, sequence, the ref (17), name hash, kind, end, skips.
  {
    const auto b = EncodeMessage(SceneBarrier{ScenePhase::kRelease, 0x0A0B0C0D, loaded, SceneKind::kConversation});
    REQUIRE_OR_RETURN_VOID(b.size() == kMessageHeaderSize + 1 + 4 + 4 + 17 + 4 + 1 + 1 + 1);
    CHECK(b[0] == 21 && b[1] == 0 && b[2] == 33 && b[3] == 0);
    CHECK(b[4] == uint8_t(ScenePhase::kRelease));
    CHECK(b[5] == 0x0D && b[8] == 0x0A);              // the generation, little-endian
    CHECK(b[9] == 1);                                  // the scene sequence
    CHECK(b[13] == uint8_t(RefKind::kNone));           // the recipient's kind
    const uint32_t hash = NameHash("Tutorial_B_Intro");
    CHECK(b[30] == uint8_t(hash) && b[33] == uint8_t(hash >> 24));
    CHECK(b[34] == uint8_t(SceneKind::kConversation) && b[35] == 0 && b[36] == 0);
  }

  // Not encoded: an unknown phase or kind, an end reason outside kEnd (or none in it), skips
  // outside kEnd, a recipient ref with stray fields.
  CHECK(EncodeMessage(SceneBarrier{ScenePhase(0), 2, loaded, SceneKind::kCutscene}).empty());
  CHECK(EncodeMessage(SceneBarrier{ScenePhase(5), 2, loaded, SceneKind::kCutscene}).empty());
  CHECK(EncodeMessage(SceneBarrier{ScenePhase::kOpen, 2, loaded, SceneKind(3)}).empty());
  CHECK(EncodeMessage(SceneBarrier{ScenePhase::kOpen, 2, loaded, SceneKind::kCutscene, SceneEndReason::kStop}).empty());
  CHECK(EncodeMessage(SceneBarrier{ScenePhase::kEnd, 2, loaded, SceneKind::kCutscene, SceneEndReason::kNone}).empty());
  CHECK(EncodeMessage(SceneBarrier{ScenePhase::kAck, 2, loaded, SceneKind::kCutscene, SceneEndReason::kNone, 1}).empty());
  SceneKey stray = loaded;
  stray.recipient.mid = 9;
  CHECK(EncodeMessage(SceneBarrier{ScenePhase::kOpen, 2, stray, SceneKind::kCutscene}).empty());

  // Decoded with the same checks: patch valid bytes into invalid ones.
  {
    auto b = EncodeMessage(SceneBarrier{ScenePhase::kEnd, 2, loaded, SceneKind::kCutscene, SceneEndReason::kStop, 1});
    REQUIRE_OR_RETURN_VOID(b.size() == kMessageHeaderSize + 33);
    const size_t phase = kMessageHeaderSize, kind = kMessageHeaderSize + 30, end = kind + 1;
    CHECK(DecodeMessages(b).has_value());
    b[phase] = 6;
    CHECK(!DecodeMessages(b).has_value());
    b[phase] = uint8_t(ScenePhase::kEnd);
    b[kind] = 0;
    CHECK(!DecodeMessages(b).has_value());
    b[kind] = uint8_t(SceneKind::kCutscene);
    b[end] = 200;  // an unknown end reason reads as other
    auto r = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(r && r->size() == 1);
    CHECK(std::get<SceneBarrier>((*r)[0]).end == SceneEndReason::kOther);
    b[phase] = uint8_t(ScenePhase::kOpen);  // an open with an end reason
    CHECK(!DecodeMessages(b).has_value());
  }

  // The name hash is part of a skip's key: the same occurrence with another name is another key,
  // and it survives the round trip in requests, decisions and applied steps.
  SceneKey other = Scene();
  other.name_hash = NameHash("conversation");
  CHECK(SkipRequestKey(1, 3, Scene(), 2) != SkipRequestKey(1, 3, other, 2));
  WorldRequest skip;
  skip.key = SkipRequestKey(1, 3, other, 2);
  CHECK(RoundTrip(skip));
  {
    auto b = EncodeMessage(skip);
    // peer, generation, kind, then sequence, the ref, the name hash, the skip sequence; then the
    // subject, the event bits, the pose and (item 9) the client's verdict byte.
    const size_t hash = kMessageHeaderSize + 2 + 4 + 1 + 4 + 17;
    REQUIRE_OR_RETURN_VOID(b.size() == kMessageHeaderSize + 2 + 4 + 1 + 4 + 17 + 4 + 4 + 17 + 4 + 4 + 1);
    CHECK(b[hash] == uint8_t(other.name_hash) && b[hash + 3] == uint8_t(other.name_hash >> 24));
    b[hash] ^= 1;
    auto r = DecodeMessages(b);
    REQUIRE_OR_RETURN_VOID(r && r->size() == 1);
    CHECK(std::get<WorldRequest>((*r)[0]).key != skip.key);
  }
}

}  // namespace

int main() {
  // Every message survives a round trip.
  {
    CHECK(RoundTrip(SampleHello()));
    CHECK(RoundTrip(Hello{}));
    CHECK(RoundTrip(Welcome{3, 0xDEADBEEF, 60, 0x0102030405060708ull, 7, "levels/zone_hub"}));
    CHECK(RoundTrip(Refuse{RefuseReason::kFull, "the session is full"}));
    CHECK(RoundTrip(Refuse{RefuseReason::kNotOwner, ""}));
    CHECK(RoundTrip(Bye{}));
    CHECK(RoundTrip(Ping{0xFFFFFFFFFFFFFFFFull}));
    CHECK(RoundTrip(Pong{123456789, 987654321}));
    CHECK(RoundTrip(LevelRequest{"a/b/c"}));
    CHECK(RoundTrip(LevelLoad{42, "z"}));
    CHECK(RoundTrip(LevelLoaded{42}));
    CHECK(RoundTrip(BarrierGo{42, 1'000'000}));
    CHECK(RoundTrip(PlayerList{}));
    CHECK(RoundTrip(PlayerList{{{1, "Owner"}, {2, "Two"}, {4, ""}}}));
    CHECK((TypeOf(Message{Hello{}}) == MessageType::kHello));
    CHECK((TypeOf(Message{PlayerList{}}) == MessageType::kPlayerList));
  }

  // The wire layout of one message: type, length, fields, little-endian.
  {
    auto b = EncodeMessage(LevelLoaded{0x01020304});
    const std::vector<uint8_t> want = {9, 0, 4, 0, 4, 3, 2, 1};
    CHECK(b == want);
    auto h = EncodeMessage(SampleHello());
    CHECK(h.size() == kMessageHeaderSize + 2 + 32 + 2 + 2 + 1 + SampleHello().name.size() + 16);
  }

  // Limits: a name over 32 bytes, a zone over 128, bad UTF-8, too many players: not encoded.
  {
    Hello h;
    h.name = std::string(kMaxNameBytes, 'x');
    CHECK(RoundTrip(h));
    h.name.push_back('x');
    CHECK(EncodeMessage(h).empty());
    std::vector<uint8_t> out = {0xAA};
    CHECK(!EncodeMessage(h, out));
    CHECK(out.size() == 1);  // nothing appended
    CHECK(RoundTrip(LevelLoad{1, std::string(kMaxZoneBytes, 'z')}));
    CHECK(EncodeMessage(LevelLoad{1, std::string(kMaxZoneBytes + 1, 'z')}).empty());
    CHECK(EncodeMessage(LevelRequest{"\xC3"}).empty());  // cut in the middle of a character
    CHECK(EncodeMessage(LevelRequest{std::string("a\0b", 3)}).empty());
    PlayerList many;
    for (uint16_t i = 0; i <= kMaxPlayerEntries; ++i) many.players.push_back({i, "p"});
    CHECK(EncodeMessage(many).empty());
  }

  // Several messages in one buffer come back in order.
  {
    std::vector<uint8_t> b;
    CHECK(EncodeMessage(Ping{1}, b));
    CHECK(EncodeMessage(LevelLoad{2, "two"}, b));
    CHECK(EncodeMessage(Bye{}, b));
    auto back = DecodeMessages(b);
    REQUIRE_OR_RETURN(back && back->size() == 3);
    CHECK(((*back)[0] == Message{Ping{1}}));
    CHECK(((*back)[1] == Message{LevelLoad{2, "two"}}));
    CHECK(((*back)[2] == Message{Bye{}}));
  }

  // Truncated input is rejected at every length short of the whole.
  {
    std::vector<uint8_t> b;
    EncodeMessage(SampleHello(), b);
    EncodeMessage(Welcome{1, 2, 60, 4, 5, "zone"}, b);
    EncodeMessage(PlayerList{{{1, "a"}, {2, "bb"}}}, b);
    CHECK(DecodeMessages(b).has_value());
    int accepted_short = 0;
    for (size_t n = 1; n < b.size(); ++n) {
      auto r = DecodeMessages(std::span<const uint8_t>(b.data(), n));
      // A cut exactly between two messages is a valid, shorter buffer.
      if (r) ++accepted_short;
    }
    CHECK(accepted_short == 2);
    CHECK(DecodeMessages({}).has_value());
    CHECK(DecodeMessages({})->empty());
  }

  // A known message whose fields are shorter than its type needs is rejected even when the length
  // field agrees; extra bytes after the known fields are ignored (a later version's additions).
  {
    std::vector<uint8_t> short_pong = {6, 0, 8, 0, 1, 2, 3, 4, 5, 6, 7, 8};  // Pong needs 16
    CHECK(!DecodeMessages(short_pong).has_value());
    std::vector<uint8_t> long_loaded = {9, 0, 6, 0, 7, 0, 0, 0, 0xEE, 0xEE};
    auto r = DecodeMessages(long_loaded);
    REQUIRE_OR_RETURN(r && r->size() == 1);
    CHECK(((*r)[0] == Message{LevelLoaded{7}}));
    // A string length that runs past its message.
    std::vector<uint8_t> bad_zone = {7, 0, 3, 0, 5, 'a', 'b'};
    CHECK(!DecodeMessages(bad_zone).has_value());
    // A name over the limit, though the bytes are there.
    std::vector<uint8_t> long_name;
    Hello h;
    EncodeMessage(h, long_name);
    // The name length byte sits after type, length, version, digest, figure, variant.
    const size_t at = kMessageHeaderSize + 2 + 32 + 2 + 2;
    CHECK(long_name[at] == 0);
    long_name[at] = 33;
    long_name.insert(long_name.begin() + at + 1, 33, 'n');
    long_name[2] = uint8_t(long_name.size() - kMessageHeaderSize);
    CHECK(!DecodeMessages(long_name).has_value());
    // An unknown refuse reason reads as "other".
    std::vector<uint8_t> odd_reason = {3, 0, 2, 0, 99, 0};
    auto o = DecodeMessages(odd_reason);
    REQUIRE_OR_RETURN(o && o->size() == 1);
    CHECK(std::get<Refuse>((*o)[0]).reason == RefuseReason::kOther);
  }

  // Unknown types are skipped by their length, between known ones.
  {
    std::vector<uint8_t> b;
    EncodeMessage(Ping{5}, b);
    const std::vector<uint8_t> unknown = {0x34, 0x12, 3, 0, 0xAA, 0xBB, 0xCC};
    b.insert(b.end(), unknown.begin(), unknown.end());
    const std::vector<uint8_t> empty_unknown = {0, 0, 0, 0};  // type 0, no body
    b.insert(b.end(), empty_unknown.begin(), empty_unknown.end());
    EncodeMessage(LevelLoaded{6}, b);
    size_t skipped = 0;
    auto r = DecodeMessages(b, &skipped);
    REQUIRE_OR_RETURN(r && r->size() == 2);
    CHECK(skipped == 2);
    CHECK(((*r)[0] == Message{Ping{5}}));
    CHECK(((*r)[1] == Message{LevelLoaded{6}}));
    // An unknown type whose length runs past the end is still a cut buffer.
    const std::vector<uint8_t> cut = {0x34, 0x12, 9, 0, 1};
    CHECK(!DecodeMessages(cut).has_value());
  }

  // UTF-8 helpers.
  {
    CHECK(IsValidUtf8("plain"));
    CHECK(IsValidUtf8("\xE2\x98\x85"));
    CHECK(IsValidUtf8("\xF0\x9F\x98\x80"));
    CHECK(!IsValidUtf8("\xC0\x80"));          // overlong NUL
    CHECK(!IsValidUtf8("\xED\xA0\x80"));      // a surrogate
    CHECK(!IsValidUtf8("\xF4\x90\x80\x80"));  // above U+10FFFF
    CHECK(!IsValidUtf8("\x80"));
    CHECK(TruncateUtf8("abc", 5) == "abc");
    CHECK(TruncateUtf8("ab\xE2\x98\x85", 4) == "ab");
    CHECK(TruncateUtf8("ab\xE2\x98\x85", 5) == "ab\xE2\x98\x85");
    CHECK(IsNone(OwnerToken{}));
    CHECK(!IsNone(SampleHello().owner_token));
    CHECK(std::string(RefuseReasonName(RefuseReason::kDigest)) == "digest");
  }

  Version2RoundTrips();
  Version2Item9();
  Version2Limits();
  Version2Scenes();

  return Finish("online_protocol");
}
