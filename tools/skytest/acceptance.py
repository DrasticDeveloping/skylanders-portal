"""The acceptance list (W39 item 6): every case the next build is judged by, run once each on one build, one game
session at a time, then the level checks of tutorial_a, tutorial_b and ww1 with two players, and one page at the end.

    python -I tools/skytest/skytest.py acceptance [--dry-run] [--run-not-ready] [--cases a,b] [--levels x,y]

Before any game starts it prints each case's estimated minutes (the median of its earlier runs' times in the store,
else its spec's timeout as the bound) and the total. Each case runs through `skytest.py test <case> --jobs 1` (its
spec's fail_fast stops it at its first fatal line; the runner's known-failure refusal, build-slot wait and preflight
gate hold as for any case); each level through `skytest.py levelcheck <level>` (a level pass with D, then its page).
The page (the store's `acceptance-<stamp>.md`): case, result, the lines that decided it (the case's reasons, a level
check's failed rows with their first finding), the log paths. Each case runs once: no repeats."""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# The cases the next build is judged by (the lead's W40 list), the cheap ones that would stop everything first: the
# plain game boots (offline-plain), two clients join through the lobby (party-continue), the owner's second session
# (soul-gem-tb), S's enemies on the clients (enemy-attack-ta), the players' hits on them (hits-that-count); then
# the rest. A case with no spec in the tree it runs from (one that arrives with its branch) gets a "no run" row.
CASES = ["offline-plain", "party-continue", "soul-gem-tb", "enemy-attack-ta", "hits-that-count",
         "moves-figure-mid-tb", "moves-swapper-tb", "moves-block-probe-tb", "moves-shot-tb", "moves-ko-tb",
         "world-event-cage-tb", "element-gate-tb", "door-alone-tb", "door-together-tb", "element-gate-dual-tb",
         "element-gate-dual-swapper-tb", "scene-lines-tb",
         "gpu-pipeline-drops", "gpu-pipeline-drops-fixed",
         "gpu-pipeline-drops-sync", "swap-zone-tb", "spark-lock-ww1", "rail-launch-tb", "fight-gate-tb",
         "enemy-damage-tb", "coins-broken-both"]
LEVELS = ["tutorial_a", "tutorial_b", "ww1"]
# Cases owed by an author who has not written (or named) their spec yet: their rows say so, never "absent".
OWED: dict[str, str] = {}
LEVEL_DEFAULT_S = 900.0      # a level check with no earlier run: the level pass's own bound
PAGE_LINE = re.compile(r"levelcheck \S+: page (.+)$", re.M)
# The owner's standing rule: our test sessions wait while he plays one of his games (the six Skylanders games:
# *recompiled.exe). Before each case the list waits while one runs, a line a minute; --while-owner-plays runs anyway.
OWNER_WAIT_S = 60.0
OWNER_AT_RISK = "OWNER DATA AT RISK"


def wait_for_owner(games, sleep=time.sleep, say=print, while_owner_plays: bool = False,
                   now=time.monotonic) -> float:
    """Waits while games() names one of the owner's own games (exe name, start time), a line every OWNER_WAIT_S;
    returns the seconds waited (0 at once, or with while_owner_plays: one line says it runs anyway)."""
    running = games()
    if not running:
        return 0.0
    names = ", ".join(n for n, _t in running)
    if while_owner_plays:
        say(f"acceptance: the owner's game {names} is running; --while-owner-plays: the case starts anyway")
        return 0.0
    t0 = now()
    while running:
        say(f"waiting: the owner's game {', '.join(n for n, _t in running)} is running")
        sleep(OWNER_WAIT_S)
        running = games()
    return now() - t0


@dataclass
class Row:
    case: str
    kind: str                  # "case" or "level check"
    result: str = "not run"
    seconds: float = 0.0
    lines: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)


