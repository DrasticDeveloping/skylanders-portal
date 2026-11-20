"""`skytest.py preflight <case ...> | --all-ready`: a spec's mistakes found before any game runs.

The confirm runs of 18 November (the game repository's logs/confirm/TRIAGE.txt) spent most of their game
time testing specs, not features: steps that could not act (an object asleep at the load, without a
run-time name, already spent), required lines the code does not write or writes in another order, waits
that could never be met. Each check below reads only files: the spec, the level data the level pass
stores (<levelpass>/<level>-manifest.txt, the probe's manifest; <levelpass>/<level>/manifest.json and
logic.txt, the static reader's), the game's source text, the launch allow list, the script command
table and, when there are any, recorded runs. Nothing starts a game.

Checks (each problem one line; `error` lines make the exit code 1, `warn` lines only with --strict):
  objects   every selector a step names (auth:<zone>:<index>, a run-time name) against the probe's
            manifest: it exists; it has a run-time name (a name step on an unnamed object never
            matches); asleep at the load and named (a name finds it only once its area streams in);
            asleep and never seen awake in the recorded runs (TreasureChest_1); not registered at the
            load; a single-fire trigger entered twice, or an object that stands inside a single-fire
            trigger's box (a goto there spends the trigger).
  waits     an unknown condition; a session wait after the script's own `online_end`; a peer that
            ends on `waitfor online_left` while the owner ends without waiting for it (the wait arms
            only on a check while connected: confirm-tb-chests ran it 600 s); a hand-over check
            (`online_wait_peer_near_entity`) on an object asleep on the waiting game (it fails at once)
            or near the level start (S's hero stands there and satisfies it); more peers waited for
            than the layout starts; a `start_after` marker no script logs.
  lines     every expect / expect_order / expect_count pattern: an error when a word of it is in no string
            literal of the source at all (nor in the spec's own scripts, whose words the game echoes), or
            when it names an object the probe saw without a run-time name; an `Input script:` marker must
            be logged by that peer's own script, in the order expect_order names them. With --wording also
            (as warnings) whether each literal run fits one log statement with at least half its word
            letters in the statement's own text, and whether all runs fit the same statement: lines
            composed from several fragments make this noisy (about 60 false hits over the ready specs of
            18 November), so it is opt-in.
  settings  a setting the launch allow list has no row for; a command whose "off" refusal is one of
            the runner's fatal lines (`refused: dev_\\w+ is off`) without its setting on that peer; an
            auth: selector without --dev_authored_probe=true on that peer.
  replay    with a recorded run of the case (newest first in the run roots): each expectation graded
            against that run's logs, as `replay` lines (warn), so a corrected spec is checked before
            its next run.

What it cannot tell (said in the report as well): anything that depends on the game's state at run time
(an enemy in a job state that cannot be hurt, a breakable that is not tracked, a hazard whose hit throws
the hero out, a join that fails), the order in which two code paths write their lines, a placeholder's
value (a label printed as a handle instead of a name), whether a step's object is really reached (a
teleport that lands short), and an object woken by an event of the spec that the data does not show.
Standard library only; run with `python -I`.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# --- problems --------------------------------------------------------------------------------------


@dataclass
class Problem:
    severity: str   # "error" | "warn"
    case: str
    peer: str
    check: str      # objects | waits | lines | settings | replay | data
    text: str

    def line(self) -> str:
        return f"{self.case}: {self.severity} {self.check} {self.peer}: {self.text}"


# --- the level data ----------------------------------------------------------------------------------

PROBE_ROW = re.compile(r"^\s*(\d+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(\S+)\s+(.+?)\s+\[([^#\]]+)#(\d+)\]\s+(\w+)")
LEVEL_START = re.compile(r"^Level start: (\S+) at")
TRIGGER_ROW = re.compile(r"^(\S+)\s+CScriptTriggerEntity\s+\[[^\]]*\]\s+at \((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\)"
                         r".*?world \((-?\d+), (-?\d+), (-?\d+)\)\.\.\((-?\d+), (-?\d+), (-?\d+)\)"
                         r".*?triggerFlags \d+ = (\S+)")


@dataclass
class ProbeObj:
    zone: str
    index: int
    name: str          # "" when the probe saw no run-time name
    cls: str
    pos: tuple[float, float, float]
    state: str         # registered | asleep | skipped
    dist: float        # from the level start


@dataclass
class Trigger:
    name: str
    pos: tuple[float, float, float]
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]
    single_fire: bool


@dataclass
class Level:
    name: str
    objects: dict[tuple[str, int], ProbeObj] = field(default_factory=dict)
    by_name: dict[str, list[ProbeObj]] = field(default_factory=dict)
    static_names: dict[str, list[tuple[float, float, float]]] = field(default_factory=dict)
    start_name: str = ""
    triggers: list[Trigger] = field(default_factory=list)

    def zone_of(self, zone: str) -> str:
        """The probe's zone spelling for an auth: zone (case folded)."""
        for (z, _i) in self.objects:
            if z.lower() == zone.lower():
                return z
        return zone


