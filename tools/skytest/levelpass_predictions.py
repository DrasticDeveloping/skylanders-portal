"""The level pass's predictions mode (`skytest.py levelpass <level> --predictions`): predicted co-op bugs
confirmed or refuted in one session, each as steps, snapshots and a check, with a verdict per bug:
CONFIRMED, NOT SEEN or COULD NOT TEST (why).

The recipes are data, in the levelpass folder (they name level objects, so they stay outside git):
<levelpass>/predictions.toml, one [[bug]] per prediction:

    [[bug]]
    n = 3                                  # the prediction's number (coop-audit-ranked.txt)
    level = "tutorial_b"
    title = "push blocks move only for the pusher"
    steps = ["lean_into GA3_PushBlock_02 6"]   # script lines; objects by name or auth:<zone>:<index>
    snap = ["GA3_PushBlock_02"]            # dumped on H and on S after the steps (and by D, see d)
    checks = ["position_differs GA3_PushBlock_02 50"]
    # could_not_test = "why"               # the recipe cannot run yet: the verdict says why

and per level, [level.<name>] d = "early" | "late" | "none" ("early" when not given): D joins at the
start and stays to the end (its position logged every second: another player's scene, teleport or
interior pull shows on it; when H ends the session D snapshots the bugs' names, its world as it
watched H act, for the exists/position/count checks), or joins at the end and snapshots (a late
joiner's world: the late-join bugs), or is not started. One player (H) acts in every step and D is the
observed one; a step by D at a moment H chooses needs a cross-client signal the pass does not have yet.

Checks (any one confirmed confirms the bug; a check whose inputs are missing makes it COULD NOT TEST):
    exists_differs <name>           the entity is live on one side of S / H (/ D) and not on another
    position_differs <name> <tol>   it is live on both and its positions differ by more than tol
    count_differs <substring>       the number of live entities holding the substring differs
    d_jump <distance>               D's hero moved more than distance within 8 s after the step began
    log <peer> <regex>              the peer logged a matching line after the step began
A step whose own command failed makes the bug COULD NOT TEST with that line, unless a check confirmed it.

No step carries a position (levelpass.position_literals); snapshots and D's positions are the game's.
"""
from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime

CHECKS = ("exists_differs", "position_differs", "count_differs", "d_jump", "log")
D_MODES = ("early", "late", "none")
D_JUMP_WINDOW_S = 8.0


@dataclass
class Bug:
    n: int
    level: str
    title: str
    steps: list[str] = field(default_factory=list)
    snap: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    could_not_test: str = ""


@dataclass
class Recipes:
    bugs: list[Bug]
    d_mode: dict[str, str]


def load_recipes(text: str) -> Recipes:
    """predictions.toml; ValueError for a recipe it cannot use (an unknown check, a bad d mode)."""
    d = tomllib.loads(text)
    bugs = []
    for b in d.get("bug", []):
        bug = Bug(n=int(b["n"]), level=str(b["level"]), title=str(b.get("title", "")),
                  steps=[str(x) for x in b.get("steps", [])], snap=[str(x) for x in b.get("snap", [])],
                  checks=[str(x) for x in b.get("checks", [])], could_not_test=str(b.get("could_not_test", "")))
        for c in bug.checks:
            if c.split()[0] not in CHECKS:
                raise ValueError(f"bug {bug.n}: unknown check {c!r} ({', '.join(CHECKS)})")
        for s in bug.steps + bug.snap:
            if ";" in s or "#" in s:
                raise ValueError(f"bug {bug.n}: {s!r} has ';' or '#' (the input script splits or comments there)")
        bugs.append(bug)
    modes = {}
    for lv, t in d.get("level", {}).items():
        m = str(t.get("d", "none"))
        if m not in D_MODES:
            raise ValueError(f"level {lv}: d = {m!r} ({', '.join(D_MODES)})")
        modes[lv] = m
    return Recipes(bugs=bugs, d_mode=modes)


