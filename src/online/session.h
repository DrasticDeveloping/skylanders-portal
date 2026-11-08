#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "netplay/lanes.h"
#include "netplay/transport.h"
#include "online/hello_guard.h"
#include "online/protocol.h"
#include "online/requests.h"
#include "online/tick.h"

namespace skylanders::online {

// A peer that sends nothing for this long is gone (a ping goes every kPingInterval).
constexpr auto kPeerTimeout = std::chrono::seconds(10);
constexpr auto kPingInterval = std::chrono::seconds(1);
// How long a refused or departed peer's connection stays to ack its last datagrams.
constexpr auto kCloseLinger = std::chrono::seconds(2);
constexpr uint16_t kServerPeerId = 0;
constexpr uint16_t kOwnerPeerId = 1;
constexpr int kMaxClients = 4;
// Connections the server holds that are not welcomed: pending ones (a datagram from an unknown
// address, no valid Hello yet) and closing ones (refused, or welcomed once and gone, lingering
// kCloseLinger). A new address is taken only while fewer than this are held, so the server holds
// at most kMaxClients + kMaxUnadmitted connections, however many addresses send Hellos.
constexpr size_t kMaxUnadmitted = 16;
// Datagrams a session takes from its transport per Pump, so a flood cannot hold the thread that
// pumps it. Every Transport::Receive call takes one datagram (an undeliverable one comes back
// empty), so the bound holds for long datagrams and error reports too.
constexpr int kMaxReceivePerPump = 4096;
// Stage 8: how many HelloCookie answers one Connect follows (a cookie lives 5 to 10 s, so a join
// that waits the full minute for the owner's let in needs about a dozen).
constexpr int kMaxCookieRounds = 32;

// Both sessions are driven by their owner: Pump(now) often (every frame, or every couple of
// milliseconds on a pump thread) receives, answers, resends and times out; nothing runs on a thread
// of its own. `now` defaults to std::chrono::steady_clock; tests pass the fake network's time. Not
// thread-safe: all calls from one thread, the one that pumps it (in the game, session_host's pump
// thread; the game thread reaches it only through that thread's queue). Events queue up until the
// owner takes them with PollEvent.

struct ServerSettings {
  uint32_t hz = 60;             // the session tick rate
  int max_clients = kMaxClients;  // 1 to 4: peer ids 1 to max_clients
  BuildDigest build_digest{};   // a Hello with another digest is refused
  OwnerToken owner_token{};     // the Hello with this token gets id 1; all zero: no owner, id 1 stays free
  uint32_t session_id = 0;      // 0: a random one
  // Version 3 (stage 3 section 3.2, D4): what S's backend runs, sent in every Welcome. Fixed for
  // the session; 0 (no backend) unless the game sets it.
  uint8_t backend_mode = 0;
  uint64_t backend_table_digest = 0;
  // Stage 4 item 2: a Hello flagged kHelloSynthetic is welcomed only when this is set (the game's
  // dev_synthetic_peers, off by default) and it comes from a loopback address (127.x); otherwise
  // it is refused kOther.
  bool allow_synthetic = false;
  // Stage 8 item 4 (design 3.4, D10; the backlog's spoofed Hello): the Hello gate. Off, a Hello
  // opens a connection as before (every loopback session the game starts without its test switch).
  // On, a datagram from an unknown address opens a connection only when it is a Hello the gate
  // (hello_guard.h) admits: size, limits, the cookie, the checks HandleHello makes (version,
  // digest, owner token, synthetic), the owner or the secret, let in, players. Until then every
  // answer is stateless (HelloCookie, JoinWait, Refuse in a kFlagStateless datagram, never larger
  // than the Hello) and nothing is held for the sender but its pending entry.
  bool gate = false;
  // The join code's secret (join code v2). 0: drawn from the OS random source at Start, never 0.
  // Drawn with the gate off too, so JoinCode always names one; only the gate checks it.
  uint32_t join_secret = 0;
  CookieKey cookie_key{};      // all zero: drawn at Start (and at each rotation)
  bool let_anyone_in = false;  // the session option "let anyone with the code in" (off: D15)
};

// What SendBackend did with a frame (stage 3 section 3.2). kFull: the lane's window holds
// netplay::kLaneWindow unacked datagrams; nothing was queued and the caller keeps the frame.
enum class BackendSend : uint8_t { kSent, kFull, kTooBig, kGone };

enum class LeaveReason : uint8_t {
  kBye,       // it said Bye
  kTimeout,   // nothing from it for kPeerTimeout
  kReplaced,  // the owner connected again from another address
  kClosed,    // End session's drain is done for it: its finals went out, then S's Bye (version 2)
  kRemoved,   // the game removed it (RemoveClient: its backend's control overflow or overrun,
              // stage 3); told Bye
};

struct ServerEvent {
  enum class Type {
    kPeerJoined,       // peer_id, name, owner, figure_id, figure_variant, endpoint
    kPeerLeft,         // peer_id, leave_reason
    kLevelRequest,     // the owner asked for `zone`; `generation` is the barrier it opened
    kBarrierReleased,  // generation, tick (the start tick sent in BarrierGo)
    kRefusalSent,      // refuse_reason, text, name (from the Hello, if any), endpoint, peer_id
    // Version 2 (stage 1B).
    kPlayerPose,       // peer_id, pose: only a pose newer than the last from that peer this generation;
                       // it meets that peer's pose preconditions only once the host gives it to
                       // AcceptPose (stage 4: a rate-dropped pose never is)
    kPlayerAction,     // peer_id, action
    kRequestReceived,  // peer_id, request (its key's peer set to the sender), before it is decided
    kEvaluateRequest,  // peer_id, request, generation (the key's): evaluate it on the game thread
                       // (the authority checks in order, then, if they pass, the replay with
                       // g_applying set and SendAppliedStep), then hand the result back with
                       // SubmitEvaluation through the pump thread's queue. The game must drop it
                       // unrun (no SubmitEvaluation needed) when, as it takes it, its generation is
                       // not Generation() or Closing() is true: the table has given that key S's
                       // final already, and a late SubmitEvaluation is dropped anyway. The game
                       // reads both under the same lock the pump thread holds when it changes them.
    kDecisionSent,     // peer_id, decision, sent: an answer for the requester. sent false: it did
                       // not go out (the peer is no longer connected, or its lane's window is
                       // full); for a final the host logs a warning, since that key's client never
                       // gets its final decision
    kPeerClosed,       // peer_id, gone: End session's drain is done for it (finals, then its Bye;
                       // a kPeerLeft with kClosed follows); gone: its SessionClosed did not come
                       // within kCloseWait
    kSceneAck,         // peer_id, scene (phase kAck): the client plays a matching occurrence of
                       // the scene S opened (item 12); not reported once End session began
    // Version 3 (stage 3).
    kBackendFrame,     // peer_id, lane, frame: one backend frame from a welcomed client, in its
                       // lane's order (the bridge's inbound ring takes it at once)
    kPeerLevelLoaded,  // peer_id, generation: the client has the current generation's level up
                       // (its LevelLoaded; also a late joiner's, which no barrier waits for): the
                       // backend's OnPeerLevelReady
    kOwnerFlags,       // peer_id (always the owner), owner_flags: stage 1C's flags snapshot, in
                       // arrival order with the owner's requests; another peer's is refused
                       // kNotOwner, and none is reported once End session began
    // Stage 5.
    kOccurrenceAccept,  // peer_id, occurrence_accept: the client's owners took these retained rows
    kAttackPress,       // peer_id, attack_press: an attack press or release, outside replay; not
                        // reported once End session began
    // Stage 8 item 4 (the Hello gate on).
    kJoinPending,      // pending_id, name, figure_id, figure_variant, endpoint: a joiner with the
                       // right code waits for the owner's LetIn or RefuseJoin (never a player slot)
    kJoinGone,         // pending_id, name, endpoint: its entry expired unanswered (the joiner hears
                       // "not let in: timed out" on its next Hello)
    kJoinCodeSpent,    // the code's wrong-secret budget is spent: no new joiner until NewJoinSecret
                       // (once per secret)
  };
  Type type = Type::kPeerJoined;
  uint16_t peer_id = 0;
  std::string name;
  bool owner = false;
  uint16_t figure_id = 0;
  uint16_t figure_variant = 0;
  LeaveReason leave_reason = LeaveReason::kBye;
  std::string zone;
  uint32_t generation = 0;
  uint64_t tick = 0;
  RefuseReason refuse_reason = RefuseReason::kOther;
  std::string text;
  netplay::Endpoint endpoint;
  PlayerPose pose;
  PlayerAction action;
  WorldRequest request;
  WorldDecision decision;
  bool gone = false;
  bool sent = false;  // kDecisionSent
  SceneBarrier scene;  // kSceneAck
  netplay::Lane lane = netplay::Lane::kGameplay;  // kBackendFrame
  std::vector<uint8_t> frame;                     // kBackendFrame
  OwnerFlags owner_flags;  // kOwnerFlags
  bool synthetic = false;                         // kPeerJoined (stage 4): a synthetic peer
  OccurrenceAccept occurrence_accept;             // kOccurrenceAccept (stage 5)
  AttackPress attack_press;                       // kAttackPress (stage 5)
  uint32_t pending_id = 0;                        // kJoinPending, kJoinGone (stage 8)
};

struct PeerInfo {
  uint16_t peer_id = 0;
  std::string name;
  bool owner = false;
  uint16_t figure_id = 0;
  uint16_t figure_variant = 0;
  netplay::Endpoint endpoint;
  std::chrono::microseconds rtt{0};
  TimePoint last_receive{};  // the last datagram from it; kPeerTimeout after this it is dropped
  netplay::ConnectionStats stats;  // its connection's counters (per-lane bytes included)
};

// The server side: accepts up to max_clients clients, keeps the peer list, answers pings with the
// tick and runs the level barrier.
//
// The level barrier: RequestLevel(zone) (the server's own call, or the owner's LevelRequest)
// opens generation G+1 and sends LevelLoad to every client connected at that moment. It releases
// when the server (MarkServerLoaded) and each of those clients (LevelLoaded) have the level up:
// BarrierGo(G, tick) goes to every client and kBarrierReleased is reported. A client that joins
// while it is open learns the level from its Welcome and is not waited for; a client that leaves
// is no longer waited for. A new request while one is open replaces it (the old one never
// releases).
//
// Requests (version 2): a client's WorldRequest goes through the request table (requests.h); its
// answers go back to the requester on the gameplay lane. A key ready to be evaluated comes out as
// kEvaluateRequest for the game thread, whose result comes back through SubmitEvaluation; only then
// is the key decided. The table is serviced once per Pump. A new generation first sends the finals
// the table owes, then LevelLoad, on the same ordered lane. EndSession sends SessionClosing; each
// client's SessionClosed (or kCloseWait without it) sends that client's finals, then its Bye, and
// the client leaves (kPeerClosed, then kPeerLeft with kClosed). While closing no Hello is welcomed
// and no new generation opens. Offline play never starts a ServerSession, so none of this runs
// there.
//
// The scene barrier (version 2, stage 1B item 12): the game opens, releases and ends a scene
// occurrence with SendSceneBarrier (to every client); a client's kAck comes back as kSceneAck. The
// session keeps no scene state: S auto-acks and releases at once (docs/online-1B.md section 1
// point 6), so nothing here waits for an ack.
class ServerSession {
 public:
  ServerSession() = default;
  ~ServerSession();
  ServerSession(const ServerSession&) = delete;
  ServerSession& operator=(const ServerSession&) = delete;

