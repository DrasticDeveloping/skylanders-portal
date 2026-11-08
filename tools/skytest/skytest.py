#!/usr/bin/env python3
"""skytest: the multiplayer test runner of the Skylanders ports' online forks.

Run from a game repository (its `just test-online` calls this). A case is a TOML spec in the game's
tests/runtime/; it names a layout and what each peer's game does (an input script) and what its log
must show. The runner starts the layout's game processes, each on its own copies of the test data,
ends them, and writes report.md and results.json in a new run folder of the worktree's test store.
It stops a run early (fail fast, on unless a spec says `fail_fast = false`) a grace after a peer's
first fatal line: a crash, a forbidden line, a dead session's default line (DEFAULT_FATAL; a spec's
`tolerate` opts lines out), or a crash exit code. A case that is not ready (`ready = false` with a
`reason`, or a PLACEHOLDER comment) is reported `not ready` at 0 s and starts no game. results.json
records per case what ran (spec, args, fixture hashes, the runner's version) and the load beside it.
A spec's `scenes = "skip" | "play" | "movies"` (play needs `scenes_reason`) becomes the
--dev_skip_scenes value the runner passes to each game it starts; a spec without the key that does
not set the setting in its args (nor [defaults].args) is refused (lint, and an error at a run). Every runner on the machine shares
one game budget ([defaults] machine_games, default 4: slot files in the main checkout's
.replication-tests/slots/): a case waits for its slots, an exclusive or timed case takes them all, and
one game boots at a time machine-wide. When 3 cases of any runner on one build stop on the same
normalized fatal line or crash site, the circuit breaker starts no further case on that build.
Two keys are off unless a spec names them: a peer's `settings_file = "none"` starts its game with no
settings file (--config_file names the run folder's settings.toml, which is not there: the game's
compiled-in defaults); a spec's [build_copy] (`remove`, `truncate` with `file` and `before`) runs the
case's games from its own copy of the build snapshot, made fresh in the case's run folder, with those
files removed or cut at a marker line (launch_args.copy_build); the shared snapshot never changes.

    skytest.py test [CASE ...] [--stage S] [--max-games N]   run cases (a name, a glob, or all)
               [--gate]                                     only the manifest's daily gate cases (gate =
                                                            true) of the stages up to S, with their expected
                                                            minutes before the run, exclusive cases first,
                                                            then longest first; the per-build cases (gate =
                                                            "per-build") are left out, each met when its
                                                            newest result on the same build snapshot is a
                                                            pass
               [--gate --per-build]                         only the per-build gate cases (once per build,
                                                            in an idle slot; nothing starts it by itself)
               [--allow-restricted]                         start games even under a restricted token
                                                            (refused otherwise; timings not comparable)
               [--jobs N]                                   up to N cases at once (default 1), their games
                                                            within --max-games; a spec with
                                                            `exclusive = true` runs alone; above 1 the
                                                            exclusive cases start first, then the longest
               [--run-not-ready]                            run the cases that are not ready anyway
               [--no-breaker]                               start cases on a build whose circuit breaker
                                                            tripped (a spec's `breaker = false` too)
               [--queue FILE]                               also the cases FILE names, re-read before each
                                                            case start (entries added run next); results,
                                                            report.md and findings.md are written after
                                                            every case either way
               [--remote HOST:PORT --remote-manifest FILE   the remote agent on the second PC (its token
                [--remote-token-file FILE]]                 typed, or read from the file) runs a spec's
                                                            `kind = "remote"` peer; the RC's payload record
                                                            is what it verifies its install against
    skytest.py list [--gate [--stage S]]                    the cases and the manifest's state; with
                                                            --gate the gate cases and their expected
                                                            minutes, the per-build ones apart with their
                                                            last result on this build (no game started)
    skytest.py lint [--timeouts [--since STAMP]]            check every spec and the manifest; warn of fps
                                                            windows nothing grades (and of waitfor timeouts)
    skytest.py timeouts [--since STAMP]                     each waitfor condition's history in the store's
                                                            logs (median, p95, max) against the specs' timeouts
    skytest.py breaker [--reset]                            the circuit breaker's state per build; --reset
                                                            clears it (no game)
    skytest.py fixtures --from-owner                        copy the owner's saves and figures into
                                                            the fixture store (read only on their side)
    skytest.py compare RUN RUN [RUN ...] --spec CASE        compare finished runs with that case's
               [--noise N]                                  [compare] settings (the first N are the
                                                            A/A; default all but the last of 3 or more)

Layouts (the game repository's docs/testing-guide.md has the details):

    solo        H alone: the plain game, offline (reference measurements)
    server+N    the server peer S and N clients H, D, E, F (N = 1..4); H is the owner's client
    server+N+hK S, N clients and K synthetic peers h1, h2 (K = 1..2; a hosted server only): a
                game-free program the game repository names in tests/skytest.toml [synthetic],
                started in the case's job object once its [synthetic.<h>] start condition shows in
                a peer's log, graded on its own output; never a game, never counted as one
    unit        no game: a unit test binary from the build (copied into a snapshot first), graded by
                its exit code and its pass line, and the ctests the case names (stage 2's
                skylanders_backend_test and backend.include_rule)

Until stage 1A gives the game a server role, S is a plain game instance with the runner's own
lifecycle script (it boots hidden and quits); it runs no case script.

Rules the runner enforces (docs/RULES.md of the game): outputs only inside the worktree's
.replication-tests/; never under the owner's data folder, never into the disc; the build is copied
into a snapshot and run from there, never from the build folder; every game's command line and input
script come from the launch builder (launch_args.py, against the game's tests/launch_allow.toml and
tests/script_commands.toml), which refuses an empty setting, a runner-owned one set twice, a path
outside the peer's own run folder, a setting off the allow list and a script command that writes
outside the run folder;
every game is in a job object, so none outlives the run; a case that cannot run says why in its
report (capacity, missing fixture, spec error) instead of being skipped silently. A runner with a
restricted token (an agent sandbox) starts no game unless --allow-restricted (its games could not use
the shader caches); every game it starts is opted out of Windows power throttling.

Standard library only; run with `python -I`.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fnmatch
import getpass
import hashlib
import json
import math
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
import backend_grades  # noqa: E402  (stage 3's backend grades: compare, faults, memory)
import launch_args as la  # noqa: E402
import remote_agent  # noqa: E402
import remote_swap_grade  # noqa: E402  (stage 4's remote_swap grade over swap_pairs.py, the ordered form)
import exclusive_grade  # noqa: E402  (stage 4's exclusive grade: a race exactly one client wins)
import swap_pairs  # noqa: E402
import memory_growth  # noqa: E402  (stage 5's soak grade)
import occurrence_outcomes  # noqa: E402  (stage 5's occurrence grade)

STORE = ".replication-tests"
CLIENTS = ["H", "D", "E", "F"]
LAYOUT = re.compile(r"^(solo|server\+([1-4])(?:\+h([1-2]))?)$")
# Synthetic peers (`server+N+hK`; Swap Force Online stage 4 item 2, its docs/online-4.md section 4.2):
# a game-free program that joins the hosted server over loopback with a scripted pose and player
# rows. Named h1, h2 in a spec's [synthetic.<name>] tables; started once the start peer's log has a
# line matching `start_after` (a condition, not a sleep); must say it connected; must end by itself
# within SYNTHETIC_EXIT_GRACE seconds of the games' end (End session drains it), else it is ended
# and fails.
SYNTHETIC = ["h1", "h2"]
SYNTHETIC_START_AFTER = r"Online: the server is in its hub"
SYNTHETIC_CONNECTED = re.compile(r"Online: synthetic peer (\d+) connected")
SYNTHETIC_FATAL = [r"Online: synthetic peer (could not connect|script |refused|leaves: no player records)"]
SYNTHETIC_EXIT_GRACE = 20.0
SYNTHETIC_ARG_PLACEHOLDERS = ("{port}", "{script}", "{digest}", "{server}", "{peer}")
SYNTHETIC_KEYS = ("script", "start_after", "start_peer", "expect", "forbid", "expect_count", "expect_order")
# A game client's peer id in its log (the session's Welcome), for the cross-client grades.
CONNECTED_AS = re.compile(r"Online: connected as peer (\d+)")
# A case that runs a unit test binary and ctests from the build instead of games (stage 2 item 7).
UNIT = "unit"
PEER_DIRS = ("data", "figures", "cache")
# Layouts the manifest may name that one PC cannot run: a second PC, other networks.
MANIFEST_ONLY_LAYOUTS = ("server+3+LAN", "internet+2")

# Log lines the game writes (skylanders-portal src/dev, the game's where hook).
TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]")
FPS = re.compile(r"Input script: ([\d.]+) guest FPS \((\d+) frames in ([\d.]+) s\), longest frame (\d+) ms, "
                 r"(\d+) over 50 ms")
WHERE = re.compile(r"Where: (\w+) at (-?[\d.]+) (-?[\d.]+) (-?[\d.]+)")
MEMORY = re.compile(r"Input script: memory (\d+) MB private, (\d+) MB working set")
BUILD = re.compile(r"\(build ([\w-]+)\)")  # "<commit>" or "<commit>-dirty-<hash>" (cmake/game_build_stamp.cmake)
# The online code's frame-time event (stage 1A): every 10 s on the server and on a client in a session.
FRAMES = re.compile(r"Online: frames (\d+) fps ([\d.]+) p50 ([\d.]+) p95 ([\d.]+) max ([\d.]+) "
                    r"private (-?\d+) working (-?\d+) video (-?\d+)")
# Where a hosted server (`server = "host"`) writes its log: the owner's session folder.
HOSTED_SERVER_LOG = Path("data") / "Online" / "session" / "game.log"
GRADES = ("server_frame_time", "client_fps", "level_load", "save_copy", "press_to_reaction", "requests_answered",
          "symmetry", "route", "bandwidth", "remote_swap", "exclusive", "occurrence_outcomes",
          "memory_growth", "data_root_untouched") + backend_grades.NAMES
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
REQUEST_KINDS = ("enter", "exit", "touch", "telegram", "skip")
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
# A peer's save_slots (stage 3 item 7, fill_save_slots): the save menu's slots (three in Swap Force:
# client/host.cpp refuses a fourth), the saved-game content type's folder (XContentType 1), and where
# an SDK content header keeps the content's file name (XCONTENT_DATA: device id 4, content type 4,
# display name 256 bytes, then file_name_raw[42]; rexglue-sdk content_manager.h).
SAVE_SLOT_MAX = 3
SAVE_CONTENT_TYPE = "00000001"
SAVE_HEADER_NAME_AT = 264
SAVE_HEADER_NAME_LEN = 42
# S's boot timeline ending outside play (server_role.cpp BootCheck) is fatal for every hosted case:
# the owner's game then waits for a hub that never comes (P1m: a case ran to its 1200 s limit).
ALWAYS_FORBIDDEN = [r"\] Crash: ", r"\[FATAL\]", r"Input script: cannot (parse|use)",
                    r"Online: the server did not reach its hub"]
# A dead session's default fatal lines (PS5): forbidden for every case like ALWAYS_FORBIDDEN (graded,
# and fail fast stops the run at one), each for one peer ("*": every peer). A session that logged one
# never recovered: none of the 215 passes in the test store of 11/6 has one, and the failed runs that
# did went on for minutes after it (79 min over 20 cases in 2 days: scene 916 s, level-guard-joiner-
# continue 850 s, x19 517 s). `waitfor title_ready` and `level_ready` timeouts stay out (passes
# recovered from them). A spec's `tolerate = [regex, ...]` takes a line that matches one of them out
# (the default is then not fatal for that line); `Online: the server ended by itself` is not fatal in a
# teardown case (one whose name has "teardown").
DEAD_SESSION_WAITS = ("online_connected", "online_server_hub", "online_barrier", "online_in_play",
                      "online_next_level", "online_level_after_mark", "online_level_end", "front_end_left",
                      "menu_ready")
SERVER_ENDED_BY_ITSELF = r"Online: the server ended by itself"
DEFAULT_FATAL = [
    ("S", r"Online: the server's session did not start"),
    ("S", r"Online: could not connect the control pipe"),
    ("H", r"Online: server: error"),
    ("H", SERVER_ENDED_BY_ITSELF),
    ("*", r"Input script: waitfor (" + "|".join(DEAD_SESSION_WAITS) + r") timed out"),
    ("*", r"Dev: .*no player frame within"),
    ("*", r"refused: dev_\w+ is off"),
    # Stage 7 (future/online-7.md section 3 "Settings", item 8): the game's own refusal of the
    # write-back on anything but a checked copy, and the dev commit gate's lost release.
    ("H", r"Online: dev_writeback_on_copies refused"),
    ("S", r"Online: commit gate timed out"),
    # Stage 7, council 7 (Sol B1, Opus B2): dev_unlock_nightmare or dev_save_reload on a data root the
    # game does not admit as a test copy (the game calls nothing then; a spec's copy should pass).
    ("*", r"Dev: \w+ refused: not a test copy"),
]
# One line each DEFAULT_FATAL pattern matches, as the game logs it (lint warns of a `tolerate` pattern that
# matches none of them: it takes nothing out).
_LOG_PREFIX_EXAMPLE = "[2026-11-06 10:00:00.000] [info] [core] [t1] "
DEFAULT_FATAL_EXAMPLES = [_LOG_PREFIX_EXAMPLE + line for line in (
    "Online: the server's session did not start (no hub)",
    "Online: could not connect the control pipe (error 2)",
    "Online: server: error: bind 127.0.0.1:53412 failed",
    "Online: the server ended by itself (exit code 1)",
    *(f"Input script: waitfor {w} timed out after 60 s" for w in DEAD_SESSION_WAITS),
    "Dev: navto: no player frame within 5 s",
    "refused: dev_navto is off",
    "Online: dev_writeback_on_copies refused (no --user_data_root); Host does not host",
    "Online: commit gate timed out SWAPFORCE_1",
    "Dev: dev_unlock_nightmare refused: not a test copy (no --user_data_root)",
)]
# A case that is not ready (PS2) is reported `not ready` at 0 s and starts no game: `ready = false`
# with a `reason`, or, without a `ready` key, a comment line in the spec that starts with PLACEHOLDER
# (the route's legs 11+ until they are filled: the case otherwise ran to its 4200 s timeout).
PLACEHOLDER = re.compile(r"(?m)^[ \t]*#[ \t]*PLACEHOLDER\b(.*)$")
# Fail fast (on unless a spec says `fail_fast = false`): while a run's games play, the runner reads the
# new bytes of each peer's log every FAIL_FAST_POLL seconds and, at the first fatal line (a forbidden
# one: the peer's `forbid`, ALWAYS_FORBIDDEN and DEFAULT_FATAL, a crash among them) or a game that exits
# with a crash code, stops every game of the run after a grace (tests/skytest.toml [defaults]
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
# Scene policy (PS6): a spec's `scenes = "skip" | "play" | "movies"` becomes the game's test-only
# --dev_skip_scenes (src/online/dev/scene_skip.cpp), which the runner passes itself, never empty: skip
# is true, play is false (with `scenes_reason`: why the case watches its scenes), movies skips only the
# level intro movies and needs the game's movies skip (`[game] movies_skip = true` in
# tests/skytest.toml; the game's setting is true/false until then). A spec with the key may not set the
# setting in its args, and a [defaults].args value may not reach a spec whose key says otherwise (the
# game's pick of two values is unverified). A spec without the key that sets it in its args keeps that
# (the runner adds nothing to it: the specs before the key). There is no default: a spec that says
# nothing is refused (a scene case run with the skip would pass or fail for the wrong reason).
SCENE_SETTING = "dev_skip_scenes"
SCENE_VALUES = {"skip": "true", "play": "false", "movies": "movies"}

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
    # [game] save_slot_name: a save slot's content name with {index} for the zero-based slot
    # ("SWAPFORCE_{index}"), for a peer's save_slots (fill_save_slots). Missing: save_slots is refused.
    save_slot_name: str = ""
    # Fail fast: seconds between a run's first fatal line and the stop of its games.
    fail_fast_grace: float = DEFAULT_FAIL_FAST_GRACE
    # [defaults] show_server_window: the server peer's window is shown (a playtest the owner
    # watches); a spec's own show_server_window wins. Missing: hidden, as before (show_server_args).
    show_server_window: bool = False
    # [game] movies_skip = true: the game's --dev_skip_scenes takes `movies` (PS19), so a spec may say
    # scenes = "movies". Missing: false (lint refuses a movies spec, and a run of one is an error).
    movies_skip: bool = False
    # PS15: [defaults] machine_games, the games every runner on the machine shares (MachineBudget), and
    # the folder of its slot files (the main checkout's .replication-tests/slots/: one for every
    # worktree; machine_slots_dir). Missing key: MACHINE_GAMES.
    machine_games: int = 4
    slots: Path | None = None
    # A unit case's ctest command (default: `ctest` on PATH, as the build prompt has it), and a
    # command put in front of its test binary (only the runner's own tests set that one).
    ctest: list[str] = field(default_factory=list)
    unit_launcher: list[str] = field(default_factory=list)
    # The launch builder's two tables (launch_args.py, design D21): the top-level `launch_allow` and
    # `script_commands` of tests/skytest.toml, by default tests/launch_allow.toml and
    # tests/script_commands.toml. No game starts without them (launch_tables).
    launch_allow: Path | None = None
    script_commands: Path | None = None
    # `test --remote HOST:PORT`: the remote agent that runs a spec's `kind = "remote"` peer on the second
    # PC (RemoteTarget). None: such a case is a capacity result (it needs the second PC).
    remote: "RemoteTarget | None" = None
    # [synthetic] (server+N+hK): the synthetic peer's exe in the build folder (copied into the snapshot
    # with the game), its argument template (SYNTHETIC_ARG_PLACEHOLDERS), the server setting the runner
    # turns on in the owner's game H for such a case (Host passes it to its server), and a command put
    # in front of it (only the runner's own tests set that one).
    synthetic_exe: str = ""
    synthetic_args: list[str] = field(default_factory=list)
    synthetic_setting: str = ""
    synthetic_launcher: list[str] = field(default_factory=list)


def main_store(repo: Path) -> Path:
    """The main checkout's test store (its .replication-tests/), which every worktree of the repository
    shares for the machine's game slots (PS15): the folder above git's common dir. The checkout's own
    store when git cannot say (a bare repository, no git)."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"], capture_output=True,
                           text=True, encoding="utf-8", errors="surrogateescape")
    except OSError:
        return repo / STORE
    common = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    if common is None:
        return repo / STORE
    common = common if common.is_absolute() else (repo / common)
    common = common.resolve()
    return (common.parent / STORE) if common.name == ".git" else repo / STORE


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
    movies = g.get("movies_skip", False)
    if not isinstance(movies, bool):
        raise SystemExit(f"{f}: [game] movies_skip is true or false")
    machine = d.get("machine_games", MACHINE_GAMES)
    if not isinstance(machine, int) or isinstance(machine, bool) or not 1 <= machine <= MACHINE_SLOT_FILES:
        raise SystemExit(f"{f}: [defaults] machine_games is a whole number from 1 to {MACHINE_SLOT_FILES} (the games"
                         " all runners on the machine share)")
    syn = c.get("synthetic", {})
    syn_args = syn.get("args", [])
    if not isinstance(syn_args, list) or not all(isinstance(x, str) and x.strip() for x in syn_args):
        raise SystemExit(f"{f}: [synthetic] args is a list of non-empty strings")
    for x in syn_args:
        for ph in ARG_PLACEHOLDER.findall(x):
            if ph not in SYNTHETIC_ARG_PLACEHOLDERS:
                raise SystemExit(f"{f}: [synthetic] args: {ph} is not one of {', '.join(SYNTHETIC_ARG_PLACEHOLDERS)}")
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
        save_slot_name=str(g.get("save_slot_name", "")),
        fail_fast_grace=float(grace),
        show_server_window=show,
        movies_skip=movies,
        machine_games=machine,
        slots=main_store(repo) / "slots",
        ctest=[os.path.expandvars(x) for x in g.get("ctest", [])],
        unit_launcher=[os.path.expandvars(x) for x in g.get("unit_launcher", [])],
        launch_allow=repo / c.get("launch_allow", "tests/launch_allow.toml"),
        script_commands=repo / c.get("script_commands", "tests/script_commands.toml"),
        synthetic_exe=str(syn.get("exe", "")),
        synthetic_args=list(syn_args),
        synthetic_setting=str(syn.get("server_setting", "")),
        synthetic_launcher=[os.path.expandvars(x) for x in syn.get("launcher", [])],
    )


# --- safety ---------------------------------------------------------------------------------------


def owner_data_roots() -> list[Path]:
    """The owner's data folders (every Documents\\Skylanders the user can have): the launch builder's
    list (launch_args.owner_data_roots), one for the runner's own outputs and every game's settings."""
    return la.owner_data_roots()


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
# {figures}, {peer}, {join} and {joincode} instead (peer_script).
ARG_PLACEHOLDERS = ("{repo}",)
ARG_PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")
# Stage 8 item 4 (join code v2): a client's script may name {joincode}, the hosted session's join code
# with its secret. {join} is the server's address:port and carries no secret, so a server whose Hello
# gate is on (dev_hello_gate_loopback on loopback) refuses it "wrong code". The code exists only once
# the owner's game has started its server, so the runner reads it from the hosted server's log line
# (the game's src/online/server/server_role.cpp, InstallServerRole:
#   REXLOG_INFO("Online: the server listens on port {}, join code {}", started->port, started->join_code);
# the code as the portal's EncodeJoinCodeV2 makes it: 18 symbols of JOIN_CODE_SYMBOLS, no dashes) and
# starts a client whose script names {joincode} only then, with the code in its script (run_case,
# start_joiners). Fails closed: with no such line (the server did not start, a build before stage 8), or
# a line whose code is not 18 such symbols, that client never starts and fails; it never joins with an
# empty or guessed code.
JOIN_CODE_PLACEHOLDER = "{joincode}"
JOIN_CODE_SYMBOLS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # the portal's join_code.cpp kSymbols (Crockford)
JOIN_CODE_V2 = re.compile(f"[{JOIN_CODE_SYMBOLS}]{{18}}")
SERVER_JOIN_CODE = re.compile(r"Online: the server listens on port \d+, join code (\S*)")
# What the launch builder checks in a {joincode} script before the code exists (prepare_peer): one word
# that no join code is (U is not a symbol), swapped for the code when it comes (fill_join_code). A spec
# may not write it itself.
JOIN_CODE_STANDIN = "JOINCODE-NOT-KNOWN-YET-U"
# The plan's daily gate: each stage names a 30-minute subset (docs/online-fork-plan.md 5.4).
GATE_MINUTES = 30
# A manifest case's `gate`: true is the daily gate; "per-build" runs once per build in an idle slot
# (the owner, 2026-11-07: the full route), outside the daily gate, whose verdict counts it met when its
# newest result on the same build snapshot is a pass (gate_cadence, per_build_state).
GATE_PER_BUILD = "per-build"


def gate_cadence(case: dict) -> str:
    """A manifest case's gate cadence: `daily` (gate = true), `per-build` (gate = "per-build") or ``
    (not a gate case: false, absent, or a value lint refuses)."""
    g = case.get("gate", False)
    if g is True:
        return "daily"
    return GATE_PER_BUILD if g == GATE_PER_BUILD else ""

# Settings the runner sets for each peer itself (launch_args.RUNNER_OWNED). A case or the defaults may
# not set them again: the game takes the last of two, so a second --user_data_root would point a test at
# any folder. The whole command line is checked by the launch builder (launch_args.check_args, through
# build_game in prepare_peer): no empty value, each of these once and inside the peer's run folder,
# exactly one --portal_mode=software, every other setting on the game's allow list.
RUNNER_OWNED = la.RUNNER_OWNED


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
    # save_slots = { "<slot>" = <fixture slot> } (stage 3 item 7): this peer's copy of the fixture saves
    # gets the save of `fixture slot` once more in `slot` (both one-based, as the save menu counts them),
    # before the game starts (fill_save_slots). {} leaves the copy as the fixture store has it.
    save_slots: dict[int, int] = field(default_factory=dict)
    # Where the peer's game runs (stage 8 item 6): "local" (this PC), or "remote" (the second PC, through
    # its remote agent: `test --remote`; one client of D, E, F per case at most).
    kind: str = "local"
    # settings_file = "template" (the default: the run folder gets a copy of [game] settings_template) or
    # "none" (Swap Force Online stage 8 P5 defaults-release: no settings file, --config_file names where it
    # would be, and the game starts on its compiled-in defaults; launch_args.RunContext.no_settings).
    settings_file: str = "template"


SETTINGS_FILE_VALUES = ("template", "none")


@dataclass
class SyntheticSpec:
    """A synthetic peer of a server+N+hK case ([synthetic.<name>]): its own script (the game's
    synthetic-peer format, written to its folder as script.txt), when it starts (the first line of
    `start_peer`'s log matching `start_after`), and what its output must show."""
    name: str
    script: str
    start_after: str = SYNTHETIC_START_AFTER
    start_peer: str = "S"
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
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
    # tolerate = [regex, ...]: lines that match one of these are not a dead session's default fatal line
    # (DEFAULT_FATAL) for this case; forbid and ALWAYS_FORBIDDEN still apply to them.
    tolerate: list[str] = field(default_factory=list)
    # Why the case is not ready (PS2: `ready = false` with `reason`, or a PLACEHOLDER comment); '' when
    # it is. A case that is not ready is reported `not ready` and starts no game (`--run-not-ready` runs
    # it anyway).
    not_ready: str = ""
    # PS6: the spec's `scenes` key ('' when it has none: SCENE_VALUES, scene_args) and, for `play`, its
    # `scenes_reason`.
    scenes: str = ""
    scenes_reason: str = ""
    # C7: `breaker = false`: the case's failures never count toward the circuit breaker, and a tripped
    # breaker still starts it (a case that crashes on purpose).
    breaker: bool = True
    # PS12: the sha256 of the spec text load_spec read (provenance records it, not the file's later
    # state: a queue lane or another workflow may edit the file while the run goes on).
    sha256: str = ""
    # [unit]: a unit case's test binary, its pass line and ctests (load_unit_spec).
    unit: dict = field(default_factory=dict)
    # server+N+hK: the synthetic peers by name (h1, h2), never games (load_synthetic).
    synthetic: dict[str, SyntheticSpec] = field(default_factory=dict)
    # [build_copy] (Swap Force Online stage 8 P5 route-missing): the case's games run from its own copy of
    # the build snapshot with these files removed or cut short (case_build_copy, launch_args.copy_build);
    # None: from the shared snapshot, as every other case.
    build_copy: la.BuildEdits | None = None


PEER_KINDS = ("local", "remote")


def peer_settings_file(spec_name: str, peer: str, own: dict, common: dict, server: str, kind: str) -> str:
    """A peer's `settings_file` (its own table, else [peer.all]): "template" or "none". A hosted server's
    settings come from Host (it copies the owner's file, or makes an empty one), and a remote peer's agent
    copies its verified install's template, so neither takes the key."""
    value = own.get("settings_file", common.get("settings_file", "template"))
    if value not in SETTINGS_FILE_VALUES:
        raise SpecError(f"{spec_name}: peer {peer}: settings_file = {value!r} ({' or '.join(SETTINGS_FILE_VALUES)})")
    if "settings_file" in own and peer == "S" and server == "host":
        raise SpecError(f"{spec_name}: peer S: a hosted server's settings come from Host, not settings_file")
    if value != "template" and kind == "remote":
        raise SpecError(f"{spec_name}: peer {peer} is remote: its agent copies the install's settings template"
                        " (settings_file is for a local peer)")
    return value


def peer_kind(spec_name: str, peer: str, own: dict, common: dict) -> str:
    """A peer's `kind` (its own table only): "local", or "remote" for a client other than H (S is the
    server, H the owner's client whose game hosts it: both stay on this PC)."""
    if "kind" in common:
        raise SpecError(f"{spec_name}: [peer.all] has no kind (a remote peer says so in its own table)")
    kind = own.get("kind", "local")
    if kind not in PEER_KINDS:
        raise SpecError(f"{spec_name}: peer {peer}: kind = {kind!r} (local or remote)")
    if kind == "remote" and peer in ("S", "H"):
        raise SpecError(f'{spec_name}: peer {peer} runs on this PC (the server and the owner\'s client); kind ='
                        ' "remote" is for D, E or F')
    return kind


def check_join_code_peers(spec_name: str, peers: dict[str, "PeerSpec"], server: str) -> None:
    """{joincode} (JOIN_CODE_PLACEHOLDER) only in the script of a local client other than H, in a hosted
    case: the code is the hosted server's (server = "host"), H is the owner whose game starts that server,
    and a remote peer's script goes to the second PC when the run starts, before the code exists. No
    script writes the stand-in itself (fill_join_code swaps every one of them)."""
    for n, ps in peers.items():
        if JOIN_CODE_STANDIN in ps.script:
            raise SpecError(f"{spec_name}: peer {n}'s script has {JOIN_CODE_STANDIN}, the runner's stand-in for"
                            " {joincode}")
        if JOIN_CODE_PLACEHOLDER not in ps.script:
            continue
        if server != "host":
            raise SpecError(f'{spec_name}: peer {n}: {{joincode}} is a hosted server\'s join code: say server = "host"')
        if n == "H":
            raise SpecError(f"{spec_name}: peer H hosts the session; {{joincode}} is for a client that joins (D, E, F)")
        if ps.kind == "remote":
            raise SpecError(f"{spec_name}: peer {n} is remote: its script goes to the second PC before the join code"
                            " exists; {joincode} is for a local client")


def uses_join_code(spec: "Spec", peer: str) -> bool:
    """The peer's script names {joincode}: it starts once the hosted server logged its code (run_case)."""
    ps = spec.peers.get(peer)
    return peer != "S" and ps is not None and JOIN_CODE_PLACEHOLDER in ps.script


def join_code_from(lines: list[str]) -> tuple[str, str]:
    """The session's join code from a hosted server's log lines, at the first SERVER_JOIN_CODE line:
    (code, "") when its code is a join code v2 (JOIN_CODE_V2), ("", why) when it is not; ("", "") while
    no such line came."""
    for line in lines:
        m = SERVER_JOIN_CODE.search(line)
        if not m:
            continue
        if JOIN_CODE_V2.fullmatch(m.group(1)):
            return m.group(1), ""
        return "", f"S logged its join code as {m.group(1)!r}, not a join code v2 (18 symbols, no dashes)"
    return "", ""


def fill_join_code(script: Path, code: str) -> None:
    """The session's join code into a prepared peer's input script (prepare_peer wrote it with
    JOIN_CODE_STANDIN, which the launch builder checked): only a join code v2, whose 18 symbols hold no
    space, `;` or `#` that could change the script's commands, and only where the stand-in stands."""
    if not JOIN_CODE_V2.fullmatch(code):
        raise SpecError(f"{code!r} is not a join code v2 (18 symbols, no dashes); {script} keeps no guess")
    text = script.read_bytes().decode("utf-8")
    if JOIN_CODE_STANDIN not in text:
        raise SpecError(f"{script} has no {JOIN_CODE_STANDIN} for the join code (it was not prepared for one)")
    script.write_bytes(text.replace(JOIN_CODE_STANDIN, code).encode("utf-8"))


def check_remote_peers(spec_name: str, peers: dict[str, "PeerSpec"], grades: dict, names: list[str]) -> None:
    """One remote peer per case at most (the agent runs one game per run plan); its settings cannot name
    this PC's checkout ({repo}); its saves stay on the second PC, so no save_copy grade names it and it
    has no save_slots (prepare_peer fills only a local peer's copy)."""
    remote = [n for n, ps in peers.items() if ps.kind == "remote"]
    if len(remote) > 1:
        raise SpecError(f"{spec_name}: peers {', '.join(remote)} are remote: one remote peer per case (the agent"
                        " on the second PC runs one game per run)")
    for n in remote:
        if any("{repo}" in a for a in peers[n].args):
            raise SpecError(f"{spec_name}: peer {n} is remote: {{repo}} names this PC's checkout, not a file there")
        if "save_copy" in grades and n in save_copy_peers(grades["save_copy"], names):
            raise SpecError(f"{spec_name}: grade save_copy: peer {n} is remote (its saves stay on the second PC)")
        if peers[n].save_slots:
            raise SpecError(f"{spec_name}: peer {n} is remote: save_slots fills a copy of the saves on this PC"
                            " (its saves stay on the second PC)")


