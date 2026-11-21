"""The level's end, derived from its own data (the level pass's exit; levelpass.py runs the steps).

The static level reader's manifest.json names the level-end chain (`level_end`: the telegrams from the
first sender to the map load, tools/level_logic.py) and its logic.json holds every telegram with the
condition its sender sends it on (`when`). This walks back from the chain's last telegram (the map load)
along the telegrams that cause each sender to send, to what a player has to do:

- a trigger to enter: a sender whose telegram goes on `eMAF_OnEnter` (ww1: LevelOutro_Trigger, which
  starts CouncilGillmen's conversation; its end starts CouncilYeti's, whose end starts CouncilEsmeralda's,
  whose end plays AudioKludge, which sends the map load);
- a counted fight: a sender whose telegram goes on `count N` (N > 1) of a message others send on their
  death (tutorial_b: EndEncounter_Manager, 11 ImDead from its own unit's placed enemies and spawners'
  spawnees). Its starters are the triggers (`eMAF_OnEnter`) that send to the counted placed enemies
  (tutorial_b's Start_Fight); its anchor, the place the fight is fought around, is a counted sender that
  does not die (a spawn point, a spawner) nearest the counted placed enemies; its waves are the counter's
  own telegrams at lower counts (each starts a spawner);
- an enemy whose death sends the telegram (a fight of one);
- nothing: the level ends on its own (tutorial_a: its intro cutscene's end starts the ride that loads
  tutorial_b), a timed level;
- a mechanic the pass cannot carry out (UNSUPPORTED): a sender on the way back whose cause is none of the
  above (a mini-game's success list, a turret's exit, a boss's own end telegram, a ride's spline event, a
  message no telegram in the level sends: a script's). Each such dead end is classed by the components of
  its node into a family of the co-op triage (FAMILIES: switch or mini-game result, fetch hand-in, thrown
  object, turret or vehicle, run-time spawns, boss, ride); one with none of them is `unknown`, named by its
  node, never guessed. A dead end that is a scripted sequence (SEQUENCE_ENDS) is how a timed level ends.

How a sender is caused, read from its telegram's `when` (the reader's words):
  `messenger on <Message> {<fields>}`        telegrams into it of that message with those fields
  `CJuiceConversationData._endTelegrams`     a conversation started in it (CConversationMessage into it)
  `when its cutscene ... ends`               its cutscene played (Scripts.CutscenePlayerMessage, not Done)
  `CSplineMoverComponentEventData._telegrams` its spline started (CSplineMoverMessage into it)
  `eMAF_OnEnter` / `eMAF_OnDeath` / `on a spawnee` a player's act (entered; died; a spawnee died)

No entity names, positions or per-level lists here: names, units and positions come from the level's
data; the steps address objects by their live reference (the probe's `auth:<zone>:<index>`, else a name
the probe saw live: levelpass.attach_refs). Objects of another zone (a swap zone, a staging map) are never
a step: a player reaches them only through their own entry. Pure; tests/test_levelpass_end.py."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

MAX_DEPTH = 12            # telegrams walked back from the map load at most
TRIGGER = "trigger"
FIGHT = "fight"
TIMED = "timed"
UNSUPPORTED = "unsupported"   # the end needs a mechanic the pass cannot carry out (family, node)

# The co-op triage's families (logs/triage/WHOLE-GAME.txt, section 2), by the component classes that carry
# them in the level data; first match wins (a boss's turret is the boss's). `H` (the level change) and the
# lines a run shows are not static.
FAMILIES = {
    "A": "a switch or mini-game result",
    "B": "a fetch hand-in",
    "C": "a thrown object",
    "D": "a turret or vehicle",
    "E": "run-time spawns",
    "F": "a boss",
    "G": "a ride (launch path, rail)",
    "M": "a menu or dialog choice",
}
FAMILY_COMPONENTS = (
    ("F", re.compile(r"Boss(?!Quest)|KalamityAI|SheepMonsterLevel|FightingBird")),
    ("D", re.compile(r"Turret|EnterVehicle")),
    ("B", re.compile(r"FetchBase|FetchManager")),
    ("C", re.compile(r"ThrowEntityBeingCarried")),
    ("A", re.compile(r"Microgame|Minigame|Hourglass|RotatingDoor|SequenceMatch")),
    ("G", re.compile(r"LaunchPath|PlayerLaunch|GrindRail")),  # RIDE
    ("M", re.compile(r"CUIDialogBoxOption")),
)
RIDE = dict(FAMILY_COMPONENTS)["G"]
UNKNOWN = "unknown"
# A dead end that is the level's own scripted sequence (an animation sequence's end): the level ends on its
# own (tutorial_a's ride).
SEQUENCE_ENDS = re.compile(r"AnimateEntitySequenceComponentData\.OnSequenceEndTelegrams")
# A trigger the level's makers named a debug trigger (fg1's A1_Outro_DebugTrigger) is their shortcut to the
# end, not a player's way: never the pass's end (it would "complete" the level by skipping it).
DEBUG_NAME = re.compile(r"debug", re.I)


def family_of(components: list[str]) -> str:
    """The family a node's component classes carry ('' for none)."""
    for fam, rx in FAMILY_COMPONENTS:
        if any(rx.search(c) for c in components):
            return fam
    return ""