  // Opens a UDP socket on bind_address:port (port 0: any free one) and starts the session; the
  // tick clock starts at `now`. false if the socket cannot be opened (Error() says why).
  bool Start(std::string_view bind_address, uint16_t port, const ServerSettings& settings,
             TimePoint now = Clock::now());
  // The same over a transport the caller made (the fake network in tests).
  bool Start(std::unique_ptr<netplay::Transport> transport, const ServerSettings& settings,
             TimePoint now = Clock::now());
  // Sends Bye to every client and closes the socket. Reports no events.
  void Stop();
  bool Running() const { return transport_ != nullptr; }

  void Pump(TimePoint now = Clock::now());
  bool PollEvent(ServerEvent& out);

  // Opens the next generation for `zone`; returns it (0 if the zone is longer than kMaxZoneBytes
  // or not UTF-8, the session is not running, or End session's drain is on: the owner's
  // LevelRequest is ignored then too).
  uint32_t RequestLevel(std::string_view zone, TimePoint now = Clock::now());
  // The server's own game has the level of `generation` up.
  void MarkServerLoaded(uint32_t generation, TimePoint now = Clock::now());
  uint32_t Generation() const { return generation_; }
  const std::string& Zone() const { return zone_; }
  bool BarrierOpen() const { return barrier_open_; }
  // The clients the open barrier still waits for (the server itself not included).
  std::vector<uint16_t> BarrierWaitingFor() const { return {waiting_.begin(), waiting_.end()}; }

