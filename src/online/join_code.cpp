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

}  // namespace skylanders::online
