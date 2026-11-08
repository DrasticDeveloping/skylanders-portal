"""The remote swap event pair grade (Swap Force Online stage 4, docs/online-4.md section 4.5, D16;
thresholds.toml [remote_swap] max_ms).

Every swap of every player is a key (swapper peer, peer epoch, generation, swap sequence). The reference
is the swapper's line

    Online: swap <n> hero <character> epoch <e> gen <g> tick <t>

and the completion the receiver's

    Online: puppet <peer> epoch <e> gen <g> body <character> (real) swap <n> tick <t>

for that key. Both ticks are the session's shared steady-clock tick (S's ServerTickClock, read on
clients through ClientTickEstimator), so a held world or a swap's own pause never hides time.

The expected work comes from the case, never from the logs alone (Sol stage 4 code r1 F4):
- `swaps`: the swaps the case ordered, either a count per swapper peer ({peer: count}; that peer's
  first `count` swap lines in log order are the swaps) or swap refs, each (peer, swap sequence) or a
  whole key (peer, epoch, generation, swap sequence);
- `receivers`: the client peer ids whose logs must show each swap (a swapper is never its own
  receiver); each must have a log;
- `pressure`: the (swap ref, receiver) pairs the case puts under capacity pressure (default none).

Grading:
- the expected count is the sum over the ordered swaps of their receivers. An ordered swap with no
  reference line (or fewer swap lines than the count), a swap line the case did not order, a receiver
  without a log, a missing pair, a duplicate completion and a completion whose character differs from
  the swapper's fail, each with its own line;
- lag = completion - reference; a completion before the reference (an early preload) is 0; every
  pair is graded against max_ms (a maximum, no percentile);
- a pressure pair needs exactly one stand-in (`... body <character> (stand-in) swap <n> tick <t>`),
  exactly one slot-free event (`Online: body slot <s> freed for puppet <peer> epoch <e> gen <g> swap
  <n> tick <t>`) and exactly one real body, in that order: stand-in <= freed <= real by tick, the
  log's line order deciding equal ticks (Sol F5). It grades the stand-in within max_ms of the
  reference and the real body within max_ms of the slot-free event (plan 2.7's fifth-character rule);
- a slot-free event on a pair the case does not mark as pressure fails (it would otherwise escape
  the ordinary grade's case or the pressure grade's).

A case that orders no swap, names no receiver, or a swap without a receiver is a spec error
(ValueError), never a pass. Standard library only; run with python -I.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

SWAP_RE = re.compile(r"Online: swap (\d+) hero (.+?) epoch (\d+) gen (\d+) tick (\d+)\s*$")
BODY_RE = re.compile(
    r"Online: puppet (\d+) epoch (\d+) gen (\d+) body (.+?) \((real|stand-in)\) swap (\d+) tick (\d+)\s*$")
FREED_RE = re.compile(r"Online: body slot (\d+) freed for puppet (\d+) epoch (\d+) gen (\d+) swap (\d+) tick (\d+)\s*$")

Key = tuple[int, int, int, int]  # swapper peer, peer epoch, generation, swap sequence
SwapRef = tuple[int, ...]  # (peer, swap sequence) or a whole Key
Event = tuple[int, int]  # (tick, line index in its log): the order of two events of one log


@dataclass
class Result:
    expected: int = 0
    graded: int = 0
    max_lag_ms: float = 0.0
    pressure_pairs: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures and self.expected > 0 and self.graded == self.expected


@dataclass
class _Seen:
    real: list[tuple[str, Event]] = field(default_factory=list)
    stand_in: list[Event] = field(default_factory=list)
    freed: list[Event] = field(default_factory=list)


def _ms(ticks: int, hz: int) -> float:
    return ticks * 1000.0 / hz


def _ref(ref: Iterable[int]) -> SwapRef:
    r = tuple(int(x) for x in ref)
    if len(r) not in (2, 4):
        raise ValueError(f"swap ref {r}: (peer, swap sequence) or (peer, epoch, generation, swap sequence)")
    return r


def _matches(ref: SwapRef, key: Key) -> bool:
    return ref == key if len(ref) == 4 else (ref[0], ref[1]) == (key[0], key[3])


def _receiver_events(text: str) -> dict[Key, _Seen]:
    seen: dict[Key, _Seen] = {}
    for idx, line in enumerate(text.splitlines()):
        b = BODY_RE.search(line)
        if b:
            key = (int(b.group(1)), int(b.group(2)), int(b.group(3)), int(b.group(6)))
            ev = (int(b.group(7)), idx)
            if b.group(5) == "real":
                seen.setdefault(key, _Seen()).real.append((b.group(4), ev))
            else:
                seen.setdefault(key, _Seen()).stand_in.append(ev)
            continue
        f = FREED_RE.search(line)
        if f:
            key = (int(f.group(2)), int(f.group(3)), int(f.group(4)), int(f.group(5)))
            seen.setdefault(key, _Seen()).freed.append((int(f.group(6)), idx))
    return seen


def grade_swap_pairs(logs: Mapping[int, str], swaps: Mapping[int, int] | Iterable[Iterable[int]],
                     receivers: Iterable[int], pressure: Iterable[tuple[Iterable[int], int]] = (),
                     max_ms: float = 500.0, hz: int = 60) -> Result:
    """`logs`: each client's log text by its peer id (S's log is not a receiver); `swaps`,
    `receivers`, `pressure`: the case's expected work (module docstring)."""
    res = Result()
    rec = [int(r) for r in receivers]
    if not rec:
        raise ValueError("no receiver: the case must name the client peers that receive the swaps")
    if len(set(rec)) != len(rec):
        raise ValueError(f"receivers {rec}: a peer named twice")

    # Every swap line, in each swapper's log order.
    observed: dict[int, list[tuple[Key, str, int]]] = {}
    logged: set[Key] = set()
    for peer, text in logs.items():
        for line in text.splitlines():
            m = SWAP_RE.search(line)
            if not m:
                continue
            key = (int(peer), int(m.group(3)), int(m.group(4)), int(m.group(1)))
            if key in logged:
                res.failures.append(f"swap {key} logged twice by its swapper")
                continue
            logged.add(key)
            observed.setdefault(int(peer), []).append((key, m.group(2), int(m.group(5))))

    # The case's ordered swaps, each resolved to its reference line or None (missing).
    required: list[tuple[int, tuple[Key, str, int] | None]] = []
    used: set[Key] = set()
    if isinstance(swaps, Mapping):
        counts = {int(p): int(c) for p, c in swaps.items()}
        if not counts or any(c < 1 for c in counts.values()):
            raise ValueError(f"swaps {counts}: the case must order at least one swap per named swapper")
        for peer, count in sorted(counts.items()):
            got = observed.get(peer, [])[:count]
            required += [(peer, o) for o in got] + [(peer, None)] * (count - len(got))
            used.update(o[0] for o in got)
            if len(got) < count:
                res.failures.append(f"peer {peer}: {count} swap(s) ordered, {len(got)} logged (missing reference)")
    else:
        refs = [_ref(r) for r in swaps]
        if not refs:
            raise ValueError("no swap: the case must order at least one swap")
        if len(set(refs)) != len(refs):
            raise ValueError(f"swaps {refs}: a swap named twice")
        for ref in refs:
            hits = [o for o in observed.get(ref[0], []) if _matches(ref, o[0])]
            if len(hits) == 1 and hits[0][0] in used:
                raise ValueError(f"swap ref {ref}: names a swap another ref already names")
            if not hits:
                res.failures.append(f"swap ref {ref}: no swap line of peer {ref[0]} (missing reference)")
            elif len(hits) > 1:
                res.failures.append(f"swap ref {ref}: {len(hits)} swap lines match ({', '.join(str(h[0]) for h in hits)})")
            required.append((ref[0], hits[0] if len(hits) == 1 else None))
            if len(hits) == 1:
                used.add(hits[0][0])
    for peer, lst in sorted(observed.items()):
        for key, _, _ in lst:
            if key not in used:
                res.failures.append(f"swap {key}: not ordered by the case (unexpected)")
    for peer, _ in required:
        if not any(r != peer for r in rec):
            raise ValueError(f"swap of peer {peer}: no receiver other than the swapper")

    # The case's pressure pairs, resolved to keys (a pair of a missing swap already failed above).
    swappers = {peer for peer, _ in required}
    pressure_set: set[tuple[Key, int]] = set()
    for ref_in, receiver in pressure:
        ref, receiver = _ref(ref_in), int(receiver)
        if ref[0] not in swappers or receiver not in rec or receiver == ref[0]:
            raise ValueError(f"pressure pair {ref} on peer {receiver}: not a pair of the ordered swaps and receivers")
        hits = [o[0] for _, o in required if o is not None and _matches(ref, o[0])]
        if len(hits) > 1:
            res.failures.append(f"pressure pair {ref} on peer {receiver}: {len(hits)} swaps match")
        elif hits:
            if (hits[0], receiver) in pressure_set:
                raise ValueError(f"pressure pair {ref} on peer {receiver}: named twice")
            pressure_set.add((hits[0], receiver))

    events: dict[int, dict[Key, _Seen]] = {}
    for r in rec:
        if r in logs:
            events[r] = _receiver_events(logs[r])
        else:
            res.failures.append(f"receiver {r}: no log (not reached)")

    for peer, ref_line in required:
        for receiver in rec:
            if receiver == peer:
                continue
            res.expected += 1
            if ref_line is None or receiver not in events:
                continue
            key, character, ref_tick = ref_line
            seen = events[receiver].get(key, _Seen())
            who = f"swap {key[3]} of peer {key[0]} (epoch {key[1]}, gen {key[2]}) on peer {receiver}"
            if not seen.real:
                res.failures.append(f"{who}: missing (no real body line)")
                continue
            if len(seen.real) > 1:
                res.failures.append(f"{who}: duplicate completion ({len(seen.real)} real body lines)")
                continue
            got, done = seen.real[0]
            if got != character:
                res.failures.append(f"{who}: wrong body {got!r}, the swapper's hero is {character!r}")
                continue
            if (key, receiver) in pressure_set:
                res.pressure_pairs += 1
                bad = False
                for name, evs in (("stand-in", seen.stand_in), ("slot-free", seen.freed)):
                    if not evs:
                        res.failures.append(f"{who}: under pressure with no {name} line"
                                            + (" (missing release)" if name == "slot-free" else ""))
                        bad = True
                    elif len(evs) > 1:
                        res.failures.append(f"{who}: duplicate {name} ({len(evs)} lines)")
                        bad = True
                if bad:
                    continue
                stand, freed = seen.stand_in[0], seen.freed[0]
                if not stand < freed < done:
                    res.failures.append(f"{who}: out of order (stand-in tick {stand[0]}, slot-free tick "
                                        f"{freed[0]}, real tick {done[0]}; must be stand-in, slot free, real)")
                    continue
                res.graded += 1
                stand_lag = max(0.0, _ms(stand[0] - ref_tick, hz))
                real_lag = _ms(done[0] - freed[0], hz)
                res.max_lag_ms = max(res.max_lag_ms, stand_lag, real_lag)
                if stand_lag > max_ms:
                    res.failures.append(f"{who}: stand-in after {stand_lag:.0f} ms (max {max_ms:.0f})")
                if real_lag > max_ms:
                    res.failures.append(f"{who}: real body {real_lag:.0f} ms after the slot freed (max {max_ms:.0f})")
                continue
            if seen.freed:
                res.failures.append(f"{who}: a slot-free line on a pair the case does not mark as pressure")
                continue
            res.graded += 1
            lag = max(0.0, _ms(done[0] - ref_tick, hz))
            res.max_lag_ms = max(res.max_lag_ms, lag)
            if lag > max_ms:
                res.failures.append(f"{who}: {lag:.0f} ms (max {max_ms:.0f})")
    return res
