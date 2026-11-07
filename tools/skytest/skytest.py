#!/usr/bin/env python3
"""skytest: the multiplayer test runner of the Skylanders ports' online forks.

Run from a game repository (its `just test-online` calls this). A case is a TOML spec in the game's
tests/runtime/; it names a layout and what each peer's game does (an input script) and what its log
must show. The runner starts the layout's game processes, each on its own copies of the test data,
ends them, and writes report.md and results.json in a new run folder of the worktree's test store.
It stops a run early (fail fast, on unless a spec says `fail_fast = false`) a grace after a peer's
first fatal line: a crash, a forbidden line, or a crash exit code.

    skytest.py test [CASE ...] [--stage S] [--max-games N]   run cases (a name, a glob, or all)
               [--gate]                                     only the manifest's gate cases (gate = true)
                                                            of the stages up to S, with their expected
                                                            minutes before the run
               [--allow-restricted]                         start games even under a restricted token
                                                            (refused otherwise; timings not comparable)
               [--jobs N]                                   up to N cases at once (default 1), their games
                                                            within --max-games; a spec with
                                                            `exclusive = true` runs alone
    skytest.py list [--gate [--stage S]]                    the cases and the manifest's state; with
                                                            --gate the gate cases and their expected
                                                            minutes (no game started)
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
report (capacity, missing fixture, spec error) instead of being skipped silently. A runner with a
restricted token (an agent sandbox) starts no game unless --allow-restricted (its games could not use
the shader caches); every game it starts is opted out of Windows power throttling.

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
import threading
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
GRADES = ("server_frame_time", "client_fps", "level_load", "save_copy", "press_to_reaction", "requests_answered",
          "symmetry", "route", "bandwidth")
# The attack path's press and reaction lines on the requester (stage 1B item 7; the game's
# online/client/attack_presses.h): `press <peer>:<seq> slot <s> at pose <p> (generation <g>)`, and
# for its Reaction `reaction <peer>:<seq> after <ms> ms (interim) landed ...` (a press whose hit
# landed on S: a pair) or `reaction <peer>:<seq> missed|refused after <ms> ms (interim)` (counted
# apart). The round trip comes from the client's barrier line (`(round trip <us> us)`).
PRESS = re.compile(r"Online: press (\d+):(\d+) slot (\d+) at pose (\d+)(?: \(generation (\d+)\))?")
REACTION = re.compile(r"Online: reaction (\d+):(\d+) after ([\d.]+) ms \(interim\) landed\b(?:.*\(generation (\d+)\))?")
REACTION_OTHER = re.compile(r"Online: reaction (\d+):(\d+) (missed|refused) after ([\d.]+) ms \(interim\)"
                            r"(?:.*\(generation (\d+)\))?")
ROUND_TRIP = re.compile(r"\(round trip (\d+) us\)")
# The request lifecycle lines (stage 1B item 9; docs/online-1B.md section 5; the game's
# session/session_host.cpp LogRequest and LogDecision, the same text on S and on the requester). A
# key is "<generation>/<peer>/<kind>/<sequence>/<what>" (session_host.h RequestKeyText).
REQ_KEY = r"(\d+/\d+/(?:enter|exit|touch|telegram|skip)/\d+/\S+)"
REQUEST_LINE = re.compile(r"Online: request (\d+):(\d+) (enter|exit|touch|telegram|skip) " + REQ_KEY)
DECIDED_APPLIED = re.compile(r"Online: decided " + REQ_KEY + r" applied seq (\d+)")
DECIDED_PENDING = re.compile(r"Online: decided " + REQ_KEY + r" pending\s*$")
CACHED_APPLIED = re.compile(r"Online: already applied " + REQ_KEY + r"\s*$")
CACHED_PENDING = re.compile(r"Online: pending " + REQ_KEY + r"\s*$")
CACHED_REFUSED = re.compile(r"Online: refused " + REQ_KEY + r" \((.+)\) cached\s*$")
SERVER_FINAL = re.compile(r"Online: final " + REQ_KEY + r" refused \((.+)\)\s*$")
REFUSED_LINE = re.compile(r"Online: refused " + REQ_KEY + r" \((.+)\)\s*$")
APPLIED_STEP = re.compile(r"Online: applied step " + REQ_KEY + r" seq \d+ (.*?)\s*$")
GONE_AT_CLOSE = re.compile(r"Online: peer (\d+) gone at close")
# A request the session refused to send (End session began, or no connection): no decision can come.
REQUEST_NOT_SENT = re.compile(r"Online: request " + REQ_KEY + r" not sent")
SYMMETRY_RESET = re.compile(r"Online: symmetry reset (\d+) (\S+)")
# The level flow's lines (stage 1B item 13; the game's server/level_flow.h and client/level_guard.h).
# S's first line of a level end (a duplicate, `not sent` and `sent to` lines read differently), the
# server's own level change, the results screens' advance, and a client's level-guard passes.
LEVEL_END_FIRST = re.compile(r"Online: level end (\w+) to (\S+) \(generation (\d+)\)\s*$")
LEVEL_END_DUPLICATE = re.compile(r"Online: level end (\w+) to (\S+) duplicate \(generation (\d+);")
OWN_LEVEL_OPEN = re.compile(r"Online: level (\d+) (\S+): open \(the server's own level change")
RESULTS_ADVANCED = re.compile(r"Online: results screen \S+ advanced by (native|press)\b")
RESULTS_DONE = re.compile(r"Online: results screens done: ")
CLIENT_PASSED = re.compile(r"Online: level request 0x[0-9A-Fa-f]+ passed (\S+) \((.+?): (.+)\)\s*$")
CLIENT_NATIVE_END = re.compile(r"Online: level end (\w+) to (\S+) played here \(not connected to the server\)")
# A client's level-guard passes that do not take it out of the session's level by itself.
CLIENT_PASS_OK = ("the session's own request", "not connected to the server",
                  "a joiner's continue from its title")
# The route checklist's evidence on S: the authority's replay of a request (`replay <key> <Name#id> by
# <who> ...`, then `replay done <key> activated 1`), the encounter watch, the scene occurrences.
REPLAY_LINE = re.compile(r"Online: replay (\S+) (\S+) by ")
REPLAY_DONE = re.compile(r"Online: replay done (\S+) activated 1\b")
ENCOUNTER_START = re.compile(r"Online: encounter start (\S+) \(from ")
ENCOUNTER_COMPLETE = re.compile(r"Online: encounter complete (\S+)")
SCENE_START = re.compile(r"Online: scene (\S+) start \S+ (.+?) recipient ")
SCENE_END = re.compile(r"Online: scene (\S+) end by ")
# The per-lane traffic (session_host.cpp LaneTraffic, every 10 s): on S per peer, bytes sent to it
# (first sends, resends and acks) / received from it, cumulative.
TRAFFIC_PEER = re.compile(r"Online: traffic peer (\d+) gameplay (\d+)/(\d+) state (\d+)/(\d+) fast (\d+)/(\d+) bytes")
# The reasons a client may resend after (requests.h IsRetryable): for an enter or a touch, and for
# an exit only `exit before enter`. Every other refusal is terminal.
RETRYABLE_ENTER = ("too_far", "rate", "not released", "not in play")
# A title folder's name (the game's title id in hex), for the save_copy grade.
TITLE_ID = re.compile(r"^[0-9A-F]{8}$")
# S's boot timeline ending outside play (server_role.cpp BootCheck) is fatal for every hosted case:
# the owner's game then waits for a hub that never comes (P1m: a case ran to its 1200 s limit).
ALWAYS_FORBIDDEN = [r"\] Crash: ", r"\[FATAL\]", r"Input script: cannot (parse|use)",
                    r"Online: the server did not reach its hub"]
# Fail fast (on unless a spec says `fail_fast = false`): while a run's games play, the runner reads the
# new bytes of each peer's log every FAIL_FAST_POLL seconds and, at the first fatal line (a forbidden
# one: the peer's `forbid` and ALWAYS_FORBIDDEN, a crash among them) or a game that exits with a crash
# code, stops every game of the run after a grace (tests/skytest.toml [defaults]
# fail_fast_grace_seconds, so the lines after it are logged) instead of waiting for the timeout.
FAIL_FAST_POLL = 1.0
DEFAULT_FAIL_FAST_GRACE = 10.0
# The guest's start in a game's log: the GPU's first SetInterruptCallback (the first line the guest
# causes; the host's startup before it, e.g. the shader storage, can stall for seconds: P1j C2).
GUEST_START = re.compile(r"SetInterruptCallback\(")
# The startup stall (P1j; reported per peer, never graded): from the host's "Initializing shader
# storage" (ReXApp::LaunchModule, which blocks there until the GPU thread has built its pipelines) to
# the guest's first line, its first `VFS:` mount or SetInterruptCallback, whichever comes first (the
# VFS line is 0.4-0.9 s before SetInterruptCallback in every log, so with the latter alone a warm start
# would read 0.5 s and a loaded one near the warning). 0.01-0.16 s with usable shader caches, 4-32 s
# without them (a restricted token, a cold cache) in 621 logs of 11/5-11/6.
SHADER_STORAGE = re.compile(r"Initializing shader storage")
GUEST_FIRST_LINE = re.compile(r"VFS:|SetInterruptCallback\(")
STARTUP_STALL_WARN_S = 1.0

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
# lines (LogTraceCounts) give every key's exact count so far (code). The sampling rule is
# CountAndShouldLog (traces.cpp: n <= kFirstLines || n % kEvery == 0, trace_counts.h).
TRACE_FIRST_LINES = 20
TRACE_EVERY = 500
TRACE_MESSAGE = re.compile(r"Trace: message (\S+) to .*\(#(\d+)\)\s*$")
TRACE_TELEGRAM = re.compile(r"Trace: telegram (\S+) sender .*\(#(\d+)\)\s*$")
TRACE_TRIGGER = re.compile(r"Trace: trigger (.+) (enter|exit|act|de-act|touch|other) by .*\(bits 0x[0-9A-Fa-f]+, "
                           r"#(\d+)\)\s*$")
TRACE_COUNTS = re.compile(r"Trace counts: (\d+) keys")
TRACE_COUNT = re.compile(r"Trace count: (.+) (\d+)\s*$")
DEFAULT_WINDOW = r"Online: barrier \d+ released at tick"
RUN_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
COMPARE_KEYS = {"peer", "run", "noise", "test", "reached", "window", "window_end", "load_floor_s", "memexport_zero",
                "rate_floor"}
# The per-frame trigger events: sub_82AE9330 is called every server frame while an entity stands in
# a volume with act (0x8) or touch (0x4), once for enter (0x1), exit (0x2) and de-act (0x10)
# (traces.cpp: TriggerEvent names them, enter > exit > act > de-act > touch, and its listener trace
# calls act and touch "the per-frame touch and act calls") (code). Their counts follow the server's
# frame rate and the window's length, which the draw skip changes (P1e: about 57 fps off, 60 on), so
# their keys ("trigger act <Name#id>", "trigger touch <Name#id>") are graded as events per frame.
RATE_EVENTS = ("act", "touch")
DEFAULT_RATE_FLOOR = 0.01  # a rate key's tolerance: the A/A spread plus this share of the A/A rate

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
    # Fail fast: seconds between a run's first fatal line and the stop of its games.
    fail_fast_grace: float = DEFAULT_FAIL_FAST_GRACE
    # [defaults] show_server_window: the server peer's window is shown (a playtest the owner
    # watches); a spec's own show_server_window wins. Missing: hidden, as before (show_server_args).
    show_server_window: bool = False


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
    grace = d.get("fail_fast_grace_seconds", DEFAULT_FAIL_FAST_GRACE)
    if not isinstance(grace, (int, float)) or isinstance(grace, bool) or grace < 0:
        raise SystemExit(f"{f}: [defaults] fail_fast_grace_seconds must be a number of seconds >= 0")
    show = d.get("show_server_window", False)
    if not isinstance(show, bool):
        raise SystemExit(f"{f}: [defaults] show_server_window is true or false")
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
        fail_fast_grace=float(grace),
        show_server_window=show,
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


# What a spec's `args` may name: the game checkout the runner tests (its repository root, as
# forward slashes), e.g. --online_route_file={repo}/tests/routes/tutorial_b.toml. Scripts take
# {figures}, {peer} and {join} instead (peer_script).
ARG_PLACEHOLDERS = ("{repo}",)
ARG_PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")
# The plan's daily gate: each stage names a 30-minute subset (docs/online-fork-plan.md 5.4).
GATE_MINUTES = 30

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
    # Fail fast (FAIL_FAST_POLL): stop the run's games at the first fatal line; `fail_fast = false`
    # waits for the script's end or the timeout, as before. A compared case's runs follow it.
    fail_fast: bool = True
    # show_server_window = true|false: the server peer's window shown or hidden for this case (a case
    # whose subject needs a hidden server, e.g. the draw skip, says false); None: the [defaults] key.
    show_server_window: bool | None = None
    # exclusive = true: the case never runs beside another under `test --jobs N` (it waits until the
    # running ones end, and nothing starts until it ends). A case whose grade measures time or load
    # (TIMED_GRADES, a [compare]) needs it: beside another case it would measure that case's load too.
    exclusive: bool = False


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
        for arg in peers[n].args:
            for ph in ARG_PLACEHOLDER.findall(arg):
                if ph not in ARG_PLACEHOLDERS:
                    raise SpecError(f"{path.name}: peer {n}: {ph} in {arg!r} is not substituted in args (only"
                                    f" {', '.join(ARG_PLACEHOLDERS)})")
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
        if gname == "requests_answered":
            check_requests_answered(path.name, g, names)
            continue
        if gname == "symmetry":
            check_symmetry(path.name, g, names)
            continue
        if gname == "route":
            check_route(path.name, g, names)
            continue
        if gname == "bandwidth":
            check_bandwidth(path.name, g, names)
            continue
        if not isinstance(g, dict) or g.get("peer", "S" if gname == "server_frame_time" else "H") not in names:
            raise SpecError(f"{path.name}: grade {gname} names a peer not in {names}")
        if gname == "level_load" and (g.get("measure") not in d.get("measure", {}) or not g.get("baseline")):
            raise SpecError(f"{path.name}: grade level_load needs `measure` (one of the spec's) and `baseline`")
        if gname == "press_to_reaction":
            check_press_to_reaction(path.name, g)
    measures = d.get("measure", {})
    for mname, m in measures.items():
        if not isinstance(m, dict) or "to" not in m:
            raise SpecError(f"{path.name}: measure {mname} needs `to` (and `from`, or it counts from the start)")
        if m.get("peer", "H") not in names:
            raise SpecError(f"{path.name}: measure {mname} is for peer {m.get('peer', 'H')}, not in {names}")
    compare = d.get("compare", {})
    if "compare" in d:
        check_compare(path.name, compare, names, server)
    fail_fast = d.get("fail_fast", True)
    if not isinstance(fail_fast, bool):
        raise SpecError(f"{path.name}: fail_fast is true or false")
    show = d.get("show_server_window")
    if show is not None and not isinstance(show, bool):
        raise SpecError(f"{path.name}: show_server_window is true or false")
    for n, p in peers.items():
        if any(a.startswith(f"--{SHOW_SERVER_SETTING}=") for a in p.args):
            raise SpecError(f"{path.name}: peer {n} sets --{SHOW_SERVER_SETTING} in its args; the spec's"
                            " show_server_window key does that")
    exclusive = d.get("exclusive", False)
    if not isinstance(exclusive, bool):
        raise SpecError(f"{path.name}: exclusive is true or false")
    # A [[compare.run]]'s extra args too (check_compare checked their shape above).
    extra = [(n, a) for r in compare.get("run", []) for n, t in r.get("peer", {}).items() for a in t.get("args", [])]
    for n, a in [(n, a) for n, p in peers.items() for a in p.args] + extra:
        key = a.split("=", 1)[0]
        if key in JOBS_OWNED_ARGS:
            raise SpecError(f"{path.name}: peer {n} sets {key}: {JOBS_OWNED_ARGS[key]}")
    return Spec(
        name=d["name"], path=path, layout=d["layout"], stage=str(d["stage"]),
        description=d.get("description", ""), timeout=float(d.get("timeout", default_timeout)),
        peers=peers, measures=measures, standin_seconds=float(d.get("standin_seconds", 45)),
        server=server, grades=grades, compare=compare, fail_fast=fail_fast, show_server_window=show,
        exclusive=exclusive,
    )


def check_press_to_reaction(name: str, g: dict) -> None:
    """A press_to_reaction grade's options: min_pairs (a whole number >= 1), statistic (p50, p95 or
    max), send_interval_ms and rtt_ms (numbers >= 0, overriding the defaults)."""
    pairs = g.get("min_pairs", 10)
    if not isinstance(pairs, int) or isinstance(pairs, bool) or pairs < 1:
        raise SpecError(f"{name}: grade press_to_reaction: min_pairs must be a whole number >= 1")
    if g.get("statistic", "p95") not in ("p50", "p95", "max"):
        raise SpecError(f"{name}: grade press_to_reaction: statistic is p50, p95 or max")
    for key in ("send_interval_ms", "rtt_ms"):
        v = g.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            raise SpecError(f"{name}: grade press_to_reaction: {key} must be a number >= 0")


def percentile(values: list[float], q: float) -> float:
    """The nearest-rank percentile of `values` (not empty): the smallest value with at least q% of
    the values at or below it."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil(n * q / 100), at least 1
    return ordered[int(rank) - 1]


