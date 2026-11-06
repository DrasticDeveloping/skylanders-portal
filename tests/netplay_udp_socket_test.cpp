#include <chrono>
#include <cstdint>
#include <cstdio>
#include <thread>
#include <vector>

#include "netplay/udp_socket.h"
#include "test_util.h"

using namespace std::chrono_literals;
using skylanders::netplay::Endpoint;
using skylanders::netplay::kLoopback;
using skylanders::netplay::UdpSocket;

namespace {

std::vector<uint8_t> Bytes(size_t n) {
  std::vector<uint8_t> v(n);
  for (size_t i = 0; i < n; ++i) v[i] = uint8_t(i * 7 + 1);
  return v;
}

// Receive takes one datagram per call. Too long ones come back empty (so a caller counting calls
// counts them); the rest come back whole, in order. Only 127.0.0.1 is bound (no firewall prompt).
void OneDatagramPerReceive() {
  UdpSocket receiver, sender;
  if (!receiver.Open("127.0.0.1", 0) || !sender.Open("127.0.0.1", 0)) {
    std::fprintf(stderr, "loopback socket: %s%s\n", receiver.LastError().c_str(),
                 sender.LastError().c_str());
    CHECK(false);
    return;
  }
  const Endpoint to{kLoopback, receiver.LocalPort()};
  CHECK(sender.LocalEndpoint() == (Endpoint{kLoopback, sender.LocalPort()}));
  const size_t sizes[] = {4000, UdpSocket::kMaxReceive + 1, UdpSocket::kMaxReceive, 10};
  for (size_t n : sizes) CHECK(sender.SendTo(to, Bytes(n)));

  std::vector<std::vector<uint8_t>> got;
  std::vector<Endpoint> from_list;
  const auto end = std::chrono::steady_clock::now() + 2s;
  while (got.size() < 4 && std::chrono::steady_clock::now() < end) {
    std::vector<uint8_t> out(5, 0xEE);  // something to clear
    Endpoint from{1, 1};
    if (receiver.Receive(out, from)) {
      got.push_back(out);
      from_list.push_back(from);
    } else {
      CHECK(receiver.LastError().empty());
      std::this_thread::sleep_for(1ms);
    }
  }
  CHECK(got.size() == 4);
  if (got.size() == 4) {
    CHECK(got[0].empty());
    CHECK(got[1].empty());
    CHECK(got[2] == Bytes(UdpSocket::kMaxReceive));
    CHECK(got[3] == Bytes(10));
    CHECK(from_list[1] == sender.LocalEndpoint());  // a datagram that was taken names its sender
    CHECK(from_list[2] == sender.LocalEndpoint() && from_list[3] == sender.LocalEndpoint());
  }

  // Nothing else is waiting.
  bool more = false;
  const auto quiet = std::chrono::steady_clock::now() + 50ms;
  while (std::chrono::steady_clock::now() < quiet) {
    std::vector<uint8_t> out;
    Endpoint from;
    more |= receiver.Receive(out, from);
    std::this_thread::sleep_for(1ms);
  }
  CHECK(!more);
  CHECK(receiver.LastError().empty());
}

}  // namespace

int main() {
  OneDatagramPerReceive();
  return Finish("netplay_udp_socket");
}