def history_seconds(runs: Path, name: str) -> list[float]:
    """Earlier run times of a case (`<stamp>-<name>` folders' result.json `seconds`), runs that started games only."""
    out = []
    for d in runs.glob(f"*-{name}"):
        try:
            r = json.loads((d / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        s = r.get("seconds")
        if isinstance(s, (int, float)) and s > 0 and r.get("status") not in ("capacity", "not ready", "not run"):
            out.append(float(s))
    return out


def spec_timeout(repo: Path, case: str) -> float | None:
    p = repo / "tests" / "runtime" / f"{case}.toml"
    try:
        m = re.search(r"(?m)^timeout\s*=\s*(\d+)", p.read_text(encoding="utf-8"))
    except OSError:
        return None
    return float(m.group(1)) if m else None


def estimate(repo: Path, runs: Path, cases: list[str], levels: list[str],
             more: list[Path] | None = None) -> list[tuple[str, float | None, str]]:
    """Each case's and level's estimated seconds and where the figure comes from (`more`: other stores' run folders
    read for the history too, e.g. the integration worktree's)."""
    out = []
    folders = [runs] + list(more or [])

    def hist(name: str) -> list[float]:
        return [s for f in folders for s in history_seconds(f, name)]
    for c in cases:
        h = hist(c)
        if h:
            out.append((c, statistics.median(h), f"median of {len(h)} earlier run(s)"))
        else:
            t = spec_timeout(repo, c)
            out.append((c, t, "its spec's timeout (no earlier run)" if t else OWED.get(c, "no spec in this tree")))
    for lv in levels:
        h = hist(f"levelpass-{lv}")
        out.append((f"levelcheck {lv}", statistics.median(h) if h else LEVEL_DEFAULT_S,
                    f"median of {len(h)} earlier level pass(es)" if h else "the level pass's bound (no earlier run)"))
    return out


def estimate_text(est: list[tuple[str, float | None, str]]) -> str:
    total = sum(s for _n, s, _w in est if s)
    lines = [f"acceptance: {len(est)} item(s), estimated {total / 60:.0f} min in all (one game session at a time):"]
    lines += [f"  {n:<28} {('%.1f min' % (s / 60)) if s else '-':>9}  ({w})" for n, s, w in est]
    return "\n".join(lines)


def new_runs(runs: Path, name: str, before: set[str]) -> list[Path]:
    return sorted(d for d in runs.glob(f"*-{name}") if d.is_dir() and d.name not in before)


def case_row(case: str, run: Path | None, out_tail: str, seconds: float) -> Row:
    row = Row(case, "case", seconds=seconds)
    if run is None:
        row.result = "no run"
        row.lines = [l for l in out_tail.splitlines() if l.strip()][-3:] or ["the runner started no game"]
        return row
    try:
        r = json.loads((run / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        row.result, row.lines, row.logs = "no result", [f"{run}: no result.json"], [str(run)]
        return row
    row.result = str(r.get("status", "?")).upper()
    row.lines = [str(x) for x in r.get("reasons", []) or []][:4]
    owner = [str(x) for x in r.get("notes", []) or [] if "owner data" in str(x) or OWNER_AT_RISK in str(x)]
    if owner and not any(OWNER_AT_RISK in x for x in row.lines):
        row.lines.append(owner[0])
    for p in r.get("peers", []) or []:
        if not isinstance(p, dict):
            continue
        row.lines += [f"{p.get('peer')}: {x}" for x in (p.get("reasons") or [])[:2]]
    row.lines = row.lines[:6]
    row.logs = [str(run)] + [str(x) for x in sorted(run.glob("*/game.log"))] + \
        [str(x) for x in sorted((run / "H" / "data" / "Online" / "session").glob("game*.log"))]
    return row


def level_row(level: str, run: Path | None, page: str | None, code: int, seconds: float, repo: Path,
              levelpass: Path) -> Row:
    row = Row(f"levelcheck {level}", "level check", seconds=seconds)
    if run is None:
        row.result = f"no run (exit {code})"
        return row
    import levelcheck as lc
    try:
        verdicts = lc.grade(run, repo, levelpass)
    except Exception as e:  # noqa: BLE001 - a grader's crash is the row's result, said
        row.result, row.lines = "grade failed", [f"{type(e).__name__}: {e}"]
        return row
    row.result = lc.overall(verdicts)
    row.lines = [f"{v.name}: {v.findings[0].what}" if v.findings else f"{v.name}: {v.counts[:160]}"
                 for v in verdicts if v.status == lc.FAIL][:8]
    row.logs = ([page] if page else []) + [str(run)]
    return row


def session_line(repo: Path) -> str:
    """The owner's session settings the list runs with (the game's tests/session_settings.toml), for the page and
    the dry run; SessionSettingsError when the file does not read (the list then does not start)."""
    import session_settings
    ss = session_settings.load(repo)
    return (f"Session settings: {ss.label()}, {len(ss.on)} settings on every game of every session case and level "
            f"check ({session_settings.FILE.as_posix()})")


def page_text(rows: list[Row], est_text: str, build: str, session: str = "") -> str:
    out = [f"# Acceptance run {time.strftime('%Y-%m-%d %H:%M')}", "", f"Build: {build}", "",
           *([session, ""] if session else []), "```", est_text, "```", "",
           "| Case | Result | Time | The lines that decided it | Logs |", "|---|---|---|---|---|"]
    for r in rows:
        dec = "<br>".join(x.replace("|", "\\|")[:300] for x in r.lines) or "-"
        logs = "<br>".join(f"`{x}`" for x in r.logs[:6]) or "-"
        out.append(f"| {r.case} | {r.result} | {r.seconds / 60:.1f} min | {dec} | {logs} |")
    passed = sum(1 for r in rows if r.result.startswith("PASS"))
    out += ["", f"{passed} of {len(rows)} passed; each case ran once."]
    return "\n".join(out) + "\n"


def run(skytest_py: Path, repo: Path, store: Path, levelpass: Path, cases: list[str], levels: list[str],
        run_not_ready: bool, dry_run: bool, build: str, history: list[Path] | None = None,
        while_owner_plays: bool = False, owner_games=None) -> int:
    runs = store / "runs"
    est = estimate(repo, runs, cases, levels, history)
    et = estimate_text(est)
    print(et, flush=True)
    import session_settings
    try:
        session = session_line(repo)
    except session_settings.SessionSettingsError as e:
        print(f"acceptance: the owner's session settings do not read, the list does not start: {e}", flush=True)
        return 2
    print(f"acceptance: {session}", flush=True)
    if owner_games is None:
        import skytest
        owner_games = lambda: skytest.owner_games_running(os.getpid())  # noqa: E731
    if dry_run:
        playing = owner_games()
        if playing:
            names = ", ".join(f"{n} (started {s})" for n, s in playing)
            print(f"acceptance: the owner's game {names} is running: "
                  + ("--while-owner-plays: it would start anyway" if while_owner_plays
                     else "it would wait before each case until it ends"), flush=True)
        else:
            print("acceptance: none of the owner's games is running: it would start at once", flush=True)
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = store / f"acceptance-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    rows: list[Row] = []
    for c in cases:
        if not (repo / "tests" / "runtime" / f"{c}.toml").is_file():
            # No spec in this tree: owed (named by OWED) or one that arrives with its branch; no runner is started.
            rows.append(Row(c, "case", result="OWED" if c in OWED else "no spec",
                            lines=[OWED.get(c, "no tests/runtime/" + c + ".toml in this tree")]))
            print(f"acceptance: {c}: {rows[-1].result}", flush=True)
            continue
        wait_for_owner(owner_games, say=lambda s: print(s, flush=True), while_owner_plays=while_owner_plays)
        before = {d.name for d in runs.glob(f"*-{c}")}
        # Every session case with the owner's session settings on every game (a spec may say false, on purpose).
        cmd = [sys.executable, "-I", str(skytest_py), "test", c, "--jobs", "1", "--session-settings"] + (
            ["--run-not-ready"] if run_not_ready else [])
        print(f"acceptance: {c} ...", flush=True)
        t0 = time.monotonic()
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(repo))
        (work / f"{c}.out.txt").write_text(p.stdout + p.stderr, encoding="utf-8")
        got = new_runs(runs, c, before)
        rows.append(case_row(c, got[-1] if got else None, p.stdout[-2000:], time.monotonic() - t0))
        print(f"acceptance: {c}: {rows[-1].result}", flush=True)
        if any(OWNER_AT_RISK in x for x in rows[-1].lines) or OWNER_AT_RISK in p.stdout:
            print(f"acceptance: {OWNER_AT_RISK} in {c}: the list stops here", flush=True)
            rows[-1].lines.insert(0, f"{OWNER_AT_RISK}: the acceptance run stopped after this case")
            levels = []
            break
    for lv in levels:
        wait_for_owner(owner_games, say=lambda s: print(s, flush=True), while_owner_plays=while_owner_plays)
        before = {d.name for d in runs.glob(f"*-levelpass-{lv}")}
        cmd = [sys.executable, "-I", str(skytest_py), "levelcheck", lv]
        print(f"acceptance: levelcheck {lv} ...", flush=True)
        t0 = time.monotonic()
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(repo))
        (work / f"levelcheck-{lv}.out.txt").write_text(p.stdout + p.stderr, encoding="utf-8")
        got = new_runs(runs, f"levelpass-{lv}", before)
        m = PAGE_LINE.search(p.stdout)
        rows.append(level_row(lv, got[-1] if got else None, m.group(1).strip() if m else None, p.returncode,
                              time.monotonic() - t0, repo, levelpass))
        print(f"acceptance: levelcheck {lv}: {rows[-1].result}", flush=True)
    text = page_text(rows, et, build, session)
    out = store / f"acceptance-{stamp}.md"
    out.write_text(text, encoding="utf-8")
    print(text, flush=True)
    print(f"acceptance: page {out}", flush=True)
    return 0 if all(r.result.startswith("PASS") for r in rows) else 1
