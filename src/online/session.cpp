#include "online/session.h"

#include <algorithm>
#include <random>
#include <variant>

#include "netplay/datagram.h"
#include "netplay/udp_socket.h"
#include "online/join_code.h"

namespace skylanders::online {

using netplay::Connection;
using netplay::Endpoint;
using netplay::Lane;
using netplay::LaneMessage;
using std::chrono::duration_cast;
using std::chrono::microseconds;

namespace {

// Connections opened by datagrams from unknown addresses that have not sent a Hello yet.
constexpr size_t kMaxPending = 16;
// Datagrams taken from the socket per Pump, so a flood cannot hold the game's thread.
constexpr int kMaxReceivePerPump = 4096;

uint32_t RandomSessionId() {
  std::random_device rd;
  uint32_t id = 0;
  while (id == 0) id = rd();
  return id;
}

uint64_t ToMicros(TimePoint t) {
  return uint64_t(duration_cast<microseconds>(t.time_since_epoch()).count());
}

TimePoint FromMicros(uint64_t us) {
  return TimePoint(duration_cast<Clock::duration>(microseconds(int64_t(us))));
}

std::vector<uint8_t> Encoded(const Message& m) { return EncodeMessage(m); }

}  // namespace

// ---------------------------------------------------------------------------------------------
// ServerSession

ServerSession::~ServerSession() { Stop(); }

bool ServerSession::Start(std::string_view bind_address, uint16_t port,
                          const ServerSettings& settings, TimePoint now) {
  Stop();  // frees the port when the same one is asked for again
  auto socket = std::make_unique<netplay::UdpSocket>();
  if (!socket->Open(bind_address, port)) {
    error_ = socket->LastError();
    return false;
  }
  return Start(std::move(socket), settings, now);
}

bool ServerSession::Start(std::unique_ptr<netplay::Transport> transport,
                          const ServerSettings& settings, TimePoint now) {
  Stop();
  error_.clear();
  if (!transport) {
    error_ = "no transport";
    return false;
  }
  settings_ = settings;
  settings_.hz = std::clamp<uint32_t>(settings_.hz, 1, 0xFFFF);  // Welcome carries it as a u16
  settings_.max_clients = std::clamp(settings_.max_clients, 1, kMaxClients);
  if (settings_.session_id == 0) settings_.session_id = RandomSessionId();
  clock_ = ServerTickClock(settings_.hz, now);
  transport_ = std::move(transport);
  peers_.clear();
  events_.clear();
  generation_ = 0;
  zone_.clear();
  barrier_open_ = false;
  server_loaded_ = false;
  waiting_.clear();
  return true;
}

void ServerSession::Stop() {
  if (!transport_) return;
  const TimePoint now = Clock::now();  // only stamps the Bye, which is never resent
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed) Post(*peer, Lane::kGameplay, Bye{}, now);
  }
  peers_.clear();
  transport_.reset();
  barrier_open_ = false;
  waiting_.clear();
}

void ServerSession::Pump(TimePoint now) {
  if (!transport_) return;
  std::vector<uint8_t> buffer;
  Endpoint from;
  for (int i = 0; i < kMaxReceivePerPump && transport_->Receive(buffer, from); ++i) {
    HandleDatagram(from, buffer, now);
  }
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed) {
      if (now - peer->conn->LastReceive() > kPeerTimeout) DropPeer(*peer, LeaveReason::kTimeout, now);
    } else if (peer->state == PeerState::kPending) {
      // No valid Hello within the timeout (one that keeps sending other things included).
      if (now - peer->created > kPeerTimeout) peer->state = PeerState::kGone;
    }
  }
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state != PeerState::kGone) peer->conn->Pump(now);
  }
  std::erase_if(peers_, [&](const auto& entry) {
    const Peer& p = *entry.second;
    return p.state == PeerState::kGone ||
           (p.state == PeerState::kClosing && now - p.closing_since >= kCloseLinger);
  });
}

bool ServerSession::PollEvent(ServerEvent& out) {
  if (events_.empty()) return false;
  out = std::move(events_.front());
  events_.pop_front();
  return true;
}

