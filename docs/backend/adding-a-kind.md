# Adding a kind

A kind is one sort of thing the online backend keeps in step between the server (S) and the
clients: a gate's state, a prop's position, a chest's coins. Each kind is one row in a kind table,
one adapter that reads and writes it in the game, an authority if clients may ask to change it,
and one block in the converge template that proves it converges under loss and faults.

The backend (`src/backend`, CMake target `skylanders_backend`) knows no game. A game's kinds, its
adapters and its authorities live in the game; the library only checks and carries them. This page
walks the test kind `test.toggle` from its table row to its converge block, then leaves you to add
`test.mover` on your own.

The design behind all this is stage 2 of Swap Force Online (`docs/online-2.md` in the game
repository). Section numbers below are that document's.

## 1. Build and run the tests

The backend is behind a switch, off by default. It needs no SDK: from the x64 Native Tools Command
Prompt for VS 2022, a portal checkout builds it on its own into any folder:

```
cmake -S . -B out -G Ninja -DCMAKE_BUILD_TYPE=Release -DSKYLANDERS_BACKEND=ON
cmake --build out --target skylanders_backend_test
ctest --test-dir out/src/backend --output-on-failure
```

(In a build with `-DREXSDK_DIR=<path to rexglue-sdk>` the top level enables testing too, and
`ctest --test-dir out -R backend` finds the same two tests.)

`ctest` runs two tests: `skylanders_backend_test` (every backend test, one binary) and
`backend.include_rule` (below). The binary runs alone too:

| Option | What it does |
|---|---|
| (none) | every test, 8 seeds per seeded case, under 10 s; prints one line per test and `all backend tests passed` |
| `--filter <text>` | only tests whose id or name contains the text (`--filter CT1`, `--filter Compare`) |
| `--list` | the ids and names, nothing run |
| `--long` | 32 seeds and the long forms; run once before a pull request |
| `--update-golden` | rewrites the golden files instead of comparing them (see section 7) |
| `--golden-dir <dir>` | another golden folder |
| `--diff <a> <b>` | diffs two map dumps (section 7) and runs nothing |

## 2. The rule you cannot break: no game in the library

Nothing under `src/backend` or `tests/backend*` may include an `online/` header (the session
library and the game are not the backend's business; netplay's headers are allowed). Two checks
hold the rule:

- at configure time, `src/backend/CMakeLists.txt` runs `src/backend/include_rule.cmake` and fails
  the configure on an `#include "online/...` line;
- the ctest `backend.include_rule` runs the same script on every test run, so a file or an include
  added after the last configure fails the next `ctest` without a reconfigure.

If you need something from the game in a kind, it goes in the game's adapter, not in the library.

## 3. Decide what the kind is: the table row

A kind is a `KindSpec` (`src/backend/kind_table.h`). `test.toggle` is in
`tests/backend_test_kinds.h`:

```cpp
inline constexpr KindSpec kTestToggle{
    .id = kToggle, .name = "test.toggle", .scope = Scope::kLevel, .tier = Tier::kStructural,
    .state_class = StateClass::kCurrentValue, .initial = InitialPolicy::kReadAtSpawn,
    .write = WriteMode::kApplied, .models = kModelCommit | kModelServerRun, .max_value_bytes = 8,
    .pending_grace_ms = 5000};
```

Each field is a decision. Answer them in this order:

