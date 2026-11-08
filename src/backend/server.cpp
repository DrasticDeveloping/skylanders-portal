#include "backend/server.h"

#include "backend/owned.h"

#include <algorithm>
#include <deque>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <variant>

namespace skylanders::backend {

namespace {

bool IsRepair(BaselineReason why) {
  return why != BaselineReason::kJoin && why != BaselineReason::kLevelReady;
}

}  // namespace

struct Server::Impl {
  struct Running {
    uint32_t id = 0;
    uint64_t at = 0;
    ScopeMask scopes = 0;
    BaselineReason why = BaselineReason::kJoin;
    std::vector<RecordKey> keys;
    size_t next = 0;
    uint32_t index = 0;
    uint64_t digest = kBaselineDigestSeed;
    bool retired_obligations = false;
  };
  // A baseline asked for while another runs (or before the next pump): its scopes, and whether it
  // is a repair (which covers every scope the peer holds when it begins).
  struct Due {
    BaselineReason why = BaselineReason::kJoin;
    ScopeMask scopes = 0;
    bool repair = false;
  };

  struct Peer {
    Peer(uint16_t id_in, PeerRole role_in, PeerLink& link_in, uint32_t epoch, const Limits& limits,
         TimePoint now)
        : id(id_in),
          role(role_in),
          link(&link_in),
          peer_epoch(epoch),
          queue(limits),
          pacer(limits, now),
          ledger(limits) {}

    uint16_t id;
    PeerRole role;
    PeerLink* link;
    uint32_t peer_epoch;
    uint64_t cursor = 0;
    uint64_t last_through = 0;
    Subscription sub;
    PeerQueue queue;  // the batcher: one FIFO per lane (lanes.h)
    Pacer pacer;      // baseline bytes
    std::optional<Running> baseline;
    std::optional<Due> due;
    TimePoint last_proof{};  // a baseline ended or a manifest matched
    TimePoint last_manifest{};
    uint64_t prefix = 0, manifest_ok = 0;
    // An Ack has shown a matched manifest in this peer epoch: its manifest_ok rose. Until then
    // manifest_ok is the initial 0, which proves nothing, also when Head() is 0 (F3).
    bool manifest_acked = false;
    // ObligationDone ids read while the state lane was backlogged, answered with ObligationAck
    // once it has room (F1). At most limits.obligation_credit: the client holds at most C entries
    // and repeats a reported one until acked, so one dropped past the bound comes again.
    std::set<uint64_t> acks_due;
    // Gameplay messages to the peer made while its gameplay lane is blocked (Sol code review r2,
    // R2): they wait here, in the order made, and go when the lane opens. One Decision per key
    // (and lease): a newer one replaces the waiting one and moves to the end, a cached repeat of a
    // key that already waits adds nothing. These and the gameplay messages queued in the FIFO are
    // what PlayCap() bounds (Sol code review r3, N1).
    struct Play {
      bool lease_end = false;
      Decision decision;  // !lease_end
      uint32_t lease = 0;  // lease_end
      LeaseEndReason why = LeaseEndReason::kExpired;
    };
    std::deque<Play> play_waiting;
    // The peer's requests and lease reports read while PlayHeld() is at PlayCap(), in the order
    // read, handled first once it is below (N1). At most HeldCap(); one more ends the peer's
    // session (Overrun).
    std::deque<Message> inbox;
    bool play_full = false;
    uint64_t cap_onsets = 0;  // onsets of PlayCap()
    uint64_t reports_coalesced = 0;  // resent reports dropped behind a waiting copy (S2-L3)
    // S ended the peer's session past both bounds (kPeerOverrun): what it sends is read and
    // dropped, nothing is sent to it, until the host removes it (N1).
    bool overrun = false;
    ObligationLedger ledger;
    bool paused = false;
    uint64_t credit_pauses = 0;
    uint64_t baselines = 0, manifests = 0, mismatches = 0;
  };

  Impl(std::span<const KindSpec> table_in, AuthoritySet& authorities_in, const Limits& limits_in)
      : table(table_in),
        limits(limits_in),
        digest(TableDigest(table_in)),
        state(table_in, limits_in),
        leases(limits_in),
        requests(table_in, authorities_in, state, leases, limits_in),
        owned(table_in) {}

  std::span<const KindSpec> table;
  Limits limits;
  uint64_t digest;
  WorldState state;
  LeaseTable leases;
  RequestTable requests;
  // Stage 4: the admission of client-owned rows (owned.h). Holds nothing until a client sends an
  // OwnedDelta, so a table without owned kinds never touches it.
  OwnedAdmission owned;
  std::map<uint16_t, std::unique_ptr<Peer>> peers;
  std::map<uint16_t, uint32_t> epochs;  // the last peer epoch given to each peer id
  // The last baseline id given to each peer id: a rejoin's baseline never reuses an id of the
  // previous connection, so nothing of a partial one can be taken for it (BL3).
  std::map<uint16_t, uint32_t> baseline_ids;
  std::deque<ServerEvent> events;
  TimePoint now{};  // the time of the call in progress (D3: the owner's clock)
  uint64_t end_writes_refused = 0;  // RequestTable::LeaseEndWritesRefused, as last reported

