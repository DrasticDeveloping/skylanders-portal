#include "backend/client.h"

#include <algorithm>
#include <deque>
#include <map>
#include <optional>
#include <set>
#include <utility>

namespace skylanders::backend {

namespace {

RowApply FromAdapter(AdapterResult r) {
  switch (r) {
    case AdapterResult::kOk: return RowApply::kApplied;
    case AdapterResult::kFailed: return RowApply::kFailed;
    case AdapterResult::kGone:
    case AdapterResult::kUnsupported:
      return RowApply::kDropped;
  }
  return RowApply::kFailed;
}

}  // namespace

struct Client::Impl {
  Impl(std::span<const KindSpec> table_in, AdapterSet& adapters_in, const Limits& limits_in)
      : table(table_in),
        adapters(adapters_in),
        limits(limits_in),
        digest(TableDigest(table_in)),
        replica(table_in, limits_in),
        holder(limits_in),
        queue(limits_in) {}

  // An open key (N1): taken by Send and kept until its final arrives or its `applied` completes.
  struct Sent {
    Request request;
    Decision last;
    bool decided = false;
    // A send of the key (Send or Resend) has no decision after it yet. Resend waits for it, so at
    // most one send of a key is unanswered and S never answers a key this client has closed.
    bool awaiting = false;
  };
  // A gameplay message made while the gameplay lane was held (F1): it waits here, in the order
  // made, and is queued once the lane goes again. A request (or resend) is named by its key, its
  // bytes are in `requests`, and a key waits once (R3); a lease report carries its payload, and a
  // newer report of the same lease replaces the waiting one at the end (the newest report wins:
  // client.h, Report; R5).
  struct Unsent {
    bool report = false;
    RequestKey key;                 // !report
    uint32_t lease = 0;             // report
    std::vector<uint8_t> payload;   // report
  };

  std::span<const KindSpec> table;
  AdapterSet& adapters;
  Limits limits;
  uint64_t digest;
  Replica replica;
  ObligationHolder holder;
  PeerLink* link = nullptr;
  PeerQueue queue;  // one FIFO per lane (lanes.h); the client's are sent at every flush
  TimePoint now{};  // the time of the call in progress
  bool hello = false;
  bool refused = false;
  uint16_t self = 0;
  uint32_t peer_epoch = 0;
  uint32_t generation = 0;
  std::map<uint32_t, uint32_t> next_sequence;  // per generation
  std::map<RequestKey, Sent> requests;
  // Granted, by id, until its LeaseEnd: the last renew, and whether a report of it went to the
  // link and has no answer yet (a decision with this lease; N1: Report waits for it). While it has
  // none, the report's payload, its last send and the sends so far: it is resent at the renew
  // ticks (S2-L3).
  struct LeaseHeld {
    TimePoint last_renew{};
    bool reported = false;
    std::vector<uint8_t> payload;
    TimePoint report_sent{};
    uint32_t sends = 0;
  };
  std::map<uint32_t, LeaseHeld> leases;
  msg::Ack last_ack;
  bool ack_sent = false;
  TimePoint last_ack_time{};
  std::deque<Unsent> unsent;              // gameplay messages waiting for the lane (F1)
  std::optional<msg::Mismatch> mismatch;  // the newest Mismatch waiting for the state lane (F1)
  std::deque<ClientEvent> events;
  // InboundLost (stage 3): the highest BaselineBegin id read, and while a loss is being repaired
  // the id a repair baseline must be above; recoverable messages are discarded until then.
  uint32_t last_baseline = 0;
  std::optional<uint32_t> repair_above;
  uint64_t discarded = 0;
  const Record* applying = nullptr;  // the row whose adapter call is in progress

  ClientEvent& Event(ClientEvent::Type type) {
    ClientEvent e;
    e.type = type;
    events.push_back(std::move(e));
    return events.back();
  }

  void Reset() {
    replica.Reset();
    holder.Reset();
    queue.Clear();
    hello = false;
    refused = false;
    self = 0;
    peer_epoch = 0;
    generation = 0;
    next_sequence.clear();
    requests.clear();
    leases.clear();
    last_ack = msg::Ack{};
    ack_sent = false;
    unsent.clear();
    mismatch.reset();
    last_baseline = 0;
    repair_above.reset();
    discarded = 0;
    applying = nullptr;
  }