def press_to_reaction_limit(g: dict, thresholds: dict, rtt_ms: float | None) -> tuple[float, dict]:
    """The threshold of plan 5.5 for press to reaction (tests/thresholds.toml [press_to_reaction]):
    fixed_ms plus what its `plus` names. send_interval is one send interval of the requester, 1000 /
    [send_rates] players_hz (the rate a press travels at in the player's records from stage 4; a spec
    can set send_interval_ms); rtt is the round trip the requester measured (the median of its
    `round trip` lines; a spec can set rtt_ms; none logged: 0, the strictest). Returns the limit and
    its parts, for the report."""
    t = thresholds.get("press_to_reaction", {})
    fixed = float(t.get("fixed_ms", 250.0))
    plus = list(t.get("plus", ["send_interval", "rtt"]))
    parts: dict = {"fixed_ms": fixed}
    limit = fixed
    if "send_interval" in plus:
        if g.get("send_interval_ms") is not None:
            interval = float(g["send_interval_ms"])
        else:
            hz = float(thresholds.get("send_rates", {}).get("players_hz", 20.0))
            interval = 1000.0 / hz if hz > 0 else 0.0
        parts["send_interval_ms"] = round(interval, 3)
        limit += interval
    if "rtt" in plus:
        rtt = float(g["rtt_ms"]) if g.get("rtt_ms") is not None else (rtt_ms or 0.0)
        parts["rtt_ms"] = round(rtt, 3)
        parts["rtt_source"] = "spec" if g.get("rtt_ms") is not None else ("log" if rtt_ms is not None else "none")
        limit += rtt
    return limit, parts


