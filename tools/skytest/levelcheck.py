"""The level check's graders: a run folder's logs against the owner's definition of working (two players
in a lobby, the host's save slot, cutscenes synced, enemies shared, pickups and loot shared, the level
completable). Every grader reads only lines the games already write; none is written for one level.

Every verdict says what it rests on: its counts and one example line with its file and line number. A
grader with nothing to grade says NOT TESTED (no data, not reached, switched off in this run), never PASS.
A finding is a difference between two games with the lines that show it (S's and the client's), or a
line on one game and the absence on the other, said so.

The run folder's games: S is H/data/Online/session/game.log (a hosted server) or S/game.log; every other
top-level folder with a game.log is a client (H, D). A client's peer number is its `connected as peer
<n>` line; its time in a level generation runs from its `level <g> ...: load` line (or its connect into
<g>) to its next load, its `online_end` or its last line (client_windows).

    python -I tools/skytest/skytest.py levelcheck --grade <run folder>
Standard library only.
"""
from __future__ import annotations

import importlib.util
import sys
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PASS, FAIL, NOT_TESTED = "PASS", "FAIL", "NOT TESTED"
INCOMPLETE = "INCOMPLETE"   # completable only: the level pass did not cover its plan (never a pass)
# completable only: the pass reached the end, but by teleport, past steps a player must do on the level's way
# (levelpass_holds.required_gates: a Spark Lock, a battle gate, a key gate) that no line shows done. Never a
# bare pass: the page names each step.
BY_TELEPORT = "BY TELEPORT"

STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]")
# An enemy row's key: placed `a<id>.<hash>.<hash>@<gen>`, a run-time spawn `s<hash>.<hash>#<n>/<m>@<gen>`.
KEY = r"([as][0-9a-f]+\.[0-9a-f]+(?:\.[0-9a-f]+|#\d+/\d+)@(\d+))"
CONNECTED = re.compile(r"Online: connected as peer (\d+) \(server tick \d+, level (\d+) (\S+)\)")
LEVEL_LOAD = re.compile(r"Online: level (\d+) (\S+): load")
SESSION_END = re.compile(r"Input script: online_end|online_end: ")
# The level's end as the client plays it (the results screen or the level-complete message): from here the
# level is over for the player, the game pauses, and nothing S does in that level is graded any more
# (ww1 20261119-001659: S woke the final wave 4.9 s before the results screen paused both clients).
LEVEL_END_HERE = re.compile(r"Online: level end \S+ (?:from the server |to \S+ )?played here")

# How long a client may take to attach an enemy row (the bind's own grace is a few seconds), and to show a
# death S sent: an S event closer than this to the end of the client's time in the level is not graded.
BIND_GRACE = timedelta(seconds=10)
DEATH_GRACE = timedelta(seconds=5)


# --- the logs ------------------------------------------------------------------------------------------


@dataclass
class Line:
    log: str          # the file, relative to the run folder
    no: int           # 1-based
    text: str         # from `Online:` / `Input script:` on (the stamp and thread cut)
    t: datetime | None

    def cite(self) -> str:
        return f"{self.log}:{self.no}: {self.text[:220]}"


@dataclass
class Log:
    who: str
    path: str
    lines: list[str]
    # A log the game rotated (`game.<n>.log` older, `game.log` the newest: S's log in a long run, batch 12 case 7,
    # ww1 20261119-124636, rotated at 12:51:56): its files' lines in time order, each file's (path, first index)
    # here, so a line is cited in its own file. Empty: one file, `path`.
    parts: list[tuple[str, int]] = field(default_factory=list)

    def find(self, rx: re.Pattern) -> list[tuple[Line, re.Match]]:
        out = []
        for i, raw in enumerate(self.lines):
            m = rx.search(raw)
            if m:
                out.append((self.line(i), m))
        return out

    def line(self, i: int) -> Line:
        raw = self.lines[i]
        k = raw.find("Online: ")
        if k < 0:
            k = raw.find("Input script: ")
        if k < 0:
            k = raw.find("] ", 26) + 2 if raw.startswith("[") else 0
        path, first = self.path, 0
        for part, start in self.parts:
            if start <= i:
                path, first = part, start
        return Line(path, i - first + 1, raw[k:].rstrip(), stamp(raw))

    def last_time(self) -> datetime | None:
        for raw in reversed(self.lines):
            t = stamp(raw)
            if t is not None:
                return t
        return None


def stamp(raw: str) -> datetime | None:
    m = STAMP.match(raw)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f") if m else None


def read_log(run: Path, rel: str, who: str) -> Log | None:
    """A game's log with the files the game rotated before it (`game.<n>.log`, the highest n the oldest), in time
    order (Log.parts)."""
    p = run / rel
    if not p.is_file():
        return None
    older = sorted((q for q in p.parent.glob(f"{p.stem}.*{p.suffix}") if q.stem[len(p.stem) + 1:].isdigit()),
                   key=lambda q: -int(q.stem[len(p.stem) + 1:]))
    lines: list[str] = []
    parts: list[tuple[str, int]] = []
    base = rel.replace("\\", "/").rsplit("/", 1)[0] + "/" if "/" in rel.replace("\\", "/") else ""
    for q in older + [p]:
        parts.append((base + q.name, len(lines)))
        lines += q.read_text(encoding="utf-8", errors="replace").splitlines()
    return Log(who, rel.replace("\\", "/"), lines, parts if older else [])


@dataclass
class Run:
    folder: Path
    server: Log | None
    clients: dict[str, Log]           # H, D, ... by folder name
    result: dict = field(default_factory=dict)


def load_run(folder: Path) -> Run:
    server = read_log(folder, "H/data/Online/session/game.log", "S") or read_log(folder, "S/game.log", "S")
    clients = {}
    for d in sorted(p for p in folder.iterdir() if p.is_dir() and p.name != "S"):
        log = read_log(folder, f"{d.name}/game.log", d.name)
        if log is not None:
            clients[d.name] = log
    try:
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {}
    return Run(folder, server, clients, result if isinstance(result, dict) else {})


# --- a client's time in each level ---------------------------------------------------------------------


@dataclass
class Window:
    gen: int
    level: str
    start: datetime
    end: datetime
    joined: Line            # the line its time in the level starts at


def client_windows(log: Log) -> dict[int, Window]:
    """The client's time in each level generation (see the module's text)."""
    marks: list[tuple[datetime, int, str, Line]] = []
    end: datetime | None = None
    level_ends: list[datetime] = []
    for i, raw in enumerate(log.lines):
        t = stamp(raw)
        if t is None:
            continue
        if LEVEL_END_HERE.search(raw):
            level_ends.append(t)
            continue
        m = CONNECTED.search(raw)
        if m and int(m.group(2)) > 0:
            marks.append((t, int(m.group(2)), m.group(3), log.line(i)))
            continue
        m = LEVEL_LOAD.search(raw)
        if m:
            marks.append((t, int(m.group(1)), m.group(2), log.line(i)))
            continue
        if end is None and SESSION_END.search(raw) and marks:
            end = t
    last = end or log.last_time()
    out: dict[int, Window] = {}
    for n, (t, gen, level, line) in enumerate(marks):
        stop = marks[n + 1][0] if n + 1 < len(marks) else last
        if stop is not None:
            stop = min([stop] + [e for e in level_ends if t <= e <= stop])
        if gen not in out and stop is not None:
            out[gen] = Window(gen, level, t, min(stop, last) if last else stop, line)
    return out


def peer_numbers(run: Run) -> dict[int, str]:
    """Peer number -> client folder, from each client's `connected as peer <n>` line."""
    out = {}
    for who, log in run.clients.items():
        for line, m in log.find(CONNECTED):
            out[int(m.group(1))] = who
            break
    return out


# --- a verdict -----------------------------------------------------------------------------------------


@dataclass
class Finding:
    what: str
    lines: list[str]           # cited lines (file:line: text) or a said absence


@dataclass
class Verdict:
    name: str
    status: str
    counts: str
    example: str = ""          # one cited line the verdict rests on
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def page_lines(self) -> list[str]:
        out = [f"  {self.name:<12} {self.status:<10} {self.counts}"]
        if self.findings:
            f = self.findings[0]
            out.append(f"  {'':<12} {'':<10} e.g. {f.what}")
            out += [f"  {'':<23}   {x}" for x in f.lines]
        elif self.example:
            out.append(f"  {'':<12} {'':<10} e.g. {self.example}")
        out += [f"  {'':<23} {n}" for n in self.notes]
        return out


def absent(log: Log, what: str) -> str:
    return f"{log.path}: no line {what} ({len(log.lines)} lines read)"


# --- lobby and save slot -------------------------------------------------------------------------------

HOST_SLOT = re.compile(r"Online: host: the server (?:continues|started) .*?slot (\d+)")
OWNER_PICK = re.compile(r"Online: host: the server follows the owner's pick \((.+)\)")
TITLE_ENTRY = re.compile(r"Online: title entry: (.+)$")
HUB = re.compile(r"^(town|hub)", re.I)


def grade_slot(run: Run) -> Verdict:
    hits = []
    for log in [run.server, *run.clients.values()]:
        if log is None:
            continue
        hits += [(line, m.group(0)) for line, m in log.find(TITLE_ENTRY)]
        hits += [(line, m.group(0)) for line, m in log.find(HOST_SLOT)]
        hits += [(line, m.group(0)) for line, m in log.find(OWNER_PICK)]
    if not hits:
        return Verdict("save slot", NOT_TESTED, "no `title entry:` or `host: the server ... slot` line in any log")
    slot = next((line for line, _ in hits if "follows the owner's pick" in line.text),
                next((line for line, _ in hits if "title entry" in line.text and "slot" in line.text), hits[0][0]))
    what = slot.text.split(": ", 1)[1] if ": " in slot.text else slot.text
    return Verdict("save slot", PASS, f"{what} ({len(hits)} line(s) on the slot in the run's logs)", slot.cite())


def grade_lobby(run: Run) -> Verdict:
    joins = {}
    for who, log in run.clients.items():
        found = log.find(CONNECTED)
        if found:
            joins[who] = found[0]
    if not joins:
        return Verdict("lobby", NOT_TESTED, "no client logged `connected as peer`")
    late = {w: (line, m) for w, (line, m) in joins.items() if int(m.group(2)) > 0 and not any(
        HUB.match(part) for part in m.group(3).split("/"))}
    counts = ", ".join(f"{w} peer {m.group(1)} at level {m.group(2)} {m.group(3).split('/')[-1]}"
                       for w, (_, m) in joins.items())
    if len(joins) < 2:
        return Verdict("lobby", NOT_TESTED, f"one client in this run ({counts})", next(iter(joins.values()))[0].cite())
    if late:
        w, (line, m) = next(iter(late.items()))
        return Verdict("lobby", NOT_TESTED, f"{w} joined late, in {m.group(3).split('/')[-1]} (the lobby join is not "
                       f"tested in this run): {counts}", line.cite())
    return Verdict("lobby", PASS, f"both joined before a level: {counts}", next(iter(joins.values()))[0].cite())


# --- enemies shared ------------------------------------------------------------------------------------

# S's enemies, every kind it logs (`enemy placed`, `enemy run-time spawn`, and the encounter path's `enemy spawn
# <key> as <Name> ... (S)`: tutorial_a's 28 ECI_Manager actors and its Feeder were missing from this row before,
# 20261119-033102), and each one's death: `dead by <peer|none> (S[, placed; attacker <Name#id|->])`. The attacker is
# S's actor name: `-` is no attacker at all; no field (the encounter path) is an attacker not recorded, and a
# player's kill there reads `dead by none (S)` too (tutorial_a: a death 4 ms after H's press started on S). The
# puppet name is no player number (S's puppet slots swap at a level change: tutorial_b 20261119-033620, `puppet 2
# for peer 1` in generation 2), so a kill is the player of `dead by <peer>`.
S_PLACED = re.compile(r"Online: enemy (placed|run-time spawn|spawn) " + KEY + r" as (\S+)")
S_DEAD = re.compile(r"Online: enemy " + KEY + r" dead by (\S+) \((.*)\)")
S_NO_ATTACKER = re.compile(r"Online: enemy " + KEY + r" died with no attacker at ")
S_STREAMED = re.compile(r"Online: enemy " + KEY + r" streamed out on the server")
S_ATTACKER = re.compile(r"attacker (\S+)")
C_ENEMY = re.compile(r"Online: enemy (spawn|replica|skipped|life|dead|retired alive|retired|bind) " + KEY + r"(.*)")
# An enemy that dies on S with no attacker this soon after S made it never got to the players: the tutorial_a
# ship greebles fell and died 0.87 to 2.8 s after their spawn, two of them before either client bound them.
DIED_SOON = timedelta(seconds=10)
KINDS = {"placed": "placed", "run-time spawn": "run-time", "spawn": "encounter"}


@dataclass
class EnemyKey:
    key: str
    gen: int
    name: str
    placed: Line
    kind: str = "placed"
    dead: Line | None = None
    by: str = ""                   # dead by <peer|none>
    attacker: str | None = None    # S's attacker field: a name, "-" (none), None (not recorded)
    no_attacker: Line | None = None
    streamed: Line | None = None


def server_enemies(log: Log) -> dict[str, EnemyKey]:
    keys: dict[str, EnemyKey] = {}
    for line, m in log.find(S_PLACED):
        if m.group(1) == "spawn" and not line.text.endswith("(S)"):
            continue  # the encounter path's line is S's own only with its (S)
        if m.group(2) not in keys:
            keys[m.group(2)] = EnemyKey(m.group(2), int(m.group(3)), m.group(4), line, KINDS[m.group(1)])
    for line, m in log.find(S_DEAD):
        k = keys.get(m.group(1))
        if k and k.dead is None:
            k.dead, k.by = line, m.group(3)
            a = S_ATTACKER.search(m.group(4))
            k.attacker = a.group(1) if a else None
    for line, m in log.find(S_NO_ATTACKER):
        k = keys.get(m.group(1))
        if k and k.no_attacker is None:
            k.no_attacker = line
    for line, m in log.find(S_STREAMED):
        k = keys.get(m.group(1))
        if k and k.streamed is None:
            k.streamed = line
    return keys


@dataclass
class ClientEnemy:
    bound: Line | None = None      # `spawn <key>` or `bind <key> (resolved)`: a copy here tied to S's row
    waits: Line | None = None      # `bind <key> by authored id ... waits (asleep here ...)`
    pending: Line | None = None    # `bind <key> (pending past grace: missing)`
    seen_dead: Line | None = None  # `life <key> alive -> dead` / `dead <key> by`: the death shown on its copy
    skipped: Line | None = None    # `skipped <key> (dead before bind)`: S's death came before any bind here
    retired_alive: Line | None = None
    retired_streamed: Line | None = None  # `retired <key> (streamed out on the server)`: S slept it, its row went
    last: Line | None = None

    def status(self) -> str:
        if self.bound:
            return "bound" + (", saw it die" if self.seen_dead else "")
        if self.waits:
            return "waiting asleep" + (", then dead before bind" if self.skipped else "")
        if self.pending:
            return "looked for, never found"
        return "dead before bind (never existed here)" if self.skipped else "no line"


def client_enemies(log: Log) -> dict[str, ClientEnemy]:
    out: dict[str, ClientEnemy] = {}
    for line, m in log.find(C_ENEMY):
        kind, key, rest = m.group(1), m.group(2), m.group(4)
        c = out.setdefault(key, ClientEnemy())
        c.last = line
        if kind == "spawn" or (kind == "bind" and "(resolved)" in rest):
            c.bound = c.bound or line
        elif kind == "bind" and "pending past grace" in rest:
            c.pending = c.pending or line
        elif kind == "bind" and " waits (asleep here" in rest:
            c.waits = c.waits or line
        elif kind == "skipped" and "dead before bind" in rest:
            c.skipped = c.skipped or line
        elif (kind == "life" and "-> dead" in rest) or kind == "dead":
            c.seen_dead = c.seen_dead or line
        elif kind == "retired alive":
            c.retired_alive = c.retired_alive or line
        elif kind == "retired" and "streamed out on the server" in rest:
            c.retired_streamed = c.retired_streamed or line
    return out


def slept_before_made(k: "EnemyKey", c: "ClientEnemy | None") -> bool:
    """A run-time spawn S put to sleep (streamed out, its row retired) before this client made a copy: it never
    died, and the client's only line for it is that retirement (never bound, waited for, looked for or killed
    here). Nobody could see it (batch 11 tutorial_b 20261119-061110: CrashSP_04_Spawned1 slept on S 3.04 s after
    its spawn, far from both players, and both clients dropped it before their 3 s wait for a copy ran out)."""
    return (k.kind == "run-time" and k.dead is None and k.streamed is not None and c is not None
            and c.retired_streamed is not None and not (c.bound or c.waits or c.pending or c.skipped or c.seen_dead))