  // The state messages a repair baseline recreates (stage 3 section 3.2's recoverable class on a
  // client): every row they carry is in the baseline of the subscribed scopes.
  static bool Recoverable(const Message& m) {
    return std::holds_alternative<msg::Delta>(m) || std::holds_alternative<msg::Tomb>(m) ||
           std::holds_alternative<msg::Through>(m) || std::holds_alternative<msg::Manifest>(m) ||
           std::holds_alternative<msg::BaselineBegin>(m) ||
           std::holds_alternative<msg::BaselineRows>(m) ||
           std::holds_alternative<msg::BaselineEnd>(m);
  }

  void Push(const Message& m) { queue.Push(m, now); }

  // A lane is held when the last flush met kFull on it (or the state lane's cap): nothing more is
  // queued on it until it goes again (F1). Periodic messages (Ack, ObligationDone repeats, lease
  // renews) are not made while it holds, and the next one made carries the newest state; one-off
  // messages wait in the client's tables (unsent, mismatch, the holder's reports).
  bool Held(netplay::Lane lane) const { return queue.Blocked(lane); }

  // A Mismatch, or the newest one waiting while the state lane is held: S treats every Mismatch
  // as a repair reason and coalesces them into one baseline (section 3.5 step 1), so only the
  // newest needs to go.
  void SendMismatch(const msg::Mismatch& m) {
    if (Held(netplay::Lane::kState) || mismatch) {
      // A newer one replaces the waiting one, but an InboundLost repair stays forced (F1).
      const bool lost = m.lost || (mismatch && mismatch->lost);
      mismatch = m;
      mismatch->lost = lost;
      return;
    }
    Push(m);
  }

  // A request, a resend or a lease report: queued now, or kept in `unsent` behind the ones
  // already waiting, so the gameplay lane's order is the order made. A key that already waits gets
  // no second entry: it goes once, with the bytes `requests` holds then (Sol code review r2, R3),
  // so `unsent` holds at most one entry per key of `requests` and one per lease. A newer report of
  // a lease replaces the waiting one and takes its place at the end, behind the requests made
  // before it (R5).
  void SendGameplay(Unsent u) {
    if (!Held(netplay::Lane::kGameplay) && unsent.empty()) {
      Queue(u);
      return;
    }
    auto same = std::find_if(unsent.begin(), unsent.end(), [&](const Unsent& w) {
      return w.report == u.report && (u.report ? w.lease == u.lease : w.key == u.key);
    });
    if (same != unsent.end()) {
      if (!u.report) return;
      unsent.erase(same);
    }
    unsent.push_back(std::move(u));
  }
  void Queue(const Unsent& u) {
    if (u.report) {
      auto held = leases.find(u.lease);
      if (held == leases.end()) return;  // its LeaseEnd came meanwhile: the key has its final
      held->second.reported = true;
      held->second.payload = u.payload;
      held->second.report_sent = now;
      held->second.sends = 1;
      msg::LeaseReport r;
      r.id = u.lease;
      r.payload = u.payload;
      Push(r);
      ClientEvent& e = Event(ClientEvent::Type::kLeaseReportSent);
      e.lease = u.lease;
      e.sends = 1;
      return;
    }
    // A key closed meanwhile needs nothing sent: a resend waiting here whose key then got S's final
    // (a retry key at a generation change or End session).
    auto it = requests.find(u.key);
    if (it != requests.end()) Push(msg::Request{it->second.request});
  }
  // What waited goes once the lanes go again.
  void SendWaiting() {
    if (!Held(netplay::Lane::kGameplay)) {
      for (const Unsent& u : unsent) Queue(u);
      unsent.clear();
    }
    if (mismatch && !Held(netplay::Lane::kState)) {
      Push(*mismatch);
      mismatch.reset();
    }
  }

  // A client's messages are few (acks, reports, requests): every lane goes at once.
  void Flush() {
    if (!link) return;
    queue.Flush(*link, now, /*now_all=*/true);
  }

  // ------------------------------------------------------------------------------------------
  // What S sent.

