"""Live sessions (`skytest.py live`): the pure parts, unit-tested in tests/test_live_session.py.

A live session is a case whose peers' scripts end in `follow <file>` (the input script's live mode,
src/dev/input_script.h): the runner starts the games exactly as it starts a case, then each line a
tester sends is checked like a spec's script line, appended to that peer's file, and the game plays it
and logs `Input script: live <n>: <line>` and `Input script: live <n> done` (or `... refused: <why>`).

What is here: the prologues (the host's title, menus, figure, Host, hub; a joiner's join; a level),
the session's files and their JSON, splitting a send into lines, counting the numbers the game will
give them, finding a line's end in a log, the idle rule, and the spec skeleton made from what was sent.
The orchestration (the session process, run_case, the slots) is in skytest.py.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The game's live mode lines (input_script.cpp PlayLiveLine, RunLive).
LIVE_START = re.compile(r"Input script: live (\d+): ")
LIVE_DONE = re.compile(r"Input script: live (\d+) done\b")
LIVE_REFUSED = re.compile(r"Input script: live (\d+) refused: (.*)")
FOLLOWING = re.compile(r"Input script: following '")
LIVE_ENDED = "Input script: live mode ended"

# Each client's followed file, in its own run folder (the game's working directory), and S's, in its
# owner's folder: Host starts S with H's working directory, and passes --dev_server_follow on.
LIVE_FILE = "live.txt"
SERVER_LIVE_FILE = "live-S.txt"
SERVER_FOLLOW_SETTING = "dev_server_follow"

DEFAULT_IDLE_MINUTES = 20.0
DEFAULT_SEND_TIMEOUT = 120.0
# Until every peer follows its file: boot, menus, hub and a level take minutes on a loaded PC.
DEFAULT_START_TIMEOUT = 1500.0
# A stop: `quit` to every live file, then this long for the games to end before they are killed.
STOP_GRACE_S = 30.0
# The session's own case timeout: far above any session (the idle stop ends it first).
SESSION_TIMEOUT_S = 12 * 3600

# Each client's figure in the prologues (the ones the existing specs use).
FIGURES = {"H": "Roller Brawl.dump", "D": "Scorp.dump", "E": "Roller Brawl.dump", "F": "Scorp.dump"}
# H's own settings in a live session: dialogue skipped for the route helpers, and S without the Hello
# gate on loopback, so a joiner joins by address:port ({join}) as the specs do.
HOST_ARGS = ["--nav_skip_dialogue=true", "--dev_hello_ungated_loopback=true"]

SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}")
CASE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,60}")


class LiveError(Exception):
    """A live command that cannot go on (its reason)."""


# --- the prologues ---------------------------------------------------------------------------------


def host_prologue(level: str | None, joining: int) -> list[str]:
    """H's way to its hub as host (the specs' prologue, e.g. hnp-lever-gate), then the level once the
    `joining` clients are in: online_mark_level before `level`, so online_level_after_mark holds only
    once the new level is up and its barrier released."""
    lines = [
        "waitfor title_ready 300",
        "online_host 0",
        "waitfor online_connected 120",
        "tap start",
        "waitfor menu_ready 60",
        "tap a",
        "waitfor menu_ready 60",
        "tap a",
        "waitfor front_end_left 120",
        f"place 0 {{figures}}/{FIGURES['H']}",
        "waitfor online_server_hub 300",
        "waitfor online_barrier 300",
        "wait_free 120",
    ]
    if level:
        if joining:
            lines.append(f"online_wait_peers {1 + joining} 600")
        lines += [
            "online_mark_level",
            f"level {level}",
            "waitfor online_level_after_mark 600",
            "waitfor online_barrier 600",
            "wait_free 120",
        ]
    return lines


def joiner_prologue(peer: str, level: str | None, late: bool) -> list[str]:
    """A client's join from its title by address:port, then H's level when there is one. A late joiner
    stops at its title with its figure placed: its join is sent live (`online_join {join}`)."""
    lines = ["waitfor title_ready 300", f"place 0 {{figures}}/{FIGURES.get(peer, FIGURES['D'])}"]
    if late:
        return lines
    lines += ["online_join {join}", "waitfor online_barrier 600", "wait_free 120"]
    if level:
        lines += ["waitfor online_next_level 900", "waitfor online_barrier 600", "wait_free 120"]
    return lines


def prologues(clients: list[str], level: str | None, late: set[str]) -> dict[str, list[str]]:
    joining = [c for c in clients if c != "H" and c not in late]
    return {c: host_prologue(level, len(joining)) if c == "H" else joiner_prologue(c, level, c in late)
            for c in clients}


def toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def toml_list(items: list[str]) -> str:
    return "[" + ", ".join(toml_string(i) for i in items) + "]"


def session_spec_text(name: str, layout: str, clients: list[str], scripts: dict[str, list[str]],
                      args: dict[str, list[str]], show_server: bool, description: str) -> str:
    """The session's own spec (loaded with load_spec, run with run_case): each client's prologue, then
    `follow live.txt` and `quit` (reached after a follow_end line). No fail fast: a fatal line is said by
    `live send` and `live list`, and the session stays for the tester to look at."""
    out = [
        f"name = {toml_string(name)}",
        f"layout = {toml_string(layout)}",
        'stage = "live"',
        'server = "host"',
        f"timeout = {SESSION_TIMEOUT_S}",
        'scenes = "skip"',
        "fail_fast = false",
        f"show_server_window = {'true' if show_server else 'false'}",
        f"description = {toml_string(description)}",
        "",
    ]
    for c in clients:
        script = "\n".join([*scripts[c], f"follow {LIVE_FILE}", "quit"])
        out += [f"[peer.{c}]", f"args = {toml_list(args.get(c, []))}", f'script = """\n{script}\n"""', ""]
    return "\n".join(out)


# --- the session's files ---------------------------------------------------------------------------


@dataclass
class SessionInfo:
    """session.json: what `live start` decided (written once, before the session process starts)."""
    name: str
    layout: str
    level: str
    clients: list[str]
    late: list[str]
    hide_server: bool
    server_live: bool
    idle_minutes: float
    prologue: dict[str, list[str]]
    args: dict[str, list[str]]
    worktree: str
    created: float = field(default_factory=time.time)


@dataclass
class SessionState:
    """state.json: written by the session process as things happen."""
    state: str = "starting"      # starting, waiting for slots, booting, live, stopping, ended, failed
    pid: int = 0                 # the session process
    run: str = ""                # the run folder
    logs: dict[str, str] = field(default_factory=dict)        # peer -> its log
    live_files: dict[str, str] = field(default_factory=dict)  # peer -> its followed file
    following: list[str] = field(default_factory=list)        # the peers that follow their file now
    problems: list[str] = field(default_factory=list)         # fatal lines, exits (peer: what)
    join: str = ""               # 127.0.0.1:<port>, for {join}
    note: str = ""               # what the session does now, or why it ended
    updated: float = 0.0
    ended: float = 0.0


def session_dir(store: Path, name: str) -> Path:
    if not SESSION_NAME.fullmatch(name) or ".." in name:
        raise LiveError(f"session name {name!r}")
    return store / "live" / name


def write_json(path: Path, data: dict) -> None:
    """Replaced whole (a reader never sees half a file): written beside it, then moved over it."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a reader has it open this instant (Windows)
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.05 * (attempt + 1))
        except FileNotFoundError:
            return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_info(folder: Path) -> SessionInfo:
    d = read_json(folder / "session.json")
    if not d:
        raise LiveError(f"no live session at {folder}")
    return SessionInfo(**d)