  Peer* Find(uint16_t id) {
    auto it = peers.find(id);
    return it == peers.end() ? nullptr : it->second.get();
  }
  const Peer* Find(uint16_t id) const {
    auto it = peers.find(id);
    return it == peers.end() ? nullptr : it->second.get();
  }

  ServerEvent& Event(ServerEvent::Type type, uint16_t peer) {
    ServerEvent e;
    e.type = type;
    e.peer = peer;
    events.push_back(std::move(e));
    return events.back();
  }

  // The state lane has no room: its head is held (kFull) or the link's unacked frames and the
  // queued ones reach limits.max_unacked_state_frames. Every state-lane producer but StateHello
  // waits while it holds (F1): deltas coalesce in the log, a due baseline waits in `due`, a
  // running one between chunks or before its end, obligations and the mark in the ledger,
  // ObligationAcks in `acks_due`, and no manifest is made (the next one carries the newest head).
  bool Backlogged(const Peer& p) const { return p.queue.Room(*p.link) == 0; }

  void Push(Peer& p, const Message& m, bool urgent = false) { p.queue.Push(m, now, urgent); }

  // ------------------------------------------------------------------------------------------
  // The gameplay lane to a peer (R2): bounded like the state lane, by the producer.

  // The gameplay messages S holds for a peer above the link, waiting (play_waiting) or queued in
  // the FIFO (decisions made in one pump before a flush met kFull are there): what PlayCap()
  // bounds.
  size_t PlayHeld(const Peer& p) const {
    return p.play_waiting.size() + p.queue.Messages(netplay::Lane::kGameplay);
  }

  // The gameplay messages S holds for a peer (PlayHeld) before it stops handling the peer's
  // requests and lease reports (N1). A peer that uses Client stays below it. Client takes at most
  // max_keys_per_peer (K) open keys (client.h, Send), and every message S has not delivered is for
  // a key open there or for one of the peer's leases: a key closes only on its final or on the
  // completion of a delivered `applied`, Resend waits for the last send's answer, a key of an ended
  // generation stays open until its final, and Report takes only a lease the client holds, one
  // report of it unanswered at a time. S says at most four things about a key before the client
  // hears them (request.cpp: `pending` once, `granted`, the final, the lease's LeaseEnd; a retry
  // key `pending`, its refusal and a final), and a LeaseEnd can follow the final that closed its
  // key, at most one per lease (max_leases_per_peer, L). So such a peer has at most 4 K + L
  // messages held; the cap is 5 K + L, a margin of K. A report the client resends while unanswered
  // (S2-L3) is answered from its key's cache, and a cached repeat of a key that already waits adds
  // nothing (SendPlay), so the resends add at most one message per lease, within the margin. Messages from S's own side (Service, an
  // evaluation, a generation change, End session) are among those four per key, and the message
  // handled just below the cap may add its children's decisions: S's holdings stay bounded. The
  // design names no number: the header's own (a council question).
  size_t PlayCap() const { return 5 * limits.max_keys_per_peer + limits.max_leases_per_peer; }

  // The requests and lease reports S holds unhandled for a peer at PlayCap() (Peer::inbox): what a
  // Client can have unanswered, one send per open key and one report per lease (client.h). A peer
  // that uses Client never reaches PlayCap(), so never this either; one past it ends the peer's
  // session (Overrun). The header's own (a council question).
  size_t HeldCap() const { return limits.max_keys_per_peer + limits.max_leases_per_peer; }

  void PushPlay(Peer& p, const Peer::Play& w) {
    if (w.lease_end) {
      Push(p, msg::LeaseEnd{w.lease, w.why});
    } else {
      Push(p, msg::Decision{w.decision});
    }
  }

  void SendPlay(Peer& p, Peer::Play w) {
    if (!p.queue.Blocked(netplay::Lane::kGameplay) && p.play_waiting.empty()) {
      PushPlay(p, w);
      return;
    }
    if (!w.lease_end) {
      auto same = std::find_if(p.play_waiting.begin(), p.play_waiting.end(),
                               [&](const Peer::Play& x) {
                                 return !x.lease_end && x.decision.key == w.decision.key &&
                                        x.decision.lease == w.decision.lease;
                               });
      if (same != p.play_waiting.end()) {
        if (w.decision.cached) return;  // it repeats the key's last decision: the waiting one
        p.play_waiting.erase(same);     // the newest decision of the key, at the end
      }
    }
    p.play_waiting.push_back(std::move(w));
  }

  // What waited goes once the gameplay lane is no longer blocked, before anything new.
  void SendWaitingPlay(Peer& p) {
    if (p.queue.Blocked(netplay::Lane::kGameplay)) return;
    for (const Peer::Play& w : p.play_waiting) PushPlay(p, w);
    p.play_waiting.clear();
  }

  // S holds PlayCap() gameplay messages for the peer: it handles none of its requests and lease
  // reports until it is below (N1). One limit event per onset.
  bool PlayFull(Peer& p) {
    if (PlayHeld(p) < PlayCap()) {
      p.play_full = false;
      return false;
    }
    if (!p.play_full) {
      ++p.cap_onsets;
      p.play_full = true;
      Event(ServerEvent::Type::kLimit, p.id).text =
          "decisions_waiting peer " + std::to_string(p.id);
    }
    return true;
  }

  // ------------------------------------------------------------------------------------------
  // The request table's outputs, as messages and events.

