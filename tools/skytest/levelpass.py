"""The automatic level pass (`skytest.py levelpass <level> [--full] [--no-late-join] [--no-end]`): one
session that boots once, a sweep generated from the level's own data, the level's end derived from its
logic (levelpass_end.py: a trigger to enter, a counted fight, or a timed level), an automatic verdict from
the game's audit. `--no-end` stops after the last mid-level step (its own case, levelpass-<level>-no-end).
An exit that cannot happen (its approach finds no entity, or an object it needs has no live reference)
is H's forbidden line: fail fast ends the run there, never a level-end wait that cannot be met.

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

import levelpass_end as le

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
    components: list[str] = field(default_factory=list)  # the static reader's component data classes
    live_state: str = ""         # the probe's state of the object its ref names (registered, asleep, skipped)

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
    level_end_chains: list[str] = field(default_factory=list)  # the static manifest's level_end text
    logic: dict | None = None     # logic.json itself (levelpass_end.derive_end walks it)


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
        o.components = [str(c) for c in e.get("components", []) or []]
        objects.append(o)
        if start is None and e.get("group") == "level_start" and (name == start_name or not start_name):
            start = pos
    content = Content(level=level, source="static manifest", start=start, objects=objects)
    content.level_end = exit_senders(data.get("level_end"))
    content.level_end_receiver = exit_receiver(data.get("level_end"))
    le_text = data.get("level_end")
    content.level_end_chains = [str(c) for c in (le_text if isinstance(le_text, list) else [le_text] if le_text else [])]
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


def load_content(folder, level: str, probe_ids: bool = True) -> Content:
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
        # --no-probe: the probe is off in the games, so only the names it saw live address objects.
        content.authored = attach_refs(content.objects, probe_content.objects, names_only=not probe_ids)
    else:
        content.authored = attach_refs(content.objects, None)
    logic = folder / level / "logic.json"
    if logic.is_file():
        try:
            content.logic = json.loads(logic.read_text(encoding="utf-8"))
        except ValueError:
            content.logic = None
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
LOCK_GAME = re.compile(r"lockpick|minigamegate|minigame_gate", re.I)
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
EXIT_FIGHT_S = 90
EXIT_LATE_WAVE_S = 30
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


# What a player acts on, read from the static reader's component data classes (manifest.json's
# "components"; class names as the reader writes them). The pass chose an action by the object's name or
# its reader group before: a chest spawner, a battle gate and a caged NPC were pressed and had nothing
# to press (the 20261118 passes); the components say what the object has.
COMP_INTERACT = "CInteractionComponentData"
COMP_CONVERSATION = "CConversationComponentData"
COMP_DESTRUCTIBLE = "Scripts.DestructibleComponentData"
COMP_COLLECTIBLE = "Scripts.CollectibleComponentData"
COMP_TRIGGER = re.compile(r"^CTriggerVolume\w*ComponentData$")
CLASS_FIGHT = ("CEncounterSpawnPoint",)
CLASS_PICKUP = ("CCollectible",)
CLASS_TRIGGER = ("CScriptTriggerEntity",)
# Reader groups whose objects are fought at the cluster whatever their components (an enemy's AI).
FIGHT_STATES = ("enemy", "encounter", "spawner")


def action_from_components(o: Obj) -> tuple[str, str] | None:
    """The action the static reader's components (and the class, for the component-less kinds) give,
    or None when they say nothing (the group's table then decides). An interaction first (what a
    player presses), then a destructible (attacked), a collectible (picked up), a spawner or encounter
    (fought), a trigger volume (entered); an object of an acted-on group with none of those has nothing
    a player acts on directly (a chest spawner's chest comes at run time, a battle gate opens on its
    fight, a caged NPC is freed by breaking its cage): visited only, and said so."""
    comps = set(o.components)
    if o.state == "minigame" or any(c.endswith("MinigameComponentData") for c in comps):
        # A mini-game (a winch, binoculars, a launch pad): pressing it enters a mode that locks the controls
        # and stopped every later audit in ww1's run 20261118-104911 (binoculars); no script plays one yet.
        return VISIT, "a mini-game (its component): no script plays it yet, pressing it locks the controls"
    if COMP_INTERACT in comps:
        if o.state == "npc" or (o.cls == "CActor" and COMP_CONVERSATION in comps):
            return TALK, "an NPC with an interaction: talked to"
        return INTERACT_A, "has an interaction (CInteractionComponentData)"
    if COMP_DESTRUCTIBLE in comps:
        return ATTACK, "a destructible (Scripts.DestructibleComponentData)"
    if o.state in FIGHT_STATES or o.cls in CLASS_FIGHT:
        return None  # the group's fight (an enemy, a spawner, an encounter)
    if COMP_COLLECTIBLE in comps or o.cls in CLASS_PICKUP:
        return PICKUP, "a collectible"
    if any(COMP_TRIGGER.match(c) for c in comps) or o.cls in CLASS_TRIGGER:
        return ENTER, "has a trigger volume: entered"
    if o.state in ("switch", "lock", "chest", "npc"):
        return VISIT, ("no interaction, destructible, spawner or trigger component: nothing a player acts on "
                       f"directly (reader group {o.state})")
    return None


def classify(o: Obj) -> tuple[str, str]:
    """The generic action for an object and why (the report's "no action yet" names the why)."""
    name = o.name
    if name and DEBUG.search(name):
        return VISIT, "a level designer's debug object: never pressed"
    if o.state in STATIC_ACTION and o.ident:  # the static reader's group (parse_static_manifest)
        act, why = STATIC_ACTION[o.state]
        if o.state == "lock" and (LOCK_GAME.search(name or "") or "Scripts.ForwardMessageComponentData" in o.components):
            return VISIT, "a Spark Lock mini-game gate: no script plays the mini-game yet (it locks the controls)"
        if name and KILL.search(name):
            return VISIT, "a kill or death zone: never entered"
        if o.loads_map:
            return VISIT, "loads another map: entered only as the level's exit"
        # The components choose among the actions of an acted-on group (and take one away); a group the
        # pass only visits (a teleporter, a mover, a mini-game, a bounce pad) is never made an action by
        # them: ww1's Cave03_TeleToInt (a teleporter with a trigger volume) was entered and dropped the
        # hero into an interior's endless fall (run 20261118-110326).
        by_comp = action_from_components(o) if act != VISIT else None
        if by_comp is not None:
            act, why = by_comp
        if act in ("attack", "interact", "pickup", "talk") and (not name or "_componentDataOverrides_" in name):
            return VISIT, "no run-time name to give a command"
        if act == "enter" and o.box is None and o.cls not in CLASS_TRIGGER and not any(
                COMP_TRIGGER.match(c) for c in o.components):
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
    probe: bool = True              # the authored probe on (--no-probe: names only, no auth: ids)
    game_args: list[str] = field(default_factory=list)  # --game-arg: extra settings for H and D (Host forwards S's)
    no_end: bool = False            # --no-end: the pass ends after its last mid-level step (no exit, no level end)
    end: object = None              # levelpass_end.End derived from logic.json (None: the older exit below)
    end_objs: dict = field(default_factory=dict)  # node key -> Obj of the end's objects (their refs)
    end_missing: str = ""           # why the derived end cannot be acted on (an object with no live reference)
    d_stays: bool = True            # D stays through the level end and must reach the next level itself
    # The owner's order (the level check's default, `levelpass`/`levelcheck` without --d-joins-late): both
    # clients join in the lobby, H picks the slot, the party enters the level together. False: D joins late,
    # into the level under test (the parked path; --d-joins-late).
    d_lobby: bool = False
    new_slot: bool = False          # --new-slot: H starts a new game in the lobby instead of the save's continue
                                    # (--d-leaves-before-end: D ends its session before the exit, as before)


# Seconds the boot and the level load take, measured (the report prints the real ones); used for the
# estimate only.
BOOT_S = 95.0
LEVEL_LOAD_S = 45.0
VISIT_S = 4.5          # goto, goto_done, wait_free, one audit (2 s settle)
AUDIT_S = 2.5
ACTION_MOVE_S = 1.0    # an action's own teleport beside its target and the wait_free after it
LATE_JOIN_S = 60.0     # D's own visit after it is in (its boot runs beside H's sweep)
EXIT_S = 90.0


def estimate_seconds(stops: list[Stop], late_join: bool, end: bool = True) -> float:
    t = BOOT_S + LEVEL_LOAD_S + (EXIT_S if end else 0.0)
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
              sample: int = SAMPLE, provoke: str = "", no_end: bool = False, d_stays: bool = True,
              d_lobby: bool = False, new_slot: bool = False) -> Plan:
    """The plan; without --full, over the budget, the sample per family goes down to 2, then 1 (the
    plan says so), after the quiet visits were dropped. `no_end` (--no-end): the level's end is found
    (the plan names it) but not attempted, and its time is not in the estimate."""
    # An end that needs a mechanic the pass cannot carry out (levelpass_end.UNSUPPORTED: a mini-game's result,
    # a turret, a boss, a ride, a cause the reader does not know): the sweep only, no end wait; the verdict
    # says INCOMPLETE with the end's family and node (pass_verdict).
    end0 = derive(content)
    if end0 is not None and end0.kind == le.UNSUPPORTED:
        no_end = True
    # drop_unreachable leaves steps out by clearing their objects' refs: each try starts from the refs as loaded.
    refs = [(o, o.ref) for o in content.objects]

    def fresh() -> None:
        for o, r in refs:
            o.ref = r
    plan = _make_plan(content, full, late_join, budget_s, sample, provoke, no_end=no_end, d_stays=d_stays)
    n = sample
    while not full and plan.estimate_s > budget_s and n > 1:
        n -= 1
        fresh()
        plan = _make_plan(content, full, late_join, budget_s, n, provoke, no_end=no_end, d_stays=d_stays)
        plan.left_out.append(f"each action family sampled {n} time(s), not {sample}, to fit the budget")
    # Still over at one per family (a level with many archetypes): at most `cap` actions of each kind
    # (fight, attack, interact, pickup, enter, talk), the first ones along the path, the cap going down
    # until the plan fits; --full acts on the rest.
    cap = KIND_CAP_START
    while not full and plan.estimate_s > budget_s and cap >= 1:
        fresh()
        plan = _make_plan(content, full, late_join, budget_s, n, provoke, kind_cap=cap, no_end=no_end,
                          d_stays=d_stays)
        plan.left_out.append(f"each action family sampled {n} time(s), not {sample}, to fit the budget")
        plan.left_out.append(f"each kind of action at most {cap} times, to fit the budget (--full acts on all)")
        cap -= 1
    plan.sample = n
    plan.d_lobby, plan.new_slot = d_lobby, new_slot
    return plan