  void OnStateMessage(Message& m, TimePoint now) {
    if (auto* hello_msg = std::get_if<msg::StateHello>(&m)) {
      if (hello_msg->table_digest != digest) {
        // Another kind table: nothing from this S can be read (VR6).
        refused = true;
        hello = false;
        Event(ClientEvent::Type::kHelloRefused).text = "table digest differs";
        return;
      }
      hello = true;
      refused = false;
      self = hello_msg->peer;
      peer_epoch = hello_msg->peer_epoch;
      generation = hello_msg->epoch;
      return;
    }
    if (!hello) return;  // every state message before StateHello is refused
    if (repair_above && Recoverable(m)) {
      // After InboundLost: discarded until a baseline S began after the loss (stage 3).
      const auto* begin = std::get_if<msg::BaselineBegin>(&m);
      if (!begin || begin->id <= *repair_above) {
        ++discarded;
        return;
      }
      repair_above.reset();
    }
    if (auto* epoch = std::get_if<msg::EpochBegin>(&m)) {
      replica.OnEpoch(epoch->generation, epoch->lsn);
      NewGeneration(epoch->generation);
    } else if (auto* delta = std::get_if<msg::Delta>(&m)) {
      replica.OnRecord(delta->record, delta->first_lsn);
    } else if (auto* tomb = std::get_if<msg::Tomb>(&m)) {
      Record r;
      r.key = tomb->key;
      r.revision = tomb->revision;
      r.tomb = true;
      r.lsn = tomb->lsn;
      const KindSpec* spec = FindKind(table, r.key.kind);
      r.epoch = spec && spec->scope == Scope::kLevel ? generation : 0;
      replica.OnRecord(r, tomb->first_lsn);
    } else if (auto* through = std::get_if<msg::Through>(&m)) {
      replica.OnThrough(through->lsn);
    } else if (auto* manifest = std::get_if<msg::Manifest>(&m)) {
      WorldState::Manifest mf;
      mf.lsn = manifest->lsn;
      mf.scopes = manifest->scopes;
      mf.digest = manifest->digests;
      uint64_t mask = 0;
      if (replica.Staging() != 0) {
        // S makes a manifest only while no baseline of this peer runs, and a baseline's
        // BaselineEnd goes before any later manifest on this ordered lane: a manifest that finds
        // this client still staging means that end was lost above the lane (Sol code review r2,
        // R4). A baseline S dropped at a generation change (BL6) leaves no staging here: its
        // EpochBegin, which comes before any manifest of the new generation, drops level staging,
        // and the rest is asked again as a new baseline, which replaces the staging. So it asks
        // for a repair now, as for a failed digest, instead of waiting for silence_baseline (a
        // manifest matched before the baseline began would keep S's silence rule quiet).
        SendMismatch(msg::Mismatch{manifest->lsn, 0});
        ClientEvent& e = Event(ClientEvent::Type::kBaselineLost);
        e.lsn = manifest->lsn;
      } else if (replica.CheckManifest(mf, &mask) == Replica::Check::kMismatch) {
        SendMismatch(msg::Mismatch{manifest->lsn, mask});
        ClientEvent& e = Event(ClientEvent::Type::kManifestMismatch);
        e.lsn = manifest->lsn;
        e.bucket_mask = mask;
      }
    } else if (auto* begin = std::get_if<msg::BaselineBegin>(&m)) {
      // A full baseline can move S's cursor past an epoch entry it never sent (a repair that began
      // after BeginEpoch): the generation changes here then, with everything EpochBegin does.
      NewGeneration(begin->epoch);
      last_baseline = std::max(last_baseline, begin->id);
      replica.OnBaselineBegin(begin->id, begin->at, begin->epoch, begin->scopes);
    } else if (auto* rows = std::get_if<msg::BaselineRows>(&m)) {
      for (const Record& r : rows->records) replica.OnBaselineRow(rows->id, r);
    } else if (auto* end = std::get_if<msg::BaselineEnd>(&m)) {
      const BaselineEndResult res = replica.OnBaselineEnd(end->id, end->at, end->digest);
      if (res.ok) {
        Event(ClientEvent::Type::kBaselineApplied).baseline = end->id;
      } else {
        SendMismatch(msg::Mismatch{end->at, 0});  // a repair (Sol F6)
        ClientEvent& e = Event(ClientEvent::Type::kBaselineFailed);
        e.baseline = end->id;
        e.lsn = end->at;
      }
    } else if (auto* ob = std::get_if<msg::Obligation>(&m)) {
      Obligation o;
      o.id = ob->id;
      o.recipient = self;
      o.key = ob->key;
      o.value = ob->value;
      o.lsn = ob->lsn;
      o.scope = ob->scope;
      o.sent = true;
      if (holder.OnObligation(o, ob->floor) == ObligationHolder::Received::kOverCredit) {
        ClientEvent& e = Event(ClientEvent::Type::kCreditExceeded);
        e.row = o.key;
        e.text = "obligation " + std::to_string(o.id);
      }
    } else if (auto* oack = std::get_if<msg::ObligationAck>(&m)) {
      holder.OnAck(oack->id, oack->floor, oack->last_sent);
    } else if (auto* decision = std::get_if<msg::Decision>(&m)) {
      OnDecision(decision->decision, now);
    } else if (auto* lease_end = std::get_if<msg::LeaseEnd>(&m)) {
      leases.erase(lease_end->id);
      ClientEvent& e = Event(ClientEvent::Type::kLeaseEnded);
      e.lease = lease_end->id;
      e.lease_end = lease_end->why;
    } else if (auto* effect = std::get_if<msg::Effect>(&m)) {
      // Best effort, never replayed: applied if its entity is here now.
      for (const Record& r : effect->records) {
        const IdentityMap::Lookup look = adapters.Identity().Resolve(r.key.entity);
        if (look.resolution != Resolution::kResolved) continue;
        if (KindAdapter* a = adapters.For(r.key.kind)) a->Apply(look.handle, r.key.slot, r.value);
      }
    }
  }

