#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

#include "netplay/transport.h"

namespace skylanders::online {

// A join code: the server's IPv4 address and port in Crockford base32 (0-9 and A-Z without I, L,
// O, U). Ten symbols hold 50 bits, most significant first: a 2-bit kind (0, the only one so far;
// stage 8 adds codes that work across the internet), the address (32) and the port (16). An
// eleventh symbol is a check (Luhn mod 32), which catches any one changed symbol and most swaps of
// two neighbours. Eight symbols, as first planned, cannot hold the 48 bits of an address and port.
constexpr size_t kJoinCodeLength = 11;

// The 11 symbols, upper case, no separators.
std::string EncodeJoinCode(const netplay::Endpoint& server);
// "XXXX-XXXX-XXX" for showing; DecodeJoinCode reads either form.
std::string FormatJoinCode(std::string_view code);
// The endpoint in `code`; nullopt for a wrong length or check, an unknown symbol or kind, or a
// zero address or port. Case-insensitive; dashes and spaces are ignored; I and L read as 1 and O
// as 0 (Crockford's rule for symbols people mistype).
std::optional<netplay::Endpoint> DecodeJoinCode(std::string_view code);

}  // namespace skylanders::online