def grade_enemies(run: Run, data: LevelData | None = None) -> Verdict:
    """Every enemy S had, per client: bound there, waiting asleep (its copy asleep far from that player: no
    fault by itself), or never there; and each death S logged: seen on a bound copy, or dead before bind. PASS
    only when every enemy a client was near enough to see (not asleep there) was bound there, every death of a
    bound enemy arrived, no enemy existed on no client, and none died on S with no attacker right after its
    spawn."""
    if run.server is None:
        return Verdict("enemies", NOT_TESTED, "no server log in this run folder")
    keys = server_enemies(run.server)
    if not keys:
        return Verdict("enemies", NOT_TESTED, "S logged no `enemy placed`, `enemy run-time spawn` or `enemy spawn ... "
                       "(S)` line")
    peers = peer_numbers(run)
    order = sorted(run.clients, key=lambda w: (w != "H", w))  # H first, as the page names the players
    # The level's holds (levelpass_holds, the generator's data): scenery and holds not released here are set
    # apart, listed, never graded.
    scenery: list[str] = []
    unreleased: list[str] = []
    for key, k in list(keys.items()):
        hold = hold_of(data, key, k.name) if k.kind == "placed" else None
        if hold is None:
            continue
        if hold.ambient:
            scenery.append(k.name.split("#")[0])
            del keys[key]
        elif released_here(run, hold, bool(k.dead and k.by.isdigit())) is None:
            unreleased.append(f"{k.name.split('#')[0]} ({hold.how}; " + "; or ".join(r.words() for r in hold.releases)
                              + ")")
            del keys[key]
    held_notes = ([f"scenery, not counted (the level never releases them): {', '.join(sorted(set(scenery)))}"]
                  if scenery else []) + [f"held, not released in this run: {u}" for u in unreleased]
    if not keys:
        return Verdict("enemies", NOT_TESTED, "every enemy S had is scenery or held and not released in this run",
                       notes=held_notes)
    kinds = {kd: sum(1 for k in keys.values() if k.kind == kd) for kd in ("placed", "encounter", "run-time")}
    dead = [k for k in keys.values() if k.dead]
    by_player = {who: sum(1 for k in dead if k.by.isdigit() and peers.get(int(k.by)) == who)
                 for who in order}
    no_attacker = [k for k in dead if k.attacker == "-"]
    unrecorded = [k for k in dead if k.attacker is None and not k.by.isdigit()]
    # Killed by an entity that is no player (`dead by none (S, placed; attacker (null)#3457)`: ww1 20261119-051557,
    # the seventh death the counts missed): named, so the totals add up.
    by_other = [k for k in dead if k.attacker not in (None, "-") and not k.by.isdigit()]
    counts = [f"S {len(keys)} ({', '.join(f'{n} {kd}' for kd, n in kinds.items())}), {len(dead)} died on S (by a "
              f"player: {', '.join(f'{w} {n}' for w, n in by_player.items()) or '-'}; by another entity "
              f"{len(by_other)}{' (' + ', '.join(sorted({k.attacker for k in by_other})[:4]) + ')' if by_other else ''}; "
              f"no attacker {len(no_attacker)}; attacker not recorded {len(unrecorded)})"]
    findings: list[Finding] = []
    expected: dict[str, list[str]] = {}       # key -> the clients that should have had it
    slept: dict[str, list[str]] = {}          # key -> the clients it went to sleep on S before they made it
    there: dict[str, dict[str, ClientEnemy | None]] = {}
    for who in order:
        log = run.clients[who]
        wins = client_windows(log)
        seen = client_enemies(log)
        n_exp = n_bound = n_wait = n_none = 0
        n_deaths = n_seen = n_before = n_asleep_quiet = 0
        for k in keys.values():
            w = wins.get(k.gen)
            if w is None:
                continue
            gone = k.dead or k.streamed
            if k.placed.t is None or k.placed.t > w.end - BIND_GRACE or (gone and gone.t and gone.t < w.start):
                continue  # made too late for this client, or gone before it came
            c = seen.get(k.key)
            if slept_before_made(k, c):
                slept.setdefault(k.key, []).append(who)
                continue  # listed apart, never graded (slept_before_made)
            n_exp += 1
            expected.setdefault(k.key, []).append(who)
            there.setdefault(k.key, {})[who] = c
            died_here = bool(k.dead and k.dead.t and w.start <= k.dead.t <= w.end - DEATH_GRACE)
            n_deaths += died_here
            if c is not None and c.bound:
                n_bound += 1
                if died_here:
                    if c.seen_dead:
                        n_seen += 1
                    else:
                        findings.append(Finding(f"{who} never saw {k.key} ({k.name}) die: dead on S, alive on its "
                                                f"bound copy here", [k.dead.cite(), (c.retired_alive or c.last).cite()]))
                elif c.seen_dead and k.dead is None and k.streamed is None:
                    findings.append(Finding(f"{who}: {k.key} ({k.name}) dead here only, S never logged its death", [
                        c.seen_dead.cite(), absent(run.server, f"`enemy {k.key} dead by`")]))
                continue
            if c is not None and c.waits:
                n_wait += 1
                if died_here:
                    if c.skipped:
                        n_before += 1
                    else:
                        n_asleep_quiet += 1
                continue
            if c is not None and c.pending:
                findings.append(Finding(f"{who} never found {k.key} ({k.name}): S had it, {who} looked for it "
                                        f"and never bound it", [k.placed.cite(), c.pending.cite()]))
                continue
            if c is not None and c.skipped:
                n_none += 1
                n_before += died_here
                continue
            findings.append(Finding(f"{who} never attached {k.key} ({k.name})", [
                k.placed.cite(), absent(log, f"for {k.key}")]))
        if wins:
            counts.append(f"{who}: bound {n_bound} of {n_exp}, waiting asleep {n_wait} (its copy asleep, far from "
                          f"this player: not a fault by itself), never existed here {n_none}; deaths {n_deaths}: "
                          f"{n_seen} seen on a bound copy, {n_before} dead before bind"
                          + (f", {n_asleep_quiet} with no word on its asleep copy" if n_asleep_quiet else ""))
        else:
            counts.append(f"{who}: in no level")

    def where(key: str) -> str:
        return ", ".join(f"{w} {c.status() if c else 'no line'}" for w, c in there.get(key, {}).items()) or \
            "no client was in its level"

    soon = [k for k in no_attacker if k.placed.t and k.dead.t and k.dead.t - k.placed.t <= DIED_SOON]
    if soon:
        lines = []
        for k in soon:
            lines += [k.dead.cite() + f" ({(k.dead.t - k.placed.t).total_seconds():.2f} s after its spawn)",
                      *([k.no_attacker.cite()] if k.no_attacker else []), f"    on the clients: {where(k.key)}"]
        nowhere = sum(1 for k in soon if all(c is None or not (c.bound or c.waits) for c in there.get(k.key, {}).values()))
        findings.insert(0, Finding(f"{len(soon)} enem{'y' if len(soon) == 1 else 'ies'} died on S with no attacker "
                                   f"within {DIED_SOON.seconds} s of {'its' if len(soon) == 1 else 'their'} spawn "
                                   f"({nowhere} reached no client)", lines))
    soon_keys = {k.key for k in soon}
    # Every client that should have had it logged it and none had a copy (a key with no line on a client is that
    # client's `never attached` above).
    nowhere_keys = [key for key, ws in expected.items() if key not in soon_keys and
                    all(there[key].get(w) is not None and not (there[key][w].bound or there[key][w].waits or
                                                               there[key][w].pending) for w in ws)]
    if nowhere_keys:
        lines = []
        for key in nowhere_keys:
            k = keys[key]
            lines += [k.placed.cite(), *([k.dead.cite()] if k.dead else []), f"    on the clients: {where(key)}"]
        findings.insert(0, Finding(f"existed on no client: {', '.join(f'{key} ({keys[key].name})' for key in nowhere_keys)}",
                                   lines))
    first = next(iter(keys.values())).placed.cite()
    fights, fight_findings = fight_findings_of(run, by_player, len(unrecorded), data)
    counts.append(fights)
    # W32 (c): the fight by D (D's own fight_area lines): its kills on S, and a fight that killed nothing by D.
    d_log = run.clients.get("D")
    d_fights = d_log.find(FIGHT_START) if d_log is not None else []
    if d_fights:
        counts.append(f"killed by D: {by_player.get('D', 0)} (D fought {len(d_fights)} time(s))")
        if not by_player.get("D"):
            fight_findings.append(Finding(
                f"D fought {len(d_fights)} time(s) and no enemy died by D on S: the second player's hits never landed "
                "on the server", [d_fights[0][0].cite(),
                                  absent(run.server, "`enemy ... dead by <D's peer>`") if run.server else "no S log"]))
    if scenery or unreleased:
        counts.append(f"set apart: {len(scenery)} scenery, {len(unreleased)} held and not released in this run")
    gone_early = [k for k in slept if k not in expected]
    if gone_early:
        counts.append(f"gone to sleep on the server before any player's game made it: {len(gone_early)}")
        held_notes.append("gone to sleep on the server before any player's game made it (not graded): " + "; ".join(
            f"{keys[k].name} ({k}): {keys[k].streamed.cite()}" for k in gone_early[:6]))
    findings = fight_findings + findings  # the fights first: the most telling
    if not expected and not findings:
        # Nothing a client should have had (every key set apart, made too late or gone before it came): not a pass.
        return Verdict("enemies", NOT_TESTED, "; ".join(counts) + "; no enemy any client should have had", first,
                       notes=held_notes)
    return Verdict("enemies", FAIL if findings else PASS, "; ".join(counts), first, findings, held_notes)


# The fights a run's steps made (H's route helpers, route_actions.cpp fight_area: `start`, one `try <k>: x at
# <Name#id> d <d>: hp <a> -> <b|gone>` per burst, then `done (...)` or `failed (...)`) and S's deaths by a player
# (`enemy <key> dead by <peer> (S, ...; attacker PeerPuppet_<n>#..)`; `dead by none` is no player's kill). A
# fight step that failed with no hit point taken, and a route that fought where no enemy died by a player, are
# findings of the enemies row, never a pass (ww1 20261119-035316: 16 bursts, hp 96 -> 96 each, "enemies
# PASS"). With S's damage trace on, its `puppet <n> damage check on ... result 0 reason ...` lines say why.
FIGHT_START = re.compile(r"Dev: fight_area (\S+) start: ")
FIGHT_TRY = re.compile(r"Dev: fight_area (\S+) try \d+: x at (\S+) d [\d.]+: hp (-?\d+) -> (\S+)")
FIGHT_END = re.compile(r"Dev: fight_area (\S+) (done|failed) \((.*)\) after ([\d.]+) s")
PLAYER_KILL = re.compile(r"Online: enemy " + KEY + r" dead by (\d+) ")
DAMAGE_REFUSED = re.compile(r"Online: puppet \d+ damage check on .*result 0 reason .*")


def fight_findings_of(run: Run, by_player: dict[str, int] | None = None,
                      unrecorded: int = 0, data: LevelData | None = None) -> tuple[str, list[Finding]]:
    """H's fights against S's deaths: `by_player` the kills S credits to each client (`dead by <peer>`),
    `unrecorded` the deaths whose attacker S does not name (the encounter path: a player's kill may hide there)."""
    by_player = by_player or {}
    h = run.clients.get("H")
    if h is None:
        return "fights: no H log", []
    fights: list[dict] = []
    cur: dict | None = None
    for i, raw in enumerate(h.lines):
        m = FIGHT_START.search(raw)
        if m:
            cur = {"area": m.group(1), "start": h.line(i), "tries": [], "hit": False, "end": None}
            fights.append(cur)
            continue
        m = FIGHT_TRY.search(raw)
        if m and cur is not None and m.group(1) == cur["area"]:
            cur["tries"].append(h.line(i))
            after = m.group(4)
            if after == "gone" or (after.lstrip("-").isdigit() and int(after) < int(m.group(3))):
                cur["hit"] = True
            continue
        m = FIGHT_END.search(raw)
        if m and cur is not None and m.group(1) == cur["area"]:
            cur["end"] = (h.line(i), m.group(2), m.group(3))
            cur = None
    kills = run.server.find(PLAYER_KILL) if run.server is not None else []
    tally = ", ".join(f"{w} {n}" for w, n in by_player.items())
    refused = run.server.find(DAMAGE_REFUSED) if run.server is not None else []
    findings: list[Finding] = []
    on_held = [fg for fg in fights if fg["tries"] and not fg["hit"] and held_victims(run, fg, data)]
    for fg in fights:
        if fg["end"] is None or fg["end"][1] != "failed" or fg["hit"] or not fg["tries"]:
            continue  # a fight that failed before any burst is the tool's (a hold, out of reach): counted below
        if fg in on_held:
            continue  # every victim held: the level's script, not the game (counted below)
        lines = [fg["end"][0].cite()] + ([fg["tries"][0].cite()] if fg["tries"] else [])
        lines.append(refused[0][0].cite() + f" (and {len(refused) - 1} more)" if refused else
                     "S logged no damage check line (its damage trace is off in this run): the hits are not "
                     "explained, only that none took a hit point")
        if tally:
            lines.append(f"S's kills by player (`dead by <peer>`): {tally}")
        lines += held_lines(run, fg, data)
        findings.append(Finding(f"a fight step failed with no damage done: {len(fg['tries'])} burst(s) at "
                                f"{fg['area']}, no hit took a hit point", lines))
    with_bursts = [fg for fg in fights if fg["tries"] and fg not in on_held]
    others = {w: n for w, n in by_player.items() if w != "H" and n}
    if with_bursts and not any(fg["hit"] for fg in with_bursts) and not by_player.get("H") and others:
        # One player's hits take nothing while the other's kill (the player S credits: `dead by <peer>`).
        findings.append(Finding(
            f"H's hits took no hit point in {len(with_bursts)} fight(s) ({sum(len(fg['tries']) for fg in with_bursts)} "
            f"burst(s)) while {', '.join(f'{w} killed {n}' for w, n in others.items())} on S: one player cannot "
            "hurt enemies the other can",
            [with_bursts[0]["tries"][0].cite(), next(l.cite() for l, m in kills if peer_of(run, m) in others)]))
    if with_bursts and not kills and not unrecorded:
        findings.append(Finding(
            f"the route fought {len(fights)} time(s) ({sum(len(fg['tries']) for fg in fights)} burst(s)) and no "
            "enemy died by a player on S",
            [fights[0]["start"].cite(),
             absent(run.server, "`enemy ... dead by <peer>` (a player's kill)") if run.server else "no S log"]))
    no_burst = [fg for fg in fights if fg["end"] is not None and fg["end"][1] == "failed" and not fg["tries"]]
    text = (f"fights: {len(fights)} step(s), {len(with_bursts)} with bursts, "
            f"{sum(1 for fg in fights if fg['hit'])} took hit points"
            + (f", {len(no_burst)} failed before any burst ({no_burst[0]['end'][2][:60]})" if no_burst else "")
            + f"; {len(kills)} enemy death(s) by a player on S"
            + "".join(f"; the fight at {fg['area']} hit only held enemies ({held_victims(run, fg, data)}): the level's "
                      "script, not a finding" for fg in on_held))
    return text, findings


def held_victims(run: Run, fg: dict, data: LevelData | None) -> str:
    """When every victim of the fight's bursts is scenery or held and not released in this run: their names and
    holds in words; else "" (the fight is graded)."""
    if data is None or not fg["tries"]:
        return ""
    try:
        at = tuple(float(v) for v in fg["area"].split(","))
    except ValueError:
        return ""
    out = []
    for name in dict.fromkeys(m.group(1) for line in fg["tries"] for m in [VICTIM.search(line.text)] if m):
        cands = [x for x in data.holds.values() if x.name == name and x.pos is not None]
        if not cands:
            return ""
        hold = min(cands, key=lambda x: math.dist(x.pos, at))
        if hold.ambient:
            out.append(f"{name} scenery")
        elif released_here(run, hold, False) is None:
            out.append(f"{name} held, not released in this run")
        else:
            return ""
    return ", ".join(out)


VICTIM = re.compile(r" try \d+: x at ([^#\s]+)#")


def held_lines(run: Run, fg: dict, data: LevelData | None) -> list[str]:
    """For a fight that took nothing: each victim's hold and release from the level's data (levelpass_holds),
    and whether this run fired it (the pass's own act line on H, a lock's opening line), so the next reader
    sees a held enemy instead of suspecting the game."""
    if data is None:
        return ["the level's data (logic.json, manifest.json) is not at hand: its holds are not shown"]
    try:
        at = tuple(float(v) for v in fg["area"].split(","))
    except ValueError:
        at = None
    out = []
    h = run.clients.get("H")
    for name in dict.fromkeys(m.group(1) for line in fg["tries"] for m in [VICTIM.search(line.text)] if m):
        cands = [x for x in data.holds.values() if x.name == name]
        if at is not None and len(cands) > 1:
            cands.sort(key=lambda x: sum((a - b) ** 2 for a, b in zip(x.pos, at)) if x.pos else float("inf"))
        if not cands:
            out.append(f"{name}: not held in the level's data (no job state, its AI not disabled)")
            continue
        hold = cands[0]
        out.append(f"{name}: {hold.words()}")
        for r in hold.releases:
            if not r.act or not r.path:
                continue
            root = r.path[0]
            if r.act == "lock":
                line = gate_opened(run, root)
                out.append(f"    {root} opened: {line.cite()}" if line else
                           f"    {root}: no line shows it opened: not fired in this run")
            else:
                acted = h.find(re.compile(r"Input script: LEVELPASS act \S+ \S+ " + re.escape(root) + r" ")) if h else []
                out.append(f"    the pass acted on {root}: {acted[0][0].cite()}" if acted else
                           f"    the pass never acted on {root} (no `LEVELPASS act ... {root}` on H): not fired by "
                           "this run")
    return out


def peer_of(run: Run, m: re.Match) -> str:
    """The client a `dead by <peer>` match names (PLAYER_KILL's peer group), or ''."""
    return peer_numbers(run).get(int(m.group(3)), "")


# --- pickups (and the coins slot) ----------------------------------------------------------------------

S_APPLIED = re.compile(r"Online: collect applied ([0-9a-f]{8}) for peer (\d+)")
COLLECT_NATIVE = r"Online: collect native {id} "
PARTNER_GOT = r"Online: (?:collect from S {id} |pickup copy not found {id} )"
OWN_DROP = re.compile(r"Online: collect native ffffffff \(not reported: no source\)")
COINS = "coins: their own row (coins)"


def grade_pickups(run: Run) -> Verdict:
    if run.server is None:
        return Verdict("pickups", NOT_TESTED, "no server log in this run folder", notes=[COINS])
    applied = run.server.find(S_APPLIED)
    if not applied:
        return Verdict("pickups", NOT_TESTED, "S applied no collect (`collect applied <id> for peer <n>`): no pickup "
                       "was taken in this run", notes=[COINS])
    peers = peer_numbers(run)
    findings: list[Finding] = []
    ok = 0
    graded = 0
    for line, m in applied:
        pid, peer = m.group(1), int(m.group(2))
        collector = peers.get(peer)
        for who, log in run.clients.items():
            w = next((w for w in client_windows(log).values() if line.t and w.start <= line.t <= w.end), None)
            if w is None:
                continue
            if who == collector:
                if not log.find(re.compile(COLLECT_NATIVE.format(id=pid))):
                    findings.append(Finding(f"{who} (the collector) logged no collect of {pid}", [
                        line.cite(), absent(log, f"`collect native {pid}`")]))
                continue
            graded += 1
            got = log.find(re.compile(PARTNER_GOT.format(id=pid)))
            if got:
                ok += 1
            else:
                findings.append(Finding(f"{who} kept pickup {pid} that peer {peer} collected on S", [
                    line.cite(), absent(log, f"`collect from S {pid}` or `pickup copy not found {pid}`")]))
    counts = f"S applied {len(applied)} collect(s); the partner's copy went {ok} of {graded}"
    if graded == 0 and not findings:
        return Verdict("pickups", NOT_TESTED, counts + " (no partner in the level at the time)", applied[0][0].cite(),
                       notes=[COINS])
    return Verdict("pickups", FAIL if findings else PASS, counts, applied[0][0].cite(), findings, [COINS])