void ServerSession::HandleDatagram(const Endpoint& from, std::span<const uint8_t> bytes,
                                   TimePoint now) {
  auto datagram = netplay::DecodeDatagram(bytes);
  if (!datagram) return;
  const netplay::DatagramHeader& h = datagram->header;
  if (h.session_id != 0 && h.session_id != settings_.session_id) return;

  auto it = peers_.find(from);
  if (it == peers_.end()) {
    // Only a client's first datagrams (sent before it knows the session id) open a connection.
    if (h.session_id != 0 || h.lane != Lane::kGameplay || (h.flags & netplay::kFlagAckOnly)) return;
    const auto pending = std::count_if(peers_.begin(), peers_.end(), [](const auto& entry) {
      return entry.second->state == PeerState::kPending;
    });
    if (size_t(pending) >= kMaxPending) return;
    auto peer = std::make_unique<Peer>();
    peer->endpoint = from;
    peer->created = now;
    peer->conn = std::make_unique<Connection>(
        [this, from](std::span<const uint8_t> d) {
          if (transport_) transport_->Send(from, d);
        },
        now);
    peer->conn->SetSessionId(settings_.session_id);
    peer->conn->SetLocalPeer(kServerPeerId);
    it = peers_.emplace(from, std::move(peer)).first;
  }

  Peer& peer = *it->second;
  if (peer.state == PeerState::kGone) return;
  peer.conn->OnDatagram(*datagram, now);
  LaneMessage m;
  while (peer.conn->Receive(m)) {
    auto messages = DecodeMessages(m.payload);
    if (!messages) continue;
    for (const Message& message : *messages) {
      // A refused or departed peer's connection only acks; what it still sends is dropped.
      if (peer.state != PeerState::kPending && peer.state != PeerState::kWelcomed) break;
      HandleMessage(peer, message, now);
    }
  }
}

void ServerSession::HandleMessage(Peer& peer, const Message& message, TimePoint now) {
  if (peer.state == PeerState::kPending) {
    if (const auto* hello = std::get_if<Hello>(&message)) HandleHello(peer, *hello, now);
    return;
  }
  if (const auto* ping = std::get_if<Ping>(&message)) {
    Post(peer, Lane::kFast, Pong{ping->client_time_us, Tick(now)}, now);
  } else if (const auto* request = std::get_if<LevelRequest>(&message)) {
    if (!peer.owner) {
      SendRefuse(peer, RefuseReason::kNotOwner, "only the owner changes the level", now, false);
      return;
    }
    const uint32_t generation = RequestLevel(request->zone, now);
    if (generation == 0) return;
    ServerEvent e;
    e.type = ServerEvent::Type::kLevelRequest;
    e.peer_id = peer.id;
    e.zone = request->zone;
    e.generation = generation;
    events_.push_back(std::move(e));
  } else if (const auto* loaded = std::get_if<LevelLoaded>(&message)) {
    if (barrier_open_ && loaded->generation == generation_ && waiting_.erase(peer.id)) {
      CheckBarrier(now);
    }
  } else if (std::holds_alternative<Bye>(message)) {
    DropPeer(peer, LeaveReason::kBye, now);
  }
}

void ServerSession::HandleHello(Peer& peer, const Hello& hello, TimePoint now) {
  peer.hello = hello;
  if (hello.protocol_version != kProtocolVersion) {
    SendRefuse(peer, RefuseReason::kVersion,
               "the server speaks protocol version " + std::to_string(kProtocolVersion), now, true);
    return;
  }
  if (hello.build_digest != settings_.build_digest) {
    SendRefuse(peer, RefuseReason::kDigest, "the server runs another build of the game", now, true);
    return;
  }
  const bool claims_owner = !IsNone(hello.owner_token);
  if (claims_owner && hello.owner_token != settings_.owner_token) {
    SendRefuse(peer, RefuseReason::kNotOwner, "the owner token does not match", now, true);
    return;
  }

  uint16_t id = 0;
  if (claims_owner) {
    // The owner's game came back (a restart, a new address): the new connection wins.
    if (Peer* old = FindById(kOwnerPeerId)) DropPeer(*old, LeaveReason::kReplaced, now);
    id = kOwnerPeerId;
  } else {
    for (uint16_t candidate = 2; candidate <= settings_.max_clients; ++candidate) {
      if (!FindById(candidate)) {
        id = candidate;
        break;
      }
    }
    if (id == 0) {
      SendRefuse(peer, RefuseReason::kFull, "the session is full", now, true);
      return;
    }
  }

  peer.state = PeerState::kWelcomed;
  peer.id = id;
  peer.owner = claims_owner;
  Welcome w;
  w.peer_id = id;
  w.session_id = settings_.session_id;
  w.tick_hz = uint16_t(settings_.hz);
  w.server_tick = Tick(now);
  w.level_generation = generation_;
  w.level_zone = zone_;
  Post(peer, Lane::kGameplay, w, now);

  ServerEvent e;
  e.type = ServerEvent::Type::kPeerJoined;
  e.peer_id = id;
  e.name = hello.name;
  e.owner = claims_owner;
  e.figure_id = hello.figure_id;
  e.figure_variant = hello.figure_variant;
  e.endpoint = peer.endpoint;
  events_.push_back(std::move(e));
  BroadcastPlayerList(now);
}

