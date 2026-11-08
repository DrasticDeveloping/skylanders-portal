"""The memory growth grade (Swap Force Online stage 5, future/online-5.md sections 3.11 and 5.6: the
soak's live segment) on recorded lines.

Every 10 s the game's frame-time event logs, on the server and on a client in a session,

    Online: frames <n> fps <f> p50 <ms> p95 <ms> max <ms> private <MB> working <MB> video <MB>

The grade takes one peer's log, finds the first line matching `anchor` (the live segment's start),
and fits a least-squares line to the private megabytes of the samples from `start_min` to `end_min`
minutes after it (5 to 20 by default, section 5.6), by each line's own timestamp. The growth is the
line's slope over 10 minutes; it must be at most `max_mb_per_10min` (2 MB, proposed in 5.6). No
anchor, or fewer than `min_samples` samples in the window (30), is "not reached": an empty trace
never passes.

Standard library only; run with python -I.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

STAMP_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]")
FRAMES_RE = re.compile(r"Online: frames \d+ fps [\d.]+ p50 [\d.]+ p95 [\d.]+ max [\d.]+ private (-?\d+) working")


@dataclass
class Result:
    samples: int = 0
    growth_mb_per_10min: float = 0.0
    first_mb: float = 0.0
    last_mb: float = 0.0
    not_reached: str = ""
    failure: str = ""

    @property
    def passed(self) -> bool:
        return not self.failure and not self.not_reached


def _stamp(line: str) -> datetime | None:
    m = STAMP_RE.match(line)
    if not m:
        return None
    text = m.group(1)
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S")


def find_anchor(text: str, anchor: str) -> datetime | None:
    """The timestamp of the first line of `text` matching `anchor` (None: no such line, or no stamp)."""
    anchor_re = re.compile(anchor)
    for line in text.splitlines():
        if anchor_re.search(line):
            return _stamp(line)
    return None


def grade_memory_growth(text: str, anchor: str, *, origin: datetime | None = None, start_min: float = 5.0,
                        end_min: float = 20.0, max_mb_per_10min: float = 2.0, min_samples: int = 30) -> Result:
    """`origin`: the segment's start when another log holds the anchor (the runner's anchor_peer);
    else the first line of `text` matching `anchor`."""
    res = Result()
    if origin is None:
        origin = find_anchor(text, anchor)
    points: list[tuple[float, float]] = []  # minutes after the anchor, private MB
    for line in text.splitlines():
        if origin is None:
            break
        m = FRAMES_RE.search(line)
        if not m:
            continue
        at = _stamp(line)
        if at is None:
            continue
        minutes = (at - origin).total_seconds() / 60.0
        if start_min <= minutes <= end_min:
            points.append((minutes, float(m.group(1))))
    if origin is None:
        res.not_reached = f"no line matches the anchor {anchor!r}"
        return res
    res.samples = len(points)
    if res.samples < min_samples:
        res.not_reached = f"{res.samples} frame sample(s) from minute {start_min:g} to {end_min:g}, at least {min_samples} needed"
        return res
    n = float(len(points))
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    slope = sxy / sxx if sxx else 0.0
    res.growth_mb_per_10min = slope * 10.0
    res.first_mb, res.last_mb = points[0][1], points[-1][1]
    if res.growth_mb_per_10min > max_mb_per_10min:
        res.failure = (f"private memory grows {res.growth_mb_per_10min:.2f} MB per 10 minutes "
                       f"({res.first_mb:g} -> {res.last_mb:g} MB), at most {max_mb_per_10min:g}")
    return res


# --- the runner's side (skytest.py: `[grade.memory_growth]` in a case) ----------------------------

SPEC_KEYS = {"peers", "anchor", "anchor_peer", "start_min", "end_min", "max_mb_per_10min", "min_samples"}


def check_spec(g: object, names: list[str]) -> str:
    """The grade's options; returns why they are wrong, or "". `anchor` (required): the pattern of
    the segment's start, searched in `anchor_peer`'s log (default H; its timestamp is the origin for
    every graded peer, all on one machine's clock); `peers`: the graded logs (default every peer);
    `start_min` (5), `end_min` (20), `max_mb_per_10min` (2.0): numbers; `min_samples` (30)."""
    if not isinstance(g, dict):
        return "must be a table"
    if unknown := set(g) - SPEC_KEYS:
        return f"unknown keys {sorted(unknown)} ({', '.join(sorted(SPEC_KEYS))})"
    if not isinstance(g.get("anchor"), str) or not g["anchor"]:
        return "`anchor` (a pattern) is required"
    try:
        re.compile(g["anchor"])
    except re.error as e:
        return f"`anchor` is not a pattern: {e}"
    if g.get("anchor_peer", "H") not in names:
        return f"`anchor_peer` {g.get('anchor_peer')!r} is not in {names}"
    peers = g.get("peers", names)
    if not isinstance(peers, list) or not peers or any(p not in names for p in peers):
        return f"`peers` must name peers of {names}"
    for key in ("start_min", "end_min", "max_mb_per_10min"):
        if key in g and (not isinstance(g[key], (int, float)) or isinstance(g[key], bool) or g[key] < 0):
            return f"{key} must be a number >= 0"
    if g.get("start_min", 5.0) >= g.get("end_min", 20.0):
        return "start_min must be below end_min"
    n = g.get("min_samples", 30)
    if not isinstance(n, int) or isinstance(n, bool) or n < 2:
        return "min_samples must be a whole number >= 2"
    return ""


def grade_spec(texts: dict[str, str], g: dict, names: list[str]) -> tuple[dict, list[str]]:
    """Runs the grade on each of `peers`; the anchor's time comes from `anchor_peer`'s log."""
    origin = find_anchor(texts.get(g.get("anchor_peer", "H"), ""), g["anchor"])
    record: dict = {}
    reasons: list[str] = []
    for peer in g.get("peers", names):
        res = grade_memory_growth(texts.get(peer, ""), g["anchor"], origin=origin,
                                  start_min=float(g.get("start_min", 5.0)), end_min=float(g.get("end_min", 20.0)),
                                  max_mb_per_10min=float(g.get("max_mb_per_10min", 2.0)),
                                  min_samples=g.get("min_samples", 30))
        record[peer] = {"samples": res.samples, "growth_mb_per_10min": round(res.growth_mb_per_10min, 3),
                        "first_mb": res.first_mb, "last_mb": res.last_mb, "not_reached": res.not_reached,
                        "violation": bool(res.failure)}
        if res.not_reached:
            reasons.append(f"grade memory_growth: {peer}: not reached: {res.not_reached}")
        elif res.failure:
            reasons.append(f"grade memory_growth: {peer}: {res.failure}")
    return record, reasons