| Field | Question | test.toggle |
|---|---|---|
| `id`, `name` | A number and a name no other kind in the table uses. The name shows in every log line and dump. | 1, `test.toggle` |
| `scope` | Does it end with the level (`kLevel`: dropped at every generation change) or last the whole session (`kSession`)? | level |
| `tier` | `kStructural` (state that must converge), `kDurableEvent` (happens once, must reach its recipient: an obligation), `kEffect` (best effort, never stored: sparks, sounds). | structural |
| `state_class` | `kCurrentValue` (the newest revision replaces the old) or `kEvent` (once only; goes with `kDurableEvent`). | current value |
| `initial` | Where its first value comes from: `kReadAtSpawn` (S reads its world when the entity spawns), `kAuthoredDefault` (a fixed default in `authored_default`), `kNever` (no peer can read it: the kind is listed `unsupported`). | read at spawn |
| `write` | What a client does with a row: `kApplied` (its adapter writes the game), `kMirrored` (kept and shown, nothing written), `kServerOnly` (never leaves S). | applied |
| `lane` | `kState` for everything stored; `kFast` only for an effect. | state |
| `models` | How a client may ask for a change: `kModelCommit` (S decides and writes), `kModelServerRun` (S's game runs it, then writes), `kModelOwnerRun` (the asking client runs it under a lease and reports; needs `leasable`). 0: clients never ask. | commit, server-run |
| `noisy` | Does it change many times a second (a moving thing)? Then S logs it at most `limits.noisy_hz` times a second per row, the last value winning. | no |
| `max_value_bytes` | The largest value, at most 512. | 8 |
| `pending_grace_ms` | How long a client waits for the row's entity (not spawned yet, streamed out) before the row is late. | 5000 |
| `depends_on` | Kinds whose rows must be applied first (a scene that needs its toggle). A kind with dependencies is "fenced": its rows wait for the earlier rows they depend on. | none |

Then run the validator. The test `VA13b ValidatorTestTable` (`tests/backend_kind_table_test.cpp`)
checks the test table; a game's table gets the same one-line test:

```cpp
CHECK(Validate(kMyTable).empty());
```

`Validate` returns one issue per broken rule, with the rule's short name. The rules, so you can
avoid them up front:

| Rule | Says |
|---|---|
| V1 ids | every id non-zero and unique; every name non-empty and unique |
| V2 initial | `initial` is set |
| V3 never | `kNever` is never `kApplied` and is listed `unsupported`; `unsupported` needs `kNever` |
| V4 cycles | every dependency exists, is not the kind itself, and the graph has no cycle |
| V5 scope | a session kind depends only on session kinds |
| V6 events | `kDurableEvent` if and only if `kEvent` |
| V7 lanes | `kFast` only for an effect |
| V8 default | `kAuthoredDefault` has a default of at most `max_value_bytes`; no other policy has one |
| V9 sizes | 0 < `max_value_bytes` <= `limits.max_value_bytes`; state classes 1 and 2 are reserved |
| V10 models | owner-run needs `leasable`; server-only takes no owner-run; `parent_wait_ms` and `requires_precondition` only on a kind that takes requests |
| V11 noisy | `noisy` only on a current value |
| V12 grace | an applied kind read at spawn has `pending_grace_ms` > 0; so has an applied kind another kind depends on |
| V13 replicated deps | a replicated kind depends only on replicated kinds |

Changing any data field of a kind changes `TableDigest`, which S and every client compare in
`StateHello`: two builds with different tables refuse each other.

The limits are never silent: a write past `max_value_bytes`, `limits.max_rows_per_level` or
`limits.max_rows_per_session`, whether the game's own `Put` or a request's commit, is refused and
S raises a `kLimit` event naming the limit and the kind (`max_rows_per_session kind test.counter
scope session`). If the census sizes a kind wrong, that event is where it shows.

## 4. Teach the game to read and write it: the adapter

An adapter (`src/backend/adapter.h`) is the only code that touches the game. It is called on the
game thread only.

```cpp
class KindAdapter {
 public:
  virtual AdapterResult Read(Handle h, uint16_t slot, std::vector<uint8_t>& value) = 0;
  virtual AdapterResult Apply(Handle h, uint16_t slot, std::span<const uint8_t> value) = 0;
  virtual AdapterResult Retire(Handle h, uint16_t slot);           // a removed row (tomb)
  virtual AdapterResult Fulfil(Handle h, const Obligation& o);     // an event kind's obligation
};
```

- `Read` fills the value from the entity behind the handle. S uses it at spawn (`kReadAtSpawn`) and
  every peer uses it in the compare queries.
- `Apply` writes a value into the game. Answer `kOk` when written, `kGone` when the entity went
  away meanwhile, `kFailed` when the write did not take (the row is retried at the next pump, and is
  late after its grace), `kUnsupported` when this peer cannot write it at all.
- `Retire` is for a tomb; `Fulfil` only for a `kDurableEvent` kind.

The adapter never looks entities up by itself. The game reports spawns and despawns to the peer's
`IdentityMap` (`AdapterSet::Identity()`), and the backend resolves a row's `EntityId` to a handle
before it calls the adapter. A level entity placed in the map is `Authored(generation, mID, name
hash, class hash)`; a spawned one is `Spawned(...)`; a player's own row is `Player(peer, peer
epoch)`; a session object is `SessionObject(id)` (`src/backend/entity_id.h`).

`test.toggle`'s adapter in `tests/backend_converge_test.cpp` is the fake world's plain adapter
(`FakeKindAdapter` in `src/backend/fake_world.h`, whose value is the entity's field) with one
check: a toggle is one byte, 0 or 1, and anything else is refused, so a bad row
shows as a failed apply and never as a silent write:

```cpp
class FakeToggleAdapter : public FakeKindAdapter {
 public:
  using FakeKindAdapter::FakeKindAdapter;
  AdapterResult Apply(Handle h, uint16_t slot, std::span<const uint8_t> value) override {
    if (value.size() != 1 || value[0] > 1) return AdapterResult::kFailed;
    return FakeKindAdapter::Apply(h, slot, value);
  }
};
std::unique_ptr<KindAdapter> MakeFakeToggleAdapter(FakeWorld& world, KindId kind) {
  return std::make_unique<FakeToggleAdapter>(world, kind);
}
```

In a game, the adapter reads and writes the entity's fields at their guest addresses instead of the
fake world's fields.

## 5. Decide who may change it: the authority

A kind with `models` set needs an `Authority` on S (`src/backend/request.h`). `Decide` is pure: it
reads the request and the records and returns a verdict; only the request table writes.

| Verdict | Means |
|---|---|
| `kCommit` with `writes` | S writes them all or none (`WorldState::ApplyAll`) and answers `applied` |
| `kServerRun` | S's game thread runs it (a `kEvaluate` event) and answers with `SubmitEvaluation` |
| `kOwnerRun` with a `lease` | the asking client runs it under the lease and reports; `OnReport` turns the report into writes. Its `writes`, if any, are applied with the grant, all or none (a held row); a set not written refuses the grant |
| `kRefuse` with a `reason` | refused; `retryable` lets the client send it again |
| `kWait` | S waits up to `wait` for something it needs |

`test.toggle`'s authority refuses a payload that is not a toggle and otherwise commits it:

```cpp
class ToggleAuthority : public FieldAuthority {
 public:
  using FieldAuthority::FieldAuthority;
  Verdict Decide(const Request& request, const DecideContext& context) override {
    if (request.payload.size() != 1 || request.payload[0] > 1) {
      Verdict refused;  // kRefuse
      refused.reason = Reason::kModel;
      return refused;
    }
    return FieldAuthority::Decide(request, context);
  }
};
std::unique_ptr<Authority> MakeToggleAuthority(KindId kind) {
  return std::make_unique<ToggleAuthority>(kind);
}
```

An owner-run kind may also override `OnLeaseEnd(lease, why, context)`: the writes for a lease
that ends without a commit (expired, report refused, its holder left, End session), applied all or
none where the end happens. It is pure like `Decide`, and it is not called at a commit or at a
generation change. With grant writes it keeps one writer for a held row: the grant sets it, the
commit or `OnLeaseEnd` clears it.

A kind with `models = 0` needs no authority: only S's own world changes it.

## 6. Prove it converges: the converge block

`tests/backend_converge.h` runs one kind through fault profiles on S and two clients (the owner and
a guest), each with its own fake world (`src/backend/fake_world.h`: entities with fields, spawns,
despawns, streaming, and the write faults), then grades the end. A kind copies one block. `test.toggle`'s is test `CT1
ConvergeTemplateToggle` in `tests/backend_converge_test.cpp`:

```cpp
BACKEND_TEST("CT1", ConvergeTemplateToggle) {
  ConvergeKind toggle{
      .spec = kTestToggle, .adapter = MakeFakeToggleAdapter, .authority = MakeToggleAuthority,
      .script = [](ConvergeScript& s) { s.Spawn(20); s.WriteEvery(50ms, 3s); s.Request(Model::kCommit, 10); },
  };
  toggle.value = [](uint32_t n) { return std::vector<uint8_t>{uint8_t(n & 1)}; };
  RunConverge(toggle, {Profile::kPlain, Profile::kLoss5Rtt300, Profile::kLoss30Rtt300,
                       Profile::kKilledDelta, Profile::kMidBaselineDisconnect,
                       Profile::kThrottledPeer, Profile::kLateJoiner,
                       Profile::kTeardownDuringApply, Profile::kStreamOut},
              /*seeds=*/kDefaultSeeds);  // 8; 32 with --long
}
```

What the block says:

- `.spec`, `.adapter`, `.authority`: the three things above. Leave `.adapter` or `.authority` out
  and the template's plain ones are used (`MakeFieldAdapter`: the value is the entity's field;
  `MakeFieldAuthority`: a commit writes the payload).
- `.script`: `Spawn(n)` puts n entities of the kind in the level on every peer; `WriteEvery(i, d)`
  has S's world change a random one every i for d; `Request(model, n)` has the clients send n
  requests of that model, spread over the writes.
- `.value`: the nth value the script writes. Without it the value is n in up to four bytes; a
  toggle needs 0 and 1.
- `.table`: set it when the kind depends on other kinds (they must be in the same table); the
  others get the plain adapter and authority.

The profiles (every one runs on every seed):

| Profile | What happens |
|---|---|
| `kPlain` | memory links, 10 ms each way |
| `kLoss5Rtt300`, `kLoss30Rtt300` | netplay's lanes over its fake network, 150 ms each way, 5 % or 30 % of datagrams lost |
| `kKilledDelta` | the third delta S sends is killed above the lane (a bug model): one manifest mismatch, one repair baseline |
| `kMidBaselineDisconnect` | the guest's link drops during its first baseline; it rejoins 1 s later |
| `kThrottledPeer` | the guest's link carries 1,000 bytes a second |
| `kLateJoiner` | the guest joins halfway through the writes |
| `kTeardownDuringApply` | halfway through, the level is torn down while rows are in flight and loaded again in the next generation |
| `kStreamOut` | the owner streams three entities out for 1.5 s; their rows wait, then apply |

After the writes the session goes quiet for `limits.compare_quiet`, then each run is graded:

- every row of every replicated kind is `equal` or `repaired` on every client (`unsupported` for a
  `kNever` kind), none still pending;
- the invariant checker passed: one final decision per request, no obligation fulfilled twice, no
  client over its obligation credit, no revision going down on a replica, no fenced row applied
  before the rows it depends on (unless a `fence broken` event names the pair in a faulty run), no
  request completed before its row arrived, every row the client took for its adapters answered in
  the same pump, S never counting fewer obligations used than a client holds, and every injected
  fault fired (a fault that never fired means the profile's case was not reached, and the run
  fails).

A failed run prints its kind, profile, seed and reasons, then the map diff of S against each
client:

```
FAIL converge test.toggle killed-delta seed 3 at 12040 ms: backend compare 1 test.toggle a00000105.1c2a3b4d.5e6f7a8b@1 stuck rev 4/3 differs replica behind
    diff S peer 1: test.toggle a00000105.1c2a3b4d.5e6f7a8b@1 slot 0 revision a 4 b 3