def peers_of(layout: str) -> list[str]:
    """The games of a layout (S and the clients); a server+N+hK layout's synthetic peers are not games
    (synthetic_of)."""
    m = LAYOUT.match(layout)
    if not m:
        raise SpecError(f"unknown layout {layout!r} (solo, server+1 .. server+4, server+N+h1/h2, unit)")
    if layout == "solo":
        return ["H"]
    return ["S"] + CLIENTS[: int(m.group(2))]


def synthetic_of(layout: str) -> list[str]:
    """A server+N+hK layout's synthetic peers (h1 .. hK); none for any other layout."""
    m = LAYOUT.match(layout)
    return SYNTHETIC[: int(m.group(3))] if m and m.group(3) else []


def load_synthetic(name: str, d: dict, games: list[str], server: str) -> dict[str, SyntheticSpec]:
    """The [synthetic.<h>] tables of a spec: exactly one per synthetic peer of its layout, each with a
    script; a hosted server (`server = "host"`: the peer joins the port the runner gave the owner's
    game); `start_peer` one of the games; the patterns compile."""
    want = synthetic_of(d["layout"])
    table = d.get("synthetic", {})
    if not isinstance(table, dict):
        raise SpecError(f"{name}: [synthetic] is a table of synthetic peers ({', '.join(SYNTHETIC)})")
    if not want:
        if table:
            raise SpecError(f"{name}: [synthetic] needs a server+N+hK layout (not {d['layout']})")
        return {}
    if server != "host":
        raise SpecError(f"{name}: synthetic peers join a hosted server: say server = \"host\"")
    if sorted(table) != sorted(want):
        raise SpecError(f"{name}: layout {d['layout']} has synthetic peers {want}; the spec names {sorted(table)}")
    out: dict[str, SyntheticSpec] = {}
    for h in want:
        t = table[h]
        if not isinstance(t, dict):
            raise SpecError(f"{name}: [synthetic.{h}] must be a table")
        unknown = sorted(set(t) - set(SYNTHETIC_KEYS))
        if unknown:
            raise SpecError(f"{name}: [synthetic.{h}] does not know {', '.join(unknown)} ({', '.join(SYNTHETIC_KEYS)})")
        script = t.get("script", "")
        if not isinstance(script, str) or not script.strip():
            raise SpecError(f"{name}: synthetic peer {h} has no script")
        start_peer = t.get("start_peer", "S")
        if start_peer not in games:
            raise SpecError(f"{name}: synthetic peer {h}: start_peer {start_peer!r} is not one of {games}")
        if not isinstance(t.get("expect_count", {}), dict) or not isinstance(t.get("expect_order", []), list):
            raise SpecError(f"{name}: synthetic peer {h}: expect_count is a table, expect_order a list")
        s = SyntheticSpec(name=h, script=script, start_after=str(t.get("start_after", SYNTHETIC_START_AFTER)),
                          start_peer=start_peer, expect=list(t.get("expect", [])), forbid=list(t.get("forbid", [])),
                          expect_count=dict(t.get("expect_count", {})), expect_order=list(t.get("expect_order", [])))
        for rx, count in s.expect_count.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise SpecError(f"{name}: synthetic peer {h}: expect_count /{rx}/ must be a whole number >= 0")
        for rx in [s.start_after] + s.expect + s.forbid + list(s.expect_count) + s.expect_order:
            try:
                re.compile(rx)
            except (re.error, TypeError) as e:
                raise SpecError(f"{name}: synthetic peer {h}: bad pattern {rx!r}: {e}") from e
        out[h] = s
    return out


def spec_readiness(path: Path, d: dict, text: str) -> str:
    """Why a spec is not ready (Spec.not_ready), '' when it is: `ready = false` needs a `reason`;
    without a `ready` key, the first PLACEHOLDER comment line names it; `ready = true` runs it anyway."""
    ready, reason = d.get("ready"), d.get("reason", "")
    if ready is not None and not isinstance(ready, bool):
        raise SpecError(f"{path.name}: ready is true or false")
    if not isinstance(reason, str):
        raise SpecError(f"{path.name}: reason is a string (why the case is not ready)")
    if ready is False:
        if not reason.strip():
            raise SpecError(f"{path.name}: ready = false needs a `reason` (what the case waits for)")
        return reason.strip()
    if ready is None and (m := PLACEHOLDER.search(text)):
        line = text.count("\n", 0, m.start()) + 1
        what = m.group(1).strip().split(":", 1)[0].strip()
        return f"PLACEHOLDER at line {line}" + (f": {what}" if what else "")
    return ""


def parse_save_slots(spec_name: str, peer: str, value: object) -> dict[int, int]:
    """A peer's `save_slots` table: one-based slot numbers as strings, each to the one-based fixture slot
    whose save it gets (SAVE_SLOT_MAX at most), never a slot to itself, and never a slot that is filled
    and also copied from (fill_save_slots copies one after another in the peer's own copy, so the later
    copy would take the filled slot's new save, not the fixture's)."""
    if not isinstance(value, dict):
        raise SpecError(f"{spec_name}: peer {peer}: save_slots is a table (\"<slot>\" = <fixture slot>)")
    out: dict[int, int] = {}
    for key, src in value.items():
        if not re.fullmatch(r"[1-9]", str(key)) or int(key) > SAVE_SLOT_MAX:
            raise SpecError(f"{spec_name}: peer {peer}: save_slots: slot {key!r} is not 1 to {SAVE_SLOT_MAX}")
        if not isinstance(src, int) or isinstance(src, bool) or not 1 <= src <= SAVE_SLOT_MAX:
            raise SpecError(f"{spec_name}: peer {peer}: save_slots: slot {key} takes a fixture slot 1 to"
                            f" {SAVE_SLOT_MAX}, not {src!r}")
        if src == int(key):
            raise SpecError(f"{spec_name}: peer {peer}: save_slots: slot {key} from itself")
        out[int(key)] = src
    for slot in sorted(set(out) & set(out.values())):
        raise SpecError(f"{spec_name}: peer {peer}: save_slots: slot {slot} is filled and copied from")
    return out


def fill_save_slots(data: Path, title: str, name_pattern: str, slots: dict[int, int]) -> list[str]:
    """Stage 3 item 7: in a peer's own copy of the fixture saves (`data`, never the store or the owner's
    folder), each profile's save of fixture slot m is copied into slot n for every n = m of `slots`. A
    save is a content folder <title>/00000001/<name> with its files and a header
    <title>/Headers/00000001/<name>.header; <name> is `name_pattern` with {index} the zero-based slot
    (tests/skytest.toml [game] save_slot_name). The copy's files and header are renamed to the new name,
    and the header's file name field (XCONTENT_DATA's file_name, 42 bytes at SAVE_HEADER_NAME_AT, the
    SDK's content_manager.h) is rewritten to it: the SDK lists a content by its header's name, so a copy
    that kept the old name would be a second entry of the old slot. A header whose field does not hold
    the old name is refused, never guessed. Returns one line per copy."""
    if not name_pattern or "{index}" not in name_pattern:
        raise SpecError("save_slots needs tests/skytest.toml [game] save_slot_name (the slot's content name, with"
                        " {index} for the zero-based slot)")
    if not title:
        raise SpecError("save_slots needs tests/skytest.toml [game] title_id")
    done: list[str] = []
    for dst_slot, src_slot in sorted(slots.items()):
        src_name = name_pattern.format(index=src_slot - 1)
        dst_name = name_pattern.format(index=dst_slot - 1)
        copied = 0
        for folder in save_folders(data, title):
            src = folder / SAVE_CONTENT_TYPE / src_name
            if not src.is_dir():
                continue
            dst = folder / SAVE_CONTENT_TYPE / dst_name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            for f in sorted(dst.iterdir()):
                if f.is_file() and f.name.startswith(src_name):
                    f.rename(dst / (dst_name + f.name[len(src_name):]))
            header = folder / "Headers" / SAVE_CONTENT_TYPE / f"{src_name}.header"
            if header.is_file():
                raw = bytearray(header.read_bytes())
                field_bytes = bytes(raw[SAVE_HEADER_NAME_AT:SAVE_HEADER_NAME_AT + SAVE_HEADER_NAME_LEN])
                if len(field_bytes) < SAVE_HEADER_NAME_LEN or field_bytes.split(b"\0", 1)[0] != src_name.encode():
                    raise SpecError(f"save_slots: {header} does not hold the file name {src_name} at byte"
                                    f" {SAVE_HEADER_NAME_AT}: not copied (the header's layout is not the SDK's)")
                if len(dst_name.encode()) > SAVE_HEADER_NAME_LEN:
                    raise SpecError(f"save_slots: {dst_name} is longer than a header's {SAVE_HEADER_NAME_LEN} bytes")
                raw[SAVE_HEADER_NAME_AT:SAVE_HEADER_NAME_AT + SAVE_HEADER_NAME_LEN] = \
                    dst_name.encode().ljust(SAVE_HEADER_NAME_LEN, b"\0")
                (header.parent / f"{dst_name}.header").write_bytes(bytes(raw))
            copied += 1
            done.append(f"{folder.parent.name}: slot {dst_slot} ({dst_name}) from slot {src_slot} ({src_name})")
        if not copied:
            raise SpecError(f"save_slots: no profile of the fixture saves has slot {src_slot} ({src_name}) to copy"
                            f" into slot {dst_slot}")
    return done


def load_spec(path: Path, default_timeout: float) -> Spec:
    try:
        data = path.read_bytes()
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")  # as read_text reads it
        d = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SpecError(f"{path.name}: {e}") from e
    for key in ("name", "layout", "stage"):
        if key not in d:
            raise SpecError(f"{path.name}: `{key}` is missing")
    if d["layout"] == UNIT:
        return load_unit_spec(path, d, default_timeout, text, hashlib.sha256(data).hexdigest())
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
        kind = peer_kind(path.name, n, own, common)
        peers[n] = PeerSpec(
            name=n,
            script=script,
            args=list(common.get("args", [])) + list(own.get("args", [])),
            expect=list(common.get("expect", [])) + list(own.get("expect", [])),
            forbid=list(common.get("forbid", [])) + list(own.get("forbid", [])),
            expect_count={**common.get("expect_count", {}), **own.get("expect_count", {})},
            expect_order=list(own.get("expect_order", common.get("expect_order", []))),
            save_slots=parse_save_slots(path.name, n, own.get("save_slots", common.get("save_slots", {}))),
            kind=kind,
            settings_file=peer_settings_file(path.name, n, own, common, d.get("server", "standin"), kind),
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
    if server == "host" and "S" in peers and peers["S"].save_slots:
        raise SpecError(f"{path.name}: save_slots: a hosted server has no data folder of its own (Host copies the"
                        " owner's slot into its session folder)")
    check_join_code_peers(path.name, peers, server)
    synthetic = load_synthetic(path.name, d, names, server)
    grades = d.get("grade", {})
    for gname, g in grades.items():
        if gname not in GRADES:
            raise SpecError(f"{path.name}: grade {gname} is not one of {', '.join(GRADES)}")
        if gname == "remote_swap" and not remote_swap_ordered(g):
            check_remote_swap(path.name, g, names, list(synthetic))
            continue
        if gname == "save_copy":
            check_save_copy(path.name, g, names, server)
            continue
        if gname == DATA_ROOT_GRADE:
            check_data_root(path.name, g, names, server)
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
        if gname in backend_grades.NAMES:
            try:
                backend_grades.check(path.name, gname, g, names)
            except (backend_grades.GradeSpecError, re.error) as e:
                raise SpecError(str(e)) from e
            continue
        if gname == "remote_swap":
            try:
                remote_swap_grade.check_remote_swap(path.name, g, names)
            except remote_swap_grade.RemoteSwapSpecError as e:
                raise SpecError(str(e)) from e
            continue
        if gname == "exclusive":
            try:
                exclusive_grade.check_exclusive(path.name, g, names)
            except exclusive_grade.ExclusiveSpecError as e:
                raise SpecError(str(e)) from e
            continue
        if gname in ("occurrence_outcomes", "memory_growth"):
            # Stage 5's grades, each in its own module (its check_spec says what it takes).
            module = occurrence_outcomes if gname == "occurrence_outcomes" else memory_growth
            if why := module.check_spec(g, names):
                raise SpecError(f"{path.name}: grade {gname}: {why}")
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
    tolerate = d.get("tolerate", [])
    if not isinstance(tolerate, list) or not all(isinstance(t, str) and t for t in tolerate):
        raise SpecError(f"{path.name}: tolerate is a list of patterns (lines that are not a default fatal line)")
    for rx in tolerate:
        try:
            re.compile(rx)
        except re.error as e:
            raise SpecError(f"{path.name}: tolerate: bad pattern {rx!r}: {e}") from e
    if tolerate:
        try:  # as default_fatal composes them: a global inline flag such as (?i) breaks the lookahead there
            re.compile(tolerate_lookahead(tolerate) + ".*?(?:x)")
        except re.error as e:
            raise SpecError(f"{path.name}: tolerate: {e} (a global flag such as (?i) at a pattern's start does not"
                            " work inside the runner's composed pattern; say (?i:...) around the part instead)") from e
    not_ready = spec_readiness(path, d, text)
    breaker = d.get("breaker", True)
    if not isinstance(breaker, bool):
        raise SpecError(f"{path.name}: breaker is true or false")
    # A [[compare.run]]'s extra args too (check_compare checked their shape above).
    extra = [(n, a) for r in compare.get("run", []) for n, t in r.get("peer", {}).items() for a in t.get("args", [])]
    for n, a in [(n, a) for n, p in peers.items() for a in p.args] + extra:
        key = a.split("=", 1)[0]
        if key in JOBS_OWNED_ARGS:
            raise SpecError(f"{path.name}: peer {n} sets {key}: {JOBS_OWNED_ARGS[key]}")
    scenes, scenes_reason = d.get("scenes", ""), d.get("scenes_reason", "")
    if "scenes" in d and scenes not in SCENE_VALUES:
        raise SpecError(f"{path.name}: scenes = {scenes!r} (skip, play or movies)")
    if not isinstance(scenes_reason, str):
        raise SpecError(f"{path.name}: scenes_reason is a string (why the case plays its scenes)")
    if scenes == "play" and not scenes_reason.strip():
        raise SpecError(f'{path.name}: scenes = "play" needs a `scenes_reason` (which scene the case watches)')
    if scenes_reason.strip() and scenes != "play":
        raise SpecError(f'{path.name}: scenes_reason without scenes = "play"')
    if scenes:
        for n, a in [(n, a) for n, p in peers.items() for a in p.args] + extra:
            if a.split("=", 1)[0] == f"--{SCENE_SETTING}":
                raise SpecError(f"{path.name}: peer {n} sets --{SCENE_SETTING} in its args beside the spec's"
                                f" scenes key: the key says it (the runner passes --{SCENE_SETTING} itself)")
    check_remote_peers(path.name, peers, grades, names)
    build_copy = None
    if "build_copy" in d:
        try:
            build_copy = la.parse_build_edits(d["build_copy"], f"{path.name}: [build_copy]")
        except la.LaunchError as e:
            raise SpecError(str(e)) from None
        remote = [n for n, p in peers.items() if p.kind == "remote"]
        if remote:
            raise SpecError(f"{path.name}: [build_copy]: peer {', '.join(remote)} is remote and runs the second PC's"
                            " verified install, not a copy of this build")
    return Spec(
        name=d["name"], path=path, layout=d["layout"], stage=str(d["stage"]),
        description=d.get("description", ""), timeout=float(d.get("timeout", default_timeout)),
        peers=peers, measures=measures, standin_seconds=float(d.get("standin_seconds", 45)),
        server=server, grades=grades, compare=compare, fail_fast=fail_fast, show_server_window=show,
        exclusive=exclusive, tolerate=list(tolerate), not_ready=not_ready, scenes=scenes,
        scenes_reason=scenes_reason.strip(), breaker=breaker, sha256=hashlib.sha256(data).hexdigest(),
        synthetic=synthetic, build_copy=build_copy,
    )


# A unit case (stage 2 item 7; the stage 2 design section 9): no game runs. The [unit] table names
#   binary       the test binary, relative to the game's build folder; it and the DLLs beside it are
#                copied into a snapshot of their own and run from there, never from the build folder
#   pass_line    the line the binary prints when every test passed ("all backend tests passed");
#                the case passes on exit code 0 and that line, both
#   args         its arguments (optional); never one that writes into the source tree or does another
#                job than the suite (UNIT_REFUSED_ARGS)
#   ctest        ctest names run after it (optional), each alone and by its exact name, from
#   ctest_dir    the build folder's subfolder that registered them (relative to the build folder);
#   ctest_config the configuration for a multi-config build (optional, ctest -C)
UNIT_KEYS = ("binary", "pass_line", "args", "ctest", "ctest_dir", "ctest_config")
# --update-golden rewrites the golden files in the source tree; --diff and --list run no test.
UNIT_REFUSED_ARGS = ("--update-golden", "--diff", "--list")
CTEST_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# The binary's summary line (the portal's tests/backend_main.cpp): "<n> <suite> tests, <m> failed, <ms> ms".
UNIT_SUMMARY = re.compile(r"^(\d+) (\w+) tests, (\d+) failed, (\d+) ms")
# The keys that say how games run or are graded (a unit case starts none). `breaker` among them: a unit
# case never counts toward the circuit breaker nor is stopped by it (C7 guards the build's games).
GAME_ONLY_KEYS = ("peer", "grade", "measure", "compare", "server", "standin_seconds", "fail_fast",
                  "show_server_window", "tolerate", "scenes", "scenes_reason", "breaker", "build_copy")


def build_relative(name: str, what: str, value: object) -> str:
    """A path inside the build folder: relative, and never climbing out of it."""
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{name}: [unit] {what} must be a path inside the build folder")
    pp = Path(value)
    if pp.is_absolute() or pp.drive or pp.root or ".." in pp.parts:
        raise SpecError(f"{name}: [unit] {what} {value!r} must be relative to the build folder, inside it")
    return value


def load_unit_spec(path: Path, d: dict, default_timeout: float, text: str = "", sha256: str = "") -> Spec:
    """A unit case's spec. Of the 1B keys it takes `ready`/`reason` and PLACEHOLDER (PS2), `exclusive`
    (it runs alone in `test --jobs N`, still without a machine slot) and the text's sha256 (PS12)."""
    name = path.name
    game_only = [k for k in GAME_ONLY_KEYS if k in d]
    if game_only:
        raise SpecError(f"{name}: a unit case runs no game; {', '.join(game_only)} do not apply")
    u = d.get("unit")
    if not isinstance(u, dict):
        raise SpecError(f"{name}: a unit case needs a [unit] table (binary, pass_line)")
    unknown = sorted(set(u) - set(UNIT_KEYS))
    if unknown:
        raise SpecError(f"{name}: [unit] does not know {', '.join(unknown)} ({', '.join(UNIT_KEYS)})")
    build_relative(name, "binary", u.get("binary"))
    if not isinstance(u.get("pass_line"), str) or not u["pass_line"].strip():
        raise SpecError(f"{name}: [unit] pass_line (the line the binary prints when every test passed) is missing")
    args = u.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise SpecError(f"{name}: [unit] args is a list of strings")
    for a in args:
        if not a.strip() or (a.startswith("--") and a.endswith("=")):
            raise SpecError(f"{name}: [unit] refusing an empty argument {a!r}")
        if any(a == r or a.startswith(r + "=") for r in UNIT_REFUSED_ARGS):
            raise SpecError(f"{name}: [unit] refusing {a}: it writes into the source tree or runs no test")
    tests = u.get("ctest", [])
    if not isinstance(tests, list) or not all(isinstance(t, str) and CTEST_NAME.match(t) for t in tests):
        raise SpecError(f"{name}: [unit] ctest is a list of test names (letters, digits, '_', '.', '-')")
    if len(set(tests)) != len(tests):
        raise SpecError(f"{name}: [unit] ctest names a test twice")
    if tests:
        build_relative(name, "ctest_dir", u.get("ctest_dir"))
    elif "ctest_dir" in u or "ctest_config" in u:
        raise SpecError(f"{name}: [unit] ctest_dir and ctest_config need ctest names")
    if "ctest_config" in u and (not isinstance(u["ctest_config"], str) or not u["ctest_config"].strip()):
        raise SpecError(f"{name}: [unit] ctest_config must be a configuration name")
    exclusive = d.get("exclusive", False)
    if not isinstance(exclusive, bool):
        raise SpecError(f"{name}: exclusive is true or false")
    return Spec(
        name=d["name"], path=path, layout=UNIT, stage=str(d["stage"]), description=d.get("description", ""),
        timeout=float(d.get("timeout", default_timeout)), peers={}, measures={}, standin_seconds=0.0,
        unit={"binary": u["binary"], "pass_line": u["pass_line"].strip(), "args": list(args), "ctest": list(tests),
              "ctest_dir": u.get("ctest_dir", ""), "ctest_config": u.get("ctest_config", "")},
        exclusive=exclusive, not_ready=spec_readiness(path, d, text), breaker=False, sha256=sha256,
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
    left out (that peer's finals could not reach it), and on any log a key with a
    `request <key> not sent` line and no answer of any kind on that log (End session beat its send,
    so no answer can come); a key with any answer (even pending or a retryable refusal) is graded."""
    keys: dict[str, None] = {}
    finals: dict[str, list[str]] = {}
    applied: dict[str, int] = {}
    cached = retried = 0
    unlogged = 0
    not_sent: set[str] = set()
    # Keys this log shows any answer for (pending, cached and retryable included): such a key is
    # graded even with a `not sent` line (Sol council batch r2 C1, as in route_requests).
    any_answer: set[str] = set()
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
        any_answer.add(key)
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
    unsent = [k for k in keys if key_peer(k) not in gone_peers and k in not_sent and k not in any_answer]
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
    `peer` (the server's log, default S), `clients` (the clients whose level-guard lines must show
    no level left by itself; default every client) and `requesters` (the clients whose request keys
    must each end applied on the server or as an expected refusal; default `clients`)."""
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
    requesters = g.get("requesters", clients)
    if not isinstance(requesters, list) or any(c not in names or c == "S" for c in requesters):
        raise SpecError(f"{spec_name}: grade route: `requesters` must name clients of {names}")


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
    (manager), [[checkpoint]] (trigger, optional expect, a pattern on S), [[scene]] (name),
    [[expected_refusal]] (reason and why, optional kind and name: a refusal a requester's key may end
    with). Returns the checklist and "" or None and why it cannot be read."""
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        return None, f"the route file {path.name} cannot be read ({e})"
    end = d.get("level_end", {})
    if not isinstance(end, dict) or not isinstance(end.get("class"), str) or not end["class"]:
        return None, f"the route file {path.name} has no [level_end] class (X11 names it)"
    refusals = d.get("expected_refusal", [])
    if not isinstance(refusals, list):
        return None, f"the route file {path.name}: expected_refusal must be an array of tables ([[expected_refusal]])"
    for i, x in enumerate(refusals, 1):
        if (not isinstance(x, dict) or not isinstance(x.get("reason"), str) or not x["reason"].strip()
                or not isinstance(x.get("why"), str) or not x["why"].strip()):
            return None, f"the route file {path.name}: expected_refusal {i} needs a `reason` and a `why`"
        if x.get("kind", "") not in ("", *REQUEST_KINDS):
            return None, (f"the route file {path.name}: expected_refusal {i}: kind {x['kind']!r} is not one of"
                          f" {', '.join(REQUEST_KINDS)}")
        if "name" in x and (not isinstance(x["name"], str) or not x["name"].strip()):
            return None, f"the route file {path.name}: expected_refusal {i}: `name` must be a nonempty name"
    return {
        "level_end": end,
        "triggers": [t["name"] for t in d.get("trigger", []) if isinstance(t, dict) and isinstance(t.get("name"), str)
                     and t.get("class", "request") in ("request", "two_player")],
        "encounters": [e["manager"] for e in d.get("encounter", []) if isinstance(e, dict)
                       and isinstance(e.get("manager"), str)],
        "checkpoints": [c for c in d.get("checkpoint", []) if isinstance(c, dict) and isinstance(c.get("trigger"), str)],
        "scenes": [c["name"] for c in d.get("scene", []) if isinstance(c, dict) and isinstance(c.get("name"), str)],
        "expected_refusals": refusals,
    }, ""


def key_ref(key: str) -> str:
    """The world ref of a request key (its last part: the trigger's or messenger's ref)."""
    return key.split("/", 4)[4]


def route_requests(texts: dict[str, str], server_lines: list[str], checklist: dict,
                   requesters: list[str]) -> tuple[dict, list[str]]:
    """The route grade's request side (council 1B, Opus O5): every key a requester logged a
    `request` line for (its own log: the keys it sent) must end applied on the server, or with a
    refusal of the route file's [[expected_refusal]] list. A key's end is S's last decision that is
    not a cached repeat, a `pending` or a retryable refusal (a too_far the client resent and S then
    applied ends applied). A touch or exit refused with `enter refused` is accepted only when its
    enter key ended with an expected refusal (not when that enter ended applied: the touch or exit
    was then lost). A key the requester logged `not sent` for (End
    session beat its send) and S never saw at all (no replay, no decision of any kind) is left out
    and counted. An expected refusal matches
    by `reason` (the refusal reason S logs), `kind` when given, and `name` when given: the name S's
    replay line gave the same ref (`replay <peer>.<n>:<key> <Name#id> by`), case folded, `Name` or
    `Name#id`; a ref S never replayed has no name and matches no row with a `name`."""
    ends: dict[str, tuple[str, str]] = {}
    ref_names: dict[str, str] = {}
    # Every key S showed any sign of (a replay, or any decision, pending, cached or retryable
    # included): a `not sent` key S saw is still graded (Sol council batch C1: a resend refused by a
    # closing session after S's pending or too_far must not hide an unapplied request).
    seen_on_s: set[str] = set()
    for line in server_lines:
        if m := REPLAY_LINE.search(line):
            key = m.group(1).split(":", 1)[-1]
            if key.count("/") >= 4:
                ref_names.setdefault(key_ref(key), m.group(2))
                seen_on_s.add(key)
            continue
        d = decision_of(line)
        if not d:
            continue
        key, what, reason = d
        seen_on_s.add(key)
        if what.startswith("cached") or what == "pending":
            continue
        if what == "refused" and not refusal_final(key, reason):
            continue
        ends[key] = ("applied", "") if what == "applied" else ("refused", reason)

    def expected(key: str, reason: str) -> bool:
        name = ref_names.get(key_ref(key))
        for x in checklist.get("expected_refusals", []):
            if x["reason"].strip().casefold() != reason.casefold():
                continue
            if x.get("kind") and x["kind"] != key_kind(key):
                continue
            if x.get("name") and not (name and route_name_matches(x["name"].strip(), name)):
                continue
            return True
        return False

    def accepted(key: str) -> bool | None:
        """True: applied or an expected refusal; False: another refusal; None: no end on S."""
        end = ends.get(key)
        if end is None:
            return None
        if end[0] == "applied":
            return True
        if key_kind(key) in ("touch", "exit") and end[1] == "enter refused":
            # Only an enter that itself ended as an expected refusal: an enter that ended applied
            # (a resend after too_far) with its touch or exit refused on the way lost that key.
            g, peer, _, seq, ref = key.split("/", 4)
            enter = "/".join((g, peer, "enter", seq, ref))
            return ends.get(enter, ("",))[0] == "refused" and accepted(enter) is True
        return expected(key, end[1])

    graded: dict = {}
    reasons: list[str] = []
    for peer in requesters:
        keys: dict[str, None] = {}
        not_sent: set[str] = set()
        for line in texts.get(peer, "").splitlines():
            if m := REQUEST_LINE.search(line):
                keys.setdefault(m.group(4), None)
            elif m := REQUEST_NOT_SENT.search(line):
                not_sent.add(m.group(1))
        unsent = [k for k in keys if k in not_sent and k not in seen_on_s]
        graded_keys = [k for k in keys if k not in unsent]
        verdicts = {k: accepted(k) for k in graded_keys}
        none = [k for k, v in verdicts.items() if v is None]
        wrong = [k for k, v in verdicts.items() if v is False]
        graded[peer] = {"keys": len(graded_keys),
                        "applied": sum(1 for k in graded_keys if ends.get(k, ("",))[0] == "applied"),
                        "expected_refusals": sum(1 for k in graded_keys if verdicts[k] and ends[k][0] == "refused"),
                        "unexpected_refusals": len(wrong), "without_end": len(none), "left_out_not_sent": len(unsent)}
        if none:
            reasons.append(f"grade route: {len(none)} request key(s) of {peer} without an end on the server: "
                           f"{', '.join(none[:3])}{' ...' if len(none) > 3 else ''}")
        if wrong:
            shown = ", ".join(f"{k} ({ends[k][1]}{'; ' + ref_names[key_ref(k)] if key_ref(k) in ref_names else ''})"
                              for k in wrong[:3])
            reasons.append(f"grade route: {len(wrong)} request key(s) of {peer} refused on the server and not listed"
                           f" as an expected refusal: {shown}{' ...' if len(wrong) > 3 else ''}")
    return graded, reasons


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
    (`level end <class> from the server played here (generation <g>, ...`). Every request key of
    `requesters` (default `clients`) ends applied on the server or as an expected refusal of the file
    (route_requests)."""
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
    clients = g.get("clients", [p for p in texts if p != "S"])
    graded["requests"], why = route_requests(texts, lines, checklist, g.get("requesters", clients))
    reasons += why
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


def remote_swap_ordered(g: object) -> bool:
    """remote_swap has two forms behind one name. The ordered form (remote_swap_grade.py: `swaps` per
    swapper, `swappers` required, `peer` carries the result, default S) is chosen by its `swaps` or
    `peer` key; every other table is the min_swaps form below (synthetic swappers, `on` carries the
    result, default H)."""
    return isinstance(g, dict) and ("swaps" in g or "peer" in g)


def check_remote_swap(spec_name: str, g: object, names: list[str], synthetic: list[str]) -> None:
    """A remote_swap grade's options: `receivers` (game clients whose puppets are graded; default every
    client), `swappers` (clients and synthetic peers whose swaps are keys; default all of them), `on`
    (the client whose result carries the grade; default H), `min_swaps` (the swaps the spec orders, a
    whole number >= 1; default 1), `max_ms` (default tests/thresholds.toml [remote_swap] max_ms) and
    `hz` (the session's tick rate; default 60). A synthetic peer is never a receiver (it has no game)."""
    if not isinstance(g, dict):
        raise SpecError(f"{spec_name}: grade remote_swap must be a table")
    clients = [n for n in names if n != "S"]
    receivers = g.get("receivers", clients)
    if not isinstance(receivers, list) or not receivers or any(p not in clients for p in receivers):
        raise SpecError(f"{spec_name}: grade remote_swap: receivers must be clients of {clients} (a synthetic peer"
                        " has no game to receive on)")
    swappers = g.get("swappers", clients + synthetic)
    if not isinstance(swappers, list) or not swappers or any(p not in clients + synthetic for p in swappers):
        raise SpecError(f"{spec_name}: grade remote_swap: swappers must be among {clients + synthetic}")
    if g.get("on", "H") not in clients:
        raise SpecError(f"{spec_name}: grade remote_swap: `on` must be a client of {clients}")
    n = g.get("min_swaps", 1)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise SpecError(f"{spec_name}: grade remote_swap: min_swaps must be a whole number >= 1")
    for key in ("max_ms", "hz"):
        v = g.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            raise SpecError(f"{spec_name}: grade remote_swap: {key} must be a number > 0")


def peer_id_in(text: str) -> int | None:
    """A client's or synthetic peer's session peer id: its first `connected as peer <n>` (a game) or
    `synthetic peer <n> connected` line; None when it never connected."""
    for rx in (CONNECTED_AS, SYNTHETIC_CONNECTED):
        if m := rx.search(text):
            return int(m.group(1))
    return None


def grade_remote_swap(texts: dict[str, str], g: dict, thresholds: dict, names: list[str],
                      synthetic: list[str]) -> tuple[dict, list[str]]:
    """remote_swap (stage 4 item 5, D16; swap_pairs.py): every swap of every swapper is a key, every
    other receiver a pair graded against max_ms on the shared tick; a missing, duplicate or wrong-body
    completion fails, and so do fewer swaps than `min_swaps` ("not reached"). Peer ids come from each
    log's connected line; a peer that never connected is "not reached"."""
    clients = [n for n in names if n != "S"]
    receivers = list(g.get("receivers", clients))
    swappers = list(g.get("swappers", clients + synthetic))
    max_ms = float(g.get("max_ms", thresholds.get("remote_swap", {}).get("max_ms", 500.0)))
    hz = int(g.get("hz", 60))
    reasons: list[str] = []
    ids: dict[str, int] = {}
    for n in dict.fromkeys(receivers + swappers):
        pid = peer_id_in(texts.get(n, ""))
        if pid is None:
            reasons.append(f"grade remote_swap: not reached ({n} never logged its peer id)")
        else:
            ids[n] = pid
    logs = {ids[n]: texts.get(n, "") for n in ids}
    rec_ids = sorted({ids[n] for n in receivers if n in ids})
    # This form orders a minimum, not a count per swapper: the swaps each swapper logged (one per key)
    # become the case's order for swap_pairs.py, whose rules then hold for every one of them (every
    # pair graded, a slot-free line only on a pressure pair the case names). A swapper with no swap or
    # no receiver other than itself has no pairs; `min_swaps` holds the total.
    counts: dict[int, int] = {}
    for n in swappers:
        if n in ids:
            counts[ids[n]] = len({(m.group(1), m.group(3), m.group(4)) for line in texts.get(n, "").splitlines()
                                  if (m := swap_pairs.SWAP_RE.search(line))})
    swaps = sum(counts.values())
    ordered = {p: c for p, c in counts.items() if c and any(r != p for r in rec_ids)}
    res = swap_pairs.Result()
    if ordered:
        try:
            res = swap_pairs.grade_swap_pairs(logs, ordered, rec_ids, max_ms=max_ms, hz=hz, swappers=set(ordered))
        except ValueError as e:
            reasons.append(f"grade remote_swap: {e}")
    graded = {"max_ms": max_ms, "swaps": swaps, "expected_pairs": res.expected, "graded_pairs": res.graded,
              "pressure_pairs": res.pressure_pairs, "max_lag_ms": round(res.max_lag_ms, 1),
              "peers": {n: ids.get(n) for n in dict.fromkeys(receivers + swappers)}}
    min_swaps = int(g.get("min_swaps", 1))
    if swaps < min_swaps:
        reasons.append(f"grade remote_swap: not reached ({swaps} swap(s), the spec orders {min_swaps})")
    if res.failures:
        graded["violation"] = True
        reasons += [f"grade remote_swap: {f}" for f in res.failures[:12]]
        if len(res.failures) > 12:
            reasons.append(f"grade remote_swap: and {len(res.failures) - 12} more")
    elif res.graded != res.expected:
        reasons.append(f"grade remote_swap: {res.graded} of {res.expected} pairs graded")
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
        elif gname in backend_grades.NAMES and g.get("peer", "H" if gname == "backend_faults" else "S") == r.peer:
            # Stage 3 (backend_grades.py): compare joins every peer's local lines; faults and memory
            # read their peer's log.
            r.graded[gname], why = backend_grades.grade(gname, texts, g, r.peer)
            reasons += why
        elif gname == "remote_swap" and remote_swap_ordered(g) and g.get("peer", "S") == r.peer:
            # The ordered form (remote_swap_grade.py): every ordered swap on every named receiver, max_ms each.
            r.graded[gname], why = remote_swap_grade.grade_remote_swap(texts, g, thresholds or {}, list(spec.peers))
            reasons += why
        elif gname == "remote_swap" and not remote_swap_ordered(g) and g.get("on", "H") == r.peer:
            # `texts` holds the synthetic peers' output too (run_case), under their names.
            r.graded[gname], why = grade_remote_swap(texts, g, thresholds or {}, list(spec.peers), list(spec.synthetic))
            reasons += why
        elif gname == "exclusive" and g.get("peer", "S") == r.peer:
            # exclusive_grade.py: exactly one of the named clients won the race, every other lost.
            r.graded[gname], why = exclusive_grade.grade_exclusive(texts, g)
            reasons += why
        elif gname == "occurrence_outcomes" and g.get("peer", "S") == r.peer:
            # Stage 5: one outcome per S occurrence on each client, S's claim lines (occurrence_outcomes.py).
            r.graded[gname], why = occurrence_outcomes.grade_spec(texts, g, list(spec.peers))
            reasons += why
        elif gname == "memory_growth" and g.get("anchor_peer", "H") == r.peer:
            # Stage 5's soak: private memory's slope on each graded peer (memory_growth.py), once per run.
            r.graded[gname], why = memory_growth.grade_spec(texts, g, list(spec.peers))
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


def build_files(cfg: Config) -> list[Path]:
    """The build's exe and DLLs and its online play files, routes/*.toml (what a snapshot copies; a
    game whose Host requires routes/unsupported.toml beside the exe, Swap Force Online stage 8 item
    5), and the synthetic peer's exe when the game names one ([synthetic] exe) and the build has it;
    empty when the build folder has no exe."""
    exe = cfg.build / cfg.exe
    if not exe.is_file():
        return []
    syn = cfg.build / cfg.synthetic_exe if cfg.synthetic_exe else None
    return [exe] + sorted(cfg.build.glob("*.dll")) + sorted((cfg.build / "routes").glob("*.toml")) + \
        ([syn] if syn is not None and syn.is_file() else [])


def build_rel(cfg: Config, f: Path) -> Path:
    """A build file's place in the build folder and so in its snapshot (routes/x.toml stays there)."""
    try:
        return f.relative_to(cfg.build)
    except ValueError:
        return Path(f.name)


def build_hash(cfg: Config, files: list[Path] | None = None) -> str:
    """The build's snapshot name (each file's name, size and change time hashed): the folder under
    store/snapshots/ and results.json's `snapshot`; '' when there is no build. A per-build gate case
    counts the results recorded for this name (per_build_state)."""
    files = build_files(cfg) if files is None else files
    if not files:
        return ""
    h = hashlib.sha256()
    for f in files:
        st = f.stat()
        h.update(f"{build_rel(cfg, f).as_posix()}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:12]


def snapshot_build(cfg: Config) -> Path:
    """Copy the exe and DLLs into the store once per build, so a rebuild during a run changes
    nothing. Runs never start the build folder's exe."""
    files = build_files(cfg)
    if not files:
        raise SystemExit(f"no build at {cfg.build / cfg.exe}: build the release first (`just build-release`)")
    snap = cfg.store / "snapshots" / build_hash(cfg, files)
    check_output_path(cfg, snap)
    if snapshot_complete(snap, files, cfg):
        return snap
    # Each runner copies into its own partial folder (PS11: three lanes started on a new build in the
    # same second shared one `<hash>.partial`, deleted and renamed it under each other, and one failed
    # with FileExistsError), then renames it into place; when the rename fails because another runner
    # put the same snapshot there first, that one is used.
    tmp = snap.with_name(f"{snap.name}.partial-{os.getpid()}-{threading.get_ident()}")
    check_output_path(cfg, tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        tmp.mkdir(parents=True)
        for f in files:
            (tmp / build_rel(cfg, f)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, tmp / build_rel(cfg, f))
        try:
            tmp.rename(snap)
        except OSError as e:
            if not snapshot_complete(snap, files, cfg):
                raise SystemExit(f"could not put the snapshot in place at {snap} ({e}), and no complete one is"
                                 " there") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return snap


def snapshot_complete(snap: Path, files: list[Path], cfg: Config | None = None) -> bool:
    """A snapshot folder with every file of the build at its size (a rename puts a whole one in place)."""
    def rel(f: Path) -> Path:
        return build_rel(cfg, f) if cfg is not None else Path(f.name)
    try:
        return all((snap / rel(f)).is_file() and (snap / rel(f)).stat().st_size == f.stat().st_size for f in files)
    except OSError:
        return False


def case_build_copy(cfg: Config, spec: Spec, snap: Path, run: Path) -> Path:
    """A case's own copy of the build snapshot (its [build_copy], launch_args.copy_build), made fresh in
    the case's run folder: inside the store, beside the peers' folders, deleted with the run by the store's
    janitor (tools/test_store_janitor.py judges everything in runs/<stamp>-<case>/ as that run). The case's
    games start from it, and a hosted server too (Host starts its own exe). The shared snapshot is never
    changed. A refusal is a spec error: no game of the case starts."""
    dest = run / la.BUILD_COPY_DIR
    check_output_path(cfg, dest)
    try:
        copy = la.copy_build(snap.resolve(), run.resolve() / la.BUILD_COPY_DIR, spec.build_copy,
                             store=cfg.store.resolve(), exe=cfg.exe)
    except (la.LaunchError, OSError) as e:
        raise SpecError(f"[build_copy]: {e}") from None
    done = [f"removed {f}" for f in spec.build_copy.remove] + \
        [f"{f} cut before {' or '.join(m)}" for f, m in spec.build_copy.truncate]
    print(f"  {spec.name}: its own build copy {copy}: {', '.join(done)}", flush=True)
    return copy


def snapshot_unit(cfg: Config, binary: str) -> Path:
    """A unit case's test binary and the DLLs beside it, copied into a snapshot of their own (keyed
    by the files, as the game's is), so a rebuild during the run changes nothing. Returns the
    copied binary."""
    src = cfg.build / binary
    if not src.is_file():
        raise SpecError(f"no unit test binary at {src}: build it first")
    files = [src] + sorted(f for f in src.parent.glob("*.dll") if f != src)
    h = hashlib.sha256(f"unit:{binary}\n".encode())
    for f in files:
        st = f.stat()
        h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    snap = cfg.store / "snapshots" / f"unit-{h.hexdigest()[:12]}"
    check_output_path(cfg, snap)
    if snapshot_complete(snap, files):
        return snap / src.name
    # As snapshot_build (PS11): each runner and each case thread copies into its own partial folder and
    # renames it into place; a rename that loses to another one's complete snapshot uses that one.
    tmp = snap.with_name(f"{snap.name}.partial-{os.getpid()}-{threading.get_ident()}")
    check_output_path(cfg, tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        tmp.mkdir(parents=True)
        for f in files:
            shutil.copy2(f, tmp / f.name)
        try:
            tmp.rename(snap)
        except OSError as e:
            if not snapshot_complete(snap, files):
                raise SpecError(f"could not put the unit snapshot in place at {snap} ({e}), and no complete one"
                                " is there") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return snap / src.name


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
    # PS15: the wait for the machine's boot token before this game started, and how its boot ended
    # (BootHold.outcome: its first frame line, its exit, or BOOT_TIMEOUT_S).
    boot_wait_s: float = 0.0
    boot: str = ""


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
    # PS2: why the case is not ready (status `not ready`, 0 s, no game started); '' otherwise.
    not_ready: str = ""
    # PS12: what ran (case_provenance: the spec's sha256, the args hash, the fixture hash, the runner's
    # version) and under what load (LoadSampler.summary: games alive, memory, CPU users, power scheme).
    provenance: dict = field(default_factory=dict)
    load: dict = field(default_factory=dict)
    # PS6: the scene policy it ran with (scene_policy).
    scenes: str = ""
    # PS15: the machine slots it held (Lease.summary: slots, of, all, waited_s, waited_for).
    machine: dict = field(default_factory=dict)
    # C7: a failed case's fatal signature (case_signature: signature, peer, line, kind); {} otherwise.
    signature: dict = field(default_factory=dict)


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
    (fail_fast_patterns: its `forbid`, ALWAYS_FORBIDDEN and DEFAULT_FATAL), and the started games' exit
    codes. `hit` is the first fatal
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


def tolerate_lookahead(tolerate: list[str]) -> str:
    """The lookahead default_fatal puts before a default's pattern: no tolerate pattern on the line."""
    return f"^(?!.*(?:{'|'.join(f'(?:{t})' for t in tolerate)}))"


def default_fatal(spec: Spec, peer: str) -> list[tuple[str, str]]:
    """A peer's dead-session lines (DEFAULT_FATAL) for this spec, as (the pattern searched, the default
    it comes from). With a `tolerate` list the pattern skips a line that matches any of its patterns
    (a lookahead on the line, so fail fast's per-line search and the grade's search of the whole log
    with re.M agree); a teardown case drops SERVER_ENDED_BY_ITSELF."""
    out = []
    skip = tolerate_lookahead(spec.tolerate) if spec.tolerate else ""
    for who, rx in DEFAULT_FATAL:
        if who not in ("*", peer) or (rx == SERVER_ENDED_BY_ITSELF and "teardown" in spec.name):
            continue
        out.append((f"{skip}.*?(?:{rx})" if skip else rx, rx))
    return out


def peer_fatal_lines(spec: Spec, peer: str) -> list[tuple[str, str]]:
    """A peer's forbidden lines, as (pattern, what the grade names): its `forbid`, ALWAYS_FORBIDDEN and
    the dead-session defaults (default_fatal). Fail fast stops at them, and grade_peer fails on them."""
    ps = spec.peers.get(peer)
    out = [(rx, rx) for rx in (ps.forbid if ps else []) + ALWAYS_FORBIDDEN]
    return out + [(rx, f"{name} (a dead session's default fatal line)") for rx, name in default_fatal(spec, peer)]


def fail_fast_patterns(spec: Spec, peer: str) -> list[str]:
    """A peer's fatal lines for fail fast: exactly the forbidden lines grade_peer fails it on."""
    return [rx for rx, _ in peer_fatal_lines(spec, peer)]


def peer_script(spec: Spec, peer: str, figures: Path, join: str = "", joincode: str = "") -> str:
    if peer == "S":
        return STANDIN_SERVER_SCRIPT.format(seconds=int(spec.standin_seconds))
    return (spec.peers[peer].script.replace("{figures}", str(figures)).replace("{peer}", peer)
            .replace("{join}", join).replace(JOIN_CODE_PLACEHOLDER, joincode))


def peer_args(cfg: Config, args: list[str]) -> list[str]:
    """A peer's spec args as the game gets them: {repo} is the checkout under test (G3, stage 1B)."""
    return [a.replace("{repo}", cfg.repo.as_posix()) for a in args]


def launcher_words(launcher: list[str], exe: Path) -> list[str]:
    """[game] launcher (only the runner's own tests set one) with {exe}: the game exe the case runs, the
    shared snapshot's or the case's own build copy's ([build_copy]), so a test can see which."""
    return [w.replace("{exe}", str(exe)) for w in launcher]


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
    games it already started) and takes the turn when it returns; `starts` keeps each turn's time.
    With `machine` (PS15), machine_turn() then takes the machine's boot token: the same gap after any
    runner's last start, and no start while another game of any runner boots."""

    def __init__(self, gap: float, machine: "MachineBudget | None" = None) -> None:
        self.gap = max(0.0, float(gap))
        self.lock = threading.Lock()
        self.last: float | None = None
        self.starts: list[float] = []
        self.machine = machine

    def machine_turn(self, idle=lambda: None) -> "BootHold | None":
        return self.machine.boot_turn(self.gap, idle) if self.machine is not None else None

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


# --- what ran and under what load (PS12) -------------------------------------------------------------
#
# results.json records for each case what ran (`provenance`: the sha256 of the spec text read, a hash of the
# arguments the spec and the defaults give its peers, a hash of the fixture saves and figures, and the
# runner's own version) and under what load (`load`): the games alive machine-wide during the case
# (sampled every LOAD_SAMPLE_S: mean and max, and of them the ones this runner did not start: other
# lanes, other runners), the available memory (at the start and its lowest), the machine's CPU use and
# the top non-game CPU users over CPU_SAMPLE_S at the start, and the active power scheme. P1m's gate
# estimate took a pass that ran with 4.9 other games alive, and Discord, Defender and another app used
# 1.2-2.8 cores each beside the games. report.md prints one load line per case (load_line); the gate
# estimate counts only passes known to have run with no other game (pass_basis: a pass whose load was
# not recorded is unknown, not alone) and of the spec text and arguments now (gate_estimate).
LOAD_SAMPLE_S = 5.0
CPU_SAMPLE_S = 1.0
TOP_CPU = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
_LOAD_API: dict = {}


def _load_api() -> tuple:
    """kernel32 (its own instance) with the argument types the load sample uses: the process snapshot,
    process and system times, and the memory status."""
    if not _LOAD_API:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        class MEMSTATUS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        entry = ctypes.POINTER(PROCESSENTRY32W)
        u64 = ctypes.POINTER(ctypes.c_ulonglong)  # a FILETIME: 100 ns units
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, entry]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, entry]
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE, u64, u64, u64, u64]
        k32.GetSystemTimes.argtypes = [u64, u64, u64]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMSTATUS)]
        _LOAD_API.update(ctypes=ctypes, k32=k32, entry=PROCESSENTRY32W, memstatus=MEMSTATUS)
    return _LOAD_API["ctypes"], _LOAD_API["k32"], _LOAD_API["entry"], _LOAD_API["memstatus"]


