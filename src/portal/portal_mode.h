#pragma once

#include <optional>
#include <string_view>

namespace skylanders::portal {

// kHybrid: the emulated portal for the game, with the figures of a real USB portal mirrored into
// it (portal/hybrid/real_portal_bridge.h).
enum class PortalMode { kNone, kSoftware, kUsb, kHybrid };

// Accepts "none", "software", "usb" or "hybrid" (any case, surrounding whitespace ignored).
// Anything else, including modes that do not exist, gives nullopt.
std::optional<PortalMode> ParsePortalMode(std::string_view text);

// The portal a run really uses for the mode it asked for. A test run (a non-empty --user_data_root;
// Swap Force Online's main safety audit) never writes to a physical portal: its usb mode runs as
// hybrid, which reads the real portal's figures into the emulated one, and its hybrid mode runs
// without write-back, so the game's writes stay in the copies (and the copies' files). Every other
// run gets the mode it asked for, with write-back in hybrid mode as before.
struct PortalPlan {
  PortalMode mode = PortalMode::kNone;
  bool real_write_back = true;  // hybrid: the game's writes to a copy go onto the real figure
};
PortalPlan PlanPortal(PortalMode requested, bool test_run);

}  // namespace skylanders::portal
