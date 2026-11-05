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
#include <vector>

#include "netplay/lanes.h"
#include "netplay/transport.h"
#include "online/protocol.h"
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

// Both sessions are driven by their owner: Pump(now) every frame receives, answers, resends and
// times out; nothing runs on a thread of its own. `now` defaults to std::chrono::steady_clock;
// tests pass the fake network's time. Not thread-safe: all calls from one thread (the game's).
// Events queue up until the owner takes them with PollEvent.

struct ServerSettings {
  uint32_t hz = 60;             // the session tick rate
  int max_clients = kMaxClients;  // 1 to 4: peer ids 1 to max_clients
  BuildDigest build_digest{};   // a Hello with another digest is refused
  OwnerToken owner_token{};     // the Hello with this token gets id 1; all zero: no owner, id 1 stays free
  uint32_t session_id = 0;      // 0: a random one
};

enum class LeaveReason : uint8_t {
  kBye,       // it said Bye
  kTimeout,   // nothing from it for kPeerTimeout
  kReplaced,  // the owner connected again from another address
};

struct ServerEvent {
  enum class Type {
    kPeerJoined,       // peer_id, name, owner, figure_id, figure_variant, endpoint
    kPeerLeft,         // peer_id, leave_reason
    kLevelRequest,     // the owner asked for `zone`; `generation` is the barrier it opened
    kBarrierReleased,  // generation, tick (the start tick sent in BarrierGo)
    kRefusalSent,      // refuse_reason, text, name (from the Hello, if any), endpoint, peer_id
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
  // or not UTF-8, or the session is not running).
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
  // The join code for the bound address and port; bound to every address (0.0.0.0), the machine's
  // primary address (loopback without a network).
  std::string JoinCode() const;
  // The connected clients, by peer id.
  std::vector<PeerInfo> Peers() const;
  size_t PeerCount() const;
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
  void HandleMessage(Peer& peer, const Message& message, TimePoint now);
  void HandleHello(Peer& peer, const Hello& hello, TimePoint now);
  void SendRefuse(Peer& peer, RefuseReason reason, std::string text, TimePoint now, bool close);
  void DropPeer(Peer& peer, LeaveReason reason, TimePoint now);
  void CheckBarrier(TimePoint now);
  void BroadcastPlayerList(TimePoint now);
  void Post(Peer& peer, netplay::Lane lane, const Message& message, TimePoint now);
  Peer* FindById(uint16_t id);

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

  ClientState State() const { return state_; }
  bool Connected() const { return state_ == ClientState::kConnected; }
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
  void HandleMessage(const Message& message, TimePoint now);
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
  TimePoint next_ping_{};
  TimePoint closing_since_{};
  std::deque<ClientEvent> events_;
  std::string error_;
};

}  // namespace skylanders::online
