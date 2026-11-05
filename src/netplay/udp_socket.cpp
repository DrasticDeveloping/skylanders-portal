#include "netplay/udp_socket.h"

#include <cstdio>
#include <cstring>
#include <mutex>
#include <utility>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <mstcpip.h>
#ifndef SIO_UDP_CONNRESET
#define SIO_UDP_CONNRESET _WSAIOW(IOC_VENDOR, 12)  // the documented value (mstcpip.h, newer SDKs)
#endif
#else
#include <arpa/inet.h>
#include <cerrno>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace skylanders::netplay {

namespace {

#ifdef _WIN32
using NativeSocket = SOCKET;
constexpr intptr_t kInvalid = intptr_t(INVALID_SOCKET);
int LastSocketError() { return WSAGetLastError(); }
bool WouldBlock(int e) { return e == WSAEWOULDBLOCK; }
void CloseNative(intptr_t h) { closesocket(NativeSocket(h)); }

// Winsock is started by the first socket and stopped by the last one.
std::mutex g_wsa_mutex;
int g_wsa_users = 0;

bool AcquireWinsock() {
  std::lock_guard lock(g_wsa_mutex);
  if (g_wsa_users == 0) {
    WSADATA data;
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) return false;
  }
  ++g_wsa_users;
  return true;
}

void ReleaseWinsock() {
  std::lock_guard lock(g_wsa_mutex);
  if (g_wsa_users > 0 && --g_wsa_users == 0) WSACleanup();
}
#else
using NativeSocket = int;
constexpr intptr_t kInvalid = -1;
int LastSocketError() { return errno; }
bool WouldBlock(int e) { return e == EAGAIN || e == EWOULDBLOCK; }
void CloseNative(intptr_t h) { close(int(h)); }
bool AcquireWinsock() { return true; }
void ReleaseWinsock() {}
#endif

sockaddr_in ToSockaddr(const Endpoint& e) {
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_addr.s_addr = htonl(e.address);
  a.sin_port = htons(e.port);
  return a;
}

Endpoint FromSockaddr(const sockaddr_in& a) {
  return Endpoint{ntohl(a.sin_addr.s_addr), ntohs(a.sin_port)};
}

}  // namespace

std::string Endpoint::ToString() const { return FormatIpv4(address) + ":" + std::to_string(port); }

std::optional<uint32_t> ParseIpv4(std::string_view text) {
  uint32_t result = 0;
  int parts = 0;
  size_t i = 0;
  while (parts < 4) {
    if (i >= text.size() || text[i] < '0' || text[i] > '9') return std::nullopt;
    uint32_t v = 0;
    size_t digits = 0;
    while (i < text.size() && text[i] >= '0' && text[i] <= '9') {
      v = v * 10 + uint32_t(text[i] - '0');
      if (++digits > 3 || v > 255) return std::nullopt;
      ++i;
    }
    result = (result << 8) | v;
    if (++parts < 4) {
      if (i >= text.size() || text[i] != '.') return std::nullopt;
      ++i;
    }
  }
  if (i != text.size()) return std::nullopt;
  return result;
}

std::string FormatIpv4(uint32_t a) {
  char buf[16];
  std::snprintf(buf, sizeof(buf), "%u.%u.%u.%u", (a >> 24) & 255, (a >> 16) & 255, (a >> 8) & 255,
                a & 255);
  return buf;
}

UdpSocket::~UdpSocket() { Close(); }

UdpSocket::UdpSocket(UdpSocket&& other) noexcept
    : handle_(std::exchange(other.handle_, kInvalid)),
      local_(std::exchange(other.local_, {})),
      error_(std::move(other.error_)) {}

UdpSocket& UdpSocket::operator=(UdpSocket&& other) noexcept {
  if (this != &other) {
    Close();
    handle_ = std::exchange(other.handle_, kInvalid);
    local_ = std::exchange(other.local_, {});
    error_ = std::move(other.error_);
  }
  return *this;
}

bool UdpSocket::IsOpen() const { return handle_ != kInvalid; }

void UdpSocket::Fail(const char* what) {
  error_ = std::string(what) + " failed (error " + std::to_string(LastSocketError()) + ")";
}

