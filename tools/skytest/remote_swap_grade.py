"""skytest's `remote_swap` grade (Swap Force Online stage 4, docs/online-4.md section 4.5, D16; thresholds.toml
[remote_swap] max_ms): the spec's side of swap_pairs.py's event pair grade.

A spec names what it orders, so a run that lost a swap, a receiver or a whole log fails instead of
grading what it happened to observe:

    [grade.remote_swap]
    swappers = ["H", "D"]   # the peers whose swaps are graded (required)
    swaps = 10              # swaps each swapper makes (required, >= 1)
    receivers = ["H", "D"]  # the clients that must show each swap (default: every client); a swapper
                            # is never its own receiver
    peer = "S"              # the result that carries the grade (default S)

Each client's session peer id comes from its own log (`Online: connected as peer <n>`); the swap lines
carry it as the swapper's key. Only the named swappers' swaps are graded, each on the named receivers
other than its swapper (a receiver's own swap is not a key; a swapper outside `receivers` is never asked
to show one). The grade fails, each with its own line, when: a swapper or receiver never connected (no
peer id), a swapper logged fewer swaps than `swaps` (counted per swapper: ten swaps by one peer never
stand for another's), the pair count is not the swaps logged x the receivers other than their swapper,
or any pair fails swap_pairs.py's own rules (missing, duplicate, wrong body, over max_ms, pressure
pairs).

Standard library only; run with python -I.
"""
from __future__ import annotations

import re

import swap_pairs

CONNECTED = re.compile(r"Online: connected as peer (\d+)")
KEYS = {"swappers", "swaps", "receivers", "peer"}


class RemoteSwapSpecError(ValueError):
    pass


def check_remote_swap(spec_name: str, g: object, names: list[str]) -> None:
    """The grade's options against the spec's peers (`names`, S included)."""
    def bad(why: str) -> RemoteSwapSpecError:
        return RemoteSwapSpecError(f"{spec_name}: grade remote_swap: {why}")

    if not isinstance(g, dict):
        raise bad("must be a table")
    if unknown := set(g) - KEYS:
        raise bad(f"unknown keys {sorted(unknown)} ({', '.join(sorted(KEYS))})")
    clients = [n for n in names if n != "S"]
    swappers = g.get("swappers")
    if not isinstance(swappers, list) or not swappers or any(s not in clients for s in swappers):
        raise bad(f"`swappers` must name clients of {clients}")
    swaps = g.get("swaps")
    if not isinstance(swaps, int) or isinstance(swaps, bool) or swaps < 1:
        raise bad("`swaps` must be a whole number >= 1")
    receivers = g.get("receivers", clients)
    if not isinstance(receivers, list) or not receivers or any(r not in clients for r in receivers):
        raise bad(f"`receivers` must name clients of {clients}")
    if not any(r != s for s in swappers for r in receivers):
        raise bad("no receiver other than the swapper")
    if g.get("peer", "S") not in names:
        raise bad(f"`peer` {g.get('peer')!r} is not in {names}")


def peer_id(text: str) -> int | None:
    """The session peer id a client's log says it connected as (the last one: a rejoin's)."""
    found = CONNECTED.findall(text)
    return int(found[-1]) if found else None


def grade_remote_swap(texts: dict[str, str], g: dict, thresholds: dict, names: list[str]) -> tuple[dict, list[str]]:
    """`texts`: every peer's log by spec name. Returns (the graded values, the failure reasons)."""
    max_ms = float(thresholds.get("remote_swap", {}).get("max_ms", 500.0))
    clients = [n for n in names if n != "S"]
    swappers: list[str] = list(g["swappers"])
    receivers: list[str] = list(g.get("receivers", clients))
    swaps = int(g["swaps"])
    why: list[str] = []

    ids: dict[str, int] = {}
    for name in sorted(set(swappers) | set(receivers)):
        pid = peer_id(texts.get(name, ""))
        if pid is None:
            why.append(f"grade remote_swap: {name} never connected (no `connected as peer` line)")
        else:
            ids[name] = pid
    graded: dict = {"max_ms": max_ms}
    if why:
        return graded, why

    # Only the named peers' logs: a receiver outside `receivers` is not required, so not graded.
    logs = {ids[n]: texts.get(n, "") for n in sorted(set(swappers) | set(receivers))}
    by_id = {v: k for k, v in ids.items()}
    seen: dict[int, set[tuple[int, int, int]]] = {ids[s]: set() for s in swappers}
    for s in swappers:
        for line in texts.get(s, "").splitlines():
            m = swap_pairs.SWAP_RE.search(line)
            if m:
                seen[ids[s]].add((int(m.group(3)), int(m.group(4)), int(m.group(1))))
    for pid, keys in seen.items():
        if len(keys) < swaps:
            why.append(f"grade remote_swap: {by_id[pid]} logged {len(keys)} swap(s), the spec orders {swaps}")

    # Only the named swappers' swaps are keys and only the named receivers receive: a swapper that is
    # not a receiver is never asked to show another's swap, and a receiver's own swap is not graded.
    res = swap_pairs.grade_swap_pairs(logs, max_ms=max_ms, receivers={ids[r] for r in receivers},
                                      swappers={ids[s] for s in swappers})
    # Receivers per swapper: the named receivers other than the swapper itself.
    expected = sum(len(seen[ids[s]]) * sum(1 for r in receivers if r != s) for s in swappers)
    graded.update({"expected": expected, "graded": res.graded, "max_lag_ms": round(res.max_lag_ms, 1),
                   "pressure_pairs": res.pressure_pairs})
    if expected == 0:
        why.append("grade remote_swap: no swap was graded (not reached)")
    if res.expected != expected:
        why.append(f"grade remote_swap: {res.expected} pair(s) found, {expected} expected")
    why += [f"grade remote_swap: {f}" for f in res.failures]
    if res.graded != res.expected and not res.failures:
        why.append(f"grade remote_swap: {res.graded} of {res.expected} pair(s) graded")
    return graded, why
