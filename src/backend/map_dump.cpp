#include "backend/map_dump.h"

#include <algorithm>
#include <charconv>
#include <chrono>
#include <map>
#include <tuple>
#include <utility>

#include "backend/lanes.h"
#include "backend/server.h"

namespace skylanders::backend {

namespace {

constexpr char kHex[] = "0123456789abcdef";

std::string Hex64(uint64_t v) {
  std::string out(16, '0');
  for (int i = 15; i >= 0; --i) {
    out[size_t(i)] = kHex[v & 0xF];
    v >>= 4;
  }
  return out;
}

std::string HexBytes(std::span<const uint8_t> bytes) {
  std::string out;
  out.reserve(bytes.size() * 2);
  for (const uint8_t b : bytes) {
    out.push_back(kHex[b >> 4]);
    out.push_back(kHex[b & 0xF]);
  }
  return out;
}

int HexDigit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

bool ParseHexBytes(std::string_view text, std::vector<uint8_t>& out) {
  out.clear();
  if (text.size() % 2 != 0) return false;
  for (size_t i = 0; i < text.size(); i += 2) {
    const int hi = HexDigit(text[i]);
    const int lo = HexDigit(text[i + 1]);
    if (hi < 0 || lo < 0) return false;
    out.push_back(uint8_t(hi << 4 | lo));
  }
  return true;
}

// One token per field: a word of more than one is written with '-' for its spaces.
std::string Token(std::string_view words) {
  std::string out(words);
  std::replace(out.begin(), out.end(), ' ', '-');
  return out.empty() ? std::string("-") : out;
}

std::string KindName(std::span<const KindSpec> table, KindId kind) {
  if (const KindSpec* spec = FindKind(table, kind)) return std::string(spec->name);
  return "#" + std::to_string(kind);
}

// The dump's order: scope, kind name, entity text, slot.
struct SortedRow {
  uint8_t scope = 0;
  std::string kind;
  std::string entity;
  const Record* record = nullptr;
  bool server_only = false;
};

std::vector<SortedRow> SortRows(std::span<const KindSpec> table, std::vector<const Record*> rows,
                                bool include_server_only) {
  std::vector<SortedRow> out;
  out.reserve(rows.size());
  for (const Record* r : rows) {
    const KindSpec* spec = FindKind(table, r->key.kind);
    const bool replicated = spec != nullptr && Replicated(*spec);
    if (!replicated && !include_server_only) continue;
    SortedRow s;
    s.scope = uint8_t(spec != nullptr ? spec->scope : Scope::kLevel);
    s.kind = KindName(table, r->key.kind);
    s.entity = ToText(r->key.entity);
    s.record = r;
    s.server_only = !replicated;
    out.push_back(std::move(s));
  }
  std::sort(out.begin(), out.end(), [](const SortedRow& a, const SortedRow& b) {
    return std::tie(a.scope, a.kind, a.entity, a.record->key.slot) <
           std::tie(b.scope, b.kind, b.entity, b.record->key.slot);
  });
  return out;
}

struct Header {
  uint64_t head = 0, prefix = 0;
  uint32_t epoch = 0;
};

std::string WriteDump(const MapDumpOptions& options, const Header& h, std::span<const KindSpec> table,
                  const std::vector<SortedRow>& rows) {
  size_t bytes = 0;
  for (const SortedRow& s : rows) bytes += s.record->value.size();
  std::string out = "map " + options.who + " head " + std::to_string(h.head) + " prefix " +
                    std::to_string(h.prefix) + " epoch " + std::to_string(h.epoch) + " rows " +
                    std::to_string(rows.size()) + " bytes " + std::to_string(bytes) + " table " +
                    Hex64(TableDigest(table)) + "\n";
  for (const SortedRow& s : rows) {
    const Record& r = *s.record;
    out += "row " + s.kind + " " + s.entity + " slot " + std::to_string(r.key.slot) + " rev " +
           std::to_string(r.revision) + " lsn " + std::to_string(r.lsn) + " epoch " +
           std::to_string(r.epoch) + " writer " + std::to_string(r.writer);
    if (r.tomb) out += " tomb";
    if (s.server_only) out += " server-only";
    out += " value " + (r.value.empty() ? std::string("-") : HexBytes(r.value));
    out += " digest " + Hex64(ValueDigest(r.value)) + "\n";
  }
  for (const auto* lines : {&options.obligation_lines, &options.lease_lines, &options.peer_lines}) {
    for (const std::string& line : *lines) out += line + "\n";
  }
  return out;
}

// Splits at runs of spaces (and a trailing '\r').
std::vector<std::string_view> Split(std::string_view line) {
  std::vector<std::string_view> out;
  size_t i = 0;
  while (i < line.size()) {
    while (i < line.size() && (line[i] == ' ' || line[i] == '\r' || line[i] == '\t')) ++i;
    const size_t start = i;
    while (i < line.size() && line[i] != ' ' && line[i] != '\r' && line[i] != '\t') ++i;
    if (i > start) out.push_back(line.substr(start, i - start));
  }
  return out;
}

template <typename T>
bool Number(std::string_view text, T& out, int base = 10) {
  if (text.empty()) return false;
  const auto [end, ec] = std::from_chars(text.data(), text.data() + text.size(), out, base);
  return ec == std::errc{} && end == text.data() + text.size();
}

// Reads `word <number>` at tokens[i]; moves i past both.
template <typename T>
bool Field(const std::vector<std::string_view>& t, size_t& i, std::string_view word, T& out,
           int base = 10) {
  if (i + 1 >= t.size() || t[i] != word) return false;
  if (!Number(t[i + 1], out, base)) return false;
  i += 2;
  return true;
}

std::string LineError(size_t n, std::string_view what) {
  return "line " + std::to_string(n) + ": " + std::string(what);
}

}  // namespace

std::string DumpMap(const WorldState& state, const MapDumpOptions& options) {
  std::vector<const Record*> rows;
  state.ForEach([&](const Record& r) { rows.push_back(&r); });
  Header h;
  h.head = state.Head();
  h.prefix = state.Head();
  h.epoch = state.Epoch();
  return WriteDump(options, h, state.Table(), SortRows(state.Table(), std::move(rows), true));
}

std::string DumpMap(const Replica& replica, const MapDumpOptions& options) {
  std::vector<const Record*> rows;
  Header h;
  replica.ForEach([&](const Record& r) {
    rows.push_back(&r);
    h.head = std::max(h.head, r.lsn);
  });
  h.prefix = options.prefix != 0 ? options.prefix : replica.Prefix();
  h.epoch = replica.Epoch();
  return WriteDump(options, h, replica.Table(), SortRows(replica.Table(), std::move(rows), false));
}

std::string ObligationLine(const Obligation& o, std::span<const KindSpec> table,
                           std::string_view state) {
  return "obligation " + std::to_string(o.id) + " to " + std::to_string(o.recipient) + " " +
         KindName(table, o.key.kind) + " " + ToText(o.key.entity) + " lsn " +
         std::to_string(o.lsn) + " " + Token(state);
}

std::string LeaseLine(const Lease& lease, std::span<const KindSpec> table, TimePoint now) {
  const bool live = lease.state == LeaseState::kGranted || lease.state == LeaseState::kReported;
  int64_t left = 0;
  if (live && lease.expires > now) {
    left = std::chrono::duration_cast<std::chrono::milliseconds>(lease.expires - now).count();
  }
  return "lease " + std::to_string(lease.id) + " " + KindName(table, lease.spec.kind) + " " +
         ToText(lease.spec.entity) + " holder " + std::to_string(lease.spec.holder) + " " +
         Token(LeaseStateName(lease.state)) + " left " + std::to_string(left);
}

std::string PeerLine(uint16_t peer, const PeerStats& s) {
  const bool baseline = s.baseline != 0;
  const auto since = [&](Scope scope) {
    return (s.subscription.scopes & MaskOf(scope)) != 0
               ? std::to_string(s.subscription.since[size_t(scope)])
               : std::string("-");
  };
  return "peer " + std::to_string(peer) + " cursor " + std::to_string(s.cursor) + " prefix " +
         std::to_string(s.prefix) + " unacked " + std::to_string(s.unacked_frames) +
         " baseline " + (baseline ? std::to_string(s.baseline) : std::string("-")) + " " +
         (baseline ? Token(BaselineReasonName(s.baseline_why)) : std::string("-")) + " since " +
         since(Scope::kSession) + " " + since(Scope::kLevel) + " credit " +
         std::to_string(s.credit_used) + " waiting " + std::to_string(s.obligations_waiting);
}

std::optional<MapDump> ParseMap(std::string_view text, std::string* error) {
  const auto fail = [&](size_t n, std::string_view what) -> std::optional<MapDump> {
    if (error != nullptr) *error = LineError(n, what);
    return std::nullopt;
  };
  MapDump dump;
  bool have_header = false;
  size_t n = 0;
  size_t start = 0;
  while (start <= text.size()) {
    size_t end = text.find('\n', start);
    if (end == std::string_view::npos) end = text.size();
    const std::string_view line = text.substr(start, end - start);
    start = end + 1;
    ++n;
    const std::vector<std::string_view> t = Split(line);
    if (t.empty()) {
      if (end == text.size()) break;
      continue;
    }
    if (t[0] == "map") {
      if (have_header) return fail(n, "a second map header");
      size_t i = 1;
      if (i < t.size() && t[i] == "S") {
        dump.who = "S";
        i += 1;
      } else if (i + 1 < t.size() && t[i] == "peer") {
        uint16_t peer = 0;
        if (!Number(t[i + 1], peer)) return fail(n, "peer number");
        dump.who = "peer " + std::string(t[i + 1]);
        i += 2;
      } else {
        return fail(n, "who (S or peer <n>)");
      }
      if (!Field(t, i, "head", dump.head)) return fail(n, "head");
      if (!Field(t, i, "prefix", dump.prefix)) return fail(n, "prefix");
      if (!Field(t, i, "epoch", dump.epoch)) return fail(n, "epoch");
      if (!Field(t, i, "rows", dump.rows)) return fail(n, "rows");
      if (!Field(t, i, "bytes", dump.bytes)) return fail(n, "bytes");
      if (!Field(t, i, "table", dump.table, 16)) return fail(n, "table");
      if (i != t.size()) return fail(n, "words after the table digest");
      have_header = true;
      continue;
    }
    if (!have_header) return fail(n, "a line before the map header");
    if (t[0] == "row") {
      MapRow row;
      if (t.size() < 3) return fail(n, "row kind and entity");
      row.kind = std::string(t[1]);
      row.entity = std::string(t[2]);
      size_t i = 3;
      if (!Field(t, i, "slot", row.slot)) return fail(n, "slot");
      if (!Field(t, i, "rev", row.revision)) return fail(n, "rev");
      if (!Field(t, i, "lsn", row.lsn)) return fail(n, "lsn");
      if (!Field(t, i, "epoch", row.epoch)) return fail(n, "epoch");
      if (!Field(t, i, "writer", row.writer)) return fail(n, "writer");
      if (i < t.size() && t[i] == "tomb") {
        row.tomb = true;
        ++i;
      }
      if (i < t.size() && t[i] == "server-only") {
        row.server_only = true;
        ++i;
      }
      if (i + 1 >= t.size() || t[i] != "value") return fail(n, "value");
      std::vector<uint8_t> value;
      if (t[i + 1] != "-") {
        if (!ParseHexBytes(t[i + 1], value)) return fail(n, "value hex");
        row.value_hex = std::string(t[i + 1]);
        std::transform(row.value_hex.begin(), row.value_hex.end(), row.value_hex.begin(),
                       [](char c) { return char(c >= 'A' && c <= 'F' ? c - 'A' + 'a' : c); });
      }
      i += 2;
      if (!Field(t, i, "digest", row.digest, 16)) return fail(n, "digest");
      if (i != t.size()) return fail(n, "words after the digest");
      if (row.digest != ValueDigest(value)) return fail(n, "digest is not the value's");
      dump.row_lines.push_back(std::move(row));
      continue;
    }
    if (t[0] == "obligation" || t[0] == "lease" || t[0] == "peer") {
      std::string_view kept = line;
      while (!kept.empty() && (kept.back() == '\r' || kept.back() == ' ')) kept.remove_suffix(1);
      while (!kept.empty() && kept.front() == ' ') kept.remove_prefix(1);
      dump.other_lines.emplace_back(kept);
      continue;
    }
    return fail(n, "unknown line '" + std::string(t[0]) + "'");
  }
  if (!have_header) return fail(n, "no map header");
  if (dump.row_lines.size() != dump.rows) {
    return fail(n, "the header says rows " + std::to_string(dump.rows) + ", the dump has " +
                       std::to_string(dump.row_lines.size()));
  }
  return dump;
}

std::string_view MapDiffName(MapDiff::What what) {
  switch (what) {
    case MapDiff::What::kOnlyA: return "only-a";
    case MapDiff::What::kOnlyB: return "only-b";
    case MapDiff::What::kRevision: return "revision";
    case MapDiff::What::kTomb: return "tomb";
    case MapDiff::What::kValue: return "value";
  }
  return "?";
}

std::vector<MapDiff> DiffMaps(const MapDump& a, const MapDump& b) {
  using Key = std::tuple<std::string, std::string, uint16_t>;
  std::map<Key, std::pair<const MapRow*, const MapRow*>> rows;
  for (const MapRow& r : a.row_lines) {
    if (!r.server_only) rows[{r.kind, r.entity, r.slot}].first = &r;
  }
  for (const MapRow& r : b.row_lines) {
    if (!r.server_only) rows[{r.kind, r.entity, r.slot}].second = &r;
  }
  const auto rev = [](const MapRow* r) {
    return r != nullptr ? "rev " + std::to_string(r->revision) : std::string("-");
  };
  const auto live = [](const MapRow& r) { return std::string(r.tomb ? "tomb" : "live"); };
  std::vector<MapDiff> out;
  for (const auto& [key, pair] : rows) {
    const auto& [ra, rb] = pair;
    MapDiff d;
    d.kind = std::get<0>(key);
    d.entity = std::get<1>(key);
    d.slot = std::get<2>(key);
    std::string av, bv;
    if (rb == nullptr) {
      d.what = MapDiff::What::kOnlyA;
      av = rev(ra);
      bv = "-";
    } else if (ra == nullptr) {
      d.what = MapDiff::What::kOnlyB;
      av = "-";
      bv = rev(rb);
    } else if (ra->tomb != rb->tomb) {
      d.what = MapDiff::What::kTomb;
      av = live(*ra);
      bv = live(*rb);
    } else if (ra->revision != rb->revision) {
      d.what = MapDiff::What::kRevision;
      av = std::to_string(ra->revision);
      bv = std::to_string(rb->revision);
    } else if (ra->digest != rb->digest || ra->value_hex != rb->value_hex) {
      d.what = MapDiff::What::kValue;
      av = Hex64(ra->digest);
      bv = Hex64(rb->digest);
    } else {
      continue;
    }
    d.text = d.kind + " " + d.entity + " slot " + std::to_string(d.slot) + " " +
             std::string(MapDiffName(d.what)) + " a " + av + " b " + bv;
    out.push_back(std::move(d));
  }
  return out;
}

}  // namespace skylanders::backend