  uint64_t Tick(TimePoint now = Clock::now()) const { return clock_.TickAt(now); }
  uint32_t Hz() const { return settings_.hz; }
  uint32_t SessionId() const { return settings_.session_id; }
  netplay::Endpoint LocalEndpoint() const;
  uint16_t Port() const { return LocalEndpoint().port; }
  // The join code (v2, with the session's secret) for the bound address and port; bound to every
  // address (0.0.0.0), the machine's primary address (loopback without a network).
  std::string JoinCode() const;
  // Stage 8 item 4: the code for another address and port (the router's external ones on
  // "Internet"); a zero address or port takes JoinCode's. Empty if the session is not running.
  std::string JoinCodeFor(uint32_t address, uint16_t port) const;
  uint32_t JoinSecret() const { return settings_.join_secret; }
  // A new code: a new secret (0: drawn from the OS random source); the gate's wrong-secret budget
  // starts again. Players already in are unaffected; a pending joiner with the old secret stays
  // pending.
  void NewJoinSecret(uint32_t secret = 0);
  // The owner's answer to a pending joiner (kJoinPending). LetIn: its next Hello is admitted (true),
  // or it hears "full" (false, the session is full). RefuseJoin: its next Hello hears "not let in".
  // false: no such entry (answered, expired) or the gate is off.
  bool LetIn(uint32_t pending_id, TimePoint now = Clock::now());
  bool RefuseJoin(uint32_t pending_id, TimePoint now = Clock::now());
  bool GateOn() const { return gate_ != nullptr; }
  const HelloGate* Gate() const { return gate_.get(); }
  // Version 2. The game thread's result for a kEvaluateRequest of `key` (on the pump thread, from
  // its queue): the decision and what follows go out now. false if it is dropped (the key is no
  // longer evaluating: its generation ended, End session began, its peer left).
  bool SubmitEvaluation(const RequestKey& key, const EvaluationResult& result,
                        TimePoint now = Clock::now());
  const RequestTable& Requests() const { return requests_; }
  // Stage 4: the host accepted a kPlayerPose of `peer` (S's rate check passed it, or there is
  // none): that peer's pending requests whose pose precondition it meets go on at the next Pump.
  // A pose the host drops is never given here, so a request naming it waits for a later accepted
  // one (or its pose wait ends). false if it is dropped: another generation, its peer left since,
  // or not newer than the last accepted one.
  bool AcceptPose(uint16_t peer, const PlayerPose& pose);
  // To every client: the steps S applied for one request, or one of its late steps (`late_step`,
  // stage 1B amendment w). false if it cannot be encoded.
  bool SendAppliedStep(const AppliedStep& step, TimePoint now = Clock::now());
  // To one client: a level end, a reaction. false if `peer` is not connected or it cannot be encoded.
  bool SendLevelEnd(uint16_t peer, const LevelEnd& end, TimePoint now = Clock::now());
  bool SendReaction(uint16_t peer, const Reaction& reaction, TimePoint now = Clock::now());
  // To every client: a scene occurrence's open, release or end (the scene barrier, item 12). false
  // for a kAck (the clients' phase), or if it cannot be encoded, or the session is not running.
  bool SendSceneBarrier(const SceneBarrier& scene, TimePoint now = Clock::now());
  // Version 3 (stage 3 section 3.2; stage 2 section 3.7): one backend frame to a welcomed client
  // on `lane` (state or gameplay). kFull when the lane's window is full (nothing queued: the
  // caller keeps the frame at its ring's head and retries), kTooBig for an empty frame or one over
  // kMaxBackendFrameBytes, kGone when `peer` is not a welcomed client.
  BackendSend SendBackend(uint16_t peer, netplay::Lane lane, std::span<const uint8_t> frame,
                          TimePoint now = Clock::now());
  // Stage 4 (docs/online-4.md sections 4.3, 4.4): to every welcomed client but `shown.peer`, an
  // attack press or release S took from that peer (gameplay lane), or its accepted pose (fast
  // lane). false if it cannot be encoded.
  bool SendPlayerActionShown(const PlayerActionShown& shown, TimePoint now = Clock::now());
  bool SendPlayerPoseShown(const PlayerPoseShown& shown, TimePoint now = Clock::now());
  // Stage 5 (future/online-5.md section 3.4, D10): one client's enemy poses, fast lane (the caller
  // filters by that client's interest radius and keeps the 10 Hz rate). false if `peer` is not a
  // welcomed client or the message cannot be encoded.
  bool SendEnemyPoses(uint16_t peer, const EnemyPoses& poses, TimePoint now = Clock::now());
  // Datagrams sent to `peer` on `lane` and not yet acked (netplay's Connection::Unacked: every
  // message on that lane counts, the session's own included); 0 for a peer that is not welcomed.
  size_t BackendUnacked(uint16_t peer, netplay::Lane lane) const;
  // The smoothed round trip to `peer`; 0 for a peer that is not welcomed.
  std::chrono::microseconds BackendRtt(uint16_t peer) const;
  // `peer` is a welcomed client (the bridge's "connected" in its metric snapshot).
  bool PeerConnected(uint16_t peer) const;
  // The game removes a welcomed client (stage 3 section 3.2: its backend's control overflow, or the
  // backend's kPeerOverrun): told Bye, kPeerLeft with kRemoved, its keys forgotten as at any leave.
  // false if `peer` is not a welcomed client.
  bool RemoveClient(uint16_t peer, TimePoint now = Clock::now());
  // End session's drain: SessionClosing to every client; from now on nothing is applied.
  void EndSession(TimePoint now = Clock::now());
  bool Closing() const { return requests_.Closing(); }
  // Every client that was awaited has closed, been taken as gone, or left: Stop() may follow.
  bool CloseComplete() const { return requests_.AllClosed(); }