  // Writes refused at a limit, by the game's own Put or by a commit's ApplyAll: never silent (LM5,
  // LM5b). The event names the limit, the kind and, for a session row, the scope.
  void DrainLimits() {
    WorldState::LimitHit hit;
    while (state.PollLimit(hit)) {
      const KindSpec* spec = FindKind(table, hit.kind);
      const std::string kind = spec ? std::string(spec->name) : "#" + std::to_string(hit.kind);
      std::string text;
      if (hit.result == PutResult::kTooBig) {
        text = "max_value_bytes kind " + kind;
      } else if (hit.scope == Scope::kSession) {
        text = "max_rows_per_session kind " + kind + " scope session";
      } else {
        text = "max_rows_per_level kind " + kind + " scope level";
      }
      Event(ServerEvent::Type::kLimit, 0).text = std::move(text);
    }
  }

  void Drain() {
    DrainLimits();
    // A kind's lease end writes (S2-L2) that ApplyAll did not write: never silent.
    if (requests.LeaseEndWritesRefused() > end_writes_refused) {
      end_writes_refused = requests.LeaseEndWritesRefused();
      Event(ServerEvent::Type::kLimit, 0).text =
          "lease end writes refused " + std::to_string(end_writes_refused);
    }
    for (RequestOutput& o : requests.TakeOutputs()) {
      Peer* p = Find(o.peer);
      if (p && p->overrun) p = nullptr;  // its session ended: nothing more is sent to it (N1)
      switch (o.type) {
        case RequestOutput::Type::kDecision:
          if (p && o.decision.key.peer_epoch == p->peer_epoch) {
            Peer::Play w;
            w.decision = o.decision;
            SendPlay(*p, std::move(w));
          }
          Event(ServerEvent::Type::kDecision, o.peer).decision = o.decision;
          break;
        case RequestOutput::Type::kEvaluate:
          Event(ServerEvent::Type::kEvaluate, o.peer).request = std::move(o.request);
          break;
        case RequestOutput::Type::kLeaseEnd: {
          if (p) {
            Peer::Play w;
            w.lease_end = true;
            w.lease = o.lease;
            w.why = o.why;
            SendPlay(*p, std::move(w));
          }
          ServerEvent& e = Event(ServerEvent::Type::kLeaseEnded, o.peer);
          e.lease = o.lease;
          e.lease_end = o.why;
          break;
        }
      }
    }
  }

  void ObligationEnded(uint16_t peer, uint64_t id, ObligationEnd end) {
    ServerEvent& e = Event(ServerEvent::Type::kObligationEnded, peer);
    e.obligation = id;
    e.obligation_end = end;
  }

  // ------------------------------------------------------------------------------------------
  // Baselines (section 3.5 step 1, D5).

  // A reason that comes while a baseline runs: dropped if it came at or before the running one's
  // `at` (that baseline answers it), else kept for when it ends. `force` (a Mismatch from
  // Client::InboundLost, Sol code review r1 F1) is never dropped: that client discards the rest of
  // the running baseline, so only one begun after it answers.
  void AskBaseline(Peer& p, BaselineReason why, ScopeMask scopes, uint64_t lsn,
                   bool force = false) {
    const bool repair = IsRepair(why);
    if (p.baseline && lsn <= p.baseline->at && !force) {
      const ScopeMask wanted = repair ? p.sub.scopes : scopes;
      if ((wanted & ~p.baseline->scopes) == 0) return;
    }
    if (!p.due) {
      p.due = Due{why, scopes, repair};
    } else {
      p.due->scopes |= scopes;
      p.due->repair = p.due->repair || repair;
      if (repair) p.due->why = why;
    }
  }

  void BeginBaseline(Peer& p, TimePoint now) {
    const Due due = *p.due;
    p.due.reset();
    const ScopeMask scopes = ScopeMask(due.scopes | (due.repair ? p.sub.scopes : 0));
    if (scopes == 0) return;
    // A baseline that covers every scope the peer holds (the join's, a repair) also moves the
    // cursor to `at`; a level subscription baseline leaves it.
    const bool full = (p.sub.scopes & ~scopes) == 0;
    Running r;
    r.id = ++baseline_ids[p.id];
    r.at = state.Head();
    r.scopes = scopes;
    r.why = due.why;
    r.keys = state.BaselineKeys(scopes);
    for (const Scope s : {Scope::kSession, Scope::kLevel}) {
      if ((scopes & MaskOf(s)) == 0) continue;
      p.sub.scopes |= MaskOf(s);
      p.sub.since[size_t(s)] = r.at;
    }
    if (full) p.cursor = std::max(p.cursor, r.at);
    if (IsRepair(due.why)) {
      // D6: the obligations already sent and not ended retire now and are never resent; the
      // waiting ones stay and go out after BaselineEnd.
      for (const uint64_t id : p.ledger.RetireSent()) {
        ObligationEnded(p.id, id, ObligationEnd::kRetiredBaseline);
        r.retired_obligations = true;
      }
    }
    uint32_t bytes = 0;
    for (const RecordKey& k : r.keys) {
      if (const Record* row = state.Get(k)) bytes += uint32_t(row->value.size() + 48);
    }
    msg::BaselineBegin begin;
    begin.id = r.id;
    begin.at = r.at;
    begin.epoch = state.Epoch();
    begin.scopes = scopes;
    begin.rows = uint32_t(r.keys.size());
    begin.bytes = bytes;
    begin.reason = due.why;
    Push(p, begin);
    p.pacer.Reset(now);
    ++p.baselines;
    ServerEvent& e = Event(ServerEvent::Type::kBaselineBegun, p.id);
    e.baseline = r.id;
    e.why = due.why;
    p.baseline = std::move(r);
  }

