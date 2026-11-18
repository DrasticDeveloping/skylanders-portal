#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>

#include "dev/live_lines.h"
#include "test_util.h"

using skylanders::dev::LiveLines;
using skylanders::dev::ReadAppended;

namespace {

void Append(const std::filesystem::path& path, const std::string& bytes) {
  std::ofstream f(path, std::ios::binary | std::ios::app);
  f << bytes;
}

}  // namespace

int main() {
  // Complete lines are numbered from 1; a piece without its newline waits.
  {
    LiveLines r;
    auto lines = r.Feed("goto 1 2 3\nwaitfor goto_done 30\ntap ");
    CHECK(lines.size() == 2);
    CHECK(lines[0].number == 1 && lines[0].text == "goto 1 2 3");
    CHECK(lines[1].number == 2 && lines[1].text == "waitfor goto_done 30");
    CHECK(r.partial() == "tap ");
    CHECK(r.Feed("x").empty());  // still no newline: never run half a line
    lines = r.Feed("\n");
    CHECK(lines.size() == 1 && lines[0].number == 3 && lines[0].text == "tap x");
    CHECK(r.partial().empty());
    CHECK(r.offset() == std::string("goto 1 2 3\nwaitfor goto_done 30\ntap x\n").size());
  }
  // Blank and comment lines count (the sender counts newlines); CRLF loses its CR.
  {
    LiveLines r;
    auto lines = r.Feed("\n# note\r\nlog a\r\n");
    CHECK(lines.size() == 3);
    CHECK(lines[0].number == 1 && lines[0].text.empty());
    CHECK(lines[1].number == 2 && lines[1].text == "# note");
    CHECK(lines[2].number == 3 && lines[2].text == "log a");
  }
  // A line split across many reads, byte by byte.
  {
    LiveLines r;
    const std::string text = "dump_entities 40\nlog done\n";
    std::vector<LiveLines::Line> all;
    for (char c : text) {
      for (auto& l : r.Feed(std::string_view(&c, 1))) all.push_back(l);
    }
    CHECK(all.size() == 2);
    CHECK(all[0].number == 1 && all[0].text == "dump_entities 40");
    CHECK(all[1].number == 2 && all[1].text == "log done");
  }
  // The file, appended while it is read: each read takes what is there after the offset.
  {
    const auto path = std::filesystem::temp_directory_path() / "skylanders_live_lines_test.txt";
    std::filesystem::remove(path);
    LiveLines r;
    CHECK(ReadAppended(path, r.offset()).empty());  // a missing file reads as nothing yet
    Append(path, "log one\nlog tw");
    auto lines = r.Feed(ReadAppended(path, r.offset()));
    CHECK(lines.size() == 1 && lines[0].number == 1 && lines[0].text == "log one");
    CHECK(ReadAppended(path, r.offset()).empty());  // nothing new
    {
      // A writer that keeps the file open while the reader reads (shared access).
      std::ofstream writer(path, std::ios::binary | std::ios::app);
      writer << "o\nlog three";
      writer.flush();
      lines = r.Feed(ReadAppended(path, r.offset()));
      CHECK(lines.size() == 1 && lines[0].number == 2 && lines[0].text == "log two");
      writer << "\n";
      writer.flush();
      lines = r.Feed(ReadAppended(path, r.offset()));
      CHECK(lines.size() == 1 && lines[0].number == 3 && lines[0].text == "log three");
    }
    std::filesystem::remove(path);
  }
  return Finish("live_lines");
}