def h_lines(bug: Bug) -> list[str]:
    """H's lines for one bug: its mark, its steps, its snapshots on H and on S."""
    if bug.could_not_test:
        return [f"log LEVELPASS pred {bug.n} skipped"]
    out = [f"log LEVELPASS pred {bug.n} begin"]
    out += [line for line in bug.steps]
    out.append("wait_free 15")
    out.append(f"log LEVELPASS pred {bug.n} snap")
    for name in bug.snap:
        out += [f"dump_entities {name}", f"online_server_dump_entities {name}", "waitfor online_server_dump_heard 30"]
    out.append(f"log LEVELPASS pred {bug.n} end")
    return out


def d_snap_lines(bugs: list[Bug]) -> list[str]:
    """D's snapshot of every name the bugs snapshot (a late joiner's world)."""
    names = sorted({n for b in bugs if not b.could_not_test for n in b.snap})
    return ["log LEVELPASS D snap"] + [f"dump_entities {n}" for n in names] + ["log LEVELPASS D snap end"]


# --- reading the logs -----------------------------------------------------------------------------------

TIME = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]")
ENTITY = re.compile(r"Online: entity (\d+) (\S+) class (\S+) at (\S+) team")
MARK = re.compile(r"Input script: LEVELPASS pred (\d+) (begin|snap|end|skipped)")
S_ASK = re.compile(r"Online: the owner asked for the entity list(?:: (\S+))?")
# The trace line is "Online: where <zone> at x,y,z" (trace_where_seconds); the older "Where: <zone> at x y z" too.
WHERE = re.compile(r"(?:Online: where|Where:) \S+ at (-?[\d.]+)[ ,](-?[\d.]+)[ ,](-?[\d.]+)")
FAILED = re.compile(r"Dev: \S+ .*\bfailed \(.*")


def _t(line: str) -> float | None:
    m = TIME.match(line)
    return datetime.strptime(m.group(1)[:23], "%Y-%m-%d %H:%M:%S.%f").timestamp() if m else None


def _pos(text: str) -> tuple[float, float, float] | None:
    try:
        x, y, z = (float(v) for v in text.split(","))
        return (x, y, z)
    except ValueError:
        return None


@dataclass
class Snap:
    """The live entities of one name on one peer at one snapshot: name -> positions."""
    found: dict[str, list[tuple[float, float, float] | None]] = field(default_factory=dict)
    taken: bool = False


def h_windows(h_text: str) -> dict[int, dict]:
    """Per bug: its begin and snap times and its H snapshot (the entity lines between snap and end), the
    failed steps between begin and snap."""
    out: dict[int, dict] = {}
    cur = None
    phase = ""
    for line in h_text.splitlines():
        m = MARK.search(line)
        if m:
            n = int(m.group(1))
            w = out.setdefault(n, {"begin": None, "snap": None, "end": None, "h": Snap(), "failed": [], "skipped": False})
            w[m.group(2)] = _t(line)
            if m.group(2) == "skipped":
                w["skipped"] = True
            cur, phase = n, m.group(2)
            if phase == "snap":
                w["h"].taken = True
            continue
        if cur is None:
            continue
        if phase == "begin" and FAILED.search(line):
            out[cur]["failed"].append(line.strip())
        elif phase == "snap":
            e = ENTITY.search(line)
            if e:
                out[cur]["h"].found.setdefault(e.group(2), []).append(_pos(e.group(4)))
    return out


def s_snapshots(s_text: str, windows: dict[int, dict]) -> dict[int, Snap]:
    """S's entity lines after each owner's request, assigned to the bug whose snap window holds the
    request's time."""
    out: dict[int, Snap] = {}
    cur: Snap | None = None
    for line in s_text.splitlines():
        m = S_ASK.search(line)
        if m:
            t = _t(line)
            cur = None
            for n, w in windows.items():
                if w["snap"] is not None and t is not None and w["snap"] - 1.0 <= t <= (w["end"] or t) + 5.0:
                    cur = out.setdefault(n, Snap(taken=True))
            continue
        e = ENTITY.search(line)
        if e and cur is not None:
            cur.found.setdefault(e.group(2), []).append(_pos(e.group(4)))
    return out


