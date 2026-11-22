#include <string>
#include <vector>

#include "portal_rex/choice_filter.h"
#include "test_util.h"

namespace cf = skylanders::choice_filter;

int main() {
  const std::vector<std::string> items = {"Old Town (town_state1, hub)", "High Peak (intro_a)", "intro_b",
                                          "Green Glade (zone1)", "zone2", "Red Ruins (ZONE3)"};
  // No word: everything, in order.
  CHECK(cf::Match(items, "") == std::vector<size_t>({0, 1, 2, 3, 4, 5}));
  CHECK(cf::Match(items, "   ") == std::vector<size_t>({0, 1, 2, 3, 4, 5}));
  // Any part, any case.
  CHECK(cf::Match(items, "green") == std::vector<size_t>({3}));
  CHECK(cf::Match(items, "ZONE") == std::vector<size_t>({3, 4, 5}));
  CHECK(cf::Match(items, "intro") == std::vector<size_t>({1, 2}));
  // Every word, in any order.
  CHECK(cf::Match(items, "glade zone1") == std::vector<size_t>({3}));
  CHECK(cf::Match(items, "zone1  green ") == std::vector<size_t>({3}));
  CHECK(cf::Match(items, "hub intro").empty());
  CHECK(cf::Match(items, "zzz").empty());
  CHECK(cf::Match({}, "a").empty());
  // The selection follows its entry, else the first.
  CHECK(cf::Keep({1, 3, 5}, 3) == 1);
  CHECK(cf::Keep({1, 3, 5}, 4) == 0);
  CHECK(cf::Keep({}, 4) == 0);
  CHECK(cf::Contains("Green Glade", "") && cf::Contains("ABC", "b") && !cf::Contains("ab", "abc"));
  return Finish("choice_filter");
}
