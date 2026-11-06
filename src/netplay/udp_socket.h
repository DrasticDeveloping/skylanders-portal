#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "netplay/transport.h"

namespace skylanders::netplay {

// A non-blocking IPv4 UDP socket. One thread uses one socket; different sockets may be used from
// different threads. On Windows the first open socket starts Winsock (WSAStartup) and the last one
// to close stops it, under a process-wide lock (the only global state here).
class UdpSocket final : public Transport {
 public:
  // The largest datagram Receive delivers; a longer one is delivered empty.
  static constexpr size_t kMaxReceive = 2048;

  UdpSocket() = default;
  ~UdpSocket() override;
  UdpSocket(const UdpSocket&) = delete;
  UdpSocket& operator=(const UdpSocket&) = delete;
  UdpSocket(UdpSocket&& other) noexcept;
  UdpSocket& operator=(UdpSocket&& other) noexcept;

  // Binds to `bind_address` ("127.0.0.1", "0.0.0.0", ...) and `port` (0: any free port). Closes
  // a socket opened before. false on failure; LastError() says why.
  bool Open(std::string_view bind_address, uint16_t port);
  void Close();
  bool IsOpen() const;
  uint16_t LocalPort() const { return local_.port; }

  bool SendTo(const Endpoint& to, std::span<const uint8_t> bytes);
  bool SendTo(std::string_view address, uint16_t port, std::span<const uint8_t> bytes);
  // Takes at most one waiting datagram; false when nothing is waiting (or the socket failed:
  // LastError()). A datagram longer than kMaxReceive, and a "port unreachable" report for an
  // earlier send that the system turns into a receive error, come back as an empty datagram (true,
  // `out` empty, `from` the sender if the system named one, else 0.0.0.0:0), so a caller that
  // limits its calls per frame counts them too (Transport::Receive).
  bool Receive(std::vector<uint8_t>& out, Endpoint& from) override;

  bool Send(const Endpoint& to, std::span<const uint8_t> bytes) override { return SendTo(to, bytes); }
  Endpoint LocalEndpoint() const override { return local_; }
  const std::string& LastError() const { return error_; }

 private:
  void Fail(const char* what);

  intptr_t handle_ = -1;  // SOCKET on Windows, int elsewhere
  Endpoint local_;
  std::string error_;
};

// The address this machine would use to reach the internet (the interface of its default route),
// found without sending anything; nullopt without a route. A server bound to 0.0.0.0 puts it in
// its join code.
std::optional<uint32_t> PrimaryLocalIpv4();

}  // namespace skylanders::netplay