  // The connected clients, by peer id.
  std::vector<PeerInfo> Peers() const;
  size_t PeerCount() const;
  // Every connection the server holds: welcomed, pending and closing (never more than
  // kMaxClients + kMaxUnadmitted).
  size_t ConnectionCount() const { return peers_.size(); }
  const std::string& Error() const { return error_; }

 private:
  enum class PeerState { kPending, kWelcomed, kClosing, kGone };
  struct Peer {
    netplay::Endpoint endpoint;
    std::unique_ptr<netplay::Connection> conn;
    PeerState state = PeerState::kPending;
    TimePoint created{};
    TimePoint closing_since{};
    uint16_t id = 0;
    bool owner = false;
    Hello hello;
  };

  void HandleDatagram(const netplay::Endpoint& from, std::span<const uint8_t> bytes, TimePoint now);
  // Stage 8: the gate's verdict on a first datagram from `from` (`size` bytes); true: admitted, a
  // connection may be made. Every other verdict is answered statelessly here.
  bool AdmitThroughGate(const netplay::Endpoint& from, size_t size, std::span<const uint8_t> payload,
                        TimePoint now);
  // HandleHello's checks that need no state (version, digest, owner token, synthetic): true when
  // they pass, else the refusal's reason and text.
  bool CompatibleHello(const Hello& hello, const netplay::Endpoint& from, RefuseReason* reason,
                       std::string* text) const;
  // One message to `to` outside any connection (kFlagStateless), at most `limit` bytes on the wire.
  void SendStateless(const netplay::Endpoint& to, const Message& message, size_t limit);
  void SendStatelessRefuse(const netplay::Endpoint& to, const Hello& hello, RefuseReason reason,
                           std::string text, size_t limit);
  void HandleMessage(Peer& peer, const Message& message, netplay::Lane lane, TimePoint now);
  void HandleHello(Peer& peer, const Hello& hello, TimePoint now);
  void SendRefuse(Peer& peer, RefuseReason reason, std::string text, TimePoint now, bool close);
  void DropPeer(Peer& peer, LeaveReason reason, TimePoint now);
  void CheckBarrier(TimePoint now);
  void BroadcastPlayerList(TimePoint now);
  bool Post(Peer& peer, netplay::Lane lane, const Message& message, TimePoint now);
  Peer* FindById(uint16_t id);
  // Sends what the request table decided, in its order.
  void FlushRequests(TimePoint now);