# --- coins (the shared placed money coins: game/coins.cpp, docs/online-7.md) -----------------------------------
#
# On when S says so (`coins on for the session (S's online_shared_coins on)`); off: not tested. Each game walks
# its zones' placed money coins (`coins walked <zone> <n> placed money coins mapped`) and keeps an account per
# level: a client's `coins <g>: <m> placed money coins mapped, <t> taken here, <r> taken by others and paid here,
# <p> pending, <u> unpaid, <w> collected without pay, ...`, S's `coins <g>: <c> claims granted, <x> refused, <m>
# placed money coins mapped here (<t> this level), ...`. A coin a client takes (`coin taken here ... gold <a> ->
# <b>`) is granted by S and paid on the others (`coin paid here ... gold ...`). A run with coins on where no game
# mapped a single coin fails ("0 coins mapped on every game": batch 8 runs 7 and 8, the walk never handed the
# coin code the level's coins), never "not tested".
COINS_SWITCH = re.compile(r"Online: coins (on|off) for the session \(S's online_shared_coins (on|off)\)")
COINS_WALKED = re.compile(r"Online: coins walked (\S+) (\d+) placed money coins mapped")
C_COINS = re.compile(r"Online: coins (\d+): (\d+) placed money coins mapped, (\d+) taken here, (\d+) taken by others and "
                     r"paid here, (\d+) pending, (\d+) unpaid, (\d+) collected without pay, (\d+) claims not sent yet")
S_COINS = re.compile(r"Online: coins (\d+): (\d+) claims granted, (\d+) refused, (\d+) placed money coins mapped here "
                     r"\((\d+) this level\)")
COIN_TAKEN = re.compile(r"Online: coin taken here (\S+) \(player \S+, gold (\d+) -> (\d+)")
COIN_PAID = re.compile(r"Online: coin paid here (\S+) \(taken by another player; player \S+, gold (\d+) -> (\d+)")
COIN_HANDLED = re.compile(r"Online: coin claim (\S+) peer (\d+) (granted|refused)")
COIN_BAD = re.compile(r"Online: coin (?:unpaid|collected without pay) \S+ .*|Online: coin claim \S+ peer \d+ refused .*")


def grade_coins(run: Run) -> Verdict:
    """The shared coins: every game's mapped coins, each coin taken on one client and paid on the others, and
    nothing unpaid, collected without pay or refused; 0 mapped on every game with coins on is a failure."""
    if run.server is None:
        return Verdict("coins", NOT_TESTED, "no server log in this run folder")
    sw = run.server.find(COINS_SWITCH)
    if not sw:
        return Verdict("coins", NOT_TESTED, "S logged no `coins on|off for the session` line (a build before shared "
                       "coins)")
    if sw[0][1].group(1) == "off":
        return Verdict("coins", NOT_TESTED, "shared coins are off in this run (S's online_shared_coins off)",
                       sw[0][0].cite())
    games = [("S", run.server)] + sorted(run.clients.items(), key=lambda kv: (kv[0] != "H", kv[0]))
    mapped: dict[str, int] = {}
    parts: list[str] = []
    for who, log in games:
        walked = sum(int(m.group(2)) for _l, m in log.find(COINS_WALKED))
        if who == "S":
            accts = log.find(S_COINS)
            acct_mapped = max([int(m.group(4)) for _l, m in accts] or [0])
            granted = sum(int(m.group(2)) for _l, m in accts)
            refused = sum(int(m.group(3)) for _l, m in accts)
            parts.append(f"S: walked {walked}, accounts {len(accts)} (mapped {acct_mapped}, claims granted {granted}, "
                         f"refused {refused})")
        else:
            accts = log.find(C_COINS)
            acct_mapped = max([int(m.group(2)) for _l, m in accts] or [0])
            sums = [sum(int(m.group(i)) for _l, m in accts) for i in (3, 4, 5, 6, 7)]
            parts.append(f"{who}: walked {walked}, accounts {len(accts)} (mapped {acct_mapped}, taken here {sums[0]}, "
                         f"paid here {sums[1]}, pending {sums[2]}, unpaid {sums[3]}, collected without pay {sums[4]})")
        mapped[who] = max(walked, acct_mapped)
    counts = "; ".join(parts)
    if not any(mapped.values()):
        accts = [l.cite() for who, log in games for l, _m in (log.find(S_COINS if who == "S" else C_COINS))[:1]]
        return Verdict("coins", FAIL, "0 coins mapped on every game: " + counts, sw[0][0].cite(), [Finding(
            "0 coins mapped on every game: no game's coin code saw the level's placed money coins (no `coins "
            "walked` line, every level's account 0 mapped), so no coin a player picks up is shared",
            accts + [absent(log, "`coins walked`") for _w, log in games])])
    findings: list[Finding] = []
    unmapped = [w for w, n in mapped.items() if n == 0]
    if unmapped:
        findings.append(Finding(f"0 coins mapped on {', '.join(unmapped)} while others mapped some", [
            absent(dict(games)[w], "`coins walked`") for w in unmapped]))
    bad = [(who, l) for who, log in games for l, _m in log.find(COIN_BAD)]
    if bad:
        findings.append(Finding(f"{len(bad)} coin(s) unpaid, collected without pay or refused",
                                [f"{who}: {l.cite()}" for who, l in bad[:12]]))
    # Each client's own takes (`coin taken here <place> (player <hero>, gold a -> b, +d; S is told)`): batch 11's
    # second round (ww1 20261119-111305) took 3 coins on H and 7 on D, and S handled no claim; the row had said
    # "no coin was taken" (the player field is a hex hero, which the old pattern refused).
    order = sorted(run.clients, key=lambda w: (w != "H", w))
    takes = {who: {m.group(1): line for line, m in run.clients[who].find(COIN_TAKEN)} for who in order}
    paid = [(who, l) for who, log in run.clients.items() for l, _m in log.find(COIN_PAID)]
    handled = run.server.find(COIN_HANDLED)
    n_taken = sum(len(v) for v in takes.values())
    counts += (f"; coins taken: {', '.join(f'{w} {len(takes[w])}' for w in order)}; S handled {len(handled)} claim(s); "
               f"paid on another game {len(paid)}")
    if not n_taken:
        return Verdict("coins", FAIL if findings else NOT_TESTED, counts + " (no game logged a take)",
                       sw[0][0].cite(), findings)
    first = next(line for who in order for line in takes[who].values())
    if not handled:
        findings.insert(0, Finding(
            f"{', '.join(f'{len(takes[w])} coin(s) taken on {w}' for w in order)}; S handled 0 claim(s): shared coins "
            "did nothing", [first.cite(), absent(run.server, "`coin claim <place> peer <p> granted|refused`")]))
    twice = sorted(p for p in set().union(*[set(v) for v in takes.values()]) if sum(p in v for v in takes.values()) > 1)
    if twice:
        findings.append(Finding(f"taken twice: {len(twice)} coin(s) taken on more than one game ({', '.join(twice[:6])})",
                                [f"{w}: {takes[w][p].cite()}" for p in twice[:3] for w in order if p in takes[w]]))
    return Verdict("coins", FAIL if findings else PASS, counts, first.cite(), findings)


# --- together: D near H where H acted (the second player follows the first) ------------------------------
#
# With the follow on (D's `Online: auto follow the host (peer 1) (script)`: `online_auto_follow on` after the
# party is in), the game moves D's hero next to H's whenever it is farther than FOLLOW_NEAR. Per stop where H
# acted (H's first `LEVELPASS act v<n>_<k>` line), S's puppet poses (`puppet <n> pose <seq> at <x,y,z>`, every
# 2 s; the puppet of each peer from `puppet <n> for peer <p> spawned ... (generation <g>)`: the slots swap at a
# level change) say whether D stood within FOLLOW_NEAR of H within FOLLOW_WINDOW after it, so the enemies and
# the loot rows are read where both players stand. Without the follow: not tested (D stayed where it entered).
FOLLOW_NEAR = 400.0           # the follow moves the hero when it is farther than this (client/auto_follow.h)
FOLLOW_WINDOW = timedelta(seconds=5)   # a move a second at most, poses every 2 s
POSE_PAIR = timedelta(seconds=1.5)     # two puppets' poses this close in time are compared
FOLLOW_ON = re.compile(r"Online: auto follow the host \(peer (\d+)\) \(script\)")
PUPPET_FOR = re.compile(r"Online: puppet (\d+) for peer (\d+) spawned at .*\(generation (\d+)\)")
PUPPET_POSE = re.compile(r"Online: puppet (\d+) pose \d+ at (-?[\d.]+),(-?[\d.]+),(-?[\d.]+) ")
H_ACT = re.compile(r"Input script: LEVELPASS act (v\d+)_\d+ ")
# A sweep step its deadline skipped (`Dev: <command> <target> ... skipped: the sweep's time is up`), and the act it
# belongs to (H's `LEVELPASS act <v>_<i> <kind> <name> ...` before it).
ACT_LINE = re.compile(r"Input script: LEVELPASS act (v\d+_\d+) (\S+) (\S+)")
STEP_SKIPPED = re.compile(r"Dev: \S+ .* skipped: the sweep's time is up")
# The rows an act's kind would have tested (W13): a pickup's collect and coins, a chest's or a crate's spill, a
# fight's enemies, a trigger's or a talk's scene and the enemies it releases.
KIND_ROWS = {"pickup": ("pickups", "coins"), "interact": ("loot", "pickups"), "attack": ("loot", "enemies"),
             "fight": ("enemies",), "enter": ("cutscenes", "enemies"), "talk": ("cutscenes",)}


def skipped_acts(h: Log | None) -> list[tuple[str, str, str, Line]]:
    """Each act of H's sweep whose step its deadline skipped: (label, kind, name, the skip line), once per act."""
    out: list[tuple[str, str, str, Line]] = []
    if h is None:
        return out
    current = None
    for i, raw in enumerate(h.lines):
        m = ACT_LINE.search(raw)
        if m:
            current = (m.group(1), m.group(2), m.group(3))
            continue
        if "LEVELPASS visit" in raw or "LEVELPASS phase" in raw:
            current = None
            continue
        if current is not None and STEP_SKIPPED.search(raw) and (not out or out[-1][0] != current[0]):
            out.append((*current, h.line(i)))
    return out


def peer_poses(server: Log) -> dict[int, list[tuple[datetime, tuple[float, float, float], Line]]]:
    """Each peer's poses on S, by the puppet each pose names and that puppet's peer at the time."""
    owner: dict[int, int] = {}
    out: dict[int, list] = {}
    for i, raw in enumerate(server.lines):
        m = PUPPET_FOR.search(raw)
        if m:
            owner[int(m.group(1))] = int(m.group(2))
            continue
        m = PUPPET_POSE.search(raw)
        if m and int(m.group(1)) in owner:
            line = server.line(i)
            if line.t is not None:
                out.setdefault(owner[int(m.group(1))], []).append(
                    (line.t, (float(m.group(2)), float(m.group(3)), float(m.group(4))), line))
    return out


def grade_together(run: Run) -> Verdict:
    """Per stop where H acted: was D within FOLLOW_NEAR of H (S's poses) in the seconds after?"""
    d, h = run.clients.get("D"), run.clients.get("H")
    if d is None or h is None:
        return Verdict("together", NOT_TESTED, "one client in this run (no second player to follow)")
    on = d.find(FOLLOW_ON)
    if not on:
        return Verdict("together", NOT_TESTED, "D did not follow H (no `auto follow the host ... (script)` on D: "
                       "--d-stays, or a build without online_auto_follow): D stayed where it entered")
    if run.server is None:
        return Verdict("together", NOT_TESTED, "no server log: no poses", on[0][0].cite())
    peers = {w: p for p, w in peer_numbers(run).items()}
    poses = peer_poses(run.server)
    hp, dp = poses.get(peers.get("H", -1), []), poses.get(peers.get("D", -1), [])
    stops: dict[str, Line] = {}
    # A step the sweep's deadline skipped is no stop where H acted (W14: ww1 20261119-124636's v19, v22, v24).
    skipped = {label for label, _k, _n, _l in skipped_acts(h)}
    for line, m in h.find(H_ACT):
        full = ACT_LINE.search(line.text)
        if full is not None and full.group(1) in skipped:
            continue
        if line.t is not None and (on[0][0].t is None or line.t >= on[0][0].t):
            stops.setdefault(m.group(1), line)
    if not stops:
        return Verdict("together", NOT_TESTED, "H acted at no stop after D's follow began", on[0][0].cite())
    near, apart, unknown = [], [], []
    findings: list[Finding] = []
    for label, act in stops.items():
        win_h = [p for p in hp if act.t <= p[0] <= act.t + FOLLOW_WINDOW]
        win_d = [p for p in dp if act.t <= p[0] <= act.t + FOLLOW_WINDOW]
        pairs = [(math.dist(a[1][:2], b[1][:2]), a, b) for a in win_h for b in win_d if abs(a[0] - b[0]) <= POSE_PAIR]
        if not pairs:
            unknown.append(label)
            continue
        dist, a, b = min(pairs, key=lambda x: x[0])
        if dist <= FOLLOW_NEAR:
            near.append(f"{label} ({dist:.0f})")
        else:
            apart.append(f"{label} ({dist:.0f})")
            findings.append(Finding(f"{label}: D was {dist:.0f} from H while H acted (the follow keeps it within "
                                    f"{FOLLOW_NEAR:.0f})", [act.cite(), a[2].cite(), b[2].cite()]))
    counts = (f"D near H (within {FOLLOW_NEAR:.0f} on S's poses) at {len(near)} of {len(stops)} stop(s) where H acted"
              + (f"; apart at {len(apart)}" if apart else "") + (f"; no poses at {len(unknown)}" if unknown else ""))
    notes = [f"near: {', '.join(near)}"] if near else []
    notes += [f"no pose pair within {POSE_PAIR.seconds + 0.5:.1f} s: {', '.join(unknown)}"] if unknown else []
    status = FAIL if findings else (PASS if near else NOT_TESTED)
    return Verdict("together", status, counts, on[0][0].cite(), findings, notes)


# --- objectives: S's objective rows against each client's applies (online/6-progress-apply, the objectives' author's
# grading, logs/progress-apply-notes.txt) ----------------------------------------------------------------------------
# With online_shared_objectives on (each client's `objectives shared for the session (S's online_shared_objectives`),
# per generation (each log's `objective list <n> tracker <t> gen <g>`, the level's list) and objective id: S's
# sequence of `objective <id> <n>/<m> (S)[ active]` equals each client's `objective applied <id> <n>/<m>[ (active)]`,
# each within OBJECTIVE_APPLY_S of S's; none of `objective <id> at ... after its apply, S has`, `objective apply
# refused` or `objective <id> own <action> run here` for an id S has a row for in that generation; each S row going
# inactive after it was active has an `objective cancelled <id>` on every client. Off: not tested.
OBJECTIVE_APPLY_S = timedelta(seconds=10)
OBJ_LIST = re.compile(r"Online: objective list \d+ tracker \S+ gen (\d+)")
OBJ_S_ROW = re.compile(r"Online: objective ([0-9a-f]{8}) (\d+)/(\d+) \(S\)( active)?")
OBJ_APPLIED = re.compile(r"Online: objective applied ([0-9a-f]{8}) (\d+)/(\d+)( \(active\))?")
OBJ_SHARED = re.compile(r"Online: objectives shared for the session \(S's online_shared_objectives")
OBJ_AFTER = re.compile(r"Online: objective ([0-9a-f]{8}) at .* after its apply, S has")
OBJ_REFUSED = re.compile(r"Online: objective apply refused ([0-9a-f]{8})")
OBJ_OWN_RUN = re.compile(r"Online: objective ([0-9a-f]{8}) own (\S+) run here")
OBJ_CANCELLED = re.compile(r"Online: objective cancelled ([0-9a-f]{8})")


def objective_lines(log: Log, rx: re.Pattern) -> list[tuple[int, Line, re.Match]]:
    """Each line of `rx` with the generation of the objective list this game had then (0 before any)."""
    gen, out = 0, []
    for i, raw in enumerate(log.lines):
        m = OBJ_LIST.search(raw)
        if m:
            gen = int(m.group(1))
            continue
        m = rx.search(raw)
        if m:
            out.append((gen, log.line(i), m))
    return out


def grade_objectives(run: Run) -> Verdict:
    """S's objective rows against each client's applies, per generation and objective (the objectives' author's
    grading)."""
    if run.server is None:
        return Verdict("objectives", NOT_TESTED, "no server log in this run folder")
    shared = {who: log.find(OBJ_SHARED) for who, log in run.clients.items()}
    if not any(shared.values()):
        return Verdict("objectives", NOT_TESTED, "online_shared_objectives is off in this run (no client logged "
                       "`objectives shared for the session`): each game runs its own objectives")
    rows: dict[tuple[int, str], list[tuple[str, Line]]] = {}
    for gen, line, m in objective_lines(run.server, OBJ_S_ROW):
        rows.setdefault((gen, m.group(1)), []).append((f"{m.group(2)}/{m.group(3)}{' active' if m.group(4) else ''}",
                                                      line))
    findings: list[Finding] = []
    order = sorted(run.clients, key=lambda w: (w != "H", w))
    counts = [f"S rows {sum(len(v) for v in rows.values())} for {len(rows)} objective(s) in "
              f"{len({g for g, _i in rows})} level(s)"]
    for who in order:
        log = run.clients[who]
        applied: dict[tuple[int, str], list[tuple[str, Line]]] = {}
        for gen, line, m in objective_lines(log, OBJ_APPLIED):
            applied.setdefault((gen, m.group(1)), []).append(
                (f"{m.group(2)}/{m.group(3)}{' active' if m.group(4) else ''}", line))
        counts.append(f"{who} applied {sum(len(v) for v in applied.values())}")
        for key in sorted(set(rows) | set(applied)):
            s_seq, c_seq = rows.get(key, []), applied.get(key, [])
            words = (f"generation {key[0]} objective {key[1]}: S {[v for v, _l in s_seq]}, {who} "
                     f"{[v for v, _l in c_seq]}")
            if [v for v, _l in s_seq] != [v for v, _l in c_seq]:
                cite = [l.cite() for _v, l in s_seq[:3]] + [l.cite() for _v, l in c_seq[:3]]
                findings.append(Finding(f"{who}'s applies differ from S's rows: {words}",
                                        cite or [absent(log, "`objective applied`")]))
                continue
            late = [(s, c) for (_v, s), (_w, c) in zip(s_seq, c_seq)
                    if s.t is not None and c.t is not None and c.t - s.t > OBJECTIVE_APPLY_S]
            if late:
                s, c = late[0]
                findings.append(Finding(f"{who} applied S's row more than {OBJECTIVE_APPLY_S.seconds} s late "
                                        f"({(c.t - s.t).total_seconds():.1f} s): {words}", [s.cite(), c.cite()]))
        for gen, line, m in objective_lines(log, OBJ_AFTER):
            findings.append(Finding(f"{who}: objective {m.group(1)} differs from S's after its apply", [line.cite()]))
        for gen, line, m in objective_lines(log, OBJ_REFUSED):
            findings.append(Finding(f"{who}: S's objective row {m.group(1)} was refused here", [line.cite()]))
        for gen, line, m in objective_lines(log, OBJ_OWN_RUN):
            if (gen, m.group(1)) in rows:
                first = rows[(gen, m.group(1))][0][1]
                findings.append(Finding(f"{who} ran its own {m.group(2)} of objective {m.group(1)}, for which S has a "
                                        f"row in generation {gen}", [line.cite(), first.cite()]))
        cancelled = {(gen, m.group(1)) for gen, _l, m in objective_lines(log, OBJ_CANCELLED)}
        for key, seq in rows.items():
            went = next((l for (a, _la), (b, l) in zip(seq, seq[1:]) if a.endswith(" active")
                         and not b.endswith(" active")), None)
            if went is not None and key not in cancelled:
                findings.append(Finding(f"{who}: S's objective {key[1]} went inactive (generation {key[0]}) and "
                                        "nothing cancelled it here", [went.cite(), absent(log, "`objective cancelled`")]))
    example = next((l.cite() for seq in rows.values() for _v, l in seq), "")
    # Each objective's sequences on S and each client (the row's evidence, PASS or FAIL).
    notes = []
    for key in sorted(rows):
        per = [f"S {[v for v, _l in rows[key]]}"]
        for who in order:
            seq = [f"{m.group(2)}/{m.group(3)}{' active' if m.group(4) else ''}"
                   for gen, _l, m in objective_lines(run.clients[who], OBJ_APPLIED) if (gen, m.group(1)) == key]
            per.append(f"{who} {seq}")
        notes.append(f"generation {key[0]} objective {key[1]}: " + ", ".join(per))
    return Verdict("objectives", FAIL if findings else PASS, "; ".join(counts), example, findings, notes)


