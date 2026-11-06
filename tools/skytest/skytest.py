#!/usr/bin/env python3
"""skytest: the multiplayer test runner of the Skylanders ports' online forks.

Run from a game repository (its `just test-online` calls this). A case is a TOML spec in the game's
tests/runtime/; it names a layout and what each peer's game does (an input script) and what its log
must show. The runner starts the layout's game processes, each on its own copies of the test data,
ends them, and writes report.md and results.json in a new run folder of the worktree's test store.

    skytest.py test [CASE ...] [--stage S] [--max-games N]   run cases (a name, a glob, or all)
    skytest.py list                                         the cases and the manifest's state
    skytest.py lint                                         check every spec and the manifest
    skytest.py fixtures --from-owner                        copy the owner's saves and figures into
                                                            the fixture store (read only on their side)
    skytest.py compare RUN RUN [RUN ...] --spec CASE        compare finished runs with that case's
               [--noise N]                                  [compare] settings (the first N are the
                                                            A/A; default all but the last of 3 or more)

Layouts (the game repository's docs/testing-guide.md has the details):

    solo        H alone: the plain game, offline (reference measurements)
    server+N    the server peer S and N clients H, D, E, F (N = 1..4); H is the owner's client

Until stage 1A gives the game a server role, S is a plain game instance with the runner's own
lifecycle script (it boots hidden and quits); it runs no case script.

Rules the runner enforces (docs/RULES.md of the game): outputs only inside the worktree's
.replication-tests/; never under the owner's data folder, never into the disc; the build is copied
into a snapshot and run from there, never from the build folder; every setting passed is non-empty;
every game is in a job object, so none outlives the run; a case that cannot run says why in its
report (capacity, missing fixture, spec error) instead of being skipped silently.

Standard library only; run with `python -I`.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import winjob  # noqa: E402

STORE = ".replication-tests"
CLIENTS = ["H", "D", "E", "F"]
LAYOUT = re.compile(r"^(solo|server\+([1-4]))$")
PEER_DIRS = ("data", "figures", "cache")
# Layouts the manifest may name that one PC cannot run: a second PC, other networks, a test binary.
MANIFEST_ONLY_LAYOUTS = ("server+3+LAN", "internet+2", "unit")

# Log lines the game writes (skylanders-portal src/dev, the game's where hook).
TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]")
FPS = re.compile(r"Input script: ([\d.]+) guest FPS \((\d+) frames in ([\d.]+) s\), longest frame (\d+) ms, "
                 r"(\d+) over 50 ms")
WHERE = re.compile(r"Where: (\w+) at (-?[\d.]+) (-?[\d.]+) (-?[\d.]+)")
MEMORY = re.compile(r"Input script: memory (\d+) MB private, (\d+) MB working set")
BUILD = re.compile(r"\(build (\w+)\)")
# The online code's frame-time event (stage 1A): every 10 s on the server and on a client in a session.
FRAMES = re.compile(r"Online: frames (\d+) fps ([\d.]+) p50 ([\d.]+) p95 ([\d.]+) max ([\d.]+) "
                    r"private (-?\d+) working (-?\d+) video (-?\d+)")
# Where a hosted server (`server = "host"`) writes its log: the owner's session folder.
HOSTED_SERVER_LOG = Path("data") / "Online" / "session" / "game.log"
GRADES = ("server_frame_time", "client_fps", "level_load", "save_copy")
# A title folder's name (the game's title id in hex), for the save_copy grade.
TITLE_ID = re.compile(r"^[0-9A-F]{8}$")
ALWAYS_FORBIDDEN = [r"\] Crash: ", r"\[FATAL\]", r"Input script: cannot (parse|use)"]

# A case's [compare] (stage 1B item 3, the draw-skip A/A and A/B; docs/online-1B.md P3): the same
# workload run several times, and what the compared peer's logs must agree on.
# The SDK's gpu_draw_counters line, one per ~10 s window (rexglue-sdk include/rex/graphics/draw_skip.h,
# FormatCounters; its unit test pins the format) (code).
GPU_COUNTERS = re.compile(r"GPU: draws (\d+) memexport (\d+) copies (\d+) swaps (\d+) zpd (\d+) viz (\d+) "
                          r"readbacks (\d+) skipped (\d+)/(\d+) in ([\d.]+) s")
GPU_COUNTER_NAMES = ("draws", "memexport", "copies", "swaps", "zpd", "viz", "readbacks", "skipped_draws",
                     "skipped_copies")
# The barrier and level lines graded for counts and order, each normalized (ticks, times and
# addresses left out). Two families, each ordered by cause on its own thread: the session's lines
# (session_host.cpp, the network pump and the level job) and the level watch's (level_watch.cpp, the
# game's thread) (code). How the two interleave is a race between threads, so each family is
# compared on its own (inference).
SESSION_SEQUENCE = (
    (re.compile(r"Online: the owner asked for (\S+)"), "the owner asked for {0}"),
    (re.compile(r"Online: level (\d+) (\S+): open"), "level {0} {1}: open"),
    (re.compile(r"Online: level (\d+): the server has it up"), "level {0}: the server has it up"),
    (re.compile(r"Online: barrier (\d+) released at tick \d+ \((\d+) clients?\)"), "barrier {0} released ({1} client(s))"),
    (re.compile(r"Online: the server is in its hub(?: \(([^)]*)\))?"), "the server is in its hub ({0})"),
    (re.compile(r"Online: End session: closing \((\d+) clients? to drain\)"), "End session: closing ({0} to drain)"),
)
LEVEL_WATCH_STEP = re.compile(r"Level watch: (.+?): (requested by the game|loading screen up|the game's load call "
                              r"returned|ready|no player during the load)")
LEVEL_WATCH_REQUEST = re.compile(r"\(request (\d+)")
LEVEL_WATCH_LOAD = re.compile(r"Level watch: (.+?): the game's load call returned after (\d+) ms")
LEVEL_OPEN = re.compile(r"Online: level (\d+) (\S+): open")
LEVEL_UP = re.compile(r"Online: level (\d+): the server has it up")
BARRIER = re.compile(r"Online: barrier (\d+) released at tick \d+")
SERVER_HUB = re.compile(r"Online: the server is in its hub")
# trace_world_messages (src/online/dev/traces.cpp): each key is logged its first 20 times and then
# every 500th with its running count (#n); `Trace counts: <k> keys` and `Trace count: <key> <n>`
# lines (LogTraceCounts) give every key's exact count so far (code).
TRACE_MESSAGE = re.compile(r"Trace: message (\S+) to .*\(#(\d+)\)\s*$")
TRACE_TELEGRAM = re.compile(r"Trace: telegram (\S+) sender .*\(#(\d+)\)\s*$")
TRACE_TRIGGER = re.compile(r"Trace: trigger (.+) (enter|exit|act|de-act|touch|other) by .*\(bits 0x[0-9A-Fa-f]+, "
                           r"#(\d+)\)\s*$")
TRACE_COUNTS = re.compile(r"Trace counts: (\d+) keys")
TRACE_COUNT = re.compile(r"Trace count: (.+) (\d+)\s*$")
DEFAULT_WINDOW = r"Online: barrier \d+ released at tick"
RUN_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
COMPARE_KEYS = {"peer", "run", "noise", "test", "reached", "window", "window_end", "load_floor_s", "memexport_zero"}

# The lifecycle script of the stand-in server peer until stage 1A: boots, stays up, quits.
STANDIN_SERVER_SCRIPT = "wait {seconds}\nfps 5\nquit\n"


class SpecError(Exception):
    pass


# --- configuration --------------------------------------------------------------------------------


@dataclass
class Config:
    repo: Path
    store: Path
    exe: str
    build: Path
    rom: Path
    settings_template: Path
    default_args: list[str]
    timeout: float
    start_gap: float
    cases_dir: Path
    manifest: Path
    thresholds: Path
    baseline: Path
    owner_saves: Path
    owner_figures: Path
    launcher: list[str]
    # The game's title id (its save folder <data>/<profile>/<title_id>), for the save_copy grade.
    title_id: str = ""


def repo_root(start: Path) -> Path:
    r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       encoding="utf-8", errors="surrogateescape")
    if r.returncode != 0:
        raise SystemExit(f"{start} is not inside a git checkout of a game")
    return Path(r.stdout.strip()).resolve()


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def load_config(repo: Path) -> Config:
    f = repo / "tests" / "skytest.toml"
    if not f.is_file():
        raise SystemExit(f"{f} is missing: the game repository names its exe, build and fixtures there")
    c = tomllib.loads(f.read_text(encoding="utf-8"))
    g, fx, d = c.get("game", {}), c.get("fixtures", {}), c.get("defaults", {})
    return Config(
        repo=repo,
        store=repo / STORE,
        exe=g["exe"],
        build=repo / g["build"],
        rom=(repo / g.get("rom", "rom")),
        settings_template=repo / g["settings_template"],
        default_args=list(d.get("args", [])),
        timeout=float(d.get("timeout", 300)),
        start_gap=float(d.get("start_gap_seconds", 3)),
        cases_dir=repo / c.get("cases", "tests/runtime"),
        manifest=repo / c.get("manifest", "tests/manifest.toml"),
        thresholds=repo / c.get("thresholds", "tests/thresholds.toml"),
        baseline=repo / c.get("baseline", "tests/baseline.toml"),
        owner_saves=expand(fx["owner_saves"]),
        owner_figures=expand(fx["owner_figures"]),
        # Only the runner's own tests set this: a command that stands in for the game's exe.
        launcher=[os.path.expandvars(x) for x in g.get("launcher", [])],
        title_id=str(g.get("title_id", "")),
    )


# --- safety ---------------------------------------------------------------------------------------


def owner_data_roots() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    docs = [home / "Documents", home / "OneDrive" / "Documents"]
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(var):
            docs.append(Path(os.environ[var]) / "Documents")
    if os.name == "nt":
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _ = winreg.QueryValueEx(k, "Personal")
            if value:
                docs.append(Path(os.path.expandvars(value)))
        except OSError:
            pass
    return [d / "Skylanders" for d in docs]


def inside(path: Path, folder: Path) -> bool:
    p, f = path.resolve(), folder.resolve()
    return p == f or f in p.parents


def check_output_path(cfg: Config, path: Path) -> None:
    """Everything a run writes goes inside the worktree's store, never the owner's data or the disc."""
    if not inside(path, cfg.store):
        raise SystemExit(f"refusing to write {path}: outside the test store {cfg.store}")
    for root in owner_data_roots():
        if inside(path, root):
            raise SystemExit(f"refusing to write {path}: inside the owner's data folder {root}")
    if inside(path, cfg.rom):
        raise SystemExit(f"refusing to write {path}: inside the disc")


# Settings the runner sets for each peer itself. A case or the defaults may not set them again: the
# game takes the last of two, so a second --user_data_root would point a test at any folder.
RUNNER_OWNED = ("user_data_root", "cache_root", "config_file", "log_file", "portal_figures_dir",
                "game_data_root", "input_script")


def check_args(args: list[str]) -> None:
    """A setting with an empty value makes the game drop its whole command line (and run on the
    owner's real data): refuse it. Each runner-owned setting appears exactly once, and the portal is
    the software one (a real portal would get the test's writes)."""
    counts: dict[str, int] = {}
    for a in args:
        if a.startswith("--") and a.endswith("="):
            raise SpecError(f"refusing an empty setting {a!r}: the game would ignore its whole command line")
        if a.startswith("--"):
            key = a[2:].split("=", 1)[0]
            key = key[3:] if key.startswith("no-") else key
            counts[key] = counts.get(key, 0) + 1
    for key in RUNNER_OWNED:
        if counts.get(key, 0) != 1:
            raise SpecError(f"refusing to start a game with --{key} set {counts.get(key, 0)} times (the runner"
                             " sets it once, for the peer's own copy)")
    if counts.get("portal_mode", 0) != 1 or "--portal_mode=software" not in args:
        raise SpecError("refusing to start a game without exactly one --portal_mode=software")


# --- specs ----------------------------------------------------------------------------------------


@dataclass
class PeerSpec:
    name: str
    script: str = ""
    args: list[str] = field(default_factory=list)
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    # Count and order expectations (stage 1B item 3): a pattern's exact number of matching lines,
    # and patterns whose first matches come in this order (each after the one before).
    expect_count: dict[str, int] = field(default_factory=dict)
    expect_order: list[str] = field(default_factory=list)


@dataclass
class Spec:
    name: str
    path: Path
    layout: str
    stage: str
    description: str
    timeout: float
    peers: dict[str, PeerSpec]
    measures: dict[str, dict]
    standin_seconds: float
    # "standin": the runner starts S as a plain game (stage 0); "host": H hosts with --role=server
    # as its child (stage 1A on), and S's log is read from H's session folder.
    server: str = "standin"
    grades: dict[str, dict] = field(default_factory=dict)
    # [compare]: the workload is run once per [[compare.run]], and the runs' logs are compared.
    compare: dict = field(default_factory=dict)


def peers_of(layout: str) -> list[str]:
    m = LAYOUT.match(layout)
    if not m:
        raise SpecError(f"unknown layout {layout!r} (solo, server+1 .. server+4)")
    if layout == "solo":
        return ["H"]
    return ["S"] + CLIENTS[: int(m.group(2))]


def load_spec(path: Path, default_timeout: float) -> Spec:
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SpecError(f"{path.name}: {e}") from e
    for key in ("name", "layout", "stage"):
        if key not in d:
            raise SpecError(f"{path.name}: `{key}` is missing")
    names = peers_of(d["layout"])
    table = d.get("peer", {})
    unknown = set(table) - set(names) - {"all"}
    if unknown:
        raise SpecError(f"{path.name}: peers {sorted(unknown)} are not in layout {d['layout']} ({names})")
    # [peer.all] is every client (H, D, E, F); the server peer S takes only its own [peer.S].
    clients_common = table.get("all", {})
    peers: dict[str, PeerSpec] = {}
    for n in names:
        own = table.get(n, {})
        common = clients_common if n != "S" else {}
        if n == "S" and own.get("script"):
            raise SpecError(f"{path.name}: the server peer S runs no case script")
        script = own.get("script", common.get("script", "")) if n != "S" else ""
        if n != "S" and not script.strip():
            raise SpecError(f"{path.name}: peer {n} has no script")
        if script and not re.search(r"(?m)^\s*quit\s*$", script):
            raise SpecError(f"{path.name}: peer {n}'s script does not end the game with `quit`")
        if ";" in script:
            raise SpecError(f"{path.name}: peer {n}'s script has a ';' (the game splits lines there, comments"
                            " included): one command per line")
        for t in (own, common):
            if not isinstance(t.get("expect_count", {}), dict) or not isinstance(t.get("expect_order", []), list):
                raise SpecError(f"{path.name}: peer {n}: expect_count is a table (pattern = count), expect_order a"
                                " list of patterns")
        peers[n] = PeerSpec(
            name=n,
            script=script,
            args=list(common.get("args", [])) + list(own.get("args", [])),
            expect=list(common.get("expect", [])) + list(own.get("expect", [])),
            forbid=list(common.get("forbid", [])) + list(own.get("forbid", [])),
            expect_count={**common.get("expect_count", {}), **own.get("expect_count", {})},
            expect_order=list(own.get("expect_order", common.get("expect_order", []))),
        )
        for rx, count in peers[n].expect_count.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise SpecError(f"{path.name}: peer {n}: expect_count /{rx}/ must be a whole number >= 0")
        for rx in peers[n].expect + peers[n].forbid + list(peers[n].expect_count) + peers[n].expect_order:
            try:
                re.compile(rx)
            except re.error as e:
                raise SpecError(f"{path.name}: peer {n}: bad pattern {rx!r}: {e}") from e
    server = d.get("server", "standin")
    if server not in ("standin", "host"):
        raise SpecError(f"{path.name}: server = {server!r} (standin or host)")
    if server == "host" and d["layout"] == "solo":
        raise SpecError(f"{path.name}: server = \"host\" needs a server+N layout")
    if server == "host" and "S" in table and table["S"].get("args"):
        raise SpecError(f"{path.name}: a hosted server gets its arguments from the owner's game, not the spec")
    grades = d.get("grade", {})
    for gname, g in grades.items():
        if gname not in GRADES:
            raise SpecError(f"{path.name}: grade {gname} is not one of {', '.join(GRADES)}")
        if gname == "save_copy":
            check_save_copy(path.name, g, names, server)
            continue
        if not isinstance(g, dict) or g.get("peer", "S" if gname == "server_frame_time" else "H") not in names:
            raise SpecError(f"{path.name}: grade {gname} names a peer not in {names}")
        if gname == "level_load" and (g.get("measure") not in d.get("measure", {}) or not g.get("baseline")):
            raise SpecError(f"{path.name}: grade level_load needs `measure` (one of the spec's) and `baseline`")
    measures = d.get("measure", {})
    for mname, m in measures.items():
        if not isinstance(m, dict) or "to" not in m:
            raise SpecError(f"{path.name}: measure {mname} needs `to` (and `from`, or it counts from the start)")
        if m.get("peer", "H") not in names:
            raise SpecError(f"{path.name}: measure {mname} is for peer {m.get('peer', 'H')}, not in {names}")
    compare = d.get("compare", {})
    if "compare" in d:
        check_compare(path.name, compare, names, server)
    return Spec(
        name=d["name"], path=path, layout=d["layout"], stage=str(d["stage"]),
        description=d.get("description", ""), timeout=float(d.get("timeout", default_timeout)),
        peers=peers, measures=measures, standin_seconds=float(d.get("standin_seconds", 45)),
        server=server, grades=grades, compare=compare,
    )


def save_copy_peers(g: dict, names: list[str]) -> list[str]:
    """The peers a save_copy grade checks: its `peers`, or every client."""
    return list(g.get("peers", [n for n in names if n != "S"]))


def check_save_copy(spec_name: str, g: object, names: list[str], server: str) -> None:
    if not isinstance(g, dict):
        raise SpecError(f"{spec_name}: grade save_copy must be a table")
    peers = g.get("peers", [n for n in names if n != "S"])
    if not isinstance(peers, list) or not peers or any(p not in names for p in peers):
        raise SpecError(f"{spec_name}: grade save_copy: `peers` must name peers of {names}")
    if server == "host" and "S" in peers:
        raise SpecError(f"{spec_name}: grade save_copy: a hosted server has no data folder of its own (its saves"
                        " are in the owner's session folder)")
    if "title" in g and not TITLE_ID.match(str(g["title"])):
        raise SpecError(f"{spec_name}: grade save_copy: `title` is a title id, eight hex digits")


def check_compare(spec_name: str, c: object, names: list[str], server: str) -> None:
    """A case's [compare]: two or more named runs of its workload ([[compare.run]], each with extra
    per-peer args, expect and forbid), which of them are the A/A (`noise`, two or more) and which are
    compared against it (`test`), the evidence a run must show to count as reached (`reached`, peer =
    patterns), and the compared peer (`peer`, default S)."""
    def bad(why: str) -> SpecError:
        return SpecError(f"{spec_name}: compare: {why}")

    if not isinstance(c, dict):
        raise bad("must be a table")
    if unknown := set(c) - COMPARE_KEYS:
        raise bad(f"unknown keys {sorted(unknown)} ({', '.join(sorted(COMPARE_KEYS))})")
    if c.get("peer", "S") not in names:
        raise bad(f"peer {c.get('peer', 'S')} is not in {names}")
    runs = c.get("run", [])
    if not isinstance(runs, list) or len(runs) < 2 or not all(isinstance(r, dict) for r in runs):
        raise bad("needs two or more [[compare.run]] tables")
    run_names = []
    for r in runs:
        name = str(r.get("name", ""))
        if not RUN_NAME.match(name) or name in run_names:
            raise bad(f"run name {name!r} must be unique and only letters, digits, - and _")
        run_names.append(name)
        if unknown := set(r) - {"name", "peer", "description"}:
            raise bad(f"run {name}: unknown keys {sorted(unknown)} (name, peer, description)")
        if not isinstance(r.get("peer", {}), dict):
            raise bad(f"run {name}: `peer` is a table of peers")
        for peer, extra in r.get("peer", {}).items():
            if peer not in names:
                raise bad(f"run {name}: peer {peer} is not in {names}")
            if not isinstance(extra, dict) or set(extra) - {"args", "expect", "forbid"}:
                raise bad(f"run {name}: peer {peer} takes only args, expect and forbid")
            if server == "host" and peer == "S" and extra.get("args"):
                raise bad(f"run {name}: a hosted server gets its arguments from the owner's game, not the spec")
            for key in ("args", "expect", "forbid"):
                if not isinstance(extra.get(key, []), list) or not all(isinstance(x, str) for x in extra.get(key, [])):
                    raise bad(f"run {name}: peer {peer}: {key} must be a list of strings")
            for rx in extra.get("expect", []) + extra.get("forbid", []):
                check_pattern(spec_name, rx)
    noise, test = c.get("noise", []), c.get("test", [])
    if not isinstance(noise, list) or len(noise) < 2 or any(n not in run_names for n in noise):
        raise bad(f"`noise` names two or more of the runs {run_names} (the A/A)")
    if not isinstance(test, list) or any(n not in run_names or n in noise for n in test):
        raise bad("`test` names runs that are not in `noise`")
    if missing := [n for n in run_names if n not in noise and n not in test]:
        raise bad(f"runs {missing} are neither in `noise` nor in `test`")
    reached = c.get("reached", {})
    if not isinstance(reached, dict) or not reached or any(
            p not in names or not isinstance(v, list) or not v for p, v in reached.items()):
        raise bad("`reached` maps peers to the patterns a run must show to count as reached")
    for rx in [x for v in reached.values() for x in v] + [c.get("window", DEFAULT_WINDOW)] + \
            ([c["window_end"]] if "window_end" in c else []):
        check_pattern(spec_name, rx)
    floor = c.get("load_floor_s", 0.0)
    if not isinstance(floor, (int, float)) or isinstance(floor, bool) or floor < 0:
        raise bad("`load_floor_s` is a number of seconds >= 0")
    if not isinstance(c.get("memexport_zero", True), bool):
        raise bad("`memexport_zero` is true or false")


def check_pattern(spec_name: str, rx: object) -> None:
    try:
        re.compile(str(rx))
    except re.error as e:
        raise SpecError(f"{spec_name}: bad pattern {rx!r}: {e}") from e


def all_specs(cfg: Config) -> list[Spec]:
    if not cfg.cases_dir.is_dir():
        return []
    return [load_spec(p, cfg.timeout) for p in sorted(cfg.cases_dir.glob("*.toml"))]


def load_manifest(cfg: Config) -> dict:
    if not cfg.manifest.is_file():
        return {"case": []}
    return tomllib.loads(cfg.manifest.read_text(encoding="utf-8"))


# --- snapshot and fixtures ------------------------------------------------------------------------


def fresh_dir(path: Path) -> Path:
    """path, or path-2, path-3 ... if a run in the same second already took it."""
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}-{n}")
    return candidate


def check_disc(cfg: Config) -> None:
    if not (cfg.rom / "default.xex").is_file():
        raise SystemExit(f"no disc at {cfg.rom} (rom/default.xex): a worktree links the main checkout's disc with"
                         " `just worktree-setup`")


def snapshot_build(cfg: Config) -> Path:
    """Copy the exe and DLLs into the store once per build, so a rebuild during a run changes
    nothing. Runs never start the build folder's exe."""
    exe = cfg.build / cfg.exe
    if not exe.is_file():
        raise SystemExit(f"no build at {exe}: build the release first (`just build-release`)")
    files = [exe] + sorted(cfg.build.glob("*.dll"))
    h = hashlib.sha256()
    for f in files:
        st = f.stat()
        h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    snap = cfg.store / "snapshots" / h.hexdigest()[:12]
    check_output_path(cfg, snap)
    if not (snap / cfg.exe).is_file():
        tmp = snap.with_name(snap.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for f in files:
            shutil.copy2(f, tmp / f.name)
        tmp.rename(snap)
    return snap


def fixture_dirs(cfg: Config) -> tuple[Path, Path]:
    return cfg.store / "store" / "saves", cfg.store / "store" / "figures"


def cmd_fixtures(cfg: Config, a: argparse.Namespace) -> int:
    saves, figures = fixture_dirs(cfg)
    if not a.from_owner:
        print(f"fixtures: saves {'present' if saves.is_dir() else 'missing'} ({saves}), figures "
              f"{'present' if figures.is_dir() else 'missing'} ({figures})")
        print("`fixtures --from-owner` copies the owner's saves and figures in (read only on their side)")
        return 0
    for src, dst in ((cfg.owner_saves, saves), (cfg.owner_figures, figures)):
        if not src.is_dir():
            raise SystemExit(f"no {src} to copy from")
        check_output_path(cfg, dst)
        if dst.exists():
            print(f"{dst}: already there, kept (delete it by hand to take a newer copy)")
            continue
        # Only the saves themselves: not the shader cache, the owner's achievements, backups, logs, or
        # files other builds left there (an earlier online build's reward ledger).
        ignore = shutil.ignore_patterns("cache", "achievements", "save-backup", "*.log", "online-*.txt")
        shutil.copytree(src, dst, ignore=ignore)
        print(f"copied {src} -> {dst}")
    return 0


# --- running a case -------------------------------------------------------------------------------


@dataclass
class PeerResult:
    peer: str
    exit_code: int | None = None
    ended_by_runner: bool = False
    seconds: float = 0.0
    status: str = "not run"
    reasons: list[str] = field(default_factory=list)
    build: str = ""
    levels: list[str] = field(default_factory=list)
    fps: list[float] = field(default_factory=list)
    longest_frame_ms: list[int] = field(default_factory=list)
    peak_private_mb: int = 0
    peak_working_set_mb: int = 0
    errors: int = 0
    measures: dict[str, float | None] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    started_after_s: float = 0.0
    frame_p95_ms: list[float] = field(default_factory=list)  # the frame-time event's windows
    peak_video_mb: int = 0
    graded: dict[str, dict] = field(default_factory=dict)
    accepted: list[str] = field(default_factory=list)  # grade failures the manifest accepts


@dataclass
class CaseResult:
    case: str
    layout: str
    stage: str
    status: str = "not run"
    reasons: list[str] = field(default_factory=list)
    folder: str = ""
    started: str = ""
    seconds: float = 0.0
    peers: list[PeerResult] = field(default_factory=list)
    leftover_processes: int = 0
    compare: dict = field(default_factory=dict)  # a [compare] case's comparison (compare_runs)


def peer_script(spec: Spec, peer: str, figures: Path, join: str = "") -> str:
    if peer == "S":
        return STANDIN_SERVER_SCRIPT.format(seconds=int(spec.standin_seconds))
    return spec.peers[peer].script.replace("{figures}", str(figures)).replace("{peer}", peer).replace("{join}", join)


def free_loopback_port() -> int:
    """A port nothing listens on now, for a hosted server (its owner passes it on)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare_peer(cfg: Config, spec: Spec, peer: str, run: Path, port: int = 0) -> tuple[Path, list[str]]:
    d = run / peer
    check_output_path(cfg, d)
    saves, figures = fixture_dirs(cfg)
    if not saves.is_dir() or not figures.is_dir():
        raise SpecError(f"missing fixtures ({saves}, {figures}): run `skytest.py fixtures --from-owner`")
    shutil.copytree(saves, d / "data")
    shutil.copytree(figures, d / "figures")
    (d / "cache").mkdir()
    shutil.copy2(cfg.settings_template, d / "settings.toml")
    script = d / "script.txt"
    script.write_text(peer_script(spec, peer, d / "figures", f"127.0.0.1:{port}" if port else ""), encoding="utf-8")
    args = [
        f"--user_data_root={d / 'data'}",
        f"--cache_root={d / 'cache'}",
        f"--config_file={d / 'settings.toml'}",
        f"--log_file={d / 'game.log'}",
        f"--portal_figures_dir={d / 'figures'}",
        f"--game_data_root={cfg.rom.resolve()}",
        f"--input_script={script}",
        *cfg.default_args,
        *(["--window_hidden=true"] if peer == "S" else []),
        *([f"--online_port={port}"] if port and peer == "H" else []),
        *spec.peers[peer].args,
    ]
    check_args(args)
    return d, args


def parse_log(text: str) -> dict:
    out = {"levels": [], "fps": [], "longest": [], "private": 0, "working": 0, "errors": 0, "build": "",
           "lines": [], "frame_p95": [], "video": 0}
    for line in text.splitlines():
        m = TS.match(line)
        t = time.mktime(time.strptime(m.group(1)[:19], "%Y-%m-%d %H:%M:%S")) + float("0" + m.group(1)[19:]) \
            if m else None
        out["lines"].append((t, line))
        if "[error]" in line:
            out["errors"] += 1
        if (b := BUILD.search(line)) and not out["build"]:
            out["build"] = b.group(1)
        if w := WHERE.search(line):
            if not out["levels"] or out["levels"][-1] != w.group(1):
                out["levels"].append(w.group(1))
        if f := FPS.search(line):
            out["fps"].append(float(f.group(1)))
            out["longest"].append(int(f.group(4)))
        if mem := MEMORY.search(line):
            out["private"] = max(out["private"], int(mem.group(1)))
            out["working"] = max(out["working"], int(mem.group(2)))
        if fr := FRAMES.search(line):
            out["frame_p95"].append(float(fr.group(4)))
            out["private"] = max(out["private"], int(fr.group(6)))
            out["working"] = max(out["working"], int(fr.group(7)))
            out["video"] = max(out["video"], int(fr.group(8)))
    return out


def lines_after(lines: list[tuple[float | None, str]], after: str | None) -> list[str]:
    """The log lines after the first one matching `after` (all of them without it)."""
    if not after:
        return [line for _, line in lines]
    rx = re.compile(after)
    for i, (_, line) in enumerate(lines):
        if rx.search(line):
            return [l for _, l in lines[i + 1:]]
    return []


def frame_rate_limit(args: list[str], settings: Path) -> float:
    """A peer's own frame limit: its --frame_rate_limit, else its settings file's, else the game's
    default (60). 0 (no cap) counts as 60, the game's pace at present_interval 1."""
    value = None
    for a in args:
        if a.startswith("--frame_rate_limit="):
            value = a.split("=", 1)[1]
    if value is None and settings.is_file():
        try:
            for table in tomllib.loads(settings.read_text(encoding="utf-8")).values():
                if isinstance(table, dict) and "frame_rate_limit" in table:
                    value = table["frame_rate_limit"]
        except tomllib.TOMLDecodeError:
            pass
    try:
        limit = float(value) if value is not None else 60.0
    except (TypeError, ValueError):
        limit = 60.0
    return limit if limit > 0 else 60.0


def baseline_value(baseline: dict, dotted: str) -> float | None:
    """`solo.hub.hub_load_seconds` in tests/baseline.toml."""
    node = baseline
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return float(node) if isinstance(node, (int, float)) else None


def grade_thresholds(spec: Spec, r: PeerResult, parsed: dict, thresholds: dict, settings_limit: float,
                     baseline: dict | None = None) -> list[str]:
    """Plan 5.5 against tests/thresholds.toml, for the grades the spec names for this peer."""
    reasons = []
    for gname, g in spec.grades.items():
        if g.get("peer", "S" if gname == "server_frame_time" else "H") != r.peer:
            continue
        after = lines_after(parsed["lines"], g.get("after"))
        if gname == "server_frame_time":
            limit = float(thresholds.get("server_frame_time", {}).get("p95_ms", 33.0))
            p95s = [float(m.group(4)) for line in after if (m := FRAMES.search(line)) and int(m.group(1)) > 0]
            worst = max(p95s) if p95s else None
            r.graded[gname] = {"value_ms": worst, "limit_ms": limit, "windows": len(p95s)}
            if worst is None:
                reasons.append(f"grade {gname}: no frame-time window after /{g.get('after', '')}/")
            elif worst > limit:
                reasons.append(f"grade {gname}: p95 {worst:.1f} ms over {limit:.0f} ms")
                r.graded[gname]["violation"] = True
        elif gname == "client_fps":
            fraction = float(thresholds.get("client_fps", {}).get("floor_fraction_of_limit", 0.7))
            floor = fraction * settings_limit
            fpss = [float(m.group(1)) for line in after if (m := FPS.search(line))]
            worst = min(fpss) if fpss else None
            r.graded[gname] = {"value_fps": worst, "floor_fps": round(floor, 1), "windows": len(fpss)}
            if worst is None:
                reasons.append(f"grade {gname}: no fps window after /{g.get('after', '')}/")
            elif worst < floor:
                reasons.append(f"grade {gname}: {worst:.1f} FPS under the floor {floor:.1f}")
                r.graded[gname]["violation"] = True
        elif gname == "level_load":
            ratio_max = float(thresholds.get("level_load", {}).get("max_ratio_to_solo", 1.5))
            solo = baseline_value(baseline or {}, g["baseline"])
            value = r.measures.get(g["measure"])
            ratio = round(value / solo, 2) if value is not None and solo else None
            r.graded[gname] = {"value_s": value, "solo_s": solo, "ratio": ratio, "max_ratio": ratio_max}
            if solo is None:
                reasons.append(f"grade {gname}: no {g['baseline']} in the baseline")
            elif ratio is None:
                reasons.append(f"grade {gname}: measure {g['measure']} has no value")
            elif ratio > ratio_max:
                reasons.append(f"grade {gname}: {value:.1f} s is {ratio:.2f}x solo ({solo:.1f} s), over {ratio_max}x")
                r.graded[gname]["violation"] = True
    return reasons


def save_folders(data: Path, title: str) -> list[Path]:
    """The game's save folders in a peer's data folder: each profile folder's title folder
    (<data>/<profile>/<title>). Nothing else in the data folder counts: the session folder of a hosted
    server (Online), the SDK's save-backup, the shader cache."""
    if not title or not data.is_dir():
        return []
    return sorted(p / title for p in data.iterdir() if p.is_dir() and not p.is_symlink() and (p / title).is_dir())


def save_state(data: Path, title: str) -> dict[str, str]:
    """Every file in the peer's save folders: its path relative to the data folder and the SHA-256 of
    its bytes. Links are never followed."""
    out: dict[str, str] = {}
    for folder in save_folders(data, title):
        for d, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
            for f in files:
                full = Path(d) / f
                if full.is_symlink():
                    continue
                h = hashlib.sha256()
                try:
                    with open(full, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                except OSError as e:
                    h.update(f"unreadable: {e}".encode())
                out[full.relative_to(data).as_posix()] = h.hexdigest()
    return out


def grade_save_copy(r: PeerResult, before: dict[str, str], after: dict[str, str], title: str) -> list[str]:
    """The save_copy grade (stage 1B, item 2): a client's save copy is byte-equal after the run (plan
    decision 13: a session never writes a client's save). Any file changed, added or removed fails."""
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    r.graded["save_copy"] = {"title": title, "files": len(before), "changed": changed, "added": added,
                             "removed": removed}
    if not title:
        return ["grade save_copy: no title id (the grade's `title` or tests/skytest.toml [game] title_id)"]
    if not before:
        return [f"grade save_copy: no save in <data>/<profile>/{title} before the run (nothing to compare)"]
    if changed or added or removed:
        r.graded["save_copy"]["violation"] = True
        parts = [f"{what} {', '.join(files[:3])}{' ...' if len(files) > 3 else ''}"
                 for what, files in (("changed", changed), ("added", added), ("removed", removed)) if files]
        return [f"grade save_copy: the save copy changed: {'; '.join(parts)}"]
    return []


def read_peer_log(run: Path, peer: str, hosted: bool) -> str:
    """A peer's log in a run folder: a hosted server's is in its owner's session folder."""
    log = run / "H" / HOSTED_SERVER_LOG if hosted and peer == "S" else run / peer / "game.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


def measure(lines: list[tuple[float | None, str]], start: str | None, end: str) -> float | None:
    """Seconds from the first line matching `start` (or the first line) to the next matching `end`."""
    t0 = None
    i = 0
    if start is None:
        for i, (t, _) in enumerate(lines):
            if t is not None:
                t0 = t
                break
    else:
        rx = re.compile(start)
        for i, (t, line) in enumerate(lines):
            if t is not None and rx.search(line):
                t0 = t
                break
    if t0 is None:
        return None
    rx = re.compile(end)
    for t, line in lines[i + 1:]:
        if t is not None and rx.search(line):
            return round(t - t0, 3)
    return None


def check_count_and_order(text: str, ps: PeerSpec) -> list[str]:
    """expect_count: the number of log lines matching each pattern; expect_order: each pattern's
    first match after the previous pattern's (so `[a, b, a]` wants an a, a later b, a later a)."""
    reasons = []
    lines = text.splitlines()
    for rx, want in ps.expect_count.items():
        got = sum(1 for line in lines if re.search(rx, line))
        if got != want:
            reasons.append(f"log has /{rx}/ {got} time(s), expected {want}")
    i, prev = 0, None
    for rx in ps.expect_order:
        at = next((k for k in range(i, len(lines)) if re.search(rx, lines[k])), None)
        if at is None:
            reasons.append(f"log lacks /{rx}/" + (f" after /{prev}/" if prev else "") + " (expect_order)")
            break
        i, prev = at + 1, rx
    return reasons


def grade_peer(spec: Spec, r: PeerResult, text: str, parsed: dict) -> None:
    reasons = []
    if r.ended_by_runner:
        reasons.append(f"did not quit within {spec.timeout:.0f} s; ended by the runner")
    elif r.exit_code is not None and r.exit_code != 0:  # None: a hosted server, its owner's child
        reasons.append(f"exit code {r.exit_code}")
    if not text:
        reasons.append("no log written")
    ps = spec.peers.get(r.peer)
    expect = ps.expect if ps else []
    forbid = (ps.forbid if ps else []) + ALWAYS_FORBIDDEN
    for rx in expect:
        if not re.search(rx, text, re.M):
            reasons.append(f"log lacks /{rx}/")
    reasons += check_count_and_order(text, ps) if ps else []
    if any(f == 0.0 for f in parsed["fps"]):
        reasons.append("presented no frame during an fps window (frozen, or not booted yet)")
    for rx in forbid:
        if m := re.search(rx, text, re.M):
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            whole = text[start:end if end != -1 else len(text)].strip()
            reasons.append(f"log has forbidden /{rx}/: {whole[:160]}")
    for name, m in spec.measures.items():
        if m.get("peer", "H") == r.peer:
            r.measures[name] = measure(parsed["lines"], m.get("from"), m["to"])
            if r.measures[name] is None:
                reasons.append(f"measure {name}: its lines were not found")
    r.reasons = reasons
    r.status = "pass" if not reasons else "fail"


def run_case(cfg: Config, spec: Spec, snap: Path, max_games: int, stamp: str,
             thresholds: dict | None = None, baseline: dict | None = None,
             accepted_grades: tuple[str, ...] = (), folder: Path | None = None) -> CaseResult:
    """One run of a case. `folder`: where the run goes (a compared case's run, inside the case's
    folder); by default a new `<stamp>-<case>` folder in the store's runs."""
    names = list(spec.peers)
    hosted = spec.server == "host"
    # A hosted server is started by its owner H, not by the runner (it is still one of the games).
    starts = [n for n in names if not (hosted and n == "S")]
    res = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, started=stamp)
    if len(names) > max_games:
        res.status = "capacity"
        res.reasons = [f"needs {len(names)} games at once, this run allows {max_games} (--max-games)"]
        return res
    run = fresh_dir(folder if folder is not None else cfg.store / "runs" / f"{stamp}-{spec.name}")
    check_output_path(cfg, run)
    run.mkdir(parents=True)
    res.folder = str(run)
    exe = snap / cfg.exe
    t_start = time.monotonic()
    procs: dict[str, subprocess.Popen] = {}
    results = {n: PeerResult(peer=n) for n in names}
    save_grade = spec.grades.get("save_copy")
    save_title = str(save_grade.get("title", cfg.title_id)) if save_grade is not None else ""
    saves_before: dict[str, dict[str, str]] = {}
    try:
        # Every peer's folder first (copies take a moment), then all games start close together, and
        # a spec error in a later peer starts no game at all.
        port = free_loopback_port() if hosted else 0
        prepared = {n: prepare_peer(cfg, spec, n, run, port) for n in starts}
        # save_copy: each named peer's save copy as it is before any game starts.
        if save_grade is not None:
            saves_before = {n: save_state(run / n / "data", save_title) for n in save_copy_peers(save_grade, names)}
        started: dict[str, float] = {}
        ended: dict[str, float] = {}  # each game's own end, for its time

        def note_ends() -> None:
            for name, proc in procs.items():
                if name not in ended and proc.poll() is not None:
                    ended[name] = time.monotonic()

        with winjob.Job() as job:
            for i, n in enumerate(starts):
                if i and cfg.start_gap > 0:
                    # Games booting in the same instant crashed and froze each other in a test
                    # (stage 0, five at once); real sessions never start them together either.
                    # The games already started are watched meanwhile.
                    gap_end = time.monotonic() + cfg.start_gap
                    while time.monotonic() < gap_end:
                        note_ends()
                        time.sleep(min(0.2, max(0.0, gap_end - time.monotonic())))
                d, args = prepared[n]
                cmd = [*cfg.launcher, *args] if cfg.launcher else [str(exe), *args]
                results[n].command = cmd
                # The game starts suspended, joins the job, and only then runs: it never runs outside
                # the job. If any step fails it is ended before the error goes on.
                with open(d / "stderr.txt", "wb") as err:
                    p = subprocess.Popen(cmd, cwd=str(d), stdout=subprocess.DEVNULL, stderr=err,
                                         stdin=subprocess.DEVNULL, creationflags=winjob.SUSPENDED_START)
                try:
                    job.add(p)
                    winjob.resume(p)
                except BaseException:
                    p.kill()
                    p.wait(timeout=30)
                    raise
                procs[n] = p
                started[n] = time.monotonic()
                results[n].started_after_s = round(started[n] - t_start, 1)
            deadline = time.monotonic() + spec.timeout
            while len(ended) < len(procs) and time.monotonic() < deadline:
                note_ends()
                time.sleep(0.5)
            for n, p in procs.items():
                if p.poll() is None:
                    p.kill()
                    results[n].ended_by_runner = True
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                results[n].exit_code = p.returncode
                results[n].seconds = round(ended.get(n, time.monotonic()) - started[n], 1)
            res.leftover_processes = job.active()
            if res.leftover_processes:
                job.terminate()
    except (SpecError, OSError) as e:
        # Every game started so far went with the job when it closed.
        res.status, res.reasons = "error", [str(e)]
        return res
    saves_after = {n: save_state(run / n / "data", save_title) for n in saves_before}
    res.seconds = round(time.monotonic() - t_start, 1)
    for n in names:
        text = read_peer_log(run, n, hosted)
        parsed = parse_log(text)
        r = results[n]
        r.build, r.levels, r.fps, r.longest_frame_ms = parsed["build"], parsed["levels"], parsed["fps"], parsed["longest"]
        r.peak_private_mb, r.peak_working_set_mb, r.errors = parsed["private"], parsed["working"], parsed["errors"]
        r.frame_p95_ms, r.peak_video_mb = parsed["frame_p95"], parsed["video"]
        grade_peer(spec, r, text, parsed)
        if spec.grades:
            limit = frame_rate_limit(r.command, run / n / "settings.toml")
            extra = grade_thresholds(spec, r, parsed, thresholds or {}, limit, baseline)
            if n in saves_before:
                extra += grade_save_copy(r, saves_before[n], saves_after[n], save_title)
            # A grade the manifest accepts for this case (plan 5.4: accepted failures with a reason)
            # is recorded, not failed on; everything else still has to pass.
            # Only a measured violation is accepted: a missing measurement or baseline still fails.
            def acceptable(why: str) -> bool:
                gname = why.split(":", 1)[0].removeprefix("grade ")
                return gname in accepted_grades and bool(r.graded.get(gname, {}).get("violation"))

            r.accepted = [why for why in extra if acceptable(why)]
            extra = [why for why in extra if why not in r.accepted]
            if extra:
                r.reasons += extra
                r.status = "fail"
    res.peers = [results[n] for n in names]
    failed = [r.peer for r in res.peers if r.status != "pass"]
    if res.leftover_processes:
        res.reasons.append(f"{res.leftover_processes} game process(es) still running after the case; ended")
    if failed:
        res.reasons.append(f"peers failed: {', '.join(failed)}")
    res.status = "pass" if not failed and not res.leftover_processes else "fail"
    return res


# --- comparing runs (a case's [compare]; the `compare` command) -------------------------------------
#
# Stage 1B item 3 (docs/online-1B.md, P3): the draw skip is trusted when the game's events do not
# change with it. The same workload runs several times: two or more runs make the A/A (`noise`,
# the skip off twice), and each `test` run (the skip on) is compared against them. Graded, from the
# compared peer's log (the server S):
#   - the barrier and level lines: equal counts and order in every run, the A/A included (they are
#     the game's flow and do not depend on timing);
#   - the world-message counts in the window (from the last barrier release to `window_end` or the
#     log's end: the owner at the spawn): a key whose A/A counts are equal must be equal in a test
#     run; a key whose A/A counts differ is noisy and may differ by the A/A spread beyond the A/A
#     range. Exact from two `Trace counts` dumps after the window starts, else sampled from the
#     trace lines' running numbers (#n, the first 20 and every 500th: the highest seen);
#   - the load times: a test run no slower than the slowest A/A run plus the A/A spread (at least
#     `load_floor_s`); faster passes (the skip saves GPU work);
#   - the GPU counters: 0 resolve readbacks in every run, and 0 memexport draws (draw_skip.h: the
#     skip is trusted only while memexport is 0) unless `memexport_zero = false`.
# A run without its `reached` evidence makes the comparison "not reached": a failure, never a pass.


def is_world_message_key(key: str) -> bool:
    """A trace_world_messages count key (traces.cpp: "message <class>", "telegram <class>",
    "trigger <event> <trigger>"); trace_telegrams' "telegram send ..." and "telegram list ..." are not."""
    if key.startswith(("telegram send ", "telegram list ")):
        return False
    return key.startswith(("message ", "telegram ", "trigger "))


def trace_dumps(texts: list[str]) -> list[tuple[int, dict[str, int]]]:
    """Each `Trace counts: <k> keys` block: its line index and every `Trace count: <key> <n>` after it."""
    dumps: list[tuple[int, dict[str, int]]] = []
    for i, line in enumerate(texts):
        if TRACE_COUNTS.search(line):
            dumps.append((i, {}))
        elif dumps and (m := TRACE_COUNT.search(line)):
            dumps[-1][1][m.group(1)] = int(m.group(2))
    return dumps


def compare_keys(text: str, c: dict) -> dict:
    """What one run's log (the compared peer's) says: the barrier and level lines in order, the
    world-message counts in the window, the load times and the GPU counter totals."""
    lines = parse_log(text)["lines"]
    texts = [line for _, line in lines]
    seq: dict[str, list[str]] = {"session": [], "level_watch": []}
    for line in texts:
        for rx, form in SESSION_SEQUENCE:
            if m := rx.search(line):
                seq["session"].append(form.format(*(g or "" for g in m.groups())))
                break
        if m := LEVEL_WATCH_STEP.search(line):
            req = LEVEL_WATCH_REQUEST.search(line, m.end())
            seq["level_watch"].append(f"{m.group(1)}: {m.group(2)}" + (f" (request {req.group(1)})" if req else ""))

    loads: dict[str, float] = {}
    t_first = next((t for t, _ in lines if t is not None), None)
    for i, (t, line) in enumerate(lines):
        if t is None:
            continue
        if SERVER_HUB.search(line) and t_first is not None and "start -> hub" not in loads:
            loads["start -> hub"] = round(t - t_first, 3)
        if m := LEVEL_OPEN.search(line):
            g, zone = m.groups()
            up, bar = f"level {g} {zone}: open -> server up", f"level {g} {zone}: open -> barrier"
            for t2, later in lines[i + 1:]:
                if t2 is None:
                    continue
                if up not in loads and (u := LEVEL_UP.search(later)) and u.group(1) == g:
                    loads[up] = round(t2 - t, 3)
                if bar not in loads and (b := BARRIER.search(later)) and b.group(1) == g:
                    loads[bar] = round(t2 - t, 3)
                if up in loads and bar in loads:
                    break
        if m := LEVEL_WATCH_LOAD.search(line):
            key, n = f"level watch {m.group(1)}: load call", 1
            while key in loads:
                n += 1
                key = f"level watch {m.group(1)}: load call #{n}"
            loads[key] = int(m.group(2)) / 1000.0

    messages: dict = {"mode": "none", "keys": {}, "window_s": None, "window": False}
    window = re.compile(c.get("window", DEFAULT_WINDOW))
    start = max((i for i, line in enumerate(texts) if window.search(line)), default=None)
    if start is not None:
        end = len(texts)
        if c.get("window_end"):
            end_rx = re.compile(c["window_end"])
            end = next((k for k in range(start + 1, len(texts)) if end_rx.search(texts[k])), len(texts))
        after = [d for d in trace_dumps(texts) if d[0] > start]
        keys: dict[str, int] = {}
        if len(after) >= 2:
            (i0, first), (i1, last) = after[0], after[-1]
            for k in set(first) | set(last):
                if is_world_message_key(k) and last.get(k, 0) - first.get(k, 0):
                    keys[k] = last.get(k, 0) - first.get(k, 0)
            mode, span = "exact", lines[i0:i1 + 1]
        else:
            for line in texts[start + 1:end]:
                if m := TRACE_MESSAGE.search(line):
                    k, n = f"message {m.group(1)}", int(m.group(2))
                elif m := TRACE_TELEGRAM.search(line):
                    k, n = f"telegram {m.group(1)}", int(m.group(2))
                elif m := TRACE_TRIGGER.search(line):
                    k, n = f"trigger {m.group(2)} {m.group(1)}", int(m.group(3))
                else:
                    continue
                keys[k] = max(keys.get(k, 0), n)
            mode, span = ("sampled" if keys else "none"), lines[start:end + 1]
        ts = [t for t, _ in span if t is not None]
        messages = {"mode": mode, "keys": keys, "window": True,
                    "window_s": round(ts[-1] - ts[0], 1) if len(ts) >= 2 else 0.0}

    gpu: dict = {n: 0 for n in GPU_COUNTER_NAMES}
    gpu.update(windows=0, seconds=0.0)
    for line in texts:
        if m := GPU_COUNTERS.search(line):
            for name, v in zip(GPU_COUNTER_NAMES, m.groups()[:9]):
                gpu[name] += int(v)
            gpu["windows"] += 1
            gpu["seconds"] = round(gpu["seconds"] + float(m.group(10)), 1)
    return {"sequences": seq, "loads": loads, "messages": messages, "gpu": gpu}


def gather_run(name: str, folder: Path, spec: Spec) -> dict:
    """One finished run of a [compare] case: its evidence (`reached`) and its compare keys."""
    c = spec.compare
    peer = c.get("peer", "S")
    hosted = spec.server == "host"
    texts = {p: read_peer_log(folder, p, hosted) for p in set(c["reached"]) | {peer}}
    missing = [f"{p} lacks /{rx}/" for p, pats in c["reached"].items() for rx in pats
               if not re.search(rx, texts[p], re.M)]
    return {"name": name, "folder": str(folder), "missing": missing, "keys": compare_keys(texts[peer], c)}


def sequence_difference(a: list[str], b: list[str]) -> str:
    """How two line sequences differ: the counts first, else the first place the order differs."""
    ca, cb = {}, {}
    for x in a:
        ca[x] = ca.get(x, 0) + 1
    for x in b:
        cb[x] = cb.get(x, 0) + 1
    counts = [f"'{k}' {ca.get(k, 0)} vs {cb.get(k, 0)}" for k in dict.fromkeys(a + b) if ca.get(k, 0) != cb.get(k, 0)]
    if counts:
        return "counts " + ", ".join(counts[:4]) + (f" and {len(counts) - 4} more" if len(counts) > 4 else "")
    i = next(k for k in range(len(a)) if a[k] != b[k])
    return f"order at line {i + 1}: '{a[i]}' vs '{b[i]}'"


def within_noise(noise: list[float], value: float, floor: float, slower_only: bool) -> tuple[bool, float]:
    """Whether `value` is within the A/A noise: the A/A range widened by its spread (at least `floor`)
    on both sides, or only above it (`slower_only`). Returns the verdict and the tolerance."""
    lo, hi = min(noise), max(noise)
    tol = max(hi - lo, floor)
    return value <= hi + tol and (slower_only or value >= lo - tol), round(tol, 3)


def compare_runs(runs: list[dict], c: dict) -> dict:
    """The comparison of gathered runs (gather_run) under a [compare] table's noise and test."""
    noise, test = list(c["noise"]), list(c.get("test", []))
    by = {r["name"]: r for r in runs}
    out: dict = {"status": "pass", "reasons": [], "peer": c.get("peer", "S"), "noise": noise, "test": test,
                 "runs": runs, "loads": {}, "messages": {}}
    unreached = [f"run {r['name']}: {', '.join(r['missing'])}" for r in runs if r["missing"]]
    not_run = [n for n in noise + test if n not in by]
    if unreached or not_run:
        out["status"] = "not reached"
        out["reasons"] = [f"compare: not reached: {w}" for w in unreached]
        if not_run:
            out["reasons"].append(f"compare: not reached: run(s) {', '.join(not_run)} not run")
        return out
    reasons: list[str] = []
    names = noise + test
    keys = {n: by[n]["keys"] for n in names}

    # The barrier and level lines: every run equal to the first A/A run.
    ref = noise[0]
    if not keys[ref]["sequences"]["session"]:
        reasons.append(f"compare: run {ref} has no barrier or level line")
    for fam, label in (("session", "barrier and level lines"), ("level_watch", "level watch lines")):
        for n in names[1:]:
            if keys[n]["sequences"][fam] != keys[ref]["sequences"][fam]:
                what = "the A/A runs disagree" if n in noise else "differ from the A/A"
                reasons.append(f"compare: {label}: {n} and {ref} {what}: "
                               f"{sequence_difference(keys[ref]['sequences'][fam], keys[n]['sequences'][fam])}")

    # The world-message counts in the window.
    modes = {n: keys[n]["messages"]["mode"] for n in names}
    if not all(keys[n]["messages"]["window"] for n in names):
        reasons.append(f"compare: no window start /{c.get('window', DEFAULT_WINDOW)}/ in run(s) "
                       f"{', '.join(n for n in names if not keys[n]['messages']['window'])}")
    elif len(set(modes.values())) > 1:
        reasons.append("compare: world messages: the runs' counts come from different sources ("
                       + ", ".join(f"{n} {m}" for n, m in modes.items()) + ")")
    elif set(modes.values()) == {"none"}:
        reasons.append("compare: world messages: no count in the window (trace_world_messages on the compared peer?)")
    else:
        table, bad = {}, []
        for k in sorted(set().union(*(keys[n]["messages"]["keys"] for n in names))):
            nv = [keys[n]["messages"]["keys"].get(k, 0) for n in noise]
            entry = {"noise": nv, "tolerance": max(nv) - min(nv), "test": {}, "ok": True}
            for n in test:
                v = keys[n]["messages"]["keys"].get(k, 0)
                entry["test"][n] = v
                if not within_noise(nv, v, 0.0, False)[0]:
                    entry["ok"] = False
                    bad.append(f"{k} {n} {v} (A/A {'/'.join(str(x) for x in nv)})")
            table[k] = entry
        out["messages"] = {"mode": next(iter(modes.values())), "keys": table,
                           "stable": sum(1 for e in table.values() if not e["tolerance"]),
                           "noisy": sum(1 for e in table.values() if e["tolerance"])}
        if bad:
            reasons.append(f"compare: world messages outside the A/A noise ({modes[ref]}): " + ", ".join(bad[:5])
                           + (f" and {len(bad) - 5} more" if len(bad) > 5 else ""))

    # The load times: a test run no slower than the A/A allows.
    floor = float(c.get("load_floor_s", 0.0))
    for k in sorted(set().union(*(keys[n]["loads"] for n in names))):
        lacking = [n for n in names if k not in keys[n]["loads"]]
        nv = [keys[n]["loads"][k] for n in noise if k in keys[n]["loads"]]
        entry: dict = {"noise": nv, "tolerance": None, "test": {n: keys[n]["loads"].get(k) for n in test}, "ok": True}
        out["loads"][k] = entry
        if lacking:
            entry["ok"] = False
            reasons.append(f"compare: load {k}: not in run(s) {', '.join(lacking)}")
            continue
        for n in test:
            ok, entry["tolerance"] = within_noise(nv, keys[n]["loads"][k], floor, True)
            if not ok:
                entry["ok"] = False
                reasons.append(f"compare: load {k}: {n} {keys[n]['loads'][k]:.3f} s, over the A/A "
                               f"{min(nv):.3f}-{max(nv):.3f} s by more than {entry['tolerance']:.3f} s")
        if not test:
            entry["tolerance"] = within_noise(nv, nv[0], floor, True)[1]

    # The GPU counters.
    for n in names:
        g = keys[n]["gpu"]
        if not g["windows"]:
            reasons.append(f"compare: run {n}: no `GPU: draws` line (gpu_draw_counters on the compared peer?)")
            continue
        if g["readbacks"]:
            reasons.append(f"compare: run {n}: {g['readbacks']} resolve readback(s), expected 0")
        if c.get("memexport_zero", True) and g["memexport"]:
            reasons.append(f"compare: run {n}: {g['memexport']} memexport draw(s); the draw skip is trusted only with 0")
    out["reasons"] = reasons
    out["status"] = "fail" if reasons else "pass"
    return out


def compare_markdown(cmp: dict) -> list[str]:
    """The comparison as report lines (report.md's case section and compare.md)."""
    noise, test = cmp["noise"], cmp["test"]

    def table(head: list[str], rows: list[list[str]]) -> list[str]:
        return (["", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                + ["| " + " | ".join(r) + " |" for r in rows])

    lines = [f"### Comparison of {cmp['peer']}'s logs: {cmp['status'].upper()}", "",
             f"A/A: {', '.join(noise)}" + (f"; compared with it: {', '.join(test)}" if test else "") + "."]
    if cmp["reasons"]:
        lines += [""] + [f"- {why}" for why in cmp["reasons"]]
    rows = []
    for r in cmp["runs"]:
        k, g, m = r["keys"], r["keys"]["gpu"], r["keys"]["messages"]
        rows.append([r["name"], "no: " + ", ".join(r["missing"]) if r["missing"] else "yes",
                     str(len(k["sequences"]["session"])), str(len(k["sequences"]["level_watch"])),
                     f"{len(m['keys'])} keys ({m['mode']})", "-" if m["window_s"] is None else f"{m['window_s']} s",
                     str(g["windows"]), str(g["draws"]), str(g["memexport"]), str(g["readbacks"]),
                     f"{g['skipped_draws']}/{g['skipped_copies']}", f"`{r['folder']}`"])
    lines += table(["Run", "Reached", "Barrier and level lines", "Level watch lines", "World messages", "Window",
                    "GPU windows", "Draws", "Memexport", "Readbacks", "Skipped draws/copies", "Folder"], rows)
    if cmp["loads"]:
        rows = [[key + ("" if e["ok"] else " (FAIL)"), " / ".join(f"{v:.3f}" for v in e["noise"]),
                 "-" if e["tolerance"] is None else f"{e['tolerance']:.3f}"]
                + ["-" if e["test"].get(n) is None else f"{e['test'][n]:.3f}" for n in test]
                for key, e in cmp["loads"].items()]
        lines += table(["Load (s)", f"A/A ({', '.join(noise)})", "Tolerance"] + test, rows)
    if cmp["messages"]:
        msgs = cmp["messages"]
        lines += ["", f"World messages ({msgs['mode']}): {len(msgs['keys'])} keys, {msgs['stable']} equal in the A/A, "
                      f"{msgs['noisy']} noisy (listed with any that failed)."]
        rows = [[k + ("" if e["ok"] else " (FAIL)"), " / ".join(str(v) for v in e["noise"])]
                + [str(e["test"][n]) for n in test]
                for k, e in msgs["keys"].items() if e["tolerance"] or not e["ok"]]
        if rows:
            lines += table(["Key", "A/A"] + test, rows)
    return lines


def write_compare(folder: Path, cmp: dict) -> None:
    (folder / "compare.json").write_text(json.dumps(cmp, indent=2), encoding="utf-8")
    (folder / "compare.md").write_text("\n".join(compare_markdown(cmp)) + "\n", encoding="utf-8")


def variant_spec(spec: Spec, run: dict) -> Spec:
    """The case as one [[compare.run]] runs it: each peer with the run's extra args, expect and forbid."""
    peers = {}
    for n, p in spec.peers.items():
        extra = run.get("peer", {}).get(n, {})
        peers[n] = replace(p, args=p.args + list(extra.get("args", [])), expect=p.expect + list(extra.get("expect", [])),
                           forbid=p.forbid + list(extra.get("forbid", [])))
    return replace(spec, name=f"{spec.name}-{run['name']}", peers=peers, compare={})


def run_compare_case(cfg: Config, spec: Spec, snap: Path, max_games: int, stamp: str,
                     thresholds: dict | None = None, baseline: dict | None = None,
                     accepted_grades: tuple[str, ...] = ()) -> CaseResult:
    """A [compare] case: its runs one after another, each in its own folder inside the case's folder
    (graded as a run of its own: expect, forbid, grades), then the comparison. A run that does not
    reach its evidence ends the case there: the comparison is "not reached", and the case fails."""
    res = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, started=stamp)
    if len(spec.peers) > max_games:
        res.status = "capacity"
        res.reasons = [f"needs {len(spec.peers)} games at once, this run allows {max_games} (--max-games)"]
        return res
    case_dir = fresh_dir(cfg.store / "runs" / f"{stamp}-{spec.name}")
    check_output_path(cfg, case_dir)
    case_dir.mkdir(parents=True)
    res.folder = str(case_dir)
    t_start = time.monotonic()
    gathered, failed = [], False
    for run in spec.compare["run"]:
        name = run["name"]
        sub = run_case(cfg, variant_spec(spec, run), snap, max_games, time.strftime("%Y%m%d-%H%M%S"), thresholds,
                       baseline, accepted_grades, folder=case_dir / name)
        res.leftover_processes += sub.leftover_processes
        for p in sub.peers:
            p.peer = f"{name}:{p.peer}"
            res.peers.append(p)
        res.reasons += [f"run {name}: {why}" for why in sub.reasons]
        if sub.status == "error":
            res.status, res.seconds = "error", round(time.monotonic() - t_start, 1)
            return res
        failed |= sub.status != "pass"
        gathered.append(gather_run(name, Path(sub.folder), spec))
        if gathered[-1]["missing"]:
            break
    res.compare = compare_runs(gathered, spec.compare)
    write_compare(case_dir, res.compare)
    res.reasons += res.compare["reasons"]
    res.seconds = round(time.monotonic() - t_start, 1)
    res.status = "pass" if not failed and res.compare["status"] == "pass" and not res.leftover_processes else "fail"
    return res


# --- reports --------------------------------------------------------------------------------------


def checkpoint_verdict(manifest: dict, stage: str, stages: list[str], results: list[CaseResult],
                       touched: list[str] | None = None, before: dict[str, str] | None = None) -> dict:
    """Plan 5.4: a checkpoint is met when every required case of its stage and the earlier ones ran
    and passed, and the run left the owner's data and the disc alone. A capacity result counts only
    where the manifest says so (capacity_ok). A grade the manifest accepts for a case
    (accepted_grades, with a reason) does not fail it; the line names it. A case of any kind that
    passed in the run before and fails now is open ("nothing that passed before fails")."""
    ran = {r.case: r for r in results}
    open_cases = [f"guard: {folder} changed" for folder in (touched or [])]
    for case_id, status in (before or {}).items():
        r = ran.get(case_id)
        if status == "pass" and r is not None and r.status in ("fail", "error"):
            open_cases.append(f"{case_id} (passed before, fails now)")
    accepted = sorted({f"{r.case}: {why}" for r in results for p in r.peers for why in p.accepted})
    for c in manifest.get("case", []):
        if c.get("kind") != "required" or str(c.get("stage")) not in stages:
            continue
        r = ran.get(c["id"])
        if not c.get("spec"):
            open_cases.append(f"{c['id']} (no spec)")
        elif r is None:
            open_cases.append(f"{c['id']} (not run)")
        elif r.status == "capacity" and c.get("capacity_ok"):
            continue
        elif r.status != "pass":
            open_cases.append(f"{c['id']} ({r.status})")
    open_cases = list(dict.fromkeys(open_cases))
    line = f"checkpoint {stage}: met" if not open_cases else f"checkpoint {stage}: NOT met: {', '.join(open_cases)}"
    if accepted:
        line += f" (accepted: {'; '.join(accepted)})"
    return {"stage": stage, "met": not open_cases, "open": open_cases, "accepted": accepted, "line": line}


def write_report(cfg: Config, out: Path, results: list[CaseResult], manifest: dict, thresholds: dict,
                 max_games: int, guard: list[dict], unwritten: list[str], checkpoint: dict | None = None) -> None:
    lines = [f"# skytest run {out.name}", "", f"At most {max_games} game(s) at once.", ""]
    lines += ["| Case | Layout | Stage | Status | Time | Why |", "|---|---|---|---|---|---|"]
    for r in results:
        why = "; ".join(r.reasons) or ""
        lines.append(f"| {r.case} | {r.layout} | {r.stage} | {r.status.upper()} | {r.seconds:.0f} s | {why} |")
    for r in results:
        if not r.peers:
            continue
        lines += ["", f"## {r.case} ({r.layout})", "", f"Folder: `{r.folder}`", "",
                  "| Peer | Status | Exit | Time | Levels | FPS | Longest frame | Peak private MB | Errors | Measures |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for p in r.peers:
            ms = ", ".join(f"{k} {v} s" if v is not None else f"{k} -" for k, v in p.measures.items())
            fps = ", ".join(f"{v:.1f}" for v in p.fps)
            lf = ", ".join(str(v) for v in p.longest_frame_ms)
            exit_ = "ended" if p.ended_by_runner else str(p.exit_code)
            lines.append(f"| {p.peer} | {p.status.upper()} | {exit_} | {p.seconds:.0f} s | {' > '.join(p.levels)} "
                         f"| {fps} | {lf} | {p.peak_private_mb} | {p.errors} | {ms} |")
        for p in r.peers:
            for why in p.reasons:
                lines.append(f"- {p.peer}: {why}")
        if r.compare:
            lines += [""] + compare_markdown(r.compare)
    written = {s for s in (c.get("id") for c in manifest.get("case", []))}
    ran = {r.case for r in results}
    missing = sorted(c["id"] for c in manifest.get("case", []) if c.get("spec") and c["id"] not in ran)
    if missing:
        lines += ["", "## Manifest cases with a spec, not run in this run", "", ", ".join(missing)]
    if unwritten:
        lines += ["", "## Required cases of these stages with no spec yet", "", ", ".join(unwritten)]
    lines += ["", "## Left alone", "", "| Folder | Files before | Files after | Unchanged |", "|---|---|---|---|"]
    for g in guard:
        lines.append(f"| {g['folder']} | {g['before'][0]} | {g['after'][0]} | {'yes' if g['unchanged'] else 'NO'} |")
    lines += ["", f"Thresholds: {cfg.thresholds.name} (version {thresholds.get('version', '?')}); manifest cases: "
              f"{len(written)}."]
    if checkpoint:
        lines += ["", f"**{checkpoint['line']}**"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def previous_statuses(cfg: Config, current: Path) -> dict[str, str]:
    """The last recorded status of each case before this run, from earlier results.json files."""
    seen: dict[str, str] = {}
    runs = cfg.store / "runs"
    if not runs.is_dir():
        return seen
    for f in sorted(runs.glob("*/results.json")):
        if f.parent == current:
            continue
        try:
            for c in json.loads(f.read_text(encoding="utf-8")).get("cases", []):
                seen[c["case"]] = c["status"]
        except (OSError, ValueError, KeyError):
            continue
    return seen


def write_findings(out: Path, results: list[CaseResult], before: dict[str, str]) -> None:
    lines = ["# Findings: what changed since the last run of each case", ""]
    changes = 0
    for r in results:
        old = before.get(r.case)
        if old != r.status:
            changes += 1
            lines.append(f"- {r.case}: {old or 'never run'} -> {r.status}" + (f" ({'; '.join(r.reasons)})" if r.reasons else ""))
    if not changes:
        lines.append("Nothing new.")
    (out / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- commands -------------------------------------------------------------------------------------


def default_max_games() -> int:
    cores = os.cpu_count() or 4
    free_gb = 8.0
    if os.name == "nt":
        try:
            import ctypes

            class MEMSTATUS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMSTATUS()
            st.dwLength = ctypes.sizeof(MEMSTATUS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                free_gb = st.ullAvailPhys / 1024**3
        except (OSError, AttributeError):
            pass
    # A game takes about 1 GB of working set and 3 GB of commit in a level, and two threads' worth of CPU.
    return max(1, min(5, cores // 3, int(free_gb // 1.5)))


def stage_order(manifest: dict) -> list[str]:
    return [str(x) for x in manifest.get("stages", [])]


def stages_up_to(manifest: dict, stage: str) -> list[str]:
    """A checkpoint grades its stage and every earlier one (plan 5.4): `--stage 1A` means 0, 1-pre, 1A."""
    order = stage_order(manifest)
    if stage not in order:
        raise SystemExit(f"unknown stage {stage!r}; the manifest's order is {order}")
    return order[: order.index(stage) + 1]


def select(specs: list[Spec], patterns: list[str], stages: list[str] | None) -> list[Spec]:
    if not patterns or patterns == ["all"]:
        chosen = list(specs)
    else:
        chosen = [s for s in specs if any(fnmatch.fnmatch(s.name, p) or s.path.stem == p for p in patterns)]
        if not chosen:
            raise SystemExit(f"no case matches {patterns}; `skytest.py list` names them")
    if stages is not None:
        chosen = [s for s in chosen if s.stage in stages]
    return chosen


def folder_state(root: Path, skip: tuple[str, ...] = ()) -> tuple[int, float, str]:
    """How many files a folder holds, the newest change time, and a hash over every file's relative
    path, size and change time (a rename or a same-time overwrite shows too); links never followed."""
    count, newest = 0, 0.0
    h = hashlib.sha256()
    if not root.exists():
        return 0, 0.0, h.hexdigest()
    entries = []
    for d, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
        for f in files:
            if f in skip:
                continue
            full = os.path.join(d, f)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            count += 1
            newest = max(newest, st.st_mtime)
            entries.append(f"{os.path.relpath(full, root)}|{st.st_size}|{st.st_mtime_ns}")
    for e in sorted(entries):
        h.update(e.encode("utf-8", "surrogateescape") + b"\n")
    return count, newest, h.hexdigest()


def guarded_folders(cfg: Config) -> dict[str, tuple[Path, tuple[str, ...]]]:
    """What a run must leave alone: the owner's data folders and the disc (whose cachenonce.txt the
    game itself rewrites at every start)."""
    # Every place the owner's data can be, also one that does not exist yet: a game creating it fails too.
    out = {f"owner data {r}": (r, ()) for r in owner_data_roots()}
    out[f"disc {cfg.rom}"] = (cfg.rom.resolve(), ("cachenonce.txt",))
    return out


def janitor_line(cfg: Config) -> None:
    j = cfg.repo / "tools" / "test_store_janitor.py"
    if j.is_file():
        r = subprocess.run([sys.executable, "-I", str(j), "--repo", str(cfg.repo), "--line"], capture_output=True,
                           text=True)
        if r.stdout.strip():
            print(r.stdout.strip())


def cmd_test(cfg: Config, a: argparse.Namespace) -> int:
    janitor_line(cfg)
    manifest = load_manifest(cfg)
    stages = stages_up_to(manifest, a.stage) if a.stage else None
    specs = select(all_specs(cfg), a.cases, stages)
    if not specs:
        raise SystemExit("no case to run")
    check_disc(cfg)
    max_games = a.max_games or default_max_games()
    snap = snapshot_build(cfg)
    guarded = guarded_folders(cfg)
    before_state = {k: folder_state(path, skip) for k, (path, skip) in guarded.items()}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = fresh_dir(cfg.store / "runs" / f"{stamp}-session")
    check_output_path(cfg, out)
    out.mkdir(parents=True)
    thresholds = tomllib.loads(cfg.thresholds.read_text(encoding="utf-8")) if cfg.thresholds.is_file() else {}
    baseline = tomllib.loads(cfg.baseline.read_text(encoding="utf-8")) if cfg.baseline.is_file() else {}
    results = []
    for spec in specs:
        print(f"{spec.name} ({spec.layout}) ...", flush=True)
        entry = next((c for c in manifest.get("case", []) if c.get("id") == spec.name), {})
        run = run_compare_case if spec.compare else run_case
        r = run(cfg, spec, snap, max_games, time.strftime("%Y%m%d-%H%M%S"), thresholds, baseline,
                tuple(entry.get("accepted_grades", [])))
        print(f"  {r.status.upper()}" + (f": {'; '.join(r.reasons)}" if r.reasons else ""), flush=True)
        results.append(r)
    after_state = {k: folder_state(path, skip) for k, (path, skip) in guarded.items()}
    guard = [{"folder": k, "before": before_state[k], "after": after_state[k],
              "unchanged": before_state[k] == after_state[k]} for k in guarded]
    # Required cases of the stages run here that have no spec yet: named, never skipped silently.
    unwritten = [c["id"] for c in manifest.get("case", [])
                 if c.get("kind") == "required" and not c.get("spec")
                 and (stages is None or str(c.get("stage")) in stages)] if a.cases in ([], ["all"]) else []
    touched = [g["folder"] for g in guard if not g["unchanged"]]
    full_run = a.cases in ([], ["all"])
    before = previous_statuses(cfg, out)
    checkpoint = checkpoint_verdict(manifest, a.stage, stages, results, touched, before) if a.stage and full_run else None
    payload = {"snapshot": str(snap), "max_games": max_games, "stages": stages, "thresholds": thresholds,
               "guarded_folders": guard, "required_without_spec": unwritten, "checkpoint": checkpoint,
               "cases": [asdict(r) for r in results]}
    (out / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_report(cfg, out, results, manifest, thresholds, max_games, guard, unwritten, checkpoint)
    write_findings(out, results, before)
    failed = [r for r in results if r.status not in ("pass", "capacity")]
    capacity = [r for r in results if r.status == "capacity"]
    if touched:
        print(f"WARNING: changed during the run: {', '.join(touched)}")
    if capacity:
        print(f"{len(capacity)} case(s) not run for capacity (a capacity result is reported, not failed):"
              f" {', '.join(r.case for r in capacity)}")
    if checkpoint:
        print(checkpoint["line"])
    print(f"report: {out / 'report.md'}")
    return 1 if failed or touched else 0


def cmd_compare(cfg: Config, a: argparse.Namespace) -> int:
    """Compares finished runs (run folders: a case's, or one run of a [compare] case) with a case's
    [compare] settings; the first `--noise` runs are the A/A. Reads the runs, writes compare.md and
    compare.json into a new folder of the store; starts no game."""
    spec = next((s for s in all_specs(cfg) if s.name == a.spec), None)
    if spec is None:
        raise SystemExit(f"no case {a.spec}; `skytest.py list` names them")
    if not spec.compare:
        raise SystemExit(f"{a.spec} has no [compare] table to compare with")
    folders = [Path(f).resolve() for f in a.runs]
    for f in folders:
        if not f.is_dir():
            raise SystemExit(f"{f} is not a run folder")
    noise = a.noise or (len(folders) - 1 if len(folders) >= 3 else len(folders))
    if len(folders) < 2 or not 2 <= noise <= len(folders):
        raise SystemExit("compare needs two or more runs, and an A/A (--noise) of two or more of them")
    names: list[str] = []
    for i, f in enumerate(folders):
        base = re.sub(r"[^A-Za-z0-9_-]", "_", f.name)
        names.append(base if base not in names else f"{base}-{i + 1}")
    gathered = [gather_run(n, f, spec) for n, f in zip(names, folders)]
    cmp = compare_runs(gathered, {**spec.compare, "noise": names[:noise], "test": names[noise:]})
    out = fresh_dir(cfg.store / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-compare-{spec.name}")
    check_output_path(cfg, out)
    out.mkdir(parents=True)
    write_compare(out, cmp)
    print(f"compare {spec.name}: {cmp['status'].upper()}" + (f": {'; '.join(cmp['reasons'])}" if cmp["reasons"] else ""))
    print(f"report: {out / 'compare.md'}")
    return 0 if cmp["status"] == "pass" else 1


def cmd_list(cfg: Config, a: argparse.Namespace) -> int:
    specs = {s.name: s for s in all_specs(cfg)}
    for s in specs.values():
        print(f"{s.name:<32} {s.layout:<9} stage {s.stage:<4} {s.description}")
    man = load_manifest(cfg).get("case", [])
    if man:
        todo = [c for c in man if c["id"] not in specs]
        print(f"\nmanifest: {len(man)} case(s); {len(man) - len(todo)} with a spec, {len(todo)} still to write")
    return 0


def cmd_lint(cfg: Config, a: argparse.Namespace) -> int:
    problems = []
    specs = {}
    for p in sorted(cfg.cases_dir.glob("*.toml")) if cfg.cases_dir.is_dir() else []:
        try:
            s = load_spec(p, cfg.timeout)
            if s.name != p.stem:
                problems.append(f"{p.name}: the file is named {p.stem} but the case {s.name}")
            if s.name in specs:
                problems.append(f"{p.name}: case name {s.name} also in {specs[s.name].path.name}")
            specs[s.name] = s
        except SpecError as e:
            problems.append(str(e))
    manifest = load_manifest(cfg)
    man = manifest.get("case", [])
    order = stage_order(manifest)
    if man and not order:
        problems.append("manifest: `stages` (the stage order) is missing")
    ids = set()
    for c in man:
        for key in ("id", "stage", "layout", "kind"):
            if key not in c:
                problems.append(f"manifest: a case lacks `{key}`: {c}")
        bad = [g for g in c.get("accepted_grades", []) if g not in GRADES]
        if bad:
            problems.append(f"manifest: {c.get('id')}: accepted_grades {bad} are not grades ({', '.join(GRADES)})")
        if c.get("accepted_grades") and not c.get("reason"):
            problems.append(f"manifest: {c.get('id')}: accepted_grades need a `reason`")
        if c.get("id") in ids:
            problems.append(f"manifest: {c.get('id')} twice")
        if order and str(c.get("stage")) not in order + ["v1"]:
            problems.append(f"manifest: {c.get('id')}: stage {c.get('stage')} is not in `stages`")
        ids.add(c.get("id"))
        if c.get("kind") not in ("required", "unsupported", "accepted-failure"):
            problems.append(f"manifest: {c.get('id')}: kind must be required, unsupported or accepted-failure")
        if c.get("layout") and not LAYOUT.match(c["layout"]) and c["layout"] not in MANIFEST_ONLY_LAYOUTS:
            problems.append(f"manifest: {c.get('id')}: unknown layout {c['layout']}")
        if c.get("kind") in ("accepted-failure", "unsupported") and not c.get("reason"):
            problems.append(f"manifest: {c.get('id')}: an {c['kind']} case needs a reason")
        if c.get("spec") and c.get("id") not in specs:
            problems.append(f"manifest: {c.get('id')}: spec = true but tests/runtime has no such case")
        if c.get("id") in specs and not c.get("spec"):
            problems.append(f"manifest: {c.get('id')}: has a spec; mark it spec = true")
        if c.get("id") in specs and specs[c["id"]].layout != c.get("layout"):
            problems.append(f"manifest: {c.get('id')}: layout {c.get('layout')} but its spec says {specs[c['id']].layout}")
        if c.get("id") in specs and specs[c["id"]].stage != str(c.get("stage")):
            problems.append(f"manifest: {c.get('id')}: stage {c.get('stage')} but its spec says {specs[c['id']].stage}")
    for name in specs:
        if man and name not in ids:
            problems.append(f"{name}: a spec with no manifest entry")
    for p in problems:
        print(p)
    print(f"{len(specs)} spec(s), {len(man)} manifest case(s), {len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="The Skylanders online forks' test runner.")
    p.add_argument("--repo", default=".", help="the game checkout (default: the current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("cases", nargs="*", help="case names or globs; none or `all` runs every case")
    t.add_argument("--stage", default=None, help="this stage's cases and every earlier stage's (a checkpoint's set)")
    t.add_argument("--max-games", type=int, default=0, help="most games at once (default: from cores and memory)")
    t.set_defaults(fn=cmd_test)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("lint").set_defaults(fn=cmd_lint)
    f = sub.add_parser("fixtures")
    f.add_argument("--from-owner", action="store_true")
    f.set_defaults(fn=cmd_fixtures)
    c = sub.add_parser("compare")
    c.add_argument("runs", nargs="+", help="run folders, the A/A first")
    c.add_argument("--spec", required=True, help="the case whose [compare] settings apply")
    c.add_argument("--noise", type=int, default=0, help="how many of the runs make the A/A (default: all but the"
                   " last of three or more, else all)")
    c.set_defaults(fn=cmd_compare)
    a = p.parse_args(argv)
    cfg = load_config(repo_root(Path(a.repo)))
    return a.fn(cfg, a)


if __name__ == "__main__":
    sys.exit(main())