def grade_press_to_reaction(lines: list[str], g: dict, thresholds: dict) -> tuple[dict, list[str]]:
    """The press_to_reaction grade (stage 1B item 7; interim, the owner's decision 2): on the
    requester's log, every press line and its reaction line, timed on the requester's clock (the
    reaction line's own milliseconds). Only a press whose hit landed makes a pair; misses, refusals
    and presses without any reaction are counted apart, and so is a reaction with no press before it.
    The pairs' distribution (min, p50, p95, max) is graded at `statistic` (p95 by default) against
    the threshold, once there are at least `min_pairs` (10) pairs. A reaction answers the press of
    the same peer, sequence and generation; only its first reaction counts."""
    presses: set[tuple] = set()
    press_lines = 0
    pairs: list[float] = []
    missed = refused = unpaired = 0
    answered: set[tuple] = set()
    rtts: list[float] = []
    for line in lines:
        if m := ROUND_TRIP.search(line):
            rtts.append(int(m.group(1)) / 1000.0)
        if m := PRESS.search(line):
            presses.add((m.group(1), m.group(2), m.group(5)))
            press_lines += 1
            continue
        reaction = REACTION.search(line)
        other = None if reaction else REACTION_OTHER.search(line)
        if not reaction and not other:
            continue
        if reaction:
            key = (reaction.group(1), reaction.group(2), reaction.group(4))
        else:
            key = (other.group(1), other.group(2), other.group(5))
        if key not in presses or key in answered:
            unpaired += 1
            continue
        answered.add(key)
        if reaction:
            pairs.append(float(reaction.group(3)))
        elif other.group(3) == "missed":
            missed += 1
        else:
            refused += 1
    rtt = sorted(rtts)[len(rtts) // 2] if rtts else None
    limit, parts = press_to_reaction_limit(g, thresholds, rtt)
    statistic = g.get("statistic", "p95")
    min_pairs = int(g.get("min_pairs", 10))
    graded: dict = {"interim": True, "presses": press_lines, "pairs": len(pairs), "missed": missed,
                    "refused": refused, "unanswered": len(presses - answered), "unpaired_reactions": unpaired,
                    "statistic": statistic, "limit_ms": round(limit, 3), "min_pairs": min_pairs, **parts}
    if pairs:
        graded.update({"min_ms": min(pairs), "p50_ms": percentile(pairs, 50), "p95_ms": percentile(pairs, 95),
                       "max_ms": max(pairs)})
    reasons = []
    if not presses:
        reasons.append("grade press_to_reaction: (interim) no press line")
    elif len(pairs) < min_pairs:
        reasons.append(f"grade press_to_reaction: (interim) {len(pairs)} landed pairs, fewer than min_pairs "
                       f"{min_pairs} ({press_lines} presses, {missed} missed, {refused} refused, "
                       f"{graded['unanswered']} unanswered)")
    else:
        value = graded[f"{statistic}_ms"]
        graded["value_ms"] = value
        if value > limit:
            reasons.append(f"grade press_to_reaction: (interim) {statistic} {value:.1f} ms over {limit:.1f} ms")
            graded["violation"] = True
    return graded, reasons


# --- the request grades (stage 1B item 9; docs/online-1B.md section 2 "The request state machine",
# section 5 "Runner") ---------------------------------------------------------------------------

def check_requests_answered(spec_name: str, g: object, names: list[str]) -> None:
    """requests_answered's options: `peers` (default every client), whose logs are graded."""
    if not isinstance(g, dict):
        raise SpecError(f"{spec_name}: grade requests_answered must be a table")
    peers = g.get("peers", [n for n in names if n != "S"])
    if not isinstance(peers, list) or not peers or any(p not in names for p in peers):
        raise SpecError(f"{spec_name}: grade requests_answered: `peers` must name peers of {names}")


def check_symmetry(spec_name: str, g: object, names: list[str]) -> None:
    """symmetry's options: `peer` (default S, the log that holds both halves), `owner` and `client`
    (the peer ids whose halves are compared, default 1 and 2), `generations` (the two generations
    whose `symmetry reset` lines start the halves; default the last two reset lines)."""
    if not isinstance(g, dict) or g.get("peer", "S") not in names:
        raise SpecError(f"{spec_name}: grade symmetry names a peer not in {names}")
    gens = g.get("generations")
    if gens is not None and (not isinstance(gens, list) or len(gens) != 2 or
                             any(not isinstance(x, int) or isinstance(x, bool) or x < 1 for x in gens)):
        raise SpecError(f"{spec_name}: grade symmetry: `generations` is two generation numbers")
    for key, default in (("owner", 1), ("client", 2)):
        v = g.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 4:
            raise SpecError(f"{spec_name}: grade symmetry: `{key}` is a client peer id, 1 to 4")
    if g.get("owner", 1) == g.get("client", 2):
        raise SpecError(f"{spec_name}: grade symmetry: `owner` and `client` must differ")


def key_kind(key: str) -> str:
    return key.split("/")[2]


def key_peer(key: str) -> str:
    return key.split("/")[1]


def refusal_final(key: str, reason: str) -> bool:
    """A refusal a check gave (not S's own `final`) is the key's final decision unless the client may
    resend after it (requests.h IsRetryable)."""
    kind = key_kind(key)
    if kind in ("enter", "touch"):
        return reason not in RETRYABLE_ENTER
    if kind == "exit":
        return reason != "exit before enter"
    return True


def decision_of(line: str) -> tuple[str, str, str] | None:
    """(key, what, reason) of a decision line: what is applied, pending, final_refused (S's final),
    refused (a check's), cached_applied, cached_pending or cached_refused."""
    if m := DECIDED_APPLIED.search(line):
        return m.group(1), "applied", ""
    if m := DECIDED_PENDING.search(line):
        return m.group(1), "pending", ""
    if m := CACHED_APPLIED.search(line):
        return m.group(1), "cached_applied", ""
    if m := CACHED_PENDING.search(line):
        return m.group(1), "cached_pending", ""
    if m := CACHED_REFUSED.search(line):
        return m.group(1), "cached_refused", m.group(2)
    if m := SERVER_FINAL.search(line):
        return m.group(1), "final_refused", m.group(2)
    if m := REFUSED_LINE.search(line):
        return m.group(1), "refused", m.group(2)
    return None


def grade_requests_answered(text: str, gone_peers: set[str]) -> tuple[dict, list[str]]:
    """requests_answered (item 9): on one peer's log, every key it logged a `request` line for has
    exactly one final decision (applied, a terminal refusal, or S's `final ... refused`); no key is
    applied twice, or applied and finally refused, or left with none. Cached answers repeat a
    decision and are counted apart, and so are the retryable refusals (the key was resent or S made
    the reason final later). On S's log the keys of a peer S logged `peer <n> gone at close` for are
    left out (that peer's finals could not reach it), and on any log a key with no final that has a
    `request <key> not sent` line (End session beat its send, so no answer can come)."""
    keys: dict[str, None] = {}
    finals: dict[str, list[str]] = {}
    applied: dict[str, int] = {}
    cached = retried = 0
    unlogged = 0
    not_sent: set[str] = set()
    for line in text.splitlines():
        if m := REQUEST_LINE.search(line):
            keys.setdefault(m.group(4), None)
            continue
        if m := REQUEST_NOT_SENT.search(line):
            not_sent.add(m.group(1))
            continue
        d = decision_of(line)
        if not d:
            continue
        key, what, reason = d
        if what.startswith("cached"):
            cached += 1
            continue
        if what == "pending":
            continue
        if what == "applied":
            applied[key] = applied.get(key, 0) + 1
            finals.setdefault(key, []).append("applied")
        elif what == "final_refused" or refusal_final(key, reason):
            finals.setdefault(key, []).append(f"refused ({reason})")
        else:
            retried += 1
        if key not in keys:
            unlogged += 1
    left_out = [k for k in keys if key_peer(k) in gone_peers]
    unsent = [k for k in keys if key_peer(k) not in gone_peers and k in not_sent and not finals.get(k)]
    graded_keys = [k for k in keys if key_peer(k) not in gone_peers and k not in unsent]
    none = [k for k in graded_keys if not finals.get(k)]
    several = [k for k in graded_keys if len(finals.get(k, [])) > 1]
    twice = [k for k in graded_keys if applied.get(k, 0) > 1]
    graded = {"keys": len(graded_keys), "finals": sum(1 for k in graded_keys if len(finals.get(k, [])) == 1),
              "applied": sum(1 for k in graded_keys if applied.get(k)), "cached_answers": cached,
              "retried_refusals": retried, "left_out_gone": len(left_out), "left_out_not_sent": len(unsent),
              "decisions_for_unlogged_keys": unlogged}
    reasons = []
    if not graded_keys:
        reasons.append("grade requests_answered: no request line (not reached)")
    if none:
        reasons.append(f"grade requests_answered: {len(none)} key(s) without a final decision: "
                       f"{', '.join(none[:3])}{' ...' if len(none) > 3 else ''}")
    if several:
        k = several[0]
        reasons.append(f"grade requests_answered: {len(several)} key(s) with more than one final decision: "
                       f"{k} ({', '.join(finals[k])})")
    if twice:
        reasons.append(f"grade requests_answered: {len(twice)} key(s) applied twice: {twice[0]}")
    if reasons:
        graded["violation"] = True
    return graded, reasons


def normalize_key(key: str, generations: dict[str, str], peers: dict[str, str],
                  sequences: dict[tuple[str, str], dict[str, str]]) -> tuple[str, str]:
    """A key with its generation mapped (the half's start to 0, later ones by their order), its peer
    swapped by `peers`, and its peer's sequence of that family (enter sequences for trigger keys,
    request sequences for telegrams, skip sequences for skips) renumbered from 0 in the half, one
    map per (peer, family). Returns the key and its normalized peer."""
    g, peer, kind, seq, what = key.split("/", 4)
    if g not in generations:
        generations[g] = str(len(generations))
    peer = peers.get(peer, peer)
    family = "trigger" if kind in ("enter", "exit", "touch") else kind
    order = sequences.setdefault((peer, family), {})
    if seq not in order:
        order[seq] = str(len(order))
    return "/".join((generations[g], peer, kind, order[seq], what)), peer


def symmetry_streams(lines: list[str], half_generation: str, swap: dict[str, str]) -> tuple[dict, int]:
    """One half's normalized streams, per normalized peer: its requests (each key once, at its first
    request line), its decisions (not the cached repeats, the `pending` answers or the retryable
    refusals, which depend on timing), and its applied steps (the step list as S logged it, the
    applied sequence left out). Returns the streams and the retryable refusals left out."""
    generations = {half_generation: "0"}
    sequences: dict = {}
    streams: dict[str, dict[str, list[str]]] = {}
    seen: set[str] = set()
    retried = 0

    def add(peer: str, stream: str, item: str) -> None:
        streams.setdefault(peer, {"requests": [], "decisions": [], "applied": []})[stream].append(item)

    for line in lines:
        if m := REQUEST_LINE.search(line):
            key = m.group(4)
            if key in seen:
                continue
            seen.add(key)
            nk, peer = normalize_key(key, generations, swap, sequences)
            add(peer, "requests", nk)
            continue
        if m := APPLIED_STEP.search(line):
            nk, peer = normalize_key(m.group(1), generations, swap, sequences)
            add(peer, "applied", f"{nk} {m.group(2)}")
            continue
        d = decision_of(line)
        if not d:
            continue
        key, what, reason = d
        if what.startswith("cached") or what == "pending":
            continue
        if what == "refused" and not refusal_final(key, reason):
            retried += 1
            continue
        nk, peer = normalize_key(key, generations, swap, sequences)
        add(peer, "decisions", f"{nk} {what}{f' ({reason})' if reason else ''}")
    return streams, retried


def grade_symmetry(text: str, g: dict) -> tuple[dict, list[str]]:
    """symmetry (item 9; docs/online-1B.md section 5): S's log holds both halves, each starting at a
    `symmetry reset <generation> <digest>` line (the verified reset; S logs one at every barrier
    release, the hub's too): the halves start at the reset lines of `generations`, or at the last
    two; the second half runs to the next reset line or the log's end. Both digests must be equal,
    or the case is not reached. Each half's streams are normalized (symmetry_streams; the second
    half with the owner's and the client's peer ids swapped), then must be equal peer by peer."""
    owner, client = str(g.get("owner", 1)), str(g.get("client", 2))
    lines = text.splitlines()
    resets = [(i, m.group(1), m.group(2)) for i, line in enumerate(lines) if (m := SYMMETRY_RESET.search(line))]
    graded: dict = {"resets": len(resets)}
    if "generations" in g:
        want = [str(x) for x in g["generations"]]
        picked = [next((r for r in resets if r[1] == w), None) for w in want]
        if any(p is None for p in picked):
            return graded, [f"grade symmetry: not reached (no `symmetry reset` line for generation(s) "
                            f"{', '.join(w for w, p in zip(want, picked) if p is None)})"]
        halves = picked
    else:
        if len(resets) < 2:
            return graded, [f"grade symmetry: not reached ({len(resets)} `symmetry reset` line(s), two needed)"]
        halves = resets[-2:]
    (a, ga, da), (b, gb, db) = halves[0], halves[1]
    graded.update({"generations": [ga, gb], "digests": [da, db]})
    if b <= a:
        return graded, ["grade symmetry: not reached (the second half's reset comes before the first's)"]
    if da != db:
        return graded, [f"grade symmetry: not reached (the halves' digests differ: {da} and {db})"]
    end = next((i for i, _, _ in resets if i > b), len(lines))
    first, retried_a = symmetry_streams(lines[a + 1:b], ga, {})
    second, retried_b = symmetry_streams(lines[b + 1:end], gb, {owner: client, client: owner})
    graded.update({"retried_refusals": [retried_a, retried_b],
                   "keys": [sum(len(s["requests"]) for s in first.values()),
                            sum(len(s["requests"]) for s in second.values())]})
    reasons = []
    if not first.get(owner, {}).get("requests"):
        reasons.append(f"grade symmetry: not reached (no request of peer {owner} in the first half)")
    for peer in sorted(set(first) | set(second)):
        for stream in ("requests", "decisions", "applied"):
            x = first.get(peer, {}).get(stream, [])
            y = second.get(peer, {}).get(stream, [])
            if x != y:
                at = next((i for i in range(min(len(x), len(y))) if x[i] != y[i]), min(len(x), len(y)))
                left = x[at] if at < len(x) else "(end)"
                right = y[at] if at < len(y) else "(end)"
                reasons.append(f"grade symmetry: peer {peer} {stream} differ at {at}: {left} | {right}")
    if reasons:
        graded["violation"] = True
    return graded, reasons


def check_route(spec_name: str, g: object, names: list[str]) -> None:
    """A route grade's options: `file` (the route file, relative to the game checkout; required),
    `peer` (the server's log, default S) and `clients` (the clients whose level-guard lines must show
    no level left by itself; default every client)."""
    if not isinstance(g, dict):
        raise SpecError(f"{spec_name}: grade route must be a table")
    f = g.get("file")
    if (not isinstance(f, str) or not f.strip() or Path(f).is_absolute() or f.startswith(("/", "\\"))
            or ".." in Path(f).parts):
        raise SpecError(f"{spec_name}: grade route: `file` is the route file's path inside the game checkout"
                        " (tests/routes/<level>.toml)")
    if g.get("peer", "S") not in names:
        raise SpecError(f"{spec_name}: grade route: `peer` {g.get('peer')!r} is not in {names}")
    clients = g.get("clients", [n for n in names if n != "S"])
    if not isinstance(clients, list) or any(c not in names or c == "S" for c in clients):
        raise SpecError(f"{spec_name}: grade route: `clients` must name clients of {names}")


def route_name_matches(want: str, seen: str) -> bool:
    """A checklist name against a logged `Name#id` (both case folded): `Name#id` or `Class#id` must
    match whole, a bare `Name` matches any id of that name."""
    want, seen = want.casefold(), seen.casefold()
    if "#" in want:
        return want == seen
    return seen.rsplit("#", 1)[0] == want


def load_route_checklist(path: Path) -> tuple[dict | None, str]:
    """The route checklist of a route file (tests/routes/<level>.toml): [level_end] (class, optional
    recipient and next_zone), the triggers whose class is request or two_player, [[encounter]]
    (manager), [[checkpoint]] (trigger, optional expect, a pattern on S), [[scene]] (name). Returns the
    checklist and "" or None and why it cannot be read."""
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        return None, f"the route file {path.name} cannot be read ({e})"
    end = d.get("level_end", {})
    if not isinstance(end, dict) or not isinstance(end.get("class"), str) or not end["class"]:
        return None, f"the route file {path.name} has no [level_end] class (X11 names it)"
    return {
        "level_end": end,
        "triggers": [t["name"] for t in d.get("trigger", []) if isinstance(t, dict) and isinstance(t.get("name"), str)
                     and t.get("class", "request") in ("request", "two_player")],
        "encounters": [e["manager"] for e in d.get("encounter", []) if isinstance(e, dict)
                       and isinstance(e.get("manager"), str)],
        "checkpoints": [c for c in d.get("checkpoint", []) if isinstance(c, dict) and isinstance(c.get("trigger"), str)],
        "scenes": [c["name"] for c in d.get("scene", []) if isinstance(c, dict) and isinstance(c.get("name"), str)],
    }, ""


def grade_route(texts: dict[str, str], checklist: dict, g: dict) -> tuple[dict, list[str]]:
    """route (stage 1B item 13; docs/online-1B.md P13): on the server's log (`peer`, default S), the
    route checklist's evidence. The level end of [level_end] (its class, and its recipient when the
    file names one) must have S's first `level end <class> to <recipient> (generation <g>)` line, else
    the case is "not reached" (a run that stopped before the end), and every level end S logged must
    come once per class and recipient. With `next_zone`, that level must open after it as S's own
    level change, come up on S and have its barrier released; how S got past the results screens
    (native or press) is recorded. Every request or two_player trigger of the file must have been
    applied on S (a replay of it that activated), every encounter started and completed, every
    checkpoint's trigger applied (and its `expect` pattern seen), every scene started and ended. No
    client of `clients` may leave the level by itself: its level guard passes only the session's own
    request, a request while not connected, or a joiner's continue from its title, it runs no level end
    of its own, and it played the server's level end of [level_end]'s class and generation exactly once
    (`level end <class> from the server played here (generation <g>, ...`)."""
    server = texts.get(g.get("peer", "S"), "")
    lines = server.splitlines()
    end = checklist["level_end"]
    want_class, want_recipient = end["class"], end.get("recipient")
    firsts = [(i, m.group(1), m.group(2), m.group(3)) for i, line in enumerate(lines)
              if (m := LEVEL_END_FIRST.search(line))]
    duplicates = sum(1 for line in lines if LEVEL_END_DUPLICATE.search(line))
    applied: set[str] = set()
    names: dict[str, str] = {}
    for line in lines:
        if m := REPLAY_LINE.search(line):
            if not m.group(1).startswith("done"):
                names.setdefault(m.group(1), m.group(2))
        elif m := REPLAY_DONE.search(line):
            if m.group(1) in names:
                applied.add(names[m.group(1)])
    started = [m.group(1) for line in lines if (m := ENCOUNTER_START.search(line))]
    completed = [m.group(1) for line in lines if (m := ENCOUNTER_COMPLETE.search(line))]
    scene_keys = {m.group(1): m.group(2) for line in lines if (m := SCENE_START.search(line))}
    scene_ended = {m.group(1) for line in lines if (m := SCENE_END.search(line))}

    def trigger_applied(name: str) -> bool:
        return any(route_name_matches(name, seen) for seen in applied)

    graded: dict = {
        "level_ends": len(firsts), "level_end_duplicates_skipped": duplicates,
        "triggers": [len([t for t in checklist["triggers"] if trigger_applied(t)]), len(checklist["triggers"])],
        "encounters": [len([e for e in checklist["encounters"]
                            if any(route_name_matches(e, x) for x in started)
                            and any(route_name_matches(e, x) for x in completed)]), len(checklist["encounters"])],
        "scenes": [len([n for n in checklist["scenes"]
                        if any(v.casefold() == n.casefold() and k in scene_ended for k, v in scene_keys.items())]),
                   len(checklist["scenes"])],
    }
    ends = [f for f in firsts if f[1] == want_class and (not want_recipient or route_name_matches(want_recipient, f[2]))]
    if not ends:
        seen = ", ".join(f"{k} {v[0]} of {v[1]}" for k, v in graded.items() if isinstance(v, list))
        return graded, [f"grade route: not reached (no `level end {want_class}"
                        f"{' to ' + want_recipient if want_recipient else ''}` line on {g.get('peer', 'S')}; "
                        f"seen before the run stopped: {seen})"]
    at = ends[0][0]
    graded["level_end"] = {"class": ends[0][1], "recipient": ends[0][2], "generation": int(ends[0][3])}
    reasons = []
    counts: dict[tuple[str, str], int] = {}
    for _, cls, recipient, _ in firsts:
        counts[(cls, recipient)] = counts.get((cls, recipient), 0) + 1
    for (cls, recipient), n in counts.items():
        if n > 1:
            reasons.append(f"grade route: level end {cls} to {recipient} {n} times (once per class and recipient)")
    advanced = [m.group(1) for line in lines[at:] if (m := RESULTS_ADVANCED.search(line))]
    graded["results_advanced"] = {"native": advanced.count("native"), "press": advanced.count("press")}
    graded["results_done"] = any(RESULTS_DONE.search(line) for line in lines[at:])
    zone = end.get("next_zone")
    if zone:
        opened = [(m.group(1), m.group(2)) for line in lines[at:] if (m := OWN_LEVEL_OPEN.search(line))
                  and m.group(2).replace("\\", "/").rsplit("/", 1)[-1].casefold() == zone.casefold()]
        if not opened:
            reasons.append(f"grade route: the next level {zone} did not open as the server's own level change after"
                           " the level end")
        else:
            gen = opened[0][0]
            graded["next_level"] = {"zone": zone, "generation": int(gen)}
            up = any(re.search(rf"Online: level {gen}: the server has it up", line) for line in lines[at:])
            released = any(re.search(rf"Online: barrier {gen} released at tick", line) for line in lines[at:])
            graded["next_level"].update({"up": up, "released": released})
            if not up:
                reasons.append(f"grade route: the next level {zone} (generation {gen}) never came up on the server")
            elif not released:
                reasons.append(f"grade route: the next level {zone}'s barrier {gen} was never released")
    for t in checklist["triggers"]:
        if not trigger_applied(t):
            reasons.append(f"grade route: trigger {t} never applied on the server")
    for e in checklist["encounters"]:
        if not any(route_name_matches(e, x) for x in started):
            reasons.append(f"grade route: encounter {e} never started on the server")
        elif not any(route_name_matches(e, x) for x in completed):
            reasons.append(f"grade route: encounter {e} never completed on the server")
    for c in checklist["checkpoints"]:
        if not trigger_applied(c["trigger"]):
            reasons.append(f"grade route: checkpoint {c['trigger']} never applied on the server")
        elif c.get("expect") and not any(re.search(c["expect"], line) for line in lines):
            reasons.append(f"grade route: checkpoint {c['trigger']}: /{c['expect']}/ not on the server")
    for n in checklist["scenes"]:
        keys = [k for k, v in scene_keys.items() if v.casefold() == n.casefold()]
        if not keys:
            reasons.append(f"grade route: scene {n} never started on the server")
        elif not any(k in scene_ended for k in keys):
            reasons.append(f"grade route: scene {n} never ended on the server")
    end_generation = graded["level_end"]["generation"]
    played_line = re.compile(rf"Online: level end {re.escape(want_class)} from the server played here "
                             rf"\(generation {end_generation}, ")
    graded["clients_played"] = {}
    for peer in g.get("clients", [p for p in texts if p != "S"]):
        played = sum(1 for line in texts.get(peer, "").splitlines() if played_line.search(line))
        graded["clients_played"][peer] = played
        if played != 1:
            reasons.append(f"grade route: client {peer} played the server's level end {want_class} of generation"
                           f" {end_generation} {played} times (exactly once)")
        for line in texts.get(peer, "").splitlines():
            if (m := CLIENT_PASSED.search(line)) and not m.group(3).startswith(CLIENT_PASS_OK):
                reasons.append(f"grade route: client {peer} left the level by itself ({m.group(2)}: {m.group(3)})")
                break
            if m := CLIENT_NATIVE_END.search(line):
                reasons.append(f"grade route: client {peer} ran its own level end ({m.group(1)} to {m.group(2)})")
                break
    if reasons:
        graded["violation"] = True
    return graded, reasons


def check_bandwidth(spec_name: str, g: object, names: list[str]) -> None:
    """A bandwidth grade's options: `peer` (the log with the per-peer traffic lines, default S),
    `statistic` (p50, p95 or max over the 10 s windows; default p95), `after` and `until` (patterns
    bounding the windows), `min_windows` (a whole number >= 1, default 3)."""
    if not isinstance(g, dict):
        raise SpecError(f"{spec_name}: grade bandwidth must be a table")
    if g.get("peer", "S") not in names:
        raise SpecError(f"{spec_name}: grade bandwidth: `peer` {g.get('peer')!r} is not in {names}")
    if g.get("statistic", "p95") not in ("p50", "p95", "max"):
        raise SpecError(f"{spec_name}: grade bandwidth: statistic is p50, p95 or max")
    n = g.get("min_windows", 3)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise SpecError(f"{spec_name}: grade bandwidth: min_windows must be a whole number >= 1")
    for key in ("after", "until"):
        if key in g:
            try:
                re.compile(g[key])
            except (re.error, TypeError) as e:
                raise SpecError(f"{spec_name}: grade bandwidth: bad `{key}` pattern: {e}") from e


def grade_bandwidth(lines: list[tuple[float | None, str]], g: dict, thresholds: dict) -> tuple[dict, list[str]]:
    """bandwidth (stage 1B item 13; docs/online-1B.md section 5, `bandwidth-tutorial_b`): from the
    server's per-peer traffic lines (`traffic peer <p> gameplay <sent>/<received> state ... fast ...
    bytes`, cumulative, every 10 s), each client's rate per window between two lines: up is what the
    server received from it, down what it sent to it, all lanes, in kilobytes (1000 bytes) per second.
    Each client's `statistic` (p95 by default) must be within tests/thresholds.toml
    [bandwidth_per_client]: up at most up_kilobytes_per_s, down at most steady_down_kilobytes_per_s
    (1B measures the pose and request floor: no enemy records yet). Windows start after `after` and
    end at `until` (both optional); a counter that went back (a new session) starts over. A client with
    fewer than `min_windows` windows, or no traffic line at all, is "not reached"."""
    t = thresholds.get("bandwidth_per_client", {})
    up_limit = float(t.get("up_kilobytes_per_s", 4.0))
    down_limit = float(t.get("steady_down_kilobytes_per_s", 20.0))
    statistic = g.get("statistic", "p95")
    min_windows = int(g.get("min_windows", 3))
    after = re.compile(g["after"]) if g.get("after") else None
    until = re.compile(g["until"]) if g.get("until") else None
    started = after is None
    last: dict[str, tuple[float, int, int]] = {}
    windows: dict[str, list[tuple[float, float]]] = {}
    for when, line in lines:
        if not started:
            started = bool(after.search(line))
            continue
        if until and until.search(line):
            break
        m = TRAFFIC_PEER.search(line)
        if not m or when is None:
            continue
        peer = m.group(1)
        down = int(m.group(2)) + int(m.group(4)) + int(m.group(6))
        up = int(m.group(3)) + int(m.group(5)) + int(m.group(7))
        prev = last.get(peer)
        last[peer] = (when, up, down)
        if prev is None or when <= prev[0] or up < prev[1] or down < prev[2]:
            continue
        dt = when - prev[0]
        windows.setdefault(peer, []).append(((up - prev[1]) / dt / 1000.0, (down - prev[2]) / dt / 1000.0))

    def stat(values: list[float]) -> float:
        return max(values) if statistic == "max" else percentile(values, 50 if statistic == "p50" else 95)

    graded: dict = {"statistic": statistic, "up_limit_kbps": up_limit, "down_limit_kbps": down_limit, "peers": {}}
    reasons = []
    if not last:
        return graded, ["grade bandwidth: not reached (no `traffic peer` line in the window)"]
    for peer in sorted(last):
        w = windows.get(peer, [])
        if len(w) < min_windows:
            reasons.append(f"grade bandwidth: not reached (peer {peer}: {len(w)} window(s), {min_windows} needed)")
            continue
        ups, downs = [x[0] for x in w], [x[1] for x in w]
        rec = {"windows": len(w), "up_kbps": round(stat(ups), 3), "down_kbps": round(stat(downs), 3),
               "up_max_kbps": round(max(ups), 3), "down_max_kbps": round(max(downs), 3)}
        graded["peers"][peer] = rec
        if rec["up_kbps"] > up_limit:
            reasons.append(f"grade bandwidth: peer {peer} up {rec['up_kbps']:.2f} KB/s ({statistic}) over {up_limit} KB/s")
            graded["violation"] = True
        if rec["down_kbps"] > down_limit:
            reasons.append(f"grade bandwidth: peer {peer} down {rec['down_kbps']:.2f} KB/s ({statistic}) over"
                           f" {down_limit} KB/s")
            graded["violation"] = True
    return graded, reasons


def grade_cross_peer(spec: Spec, r: PeerResult, texts: dict[str, str], repo: Path | None = None,
                     thresholds: dict | None = None) -> list[str]:
    """The grades a peer's result carries across peers: requests_answered on each peer it names (S's
    log gives the peers gone at close), symmetry on its peer (S) (item 9); route on its peer (S), with
    the route file from the game checkout `repo`, and bandwidth on its peer (S) (item 13)."""
    reasons: list[str] = []
    for gname, g in spec.grades.items():
        if gname == "requests_answered" and r.peer in g.get("peers", [n for n in spec.peers if n != "S"]):
            # Only S's log holds other peers' keys whose finals could not reach them.
            gone = set(GONE_AT_CLOSE.findall(texts.get("S", ""))) if r.peer == "S" else set()
            r.graded[gname], why = grade_requests_answered(texts.get(r.peer, ""), gone)
            reasons += why
        elif gname == "symmetry" and g.get("peer", "S") == r.peer:
            r.graded[gname], why = grade_symmetry(texts.get(r.peer, ""), g)
            reasons += why
        elif gname == "route" and g.get("peer", "S") == r.peer:
            checklist, why_not = load_route_checklist((repo or Path(".")) / g["file"])
            if checklist is None:
                r.graded[gname] = {}
                reasons.append(f"grade route: {why_not}")
                continue
            r.graded[gname], why = grade_route(texts, checklist, g)
            reasons += why
        elif gname == "bandwidth" and g.get("peer", "S") == r.peer:
            r.graded[gname], why = grade_bandwidth(parse_log(texts.get(r.peer, ""))["lines"], g, thresholds or {})
            reasons += why
    return reasons


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
    rate_floor = c.get("rate_floor", DEFAULT_RATE_FLOOR)
    if not isinstance(rate_floor, (int, float)) or isinstance(rate_floor, bool) or rate_floor < 0:
        raise bad("`rate_floor` is a share of the A/A rate >= 0 (0.01: 1%)")
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


def make_run_dir(cfg: "Config", path: Path) -> Path:
    """Create a fresh run folder (fresh_dir) and return it. Two runners started in the same second
    (parallel playtesters, P1m) can pick the same name between fresh_dir's check and the mkdir: the
    one that loses the mkdir takes the next suffix instead of failing with FileExistsError."""
    for _ in range(64):
        candidate = fresh_dir(path)
        check_output_path(cfg, candidate)
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue
        return candidate
    raise SystemExit(f"no free run folder next to {path} (64 tries)")


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
    stopped_by: str = ""  # ended by the runner at a fail-fast stop (CaseResult.stopped_early), not a timeout
    startup_stall_s: float | None = None  # shader storage -> the guest's first line (startup_stall)
    power_throttling: str = ""  # the runner's opt-out for this game (opt_out_of_power_throttling)


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
    # Fail fast: `stopped early: <peer> <line> (fail fast)` when the runner stopped the run's games
    # after a fatal line (a compared case: the first of its runs that stopped).
    stopped_early: str = ""
    # `test --jobs N`: the cases whose runs overlapped this one's (any part of it), so a failure can be
    # tied to load (report.md's With column); empty when it ran alone. `exclusive`: the spec's key.
    ran_with: list[str] = field(default_factory=list)
    exclusive: bool = False


def crash_exit_code(code: int | None) -> bool:
    """A Windows exit code that is an exception status (0x8xxxxxxx or 0xCxxxxxxx: an access violation
    0xC0000005, a stack overrun 0xC0000409, a breakpoint 0x80000003), not a plain exit code."""
    if code is None:
        return False
    c = code & 0xFFFFFFFF
    return 0xC0000000 <= c < 0xD0000000 or 0x80000000 <= c < 0x90000000


class LogTail:
    """The lines a game appended to its log since the last read: each read takes only the new bytes,
    keeps an unfinished last line for the next read, and starts over when the log was replaced (it got
    shorter, is another file, or is gone: a hosted server's log, which each Host's new session folder
    replaces). `generation` counts those replacements. A log that does not exist yet (a hosted
    server's, before the session) has none. The file is opened only when it grew: a log that no longer
    grows (a server that exited) is never held open while Host deletes the session folder."""

    def __init__(self, path: Path) -> None:
        self.path, self.offset, self.rest = path, 0, b""
        self.ident: tuple[int, int] | None = None
        self.generation = 0

    def _restart(self) -> None:
        self.offset, self.rest, self.generation = 0, b"", self.generation + 1

    def size(self) -> int | None:
        """The log's size now (None while there is none), after noting a replacement."""
        try:
            st = os.stat(self.path)
        except OSError:
            if self.ident is not None or self.offset:
                self.ident = None
                self._restart()
            return None
        ident = (st.st_dev, st.st_ino) if st.st_ino else None
        if st.st_size < self.offset or (ident and self.ident and ident != self.ident):
            self._restart()
        self.ident = ident or self.ident
        return st.st_size

    def new_lines(self) -> list[str]:
        end = self.size()
        if end is None or end == self.offset:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(end - self.offset)
        except OSError:
            return []
        self.offset += len(data)
        *whole, self.rest = (self.rest + data).split(b"\n")
        return [line.decode("utf-8", errors="replace").rstrip("\r") for line in whole]


class FailFast:
    """Fail fast's watch over one run: each peer's log (LogTail) against that peer's fatal patterns
    (its `forbid` and ALWAYS_FORBIDDEN), and the started games' exit codes. `hit` is the first fatal
    finding, (peer, what), and `hit_at` its time.monotonic(). A crash exit stays. A line stays while
    its log does: when the log is replaced before the stop (a hosted server's, by Host's next
    session), the line is no longer in the log that is graded, so the hit is dropped and the watch
    goes on."""

    def __init__(self, logs: dict[str, Path], patterns: dict[str, list[str]]) -> None:
        self.tails = {n: LogTail(p) for n, p in logs.items()}
        self.patterns = {n: [re.compile(rx, re.M) for rx in patterns.get(n, [])] for n in logs}
        self.hit: tuple[str, str] | None = None
        self.hit_at = 0.0
        self._hit_generation: int | None = None  # the hit line's log generation; None for an exit code

    def _found(self, hit: tuple[str, str], generation: int | None) -> tuple[str, str]:
        self.hit, self.hit_at, self._hit_generation = hit, time.monotonic(), generation
        return hit

    def check(self, procs: dict[str, subprocess.Popen] | None = None) -> tuple[str, str] | None:
        if self.hit is not None:
            tail = self.tails[self.hit[0]] if self._hit_generation is not None else None
            if tail is None:
                return self.hit
            tail.size()
            if tail.generation == self._hit_generation:
                return self.hit
            self.hit = self._hit_generation = None
        for n, tail in self.tails.items():
            for line in tail.new_lines():
                if any(rx.search(line) for rx in self.patterns[n]):
                    return self._found((n, line.strip()[:200]), tail.generation)
        for n, p in (procs or {}).items():
            if crash_exit_code(code := p.poll()):
                return self._found((n, f"exit code 0x{code & 0xFFFFFFFF:08X}"), None)
        return None


def fail_fast_patterns(spec: Spec, peer: str) -> list[str]:
    """A peer's fatal lines for fail fast: exactly the forbidden lines grade_peer fails it on."""
    ps = spec.peers.get(peer)
    return (ps.forbid if ps else []) + ALWAYS_FORBIDDEN


def peer_script(spec: Spec, peer: str, figures: Path, join: str = "") -> str:
    if peer == "S":
        return STANDIN_SERVER_SCRIPT.format(seconds=int(spec.standin_seconds))
    return spec.peers[peer].script.replace("{figures}", str(figures)).replace("{peer}", peer).replace("{join}", join)


def peer_args(cfg: Config, args: list[str]) -> list[str]:
    """A peer's spec args as the game gets them: {repo} is the checkout under test (G3, stage 1B)."""
    return [a.replace("{repo}", cfg.repo.as_posix()) for a in args]


# The ports this runner process handed to hosted servers: never one twice, so cases run at once
# (`test --jobs N`) never share one, also before their server binds it (Host boots for a while
# first). Host names its control pipe itself, after its own process id and a random nonce
# (game: src/online/server/control_pipe.cpp ControlPipe::Create), so pipes never collide.
_PORTS_GIVEN: set[int] = set()
_PORTS_LOCK = threading.Lock()
PORT_TRIES = 64


def free_loopback_port() -> int:
    """A port nothing listens on now and that this runner has not handed out yet, for a hosted server
    (its owner passes it on). OSError when Windows keeps returning ports already handed out."""
    with _PORTS_LOCK:
        for _ in range(PORT_TRIES):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            if port not in _PORTS_GIVEN:
                _PORTS_GIVEN.add(port)
                return port
    raise OSError(f"no free loopback port this run has not handed out already ({PORT_TRIES} tries)")


class StartPacer:
    """The start gap ([defaults] start_gap_seconds) between any two games the runner starts, across
    every case running at once (`test --jobs N`): each start waits until `gap` seconds after the one
    before, whichever case it was in. `wait_turn` calls `idle` while it waits (the caller watches the
    games it already started) and takes the turn when it returns; `starts` keeps each turn's time."""

    def __init__(self, gap: float) -> None:
        self.gap = max(0.0, float(gap))
        self.lock = threading.Lock()
        self.last: float | None = None
        self.starts: list[float] = []

    def wait_turn(self, idle=lambda: None) -> float:
        while True:
            with self.lock:
                now = time.monotonic()
                if self.last is None or now >= self.last + self.gap:
                    self.last = now
                    self.starts.append(now)
                    return now
                left = self.last + self.gap - now
            idle()
            time.sleep(min(0.2, max(0.01, left)))


# --- the runner's environment: its token, the games' power throttling ------------------------------
#
# A runner started under a restricted token (an agent sandbox) starts its games with that token too.
# They then cannot use the GPU shader caches in the user profile (%LOCALAPPDATA%\D3DSCache and the
# driver's cache allow only SYSTEM, Administrators and the user) and compile every pipeline cold
# before the guest starts: 4-32 s per start, more under load (P1j's startup stall). Their load and
# frame times are then not comparable with other runs, so `test` refuses such a token unless
# --allow-restricted.
WINDOWS = os.name == "nt"
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25  # TOKEN_INFORMATION_CLASS TokenIntegrityLevel
TOKEN_IS_APP_CONTAINER = 29  # TOKEN_INFORMATION_CLASS TokenIsAppContainer
MEDIUM_INTEGRITY_RID = 0x2000  # SECURITY_MANDATORY_MEDIUM_RID; low is 0x1000, untrusted 0
_WIN: dict = {}


def _win() -> tuple:
    """advapi32 and kernel32 with the argument types the token check and the throttling opt-out use."""
    if not _WIN:
        import ctypes
        from ctypes import wintypes

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.IsTokenRestricted.argtypes = [wintypes.HANDLE]
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        _WIN.update(ctypes=ctypes, wintypes=wintypes, adv=adv, k32=k32)
    return _WIN["ctypes"], _WIN["wintypes"], _WIN["adv"], _WIN["k32"]


def token_handle_restriction(token: int) -> str:
    """Why a token (a handle with TOKEN_QUERY) is restricted, '' when it is not: restricting SIDs
    (IsTokenRestricted, as a sandbox's token has), an AppContainer token, or an integrity level below
    medium. Each cause is checked on its own; one that cannot be read counts as absent."""
    ctypes, wintypes, adv, _ = _win()
    causes = []
    if adv.IsTokenRestricted(token):
        causes.append("a restricted token (IsTokenRestricted)")
    flag, size = wintypes.DWORD(), wintypes.DWORD()
    if adv.GetTokenInformation(token, TOKEN_IS_APP_CONTAINER, ctypes.byref(flag), ctypes.sizeof(flag),
                               ctypes.byref(size)) and flag.value:
        causes.append("an AppContainer token")
    adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
    if size.value:
        buf = ctypes.create_string_buffer(size.value)
        if adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_MANDATORY_LABEL.Label.Sid
            rid = adv.GetSidSubAuthority(sid, adv.GetSidSubAuthorityCount(sid)[0] - 1)[0]
            if rid < MEDIUM_INTEGRITY_RID:
                causes.append(f"a low integrity level (0x{rid:04X})")
    return "; ".join(causes)


def token_restriction() -> str:
    """Why the runner's own process token is restricted ('' when it is not, and off Windows); its
    games inherit it. Raises OSError when the token cannot be opened. The tests replace it."""
    if not WINDOWS:
        return ""
    ctypes, wintypes, adv, k32 = _win()
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        return token_handle_restriction(token)
    finally:
        k32.CloseHandle(token)


def token_preflight(allow: bool) -> dict:
    """`test`'s check before any game starts: {"restricted": True, False or None (not checked),
    "cause": ...}. A restricted token is refused with one line (SystemExit) unless `allow`; a check
    that cannot run is printed and the run goes on."""
    try:
        cause = token_restriction()
    except Exception as e:  # noqa: BLE001 - an unreadable token is reported, not fatal
        why = f"{type(e).__name__}: {e}"
        print(f"skytest: could not check this runner's token ({why}); the games start, a restricted one "
              "would show as a startup stall", flush=True)
        return {"restricted": None, "cause": f"not checked: {why}"}
    if not cause:
        return {"restricted": False, "cause": ""}
    if not allow:
        raise SystemExit(f"skytest: refused to start games: this runner has {cause}, as in an agent sandbox, so its "
                         "games could not use the GPU shader caches and would compile every pipeline cold at each "
                         "start (4-32 s); run skytest outside the sandbox, or pass --allow-restricted to run anyway "
                         "(timings then not comparable)")
    print(f"skytest: WARNING: running with {cause} (--allow-restricted): the games compile their pipelines cold, "
          "their timings are not comparable with other runs", flush=True)
    return {"restricted": True, "cause": cause}


def token_report_line(token: dict) -> str:
    """report.md's line on the runner's token (token_preflight's result)."""
    if token.get("restricted") is None:
        return f"Token: not checked ({token.get('cause', '').removeprefix('not checked: ')})."
    if token["restricted"]:
        return (f"Token: RESTRICTED ({token['cause']}); started with --allow-restricted: the games could not use "
                "the GPU shader caches, so this run's timings are not comparable with other runs.")
    return "Token: not restricted."


# Every game the runner starts opts out of Windows power throttling (EcoQoS), as the server does for
# itself (game: src/online/server/server_role.cpp OptOutOfPowerThrottling): a busy machine (a parallel
# build) otherwise slowed the test clients' loads for minutes while S, opted out, kept its pace
# (stage 1B, level-guard-not-owner). PROCESS_POWER_THROTTLING_STATE is {Version, ControlMask,
# StateMask}: a control bit with its state bit clear turns that throttling off for the process.
PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS ProcessPowerThrottling
POWER_THROTTLING_VERSION = 1  # PROCESS_POWER_THROTTLING_CURRENT_VERSION
POWER_THROTTLING_EXECUTION_SPEED = 0x1
POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
_THROTTLING_LOGGED: list[str] = []  # the one failure line printed in this runner process


def set_process_information(handle: int, info_class: int, state: tuple[int, int, int]) -> int:
    """kernel32 SetProcessInformation with a PROCESS_POWER_THROTTLING_STATE (Version, ControlMask,
    StateMask): 0 when Windows took it, else its error code. The tests replace it."""
    ctypes, wintypes, _, k32 = _win()
    s = (wintypes.ULONG * 3)(*state)
    if k32.SetProcessInformation(handle, info_class, ctypes.byref(s), ctypes.sizeof(s)):
        return 0
    return ctypes.get_last_error() or -1


def opt_out_of_power_throttling(handle: int) -> str:
    """Opts a started game (its process handle) out of power throttling: the execution speed and the
    timer resolution, else the execution speed alone (a Windows without the timer flag). Returns what
    took; a failure is printed once per runner process and never stops a run."""
    if not WINDOWS:
        return "not Windows"
    try:
        first = set_process_information(handle, PROCESS_POWER_THROTTLING, (
            POWER_THROTTLING_VERSION,
            POWER_THROTTLING_EXECUTION_SPEED | POWER_THROTTLING_IGNORE_TIMER_RESOLUTION, 0))
        if first == 0:
            return "opted out"
        second = set_process_information(handle, PROCESS_POWER_THROTTLING, (
            POWER_THROTTLING_VERSION, POWER_THROTTLING_EXECUTION_SPEED, 0))
        if second == 0:
            return f"opted out (execution speed only; the timer resolution flag was refused, error {first})"
        why = f"Windows error {second}"
    except Exception as e:  # noqa: BLE001 - never stops a run
        why = f"{type(e).__name__}: {e}"
    if not _THROTTLING_LOGGED:
        _THROTTLING_LOGGED.append(why)
        print(f"skytest: could not opt a game out of Windows power throttling ({why}); the run goes on, "
              "a busy machine may slow its games", flush=True)
    return f"failed: {why}"


# The game's test-only setting that shows the online server's window (src/online/server/server_role.cpp):
# on a server it keeps window_hidden false; on the owner's game Host passes it to the server it starts.
SHOW_SERVER_SETTING = "dev_show_server_window"


def show_server_args(cfg: Config, spec: Spec, peer: str) -> list[str]:
    """How a peer's command line hides or shows the server's window. Hidden (no key anywhere): the
    standalone S gets --window_hidden=true. Shown (the spec's show_server_window, else [defaults]):
    a standalone S gets --dev_show_server_window=true instead, and in a hosted layout H gets it (Host
    starts its server with it)."""
    show = spec.show_server_window if spec.show_server_window is not None else cfg.show_server_window
    if peer == "S":
        return [f"--{SHOW_SERVER_SETTING}=true"] if show else ["--window_hidden=true"]
    if peer == "H" and show and spec.server == "host":
        return [f"--{SHOW_SERVER_SETTING}=true"]
    return []


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
        *show_server_args(cfg, spec, peer),
        *([f"--online_port={port}"] if port and peer == "H" else []),
        *peer_args(cfg, spec.peers[peer].args),
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
    out["startup_stall"] = startup_stall(out["lines"])
    return out


def startup_stall(lines: list[tuple[float | None, str]]) -> float | None:
    """Seconds from the log's "Initializing shader storage" line to the guest's first line after it
    (GUEST_FIRST_LINE); None when the log lacks either."""
    t0 = None
    for t, line in lines:
        if t is None:
            continue
        if t0 is None:
            if SHADER_STORAGE.search(line):
                t0 = t
        elif GUEST_FIRST_LINE.search(line):
            return round(t - t0, 2)
    return None


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
        if gname in ("requests_answered", "symmetry", "save_copy", "route", "bandwidth"):
            continue  # graded across peers (grade_cross_peer, grade_save_copy)
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
        elif gname == "press_to_reaction":
            r.graded[gname], why = grade_press_to_reaction(after, g, thresholds)
            reasons += why
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


def peer_log_path(run: Path, peer: str, hosted: bool) -> Path:
    """Where a peer writes its log in a run folder: a hosted server's is in its owner's session folder."""
    return run / "H" / HOSTED_SERVER_LOG if hosted and peer == "S" else run / peer / "game.log"


def read_peer_log(run: Path, peer: str, hosted: bool) -> str:
    """A peer's log in a run folder: a hosted server's is in its owner's session folder."""
    log = peer_log_path(run, peer, hosted)
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
    if r.ended_by_runner and r.stopped_by:
        reasons.append(f"ended by the runner: {r.stopped_by}")
    elif r.ended_by_runner:
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
             accepted_grades: tuple[str, ...] = (), folder: Path | None = None,
             pacer: StartPacer | None = None) -> CaseResult:
    """One run of a case. `folder`: where the run goes (a compared case's run, inside the case's
    folder); by default a new `<stamp>-<case>` folder in the store's runs. `pacer`: the start gap
    shared with the cases running beside it (`test --jobs N`); by default this case's own."""
    pacer = pacer if pacer is not None else StartPacer(cfg.start_gap)
    names = list(spec.peers)
    hosted = spec.server == "host"
    # A hosted server is started by its owner H, not by the runner (it is still one of the games).
    starts = [n for n in names if not (hosted and n == "S")]
    res = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, started=stamp)
    if len(names) > max_games:
        res.status = "capacity"
        res.reasons = [f"needs {len(names)} games at once, this run allows {max_games} (--max-games)"]
        return res
    run = make_run_dir(cfg, folder if folder is not None else cfg.store / "runs" / f"{stamp}-{spec.name}")
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
            for n in starts:
                # Games booting in the same instant crashed and froze each other in a test
                # (stage 0, five at once); real sessions never start them together either. The gap
                # holds across every case running at once (StartPacer); the games already started
                # are watched meanwhile.
                pacer.wait_turn(note_ends)
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
                    # Before it runs: no power throttling (a failure is printed once, never fatal).
                    results[n].power_throttling = opt_out_of_power_throttling(int(getattr(p, "_handle", 0) or 0))
                    winjob.resume(p)
                except BaseException:
                    p.kill()
                    p.wait(timeout=30)
                    raise
                procs[n] = p
                started[n] = time.monotonic()
                results[n].started_after_s = round(started[n] - t_start, 1)
            deadline = time.monotonic() + spec.timeout
            # Fail fast: the first fatal line (or crash exit) moves the stop to a grace after it.
            watch = FailFast({n: peer_log_path(run, n, hosted) for n in names},
                             {n: fail_fast_patterns(spec, n) for n in names}) if spec.fail_fast else None
            stop_at, next_check, early = deadline, 0.0, False
            while len(ended) < len(procs) and time.monotonic() < stop_at:
                note_ends()
                if watch is not None and time.monotonic() >= next_check:
                    next_check = time.monotonic() + FAIL_FAST_POLL
                    # Checked through the grace too: a hit whose log was replaced meanwhile is dropped.
                    stop_at = min(deadline, watch.hit_at + cfg.fail_fast_grace) if watch.check(procs) else deadline
                    early = stop_at < deadline  # else the timeout comes first: a timeout as before
                time.sleep(min(0.5, max(0.0, stop_at - time.monotonic())))
            stopped = ""
            if early and watch is not None and watch.hit and any(p.poll() is None for p in procs.values()):
                stopped = f"stopped early: {watch.hit[0]} {watch.hit[1]} (fail fast)"
                res.stopped_early = stopped
            for n, p in procs.items():
                if p.poll() is None:
                    p.kill()
                    results[n].ended_by_runner = True
                    results[n].stopped_by = stopped
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
    texts = {n: read_peer_log(run, n, hosted) for n in names}
    for n in names:
        text = texts[n]
        parsed = parse_log(text)
        r = results[n]
        r.build, r.levels, r.fps, r.longest_frame_ms = parsed["build"], parsed["levels"], parsed["fps"], parsed["longest"]
        r.peak_private_mb, r.peak_working_set_mb, r.errors = parsed["private"], parsed["working"], parsed["errors"]
        r.frame_p95_ms, r.peak_video_mb = parsed["frame_p95"], parsed["video"]
        r.startup_stall_s = parsed["startup_stall"]
        grade_peer(spec, r, text, parsed)
        if spec.grades:
            limit = frame_rate_limit(r.command, run / n / "settings.toml")
            extra = grade_thresholds(spec, r, parsed, thresholds or {}, limit, baseline)
            if n in saves_before:
                extra += grade_save_copy(r, saves_before[n], saves_after[n], save_title)
            extra += grade_cross_peer(spec, r, texts, cfg.repo, thresholds or {})
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
    if res.stopped_early:
        res.reasons.append(res.stopped_early)
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
#     trace lines' running numbers (#n, the first 20 and every 500th: the highest seen). A sampled
#     count under 20 is exact; one of 20 or more says only that the true count lies between it and
#     the next logged number (one sampling step), so such a key passes when the runs agree within
#     one step on top of the A/A spread (sampled_range, counts_within_noise);
#   - the per-frame trigger keys (RATE_EVENTS: act and touch) as rates instead: the window's count
#     divided by the server frames in the window (the swaps of the `GPU: draws` lines, prorated to
#     the window: window_frames), or by its seconds when a run has no GPU line in it. A test run's
#     rate must lie within the A/A rates widened by their spread plus `rate_floor` (default 0.01) of
#     the mean A/A rate; a sampled count gives a range of rates (rates_within_noise). Enter, exit,
#     de-act, messages and telegrams stay counts;
#   - the load times: a test run no slower than the slowest A/A run plus the A/A spread (at least
#     `load_floor_s`); faster passes (the skip saves GPU work). They start at the guest's start, not
#     at the log's first line (`guest start -> hub`), and a level the session opened before the
#     game asked for it at its request line (`request -> server up`, `request -> barrier`; else
#     `open -> ...`): the waits before them are startup stalls, not loads (P1j C2);
#   - the GPU counters: 0 resolve readbacks in every run, and 0 memexport draws (draw_skip.h: the
#     skip is trusted only while memexport is 0) unless `memexport_zero = false`.
# A run without its `reached` evidence makes the comparison "not reached": a failure, never a pass.