KIND_CAP_START = 8   # the first per-kind cap tried when one action per family is still over the budget


def _make_plan(content: Content, full: bool, late_join: bool, budget_s: float, sample: int,
               provoke: str, kind_cap: int = 0, no_end: bool = False, d_stays: bool = True) -> Plan:
    visited = [o for o in content.objects if o.group in GROUPS_VISITED]
    # A sub-zone (a swap zone, a staging map) is its own map that a player reaches only through its own
    # entry: its objects are never visited, even when the probe walked its package at the level's load
    # (a teleport there put H where no player can be: run 3, a crash in the zone's scripts).
    other_zone = [o for o in visited if (o.zone and o.zone.lower() != content.level.lower()) or
                  (o.ref.startswith("auth:") and o.ref.split(":")[1].lower() != content.level.lower())]
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
    # The level's end from its own logic (levelpass_end.py): the chain walked back to a trigger to enter or a
    # counted fight. Its objects, the nodes on its path and its fight's arena are the end's alone: never acted
    # on during the sweep (entering the outro's trigger or fighting the counted enemies would end the level
    # under the sweep).
    end = derive(content)
    end_objs: dict[str, Obj] = {}
    end_missing = ""
    if end is not None:
        by_key = {obj_key(o): o for o in content.objects if o.name}
        end_keys = end.keys() | set(end.path_keys)
        end_objs = {k: by_key[k] for k in end_keys if k in by_key}
        end_missing = end_unaddressable(end, end_objs)
        arena = fight_area(end, end_objs)
        visited = [o for o in visited if obj_key(o) not in end_keys and not (
            arena is not None and placed(o) and dist(o.pos, arena[0]) <= arena[1])]
    # The level-end chain's first senders are the exit: acted on last, never during the sweep.
    # The exit: the first name of the level-end chain a player can act on (an end fight's spawners, an NPC,
    # a trigger); a chain with none (a scripted end, tutorial_a's ride) is a timed level: the pass waits
    # for the level's own end.
    exit_names: set[str] = set()
    for n in (content.level_end if end is None else []):
        named = [o for o in content.objects if o.name == n and placed(o) and o.ref]
        if any(exit_kind(o) == FIGHT or classify(o)[0] in (ENTER, TALK, INTERACT_A) for o in named):
            exit_names = {n}
            # The other first senders of the same segment (BomberSpawner_01 and _02) go with it.
            exit_names |= {m for m in content.level_end[:content.level_end.index(n) + 1]
                           if any(o.name == m and exit_kind(o) == exit_kind(named[0]) for o in content.objects)}
            break
    if not exit_names and content.level_end and end is None:
        # No chain member a player acts on: one step back, a sender to the chain's first member that a
        # player does act on, a trigger first (ww1: LevelOutro_Trigger -> CouncilEsmeralda, whose own
        # conversation has no interaction; without it the pass waited 240 s for an end that never came).
        first = content.level_end[0]
        senders = sorted({e.sender for e in content.edges if e.target.split(".")[-1] == first})
        for want in (ENTER, TALK, INTERACT_A):
            pick = [n for n in senders if any(o.name == n and placed(o) and classify(o)[0] == want
                                              for o in content.objects)]
            if pick:
                exit_names = {pick[0]}
                for o in content.objects:
                    if o.name == pick[0] and not o.ref:
                        o.ref = name_ref(o.name)  # the probe never saw it: its name, resolved live at the exit
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
            # The end fight, once, then once more for a late wave: anchored on the chain's sender nearest the
            # receiver (a spawner on the arena's floor; the manager itself may be placed in the air above it).
            anchor = min((o for o in exits if o.ref), key=lambda o: dist(o.pos, receiver.pos), default=None)
            if anchor is not None:
                exits = [anchor]
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
    per_kind: dict[str, int] = {}
    no_action: dict[str, int] = {}
    stops: list[Stop] = []
    exit_stop = None
    # The sweep steps left out by the preflight's object rule, named (a lost visit with its stop: it counts
    # only when the stop is still in the plan after the budget's trim).
    not_registered: list[tuple[Stop | None, str]] = []
    exit_set = set(id(c) for c in exit_clusters) if exits else set()
    for n, c in enumerate(ordered):
        actions: list[Action] = []
        visit_only: list[tuple[Obj, str]] = []
        fought = False
        lost_fight: str | None = None
        had_visit = any(o.ref for o in c.objects)
        for o in sorted(c.objects, key=lambda o: (ACTION_ORDER[classify(o)[0]], o.name, o.ident)):
            act, why = classify(o)
            if o.name and o.name in exit_names:
                act, why = EXIT_A, "the level-end chain's sender: acted on last"
            # Not registered at the load (the probe's state): no step may address it (a preflight error). The
            # exit's clusters keep theirs (a failing exit target is a real error: the pass must not start).
            unregistered = (id(c) not in exit_set and act != EXIT_A and o.ref.startswith("auth:")
                            and not reachable(o, []))
            if act != VISIT and not o.ref:
                act, why = VISIT, "no live reference (no probe id, no name a lookup takes)"
            if act == VISIT:
                if unregistered:  # the cluster's visit goes to another of its objects
                    o.ref, why = "", NOT_REGISTERED
                visit_only.append((o, why))
                no_action_key = f"{stem(o.name) or o.cls}: {why}"
                no_action[no_action_key] = no_action.get(no_action_key, 0) + 1
                continue
            if (act == FIGHT or (act == EXIT_A and exit_kind(o) == FIGHT)) and fought:
                if unregistered:
                    o.ref = ""
                continue  # one fight per cluster covers its spawners and encounters
            family = (act, family_of(o))
            if not full and act != EXIT_A and taken.get(family, 0) >= sample:
                left[family] = left.get(family, 0) + 1
                if unregistered:
                    o.ref = ""
                visit_only.append((o, f"sampled out ({act}: {sample} of its family taken)"))
                continue
            if kind_cap and act != EXIT_A and per_kind.get(act, 0) >= kind_cap:
                left[family] = left.get(family, 0) + 1
                if unregistered:
                    o.ref = ""
                visit_only.append((o, f"over the budget ({act}: at most {kind_cap} of each kind)"))
                continue
            if unregistered:
                # A step the script would have had: left out, and not its family's sample (the next of the
                # family, here or further on, takes it; the next fighter of the cluster takes its fight: the
                # cluster's one fight is named once, and only when no fighter of the cluster is left).
                o.ref = ""
                if act != FIGHT:
                    not_registered.append((None, f"{act} {step_name(o)}"))
                elif lost_fight is None:
                    lost_fight = f"{act} {step_name(o)}"
                visit_only.append((o, NOT_REGISTERED))
                no_action_key = f"{stem(o.name) or o.cls}: {NOT_REGISTERED}"
                no_action[no_action_key] = no_action.get(no_action_key, 0) + 1
                continue
            if act == FIGHT or (act == EXIT_A and exit_kind(o) == FIGHT):
                fought = True
            per_kind[act] = per_kind.get(act, 0) + 1
            taken[family] = taken.get(family, 0) + 1
            actions.append(Action(action=act, obj=o, why=why, expect=effects.get(o.name, [])))
        stop = Stop(label=f"v{n + 1}", cluster=c, actions=actions, visit_only=visit_only)
        if lost_fight is not None and not fought:
            not_registered.append((None, lost_fight))
        if had_visit and not any(o.ref for o in c.objects):
            not_registered.append((stop, f"visit {stop.label}"))
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
    not_registered += drop_unreachable(stops, exit_stop, no_action)
    left_out = [f"{family[0]} {family[1]}: {k} more not acted on (sampled)" for family, k in sorted(left.items())]
    if other_zone:
        zones = sorted({o.zone for o in other_zone})
        left_out.append(f"{len(other_zone)} object(s) of sub-zones not loaded with the level ({', '.join(zones)})")
    # Over the budget: drop visits of stops with no action, farthest from the start first; then say so.
    # The estimate keeps for the exit exactly what the sweep's deadline keeps (exit_reserve and the margin,
    # sweep_share): a plan the estimate fits is a sweep its deadline does not cut (before, the estimate kept
    # EXIT_S and the deadline the exit's own timeouts: every tutorial_b pass of 18 November had a step
    # skipped at the deadline).
    timed = (end.kind == le.TIMED) if end is not None else (bool(content.level_end) and exit_stop is None)
    est = estimate_seconds([s for s in stops if s is not exit_stop], late_join, end=False) + \
        exit_reserve(no_end, end, exit_stop, timed, d_end=late_join and d_stays) + SWEEP_MARGIN_S
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
    named = [text for s, text in not_registered if s is None or s in stops]
    if named:
        left_out.append(f"{len(named)} sweep step(s) {NOT_REGISTERED} (the probe's state; the preflight's rule): "
                        f"{', '.join(named)}")
    return Plan(level=content.level, source=content.source, stops=stops, left_out=left_out, no_action=no_action,
                unplaced=unplaced, estimate_s=round(est, 1), full=full, late_join=late_join, exit_stop=exit_stop,
                provoke=provoke, no_end=no_end, end=end, end_objs=end_objs, end_missing=end_missing, timed=timed,
                d_stays=d_stays)


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