  std::unique_ptr<netplay::Transport> transport_;
  ServerSettings settings_;
  ServerTickClock clock_;
  std::map<netplay::Endpoint, std::unique_ptr<Peer>> peers_;
  std::deque<ServerEvent> events_;
  uint32_t generation_ = 0;
  std::string zone_;
  bool barrier_open_ = false;
  bool server_loaded_ = false;
  std::set<uint16_t> waiting_;
  RequestTable requests_;
  std::unique_ptr<HelloGate> gate_;  // settings_.gate (stage 8)
  bool code_spent_reported_ = false;
  std::string error_;
};

enum class ClientState {
  kIdle,           // not started
  kConnecting,     // Hello sent, waiting for Welcome or Refuse
  kConnected,
  kDisconnecting,  // Bye sent, waiting for its ack (at most kCloseLinger)
  kRefused,
  kDisconnected,
};

enum class DisconnectReason : uint8_t {
  kTimeout,  // nothing from the server for kPeerTimeout (also while connecting)
  kBye,      // the server said Bye
  kLocal,    // Disconnect() was called
};

struct ClientEvent {
  enum class Type {
    kConnected,       // peer_id, tick (the server's, from the Welcome), generation and zone (the
                      // server's current level; generation 0: none yet)
    kRefused,         // refuse_reason, text: not accepted; the session ends
    kRequestRefused,  // refuse_reason, text: a request was turned down; still connected
    kPlayerList,      // players
    kLevelLoad,       // generation, zone: load it, then SendLevelLoaded(generation)
    kBarrierGo,       // generation, tick: everyone has the level; it started at that server tick
    kDisconnected,    // disconnect_reason
    // Version 2 (stage 1B).
    kWorldDecision,   // decision: S's answer to one of this client's requests
    kAppliedStep,     // applied: the steps S applied for a request (any requester's)
    kLevelEnd,        // level_end
    kReaction,        // reaction
    kSessionClosing,  // End session: requests stop; SessionClosed went back already
    kSceneBarrier,    // scene: S opened, released or ended a scene occurrence (never kAck)
    // Version 3 (stage 3).
    kBackendFrame,    // lane, frame: one backend frame from S, in its lane's order
    // Stage 4.
    kPlayerActionShown,  // action_shown: another player's attack press or release
    kPlayerPoseShown,    // pose_shown: another player's pose
    // Stage 5.
    kEnemyPoses,         // enemy_poses: S's enemy poses for this client (fast lane, 10 Hz)
    // Stage 8 item 4: S's gate holds this join for the owner's let in (its first JoinWait;
    // pending_id). The client keeps connecting: a Welcome or a Refuse follows.
    kJoinWaiting,
  };
  Type type = Type::kConnected;
  uint16_t peer_id = 0;
  uint32_t generation = 0;
  std::string zone;
  uint64_t tick = 0;
  RefuseReason refuse_reason = RefuseReason::kOther;
  std::string text;
  std::vector<PlayerEntry> players;
  DisconnectReason disconnect_reason = DisconnectReason::kTimeout;
  WorldDecision decision;
  AppliedStep applied;
  LevelEnd level_end;
  Reaction reaction;
  SceneBarrier scene;
  // kConnected (version 3): S's backend mode and kind table digest from the Welcome.
  uint8_t backend_mode = 0;
  uint64_t backend_table_digest = 0;
  netplay::Lane lane = netplay::Lane::kGameplay;  // kBackendFrame
  std::vector<uint8_t> frame;                     // kBackendFrame
  PlayerActionShown action_shown;                 // kPlayerActionShown (stage 4)
  PlayerPoseShown pose_shown;                     // kPlayerPoseShown (stage 4)
  EnemyPoses enemy_poses;                         // kEnemyPoses (stage 5)
  uint32_t pending_id = 0;                        // kJoinWaiting (stage 8)
};

// The client side: one server, a ping every kPingInterval for the tick estimate and liveness.
class ClientSession {
 public:
  ClientSession() = default;
  ~ClientSession();
  ClientSession(const ClientSession&) = delete;
  ClientSession& operator=(const ClientSession&) = delete;

