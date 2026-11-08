// The parity suite's client-side cell (design sections 3.9 and 10): Client::Resend from the retry
// state, the client half of 1B's RetryResendChecksAgain (RP19). It runs on the backend alone
// (FakeSession, no 1B header), so it stays here under the include rule (D2).
//
// Every other parity cell is in tests/parity_requests_test.cpp: 1B's 67 request-table cells (RP1 to
// RP67) run on 1B's own table and on the generic one at once, through section 3.9's key mapper
// (tests/parity_twin.h), plus RP0, RP-a, RP-b, RP8b and RP-c to RP-e. Those files include online/
// headers, so they live outside tests/backend*; nothing here includes them (include_rule.cmake
// refuses it).

#include <chrono>
#include <cstdint>
#include <optional>

#include "backend/request.h"
#include "backend_lanes_harness.h"
#include "backend_request_harness.h"
#include "backend_test.h"

namespace {

using namespace skylanders::backend;
using namespace std::chrono_literals;
namespace lanes = skylanders::backend::test::lanes;
namespace req = skylanders::backend::test::requests;

// RP19b: 1B's RetryResendChecksAgain (RP19) from the client: Client::Resend sends a key in the
// retry state again, unchanged; S checks it again (not from the cache). Refused locally for a final
// key, an unknown one, and a kNeverResent one.
BACKEND_TEST("RP19b", ParityRetryResendThroughClient) {
  lanes::Rig r;
  r.Join(1);
  const EntityId e = lanes::Ent(r.session.Generation(), 10);
  r.Everywhere(e, r.Now());
  CHECK(r.session.RunUntil([&] { return r.C(1).PeerEpoch() != 0; }, 1s));
  int calls = 0;
  r.authorities.authority.custom = [&](const Request&, const DecideContext&) -> std::optional<Verdict> {
    if (++calls == 1) return req::Refusal(Reason::kKind, /*retryable=*/true, 1);
    return std::nullopt;
  };
  const auto key = r.C(1).Send(lanes::Commit(test::kProp, e, lanes::Val(3)), r.Now());
  REQUIRE_OR_RETURN_VOID(key.has_value());
  CHECK(r.session.RunUntil(
      [&] {
        for (const Decision& d : lanes::DecisionsFor(r.session, 1, *key)) {
          if (d.outcome == Outcome::kRefused && !d.final) return true;
        }
        return false;
      },
      1s));
  CHECK(r.S().Requests().StateOf(*key) == RequestState::kRetry);
  CHECK(r.C(1).Resend(*key, r.Now()));
  CHECK(r.session.RunUntil([&] { return lanes::FinalFor(r.session, 1, *key).has_value(); }, 1s));
  const auto fin = lanes::FinalFor(r.session, 1, *key);
  CHECK(fin && fin->outcome == Outcome::kApplied && !fin->cached);
  CHECK(calls == 2);
  CHECK(!r.C(1).Resend(*key, r.Now()));  // final
  CHECK(!r.C(1).Resend(RequestKey{1, r.C(1).PeerEpoch(), r.session.Generation(), 99}, r.Now()));
  // A kNeverResent request: S makes every refusal terminal, and the client never resends it.
  calls = 0;
  Request tele = lanes::Commit(test::kProp, e, lanes::Val(4));
  tele.flags = kNeverResent;
  const auto tkey = r.C(1).Send(tele, r.Now());
  REQUIRE_OR_RETURN_VOID(tkey.has_value());
  CHECK(r.session.RunUntil([&] { return lanes::FinalFor(r.session, 1, *tkey).has_value(); }, 1s));
  CHECK(lanes::FinalFor(r.session, 1, *tkey)->outcome == Outcome::kRefused);
  CHECK(!r.C(1).Resend(*tkey, r.Now()));
  r.authorities.authority.custom = nullptr;
}

}  // namespace