# The party boot (the owner's lobby and save slot, lifted from the party-c / party-n flows of batch 5, not
# copied): H hosts as F4 > Online > Host does (`online_host -1`: S boots to its title and waits for the
# owner's own pick), D joins in the lobby by the session's join code and H lets it in (the server's Hello
# gate on), then H picks in the game's own menu: Story, then the save's continue (`a`) or a new game
# (`down`, `a`, `a`); S follows the owner's pick and both clients arrive in play behind one barrier. Every
# wait a condition; no overlay key presses (no debounce waits).
LOBBY_H_ARGS_DROP = ("--dev_hello_ungated_loopback=true",)
LOBBY_H_ARGS_ADD = ("--dev_hello_gate_loopback=true",)


def party_boot_h(lobby: bool, new_slot: bool) -> list[str]:
    lines = ["waitfor online_title 300", "waitfor title_ready 240", "online_host -1", "waitfor online_connected 150"]
    if lobby:
        lines += ["waitfor online_join_pending 300", "online_let_in", "online_wait_peers 2 120",
                  "log LEVELPASS lobby: both clients in, the host picks"]
    lines += ["waitfor menu_ready 60", "tap start", "waitfor menu_ready 60", "tap a", "waitfor menu_ready 60"]
    lines += (["tap down", "waitfor menu_ready 60", "tap a", "waitfor menu_ready 60", "tap a"] if new_slot
              else ["tap a"])
    lines += ["waitfor front_end_left 120", f"place 0 {{figures}}/{FIGURE_H}", "waitfor online_in_play 300",
              "waitfor online_barrier 300", "wait_free 120"]
    return lines


def party_boot_d() -> list[str]:
    """D in the lobby: its figure, the join by code, then it follows the host's pick into play."""
    return ["waitfor online_title 300", "waitfor title_ready 240", f"place 0 {{figures}}/{FIGURE_D}",
            "online_join {joincode}", "waitfor online_in_play 300", "waitfor online_barrier 300", "wait_free 120"]


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


def attach_refs(objects: list[Obj], probe: list[Obj] | None, names_only: bool = False) -> int:
    """Each object's live reference (Obj.ref): the probe's authored id when a probe object of the same
    name (or, unnamed, of the same class) lies within JOIN_DISTANCE of it, else its name when a lookup
    can take it. How many got an authored id."""
    by_name: dict[str, list[Obj]] = {}
    unnamed: list[Obj] = []
    live_names = {p.name.lower() for p in probe or [] if p.name}
    for p in probe or []:
        if "#" not in p.ident:
            continue
        (by_name.setdefault(p.name.lower(), []) if p.name else unnamed).append(p)
    joined = 0
    for o in objects:
        cands = by_name.get(o.name.lower(), []) if o.name else [p for p in unnamed if p.cls == o.cls]
        best = min(cands, key=lambda p: dist(p.pos, o.pos), default=None)
        if o.name and (best is None or dist(best.pos, o.pos) > JOIN_DISTANCE):
            # A named object the probe recorded without its name (its live name is another, or none: ww1's
            # LevelOutro_Trigger is the probe's unnamed CScriptTriggerEntity ww1#13618 at its very place):
            # the one unnamed probe object of its class at its place, when exactly one is there.
            here = [p for p in unnamed if p.cls == o.cls and dist(p.pos, o.pos) <= JOIN_DISTANCE]
            if len(here) == 1:
                best = here[0]
        if not names_only and best is not None and dist(best.pos, o.pos) <= JOIN_DISTANCE:
            zone, index = best.ident.rsplit("#", 1)
            o.ref = f"auth:{zone}:{index}"
            o.live_state = state_word(best.state)
            joined += 1
        elif probe is None or o.name.lower() in live_names:
            # A name the probe saw live (its refname may be another name at run time otherwise).
            o.ref = name_ref(o.name)
            states = {state_word(p.state) for p in by_name.get(o.name.lower(), [])}
            o.live_state = "registered" if "registered" in states else ("asleep" if "asleep" in states else
                                                                          (min(states) if states else ""))
        else:
            o.ref = ""
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


def action_lines(a: Action, label: str, after: str = "") -> list[str]:
    """An action's steps; `after`: the target the step before teleported to (its own teleport is not
    repeated: the visit's goto_entity to the same object already woke it, or was held and would be again)."""
    o = a.obj
    ref = o.ref
    out = [f"log LEVELPASS act {label} {a.action} {_safe(o.name) or '-'} {o.cls} ref {ref}"]
    if ref.startswith("auth:") and a.action in (ATTACK, INTERACT_A, TALK, PICKUP) and ref != after:
        # The helpers find a live object; goto_entity first wakes an asleep one (its area streamed in).
        out.append(f"goto_entity {ref}")
        out += dismiss_after(o)
    if a.action == FIGHT:
        out.append(f"fight_near {ref} {FIGHT_RADIUS} {ACTION_TIMEOUT[FIGHT]}")
    elif a.action == ATTACK:
        out.append(f"attack_until {ref} {ACTION_TIMEOUT[ATTACK]}")
    elif a.action in (INTERACT_A, TALK):
        out.append(f"interact {ref} {ACTION_TIMEOUT[a.action]}")
    elif a.action == PICKUP:
        out.append(f"pickup {ref} {ACTION_TIMEOUT[PICKUP]}")
        out += dismiss_after(o)
    elif a.action == EXIT_A and exit_kind(o) == FIGHT:
        out += [f"fight_near {ref} {EXIT_FIGHT_RADIUS} {EXIT_FIGHT_S}",
                f"fight_near {ref} {EXIT_FIGHT_RADIUS} {EXIT_LATE_WAVE_S}"]
    elif a.action == EXIT_A and exit_kind(o) == TALK:
        out.append(f"interact {ref} {ACTION_TIMEOUT[TALK]}")
    elif a.action in (ENTER, EXIT_A):
        out.append(f"enter_trigger {ref}")
    return out


FIGHT_RADIUS = 1500
# A major collectible (the game's CollectibleComponent: a Soul Gem, a story scroll, a map, a hat, a legendary
# treasure, a butterfly) is collected as soon as the hero lands on it or picks it up: every hero's controls
# go off and the game's found screen (CollectibleData.displayUI) pauses the game until a press closes it
# (its controls come back on CCollectibleTrackerUIClosedMessage). The screen takes two steps (the soul-gem
# cases of 18 November, int2 20261118-2235xx to 2241xx, and soulgem-found-screen 20261118-212923): A
# continues from the "found" screen to a Yes/No prompt (whether to play the preview movie; nothing plays by
# itself: after A alone the hero stayed locked 120 s), then B answers No and closes it (free 1.5 s after the
# B). B before any A does nothing (B, B, B: never free), and the d-pad is not needed (right or down, then A:
# not free). So every step that teleports to an object of the collectible component, or picks it up, is
# followed by A, a short wait (it times out on the found screen, a free hero passes it at once), B and a
# wait, and B once more in case the first came before the prompt was up. Never a second A (on the prompt A
# answers Yes), never back or start (back opened the session's pause, which nothing freed). On a free hero:
# a jump and two attacks. A screen this does not close: the next step's pause line is H's forbidden line
# (PAUSE_FATAL), the pass ends INCOMPLETE. The stored data does not say which collectibles are major, so the
# rule is the component's, never a name's.
DISMISS_LINES = ("tap a", "waitfor hero_free 3", "tap b", "waitfor hero_free 4", "tap b", "waitfor hero_free 4")


def is_collectible(o: Obj) -> bool:
    """An object of the collectible component (the static reader's components, or the probe's class)."""
    return COMP_COLLECTIBLE in o.components or o.cls in CLASS_PICKUP


def dismiss_after(o: Obj | None) -> list[str]:
    """B and the wait for the hero after a step that teleports to or picks up a collectible; [] else."""
    return list(DISMISS_LINES) if o is not None and is_collectible(o) else []
# A route step that finds the game paused with its player frames stopped (`no player frame took a read for
# 3 s (paused, ...)`): nothing after it can run until a press closes what paused it. H's forbidden line, so
# the pass ends at once instead of failing every later step after 23 s (ww1, 20261118-195819: 3.5 minutes).
PAUSE_FATAL = r"Dev: \S+ \S+ no player frame took a read for \d+ s \(paused"
# A copy made from S's run-time spawn rows (or its twin, or what it made) still in the entity table after its
# despawn: at the level change it would outlive the package of its data (online/5-endfight-spawns). H's
# forbidden line: the pass ends at once, with the crash's cause named, instead of the crash.
MADE_COPY_LEFT = r"Online: runtime spawn (?:made copy|twin) .* still in the entity table"