  // Chunks within the pacer's budget; BaselineEnd, then Through at the peer's proven x (Opus F1).
  void RunBaseline(Peer& p, TimePoint now) {
    Running& r = *p.baseline;
    p.pacer.Refill(now);
    while (r.next < r.keys.size() && !Backlogged(p)) {
      msg::BaselineRows rows;
      rows.id = r.id;
      rows.index = r.index;
      std::vector<uint8_t> probe;
      size_t taken = 0;
      while (r.next + taken < r.keys.size()) {
        const Record* row = state.Get(r.keys[r.next + taken]);
        if (!row) {  // gone since the baseline began (cannot happen within an epoch): passed
          ++taken;
          continue;
        }
        rows.records.push_back(*row);
        probe.clear();
        if (!AppendMessage(rows, probe, kMaxFrameBytes)) {
          rows.records.pop_back();
          break;
        }
        ++taken;
      }
      if (taken == 0) {  // a row too big for a frame of its own: cannot be sent, passed
        ++r.next;
        continue;
      }
      probe.clear();
      if (!rows.records.empty()) {
        AppendMessage(rows, probe, kMaxFrameBytes);
        if (!p.pacer.Take(probe.size())) break;  // the budget: the rest goes at a later pump
      }
      r.next += taken;
      if (rows.records.empty()) continue;
      for (const Record& rec : rows.records) r.digest = BaselineDigestAdd(r.digest, rec);
      Push(p, rows);
      ++r.index;
    }
    if (r.next < r.keys.size()) return;
    // The end waits for room like the chunks (F1): BaselineEnd, Through and the mark go together.
    if (Backlogged(p)) return;
    Push(p, msg::BaselineEnd{r.id, r.at, r.digest});
    Push(p, msg::Through{p.cursor});
    p.last_through = std::max(p.last_through, p.cursor);
    p.last_proof = now;
    ServerEvent& e = Event(ServerEvent::Type::kBaselineEnded, p.id);
    e.baseline = r.id;
    e.why = r.why;
    const bool mark = r.retired_obligations;
    p.baseline.reset();
    if (mark) {
      // The floor moved with no other message to carry it (RC4).
      Push(p, msg::ObligationAck{0, p.ledger.Floor(), p.ledger.LastSent()});
      p.ledger.OnMarkSent(now);
    }
  }

  // ------------------------------------------------------------------------------------------
  // One peer's send, every pump, in section 3.5's order.

  // The state lane's one-off messages whose order is met, while it has room (F1): the
  // ObligationAcks owed, the obligations the cursor has passed (in id order, while the client has
  // credit; none while a baseline runs, OB7) and the mark. Called before the walk and again after
  // it (Sol code review r2, R1): under churn that fills the lane every pump the walk would
  // otherwise take all its room, and these would wait until the churn stopped. Before the walk
  // every delta up to the cursor is already queued, so an obligation at or below the cursor still
  // follows the deltas it depends on (section 3.5 step 5); after it, the ones the walk's cursor
  // passed go.
  void SendOneOffs(Peer& p, TimePoint now) {
    // The ObligationAcks owed since the state lane was backlogged, one per id, as it has room; a
    // running baseline does not hold them, as it never held an answer (only obligations, OB7).
    while (!p.acks_due.empty() && !Backlogged(p)) {
      const uint64_t id = *p.acks_due.begin();
      p.acks_due.erase(p.acks_due.begin());
      Push(p, msg::ObligationAck{id, p.ledger.Floor(), p.ledger.LastSent()});
    }
    if (p.baseline) return;
    while (!Backlogged(p)) {
      bool credit_bound = false;
      const Obligation* o = p.ledger.NextToSend(p.cursor, &credit_bound);
      if (!o) {
        if (credit_bound && !p.paused) {
          p.paused = true;
          ++p.credit_pauses;
          Event(ServerEvent::Type::kCreditPaused, p.id);
        }
        break;
      }
      msg::Obligation out;
      out.id = o->id;
      out.floor = p.ledger.Floor();
      out.scope = o->scope;
      out.lsn = o->lsn;
      out.key = o->key;
      out.value = o->value;
      Push(p, out);
      p.ledger.MarkSent(o->id, now);
      p.paused = false;
    }
    if (p.ledger.MarkDue(now) && !Backlogged(p)) {
      Push(p, msg::ObligationAck{0, p.ledger.Floor(), p.ledger.LastSent()});
      p.ledger.OnMarkSent(now);
    }
  }