def available_memory_gb() -> float | None:
    """The machine's available physical memory in GB (GlobalMemoryStatusEx); None off Windows or on error."""
    if not WINDOWS:
        return None
    try:
        ctypes, k32, _, memstatus = _load_api()
        st = memstatus()
        st.dwLength = ctypes.sizeof(memstatus)
        if k32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys / 1024**3
    except (OSError, AttributeError):
        pass
    return None


def process_table() -> list[tuple[int, int, str]] | None:
    """Every process on the machine as (pid, parent pid, exe name), from a Toolhelp32 snapshot; None off
    Windows or when the snapshot fails (tasklist_games is the fallback for the game count)."""
    if not WINDOWS:
        return None
    try:
        ctypes, k32, entry_type, _ = _load_api()
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return None
        try:
            e = entry_type()
            e.dwSize = ctypes.sizeof(entry_type)
            out = []
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
                ok = k32.Process32NextW(snap, ctypes.byref(e))
            return out
        finally:
            k32.CloseHandle(snap)
    except (OSError, AttributeError):
        return None


def tasklist_games(exe: str) -> int | None:
    """How many processes named exe run machine-wide, from tasklist (when there is no process snapshot)."""
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, errors="replace", timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return sum(1 for line in r.stdout.splitlines() if line.lower().startswith(f'"{exe.lower()}"'))


def count_games(table: list[tuple[int, int, str]], exe: str, runner_pid: int) -> tuple[int, int]:
    """(games alive, of them not started by this runner): processes named exe, and those that do not
    descend from runner_pid (a hosted server is its owner's child; a launcher's game its child)."""
    exe = exe.lower()
    parent = {pid: ppid for pid, ppid, _ in table}

    def ours(pid: int) -> bool:
        seen = set()
        while pid and pid not in seen and len(seen) < 64:  # a reused pid can make a loop: stop there
            if pid == runner_pid:
                return True
            seen.add(pid)
            pid = parent.get(pid, 0)
        return False

    games = [pid for pid, _, name in table if name.lower() == exe]
    return len(games), sum(1 for pid in games if not ours(pid))


def _process_cpu(table: list[tuple[int, int, str]]) -> dict[int, int]:
    """Each process's kernel + user time so far (100 ns units), for the ones that can be opened."""
    ctypes, k32, _, _ = _load_api()
    out = {}
    c, x, k, u = (ctypes.c_ulonglong() for _ in range(4))
    for pid, _, _ in table:
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x), ctypes.byref(k), ctypes.byref(u)):
                out[pid] = k.value + u.value
        finally:
            k32.CloseHandle(h)
    return out


def _system_times() -> tuple[int, int] | None:
    """(idle, busy + idle) of all cores so far (GetSystemTimes; its kernel time includes the idle time)."""
    ctypes, k32, _, _ = _load_api()
    i, k, u = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
        return None
    return i.value, k.value + u.value


def cpu_sample(game_exe: str, seconds: float | None = None) -> dict:
    """The machine's CPU use over `seconds` and its top non-game users, summed by exe name in cores
    (the runner itself and the games left out): {"cpu_busy_percent", "top_cpu": [{"name", "cores"}]}."""
    if not WINDOWS:
        return {}
    seconds = CPU_SAMPLE_S if seconds is None else seconds
    try:
        table = process_table() or []
        names = {pid: name for pid, _, name in table}
        s0, t0, w0 = _system_times(), _process_cpu(table), time.monotonic()
        time.sleep(seconds)
        s1, t1, w1 = _system_times(), _process_cpu(table), time.monotonic()
    except (OSError, AttributeError) as e:
        return {"cpu_error": f"{type(e).__name__}: {e}"}
    out: dict = {}
    if s0 and s1 and s1[1] > s0[1]:
        out["cpu_busy_percent"] = round(100.0 * (1 - (s1[0] - s0[0]) / (s1[1] - s0[1])), 1)
    by_name: dict[str, float] = {}
    skip = {game_exe.lower()}
    for pid, t in t1.items():
        name = names.get(pid, "")
        if pid == os.getpid() or pid not in t0 or name.lower() in skip:
            continue
        by_name[name] = by_name.get(name, 0.0) + (t - t0[pid]) / 1e7 / max(0.1, w1 - w0)
    top = sorted(((n, c) for n, c in by_name.items() if c >= 0.05), key=lambda x: -x[1])[:TOP_CPU]
    out["top_cpu"] = [{"name": n, "cores": round(c, 2)} for n, c in top]
    return out


def _active_scheme_api() -> str:
    """What `powercfg /getactivescheme` prints, from powrprof itself (PowerGetActiveScheme and
    PowerReadFriendlyName, no process started): `<name> (<guid>)`. Raises OSError when it cannot."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    pp = ctypes.WinDLL("powrprof")
    k32 = ctypes.WinDLL("kernel32")
    pp.PowerGetActiveScheme.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(GUID))]
    pp.PowerReadFriendlyName.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    guid = ctypes.POINTER(GUID)()
    if pp.PowerGetActiveScheme(None, ctypes.byref(guid)) != 0 or not guid:
        raise OSError("PowerGetActiveScheme failed")
    try:
        g = guid.contents
        text = (f"{g.d1:08x}-{g.d2:04x}-{g.d3:04x}-{bytes(g.d4[:2]).hex()}-{bytes(g.d4[2:]).hex()}")
        size = wintypes.DWORD(0)
        pp.PowerReadFriendlyName(None, guid, None, None, None, ctypes.byref(size))
        name = ""
        if size.value:
            buf = ctypes.create_string_buffer(size.value)
            if pp.PowerReadFriendlyName(None, guid, None, None, buf, ctypes.byref(size)) == 0:
                name = buf.raw[:size.value].decode("utf-16-le", errors="replace").rstrip("\0")
        return f"{name} ({text})" if name else text
    finally:
        k32.LocalFree(guid)


def power_scheme() -> str:
    """The active power scheme (`powercfg /getactivescheme`) as `<name> (<guid>)`, from powrprof, else
    from powercfg itself; '' when unknown."""
    if not WINDOWS:
        return ""
    try:
        return _active_scheme_api()
    except (OSError, AttributeError, ValueError):
        pass
    try:
        r = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, errors="replace",
                           timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})\s*\((.+?)\)", r.stdout)
    return f"{m.group(2).strip()} ({m.group(1)})" if m else r.stdout.strip()[:120]


class LoadSampler:
    """The load beside one case (PS12), in a thread of its own from start() to summary(): at the start the
    power scheme, a game count and a CPU sample (cpu_sample, CPU_SAMPLE_S), then a game count and the
    available memory every `every` seconds. Nothing in it ever stops a case."""

    def __init__(self, exe: str, every: float | None = None, cpu_seconds: float | None = None) -> None:
        self.exe = Path(exe).name
        self.every = LOAD_SAMPLE_S if every is None else every
        self.cpu_seconds = CPU_SAMPLE_S if cpu_seconds is None else cpu_seconds
        self.games: list[tuple[int, int | None]] = []  # (alive, not this runner's or None)
        self.memory: list[float] = []
        self.start_info: dict = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="skytest-load", daemon=True)

    def start(self) -> "LoadSampler":
        self._thread.start()
        return self

    def sample(self) -> None:
        table = process_table()
        if table is not None:
            self.games.append(count_games(table, self.exe, os.getpid()))
        elif (n := tasklist_games(self.exe)) is not None:
            self.games.append((n, None))
        if (m := available_memory_gb()) is not None:
            self.memory.append(m)

    def _run(self) -> None:
        try:
            self.start_info["power_scheme"] = power_scheme()
            self.sample()
            self.start_info.update(cpu_sample(self.exe, self.cpu_seconds))
            while not self._stop.wait(self.every):
                self.sample()
        except Exception as e:  # noqa: BLE001 - a load sample never stops a case
            self.start_info["error"] = f"{type(e).__name__}: {e}"

    def summary(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=15)
        out: dict = {"samples": len(self.games), "every_s": self.every}
        if self.games:
            alive = [g for g, _ in self.games]
            out["games_alive_mean"] = round(sum(alive) / len(alive), 1)
            out["games_alive_max"] = max(alive)
            outside = [o for _, o in self.games if o is not None]
            if len(outside) == len(self.games):
                out["outside_games_mean"] = round(sum(outside) / len(outside), 1)
                out["outside_games_max"] = max(outside)
        if self.memory:
            out["memory_available_gb_start"] = round(self.memory[0], 1)
            out["memory_available_gb_min"] = round(min(self.memory), 1)
        out.update({k: v for k, v in self.start_info.items()})
        return out


def load_line(load: dict) -> str:
    """report.md's load line for a case (LoadSampler.summary)."""
    if not load:
        return "Load: not recorded."
    parts = []
    if "games_alive_mean" in load:
        g = f"games alive {load['games_alive_mean']:.1f} mean, {load['games_alive_max']} max"
        if "outside_games_max" in load:
            g += f" ({load['outside_games_max']} max not started by this runner)"
        parts.append(g)
    if "memory_available_gb_start" in load:
        parts.append(f"memory {load['memory_available_gb_start']:.1f} GB free at the start, "
                     f"{load['memory_available_gb_min']:.1f} GB lowest")
    if "cpu_busy_percent" in load or load.get("top_cpu"):
        cpu = f"CPU {load.get('cpu_busy_percent', '?')} % busy at the start"
        if load.get("top_cpu"):
            cpu += ", top: " + ", ".join(f"{t['name']} {t['cores']:.2f} cores" for t in load["top_cpu"])
        parts.append(cpu)
    if load.get("power_scheme"):
        parts.append(f"power: {load['power_scheme']}")
    return "Load: " + ("; ".join(parts) if parts else "no sample") + "."


_RUNNER_VERSION: list[str] = []


def runner_version() -> str:
    """`skytest <sha256 of skytest.py, 12 hex>`: the runner that graded a case."""
    if not _RUNNER_VERSION:
        try:
            _RUNNER_VERSION.append("skytest " + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12])
        except OSError:
            _RUNNER_VERSION.append("skytest unknown")
    return _RUNNER_VERSION[0]


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def args_sha256(cfg: Config, spec: Spec) -> str:
    """A hash of the arguments a case's peers get that are not per run (the defaults, the server window,
    each peer's spec args and a [compare] run's extra args); the runner-owned paths and the port aside."""
    try:
        scenes = scene_args(cfg, spec)
    except SpecError as e:
        scenes = [f"refused: {e}"]
    parts = [cfg.default_args, scenes] if scenes else [cfg.default_args]
    for n, p in spec.peers.items():
        parts.append([n, show_server_args(cfg, spec, n), p.args])
    for r in spec.compare.get("run", []) if spec.compare else []:
        parts.append([r.get("name"), {n: t.get("args", []) for n, t in sorted(r.get("peer", {}).items())}])
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def fixture_sha256(cfg: Config) -> str:
    """A hash over the fixture saves and figures (every file's path, size and time: folder_state)."""
    saves, figures = fixture_dirs(cfg)
    return hashlib.sha256(f"{folder_state(saves)[2]}\n{folder_state(figures)[2]}".encode()).hexdigest()


def case_provenance(cfg: Config, spec: Spec, fixtures: str) -> dict:
    return {"spec_sha256": spec.sha256 or file_sha256(spec.path), "args_sha256": args_sha256(cfg, spec),
            "fixture_sha256": fixtures, "runner": runner_version()}


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


def setting_values(args: list[str], name: str) -> list[str]:
    """Every value of `--name=value` in args, in order."""
    return [a.split("=", 1)[1] for a in args if a.split("=", 1)[0] == f"--{name}" and "=" in a]


def spec_sets_scenes(spec: Spec) -> bool:
    """Whether a peer's spec args (a [compare] run's extra args too) set --dev_skip_scenes themselves."""
    args = [a for p in spec.peers.values() for a in p.args]
    args += [a for r in (spec.compare.get("run", []) if spec.compare else [])
             for t in r.get("peer", {}).values() for a in t.get("args", [])]
    return bool(setting_values(args, SCENE_SETTING))


def scene_args(cfg: Config, spec: Spec) -> list[str]:
    """PS6: what the runner adds to each game it starts for the spec's scene policy: the key's
    --dev_skip_scenes value (SCENE_VALUES). Nothing when the spec's own args set it (a spec without the
    key) or [defaults].args already says the same. Raises SpecError for a spec that says nothing (no key,
    no setting: which policy it means is unknown, and a scene case run with the skip would test the
    wrong thing), a [defaults].args value that differs from the key (it would reach a play spec), or a
    movies spec the game cannot run (Config.movies_skip)."""
    if spec.layout == UNIT:
        return []  # no game: no scene policy (GAME_ONLY_KEYS refuses the key)
    defaults = setting_values(cfg.default_args, SCENE_SETTING)
    if not spec.scenes:
        if spec_sets_scenes(spec) or defaults:
            return []
        raise SpecError(f'{spec.path.name}: no scenes key: say scenes = "skip" (playtests skip their scenes) or'
                        ' scenes = "play" with a scenes_reason (a case whose subject is a scene); the runner'
                        " assumes neither")
    if spec.scenes == "movies" and not cfg.movies_skip:
        raise SpecError(f'{spec.path.name}: scenes = "movies" needs the game\'s movies skip (tests/skytest.toml'
                        f" [game] movies_skip = true once --{SCENE_SETTING} takes `movies`)")
    want = SCENE_VALUES[spec.scenes]
    if defaults:
        if any(v != want for v in defaults):
            raise SpecError(f"{spec.path.name}: [defaults].args sets --{SCENE_SETTING}={defaults[-1]}, which would"
                            f' reach this scenes = "{spec.scenes}" spec: take it out of [defaults].args (the'
                            " scenes key says it per spec)")
        return []
    return [f"--{SCENE_SETTING}={want}"]


def scene_policy(cfg: Config, spec: Spec) -> str:
    """The scene policy a case ran with, for its result and report: the key, else where the setting
    came from."""
    if spec.layout == UNIT:
        return "none (a unit case starts no game)"
    if spec.scenes:
        return spec.scenes + (f" ({spec.scenes_reason})" if spec.scenes_reason else "")
    if spec_sets_scenes(spec):
        return "the spec's args (--" + SCENE_SETTING + "), no scenes key"
    if setting_values(cfg.default_args, SCENE_SETTING):
        return "[defaults].args (--" + SCENE_SETTING + "), no scenes key"
    return "none (no scenes key, no --" + SCENE_SETTING + "): refused"


