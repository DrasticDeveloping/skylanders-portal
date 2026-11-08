// The system IGD client (stage 8 item 4, design D8): IgdClient on miniupnpc, when the portal is built
// with it (the CMake option SKYLANDERS_PORTAL_MINIUPNPC, thirdparty/miniupnp). The threads, the bounds
// and the results beyond the router's UPnP codes are in igd_system_detail.h; the mapping rules (what
// is read, added, renewed and deleted, and when) are PortMapper's (port_mapping.h).

#include "netplay/igd_system.h"

#include <condition_variable>
#include <deque>
#include <mutex>
#include <system_error>
#include <thread>
#include <utility>

#include "netplay/igd_system_detail.h"

#if defined(SKYLANDERS_PORTAL_MINIUPNPC)
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#endif
#include <miniupnpc.h>
#include <upnpcommands.h>
#endif

namespace skylanders::netplay {

namespace igd {

// --- the pure half --------------------------------------------------------------------------------

namespace {

// miniupnpc's own codes (upnpcommands.h), restated so this half needs no miniupnpc.
constexpr int kMiniupnpcSuccess = 0;
constexpr int kMiniupnpcHttpError = -3;
constexpr int kMiniupnpcInvalidResponse = -4;

// UPNP_GetValidIGD's results (miniupnpc.h).
constexpr int kConnectedIgd = 1;
constexpr int kPrivateIpIgd = 2;
constexpr int kDisconnectedIgd = 3;

constexpr uint32_t kMaxLeaseS = 604800;

// Digits only (no sign, no space), at most `max_digits` of them (at most 10, so the sum fits).
std::optional<uint32_t> ParseDecimal(std::string_view s, size_t max_digits, uint32_t max_value) {
  if (s.empty() || s.size() > max_digits) return std::nullopt;
  uint64_t v = 0;
  for (char c : s) {
    if (c < '0' || c > '9') return std::nullopt;
    v = v * 10 + uint64_t(c - '0');
  }
  if (v > max_value) return std::nullopt;
  return uint32_t(v);
}

}  // namespace

int ResultFromMiniupnpc(int rc) {
  if (rc == kMiniupnpcSuccess) return kUpnpOk;
  if (rc > 0) return rc;  // the router's UPnP error code
  if (rc == kMiniupnpcHttpError) return kNoAnswer;
  if (rc == kMiniupnpcInvalidResponse) return kBadAnswer;
  return kClientError;  // unknown error (-1, which is not kUpnpNoRouter here), invalid args, memory
}

int VersionFromServiceType(std::string_view t) {
  if (t == "urn:schemas-upnp-org:service:WANIPConnection:2") return 2;
  if (t == "urn:schemas-upnp-org:service:WANIPConnection:1" ||
      t == "urn:schemas-upnp-org:service:WANPPPConnection:1") {
    return 1;
  }
  return 0;
}

bool UsableIgd(int r) { return r == kConnectedIgd || r == kPrivateIpIgd || r == kDisconnectedIgd; }

bool AddAllowed(int version, bool any_port) { return any_port ? version == 2 : version == 1; }

bool SoapSafe(std::string_view text) {
  if (text.size() > 255) return false;
  for (char c : text) {
    if (c < 0x20 || c > 0x7E || c == '<' || c == '>' || c == '&' || c == '"' || c == '\'') return false;
  }
  return true;
}

bool ValidRequest(const PortMappingEntry& e) {
  return (e.protocol == "UDP" || e.protocol == "TCP") && e.external_port != 0 && e.internal_port != 0 &&
         ParseIpv4(e.internal_client).has_value() && SoapSafe(e.description) && e.lease_s <= kMaxLeaseS;
}

std::string DecimalText(uint32_t value) { return std::to_string(value); }

std::optional<uint32_t> ParseIpv4(std::string_view s) {
  uint32_t address = 0;
  for (int part = 0; part < 4; ++part) {
    const size_t dot = s.find('.');
    const bool last = part == 3;
    if (last != (dot == std::string_view::npos)) return std::nullopt;
    const std::optional<uint32_t> v = ParseDecimal(s.substr(0, last ? s.size() : dot), 3, 255);
    if (!v) return std::nullopt;
    address = (address << 8) | *v;
    if (!last) s.remove_prefix(dot + 1);
  }
  return address;
}

std::optional<uint16_t> ParsePort(std::string_view s) {
  const std::optional<uint32_t> v = ParseDecimal(s, 5, 0xFFFF);
  if (!v || *v == 0) return std::nullopt;
  return uint16_t(*v);
}

std::optional<PortMappingEntry> EntryFromAnswer(uint16_t external_port, const std::string& protocol,
                                                std::string_view internal_client,
                                                std::string_view internal_port,
                                                std::string_view description,
                                                std::string_view enabled, std::string_view lease) {
  const std::optional<uint16_t> port = ParsePort(internal_port);
  if (!port || !ParseIpv4(internal_client)) return std::nullopt;
  PortMappingEntry e;
  e.protocol = protocol;
  e.external_port = external_port;
  e.internal_client = std::string(internal_client);
  e.internal_port = *port;
  e.description = std::string(description);
  e.lease_s = ParseDecimal(lease, 10, 0xFFFFFFFFu).value_or(0);
  e.enabled = enabled != "0";
  return e;
}

// --- the worker -----------------------------------------------------------------------------------

namespace {

struct Job {
  std::function<int()> fn;
  bool started = false;
  bool done = false;
  bool cancelled = false;
  int rc = 0;
};

}  // namespace

struct BoundedWorker::State {
  std::mutex mu;
  std::condition_variable cv;  // a job queued, a job done, or stop
  std::deque<std::shared_ptr<Job>> jobs;
  bool stop = false;
  bool running_abandoned = false;  // the running job's caller has stopped waiting for it
};

namespace {

// The worker thread: owns its share of the state, so it may outlive the BoundedWorker.
void WorkerLoop(std::shared_ptr<BoundedWorker::State> s) {
  std::unique_lock lock(s->mu);
  for (;;) {
    s->cv.wait(lock, [&] { return s->stop || !s->jobs.empty(); });
    if (s->stop) break;  // queued jobs are dropped with the state
    std::shared_ptr<Job> job = std::move(s->jobs.front());
    s->jobs.pop_front();
    if (job->cancelled) continue;  // its bound passed before it started: never sent
    job->started = true;
    lock.unlock();
    int rc = kClientError;
    try {
      rc = job->fn();
    } catch (...) {
      // Out of memory in a call's own strings: that call fails, the worker goes on.
    }
    lock.lock();
    job->rc = rc;
    job->done = true;
    job->fn = nullptr;  // what the call owned goes now, on this thread
    s->running_abandoned = false;
    s->cv.notify_all();
  }
}

}  // namespace

BoundedWorker::BoundedWorker() : state_(std::make_shared<State>()) {
  std::thread(WorkerLoop, state_).detach();
}

BoundedWorker::~BoundedWorker() {
  {
    std::lock_guard lock(state_->mu);
    state_->stop = true;
  }
  state_->cv.notify_all();
}

int BoundedWorker::Run(std::function<int()> fn, std::chrono::milliseconds bound) {
  auto job = std::make_shared<Job>();
  job->fn = std::move(fn);
  std::unique_lock lock(state_->mu);
  if (state_->running_abandoned || state_->stop) return kTimedOut;
  state_->jobs.push_back(job);
  state_->cv.notify_all();
  if (!state_->cv.wait_for(lock, bound, [&] { return job->done; })) {
    job->cancelled = true;  // still queued: it never starts
    if (job->started) state_->running_abandoned = true;
    return kTimedOut;
  }
  return job->rc;
}

#if defined(SKYLANDERS_PORTAL_MINIUPNPC)
bool SystemIgdBuiltIn() { return true; }
#else
bool SystemIgdBuiltIn() { return false; }
#endif

}  // namespace igd

#if !defined(SKYLANDERS_PORTAL_MINIUPNPC)

// Built without miniupnpc: no router mapping, nothing sent.
std::unique_ptr<IgdClient> MakeSystemIgdClient() { return nullptr; }

#else

namespace {

// The router found by discovery: its control URL and service type. Shared by the client and every
// call it has handed to the worker, so an abandoned call never outlives what it reads.
struct Router {
  UPNPUrls urls{};
  IGDdatas data{};
  int version = 0;
  std::string lan_address;
  bool winsock = false;

