// The port mapping rules (stage 8 item 4, design D8) on recorded router answers: a fake IGD that
// keeps a mapping table and a log of every call, so each test can say what was and was not sent.

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "netplay/port_mapping.h"
#include "test_util.h"

using namespace skylanders::netplay;

namespace {

constexpr uint32_t kIp(uint32_t a, uint32_t b, uint32_t c, uint32_t d) {
  return (a << 24) | (b << 16) | (c << 8) | d;
}

struct Call {
  std::string op;
  uint16_t port;
};

class FakeIgd : public IgdClient {
 public:
  explicit FakeIgd(int version) : version_(version) {}
  int Version() const override { return version_; }
  int GetSpecificPortMappingEntry(uint16_t port, const std::string&, PortMappingEntry* out) override {
    calls.push_back({"get", port});
    auto it = table.find(port);
    if (it == table.end()) return kUpnpNoSuchEntry;
    *out = it->second;
    return kUpnpOk;
  }
  int AddPortMapping(const PortMappingEntry& e) override {
    calls.push_back({"add", e.external_port});
    if (auto it = add_errors.find(e.external_port); it != add_errors.end()) return it->second;
    auto it = table.find(e.external_port);
    if (it != table.end() && it->second.internal_client != e.internal_client) return kUpnpConflictInMappingEntry;
    table[e.external_port] = e;  // IGD v1 overwrites an entry of the same internal client
    if (auto r = race.find(e.external_port); r != race.end()) table[e.external_port] = r->second;
    return kUpnpOk;
  }
  int AddAnyPortMapping(const PortMappingEntry& e, uint16_t* reserved) override {
    calls.push_back({"addany", e.external_port});
    if (addany_error) return addany_error;
    uint16_t p = e.external_port;
    while (table.count(p) && !(table[p].internal_client == e.internal_client &&
                               table[p].description == e.description)) {
      ++p;
    }
    if (force_port) p = force_port;
    table[p] = e;
    table[p].external_port = p;
    if (auto r = race.find(p); r != race.end()) table[p] = r->second;
    *reserved = p;
    return kUpnpOk;
  }
  int DeletePortMapping(uint16_t port, const std::string&) override {
    calls.push_back({"delete", port});
    return table.erase(port) ? kUpnpOk : kUpnpNoSuchEntry;
  }
  int GetExternalIPAddress(uint32_t* a) override {
    *a = external;
    return kUpnpOk;
  }

  int Count(const std::string& op, int port = -1) const {
    int n = 0;
    for (const Call& c : calls) n += c.op == op && (port < 0 || c.port == port);
    return n;
  }

  int version_;
  std::map<uint16_t, PortMappingEntry> table;
  std::map<uint16_t, int> add_errors;
  std::map<uint16_t, PortMappingEntry> race;  // what the entry is after an add (another program won)
  std::vector<Call> calls;
  int addany_error = 0;
  uint16_t force_port = 0;
  uint32_t external = kIp(203, 0, 113, 7);
};

PortMappingEntry Foreign(uint16_t port, const std::string& client, const std::string& desc) {
  PortMappingEntry e;
  e.protocol = "UDP";
  e.external_port = port;
  e.internal_client = client;
  e.internal_port = port;
  e.description = desc;
  e.lease_s = 0;
  return e;
}

}  // namespace

