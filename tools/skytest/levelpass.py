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
GROUP_OTHER = "other"
GROUPS_VISITED = (GROUP_ENEMIES, GROUP_PICKUPS, GROUP_INTERACT, GROUP_TRIGGERS, GROUP_NPCS, GROUP_CHECKPOINTS,
                  GROUP_OTHER)  # other: visited and audited only
# The static reader's groups (tools/level_logic.py manifest.json schema 1) as the probe's groups; scenery
# and other are not visited.
STATIC_GROUPS = {
    "enemy": GROUP_ENEMIES, "spawner": GROUP_ENEMIES, "encounter": GROUP_ENEMIES,
    "pickup": GROUP_PICKUPS, "collectible": GROUP_PICKUPS, "chest": GROUP_PICKUPS,
    "switch": GROUP_INTERACT, "gate": GROUP_INTERACT, "lock": GROUP_INTERACT, "bounce": GROUP_INTERACT,
    "mover": GROUP_INTERACT, "teleporter": GROUP_INTERACT, "pushblock": GROUP_INTERACT,
    "breakable": GROUP_INTERACT, "turret": GROUP_INTERACT, "minigame": GROUP_INTERACT,
    "trigger": GROUP_TRIGGERS, "conversation": GROUP_NPCS, "cutscene": GROUP_NPCS, "npc": GROUP_NPCS,
    "checkpoint": GROUP_CHECKPOINTS, "level_start": GROUP_CHECKPOINTS, "level_end": GROUP_CHECKPOINTS,
    "other": GROUP_OTHER,
}


@dataclass
class Obj:
    name: str            # "" when the object has no name
    cls: str
    pos: tuple[float, float, float]
    group: str
    ident: str = ""      # "zone#index" or the static reader's own id
    state: str = ""
    kind: str = ""       # the static reader's most telling component or archetype
    box: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None  # a trigger's world box
    fires: list[str] = field(default_factory=list)  # "<message> -> <target>" the entity sends
    along: float | None = None  # distance along the level path (the static reader's sort key)
    loads_map: bool = False      # it sends CLoadMapComponentMessage: entered only as the level's exit
    ref: str = ""                # the live reference a script step names it by (attach_refs); "" for none
    zone: str = ""               # the static reader's zone (the level or one of its sub-zones)

    def point(self) -> tuple[float, float, float]:
        """Where a teleport goes to enter it: its placed position when that is inside its trigger box
        (or it has none), else the box's centre at mid-height."""
        if self.box is not None:
            (a, b) = self.box
            inside = all(min(a[i], b[i]) - 1.0 <= self.pos[i] <= max(a[i], b[i]) + 1.0 for i in range(3))
            if not inside:
                return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)
        return self.pos


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
    level_end_receiver: str = ""  # the chain's first receiver (an encounter manager), when it names one
    authored: int = 0             # objects whose live reference is the probe's exact id


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


def parse_static_manifest(data: dict, level: str) -> Content:
    """tools/level_logic.py's manifest.json, schema 1: entities with name, class, kind, group, position,
    box, fires and along; the level-end chain's text (its first senders are the exit)."""
    objects: list[Obj] = []
    start = None
    start_name = str(data.get("level_start") or "").split(".")[-1]
    for e in data.get("entities", []):
        if not isinstance(e, dict):
            continue
        pos = _num3(e.get("position"))
        if pos is None:
            continue
        group = STATIC_GROUPS.get(str(e.get("group", "")), "")
        box = None
        b = e.get("box")
        if isinstance(b, dict) and _num3(b.get("min")) and _num3(b.get("max")):
            box = (_num3(b["min"]), _num3(b["max"]))
        fires = []
        loads = False
        for f in e.get("fires", []) or []:
            if not isinstance(f, dict):
                continue
            msg = str(f.get("message", ""))
            loads = loads or "LoadMap" in msg
            to = f.get("to") or []
            fires.append(f"{msg} -> {', '.join(str(t) for t in to) or '-'}")
        name = str(e.get("name", ""))
        o = Obj(name=name, cls=str(e.get("class", "?")), pos=pos, group=group,
                ident=f"{e.get('unit', '')}", kind=str(e.get("kind", "")), box=box, fires=fires,
                along=float(e["along"]) if isinstance(e.get("along"), (int, float)) else None, loads_map=loads)
        o.state = str(e.get("group", ""))  # the static group itself
        o.zone = str(e.get("zone", ""))
        objects.append(o)
        if start is None and e.get("group") == "level_start" and (name == start_name or not start_name):
            start = pos
    content = Content(level=level, source="static manifest", start=start, objects=objects)
    content.level_end = exit_senders(data.get("level_end"))
    content.level_end_receiver = exit_receiver(data.get("level_end"))
    return content


