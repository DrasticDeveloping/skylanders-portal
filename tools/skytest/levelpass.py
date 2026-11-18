"""The automatic level pass (`skytest.py levelpass <level> [--full] [--no-late-join]`): one session that
boots once, a sweep generated from the level's own data, an automatic verdict from the game's audit.

Pure code here (no game, no files beyond what the caller hands in), unit-tested in
tests/test_levelpass.py with made-up content; skytest.py's cmd_levelpass runs it through the normal
case machinery (the launch builder and its guards, the machine's game slots, fail fast on a crash).

Content (load_content): the level's placed objects, from the first of
  <levelpass>/<level>/manifest.json   the static level reader's manifest (tools/level_logic.py: every
                                      placed entity with class, name, position, map unit and group)
  <levelpass>/<level>-manifest.txt    the authored probe's manifest (tools/level_manifest.py)
and, when it exists, <levelpass>/<level>/logic.json (which trigger sends what to whom, the encounters,
the level-end chain): it orders the sweep (a trigger before what it starts) and names each action's
expected effect, which the report checks. No coordinate is written by hand.

The plan (make_plan): objects of the groups a tester acts on are clustered by position (CLUSTER_RADIUS);
each cluster is one teleport (`goto`, `waitfor goto_done`, `wait_free`) and one audit (`online_audit`,
which also asks S for its side); then the cluster's actions, each followed by an audit. The generic
action per kind of object (classify): enemies fight_area at the cluster (one-hit damage and god mode
from the Debug settings keep it short), breakables attack_until, switches, levers, chests and other
interactions interact, pickups pickup, trigger volumes, scene and voice triggers and checkpoints are
entered by the teleport itself, the level's exit last. Objects with no generic action are visited and
audited only ("no action yet"). Every wait is a condition with a short timeout; nothing sleeps.

Sampling: by default each action family (the action and the object's name stem: TA_Ship_Destructible_04
-> TA_Ship_Destructible) is acted on at most SAMPLE times, every cluster is still visited; `--full` acts
on everything. When the estimate (estimate_seconds) is over the budget, visits of clusters with no
sampled action are dropped, farthest first, and the plan says what it left out.

The report (build_report): the audit lines of every peer (`Online: audit diff|norow|summary ...`), the
steps' own failure lines and the crash, folded per (severity, kind, class stem, what) with a count,
most severe first."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

# --- content ------------------------------------------------------------------------------------------

CLUSTER_RADIUS = 1200.0      # objects closer than this to a cluster's first object share its teleport
FAR_LIMIT = 150000.0         # a coordinate past this is a placement outside the playable level
SAMPLE = 3                   # acted-on objects per action family in a default pass
BUDGET_S = 600.0             # the owner's budget per level, wall clock, boot included

GROUP_ENEMIES = "enemies, spawners and encounters"
GROUP_PICKUPS = "pickups and collectibles"
GROUP_INTERACT = "switches, gates, locks, pads, movers, breakables, turrets, mini-games"
GROUP_TRIGGERS = "trigger volumes and level events"
GROUP_NPCS = "NPCs, conversations, scenes, voice-overs and hints"
GROUP_CHECKPOINTS = "checkpoints, level start and exit"
GROUPS_VISITED = (GROUP_ENEMIES, GROUP_PICKUPS, GROUP_INTERACT, GROUP_TRIGGERS, GROUP_NPCS, GROUP_CHECKPOINTS)


@dataclass
class Obj:
    name: str            # "" when the object has no name
    cls: str
    pos: tuple[float, float, float]
    group: str
    ident: str = ""      # "zone#index" or the static reader's own id
    state: str = ""


@dataclass
class Edge:
    """One logic edge: `sender` sends `message` to `target` (logic.json)."""
    sender: str
    message: str
    target: str
    effect: str = ""     # what the receiver does with it, when the reader's meaning table knows


@dataclass
class Content:
    level: str
    source: str
    start: tuple[float, float, float] | None
    objects: list[Obj]
    edges: list[Edge] = field(default_factory=list)
    level_end: list[str] = field(default_factory=list)  # the names in the level-end chain


PROBE_ROW = re.compile(
    r"^\s*(\d+)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(\S+)\s+(.+?)\s+\[([^\]]+)\]\s*(.*)$")
PROBE_GROUP = re.compile(r"^== (.+?): \d+\s*$")
PROBE_START = re.compile(r"^Level start: \S+ at (-?[\d.]+) (-?[\d.]+) (-?[\d.]+)")


def parse_probe_manifest(text: str, level: str) -> Content:
    """The authored probe's manifest (tools/level_manifest.py's text): its groups and rows."""
    objects: list[Obj] = []
    group = ""
    start = None
    for line in text.splitlines():
        m = PROBE_START.match(line)
        if m:
            start = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
            continue
        m = PROBE_GROUP.match(line)
        if m:
            group = m.group(1)
            continue
        if not group or group == "completeness":
            continue
        m = PROBE_ROW.match(line)
        if not m:
            continue
        name = m.group(6).strip()
        objects.append(Obj(name="" if name == "(no name)" else name, cls=m.group(5),
                           pos=(float(m.group(2)), float(m.group(3)), float(m.group(4))), group=group,
                           ident=m.group(7), state=m.group(8).strip()))
    return Content(level=level, source="probe manifest", start=start, objects=objects)


def _num3(v: object) -> tuple[float, float, float] | None:
    if isinstance(v, dict):
        v = [v.get("x"), v.get("y"), v.get("z")]
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(c, (int, float)) for c in v):
        return (float(v[0]), float(v[1]), float(v[2]))
    return None


def _first(d: dict, *keys: str, default: object = None) -> object:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def parse_logic_manifest(data: object, level: str) -> Content:
    """The static level reader's manifest.json. Its schema is the reader's (tools/level_logic.py); this
    reads it leniently: a list of entities, at the top or under `entities`/`objects`, or groups of them
    under `groups` ({group: [entity]} or [{group, entities}]); an entity's name, class, position (a list
    of three numbers or {x, y, z}), group and id under their plain names or the reader's
    (`refname`, `class_name`, `position`, `unit`)."""
    entities: list[tuple[str, dict]] = []
    start = None
    if isinstance(data, dict):
        start = _num3(_first(data, "start", "level_start", "player_start", default=None) or [])
        groups = data.get("groups")
        if isinstance(groups, dict):
            for g, items in groups.items():
                for e in items if isinstance(items, list) else []:
                    if isinstance(e, dict):
                        entities.append((str(g), e))
        elif isinstance(groups, list):
            for gg in groups:
                if isinstance(gg, dict):
                    g = str(_first(gg, "group", "name", "title", default=""))
                    for e in gg.get("entities", gg.get("objects", [])) or []:
                        if isinstance(e, dict):
                            entities.append((g, e))
        for key in ("entities", "objects"):
            for e in data.get(key, []) if isinstance(data.get(key), list) else []:
                if isinstance(e, dict):
                    entities.append((str(_first(e, "group", default="")), e))
    elif isinstance(data, list):
        entities = [(str(_first(e, "group", default="")), e) for e in data if isinstance(e, dict)]
    objects: list[Obj] = []
    for g, e in entities:
        pos = _num3(_first(e, "position", "pos", "world_position", default=None) or [])
        if pos is None:
            continue
        name = str(_first(e, "name", "refname", "ref_name", default=""))
        objects.append(Obj(name=name, cls=str(_first(e, "class", "class_name", "cls", default="?")), pos=pos,
                           group=str(_first(e, "group", default=g)), ident=str(_first(e, "id", "ident", "unit", default="")),
                           state=str(_first(e, "state", default=""))))
    return Content(level=level, source="static manifest", start=start, objects=objects)


def parse_logic(data: object) -> tuple[list[Edge], list[str]]:
    """The static reader's logic.json, leniently: every dict with a sender and a target (`from`/`sender`/
    `source`, `to`/`target`/`targets`) is an edge; `level_end` (a list of names, or of dicts with a name)
    is the level-end chain."""
    edges: list[Edge] = []
    level_end: list[str] = []

    def walk(o: object) -> None:
        if isinstance(o, dict):
            s = _first(o, "from", "sender", "source", default=None)
            t = _first(o, "to", "target", "targets", "receiver", default=None)
            if isinstance(s, str) and t is not None:
                targets = t if isinstance(t, list) else [t]
                for tt in targets:
                    if isinstance(tt, dict):
                        tt = _first(tt, "name", "refname", default="")
                    if isinstance(tt, str) and tt:
                        edges.append(Edge(sender=s, message=str(_first(o, "message", "msg", "message_class", default="")),
                                          target=tt, effect=str(_first(o, "effect", "meaning", default=""))))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    if isinstance(data, dict):
        le = data.get("level_end")
        for x in le if isinstance(le, list) else ([le] if le else []):
            name = x if isinstance(x, str) else (_first(x, "name", "refname", "sender", default="") if isinstance(x, dict) else "")
            if name:
                level_end.append(str(name))
    return edges, level_end


def load_content(folder, level: str) -> Content:
    """The level's content from the levelpass folder (a pathlib.Path): the static manifest when it
    exists, else the probe's; logic.json's edges when it exists. ValueError when neither manifest is
    there."""
    static = folder / level / "manifest.json"
    probe = folder / f"{level}-manifest.txt"
    if static.is_file():
        content = parse_logic_manifest(json.loads(static.read_text(encoding="utf-8")), level)
        content.source = str(static)
    elif probe.is_file():
        content = parse_probe_manifest(probe.read_text(encoding="utf-8", errors="replace"), level)
        content.source = str(probe)
    else:
        raise ValueError(f"no manifest for {level}: neither {static} nor {probe} (tools/level_logic.py or "
                         f"tools/level_manifest.py writes one)")
    logic = folder / level / "logic.json"
    if logic.is_file():
        content.edges, content.level_end = parse_logic(json.loads(logic.read_text(encoding="utf-8")))
    return content


# --- what to do with an object --------------------------------------------------------------------------

BREAKABLE = re.compile(r"destructible|breakable|crate|barrel|smash|bust|_break|break_", re.I)
INTERACT = re.compile(r"chest|switch|lever|winch|crank|button|valve|keylock|lock\b|_lock|gate|door|cage|plate|"
                      r"pull|wheel|turret|cannon", re.I)
EXIT = re.compile(r"level_?end|end_?level|exit|outro|finale|endencounter|end_encounter|levelcomplete", re.I)
SCENE = re.compile(r"^(vo_|cs_|hint|cutscene|cinematic|conversation|convo)", re.I)
KILL = re.compile(r"kill_?z|killzone|death_?zone|kill_?volume|deathplane|kill_?plane", re.I)
CHECKPOINT = re.compile(r"check_?point", re.I)
TRIGGER_CLASS = re.compile(r"trigger|volume", re.I)
TRIGGER_NAME = re.compile(r"trigger|volume|^tp_", re.I)
OUTLIER_FACTOR = 6.0       # an object farther from the start than this times the median is outside play
OUTLIER_FLOOR = 8000.0

# Actions: what the generator does at an object.
FIGHT, ATTACK, INTERACT_A, PICKUP, ENTER, TALK, EXIT_A, VISIT = (
    "fight", "attack", "interact", "pickup", "enter", "talk", "exit", "visit")
EXIT_FIGHT_S = 90
ACTION_ORDER = {ENTER: 0, TALK: 1, FIGHT: 2, ATTACK: 3, PICKUP: 4, INTERACT_A: 5, VISIT: 6, EXIT_A: 7}
# The timeout of each action's own command (seconds) and its expected share of it (estimate).
ACTION_TIMEOUT = {FIGHT: 25, ATTACK: 12, INTERACT_A: 10, PICKUP: 10, ENTER: 0, TALK: 10, EXIT_A: 0, VISIT: 0}
ACTION_EXPECT = {FIGHT: 15.0, ATTACK: 5.0, INTERACT_A: 6.0, PICKUP: 5.0, ENTER: 2.0, TALK: 6.0, EXIT_A: 30.0, VISIT: 0.0}


def classify(o: Obj) -> tuple[str, str]:
    """The generic action for an object and why (the report's "no action yet" names the why)."""
    name = o.name
    if name and EXIT.search(name) and o.group in (GROUP_CHECKPOINTS, GROUP_TRIGGERS, GROUP_ENEMIES, GROUP_NPCS):
        return EXIT_A, "the level's exit"
    if o.group == GROUP_ENEMIES:
        return FIGHT, "enemies, a spawner or an encounter: fought at the cluster"
    if o.group == GROUP_PICKUPS:
        if name and re.search(r"chest", name, re.I):
            return (INTERACT_A, "a chest") if name else (VISIT, "an unnamed chest")
        return (PICKUP, "a pickup") if name else (VISIT, "an unnamed pickup: no command can name it")
    if o.group == GROUP_INTERACT:
        if not name:
            return VISIT, "unnamed: no command can name it"
        if BREAKABLE.search(name):
            return ATTACK, "a breakable"
        if INTERACT.search(name):
            return INTERACT_A, "an interaction"
        return VISIT, "a mover, platform, pad or blocker: no generic action yet"
    if o.group == GROUP_NPCS:
        if name and o.cls.lower() in ("cactor", "cnpc") and not SCENE.search(name):
            return TALK, "an NPC: talked to"
        if name and SCENE.search(name):
            return ENTER, "a scene, voice-over or hint trigger: entered"
        return VISIT, "a ship, prop or unnamed actor of a scene: visited only"
    if o.group == GROUP_CHECKPOINTS and not (name and CHECKPOINT.search(name)):
        return VISIT, "a player start or level marker: visited only"
    if o.group in (GROUP_TRIGGERS, GROUP_CHECKPOINTS):
        if name and KILL.search(name):
            return VISIT, "a kill or death zone: never entered"
        if o.group == GROUP_TRIGGERS and not (TRIGGER_CLASS.search(o.cls) or (name and TRIGGER_NAME.search(name))):
            return VISIT, "a script event entity (no volume to enter)"
        return ENTER, "a trigger volume or checkpoint: entered"
    return VISIT, "visited only"


def stem(name: str) -> str:
    """A name without its trailing number: the family sampled together."""
    s = re.sub(r"[_\- ]*\d+$", "", name or "")
    return s or (name or "")


def placed(o: Obj) -> bool:
    return all(math.isfinite(c) and abs(c) < FAR_LIMIT for c in o.pos)


def dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist(a, b)


@dataclass
class Cluster:
    index: int
    center: tuple[float, float, float]
    objects: list[Obj]


def cluster(objects: list[Obj], radius: float = CLUSTER_RADIUS) -> list[Cluster]:
    """Greedy, deterministic: objects in their given order; each joins the first cluster whose seed is
    within `radius`, else seeds a new one. A cluster's teleport point is its seed's position (a placed
    object, so the point is inside the level), lifted by nothing."""
    out: list[Cluster] = []
    for o in objects:
        for c in out:
            if dist(c.center, o.pos) <= radius:
                c.objects.append(o)
                break
        else:
            out.append(Cluster(index=len(out), center=o.pos, objects=[o]))
    return out


def tour(clusters: list[Cluster], start: tuple[float, float, float] | None,
         first: list[str] | None = None) -> list[Cluster]:
    """The visiting order: nearest neighbour from the level start (deterministic, ties by index). With
    logic edges, `first` names objects whose clusters must come before others (a trigger before what it
    starts); the exit's cluster always last."""
    left = list(clusters)
    exits = [c for c in left if any(classify(o)[0] == EXIT_A for o in c.objects)]
    for c in exits:
        left.remove(c)
    order: list[Cluster] = []
    here = start if start is not None else (left[0].center if left else (0.0, 0.0, 0.0))
    while left:
        nxt = min(left, key=lambda c: (dist(here, c.center), c.index))
        order.append(nxt)
        left.remove(nxt)
        here = nxt.center
    if first:
        rank = {n: i for i, n in enumerate(first)}

        def key(c: Cluster) -> int:
            return min((rank[o.name] for o in c.objects if o.name in rank), default=len(rank))
        # A stable sort: within the logic's order the tour's order stays.
        order = sorted(order, key=key)
    return order + exits


def logic_order(edges: list[Edge]) -> list[str]:
    """Senders before their targets (a topological order of the names in the edges; cycles broken by
    name order)."""
    succ: dict[str, set[str]] = {}
    indeg: dict[str, int] = {}
    for e in edges:
        for n in (e.sender, e.target):
            succ.setdefault(n, set())
            indeg.setdefault(n, 0)
        if e.target not in succ[e.sender] and e.sender != e.target:
            succ[e.sender].add(e.target)
            indeg[e.target] += 1
    out: list[str] = []
    ready = sorted(n for n, d in indeg.items() if d == 0)
    while ready or len(out) < len(indeg):
        if not ready:  # a cycle: take the smallest name left
            ready = [min(n for n in indeg if n not in out)]
            indeg[ready[0]] = 0
        n = ready.pop(0)
        if n in out:
            continue
        out.append(n)
        for t in sorted(succ[n]):
            indeg[t] -= 1
            if indeg[t] == 0 and t not in out:
                ready.append(t)
        ready.sort()
    return out


# --- the plan --------------------------------------------------------------------------------------------

@dataclass
class Action:
    action: str
    obj: Obj
    why: str
    expect: list[str] = field(default_factory=list)  # logic: what it should cause ("<message> -> <target>")


@dataclass
class Stop:
    label: str                      # v<n>: the visit's audit label; actions a<n>_<k>
    cluster: Cluster
    actions: list[Action]
    visit_only: list[tuple[Obj, str]]


@dataclass
class Plan:
    level: str
    source: str
    stops: list[Stop]
    left_out: list[str]             # what a default pass leaves out, in words
    no_action: dict[str, int]       # "class stem: why" -> count of objects visited only
    unplaced: int
    estimate_s: float
    full: bool
    late_join: bool
    exit_stop: Stop | None


# Seconds the boot and the level load take, measured (the report prints the real ones); used for the
# estimate only.
BOOT_S = 95.0
LEVEL_LOAD_S = 45.0
VISIT_S = 6.0          # goto, goto_done, wait_free, one audit (2 s settle)
AUDIT_S = 2.5
LATE_JOIN_S = 60.0     # D's own visit after it is in (its boot runs beside H's sweep)
EXIT_S = 90.0


def estimate_seconds(stops: list[Stop], late_join: bool) -> float:
    t = BOOT_S + LEVEL_LOAD_S + EXIT_S
    for s in stops:
        t += VISIT_S
        for a in s.actions:
            t += ACTION_EXPECT[a.action] + AUDIT_S
    if late_join:
        t += LATE_JOIN_S
    return t


def make_plan(content: Content, full: bool = False, late_join: bool = True, budget_s: float = BUDGET_S,
              sample: int = SAMPLE) -> Plan:
    visited = [o for o in content.objects if o.group in GROUPS_VISITED]
    unplaced = sum(1 for o in visited if not placed(o))
    visited = [o for o in visited if placed(o)]
    start = content.start
    if start is not None and visited:
        # Script events placed far outside the play area (a level's crossbeams, its far-off markers).
        ds = sorted(dist(start, o.pos) for o in visited)
        limit = max(OUTLIER_FLOOR, OUTLIER_FACTOR * ds[len(ds) // 2])
        # The level's exit is never an outlier: a level's end can be far from its start.
        keep = [o for o in visited if dist(start, o.pos) <= limit or classify(o)[0] == EXIT_A]
        unplaced += len(visited) - len(keep)
        visited = keep
    # A stable order: by distance from the start, then name (the clusters' seeds come first in it).
    visited.sort(key=lambda o: (dist(start, o.pos) if start else 0.0, o.name, o.ident))
    clusters = cluster(visited)
    first = logic_order(content.edges) if content.edges else None
    ordered = tour(clusters, start, first)
    # The logic's expected effects per sender.
    effects: dict[str, list[str]] = {}
    for e in content.edges:
        effects.setdefault(e.sender, []).append(f"{e.message or 'message'} -> {e.target}"
                                                + (f" ({e.effect})" if e.effect else ""))
    taken: dict[tuple[str, str], int] = {}
    left: dict[tuple[str, str], int] = {}
    no_action: dict[str, int] = {}
    stops: list[Stop] = []
    exit_stop = None
    for n, c in enumerate(ordered):
        actions: list[Action] = []
        visit_only: list[tuple[Obj, str]] = []
        fought = False
        for o in sorted(c.objects, key=lambda o: (ACTION_ORDER[classify(o)[0]], o.name, o.ident)):
            act, why = classify(o)
            if act == VISIT:
                visit_only.append((o, why))
                no_action_key = f"{stem(o.name) or o.cls}: {why}"
                no_action[no_action_key] = no_action.get(no_action_key, 0) + 1
                continue
            if act == FIGHT:
                if fought:
                    continue  # one fight per cluster covers its spawners and encounters
                fought = True
            family = (act, stem(o.name) or o.cls)
            if not full and act not in (EXIT_A, FIGHT) and taken.get(family, 0) >= sample:
                left[family] = left.get(family, 0) + 1
                visit_only.append((o, f"sampled out ({act}: {sample} of its family taken)"))
                continue
            taken[family] = taken.get(family, 0) + 1
            actions.append(Action(action=act, obj=o, why=why, expect=effects.get(o.name, [])))
        stop = Stop(label=f"v{n + 1}", cluster=c, actions=actions, visit_only=visit_only)
        stops.append(stop)
        if any(a.action == EXIT_A for a in actions):
            exit_stop = stop
    left_out = [f"{family[0]} {family[1]}: {k} more not acted on (sampled)" for family, k in sorted(left.items())]
    # Over the budget: drop visits of stops with no action, farthest from the start first; then say so.
    est = estimate_seconds(stops, late_join)
    if est > budget_s:
        quiet = [s for s in stops if not s.actions]
        quiet.sort(key=lambda s: -(dist(start, s.cluster.center) if start else s.cluster.index))
        dropped = 0
        for s in quiet:
            if est <= budget_s:
                break
            stops.remove(s)
            est -= VISIT_S
            dropped += 1
        if dropped:
            left_out.append(f"{dropped} visit(s) of clusters with no sampled action (over the budget)")
    return Plan(level=content.level, source=content.source, stops=stops, left_out=left_out, no_action=no_action,
                unplaced=unplaced, estimate_s=round(est, 1), full=full, late_join=late_join, exit_stop=exit_stop)


# --- the scripts -------------------------------------------------------------------------------------------

def _xyz(p: tuple[float, float, float], lift: float = 0.0) -> str:
    return f"{p[0]:.1f} {p[1]:.1f} {p[2] + lift:.1f}"


def _safe(name: str) -> str:
    # The input script splits at every ';' and starts a comment at '#'.
    return name.replace(";", "").replace("#", "")


GOTO_LIFT = 60.0   # a teleport lands this far above the placed point (no landing inside the floor)
FIGURE_H = "Roller Brawl.dump"
FIGURE_D = "Scorp.dump"


def boot_lines(host: bool, figure: str) -> list[str]:
    """The shortest boot the specs use: title, Host (H), the two menu presses, the figure, the hub; no
    fixed wait anywhere (each step a condition)."""
    lines = ["waitfor online_title 300", "waitfor title_ready 240"]
    if host:
        lines += ["online_host 0", "waitfor online_connected 60"]
    lines += ["tap start", "waitfor menu_ready 60", "tap a", "waitfor menu_ready 60", "tap a",
              "waitfor front_end_left 120", f"place 0 {{figures}}/{figure}"]
    return lines


def action_lines(a: Action, label: str) -> list[str]:
    o = a.obj
    n = _safe(o.name)
    out = [f"log LEVELPASS act {label} {a.action} {n or '-'} {o.cls} at {_xyz(o.pos)}"]
    if a.action == FIGHT:
        out.append(f"fight_area {_xyz(o.pos)} 1500 {ACTION_TIMEOUT[FIGHT]}")
    elif a.action == ATTACK:
        out.append(f"attack_until {n} {ACTION_TIMEOUT[ATTACK]}")
    elif a.action == INTERACT_A:
        out.append(f"interact {n} {ACTION_TIMEOUT[INTERACT_A]}")
    elif a.action == PICKUP:
        out.append(f"pickup {n} {ACTION_TIMEOUT[PICKUP]}")
    elif a.action == TALK:
        out.append(f"interact {n} {ACTION_TIMEOUT[TALK]}")
    elif a.action == EXIT_A and o.group == GROUP_ENEMIES:
        out.append(f"fight_area {_xyz(o.pos)} 1500 {EXIT_FIGHT_S}")
    elif a.action in (ENTER, EXIT_A):
        out += [f"goto {_xyz(o.pos, GOTO_LIFT)}", "waitfor goto_done 10"]
    out += ["wait_free 30", f"online_audit {label}"]
    return out


def script_h(plan: Plan, late_join_after: str | None) -> str:
    """H's script: boot, Host, the level, the sweep, the scenes left, the late join's wait, the exit."""
    lines = boot_lines(True, FIGURE_H)
    lines += ["waitfor online_server_hub 300", "online_mark_level", f"level {plan.level}",
              "waitfor online_level_after_mark 300", "waitfor online_barrier 300", "wait_free 120",
              "log LEVELPASS phase sweep", "online_audit start"]
    n_stops = len(plan.stops)
    for i, s in enumerate(plan.stops):
        if late_join_after is not None and plan.late_join and i == max(0, n_stops - LATE_JOIN_LEAD):
            lines.append("log LEVELPASS late-join start")
        if s is plan.exit_stop:
            continue
        lines += stop_lines(s)
    lines.append("log LEVELPASS phase audit")
    lines.append("online_audit end")
    if plan.late_join and late_join_after is not None:
        lines += ["log LEVELPASS phase late-join", "online_wait_peers 2 300",
                  f"online_wait_peer_near {_xyz(late_join_point(plan))} 400 300", "online_audit late"]
    lines.append("log LEVELPASS phase exit")
    if plan.exit_stop is not None:
        lines += stop_lines(plan.exit_stop)
        lines += ["waitfor online_level_end 60", "waitfor online_past_level_end 240", "wait_free 120",
                  "log LEVELPASS phase next-level", "online_audit next"]
    else:
        lines.append("log LEVELPASS exit none (no level-end candidate in the content)")
    lines += ["log LEVELPASS done", "online_end", "quit"]
    return "\n".join(lines) + "\n"


LATE_JOIN_LEAD = 6     # D starts this many stops before the sweep ends (its boot runs beside them)


def late_join_point(plan: Plan) -> tuple[float, float, float]:
    """Where D ends its visit (H waits for D's pose there): the last acted-on stop's point, else the
    first stop's."""
    acted = [s for s in plan.stops if s.actions and s is not plan.exit_stop]
    s = acted[-1] if acted else plan.stops[0]
    return s.cluster.center


def stop_lines(s: Stop) -> list[str]:
    c = s.cluster
    names = ", ".join(sorted({_safe(o.name) for o in c.objects if o.name})[:6]) or "-"
    out = [f"log LEVELPASS visit {s.label} at {_xyz(c.center)} objects {len(c.objects)} ({names})",
           f"goto {_xyz(c.center, GOTO_LIFT)}", "waitfor goto_done 10", "wait_free 20", f"online_audit {s.label}"]
    for k, a in enumerate(s.actions):
        if a.action in (ENTER, EXIT_A) and a.obj.pos == c.center and a.obj.group != GROUP_ENEMIES:
            out.append(f"log LEVELPASS act {s.label}_{k + 1} {a.action} {_safe(a.obj.name) or '-'} {a.obj.cls} "
                       f"at {_xyz(a.obj.pos)} (the visit's teleport entered it)")
            continue
        out += action_lines(a, f"{s.label}_{k + 1}")
    return out


def script_d(plan: Plan) -> str:
    """D's script (the late joiner): boot, its own hub, join the session ({join}), then the visit and
    audit of the stops H acted on, ending at the late-join point H waits for."""
    lines = boot_lines(False, FIGURE_D)
    lines += ["waitfor online_in_play 300", "online_join {join}", "waitfor online_barrier 300", "wait_free 120",
              "log LEVELPASS D in", "online_audit d-in"]
    acted = [s for s in plan.stops if s.actions and s is not plan.exit_stop]
    for s in acted:
        lines += [f"log LEVELPASS D visit {s.label}", f"goto {_xyz(s.cluster.center, GOTO_LIFT)}", "waitfor goto_done 10",
                  "wait_free 20", f"online_audit d-{s.label}"]
    end = late_join_point(plan)
    lines += [f"goto {_xyz(end, GOTO_LIFT)}", "waitfor goto_done 10", "log LEVELPASS D done",
              "waitfor online_left 900", "quit"]
    return "\n".join(lines) + "\n"


# --- the report ------------------------------------------------------------------------------------------

SEVERITY = ("crash", "difference after an action", "difference at rest", "object without a row",
            "row without an object", "action failed", "step failed", "not shared (documented)")

AUDIT_DIFF = re.compile(r"Online: audit diff (\S+) (\S+) (\S+) (\S+) slot (\d+) rev (\d+) (\S+) at (\S+) local (\S+) "
                        r"row (\S+) live (\S+)(?: why (.*))?$")
AUDIT_NOROW = re.compile(r"Online: audit norow (\S+) (\S+) (\S+) (\S+) at (\S+)$")
AUDIT_SUMMARY = re.compile(r"Online: audit summary (\S+) (\S+) rows (\d+) differences (\d+) .*$")
STEP_FAIL = re.compile(r"(Input script: waitfor (\S+) timed out.*|Dev: (?:\S+ )*(?:failed|stopped|gives up)\b.*|"
                       r"Dev: wait_free stopped.*|Online: audit \S+: not done in 20 s|Online: audit \S+ refused.*)")
MARK = re.compile(r"Input script: LEVELPASS (.*)$")
TIME = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]")
CRASH = re.compile(r"Unhandled guest access violation|FATAL|Fatal error|crashed|access violation", re.I)


@dataclass
class Finding:
    severity: str
    kind: str          # the row kind, or the action
    family: str        # the object's class or name stem
    what: str          # the words that fold findings together
    obj: str           # the object (Name#id or name) and where
    step: str          # the audit label or step
    server: str = ""   # S's value
    client: str = ""   # the client's value
    lines: list[str] = field(default_factory=list)
    peer: str = ""


def label_kind(label: str) -> str:
    """What an audit label stands for: a visit (v<n>), an action (v<n>_<k>), a late joiner's (d-...)."""
    if re.fullmatch(r"v\d+_\d+", label):
        return "action"
    return "visit"


def findings_from_log(peer: str, text: str, labels: dict[str, str]) -> list[Finding]:
    """The audit lines and step failures of one peer's log. `labels` maps an audit label to the step's
    words (the LEVELPASS act/visit line before it)."""
    out: list[Finding] = []
    for line in text.splitlines():
        m = AUDIT_DIFF.search(line)
        if m:
            who, label, kind, entity, _slot, _rev, outcome, where, local, row, live, why = m.groups()
            fam = stem(local.split("#")[0]) if local != "-" else entity.split(":")[0]
            if outcome == "no_object":
                sev = "row without an object"
            else:
                sev = "difference after an action" if label_kind(label) == "action" else "difference at rest"
            out.append(Finding(severity=sev, kind=kind, family=fam, what=f"{outcome} {why or ''}".strip(),
                               obj=f"{local} {entity} at {where}", step=f"{label} {labels.get(label, '')}".strip(),
                               server=row, client=live, lines=[line.strip()], peer=f"{peer}/{who}"))
            continue
        m = AUDIT_NOROW.search(line)
        if m:
            who, label, kind, local, where = m.groups()
            out.append(Finding(severity="object without a row", kind=kind, family=stem(local.split("#")[0]),
                               what="tracked here, no row resolves to it", obj=f"{local} at {where}",
                               step=f"{label} {labels.get(label, '')}".strip(), lines=[line.strip()],
                               peer=f"{peer}/{who}"))
            continue
        m = STEP_FAIL.search(line)
        if m:
            words = m.group(1)
            sev = "action failed" if re.search(r"attack_until|interact|pickup|fight_area|encounter", words) else "step failed"
            what = re.sub(r"\d+(\.\d+)?", "N", words)[:140]
            out.append(Finding(severity=sev, kind="step", family=words.split(" ")[1] if " " in words else words,
                               what=what, obj="", step="", lines=[line.strip()], peer=peer))
    return out


def step_labels(script: str) -> dict[str, str]:
    """audit label -> the step's words, from the generated script's LEVELPASS lines."""
    labels: dict[str, str] = {}
    last = ""
    for line in script.splitlines():
        if line.startswith("log LEVELPASS "):
            last = line[len("log LEVELPASS "):]
        elif line.startswith("online_audit "):
            labels[line.split()[1]] = last
    return labels


def fold(findings: list[Finding]) -> list[tuple[Finding, int, list[Finding]]]:
    """Findings of one severity, kind, family and what are one entry with a count; most severe first,
    then by count, then by name (stable)."""
    groups: dict[tuple[str, str, str, str], list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.severity, f.kind, f.family, f.what), []).append(f)
    rank = {s: i for i, s in enumerate(SEVERITY)}
    entries = [(fs[0], len(fs), fs) for fs in groups.values()]
    entries.sort(key=lambda e: (rank.get(e[0].severity, len(rank)), -e[1], e[0].kind, e[0].family, e[0].what))
    return entries


def phase_times(text: str) -> list[tuple[str, float]]:
    """Seconds per phase from the H log's LEVELPASS marks (boot: the log's first line to `phase sweep`)."""
    from datetime import datetime
    marks: list[tuple[str, float]] = []
    first = None
    for line in text.splitlines():
        t = TIME.match(line)
        if not t:
            continue
        ts = datetime.strptime(t.group(1)[:23], "%Y-%m-%d %H:%M:%S.%f").timestamp()
        if first is None:
            first = ts
            marks.append(("boot and level load", ts))
        m = MARK.search(line)
        if m and m.group(1).startswith(("phase ", "done")):
            marks.append((m.group(1).replace("phase ", ""), ts))
    out = []
    for (name, ts), (_n2, ts2) in zip(marks, marks[1:]):
        out.append((name, round(ts2 - ts, 1)))
    return out


def summaries(text: str) -> list[str]:
    return [m.group(0) for m in (AUDIT_SUMMARY.search(l) for l in text.splitlines()) if m]


def report_text(level: str, plan: Plan, wall_s: float, phases: list[tuple[str, float]], folded,
                crash: str, sums: dict[str, list[str]], run_folder: str, notes: list[str]) -> str:
    lines = [f"Level pass: {level}", f"run folder: {run_folder}", f"content: {plan.source}",
             f"wall clock: {wall_s:.0f} s ({'under' if wall_s <= BUDGET_S else 'OVER'} the {BUDGET_S:.0f} s budget); "
             f"estimate was {plan.estimate_s:.0f} s",
             f"pass: {'full' if plan.full else 'sampled (each action family at most ' + str(SAMPLE) + ' times)'}, "
             f"{len(plan.stops)} stops, {sum(len(s.actions) for s in plan.stops)} actions, "
             f"late join {'on' if plan.late_join else 'off'}"]
    lines.append("")
    lines.append("time per phase:")
    for name, s in phases:
        lines.append(f"  {name}: {s:.1f} s")
    total = sum(n for _f, n, _fs in folded)
    lines.append("")
    lines.append(f"findings: {total} in {len(folded)} entries" + (f"; CRASH: {crash}" if crash else ""))
    for f, n, fs in folded:
        lines.append(f"- [{f.severity}] {f.kind} {f.family}: {f.what} (x{n})")
        for g in fs[:3]:
            where = f"    {g.peer} {g.step}: {g.obj}".rstrip()
            lines.append(where)
            if g.server or g.client:
                lines.append(f"      S {g.server} / client {g.client}")
            for l in g.lines[:1]:
                lines.append(f"      {l[:300]}")
        if n > 3:
            lines.append(f"    ... and {n - 3} more")
    lines.append("")
    lines.append("audit summaries (last per peer):")
    for peer, ss in sums.items():
        for s in ss[-2:]:
            lines.append(f"  {peer}: {s}")
    lines.append("")
    lines.append("not acted on:")
    for k, n in sorted(plan.no_action.items()):
        lines.append(f"  {k} (x{n})")
    for l in plan.left_out:
        lines.append(f"  {l}")
    if plan.unplaced:
        lines.append(f"  {plan.unplaced} object(s) placed outside the playable level (not visited)")
    for n in notes:
        lines.append(f"note: {n}")
    return "\n".join(lines) + "\n"


# --- the case ------------------------------------------------------------------------------------------

LATE_JOIN_MARK = r"Input script: LEVELPASS late-join start"
# H: the audit and its pipe to S (dev_backend_commands, which Host forwards), the late joiner let in
# by its loopback address (dev_hello_ungated_loopback, forwarded), the Debug page's one-hit damage and
# god mode to keep fights short and the hero alive (dev_damage_multiplier is forwarded to S, which
# scales the puppets' hits), the fight helpers' lowering setting (refused in a session in its own
# words, never "is off"), the encounter lines, logs large enough for the whole pass.
H_ARGS = ["--dev_backend_commands=true", "--dev_hello_ungated_loopback=true", "--dev_damage_multiplier=-1",
          "--dev_hero_invulnerable=true", "--dev_defeat_commands=true", "--dev_trigger_commands=true",
          "--log_max_file_size_mb=64", "--log_max_files=4"]
D_ARGS = ["--dev_backend_commands=true", "--dev_damage_multiplier=-1", "--dev_hero_invulnerable=true",
          "--log_max_file_size_mb=64", "--log_max_files=4"]
CASE_TIMEOUT_S = 1200   # the hard stop: twice the budget


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join("'" + i.replace("'", "") + "'" for i in items) + "]"


def case_name(level: str) -> str:
    return f"levelpass-{level}"


def spec_toml(plan: Plan) -> str:
    """The generated case (a skytest spec): H hosts with a hidden server (the release's draw skip), D
    starts once H's log says LEVELPASS late-join start and joins by the loopback address. The shader
    cache comes from the build snapshot. Fail fast on a dead session or a crash; nothing else stops the
    pass: every step's failure is a finding of the report, not a failed expectation."""
    late = plan.late_join
    h = script_h(plan, LATE_JOIN_MARK if late else None)
    out = [f'name = "{case_name(plan.level)}"',
           f'layout = "{"server+2" if late else "server+1"}"',
           'stage = "8"',
           'server = "host"',
           f'description = "The automatic level pass of {plan.level} (skytest levelpass; generated, not committed)"',
           f"timeout = {CASE_TIMEOUT_S}",
           'scenes = "skip"',
           "show_server_window = false",
           'shader_cache = "snapshot"',
           "",
           "[peer.H]",
           f"args = {_toml_list(H_ARGS)}",
           "script = \'\'\'\n" + h + "\'\'\'",
           ]
    if late:
        out += ["", "[peer.D]", f"args = {_toml_list(D_ARGS)}", f"start_after = '{LATE_JOIN_MARK}'",
                "script = \'\'\'\n" + script_d(plan) + "\'\'\'"]
    return "\n".join(out) + "\n"
