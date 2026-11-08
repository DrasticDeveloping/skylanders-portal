// skylanders_backend_test's main: runs the suites registered with BACKEND_TEST (backend_test.h),
// and the --diff mode for two map dumps (design sections 7.1, 9 and 10).

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

#include "backend/map_dump.h"
#include "backend_test.h"

namespace skylanders::backend::test {

namespace {

// The source tree's golden folder, set by src/backend/CMakeLists.txt.
#ifdef SKYLANDERS_BACKEND_GOLDEN_DIR
constexpr const char* kGoldenDir = SKYLANDERS_BACKEND_GOLDEN_DIR;
#else
constexpr const char* kGoldenDir = "tests/backend/golden";
#endif

bool ReadFile(const std::string& path, std::string& out) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return false;
  std::ostringstream text;
  text << in.rdbuf();
  out = text.str();
  return true;
}

bool WriteFile(const std::string& path, std::string_view text) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  if (!out) return false;
  out.write(text.data(), std::streamsize(text.size()));
  return bool(out);
}

// The line (from 1) where two texts first differ, and both lines.
void PrintFirstDifference(std::string_view expected, std::string_view actual) {
  size_t line = 1, at = 0;
  while (at < expected.size() && at < actual.size() && expected[at] == actual[at]) {
    if (expected[at] == '\n') ++line;
    ++at;
  }
  auto line_at = [](std::string_view text, size_t pos) {
    size_t start = 0;
    if (pos > 0) {
      const size_t newline = text.rfind('\n', pos - 1);
      if (newline != std::string_view::npos) start = newline + 1;
    }
    return std::string(text.substr(start, text.find('\n', start) - start));
  };
  std::fprintf(stderr, "  first difference at line %zu\n  golden: %s\n  actual: %s\n", line,
               line_at(expected, at).c_str(), line_at(actual, at).c_str());
}

int RunDiff(const char* a_path, const char* b_path) {
  std::string a_text, b_text;
  if (!ReadFile(a_path, a_text) || !ReadFile(b_path, b_text)) {
    std::fprintf(stderr, "cannot read %s or %s\n", a_path, b_path);
    return 2;
  }
  std::string error;
  const std::optional<MapDump> a = ParseMap(a_text, &error);
  if (!a) {
    std::fprintf(stderr, "%s: %s\n", a_path, error.c_str());
    return 2;
  }
  const std::optional<MapDump> b = ParseMap(b_text, &error);
  if (!b) {
    std::fprintf(stderr, "%s: %s\n", b_path, error.c_str());
    return 2;
  }
  const std::vector<MapDiff> diffs = DiffMaps(*a, *b);
  for (const MapDiff& d : diffs) std::printf("%s\n", d.text.c_str());
  std::printf("map diff %s %s: %zu differences\n", a->who.c_str(), b->who.c_str(), diffs.size());
  return diffs.empty() ? 0 : 1;
}

void Usage() {
  std::fprintf(stderr,
               "usage: skylanders_backend_test [--long] [--filter <text>] [--list] [--update-golden]\n"
               "                               [--golden-dir <dir>]\n"
               "       skylanders_backend_test --diff <map a> <map b>\n");
}

}  // namespace

std::string GoldenPath(std::string_view name) {
  std::string dir = Opts().golden_dir.empty() ? std::string(kGoldenDir) : Opts().golden_dir;
  if (!dir.empty() && dir.back() != '/' && dir.back() != '\\') dir.push_back('/');
  return dir + std::string(name);
}

bool CheckGolden(std::string_view name, std::string_view actual) {
  const std::string path = GoldenPath(name);
  if (Opts().update_golden) {
    if (!WriteFile(path, actual)) {
      std::fprintf(stderr, "FAIL cannot write golden file %s\n", path.c_str());
      ++g_failures;
      return false;
    }
    std::printf("wrote golden file %s\n", path.c_str());
    return true;
  }
  std::string expected;
  if (!ReadFile(path, expected)) {
    std::fprintf(stderr, "FAIL golden file %s missing (run with --update-golden to write it)\n",
                 path.c_str());
    ++g_failures;
    return false;
  }
  // Checked-out text may have CRLF line ends; the dumps write LF.
  std::erase(expected, '\r');
  if (expected != actual) {
    std::fprintf(stderr, "FAIL %s differs from the golden file\n", path.c_str());
    PrintFirstDifference(expected, actual);
    ++g_failures;
    return false;
  }
  return true;
}

}  // namespace skylanders::backend::test

int main(int argc, char** argv) {
  using namespace skylanders::backend::test;
  Options& options = MutableOptions();
  bool list = false;
  for (int i = 1; i < argc; ++i) {
    const std::string_view arg = argv[i];
    if (arg == "--long") {
      options.long_run = true;
    } else if (arg == "--update-golden") {
      options.update_golden = true;
    } else if (arg == "--list") {
      list = true;
    } else if (arg == "--filter" && i + 1 < argc) {
      options.filter = argv[++i];
    } else if (arg == "--golden-dir" && i + 1 < argc) {
      options.golden_dir = argv[++i];
    } else if (arg == "--diff" && i + 2 < argc) {
      return RunDiff(argv[i + 1], argv[i + 2]);
    } else {
      Usage();
      return 2;
    }
  }

  // File by file, each in the order of declaration (static initialization runs in that order
  // within one translation unit; across files it is unspecified, so the files are sorted).
  std::vector<TestCase> tests = Registry();
  std::stable_sort(tests.begin(), tests.end(), [](const TestCase& a, const TestCase& b) {
    return std::strcmp(a.file, b.file) < 0;
  });

  const auto start = std::chrono::steady_clock::now();
  size_t run = 0, failed = 0;
  for (const TestCase& t : tests) {
    if (!options.filter.empty() && std::string_view(t.id).find(options.filter) == std::string_view::npos &&
        std::string_view(t.name).find(options.filter) == std::string_view::npos) {
      continue;
    }
    if (list) {
      std::printf("%s %s\n", t.id, t.name);
      continue;
    }
    const int before = g_failures;
    const auto t0 = std::chrono::steady_clock::now();
    t.fn();
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0);
    ++run;
    const bool ok = g_failures == before;
    failed += !ok;
    std::printf("%s %s %s (%lld ms)\n", ok ? "ok" : "FAIL", t.id, t.name, static_cast<long long>(ms.count()));
  }
  if (list) return 0;

  const auto total = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - start);
  std::printf("%zu backend tests, %zu failed, %lld ms%s\n", run, failed, static_cast<long long>(total.count()),
              options.long_run ? " (--long)" : "");
  if (run == 0 && g_failures == 0) {
    std::fprintf(stderr, "FAIL no test matched\n");
    ++g_failures;
  }
  return Finish("backend");
}