int main() {
  const std::string me = "192.168.1.20";

  // IGD v1, a free port: mapped on the configured port, re-read with the nonce, renewed, removed.
  {
    FakeIgd igd(1);
    igd.external = kIp(8, 8, 4, 4);
    PortMapper m(&igd, me, 52000, "n1");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kMapped && r.external_port == 52000 && m.owned());
    CHECK(r.external_address == kIp(8, 8, 4, 4));
    CHECK(r.message.find("can only be tested by someone joining") != std::string::npos);
    CHECK(igd.table[52000].description == "Swap Force Online n1" && igd.table[52000].lease_s == 3600);
    CHECK(igd.Count("get", 52000) == 2);  // the pre-read and the re-read
    CHECK(m.Renew());
    CHECK(m.Remove() && igd.table.empty() && !m.owned());
    CHECK(!m.Remove() && !m.Renew());
  }

  // A pre-existing entry (another device) is left alone; the next candidate is mapped.
  {
    FakeIgd igd(1);
    igd.table[52000] = Foreign(52000, "192.168.1.30", "Some Game");
    PortMapper m(&igd, me, 52000, "n2");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kMapped && r.external_port == 52001);
    CHECK(igd.Count("add", 52000) == 0 && igd.Count("delete") == 0);
    CHECK(igd.table[52000].description == "Some Game");
  }

  // An earlier session's own leftover (same client, another nonce) is left alone too.
  {
    FakeIgd igd(1);
    PortMappingEntry old = Foreign(52000, me, "Swap Force Online old");
    old.internal_port = 52000;
    igd.table[52000] = old;
    PortMapper m(&igd, me, 52000, "n3");
    MappingResult r = m.Map();
    CHECK(r.external_port == 52001 && igd.Count("add", 52000) == 0 && igd.Count("delete") == 0);
    CHECK(igd.table[52000].description == "Swap Force Online old");
    // Remove deletes only the owned one.
    CHECK(m.Remove() && igd.table.count(52000) == 1 && igd.table.count(52001) == 0);
  }

  // An add failing with 718 or 729: no delete, no second add on that port; the next port is tried.
  for (int err : {kUpnpConflictInMappingEntry, kUpnpConflictWithOtherMechanisms, 501}) {
    FakeIgd igd(1);
    igd.add_errors[52000] = err;
    PortMapper m(&igd, me, 52000, "n4");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kMapped && r.external_port == 52001);
    CHECK(igd.Count("add", 52000) == 1 && igd.Count("delete") == 0);
  }

  // Every candidate refused: the failure names the router's code, or "taken".
  {
    FakeIgd igd(1);
    for (int k = 0; k < 9; ++k) igd.add_errors[uint16_t(52000 + k)] = 501;
    PortMapper m(&igd, me, 52000, "n5");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kRouterRefused && r.upnp_error == 501);
    CHECK(r.message == "the router refused the mapping (501)");
    CHECK(igd.Count("add") == 9 && igd.Count("delete") == 0 && !m.owned());
    FakeIgd full(1);
    for (int k = 0; k < 9; ++k) full.table[uint16_t(52000 + k)] = Foreign(uint16_t(52000 + k), "192.168.1.9", "x");
    PortMapper f(&full, me, 52000, "n5b");
    MappingResult rf = f.Map();
    CHECK(rf.outcome == MappingOutcome::kTakenByAnotherDevice && full.Count("add") == 0);
    CHECK(rf.message == "the port is taken on the router by another device");
  }

  // A re-read after the add that shows another nonce is a conflict: not owned, never renewed or
  // deleted, the next candidate tried.
  {
    FakeIgd igd(1);
    igd.race[52000] = Foreign(52000, me, "Swap Force Online someone-else");
    igd.race[52000].internal_port = 52000;
    PortMapper m(&igd, me, 52000, "n6");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kMapped && r.external_port == 52001);
    CHECK(m.Remove());
    CHECK(igd.Count("delete", 52000) == 0 && igd.table.count(52000) == 1);
  }

  // Renewal and removal skipped when the entry no longer matches.
  {
    FakeIgd igd(1);
    PortMapper m(&igd, me, 52000, "n7");
    CHECK(m.Map().outcome == MappingOutcome::kMapped);
    igd.table[52000] = Foreign(52000, "192.168.1.44", "replaced");
    const int adds = igd.Count("add");
    CHECK(!m.Renew() && !m.owned() && igd.Count("add") == adds);
    CHECK(!m.Remove() && igd.Count("delete") == 0 && igd.table[52000].description == "replaced");
    FakeIgd gone(1);
    PortMapper g(&gone, me, 52000, "n7b");
    CHECK(g.Map().outcome == MappingOutcome::kMapped);
    gone.table.clear();
    CHECK(!g.Renew() && gone.Count("add") == 1);
  }

  // IGD v2: only AddAnyPortMapping, never the plain AddPortMapping; the router's port is the one used.
  {
    FakeIgd igd(2);
    igd.table[52000] = Foreign(52000, "192.168.1.30", "Some Game");
    PortMapper m(&igd, me, 52000, "n8");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kMapped && r.external_port == 52001 && m.owned_port() == 52001);
    CHECK(m.Renew());
    CHECK(m.Remove());
    CHECK(igd.Count("add") == 0 && igd.Count("addany") == 2 && igd.Count("delete", 52000) == 0);
    CHECK(igd.table.count(52000) == 1);
  }
  {
    FakeIgd igd(2);
    igd.addany_error = kUpnpConflictWithOtherMechanisms;
    PortMapper m(&igd, me, 52000, "n9");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kTakenByAnotherDevice && igd.Count("addany") == 1);
    CHECK(igd.Count("add") == 0 && igd.Count("delete") == 0);
  }
  {
    // A v2 re-read showing another nonce: not owned, the next attempt asks again.
    FakeIgd igd(2);
    igd.race[52000] = Foreign(52000, me, "Swap Force Online other");
    PortMapper m(&igd, me, 52000, "n10");
    MappingResult r = m.Map();
    CHECK(r.outcome == MappingOutcome::kMapped && r.external_port == 52001 && igd.Count("add") == 0);
    CHECK(igd.Count("delete") == 0);
  }
  {
    // A v2 renewal the router answers with another port: the new entry (ours) goes, not owned now.
    FakeIgd igd(2);
    PortMapper m(&igd, me, 52000, "n11");
    CHECK(m.Map().external_port == 52000);
    igd.force_port = 52005;
    CHECK(!m.Renew() && !m.owned());
    CHECK(igd.Count("delete", 52005) == 1 && igd.Count("delete", 52000) == 0 && igd.Count("add") == 0);
  }

  // No UPnP, or NAT-PMP only: the right message, nothing sent.
  {
    PortMapper none(nullptr, me, 52000, "x");
    CHECK(none.Map().message == "no router answered UPnP");
    PortMapper pmp(nullptr, me, 52000, "x", true);
    MappingResult r = pmp.Map();
    CHECK(r.outcome == MappingOutcome::kNatPmpOnly && r.message.find("forward the port by hand") != std::string::npos);
  }

  // A mapped port behind a non-public external address says why internet hosting cannot work.
  {
    FakeIgd igd(1);
    igd.external = kIp(100, 64, 0, 9);
    PortMapper m(&igd, me, 52000, "n12");
    CHECK(m.Map().message.find("carrier-grade NAT") != std::string::npos);
  }

  // NAT-PMP: only the opcode-0 request may leave; its answer parsed.
  {
    const auto req = NatPmpExternalAddressRequest();
    CHECK(req.size() == 2 && NatPmpMaySend(req));
    const uint8_t map_udp[12] = {0, 1, 0, 0, 0xCB, 0x20, 0xCB, 0x20, 0, 0, 0x0E, 0x10};
    const uint8_t map_tcp[12] = {0, 2, 0, 0, 0xCB, 0x20, 0xCB, 0x20, 0, 0, 0x0E, 0x10};
    CHECK(!NatPmpMaySend(map_udp) && !NatPmpMaySend(map_tcp));
    const uint8_t padded[3] = {0, 0, 0};
    CHECK(!NatPmpMaySend(padded));
    const uint8_t answer[12] = {0, 128, 0, 0, 0, 0, 0x01, 0x00, 203, 0, 113, 7};
    auto a = ParseNatPmpExternalAddress(answer);
    CHECK(a.has_value() && a->result == 0 && a->epoch_s == 256 && a->address == kIp(203, 0, 113, 7));
    const uint8_t refused[12] = {0, 128, 0, 3, 0, 0, 0, 1, 0, 0, 0, 0};
    CHECK(ParseNatPmpExternalAddress(refused)->result == 3);
    const uint8_t wrong_op[12] = {0, 129, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4};
    const uint8_t wrong_ver[12] = {1, 128, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4};
    CHECK(!ParseNatPmpExternalAddress(wrong_op) && !ParseNatPmpExternalAddress(wrong_ver));
    CHECK(!ParseNatPmpExternalAddress(std::span<const uint8_t>(answer, 11)));
  }

  // The classifier: every class and both edges of each range.
  {
    struct Case {
      uint32_t a;
      AddressClass c;
    };
    const Case cases[] = {
        {kIp(9, 255, 255, 255), AddressClass::kPublic},     {kIp(10, 0, 0, 0), AddressClass::kPrivate},
        {kIp(10, 255, 255, 255), AddressClass::kPrivate},   {kIp(11, 0, 0, 0), AddressClass::kPublic},
        {kIp(172, 15, 255, 255), AddressClass::kPublic},    {kIp(172, 16, 0, 0), AddressClass::kPrivate},
        {kIp(172, 31, 255, 255), AddressClass::kPrivate},   {kIp(172, 32, 0, 0), AddressClass::kPublic},
        {kIp(192, 167, 255, 255), AddressClass::kPublic},   {kIp(192, 168, 0, 0), AddressClass::kPrivate},
        {kIp(192, 168, 255, 255), AddressClass::kPrivate},  {kIp(192, 169, 0, 0), AddressClass::kPublic},
        {kIp(100, 63, 255, 255), AddressClass::kPublic},    {kIp(100, 64, 0, 0), AddressClass::kShared},
        {kIp(100, 127, 255, 255), AddressClass::kShared},   {kIp(100, 128, 0, 0), AddressClass::kPublic},
        {kIp(0, 0, 0, 0), AddressClass::kUnreachable},      {kIp(0, 255, 255, 255), AddressClass::kUnreachable},
        {kIp(1, 0, 0, 0), AddressClass::kPublic},           {kIp(126, 255, 255, 255), AddressClass::kPublic},
        {kIp(127, 0, 0, 1), AddressClass::kUnreachable},    {kIp(128, 0, 0, 0), AddressClass::kPublic},
        {kIp(169, 253, 255, 255), AddressClass::kPublic},   {kIp(169, 254, 0, 0), AddressClass::kUnreachable},
        {kIp(169, 254, 255, 255), AddressClass::kUnreachable}, {kIp(169, 255, 0, 0), AddressClass::kPublic},
        {kIp(192, 0, 0, 0), AddressClass::kUnreachable},    {kIp(192, 0, 0, 255), AddressClass::kUnreachable},
        {kIp(192, 0, 1, 0), AddressClass::kPublic},         {kIp(192, 0, 2, 0), AddressClass::kUnreachable},
        {kIp(192, 0, 2, 255), AddressClass::kUnreachable},  {kIp(192, 0, 3, 0), AddressClass::kPublic},
        {kIp(198, 17, 255, 255), AddressClass::kPublic},    {kIp(198, 18, 0, 0), AddressClass::kUnreachable},
        {kIp(198, 19, 255, 255), AddressClass::kUnreachable}, {kIp(198, 20, 0, 0), AddressClass::kPublic},
        {kIp(198, 51, 100, 0), AddressClass::kUnreachable}, {kIp(198, 51, 100, 255), AddressClass::kUnreachable},
        {kIp(198, 51, 101, 0), AddressClass::kPublic},      {kIp(203, 0, 112, 255), AddressClass::kPublic},
        {kIp(203, 0, 113, 0), AddressClass::kUnreachable},  {kIp(203, 0, 113, 255), AddressClass::kUnreachable},
        {kIp(203, 0, 114, 0), AddressClass::kPublic},       {kIp(223, 255, 255, 255), AddressClass::kPublic},
        {kIp(224, 0, 0, 0), AddressClass::kUnreachable},    {kIp(239, 255, 255, 255), AddressClass::kUnreachable},
        {kIp(240, 0, 0, 0), AddressClass::kUnreachable},    {kIp(255, 255, 255, 254), AddressClass::kUnreachable},
        {kIp(255, 255, 255, 255), AddressClass::kUnreachable},
    };
    for (const Case& c : cases) CHECK(ClassifyExternalAddress(c.a) == c.c);
    CHECK(AddressClassMessage(AddressClass::kPublic).empty());
    CHECK(AddressClassMessage(AddressClass::kPrivate).find("double NAT") != std::string::npos);
    CHECK(AddressClassMessage(AddressClass::kUnreachable).find("cannot reach") != std::string::npos);
  }

  return Finish("netplay_port_mapping");
}