void ServerSession::SendRefuse(Peer& peer, RefuseReason reason, std::string text, TimePoint now,
                               bool close) {
  Post(peer, Lane::kGameplay, Refuse{reason, text}, now);
  ServerEvent e;
  e.type = ServerEvent::Type::kRefusalSent;
  e.peer_id = close ? 0 : peer.id;
  e.name = peer.hello.name;
  e.refuse_reason = reason;
  e.text = std::move(text);
  e.endpoint = peer.endpoint;
  events_.push_back(std::move(e));
  if (close) {
    peer.state = PeerState::kClosing;
    peer.closing_since = now;
  }
}

void ServerSession::DropPeer(Peer& peer, LeaveReason reason, TimePoint now) {
  if (peer.state != PeerState::kWelcomed) return;
  const uint16_t id = peer.id;
  if (reason == LeaveReason::kTimeout) {
    peer.state = PeerState::kGone;
  } else {
    if (reason == LeaveReason::kReplaced) Post(peer, Lane::kGameplay, Bye{}, now);
    peer.state = PeerState::kClosing;
    peer.closing_since = now;
  }
  ServerEvent e;
  e.type = ServerEvent::Type::kPeerLeft;
  e.peer_id = id;
  e.name = peer.hello.name;
  e.leave_reason = reason;
  e.endpoint = peer.endpoint;
  events_.push_back(std::move(e));
  if (barrier_open_ && waiting_.erase(id)) CheckBarrier(now);
  BroadcastPlayerList(now);
}

uint32_t ServerSession::RequestLevel(std::string_view zone, TimePoint now) {
  if (!transport_ || zone.size() > kMaxZoneBytes || !IsValidUtf8(zone)) return 0;
  ++generation_;
  zone_ = std::string(zone);
  barrier_open_ = true;
  server_loaded_ = false;
  waiting_.clear();
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state != PeerState::kWelcomed) continue;
    waiting_.insert(peer->id);
    Post(*peer, Lane::kGameplay, LevelLoad{generation_, zone_}, now);
  }
  return generation_;
}

void ServerSession::MarkServerLoaded(uint32_t generation, TimePoint now) {
  if (!barrier_open_ || generation != generation_) return;
  server_loaded_ = true;
  CheckBarrier(now);
}

void ServerSession::CheckBarrier(TimePoint now) {
  if (!barrier_open_ || !server_loaded_ || !waiting_.empty()) return;
  barrier_open_ = false;
  const uint64_t tick = Tick(now);
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed) {
      Post(*peer, Lane::kGameplay, BarrierGo{generation_, tick}, now);
    }
  }
  ServerEvent e;
  e.type = ServerEvent::Type::kBarrierReleased;
  e.generation = generation_;
  e.tick = tick;
  events_.push_back(std::move(e));
}

void ServerSession::BroadcastPlayerList(TimePoint now) {
  PlayerList list;
  for (const PeerInfo& p : Peers()) list.players.push_back(PlayerEntry{p.peer_id, p.name});
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed) Post(*peer, Lane::kGameplay, list, now);
  }
}

void ServerSession::Post(Peer& peer, Lane lane, const Message& message, TimePoint now) {
  const std::vector<uint8_t> bytes = Encoded(message);
  if (!bytes.empty()) peer.conn->Send(lane, bytes, now);
}

ServerSession::Peer* ServerSession::FindById(uint16_t id) {
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed && peer->id == id) return peer.get();
  }
  return nullptr;
}

netplay::Endpoint ServerSession::LocalEndpoint() const {
  return transport_ ? transport_->LocalEndpoint() : Endpoint{};
}

std::string ServerSession::JoinCode() const {
  Endpoint e = LocalEndpoint();
  if (e.port == 0) return {};
  if (e.address == 0) e.address = netplay::PrimaryLocalIpv4().value_or(netplay::kLoopback);
  return EncodeJoinCode(e);
}

