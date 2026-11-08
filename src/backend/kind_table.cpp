#include "backend/kind_table.h"

#include <algorithm>
#include <map>
#include <set>

namespace skylanders::backend {

namespace {

// The rules' short names, as ValidationIssue::rule carries them.
constexpr std::string_view kV1 = "V1 ids";
constexpr std::string_view kV2 = "V2 initial";
constexpr std::string_view kV3 = "V3 never";
constexpr std::string_view kV4 = "V4 cycles";
constexpr std::string_view kV5 = "V5 scope";
constexpr std::string_view kV6 = "V6 events";
constexpr std::string_view kV7 = "V7 lanes";
constexpr std::string_view kV8 = "V8 default";
constexpr std::string_view kV9 = "V9 sizes";
constexpr std::string_view kV10 = "V10 models";
constexpr std::string_view kV11 = "V11 noisy";
constexpr std::string_view kV12 = "V12 grace";
constexpr std::string_view kV13 = "V13 replicated deps";
constexpr std::string_view kV14 = "V14 urgent";

std::string Named(const KindSpec& spec) {
  return spec.name.empty() ? "kind " + std::to_string(spec.id) : std::string(spec.name);
}

class Issues {
 public:
  void Add(const KindSpec& spec, std::string_view rule, std::string_view what) {
    out_.push_back({spec.id, rule, Named(spec) + ": " + std::string(what)});
  }
  std::vector<ValidationIssue> Take() { return std::move(out_); }