def script_h(plan: Plan, late_join_after: str | None) -> str:
    """H's script: boot, Host, the level, the sweep, the late join's wait, the exit."""
    # The one boot (party_boot_h): the lobby (with D) or the host alone (D joins late, or no D).
    lines = party_boot_h(plan.late_join and plan.d_lobby and late_join_after is None, plan.new_slot)
    lines += ["online_mark_level", f"level {plan.level}",
              "waitfor online_level_after_mark 300", "waitfor online_barrier 300", "wait_free 120",
              "online_mark_level",  # the level under test: its next level is `online_level_after_mark` (NEXT_LEVEL)
              "log LEVELPASS phase sweep", "online_audit start", f"hold_cap {HOLD_CAP_S}",
              f"sweep_deadline {sweep_share(plan):.0f}"]
    n_stops = len(plan.stops)
    marked = False
    for i, s in enumerate(plan.stops):
        # D boots beside the sweep (about two minutes) and joins in its last part: the mark at the start.
        if late_join_after is not None and plan.late_join and i == 0:
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
    lines.append("sweep_deadline 0")
    lines.append("log LEVELPASS phase audit")
    lines.append("online_audit end")
    lobby = plan.late_join and plan.d_lobby and late_join_after is None
    d_end = (late_join_after is not None or lobby) and d_through_end(plan)
    if plan.late_join and late_join_after is not None and d_end:
        # D stays (the default): H waits for D in the session now, so the level ends with two players in it.
        lines += ["log LEVELPASS phase late-join", "waitfor online_peer_was_in 300", "online_wait_peers 2 120",
                  "online_audit late"]
    elif plan.late_join and late_join_after is not None:
        # --d-leaves-before-end: D ends its session once its audits are done: H waits for D in, then gone.
        lines += ["log LEVELPASS phase late-join", "waitfor online_peer_was_in 300", "waitfor online_alone 300",
                  "online_audit late"]
    if plan.no_end:
        # --no-end: the middle of the level is the evidence; its end is neither acted on nor waited for.
        lines += [f"log LEVELPASS exit not attempted (--no-end), the end would be: {_safe(end_words(plan))}",
                  "log LEVELPASS done", "online_end", "quit"]
        return "\n".join(lines) + "\n"
    lines.append("log LEVELPASS phase exit")
    if plan.end is not None and plan.end.kind in (le.TRIGGER, le.FIGHT):
        lines += end_lines(plan)
        lines += [f"waitfor {NEXT_LEVEL} {end_wait_s(plan.end) + NEXT_LOAD_S}",
                  "wait_free 120", "log LEVELPASS phase next-level", "online_audit next"]
    elif plan.exit_stop is not None:
        lines += stop_lines(plan.exit_stop)
        lines += [f"waitfor {NEXT_LEVEL} {int(EXIT_WAIT_S) + NEXT_LOAD_S}", "wait_free 120",
                  "log LEVELPASS phase next-level", "online_audit next"]
    elif plan.timed:
        # A timed level: its own end, then the next level (TIMED_END_S at most).
        lines += ["log LEVELPASS exit timed (the level ends on its own)", f"waitfor {NEXT_LEVEL} {TIMED_END_S + NEXT_LOAD_S}",
                  "wait_free 120", "log LEVELPASS phase next-level",
                  "online_audit next"]
    else:
        lines.append("log LEVELPASS exit none (no level-end candidate in the content)")
    if d_end:
        # D audits the next level itself and then leaves: H ends the session only after that.
        lines += ["log LEVELPASS phase end-of-session", f"waitfor online_alone {D_ALONE_WAIT_S}"]
    lines += ["log LEVELPASS done", "online_end", "quit"]
    return "\n".join(lines) + "\n"


def end_words(plan: Plan) -> str:
    """The level end the plan found, in words (the report's and --no-end's line)."""
    if plan.end is not None:
        return derived_words(plan)
    if plan.exit_stop is not None:
        acts = ", ".join(f"{a.action} {exit_kind(a.obj)} {a.obj.name or '-'} {a.obj.ref}" for a in plan.exit_stop.actions)
        return f"the exit's actions: {acts}"
    if plan.timed:
        return "the level ends on its own (a timed level)"
    return "none found in the content"


# A step of the exit phase whose target does not resolve ends the pass at once (fail fast on H's
# `forbid`): nothing after it can happen, and the level end's wait would only run out (ww1, 18 November
# 2026: `goto_entity LevelOutro_Trigger failed (no entity: no such entity)`, then 60 s for nothing, 11
# times). The route helpers log `Dev: <command> <selector> failed (no entity: <why>)` with the selector
# as the script gave it (route_actions.cpp, RouteRun::Fail). A sweep step's unresolved target stays a
# tool finding and the sweep goes on: no later step waits on it.
# Only the approach commands, in the pattern itself and not only in the targets it names: a fight's
# anchor that is gone after the level ended (its outro, the next level's load) is no failure of the pass,
# and the approach to the same anchor already resolved it. tutorial_b 20261118-230020 won its end fight,
# the town loaded, and a later round's `fight_near auth:tutorial_b:12036 failed (no entity ...)` matched the
# pattern by its target (the approach's anchor) and failed the pass.
EXIT_STEP_COMMANDS = ("goto_entity", "enter_trigger", "interact")
EXIT_FATAL_WHAT = "the exit's target did not resolve: the pass ends here, no wait for the level end"


def exit_refs(script: str) -> list[str]:
    """The targets the exit phase's steps address (between `LEVELPASS phase exit` and `LEVELPASS done`)."""
    out: list[str] = []
    inside = False
    for line in script.splitlines():
        if line.startswith("log LEVELPASS phase exit"):
            inside = True
            continue
        if line.startswith("log LEVELPASS done") or line.startswith("log LEVELPASS phase next-level"):
            inside = False
        parts = line.split()
        if inside and len(parts) >= 2 and parts[0] in EXIT_STEP_COMMANDS and parts[1] not in out:
            out.append(parts[1])
    return out


def exit_forbid(script: str) -> list[str]:
    """H's `forbid` for the exit: one pattern, a step of the exit naming one of its targets that found no
    entity; [] when the exit addresses none (a timed level, --no-end)."""
    refs = exit_refs(script)
    out = [r"Input script: " + END_IMPOSSIBLE] if f"log {END_IMPOSSIBLE}" in script else []
    if not refs:
        return out
    return [r"Dev: (?:" + "|".join(EXIT_STEP_COMMANDS) + r") (?:" + "|".join(re.escape(r) for r in refs)
            + r") failed \(no entity"] + out


EXIT_UNRESOLVED = re.compile(r"Dev: (\S+) (\S+) failed \((no entity[^)]*)\)")


def unresolved_exit(h_text: str, script: str) -> str:
    """The first exit step whose target did not resolve, in words ('' for none)."""
    refs = set(exit_refs(script))
    at = h_text.find("Input script: LEVELPASS phase exit")
    if at < 0 or not refs:
        return ""
    for m in EXIT_UNRESOLVED.finditer(h_text[at:]):
        if m.group(1) in EXIT_STEP_COMMANDS and m.group(2) in refs:
            return f"`{m.group(1)} {m.group(2)}` found {m.group(3)}: {EXIT_FATAL_WHAT}"
    return ""


# --- the derived end (levelpass_end.py) ---------------------------------------------------------------

TRIGGER_END_S = 150      # after the outro's trigger: its conversations (skipped), its cutscenes, the results
FIGHT_APPROACH_S = 25    # goto_entity's own limit
FIRST_ROUND_S = 60       # the first round of a counted fight
ROUND_S = 25             # each later round (a wave's arrival; each ends after 10 s with no enemy near)
FIGHT_RADIUS_MIN, FIGHT_RADIUS_MAX, FIGHT_MARGIN = 1500, 3000, 500
END_IMPOSSIBLE = "LEVELPASS exit impossible"


def obj_key(o: Obj) -> str:
    """The logic's node key of a static manifest object: <unit>.<name>."""
    return f"{o.ident}.{o.name}"


def derive(content: Content):
    """The level's end from logic.json and the chain's text; None without them (the older exit then)."""
    if not content.logic or not content.level_end_chains:
        return None
    return le.derive_end(content.level_end_chains, content.logic, content.level)


def end_unaddressable(end, objs: dict) -> str:
    """Why the end's needed objects cannot be named live ('' when they can)."""
    need = [end.trigger] if end.kind == le.TRIGGER else [end.anchor] if end.kind == le.FIGHT else []
    missing = [k for k in need if not k or k not in objs or not objs[k].ref]
    if not missing:
        return ""
    return "no live reference (no probe id, no name the probe saw) for " + ", ".join(m or "(none found)" for m in missing)


def fight_area(end, objs: dict) -> tuple[tuple[float, float, float], float] | None:
    """A counted fight's centre (its anchor) and radius: the anchor to its farthest counted placed enemy
    plus a margin, within FIGHT_RADIUS_MIN..MAX; None for other ends."""
    if end is None or end.kind != le.FIGHT or end.anchor not in objs:
        return None
    a = objs[end.anchor].pos
    far = max((dist(a, objs[k].pos) for k in end.enemies if k in objs), default=0.0)
    return a, float(min(FIGHT_RADIUS_MAX, max(FIGHT_RADIUS_MIN, round(far + FIGHT_MARGIN, -2))))


def fight_rounds(end) -> int:
    """The first round, one per wave the counter starts, one for late arrivals."""
    return 1 + len(end.waves) + 1


def end_seconds(end) -> float:
    if end.kind == le.TRIGGER:
        return FIGHT_APPROACH_S + TRIGGER_END_S
    if end.kind == le.FIGHT:
        return (FIGHT_APPROACH_S * (1 + len(end.starters)) + FIRST_ROUND_S + ROUND_S * (fight_rounds(end) - 1)
                + EXIT_WAIT_S)
    return EXIT_WAIT_S


def end_wait_s(end) -> int:
    return TRIGGER_END_S if end.kind == le.TRIGGER else int(EXIT_WAIT_S)


def _name(objs: dict, k: str) -> str:
    return objs[k].name if k in objs else k.split(".")[-1]


def _ref(objs: dict, k: str) -> str:
    return objs[k].ref if k in objs else ""


def derived_words(plan: Plan) -> str:
    end, objs = plan.end, plan.end_objs
    path = " -> ".join(dict.fromkeys(end.path))
    if end.kind == le.TRIGGER:
        return f"enter the trigger {_name(objs, end.trigger)} ({_ref(objs, end.trigger) or 'no ref'}); its chain: {path}"
    if end.kind == le.FIGHT:
        area = fight_area(end, objs)
        return (f"the counted fight of {_name(objs, end.counter) if end.counter else 'one enemy'} ({end.count} deaths of "
                f"{', '.join(_name(objs, k) for k in end.counted)}), started by "
                f"{', '.join(_name(objs, k) + ' (' + (_ref(objs, k) or 'no ref') + ')' for k in end.starters) or 'nothing'},"
                f" fought around {_name(objs, end.anchor)} ({_ref(objs, end.anchor) or 'no ref'})"
                f"{' radius ' + str(int(area[1])) if area else ''}, waves at {end.waves or 'none'}; its chain: {path}")
    if end.kind == le.UNSUPPORTED:
        return f"not attempted: {end.why}"
    return f"the level ends on its own ({end.why})"