# --- level pieces: run-time movers (ww1's travelling islands; docs/online-6.md "Spawned movers") ---------------
#
# With online_island_spawns on, S tracks each island spawner (`world tracked spawner_fires <Name#id> (...)`) and
# fires its islands (`world island spawner <Name#id> fired <n> (tick <t>): <island>`); each client with the area
# loaded (its own `world tracked spawner_fires` of that spawner) makes S's n-th island (`made <n> here (S's <m>,
# <a> ticks after S)`) or adopts it (`adopted <k> of S's <m>`). Each island's place once a second
# (`world island <spawner> #<n> at <x,y,z>`) and its end (`... #<n> gone (lived <ms> ms)`) on S and the client.
# Graded (the movers agent's rule, logs/pieces-notes.txt; calibrated on islands-probe-ww1 20261119-042921: S fired
# 18, H made the same 18, places 1.6 apart typically): every fire has its island on each such client within
# PIECE_MADE_S, matched places (within PIECE_PAIR_S of each other) within PIECE_NEAR, each S `gone` followed on the
# client within PIECE_MADE_S, none of the client's warnings. Off (S tracked no island spawner on a level whose
# data has them): not tested, each game runs its own islands.
PIECE_MADE_S = timedelta(seconds=1)
PIECE_PAIR_S = timedelta(seconds=0.5)
PIECE_NEAR = 12.0           # W31: about 10 (the probe run: 1.6 typically, 10.1 at most); was 64
PIECE_TRACKED = re.compile(r"Online: world tracked spawner_fires ([^#\s]+)#")
PIECE_FIRED = re.compile(r"Online: world island spawner ([^#\s]+)#\S* fired (\d+) \(tick")
PIECE_MADE = re.compile(r"Online: world island spawner ([^#\s]+)#\S* made (\d+) here")
PIECE_ADOPTED = re.compile(r"Online: world island spawner ([^#\s]+)#\S* adopted (\d+) of S's (\d+)")
PIECE_AT = re.compile(r"Online: world island ([^#\s]+)#\S* #(\d+) at (-?[\d.]+),(-?[\d.]+),(-?[\d.]+)")
PIECE_GONE = re.compile(r"Online: world island ([^#\s]+)#\S* #(\d+) gone")
PIECE_BAD = re.compile(r"Online: world island spawner \S+ (?:fire made no island here|has no spawn message of its own|"
                       r"spawn for S's \d+ not delivered inside the send).*")