  // Opens a UDP socket (on 127.0.0.1 for a loopback server, else on every address, any free
  // port) and sends `hello` (its name cut to kMaxNameBytes) to server_address:port. false if the
  // address is not IPv4, the socket cannot be opened or the Hello cannot be encoded (Error()).
  // Stage 8: a Hello without a nonce gets one from the OS random source; a server's HelloCookie
  // makes the client send the Hello again with it (on a fresh connection, at most
  // kMaxCookieRounds times), and its JoinWait (kJoinWaiting) keeps the join alive while the owner
  // decides.
  bool Connect(std::string_view server_address, uint16_t port, const Hello& hello,
               TimePoint now = Clock::now());
  // The same over a transport the caller made (the fake network in tests, or a socket bound
  // elsewhere).
  bool Connect(std::unique_ptr<netplay::Transport> transport, const netplay::Endpoint& server,
               const Hello& hello, TimePoint now = Clock::now());
  // Sends Bye; kDisconnected (kLocal) follows once it is acked, or after kCloseLinger.
  void Disconnect(TimePoint now = Clock::now());

  void Pump(TimePoint now = Clock::now());
  bool PollEvent(ClientEvent& out);

  // Owner only (peer id 1), while connected; false otherwise.
  bool SendLevelRequest(std::string_view zone, TimePoint now = Clock::now());
  // While connected; false otherwise.
  bool SendLevelLoaded(uint32_t generation, TimePoint now = Clock::now());
  // Version 2, while connected and End session has not begun; false otherwise. The pose goes on
  // the fast lane, the rest on the gameplay lane. A request's key gets this client's peer id.
  bool SendPlayerPose(const PlayerPose& pose, TimePoint now = Clock::now());
  bool SendPlayerAction(const PlayerAction& action, TimePoint now = Clock::now());
  bool SendWorldRequest(const WorldRequest& request, TimePoint now = Clock::now());
  // This client plays a matching occurrence of a scene S opened: `scene` goes back as kAck
  // (whatever its phase says), with its generation, key and kind. While connected and End session
  // has not begun (SessionClosed is the last thing a client sends); false otherwise.
  bool SendSceneAck(const SceneBarrier& scene, TimePoint now = Clock::now());
  // Stage 5 (future/online-5.md sections 3.4 and 3.6): the accepted occurrence keys and an attack
  // press or release, gameplay lane, in order with the backend's gameplay frames. While connected
  // and End session has not begun; false otherwise or if the message cannot be encoded.
  bool SendOccurrenceAccept(const OccurrenceAccept& accept, TimePoint now = Clock::now());
  bool SendAttackPress(const AttackPress& press, TimePoint now = Clock::now());
  // Version 3: one backend frame to S on `lane`, while connected (also during End session's
  // drain: the backend's own End session needs its last acks). The results as the server's.
  BackendSend SendBackend(netplay::Lane lane, std::span<const uint8_t> frame,
                          TimePoint now = Clock::now());
  // Datagrams sent to S on `lane` and not yet acked; 0 with no connection.
  size_t BackendUnacked(netplay::Lane lane) const { return conn_ ? conn_->Unacked(lane) : 0; }
  // S's backend mode and table digest from the Welcome (0 before it).
  uint8_t BackendMode() const { return backend_mode_; }
  uint64_t BackendTableDigest() const { return backend_table_digest_; }
  // Stage 1C: the owner's flags snapshot, gameplay lane (ordered with SendWorldRequest). While
  // connected and End session has not begun; false otherwise or when the post fails (the caller
  // keeps the snapshot and sends it again).
  bool SendOwnerFlags(const OwnerFlags& flags, TimePoint now = Clock::now());
  // End session began (SessionClosing came): no request leaves this client any more.
  bool Closing() const { return closing_; }
  // The connection's counters (per-lane bytes included); all zero with no connection.
  netplay::ConnectionStats Stats() const {
    return conn_ ? conn_->Stats() : netplay::ConnectionStats{};
  }