def end_lines(plan: Plan) -> list[str]:
    """The derived end's steps: by live reference only (no position, no name list)."""
    end, objs = plan.end, plan.end_objs
    out = [f"log LEVELPASS end {end.kind}: {_safe(derived_words(plan))}"]
    if plan.end_missing:
        # Nothing to act on: said and failed at once (H's forbid), never a wait for an end that cannot come.
        return out + [f"log {END_IMPOSSIBLE}: {_safe(plan.end_missing)}"]
    if end.kind == le.TRIGGER:
        return out + [f"enter_trigger {_ref(objs, end.trigger)}"]
    area = fight_area(end, objs)
    anchor = _ref(objs, end.anchor)
    out += [f"goto_entity {anchor}", "wait_free 60"]
    for k in end.starters:
        ref = _ref(objs, k)
        if not ref:
            out.append(f"log LEVELPASS end starter {_safe(_name(objs, k))}: no live reference, not entered")
        elif k in objs and area is not None and dist(objs[k].pos, area[0]) > 2 * area[1]:
            out.append(f"log LEVELPASS end starter {_safe(_name(objs, k))}: outside the fight's area, not entered")
        else:
            out.append(f"enter_trigger {ref}")
    radius = int(area[1]) if area else FIGHT_RADIUS_MIN
    out.append(f"fight_near {anchor} {radius} {FIRST_ROUND_S}")
    out += [f"fight_near {anchor} {radius} {ROUND_S}"] * (fight_rounds(end) - 1)
    return out


def exit_seconds(plan: Plan) -> float:
    """The exit phase's share: its actions' own timeouts and the level end's waits."""
    return exit_reserve(plan.no_end, plan.end, plan.exit_stop, plan.timed, d_end=plan.late_join and plan.d_stays)


D_END_S = 20.0   # H's wait for D's own next-level audit and D's leave after the level end (D stays)
D_ALONE_WAIT_S = 60  # at most this for D to leave after its own next level (its time beyond D_END_S is the grace's)
# D's level-end waits can be met only while H's game runs (the session ends with it): wait_needs, so a pass
# whose H ended without the level end stops at once instead of D waiting out D_LEVEL_END_WAIT_S.
D_WAIT_NEEDS = '{ online_level_after_mark = ["H"] }'
# The next level on this client: a level of a later generation than the one `online_mark_level` kept (the level
# under test) is up here, its barrier released. Not `online_level_end`/`online_past_level_end`: those hold only
# once the server's LevelEnd message came, and a server that held its level end for a client and then saw the
# level change first sends none (tutorial_a 20261118-215638: "level end held not sent (the level changed)",
# H then waited 240 s for a level end that had happened during the sweep). The condition reads the state, so
# a level change before the wait (a timed level, another player's end) is met at once.
NEXT_LEVEL = "online_level_after_mark"
NEXT_LOAD_S = 90     # the next level's load after the level end, within the wait for it


def ends_level(no_end: bool, end, exit_stop: Stop | None, timed: bool) -> bool:
    """The pass attempts the level's end (a derived trigger or fight, the older exit, or a timed level)."""
    if no_end:
        return False
    if end is not None:
        return end.kind in (le.TRIGGER, le.FIGHT, le.TIMED)
    return exit_stop is not None or timed


def d_through_end(plan: Plan) -> bool:
    """D joins, stays through the level end and must reach the next level itself (the default)."""
    return plan.late_join and plan.d_stays and ends_level(plan.no_end, plan.end, plan.exit_stop, plan.timed)


def exit_reserve(no_end: bool, end, exit_stop: Stop | None, timed: bool, d_end: bool = False) -> float:
    """exit_seconds before the Plan exists (the estimate keeps the same); `d_end`: D stays through the end
    (H then waits for D's own next level before it ends the session)."""
    if no_end:
        return 0.0
    if d_end and ends_level(no_end, end, exit_stop, timed):
        return D_END_S + exit_reserve(no_end, end, exit_stop, timed)
    if end is not None:
        return end_seconds(end) if end.kind != le.TIMED else EXIT_WAIT_S
    if exit_stop is None:
        return EXIT_WAIT_S if timed else 0.0
    t = EXIT_WAIT_S
    for a in exit_stop.actions:
        t += (EXIT_FIGHT_S + EXIT_LATE_WAVE_S) if exit_kind(a.obj) == FIGHT else ACTION_TIMEOUT[TALK]
    return t


def sweep_share(plan: Plan, budget_s: float = BUDGET_S) -> float:
    """The sweep's time (sweep_deadline): the budget less the boot, the late join, the exit and a margin;
    at least SWEEP_MIN_S."""
    rest = BOOT_S + LEVEL_LOAD_S + (LATE_JOIN_S if plan.late_join else 0.0) + exit_seconds(plan) + SWEEP_MARGIN_S
    return max(SWEEP_MIN_S, budget_s - rest)


SWEEP_MARGIN_S = 30.0
HOLD_CAP_S = 20         # hold_cap: one hold or fall ends a step after this (H and D, the whole pass)
SWEEP_MIN_S = 60.0
EXIT_WAIT_S = 60.0     # the level end's own waits after the exit's actions (online_level_end, the next level)
PROVOKE_LABEL = "provoke"
TIMED_END_S = 240      # a timed level's own end comes within this after the sweep
LATE_JOIN_LEAD = 6     # D starts this many stops before the sweep ends (its boot runs beside them)
D_VISITS = 6           # the late joiner's visits (each streams an area in; every audit covers all its rows)


# --- what the preflight lets a generated case address (preflight.py's object rules) ------------------------
#
# The pass's cases go through the runner's preflight gate. An `auth:` step on an object the probe recorded
# as not registered at the load (`skipped`) is a preflight error; so is a name step whose objects are none
# of them registered, unless an earlier step (goto_entity, enter_trigger, interact, attack_until, pickup)
# addressed something within STREAM_NEAR of it (its area streamed in). Such a sweep step is left out of
# the script and listed as NOT_REGISTERED (the report says so before the run, not after 20 s of trying);
# the exit's steps are never left out (a failing exit target is a real error: the pass must not start).
NOT_REGISTERED = "not visited: not registered at load"
STREAM_NEAR = 4000.0   # preflight.py's STREAM_RADIUS


def state_word(state: str) -> str:
    """A probe state's first word (`skipped act0` is skipped), the way preflight.py reads it."""
    return state.split()[0] if state.split() else ""


def reachable(o: Obj, seen: list[tuple[float, float, float]]) -> bool:
    """Whether a step on `o` passes the preflight's object rule after steps on the positions `seen`."""
    if not o.ref:
        return False
    if o.ref.startswith("auth:"):
        return o.live_state != "skipped"
    if o.live_state in ("", "registered"):
        return True
    return any(dist(o.pos, v) <= STREAM_NEAR for v in seen)


def step_name(o: Obj) -> str:
    """An object as the not-registered list names it: its name (or class) and its probe id."""
    ident = o.ref[5:].replace(":", "#", 1) if o.ref.startswith("auth:") else o.ident
    return f"{o.name or o.cls}" + (f" {ident}" if ident else "")


def drop_unreachable(stops: list[Stop], exit_stop: Stop | None,
                     no_action: dict[str, int]) -> list[tuple[Stop | None, str]]:
    """The sweep's steps in script order, by the preflight's object rule with the positions of the steps before
    them (a name whose objects are all asleep at the load passes only near an earlier waking step): a visit
    whose target fails goes to the next object of its cluster, an action whose target fails is left out;
    each such object visited only (NOT_REGISTERED, counted in `no_action`). The steps left out, named (a lost
    visit with its stop)."""
    seen: list[tuple[float, float, float]] = []
    out: list[tuple[Stop | None, str]] = []

    def leave_out(s: Stop, o: Obj) -> None:
        o.ref = ""
        s.visit_only.append((o, NOT_REGISTERED))
        key = f"{stem(o.name) or o.cls}: {NOT_REGISTERED}"
        no_action[key] = no_action.get(key, 0) + 1
    for s in stops:
        if s is exit_stop:
            continue
        had_visit = visit_obj(s) is not None
        while (v := visit_obj(s)) is not None and not reachable(v, seen):
            leave_out(s, v)
        if (v := visit_obj(s)) is not None:
            seen.append(v.pos)
        elif had_visit:
            out.append((s, f"visit {s.label}"))
        kept = []
        for a in s.actions:
            if a.obj.ref and not reachable(a.obj, seen):
                leave_out(s, a.obj)
            if not a.obj.ref:  # left out here, or its ref went with a visit target left out above
                out.append((None, f"{a.action} {step_name(a.obj)}"))
                continue
            kept.append(a)
            if a.action in (ATTACK, INTERACT_A, TALK, PICKUP, ENTER):
                seen.append(a.obj.pos)
        s.actions = kept
    return out


def visit_obj(s: Stop) -> Obj | None:
    """The object a stop's teleport goes to: its first object with a live reference, None for none."""
    for o in s.cluster.objects:
        if o.ref:
            return o
    return None


def visit_ref(s: Stop) -> str:
    o = visit_obj(s)
    return o.ref if o is not None else ""


def stop_lines(s: Stop) -> list[str]:
    c = s.cluster
    names = ", ".join(sorted({_safe(o.name) for o in c.objects if o.name})[:6]) or "-"
    ref = visit_ref(s)
    out = [f"log LEVELPASS visit {s.label} objects {len(c.objects)} ({names}) ref {ref or '-'}"]
    if ref:
        out.append(f"goto_entity {ref}")
        out += dismiss_after(visit_obj(s))
    out.append(f"online_audit {s.label}")
    for k, a in enumerate(s.actions):
        out += action_lines(a, f"{s.label}_{k + 1}", after=ref if k == 0 else "")
    if s.actions:
        out.append(f"online_audit {s.label}_a")
    return out