def d_snapshot(d_text: str) -> Snap:
    snap = Snap()
    on = False
    for line in d_text.splitlines():
        if "LEVELPASS D snap end" in line:
            on = False
        elif "LEVELPASS D snap" in line:
            on, snap.taken = True, True
        elif on:
            e = ENTITY.search(line)
            if e:
                snap.found.setdefault(e.group(2), []).append(_pos(e.group(4)))
    return snap


def d_positions(d_text: str) -> list[tuple[float, tuple[float, float, float]]]:
    out = []
    for line in d_text.splitlines():
        m = WHERE.search(line)
        t = _t(line)
        if m and t is not None:
            out.append((t, (float(m.group(1)), float(m.group(2)), float(m.group(3)))))
    return out


# --- verdicts ------------------------------------------------------------------------------------------

CONFIRMED, NOT_SEEN, COULD_NOT = "CONFIRMED", "NOT SEEN", "COULD NOT TEST"


def check_one(check: str, snaps: dict[str, Snap], begin: float | None, texts: dict[str, str],
              dpos: list[tuple[float, tuple[float, float, float]]]) -> tuple[str, str]:
    w = check.split()
    kind = w[0]
    taken = {p: s for p, s in snaps.items() if s.taken}
    if kind in ("exists_differs", "position_differs"):
        name = w[1]
        if "H" not in taken or "S" not in taken:
            return COULD_NOT, f"no snapshot of {name} on {'H' if 'H' not in taken else 'S'}"
        present = {p: name in s.found for p, s in taken.items()}
        if kind == "exists_differs":
            if len(set(present.values())) > 1:
                return CONFIRMED, f"{name} live on {[p for p, v in present.items() if v]}, not on {[p for p, v in present.items() if not v]}"
            return NOT_SEEN, f"{name} {'live' if present['H'] else 'absent'} on {sorted(present)}"
        tol = float(w[2])
        if not all(present.values()):
            return COULD_NOT, f"{name} not live on {[p for p, v in present.items() if not v]}"
        firsts = {p: s.found[name][0] for p, s in taken.items()}
        if any(v is None for v in firsts.values()):
            return COULD_NOT, f"{name} has no position on a peer"
        far = max(math.dist(a, b) for a in firsts.values() for b in firsts.values())
        where = ", ".join(f"{p} {v[0]:.0f},{v[1]:.0f},{v[2]:.0f}" for p, v in firsts.items())
        return (CONFIRMED if far > tol else NOT_SEEN), f"{name} at {where} (apart {far:.0f}, tolerance {tol:.0f})"
    if kind == "count_differs":
        sub = w[1].lower()
        if "H" not in taken or "S" not in taken:
            return COULD_NOT, "no snapshot on H or S"
        counts = {p: sum(len(v) for k, v in s.found.items() if sub in k.lower()) for p, s in taken.items()}
        if not any(counts.values()):
            return COULD_NOT, f"no live {w[1]}* on any peer (the step made none)"
        return (CONFIRMED if len(set(counts.values())) > 1 else NOT_SEEN), f"live {w[1]}*: {counts}"
    if kind == "d_jump":
        dist = float(w[1])
        if begin is None:
            return COULD_NOT, "the step never began"
        before = [p for t, p in dpos if t <= begin]
        after = [p for t, p in dpos if begin < t <= begin + D_JUMP_WINDOW_S]
        if not before or not after:
            return COULD_NOT, "no position of D around the step (D not in the session then)"
        jump = max(math.dist(before[-1], p) for p in after)
        return (CONFIRMED if jump > dist else NOT_SEEN), f"D moved up to {jump:.0f} within {D_JUMP_WINDOW_S:.0f} s (limit {dist:.0f})"
    if kind == "log":
        peer, rx = w[1], re.compile(" ".join(w[2:]))
        text = texts.get(peer)
        if text is None:
            return COULD_NOT, f"no log of {peer}"
        for line in text.splitlines():
            t = _t(line)
            if t is not None and begin is not None and t >= begin and rx.search(line):
                return CONFIRMED, line.strip()[:200]
        return NOT_SEEN, f"no line /{rx.pattern}/ on {peer} after the step"
    return COULD_NOT, f"unknown check {kind}"


