"""levelpass_holds: what holds a level's enemies, what releases each (walked back to a player's act), and the
gates on a level's way; a calibration on the three levels' own data when the levelpass folder is here."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import levelpass_holds as lh  # noqa: E402

ON_ENTER = "messenger activationFlags 8 = eMAF_OnEnter  count 1 (default)  max 1"
ON_DONE = "messenger on Scripts.CutscenePlayerMessage {MessageType: Done}  activationFlags 0 = none (default)"


def node(key: str, group: str, job: str | None = None, enabled: str = "true", cls: str = "CActor") -> dict:
    n = {"key": key, "name": key.split(".")[-1], "unit": key.split(".")[0], "zone": "madeup", "class": cls,
         "group": group, "kind": "", "position": [0.0, 0.0, 0.0], "components": [], "script_data": []}
    if job is not None:
        n["script_data"].append({"component": "instance_Scripts.JobStateComponentData",
                                 "class": lh.JOB_STATE, "fields": [{"name": "_isEnabled", "value": enabled},
                                                                   {"name": "SpawnStateName", "value": job}]})
    return n


def edge(frm: str, to: list[str], message: str, when: str, fields: dict | None = None, delay=None) -> dict:
    return {"from": frm, "to": to, "message": message, "when": when, "fields": fields or {}, "delay": delay}


# ww1's intro, made up: a lock's OnOpen plays a cutscene; its Done releases two held Greebles. A trigger releases
# a third; a fourth is held with nothing that releases it; a fifth's job state is off (not held).
LOGIC = {"schema": 1, "level": "madeup", "nodes": [
    node("intro.Greeble", "enemy", "Idle"), node("intro.Greeble001", "enemy", "Bored"),
    node("enc.Melee", "enemy", "Job_Idle"), node("cage.Plant", "enemy", "Capture_Loop"),
    node("pad.Chompy", "enemy", "Idle", enabled="false"),
    node("puzzles.Lock_Gate", "lock", cls="CGameEntity"), node("intro.Scene_Manager", "cutscene", cls="CEntity"),
    node("enc.Wake_Trigger", "trigger", cls="CScriptTriggerEntity")],
    "edges": [
        edge("puzzles.Lock_Gate", ["intro.Scene_Manager"], lh.CUTSCENE_MSG, "Scripts.LockedObjectComponentData.OnOpen"),
        edge("intro.Scene_Manager", ["intro.Greeble", "intro.Greeble001"], lh.RELEASE, ON_DONE, delay=0.5),
        edge("enc.Wake_Trigger", ["enc.Melee"], lh.RELEASE, ON_ENTER)]}


class Holds(unittest.TestCase):
    def test_a_lock_a_trigger_and_nothing(self) -> None:
        hs = lh.holds_of(LOGIC, [("amb.Ambi_Chompy_03", "Ambi_Chompy_03", "amb", (1.0, 2.0, 3.0))])
        self.assertEqual(sorted(hs), ["amb.Ambi_Chompy_03", "cage.Plant", "enc.Melee", "intro.Greeble",
                                      "intro.Greeble001"])  # pad.Chompy: its job state off, not held
        g = hs["intro.Greeble"]
        self.assertEqual((g.how, len(g.releases)), ("job state Idle", 1))
        r = g.releases[0]
        self.assertEqual((r.act, r.act_key, r.path, r.delay), (lh.LOCK, "puzzles.Lock_Gate",
                                                               ["Lock_Gate", "Scene_Manager"], 0.5))
        self.assertIn("a player: open the lock Lock_Gate", g.words())
        m = hs["enc.Melee"].releases[0]
        self.assertEqual((m.act, m.act_key), (lh.ENTER, "enc.Wake_Trigger"))
        self.assertTrue(hs["cage.Plant"].ambient)
        self.assertIn("nothing in the level's logic releases it (ambient)", hs["amb.Ambi_Chompy_03"].words())

    def test_a_trigger_that_plays_its_own_cutscene_is_entered(self) -> None:
        logic = {"nodes": [node("f.Chompy", "enemy", "Idle"), node("f.Intro_Trigger", "trigger", cls="CScriptTriggerEntity")],
                 "edges": [edge("f.Intro_Trigger", ["f.Chompy"], lh.RELEASE, ON_DONE)]}
        r = lh.holds_of(logic)["f.Chompy"].releases[0]
        self.assertEqual((r.act, r.path), (lh.ENTER, ["Intro_Trigger"]))

    def test_a_release_with_no_player_act_says_so(self) -> None:
        logic = {"nodes": [node("z.Lobber", "enemy", "Idle")],
                 "edges": [edge("z.Lobber", ["z.Lobber"], lh.RELEASE, "messenger on Scripts.StringMessage {MessageString: "
                                "Take Aim}")]}
        r = lh.holds_of(logic)["z.Lobber"].releases[0]
        self.assertEqual(r.act, "")
        self.assertIn("no player act found in the logic", r.words())


def lock_entity(name: str, unit: str, lock: str, zone: str = "madeup", comps: list[str] | None = None) -> dict:
    return {"name": name, "unit": unit, "zone": zone, "group": "lock", "position": [0, 0, 0],
            "components": [lh.LOCKED] + (comps or []), "details": {"LockDatas": lock}}


class Gates(unittest.TestCase):
    def test_counted_and_not(self) -> None:
        manifest = {"entities": [
            lock_entity("Intro_LockpickGate", "puzzles", "Scripts.DemonLockData{SealData: MazeMinigameSeal_gen}"),
            lock_entity("EC1_DemonGate", "enc1", "Scripts.DemonLockData{OpenDelay: 2, SealData: DemonGateSeal_gen}"),
            lock_entity("Key_Gate", "area2", "Scripts.KeyLockData{KeyData: Door_Key, UnlockRadius: 260}"),
            lock_entity("ElementalGate", "ez1", "Scripts.ElementalLockSingleElementData{UnlockRadius: 300}"),
            lock_entity("EZ1_GearDoor", "ez1", "Scripts.DemonLockData{SealData: MasterPuzzleLockSeal_gen}"),
            lock_entity("Arena_Gate", "area1", "Scripts.DemonLockData{OpenDelay: 2, StartsOpen: true}"),
            lock_entity("Chest", "loot", "", comps=["Scripts.MicrogameComponentData"]),
            lock_entity("Gate", "lp_puzzle01", "Scripts.DemonLockData{SealData: LockPick_gen}", zone="lp_puzzle01")]}
        logic = {"nodes": [node("enc1.EC1_DemonGate", "lock", cls="CGameEntity"), node("enc1.Wave_Manager", "encounter")],
                 "edges": [edge("enc1.Wave_Manager", ["enc1.EC1_DemonGate"], "", "encounter eEEP_DeathEvent after -1 events")]}
        gates = {g.name: g for g in lh.required_gates(manifest, logic, "madeup")}
        self.assertEqual(sorted(n for n, g in gates.items() if g.required), ["EC1_DemonGate", "Intro_LockpickGate", "Key_Gate"])
        self.assertEqual(gates["Intro_LockpickGate"].kind, lh.SPARK_LOCK)
        self.assertEqual(gates["EC1_DemonGate"].opener, "win its fight (Wave_Manager)")
        self.assertEqual(gates["Key_Gate"].opener, "fetch its key Door_Key")
        self.assertIn("elemental gate", gates["ElementalGate"].kind)
        self.assertIn("behind an elemental gate", gates["EZ1_GearDoor"].kind)
        self.assertIn("starts open", gates["Arena_Gate"].kind)
        self.assertEqual(gates["Chest"].kind, "a chest")
        self.assertIn("sub-zone", gates["Gate"].kind)


LEVELPASS = Path.home() / "Documents" / "T3Code" / "SwapForceOnline-work" / "levelpass"


def level(name: str) -> tuple[dict, dict]:
    return (json.loads((LEVELPASS / name / "logic.json").read_text(encoding="utf-8")),
            json.loads((LEVELPASS / name / "manifest.json").read_text(encoding="utf-8")))


@unittest.skipUnless(all((LEVELPASS / n / "logic.json").is_file() for n in ("tutorial_a", "tutorial_b", "ww1")),
                     "the levelpass folder's data is not on this machine (it is derived from the disc)")
class Calibration(unittest.TestCase):
    """The three levels of tonight's fights (logs/enemylook-notes.txt)."""

    def test_ww1_intro_greebles_wait_for_the_lockpick_gate(self) -> None:
        logic, manifest = level("ww1")
        hs = lh.holds_of(logic)
        for k in ("ww1_levelintro.GreebleMelee", "ww1_levelintro.GreebleMelee001"):
            r = hs[k].releases[0]
            self.assertEqual((r.act, r.path), (lh.LOCK, ["IntroArea_LockpickGate", "LevelIntro_GreebleCutscene_Manager"]))
        req = [g.name for g in lh.required_gates(manifest, logic, "ww1") if g.required]
        self.assertEqual(req, ["IntroArea_LockpickGate", "EC001_DemonGate", "HoneyBadger_MinigameGate",
                               "Finale_DemonGate008", "FinalPuzzle_MinigameGate", "PuzzleGateSawtooth001"])

    def test_tutorial_b_end_chompy_and_the_ambient_ones(self) -> None:
        logic, manifest = level("tutorial_b")
        disabled = [(f"{e['unit']}.{e['name']}", e["name"], e["unit"], None) for e in manifest["entities"]
                    if e.get("group") == "enemy" and str((e.get("details") or {}).get("_isEnabled", "")).startswith("false")]
        hs = lh.holds_of(logic, disabled)
        r = hs["tutorial_b_encounter_i.Chompy"].releases[0]
        self.assertEqual((r.act, r.path), (lh.ENTER, ["SwapPartsHint_RemoveTrigger"]))
        self.assertTrue(hs["tutorial_b_encounter_a.Ambi_Chompy_03"].ambient)
        self.assertEqual(sum(1 for t in logic["edges"] if t.get("message") == lh.RELEASE), 21)
        req = [(g.name, g.opener) for g in lh.required_gates(manifest, logic, "tutorial_b") if g.required]
        # W34: the arena gate is required: its fight's three managers, upstream first.
        self.assertEqual(req, [("TutorialBattleGate_01", "win its fight (EA_Manager_W1, EA_Manager_W1_B, EA_Manager_W1_B_"
                                "componentDataOverrides_archetype_CEncounterManagerComponentData_encounterActTargets_0_"
                                "targetToActOn)"),
                               ("TutorialLockedGate", "fetch its key LockedDoorKey_Intro"),
                               ("TutorialLockedGate", "fetch its key LockedDoorKey_2")])

    def test_tutorial_a_has_no_gate_and_no_release(self) -> None:
        logic, manifest = level("tutorial_a")
        self.assertEqual([g for g in lh.required_gates(manifest, logic, "tutorial_a") if g.required], [])
        self.assertTrue(all(h.ambient for h in lh.holds_of(logic).values()))


if __name__ == "__main__":
    unittest.main()
