#pragma once

#include <array>
#include <compare>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

namespace skylanders::online {

// The session messages. On the wire each one is: type (u16), length of what follows (u16), the
// fields, all little-endian; a string is a length byte and that many UTF-8 bytes, a float its
// IEEE-754 bits as a u32 (finite only), a list a count byte and its entries. A datagram's
// payload holds one or more messages. A reader skips message types it does not know by their
// length, and ignores bytes past the fields it knows inside a message (a later version may add
// fields at the end).
// Version 2 (stage 1B, docs/online-1B.md section 2): the hero pose, attack presses, world
// requests and their decisions, applied steps, level ends, reactions and End session's drain. A
// version 1 game is refused at its Hello (kVersion), so the two never mix in one session.
// Version 3 (stage 3, the pilot; future/online-3.md section 3.2): BackendFrame, the backend's
// frames on the session's lanes, and Welcome's backend mode and table digest. A version 2 game is
// refused at its Hello the same way.
constexpr uint16_t kProtocolVersion = 3;
constexpr size_t kMaxNameBytes = 32;
constexpr size_t kMaxZoneBytes = 128;
constexpr size_t kMaxRefuseTextBytes = 200;
constexpr size_t kMaxPlayerEntries = 8;
constexpr size_t kMessageHeaderSize = 4;
// The steps one AppliedStep carries. 32 steps of at most 25 bytes keep the message under a
// datagram's payload (netplay kMaxPayload, 1200 bytes less the header) with room for its key;
// a chain with more steps than this is cut by the sender (stage 1B's two-player trigger, the
// longest chain the design names, sends a handful).
constexpr size_t kMaxAppliedSteps = 32;
// The bytes one BackendFrame carries: the backend's kMaxFrameBytes (skylanders-portal
// src/backend/limits.h), so a frame and its 4-byte message header fit a datagram's payload
// (netplay kMaxPayload, 1174 bytes). The session library does not include the backend; the game's
// bridge checks the two are equal.
constexpr size_t kMaxBackendFrameBytes = 1152;
// Welcome's backend_mode bits (stage 3 section 3.2, D4): what S's backend runs, fixed for the
// session. 0: no backend (the setting off, the plain 1B path).
constexpr uint8_t kBackendModeRecordsPilot = 0x01;  // online_records_pilot
constexpr uint8_t kBackendModeChurn = 0x02;         // dev_backend_churn (the dev.churn kind)
// Stage 4 (docs/online-4.md D5; cross-stage decision 1): the player records, client puppets and
// roster (online_records_players), and 1B's request kinds on the generic table
// (online_requests_generic).
constexpr uint8_t kBackendModeRecordsPlayers = 0x04;
constexpr uint8_t kBackendModeRequestsGeneric = 0x08;
// Stage 5 (future/online-5.md D16; cross-stage decision 1, bit 4): the enemy records, the client
// simulation boundary and the occurrence scheduler (online_records_enemies).
constexpr uint8_t kBackendModeRecordsEnemies = 0x10;
constexpr uint8_t kBackendModeKnown = kBackendModeRecordsPilot | kBackendModeChurn |
                                      kBackendModeRecordsPlayers | kBackendModeRequestsGeneric |
                                      kBackendModeRecordsEnemies;
// Hello's flags (stage 4 item 2): a trailing byte, so a Hello without it reads as 0. A bit outside
// kHelloFlagsKnown makes the message invalid.
constexpr uint8_t kHelloSynthetic = 0x01;  // a synthetic peer (tools/headless_peer); S accepts it
                                           // only under dev_synthetic_peers
constexpr uint8_t kHelloFlagsKnown = kHelloSynthetic;

using BuildDigest = std::array<uint8_t, 32>;
using OwnerToken = std::array<uint8_t, 16>;  // all zero: none
bool IsNone(const OwnerToken& token);

enum class MessageType : uint16_t {
  kHello = 1,
  kWelcome = 2,
  kRefuse = 3,
  kBye = 4,
  kPing = 5,
  kPong = 6,
  kLevelRequest = 7,
  kLevelLoad = 8,
  kLevelLoaded = 9,
  kBarrierGo = 10,
  kPlayerList = 11,
  kPlayerPose = 12,
  kPlayerAction = 13,
  kWorldRequest = 14,
  kWorldDecision = 15,
  kAppliedStep = 16,
  kLevelEnd = 17,
  kReaction = 18,
  kSessionClosing = 19,
  kSessionClosed = 20,
  kSceneBarrier = 21,
  kBackendFrame = 22,
  // Stage 4.
  kPlayerActionShown = 23,
  kPlayerPoseShown = 24,
  // Stage 5.
  kOccurrenceAccept = 25,
  kAttackPress = 26,
  // Stage 1C's block (50-54, the 24-hour push's number blocks): types outside the contiguous run
  // 1..kLastType (protocol.cpp). TypeOf and the decoder map them explicitly.
  kOwnerFlags = 50,
};

enum class RefuseReason : uint8_t {
  kVersion = 1,   // another protocol version
  kDigest = 2,    // another build of the game
  kFull = 3,      // every place is taken
  kNotOwner = 4,  // a wrong owner token, or a request only the owner may make
  kOther = 5,     // also what an unknown reason reads as
};
const char* RefuseReasonName(RefuseReason reason);

// Client to server, first: who is joining.
struct Hello {
  uint16_t protocol_version = kProtocolVersion;
  BuildDigest build_digest{};
  uint16_t figure_id = 0;
  uint16_t figure_variant = 0;
  std::string name;        // UTF-8, at most kMaxNameBytes
  OwnerToken owner_token{};
  uint8_t flags = 0;       // kHello* bits (stage 4); trailing on the wire
  bool operator==(const Hello&) const = default;
};

// Server to client: accepted. tick_hz is the rate of the server's tick (the client's tick
// estimate needs it). level_generation 0: no level yet. Version 3 adds two fields at the end
// (stage 3 section 3.2, D4): backend_mode (kBackendMode* bits; a bit outside kBackendModeKnown
// makes the message invalid) and backend_table_digest (the digest of S's kind table for that
// mode; 0 with mode 0). A client builds its kind table from the mode before its first level load
// and leaves the session if its own digest for that mode differs.
struct Welcome {
  uint16_t peer_id = 0;
  uint32_t session_id = 0;
  uint16_t tick_hz = 0;
  uint64_t server_tick = 0;
  uint32_t level_generation = 0;
  std::string level_zone;  // at most kMaxZoneBytes
  uint8_t backend_mode = 0;
  uint64_t backend_table_digest = 0;
  bool operator==(const Welcome&) const = default;
};

// Server to client: not accepted (before a Welcome), or a request turned down (after it).
struct Refuse {
  RefuseReason reason = RefuseReason::kOther;
  std::string text;  // at most kMaxRefuseTextBytes
  bool operator==(const Refuse&) const = default;
};

// Either side: leaving.
struct Bye {
  bool operator==(const Bye&) const = default;
};

// Client to server, every second: clock sync and liveness.
struct Ping {
  uint64_t client_time_us = 0;
  bool operator==(const Ping&) const = default;
};

struct Pong {
  uint64_t client_time_us = 0;  // the Ping's, echoed
  uint64_t server_tick = 0;
  bool operator==(const Pong&) const = default;
};

// The owner to the server: change the level.
struct LevelRequest {
  std::string zone;
  bool operator==(const LevelRequest&) const = default;
};

// Server to clients: load this level; it is generation `generation`.
struct LevelLoad {
  uint32_t generation = 0;
  std::string zone;
  bool operator==(const LevelLoad&) const = default;
};

// Client to server: the level of this generation is up.
struct LevelLoaded {
  uint32_t generation = 0;
  bool operator==(const LevelLoaded&) const = default;
};

// Server to clients: everyone has the level; it starts at this server tick.
struct BarrierGo {
  uint32_t generation = 0;
  uint64_t start_tick = 0;
  bool operator==(const BarrierGo&) const = default;
};

struct PlayerEntry {
  uint16_t peer_id = 0;
  std::string name;
  bool operator==(const PlayerEntry&) const = default;
};

// Server to clients: who is in the session (sent when it changes).
struct PlayerList {
  std::vector<PlayerEntry> players;  // at most kMaxPlayerEntries
  bool operator==(const PlayerList&) const = default;
};

// ---------------------------------------------------------------------------------------------
// Version 2: the playable route (docs/online-1B.md section 2, "Protocol v2", "Requests: keys,
// retries and pose order", "Entity identity", "The echo boundary").

// FNV-1a, 32 bits, over the bytes of a name: the name and class hashes of an EntityRef. Every peer
// computes it the same way from the same string, so a hash is comparable across peers.
uint32_t NameHash(std::string_view name);

// Which entity a ref names. kSelf is the sender's own hero (the entering entity of its trigger
// event, the activator of its telegram): the receiver maps it to the sender's puppet. kWorld is an
// entity of the level, named by its world mID and validated by its name and class hashes.
enum class RefKind : uint8_t {
  kNone = 0,
  kSelf = 1,
  kWorld = 2,
};

// An entity across peers (design: "Entity identity"). It resolves only in its own generation. The
// world mID is the key; the hashes validate it and do not tell repeated unnamed entities apart. A
// ref that resolves to nothing or to more than one entity is refused `unresolved`, never guessed.
// For kNone and kSelf only `generation` means anything; the other fields must be 0 (a ref with
// any of them set is neither encoded nor decoded).
struct EntityRef {
  RefKind kind = RefKind::kNone;
  uint32_t generation = 0;
  uint32_t mid = 0;
  uint32_t name_hash = 0;
  uint32_t class_hash = 0;
  auto operator<=>(const EntityRef&) const = default;
};
EntityRef SelfRef(uint32_t generation);
EntityRef WorldRef(uint32_t generation, uint32_t mid, uint32_t name_hash, uint32_t class_hash);

// "Telegram N of messenger M" (design: the telegram key; X7 named it in prep as
// "<messenger>#<N>", N its index in its telegram list, with the list's owner
// "<component class>+<offset>" logged beside it). `list_owner_hash` is NameHash of that owner
// text: it validates the key the way the cross-side check compared owners, it is not a second key.
struct TelegramKey {
  EntityRef messenger;
  uint32_t list_owner_hash = 0;
  uint16_t index = 0;
  auto operator<=>(const TelegramKey&) const = default;
};

// A scene occurrence (design: Scenes): the request key's generation, the scene's sequence number on
// S in that generation, and the scene message's recipient. `name_hash` is the check field
// (docs/online-1B.md, amendment g): NameHash of the cutscene's name (the instance's +44), or of
// "conversation" for a conversation. It is part of the key, so a skip that names another scene than
// S's occurrence with that sequence and recipient is another key, and S finds no open occurrence
// for it (`scene ended`).
struct SceneKey {
  uint32_t scene_sequence = 0;
  EntityRef recipient;
  uint32_t name_hash = 0;
  auto operator<=>(const SceneKey&) const = default;
};

enum class RequestKind : uint8_t {
  kTrigger = 1,    // the hero's trigger event at sub_82AE9330
  kTelegram = 2,   // a hero-activated telegram that does not start inside a trigger enter
  kSceneSkip = 3,  // a skip of a scene occurrence
};

// The r6 bit of sub_82AE9330 (X8 confirms the values; touch only if X8 keeps it in requests).
enum class TriggerEvent : uint8_t {
  kNone = 0,
  kEnter = 1,
  kExit = 2,
  kTouch = 4,
};

// The request key (design: "Requests: keys, retries and pose order"). Only the fields of its kind
// are on the wire; the others stay 0 (the builders below fill a key that way, and the decoder
// leaves them so), so keys compare equal exactly when the design's tuples do.
//   trigger:   (peer, trigger, event kind, the client's enter sequence, generation); an exit and a
//              touch carry the enter sequence of the enter they belong to
//   telegram:  (peer, the peer's request sequence in the generation, telegram key, generation)
//   skip:      (peer, scene occurrence (generation, scene sequence, recipient, name hash), the
//              peer's skip sequence in the generation)
// The server overwrites `peer` with the sender's peer id, so a client cannot speak for another.
struct RequestKey {
  uint16_t peer = 0;
  uint32_t generation = 0;
  RequestKind kind = RequestKind::kTrigger;
  EntityRef trigger;                          // kTrigger
  TriggerEvent event = TriggerEvent::kNone;   // kTrigger
  uint32_t enter_sequence = 0;                // kTrigger
  uint32_t request_sequence = 0;              // kTelegram
  TelegramKey telegram;                       // kTelegram
  SceneKey scene;                             // kSceneSkip
  uint32_t skip_sequence = 0;                 // kSceneSkip
  auto operator<=>(const RequestKey&) const = default;
};
RequestKey TriggerRequestKey(uint16_t peer, uint32_t generation, const EntityRef& trigger,
                             TriggerEvent event, uint32_t enter_sequence);
RequestKey TelegramRequestKey(uint16_t peer, uint32_t generation, uint32_t request_sequence,
                              const TelegramKey& telegram);
RequestKey SkipRequestKey(uint16_t peer, uint32_t generation, const SceneKey& scene,
                          uint32_t skip_sequence);

// The elements a hero plays, as CurrentFigure::elements carries them (stage 1B item 11, the
// element-gate scaffold): bit e (1 to 8) set when the game's own element test answered true for
// element e on the client's hero (either half of a swapper), and kElementsKnown (bit 0) set when the
// client read them at all. 0: not read (a client before item 11, or no hero to read); the server
// then falls back to the figure at join. Nothing above bit 8 is used.
constexpr uint16_t kElementsKnown = 0x0001;
constexpr uint16_t kElementsUsed = 0x01FF;
// A value the wire takes: no bit above 8, and element bits only with kElementsKnown.
bool ValidElements(uint16_t elements);
// Element e (1 to 8) is in `elements`.
constexpr bool HasElement(uint16_t elements, int e) {
  return (elements & kElementsKnown) && e >= 1 && e <= 8 && ((elements >> e) & 1u) != 0;
}

// What a client stands on: the character and, for a swapper, both halves (0 when not a swapper),
// and the elements its hero plays now. Where a client reads the figure ids is still open (X7's;
// design section 6, the element gate); item 11's scaffold reads `elements`, which the client asks
// the game's own element test for, so it follows a swap of halves without a figure-id reader. On
// the wire `elements` is a trailing u16 after the six ids (stage 1B item 11): a pose that ends
// before it reads as 0 (not read); a value ValidElements refuses makes the message invalid.
struct CurrentFigure {
  uint16_t figure_id = 0;
  uint16_t figure_variant = 0;
  uint16_t top_id = 0;
  uint16_t top_variant = 0;
  uint16_t bottom_id = 0;
  uint16_t bottom_variant = 0;
  uint16_t elements = 0;
  bool operator==(const CurrentFigure&) const = default;
};

// Stage 4 (docs/online-4.md section 4.3, plan 2.1): the pose's teleport counter, a trailing u16
// after `elements` (a pose without it reads as 0). The owning client moves it at a teleport, a
// bounce pad or a fall reset, so S's rate check lets that pose through.

// Client to server, fast lane, about 20 Hz (tests/thresholds.toml players_hz): the hero's pose.
// `pose_sequence` counts the peer's poses from 1 (0 is never sent: only a scene skip's request
// carries pose sequence 0, for "needs no pose"). The server drops a pose not newer than the last one it has from that peer in
// that generation. Position is the hero's +40 vector; rotation is a quaternion (x, y, z, w) whose
// source X3 settles at item 6. Every float must be finite.
struct PlayerPose {
  uint32_t pose_sequence = 0;
  uint32_t generation = 0;
  std::array<float, 3> position{};
  std::array<float, 4> rotation{0, 0, 0, 1};
  CurrentFigure figure;
  uint16_t teleports = 0;  // stage 4: the teleport counter (above)
  bool operator==(const PlayerPose&) const = default;
};

// Client to server, gameplay lane: an attack press (the owner's decision 1, attack replay).
// `press_sequence` is the `press <peer>:<seq>` of the press-to-reaction lines.
struct PlayerAction {
  uint32_t press_sequence = 0;
  uint32_t generation = 0;
  uint32_t attack_id = 0;
  uint32_t pose_sequence = 0;  // the pose it was pressed at
  bool operator==(const PlayerAction&) const = default;
};

// The client's own verdict on a trigger event (docs/online-1B-amendment-l.md 3.3): the trigger's
// checks it ran read-only with its own hero, in the game's order per kind (enter: the +784
// cooldown, the accept, the act check; exit: the accept, the act check; touch: flag 23, the
// accept), only when the event's r6 has 0x20. kPassed: all passed; one of the four: the check that
// refused (the key is made anyway because the volume's r7 has a list or delegate set for the
// event); kNotRun: r6 has no 0x20, so no check runs natively. S answers each check under its
// replay token as the client did. On the wire a trailing byte (stage 1B item 9): a request that
// ends before it reads as kPassed; a value above kNotRun makes the message invalid.
enum class TriggerChecks : uint8_t {
  kPassed = 0,
  kCooldown = 1,
  kAccept = 2,
  kAct = 3,
  kTouchFlag = 4,
  kNotRun = 5,
};
// "passed", "cooldown", "accept", "act", "touch flag", "not run".
const char* TriggerChecksName(TriggerChecks checks);

// Client to server, gameplay lane (reliable, ordered): one request per chain. `subject` is the
// entity that acts (the entering entity or the activator; kSelf for the client's own hero).
// `event_bits` is the whole r6 of the trigger event (the event kind is the key's), 0 otherwise.
// `checks` is the client's verdict for a trigger request (kPassed for the other kinds).
struct WorldRequest {
  RequestKey key;
  EntityRef subject;
  uint32_t event_bits = 0;
  uint32_t pose_sequence = 0;  // the pose the request was made at; 0 (needs none): a skip only,
                               // a trigger or telegram request with 0 is refused `unsupported`
  TriggerChecks checks = TriggerChecks::kPassed;
  bool operator==(const WorldRequest&) const = default;
};

enum class DecisionOutcome : uint8_t {
  kApplied = 1,
  kPending = 2,
  kRefused = 3,
};

// Why a request was refused (design section 5, the request lifecycle lines). Retryable for an
// enter or a touch: too_far, rate, not released, not in play; for an exit: exit before enter.
// Every other reason, and every refusal of a telegram or a skip, is terminal (requests.h
// IsRetryable). An unknown value reads as kOther, which is terminal. kAlreadyFired and
// kNotActivated are amendment l's (docs/online-1B-amendment-l.md 3.4, 3.5, 3.9; stage 1B item 9):
// a single-fire trigger another enter sequence (or a native contact) consumed in the generation,
// and a replay that ran neither the trigger's dispatch nor an r7 call. A version 2 game from before
// item 9 reads them as kOther, also terminal.
enum class RefusalReason : uint8_t {
  kNone = 0,
  kTooFar = 1,
  kRate = 2,
  kNotReleased = 3,
  kNotInPlay = 4,
  kExitBeforeEnter = 5,
  kStale = 6,
  kAlreadySent = 7,
  kUnresolved = 8,
  kUnsupported = 9,
  kEnterRefused = 10,
  kSceneEnded = 11,
  kGenerationEnded = 12,
  kSessionEnded = 13,
  kOther = 14,
  kAlreadyFired = 15,
  kNotActivated = 16,
};
// The reason as the `Online:` lines write it ("too_far", "not released", ...).
const char* RefusalReasonName(RefusalReason reason);

// Server to the requester, gameplay lane: what became of a request key. `final` (only S sets it):
// the key's one final decision (applied, a terminal refusal, or S's own final). `server_final`: S
// closed the key itself, not a check (a retried key's last reason when its exit came, a generation
// change, End session): the requester logs `final <key> refused (<reason>)` only for these.
// `cached`: a repeat of the key's last decision, answered from its state; never a second decision.
// applied: final, no reason; pending: not final, no reason; refused: a reason.
struct WorldDecision {
  RequestKey key;
  DecisionOutcome outcome = DecisionOutcome::kPending;
  RefusalReason reason = RefusalReason::kNone;
  bool cached = false;
  bool final = false;
  bool server_final = false;
  uint32_t applied_sequence = 0;  // S's apply counter for an applied key, else 0
  bool operator==(const WorldDecision&) const = default;
};

// The target classes of a step (design: the echo boundary), read by S from the deliveries
// sub_82A34728 makes while it applies the step: 0x2 the activator and 0x8 both hero slots (the
// telegram's +28 bits of the same value), 0x80 any other target. A mixed step has several.
constexpr uint8_t kTargetActivator = 0x02;
constexpr uint8_t kTargetHeroes = 0x08;
constexpr uint8_t kTargetOther = 0x80;
constexpr uint8_t kTargetMask = kTargetActivator | kTargetHeroes | kTargetOther;

enum class StepKind : uint8_t {
  kTriggerEnter = 1,  // the originating trigger enter, with its bits
  kTelegram = 2,      // a telegram, by its telegram key
};

struct AppliedStepEntry {
  StepKind kind = StepKind::kTelegram;
  uint8_t targets = 0;
  EntityRef trigger;       // kTriggerEnter
  uint32_t event_bits = 0; // kTriggerEnter
  TelegramKey telegram;    // kTelegram
  bool operator==(const AppliedStepEntry&) const = default;
};

// Server to every client, gameplay lane: the steps S applied for one request, in order (the
// originating step, then the follow-on steps of the same apply). The requester is the key's peer.
// `late_step` (stage 1B amendment w): not the request's own apply but one telegram S's messengers
// sent for it on a later frame (a late step), sent to every client but the requester (which plays
// its own late steps locally); `applied_sequence` is then the apply's it follows. On the wire a
// trailing flags byte (bit 0x01): a message that ends before it reads as false; other bits are a
// later version's and are ignored.
struct AppliedStep {
  RequestKey key;
  uint32_t applied_sequence = 0;
  std::vector<AppliedStepEntry> steps;  // at most kMaxAppliedSteps
  bool late_step = false;
  bool operator==(const AppliedStep&) const = default;
};

// Server to the clients the echo boundary names, gameplay lane: a level end S applied, deduplicated
// per (class, recipient, generation).
struct LevelEnd {
  uint32_t generation = 0;
  uint32_t class_hash = 0;  // NameHash of the level-end message class
  EntityRef recipient;
  bool operator==(const LevelEnd&) const = default;
};

enum class ReactionOutcome : uint8_t {
  kLanded = 1,
  kRefused = 2,
  kMissed = 3,
};

// Server to the requester, gameplay lane: what an attack press did (press to reaction, interim).
struct Reaction {
  uint32_t press_sequence = 0;
  uint32_t generation = 0;
  EntityRef victim;
  ReactionOutcome outcome = ReactionOutcome::kMissed;
  bool operator==(const Reaction&) const = default;
};

// Server to every client, gameplay lane: End session's drain starts. The client stops making
// requests and answers SessionClosed after its last request.
struct SessionClosing {
  bool operator==(const SessionClosing&) const = default;
};

// Client to server, gameplay lane: the watermark of what the client sent; nothing follows it.
struct SessionClosed {
  bool operator==(const SessionClosed&) const = default;
};

// The scene barrier (stage 1B item 12; docs/online-1B.md section 1 point 6, section 2 Scenes, and
// amendments f and g): the level barrier's pattern for one scene occurrence, without SceneRecord
// (stage 6). S opens the occurrence when its own scene starts (kOpen to every client), acknowledges
// it as ready itself at once and releases it (kRelease to every client); it never waits for the
// clients and never holds its scene (the pause stack, the only native hold, freezes the scene
// itself). Each client that plays a matching occurrence of its own (the same kind, name hash and
// recipient in that generation) answers kAck. S's scene end goes to every client as kEnd.
// Directions: kOpen, kRelease and kEnd server to client; kAck client to server. Gameplay lane.
enum class ScenePhase : uint8_t {
  kOpen = 1,
  kAck = 2,
  kRelease = 3,
  kEnd = 4,
};
enum class SceneKind : uint8_t {
  kCutscene = 1,      // a CCutsceneManager play
  kConversation = 2,  // a CConversationSystem conversation
};
// How S's occurrence ended (kEnd only; kNone in the other phases). An unknown value reads as kOther.
enum class SceneEndReason : uint8_t {
  kNone = 0,
  kNatural = 1,      // its timeline or its last line
  kStop = 2,         // a stop: the script's, stop all, the stage, a restart
  kLevelChange = 3,  // its generation ended while it was open
  kOther = 4,
};
const char* SceneEndReasonName(SceneEndReason reason);

struct SceneBarrier {
  ScenePhase phase = ScenePhase::kOpen;
  uint32_t generation = 0;
  SceneKey scene;
  SceneKind kind = SceneKind::kCutscene;
  SceneEndReason end = SceneEndReason::kNone;
  uint8_t skips = 0;  // kEnd: skips S applied to it (saturates at 255)
  bool operator==(const SceneBarrier&) const = default;
};

// Version 3 (stage 3 section 3.2; stage 2 section 3.7): one backend frame, either direction, on the
// lane the backend chose for it (state or gameplay; the fast lane carries none in stage 3). The
// bytes are the rest of the message: a run of the backend's own messages (type, length, fields),
// which the session never reads. Empty or longer than kMaxBackendFrameBytes: invalid.
struct BackendFrame {
  std::vector<uint8_t> bytes;
  bool operator==(const BackendFrame&) const = default;
};

// Stage 4 (docs/online-4.md section 4.4): server to the other clients, gameplay lane (reliable, in
// order): one attack press or release S took from `peer` (the attack replay's accept), for the
// puppets' presentation. A release carries its press's sequence and the slot with the release bit
// (attack_id), so a receiver never orders releases against presses by sequence.
struct PlayerActionShown {
  uint16_t peer = 0;
  uint32_t peer_epoch = 0;
  uint32_t generation = 0;
  uint32_t press_sequence = 0;
  uint32_t attack_id = 0;
  uint32_t pose_sequence = 0;
  bool operator==(const PlayerActionShown&) const = default;
};

// Stage 4 (docs/online-4.md section 4.3, D6): server to the other clients, fast lane: another
// player's pose as S accepted it (newest only, no distance filter).
struct PlayerPoseShown {
  uint16_t peer = 0;
  PlayerPose pose;
  bool operator==(const PlayerPoseShown&) const = default;
};

// Stage 1C (the save spike, item 4; D5, D21): the owner's own HasTakenDamage, HasSwapped and
// PlayerDied (player keys 1, 2, 5 of CGameState for the owner's player index) and the map game keys
// 14 (SkylanderDamageTaken) and 15 (SkylandersDefeated), as the owner's game holds them. The owner
// to the server, gameplay lane (reliable, ordered: the lane its world requests use, so a snapshot
// sent before a request arrives before it). A full snapshot every time, never a delta: `revision`
// is 1 at each new generation and one more per send, so the server keeps the newest per
// generation. Sent only with online_save_handback on; a server refuses it from any peer but the
// owner (kNotOwner). Wire: generation u32, revision u32 (neither 0), flags u8 (0x01 damage, 0x02
// swapped; other bits are a later version's and ignored), died, damage taken and defeated as
// signed 32-bit values: 21 bytes. A message that ends early is invalid; bytes past it are ignored.
struct OwnerFlags {
  uint32_t generation = 0;
  uint32_t revision = 0;
  bool has_taken_damage = false;  // ePSK_Map_HasTakenDamage (player key 1)
  bool has_swapped = false;       // ePSK_Map_HasSwapped (player key 2)
  int32_t player_died = 0;        // ePSK_Map_PlayerDied (player key 5)
  int32_t damage_taken = 0;       // eGSK_Map_SkylanderDamageTaken (game key 14)
  int32_t defeated = 0;           // eGSK_Map_SkylandersDefeated (game key 15)
  bool operator==(const OwnerFlags&) const = default;
};

// Stage 5 (future/online-5.md section 3.4, D28): client to S, gameplay lane (reliable, ordered,
// never dropped by the inbound ring): the retained occurrence rows (`attack`, `telegraph`, a
// `projectile`'s terminal revision) whose owner on this client (the occurrence scheduler, the
// retired-shot ledger) has taken the full payload and committed to one outcome since the last
// message. Each key is the backend's RecordKey wire form (kind u16, EntityId 21 bytes, slot u16:
// the record key of the row), which the session library carries without reading; the game decodes
// it. One message per pump; keys of another generation are ignored by S. Empty, or more than
// kMaxOccurrenceKeys: invalid.
constexpr size_t kOccurrenceKeyBytes = 25;
constexpr size_t kMaxOccurrenceKeys = 40;
using OccurrenceKeyBytes = std::array<uint8_t, kOccurrenceKeyBytes>;
struct OccurrenceAccept {
  uint32_t generation = 0;
  std::vector<OccurrenceKeyBytes> keys;  // 1 to kMaxOccurrenceKeys
  bool operator==(const OccurrenceAccept&) const = default;
};

// Stage 5 (future/online-5.md section 3.6, D9): client to S, gameplay lane: an attack press or
// release announced outside attack replay (S never writes it into a puppet's input). S's press
// registry takes it on receipt, before the same pump hands later backend frames (a hit's
// `enemy.hit`) to the backend, so a hit sent after its press finds it. `release`: the slot's
// release of the press `press_sequence` (its hold ends at `press_tick`). `slot` is 0 to 3.
struct AttackPress {
  uint32_t press_sequence = 0;
  uint32_t generation = 0;
  uint8_t slot = 0;
  bool release = false;
  uint32_t pose_sequence = 0;
  uint64_t press_tick = 0;  // the shared tick of the press (or of the release)
  bool operator==(const AttackPress&) const = default;
};
constexpr uint8_t kMaxAttackSlot = 3;

// The variant's order is the type numbering for the contiguous run (Hello = 1 ... AttackPress =
// 26); the block types after it (OwnerFlags = 50) are mapped by TypeOf.
using Message = std::variant<Hello, Welcome, Refuse, Bye, Ping, Pong, LevelRequest, LevelLoad,
                             LevelLoaded, BarrierGo, PlayerList, PlayerPose, PlayerAction,
                             WorldRequest, WorldDecision, AppliedStep, LevelEnd, Reaction,
                             SessionClosing, SessionClosed, SceneBarrier, BackendFrame,
                             PlayerActionShown, PlayerPoseShown, OccurrenceAccept, AttackPress,
                             OwnerFlags>;
MessageType TypeOf(const Message& message);

// Appends one message to `out`. false (nothing appended) if a string is over its limit or not
// UTF-8, or a list is too long.
bool EncodeMessage(const Message& message, std::vector<uint8_t>& out);
// The message alone; empty if it cannot be encoded.
std::vector<uint8_t> EncodeMessage(const Message& message);

// Every message in `bytes`, in order. nullopt if anything is cut short (a header, or a message
// whose length runs past the end), or a known message's fields are too short or invalid. Unknown
// types are skipped and counted in `skipped`.
std::optional<std::vector<Message>> DecodeMessages(std::span<const uint8_t> bytes,
                                                   size_t* skipped = nullptr);

// Well-formed UTF-8 without NUL bytes.
bool IsValidUtf8(std::string_view text);
// The longest prefix of `text` of at most `max_bytes` that does not cut a character in two.
std::string TruncateUtf8(std::string_view text, size_t max_bytes);

}  // namespace skylanders::online
