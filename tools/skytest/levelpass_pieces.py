"""Run-time movers ("level pieces": ww1's travelling islands), from the level's own data: the spawners whose
spawned instance moves by itself, the triggers a player enters to start them, and how long a sweep stays to see
three spawns (the level pass's pieces step; the level check's pieces row grades it).

The game's rule (src/online/world/island_rules.h, online/6-level-pieces-match): an island spawner is a spawner
seen spawning, inside a delivery to itself, a clone with a piece motion class (CSplineMoverComponent,
Scripts.MoverWithWaypointsComponent, Scripts.RotaterComponent, Scripts.RotatingDoorComponent, CBobComponent).
In the data (logic.json): a `spawn` edge from the spawner to its entityInstance node, that node with one of
those classes' data (…ComponentData). Actors (CActor: birds, creatures) are left out: a moving piece is
scenery a player rides, not a creature. A start trigger (logs/pieces-notes.txt's rule) is an entity of the
level's own zone whose telegram on eMAF_OnEnter reaches such a spawner with a Scripts.SpawnerComponentMessage
(not a RemoveAll or Remove) or switches its messenger on (a StringMessage TurnOn). The spawn period is the
spawner's own re-arm (its messenger on Scripts.SpawnerComponentMessage with `delay <d>` sent to itself, ww1: 7).
Only such re-arming spawners get a pieces step (a stream of travelling pieces: three spawns to compare); a
spawner that spawns once (tutorial_b's bomber ships) is left to the sweep. The sweep stays 3 x the longest
period plus the largest first delay (ww1: 3 x 7 + 4 = 25 s), at most STAY_CAP_S.

Pure; tests/test_levelpass_pieces.py."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import levelpass_end as le

PIECE_DATA = ("CSplineMoverComponentData", "Scripts.MoverWithWaypointsComponentData", "Scripts.RotaterComponentData",
              "Scripts.RotatingDoorComponentData", "CBobComponentData")
SPAWN_MSG = "Scripts.SpawnerComponentMessage"
REARM_DELAY = re.compile(r"messenger on Scripts\.SpawnerComponentMessage.*\bdelay (\d+(?:\.\d+)?)")
STAY_CAP_S = 40.0


@dataclass
class MoverStart:
    trigger: str                     # the trigger's node key
    name: str                        # its name
    spawners: list[str] = field(default_factory=list)   # the mover spawners' node keys it reaches
    spawner_names: list[str] = field(default_factory=list)
    first_delay: float = 0.0         # the largest delay from the trigger to a spawner
    period: float = 0.0              # the longest spawn period among them
    stay_s: float = 0.0              # how long the sweep stays (3 periods and the first delay)


def mover_spawners(logic: dict, level: str) -> dict[str, float]:
    """The level's own mover spawners (node key -> spawn period, 0 when it does not re-arm itself)."""
    nodes, _tels = le.read_logic(logic)
    out: dict[str, float] = {}
    for e in logic.get("edges", []) if isinstance(logic, dict) else []:
        if not isinstance(e, dict) or e.get("kind") != "spawn" or not e.get("from"):
            continue
        sp = nodes.get(str(e["from"]))
        if sp is None or (sp.zone and sp.zone.lower() != level.lower()):
            continue
        for to in e.get("to") or []:
            inst = nodes.get(str(to))
            if inst is not None and inst.cls != "CActor" and any(c in PIECE_DATA for c in inst.components):
                out[sp.key] = 0.0
    for e in logic.get("edges", []) if isinstance(logic, dict) else []:
        if isinstance(e, dict) and e.get("from") in out and e.get("message") == SPAWN_MSG:
            m = REARM_DELAY.search(str(e.get("when") or ""))
            if m and str(e.get("roles") or "").startswith("1"):
                out[str(e["from"])] = max(out[str(e["from"])], float(m.group(1)))
    return out


def mover_starts(logic: dict | None, level: str, forbidden: set[str] = frozenset()) -> list[MoverStart]:
    """The triggers that start the level's mover spawners, fewest first covering every spawner one reaches
    (a greedy cover); `forbidden` node keys (the end's) are never one."""
    if not logic:
        return []
    nodes, _tels = le.read_logic(logic)
    spawners = mover_spawners(logic, level)
    if not spawners:
        return []
    reach: dict[str, dict[str, float]] = {}
    for e in logic.get("edges", []) if isinstance(logic, dict) else []:
        if not isinstance(e, dict) or e.get("kind") != "telegram" or le.ON_ENTER not in str(e.get("when") or ""):
            continue
        frm = str(e.get("from") or "")
        n = nodes.get(frm)
        if n is None or frm in forbidden or (n.zone and n.zone.lower() != level.lower()):
            continue
        fields = e.get("fields") if isinstance(e.get("fields"), dict) else {}
        starts = (e.get("message") == SPAWN_MSG and "Remove" not in str(fields.get("messageBehavior", ""))) or \
            (e.get("message") == "Scripts.StringMessage" and fields.get("MessageString") == "TurnOn")
        if not starts:
            continue
        delay = float(e.get("delay")) if isinstance(e.get("delay"), (int, float)) else 0.0
        for to in e.get("to") or []:
            if spawners.get(str(to), 0.0) > 0:  # a re-arming mover spawner
                got = reach.setdefault(frm, {})
                got[str(to)] = max(got.get(str(to), 0.0), delay)
    out: list[MoverStart] = []
    left = set().union(*[set(v) for v in reach.values()]) if reach else set()
    while left:
        best = max(sorted(reach), key=lambda k: len(set(reach[k]) & left))
        covered = set(reach[best]) & left
        if not covered:
            break
        left -= covered
        sps = sorted(reach[best])
        first = max(reach[best].values())
        period = max(spawners[s] for s in sps)
        stay = min(STAY_CAP_S, 3 * period + first)
        out.append(MoverStart(best, nodes[best].name, sps, [nodes[s].name for s in sps], first, period, stay))
    return out
