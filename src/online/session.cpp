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

uint32_t RandomSessionId() {
  std::random_device rd;
  uint32_t id = 0;
  while (id == 0) id = rd();
  return id;
}

// Stage 8: from the OS random source, never 0.
uint32_t RandomSecret() {
  uint32_t v = 0;
  while (v == 0) netplay::FillOsRandom({reinterpret_cast<uint8_t*>(&v), sizeof(v)});
  return v;
}
uint64_t RandomNonce() {
  uint64_t v = 0;
  while (v == 0) netplay::FillOsRandom({reinterpret_cast<uint8_t*>(&v), sizeof(v)});
  return v;
}
CookieKey RandomCookieKey() {
  CookieKey k{};
  netplay::FillOsRandom(k);
  return k;
}
bool IsZero(const CookieKey& k) {
  return std::all_of(k.begin(), k.end(), [](uint8_t b) { return b == 0; });
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
  if (settings_.join_secret == 0) settings_.join_secret = RandomSecret();
  if (IsZero(settings_.cookie_key)) settings_.cookie_key = RandomCookieKey();
  gate_.reset();
  code_spent_reported_ = false;
  if (settings_.gate) {
    gate_ = std::make_unique<HelloGate>(
        HelloGateSettings{settings_.join_secret, settings_.cookie_key, settings_.let_anyone_in,
                          settings_.max_clients},
        now);
  }
  clock_ = ServerTickClock(settings_.hz, now);
  transport_ = std::move(transport);
  peers_.clear();
  events_.clear();
  generation_ = 0;
  zone_.clear();
  barrier_open_ = false;
  server_loaded_ = false;
  waiting_.clear();
  requests_.Reset(0);
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
  gate_.reset();
  barrier_open_ = false;
  waiting_.clear();
}

void ServerSession::Pump(TimePoint now) {
  if (!transport_) return;
  std::vector<uint8_t> buffer;
  Endpoint from;
  // An empty datagram (one the transport could not deliver whole) counts here and is dropped by
  // HandleDatagram before it can open a connection.
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
  if (gate_) {
    // Stage 8: pending joiners the owner did not answer in time (their next Hello hears "timed
    // out"), and the cookie key's rotation.
    for (const PendingJoin& j : gate_->ExpirePending(now)) {
      ServerEvent e;
      e.type = ServerEvent::Type::kJoinGone;
      e.pending_id = j.id;
      e.name = j.name;
      e.endpoint = j.from;
      events_.push_back(std::move(e));
    }
    if (gate_->CookieRotationDue(now)) gate_->RotateCookieKey(RandomCookieKey(), now);
  }
  // The pose queue, held exits and End session's gone timeout, once per Pump (a server frame);
  // what it decides goes out in this Pump.
  requests_.Service(now);
  FlushRequests(now);
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
  auto datagram = netplay::DecodeDatagram(bytes);  // nullopt for an empty or short datagram
  if (!datagram) return;
  const netplay::DatagramHeader& h = datagram->header;
  if (h.session_id != 0 && h.session_id != settings_.session_id) return;

  if (h.flags & netplay::kFlagStateless) return;  // only a server sends those (stage 8)

  auto it = peers_.find(from);
  if (it == peers_.end()) {
    // Only a client's first datagrams (sent before it knows the session id) open a connection.
    if (h.session_id != 0 || h.lane != Lane::kGameplay || (h.flags & netplay::kFlagAckOnly)) return;
    // Every connection that is not welcomed counts, closing ones included: a Hello refused at once
    // (version, digest, token, full) turns kClosing in this same call and lingers kCloseLinger,
    // so counting only kPending would admit without bound. No kGone entry exists here (Pump erases
    // them after the receive loop), and peers_ never holds more than kMaxClients + kMaxUnadmitted
    // entries, so the scan in PeerCount stays short; a counter would have to follow every state
    // change instead.
    if (peers_.size() - PeerCount() >= kMaxUnadmitted) return;
    // Stage 8: with the gate on, only a Hello it admits opens a connection; it answered the rest.
    if (gate_ && !AdmitThroughGate(from, bytes.size(), datagram->payload, now)) return;
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
      HandleMessage(peer, message, m.lane, now);
    }
  }
}