  // A later generation: held level-scope obligations go, unreported (OB4). Keys of the ended
  // generations whose final came go too (an `applied` that will not complete here now). A key
  // still waiting for its final stays open: S sends a final for every key it holds at the change
  // (pending and held ones `stale generation`, a retry key its last reason, final) and refuses a
  // key of an ended generation that reaches it later `stale generation`, so the final comes.
  // EpochBegin comes on the state lane and the finals on the gameplay lane: forgetting the key
  // here would free its place while S still holds its final for this client (N1).
  void NewGeneration(uint32_t g) {
    if (g <= generation) return;
    generation = g;
    holder.OnEpochBegin();
    std::erase_if(requests, [&](const auto& entry) {
      return entry.first.generation < g && entry.second.decided && entry.second.last.final;
    });
  }

  void OnDecision(const Decision& d, TimePoint now) {
    // A late answer to a previous join is not this client's (Sol F3, VR9).
    if (d.key.peer != self || d.key.peer_epoch != peer_epoch) return;
    if (d.outcome == Outcome::kGranted && d.lease != 0) {
      LeaseHeld held;
      held.last_renew = now;
      leases.try_emplace(d.lease, std::move(held));
    }
    if (d.lease != 0 && d.outcome != Outcome::kGranted) {
      auto held = leases.find(d.lease);
      if (held != leases.end()) held->second.reported = false;  // the report's answer
    }
    auto it = requests.find(d.key);
    if (it != requests.end()) {
      it->second.last = d;
      it->second.decided = true;
      it->second.awaiting = false;
      // A final refusal is the key's end: nothing completes it, and it cannot be resent. So is
      // any final of an ended generation that came after EpochBegin (NewGeneration: it completes
      // nothing here now).
      if (d.final && (d.outcome == Outcome::kRefused || d.key.generation < generation)) {
        requests.erase(it);
      }
    }
    ClientEvent& e = Event(ClientEvent::Type::kDecided);
    e.key = d.key;
    e.decision = d;
  }

  void Receive(TimePoint now) {
    if (!link) return;
    netplay::Lane lane{};
    std::vector<uint8_t> frame;
    std::vector<Message> messages;
    while (link->Receive(lane, frame)) {
      messages.clear();
      DecodeFrame(frame, messages);
      for (Message& m : messages) OnStateMessage(m, now);
    }
  }

  // ------------------------------------------------------------------------------------------
  // The adapters.

  RowApply ApplyRow(const Record& r) {
    const KindSpec* spec = FindKind(table, r.key.kind);
    if (!spec || spec->unsupported) return RowApply::kDropped;
    if (spec->write != WriteMode::kApplied) return RowApply::kApplied;  // mirrored: kept, shown
    const IdentityMap::Lookup look = adapters.Identity().Resolve(r.key.entity);
    switch (look.resolution) {
      case Resolution::kResolved: {
        KindAdapter* a = adapters.For(r.key.kind);
        if (!a) return RowApply::kDropped;
        applying = &r;
        const AdapterResult res = r.tomb ? a->Retire(look.handle, r.key.slot)
                                         : a->Apply(look.handle, r.key.slot, r.value);
        applying = nullptr;
        return FromAdapter(res);
      }
      case Resolution::kPending:
      case Resolution::kAmbiguous:  // never picked; it may resolve when one of them goes
        return RowApply::kWaiting;
      case Resolution::kGone:
      case Resolution::kUnresolved:
        return RowApply::kDropped;
    }
    return RowApply::kWaiting;
  }