  Router() {
#ifdef _WIN32
    WSADATA wsa;
    winsock = WSAStartup(MAKEWORD(2, 2), &wsa) == 0;  // counted: pairs with the cleanup below
#endif
  }
  ~Router() {
    FreeUPNPUrls(&urls);  // frees and zeroes; safe on the zeroed start
#ifdef _WIN32
    if (winsock) WSACleanup();
#endif
  }
  Router(const Router&) = delete;
  Router& operator=(const Router&) = delete;

  const char* control() const { return urls.controlURL; }
  const char* service() const { return data.first.servicetype; }
};

// On the worker thread: SSDP discovery, then the first usable IGD among the answers.
int Discover(Router& r) {
  int error = 0;
  UPNPDev* devices = upnpDiscover(igd::kDiscoveryDelayMs, nullptr, nullptr, UPNP_LOCAL_PORT_ANY,
                                  /*ipv6=*/0, /*ttl=*/2, &error);
  if (devices == nullptr) return kUpnpNoRouter;
  char lan[64] = {};
  char wan[64] = {};
  const int state = UPNP_GetValidIGD(devices, &r.urls, &r.data, lan, int(sizeof(lan) - 1), wan,
                                     int(sizeof(wan) - 1));
  freeUPNPDevlist(devices);
  if (!igd::UsableIgd(state)) return kUpnpNoRouter;
  r.version = igd::VersionFromServiceType(r.service());
  if (r.version == 0 || r.control() == nullptr || r.control()[0] == '\0') return kUpnpNoRouter;
  if (igd::ParseIpv4(lan)) r.lan_address = lan;
  return kUpnpOk;
}

class SystemIgdClient final : public IgdClient {
 public:
  SystemIgdClient(std::unique_ptr<igd::BoundedWorker> worker, std::shared_ptr<const Router> router)
      : worker_(std::move(worker)), router_(std::move(router)) {}