def is_world_message_key(key: str) -> bool:
    """A trace_world_messages count key (traces.cpp: "message <class>", "telegram <class>",
    "trigger <event> <trigger>"); trace_telegrams' "telegram send ..." and "telegram list ..." are not."""
    if key.startswith(("telegram send ", "telegram list ")):
        return False
    return key.startswith(("message ", "telegram ", "trigger "))


def is_rate_key(key: str) -> bool:
    """A per-frame trigger key ("trigger act <Name#id>", "trigger touch <Name#id>"), graded as a rate."""
    return key.startswith(tuple(f"trigger {e} " for e in RATE_EVENTS))


def window_frames(lines: list[tuple[float | None, str]], t0: float, t1: float) -> float | None:
    """The server frames in the window [t0, t1] from the `GPU: draws` lines: each line counts the
    swaps of the `in <s> s` before its time stamp, and a line that covers the window only in part
    counts that part's share. A part of the window no line covers (the log ends before the next
    line) is filled at the covered part's swap rate. None without a line in the window."""
    swaps = covered = 0.0
    for t, line in lines:
        if t is None or not (m := GPU_COUNTERS.search(line)):
            continue
        s = float(m.group(10))
        overlap = min(t, t1) - max(t - s, t0)
        if s > 0 and overlap > 0:
            swaps += int(m.group(4)) * overlap / s
            covered += overlap
    if covered <= 0 or t1 <= t0:
        return None
    return round(swaps * (t1 - t0) / covered, 1)