def parse_probe_manifest(text: str, level: str) -> Level:
    lv = Level(level)
    for line in text.splitlines():
        m = LEVEL_START.match(line)
        if m:
            lv.start_name = m.group(1)
            continue
        m = PROBE_ROW.match(line)
        if not m:
            continue
        dist, x, y, z, cls, name, zone, idx, state = m.groups()
        name = "" if name.strip() == "(no name)" else name.strip()
        o = ProbeObj(zone, int(idx), name, cls, (float(x), float(y), float(z)), state, float(dist))
        lv.objects[(zone, int(idx))] = o
        if name:
            lv.by_name.setdefault(name.lower(), []).append(o)
    return lv


def parse_static_names(data: object) -> dict[str, list[tuple[float, float, float]]]:
    """Names and positions from the static reader's manifest.json (whatever its nesting: every dict with a
    name and a position)."""
    out: dict[str, list[tuple[float, float, float]]] = {}

    def walk(o: object) -> None:
        if isinstance(o, dict):
            name = o.get("name") or o.get("refname")
            pos = o.get("position") or o.get("pos")
            if isinstance(name, str) and isinstance(pos, (list, tuple)) and len(pos) == 3:
                try:
                    out.setdefault(name.lower(), []).append((float(pos[0]), float(pos[1]), float(pos[2])))
                except (TypeError, ValueError):
                    pass
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return out


def parse_logic_triggers(text: str) -> list[Trigger]:
    out = []
    for line in text.splitlines():
        m = TRIGGER_ROW.match(line)
        if not m:
            continue
        g = m.groups()
        out.append(Trigger(g[0], (float(g[1]), float(g[2]), float(g[3])), (float(g[4]), float(g[5]), float(g[6])),
                           (float(g[7]), float(g[8]), float(g[9])), "eSTF_SingleFire" in g[10]))
    return out


def load_level(folder: Path, level: str) -> Level | None:
    probe = folder / f"{level}-manifest.txt"
    if not probe.is_file():
        return None
    lv = parse_probe_manifest(probe.read_text(encoding="utf-8", errors="replace"), level)
    static = folder / level / "manifest.json"
    if static.is_file():
        try:
            lv.static_names = parse_static_names(json.loads(static.read_text(encoding="utf-8", errors="replace")))
        except ValueError:
            pass
    logic = folder / level / "logic.txt"
    if logic.is_file():
        lv.triggers = parse_logic_triggers(logic.read_text(encoding="utf-8", errors="replace"))
    return lv


def near(a: tuple[float, float, float], b: tuple[float, float, float], d: float) -> bool:
    return sum((x - y) ** 2 for x, y in zip(a, b)) <= d * d


def inside(p: tuple[float, float, float], lo: tuple[float, float, float], hi: tuple[float, float, float]) -> bool:
    return all(min(l, h) <= v <= max(l, h) for v, l, h in zip(p, lo, hi))


# --- wake evidence from recorded runs -----------------------------------------------------------------

WALK = re.compile(rb"authored walk (\S+) request \d+ zone (\S+) hash ([0-9a-f]{8})")
AWAKE = re.compile(rb"authored (?:wake ([0-9a-f]{8}) 1 (\d+) mid none -> [0-9a-f]+|obj ([0-9a-f]{8}) 1 (\d+) class \S+ "
                   rb"entity 1 name \S+ name0 \S+ mid [0-9a-f]+ state registered)")


@dataclass
class WakeIndex:
    zone_hash: dict[str, str] = field(default_factory=dict)        # zone (last path part, folded) -> hash
    awake: set[tuple[str, int]] = field(default_factory=set)      # (hash, index) seen registered or woken
    runs_of: dict[str, int] = field(default_factory=dict)          # hash -> logs that walked the zone

    def seen_awake(self, zone: str, index: int) -> bool | None:
        """True / False when a recorded run walked the zone; None when none did (no evidence)."""
        h = self.zone_hash.get(zone.lower())
        if h is None or not self.runs_of.get(h):
            return None
        return (h, index) in self.awake


def scan_log_bytes(data: bytes, idx: WakeIndex, walked: set[str]) -> None:
    for m in WALK.finditer(data):
        zone = re.split(rb"[\\/]", m.group(2))[-1].decode("utf-8", "replace").lower()
        h = m.group(3).decode()
        idx.zone_hash[zone] = h
        walked.add(h)
    for m in AWAKE.finditer(data):
        h = (m.group(1) or m.group(3)).decode()
        i = int(m.group(2) or m.group(4))
        idx.awake.add((h, i))


def build_wake_index(roots: list[Path], cache: Path | None) -> WakeIndex:
    """Every H, D and S game.log under the run roots, scanned once (the cache keeps each file's findings by
    path, size and time)."""
    old: dict = {}
    if cache and cache.is_file():
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
    new: dict = {}
    idx = WakeIndex()
    for root in roots:
        if not root.is_dir():
            continue
        for run in sorted(root.iterdir()):
            for rel in ("H/game.log", "D/game.log", "H/data/Online/session/game.log"):
                f = run / rel
                if not f.is_file():
                    continue
                st = f.stat()
                key = str(f)
                rec = old.get(key)
                if not rec or rec.get("size") != st.st_size or rec.get("mtime") != st.st_mtime:
                    part = WakeIndex()
                    walked: set[str] = set()
                    try:
                        scan_log_bytes(f.read_bytes(), part, walked)
                    except OSError:
                        continue
                    rec = {"size": st.st_size, "mtime": st.st_mtime, "zones": part.zone_hash,
                           "awake": sorted([list(x) for x in part.awake]), "walked": sorted(walked)}
                new[key] = rec
                idx.zone_hash.update(rec["zones"])
                idx.awake.update((h, int(i)) for h, i in rec["awake"])
                for h in rec["walked"]:
                    idx.runs_of[h] = idx.runs_of.get(h, 0) + 1
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(new), encoding="utf-8")
        except OSError:
            pass
    return idx


