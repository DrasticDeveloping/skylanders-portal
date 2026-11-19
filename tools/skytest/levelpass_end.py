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
  tutorial_b), a timed level.

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

ON_ENTER = "eMAF_OnEnter"
ON_DEATH = "eMAF_OnDeath"
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
    why: str = ""                                    # TIMED: why no player act was found

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
                                    pos=_pos(n.get("position")))
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
        return End(kind=TIMED, why="the level-end chain's map load is not among the logic's telegrams")
    # Breadth first: (telegram, path of names from it to the map load).
    frontier = [(t, [t.sender]) for t in starts]

    def done(e: End, keys: list[str]) -> End:
        e.path_keys = list(dict.fromkeys(keys))
        e.path = [name(k) for k in e.path_keys]
        return e

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
                return done(End(kind=TRIGGER, trigger=t.sender), path)
            if ON_DEATH in t.when or "on a spawnee" in t.when:
                return done(fight(t.sender, 1, [t], nodes, into, tels, own), path)
            got = causes(t, into)
            if not got:
                continue
            if n > 1:
                return done(fight(t.sender, n, got, nodes, into, tels, own), path)
            for c in got:
                nxt.append((c, [c.sender] + path))
        frontier = nxt
        if not frontier:
            break
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