def grade_pieces(run: Run, data: "LevelData | None" = None) -> Verdict:
    """S's island fires against each client's islands: made in time, in the same place, gone together."""
    if run.server is None:
        return Verdict("level pieces", NOT_TESTED, "no server log in this run folder")
    tracked = {m.group(1) for _l, m in run.server.find(PIECE_TRACKED)}
    if not tracked:
        movers = data.movers if data is not None else []
        if movers:
            return Verdict("level pieces", NOT_TESTED, f"not tested: each game runs its own islands (S tracked no island "
                           f"spawner, online_island_spawns off; the level's data has {len(movers)}: "
                           f"{', '.join(movers[:5])})")
        return Verdict("level pieces", NOT_TESTED, "S tracked no island spawner (the level has no run-time movers, or "
                       "online_island_spawns is off)")
    fires = [(line, m.group(1), int(m.group(2))) for line, m in run.server.find(PIECE_FIRED)]
    if not fires:
        return Verdict("level pieces", NOT_TESTED, f"S tracked {len(tracked)} island spawner(s) and fired none in this "
                       "run (their start trigger was not entered)", run.server.find(PIECE_TRACKED)[0][0].cite())
    s_at = [(line, m) for line, m in run.server.find(PIECE_AT)]
    s_gone = [(line, m) for line, m in run.server.find(PIECE_GONE)]
    counts = [f"S fired {len(fires)} island(s) of {len({sp for _l, sp, _n in fires})} spawner(s)"]
    findings: list[Finding] = []
    for who in sorted(run.clients, key=lambda w: (w != "H", w)):
        log = run.clients[who]
        here = {m.group(1) for _l, m in log.find(PIECE_TRACKED)}
        made = {(m.group(1), int(m.group(2))): line for line, m in log.find(PIECE_MADE)}
        adopted = [(m.group(1), int(m.group(3)), line) for line, m in log.find(PIECE_ADOPTED)]
        mine = [(line, sp, n) for line, sp, n in fires if sp in here]
        late, missing, ok = [], [], 0
        for line, sp, n in mine:
            got = made.get((sp, n))
            if got is None:
                cover = next((a for a in adopted if a[0] == sp and a[1] >= n), None)
                if cover is None:
                    missing.append(f"{line.cite()}; {absent(log, f'`{sp} made {n} here` or an adoption')}")
                else:
                    ok += 1
            elif got.t and line.t and abs(got.t - line.t) > PIECE_MADE_S:
                late.append(f"{line.cite()}; {got.cite()} ({abs((got.t - line.t).total_seconds()):.2f} s)")
            else:
                ok += 1
        c_at = log.find(PIECE_AT)
        dists, far = [], []
        # Each client place against S's nearest line of the same island in time (within PIECE_PAIR_S). Interpolating
        # between S's once-a-second lines was tried (W31) and is worse on the probe run (median 6.5, max 13.9 against
        # 1.6 and 10.1): an island bobs and curves between two lines, and S's and the client's lines are written at
        # nearly the same moment anyway.
        for sl, sm in s_at:
            pair = [(cl, cm) for cl, cm in c_at if cm.group(1) == sm.group(1) and cm.group(2) == sm.group(2) and
                    cl.t and sl.t and abs(cl.t - sl.t) <= PIECE_PAIR_S]
            if not pair:
                continue
            cl, cm = min(pair, key=lambda p: abs(p[0].t - sl.t))
            d = math.dist(tuple(float(sm.group(i)) for i in (3, 4, 5)), tuple(float(cm.group(i)) for i in (3, 4, 5)))
            dists.append(d)
            if d > PIECE_NEAR:
                far.append(f"{sl.cite()}; {cl.cite()} ({d:.1f} apart)")
        c_gone = log.find(PIECE_GONE)
        gone_missing = []
        for sl, sm in s_gone:
            if sm.group(1) not in here:
                continue
            if not any(cm.group(1) == sm.group(1) and cm.group(2) == sm.group(2) and cl.t and sl.t and
                       abs(cl.t - sl.t) <= PIECE_MADE_S for cl, cm in c_gone):
                gone_missing.append(f"{sl.cite()}; {absent(log, f'`{sm.group(1)} #{sm.group(2)} gone` within 1 s')}")
        bad = [l.cite() for l, _m in log.find(PIECE_BAD)]
        med = sorted(dists)[len(dists) // 2] if dists else None
        counts.append(f"{who}: tracked {len(here)} spawner(s); made {ok} of {len(mine)} within 1 s; places "
                      f"{len(dists)} pair(s), {len(far)} over {PIECE_NEAR:.0f}"
                      + (f" (median {med:.1f}, max {max(dists):.1f})" if dists else "")
                      + f"; gone {len(s_gone) - len(gone_missing)} of S's {len([1 for _l, m in s_gone if m.group(1) in here])}")
        for what, lines in ((f"{who}: {len(missing)} of S's island fires never made here", missing),
                            (f"{who}: {len(late)} island(s) made more than 1 s from S's fire", late),
                            (f"{who}: {len(far)} island place(s) more than {PIECE_NEAR:.0f} from S's", far),
                            (f"{who}: {len(gone_missing)} island end(s) of S not followed here within 1 s", gone_missing),
                            (f"{who}: {len(bad)} island warning(s)", bad)):
            if lines:
                findings.append(Finding(what, lines[:8]))
    status = FAIL if findings else PASS
    return Verdict("level pieces", status, "; ".join(counts), fires[0][0].cite(), findings)


# --- loot (chests and enemy drops: the spill) -----------------------------------------------------------

HOOKS_OFF = re.compile(r"Online: loot hooks off \(online_shared_loot\)")
# A carrier's key in the loot lines: a world ref `w<mID>` (a client's line adds `.<name hash>`), or a run-time
# enemy's row `s<spawner digest>.<tag hash>#<ordinal>` (the game names a run-time enemy's drop by its row since
# batch 11 run 4: sibling spawns of one spawner differ only in the ordinal).
CARRIER = r"(w[0-9a-f]{8}|s[0-9a-f]{8}\.[0-9a-f]{8}#\d+)"
W_TAIL = r"(?:\.[0-9a-f]+)?"  # a world ref's name hash in a client's line (none after a run-time row)
S_OBLIGED = re.compile(r"Online: spill obliged " + CARRIER + r" ([0-9a-f]+) to peer (\d+) \(obligation (\d+)\)")
S_ENDED = re.compile(r"Online: spill obligation (\d+) " + CARRIER + r" [0-9a-f]+ to peer (\d+) ended (.+)$")
# S's account per client, at the session's end, and each client's own (the measure of delivery: batch 8 runs 7
# and 8, the page had counted a client's own run that matched S's selection as lost: "1 of 60", "0 of 16").
S_SUMMARY = re.compile(r"Online: spill obligations to peer (\d+): (\d+) made, (\d+) completed, (\d+) ended otherwise, "
                       r"(\d+) open")
DELIVERY = re.compile(r"Online: loot delivery: (\d+) obligations, (\d+) delivered in full, (\d+) differ, (\d+) short, "
                      r"(\d+) unfinished, (\d+) own runs without S's obligation \(S's selections give (\d+) items, "
                      r"here spawned (\d+)\)")
# The carrier S's selection names (its spill line: `spill <Name> w<key>.<hash> <n>#<k> selected ...`).
S_SELECTED = re.compile(r"Online: spill (\S+) " + CARRIER + W_TAIL + r" \S+ selected (\d+) items")
# A client's end of one obligation: run in full or matched by its own run, short, differing, or expired.
C_FULFILLED = re.compile(r"Online: spill fulfilled (?:\S+ )?" + CARRIER + W_TAIL + r" \S+ \(S selected (\d+) items, "
                         r"(?:here spawned (\d+)|this game's own run matched)")
C_DIFFERS = re.compile(r"Online: spill differs (?:\S+ )?" + CARRIER + W_TAIL + r" \S+ \((.*)\)$")
C_EXPIRED = re.compile(r"Online: spill expired (?:\S+ )?" + CARRIER + W_TAIL + r" \S+ \((.*?) after \d+ s")
C_SPILL = r"Online: spill \S+ (?:\S+ )?{key}[.\s]"
# The client's copies asleep there, by name: a replica or a bind waiting for its wake, an authored sleep.
C_ASLEEP_KEY = re.compile(r"Online: enemy (?:replica " + KEY + r" at \S+ asleep|bind " + KEY + r" .*waits \(asleep here)")
C_SPAWN_NAME = re.compile(r"Online: enemy spawn " + KEY + r" as ([^#\s]+)")
C_SLEPT = re.compile(r"Online: authored sleep \S+ ([^#\s]+)#\S* .*slept")
ASLEEP, NO_ROW, OTHER = "asleep here", "no row here", "other"
UNNAMED = ("(null)", "?", "-")


def asleep_names(run: Run, log: Log) -> set[str]:
    """The names of the enemies whose copy slept on this client at some time (by name: S's enemy lines and the
    client's own spawn lines give each key's name)."""
    names: dict[str, str] = {}
    if run.server is not None:
        for _line, m in run.server.find(S_PLACED):
            names.setdefault(m.group(2), m.group(4).split("#")[0])
    for _line, m in log.find(C_SPAWN_NAME):
        names.setdefault(m.group(1), m.group(3))
    out = {m.group(1) for _line, m in log.find(C_SLEPT)}
    for _line, m in log.find(C_ASLEEP_KEY):
        key = m.group(1) or m.group(3)
        if key in names:
            out.add(names[key])
    return out


def grade_loot(run: Run) -> Verdict:
    """Per client: its own `loot delivery` account and S's `spill obligations to peer` (the measure: a client's
    own run that matched S's selection is a full delivery, as both say); every obligation not delivered in
    full names its carrier and why: asleep here (its copy slept on that client), no row here (S's entity not
    bound there), or other (a differing own run, a short spawn, no line)."""
    own = {who: log.find(OWN_DROP) for who, log in run.clients.items()}
    own_note = ", ".join(f"{who} {len(v)}" for who, v in own.items() if v)
    notes = [f"own drops collected with no S key (this game's own selection): {own_note}"] if own_note else []
    if run.server is None:
        return Verdict("loot", NOT_TESTED, "no server log in this run folder", notes=notes)
    off = run.server.find(HOOKS_OFF)
    if off:
        return Verdict("loot", NOT_TESTED, "shared loot is off in this run (each game drops its own)", off[0][0].cite(),
                       notes=notes)
    obliged = run.server.find(S_OBLIGED)
    if not obliged:
        return Verdict("loot", NOT_TESTED, "S obliged no spill to a client (no chest or drop shared in this run)",
                       notes=notes)
    peers = peer_numbers(run)
    summary = {int(m.group(1)): (line, m) for line, m in run.server.find(S_SUMMARY)}
    carrier = {m.group(2): m.group(1) for _line, m in run.server.find(S_SELECTED)}
    counts: list[str] = []
    findings: list[Finding] = []
    whole = True
    for who in sorted(run.clients, key=lambda w: (w != "H", w)):
        log = run.clients[who]
        peer = next((p for p, w in peers.items() if w == who), None)
        mine = [(line, m) for line, m in obliged if peer is not None and int(m.group(3)) == peer]
        if not mine:
            continue
        acct = log.find(DELIVERY)
        s_sum = summary.get(peer)
        if acct:
            d = acct[-1][1]
            text = (f"{who}: {d.group(1)} obligations, {d.group(2)} delivered in full, {d.group(3)} differ, {d.group(4)} "
                    f"short, {d.group(5)} unfinished (its `loot delivery`; S's selections {d.group(7)} items, spawned "
                    f"there {d.group(8)})")
            whole = whole and d.group(1) == d.group(2)
        else:
            text = f"{who}: no `loot delivery` line (its session did not end there); {len(mine)} obligations on S"
            whole = False
        if s_sum:
            m = s_sum[1]
            text += f"; S to peer {peer}: {m.group(2)} made, {m.group(3)} completed, {m.group(4)} ended otherwise"
            whole = whole and m.group(2) == m.group(3)
        else:
            text += f"; S logged no `spill obligations to peer {peer}` line"
            whole = False
        counts.append(text)
        full = {m.group(1) for _l, m in log.find(C_FULFILLED) if m.group(3) is None or int(m.group(3)) >= int(m.group(2))}
        short = {m.group(1): (line, m) for line, m in log.find(C_FULFILLED)
                 if m.group(3) is not None and int(m.group(3)) < int(m.group(2))}
        differs = {m.group(1): (line, m) for line, m in log.find(C_DIFFERS)}
        expired = {m.group(1): (line, m) for line, m in log.find(C_EXPIRED)}
        sleepers = asleep_names(run, log)
        groups: dict[str, list[str]] = {}
        for line, m in mine:
            key = m.group(1)
            if key in full:
                continue
            name = carrier.get(key, "?")
            label = f"{key} (obligation {m.group(4)}) carrier {name}"
            if key in expired:
                eline, em = expired[key]
                why = em.group(2)
                # An unnamed carrier ((null), ?) is never matched by name: its sleep is not known here.
                if "not resolved" in why and name in sleepers and name not in UNNAMED:
                    kind = ASLEEP
                    detail = f"its copy slept here; {eline.cite()}"
                elif "not resolved" in why:
                    kind = NO_ROW
                    detail = f"S's entity is not bound here; {eline.cite()}"
                else:
                    kind = OTHER
                    detail = f"{why}; {eline.cite()}"
            elif key in differs:
                kind, detail = OTHER, f"this game's own run stood against S's selection; {differs[key][0].cite()}"
            elif key in short:
                sl, sm = short[key]
                kind, detail = OTHER, f"spawned {sm.group(3)} of {sm.group(2)}; {sl.cite()}"
            else:
                tried = log.find(re.compile(C_SPILL.format(key=re.escape(key))))
                kind, detail = OTHER, (tried[-1][0].cite() if tried else absent(log, f"`spill ... {key}`"))
            groups.setdefault(kind, []).append(f"{label}: {detail}")
        for kind in (ASLEEP, NO_ROW, OTHER):
            if kind in groups:
                why = {ASLEEP: "its carrier's copy is asleep here (a far player: no orb where it died)",
                       NO_ROW: "its carrier has no row here (S's entity is not bound here: nobody there sees the drop)",
                       OTHER: "other"}[kind]
                findings.append(Finding(f"{who}: {len(groups[kind])} obligation(s) not delivered in full, {why}",
                                        groups[kind]))
    spills = {m.group(1) for _, m in obliged}
    head = f"S obliged {len(spills)} spill(s), {len(obliged)} deliveries to the clients"
    status = PASS if whole and not findings else FAIL
    return Verdict("loot", status, "; ".join([head] + counts), obliged[0][0].cite(), findings, notes)


# --- cutscenes synced (the game repository's tools/scene_sync_check.py) ---------------------------------

SKIP_SCENES = re.compile(r"--dev_skip_scenes=(true|movies)")


def scene_tool(repo: Path | None):
    p = (repo / "tools" / "scene_sync_check.py") if repo else None
    if p is None or not p.is_file():
        return None
    name = "levelcheck_scene_sync_check"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # its dataclasses look their module up while it loads
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[name]
        raise
    return mod


def scene_line(log: Log, name: str, t: datetime | None) -> str:
    """The client's line a scene of scene_sync_check starts at (its `run@`/`conversation@` names unfolded)."""
    if name.startswith("run@"):
        want, kind = name[4:] + "#", "cutscene run"
    elif name.startswith("conversation@"):
        want, kind = "recipient " + name[len("conversation@"):], "scene local"
    else:
        want, kind = name, "Online: scene"
    for i, raw in enumerate(log.lines):
        if want in raw and kind in raw and (t is None or stamp(raw) == t):
            return log.line(i).cite()
    return absent(log, f"naming {name}" + (f" at {t.time()}" if t else ""))


def grade_scenes(run: Run, repo: Path | None, start_tol: float = 1.0, end_tol: float = 2.0) -> Verdict:
    tool = scene_tool(repo)
    if tool is None:
        return Verdict("cutscenes", NOT_TESTED, "no tools/scene_sync_check.py in the game repository")
    if "H" not in run.clients or "D" not in run.clients:
        return Verdict("cutscenes", NOT_TESTED, "the run has not both clients H and D")
    h, d = run.clients["H"], run.clients["D"]
    wh, wd = client_windows(h), client_windows(d)
    rows = tool.pair(tool.read_scenes(h.lines), tool.read_scenes(d.lines))
    both = []
    for r in rows:
        g = r.key[0]
        if g not in wh or g not in wd:
            continue
        s = r.h or r.d
        start = s.start
        lo = max(wh[g].start, wd[g].start)
        hi = min(wh[g].end, wd[g].end)
        if start is None or not (lo <= start <= hi):
            continue  # before the other client came, or after it left
        both.append(r)
    out, ok = tool.judge(both, start_tol, end_tol, None)
    findings = []
    poses = peer_poses(run.server) if run.server is not None else {}
    peers = {w: p for p, w in peer_numbers(run).items()}
    away = there = 0
    for r, text in zip(both, out):
        if text.startswith("FAIL"):
            g, name, n = r.key
            lines = [scene_line(h, name, r.h.start if r.h else None), scene_line(d, name, r.d.start if r.d else None)]
            what = text[5:]
            if (r.h is None) != (r.d is None):
                # Missing on one client: how far it stood from the other when the scene started there decides
                # whose the finding is (the test's placement or the game's sharing).
                missing = "D" if r.d is None else "H"
                start = (r.h or r.d).start
                where = players_apart(poses, peers, start)
                if where is None:
                    what += f"; where {missing} stood is not known (no pose of both on S within {POSE_AT_S.seconds} s)"
                else:
                    dist_, la, lb = where
                    if dist_ > SCENE_THERE:
                        away += 1
                        what = (f"{missing} was not there (test): {dist_:.0f} from the other player when the scene "
                                f"started ({name}); {what}")
                    else:
                        there += 1
                        what = (f"{missing} was there and did not get the scene (game): {dist_:.0f} from the other "
                                f"player when it started ({name}); {what}")
                    lines += [la.cite(), lb.cite()]
            findings.append(Finding(what, lines))
    counts = (f"{len(both) - len(findings)} of {len(both)} scene(s) both clients were in played together "
              f"(start within {start_tol} s, end within {end_tol} s); {len(rows) - len(both)} outside the time both "
              "were in the level"
              + (f"; missing on one client: {there} with both players there (game), {away} with the other player "
                 f"farther than {SCENE_THERE:.0f} (test)" if there or away else ""))
    notes = expected_scene_notes(h, rows)
    skipped = [p for p in run.result.get("peers", []) if isinstance(p, dict) and SKIP_SCENES.search(str(p.get("command", "")))]
    example = scene_line(h, both[0].key[1], (both[0].h or both[0].d).start) if both else ""
    if skipped:
        return Verdict("cutscenes", NOT_TESTED, "scenes are skipped in this run (--dev_skip_scenes): " + counts, example,
                       notes=([f"FAIL-shaped differences while skipping: {len(findings)}"] if findings else []) + notes)
    if not both:
        return Verdict("cutscenes", NOT_TESTED, counts, notes=notes)
    return Verdict("cutscenes", FAIL if findings else PASS, counts, example, findings, notes)


# A scene played on one client only: the other stood within SCENE_THERE of it on S's poses when the scene started
# (it was there: the game's sharing), or farther (it was not there: the test's placement; batch 10's tutorial_b,
# D at the level start 35 km from the end arena, failed every scene that only starts where a player stands).
SCENE_THERE = 1500.0
POSE_AT_S = timedelta(seconds=3)


def players_apart(poses: dict, peers: dict, at) -> tuple[float, Line, Line] | None:
    """H's and D's distance on S's poses nearest `at` (each within POSE_AT_S), with the two pose lines."""
    if at is None:
        return None
    near = []
    for who in ("H", "D"):
        ps = [p for p in poses.get(peers.get(who, -1), []) if abs(p[0] - at) <= POSE_AT_S]
        if not ps:
            return None
        near.append(min(ps, key=lambda p: abs(p[0] - at)))
    return math.dist(near[0][1], near[1][1]), near[0][2], near[1][2]


EXPECTED_SCENES = re.compile(r"Input script: LEVELPASS scenes expected: (.*)$")


def expected_scene_notes(h: Log, rows) -> list[str]:
    """The level pass's expected scenes (`LEVELPASS scenes expected: <name>@<how>[+entered], ...`, written from
    the level's data, levelpass.scenes_expected_line) against the scenes the clients played: how many were
    played on both, and the ones on neither, named (not reached: a scene whose start the run never came to
    is no failure by itself; one the sweep entered and that neither client played is said so)."""
    found = h.find(EXPECTED_SCENES)
    if not found:
        return []
    line, m = found[0]
    expected = []
    for part in m.group(1).split(", "):
        if "@" in part:
            name, how = part.rsplit("@", 1)
            expected.append((name, how))
    played = {}
    for r in rows:
        name = r.key[1][4:] if r.key[1].startswith("run@") else r.key[1]
        played[name] = played.get(name, 0) | (1 if r.h else 0) | (2 if r.d else 0)
    both = [n for n, _ in expected if played.get(n) == 3]
    one_side = [n for n, _ in expected if played.get(n) in (1, 2)]
    entered_none = [n for n, how in expected if how.endswith("+entered") and not played.get(n)]
    neither = [f"{n} ({how})" for n, how in expected if not played.get(n) and not how.endswith("+entered")]
    out = [f"expected from the level data: {len(expected)} ({line.cite()}); played on both clients {len(both)}, "
           f"on one {len(one_side)}, on neither {len(entered_none) + len(neither)}"]
    if entered_none:
        out.append("entered by the sweep but played on neither client: " + ", ".join(entered_none))
    if neither:
        out.append("not reached (on neither client): " + ", ".join(neither))
    return out


# --- the other player is the right skylander ---------------------------------------------------------------
#
# What a player sees of the other player: each client's puppet of each remote player, from the lines the
# games write: `Online: puppet <n> spawned <character> at ... (epoch e gen g, stand-in from Hero_<own>)` and
# `Online: puppet <n> epoch e gen g body <character> (stand-in) swap k tick t`, until `puppet <n> despawned`.
# A body is right when it is a real body (online/4-remote-bodies, setting online_remote_bodies: its line reads
# `body <character> (real)`; BODY takes it as soon as it is written so) of the character that player plays at
# that moment (its own `Online: own player row <Character> figure ...` line, the newest before the moment): a
# figure change or a swap is followed. A stand-in shown longer than STANDIN_MAX_S is a failure, as is a body of
# another character. Puppet <n> is peer <n>'s (each client's `connected as peer <n>`).
STANDIN_MAX_S = 5.0
PUPPET_SPAWNED = re.compile(r"Online: puppet (\d+) spawned (\S+) at .*\((?:epoch \d+ gen \d+, )?(stand-in|real)\b")
PUPPET_BODY = re.compile(r"Online: puppet (\d+) epoch \d+ gen \d+ body (\S+) \((stand-in|real)\b")
PUPPET_GONE = re.compile(r"Online: puppet (\d+) despawned")
OWN_ROW = re.compile(r"Online: own player row (\S+) figure ")


@dataclass
class BodySpan:
    character: str
    kind: str              # stand-in or real
    start: datetime
    end: datetime
    line: Line


def body_spans(log: Log) -> dict[int, list[BodySpan]]:
    """Each puppet's bodies on this client, one span per body line (the newest body until the next line,
    its despawn or the log's end)."""
    events: list[tuple[datetime, int, str, str, Line]] = []
    for rx, kind in ((PUPPET_SPAWNED, "body"), (PUPPET_BODY, "body"), (PUPPET_GONE, "gone")):
        for line, m in log.find(rx):
            if line.t is None:
                continue
            if kind == "gone":
                events.append((line.t, int(m.group(1)), "", "gone", line))
            else:
                events.append((line.t, int(m.group(1)), m.group(2), m.group(3), line))
    events.sort(key=lambda e: (e[0], e[4].no))
    last = log.last_time()
    out: dict[int, list[BodySpan]] = {}
    for t_, puppet, character, kind, line in events:
        spans = out.setdefault(puppet, [])
        if spans and spans[-1].end == datetime.max:
            spans[-1].end = t_
        if kind != "gone":
            spans.append(BodySpan(character, kind, t_, datetime.max, line))
    for spans in out.values():
        if spans and spans[-1].end == datetime.max:
            spans[-1].end = last or spans[-1].start
    return out


def own_characters(log: Log) -> list[tuple[datetime, str, Line]]:
    return [(line.t, m.group(1).lower(), line) for line, m in log.find(OWN_ROW) if line.t is not None]


def grade_bodies(run: Run) -> Verdict:
    peers = peer_numbers(run)
    findings: list[Finding] = []
    counts: list[str] = []
    example = ""
    standins = total = 0
    for who, log in run.clients.items():
        for puppet, spans in sorted(body_spans(log).items()):
            other = peers.get(puppet)
            plays = own_characters(run.clients[other]) if other in run.clients else []
            shown = standin = 0.0
            for sp in spans:
                secs = max(0.0, (sp.end - sp.start).total_seconds())
                shown += secs
                total += 1
                example = example or sp.line.cite()
                playing = next((c for t_, c, _l in reversed(plays) if t_ <= sp.start), plays[0][1] if plays else "")
                if sp.kind == "stand-in":
                    standin += secs
                    if secs > STANDIN_MAX_S:
                        standins += 1
                        findings.append(Finding(
                            f"{who} shows {other or 'peer ' + str(puppet)} as a stand-in {sp.character} for {secs:.0f} s "
                            f"(more than {STANDIN_MAX_S:.0f} s): a copy of {who}'s own skylander",
                            [sp.line.cite()]))
                if playing and sp.character.lower() != playing:
                    findings.append(Finding(
                        f"{who} shows {other or 'peer ' + str(puppet)} as {sp.character}, but that player plays {playing}",
                        [sp.line.cite(), next(l.cite() for t_, c, l in reversed(plays) if c == playing)]))
            counts.append(f"{who} sees {other or 'peer ' + str(puppet)}: {len(spans)} body line(s), "
                          f"{shown:.0f} s shown, {standin:.0f} s of it a stand-in")
    if total == 0:
        return Verdict("other player", NOT_TESTED, "no client showed another player (no `puppet <n> spawned` line)")
    return Verdict("other player", FAIL if findings else PASS,
                   "; ".join(counts) + f"; stand-ins over {STANDIN_MAX_S:.0f} s: {standins}", example, findings)


# --- the other player's moves (online_remote_moves; online/4-other-moves) -----------------------------------
#
# W32 (b). With online_remote_moves each client sends its hero's command with its poses (`moves: capture on`) and the
# other clients feed it to that player's real body: attacks (`puppet <p> attack <seq> taken at pose <s> (waited
# <ms>[, its pose not reached])` against the sender's presses `press <p>:<seq> slot <k> tick <t> (announced)`),
# jumps, the special and the block (`puppet <p> input <jump|power|block> at pose <s>`, `puppet <p> held 0x<a> ->
# 0x<b>`), the walk (the body steers itself; `puppet <p> drift <d> corrected (#<n>, pose <s>)` when it strays), the
# behaviour states (`puppet <p> states entered 0x..` against the sender's `hero states entered 0x..` near the same
# pose), shots (`puppet shot made`, their effects dropped: `puppet shot drop <what>`) and the knock-out (`puppet <p>
# down (ko, pose <s>)`, `back as <character> after <ms>`). Off (no `moves: capture on`): not tested.
MOVES_ON = re.compile(r"Online: moves: capture on ")
SENDER_PRESS = re.compile(r"Online: press (\d+):(\d+) slot \d+ tick \d+ \(announced\)")
BODY_ATTACK = re.compile(r"Online: puppet (\d+) attack (\d+) taken at pose (\d+) \(waited (\d+) ms(, its pose not reached)?")
HERO_STATES = re.compile(r"Online: hero states entered (0x[0-9A-F,x]+) \(pose (\d+)\)")
BODY_STATES = re.compile(r"Online: puppet (\d+) states entered (0x[0-9A-F,x]+) \(pose (\d+)\)")
BODY_DRIFT = re.compile(r"Online: puppet (\d+) drift (\d+) corrected \(#(\d+), pose (\d+)\)")
BODY_HELD = re.compile(r"Online: puppet (\d+) held 0x([0-9A-F]+) -> 0x([0-9A-F]+)")
BODY_EXPIRED = re.compile(r"Online: puppet (\d+) input expired")
BODY_INPUT = re.compile(r"Online: puppet (\d+) input (jump|power|block) at pose (\d+)")
SHOT_MADE = re.compile(r"Online: puppet shot made ")
SHOT_DROP = re.compile(r"Online: puppet shot drop (\S+)")
BODY_DOWN = re.compile(r"Online: puppet (\d+) down \(ko, pose (\d+)\)(?!:)")
BODY_BACK = re.compile(r"Online: puppet (\d+) back as (\S+) after (\d+) ms")
MOVES_STATE_POSES = 20      # a body's state set matches the sender's within this many poses (1 s at 20 Hz)
MOVES_DRIFT_PER_S = 1.0     # walking: under one correction a second (in any 10 s)


def grade_moves(run: Run) -> Verdict:
    """Each client's view of the other player's moves against what that player sent."""
    if not any(log.find(MOVES_ON) for log in run.clients.values()):
        return Verdict("other player's moves", NOT_TESTED, "online_remote_moves off: no client sent its moves (no "
                       "`moves: capture on`); the other player's body only slides to its poses")
    peers = peer_numbers(run)
    counts: list[str] = []
    findings: list[Finding] = []
    example = ""
    for who, log in sorted(run.clients.items()):
        for peer, sender in sorted(peers.items()):
            if sender == who or sender not in run.clients:
                continue
            slog = run.clients[sender]
            pressed = {int(m.group(2)): line for line, m in slog.find(SENDER_PRESS) if int(m.group(1)) == peer}
            taken = {int(m.group(2)): (line, m) for line, m in log.find(BODY_ATTACK) if int(m.group(1)) == peer}
            not_reached = [line for line, m in taken.values() if m.group(5)]
            missing = sorted(set(pressed) - set(taken))
            sent = [(int(m.group(2)), set(m.group(1).split(","))) for _l, m in slog.find(HERO_STATES)]
            shown = [(line, int(m.group(3)), set(m.group(2).split(","))) for line, m in log.find(BODY_STATES)
                     if int(m.group(1)) == peer]
            matched = sum(1 for _l, pose, hs in shown
                          if any(abs(pose - sp) <= MOVES_STATE_POSES and hs & shs for sp, shs in sent))
            drifts = [line for line, m in log.find(BODY_DRIFT) if int(m.group(1)) == peer]
            worst = 0
            for i, line in enumerate(drifts):
                if line.t is None:
                    continue
                n = sum(1 for other in drifts[i:] if other.t is not None and (other.t - line.t).total_seconds() < 10)
                worst = max(worst, n)
            inputs = [m.group(2) for _l, m in log.find(BODY_INPUT) if int(m.group(1)) == peer]
            # an `input expired` while a held button (non-zero held bits) was fed
            held_now, expired_held = False, []
            for i, raw in enumerate(log.lines):
                m = BODY_HELD.search(raw)
                if m and int(m.group(1)) == peer:
                    held_now = m.group(3) != "0"
                    continue
                m = BODY_EXPIRED.search(raw)
                if m and int(m.group(1)) == peer and held_now:
                    expired_held.append(log.line(i))
            downs = [line for line, m in log.find(BODY_DOWN) if int(m.group(1)) == peer]
            backs = [(line, m) for line, m in log.find(BODY_BACK) if int(m.group(1)) == peer]
            shots = log.find(SHOT_MADE)
            drops: dict[str, int] = {}
            for _l, m in log.find(SHOT_DROP):
                drops[m.group(1)] = drops.get(m.group(1), 0) + 1
            counts.append(
                f"{who} shows {sender}: attacks taken {len(taken)} of {len(pressed)} pressed"
                + (f" ({len(not_reached)} before their pose)" if not_reached else "")
                + f"; states {matched} of {len(shown)} matching {sender}'s within {MOVES_STATE_POSES} poses"
                + f"; inputs {', '.join(f'{k} {inputs.count(k)}' for k in ('jump', 'power', 'block') if k in inputs) or '0'}"
                + f"; drift corrections {len(drifts)} (at most {worst} in 10 s)"
                + f"; shots made {len(shots)}, drops {', '.join(f'{k} {n}' for k, n in sorted(drops.items())) or '0'}"
                + (f"; knock-outs {len(downs)}, back {len(backs)}" if downs or backs else ""))
            if not example and (taken or shown):
                example = (next(iter(taken.values()))[0] if taken else shown[0][0]).cite()
            if missing:
                findings.append(Finding(f"{who}: {len(missing)} of {sender}'s attack presses never taken by its body",
                                        [pressed[s].cite() for s in missing[:4]] +
                                        [absent(log, f"`puppet {peer} attack {missing[0]} taken`")]))
            if shown and sent and not matched:
                findings.append(Finding(f"{who}: none of {sender}'s body's {len(shown)} state sets matches a state "
                                        f"{sender} entered within {MOVES_STATE_POSES} poses",
                                        [shown[0][0].cite()]))
            if worst > MOVES_DRIFT_PER_S * 10:
                findings.append(Finding(f"{who}: {sender}'s body corrected {worst} times in 10 s (over one a second)",
                                        [drifts[0].cite()]))
            if expired_held:
                findings.append(Finding(f"{who}: {len(expired_held)} `input expired` while {sender}'s button was held",
                                        [x.cite() for x in expired_held[:4]]))
            if downs and len(backs) < len(downs):
                findings.append(Finding(f"{who}: {sender}'s body went down {len(downs)} time(s), back {len(backs)}",
                                        [downs[-1].cite(), absent(log, f"`puppet {peer} back as` after it")]))
    if not counts:
        return Verdict("other player's moves", NOT_TESTED, "no two clients with peer numbers to compare")
    return Verdict("other player's moves", FAIL if findings else PASS, "; ".join(counts), example, findings)


# --- enemies hurt players (W33) ------------------------------------------------------------------------------
#
# The level pass starts both heroes invulnerable (--dev_hero_invulnerable; its job is the level), so there the row is
# not tested and the page's head says so; enemy-damage-tb and enemy-knockout-tb run without it. Graded from the
# player-damage lines (online/5-player-damage-lines): on a client, its copy of S's enemy hitting its own hero (`hero hit
# by <Name#mID> <row> (<sweep|shot|other>, <dmg>)[ refused by this game's damage check] (S's attack, played here)`)
# and its health's drop (`own vitals <h>/<max> (was <h0>; hit by ...)`); on S, the same hit on that player's puppet
# (`sentinel guard refused a hit on sentinel/puppet entry <i> PeerPuppet_<p>#.. ... by <Name#mID> <row> (<kind>)`) and
# the health it reads back (`peer <p> vitals <h>/<max>`). Every hit S counted on a puppet wants one hit on that
# player's hero by the same enemy row within PAIR_HIT_S (one to one: a client hit is used once), and every drop of
# the own health wants S's `peer <p> vitals` with the same health within PAIR_VITALS_S.
INVULNERABLE_ARG = "--dev_hero_invulnerable=true"
HERO_HIT = re.compile(r"Online: hero hit by (\S+) (\S+) \((sweep|shot|other), ([^)]*)\)( refused by this game.s damage check)?")
PUPPET_HIT = re.compile(r"Online: sentinel guard refused a hit on sentinel/puppet entry \d+ PeerPuppet_(\d+)\S* .* by (\S+) "
                        r"(\S+) \((sweep|shot|other)\)")
OWN_VITALS = re.compile(r"Online: own vitals (-?[\d.]+)/(-?[\d.]+) \(was (-?[\d.]+); hit by ")
PEER_VITALS = re.compile(r"Online: peer (\d+) vitals (-?[\d.]+)/(-?[\d.]+)")
PAIR_HIT_S = 1.0
PAIR_VITALS_S = 1.5
ANY_VITALS = re.compile(r"Online: own vitals (-?[\d.]+)/(-?[\d.]+) \(was ([^;]+); hit by (\S+)")
KO_SCREEN = re.compile(r"Online: swap placement none reason \d+ \(KO; player 0")
DEFERRED_SHOT = re.compile(r"its deferred projectile made here")


def invulnerable_clients(folder: Path) -> list[str]:
    """The clients the runner started with --dev_hero_invulnerable=true (result.json's commands)."""
    try:
        r = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(p.get("peer")) for p in r.get("peers", []) or []
            if isinstance(p, dict) and INVULNERABLE_ARG in (p.get("command") or [])]