def exit_receiver(level_end: object) -> str:
    """The first chain's first receiver ("{A} -[message]-> R => ..."); "" for none or "-"."""
    chains = level_end if isinstance(level_end, list) else ([level_end] if level_end else [])
    for chain in chains:
        m = re.match(r"\s*\{[^}]*\}\s*-\[.*?\]->\s*(\S+)", str(chain))
        if m and m.group(1) not in ("-", "?"):
            return m.group(1)
    return ""


def exit_senders(level_end: object) -> list[str]:
    """Every name of each level-end chain in its order: the first senders ("{A, B} -[message]-> C" or
    "(inferred) A -[message]-> A"), then each later sender and receiver ("=> C -[...]-> D"); `?`,
    `-` and roles dropped. make_plan acts on the first of them a player can act on."""
    out: list[str] = []

    def add(n: str) -> None:
        n = n.strip()
        if n and n not in ("?", "-") and not n.startswith("roles") and n not in out:
            out.append(n)

    for chain in level_end if isinstance(level_end, list) else ([level_end] if level_end else []):
        for seg in str(chain).split("=>"):
            seg = seg.strip()
            if seg.startswith("(inferred)"):
                seg = seg[len("(inferred)"):].strip()
            m = re.match(r"\{([^}]*)\}", seg)
            if m:
                for n in m.group(1).split(","):
                    add(n)
            else:
                m = re.match(r"(\S+)", seg)
                if m:
                    add(m.group(1))
            r = re.search(r"\]->\s*(\S+)", seg)
            if r:
                add(r.group(1))
    return out


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
        data = json.loads(static.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema") == 1:
            content = parse_static_manifest(data, level)
        else:
            content = parse_logic_manifest(data, level)
        content.source = str(static)
    elif probe.is_file():
        content = parse_probe_manifest(probe.read_text(encoding="utf-8", errors="replace"), level)
        content.source = str(probe)
    else:
        raise ValueError(f"no manifest for {level}: neither {static} nor {probe} (tools/level_logic.py or "
                         f"tools/level_manifest.py writes one)")
    # Live references: the probe's authored ids joined to the content (the probe's own content: its ids).
    if probe.is_file():
        probe_content = content if content.source == str(probe) else \
            parse_probe_manifest(probe.read_text(encoding="utf-8", errors="replace"), level)
        content.authored = attach_refs(content.objects, probe_content.objects)
    else:
        content.authored = attach_refs(content.objects, None)
    logic = folder / level / "logic.json"
    if content.objects and any(o.fires for o in content.objects):
        # The static manifest carries every sender's telegrams itself.
        content.edges = [Edge(sender=o.name, message=f.split(" -> ")[0], target=t.strip())
                         for o in content.objects for f in o.fires for t in f.split(" -> ", 1)[1].split(",")
                         if o.name and t.strip() not in ("", "-")]
    elif logic.is_file():
        edges, end = parse_logic(json.loads(logic.read_text(encoding="utf-8")))
        content.edges = edges
        content.level_end = content.level_end or end
    return content


# --- what to do with an object --------------------------------------------------------------------------

BREAKABLE = re.compile(r"destructible|breakable|crate|barrel|smash|bust|_break|break_", re.I)
INTERACT = re.compile(r"chest|switch|lever|winch|crank|button|valve|keylock|lock\b|_lock|gate|door|cage|plate|"
                      r"pull|wheel|turret|cannon", re.I)
EXIT = re.compile(r"level_?end|end_?level|exit|outro|finale|endencounter|end_encounter|levelcomplete", re.I)
SCENE = re.compile(r"^(vo_|cs_|hint|cutscene|cinematic|conversation|convo)", re.I)
DEBUG = re.compile(r"^debug|_debug|debug_|^test_|_test\b", re.I)
KILL = re.compile(r"kill_?z|killzone|death_?zone|kill_?volume|deathplane|kill_?plane", re.I)
CHECKPOINT = re.compile(r"check_?point", re.I)
TRIGGER_CLASS = re.compile(r"trigger|volume", re.I)
TRIGGER_NAME = re.compile(r"trigger|volume|^tp_", re.I)
OUTLIER_FACTOR = 6.0       # an object farther from the start than this times the median is outside play
OUTLIER_FLOOR = 8000.0

# Actions: what the generator does at an object.
FIGHT, ATTACK, INTERACT_A, PICKUP, ENTER, TALK, EXIT_A, VISIT = (
    "fight", "attack", "interact", "pickup", "enter", "talk", "exit", "visit")
EXIT_FIGHT_S = 150
EXIT_LATE_WAVE_S = 45
EXIT_RADIUS = 3000.0
EXIT_NEAR = 5000.0     # the level-end senders taken: those this close to the chain's receiver
EXIT_FIGHT_RADIUS = 2500   # the level-end senders this close share one exit fight (an arena)
ACTION_ORDER = {ENTER: 0, TALK: 1, FIGHT: 2, ATTACK: 3, PICKUP: 4, INTERACT_A: 5, VISIT: 6, EXIT_A: 7}
# The timeout of each action's own command (seconds) and its expected share of it (estimate).
ACTION_TIMEOUT = {FIGHT: 25, ATTACK: 12, INTERACT_A: 10, PICKUP: 10, ENTER: 0, TALK: 10, EXIT_A: 0, VISIT: 0}
ACTION_EXPECT = {FIGHT: 15.0, ATTACK: 5.0, INTERACT_A: 6.0, PICKUP: 5.0, ENTER: 2.0, TALK: 6.0, EXIT_A: 30.0, VISIT: 0.0}


STATIC_ACTION = {
    "enemy": (FIGHT_S := "fight", "an enemy: fought at the cluster"),
    "spawner": ("fight", "a spawner: fought at the cluster"),
    "encounter": ("fight", "an encounter: fought at the cluster"),
    "breakable": ("attack", "a breakable"),
    "switch": ("interact", "a switch or interaction"),
    "lock": ("interact", "a lock (a key gate's keylock or a battle gate)"),
    "chest": ("interact", "a chest"),
    "pickup": ("pickup", "a pickup"),
    "collectible": ("pickup", "a collectible"),
    "npc": ("talk", "an NPC: talked to"),
    "checkpoint": ("enter", "a checkpoint: entered"),
    "trigger": ("enter", "a trigger volume: entered"),
    "gate": ("visit", "a gate: audited before and after its opener"),
    "bounce": ("visit", "a bounce pad: no generic action yet"),
    "mover": ("visit", "a mover: no generic action yet"),
    "teleporter": ("visit", "a teleporter: no generic action yet"),
    "pushblock": ("visit", "a push block: no generic action yet"),
    "turret": ("visit", "a turret: no generic action yet"),
    "minigame": ("visit", "a mini-game: no generic action yet"),
    "conversation": ("visit", "a voice-over or conversation entity (started by its trigger)"),
    "cutscene": ("enter", "a cutscene trigger: entered"),
    "level_start": ("visit", "a player start: visited only"),
    "level_end": ("visit", "the level's end (its chain's senders are acted on at the exit)"),
    "other": ("visit", "a generic entity: visited and audited only"),
}


def classify(o: Obj) -> tuple[str, str]:
    """The generic action for an object and why (the report's "no action yet" names the why)."""
    name = o.name
    if name and DEBUG.search(name):
        return VISIT, "a level designer's debug object: never pressed"
    if o.state in STATIC_ACTION and o.ident:  # the static reader's group (parse_static_manifest)
        act, why = STATIC_ACTION[o.state]
        if name and KILL.search(name):
            return VISIT, "a kill or death zone: never entered"
        if o.loads_map:
            return VISIT, "loads another map: entered only as the level's exit"
        if act in ("attack", "interact", "pickup", "talk") and (not name or "_componentDataOverrides_" in name):
            return VISIT, "no run-time name to give a command"
        if act == "enter" and o.box is None and o.cls != "CScriptTriggerEntity":
            return VISIT, "no trigger box to enter"
        return act, why
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


def family_of(o: Obj) -> str:
    """What a sample counts: the static reader's kind (an archetype such as CoinGenerated or Chompy,
    or a component class) when it names one, else the name without its number, else the class."""
    k = o.kind or ""
    if k and k not in ("CPhysicalEntity", "CGameEntity", "CEntity", "CActionEntity"):
        return k
    return stem(o.name) or k or o.cls


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
    provoke: str = ""               # a kind the pass makes differ on H alone (dev_audit_diverge): the proof
    sample: int = SAMPLE            # the acted-on objects per family this plan took
    timed: bool = False             # the level ends on its own (no chain member a player acts on)


# Seconds the boot and the level load take, measured (the report prints the real ones); used for the
# estimate only.
BOOT_S = 95.0
LEVEL_LOAD_S = 45.0
VISIT_S = 4.5          # goto, goto_done, wait_free, one audit (2 s settle)
AUDIT_S = 2.5
ACTION_MOVE_S = 1.0    # an action's own teleport beside its target and the wait_free after it
LATE_JOIN_S = 60.0     # D's own visit after it is in (its boot runs beside H's sweep)
EXIT_S = 90.0


def estimate_seconds(stops: list[Stop], late_join: bool) -> float:
    t = BOOT_S + LEVEL_LOAD_S + EXIT_S
    for s in stops:
        t += VISIT_S
        for a in s.actions:
            t += ACTION_EXPECT[a.action] + ACTION_MOVE_S
        if s.actions:
            t += AUDIT_S
    if late_join:
        t += LATE_JOIN_S
    return t


def make_plan(content: Content, full: bool = False, late_join: bool = True, budget_s: float = BUDGET_S,
              sample: int = SAMPLE, provoke: str = "") -> Plan:
    """The plan; without --full, over the budget, the sample per family goes down to 2, then 1 (the
    plan says so), after the quiet visits were dropped."""
    plan = _make_plan(content, full, late_join, budget_s, sample, provoke)
    n = sample
    while not full and plan.estimate_s > budget_s and n > 1:
        n -= 1
        plan = _make_plan(content, full, late_join, budget_s, n, provoke)
        plan.left_out.append(f"each action family sampled {n} time(s), not {sample}, to fit the budget")
    plan.sample = n
    return plan


def _make_plan(content: Content, full: bool, late_join: bool, budget_s: float, sample: int,
               provoke: str) -> Plan:
    visited = [o for o in content.objects if o.group in GROUPS_VISITED]
    # A sub-zone (a swap zone) is its own map, loaded by its own entry: its objects are visited only when
    # the probe saw them in this level's load (an authored id).
    other_zone = [o for o in visited if o.zone and o.zone != content.level and not o.ref.startswith("auth:")]
    visited = [o for o in visited if o not in other_zone]
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
    # The level-end chain's first senders are the exit: acted on last, never during the sweep.
    # The exit: the first name of the level-end chain a player can act on (an end fight's spawners, an NPC,
    # a trigger); a chain with none (a scripted end, tutorial_a's ride) is a timed level: the pass waits
    # for the level's own end.
    exit_names: set[str] = set()
    for n in content.level_end:
        named = [o for o in content.objects if o.name == n and placed(o) and o.ref]
        if any(exit_kind(o) == FIGHT or classify(o)[0] in (ENTER, TALK, INTERACT_A) for o in named):
            exit_names = {n}
            # The other first senders of the same segment (BomberSpawner_01 and _02) go with it.
            exit_names |= {m for m in content.level_end[:content.level_end.index(n) + 1]
                           if any(o.name == m and exit_kind(o) == exit_kind(named[0]) for o in content.objects)}
            break
    exits = []
    for o in content.objects:
        if not (o.name and o.name in exit_names and placed(o)):
            continue
        if any(e.name == o.name and dist(e.pos, o.pos) <= CLUSTER_RADIUS for e in exits):
            continue  # one copy per place (a refname repeats across map units)
        exits.append(o)
    receiver = next((o for o in content.objects if content.level_end_receiver and o.name == content.level_end_receiver
                     and placed(o)), None)
    if receiver is not None and exits:
        near = [o for o in exits if dist(o.pos, receiver.pos) <= EXIT_NEAR]
        exits = near or exits
        if all(exit_kind(o) == FIGHT for o in exits):
            # The end fight: at the receiver (the arena's manager), once, then once more for a late wave.
            exits = [Obj(name=receiver.name, cls=receiver.cls, pos=receiver.pos, group=GROUP_ENEMIES,
                         ident=receiver.ident, state="encounter", kind=receiver.kind, along=receiver.along,
                         ref=receiver.ref)]
            exit_names = exit_names | {receiver.name}
    visited = [o for o in visited if not (o.name and o.name in exit_names)]
    # A stable order: along the level path when the content has it, else by distance from the start;
    # then name (the clusters' seeds come first in it).
    if visited and all(o.along is not None for o in visited):
        visited.sort(key=lambda o: (o.along, o.name, o.ident))
    else:
        visited.sort(key=lambda o: (dist(start, o.pos) if start else 0.0, o.name, o.ident))
    clusters = cluster(visited)
    if visited and all(o.along is not None for o in visited):
        ordered = list(clusters)  # the seeds come in path order; the clusters keep it
        tail = [c for c in ordered if any(classify(o)[0] == EXIT_A for o in c.objects)]
        ordered = [c for c in ordered if c not in tail] + tail
    else:
        first = logic_order(content.edges) if content.edges else None
        ordered = tour(clusters, start, first)
    if exits:
        exit_clusters = cluster(sorted(exits, key=lambda o: (o.along if o.along is not None else 0.0, o.name)),
                                radius=EXIT_RADIUS)
        for c in exit_clusters:
            c.index = len(ordered)
            ordered.append(c)
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
            if o.name and o.name in exit_names:
                act, why = EXIT_A, "the level-end chain's sender: acted on last"
            if act != VISIT and not o.ref:
                act, why = VISIT, "no live reference (no probe id, no name a lookup takes)"
            if act == VISIT:
                visit_only.append((o, why))
                no_action_key = f"{stem(o.name) or o.cls}: {why}"
                no_action[no_action_key] = no_action.get(no_action_key, 0) + 1
                continue
            if act == FIGHT or (act == EXIT_A and exit_kind(o) == FIGHT):
                if fought:
                    continue  # one fight per cluster covers its spawners and encounters
                fought = True
            family = (act, family_of(o))
            if not full and act != EXIT_A and taken.get(family, 0) >= sample:
                left[family] = left.get(family, 0) + 1
                visit_only.append((o, f"sampled out ({act}: {sample} of its family taken)"))
                continue
            taken[family] = taken.get(family, 0) + 1
            actions.append(Action(action=act, obj=o, why=why, expect=effects.get(o.name, [])))
        stop = Stop(label=f"v{n + 1}", cluster=c, actions=actions, visit_only=visit_only)
        if any(a.action == EXIT_A for a in actions):
            if exit_stop is None:
                exit_stop = stop
            else:  # every exit cluster's actions in the one exit stop
                exit_stop.actions += actions
                exit_stop.visit_only += visit_only
                continue
        stops.append(stop)
    if exit_stop is not None:
        stops.remove(exit_stop)
        stops.append(exit_stop)
    left_out = [f"{family[0]} {family[1]}: {k} more not acted on (sampled)" for family, k in sorted(left.items())]
    if other_zone:
        zones = sorted({o.zone for o in other_zone})
        left_out.append(f"{len(other_zone)} object(s) of sub-zones not loaded with the level ({', '.join(zones)})")
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
                unplaced=unplaced, estimate_s=round(est, 1), full=full, late_join=late_join, exit_stop=exit_stop,
                provoke=provoke, timed=bool(content.level_end) and exit_stop is None)