def dead_end_family(components: list[str], when: str) -> str:
    """A dead end's family: the component its telegram's `when` names first (a mini-game's success list,
    a vehicle's exit, a boss's end telegram, a dialog option), a spline event as the node's ride, else the
    node's own components; '' for none (then UNKNOWN)."""
    fam = family_of([when])
    if fam:
        return fam
    if "SplineMover" in when and any(RIDE.search(c) for c in components):
        return "G"
    return family_of(components)

ON_ENTER = "eMAF_OnEnter"
ON_DEATH = "eMAF_OnDeath"
DEATHS = (ON_DEATH, "on a spawnee")  # what a counted fight counts
LISTEN = re.compile(r"messenger on (\S+)(?: \{([^}]*)\})?")
COUNT = re.compile(r"\bcount (\d+)")


@dataclass
class Telegram:
    """One logic.json edge, one receiver."""
    sender: str            # node key "<unit>.<name>"
    receiver: str          # node key
    message: str
    fields: dict
    when: str
    delay: float | None = None


@dataclass
class Node:
    key: str
    name: str
    unit: str
    zone: str
    cls: str
    group: str
    kind: str
    pos: tuple[float, float, float] | None
    components: list[str] = field(default_factory=list)   # the node's component classes


@dataclass
class End:
    """What a player has to do to end the level (kind), and how it was found (path)."""
    kind: str                                        # TRIGGER, FIGHT or TIMED
    path: list[str] = field(default_factory=list)    # names from the player's act to the map load
    path_keys: list[str] = field(default_factory=list)  # their node keys
    trigger: str = ""                                # TRIGGER: the node key to enter
    counter: str = ""                                # FIGHT: the counter's key ("" for a fight of one)
    count: int = 0                                   # FIGHT: deaths the counter needs
    counted: list[str] = field(default_factory=list)   # FIGHT: keys of the counted senders (its unit's)
    enemies: list[str] = field(default_factory=list)   # FIGHT: the counted placed enemies
    starters: list[str] = field(default_factory=list)  # FIGHT: triggers that start counted enemies
    anchor: str = ""                                 # FIGHT: the key the fight is fought around
    waves: list[int] = field(default_factory=list)   # FIGHT: the counter's lower counts (each starts a wave)
    why: str = ""                                    # TIMED, UNSUPPORTED: why no player act was found
    family: str = ""                                 # UNSUPPORTED: FAMILIES' key, or UNKNOWN
    blockers: list[tuple[str, str, str]] = field(default_factory=list)  # (family, node key, its `when`)
    on_path: list[tuple[str, str]] = field(default_factory=list)        # (family, node key) on the end's path

    def keys(self) -> set[str]:
        """Every node the end acts on or counts (left out of the sweep)."""
        return {k for k in [self.trigger, self.counter, self.anchor, *self.counted, *self.enemies,
                            *self.starters] if k}


def _pos(v: object) -> tuple[float, float, float] | None:
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(c, (int, float)) for c in v):
        return (float(v[0]), float(v[1]), float(v[2]))
    return None