  int Version() const override { return router_->version; }
  std::string LanAddress() const override { return router_->lan_address; }

  int GetSpecificPortMappingEntry(uint16_t external_port, const std::string& protocol,
                                  PortMappingEntry* out) override {
    if (out == nullptr || external_port == 0 || (protocol != "UDP" && protocol != "TCP")) {
      return igd::kClientError;
    }
    auto answer = std::make_shared<PortMappingEntry>();
    const int rc = worker_->Run(
        [router = router_, external_port, protocol, answer] {
          char client[40] = {};
          char port[16] = {};
          char desc[256] = {};
          char enabled[16] = {};
          char lease[32] = {};
          const std::string ext = igd::DecimalText(external_port);
          const int got = igd::ResultFromMiniupnpc(UPNP_GetSpecificPortMappingEntryExt(
              router->control(), router->service(), ext.c_str(), protocol.c_str(), "", client, port, desc,
              sizeof(desc), enabled, lease));
          if (got != kUpnpOk) return got;
          std::optional<PortMappingEntry> e =
              igd::EntryFromAnswer(external_port, protocol, client, port, desc, enabled, lease);
          if (!e) return igd::kBadAnswer;
          *answer = std::move(*e);
          return kUpnpOk;
        },
        igd::kCallBound);
    if (rc == kUpnpOk) *out = *answer;
    return rc;
  }

  int AddPortMapping(const PortMappingEntry& entry) override {
    if (!igd::AddAllowed(router_->version, /*any_port=*/false) || !igd::ValidRequest(entry)) {
      return igd::kClientError;
    }
    return worker_->Run(
        [router = router_, e = entry] {
          const std::string ext = igd::DecimalText(e.external_port);
          const std::string in = igd::DecimalText(e.internal_port);
          const std::string lease = igd::DecimalText(e.lease_s);
          return igd::ResultFromMiniupnpc(UPNP_AddPortMapping(
              router->control(), router->service(), ext.c_str(), in.c_str(), e.internal_client.c_str(),
              e.description.c_str(), e.protocol.c_str(), "", lease.c_str()));
        },
        igd::kCallBound);
  }

