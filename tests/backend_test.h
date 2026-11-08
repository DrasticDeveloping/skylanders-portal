#pragma once

#include <string>
#include <string_view>
#include <vector>

#include "test_util.h"

// The suite registry of skylanders_backend_test (design section 9, item 1): one binary, one file
// per part (tests/backend_<part>_test.cpp), each test registered under its id from the design's
// test list (section 10) and its name:
//
//   BACKEND_TEST("ID1", IdRoundTrip) {
//     CHECK(...);  // test_util.h's CHECK; a failed check counts against this test
//   }
//
// tests/backend_main.cpp runs every registered test (file by file, in the order of declaration),
// prints one line per test and its time, then test_util.h's Finish("backend"): "all backend tests
// passed" and exit code 0, or 1. Modes:
//   --long                 32 seeds and the long forms (LM1, LM10, ...); not part of the 10 s
//   --filter <text>        only tests whose id or name contains <text>
//   --list                 print the ids and names, run nothing
//   --update-golden        CheckGolden writes the golden files instead of comparing (Opus F15)
//   --golden-dir <dir>     the golden folder (default: the source tree's tests/backend/golden)
//   --diff <a> <b>         diff two map dumps (map_dump.h's ParseMap and DiffMaps), run nothing

namespace skylanders::backend::test {

// Seeds per seeded case: kept in one place (design section 11, "the test binary grows").
constexpr int kDefaultSeeds = 8;
constexpr int kLongSeeds = 32;

struct Options {
  bool long_run = false;
  bool update_golden = false;
  std::string filter;
  std::string golden_dir;
};

inline Options& MutableOptions() {
  static Options options;
  return options;
}
inline const Options& Opts() { return MutableOptions(); }
// The seed count of this run: kDefaultSeeds, or kLongSeeds with --long.
inline int Seeds() { return Opts().long_run ? kLongSeeds : kDefaultSeeds; }
inline bool Long() { return Opts().long_run; }

// Compares `actual` with the golden file `name` (in the golden folder); under --update-golden
// writes it instead. A difference is a failed check, with the first differing line printed.
// Defined in backend_main.cpp.
bool CheckGolden(std::string_view name, std::string_view actual);
// The golden folder and a file in it.
std::string GoldenPath(std::string_view name);

using TestFn = void (*)();
struct TestCase {
  const char* id;
  const char* name;
  TestFn fn;
  const char* file;
};

inline std::vector<TestCase>& Registry() {
  static std::vector<TestCase> tests;
  return tests;
}

struct Registrar {
  Registrar(const char* id, const char* name, TestFn fn, const char* file) {
    Registry().push_back({id, name, fn, file});
  }
};

}  // namespace skylanders::backend::test

// Registers a test under its design id (a string, "ID1", "VA12b", "RP-a") and its name (an
// identifier, unique in the binary).
#define BACKEND_TEST(id, name)                                                             \
  static void BackendTest_##name();                                                        \
  static const ::skylanders::backend::test::Registrar backend_registrar_##name{            \
      id, #name, &BackendTest_##name, __FILE__};                                           \
  static void BackendTest_##name()
