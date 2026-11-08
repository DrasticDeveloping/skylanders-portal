#pragma once

// The router side of "Internet" hosting (stage 8 item 4, design D8): a UDP port mapping through UPnP
// IGD (v1 and v2) that only ever touches an entry this session created, NAT-PMP used read-only for
// the router's external address, and the classifier of that address.
//
// The rules, each one a unit test (tests/netplay_port_mapping_test.cpp):
// - The tuple asked for: UDP, external port = the configured port (or the router's choice on IGD v2),
//   internal client = the host's LAN address, internal port = the configured port (the server's
//   socket holds it exclusively), description "Swap Force Online <session nonce>", lease 3600 s.
// - A pre-existing entry is never replaced: before an add the entry for that external port is read
//   (GetSpecificPortMappingEntry); if one exists, whatever it is (an earlier session's leftover
//   included: its nonce differs), it is left alone and the next candidate is tried. IGD v1 tries the
//   configured port and up to 8 ports above it; IGD v2 asks only AddAnyPortMapping (which picks a free
//   port and never changes an existing entry); IGD v2's plain AddPortMapping is never sent.
// - An add that fails (718 ConflictInMappingEntry, 729 ConflictWithOtherMechanisms, anything else) is
//   never followed by a delete, an overwrite or a second add on that external port.
// - After a successful add the entry is read again and owned only if it carries this session's tuple
//   and nonce; anything else is a conflict: not owned, never renewed or deleted.
// - Only the owned entry is renewed and deleted, each after a re-read that still shows the owned tuple
//   and nonce. A crash leaves it to its lease.
// - NAT-PMP cannot prove a mapping is this session's, so no NAT-PMP mapping request (opcodes 1, 2) is
//   ever sent: only the 2-byte external-address request (opcode 0) and its 12-byte answer.
//
// The IGD calls go through IgdClient. TODO(stage 8 item 4, OQ-C5): the miniupnpc-backed IgdClient
// (miniupnpc as a nested submodule of the portal, the council's choice) and Host's three modes; until
// then nothing in the game creates a PortMapper.

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>

#include "netplay/transport.h"

namespace skylanders::netplay {

// --- the external address ------------------------------------------------------------------------

enum class AddressClass {
  kPublic,
  kPrivate,      // 10/8, 172.16/12, 192.168/16: the router is behind another router (double NAT)
  kShared,       // 100.64/10: carrier-grade NAT
  kUnreachable,  // every other non-public class (0/8, 127/8, 169.254/16, 192.0.0/24, the
                 // documentation and benchmark ranges, multicast, 240/4, broadcast)
};
// `address` in host byte order (192.168.1.20 is 0xC0A80114).
AddressClass ClassifyExternalAddress(uint32_t address);
// The line Host shows for a non-public class; empty for kPublic.
std::string AddressClassMessage(AddressClass c);

// --- NAT-PMP, read-only --------------------------------------------------------------------------

constexpr size_t kNatPmpAddressRequestSize = 2;
constexpr size_t kNatPmpAddressAnswerSize = 12;
// Version 0, opcode 0: "what is your external address".
std::array<uint8_t, kNatPmpAddressRequestSize> NatPmpExternalAddressRequest();
// The guard every NAT-PMP send goes through: true only for exactly the opcode-0 request above, so a
// mapping request (opcode 1 or 2) can never leave the host.
bool NatPmpMaySend(std::span<const uint8_t> datagram);

struct NatPmpAddressAnswer {
  uint16_t result = 0;     // 0 = success
  uint32_t epoch_s = 0;    // seconds since the router's mapping table started
  uint32_t address = 0;    // host byte order
};
// The router's answer: version 0, opcode 128, big-endian result, epoch and address. nullopt for any
// other size, version or opcode.
std::optional<NatPmpAddressAnswer> ParseNatPmpExternalAddress(std::span<const uint8_t> datagram);

// --- UPnP IGD ------------------------------------------------------------------------------------

// UPnP error codes the rules name.
constexpr int kUpnpOk = 0;
constexpr int kUpnpNoSuchEntry = 714;
constexpr int kUpnpConflictInMappingEntry = 718;
constexpr int kUpnpConflictWithOtherMechanisms = 729;
constexpr int kUpnpNoRouter = -1;  // the client's own: no IGD answered

struct PortMappingEntry {
  std::string protocol;         // "UDP"
  uint16_t external_port = 0;
  std::string internal_client;  // dotted IPv4
  uint16_t internal_port = 0;
  std::string description;
  uint32_t lease_s = 0;
  bool enabled = true;
};

class IgdClient {
 public:
  virtual ~IgdClient() = default;
  virtual int Version() const = 0;  // 1 or 2 (WANIPConnection:1 or :2)
  // kUpnpOk with `out` filled, kUpnpNoSuchEntry, or another error.
  virtual int GetSpecificPortMappingEntry(uint16_t external_port, const std::string& protocol,
                                          PortMappingEntry* out) = 0;
  virtual int AddPortMapping(const PortMappingEntry& entry) = 0;
  // IGD v2: the router reserves a free external port (`entry.external_port` is the preference).
  virtual int AddAnyPortMapping(const PortMappingEntry& entry, uint16_t* reserved_port) = 0;
  virtual int DeletePortMapping(uint16_t external_port, const std::string& protocol) = 0;
  virtual int GetExternalIPAddress(uint32_t* address) = 0;
  // The host's address on the interface that reached the router during discovery (dotted IPv4), the
  // internal client PortMapper asks for; empty when unknown. The system client's threads and bounds:
  // igd_system_detail.h.
  virtual std::string LanAddress() const { return {}; }
};

constexpr uint32_t kMappingLeaseS = 3600;
constexpr int kIgdV1Candidates = 9;  // the configured port and up to 8 above it
constexpr int kIgdV2Attempts = 3;

enum class MappingOutcome {
  kMapped,               // "port mapped (external <addr>:<port>)"
  kRouterRefused,        // "the router refused the mapping (<code>)"
  kTakenByAnotherDevice, // "the port is taken on the router by another device"
  kNatPmpOnly,           // "your router offers only NAT-PMP, which this version does not use ..."
  kNoUpnp,               // "no router answered UPnP"
};

struct MappingResult {
  MappingOutcome outcome = MappingOutcome::kNoUpnp;
  int upnp_error = 0;           // the last add's error, for kRouterRefused
  uint16_t external_port = 0;   // the router's answer, not the request
  uint32_t external_address = 0;
  std::string message;          // the line Host shows (and logs)
};

class PortMapper {
 public:
  // `igd` may be null (no router answered UPnP); `natpmp_answered` says whether the router answered
  // NAT-PMP's address request (for the message). `nonce` is fresh per session from the OS random
  // source; `internal_client` the host's address on the interface that reaches the router.
  PortMapper(IgdClient* igd, std::string internal_client, uint16_t port, std::string nonce,
             bool natpmp_answered = false);