def invulnerable_head(folder: Path) -> list[str]:
    who = invulnerable_clients(folder)
    return [f"heroes invulnerable in this run ({', '.join(who)}): enemy damage not tested"] if who else []


def grade_player_damage(run: Run) -> Verdict:
    """S's hits on each player's puppet against that player's own hits and health."""
    name = "enemies hurt players"
    invulnerable = invulnerable_clients(run.folder)
    if invulnerable:
        return Verdict(name, NOT_TESTED, f"heroes invulnerable in this run ({', '.join(invulnerable)}): enemy damage "
                       "not tested; this is what hid it until 19 November (enemy-damage-tb and moves-ko-tb test it, "
                       "and a level check with --vulnerable)")
    if run.server is None:
        return Verdict(name, NOT_TESTED, "no server log in this run folder")
    peers = peer_numbers(run)
    s_hits = run.server.find(PUPPET_HIT)
    s_vitals = run.server.find(PEER_VITALS)
    counts, findings, example = [], [], ""
    any_hit = False
    for peer, who in sorted(peers.items()):
        log = run.clients.get(who)
        if log is None:
            continue
        hits = [(line, m) for line, m in log.find(HERO_HIT) if not m.group(5)]
        refused = [line for line, m in log.find(HERO_HIT) if m.group(5)]
        mine = [(line, m) for line, m in s_hits if int(m.group(1)) == peer]
        used: set[int] = set()
        unmatched = []
        for sl, sm in mine:
            best = None
            for k, (cl, cm) in enumerate(hits):
                if k in used or cm.group(2) != sm.group(3) or cl.t is None or sl.t is None:
                    continue
                d = abs((cl.t - sl.t).total_seconds())
                if d <= PAIR_HIT_S and (best is None or d < best[0]):
                    best = (d, k)
            if best is None:
                unmatched.append(sl)
            else:
                used.add(best[1])
        extra = [hits[k][0] for k in range(len(hits)) if k not in used]
        drops = [(line, m) for line, m in log.find(OWN_VITALS) if float(m.group(1)) < float(m.group(3))]
        late = []
        for line, m in drops:
            ok = any(int(sm.group(1)) == peer and abs(float(sm.group(2)) - float(m.group(1))) < 0.5 and sl.t
                     and line.t and abs((sl.t - line.t).total_seconds()) <= PAIR_VITALS_S for sl, sm in s_vitals)
            if not ok:
                late.append(line)
        kinds = {k: sum(1 for _l, m in hits if m.group(3) == k) for k in ("sweep", "shot", "other")}
        any_hit = any_hit or bool(hits or mine)
        vit = log.find(ANY_VITALS)
        unknown = [line for line, m in vit if m.group(4) == "unknown" and m.group(3) != "-" and
                   float(m.group(1)) < float(m.group(3))]
        lowest = min((float(m.group(1)) for _l, m in vit), default=None)
        top = max((float(m.group(2)) for _l, m in vit), default=None)
        kos = log.find(KO_SCREEN)
        counts.append(f"{who}: hit {len(hits)} time(s) ({', '.join(f'{k} {n}' for k, n in kinds.items() if n) or '-'}"
                      f"{f', {len(refused)} refused here' if refused else ''}); S counted {len(mine)} on its puppet, "
                      f"{len(mine) - len(unmatched)} matched one to one; health drops {len(drops)}"
                      + (f", {len(unknown)} by unknown" if unknown else "")
                      + (f"; lowest health {lowest:.0f}/{top:.0f}" if lowest is not None and top else "")
                      + (f"; knocked out {len(kos)} time(s), shown the placement screen" if kos else ""))
        if mine and not hits:
            findings.append(Finding(f"{who}: one-sided: S counted {len(mine)} hit(s) on its puppet and its hero logged "
                                    "no `hero hit by` at all", [mine[0][0].cite(), absent(log, "`hero hit by`")]))
        if unknown:
            findings.append(Finding(f"{who}: its health fell {len(unknown)} time(s) with `hit by unknown` (no enemy "
                                    "hit of S's within 3 s)", [x.cite() for x in unknown[:4]]))
        if not example and hits:
            example = hits[0][0].cite()
        if unmatched:
            findings.append(Finding(f"{who}: {len(unmatched)} hit(s) S counted on its puppet with no hit on its hero by "
                                    f"the same enemy within {PAIR_HIT_S:.0f} s", [x.cite() for x in unmatched[:4]]))
        if extra and mine:
            findings.append(Finding(f"{who}: {len(extra)} hit(s) on its hero S did not count on its puppet (doubled "
                                    "or S's copy missed them)", [x.cite() for x in extra[:4]]))
        if late:
            findings.append(Finding(f"{who}: {len(late)} health drop(s) S never read back (`peer {peer} vitals` with "
                                    f"that health within {PAIR_VITALS_S} s)", [x.cite() for x in late[:4]]))
    # Who was shown the placement screen of a knock-out (only the knocked-out player should be).
    shown_ko = [who for who, log in run.clients.items() if log.find(KO_SCREEN)]
    if shown_ko:
        counts.append(f"the knock-out's placement screen shown on: {', '.join(sorted(shown_ko))}")
    # Shots: graded only where the deferred-shot fix is in (`its deferred projectile made here`); else not tested.
    if not any(log.find(DEFERRED_SHOT) for log in run.clients.values()):
        counts.append("shots not tested: no client made a deferred projectile (the bomb throwers' shots are dropped "
                      "on the clients without online/5-replica-deferred-shots)")
    if not any_hit:
        return Verdict(name, NOT_TESTED, "no player was hit in this run (no `hero hit by` on a client, no hit S counted "
                       "on a puppet): " + "; ".join(counts))
    return Verdict(name, FAIL if findings else PASS, "; ".join(counts), example, findings)


# --- what no grader covers yet -------------------------------------------------------------------------------
#
# Said at the bottom of every page so the gaps are seen without asking. From the placeholder audit's CLASS 1
# (logs/placeholder-audit.txt, two players in tutorial_a, tutorial_b and ww1 with the session's defaults) and the
# owner's own list; a line goes when a grader covers it.
NOT_COVERED = (
    "the other player's sounds and effects (the moves row reads its commands, states and shots, not what plays)",
    "which skylander's attack lands on the server (it replays the presses on its own hero: reach and damage may "
    "differ), player hits and damage numbers as the other player sees them",
    "the HUD and party panel: portraits (a number today), health bars, names, level, hat, lives",
    "food and the wallet beyond the coins' gold lines",
    "the other player's revive (a resurrection) and its falls as seen on the screen",
    "enemy movement, animation and attack timing, enemies aiming at the partner, enemy missiles, freezes and "
    "other victim states",
    "the save: whether the session's progress reaches the host's real slot",
    "Spark Lock boards, Swap Zones, shops, turrets, containers and conversations left open, push blocks and "
    "moving platforms seen in different places, sub-map scenery",
    "a cutscene one screen keeps playing after the server stopped it, conversation lines skipped on one screen",
    "co-op difficulty scaling for two players",
    "levels, modes and paths the pass does not take (swap zones, later levels, rejoining)",
)


def not_covered_lines() -> list[str]:
    return ["", "what this check does NOT look at (no grader covers it yet):"] + [f"  - {x}" for x in NOT_COVERED]


# --- completable (the level pass's own verdict) --------------------------------------------------------

H_NEXT = re.compile(r"Input script: LEVELPASS phase next-level")
D_NEXT = re.compile(r"Input script: LEVELPASS D next level")


@dataclass
class LevelData:
    """The level's own data the page reads beside the logs (levelpass_holds): its held enemies by node key and
    its gates."""
    level: str
    holds: dict
    gates: list
    authored: dict = field(default_factory=dict)   # authored index -> (name, position): <level>-manifest.txt
    movers: list = field(default_factory=list)     # the level's re-arming mover spawners' names (levelpass_pieces)


def run_level(run: Run) -> str:
    """The level of a level pass's run: its case `levelpass-<level>[-no-end]` (result.json), else ""."""
    case = str(run.result.get("case") or "")
    if not case.startswith("levelpass-"):
        return ""
    level = case[len("levelpass-"):]
    return level[:-len("-no-end")] if level.endswith("-no-end") else level


