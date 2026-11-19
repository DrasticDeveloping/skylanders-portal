"""Checks of a case before any game starts (load_spec) and during its run (FailFast), pure; unit-tested in
tests/test_script_checks.py.

host_problem (R2 of the 18 November run audit): in a hosted layout (server = "host") a client whose script
waits on a session before it hosts or joins one waits for nothing (figure-change-crash 20261118-171834: H
began with `online_wait_peers 2 240`, never hosted, and the run lost 434 s). The spec is refused.

needed_peer_gone (R1): a peer's `wait_needs` names, per wait condition, the peers that wait can be met
only with (`wait_needs = { online_standin_live = ["D"] }`). When one of those peers' game has exited
while the waiting peer still has such a `waitfor` ahead or running (its script's count of that wait more
than the log's met or timed-out lines of it) and is itself still running, the wait cannot be met
(standin-leave-level-change 20261118-141306: D quit at 14:14:09, H waited 240 s for D's stand-in to 14:18:17):
fail fast ends the run after its grace. The game logs a wait's end, not its start, so which waits need
which peers is the spec's statement: a spec that ends D early on purpose names no such wait."""
from __future__ import annotations

import re

# Steps that start or enter a session: hosting by command or through the menus (F4 opens the online
# page), joining.
SESSION_START = re.compile(r"^(online_host|online_join|key\s+F4)\b", re.I)
# Waits that need a session (docs/testing-api.md, Conditions): the waits for peers and the session's own
# conditions. Not the local ones a joiner waits on before it joins (online_title, online_in_play) and none
# this list does not name (an unknown condition is never a refusal).
SESSION_WAIT = re.compile(r"^(online_wait_peers(_at_most)?|online_wait_peer_near(_entity)?)\b|"
                          r"^waitfor\s+online_(connected|server_hub|barrier|alone|peer_was_in|peer_shown|"
                          r"level_end|past_level_end|next_level|level_after_mark|join_pending|standin_\w+)\b")
WAIT_DONE = re.compile(r"Input script: waitfor (\w+) (?:met|timed out) after")
WAITFOR = re.compile(r"^waitfor\s+(\w+)\b")


def script_lines(script: str) -> list[str]:
    return [l.strip() for l in script.splitlines() if l.strip() and not l.strip().startswith("#")]


def host_problem(script: str) -> str:
    """'' when the script hosts or joins before its first wait on a session (or never waits on one); else
    the line that waits first, in words."""
    for line in script_lines(script):
        if SESSION_START.match(line):
            return ""
        if SESSION_WAIT.match(line):
            return (f"`{line}` waits on a session before the script hosts or joins one (online_host, key F4 or"
                    " online_join first)")
    return ""


def wait_counts(script: str) -> dict[str, int]:
    """How many times the script waits for each condition."""
    out: dict[str, int] = {}
    for line in script_lines(script):
        m = WAITFOR.match(line)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + 1
    return out


def wait_done(line: str) -> str:
    """The condition a log line says a wait ended for ('' for none)."""
    m = WAIT_DONE.search(line)
    return m.group(1) if m else ""


def needed_peer_gone(needs: dict[str, dict[str, list[str]]], planned: dict[str, dict[str, int]],
                     done: dict[str, dict[str, int]], exited: dict[str, int | None]) -> tuple[str, str] | None:
    """(waiting peer, why) for the first wait that can no longer be met, None when every such wait is met
    or its peers still run. `planned`: each peer's waits per condition (wait_counts); `done`: the ends its
    log showed so far; `exited`: each peer's exit code, None while it runs."""
    for waiter, conds in needs.items():
        if waiter not in exited or exited[waiter] is not None:
            continue  # the waiter itself ended: nothing waits
        for cond, peers in conds.items():
            if planned.get(waiter, {}).get(cond, 0) <= done.get(waiter, {}).get(cond, 0):
                continue
            for p in peers:
                code = exited.get(p)
                if p in exited and code is not None:
                    return waiter, (f"waitfor {cond} cannot be met: it needs {p} (wait_needs), whose game exited"
                                    f" (exit code {code}) before it")
    return None