  void SendTo(Peer& p, TimePoint now) {
    // The gameplay lane: what waited for it goes first (R2).
    SendWaitingPlay(p);
    // 1. Due baselines: below the floor, silence, and the reasons asked for.
    const bool below_floor = p.cursor + 1 < state.Floor();
    if (below_floor && p.sub.scopes != 0) {
      AskBaseline(p, BaselineReason::kBelowFloor, 0, state.Head());
    }
    // A peer whose last matched manifest is at the head still holds a proven copy: nothing was
    // logged since the match, so a quiet session is not silence (CV6). Under churn the head moves
    // and the rule reads matches from a manifest_ok that moved (Opus F6). Only a manifest_ok an Ack
    // raised is a match: the initial 0 is not one, so at Head() 0 the silence rule still runs
    // (F3: a killed BaselineEnd of the join baseline on an empty log is repaired).
    if (p.manifest_acked && p.manifest_ok == state.Head()) p.last_proof = now;
    if (p.sub.scopes != 0 && !p.baseline && now - p.last_proof >= limits.silence_baseline) {
      AskBaseline(p, BaselineReason::kSilence, 0, state.Head());
      p.last_proof = now;  // one silence baseline per silence period
    }
    if (!p.baseline && p.due && !Backlogged(p)) BeginBaseline(p, now);
    // 2. A running baseline.
    if (p.baseline) RunBaseline(p, now);
    // The one-off messages whose order is already met take the room before the walk (R1).
    SendOneOffs(p, now);
    // 3. Deltas from the cursor, in first-change order, then Through{proven}. An item is taken only
    // while the FIFO has room, and a taken item is pushed: `false` means "not taken" (the walk
    // stops before it), so the proof covers exactly what went into the FIFO. (Pushing first and
    // answering false would leave the pushed item unproven: sent again at the next pump, and with
    // room for one frame only, never proven at all, a livelock.)
    // A delta of an urgent kind (KindSpec::urgent, stage 5's request) flushes the lane at this
    // pump: its frame is marked, and so is the Through behind the walk.
    bool urgent = false;
    if (p.cursor + 1 >= state.Floor() && !Backlogged(p)) {
      uint64_t proven = p.cursor;
      state.Walk(
          p.cursor, p.sub,
          [&](const WalkItem& item) {
            if (Backlogged(p)) return false;
            if (item.type == EntryType::kEpoch) {
              Push(p, msg::EpochBegin{item.generation, item.lsn});
            } else if (item.record) {
              const KindSpec* spec = FindKind(table, item.record->key.kind);
              const bool now_urgent = spec != nullptr && spec->urgent;
              Push(p, msg::Delta{*item.record, item.first_lsn}, now_urgent);
              urgent = urgent || now_urgent;
            }
            return true;
          },
          &proven);
      p.cursor = std::max(p.cursor, proven);
    }
    if (p.cursor > p.last_through) {
      Push(p, msg::Through{p.cursor}, urgent);
      p.last_through = p.cursor;
    }
    // 4. A manifest, at the head only, when no baseline runs, and only while the state lane has
    // room (F1): a manifest not made while it is backlogged is replaced by the one made when it
    // has room again, at the head of then.
    if (!p.baseline && p.sub.scopes != 0 && p.cursor == state.Head() &&
        now - p.last_manifest >= limits.manifest_interval && !Backlogged(p)) {
      const WorldState::Manifest m = state.MakeManifest(p.sub.scopes);
      msg::Manifest out;
      out.lsn = m.lsn;
      out.scopes = m.scopes;
      out.digests = m.digest;
      Push(p, out);
      p.last_manifest = now;
      ++p.manifests;
    }
    // 5. Obligations, in id order, once the cursor passed each and while the client has credit;
    // none while a baseline runs (OB7); the ObligationAcks owed and the mark. While the state lane
    // is backlogged they wait in S's tables (F1).
    SendOneOffs(p, now);
  }

  void Flush(Peer& p, TimePoint now) {
    const PeerQueue::FlushResult r = p.queue.Flush(*p.link, now);
    if (r.backlog_began) {
      Event(ServerEvent::Type::kLimit, p.id).text = "backlog peer " + std::to_string(p.id);
    }
  }

  // ------------------------------------------------------------------------------------------
  // What a client sent.

  void Handle(Peer& p, Message& m, TimePoint now) {
    if (auto* ack = std::get_if<msg::Ack>(&m)) {
      p.prefix = ack->prefix;
      if (ack->manifest_ok > p.manifest_ok) {  // a manifest matched (Opus F6)
        p.last_proof = now;
        p.manifest_acked = true;
      }
      p.manifest_ok = std::max(p.manifest_ok, ack->manifest_ok);
      p.ledger.OnAck(ack->holding, ack->seen);
      if (p.ledger.Used() < limits.obligation_credit) p.paused = false;
    } else if (auto* done = std::get_if<msg::ObligationDone>(&m)) {
      if (p.ledger.OnDone(done->id)) ObligationEnded(p.id, done->id, done->end);
      // Every ObligationDone is answered (section 3.6); while the state lane is backlogged the
      // answer waits in acks_due, one per id (F1).
      if (!Backlogged(p) && p.acks_due.empty()) {
        Push(p, msg::ObligationAck{done->id, p.ledger.Floor(), p.ledger.LastSent()});
      } else if (p.acks_due.size() < limits.obligation_credit || p.acks_due.contains(done->id)) {
        p.acks_due.insert(done->id);
      }
    } else if (auto* mismatch = std::get_if<msg::Mismatch>(&m)) {
      ++p.mismatches;
      AskBaseline(p,
                  mismatch->bucket_mask == 0 ? BaselineReason::kDigestMismatch
                                             : BaselineReason::kManifestMismatch,
                  0, mismatch->lsn, mismatch->lost);
    } else if (auto* request = std::get_if<msg::Request>(&m)) {
      Request r = std::move(request->request);
      // S stamps peer and peer epoch from the link, never from the client's bytes (Sol F3).
      r.key.peer = p.id;
      r.key.peer_epoch = p.peer_epoch;
      requests.OnRequest(r, p.role, now);
    } else if (auto* renew = std::get_if<msg::LeaseRenew>(&m)) {
      requests.OnLeaseRenew(p.id, renew->id, now);
    } else if (auto* report = std::get_if<msg::LeaseReport>(&m)) {
      requests.OnLeaseReport(p.id, p.peer_epoch, report->id, report->payload, p.role, now);
    } else if (auto* od = std::get_if<msg::OwnedDelta>(&m)) {
      OnOwnedDelta(p, od->record, now);
    }
    Drain();
  }