std::vector<PeerInfo> ServerSession::Peers() const {
  std::vector<PeerInfo> out;
  for (const auto& [endpoint, peer] : peers_) {
    if (peer->state != PeerState::kWelcomed) continue;
    PeerInfo info;
    info.peer_id = peer->id;
    info.name = peer->hello.name;
    info.owner = peer->owner;
    info.figure_id = peer->hello.figure_id;
    info.figure_variant = peer->hello.figure_variant;
    info.endpoint = endpoint;
    info.rtt = peer->conn->SmoothedRtt();
    info.last_receive = peer->conn->LastReceive();
    out.push_back(std::move(info));
  }
  std::sort(out.begin(), out.end(),
            [](const PeerInfo& a, const PeerInfo& b) { return a.peer_id < b.peer_id; });
  return out;
}

size_t ServerSession::PeerCount() const {
  return size_t(std::count_if(peers_.begin(), peers_.end(), [](const auto& entry) {
    return entry.second->state == PeerState::kWelcomed;
  }));
}

// ---------------------------------------------------------------------------------------------
// ClientSession

ClientSession::~ClientSession() {
  // Best effort: one Bye, so the server need not wait for the timeout.
  if (conn_ && (state_ == ClientState::kConnected || state_ == ClientState::kConnecting)) {
    Post(Lane::kGameplay, Bye{}, Clock::now());
  }
}

bool ClientSession::Connect(std::string_view server_address, uint16_t port, const Hello& hello,
                            TimePoint now) {
  const auto address = netplay::ParseIpv4(server_address);
  if (!address || *address == 0 || port == 0) {
    error_ = "not an IPv4 address and port: " + std::string(server_address) + ":" +
             std::to_string(port);
    return false;
  }
  auto socket = std::make_unique<netplay::UdpSocket>();
  // A loopback server gets a loopback client: nothing is bound where a firewall would ask.
  if (!socket->Open(netplay::IsLoopback(*address) ? "127.0.0.1" : "0.0.0.0", 0)) {
    error_ = socket->LastError();
    return false;
  }
  return Connect(std::move(socket), Endpoint{*address, port}, hello, now);
}

bool ClientSession::Connect(std::unique_ptr<netplay::Transport> transport,
                            const netplay::Endpoint& server, const Hello& hello, TimePoint now) {
  conn_.reset();
  transport_.reset();
  events_.clear();
  state_ = ClientState::kIdle;
  peer_id_ = netplay::kUnknownPeer;
  session_id_ = 0;
  generation_ = 0;
  zone_.clear();
  players_.clear();
  ticks_.Reset(60);
  error_.clear();
  if (!transport) {
    error_ = "no transport";
    return false;
  }
  Hello h = hello;
  h.name = TruncateUtf8(h.name, kMaxNameBytes);
  const std::vector<uint8_t> bytes = Encoded(h);
  if (bytes.empty()) {
    error_ = "the name is not UTF-8";
    return false;
  }
  transport_ = std::move(transport);
  server_ = server;
  conn_ = std::make_unique<Connection>(
      [this](std::span<const uint8_t> d) {
        if (transport_) transport_->Send(server_, d);
      },
      now);
  conn_->Send(Lane::kGameplay, bytes, now);
  state_ = ClientState::kConnecting;
  return true;
}

void ClientSession::Disconnect(TimePoint now) {
  if (state_ != ClientState::kConnected && state_ != ClientState::kConnecting) return;
  Post(Lane::kGameplay, Bye{}, now);
  state_ = ClientState::kDisconnecting;
  closing_since_ = now;
}

void ClientSession::Pump(TimePoint now) {
  if (!conn_) return;
  std::vector<uint8_t> buffer;
  Endpoint from;
  for (int i = 0; i < kMaxReceivePerPump && transport_->Receive(buffer, from); ++i) {
    if (from != server_) continue;
    auto datagram = netplay::DecodeDatagram(buffer);
    if (!datagram) continue;
    if (session_id_ != 0 && datagram->header.session_id != session_id_) continue;
    conn_->OnDatagram(*datagram, now);
    LaneMessage m;
    while (conn_->Receive(m)) {
      auto messages = DecodeMessages(m.payload);
      if (!messages) continue;
      for (const Message& message : *messages) HandleMessage(message, now);
    }
  }

  if (state_ == ClientState::kConnected && now >= next_ping_) {
    Post(Lane::kFast, Ping{ToMicros(now)}, now);
    next_ping_ = now + kPingInterval;
  }
  const bool live = state_ == ClientState::kConnecting || state_ == ClientState::kConnected;
  if (live && now - conn_->LastReceive() > kPeerTimeout) Finish(DisconnectReason::kTimeout);
  if (state_ == ClientState::kDisconnecting &&
      (conn_->UnackedTotal() == 0 || now - closing_since_ >= kCloseLinger)) {
    Finish(DisconnectReason::kLocal);
  }
  conn_->Pump(now);  // also the last acks of a session that just ended

  const bool done = state_ == ClientState::kDisconnected ||
                    (state_ == ClientState::kRefused && now - closing_since_ >= kCloseLinger);
  if (done) {
    conn_.reset();
    transport_.reset();
  }
}