# --- the scripts -------------------------------------------------------------------------------------------
#
# No coordinate is written into a script: every step names its object (Obj.ref) and the game reads where
# it is when the step runs (route_actions.cpp: goto_entity, enter_trigger, fight_near, and the helpers
# attack_until, interact, pickup, all through one live selector). A ref is the authored probe's exact
# identity `auth:<zone>:<index>` (the probe's manifest, joined to the content by name and placed
# position), else the object's name, resolved against the live entity table (the nearest of that name).
# An object with neither is not addressed: it is visited only through another object of its cluster.

def _safe(name: str) -> str:
    # The input script splits at every ';' and starts a comment at '#'.
    return name.replace(";", "").replace("#", "")


FIGURE_H = "Roller Brawl.dump"
FIGURE_D = "Scorp.dump"
GENERATED_NAME = re.compile(r"_componentDataOverrides_|_entityData$|^CEntityData|^CScriptTriggerEntityData|^CGameEntityData")
JOIN_DISTANCE = 5.0     # a content object and a probe object this close (same name) are one object


def boot_lines(host: bool, figure: str) -> list[str]:
    """The shortest boot the specs use: title, Host (H), the two menu presses, the figure, the hub; no
    fixed wait anywhere (each step a condition)."""
    lines = ["waitfor online_title 300", "waitfor title_ready 240"]
    if host:
        lines += ["online_host 0", "waitfor online_connected 60"]
    lines += ["tap start", "waitfor menu_ready 60", "tap a", "waitfor menu_ready 60", "tap a",
              "waitfor front_end_left 120", f"place 0 {{figures}}/{figure}"]
    return lines


