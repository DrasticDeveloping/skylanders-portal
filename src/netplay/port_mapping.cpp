#include "netplay/port_mapping.h"

#include <utility>

namespace skylanders::netplay {

namespace {

constexpr bool InNet(uint32_t a, uint32_t net, int prefix) {
  const uint32_t mask = prefix == 0 ? 0 : ~uint32_t(0) << (32 - prefix);
  return (a & mask) == (net & mask);
}

constexpr uint32_t Ip(uint32_t a, uint32_t b, uint32_t c, uint32_t d) {
  return (a << 24) | (b << 16) | (c << 8) | d;
}

std::string Dotted(uint32_t a) {
  return std::to_string(a >> 24) + "." + std::to_string((a >> 16) & 0xFF) + "." +
         std::to_string((a >> 8) & 0xFF) + "." + std::to_string(a & 0xFF);
}

uint32_t Be32(std::span<const uint8_t> d, size_t at) {
  return (uint32_t(d[at]) << 24) | (uint32_t(d[at + 1]) << 16) | (uint32_t(d[at + 2]) << 8) |
         uint32_t(d[at + 3]);
}

bool IsConflict(int rc) {
  return rc == kUpnpConflictInMappingEntry || rc == kUpnpConflictWithOtherMechanisms;
}

}  // namespace

AddressClass ClassifyExternalAddress(uint32_t a) {
  if (InNet(a, Ip(10, 0, 0, 0), 8) || InNet(a, Ip(172, 16, 0, 0), 12) ||
      InNet(a, Ip(192, 168, 0, 0), 16)) {
    return AddressClass::kPrivate;
  }
  if (InNet(a, Ip(100, 64, 0, 0), 10)) return AddressClass::kShared;
  struct Net {
    uint32_t net;
    int prefix;
  };
  static constexpr Net kUnreachable[] = {
      {Ip(0, 0, 0, 0), 8},       {Ip(127, 0, 0, 0), 8},     {Ip(169, 254, 0, 0), 16},
      {Ip(192, 0, 0, 0), 24},    {Ip(192, 0, 2, 0), 24},    {Ip(198, 18, 0, 0), 15},
      {Ip(198, 51, 100, 0), 24}, {Ip(203, 0, 113, 0), 24},  {Ip(224, 0, 0, 0), 4},
      {Ip(240, 0, 0, 0), 4},     {Ip(255, 255, 255, 255), 32},
  };
  for (const Net& n : kUnreachable) {
    if (InNet(a, n.net, n.prefix)) return AddressClass::kUnreachable;
  }
  return AddressClass::kPublic;
}

std::string AddressClassMessage(AddressClass c) {
  constexpr const char* kAdvice =
      ": internet hosting cannot work from here; forward the port on the outer router yourself, or "
      "play over the local network";
  switch (c) {
    case AddressClass::kPublic:
      return {};
    case AddressClass::kPrivate:
      return std::string("your router is behind another router (double NAT)") + kAdvice;
    case AddressClass::kShared:
      return std::string("your provider shares one address between customers (carrier-grade NAT)") +
             kAdvice;
    case AddressClass::kUnreachable:
      return std::string("the router reports an address the internet cannot reach") + kAdvice;
  }
  return {};
}

std::array<uint8_t, kNatPmpAddressRequestSize> NatPmpExternalAddressRequest() { return {0, 0}; }

bool NatPmpMaySend(std::span<const uint8_t> d) {
  return d.size() == kNatPmpAddressRequestSize && d[0] == 0 && d[1] == 0;
}

std::optional<NatPmpAddressAnswer> ParseNatPmpExternalAddress(std::span<const uint8_t> d) {
  if (d.size() != kNatPmpAddressAnswerSize || d[0] != 0 || d[1] != 128) return std::nullopt;
  NatPmpAddressAnswer a;
  a.result = uint16_t((d[2] << 8) | d[3]);
  a.epoch_s = Be32(d, 4);
  a.address = Be32(d, 8);
  return a;
}

PortMapper::PortMapper(IgdClient* igd, std::string internal_client, uint16_t port, std::string nonce,
                       bool natpmp_answered)
    : igd_(igd),
      internal_client_(std::move(internal_client)),
      port_(port),
      description_("Swap Force Online " + nonce),
      natpmp_answered_(natpmp_answered) {}

PortMappingEntry PortMapper::Wanted(uint16_t external_port) const {
  PortMappingEntry e;
  e.protocol = "UDP";
  e.external_port = external_port;
  e.internal_client = internal_client_;
  e.internal_port = port_;
  e.description = description_;
  e.lease_s = kMappingLeaseS;
  e.enabled = true;
  return e;
}

bool PortMapper::IsOurs(const PortMappingEntry& e, uint16_t external_port) const {
  return e.protocol == "UDP" && e.external_port == external_port &&
         e.internal_client == internal_client_ && e.internal_port == port_ &&
         e.description == description_;
}

bool PortMapper::OwnedAfterAdd(uint16_t external_port) {
  PortMappingEntry got;
  if (igd_->GetSpecificPortMappingEntry(external_port, "UDP", &got) != kUpnpOk) return false;
  // Some routers leave the external port out of the answer to a lookup by that port.
  if (got.external_port == 0) got.external_port = external_port;
  return IsOurs(got, external_port);
}

MappingResult PortMapper::Mapped(uint16_t external_port) {
  owned_ = true;
  owned_port_ = external_port;
  MappingResult r;
  r.outcome = MappingOutcome::kMapped;
  r.external_port = external_port;
  uint32_t address = 0;
  if (igd_->GetExternalIPAddress(&address) == kUpnpOk) r.external_address = address;
  r.message = MappingMessage(r);
  return r;
}

MappingResult PortMapper::Map() {
  MappingResult r;
  if (igd_ == nullptr) {
    r.outcome = natpmp_answered_ ? MappingOutcome::kNatPmpOnly : MappingOutcome::kNoUpnp;
    r.message = MappingMessage(r);
    return r;
  }
  int last_error = kUpnpOk;
  bool taken = false;
  if (igd_->Version() >= 2) {
    // IGD v2: only AddAnyPortMapping, which never changes an existing entry.
    uint32_t preferred = port_;
    for (int attempt = 0; attempt < kIgdV2Attempts && preferred <= 0xFFFF; ++attempt) {
      uint16_t reserved = 0;
      const int rc = igd_->AddAnyPortMapping(Wanted(uint16_t(preferred)), &reserved);
      if (rc != kUpnpOk || reserved == 0) {
        last_error = rc != kUpnpOk ? rc : kUpnpConflictInMappingEntry;
        taken = taken || IsConflict(last_error);
        break;  // the router refuses this request; asking again the same way changes nothing
      }
      if (OwnedAfterAdd(reserved)) return Mapped(reserved);
      taken = true;  // the re-read shows another entry: a conflict, never renewed or deleted
      preferred = uint32_t(reserved) + 1;
    }
  } else {
    for (int k = 0; k < kIgdV1Candidates; ++k) {
      const uint32_t p = uint32_t(port_) + uint32_t(k);
      if (p > 0xFFFF) break;
      PortMappingEntry existing;
      const int read = igd_->GetSpecificPortMappingEntry(uint16_t(p), "UDP", &existing);
      if (read == kUpnpOk) {
        taken = true;  // a pre-existing entry, an earlier session's leftover included: left alone
        continue;
      }
      if (read != kUpnpNoSuchEntry) {
        last_error = read;  // the port cannot be shown free: not touched
        continue;
      }
      const int rc = igd_->AddPortMapping(Wanted(uint16_t(p)));
      if (rc != kUpnpOk) {
        // No delete, no overwrite, no second add on this external port.
        last_error = rc;
        taken = taken || IsConflict(rc);
        continue;
      }
      if (OwnedAfterAdd(uint16_t(p))) return Mapped(uint16_t(p));
      taken = true;
    }
  }
  r.upnp_error = last_error;
  r.outcome = (taken && (last_error == kUpnpOk || IsConflict(last_error)))
                  ? MappingOutcome::kTakenByAnotherDevice
                  : MappingOutcome::kRouterRefused;
  r.message = MappingMessage(r);
  return r;
}

bool PortMapper::Renew() {
  if (!owned_ || igd_ == nullptr) return false;
  if (!OwnedAfterAdd(owned_port_)) {
    owned_ = false;  // gone or replaced: never touched again
    return false;
  }
  if (igd_->Version() >= 2) {
    uint16_t reserved = 0;
    if (igd_->AddAnyPortMapping(Wanted(owned_port_), &reserved) != kUpnpOk || reserved == 0) {
      owned_ = false;
      return false;
    }
    if (reserved != owned_port_) {
      // The router moved us: the new entry is ours only if the re-read says so, and then it goes
      // (a code carries the old port); the old entry ends with its lease.
      if (OwnedAfterAdd(reserved)) igd_->DeletePortMapping(reserved, "UDP");
      owned_ = false;
      return false;
    }
  } else if (igd_->AddPortMapping(Wanted(owned_port_)) != kUpnpOk) {
    owned_ = false;
    return false;
  }
  if (!OwnedAfterAdd(owned_port_)) {
    owned_ = false;
    return false;
  }
  return true;
}

bool PortMapper::Remove() {
  if (!owned_ || igd_ == nullptr) return false;
  owned_ = false;
  if (!OwnedAfterAdd(owned_port_)) return false;  // no longer ours: left alone
  return igd_->DeletePortMapping(owned_port_, "UDP") == kUpnpOk;
}

std::string MappingMessage(const MappingResult& r) {
  switch (r.outcome) {
    case MappingOutcome::kMapped: {
      std::string m = "port mapped (external " +
                      (r.external_address ? Dotted(r.external_address) : std::string("unknown address")) +
                      ":" + std::to_string(r.external_port) + ")";
      const AddressClass c =
          r.external_address ? ClassifyExternalAddress(r.external_address) : AddressClass::kPublic;
      if (c != AddressClass::kPublic) return m + "; " + AddressClassMessage(c);
      return m + "; whether the internet reaches it can only be tested by someone joining";
    }
    case MappingOutcome::kRouterRefused:
      return "the router refused the mapping (" + std::to_string(r.upnp_error) + ")";
    case MappingOutcome::kTakenByAnotherDevice:
      return "the port is taken on the router by another device";
    case MappingOutcome::kNatPmpOnly:
      return "your router offers only NAT-PMP, which this version does not use for mappings: "
             "forward the port by hand";
    case MappingOutcome::kNoUpnp:
      return "no router answered UPnP";
  }
  return {};
}

}  // namespace skylanders::netplay