bool ClientSession::PollEvent(ClientEvent& out) {
  if (events_.empty()) return false;
  out = std::move(events_.front());
  events_.pop_front();
  return true;
}

void ClientSession::HandleMessage(const Message& message, TimePoint now) {
  if (state_ != ClientState::kConnecting && state_ != ClientState::kConnected &&
      state_ != ClientState::kDisconnecting) {
    return;
  }
  if (std::holds_alternative<Bye>(message)) {
    Finish(DisconnectReason::kBye);
    return;
  }
  if (state_ == ClientState::kDisconnecting) return;

  if (const auto* w = std::get_if<Welcome>(&message)) {
    if (state_ != ClientState::kConnecting) return;
    session_id_ = w->session_id;
    peer_id_ = w->peer_id;
    conn_->SetSessionId(session_id_);
    conn_->SetLocalPeer(peer_id_);
    ticks_.Reset(w->tick_hz != 0 ? w->tick_hz : 60);
    generation_ = w->level_generation;
    zone_ = w->level_zone;
    state_ = ClientState::kConnected;
    next_ping_ = now;  // the first ping goes in this Pump
    ClientEvent e;
    e.type = ClientEvent::Type::kConnected;
    e.peer_id = peer_id_;
    e.generation = generation_;
    e.zone = zone_;
    e.tick = w->server_tick;
    events_.push_back(std::move(e));
    return;
  }
  if (const auto* r = std::get_if<Refuse>(&message)) {
    ClientEvent e;
    e.refuse_reason = r->reason;
    e.text = r->text;
    if (state_ == ClientState::kConnecting) {
      e.type = ClientEvent::Type::kRefused;
      state_ = ClientState::kRefused;
      closing_since_ = now;
    } else {
      e.type = ClientEvent::Type::kRequestRefused;
    }
    events_.push_back(std::move(e));
    return;
  }
  if (state_ != ClientState::kConnected) return;

  if (const auto* pong = std::get_if<Pong>(&message)) {
    ticks_.AddSample(FromMicros(pong->client_time_us), now, pong->server_tick);
  } else if (const auto* load = std::get_if<LevelLoad>(&message)) {
    generation_ = load->generation;
    zone_ = load->zone;
    ClientEvent e;
    e.type = ClientEvent::Type::kLevelLoad;
    e.generation = load->generation;
    e.zone = load->zone;
    events_.push_back(std::move(e));
  } else if (const auto* go = std::get_if<BarrierGo>(&message)) {
    ClientEvent e;
    e.type = ClientEvent::Type::kBarrierGo;
    e.generation = go->generation;
    e.tick = go->start_tick;
    events_.push_back(std::move(e));
  } else if (const auto* list = std::get_if<PlayerList>(&message)) {
    players_ = list->players;
    ClientEvent e;
    e.type = ClientEvent::Type::kPlayerList;
    e.players = list->players;
    events_.push_back(std::move(e));
  }
}

void ClientSession::Finish(DisconnectReason reason) {
  if (state_ == ClientState::kDisconnected) return;
  state_ = ClientState::kDisconnected;
  ClientEvent e;
  e.type = ClientEvent::Type::kDisconnected;
  e.disconnect_reason = reason;
  events_.push_back(std::move(e));
}

bool ClientSession::SendLevelRequest(std::string_view zone, TimePoint now) {
  if (!IsOwner() || zone.size() > kMaxZoneBytes || !IsValidUtf8(zone)) return false;
  return Post(Lane::kGameplay, LevelRequest{std::string(zone)}, now);
}

bool ClientSession::SendLevelLoaded(uint32_t generation, TimePoint now) {
  if (!Connected()) return false;
  return Post(Lane::kGameplay, LevelLoaded{generation}, now);
}

bool ClientSession::Post(Lane lane, const Message& message, TimePoint now) {
  if (!conn_) return false;
  const std::vector<uint8_t> bytes = Encoded(message);
  return !bytes.empty() && conn_->Send(lane, bytes, now) == netplay::SendResult::kSent;
}

std::chrono::microseconds ClientSession::Rtt() const {
  return conn_ ? conn_->SmoothedRtt() : microseconds(0);
}

netplay::Endpoint ClientSession::LocalEndpoint() const {
  return transport_ ? transport_->LocalEndpoint() : Endpoint{};
}

}  // namespace skylanders::online