bool UdpSocket::Open(std::string_view bind_address, uint16_t port) {
  Close();
  auto address = ParseIpv4(bind_address);
  if (!address) {
    error_ = "not an IPv4 address: " + std::string(bind_address);
    return false;
  }
  if (!AcquireWinsock()) {
    error_ = "WSAStartup failed";
    return false;
  }
  NativeSocket s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (intptr_t(s) == kInvalid) {
    Fail("socket");
    ReleaseWinsock();
    return false;
  }
  handle_ = intptr_t(s);

#ifdef _WIN32
  u_long nonblocking = 1;
  bool ok = ioctlsocket(s, FIONBIO, &nonblocking) == 0;
  // Without this, an ICMP "port unreachable" for an earlier send makes the next recvfrom fail
  // with WSAECONNRESET (a peer that went away would look like a broken socket).
  BOOL report = FALSE;
  DWORD returned = 0;
  WSAIoctl(s, SIO_UDP_CONNRESET, &report, sizeof(report), nullptr, 0, &returned, nullptr, nullptr);
#else
  int flags = fcntl(s, F_GETFL, 0);
  bool ok = flags >= 0 && fcntl(s, F_SETFL, flags | O_NONBLOCK) == 0;
#endif
  if (!ok) {
    Fail("making the socket non-blocking");
    Close();
    return false;
  }

  sockaddr_in a = ToSockaddr(Endpoint{*address, port});
  if (bind(s, reinterpret_cast<const sockaddr*>(&a), sizeof(a)) != 0) {
    Fail("bind");
    Close();
    return false;
  }
  sockaddr_in bound{};
#ifdef _WIN32
  int len = sizeof(bound);
#else
  socklen_t len = sizeof(bound);
#endif
  if (getsockname(s, reinterpret_cast<sockaddr*>(&bound), &len) != 0) {
    Fail("getsockname");
    Close();
    return false;
  }
  local_ = FromSockaddr(bound);
  error_.clear();
  return true;
}

void UdpSocket::Close() {
  if (handle_ == kInvalid) return;
  CloseNative(handle_);
  handle_ = kInvalid;
  local_ = {};
  ReleaseWinsock();
}

bool UdpSocket::SendTo(const Endpoint& to, std::span<const uint8_t> bytes) {
  if (!IsOpen()) return false;
  sockaddr_in a = ToSockaddr(to);
  auto sent = sendto(NativeSocket(handle_), reinterpret_cast<const char*>(bytes.data()),
                     int(bytes.size()), 0, reinterpret_cast<const sockaddr*>(&a), sizeof(a));
  if (sent < 0 || size_t(sent) != bytes.size()) {
    Fail("sendto");
    return false;
  }
  return true;
}

bool UdpSocket::SendTo(std::string_view address, uint16_t port, std::span<const uint8_t> bytes) {
  auto a = ParseIpv4(address);
  if (!a) {
    error_ = "not an IPv4 address: " + std::string(address);
    return false;
  }
  return SendTo(Endpoint{*a, port}, bytes);
}

bool UdpSocket::Receive(std::vector<uint8_t>& out, Endpoint& from) {
  if (!IsOpen()) return false;
  uint8_t buf[kMaxReceive + 1];  // one byte more: a datagram that fills it was too long
  for (;;) {
    sockaddr_in a{};
#ifdef _WIN32
    int len = sizeof(a);
#else
    socklen_t len = sizeof(a);
#endif
    auto got = recvfrom(NativeSocket(handle_), reinterpret_cast<char*>(buf), int(sizeof(buf)), 0,
                        reinterpret_cast<sockaddr*>(&a), &len);
    if (got >= 0) {
      if (size_t(got) > kMaxReceive) continue;  // too long (POSIX truncates it silently)
      out.assign(buf, buf + got);
      from = FromSockaddr(a);
      return true;
    }
    const int e = LastSocketError();
    if (WouldBlock(e)) return false;
#ifdef _WIN32
    if (e == WSAECONNRESET || e == WSAEMSGSIZE) continue;  // a stale ICMP report, a long datagram
#else
    if (e == ECONNREFUSED || e == EINTR) continue;
#endif
    Fail("recvfrom");
    return false;
  }
}

std::optional<uint32_t> PrimaryLocalIpv4() {
  if (!AcquireWinsock()) return std::nullopt;
  std::optional<uint32_t> result;
  NativeSocket s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (intptr_t(s) != kInvalid) {
    // Connecting a UDP socket only picks a route; nothing is sent. 203.0.113.1 is a documentation
    // address (TEST-NET-3), so the default route is the one chosen.
    sockaddr_in to = ToSockaddr(Endpoint{0xCB007101, 9});
    if (connect(s, reinterpret_cast<const sockaddr*>(&to), sizeof(to)) == 0) {
      sockaddr_in local{};
#ifdef _WIN32
      int len = sizeof(local);
#else
      socklen_t len = sizeof(local);
#endif
      if (getsockname(s, reinterpret_cast<sockaddr*>(&local), &len) == 0) {
        uint32_t a = ntohl(local.sin_addr.s_addr);
        if (a != 0) result = a;
      }
    }
    CloseNative(intptr_t(s));
  }
  ReleaseWinsock();
  return result;
}

}  // namespace skylanders::netplay
