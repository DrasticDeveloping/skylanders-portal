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
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PASS, FAIL, NOT_TESTED = "PASS", "FAIL", "NOT TESTED"
INCOMPLETE = "INCOMPLETE"   # completable only: the level pass did not cover its plan (never a pass)

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
        return Line(self.path, i + 1, raw[k:].rstrip(), stamp(raw))

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
    p = run / rel
    if not p.is_file():
        return None
    return Log(who, rel.replace("\\", "/"), p.read_text(encoding="utf-8", errors="replace").splitlines())


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

S_PLACED = re.compile(r"Online: enemy (placed|run-time spawn) " + KEY + r" as (\S+)")
S_DEAD = re.compile(r"Online: enemy " + KEY + r" dead by ")
S_STREAMED = re.compile(r"Online: enemy " + KEY + r" streamed out on the server")
C_ENEMY = re.compile(r"Online: enemy (spawn|replica|skipped|life|dead|retired alive|retired|bind) " + KEY + r"(.*)")


@dataclass
class EnemyKey:
    key: str
    gen: int
    name: str
    placed: Line
    dead: Line | None = None
    streamed: Line | None = None


def server_enemies(log: Log) -> dict[str, EnemyKey]:
    keys: dict[str, EnemyKey] = {}
    for line, m in log.find(S_PLACED):
        if m.group(2) not in keys:
            keys[m.group(2)] = EnemyKey(m.group(2), int(m.group(3)), m.group(4), line)
    for line, m in log.find(S_DEAD):
        k = keys.get(m.group(1))
        if k and k.dead is None:
            k.dead = line
    for line, m in log.find(S_STREAMED):
        k = keys.get(m.group(1))
        if k and k.streamed is None:
            k.streamed = line
    return keys


@dataclass
class ClientEnemy:
    attach: Line | None = None    # spawn, bind (resolved), replica, skipped, life, dead, retired
    pending: Line | None = None   # bind (pending past grace: missing)
    death: Line | None = None     # life alive -> dead, skipped (dead before bind), dead by
    retired_alive: Line | None = None
    last: Line | None = None


def client_enemies(log: Log) -> dict[str, ClientEnemy]:
    out: dict[str, ClientEnemy] = {}
    for line, m in log.find(C_ENEMY):
        kind, key, rest = m.group(1), m.group(2), m.group(4)
        c = out.setdefault(key, ClientEnemy())
        c.last = line
        if kind == "bind" and "pending past grace" in rest:
            c.pending = c.pending or line
            continue
        c.attach = c.attach or line
        if (kind == "life" and "-> dead" in rest) or (kind == "skipped" and "dead before bind" in rest) or kind == "dead":
            c.death = c.death or line
        if kind == "retired alive":
            c.retired_alive = c.retired_alive or line
    return out


def grade_enemies(run: Run) -> Verdict:
    if run.server is None:
        return Verdict("enemies", NOT_TESTED, "no server log in this run folder")
    keys = server_enemies(run.server)
    if not keys:
        return Verdict("enemies", NOT_TESTED, "S logged no `enemy placed` or `enemy run-time spawn` line")
    placed = sum(1 for k in keys.values() if "placed" in k.placed.text)
    counts = [f"S {len(keys)} ({placed} placed, {len(keys) - placed} run-time), "
              f"{sum(1 for k in keys.values() if k.dead)} died on S"]
    findings: list[Finding] = []
    for who, log in run.clients.items():
        wins = client_windows(log)
        seen = client_enemies(log)
        n_exp = n_att = n_deaths = n_deaths_ok = 0
        for k in keys.values():
            w = wins.get(k.gen)
            if w is None:
                continue
            gone = k.dead or k.streamed
            if k.placed.t is None or k.placed.t > w.end - BIND_GRACE or (gone and gone.t and gone.t < w.start):
                continue  # placed too late for this client, or gone before it came
            n_exp += 1
            c = seen.get(k.key)
            if c is None or (c.attach is None and c.pending is None):
                findings.append(Finding(f"{who} never attached {k.key} ({k.name})", [
                    k.placed.cite(), absent(log, f"for {k.key}")]))
                continue
            if c.attach is None:
                findings.append(Finding(f"{who} never found {k.key} ({k.name}): S had it, {who} looked for it "
                                        f"and never bound it", [k.placed.cite(), c.pending.cite()]))
                continue
            n_att += 1
            if k.dead and k.dead.t and w.start <= k.dead.t <= w.end - DEATH_GRACE:
                n_deaths += 1
                if c.death is not None:
                    n_deaths_ok += 1
                else:
                    findings.append(Finding(f"{who} never saw {k.key} ({k.name}) die: dead on S, alive here", [
                        k.dead.cite(), (c.retired_alive or c.last).cite()]))
            elif c.death is not None and k.dead is None and k.streamed is None:
                findings.append(Finding(f"{who}: {k.key} ({k.name}) dead here only, S never logged its death", [
                    c.death.cite(), absent(run.server, f"`enemy {k.key} dead by`")]))
        if wins:
            counts.append(f"{who}: attached {n_att} of {n_exp} it should have, deaths seen {n_deaths_ok} of "
                          f"{n_deaths}")
        else:
            counts.append(f"{who}: in no level")
    first = next(iter(keys.values())).placed.cite()
    return Verdict("enemies", FAIL if findings else PASS, "; ".join(counts), first, findings)