def load_state(folder: Path) -> SessionState:
    d = read_json(folder / "state.json")
    known = {k: v for k, v in d.items() if k in SessionState.__dataclass_fields__}
    return SessionState(**known)


def save_state(folder: Path, st: SessionState) -> None:
    st.updated = time.time()
    write_json(folder / "state.json", asdict(st))


# --- sending ---------------------------------------------------------------------------------------


def split_send(texts: list[str]) -> list[str]:
    """The lines of one send: each argument, split at ';' and line breaks as the game's script splits;
    blank pieces dropped, outer whitespace trimmed. A piece that is only a comment stays (the game
    numbers it, as every complete line)."""
    out: list[str] = []
    for t in texts:
        for piece in re.split(r"[;\r\n]", t):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def fill_placeholders(line: str, figures: str, join: str) -> str:
    """{figures} and {join} as in a spec's script."""
    if "{join}" in line and not join:
        raise LiveError("{join} has no value in this session (no hosted server port known yet)")
    return line.replace("{figures}", figures).replace("{join}", join)


def lines_in(data: bytes) -> int:
    """The complete lines a followed file holds: the number the game gave its last line (it counts every
    line that ends in a newline, live_lines.h)."""
    return data.count(b"\n")


def append_lines(path: Path, lines: list[str]) -> list[int]:
    """Appends the lines to the followed file in one write and returns the numbers the game gives them.
    A file whose last line has no newline (never written so by the runner) gets one first, so that piece
    is not glued to the first new line. The caller holds the peer's send lock."""
    data = path.read_bytes() if path.is_file() else b""
    prefix = b"\n" if data and not data.endswith(b"\n") else b""
    first = lines_in(data) + (1 if prefix else 0) + 1
    payload = prefix + "".join(line + "\n" for line in lines).encode("utf-8")
    with open(path, "ab") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    return list(range(first, first + len(lines)))