def read_logic(logic: dict) -> tuple[dict[str, Node], list[Telegram]]:
    """logic.json (schema 1): its nodes by key and its edges, one Telegram per receiver."""
    nodes: dict[str, Node] = {}
    for n in logic.get("nodes", []) if isinstance(logic, dict) else []:
        if not isinstance(n, dict) or not n.get("key"):
            continue
        nodes[str(n["key"])] = Node(key=str(n["key"]), name=str(n.get("name") or ""), unit=str(n.get("unit") or ""),
                                    zone=str(n.get("zone") or ""), cls=str(n.get("class") or ""),
                                    group=str(n.get("group") or ""), kind=str(n.get("kind") or ""),
                                    pos=_pos(n.get("position")),
                                    components=[str(c.get("class") or "") for c in n.get("components") or []
                                                if isinstance(c, dict)])
    tels: list[Telegram] = []
    for e in logic.get("edges", []) if isinstance(logic, dict) else []:
        if not isinstance(e, dict) or not e.get("from"):
            continue
        # A telegram to roles (the map load to the world) names no receiver: one with receiver "". Role bit 1
        # is its sender itself (tools/level_logic.py's role rule): ww1's LevelOutroEdc plays its own cutscene.
        receivers = list(e.get("to") or [])
        roles = str(e.get("roles") if e.get("roles") is not None else "")
        if (roles.isdigit() and int(roles) & 1) or roles.startswith("self"):
            receivers.append(str(e["from"]))
        for to in receivers or [""]:
            tels.append(Telegram(sender=str(e["from"]), receiver=str(to), message=str(e.get("message") or ""),
                                 fields=e.get("fields") if isinstance(e.get("fields"), dict) else {},
                                 when=str(e.get("when") or ""),
                                 delay=e.get("delay") if isinstance(e.get("delay"), (int, float)) else None))
    return nodes, tels