  void ApplyReady(TimePoint now) {
    // Every row handed out is answered in the same pump; a release an answer causes is taken in
    // the same loop (Sol F2, draft 4). A row answered waiting or failed is handed out again only
    // after the next Service; the guard stops the loop if a replica hands one out twice.
    std::set<std::pair<RecordKey, uint32_t>> unsettled;
    Record r;
    while (replica.TakeReady(r)) {
      if (!unsettled.emplace(r.key, r.revision).second) {
        replica.OnApplied(r.key, r.revision, RowApply::kWaiting, now);
        break;
      }
      const RowApply result = ApplyRow(r);
      replica.OnApplied(r.key, r.revision, result, now);
      if (result == RowApply::kApplied || result == RowApply::kDropped) {
        unsettled.erase({r.key, r.revision});
      }
    }
    ReplicaEvent re;
    while (replica.PollEvent(re)) {
      ClientEvent& e = Event(re.type == ReplicaEvent::Type::kRowLate
                                 ? ClientEvent::Type::kRowLate
                                 : ClientEvent::Type::kFenceBroken);
      e.row = re.row;
      e.dependency = re.dependency;
      e.row_apply = re.last;
    }
  }

  // Held obligations whose decision is complete here: fulfilled through the kind's adapter, or
  // refused when its entity is gone or did not come within the kind's grace.
  void Fulfil(TimePoint now) {
    for (const uint64_t id : holder.HeldIds()) {
      ObligationHolder::Entry* e = holder.Find(id);
      if (!e) continue;
      const Obligation& o = e->obligation;
      if (!replica.Completes(o.scope, o.lsn)) continue;
      const KindSpec* spec = FindKind(table, o.key.kind);
      const IdentityMap::Lookup look = adapters.Identity().Resolve(o.key.entity);
      bool wait = false;
      if (look.resolution == Resolution::kResolved) {
        KindAdapter* a = adapters.For(o.key.kind);
        const AdapterResult r = a ? a->Fulfil(look.handle, o) : AdapterResult::kUnsupported;
        if (r == AdapterResult::kOk) {
          holder.End(id, ObligationEnd::kCompleted, now);
          continue;
        }
        if (r != AdapterResult::kFailed) {
          holder.End(id, ObligationEnd::kRefused, now);
          continue;
        }
        wait = true;  // retried next pump, within the grace
      } else if (look.resolution == Resolution::kPending ||
                 look.resolution == Resolution::kAmbiguous) {
        wait = true;
      } else if (look.resolution == Resolution::kUnresolved && IsLevelForm(o.key.entity.form) &&
                 o.key.entity.epoch < adapters.Identity().Generation()) {
        // The game loaded a later level before EpochBegin came: S has retired this one
        // (kRetiredEpoch) and EpochBegin drops it, unreported (OB4). Refusing it now would
        // report an obligation S no longer holds.
        continue;
      } else {
        holder.End(id, ObligationEnd::kRefused, now);
        continue;
      }
      if (!wait) continue;
      // The grace counts world-runnable time (S2-L4): it stands still while the world is stopped.
      const TimePoint world = replica.WorldTime(now);
      if (!e->waiting) {
        e->waiting = true;
        e->waiting_since = world;
      }
      const uint32_t grace_ms = spec && spec->pending_grace_ms ? spec->pending_grace_ms
                                                               : limits.pending_grace_ms;
      if (world - e->waiting_since >= std::chrono::milliseconds(grace_ms)) {
        holder.End(id, ObligationEnd::kRefused, now);  // OB5
      }
    }
    // The reports wait in the holder while the state lane is held (F1): a reported entry stays
    // due, and its repeat goes once the lane goes; a once-only end stays queued in the holder.
    if (Held(netplay::Lane::kState)) return;
    for (const auto& [id, end] : holder.TakeReports(now)) {
      Push(msg::ObligationDone{id, end});
    }
  }