def launch_tables(cfg: Config) -> tuple[la.AllowList, la.CommandTable]:
    """The game's two launch tables (Config.launch_allow, Config.script_commands): every setting a game
    may get, with its value kind, and every script command the game registers. Without them no game
    starts."""
    for path, what in ((cfg.launch_allow, "allow list"), (cfg.script_commands, "script command table")):
        if path is None or not path.is_file():
            raise SpecError(f"no launch {what} at {path}: the launch builder (launch_args.py) checks every"
                            " game's settings and input script against the game's own tables")
    try:
        return la.AllowList.load(cfg.launch_allow), la.CommandTable.load(cfg.script_commands)
    except (la.LaunchError, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SpecError(f"the launch tables do not read: {e}") from None


def launch_context(cfg: Config, folder: Path, tables: tuple[la.AllowList, la.CommandTable],
                   no_settings: bool = False) -> la.RunContext:
    """A development run's context for the launch builder: the worktree's store, the peer's fresh run
    folder, the configured disc (resolved: a worktree's rom\\ is a junction to the main checkout's),
    the settings template (none for a peer with settings_file = "none"), and the checkout's
    tests/routes/ as the one place besides the run folder a read-path setting
    (--online_route_file={repo}/tests/routes/...) may point."""
    return la.RunContext(kind="dev", store=cfg.store.resolve(), run_folder=folder, rom=cfg.rom.resolve(),
                         template=cfg.settings_template, allow=tables[0], commands=tables[1],
                         read_roots=[(cfg.repo / "tests" / "routes").resolve()], no_settings=no_settings)


def prepare_peer(cfg: Config, spec: Spec, peer: str, run: Path, port: int = 0) -> tuple[Path, list[str]]:
    """A local peer's own run folder and command line, made by the launch builder (launch_args.build_game,
    design D21): the copies of the fixture saves and figures, the settings template's copy, the input
    script (checked command by command, every path inside the folder), and the settings, checked as a
    whole. A refusal is a spec error: no game of the case starts. A {joincode} script is written with
    JOIN_CODE_STANDIN, which run_case swaps for the session's code (fill_join_code) before the peer
    starts."""
    check_output_path(cfg, run / peer)
    saves, figures = fixture_dirs(cfg)
    if not saves.is_dir() or not figures.is_dir():
        raise SpecError(f"missing fixtures ({saves}, {figures}): run `skytest.py fixtures --from-owner`")
    tables = launch_tables(cfg)
    try:
        d = la.make_run_folder(run.resolve(), peer)
        ctx = launch_context(cfg, d, tables, no_settings=spec.peers[peer].settings_file == "none")
        script = peer_script(spec, peer, ctx.figures, f"127.0.0.1:{port}" if port else "", JOIN_CODE_STANDIN)
        runner = [*cfg.default_args, *scene_args(cfg, spec), *show_server_args(cfg, spec, peer),
                  *([f"--online_port={port}"] if port and peer == "H" else []),
                  *synthetic_server_args(cfg, spec, peer)]
        args = la.build_game(ctx, peer_args(cfg, spec.peers[peer].args), script, fixtures=(saves, figures),
                             runner_args=runner)
    except la.LaunchError as e:
        raise SpecError(f"peer {peer}: {e}") from None
    (d / "data" / ".skytest-copy").write_text("a skytest run copy\n", encoding="ascii")  # the game's dev hooks need it
    if spec.peers[peer].save_slots:
        for line in fill_save_slots(d / "data", cfg.title_id, cfg.save_slot_name, spec.peers[peer].save_slots):
            print(f"  {spec.name} {peer}: save_slots: {line}")
    return d, args


# --- the remote peer (stage 8 item 6: a client on the second PC, through tools/skytest/remote_agent.py) -
#
# A spec's `kind = "remote"` peer runs on the second PC under its remote agent. The runner sends a run
# plan (one game, by role), and the agent builds and checks the command line with the same launch builder
# against its verified install, store and fixtures. RemotePeer stands in for the peer's process in
# run_case: its poll() is the heartbeat (the agent ends a run after three missed ones) and pulls the
# game's log into the local peer folder, where fail fast and the grades read it like any other.

REMOTE_STATUS_S = 2.0  # how often a remote peer's poll asks the agent (well inside the agent's 5 s heartbeat)
REMOTE_LOG_CHUNKS = 8  # 1 MiB chunks of the remote game's log one poll pulls at most
REMOTE_AGENT_ARGS = ("portal_mode", "audio_mute")  # the agent passes these itself (remote_agent.launch_plan)
REMOTE_SKIP = ("cache/",)  # what collect leaves on the second PC: the peer's shader cache
# The agent answers at most 200 requests a minute (remote_agent.REQUESTS_PER_MINUTE) and collect fetches
# one per file: a refusal for that rate is waited out, retried every REMOTE_RATE_RETRY_S for up to
# REMOTE_RATE_WAIT_S (past the agent's 60 s window).
REMOTE_RATE_RETRY_S = 1.0
REMOTE_RATE_WAIT_S = 65.0


class RemoteError(Exception):
    """A request the remote agent refused (its reason)."""


@dataclass
class RemoteTarget:
    host: str
    port: int
    token: bytes
    manifest: str      # the RC's payload record, which the agent verifies its install against
    counter: int = 0   # the last request counter sent: every new connection goes on above it


def remote_target(where: str, token_file: str | None, manifest_file: str | None,
                  ask=getpass.getpass) -> RemoteTarget:
    """`--remote HOST:PORT`, `--remote-manifest FILE` and the agent's token (32 hex digits, from
    `--remote-token-file` or typed: the agent shows it on the second PC's console)."""
    host, sep, port = where.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise SystemExit(f"--remote {where!r}: HOST:PORT (where the agent on the second PC listens)")
    if not manifest_file:
        raise SystemExit("--remote needs --remote-manifest: the RC's payload record the agent was started with")
    try:
        manifest = Path(manifest_file).read_text(encoding="utf-8")
        text = Path(token_file).read_text(encoding="utf-8") if token_file else \
            ask("The remote agent's token (on the second PC's console): ")
    except OSError as e:
        raise SystemExit(f"--remote: {e}") from None
    try:
        token = bytes.fromhex(text.strip())
    except ValueError:
        token = b""
    if len(token) != 16:
        raise SystemExit("the remote agent's token is 32 hex digits (128 bits), as its console shows it")
    return RemoteTarget(host.strip("[]"), int(port), token, manifest)


def remote_plan_name(case: str) -> str:
    """A run plan's name from the case's: the agent's run folder adds 21 characters to it and must stay a
    64-character name the agent's collect and clean accept."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", case).lstrip("._-")[:40] or "case"


class RemotePeer:
    """A remote peer, driven like a local process by run_case: poll() asks the agent at most every
    REMOTE_STATUS_S and pulls the new part of the game's log into the local peer folder; kill() stops the
    run; wait() polls until it ended; collect() fetches the run's files and cleans its folder on the
    second PC. A lost connection ends it with a problem the peer fails on (the agent ends the game three
    missed heartbeats later)."""

    def __init__(self, client, target: RemoteTarget, run_name: str, peer: str, folder: Path,
                 command: list[str], every: float = REMOTE_STATUS_S) -> None:
        self.client, self.target, self.run_name, self.peer = client, target, run_name, peer
        self.folder, self.command, self.every = folder, command, every
        self.returncode: int | None = None
        self.state = "running"
        self.problems: list[str] = []
        self._asked: float | None = None
        self._log = folder / "game.log"

    def _call(self, op: str, patient: bool = False, **kw) -> dict:
        """One request. `patient` (collect's): a refusal for the agent's request rate is waited out."""
        end = time.monotonic() + REMOTE_RATE_WAIT_S
        while True:
            r = self.client.call(op, **kw)
            self.target.counter = self.client.counter
            if r.get("ok"):
                return r
            why = str(r.get("error", "refused"))
            if not (patient and "requests a minute" in why and time.monotonic() < end):
                raise RemoteError(why)
            time.sleep(REMOTE_RATE_RETRY_S)

    def _reconnect(self) -> None:
        """A new connection to the agent, its counter above every one sent: the agent closes one idle for
        30 s (remote_agent.IDLE_DEADLINE_S), as when this peer's game ended long before the case's others."""
        with contextlib.suppress(OSError):
            self.client.close()
        self.client = remote_agent.Client(self.target.host, self.target.port, self.target.token,
                                          counter=max(time.time_ns(), self.target.counter), timeout=60.0)

    def _lost(self, e: Exception) -> None:
        if self.returncode is None:
            self.problems.append(f"the connection to the remote agent was lost ({e}); the agent ends the game"
                                 " after three missed heartbeats")
            self.returncode, self.state = 1, "lost"

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        now = time.monotonic()
        if self._asked is not None and now - self._asked < self.every:
            return None
        self._asked = now
        try:
            st = self._call("status")
            self.pull_log()
            if st.get("run") != self.run_name:
                self.problems.append(f"the remote agent runs {st.get('run')!r}, not this case's {self.run_name}")
                self.returncode, self.state = 1, "gone"
            elif st.get("state") != "running":
                self.state = str(st.get("state"))
                if self.state not in ("exited", "stopped"):  # the heartbeat or the run-time limit ended it
                    self.problems.append(f"the remote agent ended the run ({self.state})")
                self.pull_log()
                self.returncode = self._exit_code()
        except RemoteError as e:
            self.problems.append(f"the remote agent refused a status ({e})")
            self.returncode, self.state = 1, "refused"
        except (OSError, ValueError) as e:
            self._lost(e)
        return self.returncode

    def pull_log(self) -> None:
        """The game's log as far as it is written on the second PC, appended to the local copy."""
        have = self._log.stat().st_size if self._log.exists() else 0
        for _ in range(REMOTE_LOG_CHUNKS):
            try:
                r = self._call("collect", run=self.run_name, path=f"{self.peer}/game.log", offset=have)
            except RemoteError:
                return  # no log yet
            data = base64.b64decode(r.get("data", ""), validate=True)
            if data:
                with open(self._log, "ab") as f:
                    f.write(data)
                have += len(data)
            if r.get("eof", True) or not data:
                return

    def _exit_code(self) -> int:
        try:
            r = self._call("collect", run=self.run_name, path="exit.json", offset=0)
            code = json.loads(base64.b64decode(r.get("data", ""), validate=True).decode("utf-8"))["codes"][0]
        except (RemoteError, ValueError, KeyError, IndexError, TypeError):
            code = None
        if not isinstance(code, int):
            if self.state != "stopped":  # a stopped run's game may not have its code yet: it was ended
                self.problems.append(f"the remote run ended ({self.state}) without an exit code")
            return 1
        return code

    def kill(self) -> None:
        if self.returncode is not None:
            return
        try:
            self._call("stop")
        except RemoteError:
            pass  # it ended meanwhile: the next poll says how
        except (OSError, ValueError) as e:
            self._lost(e)

    def wait(self, timeout: float | None = None) -> int | None:
        end = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.command, timeout)
            time.sleep(0.25)
        return self.returncode

    def collect(self) -> None:
        """After the run: every file of the peer's folder on the second PC but its shader cache, and the run's
        exit record, into the local peer folder (the log whole again); then the folder there is cleaned.
        A file that does not come back is a problem the peer fails on."""
        try:
            if self.state in ("lost", "running"):
                return
            try:
                files = self._listing()
            except OSError:  # the agent closed the connection while it was idle: once more on a new one
                self._reconnect()
                files = self._listing()
            for f in files:
                rel = str(f["path"])
                sub = rel[len(self.peer) + 1:] if rel.startswith(self.peer + "/") else rel
                if sub != rel or rel == "exit.json":
                    if not any(sub.startswith(s) for s in REMOTE_SKIP):
                        self._fetch(rel, sub)
            self._call("clean", patient=True, run=self.run_name)
        except RemoteError as e:
            self.problems.append(f"collecting the remote run failed: {e}")
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.problems.append(f"collecting the remote run failed ({e})")
        finally:
            self.client.close()

    def _listing(self) -> list[dict]:
        """Every file of the run, page by page (the agent bounds each listing reply, remote_agent.LIST_MAX)."""
        files: list[dict] = []
        start = 0
        while True:
            r = self._call("collect", patient=True, run=self.run_name, **({"start": start} if start else {}))
            files += r["files"]
            nxt = r.get("next")
            if nxt is None:
                return files
            if not isinstance(nxt, int) or isinstance(nxt, bool) or nxt <= start:
                raise ValueError(f"the agent's file listing does not advance (next {nxt!r} after {start})")
            start = nxt

    def _fetch(self, rel: str, sub: str) -> None:
        parts = sub.split("/")
        if not sub or ":" in sub or "\\" in sub or any(x in ("", ".", "..") for x in parts):
            raise ValueError(f"the agent listed {rel!r}, which is no plain path inside the run")
        dest = self.folder.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        off = 0
        with open(dest, "wb") as out:
            while True:
                r = self._call("collect", patient=True, run=self.run_name, path=rel, offset=off)
                data = base64.b64decode(r.get("data", ""), validate=True)
                out.write(data)
                off += len(data)
                if r.get("eof", True) or not data:
                    return


def start_remote(cfg: Config, spec: Spec, peer: str, run: Path, port: int) -> RemotePeer:
    """Starts a `kind = "remote"` peer through the agent: verifies the install there if the agent has not
    yet, then sends the run plan (one game, by role). Its settings are the runner's defaults, the scene
    policy and the spec's, without the two the agent passes itself; its script names {figures} relative
    to its run folder (the game's working directory there) and {join} with this PC's address as the agent
    sees it, on the hosted session's port."""
    target = cfg.remote
    d = run / peer
    check_output_path(cfg, d)
    d.mkdir()
    try:
        client = remote_agent.Client(target.host, target.port, target.token,
                                     counter=max(time.time_ns(), target.counter), timeout=60.0)
    except OSError as e:
        raise SpecError(f"peer {peer}: the remote agent at {target.host}:{target.port} does not answer ({e})") from None
    rp = RemotePeer(client, target, "", peer, d, [])
    try:
        if not rp._call("hello").get("verified"):
            rp._call("verify", manifest=target.manifest)
        join = f"{client.sock.getsockname()[0]}:{port}" if port else ""
        args = [a for a in [*cfg.default_args, *scene_args(cfg, spec), *show_server_args(cfg, spec, peer),
                            *spec.peers[peer].args] if a[2:].split("=", 1)[0] not in REMOTE_AGENT_ARGS]
        plan = {"name": remote_plan_name(spec.name),
                "timeout_s": min(remote_agent.MAX_RUN_S, int(spec.timeout) + 120),
                "entries": [{"role": "game", "peer": peer, "args": args,
                             "script": peer_script(spec, peer, Path("figures"), join)}]}
        (d / "remote-plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
        rp.run_name = str(rp._call("start", plan=plan)["run"])
    except RemoteError as e:
        client.close()
        raise SpecError(f"peer {peer}: the remote agent refused: {e}") from None
    except (OSError, ValueError, KeyError) as e:
        client.close()
        raise SpecError(f"peer {peer}: the remote agent's connection failed ({e})") from None
    rp.command = [f"remote {target.host}:{target.port}", *args]
    return rp


def synthetic_server_args(cfg: Config, spec: Spec, peer: str) -> list[str]:
    """A server+N+hK case's owner H gets the game's synthetic-peer server setting ([synthetic]
    server_setting) turned on, which Host passes to its server; a spec may not set it itself (the
    runner owns it, as the port). Nothing for any other case or peer."""
    setting = cfg.synthetic_setting
    if setting and any(setting_values(p.args, setting) for p in spec.peers.values()):
        raise SpecError(f"{spec.path.name}: a peer sets --{setting}; the runner sets it for a server+N+hK case")
    if not spec.synthetic or peer != "H":
        return []
    if not setting:
        raise SpecError(f"{spec.path.name}: synthetic peers need tests/skytest.toml [synthetic] server_setting (the"
                        " server setting that lets them join)")
    return [f"--{setting}=true"]


def prepare_synthetic(cfg: Config, spec: Spec, name: str, run: Path, snap: Path, port: int) -> tuple[Path, list[str]]:
    """A synthetic peer's folder (its script.txt) and its command: [synthetic] exe from the snapshot
    (or the tests' launcher) and the args template with {port} (the hosted server's), {script},
    {digest} (the SHA-256 of the snapshot's game exe: the build digest the server checks in a Hello),
    {server} (127.0.0.1: loopback only) and {peer}. Every argument is non-empty, and the line passes
    the launch builder's synthetic-peer rules (launch_args.check_synthetic_args: the session address,
    the script inside the peer's fresh run folder, the digest contract); a refusal is a spec error."""
    if not cfg.synthetic_exe and not cfg.synthetic_launcher:
        raise SpecError(f"{spec.path.name}: synthetic peers need tests/skytest.toml [synthetic] exe")
    if not port:
        raise SpecError(f"{spec.path.name}: synthetic peers need a hosted server's port")
    check_output_path(cfg, run / name)
    try:
        d = la.make_run_folder(run.resolve(), name)
        ctx = launch_context(cfg, d, launch_tables(cfg))
        la.check_synthetic_script(spec.synthetic[name].script.encode("utf-8"))
    except la.LaunchError as e:
        raise SpecError(f"synthetic peer {name}: {e}") from None
    script = d / la.SCRIPT_FILE
    script.write_text(spec.synthetic[name].script, encoding="utf-8")
    exe = snap / cfg.synthetic_exe if cfg.synthetic_exe else None
    if not cfg.synthetic_launcher and (exe is None or not exe.is_file()):
        raise SpecError(f"no synthetic peer at {cfg.build / cfg.synthetic_exe}: build it first")
    game = snap / cfg.exe
    digest = file_sha256(game) if game.is_file() else ""
    subs = {"{port}": str(port), "{script}": str(script), "{server}": "127.0.0.1", "{peer}": name,
            "{digest}": digest}
    args = []
    for a in cfg.synthetic_args:
        for ph, value in subs.items():
            a = a.replace(ph, value)
        if not a.strip():
            raise SpecError(f"{spec.path.name}: synthetic peer {name}: an argument came out empty")
        args.append(a)
    try:
        la.check_synthetic_args(ctx, args, f"127.0.0.1:{port}", digest)
    except la.LaunchError as e:
        raise SpecError(f"{spec.path.name}: synthetic peer {name}: {e}") from None
    cmd = [*cfg.synthetic_launcher, *args] if cfg.synthetic_launcher else [str(exe), *args]
    return d, cmd


def grade_synthetic(spec: Spec, r: PeerResult, text: str, started: bool) -> None:
    """A synthetic peer's grade: it started (its start condition showed), said it connected, ended by
    itself with exit code 0, logged none of its `forbid` lines nor SYNTHETIC_FATAL, and every `expect`,
    `expect_count` and `expect_order` of its table holds."""
    s = spec.synthetic[r.peer]
    reasons = []
    if not started:
        reasons.append(f"never started: {s.start_peer} never logged /{s.start_after}/")
    else:
        if r.ended_by_runner:
            reasons.append(r.stopped_by or f"did not end within {SYNTHETIC_EXIT_GRACE:.0f} s of the games' end; ended by"
                           " the runner")
        elif r.exit_code not in (0, None):
            reasons.append(f"exit code {r.exit_code}")
        if not SYNTHETIC_CONNECTED.search(text):
            reasons.append(f"log lacks /{SYNTHETIC_CONNECTED.pattern}/ (it never joined)")
        for rx in s.expect:
            if not re.search(rx, text, re.M):
                reasons.append(f"log lacks /{rx}/")
        reasons += check_count_and_order(text, s)  # type: ignore[arg-type]  (the same two fields)
        for rx in s.forbid + SYNTHETIC_FATAL:
            if m := re.search(rx, text, re.M):
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                reasons.append(f"log has forbidden /{rx}/: {text[start:end if end != -1 else len(text)].strip()[:160]}")
    r.reasons = reasons
    r.status = "pass" if not reasons else "fail"


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
        if gname in ("requests_answered", "symmetry", "save_copy", "route", "bandwidth", "remote_swap", "exclusive",
                     DATA_ROOT_GRADE):
            continue  # graded across peers (grade_cross_peer, grade_save_copy, grade_data_root)
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


# --- the data_root_untouched grade (stage 7 item 9, future/online-7.md 3.9) ---------------------------
# Each named peer's files are hashed before its game starts, at each named phase boundary and after the
# run, and each phase's changes are compared with that phase's exact allowed list. Graded: the peer's
# user data root (data/), its figure copy (figures/), the figure backups the portal keeps next to it
# (Backups/: portal/figure_file.cpp FigureBackupDir, Backups\Figures beside the figures folder) and its
# settings file (settings.toml). Left out: logs (*.log, e.g. a hosted server's game.log in the owner's
# Online/session) and the runner's own files outside those (script, stderr, screenshots, the cache).
#
#   [grade.data_root_untouched]
#   peers = ["H", "D"]       the clients graded (default: every client; a hosted S has no data root)
#   by = "D"                 whose log carries the phase lines (default: the first of peers)
#   [[grade.data_root_untouched.phase]]
#   name = "session"         every phase but the last ends at the line `Input script: data root phase
#                            <name> done` in by's log (the script's `log data root phase <name> done`;
#                            the runner hashes within one poll of it, so the script waits 2 s before
#                            its next step); the last phase ends with the run
#   allow = { H = ["slot SWAPFORCE_0", "online", "import_journal"], D = ["figure Roller Brawl"] }
#   require_changed = { D = ["slot SWAPFORCE_0"] }   entries that must show a change in this phase
#
# Without phase tables the grade is one phase, "run", with `allow` and `require_changed` at its top.
# An allowed entry names files, never a folder of saves (the design's "by name only"):
#   slot [<profile>/]<SLOT>   <data>/<profile>/<title>/00000001/<SLOT>/<SLOT>.xsg, changed only (an
#                             import replaces it in place); without a profile, the one profile that has
#                             that slot before the phase (more than one: the entry must name it)
#   header [<profile>/]<SLOT> <data>/<profile>/<title>/Headers/00000001/<SLOT>.header, changed only (an
#                             offline phase's native save; never in a session phase: the import never
#                             writes the header)
#   online                    anything under data/Online/
#   import_journal            data/save-backup/import/ and data/save-backup/previous-import/
#   import_orphaned           data/save-backup/import-orphaned/ (only a case that makes an orphan)
#   native_journal            data/save-backup/pending/ and data/save-backup/previous/ (ReplaceContent's
#                             own; an offline phase's native save)
#   figure <name>             figures/<name>.dump, changed only, and anything under
#                             Backups/Figures/<name>/ (the figure the peer played, if figures earn: CQ1)
#   file <path>               that one path, relative to the peer's run folder
DATA_ROOT_GRADE = "data_root_untouched"
DATA_ROOT_TREES = ("data", "figures", "Backups")
DATA_ROOT_FILES = ("settings.toml",)
DATA_ROOT_PHASE_LINE = re.compile(r"Input script: data root phase (\S+) done\s*$")
DATA_ROOT_PHASE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
DATA_ROOT_SLOT = re.compile(r"^(?:([0-9A-Fa-f]{16})/)?([A-Za-z0-9_]+)$")
DATA_ROOT_PREFIXES = {
    "online": ("data/Online/",),
    "import_journal": ("data/save-backup/import/", "data/save-backup/previous-import/"),
    "import_orphaned": ("data/save-backup/import-orphaned/",),
    "native_journal": ("data/save-backup/pending/", "data/save-backup/previous/"),
}
DATA_ROOT_CLASSES = ("slot", "header", "figure", "file", *DATA_ROOT_PREFIXES)
DATA_ROOT_SAVE_TYPE = "00000001"  # the saved-game content type's folder (content_manager.cpp)


def data_root_entry(entry: object) -> tuple[str, str]:
    """An allowed entry as (class, argument); ValueError when it is not one."""
    if not isinstance(entry, str):
        raise ValueError(f"{entry!r} is not a string")
    cls, _, arg = entry.strip().partition(" ")
    arg = arg.strip()
    if cls not in DATA_ROOT_CLASSES:
        raise ValueError(f"{entry!r}: {cls!r} is not one of {', '.join(DATA_ROOT_CLASSES)}")
    if cls in DATA_ROOT_PREFIXES:
        if arg:
            raise ValueError(f"{entry!r}: {cls} takes no argument")
    elif not arg:
        raise ValueError(f"{entry!r}: {cls} needs a name")
    elif cls in ("slot", "header") and not DATA_ROOT_SLOT.match(arg):
        raise ValueError(f"{entry!r}: a slot is SWAPFORCE_<n> or <16 hex digit profile>/SWAPFORCE_<n>")
    elif cls in ("figure", "file") and (".." in Path(arg).parts or Path(arg).is_absolute() or "\\" in arg
                                         or (cls == "figure" and "/" in arg)):
        raise ValueError(f"{entry!r}: a {cls} is a plain name{' or relative path' if cls == 'file' else ''}")
    return cls, arg


def data_root_phases(g: dict) -> list[dict]:
    """The grade's phases in order, each {name, allow, require_changed}: its `phase` tables, or one phase
    `run` from the grade's own `allow` and `require_changed`."""
    if "phase" in g:
        return [{"name": p.get("name"), "allow": p.get("allow", {}), "require_changed": p.get("require_changed", {})}
                for p in g["phase"]]
    return [{"name": "run", "allow": g.get("allow", {}), "require_changed": g.get("require_changed", {})}]


def data_root_peers(g: dict, names: list[str]) -> list[str]:
    """The peers a data_root_untouched grade hashes: its `peers`, or every client."""
    return list(g.get("peers", [n for n in names if n != "S"]))


def check_data_root(spec_name: str, g: object, names: list[str], server: str) -> None:
    def bad(why: str) -> SpecError:
        return SpecError(f"{spec_name}: grade {DATA_ROOT_GRADE}: {why}")
    if not isinstance(g, dict):
        raise bad("must be a table")
    unknown = set(g) - {"peers", "by", "phase", "allow", "require_changed", "title"}
    if unknown:
        raise bad(f"unknown keys {sorted(unknown)}")
    peers = g.get("peers", [n for n in names if n != "S"])
    if not isinstance(peers, list) or not peers or any(p not in names for p in peers) or len(set(peers)) != len(peers):
        raise bad(f"`peers` must name peers of {names}, each once")
    if server == "host" and "S" in peers:
        raise bad("a hosted server has no data folder of its own (its files are in the owner's Online/session)")
    by = g.get("by", peers[0])
    if by not in names or by == "S":
        raise bad("`by` names the client whose script logs the phase lines")
    if "phase" in g and ("allow" in g or "require_changed" in g):
        raise bad("with phase tables, `allow` and `require_changed` go into each phase")
    if "phase" in g and (not isinstance(g["phase"], list) or not g["phase"]):
        raise bad("`phase` is a list of tables ([[grade.data_root_untouched.phase]])")
    if "title" in g and not TITLE_ID.match(str(g["title"])):
        raise bad("`title` is a title id, eight hex digits")
    seen: set[str] = set()
    for p in g.get("phase", []):
        if not isinstance(p, dict) or set(p) - {"name", "allow", "require_changed"}:
            raise bad("a phase has `name`, `allow` and `require_changed` only")
    for p in data_root_phases(g):
        name = p["name"]
        if not isinstance(name, str) or not DATA_ROOT_PHASE_NAME.match(name) or name in seen:
            raise bad(f"phase name {name!r}: letters, digits, _ and -, each name once")
        seen.add(name)
        for key in ("allow", "require_changed"):
            table = p[key]
            if not isinstance(table, dict) or any(n not in peers for n in table):
                raise bad(f"phase {name}: `{key}` is a table of graded peers ({peers}) to lists of entries")
            for n, entries in table.items():
                if not isinstance(entries, list):
                    raise bad(f"phase {name}: {key}.{n} is a list of entries")
                for e in entries:
                    try:
                        data_root_entry(e)
                    except ValueError as err:
                        raise bad(f"phase {name}: {key}.{n}: {err}") from err
            if key == "require_changed":
                for n, entries in table.items():
                    allowed = set(p["allow"].get(n, []))
                    for e in entries:
                        if e not in allowed:
                            raise bad(f"phase {name}: require_changed.{n} entry {e!r} is not in its allow list")


def tree_state(peer_dir: Path) -> dict[str, str]:
    """Every file the grade watches in a peer's run folder (DATA_ROOT_TREES, DATA_ROOT_FILES): its path
    relative to that folder (forward slashes) and the SHA-256 of its bytes. Logs are left out; links are
    never followed."""
    out: dict[str, str] = {}

    def add(full: Path) -> None:
        if full.is_symlink() or full.suffix.lower() == ".log":
            return
        h = hashlib.sha256()
        try:
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError as e:
            h.update(f"unreadable: {e}".encode())
        out[full.relative_to(peer_dir).as_posix()] = h.hexdigest()

    for top in DATA_ROOT_TREES:
        root = peer_dir / top
        if not root.is_dir() or root.is_symlink():
            continue
        for d, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
            for f in files:
                add(Path(d) / f)
    for f in DATA_ROOT_FILES:
        if (peer_dir / f).is_file():
            add(peer_dir / f)
    return out


def data_root_profiles(state: dict[str, str], title: str, cls: str, slot: str) -> list[str]:
    """The profiles whose slot (or header) file is in `state`."""
    found = set()
    for path in state:
        parts = path.split("/")
        if cls == "slot" and len(parts) == 6 and parts[0] == "data" and parts[2] == title and \
                parts[3] == DATA_ROOT_SAVE_TYPE and parts[4] == slot and parts[5] == f"{slot}.xsg":
            found.add(parts[1])
        if cls == "header" and len(parts) == 6 and parts[0] == "data" and parts[2] == title and \
                parts[3] == "Headers" and parts[4] == DATA_ROOT_SAVE_TYPE and parts[5] == f"{slot}.header":
            found.add(parts[1])
    return sorted(found)


def data_root_matcher(entry: str, before: dict[str, str], title: str):
    """(a function of (path, how) that says whether the entry allows that change, or None, and why not)
    how is changed, added or removed. A slot or header entry without a profile resolves against the
    phase's starting state; a figure or slot entry allows only a change of the existing file."""
    cls, arg = data_root_entry(entry)
    if cls in DATA_ROOT_PREFIXES:
        prefixes = DATA_ROOT_PREFIXES[cls]
        return (lambda path, how: path.startswith(prefixes)), ""
    if cls == "file":
        return (lambda path, how: path == arg), ""
    if cls == "figure":
        dump, backups = f"figures/{arg}.dump", f"Backups/Figures/{arg}/"
        return (lambda path, how: (path == dump and how == "changed") or path.startswith(backups)), ""
    m = DATA_ROOT_SLOT.match(arg)
    profile, slot = m.group(1), m.group(2)
    if profile is None:
        profiles = data_root_profiles(before, title, cls, slot)
        if len(profiles) != 1:
            return None, (f"{entry!r} names no profile and {len(profiles)} profiles have it"
                          f"{' (' + ', '.join(profiles) + ')' if profiles else ''}; name one as <profile>/{slot}")
        profile = profiles[0]
    if cls == "slot":
        want = f"data/{profile}/{title}/{DATA_ROOT_SAVE_TYPE}/{slot}/{slot}.xsg"
    else:
        want = f"data/{profile}/{title}/Headers/{DATA_ROOT_SAVE_TYPE}/{slot}.header"
    return (lambda path, how: path == want and how == "changed"), ""


def data_root_phase_result(before: dict[str, str], after: dict[str, str], allow: list[str],
                           require: list[str], title: str) -> dict:
    """One phase of one peer: every change (changed, added, removed), which entry allowed it, the changes
    no entry allows, the required entries that saw no change, and the entries that could not be read."""
    changes = sorted([(p, "changed") for p in before.keys() & after.keys() if before[p] != after[p]] +
                     [(p, "added") for p in after.keys() - before.keys()] +
                     [(p, "removed") for p in before.keys() - after.keys()])
    matchers, problems = {}, []
    for e in allow:
        fn, why = data_root_matcher(e, before, title)
        if fn is None:
            problems.append(why)
        else:
            matchers[e] = fn
    used: dict[str, int] = {e: 0 for e in matchers}
    not_allowed = []
    for path, how in changes:
        hit = next((e for e, fn in matchers.items() if fn(path, how)), None)
        if hit is None:
            not_allowed.append(f"{how} {path}")
        else:
            used[hit] += 1
    missing = [e for e in require if used.get(e, 0) == 0 and e in matchers]
    return {"files_before": len(before), "files_after": len(after),
            "changes": [f"{how} {path}" for path, how in changes], "not_allowed": not_allowed,
            "missing_required": missing, "problems": problems}


class DataRootWatch:
    """The grade's snapshots over one run: the start (before any game), each phase line in `by`'s log
    (LogTail) in order, and the end. `problems` collects phase lines out of order and phases that never
    ended; snapshot i is phase i's start."""

    def __init__(self, run: Path, g: dict, names: list[str], log: Path) -> None:
        self.run, self.phases = run, data_root_phases(g)
        self.peers = data_root_peers(g, names)
        self.tail = LogTail(log)
        self.snaps: list[dict[str, dict[str, str]]] = [self._snap()]
        self.problems: list[str] = []
        self.finished = False

    def _snap(self) -> dict[str, dict[str, str]]:
        return {n: tree_state(self.run / n) for n in self.peers}

    def check(self) -> None:
        """Takes the snapshot of each phase line logged since the last call (the run loop calls it)."""
        for line in self.tail.new_lines():
            m = DATA_ROOT_PHASE_LINE.search(line)
            if not m:
                continue
            running = len(self.snaps) - 1
            if running >= len(self.phases) - 1 or self.phases[running]["name"] != m.group(1):
                expected = self.phases[running]["name"] if running < len(self.phases) - 1 else "none (the last" \
                    " phase ends with the run)"
                self.problems.append(f"phase line `{m.group(1)}` out of order (expected {expected})")
                continue
            self.snaps.append(self._snap())

    def finish(self) -> None:
        """After every game ended: the lines not read yet, then the end's snapshot."""
        if self.finished:
            return
        self.finished = True
        self.check()
        ended = len(self.snaps) - 1
        for p in self.phases[ended:-1]:
            self.problems.append(f"phase {p['name']} did not end: no `data root phase {p['name']} done` line")
        self.snaps.append(self._snap())


def grade_data_root(r: PeerResult, watch: DataRootWatch, title: str) -> list[str]:
    """The data_root_untouched grade for one peer (stage 7 item 9): each phase that has both ends
    graded against its own allowed list. Any change no entry allows fails and names the file."""
    reasons: list[str] = []
    phases_out = []
    reached = min(len(watch.snaps) - 1, len(watch.phases))
    # A phase that never ended: the run's end closes the phase it was in, and the phases after it have
    # no snapshot of their own (watch.problems names them).
    for i in range(reached):
        p = watch.phases[i]
        res = data_root_phase_result(watch.snaps[i][r.peer], watch.snaps[i + 1][r.peer],
                                     list(p["allow"].get(r.peer, [])), list(p["require_changed"].get(r.peer, [])),
                                     title)
        res["name"] = p["name"]
        phases_out.append(res)
        for why in res["problems"]:
            reasons.append(f"grade {DATA_ROOT_GRADE}: phase {p['name']}: {why}")
        if res["not_allowed"]:
            files = res["not_allowed"]
            reasons.append(f"grade {DATA_ROOT_GRADE}: phase {p['name']}: not allowed: {'; '.join(files[:5])}"
                           f"{f' (and {len(files) - 5} more)' if len(files) > 5 else ''}")
        for e in res["missing_required"]:
            reasons.append(f"grade {DATA_ROOT_GRADE}: phase {p['name']}: {e!r} must change and did not")
    reasons += [f"grade {DATA_ROOT_GRADE}: {why}" for why in watch.problems]
    if not title:
        reasons.append(f"grade {DATA_ROOT_GRADE}: no title id (the grade's `title` or tests/skytest.toml [game]"
                       " title_id)")
    if not watch.snaps[0].get(r.peer):
        reasons.append(f"grade {DATA_ROOT_GRADE}: no file under the peer's data root before the run (nothing to"
                       " compare)")
    r.graded[DATA_ROOT_GRADE] = {"title": title, "phases": phases_out, "problems": list(watch.problems)}
    if any(ph["not_allowed"] or ph["missing_required"] for ph in phases_out):
        r.graded[DATA_ROOT_GRADE]["violation"] = True
    return reasons


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
    for rx in expect:
        if not re.search(rx, text, re.M):
            reasons.append(f"log lacks /{rx}/")
    reasons += check_count_and_order(text, ps) if ps else []
    if any(f == 0.0 for f in parsed["fps"]):
        reasons.append("presented no frame during an fps window (frozen, or not booted yet)")
    for rx, shown in peer_fatal_lines(spec, r.peer):
        if m := re.search(rx, text, re.M):
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            whole = text[start:end if end != -1 else len(text)].strip()
            reasons.append(f"log has forbidden /{shown}/: {whole[:160]}")
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
    pacer = pacer if pacer is not None else StartPacer(cfg.start_gap, machine_budget(cfg))
    names = list(spec.peers)
    hosted = spec.server == "host"
    # A hosted server is started by its owner H, not by the runner (it is still one of the games).
    starts = [n for n in names if not (hosted and n == "S")]
    res = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, started=stamp)
    if len(names) > max_games:
        res.status = "capacity"
        res.reasons = [f"needs {len(names)} games at once, this run allows {max_games} (--max-games)"]
        return res
    remote = [n for n in starts if spec.peers[n].kind == "remote"]
    if remote and cfg.remote is None:
        res.status = "capacity"
        res.reasons = [f"peer {', '.join(remote)} runs on the second PC: no remote agent (`test --remote HOST:PORT`)"]
        return res
    run = make_run_dir(cfg, folder if folder is not None else cfg.store / "runs" / f"{stamp}-{spec.name}")
    res.folder = str(run)
    exe = snap / cfg.exe
    t_start = time.monotonic()
    procs: dict[str, subprocess.Popen] = {}
    results = {n: PeerResult(peer=n) for n in names + list(spec.synthetic)}
    syn_procs: dict[str, subprocess.Popen] = {}  # server+N+hK: the synthetic peers started so far
    syn_started: dict[str, float] = {}
    save_grade = spec.grades.get("save_copy")
    save_title = str(save_grade.get("title", cfg.title_id)) if save_grade is not None else ""
    saves_before: dict[str, dict[str, str]] = {}
    remotes: dict[str, RemotePeer] = {}  # the started remote peers (kind = "remote")
    droot_grade = spec.grades.get(DATA_ROOT_GRADE)
    droot_title = str(droot_grade.get("title", cfg.title_id)) if droot_grade is not None else ""
    droot: DataRootWatch | None = None
    try:
        # [build_copy]: the case's own copy of the snapshot, before any peer's folder; every game of the
        # case (and the synthetic peers' digest) comes from it.
        if spec.build_copy:
            snap = case_build_copy(cfg, spec, snap, run)
            exe = snap / cfg.exe
        # Every peer's folder first (copies take a moment), then all games start close together, and
        # a spec error in a later peer starts no game at all.
        port = free_loopback_port() if hosted else 0
        prepared = {n: prepare_peer(cfg, spec, n, run, port) for n in starts if n not in remote}
        # server+N+hK: each synthetic peer's folder and command, before any game starts.
        syn_prepared = {n: prepare_synthetic(cfg, spec, n, run, snap, port) for n in spec.synthetic}
        # save_copy: each named peer's save copy as it is before any game starts.
        if save_grade is not None:
            saves_before = {n: save_state(run / n / "data", save_title) for n in save_copy_peers(save_grade, names)}
        # data_root_untouched: each named peer's files before any game starts (stage 7 item 9).
        if droot_grade is not None:
            droot = DataRootWatch(run, droot_grade, names,
                                  peer_log_path(run, droot_grade.get("by", data_root_peers(droot_grade, names)[0]),
                                                hosted))
        started: dict[str, float] = {}
        ended: dict[str, float] = {}  # each game's own end, for its time
        holds: list[tuple[str, BootHold]] = []  # each started game's boot token (PS15)

        def note_ends() -> None:
            for name, proc in procs.items():
                if name not in ended and proc.poll() is not None:
                    ended[name] = time.monotonic()

        with winjob.Job() as job:
            def start_local(n: str) -> None:
                # PS15: the machine's boot token (no other runner's game boots meanwhile), held until
                # this game's first frame line, its exit or BOOT_TIMEOUT_S (BootHold.watch).
                hold = pacer.machine_turn(note_ends)
                d, args = prepared[n]
                cmd = [*launcher_words(cfg.launcher, exe), *args] if cfg.launcher else [str(exe), *args]
                results[n].command = cmd
                # The game starts suspended, joins the job, and only then runs: it never runs outside
                # the job. If any step fails it is ended before the error goes on.
                try:
                    with open(d / "stderr.txt", "wb") as err:
                        p = subprocess.Popen(cmd, cwd=str(d), stdout=subprocess.DEVNULL, stderr=err,
                                             stdin=subprocess.DEVNULL, creationflags=winjob.SUSPENDED_START)
                except BaseException:
                    if hold is not None:
                        hold.release()
                    raise
                try:
                    job.add(p)
                    # Before it runs: no power throttling (a failure is printed once, never fatal).
                    results[n].power_throttling = opt_out_of_power_throttling(int(getattr(p, "_handle", 0) or 0))
                    winjob.resume(p)
                except BaseException:
                    if hold is not None:
                        hold.release()
                    p.kill()
                    p.wait(timeout=30)
                    raise
                if hold is not None:
                    results[n].boot_wait_s = round(hold.waited_s, 1)
                    holds.append((n, hold))
                    hold.watch(peer_log_path(run, n, False), p)
                procs[n] = p
                started[n] = time.monotonic()
                results[n].started_after_s = round(started[n] - t_start, 1)

            # Stage 8 item 4: a client whose script names {joincode} starts once the hosted server
            # logged its join code (start_joiners below), never before.
            joiners = [n for n in starts if n not in remote and uses_join_code(spec, n)]
            for n in starts:
                if n in joiners:
                    continue
                # Games booting in the same instant crashed and froze each other in a test
                # (stage 0, five at once); real sessions never start them together either. The gap
                # holds across every case running at once (StartPacer); the games already started
                # are watched meanwhile.
                pacer.wait_turn(note_ends)
                if n in remote:  # on the second PC: no boot token or job of this machine
                    remotes[n] = procs[n] = start_remote(cfg, spec, n, run, port)
                    results[n].command = remotes[n].command
                    started[n] = time.monotonic()
                    results[n].started_after_s = round(started[n] - t_start, 1)
                    continue
                start_local(n)
            deadline = time.monotonic() + spec.timeout
            # {joincode}: the hosted server's log, read for its join code until the joiners started.
            join_tail = LogTail(peer_log_path(run, "S", hosted)) if joiners else None
            join_waits = list(joiners)
            not_started: dict[str, str] = {}  # a joiner that will not start, and why

            def start_joiners() -> None:
                code, problem = join_code_from(join_tail.new_lines())
                if problem:  # fails closed: no joiner starts with a code that is not one
                    not_started.update({n: f"never started: {problem}" for n in join_waits})
                    join_waits.clear()
                    return
                if not code:
                    return
                while join_waits:
                    n = join_waits.pop(0)
                    fill_join_code(prepared[n][0] / la.SCRIPT_FILE, code)
                    pacer.wait_turn(note_ends)
                    start_local(n)
            # server+N+hK: each synthetic peer waits for its start condition in its start peer's log.
            syn_waits = {n: (LogTail(peer_log_path(run, s.start_peer, hosted)), re.compile(s.start_after))
                         for n, s in spec.synthetic.items()}
            syn_logs = {n: run / n / "peer.log" for n in spec.synthetic}

            def start_synthetic() -> None:
                for sn, (tail, rx) in list(syn_waits.items()):
                    if not any(rx.search(line) for line in tail.new_lines()):
                        continue
                    del syn_waits[sn]
                    sd, scmd = syn_prepared[sn]
                    results[sn].command = scmd
                    with open(syn_logs[sn], "wb") as out:
                        sp = subprocess.Popen(scmd, cwd=str(sd), stdout=out, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, creationflags=winjob.SUSPENDED_START)
                    try:
                        job.add(sp)
                        winjob.resume(sp)
                    except BaseException:
                        sp.kill()
                        sp.wait(timeout=30)
                        raise
                    syn_procs[sn] = sp
                    syn_started[sn] = time.monotonic()
                    results[sn].started_after_s = round(syn_started[sn] - t_start, 1)

            # Fail fast: the first fatal line (or crash exit) moves the stop to a grace after it.
            watch = FailFast({**{n: peer_log_path(run, n, hosted) for n in names}, **syn_logs},
                             {**{n: fail_fast_patterns(spec, n) for n in names},
                              **{n: s.forbid + SYNTHETIC_FATAL for n, s in spec.synthetic.items()}}) \
                if spec.fail_fast else None
            stop_at, next_check, early = deadline, 0.0, False
            while len(ended) < len(procs) and time.monotonic() < stop_at:
                note_ends()
                if syn_waits:
                    start_synthetic()
                if droot is not None:
                    droot.check()  # a phase line: that phase's end snapshot
                if join_waits and not (watch is not None and watch.hit):  # no new game after a fatal line
                    start_joiners()
                if watch is not None and time.monotonic() >= next_check:
                    next_check = time.monotonic() + FAIL_FAST_POLL
                    # Checked through the grace too: a hit whose log was replaced meanwhile is dropped.
                    stop_at = min(deadline, watch.hit_at + cfg.fail_fast_grace) \
                        if watch.check({**procs, **syn_procs}) else deadline
                    early = stop_at < deadline  # else the timeout comes first: a timeout as before
                time.sleep(min(0.5, max(0.0, stop_at - time.monotonic())))
            # {joincode}: a joiner still waiting when the run ended never got the code (fails closed).
            not_started.update({n: "never started: S never logged its join code (/"
                                   f"{SERVER_JOIN_CODE.pattern}/), so {{joincode}} had no value" for n in join_waits})
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
            # The synthetic peers end by themselves (End session drains them, or their script leaves)
            # within SYNTHETIC_EXIT_GRACE of the games' end; one still running then is ended and fails.
            syn_deadline = time.monotonic() + (0.0 if stopped else SYNTHETIC_EXIT_GRACE)
            for sn, sp in syn_procs.items():
                try:
                    sp.wait(timeout=max(0.0, syn_deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    sp.kill()
                    results[sn].ended_by_runner = True
                    results[sn].stopped_by = stopped
                    try:
                        sp.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                results[sn].exit_code = sp.returncode
                results[sn].seconds = round(time.monotonic() - syn_started[sn], 1)
            res.leftover_processes = job.active()
            if res.leftover_processes:
                job.terminate()
            for n, hold in holds:  # every game ended: each token is free, its outcome known
                hold.release()
                results[n].boot = hold.outcome or "ended before its first frame line"
        for rp in remotes.values():  # ended (killed and waited for above): their files come back
            rp.collect()
    except (SpecError, OSError) as e:
        # Every game started so far went with the job when it closed; a remote one is stopped.
        for rp in remotes.values():
            rp.kill()
            rp.client.close()
        res.status, res.reasons = "error", [str(e)]
        return res
    saves_after = {n: save_state(run / n / "data", save_title) for n in saves_before}
    if droot is not None:
        droot.finish()
    res.seconds = round(time.monotonic() - t_start, 1)
    texts = {n: read_peer_log(run, n, hosted) for n in names}
    # The synthetic peers' output, graded on its own and read by the cross-client grades (remote_swap).
    syn_texts = {n: (run / n / "peer.log").read_text(encoding="utf-8", errors="replace")
                 if (run / n / "peer.log").is_file() else "" for n in spec.synthetic}
    for n in spec.synthetic:
        grade_synthetic(spec, results[n], syn_texts[n], n in syn_started)
    for n in names:
        text = texts[n]
        parsed = parse_log(text)
        r = results[n]
        r.build, r.levels, r.fps, r.longest_frame_ms = parsed["build"], parsed["levels"], parsed["fps"], parsed["longest"]
        r.peak_private_mb, r.peak_working_set_mb, r.errors = parsed["private"], parsed["working"], parsed["errors"]
        r.frame_p95_ms, r.peak_video_mb = parsed["frame_p95"], parsed["video"]
        r.startup_stall_s = parsed["startup_stall"]
        grade_peer(spec, r, text, parsed)
        if n in not_started:  # a {joincode} joiner the run never started (start_joiners)
            r.reasons.insert(0, not_started[n])
            r.status = "fail"
        if n in remotes and remotes[n].problems:  # a lost agent or a file that did not come back
            r.reasons += remotes[n].problems
            r.status = "fail"
        if spec.grades:
            limit = frame_rate_limit(r.command, run / n / "settings.toml")
            extra = grade_thresholds(spec, r, parsed, thresholds or {}, limit, baseline)
            if n in saves_before:
                extra += grade_save_copy(r, saves_before[n], saves_after[n], save_title)
            if droot is not None and n in droot.peers:
                extra += grade_data_root(r, droot, droot_title)
            extra += grade_cross_peer(spec, r, {**texts, **syn_texts}, cfg.repo, thresholds or {})
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
    res.peers = [results[n] for n in names + list(spec.synthetic)]
    failed = [r.peer for r in res.peers if r.status != "pass"]
    if res.stopped_early:
        res.reasons.append(res.stopped_early)
    if res.leftover_processes:
        res.reasons.append(f"{res.leftover_processes} game process(es) still running after the case; ended")
    if failed:
        res.reasons.append(f"peers failed: {', '.join(failed)}")
    res.status = "pass" if not failed and not res.leftover_processes else "fail"
    if res.status != "pass":  # C7: what the circuit breaker counts
        res.signature = case_signature(spec, texts)
    return res


# --- a unit case ----------------------------------------------------------------------------------


def run_tool(cmd: list[str], cwd: Path, out: Path, timeout: float) -> tuple[int | None, bool, float]:
    """One unit step (the test binary or one ctest), in a job object as every game is, its stdout
    and stderr into `out`. Returns the exit code, whether the runner ended it, and its seconds."""
    t0 = time.monotonic()
    ended = False
    with open(out, "wb") as f, winjob.Job() as job:
        p = subprocess.Popen(cmd, cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=winjob.SUSPENDED_START)
        try:
            job.add(p)
            winjob.resume(p)
        except BaseException:
            p.kill()
            p.wait(timeout=30)
            raise
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            ended = True
            p.kill()
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            job.terminate()
    return p.returncode, ended, round(time.monotonic() - t0, 1)


def ctest_command(cfg: Config) -> list[str]:
    if cfg.ctest:
        return list(cfg.ctest)
    found = shutil.which("ctest")
    if not found:
        raise SpecError("no ctest on PATH (run from the build prompt, or set [game] ctest in tests/skytest.toml)")
    return [found]


def ctest_regex(name: str) -> str:
    """An exact-name ctest -R pattern (CTEST_NAME allows only '.' as a regex character)."""
    return "^" + name.replace(".", "\\.") + "$"


def failure_lines(text: str, limit: int = 8) -> list[str]:
    lines = [x.strip() for x in text.splitlines() if x.strip().startswith("FAIL")]
    return lines[:limit] + ([f"... and {len(lines) - limit} more FAIL lines"] if len(lines) > limit else [])


def run_unit_case(cfg: Config, spec: Spec, snap: Path | None, max_games: int, stamp: str,
                  thresholds: dict | None = None, baseline: dict | None = None,
                  accepted_grades: tuple[str, ...] = (), folder: Path | None = None,
                  pacer: StartPacer | None = None) -> CaseResult:
    """A unit case: the test binary from its snapshot, graded by its exit code and its pass line,
    then each named ctest alone. Starts no game, so it needs no capacity, disc, fixtures, machine slot
    or start gap (`pacer` is taken as run_case's and not used). One result row per step: `test` for
    the binary, `ctest <name>` for each ctest."""
    u = spec.unit
    res = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, started=stamp)
    # make_run_dir: two cases of one `test --jobs N` (or two runners) may start in the same second.
    run = make_run_dir(cfg, folder if folder is not None else cfg.store / "runs" / f"{stamp}-{spec.name}")
    res.folder = str(run)
    t_start = time.monotonic()
    steps: list[PeerResult] = []
    try:
        exe = snapshot_unit(cfg, u["binary"])
        r = PeerResult(peer="test")
        r.command = [*cfg.unit_launcher, str(exe), *u["args"]]
        out = run / "test.txt"
        r.exit_code, r.ended_by_runner, r.seconds = run_tool(r.command, run, out, spec.timeout)
        text = out.read_text(encoding="utf-8", errors="replace")
        lines = [x.strip() for x in text.splitlines()]
        summary = next((m for m in (UNIT_SUMMARY.match(x) for x in lines) if m), None)
        r.graded["unit"] = {"pass_line": u["pass_line"] in lines,
                            "tests": int(summary[1]) if summary else None,
                            "failed": int(summary[3]) if summary else None,
                            "ms": int(summary[4]) if summary else None}
        if r.ended_by_runner:
            r.reasons.append(f"did not end within {spec.timeout:.0f} s; ended")
        elif r.exit_code != 0:
            r.reasons.append(f"exit code {r.exit_code}")
            r.reasons += failure_lines(text)
        if not r.graded["unit"]["pass_line"]:
            r.reasons.append(f"no '{u['pass_line']}' line")
        r.status = "pass" if not r.reasons else "fail"
        steps.append(r)
        if u["ctest"]:
            ctest = ctest_command(cfg)
            test_dir = cfg.build / u["ctest_dir"]
            if not test_dir.is_dir():
                raise SpecError(f"no ctest folder {test_dir}: configure the build first")
            for name in u["ctest"]:
                c = PeerResult(peer=f"ctest {name}")
                c.command = [*ctest, "--test-dir", str(test_dir), "-R", ctest_regex(name), "--no-tests=error",
                             "--output-on-failure", *(["-C", u["ctest_config"]] if u["ctest_config"] else [])]
                out = run / f"ctest-{name}.txt"
                c.exit_code, c.ended_by_runner, c.seconds = run_tool(c.command, run, out, spec.timeout)
                if c.ended_by_runner:
                    c.reasons.append(f"did not end within {spec.timeout:.0f} s; ended")
                elif c.exit_code != 0:
                    c.reasons.append(f"exit code {c.exit_code} (no such test, or it failed: {out.name})")
                c.status = "pass" if not c.reasons else "fail"
                steps.append(c)
    except (SpecError, OSError) as e:
        res.peers = steps
        res.status, res.reasons = "error", [str(e)]
        res.seconds = round(time.monotonic() - t_start, 1)
        return res
    res.peers = steps
    res.seconds = round(time.monotonic() - t_start, 1)
    failed = [r.peer for r in steps if r.status != "pass"]
    if failed:
        res.reasons.append(f"failed: {', '.join(failed)}")
    res.status = "pass" if not failed else "fail"
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
        if sub.signature and not res.signature:
            res.signature = sub.signature
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
        # met_by (stage 3, D14): a case that passes when each named case passed in this run (each its own
        # evidence), and its own spec too when it has one. Without a spec it needs no run of its own.
        parts = [p for p in c.get("met_by", []) if isinstance(p, str)]
        for part in parts:
            pr = ran.get(part)
            if pr is None or pr.status != "pass":
                open_cases.append(f"{c['id']} (met by {part}: {'not run' if pr is None else pr.status})")
        if parts and not c.get("spec"):
            continue
        if not c.get("spec"):
            open_cases.append(f"{c['id']} (no spec)")
        elif r is None:
            open_cases.append(f"{c['id']} (not run)")
        elif r.status == "capacity" and c.get("capacity_ok"):
            continue
        elif r.status != "pass":  # `not ready` (PS2) too, with its reason
            open_cases.append(f"{c['id']} ({r.status}" + (f": {r.not_ready}" if r.not_ready else "") + ")")
        elif c.get("amendment") and not c.get("amendment_accepted"):
            # A frozen case run on a reduced layout (stage 4's synthetic peers) counts as met only once the
            # owner accepted the scope amendment that layout rests on (docs/online-4.md section 7.3).
            open_cases.append(f"{c['id']} (passes on {c.get('layout')}; met only with {c['amendment']})")
    open_cases = list(dict.fromkeys(open_cases))
    line = f"checkpoint {stage}: met" if not open_cases else f"checkpoint {stage}: NOT met: {', '.join(open_cases)}"
    if accepted:
        line += f" (accepted: {'; '.join(accepted)})"
    return {"stage": stage, "met": not open_cases, "open": open_cases, "accepted": accepted, "line": line}


def write_report(cfg: Config, out: Path, results: list[CaseResult], manifest: dict, thresholds: dict,
                 max_games: int, guard: list[dict], unwritten: list[str], checkpoint: dict | None = None,
                 gate: dict | None = None, token: dict | None = None, jobs: int = 1, progress: str = "",
                 queue_unmatched: list[str] | None = None, breaker: dict | None = None) -> None:
    """report.md. `progress` (PS14): the run is still going; the report so far (its finished cases)
    says so and leaves out what only the run's end knows (the manifest's cases not run, the guarded
    folders, the checkpoint and the gate)."""
    head = f"At most {max_games} game(s) at once."
    if jobs > 1:
        head = (f"At most {max_games} game(s) and {jobs} case(s) at once (--jobs {jobs}); an exclusive case ran"
                " alone. With: the cases that ran beside it.")
    lines = [f"# skytest run {out.name}", "", head, ""]
    if cfg.slots is not None:
        lines += [f"The machine's budget, shared with every other runner: {cfg.machine_games} game(s); an exclusive"
                  " or timed case takes all of it (Machine: under each case).", ""]
    if breaker:
        lines += [f"**{breaker_line(breaker)}**", ""]
    if progress:
        lines += [f"**In progress: {progress}.** This report is rewritten after every case; the guarded folders,"
                  " the checkpoint and the gate come when the run ends.", ""]
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
        lines += ["", f"## {r.case} ({r.layout})", "", f"Folder: `{r.folder}`", "", load_line(r.load), ""]
        if r.machine:
            lines += [machine_line(r.machine), ""]
        if r.signature:
            lines += [f"Fatal signature ({r.signature['peer']}, {r.signature['kind']}): `{r.signature['signature']}`", ""]
        if r.scenes:
            lines += [f"Scenes: {r.scenes}", ""]
        lines += [
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
    if queue_unmatched:
        lines += ["", "## Queue entries that matched no case", "", ", ".join(queue_unmatched)]
    if progress:
        write_text_replacing(out / "report.md", "\n".join(lines) + "\n")
        return
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
        lines += ["", "## Gate", "", gate_line(gate), "", "```", *gate_table(gate), *per_build_table(gate), "```"]
        if gate.get("verdict"):
            lines += ["", f"**{gate['verdict']['line']}**"]
    write_text_replacing(out / "report.md", "\n".join(lines) + "\n")


REPLACE_RETRY_S = 5.0  # how long write_text_replacing retries a rename a reader blocks


def write_text_replacing(path: Path, text: str, retry_s: float | None = None) -> bool:
    """Writes path whole (a temporary file renamed over it), so a reader of a report that is rewritten
    after every case (PS14) never sees half of it. A target held open by a reader (Windows refuses the
    rename then) is retried for REPLACE_RETRY_S; then the previous file stays whole, the new text is
    left in the temporary file and the run says so (never a write in place, which a crash would leave
    half done). Returns whether the file was replaced."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    end, delay = time.monotonic() + (REPLACE_RETRY_S if retry_s is None else retry_s), 0.05
    while True:
        try:
            os.replace(tmp, path)
            for stale in path.parent.glob(f"{path.name}.tmp-{os.getpid()}-*"):  # report.md, results.json, ...
                try:
                    stale.unlink(missing_ok=True)  # another thread's text that a reader kept out before
                except OSError:
                    pass
            return True
        except PermissionError:
            if time.monotonic() >= end:
                break
            time.sleep(delay)
            delay = min(0.5, delay * 2)
    print(f"{path.name}: held open by a reader, not replaced; the previous file stays whole and the new text is in"
          f" {tmp} (the next write replaces both)", flush=True)
    return False


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


def pass_basis(payload: dict, case: dict) -> str:
    """How a recorded case ran (PS13; PS12 round 2, S2: a concurrency that was not recorded is unknown,
    not alone): `alone` only when its load was recorded (`load.samples` > 0) with no game its runner did
    not start (`outside_games_max` 0: other lanes, other runners), `ran_with` recorded and empty and the
    run's `jobs` 1; `beside` when another case ran with it (`ran_with`) or a game of another runner was
    alive (`outside_games_max` > 0); `unknown` otherwise (every result before PS12, a load sampled only
    by tasklist without the outside count, a --jobs run without a case beside it)."""
    load = case.get("load") if isinstance(case.get("load"), dict) else {}
    ran_with = case.get("ran_with")
    outside = load.get("outside_games_max")
    if ran_with or (isinstance(outside, (int, float)) and outside > 0):
        return "beside"
    try:
        jobs = int(payload["jobs"])
        samples = int(load.get("samples") or 0)
    except (KeyError, TypeError, ValueError):
        return "unknown"
    if ran_with == [] and jobs == 1 and samples > 0 and outside == 0:
        return "alone"
    return "unknown"


def ran_alone(payload: dict, case: dict) -> bool:
    """Whether a recorded case is known to have run with no other game on the machine (pass_basis)."""
    return pass_basis(payload, case) == "alone"


def last_run_seconds(cfg: Config, current: Path | None = None) -> dict[str, dict]:
    """Each case's recorded times, from the run store's results.json files (the same walk as
    previous_statuses, oldest run folder first): under "pass" its newest pass, under "fail" its
    longest fail, each as (`seconds`, the run folder's name), and under "passes" every pass as
    (`seconds`, the run folder's name, pass_basis, its provenance or {}). A fail can end early (a crash,
    a startup failure),
    so only a pass sets an estimate; the longest fail is shown beside a timeout. A run that ended in
    error, capacity, not ready or not run says nothing about the case's length and is skipped."""
    seen: dict[str, dict] = {}
    runs = cfg.store / "runs"
    if not runs.is_dir():
        return seen
    for f in sorted(runs.glob("*/results.json")):
        if f.parent == current:
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
            cases = payload.get("cases", [])
        except (OSError, ValueError, AttributeError):
            continue
        for c in cases:
            try:
                seconds = float(c.get("seconds", 0))
                status = c.get("status")
                if status not in ("pass", "fail") or seconds <= 0:
                    continue
                times = seen.setdefault(c["case"], {})
                if status == "pass":
                    prov = c.get("provenance") if isinstance(c.get("provenance"), dict) else {}
                    times.setdefault("passes", []).append((seconds, f.parent.name, pass_basis(payload, c), prov))
                if status == "pass" or seconds >= times.get("fail", (0.0, ""))[0]:
                    times[status] = (seconds, f.parent.name)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    return seen


def median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def case_estimate(cfg: Config, s: Spec, times: dict) -> dict:
    """One case's expected length (PS13) from its recorded times (its last_run_seconds entry), as
    `seconds`, `basis` and `source`: the median of its passes known to have run alone (pass_basis),
    else the median of all its passes (`unknown` when the load of one was not recorded, else `beside`),
    else its spec's timeout as an upper bound (per [[compare.run]] for a compared case; a case that only
    failed names its longest fail beside it). A pass whose provenance names another spec text or other
    arguments than the spec's now (PS12: `spec_sha256`, `args_sha256`) is left out and counted in the
    source; passes from before provenance stay. The gate's estimate and PS118's order (RunOrder) both
    read it."""
    passes, other_version = times.get("passes", []), 0
    if passes:
        spec_now, args_now = s.sha256 or file_sha256(s.path), args_sha256(cfg, s)
        same = [p for p in passes if p[3].get("spec_sha256", spec_now) == spec_now
                and p[3].get("args_sha256", args_now) == args_now]
        passes, other_version = same, len(passes) - len(same)
    left_out = (f"; {other_version} pass(es) of another spec text or arguments left out" if other_version
                else "")
    if passes:
        alone = [p for p in passes if p[2] == "alone"]
        unknown = [p for p in passes if p[2] == "unknown"]
        use = alone or passes
        if alone:
            what = f"median of {len(use)} pass(es) alone"
        elif unknown:
            beside_n = len(passes) - len(unknown)
            what = (f"median of {len(use)} pass(es), none known alone ({len(unknown)} with no recorded load"
                    + (f", {beside_n} beside other games" if beside_n else "") + ")")
        else:
            what = f"median of {len(use)} pass(es) beside other games, none alone"
        return {"seconds": round(median([p[0] for p in use]), 1),
                "basis": "alone" if alone else ("unknown" if unknown else "beside"),
                "source": f"{what}, newest {use[-1][1]}{left_out}"}
    runs = max(1, len(s.compare.get("run", []))) if s.compare else 1
    fail = times.get("fail")
    never = ("only passes of another spec text or arguments" if other_version else "never passed")
    return {"seconds": s.timeout * runs, "basis": "timeout",
            "source": ("spec timeout" + (f" x {runs} runs" if runs > 1 else "")
                       + (f" ({never}; longest fail {fail[0] / 60:.1f} min, {fail[1]})" if fail
                          else f" ({never}{left_out})" if other_version else " (never run)"))}


def gate_estimate(cfg: Config, manifest: dict, stages: list[str] | None, specs: list[Spec],
                  current: Path | None = None, run_not_ready: bool = False, cadence: str = "daily",
                  build: str | None = None) -> dict:
    """The gate cases of these stages (all when None) and their expected minutes (PS13, case_estimate).
    `cadence` daily: the daily gate's cases (gate = true), with the per-build ones (gate = "per-build")
    listed apart under `per_build` (never counted in the minutes; each with its `state` on `build`,
    per_build_state; a not-ready one stays not ready whatever run_not_ready says, since a daily run never
    runs it). `cadence` per-build: only the per-build cases (`test --gate --per-build`). A case that is
    not ready (PS2) counts 0 min and is named with its reason (unless `run_not_ready`); a gate case with
    no spec is `no spec` (named, never counted silently). Each row's `basis`: alone, beside, unknown,
    timeout, not ready or no spec; its `cadence`: daily or per-build."""
    by_name = {s.name: s for s in specs}
    last = last_run_seconds(cfg, current)
    rows: list[dict] = []
    apart: list[dict] = []
    for c in manifest.get("case", []):
        cad = gate_cadence(c)
        if not cad or (stages is not None and str(c.get("stage")) not in stages):
            continue
        if cad != cadence and cadence != "daily":
            continue  # a per-build run lists no daily case
        row = {"case": c["id"], "stage": str(c.get("stage")), "layout": c.get("layout", ""), "seconds": None,
               "cadence": cad}
        s = by_name.get(c["id"])
        if s is None:
            row["basis"], row["source"] = "no spec", "no spec"
        elif s.not_ready and not (run_not_ready and cad == cadence):
            row["seconds"], row["basis"], row["source"] = 0.0, "not ready", f"not ready: {s.not_ready}"
        else:
            row.update(case_estimate(cfg, s, last.get(c["id"], {})))
        (rows if cad == cadence else apart).append(row)
    g = gate_totals(stages, rows)
    g["cadence"] = cadence
    g["per_build"] = apart
    if apart:
        g["build"] = build or ""
        for row, state in zip(apart, per_build_states(cfg, apart, specs, build or "", current)):
            row["state"] = state
    return g


def gate_label(g: dict) -> str:
    """How a gate run's lines begin: `gate` (the daily gate) or `gate (per-build)`."""
    return "gate (per-build)" if g.get("cadence") == GATE_PER_BUILD else "gate"


def build_results(cfg: Config, build: str, current: Path | None = None) -> dict[str, list[tuple[str, str, dict]]]:
    """Each case's results recorded on one build snapshot (results.json's `snapshot` folder is named
    after the build: build_hash), oldest run folder first, as (run folder, status, provenance or {});
    the run folder `current` is skipped. Any run counts: a gate run, a `--per-build` run, a plain one."""
    seen: dict[str, list[tuple[str, str, dict]]] = {}
    runs = cfg.store / "runs"
    if not build or not runs.is_dir():
        return seen
    for f in sorted(runs.glob("*/results.json")):
        if f.parent == current:
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
            snap = payload.get("snapshot")
            if not isinstance(snap, str) or re.split(r"[\\/]", snap.rstrip("\\/"))[-1] != build:
                continue
            cases = payload.get("cases", [])
        except (OSError, ValueError, AttributeError):
            continue
        for c in cases:
            try:
                prov = c.get("provenance") if isinstance(c.get("provenance"), dict) else {}
                seen.setdefault(c["case"], []).append((f.parent.name, str(c["status"]), prov))
            except (TypeError, KeyError, AttributeError):
                continue
    return seen


# A result that never started the game (no machine room, the circuit breaker, not ready) does not decide a
# per-build case: the result before it stands.
PER_BUILD_NOT_RUN = ("capacity", "breaker", "not ready")


def per_build_state(cfg: Config, spec: Spec, entries: list[tuple[str, str, dict]], build: str) -> dict:
    """A per-build gate case on one build: its newest result on that build snapshot (its build_results
    entries) whose provenance names the spec text and arguments of now decides, and it is met when that
    result is a pass (the owner's decision 3: its result counts for that build), so a later fail undoes
    an earlier pass. Results with no provenance or of another spec text or arguments are left out, as
    are ones that never started the game (PER_BUILD_NOT_RUN). Returns `met`, `pass` (the run folder of
    the deciding pass, or None), `last` ([status, run folder] of the deciding result, or None),
    `undone` (the run folder of the newest pass before a deciding result that is not a pass, or None),
    `other` (passes left out: another spec text or arguments, or no provenance) and `text` (what list
    --gate and the report say)."""
    if not build:
        return {"met": False, "pass": None, "last": None, "undone": None, "other": 0,
                "text": "no build (the build folder has no exe)"}
    spec_now, args_now = spec.sha256 or file_sha256(spec.path), args_sha256(cfg, spec)
    ran = [(run, status, prov) for run, status, prov in entries if status not in PER_BUILD_NOT_RUN]
    same = [(run, status) for run, status, prov in ran
            if prov.get("spec_sha256") == spec_now and prov.get("args_sha256") == args_now]
    other = sum(1 for _, status, _ in ran if status == "pass") - sum(1 for _, status in same if status == "pass")
    left_out = f"; {other} pass(es) of another spec text or arguments, or with no provenance, left out" if other else ""
    last = [same[-1][1], same[-1][0]] if same else None
    met = last is not None and last[0] == "pass"
    earlier = [run for run, status in same if status == "pass"]
    undone = earlier[-1] if earlier and not met else None
    if met:
        text = f"last result on this build: pass ({last[1]})"
    elif last:
        text = f"no pass on this build: last result on it: {last[0]} ({last[1]})"
        if undone:
            text = (f"last result on this build: {last[0]} ({last[1]}), after its pass in {undone}"
                    " (the newest result decides)")
    else:
        text = "no result on this build"
    return {"met": met, "pass": last[1] if met else None, "last": last, "undone": undone, "other": other,
            "text": text + left_out}


def per_build_states(cfg: Config, rows: list[dict], specs: list[Spec], build: str,
                     current: Path | None = None) -> list[dict | None]:
    """per_build_state for each per-build row (None for a row with no spec or not ready: those are open
    whatever the store says), from one walk of the store."""
    by_name = {s.name: s for s in specs}
    found = build_results(cfg, build, current)
    return [per_build_state(cfg, by_name[r["case"]], found.get(r["case"], []), build)
            if r.get("basis") not in ("no spec", "not ready") and r["case"] in by_name else None for r in rows]


def gate_totals(stages: list[str] | None, rows: list[dict]) -> dict:
    """The gate's sums over its rows (gate_estimate's, or the ones a `test --gate CASE...` runs): the
    measured minutes (passes) apart from the timeout minutes (cases that never passed: an upper bound);
    `over` compares the measured minutes with the plan's subset."""
    def of(basis: str) -> list[dict]:
        return [r for r in rows if r.get("basis") == basis]

    measured = sum(r["seconds"] for r in of("alone") + of("beside") + of("unknown")) / 60
    timeouts = sum(r["seconds"] for r in of("timeout")) / 60
    return {
        "stages": stages, "cases": rows, "minutes": round(measured, 1), "timeout_minutes": round(timeouts, 1),
        "from_runs": [r["case"] for r in rows if r.get("basis") in ("alone", "beside", "unknown")],
        "from_beside": [r["case"] for r in of("beside")],
        "from_unknown": [r["case"] for r in of("unknown")],
        "from_timeouts": [r["case"] for r in of("timeout")],
        "not_ready": [r["case"] for r in of("not ready")],
        "no_spec": [r["case"] for r in of("no spec")],
        "limit_minutes": GATE_MINUTES, "over": measured > GATE_MINUTES,
    }


def gate_only(g: dict, names: set[str], patterns: list[str]) -> dict:
    """The estimate of a `test --gate CASE...` run: the rows of the cases it runs, and the spec-less
    gate cases its patterns name; the sums over those alone. Its per-build rows are the ones the
    patterns name (counted from the store, never run in a daily run)."""
    rows = [r for r in g["cases"] if r["case"] in names
            or (r.get("basis") == "no spec" and any(fnmatch.fnmatch(r["case"], p) for p in patterns))]
    out = gate_totals(g["stages"], rows)
    out["cadence"] = g.get("cadence", "daily")
    out["per_build"] = [r for r in g.get("per_build", []) if any(fnmatch.fnmatch(r["case"], p) for p in patterns)]
    if "build" in g:
        out["build"] = g["build"]
    return out


def per_build_word(row: dict) -> str:
    """A per-build row in one phrase (gate_line): its open reason, or whether this build has its pass."""
    if row.get("basis") in ("no spec", "not ready"):
        return row["source"]
    st = row.get("state") or {}
    if st.get("met"):
        return f"passed on this build ({st['pass']})"
    if st.get("undone"):
        return f"last result on this build: {st['last'][0]} ({st['last'][1]}), after its pass"
    return "no pass on this build"


def gate_line(g: dict) -> str:
    """The line `test --gate` prints before it runs, and `list --gate` after its table."""
    runs = len(g["from_runs"])
    beside = g.get("from_beside", [])
    unknown = g.get("from_unknown", [])
    alone = runs - len(beside) - len(unknown)
    line = (f"{gate_label(g)}: {len(g['cases'])} cases, expected {g['minutes']:.0f} min measured ({runs} from the"
            f" median of their passes: {alone} run alone" + (f", {len(beside)} beside other games: {', '.join(beside)}"
                                                             if beside else "")
            + (f", {len(unknown)} of unknown load (no pass known alone): {', '.join(unknown)}"
               if unknown else "") + ")")
    if g["from_timeouts"]:
        line += (f"; + up to {g.get('timeout_minutes', 0):.0f} min for {len(g['from_timeouts'])} never passed"
                 f" (spec timeouts: {', '.join(g['from_timeouts'])})")
    if g.get("not_ready"):
        line += f"; not ready, 0 min: {', '.join(g['not_ready'])}"
    if g["no_spec"]:
        line += f"; no spec, not counted: {', '.join(g['no_spec'])}"
    if g.get("per_build"):
        line += ("; per-build, not run here (once per build, `test --gate --per-build`): "
                 + ", ".join(f"{r['case']} ({per_build_word(r)})" for r in g["per_build"]))
    if g.get("cadence") == GATE_PER_BUILD:
        return line  # the plan's 30-minute subset is the daily gate's
    line += f"; the plan's subset is {g['limit_minutes']} min"
    if g["over"]:
        line += f" (over by {g['minutes'] - g['limit_minutes']:.0f} min)"
    elif g["minutes"] + g.get("timeout_minutes", 0) > g["limit_minutes"]:
        line += f" (up to {g['minutes'] + g['timeout_minutes']:.0f} min with the timeouts)"
    return line


def gate_verdict(g: dict, results: list[CaseResult], built: dict[str, dict | None] | None = None) -> dict:
    """A gate run's verdict: met when every gate case it counts ran and passed, and (a daily run) every
    per-build case has a pass on this build (`built`: per_build_state by case, read from the store at the
    run's end). A case that is not ready (PS2) is open whatever else holds, as is one with no spec or
    one that did not pass (with its status, and a not-ready case's reason); a per-build case without a
    pass is open as `per-build case <x> has no pass for this build`."""
    ran = {r.case: r for r in results}
    open_cases, met_by = [], []
    for row in g["cases"]:
        r = ran.get(row["case"])
        if row.get("basis") == "no spec":
            open_cases.append(f"{row['case']} (no spec)")
        elif r is None:
            open_cases.append(f"{row['case']} (not run)")
        elif r.status != "pass":
            open_cases.append(f"{row['case']} ({r.status}" + (f": {r.not_ready}" if r.not_ready else "") + ")")
    for row in g.get("per_build", []):
        name, st = row["case"], (built or {}).get(row["case"])
        if row.get("basis") in ("no spec", "not ready"):
            open_cases.append(f"{name} ({row['source']})")
        elif st and st.get("met"):
            met_by.append(f"{name} ({st['pass']})")
        else:
            why = [f"last result on it: {st['last'][0]} in {st['last'][1]}"] if st and st.get("last") else []
            why += [f"its pass in {st['undone']} came before that result, the newest result decides"]                 if st and st.get("undone") else []
            why += [f"{st['other']} pass(es) of another spec text or arguments, or with no provenance, left out"]                 if st and st.get("other") else []
            head = (f"per-build case {name}'s last result on this build is not a pass" if st and st.get("undone")
                    else f"per-build case {name} has no pass for this build")
            open_cases.append(head + (f" ({'; '.join(why)})" if why else ""))
    label = gate_label(g)
    line = f"{label}: NOT met: {', '.join(open_cases)}" if open_cases else f"{label}: met"
    if met_by and not open_cases:
        line += f" (per-build, passed on build {g.get('build', '')}: {', '.join(met_by)})"
    return {"met": not open_cases, "open": open_cases, "line": line,
            "per_build": {k: v for k, v in (built or {}).items()}, "build": g.get("build", "")}


def gate_table(g: dict) -> list[str]:
    lines = []
    for r in g["cases"]:
        t = f"{r['seconds'] / 60:5.1f} min" if r["seconds"] is not None else "    - min"
        if r.get("basis") == "timeout":
            t = f"<={r['seconds'] / 60:3.0f} min"
        elif r.get("basis") == "unknown":
            t = f"~{r['seconds'] / 60:4.1f} min"
        lines.append(f"{r['case']:<32} {r['layout']:<9} stage {r['stage']:<5} {t}  {r['source']}")
    return lines


def per_build_table(g: dict) -> list[str]:
    """The per-build cases apart (list --gate, the report): each as a gate_table row, then its state
    on this build (its last result there, and whether a pass meets it)."""
    rows = g.get("per_build", [])
    if not rows:
        return []
    lines = [f"per-build (once per build in an idle slot, not in the daily gate; this build:"
             f" {g.get('build') or 'none'}):"]
    # A row with no state (not ready, no spec) never runs from here: no minutes, and its reason once.
    shown = [r if r.get("state") else {**r, "seconds": None, "basis": ""} for r in rows]
    for r, row_line in zip(rows, gate_table({"cases": shown})):
        st = r.get("state")
        lines.append(f"{row_line}  | {st['text']}" if st else row_line)
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
    write_text_replacing(out / "findings.md", "\n".join(lines) + "\n")


# --- running cases at once (`test --jobs N`) --------------------------------------------------------
#
# The owner's playtests run several cases at once (stage 1B): with cases of two games (server+1), a
# machine that runs five games runs two cases side by side. Each case keeps its own run folder,
# peers, fixture copies, job object and hosted-server port (free_loopback_port; Host names its own
# control pipe), and the start gap holds across all of them (StartPacer). Cases start in the
# selection's order, in a gate run or with --jobs above 1 in PS118's (RunOrder: exclusive first, then
# longest first): the next one starts when a job is free and its games fit beside the running
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
TIMED_GRADES = ("server_frame_time", "client_fps", "level_load", "press_to_reaction", "bandwidth", "remote_swap")


def timed_grades(spec: Spec) -> list[str]:
    """What in a spec measures time or load (TIMED_GRADES and a [compare]), for lint's exclusive check."""
    return [g for g in TIMED_GRADES if g in spec.grades] + (["compare"] if spec.compare else [])


def runs_alone(spec: Spec) -> bool:
    """Whether the scheduler runs a case alone: `exclusive = true`, a time or load grade (timed_grades),
    or a remote peer (the agent on the second PC runs one game at a time)."""
    return spec.exclusive or bool(timed_grades(spec)) or any(p.kind == "remote" for p in spec.peers.values())


def case_games(spec: Spec, max_games: int) -> int:
    """The games a case holds while it runs: every peer (a hosted server too, which its owner starts);
    0 for a case over --max-games, which ends at once as a capacity result."""
    n = len(spec.peers)
    return n if n <= max_games else 0


class RunOrder:
    """PS118: the order of a gate run and of a run with --jobs above 1 (the queue's takes too): the
    cases that run alone first (runs_alone: exclusive, timed), then the rest, each group longest first
    by the gate estimate's basis (case_estimate: the median of passes run alone, else of all passes,
    else the spec's timeout). Cases of equal rank keep their given order. The scheduler's no-overtake
    rule holds for the order chosen, so nothing is starved. Called as the sort key: (0 or 1, -seconds)."""

    def __init__(self, cfg: Config, current: Path | None = None) -> None:
        self.cfg = cfg
        self.last = last_run_seconds(cfg, current)
        self.cache: dict[int, float] = {}

    def seconds(self, spec: Spec) -> float:
        if id(spec) not in self.cache:
            est = case_estimate(self.cfg, spec, self.last.get(spec.name, {}))
            self.cache[id(spec)] = float(est["seconds"] or 0.0)
        return self.cache[id(spec)]

    def __call__(self, spec: Spec) -> tuple[int, float]:
        return order_key(runs_alone(spec), self.seconds(spec))

    def line(self, specs: list[Spec]) -> str:
        """The order a run starts its cases in, as it prints it once before the first case."""
        ranked = sorted(specs, key=self)
        return ("order (exclusive first, then longest first): "
                + ", ".join(f"{s.name} ({'exclusive, ' if runs_alone(s) else ''}{self.seconds(s) / 60:.1f} min)"
                            for s in ranked))


def order_key(alone: bool, seconds: float) -> tuple[int, float]:
    """PS118's sort key: a case that runs alone before every other, then the longer before the shorter."""
    return (0 if alone else 1, -float(seconds))


# A case's run_one may say when its games' part begins (case_waits() before it waits for the machine's
# slots, case_begins() once it has them): the scheduler then counts only that part in `ran_with`, so a
# case that only waited for another case's slots is not listed as having run beside it.
_CASE_MARK = threading.local()


def case_waits() -> None:
    """Called by run_one before it waits for the machine's slots (schedule_cases)."""
    if (f := getattr(_CASE_MARK, "waits", None)) is not None:
        f()


def case_begins() -> None:
    """Called by run_one once its games may start (its slots taken; schedule_cases)."""
    if (f := getattr(_CASE_MARK, "begins", None)) is not None:
        f()


def schedule_cases(specs: list[Spec], jobs: int, max_games: int, run_one, say=None,
                   poll: float = 0.2, done=None, more=None, rank=None) -> list[CaseResult]:
    """Runs `run_one(spec)` for every spec, up to `jobs` at once (1: one after another in this
    thread, as before --jobs), each started in order once its games fit within `max_games` beside the
    running ones; an exclusive spec only when nothing runs, and nothing beside it. A runner error in
    one case (an exception) is that case's `error` result; a SystemExit or KeyboardInterrupt starts
    nothing more, waits for the running cases and is raised again. Returns the results in the specs'
    order, each with `ran_with` (the cases whose runs overlapped it: from case_begins(), when run_one
    calls case_waits() first; a case that waited and never began overlaps nothing) and `exclusive` set.

    PS14: `done(spec, result)` is called as each case ends (after its status line; its `ran_with` and
    `exclusive` already set), so the caller writes its results then. `more()` is called before each
    case start (and once more before the end): the specs it returns are appended to `specs` (the
    list grows) and run next, before the rest (`test --queue`).

    PS118: `rank(spec)` (RunOrder), when given, is the start order's sort key: the specs, and each of
    more()'s takes among themselves, start in its order (equal keys keep theirs); the results stay in
    the specs' order. Without it the specs start in their own order, as before."""
    jobs = max(1, jobs)
    printing = threading.Lock()

    def tell(text: str) -> None:
        with printing:
            (say or (lambda s: print(s, flush=True)))(text)

    results: list[CaseResult | None] = [None] * len(specs)
    spans: list[tuple[float, float]] = [(0.0, 0.0)] * len(specs)
    begun: dict[int, float] = {}  # index -> its start (monotonic), for ran_with as it ends
    waiting: set[int] = set()  # running cases that said case_waits() and not yet case_begins()
    never_began: set[int] = set()  # ended while waiting (capacity, breaker): they overlapped nothing
    running: dict[int, tuple[int, bool]] = {}  # index -> (games held, exclusive)
    stop: list[BaseException] = []
    cond = threading.Condition()  # an RLock inside: the inline run (jobs 1) takes it again
    refresh = [more is not None]  # more() is due: at the start, after each case start and each end

    def overlapped(i: int, s0: float, e0: float) -> list[str]:
        if i in never_began:
            return []
        return [specs[j].name for j, s1 in begun.items()
                if j != i and j not in waiting and j not in never_began
                and (j in running or (results[j] is not None and results[j].status != "capacity"
                                      and s1 < e0 and s0 < spans[j][1]))]

    def mark_wait(i: int) -> None:
        with cond:
            waiting.add(i)

    def mark_begin(i: int) -> None:
        with cond:
            waiting.discard(i)
            begun[i] = time.monotonic()

    def take_more() -> None:
        if not refresh[0]:
            return
        refresh[0] = False
        try:
            new = list(more())
        except Exception as e:  # noqa: BLE001 - a queue that cannot be read starts nothing new
            tell(f"queue: not read: {type(e).__name__}: {e}")
            return
        first = len(specs)
        for sp in new:
            specs.append(sp)
            results.append(None)
            spans.append((0.0, 0.0))
        pending[0:0] = ranked(range(first, len(specs)))

    def one(i: int) -> None:
        spec, t0, r = specs[i], time.monotonic(), None
        _CASE_MARK.waits, _CASE_MARK.begins = (lambda: mark_wait(i)), (lambda: mark_begin(i))
        try:
            r = run_one(spec)
        except Exception as e:  # noqa: BLE001 - one case's runner error never stops the others
            r = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, status="error",
                           reasons=[f"runner error: {type(e).__name__}: {e}"])
        except BaseException as e:  # noqa: BLE001 - raised again once the running cases ended
            with cond:
                stop.append(e)
        finally:
            _CASE_MARK.waits = _CASE_MARK.begins = None
            with cond:
                if i in waiting:
                    waiting.discard(i)
                    never_began.add(i)
                results[i], spans[i] = r, (begun.get(i, t0), time.monotonic())
                try:
                    if r is not None:  # before the case leaves `running`: the run's last line comes last
                        r.exclusive = runs_alone(spec)
                        if r.status != "capacity":
                            r.ran_with = overlapped(i, *spans[i])
                        why = f": {'; '.join(r.reasons)}" if r.reasons else ""
                        tell(f"  {r.status.upper()}{why}" if jobs == 1 else f"{spec.name}: {r.status.upper()}{why}")
                        if done is not None:
                            try:
                                done(spec, r)
                            except Exception as e:  # noqa: BLE001 - the case's result stands; the end writes it
                                tell(f"{spec.name}: its results were not written now ({type(e).__name__}: {e});"
                                     " the run's end writes them")
                finally:  # a print that fails (encoding, a closed pipe) never keeps the case in `running`
                    running.pop(i, None)
                    refresh[0] = more is not None
                    cond.notify_all()

    def ranked(indices) -> list[int]:
        """The indices in the start order: rank's (stable), else as given."""
        return sorted(indices, key=lambda i: rank(specs[i])) if rank is not None else list(indices)

    pending = ranked(range(len(specs)))
    told: set[int] = set()
    with cond:
        while True:
            if more is not None and not stop and len(running) < jobs:
                take_more()  # PS14: the queue's new cases run next
            if not ((pending and not stop) or running):
                break
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
                    begun[i] = time.monotonic()
                    refresh[0] = more is not None
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
            r.ran_with = [] if i in never_began else [
                specs[j].name for j, (s1, e1) in enumerate(spans)
                if j != i and j not in never_began and results[j] is not None and results[j].status != "capacity"
                and s1 < e0 and s0 < e1]
        out.append(r)
    return out


# --- the machine's game budget (PS15) and the circuit breaker (C7) --------------------------------
#
# Every runner on the machine shares one budget of games: lanes are several `skytest test` processes,
# from any worktree of the repository, and before PS15 each held its own --max-games, so 9 games ran
# at once while every runner printed "at most 5" (a game uses 2.1-3.3 of the 16 threads; hub load
# 7.4 s at 2 games, 17.5 s at 4, 108 s at 7-8; symmetry diverged at about 17 fps). The budget is
# `[defaults] machine_games` (MACHINE_GAMES) slots, each a file in the main checkout's
# .replication-tests/slots/ (main_store) held with a byte-range lock (msvcrt.locking): a runner that
# dies frees its slots with its handles, so nothing has to be cleaned up. A case takes a slot per game
# (its peers; a hosted server too) before its first game starts and waits for them, saying what it
# waits for, instead of failing; an exclusive or timed case (runs_alone) and a case with more games
# than the budget take every slot, so nothing of another runner runs beside a measurement. The first
# waiter holds `reserve.lock`, so a case that needs many slots is not starved by smaller ones. Free
# memory is re-checked when the slots are free (GB_PER_GAME per game of the case): short while other
# runners hold slots, the case waits; short with the machine otherwise idle, it is a capacity result.
# The boot token (`boot.lock`) lets one game boot at a time machine-wide: a start waits for the start
# gap after the last start of any runner, and the token is held until the game's first presented frame
# line (BOOT_DONE), its exit or BOOT_TIMEOUT_S. A hosted server is started by its owner's game, not by
# a runner, and takes no turn. `--jobs N` and --max-games work inside one runner on top of all this.
MACHINE_GAMES = 4
MACHINE_SLOT_FILES = 32  # slot-00 .. slot-31: a runner with a smaller budget still sees every holder
SLOT_LOCK_AT = 1 << 20  # the byte each lock file's lock covers, past its holder note at offset 0
SLOT_NOTE_BYTES = 512
SLOT_POLL_S = 0.5
GB_PER_GAME = 1.5  # available memory a game needs at its case's start (about 1 GB working set in a level)
MEMORY_RECHECK_S = 5.0  # memory short with nothing else running: re-sampled this long before `capacity`
MUTEX_TELL_S = 10.0  # a runner waiting this long for machine.lock says so (a holder suspended in a debugger)
BOOT_DONE = re.compile(r"Swap: game presentation")  # src/hooks/frame_rate_hook.cpp: the game's first swap
BOOT_TIMEOUT_S = 15.0

try:
    import msvcrt
except ImportError:  # not Windows: the runner's own tests elsewhere (POSIX locks are per process)
    msvcrt = None
    import fcntl

_PROCESS_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_PROCESS_LOCKS_LOCK = threading.Lock()
_HELD_SLOTS: set[tuple[str, int]] = set()  # (folder, slot) this process holds (threads of one runner)


def _process_lock(folder: Path, what: str) -> threading.Lock:
    """One lock per slot folder and purpose in this process: the file lock keeps other processes out,
    this one the other threads of the runner (with --jobs) whatever the platform's lock semantics."""
    with _PROCESS_LOCKS_LOCK:
        return _PROCESS_LOCKS.setdefault((str(folder), what), threading.Lock())


def _open_lock_file(path: Path) -> int:
    return os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))


def _try_lock(fd: int) -> bool:
    try:
        if msvcrt is not None:
            os.lseek(fd, SLOT_LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, SLOT_LOCK_AT, os.SEEK_SET)
        return True
    except OSError:
        return False


def _unlock_close(fd: int) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, SLOT_LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, SLOT_LOCK_AT, os.SEEK_SET)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_note(fd: int, note: dict) -> None:
    data = json.dumps(note).encode("utf-8")[:SLOT_NOTE_BYTES]
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data.ljust(SLOT_NOTE_BYTES))