def trace_dumps(texts: list[str]) -> list[tuple[int, dict[str, int]]]:
    """Each `Trace counts: <k> keys` block: its line index and every `Trace count: <key> <n>` after it."""
    dumps: list[tuple[int, dict[str, int]]] = []
    for i, line in enumerate(texts):
        if TRACE_COUNTS.search(line):
            dumps.append((i, {}))
        elif dumps and (m := TRACE_COUNT.search(line)):
            dumps[-1][1][m.group(1)] = int(m.group(2))
    return dumps


# A level load key from compare_keys: (level <g> <zone>), its anchor, what it ends at.
LOAD_ANCHOR = re.compile(r"^(level \S+ .+): (request|open) -> (server up|barrier)$")


def level_span_start(lines: list[tuple[float | None, str]], i: int, g: str, zone: str) -> tuple[float, str]:
    """Where level g's load spans start, for its open line at lines[i]: at the peer's own `Level watch:
    <zone>: requested by the game` when that comes after the open (and before the level is up or the
    next level opens): ("request" and its time). The session can open a level before the game asks
    for it, e.g. the boot hub while the server still starts up, or the owner's continue that reaches S
    before its guest runs (P1j C2): that wait is not the load's. Else the open line: ("open")."""
    t_open = lines[i][0]
    leaf = zone.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for t, line in lines[i + 1:]:
        if (u := LEVEL_UP.search(line)) and u.group(1) == g or LEVEL_OPEN.search(line):
            break
        if (t is not None and (w := LEVEL_WATCH_STEP.search(line)) and w.group(2) == "requested by the game"
                and w.group(1).casefold() == leaf):
            return t, "request"
    return t_open, "open"


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
    # The guest's start (GUEST_START; the log's first line without it): the host's startup before it
    # can stall for seconds whatever the workload (P1j C2), so the spans do not count it.
    t_guest = next((t for t, line in lines if t is not None and GUEST_START.search(line)), t_first)
    for i, (t, line) in enumerate(lines):
        if t is None:
            continue
        if SERVER_HUB.search(line) and t_guest is not None and "guest start -> hub" not in loads:
            loads["guest start -> hub"] = round(t - t_guest, 3)
        if m := LEVEL_OPEN.search(line):
            g, zone = m.groups()
            t0, anchor = level_span_start(lines, i, g, zone)
            up, bar = f"level {g} {zone}: {anchor} -> server up", f"level {g} {zone}: {anchor} -> barrier"
            for t2, later in lines[i + 1:]:
                if t2 is None:
                    continue
                if up not in loads and (u := LEVEL_UP.search(later)) and u.group(1) == g:
                    loads[up] = round(t2 - t0, 3)
                if bar not in loads and (b := BARRIER.search(later)) and b.group(1) == g:
                    loads[bar] = round(t2 - t0, 3)
                if up in loads and bar in loads:
                    break
        if m := LEVEL_WATCH_LOAD.search(line):
            key, n = f"level watch {m.group(1)}: load call", 1
            while key in loads:
                n += 1
                key = f"level watch {m.group(1)}: load call #{n}"
            loads[key] = int(m.group(2)) / 1000.0

    messages: dict = {"mode": "none", "keys": {}, "window_s": None, "window": False, "seconds": None,
                      "frames": None}
    window = re.compile(c.get("window", DEFAULT_WINDOW))
    start = max((i for i, line in enumerate(texts) if window.search(line)), default=None)
    if start is not None:
        end = len(texts)
        if c.get("window_end"):
            end_rx = re.compile(c["window_end"])
            end = next((k for k in range(start + 1, len(texts)) if end_rx.search(texts[k])), len(texts))
        after = [d for d in trace_dumps(texts) if start < d[0] < end]
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
        # The rate keys' denominators: the window's seconds and its server frames (window_frames).
        seconds = round(ts[-1] - ts[0], 3) if len(ts) >= 2 else 0.0
        messages = {"mode": mode, "keys": keys, "window": True,
                    "window_s": round(ts[-1] - ts[0], 1) if len(ts) >= 2 else 0.0, "seconds": seconds,
                    "frames": window_frames(lines, ts[0], ts[-1]) if len(ts) >= 2 else None}

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