def _fields(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        k, sep, v = part.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


def _matches(t: Telegram, message: str, fields: dict[str, str]) -> bool:
    return t.message == message and all(str(t.fields.get(k, "")) == v for k, v in fields.items())


def count_of(when: str) -> int:
    m = COUNT.search(when)
    return int(m.group(1)) if m else 1


def causes(t: Telegram, into: dict[str, list[Telegram]]) -> list[Telegram] | None:
    """The telegrams into t's sender that make it send t; None when t goes on a player's act (or its
    cause is not one this reads)."""
    w, inc = t.when, into.get(t.sender, [])
    if ON_ENTER in w or ON_DEATH in w or "on a spawnee" in w:
        return None
    m = LISTEN.search(w)
    if m:
        return [c for c in inc if _matches(c, m.group(1), _fields(m.group(2)))]
    if "_endTelegrams" in w:
        return [c for c in inc if c.message == "CConversationMessage"]
    if "cutscene" in w and "ends" in w:
        return [c for c in inc if c.message == "Scripts.CutscenePlayerMessage" and c.fields.get("MessageType") != "Done"]
    if "CSplineMoverComponentEventData" in w:
        return [c for c in inc if c.message == "CSplineMoverMessage"]
    return []


SEGMENT_SENDER = re.compile(r"^\s*(?:\(inferred\)\s*)?(?:\{([^}]*)\}|(\S+))(?:\s*\[x\d+\])?\s*-\[(\S+)")


def last_link(chain: str) -> tuple[list[str], str] | None:
    """The chain's last segment: its sender names and its message class."""
    segs = [s for s in chain.split("=>") if s.strip()]
    if not segs:
        return None
    m = SEGMENT_SENDER.match(segs[-1])
    if not m:
        return None
    names = [n.strip() for n in (m.group(1) or m.group(2)).split(",") if n.strip() and n.strip() not in ("?", "-")]
    return names, m.group(3).rstrip("]")


def derive_end(chains: list[str], logic: dict, level: str) -> End | None:
    """The level's end (End), or None when the data has no chain or no logic to walk."""
    nodes, tels = read_logic(logic)
    if not tels or not chains:
        return None
    into: dict[str, list[Telegram]] = {}
    for t in tels:
        into.setdefault(t.receiver, []).append(t)

    def own(key: str) -> bool:
        n = nodes.get(key)
        return n is None or not n.zone or n.zone.lower() == level.lower()

    def name(key: str) -> str:
        return nodes[key].name if key in nodes else key.split(".")[-1]

    starts: list[Telegram] = []
    for chain in chains:
        link = last_link(chain)
        if link is None:
            continue
        senders, message = link
        starts = [t for t in tels if name(t.sender) in senders and t.message == message and own(t.sender)]
        if starts:
            break
    if not starts:
        # Read, not guessed: the chain names a map load this logic does not hold (brn4's outro cutscene).
        link = next((last_link(c) for c in chains if last_link(c)), None)
        who = ", ".join(link[0]) if link else "?"
        return End(kind=UNSUPPORTED, family=UNKNOWN, blockers=[(UNKNOWN, who, "the map load is not in the logic")],
                   why=f"the end needs a cause this reader does not know at {who} (the level-end chain's map load "
                       "is not among the logic's telegrams)")
    # Breadth first: (telegram, path of names from it to the map load).
    frontier = [(t, [t.sender]) for t in starts]

    def done(e: End, keys: list[str]) -> End:
        e.path_keys = list(dict.fromkeys(keys))
        e.path = [name(k) for k in e.path_keys]
        for k in e.path_keys + ([e.counter] if e.counter else []) + e.counted:
            fam = family_of(nodes[k].components) if k in nodes else ""
            if fam and (fam, k) not in e.on_path:
                e.on_path.append((fam, k))
        if e.kind == FIGHT and any("on a spawnee" in c.when for c in into.get(e.counter, []) + tels
                                   if c.sender in e.counted and c.receiver in (e.counter, "")):
            e.on_path.append(("E", e.counter))
        return e

    dead: list[Telegram] = []   # senders on the way back whose cause this does not read
    seen: set[tuple[str, str, str]] = set()
    for _depth in range(MAX_DEPTH):
        nxt = []
        for t, path in frontier:
            ident = (t.sender, t.receiver, t.when)
            if ident in seen:
                continue
            seen.add(ident)
            n = count_of(t.when)
            if ON_ENTER in t.when and own(t.sender):
                if DEBUG_NAME.search(name(t.sender)):
                    dead.append(Telegram(t.sender, t.receiver, t.message, t.fields,
                                         "a debug trigger: the makers' shortcut to the end, not a player's way"))
                    continue
                return done(End(kind=TRIGGER, trigger=t.sender), path)
            if ON_DEATH in t.when or "on a spawnee" in t.when:
                return done(fight(t.sender, 1, [t], nodes, into, tels, own), path)
            got = causes(t, into)
            if not got:
                if got is not None:
                    dead.append(t)
                continue
            if n > 1 and not any(d in c.when for c in got for d in DEATHS):
                # A count of something other than deaths (ww3: two monkey switches' successes into a relay):
                # no fight; what is counted is what the end needs.
                dead.extend(got)
                continue
            if n > 1:
                return done(fight(t.sender, n, got, nodes, into, tels, own), path)
            for c in got:
                nxt.append((c, [c.sender] + path))
        frontier = nxt
        if not frontier:
            break
    blockers = []
    for t in dead:
        if SEQUENCE_ENDS.search(t.when):
            continue
        fam = dead_end_family(nodes[t.sender].components if t.sender in nodes else [], t.when)
        blockers.append((fam or UNKNOWN, t.sender, t.when))
    if blockers:
        blockers.sort(key=lambda b: b[0] == UNKNOWN)  # a classed mechanic first
        fam, key, when = blockers[0]
        what = FAMILIES.get(fam, "a cause this reader does not know")
        e = End(kind=UNSUPPORTED, family=fam, blockers=blockers,
                why=f"the end needs {what} at {name(key)} ({key}; `{when[:90]}`)")
        return done(e, [t.sender for t in starts])
    return done(End(kind=TIMED, why="no trigger to enter or fight to win among the causes of the level-end chain "
                    "(the level ends on its own)"), [t.sender for t in starts])


def _dist(a, b) -> float:
    return math.dist(a, b)


def fight(counter: str, count: int, counted: list[Telegram], nodes: dict[str, Node],
          into: dict[str, list[Telegram]], tels: list[Telegram], own) -> End:
    """A counted fight: the counter's own unit's counted senders, their starters, an anchor that does
    not die, the waves. A fight of one (count 1, an enemy's own death) has no counter."""
    unit = counter.split(".")[0]
    senders = sorted({t.sender for t in counted})
    if count > 1:
        same = [k for k in senders if k.split(".")[0] == unit]
        senders = same or senders
    senders = [k for k in senders if own(k)]
    enemies = [k for k in senders if k in nodes and nodes[k].group == "enemy"]
    starters: list[str] = []
    for k in enemies:
        for c in into.get(k, []):
            if ON_ENTER in c.when and own(c.sender) and c.sender not in starters:
                starters.append(c.sender)
    placed = [nodes[k].pos for k in enemies if nodes[k].pos] or \
             [nodes[k].pos for k in [counter] if k in nodes and nodes[k].pos]
    centre = tuple(sum(p[i] for p in placed) / len(placed) for i in range(3)) if placed else None
    lasting = [k for k in senders if k in nodes and nodes[k].group != "enemy" and nodes[k].pos]
    anchor = ""
    if centre is not None:
        pool = lasting or [k for k in enemies if nodes[k].pos]
        if pool:
            anchor = min(pool, key=lambda k: (_dist(nodes[k].pos, centre), k))
    # The waves: the counter's own telegrams at a lower count to a spawner (each brings more enemies).
    waves = sorted({count_of(t.when) for t in tels if count > 1 and t.sender == counter and count_of(t.when) < count
                    and (t.receiver in senders or (t.receiver in nodes and nodes[t.receiver].group == "spawner"))})
    return End(kind=FIGHT, counter=counter if count > 1 else "", count=count, counted=senders, enemies=enemies,
               starters=starters, anchor=anchor, waves=waves)
