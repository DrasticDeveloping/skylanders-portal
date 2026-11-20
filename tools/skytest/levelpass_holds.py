"""What holds a level's enemies, what releases each, and the gates a player must open on the way (the level
pass's fights and the level check's completable row), from the level's own data.

A placed enemy the level's script holds cannot be hurt until something releases it: the guest's per-victim hit
(sub_8297E310) sets the hit's no-damage byte when the victim's state says so (vtable +484, sub_8298A810), and a
held enemy's state does. Tonight's three "hits that do no damage" were all such enemies (logs/enemylook-notes.txt):
ww1's intro GreebleMelee pair (Job_Idle, released by LevelIntro_GreebleCutscene_Manager's Done, which
IntroArea_LockpickGate's OnOpen starts), tutorial_b's end-fight Chompy (released by SwapPartsHint_RemoveTrigger)
and tutorial_b's ambient Chompies (their AI component disabled, nothing releases them).

Held, read from the data:
- an enabled Scripts.JobStateComponentData (logic.json's script_data: `_isEnabled true`, its SpawnStateName);
  its release is a Scripts.LeaveJobStateMessage telegram to it (the component leaves its job state on the first);
- an enemy whose manifest details say `_isEnabled false` (the static reader's details of its AI component):
  nothing in the logic enables it, so it is ambient.
Each release is walked back to what a player does (player_act): enter a trigger (eMAF_OnEnter), open a lock
(Scripts.LockedObjectComponentData.OnOpen), break or kill something (eMAF_OnDeath), press an interaction, or win
an encounter (its death event); through the telegrams that cause each sender to send (levelpass_end.causes), a
cutscene's Done through what starts the cutscene (a trigger that plays its own cutscene is entered).

Gates on the way (required_gates): every Scripts.LockedObjectComponentData of the level's own zone, by its
LockDatas: a Spark Lock (a DemonLockData whose seal is a lock puzzle's: MazeMinigame, LockPick), a battle gate
(DemonLockData, opened by its fight), a key gate (KeyLockData: its key fetched). Not counted, and said why: an
elemental gate (ElementalLock*Data: it needs a Skylander of its element, which no player has to own), a lock in
an elemental gate's unit (behind it), a chest (a MicrogameComponentData), an arena gate that starts open
(StartsOpen: a fight closes it), a lock of a sub-zone (another map). Whether a counted gate blocks the way to
the end is not in the data (no level path); it is a step a player is asked to do on the level's way.

No names, positions or per-level lists here. Pure; tests/test_levelpass_holds.py."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import levelpass_end as le

JOB_STATE = "Scripts.JobStateComponentData"
RELEASE = "Scripts.LeaveJobStateMessage"
LOCKED = "Scripts.LockedObjectComponentData"
CUTSCENE_MSG = "Scripts.CutscenePlayerMessage"
ENEMY_GROUPS = ("enemy",)
MAX_DEPTH = 8

# What a player does at the root of a release (player_act).
ENTER = "enter"     # a trigger volume
LOCK = "lock"       # a lock opened (its OnOpen)
KILL = "kill"       # a death: an enemy, or a destructible's break
PRESS = "press"     # an interaction
WIN = "win"         # an encounter's death event (its wave beaten)
ACT_WORDS = {ENTER: "enter", LOCK: "open the lock", KILL: "break or kill", PRESS: "press", WIN: "win the encounter"}

DONE_LISTEN = re.compile(r"messenger on Scripts\.CutscenePlayerMessage \{MessageType: Done\}")
TRIGGER_COMP = re.compile(r"^CTriggerVolume\w*ComponentData$")


@dataclass
class Release:
    sender: str                 # the node key that sends the LeaveJobStateMessage
    act: str = ""               # ENTER, LOCK, KILL, PRESS, WIN; "" when no player act was found
    act_key: str = ""           # the node a player acts on
    path: list[str] = field(default_factory=list)  # names from the player's act to the release's sender
    delay: float | None = None

    def words(self) -> str:
        chain = " -> ".join(self.path) if self.path else "?"
        root = f"{ACT_WORDS.get(self.act, '?')} {self.path[0]}" if self.act and self.path else \
            "no player act found in the logic"
        return f"released by {self.path[-1] if self.path else self.sender} ({chain}; a player: {root})"


@dataclass
class Hold:
    key: str                    # "<unit>.<name>"
    name: str
    unit: str
    pos: tuple[float, float, float] | None
    how: str                    # "job state <SpawnStateName>" or "its AI component disabled"
    releases: list[Release] = field(default_factory=list)

    @property
    def ambient(self) -> bool:
        return not self.releases

    def words(self) -> str:
        if self.ambient:
            return f"held ({self.how}); nothing in the level's logic releases it (ambient)"
        return f"held ({self.how}); " + "; or ".join(r.words() for r in self.releases)


def _fields(sd: dict) -> dict[str, str]:
    return {str(f.get("name")): str(f.get("value")) for f in sd.get("fields", []) or [] if isinstance(f, dict)}


def _is_trigger(n: le.Node | None) -> bool:
    return n is not None and (n.group == "trigger" or n.cls == "CScriptTriggerEntity" or
                              any(TRIGGER_COMP.match(c) for c in n.components))


def player_act(t: le.Telegram, nodes: dict[str, le.Node], into: dict[str, list[le.Telegram]],
               depth: int = 0, seen: set | None = None) -> tuple[str, str, list[str]]:
    """What a player does that makes t's sender send t: (act, the node acted on, names from it to t's sender);
    ("", "", [t's sender]) when the logic shows none within MAX_DEPTH."""
    seen = set() if seen is None else seen
    name = nodes[t.sender].name if t.sender in nodes else t.sender.split(".")[-1]
    w = t.when
    if le.ON_ENTER in w:
        return ENTER, t.sender, [name]
    if le.ON_DEATH in w or "on a spawnee" in w:
        return KILL, t.sender, [name]
    if "LockedObjectComponentData.OnOpen" in w:
        return LOCK, t.sender, [name]
    if "on press" in w:
        return PRESS, t.sender, [name]
    if "eEEP_DeathEvent" in w:
        return WIN, t.sender, [name]
    if depth >= MAX_DEPTH or (t.sender, w) in seen:
        return "", "", [name]
    seen.add((t.sender, w))
    if DONE_LISTEN.search(w):
        # Its cutscene's end: the telegrams that start that cutscene (a Play), or its own trigger entered.
        got = [c for c in into.get(t.sender, []) if c.message == CUTSCENE_MSG and c.fields.get("MessageType") != "Done"]
        if not got and _is_trigger(nodes.get(t.sender)):
            return ENTER, t.sender, [name]
    else:
        got = le.causes(t, into) or []
    for c in got:
        act, key, path = player_act(c, nodes, into, depth + 1, seen)
        if act:
            return act, key, path + ([name] if path[-1] != name else [])
    return "", "", [name]


def holds_of(logic: dict | None, disabled: list[tuple[str, str, str, tuple | None]] = ()) -> dict[str, Hold]:
    """The level's held enemies by node key: an enabled job state in logic.json, and `disabled` (key, name,
    unit, position) of the manifest's enemies whose details say `_isEnabled false`; each with its releases."""
    nodes, tels = le.read_logic(logic or {})
    into: dict[str, list[le.Telegram]] = {}
    for t in tels:
        into.setdefault(t.receiver, []).append(t)
    out: dict[str, Hold] = {}
    for n in (logic or {}).get("nodes", []) if isinstance(logic, dict) else []:
        if not isinstance(n, dict) or str(n.get("group")) not in ENEMY_GROUPS:
            continue
        for sd in n.get("script_data", []) or []:
            if isinstance(sd, dict) and sd.get("class") == JOB_STATE:
                f = _fields(sd)
                if f.get("_isEnabled", "true") == "true":
                    key = str(n["key"])
                    out[key] = Hold(key, str(n.get("name") or ""), str(n.get("unit") or ""), le._pos(n.get("position")),
                                    f"job state {f.get('SpawnStateName', '?')}")
    for key, name, unit, pos in disabled:
        out.setdefault(key, Hold(key, name, unit, pos, "its AI component disabled: _isEnabled false"))
    for t in tels:
        if t.message == RELEASE and t.receiver in out:
            act, act_key, path = player_act(t, nodes, into)
            out[t.receiver].releases.append(Release(t.sender, act, act_key, path, t.delay))
    return out


# --- gates on the way --------------------------------------------------------------------------------------

SPARK = re.compile(r"MazeMinigame|LockPick", re.I)
KEY_DATA = re.compile(r"KeyData: (\S+?)[,}]")
SPARK_LOCK, BATTLE_GATE, KEY_GATE, PUZZLE_GATE = "Spark Lock", "battle gate", "key gate", "puzzle gate"
# W34: an arena gate starts open and its fight shuts it (tutorial_b's TutorialBattleGate_01: StartsOpen, opened by the
# encounter acts of EA_Manager_W1, EA_Manager_W1_B and W1_B's nested manager). It was "not counted", so no level check
# ever fought it, and the owner was locked in when S's copy of the fight never ended (19 November). Now a fight gate
# like a battle gate: required when its fight is in the logic.
ARENA_GATE = "arena gate"
FIGHT_GATES = (BATTLE_GATE, ARENA_GATE)


@dataclass
class Gate:
    name: str
    unit: str
    kind: str                   # SPARK_LOCK, BATTLE_GATE, KEY_GATE, PUZZLE_GATE, or the reason it is not counted
    required: bool
    opener: str                 # what opens it, in words
    pos: tuple[float, float, float] | None = None
    openers: list[str] = field(default_factory=list)   # the node keys whose telegrams open it (not itself)
    key_data: str = ""                                 # a key gate's KeyData


FIGHT_GROUPS = ("enemy", "encounter")


def fighters_into(gate_key: str, nodes: dict[str, le.Node], tels: list[le.Telegram]) -> list[str]:
    """The enemies and encounter managers whose telegrams reach the gate itself (its own node, not a namesake):
    the fight that opens it; W34: with the encounter managers whose own encounter act starts one of them, upstream
    first (tutorial_b's arena: EA_Manager_W1 and EA_Manager_W1_B act on W1_B's nested manager, which acts on the
    gate: the nested one runs only after the other two ended)."""
    direct = sorted({t.sender for t in tels if t.receiver == gate_key and t.sender != gate_key and t.sender in nodes
                     and nodes[t.sender].group in FIGHT_GROUPS})
    out: list[str] = []
    seen: set[str] = set()

    def upstream(k: str, depth: int) -> None:
        if k in seen or depth > 6:
            return
        seen.add(k)
        if nodes[k].group == "encounter":
            for u in sorted({t.sender for t in tels if t.receiver == k and t.sender != k and t.sender in nodes
                             and nodes[t.sender].group == "encounter" and "encounter" in str(t.when)}):
                upstream(u, depth + 1)
        out.append(k)

    for k in direct:
        upstream(k, 0)
    return out


def required_gates(manifest: dict, logic: dict | None, level: str) -> list[Gate]:
    """Every lock of the level's own zone, counted (required) or not with the reason (the module's head)."""
    nodes, tels = le.read_logic(logic or {})
    by_name: dict[str, list[le.Telegram]] = {}
    for t in tels:
        if t.receiver in nodes:
            by_name.setdefault(nodes[t.receiver].name, []).append(t)
    ents = [e for e in manifest.get("entities", []) or [] if isinstance(e, dict)]
    elemental_units = {str(e.get("unit")) for e in ents if LOCKED in (e.get("components") or [])
                       and "ElementalLock" in str((e.get("details") or {}).get("LockDatas", ""))}
    out: list[Gate] = []
    for e in ents:
        comps = e.get("components") or []
        if LOCKED not in comps:
            continue
        name, unit = str(e.get("name") or ""), str(e.get("unit") or "")
        lock = str((e.get("details") or {}).get("LockDatas", ""))
        pos = le._pos(e.get("position"))
        if str(e.get("zone") or "").lower() != level.lower():
            out.append(Gate(name, unit, "a lock of a sub-zone (another map)", False, "", pos))
        elif "Scripts.MicrogameComponentData" in comps or e.get("group") == "chest":
            out.append(Gate(name, unit, "a chest", False, "", pos))
        elif "ElementalLock" in lock:
            out.append(Gate(name, unit, "an elemental gate (needs a Skylander of its element)", False, "", pos))
        elif unit in elemental_units:
            out.append(Gate(name, unit, f"behind an elemental gate (its unit {unit})", False, "", pos))
        elif "StartsOpen: true" in lock:
            fighters = fighters_into(f"{unit}.{name}", nodes, tels)
            if fighters:
                out.append(Gate(name, unit, ARENA_GATE, True, "win its fight (" + ", ".join(
                    nodes[k].name for k in fighters[:4]) + ")", pos, openers=fighters))
            else:
                out.append(Gate(name, unit, "an arena gate that starts open (no fight in the logic opens it)", False,
                                "", pos))
        elif "KeyLockData" in lock:
            k = KEY_DATA.search(lock)
            out.append(Gate(name, unit, KEY_GATE, True, f"fetch its key {k.group(1) if k else '?'}", pos,
                            key_data=k.group(1) if k else ""))
        elif SPARK.search(lock):
            out.append(Gate(name, unit, SPARK_LOCK, True, "solve its lock puzzle", pos))
        elif "DemonGateSeal" in lock:
            senders = sorted({nodes[t.sender].name for t in by_name.get(name, []) if t.sender in nodes} - {name})
            out.append(Gate(name, unit, BATTLE_GATE, True, "win its fight" + (f" ({', '.join(senders[:4])})" if senders
                                                                                else ""), pos,
                            openers=fighters_into(f"{unit}.{name}", nodes, tels)))
        else:
            senders = sorted({nodes[t.sender].name for t in by_name.get(name, []) if t.sender in nodes} - {name})
            out.append(Gate(name, unit, PUZZLE_GATE, True, "its opener" + (f" ({', '.join(senders[:4])})" if senders
                                                                           else " (not in the logic)"), pos))
    return out