def name_ref(name: str) -> str:
    """A name a live lookup can take: not empty, not a data object's generated name."""
    if not name or GENERATED_NAME.search(name):
        return ""
    return _safe(name)


def attach_refs(objects: list[Obj], probe: list[Obj] | None) -> int:
    """Each object's live reference (Obj.ref): the probe's authored id when a probe object of the same
    name (or, unnamed, of the same class) lies within JOIN_DISTANCE of it, else its name when a lookup
    can take it. How many got an authored id."""
    by_name: dict[str, list[Obj]] = {}
    unnamed: list[Obj] = []
    for p in probe or []:
        if "#" not in p.ident:
            continue
        (by_name.setdefault(p.name.lower(), []) if p.name else unnamed).append(p)
    joined = 0
    for o in objects:
        cands = by_name.get(o.name.lower(), []) if o.name else [p for p in unnamed if p.cls == o.cls]
        best = min(cands, key=lambda p: dist(p.pos, o.pos), default=None)
        if best is not None and dist(best.pos, o.pos) <= JOIN_DISTANCE:
            zone, index = best.ident.rsplit("#", 1)
            o.ref = f"auth:{zone}:{index}"
            joined += 1
        else:
            o.ref = name_ref(o.name)
    return joined


FIGHT_GROUPS = ("enemy", "spawner", "encounter")