def _read_note(path: Path) -> dict:
    """A lock file's holder note (who holds it); {} when it has none or cannot be read."""
    try:
        with open(path, "rb") as f:
            note = json.loads(f.read(SLOT_NOTE_BYTES).decode("utf-8", errors="replace").strip() or "{}")
        return note if isinstance(note, dict) else {}
    except (OSError, ValueError):
        return {}


def check_slots_folder(cfg: Config, folder: Path) -> None:
    """The slot folder is a test store's (`.replication-tests/slots`), never the owner's data or the disc."""
    if folder.parent.name != STORE:
        raise SystemExit(f"refusing the machine's slot folder {folder}: not inside a {STORE} folder")
    for root in owner_data_roots():
        if inside(folder, root):
            raise SystemExit(f"refusing the machine's slot folder {folder}: inside the owner's data folder {root}")
    if inside(folder, cfg.rom):
        raise SystemExit(f"refusing the machine's slot folder {folder}: inside the disc")


def machine_budget(cfg: Config) -> "MachineBudget | None":
    if cfg.slots is None:
        return None
    check_slots_folder(cfg, cfg.slots)
    return MachineBudget(cfg.slots, cfg.machine_games)


def machine_need(spec: Spec, games: int) -> int:
    """The machine slots a case takes: one per game, or every slot for an exclusive or timed case
    (runs_alone) and for a case with as many games as the budget or more."""
    return games if runs_alone(spec) or len(spec.peers) >= games else len(spec.peers)