# --- the code's log text -------------------------------------------------------------------------------

LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
FMT_FIELD = re.compile(r"\{[^{}]*\}")


@dataclass
class Fmt:
    pieces: list[str]      # literal text between placeholders
    free_start: bool       # a `+` concatenation before it: any text may come first
    free_end: bool


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def fmt_pieces(text: str) -> list[str]:
    """A format string's literal pieces ({...} fields are placeholders; {{ and }} are braces)."""
    t = text.replace("{{", "\x01").replace("}}", "\x02")
    parts = FMT_FIELD.split(t)
    return [p.replace("\x01", "{").replace("\x02", "}") for p in parts]


def extract_formats(source: str) -> list[Fmt]:
    out = []
    lits = list(LITERAL.finditer(source))
    i = 0
    while i < len(lits):
        # adjacent literals (only whitespace between) are one string
        j = i
        text = _unescape(lits[i].group(1))
        while j + 1 < len(lits) and not source[lits[j].end():lits[j + 1].start()].strip():
            j += 1
            text += _unescape(lits[j].group(1))
        before = source[max(0, lits[i].start() - 40):lits[i].start()].rstrip()
        after = source[lits[j].end():lits[j].end() + 40].lstrip().lstrip(")").lstrip()
        if source[lits[i].start() - 1:lits[i].start()] != "'" and text.strip():
            # a literal that does not begin a log line (no "Online: " ...) is a fragment some other format
            # embeds (`run.Log("Dev: {} {} {}", ..., fmt::format("gives up on ..."))`): free at both ends
            fragment = not text.startswith(PREFIXES)
            out.append(Fmt(fmt_pieces(text), before.endswith("+") or fragment, after.startswith("+") or fragment))
        i = j + 1
    return out


WORDS3 = re.compile(r"(?<![\w.])[a-z]{3,}(?![\w.])")


def _letters(text: str) -> int:
    return sum(len(w) for w in re.findall(r"[a-z]{3,}", text))


def compatible(r: str, f: Fmt, share: float = 0.5) -> bool:
    """Can `r` be a substring of a string this format writes, with most of its wording in the format's own
    literal text? Placeholders take any text, but the letters of r's lower-case words that land in literal
    pieces must be at least `share` of them (and at least 3): a run a placeholder could hold whole says
    nothing about the code's wording."""
    pieces = f.pieces
    n = len(pieces)
    total = sum(len(w) for w in WORDS3.findall(r))
    need = max(3, int(total * share + 0.999)) if total else 3

    def ok(cov: int) -> bool:
        return cov >= need

    def tail(rest: str, j: int, cov: int) -> bool:
        for k in range(len(rest) + 1):           # a placeholder before piece j takes rest[:k]
            s = rest[k:]
            if not s:
                return ok(cov)
            if j >= n:
                return f.free_end and ok(cov)
            if not pieces[j]:
                continue
            if pieces[j].startswith(s):
                if ok(cov + _letters(s)):
                    return True
                continue
            if s.startswith(pieces[j]) and (j + 1 < n or f.free_end):
                if tail(s[len(pieces[j]):], j + 1, cov + _letters(pieces[j])):
                    return True
        return False

    for i, piece in enumerate(pieces):
        for o in range(len(piece)):
            seg = piece[o:]
            if seg.startswith(r):
                if ok(_letters(r)):
                    return True
                continue
            if r.startswith(seg) and (i + 1 < n or f.free_end):
                if tail(r[len(seg):], i + 1, _letters(seg)):
                    return True
        if (i > 0 or f.free_start) and tail(r, i, 0):
            return True
    return False


WORD = re.compile(r"(?<![\w.])[a-z]{4,}(?![\w.])")


@dataclass
class Corpus:
    formats: list[Fmt] = field(default_factory=list)
    by_word: dict[str, list[int]] = field(default_factory=dict)
    words: set[str] = field(default_factory=set)
    whole: set[str] = field(default_factory=set)   # literals that are one word: values put into placeholders
    text: str = ""                                 # the source text (the table's rows are checked in it)
    lit_text: str = ""                             # every string literal, one per line

    def add(self, fmts: list[Fmt]) -> None:
        for f in fmts:
            k = len(self.formats)
            self.formats.append(f)
            joined = "\x00".join(f.pieces)
            for w in set(re.findall(r"[a-z]{3,}", joined)):
                self.by_word.setdefault(w, []).append(k)
                self.words.add(w)
            if len(f.pieces) == 1 and re.fullmatch(r"[A-Za-z_.]+", f.pieces[0]):
                self.whole.add(f.pieces[0])