void ServerSession::HandleMessage(Peer& peer, const Message& message, Lane lane, TimePoint now) {
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
    // 0 also while End session's drain is on: no generation changes during the drain.
    const uint32_t generation = RequestLevel(request->zone, now);
    if (generation == 0) return;
    ServerEvent e;
    e.type = ServerEvent::Type::kLevelRequest;
    e.peer_id = peer.id;
    e.zone = request->zone;
    e.generation = generation;
    events_.push_back(std::move(e));
  } else if (const auto* loaded = std::get_if<LevelLoaded>(&message)) {
    if (loaded->generation == generation_ && generation_ != 0) {
      ServerEvent e;
      e.type = ServerEvent::Type::kPeerLevelLoaded;
      e.peer_id = peer.id;
      e.generation = loaded->generation;
      events_.push_back(std::move(e));
    }
    if (barrier_open_ && loaded->generation == generation_ && waiting_.erase(peer.id)) {
      CheckBarrier(now);
    }
  } else if (std::holds_alternative<Bye>(message)) {
    DropPeer(peer, LeaveReason::kBye, now);
  } else if (const auto* pose = std::get_if<PlayerPose>(&message)) {
    // An older pose (the fast lane drops only datagrams older than the newest, not stale content)
    // or one of another generation is dropped here. Received is not accepted: pending keys see
    // it only after the host's AcceptPose (stage 4: S's rate check sits in between).
    if (!requests_.OnPoseReceived(peer.id, pose->generation, pose->pose_sequence)) return;
    ServerEvent e;
    e.type = ServerEvent::Type::kPlayerPose;
    e.peer_id = peer.id;
    e.pose = *pose;
    events_.push_back(std::move(e));
  } else if (const auto* action = std::get_if<PlayerAction>(&message)) {
    if (requests_.Closing()) return;  // End session: nothing is applied any more
    ServerEvent e;
    e.type = ServerEvent::Type::kPlayerAction;
    e.peer_id = peer.id;
    e.action = *action;
    events_.push_back(std::move(e));
  } else if (const auto* world_request = std::get_if<WorldRequest>(&message)) {
    ServerEvent e;
    e.type = ServerEvent::Type::kRequestReceived;
    e.peer_id = peer.id;
    e.request = *world_request;
    e.request.key.peer = peer.id;  // a client speaks only for itself
    const WorldRequest r = e.request;
    events_.push_back(std::move(e));
    requests_.OnRequest(r, now);
    FlushRequests(now);
  } else if (std::holds_alternative<SessionClosed>(message)) {
    requests_.OnClosed(peer.id);
    FlushRequests(now);
  } else if (const auto* frame = std::get_if<BackendFrame>(&message)) {
    ServerEvent e;
    e.type = ServerEvent::Type::kBackendFrame;
    e.peer_id = peer.id;
    e.lane = lane;
    e.frame = frame->bytes;
    events_.push_back(std::move(e));
  } else if (const auto* accept = std::get_if<OccurrenceAccept>(&message)) {  // stage 5
    ServerEvent e;
    e.type = ServerEvent::Type::kOccurrenceAccept;
    e.peer_id = peer.id;
    e.occurrence_accept = *accept;
    events_.push_back(std::move(e));
  } else if (const auto* press = std::get_if<AttackPress>(&message)) {  // stage 5
    if (requests_.Closing()) return;  // End session: nothing is applied any more
    ServerEvent e;
    e.type = ServerEvent::Type::kAttackPress;
    e.peer_id = peer.id;
    e.attack_press = *press;
    events_.push_back(std::move(e));
  } else if (const auto* scene = std::get_if<SceneBarrier>(&message)) {
    // A client only acknowledges; any other phase from it is dropped, and so is an ack that comes
    // after End session began (the scene barrier has nothing left to wait for).
    if (scene->phase != ScenePhase::kAck || requests_.Closing()) return;
    ServerEvent e;
    e.type = ServerEvent::Type::kSceneAck;
    e.peer_id = peer.id;
    e.scene = *scene;
    events_.push_back(std::move(e));
  } else if (const auto* flags = std::get_if<OwnerFlags>(&message)) {
    // Stage 1C: only the owner's flags count (the row: "the owner's own ... from the owner's
    // record"); after End session began nothing is applied any more.
    if (!peer.owner) {
      SendRefuse(peer, RefuseReason::kNotOwner, "only the owner sends its flags", now, false);
      return;
    }
    if (requests_.Closing()) return;
    ServerEvent e;
    e.type = ServerEvent::Type::kOwnerFlags;
    e.peer_id = peer.id;
    e.owner_flags = *flags;
    events_.push_back(std::move(e));
  }
}