def verdicts(recipes: list[Bug], texts: dict[str, str]) -> list[tuple[Bug, str, list[str]]]:
    """Each bug's verdict and its reasons, from the run's logs (H, S and D when there)."""
    windows = h_windows(texts.get("H", ""))
    s_snaps = s_snapshots(texts.get("S", ""), windows)
    d_snap = d_snapshot(texts["D"]) if "D" in texts else Snap()
    dpos = d_positions(texts["D"]) if "D" in texts else []
    out = []
    for bug in recipes:
        if bug.could_not_test:
            out.append((bug, COULD_NOT, [bug.could_not_test]))
            continue
        w = windows.get(bug.n)
        if w is None or w["begin"] is None:
            out.append((bug, COULD_NOT, ["the pass did not reach its steps (an earlier stop, a crash or the timeout)"]))
            continue
        snaps = {"H": w["h"], "S": s_snaps.get(bug.n, Snap())}
        if d_snap.taken:
            snaps["D"] = d_snap
        results = [check_one(c, snaps, w["begin"], texts, dpos) for c in bug.checks]
        reasons = [f"{c}: {v}: {why}" for c, (v, why) in zip(bug.checks, results)]
        if any(v == CONFIRMED for v, _ in results):
            verdict = CONFIRMED
        elif w["failed"]:
            verdict = COULD_NOT
            reasons.append("a step failed: " + w["failed"][0][-200:])
        elif any(v == COULD_NOT for v, _ in results):
            verdict = COULD_NOT
        else:
            verdict = NOT_SEEN
        out.append((bug, verdict, reasons))
    return out


def verdict_text(rows: list[tuple[Bug, str, list[str]]]) -> str:
    lines = ["predictions:"]
    for bug, v, reasons in sorted(rows, key=lambda r: r[0].n):
        lines.append(f"  #{bug.n} {v}: {bug.title}")
        for r in reasons:
            lines.append(f"      {r}")
    return "\n".join(lines) + "\n"


# --- the scripts and the case ---------------------------------------------------------------------------

def scripts(level: str, bugs: list[Bug], d_mode: str) -> tuple[str, str | None]:
    """H's and D's scripts of a predictions run: H boots, hosts and loads the level, runs each bug's
    steps and snapshots (an audit after each), then waits for D (late: D joins, snapshots and leaves;
    early: D joined at the start and idles, its position logged) and ends the session."""
    import levelpass as lp
    h = lp.boot_lines(True, lp.FIGURE_H)
    h += ["waitfor online_server_hub 300", "online_mark_level", f"level {level}",
          "waitfor online_level_after_mark 300", "waitfor online_barrier 300", "wait_free 120",
          "log LEVELPASS phase predictions", "online_audit start", f"hold_cap {lp.HOLD_CAP_S}"]
    if d_mode == "early":
        # D in play and its pose shown here (online_peer_shown), not only connected: run 20261118-114820
        # began the bugs 17 s before D was in the level.
        h += ["log LEVELPASS late-join start", "online_wait_peers 2 300", "waitfor online_peer_shown 300", "wait_free 60"]
    for bug in bugs:
        h += h_lines(bug)
        if not bug.could_not_test:
            h.append(f"online_audit p{bug.n}")
    if d_mode == "late":
        h += ["log LEVELPASS late-join start", "log LEVELPASS phase late-join", "waitfor online_peer_was_in 300",
              "waitfor online_alone 300", "online_audit late"]
    h += ["log LEVELPASS done", "online_end", "quit"]
    d = None
    if d_mode != "none":
        d = lp.boot_lines(False, lp.FIGURE_D)
        d += ["waitfor online_in_play 300", "online_join {join}", "waitfor online_barrier 300", "wait_free 120",
              "log LEVELPASS D in", "online_audit d-in"]
        if d_mode == "late":
            d += d_snap_lines(bugs) + ["online_end", "quit"]
        else:
            # Idle to the end, its position every second (trace_where_seconds); then its own snapshot of the
            # bugs' names: the world it saw H change.
            d += ["waitfor online_left 1200"] + d_snap_lines(bugs) + ["quit"]
    return "\n".join(h) + "\n", ("\n".join(d) + "\n" if d else None)