  // Stage 4 (owned.h): a client's own player row. The six checks, then S's own write, so the row
  // reaches every other peer through the change log. A refusal is counted always and reported as a
  // kLimit event once per (peer, kind, reason) per second; a stale revision is never reported. The
  // revision is remembered only when the Put took the value (Sol stage 4 F2): a write refused at
  // the row limit (WorldState's own kLimit line) leaves the client's next revision admissible.
  void OnOwnedDelta(Peer& p, const Record& r, TimePoint now) {
    const OwnedVerdict v = owned.Admit(p.id, p.peer_epoch, r, [&] {
      const PutResult w = state.Put(r.key, r.value, now);
      return w == PutResult::kWritten || w == PutResult::kUnchanged;
    });
    if (v == OwnedVerdict::kAdmitted) return;
    if (owned.ShouldLog(p.id, r.key.kind, v, now)) {
      const KindSpec* spec = FindKind(table, r.key.kind);
      Event(ServerEvent::Type::kLimit, p.id).text =
          "owned write refused " + std::to_string(p.id) + " " +
          (spec ? std::string(spec->name) : std::to_string(r.key.kind)) + " (" +
          std::string(OwnedVerdictName(v)) + ")";
    }
  }

  // Stage 4: the peer epoch ended; a tomb for each owned row it wrote (section 3.5), S's only
  // write to an owned kind.
  void TombOwned(uint16_t peer, uint32_t peer_epoch, TimePoint now) {
    for (const RecordKey& key : owned.EndPeerEpoch(peer, peer_epoch)) state.Remove(key, now);
  }

  // A request or a lease report: what S answers on the gameplay lane, so what PlayCap() holds back.
  static bool AnsweredOnGameplay(const Message& m) {
    return std::holds_alternative<msg::Request>(m) || std::holds_alternative<msg::LeaseReport>(m);
  }

  // Every frame of the peer is read at every pump, and every message read is handled or held (N1).
  // S never leaves frames in the link: netplay's Connection acks a datagram when it arrives and
  // keeps what was not taken without a bound, and stage 3's backend frames share the session's
  // lanes with its own messages, so a link S stopped reading would grow below the PeerLink (a
  // probe over NetplayLink: 28,000 frames in 36 s). While S holds PlayCap() gameplay messages for
  // the peer, its requests and lease reports wait in `inbox`, in order, and are handled first
  // once it is below; its state messages (Ack, ObligationDone, Mismatch) and lease renews are
  // handled at once, as they add nothing to the gameplay lane, so a lease does not expire for the
  // wait. Past HeldCap() held, S ends the peer's session (Overrun).
  void Receive(Peer& p, TimePoint now) {
    while (!p.overrun && !p.inbox.empty() && !PlayFull(p)) {
      Message m = std::move(p.inbox.front());
      p.inbox.pop_front();
      Handle(p, m, now);
    }
    netplay::Lane lane{};
    std::vector<uint8_t> frame;
    std::vector<Message> messages;
    while (p.link->Receive(lane, frame)) {
      if (p.overrun) continue;  // its session ended: read and dropped
      messages.clear();
      DecodeFrame(frame, messages);  // a broken tail ends the frame; what came before stands
      for (Message& m : messages) {
        // Behind what is held, or at the cap: held. PlayHeld() only grows while S reads (nothing
        // is flushed meanwhile), so a held message keeps every later one behind it.
        if (AnsweredOnGameplay(m) && (!p.inbox.empty() || PlayFull(p))) {
          // A report resent while unanswered (S2-L3) is the same report (the client never remakes
          // one with another payload): one per lease waits, so the resends of a held peer do not
          // fill HeldCap(). The first copy is the one handled; a later one would be a replay.
          if (const auto* rep = std::get_if<msg::LeaseReport>(&m)) {
            const bool waiting = std::any_of(p.inbox.begin(), p.inbox.end(), [&](const Message& x) {
              const auto* w = std::get_if<msg::LeaseReport>(&x);
              return w && w->id == rep->id;
            });
            if (waiting) {
              ++p.reports_coalesced;
              continue;
            }
          }
          if (p.inbox.size() >= HeldCap()) {
            Overrun(p, now);
            break;
          }
          p.inbox.push_back(std::move(m));
          continue;
        }
        Handle(p, m, now);
      }
    }
  }