D_LEVEL_END_WAIT_S = 600   # D's wait for the level end: the whole case (its stop is at CASE_TIMEOUT_S)


def script_d(plan: Plan) -> str:
    """D's script (the late joiner): boot, its own hub, join the session ({join}), the visit and audit
    of up to D_VISITS stops H acted on (spread over the level), then it ends its session (H waits for
    that: online_alone)."""
    if plan.d_lobby:
        # In the lobby with H (party_boot_d): into the first level the pick loads (the save's hub, a new game's
        # level), then the level under test when H's `level` command loads it. No visits of its own: no
        # condition tells D that H's sweep is over, and the graders read D's lines for every key wherever it
        # stands; D is in the session from the lobby to the next level, as a second player is.
        lines = party_boot_d() + ["online_mark_level", "log LEVELPASS D in the first level",
                                  f"waitfor {NEXT_LEVEL} 600", "waitfor online_barrier 300", "wait_free 120",
                                  "online_mark_level", f"hold_cap {HOLD_CAP_S}", "log LEVELPASS D in",
                                  "online_audit d-in"]
        if d_through_end(plan):
            lines += ["log LEVELPASS D stays for the level end", f"waitfor {NEXT_LEVEL} {D_LEVEL_END_WAIT_S}",
                      "wait_free 120", "log LEVELPASS D next level", "online_audit d-next"]
        lines += ["log LEVELPASS D done", "online_end", "quit"]
        return "\n".join(lines) + "\n"
    lines = boot_lines(False, FIGURE_D)
    lines += ["waitfor online_in_play 300", "online_join {join}", "waitfor online_barrier 300", "wait_free 120",
              "online_mark_level", f"hold_cap {HOLD_CAP_S}",
              "log LEVELPASS D in", "online_audit d-in"]
    acted = [s for s in plan.stops if s.actions and s is not plan.exit_stop and visit_ref(s)
             and reachable(visit_obj(s), [])]
    if len(acted) > D_VISITS:
        step = len(acted) / D_VISITS
        acted = [acted[int(i * step)] for i in range(D_VISITS - 1)] + [acted[-1]]
    for s in acted:
        lines += [f"log LEVELPASS D visit {s.label}", f"goto_entity {visit_ref(s)}", *dismiss_after(visit_obj(s)),
                  f"online_audit d-{s.label}"]
    if d_through_end(plan):
        # D stays through the level end and must reach the next level itself: the same waits as H's, as
        # long as the case may run (the level ends when H's exit or the level's own timer ends it).
        lines += ["log LEVELPASS D visits done, stays for the level end",
                  f"waitfor {NEXT_LEVEL} {D_LEVEL_END_WAIT_S}",
                  "wait_free 120", "log LEVELPASS D next level", "online_audit d-next"]
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
            "row without an object", "S keeps rows of removed entities", "action failed", "step failed",
            "not shared (documented)", "tool: could not act", "asleep on the audited side")

AUDIT_DIFF = re.compile(r"Online: audit diff (\S+) (\S+) (\S+) (\S+) slot (\d+) rev (\d+) (\S+) at (\S+) local (\S+) "
                        r"row (\S+) live (\S+)(?: why (.*))?$")
AUDIT_NOROW = re.compile(r"Online: audit norow (\S+) (\S+) (\S+) (\S+) at (\S+)$")
AUDIT_SUMMARY = re.compile(r"Online: audit summary (\S+) (\S+) rows (\d+) differences (\d+) .*$")
# A tracked object with no row that is asleep on the audited side (its area streamed out; the game's
# `audit asleep` line): the census lets a binding go while its copy sleeps, so no row resolves to it by
# design. Not a finding: the report lists them under their own heading, counted (ASLEEP).
AUDIT_ASLEEP = re.compile(r"Online: audit asleep (\S+) (\S+) (\S+) (\S+) at (\S+)$")
ASLEEP = "asleep on the audited side"
# A step the TOOL could not carry out (not a finding about the game): the report lists them in their own
# section, "tool: could not act", by kind, so the findings are only about the game. First match wins;
# a failure none of these names stays a finding ("action failed" or "step failed").
TOOL = "tool: could not act"
TOOL_KINDS = (
    (re.compile(r"skipped: the sweep's time is up"), "not reached in the budget"),
    (re.compile(r"bad arguments|unknown command"), "the step's own words were refused (a generator bug)"),
    (re.compile(r"no (?:static )?floor under it or beside it"), "no static floor under the object or beside it (on a moving ship or platform, off the map, or its area's collision not in)"),
    (re.compile(r"still asleep .*wakes only at its own event"), "asleep: wakes only at its own event, not for a visit"),
    (re.compile(r"still asleep|asleep \(not in the entity table\)|asleep, and the teleport"), "asleep: did not wake for the visit"),
    (re.compile(r"no such entity|is gone\b|no authored object"), "not in the live game by that name or id"),
    (re.compile(r"held too long|the game held the hero"), "teleport: the game held the hero"),
    (re.compile(r"under the target's floor|off the point|teleport failed|bad landing"), "teleport: no good landing beside it"),
    (re.compile(r"the route did not get here|out of reach|approach: teleport failed|no closer than"), "could not get within reach"),
    (re.compile(r"no CInteractionComponent on"), "no interaction on or near the object"),
    (re.compile(r"no player frame|no hero"), "no player frame or hero to act with"),
    (re.compile(r"time: hp (-?\d+) -> \1\b|no damage for \d+ s"), "the hero's hits took no hit point (not attackable now: bolted, scripted)"),
)


def tool_kind(words: str) -> str:
    """The tool's failure kind of a failed step's words (TOOL_KINDS), "" when it may be the game's."""
    for rx, kind in TOOL_KINDS:
        if rx.search(words):
            return kind
    return ""


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


def name_hash(name: str) -> int:
    """The game's NameHash (FNV-1a, 32 bits, over the name's bytes): an entity id's middle word."""
    h = 2166136261
    for b in name.encode("utf-8", errors="replace"):
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def names_by_hash(objects: list[Obj]) -> dict[str, str]:
    """Hex name hash -> the content's name(s) with it, for the report's ids."""
    out: dict[str, set[str]] = {}
    for o in objects:
        if o.name:
            out.setdefault(f"{name_hash(o.name):08x}", set()).add(o.name)
    return {h: "/".join(sorted(n)[:3]) for h, n in out.items()}


ENTITY_ID = re.compile(r"\ba[0-9a-f]{8}\.([0-9a-f]{8})\.[0-9a-f]{8}@\d+")


def name_ids(findings: list[Finding], names: dict[str, str]) -> None:
    """Each finding's entity ids get the content's name for their name hash, where it has one."""
    for f in findings:
        for m in ENTITY_ID.finditer(f.obj + " " + f.family):
            n = names.get(m.group(1))
            if n and n not in f.obj:
                f.obj = f"{f.obj} ({n})"
                if f.family == m.group(0) or f.family.startswith("a"):
                    f.family = stem(n)


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
        m = AUDIT_ASLEEP.search(line)
        if m:
            who, label, kind, local, where = m.groups()
            out.append(Finding(severity=ASLEEP, kind=kind, family=stem(local.split("#")[0]),
                               what="tracked here, asleep: no row binds while it sleeps", obj=f"{local} at {where}",
                               step=f"{label} {labels.get(label, '')}".strip(), lines=[line.strip()],
                               peer=f"{peer}/{who}"))
            continue
        m = STEP_FAIL.search(line)
        if m:
            words = m.group(1)
            family = words.split(" ")[1] if " " in words else words
            if family == "route" and " teleport " in words:
                family = words.split(" ")[3] if len(words.split(" ")) > 3 else family  # the helper's own name
            tk = tool_kind(words)
            if tk:
                out.append(Finding(severity=TOOL, kind="tool", family=family, what=tk, obj="", step="",
                                   lines=[line.strip()], peer=peer))
                continue
            sev = "action failed" if re.search(r"attack_until|interact|pickup|fight_area|encounter", words) else "step failed"
            what = re.sub(r"\d+(\.\d+)?", "N", words)[:140]
            out.append(Finding(severity=sev, kind="step", family=family,
                               what=what, obj="", step="", lines=[line.strip()], peer=peer))
    return out


# --- the audit's artefacts (findings-verified.txt, 20261118: F1, F2, F3, F5, F7) -------------------------
#
# Three kinds of audit line that are not a difference between the games, taken out of the findings:
# (a) a lease-carried kind (a container's mini-game, a turret, a conversation) has a row only once a player
#     started it: "object without a row" for one whose start no log shows is the audit's, not the game's;
# (b) a row whose entity went on S and on the client alike (the level's own script removed it everywhere;
#     S's own audit counts it pending) is a stale row: one entry per level, "S does not retire rows of
#     removed entities (N rows)", not one per object and audit;
# (c) a breakable whose client copy went while S's row was still intact is in flight when a later audit of
#     the same peer no longer reports it (S caught up): dropped, counted in a note.
LEASE_KINDS = ("container", "turret", "conversation")
LEASE_START = re.compile(r"Online: start ran start\.\S+ ([^\s#]+)#\d+ \(lease")
STALE_KINDS = ("prop", "mover")
SUMMARY_KINDS = re.compile(r"Online: audit summary (\S+) (\S+) rows .* kinds (.*)$")
STALE_WHAT = "S does not retire rows of removed entities"


