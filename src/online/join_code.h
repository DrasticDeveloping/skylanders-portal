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

// Join code v2 (stage 8 item 4, design D10): one code for the LAN and internet modes, carrying the
// session's join secret. 85 bits, most significant first: kind (2 bits, 1; 1A's codes are kind 0),
// revision (3 bits, 0; a later format inside kind 1 raises it), the IPv4 address (32), the port
// (16) and the secret (32), as 17 Crockford base32 symbols, then one Luhn mod 32 check symbol
// computed as 1A's: 18 symbols, shown "XXXX-XXXX-XXXX-XXXX-XX". The secret is drawn per session
// from the OS random source and is never zero; it is not the owner token (which never appears in
// a code) and grants only the right to ask to be let in. When the session wiring lands, 1A's
// EncodeJoinCode and DecodeJoinCode above are retired (DecodeJoinCodeV2 already names their codes
// "older version").
constexpr size_t kJoinCodeV2Length = 18;
constexpr uint32_t kJoinCodeV2Kind = 1;
constexpr uint32_t kJoinCodeV2Revision = 0;

struct JoinTarget {
  netplay::Endpoint server;
  uint32_t secret = 0;
  auto operator<=>(const JoinTarget&) const = default;
};

enum class JoinCodeStatus : uint8_t {
  kOk = 0,
  kNotAJoinCode,  // wrong length or check, an unknown symbol, a zero address, port or secret
  kOlderVersion,  // 11 symbols that decode as a 1A code
  kNewerVersion,  // 18 symbols with kind 1 and a revision this build does not know, or another kind
};
// The refusal in plain words: "not a join code", "this code is from an older version", "this code
// is from a newer version" ("" for kOk).
const char* JoinCodeStatusText(JoinCodeStatus status);

struct JoinCodeResult {
  JoinCodeStatus status = JoinCodeStatus::kNotAJoinCode;
  JoinTarget target;  // only for kOk
};

// The 18 symbols, upper case, no separators; empty if the address, port or secret is zero.
// FormatJoinCode shows it in groups of four.
std::string EncodeJoinCodeV2(const JoinTarget& target);
// Reads either form, with the same leniency as DecodeJoinCode (case, dashes, spaces, I/L/O).
JoinCodeResult DecodeJoinCodeV2(std::string_view code);

// A code as a player typed or pasted it (F4 > Online > Join, the input script's online_join), as
// the bare symbols the decoders read: ASCII spaces, tabs and line ends, dashes, and the look-alikes a
// web page or a messenger may paste (no-break space U+00A0, the hyphens and dashes U+2010-U+2015,
// minus U+2212, zero-width space and joiners U+200B-U+200D, the byte order mark U+FEFF, all as
// UTF-8) are dropped; a-z become A-Z. Everything else stays as it is, so a stray character still
// makes the decoder answer "not a join code"; I, L and O stay too (the decoders read them as 1, 1
// and 0).
std::string NormalizeJoinCodeText(std::string_view text);

}  // namespace skylanders::online
