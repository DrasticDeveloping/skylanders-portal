#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <string_view>
#include <vector>

// The backend core of Swap Force Online (stage 2, docs/online-2.md in the game repository): records
// with revisions under an epoch, one change log with a cursor per peer, baselines, obligations,
// leases and requests, over a transport contract (PeerLink). Game-independent: it includes no
// online/ header, only netplay's (D2). The game reaches it through adapters (adapter.h) from stage 3.
//
// One thread, no clock of its own (D3): the owner calls Pump(now) and passes `now` to every call
// that needs time; tests pass the fake network's time, so every test repeats exactly. Nothing in
// the library is thread-safe.

namespace skylanders::backend {

// The steady clock, as netplay's (netplay::TimePoint is the same type), so the fake network's time
// goes straight in.
using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;
using Duration = Clock::duration;

// A peer's role in the session: the server peer (id 0), the owner's client (id 1), a guest (2 to 4).
enum class PeerRole : uint8_t {
  kServer = 0,
  kOwner = 1,
  kGuest = 2,
};
// "server", "owner", "guest".
std::string_view PeerRoleName(PeerRole role);

// Why a request, a report or a lease was refused or ended (design section 3.9). Generic only (D11):
// it lives here, not in request.h, so lease.h needs no request.h (Opus F12). A kind's own reason
// is kKind plus the decision's opaque kind_reason byte, named by the kind's reason_name; 1B's
// RefusalReason mapping lives in the game's stage 3/4 bridge.
enum class Reason : uint8_t {
  kNone = 0,
  kStaleGeneration,
  kSessionEnded,
  kRate,
  kLimit,
  kStaleBasis,
  kLeased,
  kLeaseExpired,
  kNotHolder,
  kModel,
  kParentRefused,
  kParentMissing,
  kClosedByChild,
  kPreconditionMissing,
  kUnresolved,
  kUnsupported,
  kKind,
};
// "none", "stale generation", "session ended", ... (the request and map lines' words).
std::string_view ReasonName(Reason reason);

// Bounded little-endian writer: appends to a byte vector, never past `limit` bytes in all. A write
// that would pass the limit writes nothing and sets the failed flag; the caller checks Ok() once at
// the end (the codec drops the whole message then).
class Writer {
 public:
  explicit Writer(std::vector<uint8_t>& out, size_t limit = std::numeric_limits<size_t>::max());

  void U8(uint8_t v);
  void U16(uint16_t v);
  void U32(uint32_t v);
  void U64(uint64_t v);
  void Bytes(std::span<const uint8_t> b);
  void Bytes(std::string_view s);
  // Overwrites a u16 written earlier at `offset` (a message's length field).
  void PatchU16(size_t offset, uint16_t v);

  size_t Size() const;
  // Bytes that may still be written before the limit.
  size_t Room() const;
  bool Ok() const;
  void Fail();

 private:
  void Le(uint64_t v, int n);

  std::vector<uint8_t>& out_;
  size_t limit_;
  bool failed_ = false;
};

// Bounded little-endian reader over a span. A read past the end sets the failed flag and returns 0
// (an empty span for Bytes); the caller checks Ok() once at the end.
class Reader {
 public:
  explicit Reader(std::span<const uint8_t> in);

  uint8_t U8();
  uint16_t U16();
  uint32_t U32();
  uint64_t U64();
  std::span<const uint8_t> Bytes(size_t n);
  // Moves past `n` bytes (a field or a message the reader does not know).
  void Skip(size_t n);

  size_t Remaining() const;
  size_t Position() const;
  bool Ok() const;
  void Fail();

 private:
  uint64_t Le(int n);

  std::span<const uint8_t> in_;
  size_t pos_ = 0;
  bool failed_ = false;
};

}  // namespace skylanders::backend