bool ServerSession::CompatibleHello(const Hello& hello, const Endpoint& from, RefuseReason* reason,
                                    std::string* text) const {
  auto refuse = [&](RefuseReason r, const char* t) {
    *reason = r;
    *text = t;
    return false;
  };
  if (hello.protocol_version != kProtocolVersion) {
    *reason = RefuseReason::kVersion;
    *text = "the server speaks protocol version " + std::to_string(kProtocolVersion);
    return false;
  }
  if (hello.build_digest != settings_.build_digest) {
    return refuse(RefuseReason::kDigest, "the server runs another build of the game");
  }
  if (!IsNone(hello.owner_token) && hello.owner_token != settings_.owner_token) {
    return refuse(RefuseReason::kNotOwner, "the owner token does not match");
  }
  if ((hello.flags & kHelloSynthetic) != 0 && !settings_.allow_synthetic) {
    return refuse(RefuseReason::kOther, "synthetic peers are off");
  }
  if ((hello.flags & kHelloSynthetic) != 0 && !netplay::IsLoopback(from.address)) {
    return refuse(RefuseReason::kOther, "synthetic peers only on loopback");
  }
  return true;
}

bool ServerSession::AdmitThroughGate(const Endpoint& from, size_t size,
                                     std::span<const uint8_t> payload, TimePoint now) {
  // Only a Hello can open a connection; anything else from an unknown address is dropped unanswered.
  auto messages = DecodeMessages(payload);
  if (!messages || messages->empty()) return false;
  const auto* hello = std::get_if<Hello>(&messages->front());
  if (!hello) return false;
  HelloFacts f;
  f.datagram_bytes = size;
  f.from = from;
  f.client_nonce = hello->client_nonce;
  f.cookie = hello->cookie;
  f.secret = hello->join_secret;
  f.name = hello->name;
  f.figure_id = hello->figure_id;
  f.figure_variant = hello->figure_variant;
  RefuseReason reason = RefuseReason::kOther;
  std::string text;
  f.compatible = CompatibleHello(*hello, from, &reason, &text);
  f.owner = f.compatible && !IsNone(hello->owner_token);  // compatible: the token matched
  // HandleHello replaces a welcomed owner's connection with this one: no new place (Sol 8B F2).
  f.replaces_owner = f.owner && FindById(kOwnerPeerId) != nullptr;
  const HelloDecision d = gate_->Check(f, int(PeerCount()), now);
  switch (d.verdict) {
    case HelloVerdict::kDrop:
      return false;
    case HelloVerdict::kAdmit:
      return true;
    case HelloVerdict::kCookieReply:
      SendStateless(from, HelloCookie{d.cookie}, size);
      return false;
    case HelloVerdict::kWait: {
      SendStateless(from, JoinWait{d.pending_id}, size);
      if (d.new_pending) {
        ServerEvent e;
        e.type = ServerEvent::Type::kJoinPending;
        e.pending_id = d.pending_id;
        e.name = hello->name;
        e.figure_id = hello->figure_id;
        e.figure_variant = hello->figure_variant;
        e.endpoint = from;
        events_.push_back(std::move(e));
      }
      return false;
    }
    case HelloVerdict::kIncompatible:
      SendStatelessRefuse(from, *hello, reason, std::move(text), size);
      return false;
    case HelloVerdict::kWrongCode:
      SendStatelessRefuse(from, *hello, RefuseReason::kWrongCode, "wrong code", size);
      return false;
    case HelloVerdict::kCodeSpent:
      SendStatelessRefuse(from, *hello, RefuseReason::kCodeSpent,
                          "this code was tried too often; ask the host for a new one", size);
      if (!code_spent_reported_) {
        code_spent_reported_ = true;
        ServerEvent e;
        e.type = ServerEvent::Type::kJoinCodeSpent;
        events_.push_back(std::move(e));
      }
      return false;
    case HelloVerdict::kBusy:
      SendStatelessRefuse(from, *hello, RefuseReason::kBusy, "the host is busy, try again", size);
      return false;
    case HelloVerdict::kFull:
      SendStatelessRefuse(from, *hello, RefuseReason::kFull, "the session is full", size);
      return false;
    case HelloVerdict::kNotLetIn:
      SendStatelessRefuse(from, *hello, RefuseReason::kNotLetIn,
                          d.timed_out ? "not let in: timed out" : "not let in", size);
      return false;
  }
  return false;
}