# --- pickups (and the coins slot) ----------------------------------------------------------------------

S_APPLIED = re.compile(r"Online: collect applied ([0-9a-f]{8}) for peer (\d+)")
COLLECT_NATIVE = r"Online: collect native {id} "
PARTNER_GOT = r"Online: (?:collect from S {id} |pickup copy not found {id} )"
OWN_DROP = re.compile(r"Online: collect native ffffffff \(not reported: no source\)")
COINS = "coins: not graded yet (the coin sharing names its log lines; this row waits for them)"


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


# --- loot (chests and enemy drops: the spill) -----------------------------------------------------------

HOOKS_OFF = re.compile(r"Online: loot hooks off \(online_shared_loot\)")
S_OBLIGED = re.compile(r"Online: spill obliged (w[0-9a-f]{8}) ([0-9a-f]+) to peer (\d+) \(obligation (\d+)\)")
S_ENDED = re.compile(r"Online: spill obligation (\d+) (w[0-9a-f]{8}) [0-9a-f]+ to peer (\d+) ended (.+)$")
C_FULFILLED = re.compile(r"Online: spill fulfilled (?:\S+ )?(w[0-9a-f]{8})\.[0-9a-f]+ \S+ \(S selected (\d+) items, "
                         r"here spawned (\d+)\)")
C_SPILL = r"Online: spill \S+ (?:\S+ )?{key}\."
DELIVERY = re.compile(r"Online: loot delivery: .+")


def grade_loot(run: Run) -> Verdict:
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
    ended = {(m.group(2), int(m.group(3))): (line, m.group(4)) for line, m in run.server.find(S_ENDED)}
    peers = peer_numbers(run)
    fulfilled: dict[str, dict[str, tuple[Line, int, int]]] = {}
    for who, log in run.clients.items():
        fulfilled[who] = {m.group(1): (line, int(m.group(2)), int(m.group(3))) for line, m in log.find(C_FULFILLED)}
    findings: list[Finding] = []
    full = 0
    for line, m in obliged:
        key, peer = m.group(1), int(m.group(3))
        who = peers.get(peer)
        if who is None:
            continue
        got = fulfilled[who].get(key)
        s_end = ended.get((key, peer))
        if got and got[2] >= got[1]:
            full += 1
            continue
        log = run.clients[who]
        tried = log.find(re.compile(C_SPILL.format(key=key)))
        here = got[0].cite() if got else (tried[-1][0].cite() if tried else absent(log, f"`spill ... {key}`"))
        what = (f"{who} spawned {got[2]} of the {got[1]} items S selected for {key}" if got else
                f"{who} never ran S's selection for {key} (obligation {m.group(4)})")
        lines = [line.cite(), here] + ([s_end[0].cite()] if s_end else [])
        findings.append(Finding(what, lines))
    spills = {m.group(1) for _, m in obliged}
    lost = {f.what.split(" for ")[1].split(" ")[0] for f in findings if " for " in f.what}
    counts = (f"S obliged {len(spills)} spill(s) ({len(obliged)} deliveries to the clients); delivered in full {full}, "
              f"short or lost {len(findings)} (of {len(lost)} spill(s))")
    notes += [line.cite() for log in run.clients.values() for line, _ in log.find(DELIVERY)]
    return Verdict("loot", FAIL if findings else PASS, counts, obliged[0][0].cite(), findings, notes)


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
        want, kind = name, "scene"
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
    for r, text in zip(both, out):
        if text.startswith("FAIL"):
            g, name, n = r.key
            findings.append(Finding(text[5:], [scene_line(h, name, r.h.start if r.h else None),
                                               scene_line(d, name, r.d.start if r.d else None)]))
    counts = (f"{len(both) - len(findings)} of {len(both)} scene(s) both clients were in played together "
              f"(start within {start_tol} s, end within {end_tol} s); {len(rows) - len(both)} outside the time both "
              "were in the level")
    notes = expected_scene_notes(h, rows)
    skipped = [p for p in run.result.get("peers", []) if isinstance(p, dict) and SKIP_SCENES.search(str(p.get("command", "")))]
    example = scene_line(h, both[0].key[1], (both[0].h or both[0].d).start) if both else ""
    if skipped:
        return Verdict("cutscenes", NOT_TESTED, "scenes are skipped in this run (--dev_skip_scenes): " + counts, example,
                       notes=([f"FAIL-shaped differences while skipping: {len(findings)}"] if findings else []) + notes)
    if not both:
        return Verdict("cutscenes", NOT_TESTED, counts, notes=notes)
    return Verdict("cutscenes", FAIL if findings else PASS, counts, example, findings, notes)


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