def exit_kind(o: Obj) -> str:
    """What the exit does at one of its senders: fight (an enemy, spawner or encounter), talk (an NPC),
    else enter (a trigger: a teleport into it)."""
    if o.state in FIGHT_GROUPS or o.group == GROUP_ENEMIES:
        return FIGHT
    if o.state in ("npc",) or (o.cls.lower() == "cactor" and o.group == GROUP_NPCS):
        return TALK
    return ENTER


def action_lines(a: Action, label: str) -> list[str]:
    o = a.obj
    ref = o.ref
    out = [f"log LEVELPASS act {label} {a.action} {_safe(o.name) or '-'} {o.cls} ref {ref}"]
    if a.action == FIGHT:
        out.append(f"fight_near {ref} {FIGHT_RADIUS} {ACTION_TIMEOUT[FIGHT]}")
    elif a.action == ATTACK:
        out.append(f"attack_until {ref} {ACTION_TIMEOUT[ATTACK]}")
    elif a.action in (INTERACT_A, TALK):
        out.append(f"interact {ref} {ACTION_TIMEOUT[a.action]}")
    elif a.action == PICKUP:
        out.append(f"pickup {ref} {ACTION_TIMEOUT[PICKUP]}")
    elif a.action == EXIT_A and exit_kind(o) == FIGHT:
        out += [f"fight_near {ref} {EXIT_FIGHT_RADIUS} {EXIT_FIGHT_S}",
                f"fight_near {ref} {EXIT_FIGHT_RADIUS} {EXIT_LATE_WAVE_S}"]
    elif a.action == EXIT_A and exit_kind(o) == TALK:
        out.append(f"interact {ref} {ACTION_TIMEOUT[TALK]}")
    elif a.action in (ENTER, EXIT_A):
        out.append(f"enter_trigger {ref}")
    out.append("wait_free 30")
    return out