def server_pending(text: str) -> dict[str, int]:
    """Per kind, the most pending rows any of S's audit summaries counted (S's census no longer has their
    entity): the most, not the last, as a timed level can change under the pass (tutorial_a's S counted 15
    pending props before the next level reset its rows)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = SUMMARY_KINDS.search(line)
        if not m or m.group(1) != "S":
            continue
        for part in m.group(3).split():
            k, _, v = part.partition("=")
            nums = v.split("/")
            if len(nums) == 4 and nums[3].isdigit():
                out[k] = max(out.get(k, 0), int(nums[3]))
    return out


def audit_artefacts(findings: list[Finding], texts: dict[str, str]) -> tuple[list[Finding], list[str]]:
    """The findings without the audit's artefacts (a, b, c above), and the notes that count them."""
    leased = {m.group(1) for t in texts.values() for m in LEASE_START.finditer(t)}
    pending = server_pending(texts.get("S", ""))
    out: list[Finding] = []
    stale: list[Finding] = []
    lease_left = 0
    settled: set[str] = set()
    for f in findings:
        if f.severity == "object without a row" and f.kind in LEASE_KINDS and f.obj.split("#")[0] not in leased:
            lease_left += 1
            continue
        if (f.severity == "row without an object" and f.kind in STALE_KINDS and f.what.startswith("no_object gone")
                and pending.get(f.kind, 0) > 0):
            stale.append(f)
            continue
        if f.severity == "row without an object" and f.kind == "breakable" and _settled_later(f, texts):
            settled.add(f.obj.split(" ")[1] if " " in f.obj else f.obj)
            continue
        out.append(f)
    notes: list[str] = []
    if stale:
        rows = sorted({f.obj.split(" ")[1] if " " in f.obj else f.obj for f in stale})
        first = stale[0]
        out.append(Finding(severity="S keeps rows of removed entities", kind="audit", family="S",
                           what=f"{STALE_WHAT} ({len(rows)} rows of {', '.join(sorted({f.kind for f in stale}))})",
                           obj=", ".join(rows[:6]) + (" ..." if len(rows) > 6 else ""), step=first.step,
                           lines=first.lines[:1], peer=first.peer))
    if lease_left:
        notes.append(f"{lease_left} audit line(s) of lease-carried kinds ({', '.join(LEASE_KINDS)}) without a row and "
                     "no start in any log left out (they have a row only once a player starts them)")
    if settled:
        notes.append(f"{len(settled)} breakable row(s) gone on a client before S caught up, settled at a later audit "
                     "(in flight, not findings)")
    return out, notes


EXIT_NOT_ACTED = "the exit's action could not be carried out, so the level was not ended by a player"


def exit_not_reached(findings: list[Finding], h_text: str) -> list[Finding]:
    """The level end's wait timing out after the exit's own action failed for the tool (no floor in the
    end arena, out of reach) is the tool's too: that timeout moves to "tool: could not act"."""
    at = h_text.find("Input script: LEVELPASS phase exit")
    if at < 0:
        return findings
    after = h_text[at:]
    tool_failed = any(tool_kind(m.group(1)) for m in STEP_FAIL.finditer(after)
                      if not m.group(1).startswith("Input script: waitfor"))
    if not tool_failed:
        return findings
    out = []
    for f in findings:
        if f.severity == "step failed" and f.lines and re.search(r"waitfor online_(level_end|level_after_mark) timed out",
                                                                f.lines[0]) \
                and f.lines[0] in after:
            f = Finding(severity=TOOL, kind="tool", family="exit", what=EXIT_NOT_ACTED, obj="", step="",
                        lines=f.lines, peer=f.peer)
        out.append(f)
    return out


def _settled_later(f: Finding, texts: dict[str, str]) -> bool:
    """The row's last audit line in the same peer's log is followed by a later audit summary of the same
    side (which no longer reports it): it settled (S caught up)."""
    peer, _, who = f.peer.partition("/")
    text = texts.get(peer, "")
    row = f.obj.split(" ")[1] if " " in f.obj else ""
    if not who or not row:
        return False
    last = None
    for last in re.finditer(rf"Online: audit diff {re.escape(who)} \S+ \S+ {re.escape(row)} ", text):
        pass
    if last is None:
        return False
    return re.search(rf"Online: audit summary {re.escape(who)} ", text[last.end():]) is not None


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
        # Values out of the key: one object's facing that differs by other amounts at each audit is one
        # entry (ww1's EZ2_Skully gave ten); the entry's first finding keeps its own values.
        key = (f.severity, f.kind, f.family, re.sub(r"-?\d+(?:\.\d+)?", "N", f.what))
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


def _entries(lines: list[str], folded) -> None:
    """One folded entry each: its head line and up to three examples (peer, step, object, values, line)."""
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


def report_text(level: str, plan: Plan, wall_s: float, phases: list[tuple[str, float]], folded,
                crash: str, sums: dict[str, list[str]], run_folder: str, notes: list[str],
                boot: list[tuple[str, float]] | None = None) -> str:
    lines = [f"Level pass: {level}", f"run folder: {run_folder}", f"content: {plan.source}",
             f"wall clock: {wall_s:.0f} s ({'under' if wall_s <= BUDGET_S else 'OVER'} the {BUDGET_S:.0f} s budget); "
             f"estimate was {plan.estimate_s:.0f} s",
             f"pass: {'full' if plan.full else 'sampled (each action family at most ' + str(plan.sample) + ' times)'}, "
             f"{len(plan.stops)} stops, {sum(len(s.actions) for s in plan.stops)} actions, "
             f"late join {'on' if plan.late_join else 'off'}",
             (f"level end: NOT ATTEMPTED ({end_words(plan)}): judged on the mid-level steps only"
              if plan.end is not None and plan.end.kind == le.UNSUPPORTED else
              f"level end: NOT ATTEMPTED (--no-end): judged on the mid-level steps only; the end would be "
              f"{end_words(plan)}") if plan.no_end else f"level end: attempted ({end_words(plan)})"]
    lines.append("")
    lines.append("time per phase:")
    for name, s in phases:
        lines.append(f"  {name}: {s:.1f} s")
    if boot:
        lines.append("boot steps (H, seconds each condition waited):")
        lines.append("  " + ", ".join(f"{n} {s:.1f}" for n, s in boot))
    game = [e for e in folded if e[0].severity not in (TOOL, ASLEEP)]
    tool = [e for e in folded if e[0].severity == TOOL]
    asleep = [e for e in folded if e[0].severity == ASLEEP]
    total = sum(n for _f, n, _fs in game)
    lines.append("")
    lines.append(f"findings: {total} in {len(game)} entries" + (f"; CRASH: {crash}" if crash else ""))
    _entries(lines, game)
    lines.append("")
    lines.append(f"tool: could not act: {sum(n for _f, n, _fs in tool)} in {len(tool)} entries "
                 "(steps the pass could not carry out; not findings about the game)")
    _entries(lines, tool)
    lines.append("")
    lines.append(f"asleep on the audited side: {sum(n for _f, n, _fs in asleep)} in {len(asleep)} entries "
                 "(tracked objects with no row while their copy sleeps; not findings)")
    _entries(lines, asleep)
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
# The case's stop (R3): the owner's 10 minutes and a minute's grace, no longer twice the budget (a sweep that
# ran 20 minutes, 18 November). A pass the runner stops there, or one with steps skipped at the sweep's
# deadline, is INCOMPLETE (pass_verdict), never a pass.
SOFT_GRACE_S = 60
CASE_TIMEOUT_S = int(BUDGET_S + SOFT_GRACE_S)


PASS, INCOMPLETE, FAIL = "PASS", "INCOMPLETE", "FAIL"
ENDED_FOR_TIME = re.compile(r"did not quit within \d+ s")
SKIPPED = re.compile(r"Dev: (\S+) (\S+).* skipped: the sweep's time is up")


PAUSE_LINE = re.compile(r"no player frame took a read for \d+ s \(paused")
PAUSED_HELD = re.compile(r"\((?:[^)]*, )?paused[,)]")


def paused_after(h_text: str) -> str:
    """The step after which the game paused: the last LEVELPASS act or visit before H's first line that
    found it paused ('' for none)."""
    step = ""
    for line in h_text.splitlines():
        m = MARK.search(line)
        if m and m.group(1).startswith(("act ", "visit ")):
            step = m.group(1)
            continue
        if "Dev: " in line and PAUSED_HELD.search(line):
            return step
    return ""


RELAYED_TO = r"relay (?:activator (\S+?)@(0x[0-9A-Fa-f]+)|native (\S+)) \+16 .*?\(relay {counter}#\d+"
NOT_SHARED = re.compile(r"Online: enemy run-time spawns of (\S+) not shared \(([^)]*)\)|"
                        r"Online: runtime spawn (\S+?)_Spawned\d+ not claimed \(([^)]*)\)")
BIND_MISSING = re.compile(r"Online: enemy bind (\S+) \(pending past grace: missing\)")


def counted_fight_notes(plan: Plan, s_text: str, h_text: str) -> list[str]:
    """For a counted end fight: the deaths S's counter heard (each sender once: S's relay lines to the counter,
    a death by a client's puppet or S's own), and the enemies of the fight a client could not fight (S's
    run-time spawns it did not share, and the shared ones the client never bound); [] for other ends."""
    end = plan.end
    if end is None or end.kind != le.FIGHT or not end.counter:
        return []
    counter = end.counter.split(".")[-1]
    rx = re.compile(RELAYED_TO.format(counter=re.escape(counter)))
    heard: list[str] = []
    for m in rx.finditer(s_text):
        who = f"{m.group(1)}@{m.group(2)}" if m.group(1) else m.group(3)
        if who not in heard:
            heard.append(who)
    out = [f"end fight: S's {counter} heard {len(heard)} of its {end.count} counted deaths (S's relay lines: "
           + (", ".join(h.split("@")[0] for h in heard) or "none") + ")"]
    units = {k.split(".")[-1] for k in end.counted}
    unshared = sorted({m.group(1) or m.group(3) for m in NOT_SHARED.finditer(s_text)
                       if (m.group(1) or m.group(3)) in units})
    if unshared:
        out.append("end fight: S did not share the run-time spawns of " + ", ".join(unshared)
                   + " with the clients (`enemy run-time spawns of <spawner> not shared` or `runtime spawn ... not"
                   " claimed`): no client can fight them")
    at = h_text.find("Input script: LEVELPASS phase exit")
    missing = sorted({m.group(1) for m in BIND_MISSING.finditer(h_text[at:] if at >= 0 else "")})
    if missing:
        out.append(f"end fight: H never bound {len(missing)} enemy spawn(s) S shared during the exit (`enemy bind <id>"
                   " (pending past grace: missing)`): " + ", ".join(missing[:6]))
    return out


