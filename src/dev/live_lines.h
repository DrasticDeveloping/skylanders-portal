// The input script's live mode (`follow <file>`, input_script.h): the complete lines appended to a
// file that another process (the test runner's `live send`) keeps writing. No ReXGlue dependency,
// so it is unit-tested (tests/live_lines_test.cpp).
//
// Only a line that ends in a newline is taken: a line written in pieces (or the end of a file still
// being written) waits until its newline is there, so a partly written line never runs. Lines are
// numbered from 1 in file order, every complete line counts (a blank or comment line too), so a
// sender that counts the newlines it wrote knows the number the game logs for its line.

#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace skylanders::dev {

class LiveLines {
 public:
  struct Line {
    uint64_t number;   // 1 = the file's first line
    std::string text;  // without its line end (a CR before the LF is dropped too)
  };

  // Takes the bytes read after the last call; returns the lines they complete, in order. A piece of
  // a line without its newline is kept for the next call.
  std::vector<Line> Feed(std::string_view bytes) {
    std::vector<Line> out;
    offset_ += bytes.size();
    partial_.append(bytes);
    size_t start = 0;
    for (size_t nl; (nl = partial_.find('\n', start)) != std::string::npos; start = nl + 1) {
      std::string text = partial_.substr(start, nl - start);
      if (!text.empty() && text.back() == '\r') text.pop_back();
      out.push_back({next_number_++, std::move(text)});
    }
    partial_.erase(0, start);
    return out;
  }

  // Bytes taken so far (the complete lines and the piece kept): where the next read starts.
  uint64_t offset() const { return offset_; }
  // The piece of a line still waiting for its newline.
  const std::string& partial() const { return partial_; }

 private:
  std::string partial_;
  uint64_t next_number_ = 1;
  uint64_t offset_ = 0;
};

// What the file holds after `offset` right now (empty if it is missing or has nothing new). The file
// is opened for this read only, shared, so the writer may append at the same time: what is read is
// a prefix of what will be there, and the rest comes with the next read.
inline std::string ReadAppended(const std::filesystem::path& path, uint64_t offset) {
  std::ifstream file(path, std::ios::binary);
  if (!file) return {};
  file.seekg(0, std::ios::end);
  const std::streamoff end = file.tellg();
  if (end < 0 || uint64_t(end) <= offset) return {};
  file.seekg(std::streamoff(offset), std::ios::beg);
  std::string bytes(size_t(uint64_t(end) - offset), '\0');
  file.read(bytes.data(), std::streamsize(bytes.size()));
  bytes.resize(size_t(file.gcount()));
  return bytes;
}

}  // namespace skylanders::dev