FIGHT_RADIUS = 1500


def script_h(plan: Plan, late_join_after: str | None) -> str:
    """H's script: boot, Host, the level, the sweep, the late join's wait, the exit."""
    lines = boot_lines(True, FIGURE_H)
    lines += ["waitfor online_server_hub 300", "online_mark_level", f"level {plan.level}",
              "waitfor online_level_after_mark 300", "waitfor online_barrier 300", "wait_free 120",
              "log LEVELPASS phase sweep", "online_audit start"]
    n_stops = len(plan.stops)
    marked = False
    for i, s in enumerate(plan.stops):
        lead = n_stops if plan.timed else LATE_JOIN_LEAD
        if late_join_after is not None and plan.late_join and i == max(0, n_stops - lead):
            lines.append("log LEVELPASS late-join start")
            marked = True
        if s is plan.exit_stop:
            continue
        lines += stop_lines(s)
        if plan.provoke and i == 0:
            # The audit's own proof: one object of the kind made different on H alone, then audited.
            lines += [f"log LEVELPASS provoke {plan.provoke}", f"dev_audit_diverge {plan.provoke}",
                      f"online_audit {PROVOKE_LABEL}"]
    if late_join_after is not None and plan.late_join and not marked:
        lines.append("log LEVELPASS late-join start")
    lines.append("log LEVELPASS phase audit")
    lines.append("online_audit end")
    if plan.late_join and late_join_after is not None:
        # D ends its session once its audits are done: H waits for D in, then for D gone.
        lines += ["log LEVELPASS phase late-join", "online_wait_peers 2 300", "waitfor online_alone 300",
                  "online_audit late"]
    lines.append("log LEVELPASS phase exit")
    if plan.exit_stop is not None:
        lines += stop_lines(plan.exit_stop)
        lines += ["waitfor online_level_end 60", "waitfor online_past_level_end 240", "wait_free 120",
                  "log LEVELPASS phase next-level", "online_audit next"]
    elif plan.timed:
        # A timed level: its own end, then the next level (TIMED_END_S at most).
        lines += ["log LEVELPASS exit timed (the level ends on its own)", f"waitfor online_level_end {TIMED_END_S}",
                  "waitfor online_past_level_end 240", "wait_free 120", "log LEVELPASS phase next-level",
                  "online_audit next"]
    else:
        lines.append("log LEVELPASS exit none (no level-end candidate in the content)")
    lines += ["log LEVELPASS done", "online_end", "quit"]
    return "\n".join(lines) + "\n"


PROVOKE_LABEL = "provoke"
TIMED_END_S = 240      # a timed level's own end comes within this after the sweep
LATE_JOIN_LEAD = 6     # D starts this many stops before the sweep ends (its boot runs beside them)
D_VISITS = 6           # the late joiner's visits (each streams an area in; every audit covers all its rows)


def visit_ref(s: Stop) -> str:
    """The object a stop's teleport goes to: its first object with a live reference, "" for none."""
    for o in s.cluster.objects:
        if o.ref:
            return o.ref
    return ""


