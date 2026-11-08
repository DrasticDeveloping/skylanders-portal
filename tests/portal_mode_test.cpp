#include "portal/portal_mode.h"
#include "test_util.h"

using skylanders::portal::ParsePortalMode;
using skylanders::portal::PlanPortal;
using skylanders::portal::PortalMode;

int main() {
  CHECK(ParsePortalMode("software") == PortalMode::kSoftware);
  CHECK(ParsePortalMode("none") == PortalMode::kNone);
  CHECK(ParsePortalMode("  Software \t") == PortalMode::kSoftware);
  CHECK(ParsePortalMode("NONE") == PortalMode::kNone);

  CHECK(ParsePortalMode("usb") == PortalMode::kUsb);
  CHECK(ParsePortalMode("Hybrid") == PortalMode::kHybrid);

  // Unknown values are rejected.
  CHECK(!ParsePortalMode("").has_value());
  CHECK(!ParsePortalMode("   ").has_value());
  CHECK(!ParsePortalMode("banana").has_value());
  CHECK(!ParsePortalMode("software extra").has_value());

  // PlanPortal (Sol's N2): a test run (--user_data_root) never writes to a physical portal; usb
  // runs as hybrid without write-back, hybrid without write-back; software and none stay. A run
  // without the setting gets what it asked for, hybrid with write-back.
  CHECK(PlanPortal(PortalMode::kUsb, true).mode == PortalMode::kHybrid);
  CHECK(!PlanPortal(PortalMode::kUsb, true).real_write_back);
  CHECK(PlanPortal(PortalMode::kHybrid, true).mode == PortalMode::kHybrid);
  CHECK(!PlanPortal(PortalMode::kHybrid, true).real_write_back);
  CHECK(PlanPortal(PortalMode::kSoftware, true).mode == PortalMode::kSoftware);
  CHECK(PlanPortal(PortalMode::kNone, true).mode == PortalMode::kNone);
  CHECK(PlanPortal(PortalMode::kUsb, false).mode == PortalMode::kUsb);
  CHECK(PlanPortal(PortalMode::kUsb, false).real_write_back);
  CHECK(PlanPortal(PortalMode::kHybrid, false).mode == PortalMode::kHybrid);
  CHECK(PlanPortal(PortalMode::kHybrid, false).real_write_back);
  CHECK(PlanPortal(PortalMode::kSoftware, false).mode == PortalMode::kSoftware);
  CHECK(PlanPortal(PortalMode::kNone, false).mode == PortalMode::kNone);

  return Finish("portal_mode");
}