@dataclass
class Lease:
    """The machine slots one case holds (MachineBudget.acquire) until release(); `capacity` instead
    when the case cannot run (memory short with nothing else running)."""
    folder: str
    games: int
    slots: list[int] = field(default_factory=list)
    fds: list[int] = field(default_factory=list)
    waited_s: float = 0.0
    waited_for: list[str] = field(default_factory=list)
    capacity: str = ""

    def release(self) -> None:
        for i, fd in zip(self.slots, self.fds):
            _unlock_close(fd)
            with _PROCESS_LOCKS_LOCK:
                _HELD_SLOTS.discard((self.folder, i))
        self.fds = []

    def summary(self) -> dict:
        return {"slots": len(self.slots), "of": self.games, "all": len(self.slots) >= self.games,
                "waited_s": round(self.waited_s, 1), "waited_for": self.waited_for[:5]}


class BootHold:
    """The machine's boot token while one game boots (MachineBudget.boot_turn): watch() releases it in a
    thread of its own at the game's first presented frame line, its exit or BOOT_TIMEOUT_S; release()
    at once (a start that failed). `outcome` says which came."""

    def __init__(self, fd: int, lock: threading.Lock, waited: float) -> None:
        self.fd, self.lock, self.waited_s = fd, lock, waited
        self.outcome = ""
        self._released = threading.Event()
        self._once = threading.Lock()

    def release(self) -> None:
        with self._once:
            if self._released.is_set():
                return
            self._released.set()
            _unlock_close(self.fd)
            self.lock.release()

    def watch(self, log: Path, proc: subprocess.Popen, timeout: float | None = None,
              on_done=None) -> threading.Thread:
        limit = BOOT_TIMEOUT_S if timeout is None else timeout

        def run() -> None:
            t0, tail = time.monotonic(), LogTail(log)
            try:
                while not self._released.is_set():
                    took = time.monotonic() - t0
                    if any(BOOT_DONE.search(line) for line in tail.new_lines()):
                        self.outcome = f"first frame after {took:.1f} s"
                    elif proc.poll() is not None:
                        self.outcome = f"exited after {took:.1f} s"
                    elif took >= limit:
                        self.outcome = f"no first frame line within {limit:.0f} s"
                    if self.outcome:
                        break
                    time.sleep(0.1)
            finally:
                self.release()
                if on_done is not None:
                    on_done(self)

        t = threading.Thread(target=run, name="skytest-boot", daemon=True)
        t.start()
        return t


def memory_recheck(want: float, mem: float, seconds: float | None = None, every: float = 1.0) -> float | None:
    """Free memory sampled again for up to `seconds` (MEMORY_RECHECK_S) until it reaches `want`: the
    highest sample (a single low sample right after the previous case's games exited is no capacity)."""
    end = time.monotonic() + (MEMORY_RECHECK_S if seconds is None else seconds)
    best = mem
    while best < want and time.monotonic() < end:
        time.sleep(min(every, max(0.0, end - time.monotonic())))
        if (m := available_memory_gb()) is not None:
            best = max(best, m)
    return best


