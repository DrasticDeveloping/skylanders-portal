#pragma once

#include <chrono>
#include <compare>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace skylanders::netplay {

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

// An IPv4 address and a UDP port. `address` is in host order: 127.0.0.1 is 0x7F000001.
struct Endpoint {
  uint32_t address = 0;
  uint16_t port = 0;

  auto operator<=>(const Endpoint&) const = default;
  // "a.b.c.d:port".
  std::string ToString() const;
};

// "a.b.c.d" to a host-order address; nullopt for anything else (no names, no IPv6).
std::optional<uint32_t> ParseIpv4(std::string_view text);
std::string FormatIpv4(uint32_t address);
constexpr uint32_t kLoopback = 0x7F000001;
constexpr bool IsLoopback(uint32_t address) { return (address >> 24) == 127; }

// Where datagrams go and come from: a UDP socket, or the fake network in tests.
class Transport {
 public:
  virtual ~Transport() = default;
  // Sends one datagram. false: it could not be handed to the network.
  virtual bool Send(const Endpoint& to, std::span<const uint8_t> bytes) = 0;
  // Takes one waiting datagram; false when nothing is waiting. Never blocks.
  virtual bool Receive(std::vector<uint8_t>& out, Endpoint& from) = 0;
  // The address and port this transport is bound to (0.0.0.0 when bound to every address).
  virtual Endpoint LocalEndpoint() const = 0;
};

}  // namespace skylanders::netplay