void ServerSession::SendStateless(const Endpoint& to, const Message& message, size_t limit) {
  if (!transport_) return;
  const std::vector<uint8_t> payload = Encoded(message);
  if (payload.empty()) return;
  netplay::DatagramHeader h;
  h.lane = Lane::kGameplay;
  h.flags = netplay::kFlagStateless;
  h.session_id = 0;
  h.sender = kServerPeerId;
  h.sequence = 1;  // a data datagram's sequence is never 0; the client never feeds it to a lane
  std::vector<uint8_t> out;
  // Never larger than what came in (design 3.4 step 1): no amplification from a spoofed address.
  if (!netplay::EncodeDatagram(h, payload, out) || out.size() > limit) return;
  transport_->Send(to, out);
}

void ServerSession::SendStatelessRefuse(const Endpoint& to, const Hello& hello, RefuseReason reason,
                                        std::string text, size_t limit) {
  // The text is cut so the datagram stays within `limit` (header, message header, reason, length).
  constexpr size_t kOverhead = netplay::kDatagramHeaderSize + kMessageHeaderSize + 2;
  const size_t room = limit > kOverhead ? limit - kOverhead : 0;
  text = TruncateUtf8(text, std::min(room, kMaxRefuseTextBytes));
  SendStateless(to, Refuse{reason, text}, limit);
  ServerEvent e;
  e.type = ServerEvent::Type::kRefusalSent;
  e.name = hello.name;
  e.refuse_reason = reason;
  e.text = std::move(text);
  e.endpoint = to;
  events_.push_back(std::move(e));
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
  if ((hello.flags & kHelloSynthetic) != 0 && !settings_.allow_synthetic) {  // stage 4 item 2
    SendRefuse(peer, RefuseReason::kOther, "synthetic peers are off", now, true);
    return;
  }
  // A synthetic peer is a dev tool on this machine (tools/headless_peer): one from another address
  // is refused even with allow_synthetic on (Opus 4 B6).
  if ((hello.flags & kHelloSynthetic) != 0 && !netplay::IsLoopback(peer.endpoint.address)) {
    SendRefuse(peer, RefuseReason::kOther, "synthetic peers only on loopback", now, true);
    return;
  }
  if (requests_.Closing()) {
    // End session's drain: a client welcomed now would never get SessionClosing nor be awaited.
    SendRefuse(peer, RefuseReason::kOther, "the session is ending", now, true);
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
  w.backend_mode = settings_.backend_mode;
  w.backend_table_digest = settings_.backend_table_digest;
  Post(peer, Lane::kGameplay, w, now);

  ServerEvent e;
  e.type = ServerEvent::Type::kPeerJoined;
  e.peer_id = id;
  e.name = hello.name;
  e.owner = claims_owner;
  e.figure_id = hello.figure_id;
  e.figure_variant = hello.figure_variant;
  e.endpoint = peer.endpoint;
  e.synthetic = (hello.flags & kHelloSynthetic) != 0;
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
  // Its keys go: a client that comes back under the same id starts its sequences again.
  requests_.ForgetPeer(id);
  if (reason == LeaveReason::kTimeout) {
    peer.state = PeerState::kGone;
  } else {
    // Replaced: told Bye. Closed: its finals went out just before this, on the same lane; now its
    // Bye. The connection lingers kCloseLinger to ack what is still in flight.
    if (reason == LeaveReason::kReplaced || reason == LeaveReason::kClosed ||
        reason == LeaveReason::kRemoved) {
      Post(peer, Lane::kGameplay, Bye{}, now);
    }
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
  if (!transport_ || requests_.Closing() || zone.size() > kMaxZoneBytes || !IsValidUtf8(zone)) {
    return 0;
  }
  ++generation_;
  zone_ = std::string(zone);
  barrier_open_ = true;
  server_loaded_ = false;
  waiting_.clear();
  // The finals owed for the older generations' keys go first, on the same ordered lane as the
  // LevelLoad below, so a client has them before the new generation starts (invariant 3).
  requests_.OnGenerationChange(generation_);
  FlushRequests(now);
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

bool ServerSession::Post(Peer& peer, Lane lane, const Message& message, TimePoint now) {
  const std::vector<uint8_t> bytes = Encoded(message);
  return !bytes.empty() && peer.conn->Send(lane, bytes, now) == netplay::SendResult::kSent;
}

namespace {
// One backend frame as a BackendFrame message, sent on `lane` of `conn`.
BackendSend SendFrame(netplay::Connection& conn, Lane lane, std::span<const uint8_t> frame,
                      TimePoint now) {
  if (frame.empty() || frame.size() > kMaxBackendFrameBytes) return BackendSend::kTooBig;
  BackendFrame m;
  m.bytes.assign(frame.begin(), frame.end());
  const std::vector<uint8_t> bytes = Encoded(m);
  if (bytes.empty()) return BackendSend::kTooBig;
  switch (conn.Send(lane, bytes, now)) {
    case netplay::SendResult::kSent: return BackendSend::kSent;
    case netplay::SendResult::kWindowFull: return BackendSend::kFull;
    default: return BackendSend::kTooBig;
  }
}
}  // namespace

BackendSend ServerSession::SendBackend(uint16_t peer_id, Lane lane, std::span<const uint8_t> frame,
                                       TimePoint now) {
  Peer* p = FindById(peer_id);
  if (!p || p->state != PeerState::kWelcomed) return BackendSend::kGone;
  return SendFrame(*p->conn, lane, frame, now);
}

size_t ServerSession::BackendUnacked(uint16_t peer_id, Lane lane) const {
  for (const auto& [endpoint, peer] : peers_) {
    if (peer->id == peer_id && peer->state == PeerState::kWelcomed) return peer->conn->Unacked(lane);
  }
  return 0;
}

std::chrono::microseconds ServerSession::BackendRtt(uint16_t peer_id) const {
  for (const auto& [endpoint, peer] : peers_) {
    if (peer->id == peer_id && peer->state == PeerState::kWelcomed) return peer->conn->SmoothedRtt();
  }
  return std::chrono::microseconds(0);
}

bool ServerSession::RemoveClient(uint16_t peer_id, TimePoint now) {
  Peer* p = FindById(peer_id);
  if (!p || p->state != PeerState::kWelcomed) return false;
  DropPeer(*p, LeaveReason::kRemoved, now);
  return true;
}

bool ServerSession::PeerConnected(uint16_t peer_id) const {
  for (const auto& [endpoint, peer] : peers_) {
    if (peer->id == peer_id && peer->state == PeerState::kWelcomed) return true;
  }
  return false;
}

void ServerSession::FlushRequests(TimePoint now) {
  for (RequestOutput& o : requests_.TakeOutputs()) {
    ServerEvent e;
    e.peer_id = o.peer;
    switch (o.type) {
      case RequestOutput::Type::kDecision: {
        Peer* p = FindById(o.peer);
        e.type = ServerEvent::Type::kDecisionSent;
        e.decision = o.decision;
        e.sent = p && Post(*p, Lane::kGameplay, o.decision, now);
        events_.push_back(std::move(e));
        break;
      }
      case RequestOutput::Type::kEvaluate:
        e.type = ServerEvent::Type::kEvaluateRequest;
        e.request = o.request;
        e.generation = o.request.key.generation;
        events_.push_back(std::move(e));
        break;
      case RequestOutput::Type::kPeerClosed:
        e.type = ServerEvent::Type::kPeerClosed;
        e.gone = o.gone;
        events_.push_back(std::move(e));
        // Its finals went out just before this, on the same lane: it leaves like any other client
        // (its Bye, kPeerLeft, the barrier no longer waits for it, the player list without it).
        if (Peer* p = FindById(o.peer)) DropPeer(*p, LeaveReason::kClosed, now);
        break;
    }
  }
}

bool ServerSession::SubmitEvaluation(const RequestKey& key, const EvaluationResult& result,
                                     TimePoint now) {
  if (!transport_) return false;
  const bool taken = requests_.SubmitEvaluation(key, result, now);
  FlushRequests(now);
  return taken;
}

bool ServerSession::AcceptPose(uint16_t peer, const PlayerPose& pose) {
  if (!transport_) return false;
  return requests_.OnPoseAccepted(peer, pose.generation, pose.pose_sequence);
}

bool ServerSession::SendAppliedStep(const AppliedStep& step, TimePoint now) {
  const std::vector<uint8_t> bytes = Encoded(step);
  if (!transport_ || bytes.empty()) return false;
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed) peer->conn->Send(Lane::kGameplay, bytes, now);
  }
  return true;
}

bool ServerSession::SendLevelEnd(uint16_t peer, const LevelEnd& end, TimePoint now) {
  Peer* p = FindById(peer);
  return p && Post(*p, Lane::kGameplay, end, now);
}

bool ServerSession::SendReaction(uint16_t peer, const Reaction& reaction, TimePoint now) {
  Peer* p = FindById(peer);
  return p && Post(*p, Lane::kGameplay, reaction, now);
}

// Stage 4: to every welcomed client but the one the action or pose came from.
bool ServerSession::SendPlayerActionShown(const PlayerActionShown& shown, TimePoint now) {
  const std::vector<uint8_t> bytes = Encoded(shown);
  if (!transport_ || bytes.empty()) return false;
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed && peer->id != shown.peer) peer->conn->Send(Lane::kGameplay, bytes, now);
  }
  return true;
}