 private:
  std::vector<ValidationIssue> out_;
};

}  // namespace

bool Replicated(const KindSpec& spec) {
  return spec.tier != Tier::kEffect && spec.write != WriteMode::kServerOnly;
}

bool Fenced(const KindSpec& spec) { return !spec.depends_on.empty(); }

const KindSpec* FindKind(std::span<const KindSpec> table, KindId id) {
  for (const KindSpec& spec : table) {
    if (spec.id == id) return &spec;
  }
  return nullptr;
}

std::vector<ValidationIssue> Validate(std::span<const KindSpec> table, const Limits& limits) {
  Issues issues;
  const size_t max_bytes = std::min<size_t>(limits.max_value_bytes, kMaxValueBytes);

  // Lookups by id. With a duplicate id (a V1 issue) the first spec wins for the other rules.
  std::map<KindId, const KindSpec*> by_id;
  for (const KindSpec& spec : table) by_id.emplace(spec.id, &spec);
  auto find = [&](KindId id) -> const KindSpec* {
    const auto it = by_id.find(id);
    return it == by_id.end() ? nullptr : it->second;
  };

  // V1 ids: every id non-zero and unique; every name non-empty and unique.
  {
    std::map<KindId, size_t> ids;
    std::map<std::string_view, size_t> names;
    for (const KindSpec& spec : table) {
      if (spec.id == 0) issues.Add(spec, kV1, "id 0");
      else if (++ids[spec.id] == 2) issues.Add(spec, kV1, "id " + std::to_string(spec.id) + " used twice");
      if (spec.name.empty()) issues.Add(spec, kV1, "no name");
      else if (++names[spec.name] == 2) issues.Add(spec, kV1, "name used twice");
    }
  }

  // Who depends on whom, for V12 (only known kinds other than the dependent itself).
  std::set<KindId> depended_on;
  for (const KindSpec& spec : table) {
    for (const KindId dep : spec.depends_on) {
      if (dep != spec.id && find(dep) != nullptr) depended_on.insert(dep);
    }
  }

  for (const KindSpec& spec : table) {
    // V2 initial: the initial policy is set (the plan's "required initial policy"), and so is the
    // write mode, which every other rule reads.
    if (spec.initial == InitialPolicy::kUnset) issues.Add(spec, kV2, "initial policy unset");
    else if (uint8_t(spec.initial) > uint8_t(InitialPolicy::kNever)) issues.Add(spec, kV2, "initial policy unknown");
    if (spec.write == WriteMode::kUnset) issues.Add(spec, kV2, "write mode unset");
    else if (uint8_t(spec.write) > uint8_t(WriteMode::kServerOnly)) issues.Add(spec, kV2, "write mode unknown");

    // V3 never: no `applied` on `never`, and `never` is exactly the unsupported kinds.
    if (spec.initial == InitialPolicy::kNever) {
      if (spec.write == WriteMode::kApplied) issues.Add(spec, kV3, "applied on never");
      if (!spec.unsupported) issues.Add(spec, kV3, "never without unsupported");
    } else if (spec.unsupported) {
      issues.Add(spec, kV3, "unsupported needs never");
    }

    // V4 cycles (the self and missing parts; the cycle walk is below).
    for (const KindId dep : spec.depends_on) {
      if (dep == spec.id) issues.Add(spec, kV4, "depends on itself");
      else if (find(dep) == nullptr) issues.Add(spec, kV4, "depends on unknown kind " + std::to_string(dep));
    }

    // V5 scope: a session-scope kind depends only on session-scope kinds.
    if (uint8_t(spec.scope) > uint8_t(Scope::kLevel)) issues.Add(spec, kV5, "scope unknown");
    if (spec.scope == Scope::kSession) {
      for (const KindId dep : spec.depends_on) {
        const KindSpec* d = find(dep);
        if (d != nullptr && dep != spec.id && d->scope != Scope::kSession) {
          issues.Add(spec, kV5, "session kind depends on " + Named(*d) + ", not session scope");
        }
      }
    }

    // V6 events: a durable event if and only if an event.
    if (uint8_t(spec.tier) > uint8_t(Tier::kEffect)) issues.Add(spec, kV6, "tier unknown");
    if ((spec.tier == Tier::kDurableEvent) != (spec.state_class == StateClass::kEvent)) {
      issues.Add(spec, kV6, spec.tier == Tier::kDurableEvent ? "durable event not of the event class"
                                                              : "event class not a durable event");
    }

    // V7 lanes: the fast lane only for effects. That an effect is in no baseline and no manifest
    // is Replicated(), which every walk uses.
    if (uint8_t(spec.lane) > uint8_t(LaneUse::kFast)) issues.Add(spec, kV7, "lane unknown");
    if (spec.lane == LaneUse::kFast && spec.tier != Tier::kEffect) issues.Add(spec, kV7, "fast lane for a non-effect");

    // V8 default: only kAuthoredDefault has one, of at most max_value_bytes.
    if (spec.initial == InitialPolicy::kAuthoredDefault) {
      if (spec.authored_default.empty()) issues.Add(spec, kV8, "authored default missing");
      else if (spec.authored_default.size() > spec.max_value_bytes) issues.Add(spec, kV8, "authored default over max_value_bytes");
    } else if (!spec.authored_default.empty()) {
      issues.Add(spec, kV8, "authored default without kAuthoredDefault");
    }

    // V9 sizes: 0 < max_value_bytes <= the limit; the state class is a known one (1 and 2 are
    // reserved: stage 4's client-owned value, a merge-on-S class).
    if (spec.max_value_bytes == 0) issues.Add(spec, kV9, "max_value_bytes 0");
    else if (spec.max_value_bytes > max_bytes) issues.Add(spec, kV9, "max_value_bytes over " + std::to_string(max_bytes));
    if (spec.state_class != StateClass::kCurrentValue && spec.state_class != StateClass::kEvent) {
      issues.Add(spec, kV9, "state class " + std::to_string(unsigned(spec.state_class)) + " reserved or unknown");
    }

    // V10 models.
    if ((spec.models & ~uint8_t(kModelCommit | kModelServerRun | kModelOwnerRun)) != 0) {
      issues.Add(spec, kV10, "unknown model bits");
    }
    if ((spec.models & kModelOwnerRun) != 0 && !spec.leasable) issues.Add(spec, kV10, "owner-run without leasable");
    if ((spec.models & kModelOwnerRun) != 0 && spec.write == WriteMode::kServerOnly) {
      issues.Add(spec, kV10, "owner-run on a server-only kind");
    }
    if (spec.models == 0) {
      if (spec.parent_wait_ms != 0) issues.Add(spec, kV10, "parent_wait_ms on a kind that takes no requests");
      if (spec.requires_precondition) issues.Add(spec, kV10, "requires_precondition on a kind that takes no requests");
    }

    // V11 noisy: only a current value is coalesced.
    if (spec.noisy && spec.state_class != StateClass::kCurrentValue) issues.Add(spec, kV11, "noisy on a non-current value");

    // V12 grace: an applied kind that waits for its entity, or that a fence waits for, has a grace.
    if (spec.write == WriteMode::kApplied && spec.pending_grace_ms == 0) {
      if (spec.initial == InitialPolicy::kReadAtSpawn) issues.Add(spec, kV12, "applied, read at spawn, grace 0");
      else if (depended_on.contains(spec.id)) issues.Add(spec, kV12, "applied, depended on, grace 0");
    }

    // V13 replicated deps: a fenced row never waits for a row that is never sent.
    if (Replicated(spec)) {
      for (const KindId dep : spec.depends_on) {
        const KindSpec* d = find(dep);
        if (d != nullptr && dep != spec.id && !Replicated(*d)) {
          issues.Add(spec, kV13, "depends on " + Named(*d) + ", which is not replicated");
        }
      }
    }

    // V14 urgent: only a replicated kind's deltas ride the state lane's batch.
    if (spec.urgent && !Replicated(spec)) issues.Add(spec, kV14, "urgent on a kind that is not replicated");
  }

  // V4 cycles: a kind that reaches itself through other kinds (self edges and unknown ids are
  // reported above and skipped here). Tables are small; a walk per kind is enough.
  for (const KindSpec& spec : table) {
    if (find(spec.id) != &spec) continue;  // a duplicate id: V1's
    std::set<KindId> seen;
    std::vector<KindId> stack;
    for (const KindId dep : spec.depends_on) {
      if (dep != spec.id) stack.push_back(dep);
    }
    bool cycle = false;
    while (!stack.empty() && !cycle) {
      const KindId at = stack.back();
      stack.pop_back();
      if (at == spec.id) {
        cycle = true;
        break;
      }
      if (!seen.insert(at).second) continue;
      if (const KindSpec* d = find(at)) {
        for (const KindId next : d->depends_on) {
          if (next != at) stack.push_back(next);
        }
      }
    }
    if (cycle) issues.Add(spec, kV4, "dependency cycle");
  }

  return issues.Take();
}

uint64_t TableDigest(std::span<const KindSpec> table) {
  std::vector<const KindSpec*> sorted;
  sorted.reserve(table.size());
  for (const KindSpec& spec : table) sorted.push_back(&spec);
  std::stable_sort(sorted.begin(), sorted.end(), [](const KindSpec* a, const KindSpec* b) {
    return a->id != b->id ? a->id < b->id : a->name < b->name;
  });

  // Every data field, in a fixed layout; the contents of the spans, never their addresses; only
  // whether each function pointer is set.
  std::vector<uint8_t> bytes;
  Writer w(bytes);
  w.U32(uint32_t(sorted.size()));
  for (const KindSpec* s : sorted) {
    w.U16(s->id);
    w.U16(uint16_t(s->name.size()));
    w.Bytes(s->name);
    w.U16(s->version);
    w.U8(uint8_t(s->scope));
    w.U8(uint8_t(s->tier));
    w.U8(uint8_t(s->state_class));
    w.U8(uint8_t(s->initial));
    w.U8(uint8_t(s->write));
    w.U8(uint8_t(s->lane));
    w.U8(s->models);
    w.U8(uint8_t((s->leasable ? 1 : 0) | (s->noisy ? 2 : 0) | (s->unsupported ? 4 : 0) |
                 (s->parent_missing_retryable ? 8 : 0) | (s->requires_precondition ? 16 : 0) |
                 (s->equal != nullptr ? 32 : 0) | (s->reason_name != nullptr ? 64 : 0) |
                 (s->urgent ? 128 : 0)));
    w.U16(s->max_value_bytes);
    w.U32(s->pending_grace_ms);
    w.U32(s->parent_wait_ms);
    // The dependencies are a set: their order in the span is not part of the table.
    std::vector<KindId> deps(s->depends_on.begin(), s->depends_on.end());
    std::sort(deps.begin(), deps.end());
    w.U16(uint16_t(deps.size()));
    for (const KindId d : deps) w.U16(d);
    w.U32(uint32_t(s->authored_default.size()));
    w.Bytes(s->authored_default);
  }
  return ValueDigest(bytes);
}

}  // namespace skylanders::backend