class MachineBudget:
    """The game budget every runner on the machine shares (PS15): `games` slots in `folder`."""

    def __init__(self, folder: Path, games: int, owner: str = "") -> None:
        self.folder, self.games = Path(folder), max(1, min(int(games), MACHINE_SLOT_FILES))
        self.owner = owner or f"skytest pid {os.getpid()}"
        self.folder.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def mutex(self):
        """The machine-wide critical section (machine.lock) for taking slots and writing the breaker."""
        with _process_lock(self.folder, "mutex"):
            fd = _open_lock_file(self.folder / "machine.lock")
            try:
                t0, told = time.monotonic(), False
                while not _try_lock(fd):
                    if not told and time.monotonic() - t0 >= MUTEX_TELL_S:
                        told = True
                        print(f"skytest: waits for {self.folder / 'machine.lock'} for {MUTEX_TELL_S:.0f} s: another"
                              " runner holds it (a runner stopped in a debugger or frozen holds every runner here)",
                              flush=True)
                    time.sleep(0.02)
            except BaseException:
                os.close(fd)
                raise
            try:
                yield
            finally:
                _unlock_close(fd)

    def slot_path(self, i: int) -> Path:
        return self.folder / f"slot-{i:02d}"

    def _probe(self) -> tuple[list[tuple[int, int]], dict[int, dict]]:
        """Inside the mutex: every slot file that is free, locked now ((slot, fd)), and the holders'
        notes of the others."""
        taken, held = [], {}
        try:
            for i in range(MACHINE_SLOT_FILES):
                with _PROCESS_LOCKS_LOCK:
                    mine = (str(self.folder), i) in _HELD_SLOTS
                if not mine:
                    fd = _open_lock_file(self.slot_path(i))
                    if _try_lock(fd):
                        taken.append((i, fd))
                        continue
                    os.close(fd)
                held[i] = _read_note(self.slot_path(i))
        except BaseException:  # a slot file that cannot be opened: the slots locked so far are freed
            for _, fd in taken:
                _unlock_close(fd)
            raise
        return taken, held

    def held_all(self) -> str:
        """Who holds every slot now (an exclusive or timed case of any runner: its note says `all`), as
        `<case> (<owner>)`; '' when nobody does. Takes nothing."""
        with self.mutex():
            taken, held = self._probe()
            for _, fd in taken:
                _unlock_close(fd)
        for note in held.values():
            if note.get("all"):
                return f"{note.get('case', '?')} ({note.get('owner', '?')})"
        return ""

    def holders(self, held: dict[int, dict]) -> str:
        groups: dict[tuple, int] = {}
        for note in held.values():
            key = (str(note.get("case", "?")), str(note.get("owner", "?")))
            groups[key] = groups.get(key, 0) + 1
        return ", ".join(f"{case} ({owner}, {n})" for (case, owner), n in sorted(groups.items())) or "nobody"

    def acquire(self, need: int, case: str, memory_games: int = 0, say=None, idle=lambda: None,
                poll: float = SLOT_POLL_S) -> Lease:
        """Waits until `need` slots are free and memory fits `memory_games` games (default: need), and
        takes them. `say(text)` is told what the case waits for, once per change."""
        need = max(1, min(int(need), self.games))
        memory_games = memory_games or need
        t0, told = time.monotonic(), ""
        lease = Lease(folder=str(self.folder), games=self.games)
        reserve: int | None = None
        rechecked: float | None = None  # the best free memory of the re-check (memory_recheck), once done
        try:
            while True:
                why, recheck = "", None
                with self.mutex():
                    if reserve is None:
                        fd = _open_lock_file(self.folder / "reserve.lock")
                        if _try_lock(fd):
                            reserve = fd
                            _write_note(fd, {"case": case, "owner": self.owner, "need": need, "since": time.time()})
                        else:
                            os.close(fd)
                    if reserve is None:
                        note = _read_note(self.folder / "reserve.lock")
                        why = (f"machine slots: {note.get('case', '?')} ({note.get('owner', '?')}) waits first"
                               f" for {note.get('need', '?')}")
                    else:
                        taken, held = self._probe()
                        # A holder of every slot (its note says `all`: a runner whose budget is larger than
                        # this one's) leaves nothing free whatever the count says.
                        free = 0 if any(n.get("all") for n in held.values()) else self.games - len(held)
                        if free >= need:
                            keep = taken[:need]
                            for _, fd in taken[need:]:
                                _unlock_close(fd)
                            mem = available_memory_gb()
                            want = memory_games * GB_PER_GAME
                            if mem is not None and mem < want and not held and rechecked is not None:
                                mem = max(mem, rechecked)
                            if mem is not None and mem < want:
                                for _, fd in keep:
                                    _unlock_close(fd)
                                if not held and rechecked is None:
                                    # Nothing else runs: the previous case's games may only just have exited
                                    # (their memory not back yet); sampled again, outside the mutex, first.
                                    recheck = (want, mem)
                                elif not held:
                                    lease.capacity = (f"needs {want:.1f} GB of free memory for {memory_games} game(s),"
                                                      f" {mem:.1f} GB free with no other runner's game running")
                                    lease.waited_s = time.monotonic() - t0
                                    return lease
                                else:
                                    why = (f"memory: needs {want:.1f} GB free for {memory_games} game(s), {mem:.1f}"
                                           f" GB free; slots held by {self.holders(held)}")
                            else:
                                note = {"case": case, "owner": self.owner, "pid": os.getpid(), "games": need,
                                        "all": need >= self.games, "since": time.time()}
                                for i, fd in keep:
                                    _write_note(fd, note)
                                    with _PROCESS_LOCKS_LOCK:
                                        _HELD_SLOTS.add((str(self.folder), i))
                                lease.slots, lease.fds = [i for i, _ in keep], [fd for _, fd in keep]
                                lease.waited_s = time.monotonic() - t0
                                return lease
                        else:
                            for _, fd in taken:
                                _unlock_close(fd)
                            what = "every slot" if need >= self.games else f"{need} of {self.games}"
                            why = (f"machine slots: needs {what}, {max(0, free)} free; held by"
                                   f" {self.holders(held)}")
                if recheck is not None:
                    rechecked = memory_recheck(*recheck)
                    continue
                if why and why != told:
                    told = why
                    lease.waited_for.append(why)
                    if say is not None:
                        say(why)
                end = time.monotonic() + poll
                while time.monotonic() < end:
                    idle()
                    time.sleep(min(0.1, max(0.0, end - time.monotonic())))
        finally:
            if reserve is not None:
                _unlock_close(reserve)

    def boot_turn(self, gap: float, idle=lambda: None) -> BootHold:
        """The machine's boot token for one game start: waits until no game of any runner is booting
        and `gap` seconds have passed since the last start any runner made, then takes it."""
        lock, t0 = _process_lock(self.folder, "boot"), time.monotonic()
        while True:
            if lock.acquire(blocking=False):
                try:
                    fd = _open_lock_file(self.folder / "boot.lock")
                except BaseException:  # never keeps this runner's boot lock: every later start would hang
                    lock.release()
                    raise
                if _try_lock(fd):
                    break
                os.close(fd)
                lock.release()
            idle()
            time.sleep(0.05)
        try:
            try:
                last = float(_read_note(self.folder / "boot.lock").get("last_start", 0.0))
            except (TypeError, ValueError):
                last = 0.0
            if not math.isfinite(last) or last > time.time():
                last = 0.0  # a start in the future (a clock set back, a resumed machine, a bad note): no wait
            end = time.monotonic() + max(0.0, min(last + gap - time.time(), gap))
            while (left := end - time.monotonic()) > 0:
                idle()
                time.sleep(min(0.1, left))
            _write_note(fd, {"last_start": time.time(), "owner": self.owner})
        except BaseException:
            _unlock_close(fd)
            lock.release()
            raise
        return BootHold(fd, lock, time.monotonic() - t0)


def machine_line(m: dict) -> str:
    """report.md's machine line for a case (Lease.summary, with `boot_wait_s` and `not_alone`)."""
    if not m:
        return "Machine: no slots recorded."
    if "slots" in m:
        text = f"Machine: {m['slots']} of {m['of']} game slots" + (" (all: nothing of another runner beside it)"
                                                                    if m.get("all") else "")
    else:
        text = "Machine: no slots recorded"
    if m.get("waited_s"):
        text += f"; waited {m['waited_s']:.0f} s" + (f" ({m['waited_for'][0]})" if m.get("waited_for") else "")
    if m.get("boot_wait_s"):
        text += f"; its games waited {m['boot_wait_s']:.0f} s in all for the machine's boot turn (in its seconds)"
    if m.get("not_alone"):
        text += f"; NOT ALONE: {m['not_alone']}"
    return text + "."


def not_alone_note(spec: Spec, load: dict) -> str:
    """PS15: an exclusive or timed case beside games its runner did not start (load.outside_games_max:
    another repository's runner, an older runner, a game started by hand), which the slots cannot hold
    off; '' otherwise."""
    n = load.get("outside_games_max") if load else None
    if not runs_alone(spec) or not n:
        return ""
    return (f"{n} game(s) not started by this runner ran beside this exclusive or timed case (another repository's"
            " runner, an older runner or a game started by hand): its time and load grades are not its own")


# The circuit breaker (C7, PS66): when BREAKER_CASES cases of any runner on one build snapshot stopped
# on the same fatal signature, no runner on that snapshot starts another case (P1l part B lost 5 of 13
# cases, 793 s, to one S crash that every lane found again). A signature is the case's crash line when
# a peer logged one (its crash site: the exception and the call chain), else its first `[FATAL]` line or
# line of the peer's own `forbid`, normalized (case_signature): the log prefix (time, level, thread),
# thread and process ids, ports and heap addresses (hex numbers from 0x10000 on, outside the call chain)
# are taken out; code addresses (sub_..., module!symbol+offset) and small numbers (a null pointer's
# offset) stay. A symptom that many causes produce is no signature (BREAKER_WEAK: a `waitfor ... timed
# out`, every dead session's default line, the server's missing hub, an input script that cannot be
# parsed; machine load and the environment produce them as often as the build, S13), even when a
# spec's forbid names it, nor is a bare crash exit code. Each case that failed with a signature appends
# a line to `breaker-<snapshot>.jsonl` in the slot folder (under the machine mutex); the breaker trips
# at BREAKER_CASES distinct case names among the rows of the last BREAKER_WINDOW_S. A spec's `breaker =
# false` neither counts nor stops (a case that crashes on purpose); `test --no-breaker` starts cases on
# a tripped snapshot anyway (its failures still count); `skytest.py breaker --reset` deletes the files.
# A new build is a new snapshot.
BREAKER_CASES = 3
BREAKER_FRAMES = 8  # the call chain frames a crash signature keeps
BREAKER_WINDOW_S = 12 * 3600.0  # older rows do not count (a fixed config or a quiet machine starts clean)
GENERIC_FATAL = (re.compile(r"Input script: waitfor \S+ timed out"),)
# The lines a signature is taken from besides the peer's forbid (crash lines first: CRASH_LINE).
BREAKER_SIGNATURE_LINES = [r"\] Crash: ", r"\[FATAL\]"]
BREAKER_WEAK = GENERIC_FATAL + tuple(re.compile(rx) for _, rx in DEFAULT_FATAL) + (
    re.compile(r"Online: the server did not reach its hub"), re.compile(r"Input script: cannot (parse|use)"))
CRASH_LINE = re.compile(r"\] Crash: ")
LOG_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+")
HEX_NUMBER = re.compile(r"(?<!exception )\b(0x[0-9A-Fa-f]+|(?=[0-9A-Fa-f]*\d)[0-9A-Fa-f]{8,16})\b")
CODE_FRAME = re.compile(r"^(sub_[0-9A-Fa-f]+(\+0x[0-9A-Fa-f]+)?|[\w.-]+!\S+)$")


def _heap_address(m: re.Match) -> str:
    try:
        return "<addr>" if int(m.group(1), 16) >= 0x10000 else m.group(1)
    except ValueError:
        return m.group(1)


def normalize_fatal(line: str) -> str:
    """A fatal line as a signature (the circuit breaker's): see BREAKER_CASES."""
    s = LOG_PREFIX.sub("", line.strip())
    s = re.sub(r"\b(thread|pid|process) \d+", r"\1 <n>", s)
    s = re.sub(r"\b(\d{1,3}(?:\.\d{1,3}){3}):\d+", r"\1:<port>", s)
    head, sep, chain = s.partition("; call chain: ")
    head = HEX_NUMBER.sub(_heap_address, head)
    if sep:
        frames = [f if CODE_FRAME.match(f) else HEX_NUMBER.sub("<addr>", f)
                  for f in chain.split(" <- ")[:BREAKER_FRAMES]]
        head += sep + " <- ".join(frames)
    return head[:400]


def case_signature(spec: Spec, texts: dict[str, str]) -> dict:
    """A failed case's fatal signature (the circuit breaker's): the earliest crash line of any peer,
    else the earliest `[FATAL]` or `forbid` line that is not a weak symptom (BREAKER_WEAK); {} when there
    is none. Each pattern is searched on its own (a forbid may start with a global flag such as (?i))."""
    crash: tuple | None = None
    first: tuple | None = None
    for n, text in texts.items():
        ps = spec.peers.get(n)
        if not text:
            continue
        hits: dict[int, str] = {}  # line start -> line, in the log's order once sorted
        for rx in (ps.forbid if ps else []) + BREAKER_SIGNATURE_LINES:
            try:
                pattern = re.compile(rx, re.M)
            except re.error:
                continue  # load_spec checked every forbid; never a runner error here
            for m in pattern.finditer(text):
                start = text.rfind("\n", 0, m.start()) + 1
                if start not in hits:
                    end = text.find("\n", m.end())
                    hits[start] = text[start:end if end >= 0 else len(text)].rstrip("\r")
        for start in sorted(hits):
            line = hits[start]
            if any(g.search(line) for g in BREAKER_WEAK):
                continue
            stamp = TS.match(line)
            ts = stamp.group(1) if stamp else "~"  # a line without a time sorts last
            if CRASH_LINE.search(line):
                if crash is None or ts < crash[0]:
                    crash = (ts, n, line)
                break  # this peer's crash: nothing after it matters
            if first is None or ts < first[0]:
                first = (ts, n, line)
    pick = crash or first
    if pick is None:
        return {}
    return {"signature": normalize_fatal(pick[2]), "peer": pick[1], "line": pick[2].strip()[:300],
            "kind": "crash" if crash else "line"}


class CircuitBreaker:
    """The breaker of one build snapshot, shared by every runner through `breaker-<snapshot>.jsonl` in
    the machine's slot folder."""

    def __init__(self, budget: MachineBudget, snapshot: str, cases: int = BREAKER_CASES) -> None:
        self.budget, self.snapshot, self.cases = budget, snapshot, cases
        self.path = budget.folder / f"breaker-{snapshot}.jsonl"

    def record(self, case: str, folder: str, sig: dict) -> None:
        row = {"case": case, "run": folder, "snapshot": self.snapshot, "at": time.strftime("%Y%m%d-%H%M%S"),
               "t": round(time.time(), 1), "pid": os.getpid(), **sig}
        with self.budget.mutex():
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def tripped(self, now: float | None = None) -> dict | None:
        """{signature, cases, peers, snapshot, file} once BREAKER_CASES distinct cases stopped on one
        signature within the last BREAKER_WINDOW_S."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        now = time.time() if now is None else now
        by_sig: dict[str, dict] = {}
        for raw in text.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue  # a line being written
            if not isinstance(row, dict) or not row.get("signature") or not row.get("case"):
                continue
            at = breaker_row_time(row)
            if at is None or not (now - BREAKER_WINDOW_S <= at <= now + 60):
                continue  # too old, or a time that cannot be read (a clock set back counts as unknown)
            g = by_sig.setdefault(row["signature"], {"cases": [], "peers": []})
            if row["case"] not in g["cases"]:
                g["cases"].append(row["case"])
            if row.get("peer") and row["peer"] not in g["peers"]:
                g["peers"].append(row["peer"])
            if len(g["cases"]) >= self.cases:
                return {"signature": row["signature"], "cases": g["cases"], "peers": g["peers"],
                        "snapshot": self.snapshot, "file": str(self.path)}
        return None


def breaker_row_time(row: dict) -> float | None:
    """When a breaker row was written: its `t`, else its `at` stamp (local time); None when neither reads."""
    try:
        return float(row["t"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return time.mktime(time.strptime(str(row.get("at", "")), "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


def breaker_line(t: dict) -> str:
    return (f"circuit breaker: {len(t['cases'])} cases on build {t['snapshot']} stopped on `{t['signature']}`"
            f" ({', '.join(t['cases'])}): no new case starts; `--no-breaker` (or `breaker = false` in a spec)"
            f" starts them anyway; `skytest.py breaker --reset` clears it ({t.get('file', 'the slot folder')};"
            f" rows older than {BREAKER_WINDOW_S / 3600:.0f} h do not count)")


def cmd_breaker(cfg: Config, a: argparse.Namespace) -> int:
    """C7: each build's breaker file in the machine's slot folder (tripped or not, from its rows of the
    last BREAKER_WINDOW_S); `--reset` deletes them. Starts no game."""
    budget = machine_budget(cfg)
    if budget is None:
        print("no slot folder (the repository has no test store)")
        return 0
    files = sorted(budget.folder.glob("breaker-*.jsonl"))
    if getattr(a, "reset", False):
        with budget.mutex():
            for f in files:
                f.unlink(missing_ok=True)
        print(f"circuit breaker: reset, {len(files)} file(s) deleted in {budget.folder}")
        return 0
    for f in files:
        t = CircuitBreaker(budget, f.stem.removeprefix("breaker-")).tripped()
        print(breaker_line(t) if t else f"{f.name}: not tripped")
    print(f"{len(files)} breaker file(s) in {budget.folder}")
    return 0


def breaker_result(spec: Spec, t: dict) -> CaseResult:
    """A case the tripped breaker did not start: status `breaker`, 0 s, never a pass."""
    return CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, status="breaker",
                      reasons=[f"not started: {breaker_line(t)}"], exclusive=runs_alone(spec),
                      started=time.strftime("%Y%m%d-%H%M%S"))


# --- commands -------------------------------------------------------------------------------------


def default_max_games() -> int:
    """This runner's own cap without --max-games: from the cores (a game takes two threads' worth of
    CPU). Free memory is no longer read once here: MachineBudget.acquire re-checks it at each case's
    start (PS15), so a case waits for memory instead of being a capacity result for the whole run."""
    cores = os.cpu_count() or 4
    return max(1, min(5, cores // 3))


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


# The test store's line (tools/test_store_janitor.py --line), never in the way of the first case (PS1:
# the janitor's walk took 145-235 s before every `test` in a store of 500 runs, 37 % of a lane's time):
# it runs beside the cases at a below-normal CPU priority and in Windows' background mode (low I/O and
# memory priority: JANITOR_BOOTSTRAP), and its line comes after them. It never runs beside a measured
# case: a run with a case that runs alone (runs_alone) skips the walk, a case of the queue that runs
# alone ends a walk still running, and a walk is not started while another runner's case holds every
# machine slot (MachineBudget.held_all). The runner waits at most JANITOR_WAIT_S for it after the cases,
# then ends it and says so.
JANITOR_WAIT_S = 20.0
# Starts the janitor in background mode (PROCESS_MODE_BACKGROUND_BEGIN can only be set by the process
# itself), then runs it as its own script: its command line and output stay as they are (a game
# repository's older janitor too).
JANITOR_BOOTSTRAP = (
    "import ctypes, runpy, sys\n"
    "try:\n"
    "    k = ctypes.windll.kernel32\n"
    "    k.GetCurrentProcess.restype = ctypes.c_void_p\n"
    "    k.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)\n"
    "    k.SetPriorityClass(k.GetCurrentProcess(), 0x00100000)\n"
    "except Exception:\n"
    "    pass\n"
    "script = sys.argv.pop(1)\n"
    "sys.argv[0] = script\n"
    "runpy.run_path(script, run_name='__main__')\n")
JANITOR_NOT_MEASURED = "test store: not measured ({why}; `just janitor` reports the store)"


class StoreJanitor:
    """The janitor's `--line` in a process of its own: start() launches it (when the game repository has
    the janitor; not while another runner's case holds every machine slot), skip() says why it was not,
    line() waits for it at most `wait` seconds and returns its line, or a note that it was ended or not
    run. stop() ends it (a run that stops early; with a reason, a case that runs alone is starting)."""

    def __init__(self, cfg: Config) -> None:
        self.script = cfg.repo / "tools" / "test_store_janitor.py"
        self.repo = cfg.repo
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.note = ""
        self._lock = threading.Lock()

    def skip(self, why: str) -> "StoreJanitor":
        if self.script.is_file() and not self.note:
            self.note = JANITOR_NOT_MEASURED.format(why=why)
        return self

    def start(self, budget: "MachineBudget | None" = None) -> "StoreJanitor":
        with self._lock:
            if self.proc is not None or self.note or not self.script.is_file():
                return self
            holder = ""
            if budget is not None:
                try:
                    holder = budget.held_all()
                except OSError:
                    holder = ""
            if holder:
                self.note = JANITOR_NOT_MEASURED.format(
                    why=f"every machine slot is held by {holder}: the walk never runs beside a measured case")
                return self
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-I", "-c", JANITOR_BOOTSTRAP, str(self.script), "--repo", str(self.repo),
                     "--line"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True,
                    encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
                self.started = time.monotonic()
            except OSError:
                self.proc = None
        return self

    def line(self, wait: float = JANITOR_WAIT_S) -> str:
        if self.note:
            return self.note
        if self.proc is None:
            return ""
        try:
            out, _ = self.proc.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            self.stop()
            took = time.monotonic() - self.started
            return JANITOR_NOT_MEASURED.format(why=f"the janitor's walk was still running after {took:.0f} s")
        except (OSError, ValueError):
            return self.note
        return self.note or (out or "").strip()

    def stop(self, why: str = "") -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
                if why and not self.note:
                    self.note = JANITOR_NOT_MEASURED.format(why=f"the janitor's walk was ended: {why}")
                try:
                    self.proc.communicate(timeout=10)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    pass


def not_ready_result(spec: Spec) -> CaseResult:
    """PS2: a case that is not ready, reported at 0 s without a game (or a run folder)."""
    return CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, status="not ready",
                      reasons=[f"not ready: {spec.not_ready}"], not_ready=spec.not_ready,
                      exclusive=runs_alone(spec), started=time.strftime("%Y%m%d-%H%M%S"))


class CaseQueue:
    """`test --queue FILE` (PS14): a lane's case list that the runner re-reads before each case start.
    One case name or glob per line (several on a line, separated by spaces, also do); blank lines and
    `#` comments are skipped. Entries are tracked by their text: the n-th occurrence of an entry is
    taken once, so an entry added anywhere runs next, a case named twice runs twice, removing an entry
    already taken changes nothing, and an edited entry is a new one."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.taken: dict[str, int] = {}  # entry -> how many of its occurrences were taken
        self.missing_told = False

    def entries(self) -> list[str]:
        text = self.path.read_text(encoding="utf-8-sig")
        out = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            out += line.split()
        return out

    def take(self) -> list[str]:
        """The entries added since the last take ([] when the file is gone: said once)."""
        try:
            entries = self.entries()
        except OSError as e:
            if not self.missing_told:
                self.missing_told = True
                print(f"queue: {self.path} not read ({e}); nothing more is taken from it", flush=True)
            return []
        seen: dict[str, int] = {}
        new = []
        for e in entries:
            seen[e] = seen.get(e, 0) + 1
            if seen[e] > self.taken.get(e, 0):
                self.taken[e] = seen[e]
                new.append(e)
        return new


