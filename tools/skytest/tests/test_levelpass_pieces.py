"""levelpass_pieces: the run-time movers' spawners, their start triggers and the sweep's stay; a calibration on
ww1's own data (its travelling islands) when the levelpass folder is here."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import levelpass_pieces as pc  # noqa: E402

ON_ENTER = "messenger activationFlags 8 = eMAF_OnEnter  count 1 (default)  max 1  delay 0 (default)"


def node(key: str, cls: str, comps: list[str], zone: str = "madeup") -> dict:
    return {"key": key, "name": key.split(".")[-1], "unit": key.split(".")[0], "zone": zone, "class": cls, "group": "",
            "kind": "", "position": [0, 0, 0], "components": [{"class": c} for c in comps]}


def telegram(frm: str, to: list[str], message: str, when: str, fields=None, delay=None, roles=None) -> dict:
    return {"kind": "telegram", "from": frm, "to": to, "message": message, "when": when, "fields": fields or {},
            "delay": delay, "roles": roles}


def spawn(frm: str, inst: str) -> dict:
    return {"kind": "spawn", "from": frm, "to": [inst], "message": None, "when": "spawns on message"}


REARM = "messenger on Scripts.SpawnerComponentMessage {AnglesOverride: x}  count 1 (default)  max -1 (default)  delay 7"
LOGIC = {"nodes": [
    node("ez.Island0", "CEntity", ["Scripts.SpawnerComponentData"]),
    node("ez.Island1", "CEntity", ["Scripts.SpawnerComponentData"]),
    node("ez.Island0_inst", "CPhysicalEntity", ["CSplineMoverComponentData", "CBobComponentData"]),
    node("ez.Island1_inst", "CPhysicalEntity", ["CSplineMoverComponentData"]),
    node("ez.Ship", "CEntity", ["Scripts.SpawnerComponentData"]),
    node("ez.Ship_inst", "CPhysicalEntity", ["CSplineMoverComponentData"]),
    node("ez.Bird", "CEntity", ["Scripts.SpawnerComponentData"]),
    node("ez.Bird_inst", "CActor", ["CSplineMoverComponentData"]),
    node("ez.Start_Trig", "CScriptTriggerEntity", []), node("ez.Despawn_Trig", "CScriptTriggerEntity", [])],
    "edges": [
        spawn("ez.Island0", "ez.Island0_inst"), spawn("ez.Island1", "ez.Island1_inst"), spawn("ez.Ship", "ez.Ship_inst"),
        spawn("ez.Bird", "ez.Bird_inst"),
        telegram("ez.Island0", [], pc.SPAWN_MSG, REARM, roles="1"),
        telegram("ez.Island1", [], pc.SPAWN_MSG, REARM, roles="1"),
        telegram("ez.Start_Trig", ["ez.Island0"], pc.SPAWN_MSG, ON_ENTER, delay=1.0),
        telegram("ez.Start_Trig", ["ez.Island1"], pc.SPAWN_MSG, ON_ENTER, delay=4.0),
        telegram("ez.Start_Trig", ["ez.Ship"], pc.SPAWN_MSG, ON_ENTER),
        telegram("ez.Despawn_Trig", ["ez.Island0", "ez.Island1"], pc.SPAWN_MSG, ON_ENTER,
                 fields={"messageBehavior": "RemoveAll"})]}


class Pieces(unittest.TestCase):
    def test_spawners_their_periods_and_no_actor(self) -> None:
        self.assertEqual(pc.mover_spawners(LOGIC, "madeup"), {"ez.Island0": 7.0, "ez.Island1": 7.0, "ez.Ship": 0.0})

    def test_the_start_trigger_and_its_stay(self) -> None:
        starts = pc.mover_starts(LOGIC, "madeup")
        self.assertEqual(len(starts), 1)  # the RemoveAll trigger never starts; the one-shot ship is no step
        m = starts[0]
        self.assertEqual((m.name, m.spawner_names, m.first_delay, m.period, m.stay_s),
                         ("Start_Trig", ["Island0", "Island1"], 4.0, 7.0, 25.0))
        self.assertEqual(pc.mover_starts(LOGIC, "madeup", forbidden={"ez.Start_Trig"}), [])


LEVELPASS = Path.home() / "Documents" / "T3Code" / "SwapForceOnline-work" / "levelpass"


@unittest.skipUnless((LEVELPASS / "ww1" / "logic.json").is_file(), "the levelpass folder's data is not on this machine")
class Calibration(unittest.TestCase):
    def test_ww1_islands(self) -> None:
        logic = json.loads((LEVELPASS / "ww1" / "logic.json").read_text(encoding="utf-8"))
        [m] = pc.mover_starts(logic, "ww1")
        self.assertEqual((m.name, len(m.spawners), m.period, m.first_delay, m.stay_s),
                         ("EZ3_StartIslands_Trig", 5, 7.0, 4.0, 25.0))
        tb = json.loads((LEVELPASS / "tutorial_b" / "logic.json").read_text(encoding="utf-8"))
        self.assertEqual(pc.mover_starts(tb, "tutorial_b"), [])  # its bomber ships spawn once


if __name__ == "__main__":
    unittest.main()
