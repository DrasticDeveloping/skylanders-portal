#include "backend/adapter.h"

namespace skylanders::backend {

void AdapterSet::Register(KindId kind, KindAdapter& adapter) { adapters_[kind] = &adapter; }

KindAdapter* AdapterSet::For(KindId kind) {
  auto it = adapters_.find(kind);
  return it == adapters_.end() ? nullptr : it->second;
}

IdentityMap& AdapterSet::Identity() { return identity_; }

// An applied kind needs an adapter on every client (the client writes it into the game, effects
// included); a mirrored or server-only kind, or one marked unsupported here, needs none.
std::vector<KindId> AdapterSet::Missing(std::span<const KindSpec> table) const {
  std::vector<KindId> out;
  for (const KindSpec& spec : table) {
    if (spec.write != WriteMode::kApplied || spec.unsupported) continue;
    if (!adapters_.contains(spec.id)) out.push_back(spec.id);
  }
  return out;
}

}  // namespace skylanders::backend