  // A peer past PlayCap() and HeldCap(): a sender past Client's admission, since a peer that uses
  // Client reaches neither (N1). S cannot hold more without a bound, and must not drop a request
  // or report unanswered, so it ends the peer's session: the peer's keys, leases and obligations
  // go as at RemovePeer (pending keys end `session ended` on S's side, nothing is sent), its held
  // messages and queues are cleared, and from now on S reads and drops what the peer sends and
  // sends it nothing. kPeerOverrun tells the host, which removes the peer and closes its
  // connection; a rejoin gets a new peer epoch (AddPeer). The policy is the header's own (a
  // council question).
  void Overrun(Peer& p, TimePoint now) {
    p.overrun = true;
    p.inbox.clear();
    p.play_waiting.clear();
    p.queue.Clear();
    p.acks_due.clear();
    p.baseline.reset();
    p.due.reset();
    requests.ForgetPeer(p.id, now);
    for (const uint64_t id : p.ledger.RetireAll()) {
      ObligationEnded(p.id, id, ObligationEnd::kRetiredPeerLeft);
    }
    Event(ServerEvent::Type::kPeerOverrun, p.id).text =
        "held " + std::to_string(HeldCap()) + " requests and reports past the gameplay cap";
    Drain();
  }

  void Trim() {
    if (peers.empty()) {
      if (state.Head() + 1 > state.Floor()) state.TrimTo(state.Head() + 1);
      return;
    }
    uint64_t slowest = state.Head();
    for (const auto& [id, p] : peers) {
      if (!p->overrun) slowest = std::min(slowest, p->cursor);  // an ended session holds nothing
    }
    uint64_t floor = slowest + 1;
    // Past log_retention the log lets the slowest go: that peer rebaselines (kBelowFloor).
    if (state.Head() > limits.log_retention && state.Head() - limits.log_retention + 1 > floor) {
      floor = state.Head() - limits.log_retention + 1;
    }
    if (floor > state.Floor()) state.TrimTo(floor);
  }
};

Server::Server(std::span<const KindSpec> table, AuthoritySet& authorities, const Limits& limits)
    : impl_(std::make_unique<Impl>(table, authorities, limits)) {
  impl_->requests.Reset(impl_->state.Epoch());
}

Server::~Server() = default;

void Server::AddPeer(uint16_t peer, PeerRole role, PeerLink& link, TimePoint now) {
  if (impl_->Find(peer)) RemovePeer(peer, now);
  const uint32_t epoch = ++impl_->epochs[peer];
  impl_->now = now;
  auto p = std::make_unique<Impl::Peer>(peer, role, link, epoch, impl_->limits, now);
  p->cursor = impl_->state.Head();
  p->last_through = p->cursor;
  p->last_proof = now;
  p->last_manifest = now;
  msg::StateHello hello;
  hello.table_digest = impl_->digest;
  hello.epoch = impl_->state.Epoch();
  hello.head = impl_->state.Head();
  hello.peer = peer;
  hello.peer_epoch = epoch;
  p->queue.Push(hello, now);
  p->due = Impl::Due{BaselineReason::kJoin, kScopeSession, false};
  impl_->peers[peer] = std::move(p);
}

void Server::OnPrecondition(uint16_t peer, uint32_t sequence, TimePoint now) {
  impl_->now = now;
  impl_->requests.OnPrecondition(peer, sequence, now);
  impl_->Drain();
}

void Server::RemovePeer(uint16_t peer, TimePoint now) {
  impl_->now = now;
  Impl::Peer* p = impl_->Find(peer);
  impl_->requests.ForgetPeer(peer, now);
  if (p) {
    for (const uint64_t id : p->ledger.RetireAll()) {
      impl_->ObligationEnded(peer, id, ObligationEnd::kRetiredPeerLeft);
    }
    impl_->TombOwned(peer, p->peer_epoch, now);  // stage 4: the departed epoch's owned rows
    impl_->peers.erase(peer);
  }
  impl_->Drain();
}

void Server::BeginEpoch(uint32_t generation, TimePoint now) {
  impl_->now = now;
  // Finals first, as in 1B: they are queued before anything of the new level.
  impl_->requests.OnGenerationChange(generation, now);
  impl_->Drain();
  for (auto& [id, p] : impl_->peers) {
    for (const uint64_t ob : p->ledger.RetireScope(Scope::kLevel)) {
      impl_->ObligationEnded(id, ob, ObligationEnd::kRetiredEpoch);
    }
    p->sub.scopes &= ScopeMask(~kScopeLevel);
    p->sub.since[size_t(Scope::kLevel)] = 0;
    if (p->due) {
      p->due->scopes &= ScopeMask(~kScopeLevel);
      if (p->due->scopes == 0 && !p->due->repair) p->due.reset();
    }
    // A running baseline of the level scope is dropped (BL6); its session part is asked again.
    if (p->baseline && (p->baseline->scopes & kScopeLevel) != 0) {
      const ScopeMask rest = ScopeMask(p->baseline->scopes & ~kScopeLevel);
      const BaselineReason why = p->baseline->why;
      p->baseline.reset();
      if (rest != 0) impl_->AskBaseline(*p, why, rest, impl_->state.Head());
    }
  }
  impl_->state.BeginEpoch(generation);
}

void Server::OnPeerLevelReady(uint16_t peer, uint32_t generation, TimePoint /*now*/) {
  Impl::Peer* p = impl_->Find(peer);
  if (!p || p->overrun || generation != impl_->state.Epoch()) return;
  if ((p->sub.scopes & kScopeLevel) != 0) return;  // already subscribed in this generation
  impl_->AskBaseline(*p, BaselineReason::kLevelReady, kScopeLevel, impl_->state.Head());
}

void Server::BeginClose(TimePoint now) {
  impl_->now = now;
  impl_->requests.BeginClose(now);
  impl_->Drain();
}

WorldState& Server::State() { return impl_->state; }

uint64_t Server::Oblige(uint16_t recipient, const RecordKey& key, std::span<const uint8_t> value) {
  Impl::Peer* p = impl_->Find(recipient);
  if (!p || p->overrun) return 0;  // absent, or its session ended (kPeerOverrun)
  const KindSpec* spec = FindKind(impl_->table, key.kind);
  if (!spec) {
    impl_->Event(ServerEvent::Type::kLimit, recipient).text = "unknown kind";
    return 0;
  }
  if (spec->scope == Scope::kLevel && (p->sub.scopes & kScopeLevel) == 0) return 0;  // OB8
  if (value.size() > spec->max_value_bytes || value.size() > kMaxValueBytes) {
    impl_->Event(ServerEvent::Type::kLimit, recipient).text =
        "max_value_bytes kind " + std::string(spec->name);
    return 0;
  }
  if (p->ledger.Full()) {
    impl_->Event(ServerEvent::Type::kLimit, recipient).text =
        "max_obligations_per_peer peer " + std::to_string(recipient);
    return 0;
  }
  Obligation o;
  o.id = p->ledger.NextId();
  o.recipient = recipient;
  o.key = key;
  o.value.assign(value.begin(), value.end());
  o.scope = spec->scope;
  o.lsn = impl_->state.LogObligation(o.id);
  const uint64_t id = o.id;
  p->ledger.Add(std::move(o));
  return id;
}

bool Server::SubmitEvaluation(const RequestKey& key, const EvaluationResult& result, TimePoint now) {
  impl_->now = now;
  const bool taken = impl_->requests.SubmitEvaluation(key, result, now);
  if (!taken) {
    ServerEvent& e = impl_->Event(ServerEvent::Type::kLateEvaluationDropped, key.peer);
    e.request.key = key;
  }
  impl_->Drain();
  return taken;
}

void Server::Pump(TimePoint now) {
  impl_->now = now;
  for (auto& [id, p] : impl_->peers) impl_->Receive(*p, now);
  impl_->state.Service(now);
  impl_->requests.Service(now);
  impl_->Drain();
  for (auto& [id, p] : impl_->peers) {
    if (!p->overrun) impl_->SendTo(*p, now);  // nothing goes to an ended session (N1)
  }
  for (auto& [id, p] : impl_->peers) {
    if (!p->overrun) impl_->Flush(*p, now);
  }
  impl_->Trim();
  impl_->DrainLimits();
}

bool Server::PollEvent(ServerEvent& out) {
  impl_->DrainLimits();  // a game Put refused since the last pump
  if (impl_->events.empty()) return false;
  out = std::move(impl_->events.front());
  impl_->events.pop_front();
  return true;
}

const RequestTable& Server::Requests() const { return impl_->requests; }
const LeaseTable& Server::Leases() const { return impl_->leases; }

PeerStats Server::Stats(uint16_t peer) const {
  PeerStats s;
  const Impl::Peer* p = impl_->Find(peer);
  if (!p) return s;
  s.peer_epoch = p->peer_epoch;
  s.cursor = p->cursor;
  s.prefix = p->prefix;
  s.manifest_ok = p->manifest_ok;
  s.unacked_frames = p->link->UnackedFrames(netplay::Lane::kState);
  s.subscription = p->sub;
  s.baseline = p->baseline ? p->baseline->id : 0;
  s.baseline_why = p->baseline ? p->baseline->why : BaselineReason::kJoin;
  s.obligations_outstanding = p->ledger.Outstanding();
  s.obligation_floor = p->ledger.Floor();
  s.obligation_reports_ignored = p->ledger.Ignored();
  s.credit_used = p->ledger.Used();
  s.obligations_waiting = p->ledger.Waiting();
  s.credit_pauses = p->credit_pauses;
  s.client_seen = p->ledger.ClientSeen();
  s.frames_held = p->queue.Held();
  s.frames_queued = p->queue.Frames();
  s.state_frames_queued = p->queue.Frames(netplay::Lane::kState);
  s.acks_due = p->acks_due.size();
  s.decisions_waiting = p->play_waiting.size();
  s.gameplay_held = impl_->PlayHeld(*p);
  s.cap_onsets = p->cap_onsets;
  s.requests_held = p->inbox.size();
  s.reports_coalesced = p->reports_coalesced;
  s.overrun = p->overrun;
  s.baselines = p->baselines;
  s.manifests = p->manifests;
  s.mismatches = p->mismatches;
  return s;
}

// Stage 4 (owned.h).
void Server::SetOwnedSchema(OwnedSchemaCheck schema) { impl_->owned.SetSchema(schema); }
const OwnedAdmission& Server::Owned() const { return impl_->owned; }

}  // namespace skylanders::backend