@dataclass
class LineOutcome:
    number: int
    started: bool = False
    done: bool = False
    refused: str = ""   # why, when the game refused it

    @property
    def over(self) -> bool:
        return self.done or bool(self.refused)


def match_outcomes(lines: list[str], outcomes: dict[int, LineOutcome]) -> None:
    """Notes each wanted line's start, end or refusal in a peer's new log lines."""
    for line in lines:
        if m := LIVE_DONE.search(line):
            if (o := outcomes.get(int(m.group(1)))) is not None:
                o.done = True
        elif m := LIVE_REFUSED.search(line):
            if (o := outcomes.get(int(m.group(1)))) is not None:
                o.refused = m.group(2).strip() or "refused"
        elif m := LIVE_START.search(line):
            if (o := outcomes.get(int(m.group(1)))) is not None:
                o.started = True


def all_over(outcomes: dict[int, LineOutcome]) -> bool:
    """Every line of the send is done or refused. A blank or comment line logs nothing: the game takes
    it silently, so the caller leaves those out of `outcomes`."""
    return all(o.over for o in outcomes.values())


def is_silent(line: str) -> bool:
    """A line the game takes without a log line (only a comment)."""
    return not line.split("#", 1)[0].strip()


def idle_seconds(last_activity: float, now: float) -> float:
    return max(0.0, now - last_activity)


def idle_due(last_activity: float, now: float, idle_minutes: float) -> bool:
    """The idle stop: no send (and no start) for idle_minutes. 0 or less turns it off."""
    return idle_minutes > 0 and idle_seconds(last_activity, now) >= idle_minutes * 60


# --- the spec skeleton -----------------------------------------------------------------------------


@dataclass
class Sent:
    """One line of sends.jsonl."""
    peer: str
    number: int
    line: str
    t: float
    seq: int = 0


def read_sends(path: Path) -> list[Sent]:
    out: list[Sent] = []
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            d = json.loads(raw)
            out.append(Sent(**{k: v for k, v in d.items() if k in Sent.__dataclass_fields__}))
    return out


def skeleton_spec(case: str, info: SessionInfo, sends: list[Sent], first: int | None = None,
                  last: int | None = None, source: str = "") -> str:
    """A spec file tests/runtime/<case>.toml from the session: each client's prologue, then the lines
    sent to it (live numbers `first`..`last` of each peer, all by default), then `quit`. Only the waits
    the tester sent; nothing else is added. Lines sent to S stay as comments (S runs no case script:
    its client sends them as online_server_* commands). expect and forbid are left for the tester."""
    def chosen(s: Sent) -> bool:
        return (first is None or s.number >= first) and (last is None or s.number <= last)

    by_peer: dict[str, list[str]] = {}
    for s in sends:
        if chosen(s):
            by_peer.setdefault(s.peer, []).append(s.line.replace("{figures}", "{figures}"))
    out = [
        f"# Made by `skytest.py live spec` from the live session {info.name}{(' (' + source + ')') if source else ''}:",
        "# each client's prologue, then the lines sent to it, in order. Fill in expect and forbid and a",
        "# description, then run it once (`skytest.py test <case>`) before it is used as proof.",
        f"name = {toml_string(case)}",
        f"layout = {toml_string(info.layout)}",
        'stage = "live"',
        'server = "host"',
        "timeout = 900",
        'scenes = "skip"',
    ]
    if info.hide_server:
        out.append("show_server_window = false")
    out += [f"description = {toml_string('made from live session ' + info.name)}", ""]
    for c in info.clients:
        body = [*info.prologue.get(c, []), *by_peer.get(c, [])]
        if not body or body[-1].strip() != "quit":
            body.append("quit")
        out += [f"[peer.{c}]", f"args = {toml_list(info.args.get(c, []))}",
                "forbid = ['Input script: waitfor .*timed out']", "expect = []",
                f'script = """\n' + "\n".join(body) + '\n"""', ""]
    if by_peer.get("S"):
        out += ["# Sent to S live (S runs no case script; send them from a client as online_server_* commands):"]
        out += [f"#   {line}" for line in by_peer["S"]]
        out.append("")
    return "\n".join(out)