  // A key whose `applied` is complete here raises kCompleted once and goes: the map holds only
  // keys still waiting for a decision, a resend or a completion.
  void Completions() {
    for (auto it = requests.begin(); it != requests.end();) {
      const Sent& s = it->second;
      if (!s.decided || s.last.outcome != Outcome::kApplied ||
          !replica.Completes(s.last.scope, s.last.at_lsn)) {
        ++it;
        continue;
      }
      ClientEvent& e = Event(ClientEvent::Type::kCompleted);
      e.key = it->first;
      e.decision = s.last;
      it = requests.erase(it);
    }
  }

  // Renews every held lease each lease_renew_interval, a reported one too (S2-L3: the term stays
  // live while the report is unresolved); at the same tick an unanswered report whose last send is
  // an interval old goes again, the same id and payload, behind the renew.
  void Renew(TimePoint now) {
    if (Held(netplay::Lane::kGameplay)) return;  // periodic: the next one goes when it opens (F1)
    for (auto& [id, held] : leases) {
      if (now - held.last_renew < limits.lease_renew_interval) continue;
      Push(msg::LeaseRenew{id});
      held.last_renew = now;
      if (!held.reported || now - held.report_sent < limits.lease_renew_interval) continue;
      msg::LeaseReport r;
      r.id = id;
      r.payload = held.payload;
      Push(r);
      held.report_sent = now;
      ++held.sends;
      ClientEvent& e = Event(ClientEvent::Type::kLeaseReportSent);
      e.lease = id;
      e.sends = held.sends;
    }
  }

