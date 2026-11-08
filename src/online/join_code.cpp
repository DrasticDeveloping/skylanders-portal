#include "online/join_code.h"

#include <array>

namespace skylanders::online {

namespace {

constexpr char kSymbols[] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
static_assert(sizeof(kSymbols) == 33);
constexpr size_t kDataSymbols = kJoinCodeLength - 1;
constexpr uint32_t kBase = 32;

int SymbolValue(char c) {
  if (c >= 'a' && c <= 'z') c = char(c - 'a' + 'A');
  if (c == 'I' || c == 'L') return 1;
  if (c == 'O') return 0;
  for (int i = 0; i < 32; ++i) {
    if (kSymbols[i] == c) return i;
  }
  return -1;
}

// Luhn mod N over symbol values, rightmost first. `with_check`: the last value is the check.
uint32_t LuhnSum(const uint32_t* values, size_t count, bool with_check) {
  uint32_t factor = with_check ? 1 : 2;
  uint32_t sum = 0;
  for (size_t i = count; i-- > 0;) {
    uint32_t addend = factor * values[i];
    factor = factor == 2 ? 1 : 2;
    sum += addend / kBase + addend % kBase;
  }
  return sum % kBase;
}

}  // namespace

std::string EncodeJoinCode(const netplay::Endpoint& server) {
  constexpr uint64_t kind = 0;
  const uint64_t value = (kind << 48) | (uint64_t(server.address) << 16) | server.port;
  std::array<uint32_t, kJoinCodeLength> v{};
  for (size_t i = 0; i < kDataSymbols; ++i) {
    v[i] = uint32_t(value >> (5 * (kDataSymbols - 1 - i))) & 31;
  }
  v[kDataSymbols] = (kBase - LuhnSum(v.data(), kDataSymbols, false)) % kBase;
  std::string code;
  for (uint32_t x : v) code.push_back(kSymbols[x]);
  return code;
}

std::string FormatJoinCode(std::string_view code) {
  std::string out;
  for (size_t i = 0; i < code.size(); ++i) {
    if (i > 0 && i % 4 == 0) out.push_back('-');
    out.push_back(code[i]);
  }
  return out;
}

std::optional<netplay::Endpoint> DecodeJoinCode(std::string_view code) {
  std::array<uint32_t, kJoinCodeLength> v{};
  size_t n = 0;
  for (char c : code) {
    if (c == '-' || c == ' ') continue;
    const int x = SymbolValue(c);
    if (x < 0 || n == kJoinCodeLength) return std::nullopt;
    v[n++] = uint32_t(x);
  }
  if (n != kJoinCodeLength || LuhnSum(v.data(), n, true) != 0) return std::nullopt;
  uint64_t value = 0;
  for (size_t i = 0; i < kDataSymbols; ++i) value = (value << 5) | v[i];
  if ((value >> 48) != 0) return std::nullopt;  // a kind this build does not know
  netplay::Endpoint e{uint32_t(value >> 16), uint16_t(value)};
  if (e.address == 0 || e.port == 0) return std::nullopt;
  return e;
}

const char* JoinCodeStatusText(JoinCodeStatus status) {
  switch (status) {
    case JoinCodeStatus::kOk: return "";
    case JoinCodeStatus::kNotAJoinCode: return "not a join code";
    case JoinCodeStatus::kOlderVersion: return "this code is from an older version";
    case JoinCodeStatus::kNewerVersion: return "this code is from a newer version";
  }
  return "not a join code";
}

namespace {

constexpr size_t kV2DataSymbols = kJoinCodeV2Length - 1;  // 17 symbols, 85 bits

// The 85 bits as two parts: high = kind (2) | revision (3) | address (32), 37 bits; low = port
// (16) | secret (32), 48 bits. Symbol i holds bits 84-5i .. 80-5i of the whole.
uint32_t V2Symbol(uint64_t high, uint64_t low, size_t i) {
  uint32_t out = 0;
  for (int b = 0; b < 5; ++b) {
    const int bit = 84 - int(5 * i) - b;  // most significant first
    const uint64_t v = bit >= 48 ? (high >> (bit - 48)) & 1 : (low >> bit) & 1;
    out = (out << 1) | uint32_t(v);
  }
  return out;
}

}  // namespace

std::string EncodeJoinCodeV2(const JoinTarget& target) {
  if (target.server.address == 0 || target.server.port == 0 || target.secret == 0) return {};
  const uint64_t high = (uint64_t(kJoinCodeV2Kind) << 35) | (uint64_t(kJoinCodeV2Revision) << 32) |
                        target.server.address;
  const uint64_t low = (uint64_t(target.server.port) << 32) | target.secret;
  std::array<uint32_t, kJoinCodeV2Length> v{};
  for (size_t i = 0; i < kV2DataSymbols; ++i) v[i] = V2Symbol(high, low, i);
  v[kV2DataSymbols] = (kBase - LuhnSum(v.data(), kV2DataSymbols, false)) % kBase;
  std::string code;
  for (uint32_t x : v) code.push_back(kSymbols[x]);
  return code;
}

JoinCodeResult DecodeJoinCodeV2(std::string_view code) {
  JoinCodeResult result;
  std::array<uint32_t, kJoinCodeV2Length> v{};
  size_t n = 0;
  for (char c : code) {
    if (c == '-' || c == ' ') continue;
    const int x = SymbolValue(c);
    if (x < 0 || n == kJoinCodeV2Length) return result;
    v[n++] = uint32_t(x);
  }
  if (n == kJoinCodeLength) {
    // A 1A code: valid as one (right check, kind 0, nonzero fields) means "older version".
    if (DecodeJoinCode(code)) result.status = JoinCodeStatus::kOlderVersion;
    return result;
  }
  if (n != kJoinCodeV2Length || LuhnSum(v.data(), n, true) != 0) return result;
  uint64_t high = 0, low = 0;  // 37 and 48 bits
  for (size_t i = 0; i < kV2DataSymbols; ++i) {
    for (int b = 4; b >= 0; --b) {
      high = (high << 1) | (low >> 47);
      low = ((low << 1) | ((v[i] >> b) & 1)) & ((uint64_t(1) << 48) - 1);
    }
  }
  const uint32_t kind = uint32_t(high >> 35) & 3;
  const uint32_t revision = uint32_t(high >> 32) & 7;
  if (kind != kJoinCodeV2Kind || revision != kJoinCodeV2Revision) {
    result.status = JoinCodeStatus::kNewerVersion;  // another kind, or a revision of kind 1 we lack
    return result;
  }
  JoinTarget t{{uint32_t(high), uint16_t(low >> 32)}, uint32_t(low)};
  if (t.server.address == 0 || t.server.port == 0 || t.secret == 0) return result;
  result.status = JoinCodeStatus::kOk;
  result.target = t;
  return result;
}

}  // namespace skylanders::online
