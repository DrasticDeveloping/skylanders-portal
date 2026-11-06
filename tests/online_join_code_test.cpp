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

  return Finish("online_join_code");
}
