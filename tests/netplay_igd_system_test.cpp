// The system IGD client's pure half (stage 8 item 4, design D8; src/netplay/igd_system_detail.h):
// miniupnpc's results as IgdClient results, the router's text answers, the requests the client
// refuses to send, and the bounded worker every call goes through. Nothing here touches a router or
// the network: MakeSystemIgdClient() is called only when the portal is built without miniupnpc,
// where it sends nothing.

#include <atomic>
#include <chrono>
#include <cstdint>
#include <future>
#include <thread>

#include "netplay/igd_system.h"
#include "netplay/igd_system_detail.h"
#include "netplay/port_mapping.h"
#include "test_util.h"

using namespace skylanders::netplay;
using namespace std::chrono_literals;

namespace {

void TestResults() {
  CHECK(igd::ResultFromMiniupnpc(0) == kUpnpOk);
  // The router's UPnP codes pass through unchanged: PortMapper's rules key on 714, 718 and 729.
  CHECK(igd::ResultFromMiniupnpc(714) == kUpnpNoSuchEntry);
  CHECK(igd::ResultFromMiniupnpc(718) == kUpnpConflictInMappingEntry);
  CHECK(igd::ResultFromMiniupnpc(729) == kUpnpConflictWithOtherMechanisms);
  CHECK(igd::ResultFromMiniupnpc(402) == 402);
  CHECK(igd::ResultFromMiniupnpc(501) == 501);
  CHECK(igd::ResultFromMiniupnpc(606) == 606);
  CHECK(igd::ResultFromMiniupnpc(728) == 728);
  // miniupnpc's own codes: -1 (unknown) is not "no router", -3 is no answer, -4 a bad answer.
  CHECK(igd::ResultFromMiniupnpc(-1) == igd::kClientError);
  CHECK(igd::ResultFromMiniupnpc(-1) != kUpnpNoRouter);
  CHECK(igd::ResultFromMiniupnpc(-2) == igd::kClientError);
  CHECK(igd::ResultFromMiniupnpc(-3) == igd::kNoAnswer);
  CHECK(igd::ResultFromMiniupnpc(-4) == igd::kBadAnswer);
  CHECK(igd::ResultFromMiniupnpc(-5) == igd::kClientError);
  CHECK(igd::ResultFromMiniupnpc(-101) == igd::kClientError);
  // The client's own results are negative, apart from each other, kUpnpOk and kUpnpNoRouter.
  const int own[] = {igd::kTimedOut, igd::kNoAnswer, igd::kBadAnswer, igd::kClientError};
  for (int i = 0; i < 4; ++i) {
    CHECK(own[i] < kUpnpNoRouter);
    for (int j = i + 1; j < 4; ++j) CHECK(own[i] != own[j]);
  }
}

void TestDiscovery() {
  CHECK(igd::VersionFromServiceType("urn:schemas-upnp-org:service:WANIPConnection:2") == 2);
  CHECK(igd::VersionFromServiceType("urn:schemas-upnp-org:service:WANIPConnection:1") == 1);
  CHECK(igd::VersionFromServiceType("urn:schemas-upnp-org:service:WANPPPConnection:1") == 1);
  CHECK(igd::VersionFromServiceType("") == 0);
  CHECK(igd::VersionFromServiceType("urn:schemas-upnp-org:service:WANIPConnection:3") == 0);
  CHECK(igd::VersionFromServiceType("urn:schemas-upnp-org:service:WANIPv6FirewallControl:1") == 0);
  CHECK(igd::VersionFromServiceType("urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1") == 0);

  CHECK(!igd::UsableIgd(0));  // no IGD
  CHECK(igd::UsableIgd(1));   // connected
  CHECK(igd::UsableIgd(2));   // connected, reserved external address
  CHECK(igd::UsableIgd(3));   // reports itself not connected
  CHECK(!igd::UsableIgd(4));  // a UPnP device that is no IGD
  CHECK(!igd::UsableIgd(-1)); // miniupnpc's memory error
}

PortMappingEntry Request() {
  PortMappingEntry e;
  e.protocol = "UDP";
  e.external_port = 50000;
  e.internal_client = "192.168.1.20";
  e.internal_port = 50000;
  e.description = "Swap Force Online 0123456789abcdef";
  e.lease_s = kMappingLeaseS;
  return e;
}

void TestRequests() {
  // IGD v2's plain AddPortMapping is never sent; AddAnyPortMapping only exists on v2.
  CHECK(igd::AddAllowed(1, false));
  CHECK(!igd::AddAllowed(1, true));
  CHECK(!igd::AddAllowed(2, false));
  CHECK(igd::AddAllowed(2, true));
  CHECK(!igd::AddAllowed(0, false));
  CHECK(!igd::AddAllowed(0, true));

  CHECK(igd::SoapSafe("Swap Force Online 0123456789abcdef"));
  CHECK(igd::SoapSafe(""));
  CHECK(!igd::SoapSafe("a<b"));
  CHECK(!igd::SoapSafe("a>b"));
  CHECK(!igd::SoapSafe("a&b"));
  CHECK(!igd::SoapSafe("a\"b"));
  CHECK(!igd::SoapSafe("a'b"));
  CHECK(!igd::SoapSafe("a\nb"));
  CHECK(!igd::SoapSafe("caf\xC3\xA9"));
  CHECK(igd::SoapSafe(std::string(255, 'x')));
  CHECK(!igd::SoapSafe(std::string(256, 'x')));

  CHECK(igd::ValidRequest(Request()));
  PortMappingEntry e = Request();
  e.protocol = "TCP";
  CHECK(igd::ValidRequest(e));
  e = Request();
  e.protocol = "udp";
  CHECK(!igd::ValidRequest(e));
  e = Request();
  e.external_port = 0;
  CHECK(!igd::ValidRequest(e));
  e = Request();
  e.internal_port = 0;
  CHECK(!igd::ValidRequest(e));
  e = Request();
  e.internal_client = "my-pc";
  CHECK(!igd::ValidRequest(e));
  e = Request();
  e.internal_client = "";
  CHECK(!igd::ValidRequest(e));
  e = Request();
  e.description = "Swap Force Online <x>";
  CHECK(!igd::ValidRequest(e));
  e = Request();
  e.lease_s = 604800;
  CHECK(igd::ValidRequest(e));
  e.lease_s = 604801;
  CHECK(!igd::ValidRequest(e));

  CHECK(igd::DecimalText(50000) == "50000");
  CHECK(igd::DecimalText(0) == "0");
  CHECK(igd::DecimalText(kMappingLeaseS) == "3600");
}

void TestAnswers() {
  CHECK(igd::ParseIpv4("192.168.1.20") == 0xC0A80114u);
  CHECK(igd::ParseIpv4("0.0.0.0") == 0u);
  CHECK(igd::ParseIpv4("255.255.255.255") == 0xFFFFFFFFu);
  CHECK(igd::ParseIpv4("203.0.113.7") == 0xCB007107u);
  CHECK(igd::ParseIpv4("010.001.002.003") == 0x0A010203u);  // decimal, never octal
  CHECK(!igd::ParseIpv4(""));
  CHECK(!igd::ParseIpv4("256.1.1.1"));
  CHECK(!igd::ParseIpv4("1.2.3"));
  CHECK(!igd::ParseIpv4("1.2.3.4.5"));
  CHECK(!igd::ParseIpv4("1.2.3."));
  CHECK(!igd::ParseIpv4(".1.2.3"));
  CHECK(!igd::ParseIpv4("1..2.3"));
  CHECK(!igd::ParseIpv4(" 1.2.3.4"));
  CHECK(!igd::ParseIpv4("1.2.3.4 "));
  CHECK(!igd::ParseIpv4("1.2.3.-4"));
  CHECK(!igd::ParseIpv4("1.2.3.0004"));
  CHECK(!igd::ParseIpv4("a.b.c.d"));
  CHECK(!igd::ParseIpv4("::1"));

  CHECK(igd::ParsePort("50000") == uint16_t(50000));
  CHECK(igd::ParsePort("1") == uint16_t(1));
  CHECK(igd::ParsePort("65535") == uint16_t(65535));
  CHECK(!igd::ParsePort("0"));
  CHECK(!igd::ParsePort("65536"));
  CHECK(!igd::ParsePort("99999"));
  CHECK(!igd::ParsePort("123456"));
  CHECK(!igd::ParsePort(""));
  CHECK(!igd::ParsePort("+1"));
  CHECK(!igd::ParsePort("1a"));
  CHECK(!igd::ParsePort(" 1"));

  auto e = igd::EntryFromAnswer(50001, "UDP", "192.168.1.20", "50000", "Swap Force Online 01", "1", "3599");
  CHECK(e.has_value());
  if (e) {
    CHECK(e->protocol == "UDP");
    CHECK(e->external_port == 50001);  // from the request: the answer carries none
    CHECK(e->internal_client == "192.168.1.20");
    CHECK(e->internal_port == 50000);
    CHECK(e->description == "Swap Force Online 01");
    CHECK(e->lease_s == 3599);
    CHECK(e->enabled);
  }
  e = igd::EntryFromAnswer(50000, "UDP", "192.168.1.30", "50000", "", "0", "");
  CHECK(e.has_value());
  if (e) {
    CHECK(!e->enabled);
    CHECK(e->lease_s == 0);
    CHECK(e->description.empty());
  }
  e = igd::EntryFromAnswer(50000, "UDP", "192.168.1.30", "50000", "x", "1", "forever");
  CHECK(e.has_value() && e->lease_s == 0);
  e = igd::EntryFromAnswer(50000, "UDP", "192.168.1.30", "50000", "x", "1", "99999999999");
  CHECK(e.has_value() && e->lease_s == 0);
  // An answer without a readable client or port is no entry (the call says kBadAnswer).
  CHECK(!igd::EntryFromAnswer(50000, "UDP", "", "50000", "x", "1", "0"));
  CHECK(!igd::EntryFromAnswer(50000, "UDP", "192.168.1.20", "", "x", "1", "0"));
  CHECK(!igd::EntryFromAnswer(50000, "UDP", "192.168.1.20", "0", "x", "1", "0"));
  CHECK(!igd::EntryFromAnswer(50000, "UDP", "router", "50000", "x", "1", "0"));
}

// The bounds: a call that runs past its bound is abandoned and later calls fail at once until it
// returns; a call whose bound passes before it starts never runs. No sleeps decide anything: the slow
// job waits on a promise the test keeps.
void TestWorker() {
  {
    igd::BoundedWorker w;
    CHECK(w.Run([] { return 7; }, 2000ms) == 7);
    CHECK(w.Run([] { return kUpnpNoSuchEntry; }, 2000ms) == kUpnpNoSuchEntry);
  }
  {
    igd::BoundedWorker w;
    auto release = std::make_shared<std::promise<void>>();
    std::shared_future<void> released = release->get_future().share();
    auto started = std::make_shared<std::atomic<bool>>(false);
    auto later_ran = std::make_shared<std::atomic<int>>(0);
    // The worker takes the call at once (it is idle), so the bound passes while the call runs.
    CHECK(w.Run(
              [released, started] {
                started->store(true);
                released.wait();
                return 1;
              },
              200ms) == igd::kTimedOut);
    const auto start_deadline = std::chrono::steady_clock::now() + 5000ms;
    while (!started->load() && std::chrono::steady_clock::now() < start_deadline) std::this_thread::yield();
    CHECK(started->load());
    // The abandoned call is still running: the next call is refused at once and never runs.
    const auto t0 = std::chrono::steady_clock::now();
    CHECK(w.Run([later_ran] { return ++*later_ran; }, 5000ms) == igd::kTimedOut);
    CHECK(std::chrono::steady_clock::now() - t0 < 2000ms);
    release->set_value();
    // Once the abandoned call has returned, calls run again.
    int rc = igd::kTimedOut;
    const auto deadline = std::chrono::steady_clock::now() + 5000ms;
    while (rc == igd::kTimedOut && std::chrono::steady_clock::now() < deadline) {
      rc = w.Run([] { return 3; }, 1000ms);
      if (rc == igd::kTimedOut) std::this_thread::sleep_for(5ms);
    }
    CHECK(rc == 3);
    CHECK(later_ran->load() == 0);
  }
  {
    // A call queued behind a running one (from another thread) whose bound passes first never runs.
    igd::BoundedWorker w;
    auto release = std::make_shared<std::promise<void>>();
    std::shared_future<void> released = release->get_future().share();
    auto started = std::make_shared<std::atomic<bool>>(false);
    auto queued_ran = std::make_shared<std::atomic<int>>(0);
    int slow_rc = 0;
    std::thread slow([&] {
      slow_rc = w.Run(
          [released, started] {
            started->store(true);
            released.wait();
            return 11;
          },
          10000ms);
    });
    const auto deadline = std::chrono::steady_clock::now() + 5000ms;
    while (!started->load() && std::chrono::steady_clock::now() < deadline) std::this_thread::yield();
    CHECK(started->load());
    CHECK(w.Run([queued_ran] { return ++*queued_ran; }, 30ms) == igd::kTimedOut);
    release->set_value();
    slow.join();
    CHECK(slow_rc == 11);
    CHECK(w.Run([] { return 12; }, 2000ms) == 12);  // runs after the dropped one would have
    CHECK(queued_ran->load() == 0);
  }
  {
    // Destroying the worker never waits for a running call.
    auto release = std::make_shared<std::promise<void>>();
    std::shared_future<void> released = release->get_future().share();
    const auto t0 = std::chrono::steady_clock::now();
    {
      igd::BoundedWorker w;
      CHECK(w.Run([released] {
                    released.wait();
                    return 0;
                  },
                  20ms) == igd::kTimedOut);
    }
    CHECK(std::chrono::steady_clock::now() - t0 < 2000ms);
    release->set_value();  // the detached worker finishes its call and ends
  }
}

void TestWithoutMiniupnpc() {
  // Only without miniupnpc: with it, MakeSystemIgdClient() would search the network for a router.
  if (!igd::SystemIgdBuiltIn()) CHECK(MakeSystemIgdClient() == nullptr);
}

}  // namespace

int main() {
  TestResults();
  TestDiscovery();
  TestRequests();
  TestAnswers();
  TestWorker();
  TestWithoutMiniupnpc();
  return Finish("netplay igd system");
}