def stop_lines(s: Stop) -> list[str]:
    c = s.cluster
    names = ", ".join(sorted({_safe(o.name) for o in c.objects if o.name})[:6]) or "-"
    ref = visit_ref(s)
    out = [f"log LEVELPASS visit {s.label} objects {len(c.objects)} ({names}) ref {ref or '-'}"]
    if ref:
        out += [f"goto_entity {ref}", "wait_free 20"]
    out.append(f"online_audit {s.label}")
    for k, a in enumerate(s.actions):
        out += action_lines(a, f"{s.label}_{k + 1}")
    if s.actions:
        out.append(f"online_audit {s.label}_a")
    return out


def script_d(plan: Plan) -> str:
    """D's script (the late joiner): boot, its own hub, join the session ({join}), the visit and audit
    of up to D_VISITS stops H acted on (spread over the level), then it ends its session (H waits for
    that: online_alone)."""
    lines = boot_lines(False, FIGURE_D)
    lines += ["waitfor online_in_play 300", "online_join {join}", "waitfor online_barrier 300", "wait_free 120",
              "log LEVELPASS D in", "online_audit d-in"]
    acted = [s for s in plan.stops if s.actions and s is not plan.exit_stop and visit_ref(s)]
    if len(acted) > D_VISITS:
        step = len(acted) / D_VISITS
        acted = [acted[int(i * step)] for i in range(D_VISITS - 1)] + [acted[-1]]
    for s in acted:
        lines += [f"log LEVELPASS D visit {s.label}", f"goto_entity {visit_ref(s)}", "wait_free 20",
                  f"online_audit d-{s.label}"]
    lines += ["log LEVELPASS D done", "online_end", "quit"]
    return "\n".join(lines) + "\n"


POSITION_LITERAL = re.compile(r"(?<![\w:.\-])-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?|at:-?\d|\bgoto -?\d|fight_area\b|"
                              r"online_wait_peer_near\b|online_server_goto\b|walkto\b|navto\b")


def position_literals(script: str) -> list[str]:
    """The lines of a level-pass script that carry a position (three numbers in a row, an at: point, a
    goto, fight_area, walkto or navto with coordinates): a level pass names objects, never places."""
    return [l for l in script.splitlines() if POSITION_LITERAL.search(l)]


# --- the report ------------------------------------------------------------------------------------------

SEVERITY = ("crash", "level changed before the exit", "provoked difference (expected)", "difference after an action", "difference at rest", "object without a row",
            "row without an object", "action failed", "step failed", "not shared (documented)")

AUDIT_DIFF = re.compile(r"Online: audit diff (\S+) (\S+) (\S+) (\S+) slot (\d+) rev (\d+) (\S+) at (\S+) local (\S+) "
                        r"row (\S+) live (\S+)(?: why (.*))?$")
AUDIT_NOROW = re.compile(r"Online: audit norow (\S+) (\S+) (\S+) (\S+) at (\S+)$")
AUDIT_SUMMARY = re.compile(r"Online: audit summary (\S+) (\S+) rows (\d+) differences (\d+) .*$")
STEP_FAIL = re.compile(r"(Input script: waitfor (\S+) timed out.*|Dev: (?:\S+ )*(?:failed|stopped|gives up)\b.*|"
                       r"Dev: wait_free stopped.*|Online: audit \S+: not done in 20 s|Online: audit \S+ refused.*)")
MARK = re.compile(r"Input script: LEVELPASS (.*)$")
DIVERGE = re.compile(r"Online: audit diverge (\S+) (\S+) (\S+): this client alone")
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
    sightings: int = 0  # fold: how many audit lines the entry's objects had in all


def label_kind(label: str) -> str:
    """What an audit label stands for: a visit (v<n>), an action (v<n>_<k>), a late joiner's (d-...)."""
    if re.fullmatch(r"v\d+_(\d+|a)", label):
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


LEVEL_LOAD = re.compile(r"Online: level (\d+) (\S+): load")


def level_changes(text: str, timed: bool) -> list[Finding]:
    """A level load on H after the sweep began and before its exit phase: the level changed under the
    pass (the steps after it ran in the next level). In a timed level (it ends on its own) the same,
    named as its own end."""
    out: list[Finding] = []
    phase, step = "", ""
    for line in text.splitlines():
        m = MARK.search(line)
        if m:
            w = m.group(1)
            if w.startswith("phase "):
                phase = w[len("phase "):]
            elif w.startswith(("visit ", "act ")):
                step = w
            continue
        m = LEVEL_LOAD.search(line)
        if m and phase in ("sweep", "audit", "late-join"):
            what = ("the level ended on its own (a timed level)" if timed else
                    "the level ended before the pass's exit") + f"; loading {m.group(2)}"
            out.append(Finding(severity="level changed before the exit", kind="level", family=m.group(2), what=what,
                               obj="", step=f"during {phase}, after `{step}`", lines=[line.strip()], peer="H"))
    return out


