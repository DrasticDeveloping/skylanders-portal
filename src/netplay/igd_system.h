#pragma once

// The system's IGD client (stage 8 item 4): miniupnpc when the portal is built with it, otherwise
// none. A null result means the router mapping is unavailable; Host then says so and goes on.

#include <memory>

#include "netplay/port_mapping.h"

namespace skylanders::netplay {

std::unique_ptr<IgdClient> MakeSystemIgdClient();

}  // namespace skylanders::netplay
