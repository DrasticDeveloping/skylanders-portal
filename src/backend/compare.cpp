#include "backend/compare.h"

#include <algorithm>
#include <chrono>
#include <map>
#include <optional>
#include <utility>

namespace skylanders::backend {

namespace {

using Live = LocalCompare::Live;

std::string Hex64(uint64_t v) {
  constexpr char kHex[] = "0123456789abcdef";
  std::string out(16, '0');
  for (int i = 15; i >= 0; --i) {
    out[size_t(i)] = kHex[v & 0xF];
    v >>= 4;
  }
  return out;
}

bool ValuesEqual(const KindSpec& spec, std::span<const uint8_t> a, std::span<const uint8_t> b) {
  if (spec.equal != nullptr) return spec.equal(a, b);
  return std::equal(a.begin(), a.end(), b.begin(), b.end());
}

// One row of the copy against the live world (compare.h's table).
LocalCompare CompareRow(const KindSpec& spec, const Record& r, AdapterSet& live, bool repair,
                        TimePoint now) {
  LocalCompare c;
  c.key = r.key;
  c.revision = r.revision;
  c.digest = ValueDigest(r.value);
  const auto stuck = [&](std::string why) {
    c.live = Live::kStuck;
    c.differs = std::move(why);
    return c;
  };

  if (spec.unsupported || spec.initial == InitialPolicy::kNever) {
    c.live = Live::kUnsupported;
    return c;
  }
  KindAdapter* adapter = live.For(r.key.kind);
  if (adapter == nullptr) {
    if (spec.write == WriteMode::kApplied) return stuck("no adapter");
    c.live = Live::kEqual;  // mirrored or server-only here: the copy is the value
    return c;
  }

  IdentityMap& identity = live.Identity();
  const IdentityMap::Lookup found = identity.Resolve(r.key.entity);

  if (r.tomb) {
    if (found.resolution != Resolution::kResolved) {
      c.live = Live::kEqual;
      return c;
    }
    if (!repair) return stuck("tomb live");
    adapter->Retire(found.handle, r.key.slot);
    if (identity.Resolve(r.key.entity).resolution != Resolution::kResolved) {
      c.live = Live::kRepaired;
      return c;
    }
    return stuck("tomb live");
  }

  switch (found.resolution) {
    case Resolution::kResolved: break;
    case Resolution::kPending: {
      c.live = Live::kPending;
      if (now != TimePoint{} && now > found.since) c.pending_for = now - found.since;
      // Pending, then stuck (section 3.5, SR6c): an entity missing past the kind's grace will not
      // be waited for by the fence either; the row is stuck with how long it has waited.
      const uint32_t grace_ms =
          spec.pending_grace_ms != 0 ? spec.pending_grace_ms : Limits{}.pending_grace_ms;
      const auto waited = std::chrono::duration_cast<std::chrono::milliseconds>(c.pending_for);
      if (now != TimePoint{} && waited >= std::chrono::milliseconds(grace_ms)) {
        return stuck("pending " + std::to_string(waited.count()));
      }
      return c;
    }
    case Resolution::kAmbiguous: return stuck("ambiguous");
    case Resolution::kUnresolved: return stuck("unresolved");
    case Resolution::kGone: return stuck("gone");
  }

  std::vector<uint8_t> value;
  const auto read = [&]() -> std::optional<LocalCompare> {
    value.clear();
    switch (adapter->Read(found.handle, r.key.slot, value)) {
      case AdapterResult::kOk: return std::nullopt;
      case AdapterResult::kUnsupported: c.live = Live::kUnsupported; return c;
      case AdapterResult::kGone: return stuck("gone");
      case AdapterResult::kFailed: return stuck("read failed");
    }
    return stuck("read failed");
  };
  if (auto done = read()) return *done;
  if (ValuesEqual(spec, r.value, value)) {
    c.live = Live::kEqual;
    return c;
  }
  if (!repair) return stuck("value " + Hex64(ValueDigest(value)));

  switch (adapter->Apply(found.handle, r.key.slot, r.value)) {
    case AdapterResult::kOk: break;
    case AdapterResult::kUnsupported: c.live = Live::kUnsupported; return c;
    case AdapterResult::kGone: return stuck("gone");
    case AdapterResult::kFailed: return stuck("apply failed");
  }
  if (auto done = read()) return *done;
  if (ValuesEqual(spec, r.value, value)) {
    c.live = Live::kRepaired;
    return c;
  }
  return stuck("value " + Hex64(ValueDigest(value)));
}

template <typename Copy>
std::vector<LocalCompare> CompareCopy(const Copy& copy, AdapterSet& live, KindId kind, bool repair,
                                      TimePoint now) {
  std::vector<LocalCompare> out;
  const KindSpec* spec = FindKind(copy.Table(), kind);
  if (spec == nullptr) return out;
  std::vector<const Record*> rows;
  copy.ForEach([&](const Record& r) {
    if (r.key.kind == kind) rows.push_back(&r);
  });
  // Collected first and compared after, so an adapter never runs inside the copy's iteration.
  std::sort(rows.begin(), rows.end(),
            [](const Record* a, const Record* b) { return a->key < b->key; });
  out.reserve(rows.size());
  for (const Record* r : rows) out.push_back(CompareRow(*spec, *r, live, repair, now));
  return out;
}

CompareResult ResultOf(Live live) {
  switch (live) {
    case Live::kEqual: return CompareResult::kEqual;
    case Live::kRepaired: return CompareResult::kRepaired;
    case Live::kPending: return CompareResult::kPending;
    case Live::kStuck: return CompareResult::kStuck;
    case Live::kUnsupported: return CompareResult::kUnsupported;
  }
  return CompareResult::kStuck;
}

// kStuck > kPending > kRepaired > kEqual.
int Rank(CompareResult r) {
  switch (r) {
    case CompareResult::kEqual: return 0;
    case CompareResult::kRepaired: return 1;
    case CompareResult::kPending: return 2;
    case CompareResult::kStuck: return 3;
    case CompareResult::kUnsupported: return 4;  // never ranked: the join handles it first
  }
  return 3;
}

std::string Side(std::string_view side, const LocalCompare& c) {
  return std::string(side) + ": " +
         (c.differs.empty() ? std::string(CompareResultName(ResultOf(c.live))) : c.differs);
}

int64_t Ms(Duration d) {
  return std::chrono::duration_cast<std::chrono::milliseconds>(d).count();
}

}  // namespace

std::string_view CompareResultName(CompareResult r) {
  switch (r) {
    case CompareResult::kEqual: return "equal";
    case CompareResult::kRepaired: return "repaired";
    case CompareResult::kPending: return "pending";
    case CompareResult::kStuck: return "stuck";
    case CompareResult::kUnsupported: return "unsupported";
  }
  return "?";
}

std::vector<LocalCompare> CompareLocal(const WorldState& s, AdapterSet& live, KindId kind,
                                       bool repair, TimePoint now) {
  return CompareCopy(s, live, kind, repair, now);
}

std::vector<LocalCompare> CompareLocal(const Replica& r, AdapterSet& live, KindId kind,
                                       bool repair, TimePoint now) {
  return CompareCopy(r, live, kind, repair, now);
}

std::vector<RowCompare> JoinCompare(std::span<const LocalCompare> server,
                                    std::span<const LocalCompare> peer) {
  std::map<RecordKey, std::pair<const LocalCompare*, const LocalCompare*>> keys;
  for (const LocalCompare& c : server) keys[c.key].first = &c;
  for (const LocalCompare& c : peer) keys[c.key].second = &c;

  std::vector<RowCompare> out;
  out.reserve(keys.size());
  for (const auto& [key, pair] : keys) {
    const auto& [s, p] = pair;
    RowCompare row;
    row.key = key;
    row.stored_revision = s != nullptr ? s->revision : 0;
    row.replica_revision = p != nullptr ? p->revision : 0;
    const auto stuck = [&](std::string why) {
      row.result = CompareResult::kStuck;
      row.differs = std::move(why);
    };
    if (p == nullptr) {
      stuck("replica missing");
    } else if (s == nullptr) {
      stuck("replica extra");
    } else if (p->revision < s->revision) {
      stuck("replica behind");
    } else if (p->revision != s->revision || p->digest != s->digest) {
      stuck("replica differs");
    } else if (s->live == Live::kUnsupported && p->live == Live::kUnsupported) {
      row.result = CompareResult::kUnsupported;
    } else if (s->live == Live::kUnsupported) {
      stuck("server: unsupported");
    } else if (p->live == Live::kUnsupported) {
      stuck("client: unsupported");
    } else {
      const CompareResult rs = ResultOf(s->live);
      const CompareResult rp = ResultOf(p->live);
      row.result = Rank(rs) >= Rank(rp) ? rs : rp;
      if (row.result != CompareResult::kEqual) {
        // Name every side that has the worse result, server first.
        if (rs == row.result) row.differs = Side("server", *s);
        if (rp == row.result) {
          if (!row.differs.empty()) row.differs += ", ";
          row.differs += Side("client", *p);
        }
      }
      if (row.result == CompareResult::kPending) {
        if (rs == row.result) row.pending_for = std::max(row.pending_for, s->pending_for);
        if (rp == row.result) row.pending_for = std::max(row.pending_for, p->pending_for);
      }
    }
    out.push_back(std::move(row));
  }
  return out;
}

CompareSummary Summarize(KindId kind, std::span<const RowCompare> rows) {
  CompareSummary s;
  s.kind = kind;
  for (const RowCompare& r : rows) {
    if (r.key.kind != kind) continue;
    switch (r.result) {
      case CompareResult::kEqual: ++s.equal; break;
      case CompareResult::kRepaired: ++s.repaired; break;
      case CompareResult::kPending: ++s.pending; break;
      case CompareResult::kStuck: ++s.stuck; break;
      case CompareResult::kUnsupported: ++s.unsupported; break;
    }
  }
  return s;
}

std::string LocalLine(uint16_t peer, std::string_view kind_name, const LocalCompare& c) {
  std::string line = "backend local " + std::to_string(peer) + " " + std::string(kind_name) + " " +
                     ToText(c.key.entity) + " slot " + std::to_string(c.key.slot) + " rev " +
                     std::to_string(c.revision) + " digest " + Hex64(c.digest) + " " +
                     std::string(CompareResultName(ResultOf(c.live)));
  if (c.live == Live::kPending) line += " pending " + std::to_string(Ms(c.pending_for));
  if (!c.differs.empty()) line += " differs " + c.differs;
  return line;
}

std::string CompareLine(uint16_t peer, std::string_view kind_name, const RowCompare& r) {
  std::string line = "backend compare " + std::to_string(peer) + " " + std::string(kind_name) +
                     " " + ToText(r.key.entity) + " " + std::string(CompareResultName(r.result)) +
                     " rev " + std::to_string(r.stored_revision) + "/" +
                     std::to_string(r.replica_revision);
  if (!r.differs.empty()) line += " differs " + r.differs;
  return line;
}

std::string ComparedLine(uint16_t peer, std::string_view kind_name, const CompareSummary& s) {
  return "backend compared " + std::to_string(peer) + " " + std::string(kind_name) + " equal " +
         std::to_string(s.equal) + " repaired " + std::to_string(s.repaired) + " pending " +
         std::to_string(s.pending) + " stuck " + std::to_string(s.stuck) + " unsupported " +
         std::to_string(s.unsupported);
}

}  // namespace skylanders::backend