  MappingResult Map();
  // Renewal at half the lease: re-reads the owned entry and renews it only if it still carries the
  // owned tuple and nonce. False (and not owned any more) otherwise: "the router dropped the port
  // mapping: new players may not reach you".
  bool Renew();
  // At End session and normal exit: deletes the entry only if a re-read still shows it is ours.
  bool Remove();

  bool owned() const { return owned_; }
  uint16_t owned_port() const { return owned_port_; }
  const std::string& description() const { return description_; }
  // The tuple this session asks for, on `external_port`.
  PortMappingEntry Wanted(uint16_t external_port) const;
  // True when `e` is exactly the owned tuple with this session's nonce.
  bool IsOurs(const PortMappingEntry& e, uint16_t external_port) const;

 private:
  bool OwnedAfterAdd(uint16_t external_port);
  MappingResult Mapped(uint16_t external_port);

  IgdClient* igd_;
  std::string internal_client_;
  uint16_t port_;
  std::string description_;
  bool natpmp_answered_;
  bool owned_ = false;
  uint16_t owned_port_ = 0;
};

std::string MappingMessage(const MappingResult& r);
constexpr const char* kMappingDroppedMessage =
    "the router dropped the port mapping: new players may not reach you";

// The owned mapping's life in one "Internet" session (stage 8 item 4, design 3.4): Start maps once
// (PortMapper::Map), Tick renews at half the lease while the mapping is owned (a failed renewal
// says kMappingDroppedMessage once and the mapping is never touched again), Stop deletes it at End
// session and at the server's normal exit. A crash leaves it to its lease. Pure: the clock is
// passed in, the IGD calls go through `igd` (which the caller owns and keeps alive), so the unit
// test drives it on fake time with a fake IgdClient. Not thread-safe: one thread (the server's
// mapping thread) calls all of it.
class MappingLease {
 public:
  MappingLease(IgdClient* igd, std::string internal_client, uint16_t port, std::string nonce,
               bool natpmp_answered = false);
  MappingResult Start(TimePoint now);
  // Renews when due; returns the line to show when the renewal failed (once), else empty.
  std::string Tick(TimePoint now);
  // Deletes the owned mapping (after the re-read); true if it was deleted. Idempotent.
  bool Stop();
  bool owned() const { return mapper_.owned(); }
  // When the next renewal is due (only meaningful while owned()).
  TimePoint NextRenewal() const { return next_renewal_; }
  const PortMapper& mapper() const { return mapper_; }

 private:
  PortMapper mapper_;
  TimePoint next_renewal_{};
};

}  // namespace skylanders::netplay