```

Rerun just that case with `skylanders_backend_test --filter CT1` and fix the cause; never widen the
grade to let it pass.

## 7. Reading what went wrong: compare lines and map dumps

The compare queries (`src/backend/compare.h`) run in two halves: `CompareLocal` on each peer (its
copy against the live value its adapter reads; with repair, one `Apply` and one read again), then
`JoinCompare` of S's lines with one client's, over every key either side has. The results:

| Result | Means |
|---|---|
| `equal` | the copies agree and the live read agrees on every side |
| `repaired` | a live value differed and one apply fixed it |
| `pending` | the entity is not there yet (with how long) |
| `stuck` | anything else: `replica missing`, `replica extra`, `replica behind`, `replica differs`, or a side's own words (`server: value ...`, `client: gone`, `server: unsupported`) |
| `unsupported` | both sides cannot read the kind (a `kNever` kind) |

Their lines, as the game will log them from stage 3:

```
backend local <peer> <kind> <entity> slot <n> rev <r> digest <hex> <result>[ pending <ms>][ differs <what>]
backend compare <peer> <kind> <entity> <result> rev <s>/<r>[ differs <what>]
backend compared <peer> <kind> equal <n> repaired <n> pending <n> stuck <n> unsupported <n>
```

A map dump (`src/backend/map_dump.h`) is a peer's whole state as text, one line per row, sorted so
two dumps diff line by line:

```
map S head 8 prefix 8 epoch 1 rows 5 bytes 7 table c1ff1df2e76b85c6
row md.count p1/1 slot 0 rev 1 lsn 4 epoch 0 writer 0 value 0500 digest 08218a07b4dd0020
row md.gate a00000011.c07d63cf.968d3732@1 slot 0 rev 2 lsn 8 epoch 1 writer 0 tomb value - digest cbf29ce484222325
row md.secret a00000020.45496baf.d432914f@1 slot 0 rev 1 lsn 6 epoch 1 writer 0 server-only value abcd digest 09979b07b61b0365
```

`skylanders_backend_test --diff a.txt b.txt` prints what differs between two dumps (rows only on one
side, another revision, a tomb on one side, another value; server-only rows, LSNs and writers are
not compared).

Both formats are pinned by golden files in `tests/backend/golden` (`map.txt` by `MD1`,
`compare.txt` by `CQ1`). The tests only compare them. If you change a format on purpose, run
`skylanders_backend_test --update-golden`, read the diff of the golden files, and commit them with
the change that explains them.

## 8. Checklist

1. The table row, each field answered as in section 3; `Validate` returns nothing.
2. The adapter: `Read` and `Apply` (and `Retire`, `Fulfil` where they apply), on the game thread,
   entities through the `IdentityMap`.
3. The authority, if `models` is set; `Decide` reads and returns, never writes.
4. The converge block, with a script that exercises what the kind does; `--filter CT` passes, and
   `--long` passes once before the pull request.
5. No `online/` include anywhere under `src/backend` or `tests/backend*`; `ctest` passes both tests.

## 9. Your turn: test.mover

`test.mover` is a moving platform: a current value written many times a second, which S must not
send at that rate. It is in no table yet. Add it from this page alone:

- It is a level-scope structural current value, applied on the clients, read at spawn, and
  `noisy` (V11 allows `noisy` only on a current value).
- Its value is a position: three little-endian `int32` (12 bytes), so `max_value_bytes = 12`.
- Being applied and read at spawn, it needs a `pending_grace_ms` above 0 (V12).
- Clients do not ask to move it: `models = 0`, no authority.
- Give it its own table in your test file (`const KindSpec kMoverTable[] = {kTestMover};`) with an
  id no other test kind uses, and check it with `Validate`.
- Its converge block uses the plain adapter, a `.value` that makes a 12-byte position from n, and a
  script that writes much faster than `limits.noisy_hz` (2 per second), for example
  `s.Spawn(10); s.WriteEvery(20ms, 3s);`, through every profile.

What you should see: every profile converges to S's last position for each mover, and S's log takes
at most two changes per mover per second however fast its world writes (the noisy hold; test `LM9`
checks the same on direct calls).