def build_corpus(roots: list[Path]) -> Corpus:
    c = Corpus()
    texts = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix in (".cpp", ".h", ".hpp", ".cc", ".inl") and p.is_file():
                try:
                    s = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                c.add(extract_formats(s))
                texts.append(s)
    c.text = "\n".join(texts)
    c.lit_text = "\n".join("\x00".join(f.pieces) for f in c.formats)
    return c


# --- the patterns ------------------------------------------------------------------------------------

PREFIXES = ("Online: ", "Dev: ", "Input script: ", "Debug: ", "Level watch: ")


def literal_runs(rx: str) -> list[str]:
    """The literal text runs of a regex that every match must contain, outside alternations; a quantified
    character ends its run (it may be absent)."""
    runs: list[str] = []
    cur = ""
    alt_groups = set()              # the groups (by their opening index) that hold a `|`
    level_starts: list[int] = []
    i = 0
    while i < len(rx):
        ch = rx[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            j = rx.find("]", i + 2)
            i = (j + 1) if j > 0 else i + 1
            continue
        if ch == "(":
            level_starts.append(i)
        elif ch == ")" and level_starts:
            level_starts.pop()
        elif ch == "|":
            alt_groups.add(level_starts[-1] if level_starts else -1)
        i += 1
    if -1 in alt_groups:
        return []                   # a top-level alternation: no run is required
    i = 0
    open_groups: list[int] = []

    def flush() -> None:
        nonlocal cur
        if cur and not any(g in alt_groups for g in open_groups):
            runs.append(cur)
        cur = ""
    while i < len(rx):
        ch = rx[i]
        nxt = rx[i + 1] if i + 1 < len(rx) else ""
        if ch == "\\" and nxt:
            if nxt in "dDsSwWbB":
                flush()
                i += 2
                continue
            lit = nxt
            i += 2
            q = rx[i] if i < len(rx) else ""
            if q in "?*" or (q == "{" and re.match(r"\{\d*,?\d*\}", rx[i:])):
                flush()
                continue
            if q == "+":
                cur += lit
                flush()
                continue
            cur += lit
            continue
        if ch == "[":
            flush()
            j = rx.find("]", i + 2)
            i = (j + 1) if j > 0 else i + 1
            continue
        if ch == "(":
            flush()
            open_groups.append(i)
            i += 1
            if rx[i:i + 2] == "?:":
                i += 2
            continue
        if ch == ")":
            flush()
            if open_groups:
                open_groups.pop()
            i += 1
            if i < len(rx) and rx[i] in "?*+":
                i += 1
            continue
        if ch in ".|^$":
            flush()
            i += 1
            continue
        if ch in "?*":
            cur = cur[:-1]
            flush()
            i += 1
            continue
        if ch == "+":
            flush()
            i += 1
            continue
        if ch == "{" and re.match(r"\{\d*,?\d*\}", rx[i:]):
            cur = cur[:-1]
            flush()
            i = rx.index("}", i) + 1
            continue
        cur += ch
        i += 1
    flush()
    return runs


def pattern_problem(rx: str, corpus: Corpus, values: frozenset[str] = frozenset()) -> str:
    """'' when every checkable literal run of the pattern fits some log statement; else why not. `values`:
    words the spec's own scripts pass (labels, names), which the game echoes into placeholders."""
    for run in literal_runs(rx):
        r = run
        for p in PREFIXES:
            if r.startswith(p):
                r = r[len(p):]
                break
        why = _run_problem(r, corpus, values)
        if why:
            # a word the code has only as a whole literal is a value put into a placeholder (an enum's name,
            # a reason), and so is a word of the script's own: split the run there and check the pieces
            words = [w for w in re.findall(r"[a-z]{4,}", r) if w in corpus.whole or w in values]
            if words:
                parts = re.split("|".join(re.escape(w) for w in words), r)
                # the value's neighbours must still be the code's: checkable words, or no letters at all
                def verified(x: str) -> bool:
                    if not re.search(r"[A-Za-z]", x):
                        return True
                    return len(x.strip()) >= 8 and bool(WORD.search(x)) and not _run_problem(x, corpus, values)
                lettered = [x for x in parts if re.search(r"[A-Za-z]", x)]
                # one value between punctuation (` (leased)`), or values beside wording that checks out
                if (not lettered and len(words) == 1) or (lettered and all(verified(x) for x in parts)):
                    continue
            return why
    return ""


def anchored(f: Fmt) -> bool:
    """A format that begins a log line (its first piece starts with a prefix), not an embedded fragment."""
    return bool(f.pieces) and f.pieces[0].startswith(PREFIXES)


def pattern_warning(rx: str, corpus: Corpus) -> str:
    """'' unless the pattern's runs each fit a log line, but no single line-beginning statement fits all of
    them (they would have to come from different log lines)."""
    sets = []
    for run in literal_runs(rx):
        if len(run.strip()) < 8 or not WORD.search(run):
            continue
        x = run
        for pre in PREFIXES:
            if x.startswith(pre):
                x = x[len(pre):]
                break
        cand: set[int] = set()
        for w in set(re.findall(r"(?<![\w.])[a-z]{3,}(?![\w.])", x)):
            cand.update(corpus.by_word.get(w, ()))
        fits = {k for k in cand if anchored(corpus.formats[k]) and compatible(x, corpus.formats[k])}
        if not fits:
            return ""          # this run fits only fragments (or values): its line cannot be told
        sets.append(fits)
    if len(sets) >= 2 and not set.intersection(*sets):
        return "its parts fit different log statements, no single one writes them all"
    return ""


def _run_problem(x: str, corpus: Corpus, values: frozenset[str] = frozenset()) -> str:
    words = WORD.findall(x)
    if len(x.strip()) < 8 or not words:
        return ""
    unknown = [w for w in words if w not in corpus.words and w not in values]
    if unknown:
        return f"no log text of the code has the word(s) {', '.join(sorted(set(unknown)))} (in {x.strip()!r})"
    cand: set[int] = set()
    for w in set(re.findall(r"(?<![\w.])[a-z]{3,}(?![\w.])", x)):
        for v in (w, w[:-1], w[:-2]):          # a plural's `s` may be a field (`client{}`)
            if len(v) >= 3:
                cand.update(corpus.by_word.get(v, ()))
    if not any(compatible(x, corpus.formats[k]) for k in cand):
        # text the code has verbatim in some literal may be a value one format puts into another's field
        core = re.sub(r"^\W+|\W+$", "", x)
        if len(core) >= 8 and core in corpus.lit_text:
            return ""
        return f"no log statement writes {x.strip()!r}"
    return ""


# --- the spec's scripts ------------------------------------------------------------------------------

SELECTOR_COMMANDS = {"goto_entity", "enter_trigger", "interact", "attack_until", "pickup", "fight_near",
                     "encounter_clear", "lean_into", "door", "wait_moved", "online_wait_peer_near_entity",
                     "push", "goto_push_side", "wait_push_block"}
ENTER_COMMANDS = {"enter_trigger"}
STREAM_RADIUS = 4000.0   # an earlier teleport this close to an asleep object has likely streamed its area in
TOUCH_COMMANDS = {"goto_entity", "interact", "attack_until", "pickup", "push", "goto_push_side", "door"}
# Commands whose refusal when a dev setting is off is a runner-fatal line (`refused: dev_\w+ is off`),
# the setting, and the source text the table rests on (checked against the corpus: a row whose text the
# code no longer has is reported as stale).
FATAL_OFF = {
    "fight_near": ("dev_defeat_commands", "dev_defeat_commands is off"),
    "fight_area": ("dev_defeat_commands", "dev_defeat_commands is off"),
    "encounter_clear": ("dev_defeat_commands", "dev_defeat_commands is off"),
    "dev_encounter_start": ("dev_trigger_commands", "dev_encounter_start refused: dev_trigger_commands is off"),
    "online_server_dev_encounter_start": ("dev_trigger_commands", "dev_encounter_start refused: dev_trigger_commands is off"),
    "online_audit": ("dev_backend_commands", "refused: dev_backend_commands is off"),
    "dev_lock_watch": ("dev_backend_commands", "dev_lock_watch refused: dev_backend_commands is off"),
    "dev_enemy": ("dev_enemy_commands", "dev_enemy refused: dev_enemy_commands is off"),
    "online_server_enemy": ("dev_enemy_commands", "online_server_enemy refused: dev_enemy_commands is off"),
    "authored_force_main_walk": ("dev_authored_test", "authored_force_main_walk refused: dev_authored_test is off"),
}
SESSION_WAITS = re.compile(r"^online_(connected|server_hub|barrier|alone|peer_was_in|level_end|past_level_end|"
                           r"next_level|level_after_mark|join_pending|identity_witnessed|enemy_bound|late_step_seen|"
                           r"scene_matched|level_loading|standin_\w+)$")
OWNER_WAITS_FOR_PEER = re.compile(r"^(waitfor\s+online_alone|online_wait_peers_at_most|waitfor\s+online_peer_was_in)\b")
PASSIVE = {"log", "waitfor", "wait", "wait_free", "fps", "shot", "dump_entities", "online_server_dump_entities",
           "quit", "online_identity_report", "tracecounts"}


def lines_of(script: str) -> list[str]:
    return [l.strip() for l in script.splitlines() if l.strip() and not l.strip().startswith("#")]


def setting_on(args: list[str], name: str) -> bool:
    return any(a == f"--{name}=true" or a.startswith(f"--{name}=") and a.split("=", 1)[1] not in ("false", "0")
               for a in args)


def layout_clients(layout: str) -> int:
    m = re.match(r"server\+(\d+)", layout or "")
    return int(m.group(1)) if m else (1 if layout == "solo" else 0)


@dataclass
class Context:
    levels: dict[str, Level]
    corpus: Corpus | None
    wakes: WakeIndex | None
    allow: set[str] | None
    conditions: set[str] | None
    runs: list[Path] = field(default_factory=list)
    wording: bool = False      # also the field-aware fit of each pattern (warnings; noisy, see the header)


def resolve(sel: str, lv: Level) -> tuple[list[ProbeObj], str]:
    """The probe objects a selector names, and a problem when it can never find one."""
    if sel.startswith("auth:"):
        parts = sel.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            return [], f"{sel}: not auth:<zone>:<index>"
        o = lv.objects.get((lv.zone_of(parts[1]), int(parts[2])))
        return ([o], "") if o else ([], f"{sel}: no object {parts[1]}#{parts[2]} in {lv.name}'s probe manifest")
    if sel.startswith(("nearest:", "id:", "at:")) or sel == "last" or sel.isdigit() or "@" in sel:
        return [], ""
    objs = lv.by_name.get(sel.lower(), [])
    if objs:
        return objs, ""
    if sel.lower() in lv.static_names:
        # the static data names it; the probe saw it without a run-time name
        hits = []
        for pos in lv.static_names[sel.lower()]:
            for o in lv.objects.values():
                if not o.name and near(o.pos, pos, 2.0):
                    hits.append(f"auth:{o.zone}:{o.index}")
        alt = f" (use {', '.join(sorted(set(hits))[:2])})" if hits else ""
        return [], f"{sel}: has no run-time name: a name step never matches it{alt}"
    return [], ""


def check_spec(name: str, d: dict, ctx: Context) -> list[Problem]:
    P: list[Problem] = []

    def add(sev: str, peer: str, check: str, text: str) -> None:
        P.append(Problem(sev, name, peer, check, text))

    peers = d.get("peer") or {}
    layout = str(d.get("layout", ""))
    clients = layout_clients(layout)
    # the level each script plays: its own `level` lines, else the owner's
    owner_levels = [l.split()[1] for l in lines_of((peers.get("H") or {}).get("script", "")) if l.split()[0] in ("level", "debug_level") and len(l.split()) > 1]
    logged: dict[str, list[str]] = {}
    for pn, pd in peers.items():
        logged[pn] = [l.split(None, 1)[1] for l in lines_of(pd.get("script", "") or "") if l.split()[0] == "log" and len(l.split()) > 1]
    all_markers = {m for ms in logged.values() for m in ms}
    values = frozenset(w for pd in peers.values() for w in re.findall(r"[a-z]{4,}", (pd.get("script", "") or "").lower()))
    marker_like = re.compile(r"^[A-Z][A-Z0-9_]*(_[A-Z0-9_]+)+$")
    entered_single: dict[tuple[str, int], str] = {}

    for pn, pd in peers.items():
        script = pd.get("script", "") or ""
        args = list(pd.get("args", []) or [])
        level = owner_levels[0] if owner_levels else None
        # W38: a client with no `level` line of its own follows the owner's levels in order: each `waitfor
        # online_level_after_mark`, and each `waitfor online_next_level` after its first, moves it to the next one.
        own_levels = any(l.split()[0] in ("level", "debug_level") for l in lines_of(script))
        follow_i, follow_seen = 0, False
        woken: set[tuple[str, int]] = set()
        visited: list[tuple[float, float, float]] = []   # where earlier steps put the hero (streams areas in)
        walked = False                                    # a route walk with no position the data can read
        ended = False
        lines = lines_of(script)
        # --- settings
        if ctx.allow is not None:
            for a in args:
                n = a[2:].split("=", 1)[0] if a.startswith("--") else ""
                if n and n not in ctx.allow:
                    add("error", pn, "settings", f"{n}: no row in tests/launch_allow.toml (the launch builder refuses it)")
        for li, line in enumerate(lines):
            w = line.split()
            cmd = w[0]
            if cmd in ("level", "debug_level") and len(w) > 1:
                level = w[1]
            if (cmd == "waitfor" and len(w) > 1 and not own_levels and owner_levels and
                    w[1] in ("online_level_after_mark", "online_next_level")):
                if w[1] == "online_level_after_mark" or follow_seen:
                    follow_i = min(follow_i + 1, len(owner_levels) - 1)
                    level = owner_levels[follow_i]
                follow_seen = True
            if cmd == "online_end":
                ended = True
            if cmd in ("goto", "navto", "walkto", "online_server_goto") and len(w) >= 4:
                try:
                    visited.append((float(w[1]), float(w[2]), float(w[3])))
                except ValueError:
                    walked = True
            elif cmd in ("navto", "walkto", "navlink", "navplatform", "fight_area", "route"):
                walked = True
            if cmd in ("online_join", "online_host"):
                ended = False
            if cmd in FATAL_OFF:
                setting, _src = FATAL_OFF[cmd]
                if not setting_on(args, setting):
                    add("error", pn, "settings", f"`{line}` needs --{setting}=true here: off, its refusal "
                        f"(`refused: {setting} is off`) is a fatal line of the runner")
            if cmd == "waitfor" and len(w) > 1:
                cond = w[1]
                if ctx.conditions is not None and cond not in ctx.conditions:
                    add("error", pn, "waits", f"`{line}`: {cond} is not a registered condition")
                if ended and SESSION_WAITS.match(cond):
                    add("error", pn, "waits", f"`{line}` comes after this script's own online_end: it can never be met")
            if cmd == "online_wait_peers" and len(w) > 1 and w[1].isdigit() and "or_fewer" not in w and int(w[1]) > clients:
                add("error", pn, "waits", f"`{line}`: the layout {layout} starts {clients} client(s)")
            if cmd in SELECTOR_COMMANDS and len(w) > 1:
                sel = w[1]
                if sel.startswith("auth:") and not setting_on(args, "dev_authored_probe"):
                    add("error", pn, "settings", f"`{line}`: an auth: selector needs --dev_authored_probe=true on this peer")
                lv = ctx.levels.get(level or "")
                if lv is None:
                    continue
                objs, why = resolve(sel, lv)
                if why:
                    add("error", pn, "objects", f"`{line}`: {why}")
                    continue
                if not objs:
                    continue
                keys = {(o.zone, o.index) for o in objs}
                if sel.startswith("auth:"):
                    o = objs[0]
                    if o.state == "skipped":
                        add("error", pn, "objects", f"`{line}`: {o.zone}#{o.index} is not registered at the load (skipped)")
                    elif o.state == "asleep" and cmd != "online_wait_peer_near_entity" and ctx.wakes is not None:
                        seen = ctx.wakes.seen_awake(o.zone, o.index)
                        if seen is False:
                            add("warn", pn, "objects", f"`{line}`: {o.name or '(no name)'} {o.zone}#{o.index} is asleep at "
                                f"the load and was never seen awake in {ctx.wakes.runs_of.get(ctx.wakes.zone_hash.get(o.zone.lower(), ''), 0)} "
                                "recorded log(s) of its zone: a teleport may not wake it")
                elif all(o.state != "registered" for o in objs) and not (keys & woken):
                    if any(near(v, o.pos, STREAM_RADIUS) for v in visited for o in objs) or walked:
                        add("warn", pn, "objects", f"`{line}`: {sel} is asleep at the load: found by name only if an "
                            "earlier step's move has streamed its area in (address it as auth:<zone>:<index> to be sure)")
                        continue
                    add("error", pn, "objects", f"`{line}`: {sel} is asleep at the load ({', '.join(f'{o.zone}#{o.index}' for o in objs[:2])}): "
                        "a name finds it only once its area streams in; name it auth:<zone>:<index> or wake it first")
                if cmd == "online_wait_peer_near_entity":
                    o = objs[0]
                    if o.state != "registered" and not (keys & woken) and any(near(v, o.pos, STREAM_RADIUS) for v in visited):
                        add("warn", pn, "waits", f"`{line}`: {o.name or o.zone + '#' + str(o.index)} is asleep at the load; "
                            "an earlier teleport near it has likely streamed it in (the check fails at once if not)")
                    elif o.state != "registered" and not (keys & woken):
                        add("error", pn, "waits", f"`{line}`: {o.name or o.zone + '#' + str(o.index)} is asleep on this game: "
                            "the check fails at once (`no entity: asleep`)")
                    try:
                        radius = float(w[2])
                    except (IndexError, ValueError):
                        radius = 0.0
                    if o.dist <= radius + 300.0:
                        add("warn", pn, "waits", f"`{line}`: {o.dist:.0f} from the level start ({lv.start_name}), where "
                            "S's hero stands: S's pose satisfies the check whatever the other client does")
                if cmd in ("goto_entity", "enter_trigger", "interact", "door", "attack_until", "pickup"):
                    woken |= keys
                    visited.extend(o.pos for o in objs)
                # single-fire triggers
                for o in objs:
                    for t in lv.triggers:
                        if not t.single_fire:
                            continue
                        if cmd in ENTER_COMMANDS and near(t.pos, o.pos, 2.0):
                            k = (o.zone, o.index)
                            if k in entered_single:
                                add("warn", pn, "objects", f"`{line}`: {t.name} is single-fire and was already entered "
                                    f"by {entered_single[k]}: S refuses it `already fired`")
                            else:
                                entered_single[k] = f"{pn} `{line}`"
                        elif cmd in TOUCH_COMMANDS and not near(t.pos, o.pos, 2.0) and inside(o.pos, t.lo, t.hi):
                            add("warn", pn, "objects", f"`{line}`: the object stands inside single-fire trigger "
                                f"{t.name}'s box: a goto there fires and spends it")
        # --- the end: online_left against the owner's end
        if pn != "H" and any(re.match(r"^waitfor\s+online_left\b", l) for l in lines):
            idx = max(i for i, l in enumerate(lines) if re.match(r"^waitfor\s+online_left\b", l))
            active = [l for l in lines[:idx] if l.split()[0] not in PASSIVE and not l.startswith(("online_join", "place", "tap"))]
            hl = lines_of((peers.get("H") or {}).get("script", "") or "")
            end_i = next((i for i, l in enumerate(hl) if l.split()[0] == "online_end"), None)
            waits_for_peer = end_i is not None and any(OWNER_WAITS_FOR_PEER.match(l) for l in hl[:end_i])
            if active and not waits_for_peer:
                add("warn", pn, "waits", "ends on `waitfor online_left` while H ends the session without waiting for "
                    f"{pn} (no online_alone / online_wait_peers_at_most before its online_end): the wait arms only on a "
                    f"check while connected, so if H ends first it runs to its limit; let {pn} leave (online_end) and "
                    "H end on `waitfor online_alone`")
        # --- markers and patterns
        sa = pd.get("start_after", "")
        if sa:
            m = re.match(r"^Input script: (.+)$", sa)
            if m and not any(re.fullmatch(m.group(1), x) or re.search(m.group(1), x) for x in all_markers):
                add("error", pn, "waits", f"start_after {sa!r}: no script logs that marker; this peer never starts")
        pats: list[tuple[str, str]] = []
        for key in ("expect", "forbid", "expect_order"):
            pats += [(key, x) for x in (pd.get(key) or [])]
        pats += [("expect_count", x) for x in (pd.get("expect_count") or {})]
        order_markers = []
        for key, rx in pats:
            try:
                re.compile(rx)
            except re.error as e:
                add("error", pn, "lines", f"{key} {rx!r}: not a regex ({e})")
                continue
            own_hit = [x for x in logged.get(pn, []) if re.search(rx, "Input script: " + x) or re.search(rx, x)]
            if own_hit and key != "forbid":
                if key == "expect_order":
                    order_markers.append(own_hit[0])
                continue
            m = re.match(r"^Input script: ([A-Za-z0-9_ .:-]+)$", rx)
            if m and key != "forbid" and (marker_like.match(m.group(1)) or m.group(1) in all_markers):
                mk = m.group(1)
                own = [x for x in logged.get(pn, []) if re.search(re.escape(mk), x) or x == mk]
                live = any(l.split()[0] == "follow" for l in lines_of(pd.get("script", "") or ""))
                if not own:
                    other = [p for p, ms in logged.items() if p != pn and mk in ms]
                    add("warn" if live else "error", pn, "lines", f"{key} marker {mk!r}: this peer's script never logs it"
                        + (f" (peer {', '.join(other)} does: a peer's log has only its own markers)" if other else ""))
                elif key == "expect_order":
                    order_markers.append(mk)
                continue
            lv = ctx.levels.get(owner_levels[0] if owner_levels else "")
            if lv is not None and key != "forbid":
                for run in literal_runs(rx):
                    for tok in re.findall(r"\b[A-Z][A-Za-z0-9_]*[0-9_][A-Za-z0-9_]*\b", run):
                        if tok.lower() in lv.static_names and tok.lower() not in lv.by_name:
                            add("error", pn, "lines", f"{key} {rx!r}: {tok} has no run-time name in {lv.name} (the probe "
                                "saw it unnamed): the game prints its label without that name, so this never matches")
            if ctx.corpus is not None and key != "forbid":
                why = pattern_problem(rx, ctx.corpus, values)
                if why.startswith("no log text"):
                    add("error", pn, "lines", f"{key} {rx!r}: {why}")
                elif why and ctx.wording:
                    add("warn", pn, "lines", f"{key} {rx!r}: {why}")
                elif ctx.wording:
                    warn = pattern_warning(rx, ctx.corpus)
                    if warn:
                        add("warn", pn, "lines", f"{key} {rx!r}: {warn}")
        mine = logged.get(pn, [])
        seq = [mine.index(mk) for mk in order_markers if mk in mine]
        if seq != sorted(seq):
            add("error", pn, "lines", f"expect_order names the markers {order_markers} in another order than the "
                f"script logs them {[x for x in mine if x in order_markers]}")
    return P


def check_table(corpus: Corpus) -> list[str]:
    """Rows of FATAL_OFF whose source text the code no longer has (the table went stale)."""
    return [f"preflight table: {cmd}: the code has no text {txt!r}" for cmd, (_s, txt) in FATAL_OFF.items()
            if txt not in corpus.text]


# --- replay against a recorded run -------------------------------------------------------------------


def run_logs(run: Path) -> dict[str, Path]:
    return {"H": run / "H" / "game.log", "D": run / "D" / "game.log", "S": run / "H" / "data" / "Online" / "session" / "game.log"}


def replay(name: str, d: dict, run: Path) -> list[Problem]:
    P: list[Problem] = []
    logs = run_logs(run)
    for pn, pd in (d.get("peer") or {}).items():
        f = logs.get(pn)
        if not f or not f.is_file():
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        tag = run.name

        def first(rx: str, start: int = 0) -> int | None:
            r = re.compile(rx)
            return next((i for i in range(start, len(lines)) if r.search(lines[i])), None)
        for rx in pd.get("expect") or []:
            if first(rx) is None:
                P.append(Problem("warn", name, pn, "replay", f"{tag}: expect {rx!r} would fail"))
        for rx in pd.get("forbid") or []:
            h = first(rx)
            if h is not None:
                P.append(Problem("warn", name, pn, "replay", f"{tag}: forbid {rx!r} would hit (line {h + 1})"))
        at = 0
        for rx in pd.get("expect_order") or []:
            h = first(rx, at)
            if h is None:
                P.append(Problem("warn", name, pn, "replay", f"{tag}: expect_order {rx!r} would fail from line {at + 1}"))
                break
            at = h + 1
        for rx, n in (pd.get("expect_count") or {}).items():
            c = sum(1 for l in lines if re.search(rx, l))
            if c != n:
                P.append(Problem("warn", name, pn, "replay", f"{tag}: expect_count {rx!r} = {c}, wants {n}"))
    return P


def latest_run(roots: list[Path], case: str) -> Path | None:
    found = []
    for root in roots:
        if root.is_dir():
            found += [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}-\d{6}-" + re.escape(case), p.name)]
    return max(found, key=lambda p: p.name) if found else None