  int AddAnyPortMapping(const PortMappingEntry& entry, uint16_t* reserved_port) override {
    if (reserved_port == nullptr || !igd::AddAllowed(router_->version, /*any_port=*/true) ||
        !igd::ValidRequest(entry)) {
      return igd::kClientError;
    }
    auto reserved = std::make_shared<uint16_t>(0);
    const int rc = worker_->Run(
        [router = router_, e = entry, reserved] {
          char got_port[16] = {};
          const std::string ext = igd::DecimalText(e.external_port);
          const std::string in = igd::DecimalText(e.internal_port);
          const std::string lease = igd::DecimalText(e.lease_s);
          const int got = igd::ResultFromMiniupnpc(UPNP_AddAnyPortMapping(
              router->control(), router->service(), ext.c_str(), in.c_str(), e.internal_client.c_str(),
              e.description.c_str(), e.protocol.c_str(), "", lease.c_str(), got_port));
          if (got != kUpnpOk) return got;
          const std::optional<uint16_t> p = igd::ParsePort(got_port);
          if (!p) return igd::kBadAnswer;  // a mapping may exist; it is never re-read as ours
          *reserved = *p;
          return kUpnpOk;
        },
        igd::kCallBound);
    *reserved_port = rc == kUpnpOk ? *reserved : 0;
    return rc;
  }

  int DeletePortMapping(uint16_t external_port, const std::string& protocol) override {
    if (external_port == 0 || (protocol != "UDP" && protocol != "TCP")) return igd::kClientError;
    return worker_->Run(
        [router = router_, external_port, protocol] {
          const std::string ext = igd::DecimalText(external_port);
          return igd::ResultFromMiniupnpc(UPNP_DeletePortMapping(router->control(), router->service(),
                                                                 ext.c_str(), protocol.c_str(), ""));
        },
        igd::kCallBound);
  }

  int GetExternalIPAddress(uint32_t* address) override {
    if (address == nullptr) return igd::kClientError;
    auto got_address = std::make_shared<uint32_t>(0);
    const int rc = worker_->Run(
        [router = router_, got_address] {
          char text[40] = {};
          const int got = igd::ResultFromMiniupnpc(
              UPNP_GetExternalIPAddress(router->control(), router->service(), text));
          if (got != kUpnpOk) return got;
          const std::optional<uint32_t> a = igd::ParseIpv4(text);
          if (!a) return igd::kBadAnswer;
          *got_address = *a;
          return kUpnpOk;
        },
        igd::kCallBound);
    if (rc == kUpnpOk) *address = *got_address;
    return rc;
  }

 private:
  std::unique_ptr<igd::BoundedWorker> worker_;
  std::shared_ptr<const Router> router_;
};

}  // namespace

std::unique_ptr<IgdClient> MakeSystemIgdClient() {
  std::unique_ptr<igd::BoundedWorker> worker;
  try {
    worker = std::make_unique<igd::BoundedWorker>();
  } catch (const std::system_error&) {
    return nullptr;  // no thread: no mapping, Host says it is unavailable
  }
  auto router = std::make_shared<Router>();
#ifdef _WIN32
  if (!router->winsock) return nullptr;
#endif
  // Discovery writes the router only on the worker; it is read here only after the worker said it
  // finished (Run's lock orders the two). If the bound passes first, the abandoned discovery keeps
  // its own share of the router and nothing here reads it.
  const int rc = worker->Run([router] { return Discover(*router); }, igd::kDiscoveryBound);
  if (rc != kUpnpOk) return nullptr;
  return std::make_unique<SystemIgdClient>(std::move(worker), std::move(router));
}

#endif  // SKYLANDERS_PORTAL_MINIUPNPC

}  // namespace skylanders::netplay