def sampled_range(n: int) -> tuple[int, int]:
    """What a sampled count (the highest #n seen) says about the true count: exact under the first
    TRACE_FIRST_LINES (each of them is logged), else from n up to the next logged number (one step)."""
    if n < TRACE_FIRST_LINES:
        return n, n
    return n, (n // TRACE_EVERY + 1) * TRACE_EVERY


def counts_within_noise(noise: list[int], value: int, sampled: bool) -> tuple[bool, int]:
    """Whether a world-message count is within the A/A noise: the A/A range widened by its spread.
    Sampled counts are ranges (sampled_range): the test run's range has to meet the A/A's widened
    range, so readings one sampling step apart pass. Returns the verdict and the step used (0: exact)."""
    if not sampled:
        return within_noise(noise, value, 0.0, False)[0], 0
    spread = max(noise) - min(noise)
    lo, hi = min(noise), max(sampled_range(n)[1] for n in noise)
    tlo, thi = sampled_range(value)
    step = max(r[1] - r[0] for r in map(sampled_range, noise + [value]))
    return tlo <= hi + spread and thi >= lo - spread, step


def rates_within_noise(noise: list[tuple[float, float]], value: tuple[float, float],
                       floor: float) -> tuple[bool, float]:
    """Whether a rate key's rate is within the A/A noise. Each rate is a range (low, high): a point
    for an exact count, the count's sampling step divided by the frames for a sampled one. The A/A's
    range widened by its spread (of the low ends) plus `floor` times the mean A/A rate has to meet the
    test run's range. Returns the verdict and the tolerance."""
    lows = [r[0] for r in noise]
    lo, hi = min(lows), max(r[1] for r in noise)
    tol = (max(lows) - lo) + floor * sum(lows) / len(lows)
    return value[0] <= hi + tol and value[1] >= lo - tol, tol


def rate_text(r: tuple[float, float] | list[float]) -> str:
    """A rate (low, high) for the report: one number, or low-high for a sampled count's range."""
    return f"{r[0]:.4f}" if r[0] == r[1] else f"{r[0]:.4f}-{r[1]:.4f}"


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
        sampled = modes[ref] == "sampled"
        all_keys = sorted(set().union(*(keys[n]["messages"]["keys"] for n in names)))
        for k in [k for k in all_keys if not is_rate_key(k)]:
            nv = [keys[n]["messages"]["keys"].get(k, 0) for n in noise]
            entry = {"noise": nv, "tolerance": max(nv) - min(nv), "step": 0, "test": {}, "ok": True,
                     "within_step": False}
            for n in test:
                v = keys[n]["messages"]["keys"].get(k, 0)
                entry["test"][n] = v
                ok, step = counts_within_noise(nv, v, sampled)
                entry["step"] = max(entry["step"], step)
                if not ok:
                    entry["ok"] = False
                    bad.append(f"{k} {n} {v} (A/A {'/'.join(str(x) for x in nv)}"
                               + (f"; sampled, one step {step}" if step else "") + ")")
                elif not within_noise(nv, v, 0.0, False)[0]:
                    entry["within_step"] = True
            entry["within_step"] = entry["within_step"] and entry["ok"]
            table[k] = entry
        out["messages"] = {"mode": next(iter(modes.values())), "keys": table,
                           "stable": sum(1 for e in table.values() if not e["tolerance"]),
                           "noisy": sum(1 for e in table.values() if e["tolerance"]),
                           "within_step": sum(1 for e in table.values() if e["within_step"])}
        if bad:
            reasons.append(f"compare: world messages outside the A/A noise ({modes[ref]}): " + ", ".join(bad[:5])
                           + (f" and {len(bad) - 5} more" if len(bad) > 5 else ""))

        # The per-frame keys as rates: per server frame when every run has GPU lines in its window,
        # else per second for all of them (one unit across the runs).
        rate_keys = [k for k in all_keys if is_rate_key(k)]
        per_frame = all(keys[n]["messages"]["frames"] for n in names)
        unit = "frame" if per_frame else "second"
        den = {n: keys[n]["messages"]["frames"] if per_frame else keys[n]["messages"]["seconds"] for n in names}
        rate_floor = float(c.get("rate_floor", DEFAULT_RATE_FLOOR))
        rates, bad = {}, []
        if rate_keys and not all(den.values()):
            reasons.append("compare: per-frame keys: no window length in run(s) "
                           + ", ".join(n for n in names if not den[n]))
        elif rate_keys:
            def rate(n: str, k: str) -> tuple[float, float]:
                v = keys[n]["messages"]["keys"].get(k, 0)
                lo, hi = sampled_range(v) if sampled else (v, v)
                return lo / den[n], hi / den[n]

            for k in rate_keys:
                nr = [rate(n, k) for n in noise]
                entry = {"noise": [keys[n]["messages"]["keys"].get(k, 0) for n in noise], "noise_rate": nr,
                         "tolerance": rates_within_noise(nr, nr[0], rate_floor)[1], "test": {}, "test_rate": {},
                         "ok": True}
                for n in test:
                    tr = rate(n, k)
                    entry["test"][n], entry["test_rate"][n] = keys[n]["messages"]["keys"].get(k, 0), tr
                    ok, entry["tolerance"] = rates_within_noise(nr, tr, rate_floor)
                    if not ok:
                        entry["ok"] = False
                        bad.append(f"{k} {n} {rate_text(tr)} (A/A {'/'.join(rate_text(r) for r in nr)}, "
                                   f"tolerance {entry['tolerance']:.4f})")
                rates[k] = entry
        out["messages"].update(rates=rates, rate_unit=unit, rate_floor=rate_floor,
                               denominators={n: den[n] for n in names})
        if bad:
            reasons.append(f"compare: per-frame keys outside the A/A noise (events per {unit}): " + ", ".join(bad[:5])
                           + (f" and {len(bad) - 5} more" if len(bad) > 5 else ""))

    # The load times: a test run no slower than the A/A allows.
    floor = float(c.get("load_floor_s", 0.0))
    anchors_differ: set[str] = set()
    for k in sorted(set().union(*(keys[n]["loads"] for n in names))):
        lacking = [n for n in names if k not in keys[n]["loads"]]
        nv = [keys[n]["loads"][k] for n in noise if k in keys[n]["loads"]]
        entry: dict = {"noise": nv, "tolerance": None, "test": {n: keys[n]["loads"].get(k) for n in test}, "ok": True}
        out["loads"][k] = entry
        if lacking:
            entry["ok"] = False
            # A level span that starts at the request in one run and at the open in another (the
            # request reached S before or after the session opened the level): one clear reason for
            # the pair, not two "not in run(s)" lines.
            if a := LOAD_ANCHOR.match(k):
                other = f"{a.group(1)}: {'open' if a.group(2) == 'request' else 'request'} -> {a.group(3)}"
                if all(other in keys[n]["loads"] for n in lacking):
                    pair = f"{a.group(1)}: -> {a.group(3)}"
                    if pair not in anchors_differ:
                        anchors_differ.add(pair)
                        by = {w: [n for n in names if f"{a.group(1)}: {w} -> {a.group(3)}" in keys[n]["loads"]]
                              for w in ("request", "open")}
                        reasons.append(f"compare: load {a.group(1)} -> {a.group(3)}: the anchor differs between "
                                       f"runs: the request in run(s) {', '.join(by['request'])}, the open in "
                                       f"run(s) {', '.join(by['open'])}")
                    continue
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
        rates = msgs.get("rates", {})
        lines += ["", f"World messages ({msgs['mode']}): {len(msgs['keys'])} keys graded as counts, {msgs['stable']} "
                      f"equal in the A/A, {msgs['noisy']} noisy (listed with any that failed or passed within one "
                      f"step); {len(rates)} per-frame key(s) graded as rates (below)."]
        if msgs["mode"] == "sampled":
            lines += ["", f"Sampled counts: the trace logs a key's first {TRACE_FIRST_LINES} events and then every "
                          f"{TRACE_EVERY}th, so a count under {TRACE_FIRST_LINES} is exact and a higher one is known "
                          "only to within one sampling step (the true count lies between it and the next logged "
                          "number). Such a key passes when the runs agree within one step on top of the A/A spread; "
                          f"{msgs.get('within_step', 0)} key(s) passed that way (marked 'sampled, within one step')."]

        def mark(e: dict) -> str:
            return " (FAIL)" if not e["ok"] else " (sampled, within one step)" if e.get("within_step") else ""

        rows = [[k + mark(e), " / ".join(str(v) for v in e["noise"])]
                + [str(e["test"][n]) for n in test]
                for k, e in msgs["keys"].items() if e["tolerance"] or not e["ok"] or e.get("within_step")]
        if rows:
            lines += table(["Key", "A/A"] + test, rows)
        if rates:
            unit, den = msgs["rate_unit"], msgs["denominators"]
            secs = {r["name"]: r["keys"]["messages"]["seconds"] for r in cmp["runs"]}
            source = ("server frames in the window, from the swaps of the `GPU: draws` lines" if unit == "frame" else
                      "seconds of the window (a run has no `GPU: draws` line in it, so no frame count)")
            lines += ["", f"Per-frame keys graded as rates ({', '.join(f'trigger {e}' for e in RATE_EVENTS)}: an "
                          "event every server frame while an entity stands in the volume, so their counts follow "
                          f"the frame rate): events per {unit}, the window's count divided by the {source}. A test "
                          "run passes within the A/A rates widened by their spread plus "
                          f"{msgs['rate_floor'] * 100:g}% of the mean A/A rate"
                          + (" (a sampled count gives a range of rates, low-high)" if msgs["mode"] == "sampled" else "")
                          + ".", "",
                      f"{unit.capitalize()}s in the window: "
                      + ", ".join(f"{n} {den[n]:g}" + (f" ({den[n] / secs[n]:.2f} per s over {secs[n]:g} s)"
                                                       if unit == "frame" and secs.get(n) else "") for n in den)
                      + "."]
            rows = [[k + ("" if e["ok"] else " (FAIL)"), " / ".join(str(v) for v in e["noise"]),
                     " / ".join(rate_text(r) for r in e["noise_rate"]), f"{e['tolerance']:.4f}"]
                    + [f"{e['test'][n]}: {rate_text(e['test_rate'][n])}" for n in test]
                    for k, e in rates.items()]
            lines += table(["Key", "A/A counts", f"A/A per {unit}", "Tolerance"]
                           + [f"{n} (count: per {unit})" for n in test], rows)
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
                     accepted_grades: tuple[str, ...] = (), pacer: StartPacer | None = None) -> CaseResult:
    """A [compare] case: its runs one after another, each in its own folder inside the case's folder
    (graded as a run of its own: expect, forbid, grades), then the comparison. A run that does not
    reach its evidence ends the case there: the comparison is "not reached", and the case fails."""
    res = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, started=stamp)
    if len(spec.peers) > max_games:
        res.status = "capacity"
        res.reasons = [f"needs {len(spec.peers)} games at once, this run allows {max_games} (--max-games)"]
        return res
    case_dir = make_run_dir(cfg, cfg.store / "runs" / f"{stamp}-{spec.name}")
    res.folder = str(case_dir)
    t_start = time.monotonic()
    gathered, failed = [], False
    for run in spec.compare["run"]:
        name = run["name"]
        sub = run_case(cfg, variant_spec(spec, run), snap, max_games, time.strftime("%Y%m%d-%H%M%S"), thresholds,
                       baseline, accepted_grades, folder=case_dir / name, pacer=pacer)
        res.leftover_processes += sub.leftover_processes
        for p in sub.peers:
            p.peer = f"{name}:{p.peer}"
            res.peers.append(p)
        res.reasons += [f"run {name}: {why}" for why in sub.reasons]
        if sub.stopped_early and not res.stopped_early:
            res.stopped_early = f"run {name}: {sub.stopped_early}"
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
                 max_games: int, guard: list[dict], unwritten: list[str], checkpoint: dict | None = None,
                 gate: dict | None = None, token: dict | None = None, jobs: int = 1) -> None:
    head = f"At most {max_games} game(s) at once."
    if jobs > 1:
        head = (f"At most {max_games} game(s) and {jobs} case(s) at once (--jobs {jobs}); an exclusive case ran"
                " alone. With: the cases that ran beside it.")
    lines = [f"# skytest run {out.name}", "", head, ""]
    if token is not None:
        lines += [token_report_line(token), ""]
    if jobs > 1:  # the With column only when cases could run at once: --jobs 1 keeps the table as before
        lines += ["| Case | Layout | Stage | Status | Time | With | Why |", "|---|---|---|---|---|---|---|"]
    else:
        lines += ["| Case | Layout | Stage | Status | Time | Why |", "|---|---|---|---|---|---|"]
    for r in results:
        why = "; ".join(r.reasons) or ""
        if jobs > 1:
            beside = ", ".join(r.ran_with) or ("alone (exclusive)" if r.exclusive else "-")
            lines.append(f"| {r.case} | {r.layout} | {r.stage} | {r.status.upper()} | {r.seconds:.0f} s | {beside} "
                         f"| {why} |")
        else:
            lines.append(f"| {r.case} | {r.layout} | {r.stage} | {r.status.upper()} | {r.seconds:.0f} s | {why} |")
    for r in results:
        if not r.peers:
            continue
        lines += ["", f"## {r.case} ({r.layout})", "", f"Folder: `{r.folder}`", "",
                  "| Peer | Status | Exit | Time | Startup stall | Levels | FPS | Longest frame | Peak private MB "
                  "| Errors | Measures |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in r.peers:
            ms = ", ".join(f"{k} {v} s" if v is not None else f"{k} -" for k, v in p.measures.items())
            fps = ", ".join(f"{v:.1f}" for v in p.fps)
            lf = ", ".join(str(v) for v in p.longest_frame_ms)
            exit_ = "ended" if p.ended_by_runner else str(p.exit_code)
            stall = f"{p.startup_stall_s:.2f} s" if p.startup_stall_s is not None else "-"
            lines.append(f"| {p.peer} | {p.status.upper()} | {exit_} | {p.seconds:.0f} s | {stall} "
                         f"| {' > '.join(p.levels)} | {fps} | {lf} | {p.peak_private_mb} | {p.errors} | {ms} |")
        for p in r.peers:
            for why in p.reasons:
                lines.append(f"- {p.peer}: {why}")
        for p in r.peers:
            if p.startup_stall_s is not None and p.startup_stall_s > STARTUP_STALL_WARN_S:
                lines.append(f"- WARNING: {p.peer}: startup stall {p.startup_stall_s:.1f} s (shader storage to the "
                             f"guest's first line, over {STARTUP_STALL_WARN_S:.0f} s): the GPU shader caches were cold "
                             "or unusable (a restricted token?); this run's load and frame times carry it")
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
    if gate:
        lines += ["", "## Gate", "", gate_line(gate), "", "```", *gate_table(gate), "```"]
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


def last_run_seconds(cfg: Config, current: Path | None = None) -> dict[str, dict[str, tuple[float, str]]]:
    """Each case's recorded times, from the run store's results.json files (the same walk as
    previous_statuses, oldest run folder first): under "pass" its newest pass, under "fail" its
    longest fail, each as (`seconds`, the run folder's name). A fail can end early (a crash, a startup
    failure), so only a pass sets an estimate; the longest fail is shown beside a timeout. A run that
    ended in error, capacity or not run says nothing about the case's length and is skipped."""
    seen: dict[str, dict[str, tuple[float, str]]] = {}
    runs = cfg.store / "runs"
    if not runs.is_dir():
        return seen
    for f in sorted(runs.glob("*/results.json")):
        if f.parent == current:
            continue
        try:
            cases = json.loads(f.read_text(encoding="utf-8")).get("cases", [])
        except (OSError, ValueError):
            continue
        for c in cases:
            try:
                seconds = float(c.get("seconds", 0))
                status = c.get("status")
                if status not in ("pass", "fail") or seconds <= 0:
                    continue
                times = seen.setdefault(c["case"], {})
                if status == "pass" or seconds >= times.get("fail", (0.0, ""))[0]:
                    times[status] = (seconds, f.parent.name)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    return seen


def gate_estimate(cfg: Config, manifest: dict, stages: list[str] | None, specs: list[Spec],
                  current: Path | None = None) -> dict:
    """The gate cases (gate = true) of these stages (all when None) and their expected minutes: a
    case's newest pass, else its spec's timeout as an upper bound (per [[compare.run]] for a compared
    case; a case that only failed names its longest fail beside it), else `no spec` (named, never
    counted silently)."""
    by_name = {s.name: s for s in specs}
    last = last_run_seconds(cfg, current)
    rows = []
    for c in manifest.get("case", []):
        if not c.get("gate") or (stages is not None and str(c.get("stage")) not in stages):
            continue
        row = {"case": c["id"], "stage": str(c.get("stage")), "layout": c.get("layout", ""), "seconds": None}
        s = by_name.get(c["id"])
        times = last.get(c["id"], {})
        if "pass" in times and s is not None:
            row["seconds"], run = times["pass"]
            row["source"] = f"last pass {run}"
        elif s is not None:
            runs = max(1, len(s.compare.get("run", []))) if s.compare else 1
            row["seconds"] = s.timeout * runs
            fail = times.get("fail")
            row["source"] = ("spec timeout" + (f" x {runs} runs" if runs > 1 else "")
                             + (f" (never passed; longest fail {fail[0] / 60:.1f} min, {fail[1]})" if fail
                                else " (never run)"))
        else:
            row["source"] = "no spec"
        rows.append(row)
    return gate_totals(stages, rows)


def gate_totals(stages: list[str] | None, rows: list[dict]) -> dict:
    """The gate's sums over its rows (gate_estimate's, or the ones a `test --gate CASE...` runs)."""
    timed = [r for r in rows if r["seconds"] is not None]
    minutes = sum(r["seconds"] for r in timed) / 60
    return {
        "stages": stages, "cases": rows, "minutes": round(minutes, 1),
        "from_runs": [r["case"] for r in rows if r["source"].startswith("last pass")],
        "from_timeouts": [r["case"] for r in rows if r["source"].startswith("spec timeout")],
        "no_spec": [r["case"] for r in rows if r["source"] == "no spec"],
        "limit_minutes": GATE_MINUTES, "over": minutes > GATE_MINUTES,
    }


def gate_only(g: dict, names: set[str], patterns: list[str]) -> dict:
    """The estimate of a `test --gate CASE...` run: the rows of the cases it runs, and the spec-less
    gate cases its patterns name; the sums over those alone."""
    rows = [r for r in g["cases"] if r["case"] in names
            or (r["source"] == "no spec" and any(fnmatch.fnmatch(r["case"], p) for p in patterns))]
    return gate_totals(g["stages"], rows)


def gate_line(g: dict) -> str:
    """The line `test --gate` prints before it runs, and `list --gate` after its table."""
    line = (f"gate: {len(g['cases'])} cases, expected {g['minutes']:.0f} min ({len(g['from_runs'])} from their last"
            f" passes, {len(g['from_timeouts'])} from spec timeouts" +
            (f": {', '.join(g['from_timeouts'])}" if g["from_timeouts"] else "") + ")")
    if g["no_spec"]:
        line += f"; no spec, not counted: {', '.join(g['no_spec'])}"
    line += f"; the plan's subset is {g['limit_minutes']} min"
    if g["over"]:
        line += f" (over by {g['minutes'] - g['limit_minutes']:.0f} min)"
    return line


def gate_table(g: dict) -> list[str]:
    lines = []
    for r in g["cases"]:
        t = f"{r['seconds'] / 60:5.1f} min" if r["seconds"] is not None else "    - min"
        lines.append(f"{r['case']:<32} {r['layout']:<9} stage {r['stage']:<5} {t}  {r['source']}")
    return lines


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


# --- running cases at once (`test --jobs N`) --------------------------------------------------------
#
# The owner's playtests run several cases at once (stage 1B): with cases of two games (server+1), a
# machine that runs five games runs two cases side by side. Each case keeps its own run folder,
# peers, fixture copies, job object and hosted-server port (free_loopback_port; Host names its own
# control pipe), and the start gap holds across all of them (StartPacer). Cases start in the
# selection's order: the next one starts when a job is free and its games fit beside the running
# ones' within --max-games, and no later case jumps ahead of it (so a big or exclusive case is never
# starved). A case with `exclusive = true` waits until nothing runs and runs alone. The results stay
# in the selection's order, each with the cases that ran beside it (CaseResult.ran_with).

# Settings no spec may set (load_spec): the runner picks a hosted server's port per case and Host
# names its control pipe per process; a fixed one would be shared by cases run at once.
JOBS_OWNED_ARGS = {
    "--online_port": "the runner gives each hosted case its own free port (cases run at once never share one)",
    "--online_control_pipe": "Host names the control pipe after its own process (cases run at once never share one)",
}
# The grades that measure time or load: a spec with one of them, or a [compare], is `exclusive = true`
# (lint), since beside another case the machine's load is not the case's own. The scheduler runs such
# a spec alone even when the mark is missing (runs_alone), so a spec that skipped lint never measures
# another case's load.
TIMED_GRADES = ("server_frame_time", "client_fps", "level_load", "press_to_reaction", "bandwidth")


def timed_grades(spec: Spec) -> list[str]:
    """What in a spec measures time or load (TIMED_GRADES and a [compare]), for lint's exclusive check."""
    return [g for g in TIMED_GRADES if g in spec.grades] + (["compare"] if spec.compare else [])


def runs_alone(spec: Spec) -> bool:
    """Whether the scheduler runs a case alone: `exclusive = true`, or a time or load grade (timed_grades)."""
    return spec.exclusive or bool(timed_grades(spec))


def case_games(spec: Spec, max_games: int) -> int:
    """The games a case holds while it runs: every peer (a hosted server too, which its owner starts);
    0 for a case over --max-games, which ends at once as a capacity result."""
    n = len(spec.peers)
    return n if n <= max_games else 0


def schedule_cases(specs: list[Spec], jobs: int, max_games: int, run_one, say=None,
                   poll: float = 0.2) -> list[CaseResult]:
    """Runs `run_one(spec)` for every spec, up to `jobs` at once (1: one after another in this
    thread, as before --jobs), each started in order once its games fit within `max_games` beside the
    running ones; an exclusive spec only when nothing runs, and nothing beside it. A runner error in
    one case (an exception) is that case's `error` result; a SystemExit or KeyboardInterrupt starts
    nothing more, waits for the running cases and is raised again. Returns the results in the specs'
    order, each with `ran_with` (the cases whose runs overlapped it) and `exclusive` set."""
    jobs = max(1, jobs)
    printing = threading.Lock()

    def tell(text: str) -> None:
        with printing:
            (say or (lambda s: print(s, flush=True)))(text)

    results: list[CaseResult | None] = [None] * len(specs)
    spans: list[tuple[float, float]] = [(0.0, 0.0)] * len(specs)
    running: dict[int, tuple[int, bool]] = {}  # index -> (games held, exclusive)
    stop: list[BaseException] = []
    cond = threading.Condition()  # an RLock inside: the inline run (jobs 1) takes it again

    def one(i: int) -> None:
        spec, t0, r = specs[i], time.monotonic(), None
        try:
            r = run_one(spec)
        except Exception as e:  # noqa: BLE001 - one case's runner error never stops the others
            r = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, status="error",
                           reasons=[f"runner error: {type(e).__name__}: {e}"])
        except BaseException as e:  # noqa: BLE001 - raised again once the running cases ended
            with cond:
                stop.append(e)
        finally:
            with cond:
                results[i], spans[i] = r, (t0, time.monotonic())
                try:
                    if r is not None:  # before the case leaves `running`: the run's last line comes last
                        why = f": {'; '.join(r.reasons)}" if r.reasons else ""
                        tell(f"  {r.status.upper()}{why}" if jobs == 1 else f"{spec.name}: {r.status.upper()}{why}")
                finally:  # a print that fails (encoding, a closed pipe) never keeps the case in `running`
                    running.pop(i, None)
                    cond.notify_all()

    pending = list(range(len(specs)))
    told: set[int] = set()
    with cond:
        while (pending and not stop) or running:
            if pending and not stop:
                i = pending[0]
                spec = specs[i]
                need = case_games(spec, max_games)
                held = sum(g for g, _ in running.values())
                beside = [specs[j].name for j in running]
                if running and any(ex for _, ex in running.values()):
                    why = f"waits: {', '.join(beside)} runs alone (exclusive)"
                elif running and runs_alone(spec):
                    why = f"waits for {', '.join(beside)} to end (exclusive: it runs alone)"
                elif held + need > max_games:
                    why = f"waits for games: needs {need}, {max_games - held} of {max_games} free (--max-games)"
                elif len(running) >= jobs:
                    why = ""  # a job ends first: nothing to say
                else:
                    pending.pop(0)
                    running[i] = (need, runs_alone(spec))
                    if jobs == 1:
                        tell(f"{spec.name} ({spec.layout}) ...")
                        one(i)
                    else:
                        tell(f"{spec.name} ({spec.layout}) ... started"
                             + (f" beside {', '.join(beside)}" if beside else " alone")
                             + (" (exclusive)" if runs_alone(spec) else "") + f", {held + need} of {max_games} games")
                        threading.Thread(target=one, args=(i,), name=f"skytest-{spec.name}", daemon=True).start()
                    continue
                if why and i not in told:
                    told.add(i)
                    tell(f"{spec.name}: {why}")
            cond.wait(timeout=poll)
    if stop:
        raise stop[0]
    out: list[CaseResult] = []
    for i, (spec, r) in enumerate(zip(specs, results)):
        assert r is not None
        r.exclusive = runs_alone(spec)
        if r.status != "capacity":
            s0, e0 = spans[i]
            r.ran_with = [specs[j].name for j, (s1, e1) in enumerate(spans)
                          if j != i and results[j] is not None and results[j].status != "capacity"
                          and s1 < e0 and s0 < e1]
        out.append(r)
    return out


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
    gate_run = bool(getattr(a, "gate", False))
    gate = None
    every_spec = all_specs(cfg)
    specs = select(every_spec, a.cases, stages)
    if gate_run:
        gate = gate_estimate(cfg, manifest, stages, every_spec)
        gate_ids = {r["case"] for r in gate["cases"]}
        specs = [s for s in specs if s.name in gate_ids]
        if a.cases not in ([], ["all"]):
            gate = gate_only(gate, {s.name for s in specs}, a.cases)
        print(gate_line(gate), flush=True)
    if not specs:
        raise SystemExit("no case to run")
    # Before anything is copied or started: a restricted token is refused unless --allow-restricted.
    token = token_preflight(bool(getattr(a, "allow_restricted", False)))
    gate_started = time.monotonic()
    check_disc(cfg)
    max_games = a.max_games or default_max_games()
    jobs = getattr(a, "jobs", None)
    jobs = 1 if jobs is None else int(jobs)
    if jobs < 1:
        raise SystemExit("--jobs is 1 or more")
    snap = snapshot_build(cfg)
    guarded = guarded_folders(cfg)
    before_state = {k: folder_state(path, skip) for k, (path, skip) in guarded.items()}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = make_run_dir(cfg, cfg.store / "runs" / f"{stamp}-session")
    thresholds = tomllib.loads(cfg.thresholds.read_text(encoding="utf-8")) if cfg.thresholds.is_file() else {}
    baseline = tomllib.loads(cfg.baseline.read_text(encoding="utf-8")) if cfg.baseline.is_file() else {}
    # --jobs N: up to N cases at once within --max-games, exclusive ones alone (schedule_cases); the
    # start gap holds across them all (one StartPacer).
    pacer = StartPacer(cfg.start_gap)

    def run_one(spec: Spec) -> CaseResult:
        entry = next((c for c in manifest.get("case", []) if c.get("id") == spec.name), {})
        run = run_compare_case if spec.compare else run_case
        return run(cfg, spec, snap, max_games, time.strftime("%Y%m%d-%H%M%S"), thresholds, baseline,
                   tuple(entry.get("accepted_grades", [])), pacer=pacer)

    results = schedule_cases(specs, jobs, max_games, run_one)
    after_state = {k: folder_state(path, skip) for k, (path, skip) in guarded.items()}
    guard = [{"folder": k, "before": before_state[k], "after": after_state[k],
              "unchanged": before_state[k] == after_state[k]} for k in guarded]
    # Required cases of the stages run here that have no spec yet: named, never skipped silently.
    unwritten = [c["id"] for c in manifest.get("case", [])
                 if c.get("kind") == "required" and not c.get("spec")
                 and (stages is None or str(c.get("stage")) in stages)] if a.cases in ([], ["all"]) else []
    if gate is not None:
        unwritten = list(gate["no_spec"])  # a gate run names its own spec-less cases, nothing else
    touched = [g["folder"] for g in guard if not g["unchanged"]]
    # A gate run is a subset: never a checkpoint.
    full_run = a.cases in ([], ["all"]) and gate is None
    before = previous_statuses(cfg, out)
    checkpoint = checkpoint_verdict(manifest, a.stage, stages, results, touched, before) if a.stage and full_run else None
    payload = {"snapshot": str(snap), "max_games": max_games, "jobs": jobs, "stages": stages, "thresholds": thresholds,
               "guarded_folders": guard, "required_without_spec": unwritten, "checkpoint": checkpoint,
               "restricted_token": token["restricted"], "restricted_token_cause": token["cause"],
               "cases": [asdict(r) for r in results]}
    if gate is not None:
        gate["actual_minutes"] = round((time.monotonic() - gate_started) / 60, 1)
        gate["case_minutes"] = round(sum(r.seconds for r in results) / 60, 1)
        payload["gate"] = gate
    (out / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_report(cfg, out, results, manifest, thresholds, max_games, guard, unwritten, checkpoint, gate, token,
                 jobs=jobs)
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
    if gate is not None:
        print(f"gate: {gate['actual_minutes']:.0f} min (the cases {gate['case_minutes']:.0f} min; expected"
              f" {gate['minutes']:.0f})")
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
    out = make_run_dir(cfg, cfg.store / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-compare-{spec.name}")
    write_compare(out, cmp)
    print(f"compare {spec.name}: {cmp['status'].upper()}" + (f": {'; '.join(cmp['reasons'])}" if cmp["reasons"] else ""))
    print(f"report: {out / 'compare.md'}")
    return 0 if cmp["status"] == "pass" else 1


def cmd_list(cfg: Config, a: argparse.Namespace) -> int:
    if getattr(a, "gate", False):
        manifest = load_manifest(cfg)
        stage = getattr(a, "stage", None)
        g = gate_estimate(cfg, manifest, stages_up_to(manifest, stage) if stage else None, all_specs(cfg))
        for line in gate_table(g):
            print(line)
        print(gate_line(g))
        return 0
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
            if timed_grades(s) and not s.exclusive:
                problems.append(f"{p.name}: grades time or load ({', '.join(timed_grades(s))}) but is not"
                                " `exclusive = true`: beside another case (test --jobs) it would measure that load too")
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
    t.add_argument("--gate", action="store_true", help="only the manifest's gate cases (of the stages up to --stage,"
                   " or all), with their expected minutes before the run")
    t.add_argument("--allow-restricted", action="store_true", help="start the games even when this runner has a"
                   " restricted token (an agent sandbox): their shader caches are unusable, timings not comparable")
    t.add_argument("--jobs", type=int, default=1, help="most cases at once (default 1: one after another); their"
                   " games count against --max-games, and a spec with exclusive = true runs alone")
    t.set_defaults(fn=cmd_test)
    ls = sub.add_parser("list")
    ls.add_argument("--gate", action="store_true", help="the gate cases and their expected minutes (from each"
                    " case's last finished run, else its spec's timeout)")
    ls.add_argument("--stage", default=None, help="with --gate: the gate cases of this stage and every earlier one")
    ls.set_defaults(fn=cmd_list)
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
