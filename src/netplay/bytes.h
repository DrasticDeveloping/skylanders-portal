#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace skylanders::netplay {

// Little-endian writer that appends to a byte vector.
class ByteWriter {
 public:
  explicit ByteWriter(std::vector<uint8_t>& out) : out_(out) {}

  void U8(uint8_t v) { out_.push_back(v); }
  void U16(uint16_t v) { Le(v, 2); }
  void U32(uint32_t v) { Le(v, 4); }
  void U64(uint64_t v) { Le(v, 8); }
  void Bytes(std::span<const uint8_t> b) { out_.insert(out_.end(), b.begin(), b.end()); }
  void Bytes(std::string_view s) { out_.insert(out_.end(), s.begin(), s.end()); }
  size_t Size() const { return out_.size(); }
  // Overwrites a u16 written earlier at `offset`.
  void PatchU16(size_t offset, uint16_t v) {
    out_[offset] = uint8_t(v);
    out_[offset + 1] = uint8_t(v >> 8);
  }

 private:
  void Le(uint64_t v, int n) {
    for (int i = 0; i < n; ++i) out_.push_back(uint8_t(v >> (8 * i)));
  }
  std::vector<uint8_t>& out_;
};

// Little-endian reader over a span. A read past the end sets the failed flag and returns 0; the
// caller checks Ok() once at the end.
class ByteReader {
 public:
  explicit ByteReader(std::span<const uint8_t> in) : in_(in) {}

  uint8_t U8() { return uint8_t(Le(1)); }
  uint16_t U16() { return uint16_t(Le(2)); }
  uint32_t U32() { return uint32_t(Le(4)); }
  uint64_t U64() { return Le(8); }
  std::span<const uint8_t> Bytes(size_t n) {
    if (failed_ || in_.size() - pos_ < n) {
      failed_ = true;
      return {};
    }
    auto s = in_.subspan(pos_, n);
    pos_ += n;
    return s;
  }
  std::string String(size_t n) {
    auto b = Bytes(n);
    return std::string(b.begin(), b.end());
  }
  size_t Remaining() const { return failed_ ? 0 : in_.size() - pos_; }
  size_t Position() const { return pos_; }
  bool Ok() const { return !failed_; }
  void Fail() { failed_ = true; }

 private:
  uint64_t Le(int n) {
    if (failed_ || in_.size() - pos_ < size_t(n)) {
      failed_ = true;
      return 0;
    }
    uint64_t v = 0;
    for (int i = 0; i < n; ++i) v |= uint64_t(in_[pos_ + i]) << (8 * i);
    pos_ += n;
    return v;
  }
  std::span<const uint8_t> in_;
  size_t pos_ = 0;
  bool failed_ = false;
};

}  // namespace skylanders::netplay