  ClientState State() const { return state_; }
  bool Connected() const { return state_ == ClientState::kConnected; }
  // Stage 8: S's gate holds this join for the owner's let in (a JoinWait came, no Welcome yet).
  bool JoinWaiting() const { return state_ == ClientState::kConnecting && join_waiting_; }
  uint16_t PeerId() const { return peer_id_; }
  bool IsOwner() const { return Connected() && peer_id_ == kOwnerPeerId; }
  uint32_t SessionId() const { return session_id_; }
  // The level the server last named (Welcome or LevelLoad).
  uint32_t Generation() const { return generation_; }
  const std::string& Zone() const { return zone_; }
  const std::vector<PlayerEntry>& Players() const { return players_; }
  const ClientTickEstimator& TickEstimator() const { return ticks_; }
  uint64_t EstimatedServerTick(TimePoint now = Clock::now()) const { return ticks_.EstimateTick(now); }
  // The smoothed round trip of the reliable lanes.
  std::chrono::microseconds Rtt() const;
  const netplay::Endpoint& Server() const { return server_; }
  netplay::Endpoint LocalEndpoint() const;
  const std::string& Error() const { return error_; }

 private:
  void HandleMessage(const Message& message, netplay::Lane lane, TimePoint now);
  // Stage 8: a kFlagStateless datagram from the server (its gate's answer) while connecting.
  void HandleStateless(std::span<const uint8_t> payload, TimePoint now);
  // The Hello (hello_, with its cookie) on a fresh connection, at sequence 1.
  void SendHello(TimePoint now);
  void Finish(DisconnectReason reason);
  bool Post(netplay::Lane lane, const Message& message, TimePoint now);

  std::unique_ptr<netplay::Transport> transport_;
  std::unique_ptr<netplay::Connection> conn_;
  netplay::Endpoint server_;
  ClientState state_ = ClientState::kIdle;
  uint16_t peer_id_ = netplay::kUnknownPeer;
  uint32_t session_id_ = 0;
  uint32_t generation_ = 0;
  std::string zone_;
  std::vector<PlayerEntry> players_;
  ClientTickEstimator ticks_;
  bool closing_ = false;
  uint8_t backend_mode_ = 0;
  uint64_t backend_table_digest_ = 0;
  TimePoint next_ping_{};
  TimePoint closing_since_{};
  std::deque<ClientEvent> events_;
  Hello hello_;               // as sent (stage 8: resent with the gate's cookie)
  int cookie_rounds_ = 0;
  bool join_waiting_ = false;
  std::string error_;
};

}  // namespace skylanders::online
