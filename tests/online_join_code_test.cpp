#include <cctype>
#include <cstdint>
#include <string>

#include "online/join_code.h"
#include "test_util.h"

using namespace skylanders::online;
using skylanders::netplay::Endpoint;

namespace {
constexpr char kSymbols[] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
}

int main() {
  // Round trips over corner and ordinary values.
  {
    const Endpoint cases[] = {
        {0x7F000001, 7777},      {0xC0A80114, 52000}, {0x0A000002, 1},
        {0xFFFFFFFF, 65535},     {0x00000001, 1},     {0xAC1F0001, 3074},
    };
    for (const Endpoint& e : cases) {
      const std::string code = EncodeJoinCode(e);
      CHECK(code.size() == kJoinCodeLength);
      for (char c : code) CHECK(std::string(kSymbols).find(c) != std::string::npos);
      auto back = DecodeJoinCode(code);
      CHECK(back.has_value() && *back == e);
    }
    // Different endpoints give different codes.
    CHECK(EncodeJoinCode({0x7F000001, 7777}) != EncodeJoinCode({0x7F000001, 7778}));
    CHECK(EncodeJoinCode({0x7F000001, 7777}) != EncodeJoinCode({0x7F000002, 7777}));
  }

  // The check symbol catches every one-symbol change, at every position, to every other symbol.
  {
    const std::string code = EncodeJoinCode({0xC0A80114, 52000});
    int caught = 0, total = 0;
    for (size_t i = 0; i < code.size(); ++i) {
      for (char c : std::string(kSymbols)) {
        if (c == code[i]) continue;
        std::string changed = code;
        changed[i] = c;
        ++total;
        caught += !DecodeJoinCode(changed).has_value();
      }
    }
    CHECK(caught == total);
    // Most swaps of two neighbours too.
    int swaps_caught = 0, swaps = 0;
    for (size_t i = 0; i + 1 < code.size(); ++i) {
      if (code[i] == code[i + 1]) continue;
      std::string swapped = code;
      std::swap(swapped[i], swapped[i + 1]);
      ++swaps;
      swaps_caught += !DecodeJoinCode(swapped).has_value();
    }
    CHECK(swaps_caught * 10 >= swaps * 8);
  }

  // Lower case, dashes and spaces are accepted; I, L and O read as 1, 1 and 0.
  {
    const Endpoint e{0x7F000001, 40000};
    const std::string code = EncodeJoinCode(e);
    const std::string shown = FormatJoinCode(code);
    CHECK(shown.size() == kJoinCodeLength + 2);
    CHECK(shown[4] == '-' && shown[9] == '-');
    CHECK(DecodeJoinCode(shown) == e);
    std::string lower;
    for (char c : shown) lower.push_back(char(std::tolower(uint8_t(c))));
    CHECK(DecodeJoinCode(lower) == e);
    CHECK(DecodeJoinCode(" " + code.substr(0, 3) + " - " + code.substr(3) + " ") == e);
    std::string typo = code;
    for (char& c : typo) {
      if (c == '1') c = 'l';
      else if (c == '0') c = 'O';
    }
    CHECK(DecodeJoinCode(typo) == e);
  }

  // Rejects: wrong length, U and other symbols, a zero port or address, an unknown kind.
  {
    const std::string code = EncodeJoinCode({0x7F000001, 40000});
    CHECK(!DecodeJoinCode(code.substr(0, kJoinCodeLength - 1)));
    CHECK(!DecodeJoinCode(code + "0"));
    CHECK(!DecodeJoinCode(""));
    std::string u = code;
    u[2] = 'U';
    CHECK(!DecodeJoinCode(u));
    std::string star = code;
    star[2] = '*';
    CHECK(!DecodeJoinCode(star));
    CHECK(!DecodeJoinCode(EncodeJoinCode({0x7F000001, 0})));
    CHECK(!DecodeJoinCode(EncodeJoinCode({0, 7000})));
    // Kind 1: the kind is the first symbol's top two bits (value 8 sets kind 1). Whatever the
    // check symbol (one of the 32 passes the check), the code is refused.
    std::string body = code.substr(0, kJoinCodeLength - 1);
    body[0] = kSymbols[std::string(kSymbols).find(body[0]) | 8];
    bool found = false;
    for (char c : std::string(kSymbols)) {
      if (DecodeJoinCode(body + c)) found = true;
    }
    CHECK(!found);
  }

  // Join code v2 (stage 8 item 4, `join-code-v2`): the design's golden vectors, computed by a
  // stdlib script that mirrors this file (addresses from the documentation ranges).
  {
    const JoinTarget a{{0xCB007107, 50000}, 0x89ABCDEF};  // 203.0.113.7
    const JoinTarget b{{0xC0A80114, 52000}, 0x00000001};  // 192.168.1.20
    CHECK(EncodeJoinCodeV2(a) == "8SC0721Y3A24TQKFFQ");
    CHECK(EncodeJoinCodeV2(b) == "8R2M0256B40000001J");
    CHECK(FormatJoinCode(EncodeJoinCodeV2(a)) == "8SC0-721Y-3A24-TQKF-FQ");
    const JoinCodeResult ra = DecodeJoinCodeV2("8SC0-721Y-3A24-TQKF-FQ");
    CHECK(ra.status == JoinCodeStatus::kOk && ra.target == a);
    CHECK(DecodeJoinCodeV2("8r2m-0256-b400-0000-1j").target == b);
    // The first with revision 1, and with kind 2: a newer version.
    CHECK(DecodeJoinCodeV2("9SC0-721Y-3A24-TQKF-FN").status == JoinCodeStatus::kNewerVersion);
    CHECK(DecodeJoinCodeV2("GSC0-721Y-3A24-TQKF-F6").status == JoinCodeStatus::kNewerVersion);
    // 1A's code for 192.168.1.20:52000: an older version.
    CHECK(EncodeJoinCode({0xC0A80114, 52000}) == "60N00H9JS0V");
    CHECK(DecodeJoinCodeV2("60N0-0H9J-S0V").status == JoinCodeStatus::kOlderVersion);
    CHECK(std::string(JoinCodeStatusText(JoinCodeStatus::kOlderVersion)) == "this code is from an older version");
    // 11 symbols that fail 1A's check are not a join code, not "older".
    CHECK(DecodeJoinCodeV2("60N0-0H9J-S0W").status == JoinCodeStatus::kNotAJoinCode);
  }

  // v2 round trips, and every one-symbol change caught.
  {
    const JoinTarget cases[] = {
        {{0x7F000001, 7777}, 1},          {{0xFFFFFFFF, 65535}, 0xFFFFFFFF},
        {{0x00000001, 1}, 0x80000000},    {{0x0A000002, 40000}, 0x12345678},
    };
    for (const JoinTarget& t : cases) {
      const std::string code = EncodeJoinCodeV2(t);
      CHECK(code.size() == kJoinCodeV2Length);
      const JoinCodeResult r = DecodeJoinCodeV2(FormatJoinCode(code));
      CHECK(r.status == JoinCodeStatus::kOk && r.target == t);
    }
    const std::string code = EncodeJoinCodeV2({{0xCB007107, 50000}, 0x89ABCDEF});
    int caught = 0, total = 0;
    for (size_t i = 0; i < code.size(); ++i) {
      for (char c : std::string(kSymbols)) {
        if (c == code[i]) continue;
        std::string changed = code;
        changed[i] = c;
        ++total;
        caught += DecodeJoinCodeV2(changed).status != JoinCodeStatus::kOk;
      }
    }
    CHECK(caught == total);
  }

  // v2 refusals: zero fields (never encoded, and refused when forged with a valid check), wrong
  // lengths, unknown symbols.
  {
    CHECK(EncodeJoinCodeV2({{0xCB007107, 50000}, 0}).empty());
    CHECK(EncodeJoinCodeV2({{0, 50000}, 7}).empty());
    CHECK(EncodeJoinCodeV2({{0xCB007107, 0}, 7}).empty());
    // A forged code with secret 0: change the last data symbol to zero its low bits and try every
    // check symbol; whichever passes the check must still be refused.
    const std::string code = EncodeJoinCodeV2({{0xCB007107, 50000}, 0x0000001F});
    std::string body = code.substr(0, kJoinCodeV2Length - 1);
    body[kJoinCodeV2Length - 2] = '0';  // the secret's low 5 bits: now 0, so the secret is 0
    bool any_ok = false;
    for (char c : std::string(kSymbols)) {
      const JoinCodeResult r = DecodeJoinCodeV2(body + c);
      if (r.status == JoinCodeStatus::kOk) any_ok = true;
    }
    CHECK(!any_ok);
    CHECK(DecodeJoinCodeV2(code.substr(0, 17)).status == JoinCodeStatus::kNotAJoinCode);
    CHECK(DecodeJoinCodeV2(code + "0").status == JoinCodeStatus::kNotAJoinCode);
    CHECK(DecodeJoinCodeV2("").status == JoinCodeStatus::kNotAJoinCode);
    std::string u = code;
    u[3] = 'U';
    CHECK(DecodeJoinCodeV2(u).status == JoinCodeStatus::kNotAJoinCode);
  }

  return Finish("online_join_code");
}
