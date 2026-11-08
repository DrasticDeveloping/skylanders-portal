"""The remote swap event pair grade (Swap Force Online stage 4, docs/online-4.md section 4.5, D16;
thresholds.toml [remote_swap] max_ms).

Every swap of every player is a key (swapper peer, peer epoch, generation, swap sequence); every
other client of the run is a receiver. The reference is the swapper's line

    Online: swap <n> hero <character> epoch <e> gen <g> tick <t>

and the completion the receiver's first

    Online: puppet <peer> epoch <e> gen <g> body <character> (real) swap <n> tick <t>

for that key. Both ticks are the session's shared steady-clock tick (S's ServerTickClock, read on
clients through ClientTickEstimator), so a held world or a swap's own pause never hides time.

- lag = completion - reference; a completion before the reference (an early preload) is 0;
- every pair is graded against max_ms (a maximum, no percentile);
- the expected count is swaps x receivers: a missing pair, a duplicate completion and a completion
  whose character differs from the swapper's fail, each with its own line;
- under capacity pressure (the receiver logged `Online: body slot <s> freed for puppet <peer> epoch
  <e> gen <g> swap <n> tick <t>` for the key) the pair grades two things: a stand-in
  (`... body <character> (stand-in) swap <n> tick <t>`) within max_ms of the reference, and the real
  body within max_ms of the slot-free event (plan 2.7's fifth-character rule).

Standard library only; run with python -I.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SWAP_RE = re.compile(r"Online: swap (\d+) hero (.+?) epoch (\d+) gen (\d+) tick (\d+)\s*$")
BODY_RE = re.compile(
    r"Online: puppet (\d+) epoch (\d+) gen (\d+) body (.+?) \((real|stand-in)\) swap (\d+) tick (\d+)\s*$")
FREED_RE = re.compile(r"Online: body slot (\d+) freed for puppet (\d+) epoch (\d+) gen (\d+) swap (\d+) tick (\d+)\s*$")

Key = tuple[int, int, int, int]  # swapper peer, peer epoch, generation, swap sequence


@dataclass
class Result:
    expected: int = 0
    graded: int = 0
    max_lag_ms: float = 0.0
    pressure_pairs: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures and self.graded == self.expected


def _ms(ticks: int, hz: int) -> float:
    return ticks * 1000.0 / hz


def grade_swap_pairs(logs: dict[int, str], max_ms: float = 500.0, hz: int = 60) -> Result:
    """`logs`: each client's log text by its peer id (S's log is not a receiver)."""
    res = Result()
    swaps: dict[Key, tuple[str, int]] = {}  # key -> (character, reference tick)
    for peer, text in logs.items():
        for line in text.splitlines():
            m = SWAP_RE.search(line)
            if not m:
                continue
            key = (peer, int(m.group(3)), int(m.group(4)), int(m.group(1)))
            if key in swaps:
                res.failures.append(f"swap {key} logged twice by its swapper")
                continue
            swaps[key] = (m.group(2), int(m.group(5)))

    for key, (character, ref_tick) in sorted(swaps.items()):
        for receiver, text in sorted(logs.items()):
            if receiver == key[0]:
                continue
            res.expected += 1
            real: list[tuple[str, int]] = []
            stand_in: list[int] = []
            freed: list[int] = []
            for line in text.splitlines():
                b = BODY_RE.search(line)
                if b and (int(b.group(1)), int(b.group(2)), int(b.group(3)), int(b.group(6))) == key:
                    if b.group(5) == "real":
                        real.append((b.group(4), int(b.group(7))))
                    else:
                        stand_in.append(int(b.group(7)))
                    continue
                f = FREED_RE.search(line)
                if f and (int(f.group(2)), int(f.group(3)), int(f.group(4)), int(f.group(5))) == key:
                    freed.append(int(f.group(6)))
            who = f"swap {key[3]} of peer {key[0]} (epoch {key[1]}, gen {key[2]}) on peer {receiver}"
            if not real:
                res.failures.append(f"{who}: missing (no real body line)")
                continue
            if len(real) > 1:
                res.failures.append(f"{who}: duplicate completion ({len(real)} real body lines)")
                continue
            got, done_tick = real[0]
            if got != character:
                res.failures.append(f"{who}: wrong body {got!r}, the swapper's hero is {character!r}")
                continue
            res.graded += 1
            if freed:
                res.pressure_pairs += 1
                if not stand_in:
                    res.failures.append(f"{who}: under pressure with no stand-in")
                    continue
                stand_lag = max(0.0, _ms(min(stand_in) - ref_tick, hz))
                real_lag = max(0.0, _ms(done_tick - min(freed), hz))
                res.max_lag_ms = max(res.max_lag_ms, stand_lag, real_lag)
                if stand_lag > max_ms:
                    res.failures.append(f"{who}: stand-in after {stand_lag:.0f} ms (max {max_ms:.0f})")
                if real_lag > max_ms:
                    res.failures.append(f"{who}: real body {real_lag:.0f} ms after the slot freed (max {max_ms:.0f})")
                continue
            lag = max(0.0, _ms(done_tick - ref_tick, hz))
            res.max_lag_ms = max(res.max_lag_ms, lag)
            if lag > max_ms:
                res.failures.append(f"{who}: {lag:.0f} ms (max {max_ms:.0f})")
    return res
