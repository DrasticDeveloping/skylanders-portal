"""The graph trace grade (Swap Force Online stage 5, the shared enemies' evidence run enemy-attack-ta): ties an
encounter's own spawned rows on a client to repeated samples of the same rows on the client and on the server,
over overlapping non-zero server ticks inside a window of the client's script.

The game logs, with dev_enemy_graph_trace on (src/online/backend/graph_trace.h):

    a client: Online: graph trace copy <row> tick <t> states ...
    the server: Online: graph trace enemy <row> tick <t> states ...

The grade takes ONE row: the first `row_from` line (a pattern with one group: the row id, e.g. the client's
`enemy spawn <row> as ECI_Manager_actor_N#<addr> (live)`) after the first `rows_after` line (the spec's start line,
so the row the spec's expect_order matched), the window from the client's lines matching
`window_from` and `window_to` (by each line's own timestamp; the server's lines are filtered by the same times, one
machine's clock), and counts per row the samples inside the window with a tick above 0. It passes when at least one
row has `min_samples` (2) samples on both sides and the two sides' tick ranges overlap. No row, no window, or no such
row is "not reached": an empty trace never passes. It grades evidence, never that an attack landed.

Standard library only; run with python -I.
"""
from __future__ import annotations

import re
from datetime import datetime

STAMP_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]")
COPY_RE = re.compile(r"Online: graph trace copy (\S+) tick (\d+) ")
ENEMY_RE = re.compile(r"Online: graph trace enemy (\S+) tick (\d+) ")
SPEC_KEYS = {"row_from", "rows_after", "window_from", "window_to", "client", "server", "min_samples"}


def _stamp(line: str) -> datetime | None:
    m = STAMP_RE.match(line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def check_spec(g: object, names: list[str]) -> str:
    """The grade's options; returns why they are wrong, or "". `row_from` (a pattern with one group), `rows_after`,
    `window_from` and `window_to` (patterns) are required; `client` (H) and `server` (S) name peers; `min_samples`
    (2) >= 1."""
    if not isinstance(g, dict):
        return "must be a table"
    if unknown := set(g) - SPEC_KEYS:
        return f"unknown keys {sorted(unknown)} ({', '.join(sorted(SPEC_KEYS))})"
    for key in ("row_from", "rows_after", "window_from", "window_to"):
        if not isinstance(g.get(key), str) or not g[key]:
            return f"`{key}` (a pattern) is required"
        try:
            rx = re.compile(g[key])
        except re.error as e:
            return f"`{key}` is not a pattern: {e}"
        if key == "row_from" and rx.groups != 1:
            return "`row_from` needs exactly one group (the row id)"
    for key, default in (("client", "H"), ("server", "S")):
        if g.get(key, default) not in names:
            return f"`{key}` {g.get(key, default)!r} is not in {names}"
    n = g.get("min_samples", 2)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return "min_samples must be a whole number >= 1"
    return ""


def _window(text: str, start_rx: str, end_rx: str) -> tuple[datetime, datetime] | None:
    start = end = None
    for line in text.splitlines():
        t = _stamp(line)
        if t is None:
            continue
        if start is None and re.search(start_rx, line):
            start = t
        elif start is not None and re.search(end_rx, line):
            end = t
            break
    return (start, end) if start and end else None


def _samples(text: str, rx: re.Pattern, rows: set[str], window: tuple[datetime, datetime]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for line in text.splitlines():
        m = rx.search(line)
        if not m or m.group(1) not in rows:
            continue
        t = _stamp(line)
        tick = int(m.group(2))
        if t is None or tick <= 0 or not (window[0] <= t <= window[1]):
            continue
        out.setdefault(m.group(1), []).append(tick)
    return out


def _first_row_after(text: str, row_from: str, rows_after: str) -> str | None:
    """The row of the first `row_from` line after the first `rows_after` line (the spawn line a spec's expect_order
    matches after its own start line), or None."""
    after = False
    for line in text.splitlines():
        if not after:
            after = re.search(rows_after, line) is not None
            continue
        m = re.search(row_from, line)
        if m:
            return m.group(1)
    return None


def grade(client_text: str, server_text: str, row_from: str, rows_after: str, window_from: str, window_to: str,
          min_samples: int = 2) -> tuple[dict, str, str]:
    """Returns (record, not_reached, failure). Review B10 (round 2): the one row graded is the first `row_from` spawn
    after `rows_after`, never a row from earlier in the log."""
    row = _first_row_after(client_text, row_from, rows_after)
    rows = {row} if row else set()
    record: dict = {"row": row or "", "tied": []}
    if not rows:
        return record, "no `row_from` line after `rows_after` on the client", ""
    window = _window(client_text, window_from, window_to)
    if window is None:
        return record, "the client's window (window_from .. window_to) was not found", ""
    copies = _samples(client_text, COPY_RE, rows, window)
    enemies = _samples(server_text, ENEMY_RE, rows, window)
    for row in sorted(rows):
        c, s = copies.get(row, []), enemies.get(row, [])
        if len(c) < min_samples or len(s) < min_samples:
            continue
        if max(min(c), min(s)) <= min(max(c), max(s)):
            record["tied"].append({"row": row, "client": len(c), "server": len(s), "client_ticks": [min(c), max(c)],
                                   "server_ticks": [min(s), max(s)]})
    record["client_rows_sampled"] = len(copies)
    record["server_rows_sampled"] = len(enemies)
    if not record["tied"]:
        return record, (f"the row {row} did not have {min_samples} samples on both sides with overlapping ticks "
                        f"inside the window (client samples {len(copies.get(row, []))}, server "
                        f"{len(enemies.get(row, []))})"), ""
    return record, "", ""


def grade_spec(texts: dict[str, str], g: dict, names: list[str]) -> tuple[dict, list[str]]:
    """Runs the grade once, on the client's and the server's logs."""
    del names
    record, not_reached, failure = grade(texts.get(g.get("client", "H"), ""), texts.get(g.get("server", "S"), ""),
                                         g["row_from"], g["rows_after"], g["window_from"], g["window_to"],
                                         g.get("min_samples", 2))
    record["not_reached"] = not_reached
    reasons = []
    if not_reached:
        reasons.append(f"grade graph_trace: not reached: {not_reached}")
    elif failure:
        reasons.append(f"grade graph_trace: {failure}")
    return record, reasons