def level_data(levelpass: Path | None, level: str) -> LevelData | None:
    """<levelpass>/<level>/logic.json and manifest.json read into LevelData; None when either is missing."""
    import levelpass_holds as lh
    if levelpass is None or not level:
        return None
    try:
        logic = json.loads((levelpass / level / "logic.json").read_text(encoding="utf-8"))
        manifest = json.loads((levelpass / level / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    disabled = [(f"{e.get('unit')}.{e.get('name')}", str(e.get("name")), str(e.get("unit")),
                 tuple(e["position"]) if isinstance(e.get("position"), list) and len(e["position"]) == 3 else None)
                for e in manifest.get("entities", []) or [] if isinstance(e, dict) and e.get("group") == "enemy"
                and str((e.get("details") or {}).get("_isEnabled", "")).startswith("false")]
    authored: dict = {}
    probe = levelpass / f"{level}-manifest.txt"
    if probe.is_file():
        import levelpass as lp
        try:
            for o in lp.parse_probe_manifest(probe.read_text(encoding="utf-8", errors="replace"), level).objects:
                zone, _, idx = o.ident.partition("#")
                if zone.lower() == level.lower() and idx.isdigit() and o.name:
                    authored[int(idx)] = (o.name, o.pos)
        except (OSError, ValueError):
            authored = {}
    import levelpass_pieces as lpc
    movers = sorted(k.split(".")[-1] for k, period in lpc.mover_spawners(logic, level).items() if period > 0)
    return LevelData(level, lh.holds_of(logic, disabled), lh.required_gates(manifest, logic, level), authored, movers)


# --- held enemies in a run (levelpass_holds: what the generator reads) -------------------------------------
#
# A placed key on S (`a<..>.<zone hash>.<0001xxxx>@<g>`) names its authored index in its low 16 bits (tutorial_b:
# a00000000.0629f659.00013057@2 is [tutorial_b#12375] Chompy in the probe's manifest); its name and place tie it
# to the level's node and so to its hold. An ambient hold (nothing in the logic releases it) is scenery: not
# counted. A hold whose release this run did not fire is listed, not graded. A held enemy whose release fired
# is graded as any other.
AUTHORED_KEY = re.compile(r"^a[0-9a-f]+\.[0-9a-f]{8}\.([0-9a-f]+)@\d+$")
NEAR_NODE = 300.0   # the probe's live place and the static reader's within this: the same object


def hold_of(data: LevelData | None, key: str, name: str):
    """The level's hold of S's enemy `key` (named `name` on S), or None."""
    if data is None or not data.holds:
        return None
    base = name.split("#")[0]
    m = AUTHORED_KEY.match(key)
    row = data.authored.get(int(m.group(1), 16) & 0xFFFF) if m else None
    cands = [h for h in data.holds.values() if h.name == (row[0] if row else base)]
    if row and cands:
        near = [h for h in cands if h.pos is not None and math.dist(h.pos, row[1]) <= NEAR_NODE]
        return min(near, key=lambda h: math.dist(h.pos, row[1])) if near else None
    return cands[0] if len(cands) == 1 else None


def released_here(run: Run, hold, dead_by_player: bool) -> Line | str | None:
    """Whether this run fired the hold's release: the pass acted on its root (any LEVELPASS line of H naming it),
    a lock's opening line, or the enemy died by a player (it took damage: released). A cited line, a reason, or
    None."""
    if dead_by_player:
        return "it died by a player on S"
    h = run.clients.get("H")
    for r in hold.releases:
        if not r.path:
            continue
        root = r.path[0]
        if r.act == "lock":
            line = gate_opened(run, root)
            if line:
                return line
        elif h is not None:
            hit = h.find(re.compile(r"Input script: LEVELPASS .*\b" + re.escape(root) + r"\b"))
            if hit:
                return hit[0][0]
    return None


# The pass's required phase (levelpass.plan_required): H's `LEVELPASS required r<k> <kind> <gate> (...)` before
# the step, or `... not performed: <why>`. Each gate's opening is read on each game apart: a client's `world gate
# <Name#id> open apply` (S's open applied there) or `backend key gate <Name#id> ... unlocked`, S's `world gate
# <Name#id> tombstoned` or `availability <Name#id> removed`, S's `backend gate.open applied <h> key <k>` (its own
# read of its gate settled open after a client's press: the press's `backend press held <Name#id> key <k>` on that
# client names the gate), and on H the step's own `wait_moved <Name#id> done`. S writes no `world gate ... open
# apply` (that is a client applying S's row): the key-gate run tutorial_b 20261119-065111 opened gate 1 on S at
# 06:54:11.795 and the page said "not on S".
# Gates of the same name (tutorial_b's two TutorialLockedGate) are told apart by their order along the level and
# the distinct ids each game opened.
REQ_LINE = re.compile(r"Input script: LEVELPASS required (r\d+) (\S+) (\S+)(?: not performed: (.*))?")
GATES_SKIPPED = re.compile(r"Input script: LEVELPASS gates not attempted \(--no-gates\)")


def when(line: Line) -> str:
    """A line's time of day to the millisecond, or ?."""
    return line.t.strftime("%H:%M:%S.%f")[:-3] if line.t is not None else "?"


GATE_APPLIED_S = re.compile(r"Online: backend gate\.open applied \S+ key (\S+)")


def server_gate_opens(run: Run, name: str) -> list[tuple[str, Line]]:
    """S's own opens of a gate of this name: each `backend gate.open applied <h> key <k>` whose request a client's
    `backend press held <name>#<id> key <k>` sent, as (that client and its <name>#<id>, S's line)."""
    if run.server is None:
        return []
    pressed = {}
    for who, log in run.clients.items():
        for _line, m in log.find(re.compile(r"Online: backend press held (" + re.escape(name) + r"#\d+) key (\S+)")):
            pressed.setdefault(m.group(2), f"{who}:{m.group(1)}")
    return [(pressed[m.group(1)], line) for line, m in run.server.find(GATE_APPLIED_S) if m.group(1) in pressed]


# A gate's lock state (W17: a Scripts.LockedObjectComponent gate opens in place, its origin never moves, so
# wait_moved never sees it). A gate's row is named by S's reference: `a<S's entity id, 8 hex>.<name hash>.<class>`
# (S's `authored obj <zone> <i> <index> class <class> entity 1 name <hash> ... mid <id hex> ... text <Name>` line).
# Every game logs the same authored list with its own entity ids, so S's reference names a gate by its authored
# index, and each game's own line for that index names its `<Name>#<id>` (ww1: Finale_DemonGate008 is S's and H's
# 0x93d, D's 0x95a). A client shows the opening as `backend key gate <S's ref>@<g> watched <p> (unlocked
# Scripts.<Lock>=<n>)`; S writes the gate's row once when the level starts (`world 2 <ref>@<g> written`) and again
# when the gate opens (batch 12 case 7, ww1: Finale_DemonGate008 S 12:54:21.128, H 12:54:21.454, D 12:54:21.363;
# PuzzleGateSawtooth001 12:56:09.599, 56:10.041, 56:10.016).
AUTHORED_GATE = re.compile(r"Online: authored obj (\S+) \d+ (\d+) class ([0-9a-f]{8}) entity \d+ name ([0-9a-f]{8}) "
                           r"name0 \S+ mid ([0-9a-f]+) .* text (\S+)$")
KEY_GATE_UNLOCKED = re.compile(r"Online: backend key gate (a[0-9a-f]{8}\.[0-9a-f]{8}\.[0-9a-f]{8})@\d+ watched \S+ "
                               r"\(unlocked ")
GATE_ROW_WRITTEN = re.compile(r"Online: world 2 (a[0-9a-f]{8}\.[0-9a-f]{8}\.[0-9a-f]{8})@\d+ written")


def authored_gates(log: Log, name: str) -> dict[tuple[str, str], tuple[str, str]]:
    """This game's gates of this name: (zone, authored index) -> (its reference, its `<name>#<id>`)."""
    out = {}
    for _line, m in log.find(AUTHORED_GATE):
        if m.group(6) == name:
            ident = int(m.group(5), 16)
            out[(m.group(1), m.group(2))] = (f"a{ident:08x}.{m.group(4)}.{m.group(3)}", f"{name}#{ident}")
    return out


def gate_refs(log: Log, name: str, server: Log | None = None) -> dict[str, str]:
    """The row references of the gates of this name (S's, from `server` when given, else this game's own), each to
    this game's `<name>#<id>` of the same authored gate."""
    own = authored_gates(log, name)
    named = authored_gates(server, name) if server is not None else own
    out = {}
    for key, (ref, _ident) in named.items():
        if key in own:
            out[ref] = own[key][1]
    return out


def lock_state_opens(log: Log, name: str, server: bool, s_log: Log | None = None) -> list[tuple[str, Line]]:
    """(<name>#<id>, the line) for each opening this game shows by the gate's lock state: on S a gate's row written
    again after its first write, on a client its `backend key gate <S's ref> ... (unlocked ...)` (`s_log`: S's log,
    whose references the rows carry; None: this game's own)."""
    refs = gate_refs(log, name, None if server else s_log)
    if not refs:
        return []
    out: list[tuple[str, Line]] = []
    if server:
        written: set[str] = set()
        for line, m in log.find(GATE_ROW_WRITTEN):
            ref = m.group(1)
            if ref in refs:
                if ref in written:
                    out.append((refs[ref], line))
                written.add(ref)
    else:
        out = [(refs[m.group(1)], line) for line, m in log.find(KEY_GATE_UNLOCKED) if m.group(1) in refs]
    return out


def gate_open_ids(log: Log, name: str, mover: bool = False, run: Run | None = None) -> list[Line]:
    """One line per distinct `<name>#<id>` this game shows opened, in time order: S's own and each client's lines,
    the lock state (lock_state_opens), on S with `run` also S's own applied opens (server_gate_opens: one that
    follows S's write of a gate's row within 2 s is that gate's)."""
    n = re.escape(name)
    rx = re.compile(r"(?:Online: (?:world gate (" + n + r"#\d+) (?:open apply|tombstoned)|availability (" + n +
                    r"#\d+) removed|backend key gate (" + n + r"#\d+)\S* unlocked)" +
                    (r"|Dev: wait_moved (" + n + r"#\d+) done \()" if mover else ")"))
    server = run is not None and log is run.server
    seen: dict[str, Line] = {}
    found = [(next(g for g in m.groups() if g), line) for line, m in log.find(rx)]
    found += lock_state_opens(log, name, server, run.server if run is not None else None)
    for ident, line in sorted(found, key=lambda x: (x[1].t or datetime.min, x[1].no)):
        seen.setdefault(ident, line)
    if server:
        rows = [(ident, line) for ident, line in lock_state_opens(log, name, True)]
        for who_ident, line in server_gate_opens(run, name):
            near = [ident for ident, row in rows if row.t is not None and line.t is not None and
                    timedelta(0) <= line.t - row.t <= timedelta(seconds=2)]
            seen.setdefault(near[-1] if near else "pressed " + who_ident, line)
    return sorted(seen.values(), key=lambda l: (l.t or datetime.min, l.no))


def gate_opened(run: Run, name: str) -> Line | None:
    """A line of any game that shows the gate opened (the first)."""
    for log in ([run.server] if run.server else []) + list(run.clients.values()):
        ids = gate_open_ids(log, name, mover=log.who == "H", run=run)
        if ids:
            return ids[0]
    return None


def key_gate_trail(run: Run, name: str, k: int) -> list[str]:
    """A key gate's carried key, in order (the k-th gate of that name): touched and following on H, what S
    logged for the key, the held press at the lock on H, S's decision, and D's own copy of the key."""
    h = run.clients.get("H")
    out: list[str] = []
    key = re.escape(name) + r"_key#\S+"
    picked = h.find(re.compile(r"Dev: pickup " + key + r" (done|failed)")) if h else []
    if len(picked) <= k or picked[k][1].group(1) != "done":
        out.append("key not carried on H: " + (picked[k][0].cite() if len(picked) > k else
                                                absent(h, f"`pickup {name}_key ... done`") if h else "no H log"))
    else:
        out.append(f"key touched and following on H: {picked[k][0].cite()}")
    # W25: the key by name, who carried it and when (a client's `carried key <ref> row carried by <p>/<e> here (key
    # <Name#id>)`), and S's `carried key <ref> row consumed (by <p>)`.
    if h is not None and len(picked) > k and picked[k][1].group(1) == "done":
        label = re.search(r"pickup (\S+_key#\d+)", picked[k][0].text)
        carried = h.find(re.compile(r"Online: carried key (\S+) row carried by (\d+)/\d+ here \(key "
                                    + re.escape(label.group(1)) + r"\)")) if label else []
        if carried:
            line, m = carried[0]
            who = peer_numbers(run).get(int(m.group(2)), f"peer {m.group(2)}")
            words = f"its key {label.group(1)}: carried by {who} from {when(line)}"
            if run.server is not None:
                gone = run.server.find(re.compile(r"Online: carried key " + re.escape(m.group(1))
                                                  + r" row consumed \(by (\d+)\)"))
                if gone:
                    words += f", consumed at {when(gone[0][0])} (S)"
            out.append(words + f" ({line.cite()})")
    if run.server is not None:
        # What S tracked or did with the key (its row, a carry, a claim), not its level-start volume line.
        s_key = [l for l, _m in run.server.find(re.compile(r"Online: .*\b" + key + r".*"))
                 if "single-fire volume" not in l.text]
        out.append(f"S for the key: {s_key[-1].cite()}" if s_key else
                   absent(run.server, f"naming {name}_key (other than its level-start volume)"))
        # One decision per press (`gate.open run` or `refused`), the k-th for the k-th gate of this name.
        decided = run.server.find(re.compile(r"Online: backend gate\.open (?:run|refused) .*"))
        if len(decided) > k:
            out.append(f"S's decision: {decided[k][0].cite()}")
        elif decided:
            out.append(f"S's decision: none for this press (S decided {len(decided)}, the last {decided[-1][0].cite()})")
    if h is not None:
        held = h.find(re.compile(r"Online: backend press (held|released|cancelled|refused|slow)[^\n]*" + re.escape(name)))
        out.append("press at the lock on H: " + "; ".join(l.cite() for l, _m in held[:3]) if held else
                   "no held press at the lock on H (" + absent(h, f"`backend press held ... {name}`") + ")")
        pressed = h.find(re.compile(r"Dev: interact " + re.escape(name) + r"_keylock#\S+ (done|failed).*"))
        if len(pressed) > k:
            out.append(f"interact on H: {pressed[k][0].cite()}")
        # The gate each press went to (`backend press held <Name#id> key <k>`): a press on a gate an earlier step
        # pressed is another gate's lock (tutorial_b 20261119-065111: step 2 pressed gate 1's lock by its name).
        gates = [(line, m.group(1)) for line, m in
                 h.find(re.compile(r"Online: backend press held (" + re.escape(name) + r"#\d+) key \S+"))]
        if len(gates) > k and gates[k][1] in [g for _l, g in gates[:k]]:
            out.append(f"the press went to {gates[k][1]}, the gate an earlier step pressed (another gate's lock, not "
                       f"this one's): {gates[k][0].cite()}")
    d = run.clients.get("D")
    if d is not None:
        d_key = [l for l, _m in d.find(re.compile(r"Online: .*\b" + key + r".*"))
                 if "single-fire volume" not in l.text and "unsupported carried_item" not in l.text]
        out.append(f"D's own key: {d_key[-1].cite()}" if d_key else
                   "D's own key: no line after its level start (still at home: nothing on D moves or removes it)")
    return out


def battle_gate_left(run: Run, data: "LevelData", gate) -> str:
    """For a battle gate S did not open: its fight's enemies still alive on S (by name: the openers and the
    encounters' actors) and whether H had them bound."""
    if run.server is None:
        return "no S log"
    names = [k.split(".")[-1] for k in gate.openers]
    keys = server_enemies(run.server)
    seen = client_enemies(run.clients["H"]) if "H" in run.clients else {}
    alive = []
    for key, k in keys.items():
        base = k.name.split("#")[0]
        if any(base == n or base.startswith(n + "_actor_") for n in names) and k.dead is None:
            c = seen.get(key)
            alive.append(f"{k.name} ({'bound on H' if c is not None and c.bound else 'not bound on H'})")
    return ("still alive on S: " + ", ".join(alive[:8])) if alive else \
        f"none of its fight's enemies ({', '.join(names[:4])}) alive on S: it never started, or its count is elsewhere"


def required_steps(run: Run, data: LevelData | None) -> tuple[list[str], list[str]]:
    """Each required gate (levelpass_holds.required_gates), in the pass's order: the ones not done (not performed,
    or not opened on S, H and D, with where it broke) and the ones done, in words; the first of the first list is
    the step the completable row names."""
    if data is None:
        return [], []
    h = run.clients.get("H")
    reqs = h.find(REQ_LINE) if h is not None else []
    games = [("S", run.server)] + sorted(run.clients.items(), key=lambda kv: (kv[0] != "H", kv[0]))
    games = [(w, lg) for w, lg in games if lg is not None]
    opened = {}
    shut, done = [], []
    nth: dict[str, int] = {}
    for g in [g for g in data.gates if g.required]:
        k = nth.get(g.name, 0)
        nth[g.name] = k + 1
        mine = [(line, m) for line, m in reqs if m.group(3) == g.name]
        req = mine[k] if len(mine) > k else None
        if req is None:
            shut.append(f"{g.name} ({g.kind}: {g.opener}): not performed by this pass (no `LEVELPASS required ... "
                        f"{g.name}` on H)")
            continue
        if req[1].group(4):
            shut.append(f"{g.name} ({g.kind}): not performed: {req[1].group(4)} ({req[0].cite()})")
            continue
        where = {}
        for who, lg in games:
            ids = opened.setdefault((who, g.name), gate_open_ids(lg, g.name, mover=who == "H", run=run))
            where[who] = ids[k] if len(ids) > k else None
        missing = [w for w, line in where.items() if line is None]
        trail = key_gate_trail(run, g.name, k) if g.kind == "key gate" else []
        if g.kind == "battle gate" and where.get("S") is None:
            trail.append(battle_gate_left(run, data, g))
        passed, faults, unchecked = gate_passes(run, g.name, k, where)
        walk = ([f"passed on foot by {', '.join(passed)}"] if passed else []) + unchecked
        if missing:
            opened_on = [f"{w} at {when(where[w])}" for w in where if where[w] is not None]
            shut.append(f"{g.name} ({g.kind}): attempted by the pass ({req[0].cite()}: its own line, no proof of anything the game did); opened on "
                        f"{', '.join(opened_on) or 'no game'}, not on {', '.join(missing)}"
                        + ("; " + "; ".join(faults + walk + trail) if faults or walk or trail else ""))
        elif faults:
            shut.append(f"{g.name} ({g.kind}): opened on {', '.join(where)} but " + "; ".join(faults)
                        + ("; " + "; ".join(walk + trail) if walk or trail else ""))
        else:
            done.append(f"{g.name} ({g.kind}): opened on {', '.join(where)}: "
                        + "; ".join(f"{w} at {when(line)} {line.cite()}" for w, line in where.items())
                        + ("; " + "; ".join(walk + trail) if walk or trail else ""))
    return shut, done


# W23: each client's walk through the gate (`Dev: pass_gate <Name#id> done (passed on foot: ...)` / `failed (not
# passed on foot: ...)` / `failed (not walkable straight: ...)`, the k-th of that name for the k-th gate). A walk
# that failed after the gate opened on that game is "gate open but not passable" (a finding); one that ended before
# it opened there, or a line that is no player's way (not walkable straight), is said and not counted either way.
PASS_GATE = re.compile(r"Dev: pass_gate (\S+#\d+) (done|failed) \((.*)\)")


def gate_passes(run: Run, name: str, k: int, where: dict) -> tuple[list[str], list[str], list[str]]:
    """(passed: "<who> at <t>", faults, not checked) for the k-th gate of this name on each client."""
    passed, faults, unchecked = [], [], []
    for who in sorted(run.clients, key=lambda w: (w != "H", w)):
        log = run.clients[who]
        mine = [(line, m) for line, m in log.find(PASS_GATE) if m.group(1).split("#")[0] == name]
        if len(mine) <= k:
            if log.find(PASS_GATE):
                unchecked.append(f"no walk through it on {who}")
            continue
        line, m = mine[k]
        if m.group(2) == "done":
            passed.append(f"{who} at {when(line)}")
        elif m.group(3).startswith("not walkable straight"):
            unchecked.append(f"passing not checked on {who}: {m.group(3)} ({line.cite()})")
        elif where.get(who) is not None and where[who].t is not None and line.t is not None and where[who].t <= line.t:
            faults.append(f"gate open but not passable on {who}: {m.group(3)} ({line.cite()}; opened there at "
                          f"{when(where[who])})")
        else:
            unchecked.append(f"{who}'s walk ended before the gate opened there ({line.cite()})")
    return passed, faults, unchecked


# "Reached the next level" is the game's own evidence on that client: a level loaded after the level under test
# (`Online: level <g> <path>: load` with a later generation) and that generation's start released there
# (`Online: barrier <g> released`: the hero in play). Never the script's own marker (`LEVELPASS phase
# next-level`, `LEVELPASS D next level`), which runs whatever its wait's outcome: batch 11 run 4 (tutorial_b
# 20261119-063529) logged it right after `waitfor online_level_after_mark timed out after 150.0 s`, and the page
# said "H yes". A timed-out wait for the level change is cited as the reason.
CLIENT_BARRIER = re.compile(r"Online: barrier (\d+) released at tick")
LEVEL_WAIT_TIMEOUT = re.compile(r"Input script: waitfor online_level_after_mark timed out after [\d.]+ s")


def next_level_reached(log: Log, level: str) -> tuple[Line | None, str]:
    """(the line that shows this client in play in the level after `level`, or None; why not, in words)."""
    loads = [(line, int(m.group(1)), m.group(2)) for line, m in log.find(LEVEL_LOAD)]
    # A late joiner comes into the level under test by its Welcome (`connected as peer <n> (..., level <g> <path>)`).
    joined = [(line, int(m.group(2)), m.group(3)) for line, m in log.find(CONNECTED)]
    under = [g for _l, g, path in loads + joined if level and path.split("/")[-1].lower() == level.lower()]
    if not under:
        return None, (f"no load of {level} on this game" if level else "the level under test is not known (no case name)")
    after = [(line, g, path) for line, g, path in loads if g > min(under)]
    timed_out = log.find(LEVEL_WAIT_TIMEOUT)
    if not after:
        return None, ("no level loaded after " + level + (f"; {timed_out[-1][0].cite()}" if timed_out else ""))
    load, g, path = after[0]
    played = [line for line, m in log.find(CLIENT_BARRIER) if int(m.group(1)) == g]
    if not played:
        return None, f"loaded {path.split('/')[-1]} ({load.cite()}) but its start was never released here (not in play)"
    return played[0], ""


def _levelpass():
    """The level pass module (its log readers), or None when it cannot be loaded beside this one."""
    try:
        import levelpass
        return levelpass
    except Exception:
        return None


def grade_completable(run: Run, data: LevelData | None = None) -> Verdict:
    """The level pass's verdict (result.json `levelpass`, written by `levelpass`) and each client's mark of
    the next level. Two players: a run without D does not show it (NOT TESTED)."""
    lp = run.result.get("levelpass") if isinstance(run.result.get("levelpass"), dict) else None
    if lp is None:
        return Verdict("completable", NOT_TESTED, "not a level pass (result.json has no `levelpass` verdict)")
    verdict = str(lp.get("verdict", ""))
    missed = [str(x) for x in lp.get("not_covered", []) or []]
    reached = {}
    why_not: dict[str, str] = {}
    level = run_level(run)
    for who in ("H", "D"):
        log = run.clients.get(who)
        if log is None:
            reached[who] = None
            continue
        reached[who], why_not[who] = next_level_reached(log, level)
    counts = (f"level pass {verdict or '?'}; next level reached (the level change and play on that game): " +
              ", ".join(f"{w} {'yes' if line else 'no'}" for w, line in reached.items() if w in run.clients))
    example = next((line.cite() for line in reached.values() if line), "")
    notes = [f"not covered: {m}" for m in missed]
    notes += [f"{w} did not reach the next level: {why}" for w, why in why_not.items() if reached.get(w) is None and why]
    # A client's mark that landed inside a load (levelpass.late_marks): its script ran a level late; the run's
    # failure is the test tool's (batch 12 run 2: D's mark 0.02 s after tutorial_b began loading).
    lpm = _levelpass()
    for who in ("H", "D"):
        log = run.clients.get(who)
        for lm in (lpm.late_marks("\n".join(log.lines)) if log is not None and lpm is not None else [])[:1]:
            notes.insert(0, f"tool fault: {lm.words(who)} ({log.line(lm.mark_no - 1).cite()})")
    # The pass teleports: it can reach the end without doing what a player must on the way.
    shut, done = required_steps(run, data)
    if data is None:
        counts += "; required steps on the way not checked (no level data for this run)"
    notes += [f"required, not done: {s}" for s in shut] + [f"required, done: {s}" for s in done]
    if verdict == "FAIL":
        return Verdict("completable", FAIL, counts, example, notes=notes)
    if verdict == "INCOMPLETE":
        return Verdict("completable", INCOMPLETE, counts, example, notes=notes)
    if "D" not in run.clients:
        return Verdict("completable", NOT_TESTED, counts + " (one client in this run: two players not shown)", example,
                       notes=notes)
    gates_skipped = bool(run.clients.get("H") and run.clients["H"].find(GATES_SKIPPED))
    if verdict == "PASS" and all(reached.values()):
        if shut and gates_skipped:
            return Verdict("completable", BY_TELEPORT, f"completable by teleport; gates not attempted (--no-gates); "
                           f"required steps not performed: {len(shut)}; {counts}", example, notes=notes)
        if shut:
            return Verdict("completable", BY_TELEPORT, f"completable by teleport; first step not done: {shut[0]}; "
                           f"required steps not done: {len(shut)} of {len(shut) + len(done)}; {counts}", example,
                           notes=notes)
        if data is not None and done:
            return Verdict("completable", PASS, f"completable by play: {len(done)} required step(s) performed, every "
                           f"gate opened on {', '.join(['S'] + sorted(run.clients, key=lambda w: (w != 'H', w)))}; "
                           f"{counts}", example, notes=notes)
        return Verdict("completable", PASS, counts, example, notes=notes)
    missing = [w for w, line in reached.items() if line is None and w in run.clients]
    return Verdict("completable", FAIL, counts, example, [Finding(
        f"{', '.join(missing)} did not reach the next level", [f"{w}: {why_not.get(w, '')}" for w in missing])], notes)


# --- the page ------------------------------------------------------------------------------------------


def grade(folder: Path, repo: Path | None, levelpass: Path | None = None) -> list[Verdict]:
    """Every row of the page; `levelpass` (the [levelpass] folder) gives the level's own data: its required
    gates (completable) and its held enemies (a fight that lands nothing)."""
    run = load_run(folder)
    data = level_data(levelpass, run_level(run))
    together = grade_together(run)
    rows = [grade_completable(run, data), grade_lobby(run), grade_slot(run), grade_bodies(run), together,
            grade_scenes(run, repo), grade_enemies(run, data), grade_pickups(run), grade_coins(run), grade_loot(run),
            grade_objectives(run), grade_pieces(run, data), grade_moves(run), grade_player_damage(run)]
    # The enemies and the loot are read where both players stand only when D followed H: each says which.
    followed = bool(run.clients.get("D") and run.clients["D"].find(FOLLOW_ON))
    if together.status in (PASS, FAIL):
        where = f"read with D following H: {together.counts}"
    elif followed:
        # D's follow ran, but the together row had nothing to read (batch 11 run 3: H hung before any stop).
        where = f"D followed H (its `auto follow ... (script)` line), but where both stood was not read: {together.counts}"
    else:
        where = ("read with D where it entered the level (no follow in this run): D's enemies and loot far from H "
                 "are asleep there")
    for v in rows:
        if v.name in ("enemies", "loot"):
            v.notes.append(where)
    # A sweep that skipped steps for time: each NOT TESTED row those steps would have tested says so (W13).
    skipped = skipped_acts(run.clients.get("H"))
    for v in rows:
        mine = [s for s in skipped if v.name in KIND_ROWS.get(s[1], ())]
        # Not for a row the run turned off (shared coins or loot off, scenes skipped): time made no difference there.
        off = re.search(r"\boff\b|--dev_skip_scenes", v.counts)
        if v.status == NOT_TESTED and mine and not off:
            v.notes.insert(0, f"not tested partly for time: the sweep's deadline skipped {len(mine)} step(s) that "
                              f"test it: " + ", ".join(f"{label} {kind} {name}" for label, kind, name, _l in mine[:6])
                           + f" ({mine[0][3].cite()})")
    return rows


def skipped_head(h: Log | None) -> list[str]:
    """The page's head line for a sweep that skipped steps for time, with the NOT TESTED rows they would test."""
    skipped = skipped_acts(h)
    if not skipped:
        return []
    rows = sorted({r for _l, kind, _n, _x in skipped for r in KIND_ROWS.get(kind, ())})
    return [f"the sweep skipped {len(skipped)} act(s) for time ({skipped[0][3].cite()}); rows they would test: "
            f"{', '.join(rows)} (a NOT TESTED row among them says so)"]


def run_head(folder: Path) -> list[str]:
    """The page's head from the run's result.json: the case, its status, minutes, build snapshot, runner."""
    try:
        r = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["run: no result.json"]
    prov = r.get("provenance") if isinstance(r.get("provenance"), dict) else {}
    secs = r.get("seconds")
    return [f"case {r.get('case', '?')}: runner status {r.get('status', '?')}, "
            f"{(secs / 60):.1f} min" if isinstance(secs, (int, float)) else f"case {r.get('case', '?')}",
            f"build snapshot {prov.get('build') or '?'}, runner {prov.get('runner') or '?'}",
            *figures_head(folder),
            *invulnerable_head(folder),
            *skipped_head(read_log(folder, "H/game.log", "H")),
            *route_head(folder)]


# W24: a sweep target with no floor a player can stand on near it (a trigger in the air: tutorial_b's rail-ride end
# trigger, auth:tutorial_b:8394) is not visited: the route teleport says `failed (no floor: ...)`; and a hero held too
# long at the same spot twice is taken out to the last ground it stood on. The page names both.
NO_FLOOR = re.compile(r"Dev: route teleport \S+ failed \((no floor: .*)\) \(target (.+)\)$")
TAKEN_OUT = re.compile(r"Dev: route held too long at the same spot twice \(([^)]*)\): (taken out to the last ground it "
                       r"stood on \S+|no ground it stood on is known, left there)")
VISIT_OR_ACT = re.compile(r"Input script: LEVELPASS (?:visit (v\d+)|act (v\d+_\d+) \S+) .*?(?:ref (\S+))?$")


# W27b: a target in the air the level reaches by a launch pad's ride: its scripted run (`cutscene run <Name#id> starts
# here`) begins on that client between a `launch pad <pad>: launched` and that ride's end (`launch_pad <pad> done|
# failed`).
LAUNCHED = re.compile(r"Dev: launch pad (\S+): launched")
RIDE_END = re.compile(r"Dev: launch_pad (\S+) (?:done|failed)")
RUN_START = re.compile(r"Online: cutscene run (\S+?)#\d+ starts here")


def ride_reached(log: Log | None) -> dict[str, tuple[str, Line]]:
    """The names whose scripted run began during a launch ride on this client: name -> (the pad, the run's line)."""
    out: dict[str, tuple[str, Line]] = {}
    if log is None:
        return out
    riding = ""
    for i, raw in enumerate(log.lines):
        m = LAUNCHED.search(raw)
        if m:
            riding = m.group(1)
            continue
        if RIDE_END.search(raw):
            riding = ""
            continue
        m = RUN_START.search(raw)
        if m and riding and m.group(1) not in out:
            out[m.group(1)] = (riding, log.line(i))
    return out


# W31: the follow's rescue (client/auto_follow.cpp): a follower the game put back twice after a move, taken back to
# its own last ground; and the game's own put-backs (`Stuck falling: player put back`) on that client.
FOLLOW_RESCUED = re.compile(r"Online: auto follow \d+: rescued: put back \d+ times since the move to (\S+); back to "
                            r"its own last ground (\S+)")
PUT_BACK = re.compile(r"Stuck falling: player put back after")


def route_head(folder: Path) -> list[str]:
    """The page's lines for targets not visited (no floor) and heroes taken out of a stuck spot, per client."""
    out = []
    for who in ("H", "D"):
        log = read_log(folder, f"{who}/game.log", who)
        if log is None:
            continue
        label, missed, taken, rescued, put_backs, first_put = "", [], [], [], 0, None
        for i, raw in enumerate(log.lines):
            m = FOLLOW_RESCUED.search(raw)
            if m:
                rescued.append(f"from {m.group(1)} to {m.group(2)} ({log.line(i).cite()})")
                continue
            if PUT_BACK.search(raw):
                put_backs += 1
                first_put = first_put or log.line(i)
                continue
            m = VISIT_OR_ACT.search(raw)
            if m:
                label = m.group(1) or m.group(2)
                continue
            m = NO_FLOOR.search(raw)
            if m:
                missed.append(f"{label + ' ' if label else ''}{m.group(2)} ({m.group(1)}; {log.line(i).cite()})")
                continue
            m = TAKEN_OUT.search(raw)
            if m:
                taken.append(f"at {m.group(1)}: {m.group(2)} ({log.line(i).cite()})")
        rode = ride_reached(log)
        for name, (pad, line) in rode.items():
            out.append(f"reached by the launch ride on {who}: {name} (the ride of {pad}; {line.cite()})")
        missed = [m for m in missed if not any(m.split(" ", 1)[-1].startswith(n + "#") for n in rode)]
        if missed:
            out.append(f"not visited on {who}: no floor (a trigger in the air): " + "; ".join(missed[:4])
                       + (f"; and {len(missed) - 4} more" if len(missed) > 4 else ""))
        if taken:
            out.append(f"{who} held too long at the same spot twice, {len(taken)} time(s): " + "; ".join(taken[:3]))
        if rescued:
            out.append(f"{who} rescued by the follow (put back twice after a move, taken to its own last ground), "
                       f"{len(rescued)} time(s): " + "; ".join(rescued[:3]))
        if put_backs >= 2:
            out.append(f"{who} put back by the game {put_backs} times (`Stuck falling: player put back`; the first "
                       f"{first_put.cite()})")
    return out


PLACE_LINE = re.compile(r"^\s*place (\d+) .*?([^/\\]+)\.dump\s*$")


def figures_head(folder: Path) -> list[str]:
    """The page's line naming each player's figures (W21), from its script's `place` lines; a Top and a Bottom of
    one name are a swapper."""
    out = []
    for who in ("H", "D"):
        script = folder / who / "script.txt"
        if not script.is_file():
            continue
        names = [m.group(2) for m in map(PLACE_LINE.match, script.read_text(encoding="utf-8", errors="replace")
                                         .splitlines()) if m]
        if not names:
            continue
        if len(names) == 2 and names[0].endswith(" Top") and names[1].endswith(" Bottom") and \
                names[0][:-4] == names[1][:-7]:
            out.append(f"{who} {names[0][:-4]} (a swapper: {names[0]} + {names[1]})")
        else:
            out.append(f"{who} {' + '.join(names)}")
    return [f"figures: {'; '.join(out)}"] if out else []


def overall(verdicts: list[Verdict]) -> str:
    """The page's one line: FAIL (and which rows) when a grader failed, INCOMPLETE when the pass did not cover
    its plan, PASS only when every row passed; with every row NOT TESTED named next to it, so nobody reads the
    rest as passed."""
    untested = [v.name for v in verdicts if v.status == NOT_TESTED]
    teleport = [v.name for v in verdicts if v.status == BY_TELEPORT]
    tail = (f"; NOT TESTED: {', '.join(untested)}" if untested else "") + \
        (f"; BY TELEPORT: {', '.join(teleport)}" if teleport else "")
    if any(v.status == FAIL for v in verdicts):
        return "FAIL: " + ", ".join(v.name for v in verdicts if v.status == FAIL) + \
            ("; INCOMPLETE" if any(v.status == INCOMPLETE for v in verdicts) else "") + tail
    if any(v.status == INCOMPLETE for v in verdicts):
        return "INCOMPLETE" + tail
    if untested:
        return "NOT ALL TESTED: " + ", ".join(untested) + (f"; BY TELEPORT: {', '.join(teleport)}" if teleport else "")
    return "PASS" if not teleport else "BY TELEPORT: " + ", ".join(teleport)


def page(folder: Path, verdicts: list[Verdict], head: list[str] | None = None) -> str:
    out = [f"level check: {folder}", *(head or []), f"overall: {overall(verdicts)}", ""]
    for v in verdicts:
        out += v.page_lines()
    more = [(v, f) for v in verdicts for f in v.findings[1:]]
    if more:
        out += ["", f"every other finding ({len(more)}):"]
        for v, f in more:
            out.append(f"  {v.name}: {f.what}")
            out += [f"      {x}" for x in f.lines]
    out += not_covered_lines()
    out += [f"  - {name}: no ground a player can stand on near it (a target in the air): the level reaches it "
            "another way (a ride, a launch, a script that moves it) the pass does not take; not covered by the pass"
            for name in no_floor_targets(folder)]
    return "\n".join(out)


def no_floor_targets(folder: Path) -> list[str]:
    """W27: the targets the route refused for having no floor near them (`route teleport ... failed (no floor: ...)
    (target <Name#id>)`) on any client, each once, by name without its id."""
    seen: list[str] = []
    rode: set[str] = set()
    for who in ("H", "D"):
        rode |= set(ride_reached(read_log(folder, f"{who}/game.log", who)))
    for who in ("H", "D"):
        log = read_log(folder, f"{who}/game.log", who)
        for _line, m in (log.find(NO_FLOOR) if log is not None else []):
            if not re.fullmatch(r"\S+#\d+", m.group(2)):
                continue  # a fight's area or an asleep target's stored position: no entity of the level by name
            name = m.group(2).split("#")[0]
            if name not in seen and name not in rode:
                seen.append(name)
    return seen


# --- the static table: every level's end and mechanics from its data, no game ----------------------------

LEVEL_TYPES = (  # by the level's name (the game's own naming)
    (re.compile(r"^town_state"), "hub"),
    (re.compile(r"^town_"), "hub side game"),
    (re.compile(r"^(pvp|ringout)_"), "versus arena"),
    (re.compile(r"^(survival|sv)_"), "survival arena"),
    (re.compile(r"^hc_"), "heroic challenge"),
    (re.compile(r"^lp_puzzle"), "lock puzzle"),
    (re.compile(r"_speedster$"), "swap zone"),
    (re.compile(r"^ap_"), "adventure pack"),
)
TYPE_ORDER = ["story", "adventure pack", "hub", "swap zone", "heroic challenge", "lock puzzle", "hub side game",
              "survival arena", "versus arena"]
STATIC_COLUMNS = [("switches/mini-games", "A"), ("fetch hand-ins", "B"), ("thrown objects", "C"),
                  ("turrets/vehicles", "D"), ("bosses", "F"), ("rides", "G")]
SPAWNERS = re.compile(r"SpawnerComponentData$|CEncounterManagerComponentData$")


def level_type(level: str) -> str:
    return next((name for rx, name in LEVEL_TYPES if rx.search(level)), "story")


@dataclass
class StaticRow:
    level: str
    kind: str
    ends: str          # how the level ends, in words
    needs: str         # what the pass cannot do at the end ('' when it can)
    on_way: str        # families on the end's path
    counts: dict[str, int]
    spawners: int
    probe: bool
    detail: str        # the full reason for an end the pass cannot carry out


def static_row(folder: Path, level: str) -> StaticRow:
    import levelpass_end as le
    logic = json.loads((folder / level / "logic.json").read_text(encoding="utf-8"))
    nodes, _ = le.read_logic(logic)
    counts = {fam: 0 for _, fam in STATIC_COLUMNS}
    spawners = 0
    for n in nodes.values():
        fam = le.family_of(n.components)
        if fam in counts:
            counts[fam] += 1
        spawners += any(SPAWNERS.search(c) for c in n.components)
    chains = logic.get("level_end") or []
    end = le.derive_end(chains, logic, level) if chains else None

    def name(k: str) -> str:
        return nodes[k].name if k in nodes else k.split(".")[-1]
    needs = detail = ""
    if end is None:
        ends = "no level end in the data"
    elif end.kind == le.TRIGGER:
        ends = f"enter a trigger: {name(end.trigger)}"
    elif end.kind == le.FIGHT:
        ends = (f"win a fight: {name(end.counter)} ({end.count} deaths)" if end.counter else "kill one enemy: "
                + ", ".join(name(k) for k in end.counted[:2]))
    elif end.kind == le.TIMED:
        ends = "on its own (no player act found)"
    else:
        fam, key, _when = end.blockers[0]
        ends = "the pass cannot end it"
        needs = (f"{le.FAMILIES[fam]}: {name(key)}" if fam in le.FAMILIES else f"unknown: {name(key)}")
        others = sorted({f for f, _k, _w in end.blockers[1:] if f != fam})
        if others:
            needs += " (also " + ", ".join(le.FAMILIES.get(f, "unknown") for f in others) + ")"
        detail = end.why
    on_way = ", ".join(sorted({le.FAMILIES[f] for f, _k in end.on_path})) if end and end.on_path else ""
    return StaticRow(level, level_type(level), ends, needs, on_way, counts, spawners,
                     (folder / f"{level}-manifest.txt").is_file(), detail)


def summary_line(rows: list[StaticRow]) -> str:
    """The table's one line for the owner: the levels with an end in their data that the pass can end today,
    those it cannot (per reason), those whose end is unknown (with the node)."""
    with_end = [r for r in rows if r.ends != "no level end in the data"]
    can = [r for r in with_end if not r.needs]
    unknown = [r for r in with_end if r.needs.startswith("unknown")]
    cannot: dict[str, list[str]] = {}
    for r in with_end:
        if r.needs and not r.needs.startswith("unknown"):
            cannot.setdefault(r.needs.split(":")[0], []).append(r.level)
    own = [r.level for r in can if r.ends.startswith("on its own")]
    return ("Today: of the " + str(len(with_end)) + " levels with an end in their data, the pass can end "
            + str(len(can)) + " (" + ", ".join(r.level for r in can)
            + (f"; {', '.join(own)} by waiting, it ends on its own" if own else "") + "); it cannot end "
            + str(sum(len(v) for v in cannot.values())) + " ("
            + "; ".join(f"{k} {len(v)}: {', '.join(v)}" for k, v in sorted(cannot.items(), key=lambda x: (-len(x[1]), x[0])))
            + "); unknown " + str(len(unknown)) + " ("
            + ", ".join(f"{r.level} at {r.needs.split(': ', 1)[1]}" for r in unknown) + ").")


def static_table(folder: Path) -> str:
    levels = sorted(p.parent.name for p in folder.glob("*/logic.json"))
    rows = [static_row(folder, lv) for lv in levels]
    rows.sort(key=lambda r: (TYPE_ORDER.index(r.kind) if r.kind in TYPE_ORDER else 99, r.level))
    heads = ["level", "type", "how the level ends", "what the pass cannot do at the end", "on the way to the end",
             *(h for h, _ in STATIC_COLUMNS), "enemy spawners", "probe data"]
    cells = [[r.level, r.kind, r.ends, r.needs or "-", r.on_way or "-", *(str(r.counts[f]) for _, f in STATIC_COLUMNS),
              str(r.spawners), "yes" if r.probe else "no"] for r in rows]
    width = [max(len(h), *(len(c[i]) for c in cells)) for i, h in enumerate(heads)]
    line = lambda c: " | ".join(x.ljust(width[i]) for i, x in enumerate(c)).rstrip()  # noqa: E731
    by_end = {}
    for r in rows:
        k = ("cannot end it" if r.needs else r.ends.split(":")[0])
        by_end[k] = by_end.get(k, 0) + 1
    out = [
        f"Every level's end and mechanics, read from the level data in {folder} (no game). {len(rows)} levels.",
        "How the level ends: what the level pass would do at the end (enter a trigger, win a fight, wait), found by",
        "  walking the level's own telegrams back from its map load. 'The pass cannot end it': the end needs what the",
        "  next column says (a mini-game's result, a turret, a boss, a ride, a menu choice), or 'unknown' at the node",
        "  where the reader stopped. Counts: objects of that kind in the level. Probe data: the pass can run (yes/no).",
        summary_line(rows),
        "",
        line(heads),
        line(["-" * w for w in width]),
        *(line(c) for c in cells),
        "",
        "Totals by how the level ends: " + ", ".join(f"{k} {v}" for k, v in sorted(by_end.items(), key=lambda x: -x[1])),
        "",
        "Why the pass cannot end these levels (the node and the telegram the reader stopped at):",
    ]
    out += [f"  {r.level}: {r.detail}" for r in rows if r.detail]
    return "\n".join(out)