def cmd_test(cfg: Config, a: argparse.Namespace) -> int:
    manifest = load_manifest(cfg)
    stages = stages_up_to(manifest, a.stage) if a.stage else None
    gate_run = bool(getattr(a, "gate", False))
    per_build_run = bool(getattr(a, "per_build", False))
    if per_build_run and not gate_run:
        raise SystemExit("--per-build goes with --gate (`test --gate --per-build`: the per-build gate cases)")
    run_not_ready = bool(getattr(a, "run_not_ready", False))
    queue_file = getattr(a, "queue", None)
    queue = CaseQueue(Path(queue_file)) if queue_file else None
    if queue is not None:
        if gate_run:
            raise SystemExit("--queue and --gate do not go together (a gate run is the manifest's fixed subset)")
        if not queue.path.is_file():
            raise SystemExit(f"--queue {queue.path}: no such file")
    gate = None
    every_spec = all_specs(cfg)
    queue_unmatched: list[str] = []

    spec_told: set[tuple[str, str]] = set()

    def refresh_specs() -> None:
        """The specs read again for the queue's entries (R2): a new spec file is found and an edited one
        runs its new text; a spec that does not read now keeps the text read before (said once)."""
        nonlocal every_spec
        by_path = {sp.path: sp for sp in every_spec}
        fresh: list[Spec] = []
        for path in sorted(cfg.cases_dir.glob("*.toml")) if cfg.cases_dir.is_dir() else []:
            old = by_path.get(path)
            digest = file_sha256(path)
            if not digest or (old is not None and old.sha256 == digest):
                if old is not None:
                    fresh.append(old)
                continue
            try:
                fresh.append(load_spec(path, cfg.timeout))
                if old is not None and (path.name, digest) not in spec_told:
                    spec_told.add((path.name, digest))
                    print(f"queue: {path.name} changed since it was read; the queue's entries run its new text",
                          flush=True)
            except SpecError as e:
                if old is not None:
                    fresh.append(old)
                if (path.name, digest) not in spec_told:
                    spec_told.add((path.name, digest))
                    print(f"queue: {path.name} does not read now ({e}); "
                          + ("the text read before runs" if old is not None else "not taken"), flush=True)
        every_spec = fresh

    def from_queue() -> list[Spec]:
        """The queue's new entries as specs (each a copy of its own, so a case taken twice runs twice),
        from the spec files as they are now (refresh_specs); an entry that names no case is said and
        listed in the report."""
        chosen: list[Spec] = []
        entries = queue.take() if queue is not None else []
        if entries:
            refresh_specs()
        for entry in entries:
            found = [sp for sp in every_spec if fnmatch.fnmatch(sp.name, entry) or sp.path.stem == entry]
            if not found:
                queue_unmatched.append(entry)
                print(f"queue: {entry} matches no case (skipped); `skytest.py list` names them", flush=True)
            chosen += [replace(sp) for sp in found]
        return chosen

    specs = select(every_spec, a.cases, stages) if (a.cases or queue is None) else []
    if gate_run:
        cadence = GATE_PER_BUILD if per_build_run else "daily"
        gate = gate_estimate(cfg, manifest, stages, every_spec, run_not_ready=run_not_ready, cadence=cadence,
                             build=build_hash(cfg))
        gate_ids = {r["case"] for r in gate["cases"]}
        named = specs
        specs = [s for s in specs if s.name in gate_ids]
        if a.cases not in ([], ["all"]):
            gate = gate_only(gate, {s.name for s in specs}, a.cases)
            other = {c.get("id") for c in manifest.get("case", []) if gate_cadence(c) not in ("", cadence)}
            for s in named:
                if s.name in other:
                    print(f"{gate_label(gate)}: {s.name} is a per-build case: not run in the daily gate (`test --gate"
                          " --per-build` runs it); its pass on this build counts" if cadence == "daily" else
                          f"{gate_label(gate)}: {s.name} is a daily gate case: not run here (`test --gate` runs it)",
                          flush=True)
        print(gate_line(gate), flush=True)
    specs += from_queue()
    if not specs:
        raise SystemExit("no case to run" + (f" (the queue {queue.path} names none yet)" if queue is not None else ""))
    # Before anything is copied or started: a restricted token is refused unless --allow-restricted.
    token = token_preflight(bool(getattr(a, "allow_restricted", False)))
    gate_started = time.monotonic()

    def starts_games(sp: Spec) -> bool:
        """A case that starts games: not a unit case, nor one reported `not ready` (PS2)."""
        return sp.layout != UNIT and (run_not_ready or not sp.not_ready)

    # The disc and the game's snapshot only when a selected case starts games (a run of unit cases needs
    # neither); a game case the queue adds later takes them as it starts (game_snapshot).
    if any(starts_games(sp) for sp in specs):
        check_disc(cfg)
    if getattr(a, "remote", None):
        cfg.remote = remote_target(a.remote, getattr(a, "remote_token_file", None), getattr(a, "remote_manifest", None))
    max_games = a.max_games or default_max_games()
    jobs = getattr(a, "jobs", None)
    jobs = 1 if jobs is None else int(jobs)
    if jobs < 1:
        raise SystemExit("--jobs is 1 or more")
    # PS2: a case that is not ready starts no game; it is reported at 0 s with its reason.
    order: list[Spec] = []  # every case taken, in order (the command line's, then the queue's)
    runnable: list[Spec] = []  # the scheduler's list (it grows with the queue's cases)
    by_case: dict[int, CaseResult] = {}  # id(spec) -> its result, as each case ends

    def take(new: list[Spec]) -> list[Spec]:
        """Files the specs taken: the ready ones are returned (to run), the others get their `not
        ready` result at once."""
        ready = []
        for sp in new:
            order.append(sp)
            if run_not_ready or not sp.not_ready:
                ready.append(sp)
                continue
            print(f"{sp.name}: NOT READY: {sp.not_ready} (no game started)", flush=True)
            r = not_ready_result(sp)
            r.provenance = case_provenance(cfg, sp, fixtures)
            r.scenes = scene_policy(cfg, sp)
            by_case[id(sp)] = r
        return ready

    fixtures = fixture_sha256(cfg)
    runnable += take(specs)
    # PS118: a gate run (symmetry first, the owner's decision of 2026-11-07) and a run of --jobs above 1
    # (the queue's takes too) start the cases that run alone first, then the longest first.
    rank = RunOrder(cfg) if (jobs > 1 or gate_run) else None
    if rank is not None and len(runnable) > 1:
        print(rank.line(runnable), flush=True)
    janitor = StoreJanitor(cfg)
    budget = machine_budget(cfg)
    if any(runs_alone(sp) for sp in runnable):
        janitor.skip("a case of this run runs alone: the janitor's walk never runs beside a measured case")
    else:
        janitor.start(budget)  # beside the cases: nothing measured runs first
    try:
        snap: Path | None = None
        breaker: CircuitBreaker | None = None
        snapshot_lock = threading.Lock()

        def game_snapshot() -> Path:
            """The game's build snapshot and its circuit breaker (C7: keyed by the snapshot), taken once,
            by the first case that starts games."""
            nonlocal snap, breaker
            with snapshot_lock:
                if snap is None:
                    check_disc(cfg)
                    snap = snapshot_build(cfg)
                    breaker = CircuitBreaker(budget, snap.name) if budget is not None else None
                return snap

        if any(starts_games(sp) for sp in runnable):
            game_snapshot()
        guarded = guarded_folders(cfg)
        before_state = {k: folder_state(path, skip) for k, (path, skip) in guarded.items()}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = make_run_dir(cfg, cfg.store / "runs" / f"{stamp}-session")
        thresholds = tomllib.loads(cfg.thresholds.read_text(encoding="utf-8")) if cfg.thresholds.is_file() else {}
        baseline = tomllib.loads(cfg.baseline.read_text(encoding="utf-8")) if cfg.baseline.is_file() else {}
        # The last recorded status of each case before this run (findings, the checkpoint's "passed before").
        before = previous_statuses(cfg, out)
        # --jobs N: up to N cases at once within --max-games, exclusive ones alone (schedule_cases); the
        # start gap holds across them all (one StartPacer). PS15: every case first takes its slots of
        # the machine's budget (shared with every other runner), and each start the machine's boot token.
        if budget is not None:
            budget.owner = f"skytest pid {os.getpid()}, {out.name}"
        pacer = StartPacer(cfg.start_gap, budget)
        # C7: the circuit breaker of this build snapshot (game_snapshot), shared with every runner on the
        # machine; None while no case has started games. A unit case's spec.breaker is False.
        breaker_stops = not getattr(a, "no_breaker", False)
        breaker_told: list[str] = []

        def tell_breaker(t: dict) -> None:
            with _PROCESS_LOCKS_LOCK:
                if t["signature"] in breaker_told:
                    return
                breaker_told.append(t["signature"])
            print(breaker_line(t) if breaker_stops else
                  breaker_line(t).split(": no new case starts", 1)[0] + ": --no-breaker: cases go on starting",
                  flush=True)

        def tripped(spec: Spec) -> dict | None:
            t = breaker.tripped() if breaker is not None and spec.breaker else None
            if t is not None:
                tell_breaker(t)
            return t if breaker_stops else None

        def run_one(spec: Spec) -> CaseResult:
            entry = next((c for c in manifest.get("case", []) if c.get("id") == spec.name), {})
            # A unit case (stage 2 item 7) starts no game: no game snapshot, no machine slots (its
            # case_games is 0, so it fits beside any case), no boot token; an exclusive one still runs alone.
            unit = spec.layout == UNIT
            run = run_unit_case if unit else run_compare_case if spec.compare else run_case
            r: CaseResult | None = None
            lease: Lease | None = None
            case_waits()  # ran_with counts from case_begins(): the slots' wait is not running beside others
            if spec.sha256 and file_sha256(spec.path) not in ("", spec.sha256):
                print(f"{spec.name}: {spec.path.name} changed since it was read; the case runs the text read before"
                      " (provenance records that text's sha256)", flush=True)
            game = None if unit else game_snapshot()
            if (t := tripped(spec)) is not None:
                r = breaker_result(spec, t)
            elif budget is not None and not unit and len(spec.peers) <= max_games:
                need = machine_need(spec, budget.games)
                if runs_alone(spec):
                    janitor.stop("a case that runs alone started")  # PS1: never beside a measured case
                lease = budget.acquire(need, spec.name, memory_games=len(spec.peers),
                                       say=lambda why: print(f"{spec.name}: waits for {why}", flush=True))
                if lease.capacity:
                    r = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, status="capacity",
                                   reasons=[lease.capacity], started=time.strftime("%Y%m%d-%H%M%S"))
                elif (t := tripped(spec)) is not None:  # it tripped while this case waited
                    r = breaker_result(spec, t)
            try:
                if r is None:
                    case_begins()
                    if runs_alone(spec):
                        janitor.stop("a case that runs alone started")
                    # PS12: the load beside the case, sampled while it runs, and what ran.
                    sampler = LoadSampler(cfg.exe).start()
                    try:
                        r = run(cfg, spec, game, max_games, time.strftime("%Y%m%d-%H%M%S"), thresholds, baseline,
                                tuple(entry.get("accepted_grades", [])), pacer=pacer)
                    finally:
                        load = sampler.summary()
                    if r.status != "capacity":
                        r.load = load
            finally:
                if lease is not None:
                    lease.release()
            if lease is not None:
                r.machine = lease.summary()
            if waits := round(sum(p.boot_wait_s for p in r.peers), 1):
                r.machine["boot_wait_s"] = waits  # R2: the boot turn's cost, apart (it is in r.seconds)
            if note := not_alone_note(spec, r.load):
                r.machine["not_alone"] = note
                print(f"{spec.name}: NOT ALONE: {note}", flush=True)
            r.provenance = case_provenance(cfg, spec, fixtures)
            r.scenes = scene_policy(cfg, spec)
            if breaker is not None and spec.breaker and r.status in ("fail", "error") and r.signature:
                breaker.record(spec.name, r.folder, r.signature)
                if (t := breaker.tripped()) is not None:
                    tell_breaker(t)
            return r

        def finished() -> list[CaseResult]:
            return [by_case[id(sp)] for sp in order if id(sp) in by_case]

        def write_progress() -> None:
            """PS14: results.json, report.md and findings.md as they stand (the finished cases so far)."""
            done_now = finished()
            note = f"{len(done_now)} of {len(order)} case(s) ended" + (", more may come from the queue"
                                                                       if queue is not None else "")
            payload = {"in_progress": note, "snapshot": str(snap) if snap else "", "max_games": max_games, "jobs": jobs,
                       "machine": machine_info, "stages": stages, "restricted_token": token["restricted"],
                       "restricted_token_cause": token["cause"], "runner": runner_version(),
                       "cases": [asdict(r) for r in done_now]}
            write_text_replacing(out / "results.json", json.dumps(payload, indent=2))
            write_report(cfg, out, done_now, manifest, thresholds, max_games, [], [], token=token, jobs=jobs,
                         progress=note, queue_unmatched=queue_unmatched,
                         breaker=breaker.tripped() if breaker is not None else None)
            write_findings(out, done_now, before)

        def case_done(spec: Spec, r: CaseResult) -> None:
            """PS14: a case ended: its result.json in its own folder, then the session's files; the
            report's path is printed with it."""
            by_case[id(spec)] = r
            if r.folder and Path(r.folder).is_dir():
                write_text_replacing(Path(r.folder) / "result.json",
                                     json.dumps({"session": str(out), "runner": runner_version(), **asdict(r)},
                                                indent=2))
            write_progress()
            print(f"  report: {out / 'report.md'}" if jobs == 1 else f"{spec.name}: report: {out / 'report.md'}",
                  flush=True)

        machine_info = {"games": cfg.machine_games, "slots": str(cfg.slots) if cfg.slots else ""}
        write_progress()
        print(f"report (rewritten after every case): {out / 'report.md'}", flush=True)
        if not runnable and queue is not None:  # only not-ready cases so far: the queue may still add some
            runnable += take(from_queue())
        if runnable:
            got = schedule_cases(runnable, jobs, max_games, run_one, done=case_done,
                                 more=(lambda: take(from_queue())) if queue is not None else None, rank=rank)
            for sp, r in zip(runnable, got):  # `runnable` grew with the queue's cases
                by_case.setdefault(id(sp), r)
        results = [by_case[id(sp)] for sp in order]
        after_state = {k: folder_state(path, skip) for k, (path, skip) in guarded.items()}
        tripped_now = breaker.tripped() if breaker is not None else None
        store_line = janitor.line()  # never started after the cases (PS1): their end waits for nothing new
    finally:
        janitor.stop()
    guard = [{"folder": k, "before": before_state[k], "after": after_state[k],
              "unchanged": before_state[k] == after_state[k]} for k in guarded]
    # Required cases of the stages run here that have no spec yet: named, never skipped silently. A met_by
    # case with no spec of its own needs none (checkpoint_verdict grades its parts).
    unwritten = [c["id"] for c in manifest.get("case", [])
                 if c.get("kind") == "required" and not c.get("spec") and not c.get("met_by")
                 and (stages is None or str(c.get("stage")) in stages)] if (a.cases in ([], ["all"])
                                                                              and queue is None) else []
    if gate is not None:
        unwritten = list(gate["no_spec"])  # a gate run names its own spec-less cases, nothing else
    touched = [g["folder"] for g in guard if not g["unchanged"]]
    # A gate run is a subset: never a checkpoint; nor is a queue's run (its cases are the lane's).
    full_run = a.cases in ([], ["all"]) and gate is None and queue is None
    checkpoint = checkpoint_verdict(manifest, a.stage, stages, results, touched, before) if a.stage and full_run else None
    payload = {"snapshot": str(snap) if snap else "", "max_games": max_games, "jobs": jobs, "machine": machine_info,
               "breaker": tripped_now, "stages": stages, "thresholds": thresholds,
               "guarded_folders": guard, "required_without_spec": unwritten, "checkpoint": checkpoint,
               "restricted_token": token["restricted"], "restricted_token_cause": token["cause"],
               "runner": runner_version(), "cases": [asdict(r) for r in results]}
    if queue is not None:
        payload["queue"] = {"file": str(queue.path), "unmatched": queue_unmatched}
    if gate is not None:
        gate["actual_minutes"] = round((time.monotonic() - gate_started) / 60, 1)
        gate["case_minutes"] = round(sum(r.seconds for r in results) / 60, 1)
        if gate.get("per_build"):  # read again now: an idle slot's --per-build run may have ended meanwhile
            gate["build"] = snap.name if snap else build_hash(cfg)  # no game started: the build as it is
            states = per_build_states(cfg, gate["per_build"], every_spec, gate["build"], out)
            for row, state in zip(gate["per_build"], states):
                row["state"] = state
        gate["verdict"] = gate_verdict(gate, results, {r["case"]: r.get("state") for r in gate.get("per_build", [])})
        payload["gate"] = gate
    write_text_replacing(out / "results.json", json.dumps(payload, indent=2))
    write_report(cfg, out, results, manifest, thresholds, max_games, guard, unwritten, checkpoint, gate, token,
                 jobs=jobs, queue_unmatched=queue_unmatched, breaker=tripped_now)
    write_findings(out, results, before)
    failed = [r for r in results if r.status not in ("pass", "capacity")]
    capacity = [r for r in results if r.status == "capacity"]
    not_ready = [r for r in results if r.status == "not ready"]
    stopped = [r for r in results if r.status == "breaker"]
    if touched:
        print(f"WARNING: changed during the run: {', '.join(touched)}")
    if capacity:
        print(f"{len(capacity)} case(s) not run for capacity (a capacity result is reported, not failed):"
              f" {', '.join(r.case for r in capacity)}")
    if not_ready:
        print(f"{len(not_ready)} case(s) not ready (no game started; not met): "
              + "; ".join(f"{r.case} ({r.not_ready})" for r in not_ready))
    if queue_unmatched:
        print(f"{len(queue_unmatched)} queue entr(y/ies) matched no case: {', '.join(queue_unmatched)}")
    if stopped:
        print(f"{len(stopped)} case(s) not started by the circuit breaker (not met): "
              f"{', '.join(r.case for r in stopped)}")
    if checkpoint:
        print(checkpoint["line"])
    if gate is not None:
        label = gate_label(gate)
        print(f"{label}: {gate['actual_minutes']:.0f} min (the cases {gate['case_minutes']:.0f} min; expected"
              f" {gate['minutes']:.0f}); {gate['verdict']['line'].removeprefix(label + ': ')}")
    if store_line:
        print(store_line)
    print(f"report: {out / 'report.md'}")
    return 1 if failed or touched or queue_unmatched else 0


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
        g = gate_estimate(cfg, manifest, stages_up_to(manifest, stage) if stage else None, all_specs(cfg),
                          build=build_hash(cfg))
        for line in gate_table(g) + per_build_table(g):
            print(line)
        print(gate_line(g))
        return 0
    specs = {s.name: s for s in all_specs(cfg)}
    for s in specs.values():
        ready = f" (not ready: {s.not_ready})" if s.not_ready else ""
        print(f"{s.name:<32} {s.layout:<9} stage {s.stage:<4} {s.description}{ready}")
    man = load_manifest(cfg).get("case", [])
    if man:
        todo = [c for c in man if c["id"] not in specs and not c.get("met_by")]  # met_by: no spec of its own
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
            problems += scene_problems(cfg, s)
            if s.synthetic and not ((cfg.synthetic_exe or cfg.synthetic_launcher) and cfg.synthetic_setting):
                problems.append(f"{p.name}: synthetic peers, but tests/skytest.toml [synthetic] lacks exe or"
                                " server_setting")
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
        if c.get("gate", False) is not False and not gate_cadence(c):
            problems.append(f"manifest: {c.get('id')}: gate must be true, false or \"{GATE_PER_BUILD}\""
                            f" (not {c['gate']!r})")
        if c.get("kind") not in ("required", "unsupported", "accepted-failure"):
            problems.append(f"manifest: {c.get('id')}: kind must be required, unsupported or accepted-failure")
        if (c.get("layout") and not LAYOUT.match(c["layout"]) and c["layout"] != UNIT
                and c["layout"] not in MANIFEST_ONLY_LAYOUTS):
            problems.append(f"manifest: {c.get('id')}: unknown layout {c['layout']}")
        if c.get("kind") in ("accepted-failure", "unsupported") and not c.get("reason"):
            problems.append(f"manifest: {c.get('id')}: an {c['kind']} case needs a reason")
        if "amendment" in c and (not isinstance(c["amendment"], str) or not c["amendment"].strip()
                                 or not isinstance(c.get("amendment_accepted", False), bool)):
            problems.append(f"manifest: {c.get('id')}: amendment names the scope amendment (a string) and"
                            " amendment_accepted is true or false")
        if c.get("spec") and c.get("id") not in specs:
            problems.append(f"manifest: {c.get('id')}: spec = true but tests/runtime has no such case")
        if c.get("id") in specs and not c.get("spec"):
            problems.append(f"manifest: {c.get('id')}: has a spec; mark it spec = true")
        if c.get("id") in specs and specs[c["id"]].layout != c.get("layout"):
            problems.append(f"manifest: {c.get('id')}: layout {c.get('layout')} but its spec says {specs[c['id']].layout}")
        if c.get("id") in specs and specs[c["id"]].stage != str(c.get("stage")):
            problems.append(f"manifest: {c.get('id')}: stage {c.get('stage')} but its spec says {specs[c['id']].stage}")
    problems += met_by_problems(man)
    for name in specs:
        if man and name not in ids:
            problems.append(f"{name}: a spec with no manifest entry")
    for p in problems:
        print(p)
    # Notes, not problems: the cases that are not ready (PS2), and a spec whose last pass logged a line
    # it tolerates (PS5: a default fatal line taken out by its `tolerate`; worth a look).
    notes = [f"note: {s.path.name}: not ready ({s.not_ready})" for s in specs.values() if s.not_ready]
    tolerating = [s for s in specs.values() if s.tolerate]
    if tolerating:
        last = last_pass_folders(cfg)
        for s in tolerating:
            run = last.get(s.name)
            for peer, line in tolerated_lines(s, Path(run)) if run else []:
                notes.append(f"note: {s.path.name}: its last pass ({Path(run).name}) logged a line it tolerates: "
                             f"{peer} {line}")
    notes += scene_notes(cfg, list(specs.values()))
    for n in notes:
        print(n)
    # Warnings, not problems: an fps window that nothing grades (C10); with --timeouts, the waitfor
    # timeouts against their history in the store (C6).
    warnings = [w for sp in specs.values() for w in ungraded_fps_windows(sp)]
    warnings += [w for sp in specs.values() for w in idle_tolerate(sp)]
    if getattr(a, "timeouts", False):
        _, timeout_warnings, _ = timeout_report(cfg, list(specs.values()), getattr(a, "since", None))
        warnings += timeout_warnings
    for w in warnings:
        print(w)
    print(f"{len(specs)} spec(s), {len(man)} manifest case(s), {len(problems)} problem(s)"
          + (f", {len(warnings)} warning(s)" if warnings else ""))
    return 1 if problems else 0


def met_by_problems(man: list[dict]) -> list[str]:
    """A manifest case's `met_by` (checkpoint_verdict): a non-empty list of other cases of the manifest, each
    a required case of the same stage (a part of another stage, or one that may fail, would let the
    case pass on less than its own evidence), none itself a met_by case."""
    by_id = {c.get("id"): c for c in man}
    problems = []
    for c in man:
        if "met_by" not in c:
            continue
        parts = c["met_by"]
        if not isinstance(parts, list) or not parts or not all(isinstance(p, str) for p in parts):
            problems.append(f"manifest: {c.get('id')}: met_by is a list of case ids")
            continue
        if not c.get("spec") and c.get("gate", False) is not False:
            problems.append(f"manifest: {c.get('id')}: a met_by case with no spec of its own is not a gate case"
                            " (put its parts in the gate)")
        for p in parts:
            part = by_id.get(p)
            if p == c.get("id") or part is None:
                problems.append(f"manifest: {c.get('id')}: met_by names {p}, which is not another case of the manifest")
            elif part.get("kind") != "required" or str(part.get("stage")) != str(c.get("stage")):
                problems.append(f"manifest: {c.get('id')}: met_by {p} is not a required case of stage {c.get('stage')}")
            elif "met_by" in part:
                problems.append(f"manifest: {c.get('id')}: met_by {p} is itself met by other cases (one level only)")
    return problems


def scene_problems(cfg: Config, spec: Spec) -> list[str]:
    """PS6's lint: a spec that says no scene policy, a movies spec the game cannot run and a
    [defaults].args value that would reach a spec whose key says otherwise (scene_args refuses all three
    at a run), and a spec without the key whose own --dev_skip_scenes differs from a [defaults].args one
    (two values; the game's pick is unverified)."""
    try:
        scene_args(cfg, spec)
    except SpecError as e:
        return [str(e)]
    defaults = setting_values(cfg.default_args, SCENE_SETTING)
    if not spec.scenes and defaults and spec_sets_scenes(spec):
        own = {v for p in spec.peers.values() for v in setting_values(p.args, SCENE_SETTING)}
        if own - set(defaults):
            return [f"{spec.path.name}: sets --{SCENE_SETTING}={sorted(own - set(defaults))[0]} in its args and"
                    f" [defaults].args sets --{SCENE_SETTING}={defaults[-1]}: the game gets both (which one wins is"
                    " unverified); say scenes = \"skip\" or \"play\" and take the setting out of [defaults].args"]
    return []


def scene_notes(cfg: Config, specs: list[Spec]) -> list[str]:
    """PS6's note: the specs that set --dev_skip_scenes in their args instead of the scenes key (a spec
    with neither is a problem: scene_problems)."""
    if setting_values(cfg.default_args, SCENE_SETTING):
        return []
    in_args = sorted(sp.name for sp in specs if not sp.scenes and spec_sets_scenes(sp))
    notes = []
    if in_args:
        notes.append(f"note: {len(in_args)} spec(s) set --{SCENE_SETTING} in their args instead of the scenes key"
                     f" (scenes = \"skip\" or \"play\" says it): {', '.join(in_args)}")
    return notes


def idle_tolerate(spec: Spec) -> list[str]:
    """PS5: a spec's `tolerate` patterns that match none of the dead session's default lines as the game
    logs them (DEFAULT_FATAL_EXAMPLES): they take nothing out (a line that is not fatal anyway)."""
    idle = [t for t in spec.tolerate if not any(re.search(t, ex) for ex in DEFAULT_FATAL_EXAMPLES)]
    if not idle:
        return []
    return [f"warning: {spec.path.name}: tolerate pattern(s) that match no dead session's default fatal line:"
            f" {', '.join(repr(t) for t in idle)}; they take nothing out (forbid and the always-forbidden lines"
            " are not tolerated): drop them unless a default line is meant"]


# C10: an `fps N` window in a peer's script (the game measures N seconds of frames).
FPS_COMMAND = re.compile(r"^[ \t]*fps[ \t]+(\d+(?:\.\d+)?)[ \t]*$")
# S14 / PS67: every fps window feeds the frozen-game check (grade_peer: a window of 0 FPS fails the
# peer), graded or not, so lint never suggests deleting one; a window that only that check reads needs
# no more than 2 s (3.6 times the worst hitch seen, 549 ms in 498 windows).
FPS_LIVENESS_S = 2.0


def ungraded_fps_windows(spec: Spec) -> list[str]:
    """C10: the `fps N` windows longer than FPS_LIVENESS_S of a spec that grades no FPS: no client_fps
    grade, and none of the window's peer's expect, expect_count or expect_order patterns names FPS. Only
    the frozen-game check (a window of 0 FPS fails the peer) reads them then, and it needs no more than
    2 s: the warning suggests shortening each to `fps 2` (or grading it), never deleting it (S14: the
    window is that liveness check). One warning per spec, naming each window (peer, script line,
    seconds); a window of 2 s or less is not named."""
    if "client_fps" in spec.grades:
        return []
    windows = []
    for n, p in spec.peers.items():
        if any("FPS" in rx for rx in p.expect + list(p.expect_count) + p.expect_order):
            continue
        for no, line in enumerate(p.script.splitlines(), 1):
            if (m := FPS_COMMAND.match(line)) and float(m.group(1)) > FPS_LIVENESS_S:
                windows.append((n, no, float(m.group(1))))
    if not windows:
        return []
    named = ", ".join(f"{n} line {no} ({v:g} s)" for n, no, v in windows)
    spare = sum(v - FPS_LIVENESS_S for _, _, v in windows)
    return [f"warning: {spec.path.name}: fps window(s) with no FPS grade or expect: {named}; only the frozen-game"
            f" check reads them (a window of 0 FPS fails the peer), so keep each window: shorten it to"
            f" `fps {FPS_LIVENESS_S:g}`, enough for that check ({spare:g} s per run less), or grade it (client_fps,"
            " or an expect on `guest FPS`)"]


# C6: the waitfor timeouts against how long each condition took in the store's logs. The game logs
# `Input script: waitfor <condition> met after <s> s` or `... timed out after <s> s` for each waitfor
# (a timed-out one goes on with the script, so a silent hang waits the whole timeout).
WAITFOR_RESULT = re.compile(rb"Input script: waitfor (\S+) (met|timed out) after ([\d.]+) s")
WAITFOR_COMMAND = re.compile(r"^[ \t]*waitfor[ \t]+(\S+)[ \t]+([\d.]+)[ \t]*$")
TIMEOUT_MIN_SAMPLES = 5  # met waits a condition needs before a timeout is called too long
TIMEOUT_P95_FACTOR = 3.0
TIMEOUT_MARGIN_S = 60.0


def run_game_logs(folder: Path) -> list[Path]:
    """Every game.log of one run folder: <run>/<peer>/game.log, a hosted server's
    <run>/<peer>/data/Online/session/game.log, and a compared case's <run>/<name>/<peer>/... Only those
    folders are listed (scandir), never a peer's data, figures or cache."""
    out: list[Path] = []

    def peer_logs(at: str, depth: int) -> None:
        try:
            with os.scandir(at) as it:
                children = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for c in children:
            try:
                with os.scandir(c.path) as it:
                    names = {e.name for e in it}
            except OSError:
                continue
            if "game.log" in names:
                out.append(Path(c.path) / "game.log")
            if "data" in names:
                hosted = Path(c.path) / HOSTED_SERVER_LOG
                if hosted.is_file():
                    out.append(hosted)
            elif "game.log" not in names and depth == 0:
                peer_logs(c.path, 1)  # a compared case's run folder

    peer_logs(str(folder), 0)
    return out


def store_game_logs(store: Path, since: str | None = None) -> list[tuple[str, Path]]:
    """(run folder name, log) for every game.log of the store's runs (run_game_logs). `since`: only run
    folders whose name (it starts with the stamp, YYYYMMDD-HHMMSS) sorts at or after it."""
    out: list[tuple[str, Path]] = []
    runs = store / "runs"
    try:
        with os.scandir(runs) as it:
            run_dirs = sorted((e.name, e.path) for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return out
    for name, path in run_dirs:
        if since and name < since:
            continue
        out += [(name, log) for log in run_game_logs(Path(path))]
    return out


def waitfor_history(store: Path, since: str | None = None) -> tuple[dict[str, dict[str, list]], int]:
    """Each condition's met waits and timed-out waits in the store's logs: {condition: {"met": [(s,
    run)], "timed_out": [(s, run)]}}, and the number of logs read."""
    history: dict[str, dict[str, list]] = {}
    logs = store_game_logs(store, since)
    for run, log in logs:
        try:
            data = log.read_bytes()
        except OSError:
            continue
        if b"Input script: waitfor" not in data:
            continue
        for m in WAITFOR_RESULT.finditer(data):
            cond = m.group(1).decode("ascii", "replace")
            kind = "met" if m.group(2) == b"met" else "timed_out"
            history.setdefault(cond, {"met": [], "timed_out": []})[kind].append((float(m.group(3)), run))
    return history, len(logs)


def spec_waitfors(spec: Spec) -> list[tuple[str, float, int, list[str]]]:
    """(condition, timeout, script line, peers) of each waitfor in the spec's scripts, once per line that
    several peers share."""
    seen: dict[tuple[str, float, int], list[str]] = {}
    for n, p in spec.peers.items():
        for no, line in enumerate(p.script.splitlines(), 1):
            if m := WAITFOR_COMMAND.match(line):
                seen.setdefault((m.group(1), float(m.group(2)), no), []).append(n)
    return [(c, t, no, peers) for (c, t, no), peers in seen.items()]


def timeout_report(cfg: Config, specs: list[Spec], since: str | None = None) -> tuple[list[str], list[str], dict]:
    """C6: (the history table's lines, the warnings, the per-condition stats). A waitfor timeout is too
    long above max(3 x p95, max + 60 s) of its condition's met waits (with TIMEOUT_MIN_SAMPLES or more):
    a silent hang waits all of it before the timed-out line. It is too short below the longest met
    wait seen: a slow but healthy run (a loaded machine) times out there. Read only on the store."""
    t0 = time.monotonic()
    history, logs = waitfor_history(cfg.store, since)
    used: dict[str, set[float]] = {}
    for sp in specs:
        for cond, t, _, _ in spec_waitfors(sp):
            used.setdefault(cond, set()).add(t)
    stats: dict[str, dict] = {}
    for cond, h in history.items():
        met = [v for v, _ in h["met"]]
        st = {"met": len(met), "timed_out": len(h["timed_out"])}
        if met:
            top = max(h["met"])
            st.update(median=round(median(met), 1), p95=round(percentile(met, 95), 1), max=top[0], max_run=top[1])
        stats[cond] = st
    met_total = sum(st["met"] for st in stats.values())
    out_total = sum(st["timed_out"] for st in stats.values())
    lines = [f"waitfor history: {logs} game.log file(s) in {cfg.store / 'runs'}"
             + (f" (runs from {since} on)" if since else "")
             + f": {met_total} met, {out_total} timed out ({time.monotonic() - t0:.1f} s); pooled over every run,"
             " any load and any scene policy (older runs record neither; --since narrows it)",
             "", "| Condition | Met | Timed out | Median | p95 | Max | Max in | Spec timeouts |",
             "|---|---|---|---|---|---|---|---|"]
    for cond in sorted(set(stats) | set(used)):
        st = stats.get(cond, {"met": 0, "timed_out": 0})
        spec_t = ", ".join(f"{t:g}" for t in sorted(used.get(cond, ()))) or "-"
        if st.get("met"):
            lines.append(f"| {cond} | {st['met']} | {st['timed_out']} | {st['median']:g} s | {st['p95']:g} s "
                         f"| {st['max']:g} s | {st['max_run']} | {spec_t} |")
        else:
            lines.append(f"| {cond} | 0 | {st['timed_out']} | - | - | - | - | {spec_t} |")
    # One warning per condition, timeout and kind, naming every spec line that has it.
    found: dict[tuple[str, float, str], list[str]] = {}
    for sp in sorted(specs, key=lambda x: x.path.name):
        for cond, t, no, peers in spec_waitfors(sp):
            st = stats.get(cond)
            if not st or not st.get("met"):
                continue
            where = f"{sp.path.stem} ({', '.join(peers)} line {no})"
            if st["met"] >= TIMEOUT_MIN_SAMPLES and t > timeout_ceiling(st):
                found.setdefault((cond, t, "long"), []).append(where)
            if t < st["max"]:
                found.setdefault((cond, t, "short"), []).append(where)
    warnings = []
    for (cond, t, kind), where in sorted(found.items()):
        st = stats[cond]
        top, fits = timeout_ceiling(st), f"{math.ceil(st['max']):d}"
        fit = (f"it fits {fits}-{math.floor(top):d} s" if st["met"] >= TIMEOUT_MIN_SAMPLES and top >= st["max"]
               else f"it fits {fits} s or more")
        if kind == "long":
            why = (f"over max({TIMEOUT_P95_FACTOR:g} x p95 {st['p95']:g}, max {st['max']:g} + {TIMEOUT_MARGIN_S:g}) ="
                   f" {top:.0f} s of {st['met']} met waits: a silent hang waits {t:g} s for its timed-out line")
        else:
            why = (f"below the longest met wait seen, {st['max']:g} s ({st['max_run']}): a slow but healthy run times"
                   " out there")
        warnings.append(f"warning: `waitfor {cond} {t:g}` is {why}; {fit}; in {len(where)} spec line(s): "
                        + ", ".join(where))
    return lines, warnings, stats


def timeout_ceiling(st: dict) -> float:
    """C6: the longest waitfor timeout a condition's history allows: max(3 x p95, max + 60 s)."""
    return max(TIMEOUT_P95_FACTOR * st["p95"], st["max"] + TIMEOUT_MARGIN_S)


def cmd_timeouts(cfg: Config, a: argparse.Namespace) -> int:
    """C6: each condition's waitfor history in the store and the spec timeouts that do not fit it
    (advisory: exits 0). Reads only the store's game.log files; starts no game."""
    specs = all_specs(cfg)
    lines, warnings, _ = timeout_report(cfg, specs, getattr(a, "since", None))
    for line in lines:
        print(line)
    if warnings:
        print()
    for w in warnings:
        print(w)
    print(f"{len(specs)} spec(s), {len(warnings)} warning(s)")
    return 0


def last_pass_folders(cfg: Config) -> dict[str, str]:
    """Each case's newest passing run folder, from the run store's results.json files."""
    out: dict[str, str] = {}
    runs = cfg.store / "runs"
    for f in sorted(runs.glob("*/results.json")) if runs.is_dir() else []:
        try:
            for c in json.loads(f.read_text(encoding="utf-8")).get("cases", []):
                if c.get("status") == "pass" and c.get("folder"):
                    out[c["case"]] = c["folder"]
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            continue
    return out


def tolerated_lines(spec: Spec, folder: Path, most: int = 3) -> list[tuple[str, str]]:
    """The lines of a run folder's logs (a compared case's runs too) that are a default fatal line
    (DEFAULT_FATAL) for their peer but that the spec's `tolerate` takes out: (peer, line), at most `most`."""
    if not spec.tolerate or not folder.is_dir():
        return []
    tolerate = [re.compile(t) for t in spec.tolerate]
    found = []
    for log in sorted(run_game_logs(folder)):  # the peers' logs only, never their data, figures or cache
        rel = log.relative_to(folder).as_posix()
        peer = "S" if "/Online/session/" in f"/{rel}" else log.parent.name
        fatal = [re.compile(rx) for who, rx in DEFAULT_FATAL if who in ("*", peer)]
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if any(rx.search(line) for rx in fatal) and any(t.search(line) for t in tolerate):
                found.append((peer, line.strip()[:160]))
                if len(found) >= most:
                    return found
    return found


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="The Skylanders online forks' test runner.")
    p.add_argument("--repo", default=".", help="the game checkout (default: the current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("cases", nargs="*", help="case names or globs; none or `all` runs every case")
    t.add_argument("--stage", default=None, help="this stage's cases and every earlier stage's (a checkpoint's set)")
    t.add_argument("--max-games", type=int, default=0, help="most games at once in this runner (default: from the"
                   " cores); the machine's budget ([defaults] machine_games) holds across every runner on top")
    t.add_argument("--gate", action="store_true", help="only the manifest's gate cases (of the stages up to --stage,"
                   " or all), with their expected minutes before the run")
    t.add_argument("--per-build", action="store_true", help="with --gate: only the per-build gate cases (gate ="
                   " \"per-build\" in the manifest: once per build, in an idle slot); a daily `--gate` run leaves them"
                   " out and counts each met when its newest result on the same build snapshot is a pass")
    t.add_argument("--allow-restricted", action="store_true", help="start the games even when this runner has a"
                   " restricted token (an agent sandbox): their shader caches are unusable, timings not comparable")
    t.add_argument("--jobs", type=int, default=1, help="most cases at once (default 1: one after another); their"
                   " games count against --max-games, and a spec with exclusive = true runs alone")
    t.add_argument("--run-not-ready", action="store_true", help="run the cases that are not ready (ready = false,"
                   " a PLACEHOLDER) anyway, e.g. for the legs that exist; their result is graded as usual")
    t.add_argument("--no-breaker", action="store_true", help="start cases even when the circuit breaker of this"
                   " build tripped (3 cases of any runner stopped on one fatal signature); their failures still count")
    t.add_argument("--queue", default=None, metavar="FILE", help="also run the cases this file names (one name or"
                   " glob per line, # comments); it is re-read before each case starts, and entries added to it"
                   " run next (a lane's list: one command per lane)")
    t.add_argument("--remote", default=None, metavar="HOST:PORT", help="the remote agent on the second PC"
                   " (tools/skytest/remote_agent.py serve) that runs a spec's kind = \"remote\" peer")
    t.add_argument("--remote-manifest", default=None, metavar="FILE", help="with --remote: the RC's payload"
                   " record (package_check.py), which the agent verifies its install against")
    t.add_argument("--remote-token-file", default=None, metavar="FILE", help="with --remote: a file with the"
                   " agent's token (else it is asked for: the agent shows it on its console)")
    t.set_defaults(fn=cmd_test)
    ls = sub.add_parser("list")
    ls.add_argument("--gate", action="store_true", help="the gate cases and their expected minutes (from each"
                    " case's last finished run, else its spec's timeout)")
    ls.add_argument("--stage", default=None, help="with --gate: the gate cases of this stage and every earlier one")
    ls.set_defaults(fn=cmd_list)
    li = sub.add_parser("lint")
    li.add_argument("--timeouts", action="store_true", help="also warn of waitfor timeouts that do not fit their"
                    " condition's history in the store (as `skytest.py timeouts`)")
    li.add_argument("--since", default=None, metavar="STAMP", help="with --timeouts: only runs from this stamp on"
                    " (YYYYMMDD or YYYYMMDD-HHMMSS)")
    li.set_defaults(fn=cmd_lint)
    to = sub.add_parser("timeouts", help="each waitfor condition's history (median, p95, max) in the store's logs"
                        " and the spec timeouts that do not fit it")
    to.add_argument("--since", default=None, metavar="STAMP", help="only runs from this stamp on (YYYYMMDD or"
                    " YYYYMMDD-HHMMSS)")
    to.set_defaults(fn=cmd_timeouts)
    br = sub.add_parser("breaker", help="the circuit breaker's files in the machine's slot folder (each build's,"
                        " tripped or not); --reset deletes them")
    br.add_argument("--reset", action="store_true", help="delete every build's breaker file (cases start again)")
    br.set_defaults(fn=cmd_breaker)
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
