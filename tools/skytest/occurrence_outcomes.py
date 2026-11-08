"""The occurrence grade (Swap Force Online stage 5, future/online-5.md sections 3.4, 5.2 and 5.6,
D24, D27, D28) on recorded lines: every attack occurrence S started has exactly one outcome on each
graded client, and S's retirement of its row is read from S's claim lines, never from transport.

S's lines (the claim table, the game's online/backend/stage5.cpp):

    Online: attack <ref> #<seq> (delivered all|claims ended)
    Online: attack <ref> #<seq> expired unconfirmed peer <p>[ (table full)]

A client's lines (its occurrence scheduler):

    Online: attack <ref> #<seq> <hash> start <t> (forced at <t'>|late <n>|missed|missed (no telegraph)|telegraph missed|undelivered|duplicate)
    Online: attack <ref> #<seq> accepted

- S's occurrences are the keys (ref, seq) S retired: one retirement each (`delivered all`, `claims
  ended`, or one `expired unconfirmed` line per open peer); `delivered all` together with an expiry
  of the same key, or one peer's expiry twice, fails.
- Each graded client must log exactly one outcome per S occurrence: none or two fail. `duplicate`
  is a dev line (a row for a key already decided), never an outcome.
- missed and undelivered outcomes are limited (0 each on loopback, section 5.6). `telegraph missed`
  (a telegraphed occurrence whose telegraph came after L: nothing shown) is the key's one outcome
  and counts as missed (section 5.6's missed telegraphs, 0 on loopback).
- `expired unconfirmed peer <p>` lines are delivery-proof failures, graded apart from the client's
  outcome: their count per peer must equal `expected_unconfirmed` (0 by default). A case that holds
  a client's accepts through a live fight (P4 (e)) cannot know how many occurrences fall in the hold:
  `min_unconfirmed` asks at least that many for the peer instead, each for a key that client logged
  `accepted` (a held accept, never a lost row). A key the client accepted and S then expired must
  still get forced, late or missed there, never undelivered (the client owned the entry).
- Fewer than `min_occurrences` S occurrences (20, section 5.2) is "not reached": an empty trace
  never passes.
- A client outcome for a key S has not retired (the run ended inside the retention) is counted as
  `unretired`, not graded.

Standard library only; run with python -I.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

RETIRED_RE = re.compile(r"Online: attack (\S+) #(\d+) \((delivered all|claims ended)\)\s*$")
EXPIRED_RE = re.compile(r"Online: attack (\S+) #(\d+) expired unconfirmed peer (\d+)(?: \(table full\))?\s*$")
OUTCOME_RE = re.compile(
    r"Online: attack (\S+) #(\d+) (\S+) start (-?\d+) "
    r"\((forced at -?\d+|late \d+|missed \(no telegraph\)|missed|telegraph missed|undelivered|duplicate)\)\s*$")
ACCEPTED_RE = re.compile(r"Online: attack (\S+) #(\d+) accepted\s*$")
CONNECTED_RE = re.compile(r"Online: connected as peer (\d+)")

Key = tuple[str, int]  # the enemy's ref (with its incarnation), the occurrence sequence


def _kind(outcome: str) -> str:
    if outcome.startswith("forced"):
        return "forced"
    if outcome.startswith("late"):
        return "late"
    if outcome.startswith("missed") or outcome == "telegraph missed":
        return "missed"
    return outcome  # undelivered, duplicate


@dataclass
class Result:
    occurrences: int = 0
    delivered_all: int = 0
    expired: dict[int, int] = field(default_factory=dict)          # peer -> expired unconfirmed lines
    counts: dict[str, dict[str, int]] = field(default_factory=dict)  # client -> outcome kind -> count
    unretired: dict[str, int] = field(default_factory=dict)        # client -> outcomes for unretired keys
    not_reached: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures and not self.not_reached


def grade_occurrences(server_log: str, client_logs: dict[str, str], *, min_occurrences: int = 20,
                      max_missed: int = 0, max_undelivered: int = 0,
                      expected_unconfirmed: dict[int, int] | None = None,
                      min_unconfirmed: dict[int, int] | None = None) -> Result:
    """`server_log`: S's log text; `client_logs`: each graded client's log text by its peer name."""
    res = Result()
    retired: dict[Key, str] = {}
    expired: dict[Key, list[int]] = defaultdict(list)
    for line in server_log.splitlines():
        if m := RETIRED_RE.search(line):
            key = (m.group(1), int(m.group(2)))
            if key in retired:
                res.failures.append(f"S: attack {key[0]} #{key[1]} retired twice")
            retired[key] = m.group(3)
        elif m := EXPIRED_RE.search(line):
            key = (m.group(1), int(m.group(2)))
            peer = int(m.group(3))
            if peer in expired[key]:
                res.failures.append(f"S: attack {key[0]} #{key[1]} expired twice for peer {peer}")
            expired[key].append(peer)
    for key, peers in expired.items():
        if retired.get(key) == "delivered all":
            res.failures.append(f"S: attack {key[0]} #{key[1]} both delivered all and expired for peer {peers[0]}")
    keys = set(retired) | set(expired)
    res.occurrences = len(keys)
    res.delivered_all = sum(1 for k in keys if retired.get(k) == "delivered all")
    res.expired = dict(Counter(p for peers in expired.values() for p in peers))
    want = expected_unconfirmed or {}
    at_least = min_unconfirmed or {}
    for peer in sorted(set(res.expired) | set(want) | set(at_least)):
        got = res.expired.get(peer, 0)
        if peer in at_least:
            if got < at_least[peer]:
                res.failures.append(f"S: {got} expired unconfirmed line(s) for peer {peer}, at least {at_least[peer]}")
        elif got != want.get(peer, 0):
            res.failures.append(f"S: {got} expired unconfirmed line(s) for peer {peer}, expected {want.get(peer, 0)}")
    if res.occurrences < min_occurrences:
        res.not_reached = f"{res.occurrences} attack occurrence(s) retired on S, at least {min_occurrences} needed"

    for name, text in client_logs.items():
        connected = CONNECTED_RE.search(text)
        peer = int(connected.group(1)) if connected else None
        if peer is None:
            res.failures.append(f"{name}: no `connected as peer` line")
        outcomes: dict[Key, list[str]] = defaultdict(list)
        accepted: set[Key] = set()
        counts: Counter[str] = Counter()
        for line in text.splitlines():
            if m := OUTCOME_RE.search(line):
                kind = _kind(m.group(5))
                counts[kind] += 1
                if kind != "duplicate":
                    outcomes[(m.group(1), int(m.group(2)))].append(m.group(5))
            elif m := ACCEPTED_RE.search(line):
                accepted.add((m.group(1), int(m.group(2))))
        res.counts[name] = dict(counts)
        res.unretired[name] = sum(len(v) for k, v in outcomes.items() if k not in keys)
        for key in sorted(keys):
            got = outcomes.get(key, [])
            if not got:
                res.failures.append(f"{name}: attack {key[0]} #{key[1]} has no outcome")
            elif len(got) > 1:
                res.failures.append(f"{name}: attack {key[0]} #{key[1]} has {len(got)} outcomes ({', '.join(got)})")
            elif (peer is not None and peer in expired.get(key, []) and key in accepted
                  and _kind(got[0]) == "undelivered"):
                res.failures.append(f"{name}: attack {key[0]} #{key[1]} accepted, then undelivered at S's expiry")
            if peer in at_least and peer in expired.get(key, []) and key not in accepted:
                res.failures.append(f"{name}: attack {key[0]} #{key[1]} expired for peer {peer} without its "
                                    f"`accepted` line (a lost row, not a held accept)")
        if counts["missed"] > max_missed:
            res.failures.append(f"{name}: {counts['missed']} missed occurrence(s), at most {max_missed}")
        if counts["undelivered"] > max_undelivered:
            res.failures.append(f"{name}: {counts['undelivered']} undelivered occurrence(s), at most {max_undelivered}")
    return res


# --- the runner's side (skytest.py: `[grade.occurrence_outcomes]` in a case) ----------------------

SPEC_KEYS = {"peer", "clients", "min_occurrences", "max_missed", "max_undelivered", "expected_unconfirmed",
             "min_unconfirmed"}


def check_spec(g: object, names: list[str]) -> str:
    """The grade's options; returns why they are wrong, or "". `peer`: the log with the claim lines
    (default S); `clients`: the graded clients (default every client of the case); `min_occurrences`
    (20), `max_missed` (0), `max_undelivered` (0): whole numbers >= 0; `expected_unconfirmed`: a table
    of peer id (a string) to the number of expired unconfirmed lines the case injects (default none);
    `min_unconfirmed`: the same table as a minimum, for a hold whose count the case cannot know (a
    peer in only one of the two)."""
    if not isinstance(g, dict):
        return "must be a table"
    if unknown := set(g) - SPEC_KEYS:
        return f"unknown keys {sorted(unknown)} ({', '.join(sorted(SPEC_KEYS))})"
    if g.get("peer", "S") not in names:
        return f"`peer` {g.get('peer')!r} is not in {names}"
    clients = g.get("clients", [n for n in names if n != "S"])
    if not isinstance(clients, list) or not clients or any(c not in names or c == "S" for c in clients):
        return f"`clients` must name clients of {names}"
    for key in ("min_occurrences", "max_missed", "max_undelivered"):
        v = g.get(key, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return f"{key} must be a whole number >= 0"
    for key in ("expected_unconfirmed", "min_unconfirmed"):
        table = g.get(key, {})
        if not isinstance(table, dict) or any(
                not str(k).isdigit() or not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in table.items()):
            return f"{key} maps peer ids (\"1\") to whole numbers >= 0"
    if both := {int(k) for k in g.get("expected_unconfirmed", {})} & {int(k) for k in g.get("min_unconfirmed", {})}:
        return f"peer(s) {sorted(both)} in both expected_unconfirmed and min_unconfirmed"
    return ""


def grade_spec(texts: dict[str, str], g: dict, names: list[str]) -> tuple[dict, list[str]]:
    """Runs the grade on a finished run's logs (`texts` by peer name); returns the record and the
    failure reasons (`grade occurrence_outcomes: ...`)."""
    clients = g.get("clients", [n for n in names if n != "S"])
    res = grade_occurrences(
        texts.get(g.get("peer", "S"), ""), {c: texts.get(c, "") for c in clients},
        min_occurrences=g.get("min_occurrences", 20), max_missed=g.get("max_missed", 0),
        max_undelivered=g.get("max_undelivered", 0),
        expected_unconfirmed={int(k): v for k, v in g.get("expected_unconfirmed", {}).items()},
        min_unconfirmed={int(k): v for k, v in g.get("min_unconfirmed", {}).items()})
    record = {"occurrences": res.occurrences, "delivered_all": res.delivered_all,
              "expired": {str(k): v for k, v in res.expired.items()}, "counts": res.counts,
              "unretired": res.unretired, "not_reached": res.not_reached, "failures": res.failures[:20]}
    reasons = [f"grade occurrence_outcomes: not reached: {res.not_reached}"] if res.not_reached else []
    reasons += [f"grade occurrence_outcomes: {f}" for f in res.failures[:20]]
    if len(res.failures) > 20:
        reasons.append(f"grade occurrence_outcomes: {len(res.failures) - 20} more")
    return record, reasons