bool ServerSession::SendPlayerPoseShown(const PlayerPoseShown& shown, TimePoint now) {
  const std::vector<uint8_t> bytes = Encoded(shown);
  if (!transport_ || bytes.empty()) return false;
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed && peer->id != shown.peer) peer->conn->Send(Lane::kFast, bytes, now);
  }
  return true;
}

bool ServerSession::SendEnemyPoses(uint16_t peer_id, const EnemyPoses& poses, TimePoint now) {
  const std::vector<uint8_t> bytes = Encoded(poses);
  if (!transport_ || bytes.empty()) return false;
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed && peer->id == peer_id) {
      peer->conn->Send(Lane::kFast, bytes, now);
      return true;
    }
  }
  return false;
}

bool ServerSession::SendSceneBarrier(const SceneBarrier& scene, TimePoint now) {
  if (scene.phase == ScenePhase::kAck) return false;
  const std::vector<uint8_t> bytes = Encoded(scene);
  if (!transport_ || bytes.empty()) return false;
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state == PeerState::kWelcomed) peer->conn->Send(Lane::kGameplay, bytes, now);
  }
  return true;
}

void ServerSession::EndSession(TimePoint now) {
  if (!transport_ || requests_.Closing()) return;
  std::vector<uint16_t> ids;
  for (auto& [endpoint, peer] : peers_) {
    if (peer->state != PeerState::kWelcomed) continue;
    ids.push_back(peer->id);
    Post(*peer, Lane::kGameplay, SessionClosing{}, now);
  }
  requests_.BeginClose(ids, now);
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

std::string ServerSession::JoinCode() const { return JoinCodeFor(0, 0); }

std::string ServerSession::JoinCodeFor(uint32_t address, uint16_t port) const {
  Endpoint e = LocalEndpoint();
  if (e.port == 0) return {};
  if (e.address == 0) e.address = netplay::PrimaryLocalIpv4().value_or(netplay::kLoopback);
  if (address != 0) e.address = address;
  if (port != 0) e.port = port;
  // Join code v2 (stage 8 item 4, D10): 1A's code is retired; its decoder answers "older version".
  return EncodeJoinCodeV2(JoinTarget{e, settings_.join_secret});
}

void ServerSession::NewJoinSecret(uint32_t secret) {
  settings_.join_secret = secret != 0 ? secret : RandomSecret();
  if (gate_) gate_->NewSecret(settings_.join_secret);
  code_spent_reported_ = false;
}

bool ServerSession::LetIn(uint32_t pending_id, TimePoint now) {
  if (!gate_) return false;
  return gate_->LetIn(pending_id, int(PeerCount()), now, nullptr) == HelloVerdict::kAdmit;
}

bool ServerSession::RefuseJoin(uint32_t pending_id, TimePoint now) {
  return gate_ && gate_->Refuse(pending_id, now);
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
    info.stats = peer->conn->Stats();
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
  closing_ = false;
  error_.clear();
  if (!transport) {
    error_ = "no transport";
    return false;
  }
  Hello h = hello;
  h.name = TruncateUtf8(h.name, kMaxNameBytes);
  // Stage 8: the nonce the server's cookie is bound to, fresh per Connect; no cookie yet.
  if (h.client_nonce == 0) h.client_nonce = RandomNonce();
  h.cookie = 0;
  if (Encoded(h).empty()) {
    error_ = "the name is not UTF-8";
    return false;
  }
  hello_ = h;
  cookie_rounds_ = 0;
  join_waiting_ = false;
  transport_ = std::move(transport);
  server_ = server;
  SendHello(now);
  state_ = ClientState::kConnecting;
  return true;
}

void ClientSession::SendHello(TimePoint now) {
  conn_ = std::make_unique<Connection>(
      [this](std::span<const uint8_t> d) {
        if (transport_) transport_->Send(server_, d);
      },
      now);
  conn_->Send(Lane::kGameplay, Encoded(hello_), now);
}

void ClientSession::HandleStateless(std::span<const uint8_t> payload, TimePoint now) {
  // Only a join under way takes the gate's answers; afterwards they are stale.
  if (state_ != ClientState::kConnecting || !conn_) return;
  auto messages = DecodeMessages(payload);
  if (!messages) return;
  for (const Message& m : *messages) {
    if (const auto* c = std::get_if<HelloCookie>(&m)) {
      // The Hello again with the cookie, on a fresh connection (sequence 1 again: the server holds
      // nothing for this join yet). A server that keeps sending cookies is followed only so often.
      if (++cookie_rounds_ > kMaxCookieRounds) continue;
      hello_.cookie = c->cookie;
      SendHello(now);
    } else if (const auto* w = std::get_if<JoinWait>(&m)) {
      if (!join_waiting_) {
        join_waiting_ = true;
        ClientEvent e;
        e.type = ClientEvent::Type::kJoinWaiting;
        e.pending_id = w->pending_id;
        events_.push_back(std::move(e));
      }
    } else if (std::holds_alternative<Refuse>(m)) {
      HandleMessage(m, Lane::kGameplay, now);
      // Nothing to linger for: the server holds no connection for this join.
      if (state_ == ClientState::kRefused) closing_since_ = now - kCloseLinger;
      return;
    }
  }
  if (conn_) conn_->SetLastReceive(now);  // the server answers: the join is alive
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
    if (!datagram) continue;  // an empty datagram (too long, an error report) ends here
    if (datagram->header.flags & netplay::kFlagStateless) {  // the gate's answer (stage 8)
      HandleStateless(datagram->payload, now);
      if (!conn_) break;
      continue;
    }
    if (session_id_ != 0 && datagram->header.session_id != session_id_) continue;
    conn_->OnDatagram(*datagram, now);
    LaneMessage m;
    while (conn_->Receive(m)) {
      auto messages = DecodeMessages(m.payload);
      if (!messages) continue;
      for (const Message& message : *messages) HandleMessage(message, m.lane, now);
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

void ClientSession::HandleMessage(const Message& message, Lane lane, TimePoint now) {
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
    backend_mode_ = w->backend_mode;
    backend_table_digest_ = w->backend_table_digest;
    state_ = ClientState::kConnected;
    next_ping_ = now;  // the first ping goes in this Pump
    ClientEvent e;
    e.type = ClientEvent::Type::kConnected;
    e.peer_id = peer_id_;
    e.generation = generation_;
    e.zone = zone_;
    e.tick = w->server_tick;
    e.backend_mode = backend_mode_;
    e.backend_table_digest = backend_table_digest_;
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
  } else if (const auto* decision = std::get_if<WorldDecision>(&message)) {
    ClientEvent e;
    e.type = ClientEvent::Type::kWorldDecision;
    e.decision = *decision;
    events_.push_back(std::move(e));
  } else if (const auto* applied = std::get_if<AppliedStep>(&message)) {
    ClientEvent e;
    e.type = ClientEvent::Type::kAppliedStep;
    e.applied = *applied;
    events_.push_back(std::move(e));
  } else if (const auto* end = std::get_if<LevelEnd>(&message)) {
    ClientEvent e;
    e.type = ClientEvent::Type::kLevelEnd;
    e.level_end = *end;
    events_.push_back(std::move(e));
  } else if (const auto* reaction = std::get_if<Reaction>(&message)) {
    ClientEvent e;
    e.type = ClientEvent::Type::kReaction;
    e.reaction = *reaction;
    events_.push_back(std::move(e));
  } else if (const auto* shown = std::get_if<PlayerActionShown>(&message)) {  // stage 4
    ClientEvent e;
    e.type = ClientEvent::Type::kPlayerActionShown;
    e.action_shown = *shown;
    events_.push_back(std::move(e));
  } else if (const auto* pose_shown = std::get_if<PlayerPoseShown>(&message)) {  // stage 4
    ClientEvent e;
    e.type = ClientEvent::Type::kPlayerPoseShown;
    e.pose_shown = *pose_shown;
    events_.push_back(std::move(e));
  } else if (const auto* enemy_poses = std::get_if<EnemyPoses>(&message)) {  // stage 5
    ClientEvent e;
    e.type = ClientEvent::Type::kEnemyPoses;
    e.enemy_poses = *enemy_poses;
    events_.push_back(std::move(e));
  } else if (const auto* frame = std::get_if<BackendFrame>(&message)) {
    ClientEvent e;
    e.type = ClientEvent::Type::kBackendFrame;
    e.lane = lane;
    e.frame = frame->bytes;
    events_.push_back(std::move(e));
  } else if (const auto* scene = std::get_if<SceneBarrier>(&message)) {
    if (scene->phase == ScenePhase::kAck) return;  // the clients' phase: not the server's to send
    ClientEvent e;
    e.type = ClientEvent::Type::kSceneBarrier;
    e.scene = *scene;
    events_.push_back(std::move(e));
  } else if (std::holds_alternative<SessionClosing>(message)) {
    if (closing_) return;
    // From here no request leaves this client (the Send calls refuse), so SessionClosed, sent now
    // on the same ordered lane, is the watermark of everything it sent.
    closing_ = true;
    Post(Lane::kGameplay, SessionClosed{}, now);
    ClientEvent e;
    e.type = ClientEvent::Type::kSessionClosing;
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

bool ClientSession::SendPlayerPose(const PlayerPose& pose, TimePoint now) {
  if (!Connected() || closing_) return false;
  return Post(Lane::kFast, pose, now);
}

bool ClientSession::SendPlayerAction(const PlayerAction& action, TimePoint now) {
  if (!Connected() || closing_) return false;
  return Post(Lane::kGameplay, action, now);
}

bool ClientSession::SendWorldRequest(const WorldRequest& request, TimePoint now) {
  if (!Connected() || closing_) return false;
  WorldRequest r = request;
  r.key.peer = peer_id_;
  return Post(Lane::kGameplay, r, now);
}

bool ClientSession::SendOccurrenceAccept(const OccurrenceAccept& accept, TimePoint now) {
  if (!Connected() || closing_) return false;
  return Post(Lane::kGameplay, accept, now);
}

bool ClientSession::SendAttackPress(const AttackPress& press, TimePoint now) {
  if (!Connected() || closing_) return false;
  return Post(Lane::kGameplay, press, now);
}

bool ClientSession::SendSceneAck(const SceneBarrier& scene, TimePoint now) {
  if (!Connected() || closing_) return false;
  SceneBarrier ack = scene;
  ack.phase = ScenePhase::kAck;
  ack.end = SceneEndReason::kNone;
  ack.skips = 0;
  return Post(Lane::kGameplay, ack, now);
}

bool ClientSession::SendOwnerFlags(const OwnerFlags& flags, TimePoint now) {
  if (!Connected() || closing_) return false;
  return Post(Lane::kGameplay, flags, now);
}

bool ClientSession::Post(Lane lane, const Message& message, TimePoint now) {
  if (!conn_) return false;
  const std::vector<uint8_t> bytes = Encoded(message);
  return !bytes.empty() && conn_->Send(lane, bytes, now) == netplay::SendResult::kSent;
}

BackendSend ClientSession::SendBackend(Lane lane, std::span<const uint8_t> frame, TimePoint now) {
  if (!conn_ || state_ != ClientState::kConnected) return BackendSend::kGone;
  return SendFrame(*conn_, lane, frame, now);
}

std::chrono::microseconds ClientSession::Rtt() const {
  return conn_ ? conn_->SmoothedRtt() : microseconds(0);
}

netplay::Endpoint ClientSession::LocalEndpoint() const {
  return transport_ ? transport_->LocalEndpoint() : Endpoint{};
}

}  // namespace skylanders::online