H_NEXT = "Input script: LEVELPASS phase next-level"
D_IN = "Input script: LEVELPASS D in"
D_NEXT = "Input script: LEVELPASS D next level"
LEVEL_LOAD_LINE = re.compile(r"Online: level (\d+) (\S+): load")
LEVEL_END_PLAYED = re.compile(r"Online: level end \S+ from the server played here")
LEVEL_END_DROPPED = re.compile(r"Online: level end held not sent \(([^)]*)\)")
LEVEL_END_HELD = re.compile(r"Online: level end (\S+) to (\S+) held \(([^)]*)\): ([^(]*)")


def client_progress(text: str, peer: str) -> str:
    """How far one client got, in words, from its log's marks."""
    if not text:
        return "no log"
    if peer == "D" and D_IN not in text:
        return "never joined"
    if (H_NEXT if peer == "H" else D_NEXT) in text:
        return "reached the next level"
    loads = [m.group(2) for m in LEVEL_LOAD_LINE.finditer(text)]
    if peer == "H" and len(loads) >= 3 or peer == "D" and loads:
        return f"saw the level change ({loads[-1]}), not the next level's mark"
    return "did not see the level change"


def level_end_notes(s_text: str, texts: dict[str, str]) -> list[str]:
    """Whether the server's level end reached each client (its LevelEnd played there), and S's own hold or
    drop of it: a level that changed without its level end is a finding about the session, not the pass."""
    out = []
    for m in LEVEL_END_HELD.finditer(s_text):
        out.append(f"S held its level end {m.group(1)} ({m.group(3)}): {m.group(4).strip()}")
        break
    for m in LEVEL_END_DROPPED.finditer(s_text):
        out.append(f"S did not send its level end to any client: held, then not sent ({m.group(1)})")
    for peer, text in texts.items():
        if peer in ("H", "D") and text:
            out.append(f"{peer}: the server's level end " + ("played here" if LEVEL_END_PLAYED.search(text) else
                                                               "never came (no `level end ... from the server played here`)"))
    return out


def clients_line(plan: Plan, h_text: str, d_text: str) -> str:
    """Each client's progress, for the report's head."""
    out = [f"H {client_progress(h_text, 'H')}"]
    if plan.late_join:
        how = "stays through the end" if d_through_end(plan) else "leaves before the end (--d-leaves-before-end)"
        out.append(f"D ({how}) {client_progress(d_text, 'D')}")
    return "clients: " + "; ".join(out)


def pass_verdict(status: str, reasons: list[str], h_text: str, plan: Plan, d_text: str = "") -> tuple[str, list[str]]:
    """The pass's verdict and what it did not cover: FAIL for a failed run that was not only stopped for
    time; INCOMPLETE when the runner stopped it at the soft budget, steps were skipped at the sweep's
    deadline, or the exit it planned was not reached by every client that was to be in the session at the
    exit (H, and D unless --d-leaves-before-end); PASS otherwise: every such client reached the next level."""
    missed: list[str] = []
    if plan.end is not None and plan.end.kind == le.UNSUPPORTED:
        missed.append(f"the level's end, not attempted: {plan.end.why}")
    skipped = [f"{m.group(1)} {m.group(2)}" for m in SKIPPED.finditer(h_text)]
    if skipped:
        missed.append(f"{len(skipped)} step(s) skipped at the sweep's deadline: " + ", ".join(skipped[:8])
                      + (" ..." if len(skipped) > 8 else ""))
    timed_out = any(ENDED_FOR_TIME.search(r) for r in reasons)
    if timed_out:
        missed.append(f"stopped by the runner at the soft budget ({CASE_TIMEOUT_S} s)")
    planned_end = not plan.no_end and (plan.end is not None and plan.end.kind != le.TIMED or plan.exit_stop is not None
                                       or plan.timed)
    if planned_end and H_NEXT not in h_text:
        missed.append("the level's end and the next level (not reached)")
    if d_through_end(plan) and D_NEXT not in d_text:
        missed.append(f"D did not reach the next level ({client_progress(d_text, 'D')}): the pass shows one client only")
    paused = paused_after(h_text) if any(PAUSE_LINE.search(r) for r in reasons) else ""
    if paused:
        missed.insert(0, f"the game stayed paused with its frames stopped after `{paused}` (a screen the pass's press"
                         " did not close); the pass ended at the next step")
    if status not in ("pass",) and not paused and not (timed_out and all(
            ENDED_FOR_TIME.search(r) or r.startswith("peers failed") for r in reasons)):
        return FAIL, missed
    return (INCOMPLETE if missed else PASS), missed


def probe_args(args: list[str], probe: bool, extra: list[str] | None = None) -> list[str]:
    """A peer's arguments, without the authored probe for a --no-probe pass, plus the pass's --game-arg
    settings (after the defaults, so a later value wins)."""
    return (list(args) if probe else [a for a in args if not a.startswith("--dev_authored_probe")]) + list(extra or [])


def check_game_arg(arg: str) -> str:
    """A --game-arg value: `--name=value` with a value (an empty value makes the game drop its whole command
    line, its user data root included)."""
    name, eq, value = arg.partition("=")
    if not name.startswith("--") or len(name) < 3 or not eq or not value.strip() or "'" in arg:
        raise ValueError(f"--game-arg {arg!r}: give --name=value with a non-empty value")
    # A later value wins over the case's own: these would move the pass off the test copy of the owner's
    # data or onto the real portal (docs/RULES.md), so they are never taken from --game-arg.
    if name[2:].strip().lower() in GAME_ARG_REFUSED:
        raise ValueError(f"--game-arg {arg!r}: {name} is the runner's own (the test copy and the software "
                         "portal): not settable here")
    return arg


GAME_ARG_REFUSED = ("user_data_root", "portal_mode")


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join("'" + i.replace("'", "") + "'" for i in items) + "]"


def case_name(level: str, no_end: bool = False) -> str:
    """The generated case's name: a --no-end pass is its own case (its runs and results apart)."""
    return f"levelpass-{level}" + ("-no-end" if no_end else "")


def spec_from_scripts(level: str, h: str, d: str | None, d_args: list[str] | None = None,
                      name: str | None = None) -> str:
    """A case from ready scripts (the predictions mode): H hosts, S hidden, D (when given) starts at H's
    LEVELPASS late-join start mark."""
    out = [f'name = "{name or case_name(level)}"', f'layout = "{"server+2" if d else "server+1"}"', 'stage = "8"',
           'server = "host"', f'description = "The level pass of {level} (skytest levelpass; generated, not committed)"',
           f"timeout = {CASE_TIMEOUT_S}", 'scenes = "skip"', "show_server_window = false", 'shader_cache = "snapshot"',
           "", "[peer.H]", f"args = {_toml_list(H_ARGS)}", "script = '''\n" + h + "'''"]
    if d:
        out += ["", "[peer.D]", f"args = {_toml_list(D_ARGS + (d_args or []))}", f"start_after = '{LATE_JOIN_MARK}'",
                "script = '''\n" + d + "'''"]
    return "\n".join(out) + "\n"


def spec_toml(plan: Plan) -> str:
    """The generated case (a skytest spec): H hosts with a hidden server (the release's draw skip), D
    starts once H's log says LEVELPASS late-join start and joins by the loopback address. The shader
    cache comes from the build snapshot. Fail fast on a dead session or a crash; nothing else stops the
    pass: every step's failure is a finding of the report, not a failed expectation."""
    late = plan.late_join
    lobby = late and plan.d_lobby
    h = script_h(plan, LATE_JOIN_MARK if late and not lobby else None)
    h_args = probe_args(H_ARGS, plan.probe, plan.game_args)
    if lobby:  # the join by code with the host's let in: the server's Hello gate on (the party flow's)
        h_args = [x for x in h_args if x not in LOBBY_H_ARGS_DROP] + [x for x in LOBBY_H_ARGS_ADD if x not in h_args]
    forbid = exit_forbid(h) + [PAUSE_FATAL, MADE_COPY_LEFT]
    out = [f'name = "{case_name(plan.level, plan.no_end)}"',
           f'layout = "{"server+2" if late else "server+1"}"',
           'stage = "8"',
           'server = "host"',
           f'description = "The automatic level pass of {plan.level}{" without its end (--no-end)" if plan.no_end else ""}'
           ' (skytest levelpass; generated, not committed)"',
           f"timeout = {CASE_TIMEOUT_S}",
           'scenes = "skip"',
           "show_server_window = false",
           'shader_cache = "snapshot"',
           "",
           "[peer.H]",
           f"args = {_toml_list(h_args)}",
           *([f"forbid = {_toml_list(forbid)}"] if forbid else []),
           "script = \'\'\'\n" + h + "\'\'\'",
           ]
    if lobby:
        out += ["", "[peer.D]", f"args = {_toml_list(probe_args(D_ARGS, plan.probe, plan.game_args))}",
                *([f"wait_needs = {D_WAIT_NEEDS}"] if d_through_end(plan) else []),
                "script = \'\'\'\n" + script_d(plan) + "\'\'\'"]
    elif late:
        out += ["", "[peer.D]", f"args = {_toml_list(probe_args(D_ARGS, plan.probe, plan.game_args))}", f"start_after = '{LATE_JOIN_MARK}'",
                *([f"wait_needs = {D_WAIT_NEEDS}"] if d_through_end(plan) else []),
                "script = \'\'\'\n" + script_d(plan) + "\'\'\'"]
    return "\n".join(out) + "\n"