def provoked(text: str) -> list[tuple[str, str]]:
    """(kind, entity text) of each dev_audit_diverge in a peer's log."""
    return [(m.group(1), m.group(2)) for m in (DIVERGE.search(l) for l in text.splitlines()) if m]


def mark_provoked(findings: list[Finding], diverged: list[tuple[str, str]]) -> int:
    """The findings on a diverged entity become `provoked difference (expected)`; how many."""
    n = 0
    for f in findings:
        for kind, entity in diverged:
            if f.kind == kind and f" {entity} " in f" {f.obj} " and f.severity != "crash":
                f.severity = "provoked difference (expected)"
                n += 1
    return n


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
    """Findings of one severity, kind, family and what are one entry; its count is the number of
    distinct objects (an object seen in several audits counts once; the entry keeps one finding per
    object, the first); most severe first, then by count, then by name (stable)."""
    groups: dict[tuple[str, str, str, str], list[Finding]] = {}
    seen: set[tuple] = set()
    sightings: dict[tuple[str, str, str, str], int] = {}
    for f in findings:
        key = (f.severity, f.kind, f.family, f.what)
        sightings[key] = sightings.get(key, 0) + 1
        obj = (key, f.peer, f.obj) if f.obj else (key, f.peer, f.lines[0] if f.lines else "")
        if obj in seen:
            continue
        seen.add(obj)
        groups.setdefault(key, []).append(f)
    for key, fs in groups.items():
        fs[0].sightings = sightings[key]
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


WAIT_MET = re.compile(r"Input script: waitfor (\S+) (met|timed out) after ([\d.]+) s")
WAIT_FREE = re.compile(r"Dev: wait_free free after ([\d.]+) s")


def boot_phases(text: str) -> list[tuple[str, float]]:
    """The boot's steps from H's log, up to the sweep's start: each waitfor's wait (seconds) in order,
    and the game's start to its first line as `process start`."""
    from datetime import datetime
    out: list[tuple[str, float]] = []
    first = None
    for line in text.splitlines():
        t = TIME.match(line)
        if t and first is None:
            first = datetime.strptime(t.group(1)[:23], "%Y-%m-%d %H:%M:%S.%f").timestamp()
        if "LEVELPASS phase sweep" in line:
            break
        m = WAIT_MET.search(line)
        if m:
            out.append((m.group(1) + ("" if m.group(2) == "met" else " (timed out)"), float(m.group(3))))
            continue
        m = WAIT_FREE.search(line)
        if m:
            out.append(("wait_free", float(m.group(1))))
    return out


def summaries(text: str) -> list[str]:
    return [m.group(0) for m in (AUDIT_SUMMARY.search(l) for l in text.splitlines()) if m]


def report_text(level: str, plan: Plan, wall_s: float, phases: list[tuple[str, float]], folded,
                crash: str, sums: dict[str, list[str]], run_folder: str, notes: list[str],
                boot: list[tuple[str, float]] | None = None) -> str:
    lines = [f"Level pass: {level}", f"run folder: {run_folder}", f"content: {plan.source}",
             f"wall clock: {wall_s:.0f} s ({'under' if wall_s <= BUDGET_S else 'OVER'} the {BUDGET_S:.0f} s budget); "
             f"estimate was {plan.estimate_s:.0f} s",
             f"pass: {'full' if plan.full else 'sampled (each action family at most ' + str(plan.sample) + ' times)'}, "
             f"{len(plan.stops)} stops, {sum(len(s.actions) for s in plan.stops)} actions, "
             f"late join {'on' if plan.late_join else 'off'}"]
    lines.append("")
    lines.append("time per phase:")
    for name, s in phases:
        lines.append(f"  {name}: {s:.1f} s")
    if boot:
        lines.append("boot steps (H, seconds each condition waited):")
        lines.append("  " + ", ".join(f"{n} {s:.1f}" for n, s in boot))
    total = sum(n for _f, n, _fs in folded)
    lines.append("")
    lines.append(f"findings: {total} in {len(folded)} entries" + (f"; CRASH: {crash}" if crash else ""))
    for f, n, fs in folded:
        seen = f" in {f.sightings} audit line(s)" if f.sightings > n else ""
        lines.append(f"- [{f.severity}] {f.kind} {f.family}: {f.what} (x{n}{seen})")
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
H_ARGS = ["--dev_authored_probe=true", "--dev_backend_commands=true", "--dev_hello_ungated_loopback=true", "--dev_damage_multiplier=-1",
          "--dev_hero_invulnerable=true", "--dev_defeat_commands=true", "--dev_trigger_commands=true",
          "--log_max_file_size_mb=64", "--log_max_files=4"]
D_ARGS = ["--dev_authored_probe=true", "--dev_backend_commands=true", "--dev_damage_multiplier=-1", "--dev_hero_invulnerable=true",
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
