#pragma once

// The system IGD client's rules and its pure half (stage 8 item 4, design D8; the client is
// igd_system.cpp, its entry point MakeSystemIgdClient in igd_system.h).
//
// Threads (which thread may call what):
// - MakeSystemIgdClient() blocks while it discovers the router: up to kDiscoveryBound. The discovery
//   itself runs on the client's own worker thread; the calling thread only waits for it. Call it from
//   a background thread (the server's mapping thread), never from the game thread.
// - Every IgdClient method of the returned client blocks up to kCallBound (one SOAP call to the
//   router, run on the worker thread). Call them from that same one background thread, which also
//   owns the PortMapper; PortMapper::Map, Renew and Remove make several calls in a row, so they block
//   for several bounds. Version() and LanAddress() only read what discovery found and never block.
// - The worker runs one call at a time. A call that is still waiting to start when its bound passes
//   never starts. A call that is running when its bound passes is abandoned: the caller gets
//   kTimedOut at once, and every later call gets kTimedOut at once until the router has answered
//   (or miniupnpc has given up on) the abandoned one. Destroying the client never waits: the worker
//   finishes the call it is running, drops the rest and ends.
// - An abandoned add can still reach the router after its caller was told kTimedOut. PortMapper
//   never counts it as owned (it is never re-read as ours), so it is never renewed or deleted and
//   ends with its lease (kMappingLeaseS), like every entry this session does not own.
//
// Nothing here touches the network: every function below is unit-tested
// (tests/netplay_igd_system_test.cpp), whether or not the portal is built with miniupnpc.

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <string_view>

#include "netplay/port_mapping.h"

namespace skylanders::netplay::igd {

// --- the bounds -----------------------------------------------------------------------------------

// SSDP: how long discovery waits for routers to answer one search (miniupnpc sends up to four
// searches, one per device type, until one is answered).
constexpr int kDiscoveryDelayMs = 1500;
// Discovery in all: the searches, then each answering device's description and connection checks.
constexpr std::chrono::milliseconds kDiscoveryBound{8000};
// One call to the router (a SOAP request and its answer).
constexpr std::chrono::milliseconds kCallBound{4000};

// --- the results ------------------------------------------------------------------------------------

// What the system client returns beyond kUpnpOk, kUpnpNoRouter and the router's own UPnP error codes
// (positive, passed through unchanged: kUpnpNoSuchEntry, kUpnpConflictInMappingEntry, 402, 606, ...).
// All negative and below kUpnpNoRouter, so none of them is ever read as a UPnP code or as "no router".
constexpr int kTimedOut = -2;     // not finished within its bound (an abandoned call may still finish)
constexpr int kNoAnswer = -3;     // no HTTP answer from the router (miniupnpc's UPNPCOMMAND_HTTP_ERROR)
constexpr int kBadAnswer = -4;    // an answer that could not be read (UPNPCOMMAND_INVALID_RESPONSE, or a
                                  // field this client refuses: an address or port that does not parse)
constexpr int kClientError = -5;  // refused before sending (a request this client never sends), or
                                  // miniupnpc's own failure (invalid arguments, memory, unknown error)

// miniupnpc's command result (UPNPCOMMAND_SUCCESS, its negative UPNPCOMMAND_* codes, or the router's
// UPnP error code) as an IgdClient result.
int ResultFromMiniupnpc(int rc);

// --- discovery --------------------------------------------------------------------------------------

// The IGD version from the connection service's type: WANIPConnection:2 is 2, WANIPConnection:1 and
// WANPPPConnection:1 are 1, anything else 0 (not usable).
int VersionFromServiceType(std::string_view service_type);

// UPNP_GetValidIGD's result: 1 (a connected IGD), 2 (connected, with a reserved external address: the
// classifier then says why the internet cannot reach it) and 3 (an IGD that reports itself not
// connected; some routers misreport it, and the external-address read and the classifier decide) can
// be used; 0 (none), 4 (a UPnP device that is no IGD) and errors cannot.
bool UsableIgd(int get_valid_igd_result);

// --- requests ---------------------------------------------------------------------------------------

// Plain AddPortMapping goes only to IGD v1, AddAnyPortMapping only to IGD v2 (design D8: IGD v2's plain
// AddPortMapping is never sent). The client refuses any other combination without sending it.
bool AddAllowed(int version, bool any_port);

// Text miniupnpc puts into a SOAP body as it is (it does not escape): printable ASCII without
// < > & " ', at most 255 characters.
bool SoapSafe(std::string_view text);

// An entry the client may send: protocol UDP or TCP, both ports non-zero, a dotted IPv4 internal
// client, a SOAP-safe description, a lease of at most 604800 s (the UPnP maximum).
bool ValidRequest(const PortMappingEntry& e);

// Decimal text for miniupnpc's arguments.
std::string DecimalText(uint32_t value);

// --- answers ----------------------------------------------------------------------------------------

// Strict readers of the router's text fields. An address is a.b.c.d (1 to 3 digits each, 0 to 255),
// returned in host byte order (192.168.1.20 is 0xC0A80114); a port is 1 to 65535 in digits only.
std::optional<uint32_t> ParseIpv4(std::string_view dotted);
std::optional<uint16_t> ParsePort(std::string_view decimal);

// GetSpecificPortMappingEntry's answer for `external_port` as an entry. The answer carries no
// protocol or external port, so both come from the request. nullopt when the internal client or port
// does not parse (the call then returns kBadAnswer: the port is not shown free and not shown ours).
// `enabled` "0" is false, anything else true; a lease that does not parse is 0.
std::optional<PortMappingEntry> EntryFromAnswer(uint16_t external_port, const std::string& protocol,
                                                std::string_view internal_client,
                                                std::string_view internal_port,
                                                std::string_view description,
                                                std::string_view enabled, std::string_view lease);

// --- the worker -------------------------------------------------------------------------------------

// One thread that runs calls in order, each bounded as the thread rules above say. The client runs
// every miniupnpc call through it; it knows nothing of the network.
class BoundedWorker {
 public:
  BoundedWorker();  // starts the thread (throws std::system_error if it cannot)
  ~BoundedWorker();  // never waits: see the thread rules
  BoundedWorker(const BoundedWorker&) = delete;
  BoundedWorker& operator=(const BoundedWorker&) = delete;

  // Runs `job` on the worker thread and returns its result, or kTimedOut if it has not finished
  // within `bound` (or an abandoned call is still running). `job` must own everything it touches: it
  // may outlive this call and the worker's owner.
  int Run(std::function<int()> job, std::chrono::milliseconds bound);

  struct State;

 private:
  std::shared_ptr<State> state_;
};

// True when the portal was built with miniupnpc (SKYLANDERS_PORTAL_MINIUPNPC); without it
// MakeSystemIgdClient() always returns nullptr and sends nothing.
bool SystemIgdBuiltIn();

}  // namespace skylanders::netplay::igd
