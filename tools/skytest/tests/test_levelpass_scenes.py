"""--scenes play (levelpass.py: scenes_of, plan_scenes, the scripts, the estimate) and the level check's
expected-scene notes (levelcheck.py), on made-up level data and, when it is there, the stored tutorial_b.
No game. Run: python -I -m unittest discover -s tools/skytest/tests -p "test_levelpass_scenes.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import levelcheck as lc  # noqa: E402
import levelpass as lp  # noqa: E402
import levelpass_end as le  # noqa: E402
from test_levelpass_end import FOLDER, stored  # noqa: E402

PLAYER = lp.SCENE_PLAYER


def obj(unit, name, cls="CEntity", pos=(0.0, 0.0, 0.0), scene="", startup=False, comps=(PLAYER,), box=None,
        ref=""):
    o = lp.Obj(name, cls, pos, lp.GROUP_TRIGGERS if "Trigger" in cls else "other", ident=unit, box=box)
    o.components, o.scene, o.at_startup, o.ref = list(comps), scene, startup, ref or f"auth:lvl:{abs(hash(name)) % 9999}"
    return o


def tel(frm, to, message, when, fields=None, roles=None):
    return {"from": frm, "to": to, "message": message, "fields": fields or {}, "when": when, "roles": roles}


ENTER = "messenger activationFlags 8 = eMAF_OnEnter  count 1 (default)  max 1  delay 0 (default)"
DEATH = "messenger activationFlags 4 = eMAF_OnDeath  count 1 (default)  max 1  delay 0 (default)"


def level() -> lp.Content:
    """An intro at the load, a trigger that plays another entity's scene, a trigger volume that is its own
    scene, a scene after a fight, and a lever."""
    box = ((900.0, -100.0, -100.0), (1100.0, 100.0, 100.0))
    objs = [obj("u", "Intro", scene="LVL_Intro", startup=True),
            obj("u", "Scene_Trig", "CScriptTriggerEntity", (2000.0, 0.0, 0.0), comps=("CTriggerVolumeBoxComponentData",)),
            obj("u", "Lobber_Scene", "CEntity", (2050.0, 0.0, 0.0), scene="LVL_LobberIntro"),
            obj("u", "Self_Trig", "CScriptTriggerEntity", (1000.0, 0.0, 0.0), scene="LVL_ChompyIntro", box=box),
            obj("u", "Boss", "CActor", (5000.0, 0.0, 0.0), comps=("CActorData",)),
            obj("u", "Boss_Down", "CEntity", (5000.0, 0.0, 0.0), scene="LVL_BossDown"),
            obj("u", "Lever_A", "CGameEntity", (3000.0, 0.0, 0.0), comps=("Scripts.LeverComponentData",))]
    objs[-1].group = lp.GROUP_INTERACT
    nodes = [{"key": f"u.{o.name}", "name": o.name, "unit": "u", "zone": "lvl", "class": o.cls, "group": "other",
              "kind": "", "position": list(o.pos), "components": [{"key": c, "class": c} for c in o.components]}
             for o in objs]
    edges = [tel("u.Scene_Trig", ["u.Lobber_Scene"], "Scripts.CutscenePlayerMessage", ENTER),
             tel("u.Boss", ["u.Boss_Down"], "Scripts.CutscenePlayerMessage", DEATH)]
    c = lp.Content("lvl", "made", (0.0, 0.0, 0.0), objs)
    c.logic = {"schema": 1, "nodes": nodes, "edges": edges}
    return c


class Scenes(unittest.TestCase):
    def test_how_each_scene_starts(self):
        got = {sc.name: (sc.kind, sc.starter.name if sc.starter else "") for sc in lp.scenes_of(level())}
        self.assertEqual(got, {"LVL_Intro": ("load", ""), "LVL_LobberIntro": ("trigger", "Scene_Trig"),
                               "LVL_ChompyIntro": ("trigger", "Self_Trig"), "LVL_BossDown": ("fight", "")})

    def test_the_plan_enters_each_scene_trigger_and_keeps_its_sweep(self):
        c = level()
        plain = lp.make_plan(c, late_join=False)
        p = lp.make_plan(level(), late_join=False, scenes=True, scene_lengths={"LVL_LobberIntro": 40.0})
        self.assertEqual(len(p.stops), len(plain.stops))
        entered = sorted(sc.name for scs in p.scene_at.values() for sc in scs)
        self.assertEqual(entered, ["LVL_ChompyIntro", "LVL_LobberIntro"])
        self.assertEqual(p.scene_load_s, lp.SCENE_LOAD_S)  # the intro was never measured
        # The fight's scene (LVL_BossDown) plays inside the sweep too: its 15 s count, it is not entered.
        self.assertEqual(p.scene_extra_s, lp.SCENE_LOAD_S + 40.0 + lp.SCENE_S + 2 * lp.SCENE_STEP_S + lp.SCENE_S)
        h = lp.script_h(p, None)
        self.assertIn("log LEVELPASS scenes expected: LVL_Intro@load, LVL_LobberIntro@trigger+entered", h)
        lobber = next(sc for scs in p.scene_at.values() for sc in scs if sc.name == "LVL_LobberIntro")
        self.assertIn(f"enter_trigger {lobber.starter.ref}\nwait_free {40 + lp.SCENE_WAIT_MARGIN_S}\n", h)
        self.assertIn(f"hold_cap {int(lp.SCENE_LOAD_S) + lp.SCENE_WAIT_MARGIN_S}", h)
        spec = lp.spec_toml(p)
        self.assertIn('scenes = "play"', spec)
        self.assertIn(f"timeout = {int(lp.CASE_TIMEOUT_S + p.scene_extra_s)}", spec)
        self.assertEqual(lp.position_literals(h), [])
        # Without --scenes play nothing changes.
        self.assertNotIn("scenes expected", lp.script_h(plain, None))
        self.assertIn('scenes = "skip"', lp.spec_toml(plain))
        self.assertIn(f"timeout = {lp.CASE_TIMEOUT_S}", lp.spec_toml(plain))

    def test_lengths_measured_in_a_log(self):
        log = "\n".join([
            "[t] Online: scene local 2/L1 start cutscene Tutorial_B_Intro recipient - from the level load",
            "[t] Online: scene local 2/L1 end by natural after 63.0 s (no server scene)",
            "[t] Online: scene local 2/L2 start cutscene TB_GreebleLobberIntro recipient - from x",
            "[t] Online: scene local 2/L2 end by skip after 2.0 s (no server scene)",
            "[t] Online: scene local 3/L1 start cutscene TB_GreebleLobberIntro recipient - from x",
            "[t] Online: scene local 3/L1 end by natural after 14.3 s (no server scene)"])
        self.assertEqual(lp.scene_lengths_from_log(log), {"Tutorial_B_Intro": 63.0, "TB_GreebleLobberIntro": 14.3})


@unittest.skipUnless(FOLDER is not None and stored("tutorial_b"), "no stored tutorial_b data")
class StoredTutorialB(unittest.TestCase):
    def test_tutorial_b_scenes(self):
        c = lp.load_content(FOLDER, "tutorial_b")
        got = {sc.name: (sc.kind, sc.starter.name if sc.starter else "") for sc in lp.scenes_of(c, lp.derive(c))}
        self.assertEqual(got["Tutorial_B_Intro"], ("load", ""))
        self.assertEqual(got["TB_GreebleLobberIntro"], ("trigger", "EB_LobberIntro_Trigger"))
        self.assertEqual(got["TB_ChompyPlantIntro"][0], "trigger")
        self.assertEqual(got["Tutorial_B_Outro"][0], "end")

    def test_every_entered_scene_is_in_the_script(self):
        """A stop that hosts a scene step is no quiet visit: the budget's trim never drops it."""
        c = lp.load_content(FOLDER, "tutorial_b")
        p = lp.make_plan(c, d_lobby=True, scenes=True)
        h = lp.script_h(p, None)
        for sc in (sc for scs in p.scene_at.values() for sc in scs):
            self.assertIn(f"log LEVELPASS scene {lp._safe(sc.name)} enter {sc.starter.ref}", h)
        self.assertEqual(len([sc for sc in p.scene_list if sc.entered]), 3)


class ExpectedSceneNotes(unittest.TestCase):
    def test_both_one_and_neither(self):
        h = lc.Log("H", "H/game.log", [
            "[2026-11-19 00:10:00.000] [info] [core] [t1] Input script: LEVELPASS scenes expected: A@load, "
            "B@trigger+entered, C@fight, D@trigger+entered"])
        row = lambda name, hh, dd: type("R", (), {"key": (2, name, 0), "h": hh, "d": dd})  # noqa: E731
        rows = [row("A", 1, 1), row("run@B", 1, None)]
        notes = lc.expected_scene_notes(h, rows)
        self.assertIn("expected from the level data: 4 (H/game.log:1:", notes[0])
        self.assertIn("played on both clients 1, on one 1, on neither 2", notes[0])
        self.assertEqual(notes[1], "entered by the sweep but played on neither client: D")
        self.assertEqual(notes[2], "not reached (on neither client): C (fight)")
        self.assertEqual(lc.expected_scene_notes(lc.Log("H", "H/game.log", []), rows), [])


if __name__ == "__main__":
    unittest.main()