  void Ack(TimePoint now) {
    msg::Ack a;
    a.prefix = replica.Prefix();
    a.manifest_ok = replica.ManifestOk();
    a.holding = holder.Holding();
    a.seen = holder.Seen();
    const bool changed = !ack_sent || a.prefix != last_ack.prefix ||
                         a.manifest_ok != last_ack.manifest_ok || a.holding != last_ack.holding ||
                         a.seen != last_ack.seen;
    if (!changed && now - last_ack_time < limits.obligation_report_interval) return;
    // Periodic and carrying only the newest counts: not made while the state lane is held, and
    // the one made when it goes again replaces every one not sent (F1). S's account stays safe
    // meanwhile: an older Ack gives a `used` at least as large (section 3.6).
    if (Held(netplay::Lane::kState)) return;
    Push(a);
    last_ack = a;
    ack_sent = true;
    last_ack_time = now;
  }
};

Client::Client(std::span<const KindSpec> table, AdapterSet& adapters, const Limits& limits)
    : impl_(std::make_unique<Impl>(table, adapters, limits)) {}

Client::~Client() = default;

void Client::Attach(PeerLink& link, TimePoint now) {
  impl_->Reset();
  impl_->link = &link;
  impl_->now = now;
  impl_->last_ack_time = now;
}

std::optional<RequestKey> Client::Send(Request request, TimePoint now, SendRefusal* why) {
  Impl& c = *impl_;
  c.now = now;
  auto refuse = [&](SendRefusal r) {
    if (why) *why = r;
    return std::nullopt;
  };
  if (why) *why = SendRefusal::kNone;
  if (!c.hello || !c.link) return refuse(SendRefusal::kNoHello);
  // Admission (N1): nothing is taken that the client could not keep until its final.
  if (c.requests.size() >= c.limits.max_keys_per_peer) return refuse(SendRefusal::kKeyBound);
  request.key.peer = c.self;
  request.key.peer_epoch = c.peer_epoch;
  if (request.key.generation == 0) request.key.generation = c.generation;
  if (request.key.sequence != 0 && c.requests.contains(request.key)) {
    return refuse(SendRefusal::kKeyOpen);  // a second send of an open key is Resend's
  }
  if (request.key.sequence == 0) {
    request.key.sequence = ++c.next_sequence[request.key.generation];
  } else {
    uint32_t& next = c.next_sequence[request.key.generation];
    next = std::max(next, request.key.sequence);
  }
  if (request.parent.sequence != 0) {
    request.parent.peer = c.self;
    request.parent.peer_epoch = c.peer_epoch;
    request.parent.generation = request.key.generation;
  }
  // Sequences of older generations are not needed again.
  std::erase_if(c.next_sequence, [&](const auto& entry) { return entry.first < c.generation; });
  const RequestKey key = request.key;
  Impl::Sent& s = c.requests[key];
  s.request = std::move(request);
  s.awaiting = true;
  Impl::Unsent u;
  u.key = key;
  c.SendGameplay(std::move(u));
  c.Flush();
  return key;
}

bool Client::Resend(const RequestKey& key, TimePoint now, SendRefusal* why) {
  Impl& c = *impl_;
  c.now = now;
  auto refuse = [&](SendRefusal r) {
    if (why) *why = r;
    return false;
  };
  if (why) *why = SendRefusal::kNone;
  if (!c.hello || !c.link) return refuse(SendRefusal::kNoHello);
  auto it = c.requests.find(key);
  if (it == c.requests.end()) return refuse(SendRefusal::kNotRetry);
  Impl::Sent& s = it->second;
  if ((s.request.flags & kNeverResent) != 0) return refuse(SendRefusal::kNotRetry);
  // One unanswered send of a key at a time (N1).
  if (s.awaiting) return refuse(SendRefusal::kUnanswered);
  // The retry state: its last answer a refusal that is not final.
  if (!s.decided || s.last.outcome != Outcome::kRefused || s.last.final) {
    return refuse(SendRefusal::kNotRetry);
  }
  s.awaiting = true;
  Impl::Unsent u;
  u.key = key;
  c.SendGameplay(std::move(u));
  c.Flush();
  return true;
}

bool Client::Report(uint32_t lease, std::span<const uint8_t> payload, TimePoint now,
                    SendRefusal* why) {
  Impl& c = *impl_;
  c.now = now;
  if (why) *why = SendRefusal::kNone;
  if (!c.hello || !c.link) {
    if (why) *why = SendRefusal::kNoHello;
    return false;
  }
  auto held = c.leases.find(lease);
  if (held == c.leases.end()) {
    if (why) *why = SendRefusal::kNotHeld;
    return false;
  }
  if (held->second.reported) {  // one report of a lease unanswered at a time (N1)
    if (why) *why = SendRefusal::kUnanswered;
    return false;
  }
  Impl::Unsent u;
  u.report = true;
  u.lease = lease;
  u.payload.assign(payload.begin(), payload.end());
  c.SendGameplay(std::move(u));
  c.Flush();
  return true;
}

void Client::Pump(TimePoint now) {
  Impl& c = *impl_;
  c.now = now;
  c.Receive(now);
  if (!c.hello) {
    c.Flush();
    return;
  }
  c.replica.Service(now);
  c.ApplyReady(now);
  c.Fulfil(now);
  c.Completions();
  c.SendWaiting();
  c.Renew(now);
  c.Ack(now);
  c.Flush();
}

void Client::InboundLost(TimePoint now) {
  Impl& c = *impl_;
  c.now = now;
  if (!c.hello) return;
  c.replica.DropStaging();
  c.repair_above = c.last_baseline;
  c.discarded = 0;
  const uint64_t prefix = c.replica.Prefix();
  // Forced (Sol code review r1 F1): S must not count a running baseline as the answer, since its
  // rest is discarded here.
  c.SendMismatch(msg::Mismatch{prefix, 0, /*lost=*/true});
  ClientEvent& e = c.Event(ClientEvent::Type::kInboundLost);
  e.lsn = prefix;
  c.Flush();
}

bool Client::AwaitingRepairBaseline() const { return impl_->repair_above.has_value(); }
uint64_t Client::DiscardedAfterLoss() const { return impl_->discarded; }
const Record* Client::Applying() const { return impl_->applying; }

void Client::SetWorldStopped(bool stopped, TimePoint now) {
  impl_->replica.SetWorldStopped(stopped, now);
}

const Replica& Client::State() const { return impl_->replica; }
const ObligationHolder& Client::Obligations() const { return impl_->holder; }
size_t Client::QueuedFrames() const { return impl_->queue.Frames(); }
size_t Client::Waiting() const {
  return impl_->unsent.size() + (impl_->mismatch ? 1 : 0);
}
size_t Client::OpenKeys() const { return impl_->requests.size(); }
uint16_t Client::Self() const { return impl_->hello ? impl_->self : 0; }
uint32_t Client::PeerEpoch() const { return impl_->hello ? impl_->peer_epoch : 0; }

bool Client::PollEvent(ClientEvent& out) {
  if (impl_->events.empty()) return false;
  out = std::move(impl_->events.front());
  impl_->events.pop_front();
  return true;
}

}  // namespace skylanders::backend
