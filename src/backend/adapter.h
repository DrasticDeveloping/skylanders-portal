#pragma once

#include <cstdint>
#include <map>
#include <span>
#include <vector>

#include "backend/entity_id.h"
#include "backend/kind_table.h"
#include "backend/obligation.h"
#include "backend/record.h"

// The adapter contract (design section 3.11): what every per-game kind adapter fills, from stage 3
// on. Adapters are the only code that knows the game (plan 2.1). All calls come on the game thread;
// a guest read happens only there (1-pre's rule). No code: the fakes (fake_world.h) and the game
// implement it.

namespace skylanders::backend {

enum class AdapterResult : uint8_t { kOk, kFailed, kGone, kUnsupported };

class KindAdapter {
 public:
  virtual ~KindAdapter() = default;
  virtual AdapterResult Read(Handle h, uint16_t slot, std::vector<uint8_t>& value) = 0;
  virtual AdapterResult Apply(Handle h, uint16_t slot, std::span<const uint8_t> value) = 0;
  // A tomb.
  virtual AdapterResult Retire(Handle /*h*/, uint16_t /*slot*/) { return AdapterResult::kOk; }
  virtual AdapterResult Fulfil(Handle /*h*/, const Obligation& /*o*/) {
    return AdapterResult::kUnsupported;
  }
};

// Per peer: one adapter per applied kind, and the IdentityMap they feed.
class AdapterSet {
 public:
  void Register(KindId kind, KindAdapter& adapter);
  KindAdapter* For(KindId kind);
  IdentityMap& Identity();
  // Applied kinds with no adapter.
  std::vector<KindId> Missing(std::span<const KindSpec> table) const;

 private:
  std::map<KindId, KindAdapter*> adapters_;
  IdentityMap identity_;
};

}  // namespace skylanders::backend