# --- completable (the level pass's own verdict) --------------------------------------------------------

H_NEXT = re.compile(r"Input script: LEVELPASS phase next-level")
D_NEXT = re.compile(r"Input script: LEVELPASS D next level")


def grade_completable(run: Run) -> Verdict:
    """The level pass's verdict (result.json `levelpass`, written by `levelpass`) and each client's mark of
    the next level. Two players: a run without D does not show it (NOT TESTED)."""
    lp = run.result.get("levelpass") if isinstance(run.result.get("levelpass"), dict) else None
    if lp is None:
        return Verdict("completable", NOT_TESTED, "not a level pass (result.json has no `levelpass` verdict)")
    verdict = str(lp.get("verdict", ""))
    missed = [str(x) for x in lp.get("not_covered", []) or []]
    reached = {}
    for who, rx in (("H", H_NEXT), ("D", D_NEXT)):
        log = run.clients.get(who)
        hit = log.find(rx) if log else []
        reached[who] = hit[0][0] if hit else None
    counts = (f"level pass {verdict or '?'}; next level reached: " +
              ", ".join(f"{w} {'yes' if line else 'no'}" for w, line in reached.items() if w in run.clients))
    example = next((line.cite() for line in reached.values() if line), "")
    notes = [f"not covered: {m}" for m in missed]
    if verdict == "FAIL":
        return Verdict("completable", FAIL, counts, example, notes=notes)
    if verdict == "INCOMPLETE":
        return Verdict("completable", INCOMPLETE, counts, example, notes=notes)
    if "D" not in run.clients:
        return Verdict("completable", NOT_TESTED, counts + " (one client in this run: two players not shown)", example,
                       notes=notes)
    if verdict == "PASS" and all(reached.values()):
        return Verdict("completable", PASS, counts, example, notes=notes)
    missing = [w for w, line in reached.items() if line is None]
    return Verdict("completable", FAIL, counts, example, [Finding(f"{', '.join(missing)} logged no next-level mark", [
        absent(run.clients[w], f"`{(H_NEXT if w == 'H' else D_NEXT).pattern}`") for w in missing if w in run.clients])],
        notes)


# --- the page ------------------------------------------------------------------------------------------


def grade(folder: Path, repo: Path | None) -> list[Verdict]:
    run = load_run(folder)
    return [grade_completable(run), grade_lobby(run), grade_slot(run), grade_scenes(run, repo), grade_enemies(run),
            grade_pickups(run), grade_loot(run)]


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
            f"build snapshot {prov.get('build') or '?'}, runner {prov.get('runner') or '?'}"]


def overall(verdicts: list[Verdict]) -> str:
    """The page's one word: FAIL when a grader failed, INCOMPLETE when the pass did not cover its plan, PASS
    only when every row passed, else NOT ALL TESTED (and which rows)."""
    if any(v.status == FAIL for v in verdicts):
        return "FAIL: " + ", ".join(v.name for v in verdicts if v.status == FAIL)
    if any(v.status == INCOMPLETE for v in verdicts):
        return "INCOMPLETE"
    rest = [v.name for v in verdicts if v.status != PASS]
    return "PASS" if not rest else "NOT ALL TESTED: " + ", ".join(rest)


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
    return "\n".join(out)


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
