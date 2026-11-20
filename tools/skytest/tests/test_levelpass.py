"""Tests for the level pass's generator and report (levelpass.py) on made-up content: the manifest
readers, the classification, clustering and order, sampling and the budget, the generated scripts (no
fixed wait, every wait a condition with a timeout, unique audit labels), and the report's folding.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_levelpass.py"
"""
from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import levelpass as lp  # noqa: E402
import levelpass_holds as lh  # noqa: E402

# A runner config as a machine without [defaults] show_server_window has it (a visible run, server hidden unless the
# case says otherwise): what show_server_args reads.
SHOWN_DEFAULT_OFF = type("Cfg", (), {"bulk": None, "show_server_window": False})()

PROBE = """Level manifest: madeup (level request 2 in game.log)
Level start: Start_01 at 0.0 0.0 0.0

== checkpoints, level start and exit: 2
   classes: CPlayerStartEntity 1, CScriptTriggerEntity 1
       0        0.0       0.0      0.0  CPlayerStartEntity         Start_01                             [madeup#1] registered
    9000     9000.0       0.0      0.0  CScriptTriggerEntity       Level_Exit                           [madeup#2] registered

== enemies, spawners and encounters: 3
     500      500.0       0.0      0.0  CEncounterSpawnPoint       (no name)                            [madeup#3] registered
     600      600.0       0.0      0.0  CActionEntity              Fight_Manager                        [madeup#4] registered
    5000     5000.0       0.0      0.0  CGameEntity                Grub_Spawner_01                      [madeup#5] registered

== switches, gates, locks, pads, movers, breakables, turrets, mini-games: 6
     300      300.0     100.0      0.0  CPhysicalEntity            Box_Destructible_01                  [madeup#6] asleep
     310      310.0     100.0      0.0  CPhysicalEntity            Box_Destructible_02                  [madeup#7] asleep
     320      320.0     100.0      0.0  CPhysicalEntity            Box_Destructible_03                  [madeup#8] asleep
     330      330.0     100.0      0.0  CPhysicalEntity            Box_Destructible_04                  [madeup#9] asleep
    3000     3000.0       0.0      0.0  CGameEntity                Lever_A                              [madeup#10] registered
    3100     3100.0       0.0      0.0  CDynamicClipEntity          Lift_Platform                        [madeup#11] registered

== trigger volumes and level events: 3
    2000     2000.0       0.0      0.0  CScriptTriggerEntity       (no name)                            [madeup#12] registered
    2100     2100.0       0.0      0.0  CGameEntity                Event_StartFight                     [madeup#13] registered
  900000   900000.0       0.0      0.0  CPhysicalEntity            CrossBeam_Far                        [madeup#14] registered

== effects, sound and scenery: 1
     100      100.0       0.0      0.0  CGameEntity                vfx_Smoke                            [madeup#15] registered

== completeness
   total: 15 placed entities
"""


def content() -> lp.Content:
    c = lp.parse_probe_manifest(PROBE, "madeup")
    lp.attach_refs(c.objects, c.objects)  # the probe's own manifest: every object its authored id
    return c


class Readers(unittest.TestCase):
    def test_probe_manifest(self):
        c = content()
        self.assertEqual(c.start, (0.0, 0.0, 0.0))
        self.assertEqual(len(c.objects), 15)
        unnamed = [o for o in c.objects if o.ident == "madeup#3"][0]
        self.assertEqual(unnamed.name, "")
        self.assertEqual(unnamed.group, lp.GROUP_ENEMIES)
        box = [o for o in c.objects if o.name == "Box_Destructible_01"][0]
        self.assertEqual(box.pos, (300.0, 100.0, 0.0))
        self.assertEqual(box.state, "asleep")

    def test_static_manifest_lenient(self):
        data = {"start": {"x": 1, "y": 2, "z": 3},
                "groups": {lp.GROUP_INTERACT: [{"refname": "Lever_A", "class_name": "CGameEntity",
                                                "position": [10, 20, 30], "unit": "area1.mua"}]},
                "entities": [{"name": "Chest_1", "class": "CPhysicalEntity", "position": {"x": 5, "y": 6, "z": 7},
                              "group": lp.GROUP_PICKUPS},
                             {"name": "NoPos", "class": "C"}]}
        c = lp.parse_logic_manifest(data, "madeup")
        self.assertEqual(c.start, (1.0, 2.0, 3.0))
        self.assertEqual(sorted(o.name for o in c.objects), ["Chest_1", "Lever_A"])
        lever = [o for o in c.objects if o.name == "Lever_A"][0]
        self.assertEqual((lever.cls, lever.pos, lever.ident), ("CGameEntity", (10.0, 20.0, 30.0), "area1.mua"))

    def test_logic_edges_and_level_end(self):
        data = {"triggers": [{"name": "T1", "telegrams": [{"from": "T1", "message": "CEncounterToggleMessage",
                                                             "to": ["Fight_Manager"], "effect": "starts the encounter"}]}],
                "level_end": [{"name": "Fight_Manager"}, "Level_Exit"]}
        edges, end = lp.parse_logic(data)
        self.assertEqual([(e.sender, e.target, e.effect) for e in edges],
                         [("T1", "Fight_Manager", "starts the encounter")])
        self.assertEqual(end, ["Fight_Manager", "Level_Exit"])

    def test_chain_names(self):
        chains = ["(inferred) CS_Intro -[Scripts.CutscenePlayerMessage {MessageType: Done}]-> CS_Intro  =>  "
                  "CS_Intro -[Scripts.StringMessage {MessageString: Go}]-> EventMgr  =>  "
                  "EventMgr -[X after 0.1s]-> TP_Start  =>  TP_Start -[CLoadMapComponentMessage {_mapPath: a/b}]-> roles 4",
                  "{Sp_01, Sp_02, ?} -[Scripts.StringMessage {MessageString: ImDead}]-> End_Mgr  =>  End_Mgr [x11] -[Y]-> Outro"]
        self.assertEqual(lp.exit_senders(chains), ["CS_Intro", "EventMgr", "TP_Start", "Sp_01", "Sp_02", "End_Mgr",
                                                   "Outro"])

    def test_exit_one_step_back(self):
        """No chain member a player acts on, but a sender to its first member is (ww1's LevelOutro_Trigger ->
        CouncilEsmeralda): that sender is the exit, by its name when the probe never saw it, not a timed wait."""
        c = content()
        c.objects = [o for o in c.objects if o.name != "Level_Exit"]
        c.level_end = ["Event_StartFight"]
        lever = [o for o in c.objects if o.name == "Lever_A"][0]
        lever.ref = ""
        c.edges = [lp.Edge(sender="Lever_A", message="CFireBehaviorEventMessage", target="unit.Event_StartFight")]
        p = lp.make_plan(c, late_join=False)
        self.assertFalse(p.timed)
        self.assertEqual([(a.action, a.obj.name) for a in p.exit_stop.actions], [(lp.EXIT_A, "Lever_A")])
        self.assertEqual(lever.ref, "Lever_A")

    def test_timed_level(self):
        c = content()
        c.objects = [o for o in c.objects if o.name != "Level_Exit"]
        c.level_end = ["Event_StartFight"]  # a script event: nothing a player acts on
        p = lp.make_plan(c, late_join=True)
        self.assertTrue(p.timed)
        self.assertIsNone(p.exit_stop)
        h = lp.script_h(p, lp.LATE_JOIN_MARK)
        self.assertIn(f"waitfor online_level_after_mark {lp.TIMED_END_S + lp.NEXT_LOAD_S}", h)
        self.assertNotIn("online_level_end", h)  # the server's LevelEnd may never come (a held end): the next level
        self.assertLess(h.index("LEVELPASS late-join start"), h.index("LEVELPASS visit v1"))

    def test_load_content_prefers_static(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / "madeup-manifest.txt").write_text(PROBE, encoding="utf-8")
            self.assertEqual(lp.load_content(folder, "madeup").source, str(folder / "madeup-manifest.txt"))
            (folder / "madeup").mkdir()
            (folder / "madeup" / "manifest.json").write_text(json.dumps({"entities": []}), encoding="utf-8")
            (folder / "madeup" / "logic.json").write_text(json.dumps({"e": [{"from": "a", "to": "b"}]}), encoding="utf-8")
            c = lp.load_content(folder, "madeup")
            self.assertEqual(c.source, str(folder / "madeup" / "manifest.json"))
            self.assertEqual(len(c.edges), 1)
            with self.assertRaises(ValueError):
                lp.load_content(folder, "nolevel")


class Classify(unittest.TestCase):
    def obj(self, name, group, cls="CGameEntity"):
        return lp.Obj(name=name, cls=cls, pos=(1.0, 1.0, 1.0), group=group)

    def test_actions(self):
        self.assertEqual(lp.classify(self.obj("Box_Destructible_01", lp.GROUP_INTERACT))[0], lp.ATTACK)
        self.assertEqual(lp.classify(self.obj("Lever_A", lp.GROUP_INTERACT))[0], lp.INTERACT_A)
        self.assertEqual(lp.classify(self.obj("Lift_Platform", lp.GROUP_INTERACT))[0], lp.VISIT)
        self.assertEqual(lp.classify(self.obj("", lp.GROUP_INTERACT))[0], lp.VISIT)
        self.assertEqual(lp.classify(self.obj("Coin_1", lp.GROUP_PICKUPS))[0], lp.PICKUP)
        self.assertEqual(lp.classify(self.obj("Treasure_Chest", lp.GROUP_PICKUPS))[0], lp.INTERACT_A)
        self.assertEqual(lp.classify(self.obj("", lp.GROUP_ENEMIES))[0], lp.FIGHT)
        self.assertEqual(lp.classify(self.obj("Tessa", lp.GROUP_NPCS, "CActor"))[0], lp.TALK)
        self.assertEqual(lp.classify(self.obj("VO_Intro", lp.GROUP_NPCS, "CPhysicalEntity"))[0], lp.ENTER)
        self.assertEqual(lp.classify(self.obj("Ship_Model", lp.GROUP_NPCS, "CPhysicalEntity"))[0], lp.VISIT)
        self.assertEqual(lp.classify(self.obj("", lp.GROUP_TRIGGERS, "CScriptTriggerEntity"))[0], lp.ENTER)
        self.assertEqual(lp.classify(self.obj("Event_StartFight", lp.GROUP_TRIGGERS))[0], lp.VISIT)
        self.assertEqual(lp.classify(self.obj("KillZ_01", lp.GROUP_TRIGGERS, "CScriptTriggerEntity"))[0], lp.VISIT)
        self.assertEqual(lp.classify(self.obj("Player_Respawn_01", lp.GROUP_CHECKPOINTS))[0], lp.VISIT)
        self.assertEqual(lp.classify(self.obj("Checkpoint_02", lp.GROUP_CHECKPOINTS))[0], lp.ENTER)
        self.assertEqual(lp.classify(self.obj("Level_Exit", lp.GROUP_CHECKPOINTS))[0], lp.EXIT_A)
        self.assertEqual(lp.classify(self.obj("EndEncounter_Manager", lp.GROUP_ENEMIES))[0], lp.EXIT_A)

    def static(self, name, state, components, cls="CGameEntity", box=None):
        o = lp.Obj(name=name, cls=cls, pos=(1.0, 1.0, 1.0), group=lp.STATIC_GROUPS.get(state, ""), ident="u1",
                   box=box)
        o.state = state
        o.components = list(components)
        return o

    def test_actions_from_components(self):
        """The static reader's components choose the action, not the name or the reader group."""
        # Nothing a player presses: a chest spawner, a battle gate, a caged NPC (the 20261118 passes).
        for name, state, comps in (
                ("MagicItem_HiddenTreasure_ChestSpawner", "chest", ["Scripts.MagicItem_HiddenTreasure_ChestComponentData"]),
                ("TutorialBattleGate_01", "lock", ["CMessengerComponentData", "Scripts.LockedObjectComponentData"]),
                ("TrappingPit_FoxenMale", "npc", ["CBehaviorComponentData", "CConversationComponentData"]),
                ("RidingBird", "npc", ["CBehaviorComponentData", "Scripts.SpawnerComponentData"])):
            act, why = lp.classify(self.static(name, state, comps, "CActor" if state == "npc" else "CGameEntity"))
            self.assertEqual(act, lp.VISIT, name)
            self.assertIn("nothing a player acts on directly", why)
        # An interaction: pressed; on an NPC, talked to.
        self.assertEqual(lp.classify(self.static("TreasureChest_3", "chest", ["CInteractionComponentData"]))[0],
                         lp.INTERACT_A)
        self.assertEqual(lp.classify(self.static("Tessa", "npc", ["CInteractionComponentData", "CConversationComponentData"],
                                                 "CActor"))[0], lp.TALK)
        # The interaction wins over a destructible (a cage with a press); a destructible alone is attacked,
        # whatever its reader group (a sheep cage that spawns its sheep).
        self.assertEqual(lp.classify(self.static("BigBait_Cage", "switch", ["CInteractionComponentData",
                                                                            "Scripts.DestructibleComponentData"]))[0],
                         lp.INTERACT_A)
        self.assertEqual(lp.classify(self.static("SavedSheep_Cage002", "spawner", ["Scripts.DestructibleComponentData",
                                                                                   "Scripts.SpawnerComponentData"]))[0],
                         lp.ATTACK)
        # A trigger volume component: entered, with no box of its own (its live origin).
        self.assertEqual(lp.classify(self.static("GillmanConvo", "npc", ["CTriggerVolumeSphereComponentData",
                                                                         "Scripts.InteractionSequenceComponentData"],
                                                 "CActor"))[0], lp.ENTER)
        # A group the pass only visits is not made an action by its components (a teleporter's trigger volume).
        self.assertEqual(lp.classify(self.static("Cave03_TeleToInt", "teleporter", ["Scripts.TeleporterComponentData",
                                                                                "CTriggerVolumeBoxComponentData"]))[0], lp.VISIT)
        # A mini-game with an interaction (binoculars, a winch): never pressed.
        self.assertEqual(lp.classify(self.static("UndeadZone_Binoculars", "minigame", ["CInteractionComponentData",
                                                                                  "Scripts.BinocularMinigameComponentData"]))[0], lp.VISIT)
        self.assertEqual(lp.classify(self.static("TreasureChest_3", "chest", ["CInteractionComponentData", "Scripts.MicrogameComponentData"]))[0],
                         lp.INTERACT_A)  # a chest's own microgame: pressed
        # A collectible: picked up; enemies and spawners: fought as before.
        self.assertEqual(lp.classify(self.static("StoryScroll", "collectible", ["Scripts.CollectibleComponentData"]))[0],
                         lp.PICKUP)
        self.assertEqual(lp.classify(self.static("AttackJunk_Spawner", "spawner", ["Scripts.SpawnerComponentData"]))[0],
                         lp.FIGHT)
        self.assertEqual(lp.classify(self.static("Chompy", "enemy", ["CSwarmerAIComponentData"], "CActor"))[0], lp.FIGHT)

    def test_debug_and_trigger_point(self):
        self.assertEqual(lp.classify(self.obj("Debug_ResetSwitch", lp.GROUP_INTERACT))[0], lp.VISIT)
        inside = lp.Obj("T", "CScriptTriggerEntity", (0.0, 0.0, 0.0), lp.GROUP_TRIGGERS,
                        box=((-10.0, -10.0, -10.0), (10.0, 10.0, 10.0)))
        self.assertEqual(inside.point(), (0.0, 0.0, 0.0))
        outside = lp.Obj("T", "CScriptTriggerEntity", (100.0, 0.0, 0.0), lp.GROUP_TRIGGERS,
                         box=((-10.0, -10.0, 0.0), (10.0, 10.0, 20.0)))
        self.assertEqual(outside.point(), (0.0, 0.0, 10.0))

    def test_stem(self):
        self.assertEqual(lp.stem("TA_Ship_Destructible_04"), "TA_Ship_Destructible")
        self.assertEqual(lp.stem("Lever_A"), "Lever_A")
        self.assertEqual(lp.stem(""), "")


class Order(unittest.TestCase):
    def test_cluster_greedy_and_deterministic(self):
        objs = [lp.Obj("a", "C", (0.0, 0.0, 0.0), lp.GROUP_INTERACT), lp.Obj("b", "C", (1000.0, 0.0, 0.0), lp.GROUP_INTERACT),
                lp.Obj("c", "C", (2100.0, 0.0, 0.0), lp.GROUP_INTERACT), lp.Obj("d", "C", (2200.0, 0.0, 0.0), lp.GROUP_INTERACT)]
        cs = lp.cluster(objs, radius=1200)
        self.assertEqual([[o.name for o in c.objects] for c in cs], [["a", "b"], ["c", "d"]])
        self.assertEqual(cs[1].center, (2100.0, 0.0, 0.0))  # a placed object's point, never a mean
        self.assertEqual([[o.name for o in c.objects] for c in lp.cluster(objs, radius=1200)],
                         [[o.name for o in c.objects] for c in cs])

    def test_tour_nearest_and_exit_last(self):
        cs = [lp.Cluster(0, (5000.0, 0.0, 0.0), [lp.Obj("Level_Exit", "C", (5000.0, 0.0, 0.0), lp.GROUP_CHECKPOINTS)]),
              lp.Cluster(1, (3000.0, 0.0, 0.0), [lp.Obj("x", "C", (3000.0, 0.0, 0.0), lp.GROUP_INTERACT)]),
              lp.Cluster(2, (1000.0, 0.0, 0.0), [lp.Obj("y", "C", (1000.0, 0.0, 0.0), lp.GROUP_INTERACT)])]
        self.assertEqual([c.index for c in lp.tour(cs, (0.0, 0.0, 0.0))], [2, 1, 0])
        # The logic's order wins over distance (a trigger before what it starts); the exit stays last.
        self.assertEqual([c.index for c in lp.tour(cs, (0.0, 0.0, 0.0), first=["x", "y"])], [1, 2, 0])

    def test_logic_order(self):
        edges = [lp.Edge("T1", "m", "Mgr"), lp.Edge("Mgr", "m", "Gate"), lp.Edge("A", "m", "B"), lp.Edge("B", "m", "A")]
        order = lp.logic_order(edges)
        self.assertLess(order.index("T1"), order.index("Mgr"))
        self.assertLess(order.index("Mgr"), order.index("Gate"))
        self.assertEqual(sorted(order), sorted({"T1", "Mgr", "Gate", "A", "B"}))  # a cycle still ends


class Plans(unittest.TestCase):
    def test_sampling_outliers_and_exit(self):
        p = lp.make_plan(content())
        acted = [(a.action, a.obj.name) for s in p.stops for a in s.actions]
        boxes = [n for act, n in acted if act == lp.ATTACK]
        self.assertEqual(boxes, ["Box_Destructible_01", "Box_Destructible_02", "Box_Destructible_03"])
        self.assertIn("attack Box_Destructible: 1 more not acted on (sampled)", p.left_out)
        self.assertEqual(p.unplaced, 1)  # CrossBeam_Far
        self.assertIsNotNone(p.exit_stop)
        self.assertIs(p.stops[-1], p.exit_stop)
        self.assertTrue(any(k.startswith("Lift_Platform") for k in p.no_action))
        full = lp.make_plan(content(), full=True)
        self.assertEqual(len([a for s in full.stops for a in s.actions if a.action == lp.ATTACK]), 4)

    def test_one_fight_per_cluster(self):
        p = lp.make_plan(content())
        for s in p.stops:
            self.assertLessEqual(len([a for a in s.actions if a.action == lp.FIGHT]), 1)

    def test_budget_drops_quiet_visits(self):
        objs = [lp.Obj(f"Lift_{i}", "C", (i * 5000.0, 0.0, 0.0), lp.GROUP_INTERACT) for i in range(1, 40)]
        c = lp.Content("madeup", "made", (0.0, 0.0, 0.0), objs)
        # No exit: the estimate keeps only the sweep's margin for it (as the sweep's deadline does), and the
        # sweep's measured spread inside the deadline (SWEEP_SPREAD_S: ww1's 175 s sweep had none left).
        budget = lp.BOOT_S + lp.LEVEL_LOAD_S + lp.SWEEP_MARGIN_S + lp.SWEEP_SPREAD_S + 10 * lp.VISIT_S
        p = lp.make_plan(c, late_join=False, budget_s=budget)
        self.assertLessEqual(p.estimate_s, budget)
        self.assertEqual(len(p.stops), 10)
        self.assertTrue(any("over the budget" in x for x in p.left_out))
        # The nearest stay.
        self.assertEqual(p.stops[0].cluster.center, (5000.0, 0.0, 0.0))

    def test_the_sweep_keeps_its_spread_free(self):
        """One visit fewer when the stops would fill the sweep's deadline to the second (ww1, 19 November)."""
        objs = [lp.Obj(f"Lift_{i}", "C", (i * 5000.0, 0.0, 0.0), lp.GROUP_INTERACT) for i in range(1, 40)]
        c = lp.Content("madeup", "made", (0.0, 0.0, 0.0), objs)
        budget = lp.BOOT_S + lp.LEVEL_LOAD_S + lp.SWEEP_MARGIN_S + 10 * lp.VISIT_S
        p = lp.make_plan(c, late_join=False, budget_s=budget)
        self.assertEqual(len(p.stops), 10 - math.ceil(lp.SWEEP_SPREAD_S / lp.VISIT_S))

    def test_budget_caps_each_kind(self):
        """One per family still over the budget: at most a few actions of each kind, the first along the
        path, and the plan says so; --full keeps them all."""
        objs = []
        for i in range(1, 21):
            o = lp.Obj(f"Lever{chr(64 + i)}", "C", (i * 5000.0, 0.0, 0.0), lp.GROUP_INTERACT, ident=f"madeup#{i}")
            o.ref = f"auth:madeup:{i}"
            objs.append(o)
        c = lp.Content("madeup", "made", (0.0, 0.0, 0.0), objs)
        budget = lp.BOOT_S + lp.LEVEL_LOAD_S + lp.EXIT_S + 8 * (lp.VISIT_S + lp.ACTION_EXPECT[lp.INTERACT_A] +
                                                                 lp.ACTION_MOVE_S + lp.AUDIT_S)
        p = lp.make_plan(c, late_join=False, budget_s=budget)
        acts = [a for s in p.stops for a in s.actions]
        self.assertLessEqual(p.estimate_s, budget)
        self.assertTrue(0 < len(acts) < 20)
        self.assertTrue(any("each kind of action at most" in x for x in p.left_out))
        self.assertEqual(acts[0].obj.name, "LeverA")  # the first along the path stay
        full = lp.make_plan(c, full=True, late_join=False, budget_s=budget)
        self.assertEqual(sum(len(s.actions) for s in full.stops), 20)

    def test_expected_effects_from_logic(self):
        c = content()
        c.edges = [lp.Edge("Lever_A", "StringMessage", "Lift_Platform", "moves")]
        p = lp.make_plan(c)
        lever = [a for s in p.stops for a in s.actions if a.obj.name == "Lever_A"][0]
        self.assertEqual(lever.expect, ["StringMessage -> Lift_Platform (moves)"])


UNREGISTERED = """
== pickups and collectibles: 3
    4000     4000.0       0.0      0.0  CPhysicalEntity            Gem_01                               [madeup#16] skipped act0
    4010     4010.0       0.0      0.0  CPhysicalEntity            Gem_02                               [madeup#17] registered
    7000     7000.0       0.0      0.0  CPhysicalEntity            Scroll_01                            [madeup#18] skipped

== completeness
"""


def unregistered_content(exit_state: str = "registered") -> lp.Content:
    text = PROBE.replace("\n== completeness\n", UNREGISTERED).replace(
        "[madeup#2] registered", f"[madeup#2] {exit_state}")
    c = lp.parse_probe_manifest(text, "madeup")
    lp.attach_refs(c.objects, c.objects)
    return c


class NotRegistered(unittest.TestCase):
    """A generated case passes its own preflight: no sweep step addresses an object the probe recorded as not
    registered at the load (an auth: error in preflight.py), or a name whose objects all sleep with no earlier
    step near; such steps are left out and named; the exit's never are."""

    def test_state_word(self):
        self.assertEqual(lp.state_word("skipped act0"), "skipped")
        self.assertEqual(lp.state_word(""), "")
        c = unregistered_content()
        gem = next(o for o in c.objects if o.name == "Gem_01")
        self.assertEqual((gem.ref, gem.live_state), ("auth:madeup:16", "skipped"))

    def test_sample_goes_to_a_registered_sibling(self):
        p = lp.make_plan(unregistered_content(), sample=1)
        picked = [a.obj.name for s in p.stops for a in s.actions if a.action == lp.PICKUP]
        self.assertEqual(picked, ["Gem_02"])
        line = next(x for x in p.left_out if lp.NOT_REGISTERED in x)
        self.assertIn("pickup Gem_01 madeup#16", line)
        for script in (lp.script_h(p, None), lp.script_d(p)):
            self.assertNotIn("auth:madeup:16", script)
            self.assertNotIn("auth:madeup:18", script)

    def test_lost_visit_is_named_and_the_exit_kept(self):
        p = lp.make_plan(unregistered_content(exit_state="skipped"), sample=1)
        line = next(x for x in p.left_out if lp.NOT_REGISTERED in x)
        scroll = next(s for s in p.stops if any(o.name == "Scroll_01" for o in s.cluster.objects))
        self.assertIn(f"visit {scroll.label}", line)
        self.assertEqual(lp.visit_ref(scroll), "")
        # The exit is never left out: its failing target is the preflight's error, the pass does not start.
        self.assertIn("madeup:2", "\n".join(a.obj.ref for a in p.exit_stop.actions))

    def test_name_asleep_needs_an_earlier_step_near(self):
        def stop(label, x, state):
            o = lp.Obj(f"Thing_{label}", "C", (x, 0.0, 0.0), lp.GROUP_INTERACT, ref=f"Thing_{label}")
            o.live_state = state
            return lp.Stop(label=label, cluster=lp.Cluster(0, o.pos, [o]), actions=[], visit_only=[])
        stops = [stop("v1", 0.0, "registered"), stop("v2", lp.STREAM_NEAR - 100.0, "asleep"),
                 stop("v3", 50000.0, "asleep")]
        out = lp.drop_unreachable(stops, None, {})
        self.assertEqual([t for _, t in out], ["visit v3"])
        self.assertEqual([lp.visit_ref(s) for s in stops], ["Thing_v1", "Thing_v2", ""])


class Scripts(unittest.TestCase):
    def check_script(self, text: str):
        for line in text.splitlines():
            self.assertFalse(re.match(r"^wait\s+\d", line), f"a fixed wait: {line}")
            self.assertNotIn(";", line)
            if line.startswith("waitfor "):
                self.assertRegex(line, r"^waitfor \S+ \d+$")
            if line.startswith(("wait_free", "online_wait_peers", "online_wait_peer_near")):
                self.assertRegex(line, r"\d+$")
        self.assertEqual(lp.position_literals(text), [], "a level-pass script names objects, never positions")
        labels = [l.split()[1] for l in text.splitlines() if l.startswith("online_audit ")]
        self.assertEqual(len(labels), len(set(labels)), "audit labels are unique")

    def test_h_and_d(self):
        p = lp.make_plan(content())
        h = lp.script_h(p, late_join_after="LEVELPASS late-join start")
        self.check_script(h)
        self.assertIn("online_host -1", h)  # the party boot: S waits for the owner's own pick
        self.assertNotIn("online_join_pending", h)  # D joins late here: no lobby wait
        self.assertIn("level madeup", h)
        self.assertIn("log LEVELPASS late-join start", h)
        self.assertIn("waitfor online_peer_was_in 300", h)  # D may have come and gone already
        self.assertIn(f"waitfor online_level_after_mark {int(lp.EXIT_WAIT_S) + lp.NEXT_LOAD_S}", h)
        # The level under test is marked once it is up (the exit's wait is for a later one), on H and D.
        self.assertLess(h.index("online_mark_level", h.index("level madeup")), h.index("LEVELPASS phase sweep"))
        self.assertLess(h.index("LEVELPASS phase late-join"), h.index("LEVELPASS phase exit"))
        self.assertIn("attack_until auth:madeup:6 12", h)
        self.assertIn("interact auth:madeup:10 10", h)
        self.assertIn("goto_entity auth:madeup:", h)
        d = lp.script_d(p)
        self.check_script(d)
        self.assertIn("online_join {join}", d)
        self.assertNotIn("online_host", d)
        self.assertIn("goto_entity auth:madeup:", d)
        self.assertLess(d.index("online_end"), d.index("quit"))
        # By default D stays through the level end and reaches the next level itself; H waits for D in the
        # session before its exit and for D gone only after the next level.
        self.assertTrue(lp.d_through_end(p))
        self.assertLess(h.index("online_wait_peers 2 120"), h.index("LEVELPASS phase exit"))
        self.assertNotIn("waitfor online_alone 300", h)
        self.assertLess(h.index("online_audit next"), h.index(f"waitfor online_alone {lp.D_ALONE_WAIT_S}"))
        self.assertLess(d.index("online_mark_level"), d.index("LEVELPASS D in"))
        for line in (f"waitfor online_level_after_mark {lp.D_LEVEL_END_WAIT_S}",
                     "log LEVELPASS D next level", "online_audit d-next"):
            self.assertIn(line, d)
        self.assertLess(d.index("log LEVELPASS D next level"), d.index("online_end"))
        self.assertIn(f"wait_needs = {lp.D_WAIT_NEEDS}", lp.spec_toml(p))
        # --d-leaves-before-end: as before (D gone before the exit, nothing of the end on D).
        old = lp.make_plan(content(), d_stays=False)
        ho, do = lp.script_h(old, lp.LATE_JOIN_MARK), lp.script_d(old)
        self.assertLess(ho.index("waitfor online_alone 300"), ho.index("LEVELPASS phase exit"))
        self.assertNotIn("online_wait_peers 2", ho)
        self.assertNotIn("online_level_after_mark", do)
        self.assertNotIn("wait_needs", lp.spec_toml(old))
        # --no-end: no end to reach, D leaves as before.
        self.assertFalse(lp.d_through_end(lp.make_plan(content(), no_end=True)))

    def test_sweep_deadline(self):
        p = lp.make_plan(content())
        h = lp.script_h(p, lp.LATE_JOIN_MARK)
        share = lp.sweep_share(p)
        self.assertIn(f"sweep_deadline {share:.0f}", h)
        self.assertLess(h.index(f"sweep_deadline {share:.0f}"), h.index("LEVELPASS visit v1"))
        self.assertLess(h.index("sweep_deadline 0"), h.index("LEVELPASS phase late-join"))
        self.assertGreaterEqual(share, lp.SWEEP_MIN_S)
        # One hold or fall ends a step after HOLD_CAP_S on H and D, from the sweep to the end (not only the sweep).
        self.assertLess(h.index(f"hold_cap {lp.HOLD_CAP_S}"), h.index("LEVELPASS visit v1"))
        self.assertNotIn("hold_cap 0", h)
        self.assertIn(f"hold_cap {lp.HOLD_CAP_S}", lp.script_d(p))
        self.assertLessEqual(share + lp.BOOT_S + lp.LEVEL_LOAD_S + lp.LATE_JOIN_S + lp.exit_seconds(p), lp.BUDGET_S)

    def test_no_exit_no_exit_waits(self):
        c = content()
        c.objects = [o for o in c.objects if o.name != "Level_Exit"]
        p = lp.make_plan(c, late_join=False)
        h = lp.script_h(p, late_join_after=None)
        self.check_script(h)
        self.assertNotIn("online_level_after_mark", h.split("online_mark_level")[-1])
        self.assertIn("LEVELPASS exit none", h)
        self.assertNotIn("online_wait_peers", h)

    def test_no_end_stops_after_the_last_mid_level_step(self):
        """--no-end: the exit is found and named but neither acted on nor waited for; its own case; the
        estimate and the sweep's share have no exit time; the report says the end was not attempted."""
        full, p = lp.make_plan(content()), lp.make_plan(content(), no_end=True)
        self.assertTrue(p.no_end)
        self.assertIsNotNone(p.exit_stop)  # found, named in the line below
        h = lp.script_h(p, lp.LATE_JOIN_MARK)
        self.check_script(h)
        for words in ("online_level_end", "online_past_level_end", f"waitfor online_level_after_mark {int(lp.EXIT_WAIT_S) + lp.NEXT_LOAD_S}",
                      "online_audit next", "LEVELPASS phase exit",
                      "enter_trigger auth:madeup:2"):
            self.assertNotIn(words, h)
        self.assertIn("LEVELPASS exit not attempted (--no-end)", h)
        self.assertIn("Level_Exit", h.split("exit not attempted", 1)[1].splitlines()[0])
        # The sweep, its final audit and the late join's audit all still run, in order, before the end line.
        for words in ("LEVELPASS phase audit", "online_audit end", "waitfor online_alone 300", "online_audit late"):
            self.assertLess(h.index(words), h.index("exit not attempted"))
        self.assertTrue(h.rstrip().endswith("LEVELPASS done\nonline_end\nquit"))
        self.assertEqual(lp.exit_seconds(p), 0.0)
        self.assertGreater(lp.sweep_share(p), lp.sweep_share(full))
        self.assertLess(p.estimate_s, full.estimate_s)
        self.assertEqual(lp.case_name("madeup", True), "levelpass-madeup-no-end")
        self.assertIn('name = "levelpass-madeup-no-end"', lp.spec_toml(p))
        self.assertIn('name = "levelpass-madeup"', lp.spec_toml(full))
        report = lp.report_text("madeup", p, 100.0, [], [], "", {}, "run", [])
        self.assertIn("level end: NOT ATTEMPTED (--no-end)", report)
        self.assertIn("level end: attempted", lp.report_text("madeup", full, 100.0, [], [], "", {}, "run", []))
        # A timed level does not wait for its own end either.
        c = content()
        c.objects = [o for o in c.objects if o.name != "Level_Exit"]
        c.level_end = ["Event_StartFight"]
        t = lp.make_plan(c, no_end=True)
        self.assertTrue(t.timed)
        ht = lp.script_h(t, lp.LATE_JOIN_MARK)
        self.assertNotIn(f"waitfor online_level_after_mark {lp.TIMED_END_S + lp.NEXT_LOAD_S}", ht)
        self.assertIn("the level ends on its own", ht)

    def test_no_probe(self):
        """--no-probe: no auth: ids in the steps and no probe setting on any game."""
        c = lp.parse_probe_manifest(PROBE, "madeup")
        lp.attach_refs(c.objects, c.objects, names_only=True)
        self.assertFalse(any(o.ref.startswith("auth:") for o in c.objects))
        p = lp.make_plan(c)
        p.probe = False
        text = lp.spec_toml(p)
        self.assertNotIn("dev_authored_probe", text)
        self.assertNotIn("auth:", text)
        self.assertIn("dev_authored_probe", lp.spec_toml(lp.make_plan(content())))

    def test_names_are_safe(self):
        o = lp.Obj("Bad;Name#1", "C", (1.0, 2.0, 3.0), lp.GROUP_INTERACT)
        o.ref = lp.name_ref(o.name)
        lines = lp.action_lines(lp.Action(lp.INTERACT_A, o, "x"), "v1_1")
        self.assertTrue(all(";" not in l and "#" not in l for l in lines))


class Refs(unittest.TestCase):
    def test_join_and_fallback(self):
        probe = [lp.Obj("Lever_A", "CGameEntity", (10.0, 0.0, 0.0), lp.GROUP_INTERACT, ident="zone_x#12"),
                 lp.Obj("", "CEncounterSpawnPoint", (500.0, 0.0, 0.0), lp.GROUP_ENEMIES, ident="zone_x#40")]
        objs = [lp.Obj("Lever_A", "CGameEntity", (11.0, 1.0, 0.0), lp.GROUP_INTERACT),
                lp.Obj("", "CEncounterSpawnPoint", (500.5, 0.0, 0.0), lp.GROUP_ENEMIES),
                lp.Obj("Chest_9", "CPhysicalEntity", (900.0, 0.0, 0.0), lp.GROUP_PICKUPS),
                lp.Obj("Lever_A", "CGameEntity", (5000.0, 0.0, 0.0), lp.GROUP_INTERACT),
                lp.Obj("X_componentDataOverrides_archetype_Y", "C", (1.0, 1.0, 1.0), lp.GROUP_ENEMIES),
                lp.Obj("", "CGameEntity", (2.0, 2.0, 2.0), lp.GROUP_OTHER)]
        self.assertEqual(lp.attach_refs(objs, probe), 2)
        # Chest_9 is not a name the probe saw live: no reference; Lever_A is (its other copy).
        self.assertEqual([o.ref for o in objs], ["auth:zone_x:12", "auth:zone_x:40", "", "Lever_A", "", ""])
        lp.attach_refs(objs, None)  # no probe: names as they are
        self.assertEqual(objs[2].ref, "Chest_9")

    def test_position_literals(self):
        self.assertEqual(lp.position_literals("\n".join(["goto_entity auth:madeup:1", "fight_near Lever_A 1500 25",
                                                         "wait_free 30"])), [])
        for bad in ("goto 1.0 2.0 3.0", "fight_area 1 2 3 1500 25", "attack_until at:1,2,3 10",
                    "log LEVELPASS visit at 12.5 -3.0 7", "online_wait_peer_near 1 2 3 400 300"):
            self.assertEqual(len(lp.position_literals(bad)), 1, bad)


class Report(unittest.TestCase):
    LOG = "\n".join([
        "[2026-11-18 10:00:00.000] [info] Input script: playing 'x'",
        "[2026-11-18 10:01:30.000] [info] Input script: LEVELPASS phase sweep",
        "[2026-11-18 10:01:31.000] [info] Online: audit diff p1 v1_2 breakable Au:5 slot 0 rev 2 differs at 1,2,3 local Box_Destructible_01#44 row 00 live 01 why value",
        "[2026-11-18 10:01:31.000] [info] Online: audit diff p1 v2_1 breakable Au:6 slot 0 rev 2 differs at 1,2,4 local Box_Destructible_02#45 row 00 live 01 why value",
        "[2026-11-18 10:01:32.000] [info] Online: audit diff p1 v3 gate Au:7 slot 0 rev 1 no_object at - local - row - live - why unresolved",
        "[2026-11-18 10:01:33.000] [info] Online: audit norow p1 v3 mover Lift_Platform#9 at 3100,0,0",
        "[2026-11-18 10:01:34.000] [info] Dev: interact Lever_A failed (out of the prompt's range (may press stays 0))",
        "[2026-11-18 10:01:35.000] [info] Input script: waitfor goto_done timed out after 10.0 s",
        "[2026-11-18 10:01:36.000] [info] Online: audit summary p1 v3 rows 9 differences 2 in_flight 0 pending 0 unsupported 0 not_audited 3 no_row 1 kinds gate=1/1/0/0",
        "[2026-11-18 10:03:00.000] [info] Input script: LEVELPASS phase exit",
        "[2026-11-18 10:03:30.000] [info] Input script: LEVELPASS done",
    ])

    def test_findings_and_fold(self):
        labels = {"v1_2": "act v1_2 attack Box_Destructible_01", "v2_1": "act v2_1 attack Box_Destructible_02"}
        fs = lp.findings_from_log("H", self.LOG, labels)
        sev = sorted(f.severity for f in fs)
        self.assertEqual(sev, sorted(["difference after an action", "difference after an action", "row without an object",
                                      "object without a row", "action failed", "step failed"]))
        folded = lp.fold(fs)
        first, n, _ = folded[0]
        self.assertEqual((first.severity, first.kind, first.family, n), ("difference after an action", "breakable",
                                                                         "Box_Destructible", 2))
        self.assertEqual(first.server, "00")
        self.assertEqual(first.client, "01")
        self.assertIn("Box_Destructible_01", first.step)
        # Folding is stable: the same findings in another order fold the same.
        again = lp.fold(list(reversed(fs)))
        self.assertEqual([(f.severity, f.kind, f.family, f.what, k) for f, k, _ in again],
                         [(f.severity, f.kind, f.family, f.what, k) for f, k, _ in folded])

    def test_phase_times_and_summaries(self):
        phases = lp.phase_times(self.LOG)
        self.assertEqual(phases, [("boot and level load", 90.0), ("sweep", 90.0), ("exit", 30.0)])
        self.assertEqual(len(lp.summaries(self.LOG)), 1)

    def test_provoked(self):
        log = "\n".join([
            "x Online: audit diverge breakable Au:5 Box_Destructible_01#44: this client alone, S's row stays 00",
            "x Online: audit diff p1 provoke breakable Au:5 slot 0 rev 2 differs at 1,2,3 local Box_Destructible_01#44"
            " row 00 live 02 why value",
            "x Online: audit diff p1 v2 breakable Au:6 slot 0 rev 2 differs at 1,2,4 local Box_Destructible_02#45 row 00"
            " live 01 why value"])
        fs = lp.findings_from_log("H", log, {})
        self.assertEqual(lp.mark_provoked(fs, lp.provoked(log)), 1)
        self.assertEqual(sorted(f.severity for f in fs), ["difference at rest", "provoked difference (expected)"])
        self.assertEqual(lp.fold(fs)[0][0].severity, "provoked difference (expected)")
        p = lp.make_plan(content(), late_join=False, provoke="breakable")
        h = lp.script_h(p, None)
        self.assertLess(h.index("online_audit v1\n"), h.index("dev_audit_diverge breakable"))
        self.assertLess(h.index("dev_audit_diverge breakable"), h.index("online_audit provoke"))

    def test_boot_phases(self):
        log = "\n".join([
            "[2026-11-18 10:00:00.000] [info] Input script: playing 'x'",
            "[2026-11-18 10:00:12.000] [info] Input script: waitfor title_ready met after 11.5 s",
            "[2026-11-18 10:01:12.000] [info] Input script: waitfor online_server_hub met after 40.2 s",
            "[2026-11-18 10:01:20.000] [info] Dev: wait_free free after 0.3 s (held: no)",
            "[2026-11-18 10:01:21.000] [info] Input script: LEVELPASS phase sweep",
            "[2026-11-18 10:01:30.000] [info] Input script: waitfor goto_done met after 1.0 s"])
        self.assertEqual(lp.boot_phases(log), [("title_ready", 11.5), ("online_server_hub", 40.2), ("wait_free", 0.3)])

    def test_name_ids(self):
        self.assertEqual(lp.name_hash(""), 2166136261)
        self.assertEqual(f"{lp.name_hash('a'):08x}", "e40c292c")  # FNV-1a 32 of "a"
        objs = [lp.Obj("Lever_A", "C", (0.0, 0.0, 0.0), lp.GROUP_INTERACT)]
        h = f"{lp.name_hash('Lever_A'):08x}"
        f = lp.Finding(severity="row without an object", kind="gate", family=f"a00000010.{h}.12345678@2", what="x",
                       obj=f"- a00000010.{h}.12345678@2 at -", step="v1")
        lp.name_ids([f], lp.names_by_hash(objs))
        self.assertTrue(f.obj.endswith("(Lever_A)"))
        self.assertEqual(f.family, "Lever_A")

    def test_step_labels(self):
        p = lp.make_plan(content(), late_join=False)
        labels = lp.step_labels(lp.script_h(p, None))
        self.assertIn("v1", labels)
        self.assertTrue(all(isinstance(v, str) for v in labels.values()))

    def test_fold_ignores_values(self):
        """One object differing by other amounts at each audit is one entry."""
        f1 = lp.Finding(severity="difference at rest", kind="mover", family="EZ2_Skully", what="differs facing 0.1 0.0 1.4 vs -0.1 0 1.3",
                        obj="EZ2_Skully#1 a1 at 1,2,3", step="v1", peer="H/p1")
        f2 = lp.Finding(severity="difference at rest", kind="mover", family="EZ2_Skully", what="differs facing 0.1 0.0 1.4 vs 0.0 0.1 1.3",
                        obj="EZ2_Skully#1 a1 at 1,2,3", step="v2", peer="H/p1")
        folded = lp.fold([f1, f2])
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0][1], 1)

    def test_tool_kinds(self):
        """A step the tool could not carry out is its own kind; what may be the game's is not."""
        k = lp.tool_kind
        self.assertEqual(k("Dev: goto_entity auth:ww1:5 failed (teleport failed: landed 1,2,3, 287 under the target's "
                           "floor at 584.90 (more than 150: fell)) after 3.0 s"), "teleport: no good landing beside it")
        self.assertEqual(k("Dev: goto_entity auth:ww1:5 failed (teleport failed: held too long after the teleport) after "
                           "3 s"), "teleport: the game held the hero")
        self.assertIn("own event", k("Dev: goto_entity auth:ww1:5 failed (no entity: still asleep 4 s after the teleport to its "
                                     "stored position: asleep; its area streamed in (12 stream-ins, none for 1.5 s): it "
                                     "wakes only at its own event) after 5 s"))
        self.assertEqual(k("Dev: interact auth:ww1:7 failed (no entity: asleep (not in the entity table) at its stored "
                           "position 1,2,3) after 0.1 s"), "asleep: did not wake for the visit")
        self.assertEqual(k("Dev: goto_entity TA_Ship_Junk_1 failed (no entity: no such entity) after 0.1 s"),
                         "not in the live game by that name or id")
        self.assertEqual(k("Dev: interact Gate#3 failed (no CInteractionComponent on Gate#3 or within 200 of it, and no "
                           "CInteraction seen near it) after 2 s"), "no interaction on or near the object")
        self.assertEqual(k("Dev: fight_area 1,2,-3 failed (the hero is 37913 from the area, more than its radius 2500 + "
                           "1500: the route did not get here, nothing walked) after 0.0 s"), "could not get within reach")
        self.assertIn("no static floor", k("Dev: route teleport goto_entity failed (landed 1,2,3, 300 off the point 4,5,6 (more "
                                    "than 200); no static floor under it or beside it (0 no floor, 1 no floor)) (target X#1)"))
        # The game's (or not known to be the tool's): stays a finding.
        self.assertIn("no hit point", k("Dev: attack_until ShipJunk_2#7 failed (time: hp 50 -> 50, 9 bursts) after 30 s"))
        self.assertEqual(k("Dev: attack_until Crate#7 failed (time: hp 50 -> 20, 9 bursts) after 30 s"), "")
        self.assertEqual(k("Dev: interact Snagglescale#4 failed (the interaction went away after rt) after 3 s"), "")
        self.assertEqual(k("Input script: waitfor online_level_end timed out after 60 s"), "")

    def test_report_separates_tool(self):
        """The findings section is only about the game; the tool's failures have their own section."""
        p = lp.make_plan(content(), late_join=False)
        log = self.LOG + ("[2026-11-18 05:20:00.000] [info] [core] [t1] Dev: goto_entity auth:madeup:3 failed (teleport "
                          "failed: landed 1,2,3, 300 under the target's floor at 400 (more than 150: fell)) after 4 s\n")
        fs = lp.findings_from_log("H", log, {})
        self.assertTrue(any(f.severity == lp.TOOL and f.family == "goto_entity" for f in fs))
        text = lp.report_text("madeup", p, 420.0, lp.phase_times(log), lp.fold(fs), "", {"H": lp.summaries(log)},
                              "runfolder", [])
        head, tool = text.split("tool: could not act: ", 1)
        self.assertNotIn("[tool: could not act]", head)
        self.assertIn("[tool: could not act] tool goto_entity: teleport: no good landing beside it (x1)", tool)
        self.assertIn("[action failed]", head)
        self.assertTrue(tool.startswith("1 in 1 entries"))

    def test_audit_artefacts(self):
        """Lease kinds without a start, stale rows S also lost, and breakables S caught up with later are not
        per-object findings."""
        t = "[2026-11-18 03:35:{:02d}.000] [info] [core] [t1] "
        h = "\n".join([
            t.format(1) + "Online: audit norow p1 v1 container Hanging_Base#2606 at 1,2,3",
            t.format(2) + "Online: audit norow p1 v1 turret Turret#2063 at 1,2,3",
            t.format(3) + "Online: start ran start.personal Turret#2063 (lease 1)",
            t.format(4) + "Online: audit diff p1 v1 prop a0000082e.1.2@2 slot 0 rev 2 no_object at - local - row 010100 live - why gone",
            t.format(5) + "Online: audit diff p1 v1 prop a0000082f.1.2@2 slot 0 rev 2 no_object at - local - row 010100 live - why gone",
            t.format(6) + "Online: audit diff p1 v1 breakable a00000900.1.2@2 slot 0 rev 1 no_object at - local - row 00 live - why gone",
            t.format(7) + "Online: audit diff p1 v1 breakable a00000901.1.2@2 slot 0 rev 1 no_object at - local - row 00 live - why gone",
            t.format(8) + "Online: audit summary p1 v1 rows 9 differences 4 in_flight 0 pending 0 unsupported 0 not_audited 0 no_row 2 kinds prop=2/2/0/0",
            t.format(9) + "Online: audit diff p1 v2 breakable a00000901.1.2@2 slot 0 rev 1 no_object at - local - row 00 live - why gone",
        ]) + "\n"
        srv = t.format(8) + ("Online: audit summary S v1 rows 9 differences 0 in_flight 0 pending 2 unsupported 0 not_audited 0 "
                             "no_row 0 kinds prop=2/0/0/2 breakable=2/0/0/0") + "\n"
        texts = {"H": h, "S": srv}
        fs = lp.findings_from_log("H", h, {})
        out, notes = lp.audit_artefacts(fs, texts)
        sev = [f.severity for f in out]
        # (a) the container without a start goes; the turret a player started stays.
        self.assertEqual([f.obj.split("#")[0] for f in out if f.severity == "object without a row"], ["Turret"])
        # (b) the two props S lost too: one entry.
        self.assertEqual(sev.count("S keeps rows of removed entities"), 1)
        self.assertIn("(2 rows of prop)", [f for f in out if f.severity == "S keeps rows of removed entities"][0].what)
        # (c) breakable 900 settled at the later audit; 901 was reported again after it and stays.
        rows = {f.obj.split(" ")[1] for f in out if f.severity == "row without an object"}
        self.assertEqual(rows, {"a00000901.1.2@2"})
        self.assertTrue(any("1 breakable row(s)" in n for n in notes))
        self.assertTrue(any("lease-carried" in n for n in notes))

    def test_exit_not_reached(self):
        """A level end that did not come after the exit's action failed for the tool is the tool's."""
        t = "[2026-11-18 10:27:{:02d}.000] [info] [core] [t1] "
        h = "\n".join([
            t.format(1) + "Input script: LEVELPASS phase exit",
            t.format(2) + "Dev: fight_area 1,2,3 failed (the hero is 18372 from the area, more than its radius 2500 + 1500: "
                          "the route did not get here, nothing walked) after 0.0 s",
            t.format(9) + "Input script: waitfor online_level_end timed out after 60.0 s",
        ]) + "\n"
        fs = lp.exit_not_reached(lp.findings_from_log("H", h, {}), h)
        self.assertFalse(any(f.severity == "step failed" for f in fs))
        self.assertTrue(any(f.severity == lp.TOOL and f.what == lp.EXIT_NOT_ACTED for f in fs))
        # An exit whose action ran: the timeout stays a finding.
        h2 = t.format(1) + "Input script: LEVELPASS phase exit\n" + t.format(9) + \
            "Input script: waitfor online_level_end timed out after 60.0 s\n"
        self.assertTrue(any(f.severity == "step failed" for f in lp.exit_not_reached(lp.findings_from_log("H", h2, {}), h2)))

    def test_report_text(self):
        p = lp.make_plan(content(), late_join=False)
        fs = lp.findings_from_log("H", self.LOG, {})
        text = lp.report_text("madeup", p, 420.0, lp.phase_times(self.LOG), lp.fold(fs), "", {"H": lp.summaries(self.LOG)},
                              "runfolder", [])
        self.assertIn("wall clock: 420 s (under the 600 s budget)", text)
        self.assertIn("[difference after an action] breakable Box_Destructible", text)
        self.assertLess(text.index("[difference after an action]"), text.index("[action failed]"))

    def test_game_arg(self):
        self.assertEqual(lp.check_game_arg("--online_authored_ids=2"), "--online_authored_ids=2")
        for bad in ("--online_authored_ids=", "online_authored_ids=2", "--x", "--user_data_root=C:/x",
                    "--portal_mode=usb", "--Portal_Mode=software"):
            with self.assertRaises(ValueError, msg=bad):
                lp.check_game_arg(bad)

    def test_asleep_heading(self):
        # An object asleep on the audited side is not "object without a row": its own heading, counted.
        p = lp.make_plan(content(), late_join=False)
        log = "\n".join([
            "x Online: audit norow p1 v2_a breakable Crate_A#11 at 1,2,3",
            "x Online: audit asleep p1 v2_a breakable (null)#4625 at 4,5,6",
            "x Online: audit asleep p1 v3 breakable (null)#4625 at 4,5,6",
            "x Online: audit asleep p1 v3 gate GateDestructible001#900 at 7,8,9",
            "x Online: audit summary p1 v3 rows 9 differences 1 in_flight 0 pending 0 unsupported 0 not_audited 3"
            " no_row 1 asleep 2 kinds breakable=1/1/0/0"])
        fs = lp.findings_from_log("H", log, {})
        self.assertEqual(sorted(f.severity for f in fs), sorted(["object without a row"] + [lp.ASLEEP] * 3))
        text = lp.report_text("madeup", p, 420.0, lp.phase_times(log), lp.fold(fs), "", {"H": lp.summaries(log)},
                              "runfolder", [])
        head, rest = text.split("asleep on the audited side: ", 1)
        self.assertIn("findings: 1 in 1 entries", head)
        self.assertNotIn("[asleep on the audited side]", head)
        self.assertTrue(rest.startswith("2 in 2 entries"))
        self.assertIn("[asleep on the audited side] gate GateDestructible", rest)
        self.assertEqual(len(lp.summaries(log)), 1)


class Case(unittest.TestCase):
    """The generated case through skytest's own spec reader, and the runner keys it uses."""

    def setUp(self):
        import skytest
        self.sk = skytest
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text: str):
        f = self.dir / "case.toml"
        f.write_text(text, encoding="utf-8")
        return self.sk.load_spec(f, 300)

    def test_the_lobby_boot_is_the_owners_order(self):
        """Both clients join in the lobby (D by the join code, H lets it in), then H picks the slot (the
        save's continue, or a new game with --new-slot), the party enters the level together; D stays to the
        next level; nothing waits for a late join."""
        p = lp.make_plan(content(), d_lobby=True)
        s = self.load(lp.spec_toml(p))
        self.assertEqual(s.layout, "server+2")
        self.assertFalse(s.peers["D"].start_after)  # starts with H: no late start
        self.assertIn("--dev_hello_gate_loopback=true", s.peers["H"].args)
        self.assertNotIn("--dev_hello_ungated_loopback=true", s.peers["H"].args)
        h, d = s.peers["H"].script, s.peers["D"].script
        order = ["online_host -1", "waitfor online_join_pending", "online_let_in", "online_wait_peers 2",
                 "tap start", "waitfor front_end_left", "level madeup", "LEVELPASS phase sweep"]
        self.assertEqual([h.index(x) for x in order], sorted(h.index(x) for x in order))
        self.assertNotIn("late-join", h)
        self.assertIn("waitfor online_alone", h)  # D's next level before H ends the session
        self.assertIn("tap a\nwaitfor front_end_left", h)  # the continue
        self.assertLess(d.index("online_join {joincode}"), d.index("waitfor online_in_play"))
        self.assertNotIn("online_join {join}\n", d)
        self.assertNotIn("LEVELPASS D visit", d)
        self.assertIn("LEVELPASS D next level", d)
        for text in (h, d):
            self.assertFalse([l for l in text.splitlines() if re.match(r"^wait\s+\d", l)], "a fixed wait")
        new = lp.make_plan(content(), d_lobby=True)
        new.new_slot = True
        hn = self.load(lp.spec_toml(new)).peers["H"].script
        self.assertIn("tap down\nwaitfor menu_ready 60\ntap a\nwaitfor menu_ready 60\ntap a\nwaitfor front_end_left", hn)

    def test_generated_case_reads(self):
        p = lp.make_plan(content())
        s = self.load(lp.spec_toml(p))
        self.assertEqual((s.name, s.layout, s.server, s.shader_cache, s.show_server_window),
                         ("levelpass-madeup", "server+2", "host", "snapshot", True))
        # The host's server is shown at 640x360 as in every playtest: H gets --dev_show_server_window=true (Host
        # starts its server with it); --server-hidden keeps real play's hidden server.
        self.assertEqual(self.sk.show_server_args(SHOWN_DEFAULT_OFF, s, "H"), ["--dev_show_server_window=true"])
        p.server_hidden = True
        hidden = self.load(lp.spec_toml(p))
        self.assertFalse(hidden.show_server_window)
        self.assertEqual(self.sk.show_server_args(SHOWN_DEFAULT_OFF, hidden, "H"), [])
        p.server_hidden = False
        self.assertEqual(s.peers["D"].start_after, lp.LATE_JOIN_MARK)
        self.assertEqual(s.peers["D"].start_peer, "H")
        self.assertIn("--dev_backend_commands=true", s.peers["H"].args)
        solo = self.load(lp.spec_toml(lp.make_plan(content(), late_join=False)))
        self.assertEqual(solo.layout, "server+1")
        self.assertNotIn("D", solo.peers)

    def test_an_unresolved_exit_target_ends_the_pass(self):
        """Item 3: an exit step whose target finds no entity is H's forbidden line (fail fast ends the run at
        once); a sweep step's is not; the report names the step and the target."""
        p = lp.make_plan(content())
        h = lp.script_h(p, lp.LATE_JOIN_MARK)
        self.assertEqual(lp.exit_refs(h), ["auth:madeup:2"])
        s = self.load(lp.spec_toml(p))
        self.assertEqual(s.peers["H"].forbid, lp.exit_forbid(h) + [lp.PAUSE_FATAL, lp.MADE_COPY_LEFT])
        self.assertEqual(s.peers["D"].forbid, [])
        rx = re.compile(s.peers["H"].forbid[0])
        prefix = "[2026-11-18 16:42:12.345] [info] [core] [t2144] "
        line = prefix + "Dev: enter_trigger auth:madeup:2 failed (no entity: no authored object madeup:2 in this level's walks) after 0.0 s"
        self.assertTrue(rx.search(line))
        self.assertTrue(rx.search(prefix + "Dev: goto_entity auth:madeup:2 failed (no entity: no such entity) after 0.0 s"))
        # tutorial_b 20261118-230020: the fight won, the town loaded, a later round's fight_near on the anchor the
        # approach named found no entity: not the line (it failed the pass); the approach's own is.
        fx = re.compile(lp.exit_forbid("log LEVELPASS phase exit\ngoto_entity auth:tutorial_b:12036\nwait_free 60\n"
                                       "fight_near auth:tutorial_b:12036 1800 60\nlog LEVELPASS done\n")[0])
        self.assertFalse(fx.search(prefix + "Dev: fight_near auth:tutorial_b:12036 failed (no entity: no authored object "
                                   "tutorial_b:12036 in this level's walks) after 1.0 s"))
        self.assertTrue(fx.search(prefix + "Dev: goto_entity auth:tutorial_b:12036 failed (no entity: x) after 0.0 s"))
        self.assertEqual(lp.unresolved_exit(prefix + "Input script: LEVELPASS phase exit\n" + prefix +
                                            "Dev: fight_near auth:tutorial_b:12036 failed (no entity: x) after 1.0 s\n",
                                            "log LEVELPASS phase exit\ngoto_entity auth:tutorial_b:12036\n"
                                            "fight_near auth:tutorial_b:12036 1800 60\nlog LEVELPASS done\n"), "")
        # A sweep target, another failure of the exit's own target, and a longer id are not the line.
        self.assertFalse(rx.search(prefix + "Dev: goto_entity auth:madeup:6 failed (no entity: no such entity) after 0.0 s"))
        self.assertFalse(rx.search(prefix + "Dev: enter_trigger auth:madeup:2 failed (teleport failed) after 3.0 s"))
        self.assertFalse(rx.search(prefix + "Dev: enter_trigger auth:madeup:21 failed (no entity: x) after 0.0 s"))
        text = "\n".join([prefix + "Input script: LEVELPASS phase exit", line])
        got = lp.unresolved_exit(text, h)
        self.assertIn("`enter_trigger auth:madeup:2` found no entity: no authored object madeup:2", got)
        self.assertIn("no wait for the level end", got)
        self.assertEqual(lp.unresolved_exit(prefix + "Input script: LEVELPASS phase exit\n", h), "")
        # The ww1 literal of 18 November: a name the exit addresses.
        self.assertTrue(re.search(lp.exit_forbid("log LEVELPASS phase exit\ngoto_entity LevelOutro_Trigger\n"
                                                 "enter_trigger LevelOutro_Trigger\nlog LEVELPASS done\n")[0],
                                  "Dev: goto_entity LevelOutro_Trigger failed (no entity: no such entity) after 0.0 s"))
        # --no-end and a timed level address no exit target: no forbid.
        self.assertIn(f"forbid = {lp._toml_list([lp.PAUSE_FATAL, lp.MADE_COPY_LEFT])}",
                      lp.spec_toml(lp.make_plan(content(), no_end=True)))
        # The made-copy line (online/5-endfight-spawns) at the level change, a twin, a dependent: forbidden.
        rx = re.compile(lp.MADE_COPY_LEFT)
        for line in ("Online: runtime spawn made copy s1.52540001#1/0@2 Foo_Spawned1#33 still in the entity table after "
                     "its despawn (the level request)",
                     "Online: runtime spawn made copy dependent Foo_Spawned1_Fx#34 still in the entity table after its "
                     "despawn (the level request)",
                     "Online: runtime spawn twin Foo_Spawned2#35 still in the entity table after its despawn"):
            self.assertTrue(rx.search(line), line)
        self.assertFalse(rx.search("Online: runtime spawn made copy s1.52540001#1/0@2 Foo_Spawned1#33 removed (the "
                                   "level request; 0 dependent(s) before it)"))

    def base(self, d_extra: str = "", top: str = "") -> str:
        return "\n".join([
            'name = "x"', 'layout = "server+2"', 'stage = "8"', 'server = "host"', 'scenes = "skip"', top,
            "[peer.H]", 'script = """', "quit", '"""',
            "[peer.D]", 'script = """', "online_join {join}", "quit", '"""', d_extra, ""])

    def test_start_after_rules(self):
        self.assertEqual(self.load(self.base("start_after = 'mark'")).peers["D"].start_after, "mark")
        with self.assertRaises(self.sk.SpecError):
            self.load(self.base("start_after = '('"))
        with self.assertRaises(self.sk.SpecError):
            self.load(self.base("start_after = 'm'\nstart_peer = 'D'"))
        with self.assertRaises(self.sk.SpecError):  # H hosts: it never starts late
            self.load(self.base().replace("[peer.H]\n", "[peer.H]\nstart_after = 'm'\n"))

    def test_shader_cache_values(self):
        self.assertEqual(self.load(self.base()).shader_cache, "fresh")
        with self.assertRaises(self.sk.SpecError):
            self.load(self.base(top='shader_cache = "warm"'))

    def test_copy_cache_merges(self):
        src, dst = self.dir / "src", self.dir / "dst"
        (src / "shaders" / "shareable").mkdir(parents=True)
        (dst / "shaders" / "shareable").mkdir(parents=True)
        (src / "shaders" / "shareable" / "a.xpso").write_bytes(b"x" * 10)
        (src / "shaders" / "shareable" / "b.xsh").write_bytes(b"y" * 5)
        (dst / "shaders" / "shareable" / "b.xsh").write_bytes(b"z" * 8)  # larger: stays
        self.assertEqual(self.sk.copy_cache(src, dst), 1)
        self.assertEqual((dst / "shaders" / "shareable" / "a.xpso").read_bytes(), b"x" * 10)
        self.assertEqual((dst / "shaders" / "shareable" / "b.xsh").read_bytes(), b"z" * 8)
        self.assertEqual(self.sk.copy_cache(self.dir / "none", dst), 0)




class SecondPlayerFollows(unittest.TestCase):
    """D follows H through the level once the party is in (the game's Auto follow); --d-stays keeps the old D."""

    def test_d_turns_the_follow_on_after_it_is_in(self) -> None:
        plan = lp.make_plan(content(), d_lobby=True)
        self.assertNotIn("online_auto_follow", lp.script_d(plan))  # off unless the command is known (skytest)
        plan.d_follow = True
        lines = lp.script_d(plan).splitlines()
        k = lines.index("online_auto_follow on")
        self.assertLess(lines.index("log LEVELPASS D in"), k)
        self.assertEqual(lines[k + 1], "log LEVELPASS D follows H")
        # D presses nothing after it: it only waits for the level's end.
        self.assertFalse([l for l in lines[k:] if l.split(" ")[0] in ("tap", "press", "hold", "interact",
                                                                         "goto_entity", "enter_trigger")])

    def test_the_command_is_used_only_when_the_game_registers_it(self) -> None:
        import tempfile
        import skytest as sk
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "script_commands.toml"
            cfg = type("Cfg", (), {"script_commands": table, "repo": Path(tmp)})()
            table.write_text('[commands]\nonline_end = "none"\n', encoding="utf-8")
            self.assertFalse(sk.auto_follow_known(cfg))
            table.write_text('[commands]\nonline_end = "none"\nonline_auto_follow = "none"\n', encoding="utf-8")
            self.assertTrue(sk.auto_follow_known(cfg))


class ScenesInTheSweep(unittest.TestCase):
    """--scenes play: the sweep's time counts the scenes that play in it, and its deadline moves by them; D,
    not following H, comes to each scene starter and to the end fight (batch 10's tutorial_b)."""

    LOG = "\n".join([
        "[2026-11-19 05:09:34.226] [info] [core] [t1] Input script: LEVELPASS phase sweep",
        "[2026-11-19 05:09:56.514] [info] [core] [t1] Online: scene hold on here: this player is held (1 shared scene)",
        "[2026-11-19 05:10:30.118] [info] [core] [t1] Online: scene hold released here (the server released it)",
        "[2026-11-19 05:10:51.513] [info] [core] [t1] Online: scene hold on here: this player is held (1 shared scene)",
        "[2026-11-19 05:11:01.897] [info] [core] [t1] Online: scene hold released here (the server released it)",
        "[2026-11-19 05:12:32.943] [info] [core] [t1] Input script: LEVELPASS phase audit",
        "[2026-11-19 05:13:18.238] [info] [core] [t1] Online: scene hold on here: this player is held (the outro)",
        "[2026-11-19 05:13:46.844] [info] [core] [t1] Online: scene hold released here (the server released it)"])

    def test_the_sweeps_scene_time_from_a_run(self) -> None:
        self.assertAlmostEqual(lp.sweep_scene_seconds_from_log(self.LOG), 33.604 + 10.384, places=3)
        self.assertIsNone(lp.sweep_scene_seconds_from_log("Input script: LEVELPASS phase sweep\n"))

    def test_the_estimate_takes_the_larger_of_data_and_measure(self) -> None:
        scene = lp.SceneStart("TB_Intro", "trigger", lp.Obj("x", "C", (0.0, 0.0, 0.0), lp.GROUP_TRIGGERS), entered=True)
        other = lp.SceneStart("Talk", "other", lp.Obj("y", "C", (0.0, 0.0, 0.0), lp.GROUP_TRIGGERS))
        load = lp.SceneStart("Intro", "load", lp.Obj("z", "C", (0.0, 0.0, 0.0), lp.GROUP_TRIGGERS))
        lengths = {"TB_Intro": 10.0}
        self.assertEqual(lp.sweep_scene_estimate([scene, other, load], lengths), 10.0 + lp.SCENE_STEP_S + lp.SCENE_S)
        lengths[lp.SWEEP_SCENES_KEY] = 56.5
        self.assertEqual(lp.sweep_scene_estimate([scene, other, load], lengths), 56.5 + lp.SCENE_STEP_S)

    def plan_with_a_scene(self):
        plan = lp.make_plan(content(), d_lobby=True)
        plan.scenes = True
        starter = next(o for o in plan.stops[0].cluster.objects if o.ref)
        sc = lp.SceneStart("TB_Intro", "trigger", starter, starter=starter, length=10.0, entered=True)
        plan.scene_at = {plan.stops[0].label: [sc]}
        return plan, starter

    def test_the_deadline_is_set_again_before_each_stop(self) -> None:
        plan, _ = self.plan_with_a_scene()
        rearm = lp.sweep_rearm(plan)
        self.assertEqual(len(rearm), len(plan.stops))
        self.assertAlmostEqual(rearm[0], lp.sweep_share(plan, lp.BUDGET_S + plan.scene_extra_s))
        self.assertEqual(rearm, sorted(rearm, reverse=True))
        lines = lp.script_h(plan, None).splitlines()
        k = next(i for i, l in enumerate(lines) if l.startswith(f"log LEVELPASS visit {plan.stops[1].label} "))
        self.assertEqual(lines[k - 2:k], ["wait_free 120", f"sweep_deadline {rearm[1]:.0f}"])

    def test_d_comes_to_the_scene_starter_unless_it_follows(self) -> None:
        plan, starter = self.plan_with_a_scene()
        h = lp.script_h(plan, None).splitlines()
        k = h.index(f"enter_trigger {starter.ref}")
        self.assertEqual(h[k - 3:k - 1], [f"goto_entity {starter.ref} {lp.D_STANDOFF}",
                                          f"online_wait_peer_near_entity {starter.ref} {lp.D_NEAR} {lp.H_WAIT_D_S}"])
        d = lp.script_d(plan).splitlines()
        self.assertIn(f"online_wait_peer_near_entity {starter.ref} {lp.D_NEAR} {lp.D_COME_WAIT_S}", d)
        self.assertIn(f"goto_entity {starter.ref} {lp.D_STANDOFF}", d)
        plan.d_follow = True  # the game's follow brings D: no waits
        self.assertFalse([l for l in lp.script_d(plan).splitlines() if "online_wait_peer_near_entity" in l])
        self.assertFalse([l for l in lp.script_h(plan, None).splitlines() if "online_wait_peer_near_entity" in l])


BATCH10_TB = Path("D:/SwapForceOnline-data/tests/int2/runs/20261119-050629-levelpass-tutorial_b")


@unittest.skipUnless((BATCH10_TB / "H" / "game.log").is_file(), "batch 10's run folder is not on this machine")
class ScenesCalibration(unittest.TestCase):
    def test_batch_10_tutorial_b_sweep_lost_56_s_to_scenes(self) -> None:
        text = (BATCH10_TB / "H" / "game.log").read_text(encoding="utf-8", errors="replace")
        self.assertAlmostEqual(lp.sweep_scene_seconds_from_log(text), 56.54, places=2)


LEVELPASS_DATA = Path.home() / "Documents" / "T3Code" / "SwapForceOnline-work" / "levelpass"


@unittest.skipUnless((LEVELPASS_DATA / "ww1" / "manifest.json").is_file(), "the levelpass folder's data is not here")
class RequiredStepsPlan(unittest.TestCase):
    """The gates a player opens on the way, opened the way a player does (plan_required)."""

    def plan(self, level: str, **kw):
        return lp.make_plan(lp.load_content(LEVELPASS_DATA, level), d_lobby=True, **kw)

    def test_d_fights_one_fight_while_h_stands_by(self) -> None:
        # W32 (c): the plan's first fight is D's: H waits beside it (stand_by), D stops following, fights, follows again.
        old = lp.D_FIGHT
        self.addCleanup(setattr, lp, "D_FIGHT", old)
        lp.D_FIGHT = True
        p = self.plan("tutorial_a")  # tutorial_b's sampled sweep has no fight action (its fights are sampled out)
        p.d_follow = True
        ref = lp.d_fight_ref(p)
        self.assertTrue(ref.startswith("auth:"), ref)
        h = lp.script_h(p, None).splitlines()
        k = h.index(f"stand_by {ref} {lp.FIGHT_RADIUS} {4 * lp.ACTION_TIMEOUT[lp.FIGHT]}")
        self.assertEqual(h[k - 1], f"online_wait_peer_near_entity {ref} {lp.FIGHT_RADIUS} 60")
        self.assertNotIn(f"fight_near {ref} {lp.FIGHT_RADIUS} {lp.ACTION_TIMEOUT[lp.FIGHT]}", h)
        d = lp.script_d(p).splitlines()
        j = d.index(f"fight_near {ref} {lp.FIGHT_RADIUS} {3 * lp.ACTION_TIMEOUT[lp.FIGHT]}")
        self.assertEqual(d[j - 3:j], [f"online_wait_peer_near_entity {ref} {lp.FIGHT_RADIUS} {lp.D_FIGHT_WAIT_S}",
                                      "online_auto_follow off", f"goto_entity {ref} 300"])
        self.assertEqual(d[j + 1], "online_auto_follow on")
        lp.D_FIGHT = False
        self.assertEqual(lp.d_fight_ref(p), "")
        self.assertNotIn("stand_by", lp.script_h(p, None))

    def test_the_late_joiner_waits_for_its_arrival(self) -> None:
        # W30: the late joiner is placed only after its arrival move is over, where the build has the condition.
        old = lp.ARRIVAL_DONE
        self.addCleanup(setattr, lp, "ARRIVAL_DONE", old)
        p = lp.make_plan(lp.load_content(LEVELPASS_DATA, "tutorial_b"), d_lobby=False)
        lp.ARRIVAL_DONE = True
        d = lp.script_d(p).splitlines()
        k = d.index("waitfor online_barrier 300")
        self.assertEqual(d[k + 1], lp.ARRIVAL_WAIT)
        self.assertLess(k + 1, min(i for i, l in enumerate(d) if l.startswith("goto_entity ")))
        lp.ARRIVAL_DONE = False
        self.assertNotIn(lp.ARRIVAL_WAIT, lp.script_d(p))

    def test_tutorial_b_carries_both_keys(self) -> None:
        p = self.plan("tutorial_b")
        self.assertEqual([(r.kind, r.why_not) for r in p.required],
                         [("arena gate", ""), ("key gate", ""), ("key gate", "")])  # W34: the arena's fight first
        # W34: the arena gate's three managers, upstream first (W1 and W1_B act on the nested one, which opens it).
        self.assertEqual([l.split()[1] for l in p.required[0].h if l.startswith("encounter_clear ")],
                         ["auth:tutorial_b:13926", "auth:tutorial_b:13947", "auth:tutorial_b:13980"])
        h = lp.script_h(p, None).splitlines()
        k = h.index("log LEVELPASS phase required")
        self.assertLess(h.index("sweep_deadline 0"), k)
        # W25: each gate's own key (key_of:<gate>), never a key of the same name by distance.
        keys = [r for r in p.required if r.kind == "key gate"]
        for r in keys:
            self.assertIn(f"pickup key_of:{r.gate_ref} {lp.KEY_PICKUP_S}", h[k:])
        # W10: back to the gate, then the lock nearest the hero there (a bounded nearest: of the lock's own name):
        # the bare name from the pickup's end pressed gate 1's lock for gate 2 (tutorial_b 20261119-065111).
        for r in keys:
            press = f"interact nearest:TutorialLockedGate_keylock:{lp.KEY_LOCK_NEAR} {lp.KEY_PRESS_S}"
            i = h.index(f"log LEVELPASS required {r.label} key_gate TutorialLockedGate (fetch its key {r.gate.key_data})")
            self.assertEqual(h[i + 1:i + 6], [f"goto_entity {r.gate_ref}", f"pickup key_of:{r.gate_ref} {lp.KEY_PICKUP_S}",
                                              f"goto_entity {r.gate_ref}", press, f"online_audit {r.label}"])
        self.assertNotIn(f"interact TutorialLockedGate_keylock {lp.KEY_PRESS_S}", h)
        self.assertEqual([l for l in h if "nearest:" in l and "_keylock:" not in l], [])  # the key: exact name only
        self.assertEqual(lp.position_literals("\n".join(h)), [])
        d = lp.script_d(p)
        self.assertIn(f"online_wait_peer_near_entity {p.required[0].gate_ref} {lp.D_NEAR} {lp.D_COME_WAIT_S}", d)

    def test_the_gates_phase_has_its_own_budget(self) -> None:
        # W13 (batch 12 case 7, ww1 20261119-124636): the gates took the sweep's time (deadline 60 s, 175 s without).
        for level in ("tutorial_b", "ww1"):
            gated, free = self.plan(level), self.plan(level, no_gates=True)
            self.assertGreater(gated.required_s, 0, level)
            self.assertEqual(gated.required_s, sum(r.est_s for r in gated.required if not r.why_not))
            self.assertEqual(lp.plan_budget(gated), lp.BUDGET_S + gated.required_s)
            self.assertEqual(lp.sweep_share(gated, lp.plan_budget(gated)), lp.sweep_share(free, lp.plan_budget(free)),
                             level)
            self.assertEqual(len(gated.stops), len(free.stops), level)
            self.assertIn(f"timeout = {int(lp.CASE_TIMEOUT_S + gated.required_s)}", lp.spec_toml(gated))
            words = lp.budget_words(gated)
            self.assertIn(f"gates phase {gated.required_s:.0f} s of its own", words)
            self.assertIn(f"total {lp.BUDGET_S + gated.required_s:.0f} s", words)

    def test_each_client_walks_through_each_gate(self) -> None:
        # W23: H after its step (before the audit), D when H is at the gate with its follow off; the data's yaw.
        p = self.plan("tutorial_b")
        p.pass_gates, p.d_follow = True, True
        h = lp.script_h(p, None).splitlines()
        for r in p.required:
            i = h.index(f"online_audit {r.label}")
            self.assertEqual(h[i - 1], f"pass_gate {r.gate_ref} {lp.PASS_DISTANCE} {lp.PASS_H_S} yaw {r.yaw:.4f}")
        key = next(r for r in p.required if r.kind == "key gate")  # W34: the arena gate comes first now
        self.assertEqual(key.yaw, 0.6109)
        d = lp.script_d(p).splitlines()
        j = d.index(f"log LEVELPASS D passes required {key.label} TutorialLockedGate")
        self.assertEqual(d[j + 2], "online_auto_follow off")
        self.assertTrue(d[j + 3].startswith(f"pass_gate {key.gate_ref} {lp.PASS_DISTANCE} "))
        self.assertEqual(d[j + 4], "online_auto_follow on")
        self.assertEqual(lp.position_literals("\n".join(h + d)), [])
        off = self.plan("tutorial_b")  # a build without pass_gate: no walk
        self.assertFalse([l for l in lp.script_h(off, None).splitlines() + lp.script_d(off).splitlines()
                          if l.startswith("pass_gate")])

    def test_a_gate_h_cannot_pass_stops_the_level(self) -> None:
        # W26: H's walk failing right after its own step is H's fatal line; the verdict names the gate and why.
        p = self.plan("tutorial_b")
        p.pass_gates = True
        import tomllib
        self.assertIn(lp.REQUIRED_GATE_FATAL, tomllib.loads(lp.spec_toml(p))["peer"]["H"]["forbid"])
        self.assertNotIn("pass_gate", lp.spec_toml(self.plan("tutorial_b")))  # no pass_gate in the build: as before
        h = "\n".join([
            "[2026-11-19 15:48:00.000] [info] [core] [t1] Input script: LEVELPASS required r2 key_gate TutorialLockedGate (x)",
            "[2026-11-19 15:48:06.744] [info] [core] [t1] Dev: pickup TutorialLockedGate_key#2609 failed (not carried after "
            "the teleports: carrier none) after 5.3 s",
            "[2026-11-19 15:48:48.956] [info] [core] [t1] Dev: pass_gate TutorialLockedGate#2574 failed (not passed on "
            "foot: stopped at 1,2,3, 56 before the gate's plane (x)) after 31.0 s"])
        self.assertEqual(lp.required_gate_stop(h), "gate TutorialLockedGate not opened: pickup "
                         "TutorialLockedGate_key#2609 failed (not carried after the teleports: carrier none); the rest of "
                         "the level not attempted")
        verdict, missed = lp.pass_verdict("fail", ["forbidden line"], h, p)
        self.assertEqual(verdict, lp.FAIL)
        self.assertTrue(missed[0].startswith("gate TutorialLockedGate not opened: "), missed)

    def test_launch_pads_are_ridden_only_when_switched_on(self) -> None:
        # W27b: tutorial_b's two rail pads from the data (B, 7 presses); each ridden, never sampled out.
        content = lp.load_content(LEVELPASS_DATA, "tutorial_b")
        self.assertEqual(lp.launch_pads(content.manifest),
                         {"tutorial_b_gameplay_launchpad.EpicRailpad": ("b", 7),
                          "tutorial_b_gameplay_launchpad.EpicRailpad001": ("b", 7)})
        old = lp.LAUNCH_RIDES
        self.addCleanup(setattr, lp, "LAUNCH_RIDES", old)
        lp.LAUNCH_RIDES = True
        h = lp.script_h(self.plan("tutorial_b", no_gates=True), None).splitlines()
        for ref in ("auth:tutorial_b:6802", "auth:tutorial_b:6899"):
            k = h.index(f"launch_pad {ref} b {7 + lp.LAUNCH_EXTRA_PRESSES} {lp.LAUNCH_RIDE_S}")
            self.assertEqual(h[k - 1], f"interact {ref} 20")
        lp.LAUNCH_RIDES = False
        off = lp.script_h(self.plan("tutorial_b", no_gates=True), None)
        self.assertNotIn("launch_pad", off)

    def test_no_gates_keeps_the_full_sweep_and_says_so(self) -> None:
        gated, free = self.plan("tutorial_b"), self.plan("tutorial_b", no_gates=True)
        self.assertEqual(free.required, [])
        self.assertEqual(len(free.stops), len(gated.stops))  # W13: the gates no longer shrink the sweep
        self.assertLessEqual(free.estimate_s, lp.BUDGET_S)
        self.assertIn(f"log {lp.GATES_SKIPPED}", lp.script_h(free, None).splitlines())
        self.assertIn("; gates not attempted (--no-gates); total 600 s", lp.budget_words(free))
        # W34: the arena gate's fight (three managers) and the two keys.
        arena = gated.required[0]
        self.assertEqual(arena.kind, "arena gate")
        self.assertIn(f"gates phase {2 * lp.REQ_KEY_S + arena.est_s:.0f} s of its own (3 of 3 required gate(s) performed)",
                      lp.budget_words(gated))

    def test_ww1_fights_its_battle_gates_and_waits_for_the_lock_commands(self) -> None:
        p = self.plan("ww1")
        kinds = [(r.kind, bool(r.why_not)) for r in p.required]
        self.assertEqual(kinds, [("Spark Lock", True), ("battle gate", False), ("Spark Lock", True), ("battle gate", False),
                                 ("Spark Lock", True), ("battle gate", False)])
        saw = next(r for r in p.required if r.gate.name == "PuzzleGateSawtooth001")
        self.assertTrue(saw.h[1].startswith("enter_trigger "))  # the caged Greebles' release first
        self.assertTrue(saw.h[2].startswith("fight_near "))
        # W16: EncounterManager001_002 starts within its activation radius (2000, nothing toggles it): the step goes
        # to the manager first (batch 12 case 7 fought a placed enemy 2990 away; the encounter never started).
        ec = next(r for r in p.required if r.gate.name == "EC001_DemonGate")
        self.assertEqual(ec.h[1], "goto_entity auth:ww1:8291")
        self.assertEqual(ec.h[2], "encounter_clear auth:ww1:8291 1500 70 exact")  # W18: 6 spawns: 30 + 5 * 6 + 10
        # W18: the three-wave gate (3 placed, 6 and 5 spawns): 30 + 5 * 14 + 10.
        fin = next(r for r in p.required if r.gate.name == "Finale_DemonGate008")
        # W34: every manager of the fight is fought (the placed wave by fight_near, then each wave's manager).
        self.assertEqual([l for l in fin.h if l.startswith(("fight_near ", "encounter_clear "))],
                         ["fight_near auth:ww1:6561 1500 110", "encounter_clear auth:ww1:6693 1500 110 exact",
                          "encounter_clear auth:ww1:6722 1500 110 exact"])
        self.assertEqual(fin.est_s, lp.REQ_BATTLE_S + 3 * 110)
        # W17: no gate step waits for its gate to move (a LockedObjectComponent gate opens in place).
        for r in p.required + self.plan("tutorial_b").required + self.plan("ww1", lock_commands=True).required:
            self.assertFalse([l for l in r.h if l.startswith("wait_moved")], r.h)
        spark = self.plan("ww1", lock_commands=True).required[0]
        self.assertEqual(spark.h[2:5], [f"interact {spark.gate_ref} 20", "waitfor lock_ready 30", "lock_solve 120"])
        self.assertEqual(spark.d, [f"interact {spark.gate_ref} 20", "waitfor lock_ready 30", "lock_solve 120"])


class PauseScreens(unittest.TestCase):
    """A pickup may bring up the game's found screen, which pauses the game until a press (ww1 20261119-051557: 90 s
    held next to SoulGem_lockjaw): the step runs under a short hold cap, then the screen is dismissed as a player
    does and the pass's own cap comes back; --long-pause keeps one screen up on purpose."""

    def gem(self) -> "lp.Action":
        o = lp.Obj("SoulGem_x", "CPhysicalEntity", (0.0, 0.0, 0.0), lp.GROUP_PICKUPS, ident="u", ref="auth:lvl:9")
        o.components = [lp.COMP_COLLECTIBLE]
        return lp.Action(action=lp.PICKUP, obj=o, why="a collectible")

    def test_a_pickup_dismisses_its_screen_within_seconds(self) -> None:
        lines = lp.action_lines(self.gem(), "v3_1", after="auth:lvl:9", cap=90)
        self.assertEqual(lines[0], f"hold_cap {lp.PAUSE_HOLD_S}")
        k = lines.index("pickup auth:lvl:9 10")
        self.assertTrue(lines[k + 1].startswith("log LEVELPASS pause check v3_1"))
        self.assertEqual(lines[k + 2:], [*lp.DISMISS_LINES, "log LEVELPASS pause check v3_1: one more B if the hero "
                                         "is still held", "tap b", "waitfor hero_free 5", "hold_cap 90", "wait_free 90"])
        lever = lp.Action(action=lp.INTERACT_A, obj=lp.Obj("Lever", "C", (0.0, 0.0, 0.0), lp.GROUP_INTERACT, ref="auth:lvl:4"),
                          why="a switch")
        self.assertNotIn("hold_cap 4", lp.action_lines(lever, "v3_2", cap=90))  # a microgame may hold it on purpose

    def test_one_long_pause_on_purpose(self) -> None:
        lines = lp.action_lines(self.gem(), "v3_1", after="auth:lvl:9", cap=20, long_pause=60)
        self.assertTrue(lines[0].startswith("log LEVELPASS long pause v3_1: the screen stays up 60 s"))
        self.assertEqual(lines[1], "hold_cap 60")
        self.assertEqual(lines[-2:], ["hold_cap 20", "wait_free 20"])


class FixedWaits(unittest.TestCase):
    """No generated step waits longer than the pass can (the input script's `wait` takes seconds: batch 11 run 3's
    `wait 25000` held ww1's game slot for its whole 660 s)."""

    def test_the_checker(self) -> None:
        self.assertEqual(lp.wait_errors("wait 25\nwait 3\n", 100), [])
        self.assertEqual(lp.wait_errors("wait 25000\n", 600)[0], "wait 25000 is over 120 s (the input script's wait "
                                                                  "takes seconds)")
        self.assertIn("add up to", lp.wait_errors("wait 100\nwait 100\n", 150)[-1])

    def test_every_generated_script(self) -> None:
        plans = [lp.make_plan(content(), d_lobby=True), lp.make_plan(content(), d_lobby=True, scenes=True)]
        if (LEVELPASS_DATA / "ww1" / "logic.json").is_file():
            for level in ("tutorial_a", "tutorial_b", "ww1"):
                c = lp.load_content(LEVELPASS_DATA, level)
                plans += [lp.make_plan(c, d_lobby=True), lp.make_plan(c, d_lobby=True, scenes=True, lock_commands=True),
                          lp.make_plan(c, d_lobby=True, no_gates=True)]
        for p in plans:
            p.long_pause_s = 60 if lp.long_pause_label(p) is None else 0  # the option's own hold is no fixed wait
            for script in (lp.script_h(p, lp.LATE_JOIN_MARK), lp.script_h(p, None), lp.script_d(p)):
                self.assertEqual(lp.wait_errors(script, p.estimate_s), [], p.level)
        ww1 = [p for p in plans if p.level == "ww1" and p.pieces]
        if ww1:
            self.assertIn("wait 25", lp.script_h(ww1[0], None).splitlines())  # the islands' stay, in seconds


class FoundScreen:
    """The Soul Gem's found screen as the recorded runs show it: it takes no press before READY_S after the
    collect (A 5.1 s after it did nothing: ww1 20261119-110623; A 12 s, 13 s and 92 s after it was taken: the
    18 November probes, ww1 20261119-051557); A takes it from "found" to the prompt, B on the prompt closes it
    (the hero free 1.5 s later), B on "found" does nothing, A on the prompt answers Yes (the preview movie)."""
    READY_S = 10.0

    def __init__(self, start_s: float) -> None:
        self.t, self.state, self.free_at = start_s, "found", None

    def held(self) -> bool:
        return self.free_at is None or self.t < self.free_at

    def play(self, lines: list[str]) -> str:
        for line in lines:
            word, *rest = line.split()
            if word in ("waitfor", "wait_free"):
                n = float(rest[-1])
                if self.held():
                    self.t = min(self.t + n, self.free_at) if self.free_at is not None else self.t + n
            elif word == "wait":
                self.t += float(rest[0])
            elif line == "tap a" and self.t >= self.READY_S:
                self.state = {"found": "prompt", "prompt": "movie"}.get(self.state, self.state)
            elif line == "tap b" and self.t >= self.READY_S and self.state == "prompt":
                self.state, self.free_at = "closed", self.t + 1.5
        return self.state


W3_DISMISS = ["tap a", "waitfor hero_free 3", "tap b", "waitfor hero_free 4", "tap b", "waitfor hero_free 4"]
FIXTURE_RUNS = Path("D:/SwapForceOnline-data/tests/int2/runs")


class FoundScreenDismissal(unittest.TestCase):
    """W7: the dismissal waits for the screen before its one A (batch 11 second round: W3's A, B, B came 5.1 s after
    the collect and closed nothing; the game stayed paused until fail-fast)."""

    def gem_step(self) -> list[str]:
        o = lp.Obj("SoulGem_x", "CPhysicalEntity", (0.0, 0.0, 0.0), lp.GROUP_PICKUPS, ident="u", ref="auth:lvl:9")
        o.components = [lp.COMP_COLLECTIBLE]
        lines = lp.action_lines(lp.Action(action=lp.PICKUP, obj=o, why="a gem"), "v19_1", after="auth:lvl:9", cap=20)
        return lines[lines.index("pickup auth:lvl:9 10") + 1:]

    def test_the_model_matches_both_recorded_runs(self) -> None:
        self.assertEqual(FoundScreen(5.1).play(W3_DISMISS), "found")    # 20261119-110623: never freed
        self.assertEqual(FoundScreen(91.0).play(W3_DISMISS), "closed")  # 20261119-051557: A at 92 s, then B

    def test_the_new_dismissal_closes_it_early_and_late_and_never_presses_a_twice(self) -> None:
        after = self.gem_step()
        self.assertEqual(after.count("tap a"), 1)
        for start in (0.5, 4.0, 5.1, 6.0, 30.0, 91.0):
            self.assertEqual(FoundScreen(start).play(after), "closed", start)

    def test_long_pause_ends_with_the_same_dismissal(self) -> None:
        o = lp.Obj("SoulGem_x", "CPhysicalEntity", (0.0, 0.0, 0.0), lp.GROUP_PICKUPS, ident="u", ref="auth:lvl:9")
        o.components = [lp.COMP_COLLECTIBLE]
        lines = lp.action_lines(lp.Action(action=lp.PICKUP, obj=o, why="a gem"), "v19_1", after="auth:lvl:9", cap=20,
                                long_pause=60)
        after = lines[lines.index("pickup auth:lvl:9 10") + 1:]
        self.assertEqual(after[1:1 + len(lp.DISMISS_LINES)], list(lp.DISMISS_LINES))
        self.assertEqual(FoundScreen(60.0).play(after), "closed")

    @unittest.skipUnless((FIXTURE_RUNS / "20261119-110623-levelpass-ww1" / "H" / "game.log").is_file() and
                         (FIXTURE_RUNS / "20261119-051557-levelpass-ww1" / "H" / "game.log").is_file(),
                         "the two recorded ww1 runs are not on this machine")
    def test_the_recorded_starts(self) -> None:
        import datetime as dt

        def start_of(run: str, pattern: str) -> float:
            text = (FIXTURE_RUNS / run / "H" / "game.log").read_text(encoding="utf-8", errors="replace")
            stamp = lambda line: dt.datetime.strptime(line[1:24], "%Y-%m-%d %H:%M:%S.%f")
            got = next(l for l in text.splitlines() if "Online: collect native 000b0017" in l)
            done = next(l for l in text.splitlines() if pattern in l)
            return (stamp(done) - stamp(got)).total_seconds()

        new = start_of("20261119-110623-levelpass-ww1", "LEVELPASS pause check v19_1")
        old = start_of("20261119-051557-levelpass-ww1", "Dev: pickup (null)#3755 failed")
        self.assertAlmostEqual(new, 5.088, places=2)
        self.assertGreater(old, 90.0)
        self.assertEqual(FoundScreen(new).play(W3_DISMISS), "found")        # what the new run did
        self.assertEqual(FoundScreen(new).play(self.gem_step()), "closed")  # what W7 does from there


MARK_RACE_RUN = Path("D:/SwapForceOnline-data/tests/int2/runs/20261119-120551-levelpass-tutorial_b")


class MarkRace(unittest.TestCase):
    """W11 (batch 12 run 2): D's mark of its first level landed 0.02 s after the level under test began loading, so
    its wait for the next level was met only in the hub. D now marks before its first level's barrier."""

    def test_d_marks_in_play_before_the_barrier_and_h_marks_before_its_level_command(self) -> None:
        p = lp.make_plan(content(), d_lobby=True)
        d = lp.script_d(p).splitlines()
        k = d.index("waitfor online_in_play 300")
        self.assertEqual(d[k + 1:k + 3], ["online_mark_level", "waitfor online_barrier 300"])
        first_in = d.index("log LEVELPASS D in the first level")
        self.assertNotIn("online_mark_level", d[k + 2:first_in])   # no second mark after the barrier (the race)
        self.assertEqual(d[first_in + 1], f"waitfor {lp.NEXT_LEVEL} 600")
        h = lp.script_h(p, None).splitlines()
        self.assertEqual(h[h.index(f"level {p.level}") - 1], "online_mark_level")

    def test_a_mark_inside_a_load_is_named(self) -> None:
        log = "\n".join([
            "[2026-11-19 12:07:45.115] [info] [core] [t1] Online: barrier 1 released at tick 6079",
            "[2026-11-19 12:07:45.454] [info] [core] [t1] Online: level 2 tutorial/tutorial_b/tutorial_b: load",
            "[2026-11-19 12:07:45.472] [info] [core] [t2] Online: online_mark_level: generation 2",
            "[2026-11-19 12:07:58.244] [info] [core] [t1] Online: barrier 2 released at tick 6867",
            "[2026-11-19 12:07:58.256] [info] [core] [t2] Online: online_mark_level: generation 2"])
        late = lp.late_marks(log)
        self.assertEqual([(m.generation, m.load_no, m.mark_no, m.mark_at) for m in late], [(2, 2, 3, "12:07:45.472")])
        self.assertTrue(lp.client_progress(log + "\nInput script: LEVELPASS D in", "D", "tutorial_b").startswith(
            "its mark landed inside a load: `online_mark_level: generation 2` at 12:07:45.472"))

    def test_the_first_levels_mark_before_its_barrier_is_not_named(self) -> None:
        # W20 (batch 13 case 7, 20261119-141657): D's own order marks the hub while it loads, no start released yet.
        log = "\n".join([
            "[2026-11-19 14:17:35.922] [info] [core] [t1] Online: level 1 town/town_state10/town_state10: load",
            "[2026-11-19 14:18:50.743] [info] [core] [t2] Online: online_mark_level: generation 1",
            "[2026-11-19 14:18:51.739] [info] [core] [t1] Online: barrier 1 released at tick 6091",
            "[2026-11-19 14:18:51.833] [info] [core] [t1] Online: level 2 tutorial/tutorial_b/tutorial_b: load",
            "[2026-11-19 14:19:05.077] [info] [core] [t1] Online: barrier 2 released at tick 6891",
            "[2026-11-19 14:19:05.402] [info] [core] [t2] Online: online_mark_level: generation 2"])
        self.assertEqual(lp.late_marks(log), [])

    @unittest.skipUnless((MARK_RACE_RUN / "D" / "game.log").is_file() and (MARK_RACE_RUN / "H" / "game.log").is_file(),
                         "batch 12's tutorial_b run is not on this machine")
    def test_the_recorded_run(self) -> None:
        read = lambda who: (MARK_RACE_RUN / who / "game.log").read_text(encoding="utf-8", errors="replace")
        d, h = read("D"), read("H")
        late = lp.late_marks(d)
        self.assertEqual([(m.generation, m.load_at, m.mark_at) for m in late], [(2, "12:07:45.454", "12:07:45.472")])
        self.assertEqual(lp.late_marks(h), [])  # H marks before its own `level`
        # Where the new order marks: right after D's `waitfor online_in_play` was met, D's latest generation was 1.
        lines = d.splitlines()
        met = next(i for i, l in enumerate(lines) if "Input script: waitfor online_in_play met" in l)
        loads = [int(m.group(1)) for m in map(lp.LEVEL_LOAD_LINE.search, lines[:met]) if m]
        self.assertEqual(loads[-1], 1)


class Figures(unittest.TestCase):
    """W21: a named figure for one player from the copied figure folder, a swapper as its two halves."""

    def folder(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name)
        for name in ("Scorp.dump", "Spy Rise Top.dump", "Spy Rise Bottom.dump", "Trap Shadow Top.dump"):
            (f / name).write_bytes(b"x")
        return f

    def test_a_figure_a_swapper_and_a_refusal(self) -> None:
        f = self.folder()
        self.assertEqual(lp.figure_files(f, "Scorp"), ["Scorp.dump"])
        self.assertEqual(lp.figure_files(f, "Spy Rise"), ["Spy Rise Top.dump", "Spy Rise Bottom.dump"])
        self.assertEqual(lp.figure_words(["Spy Rise Top.dump", "Spy Rise Bottom.dump"]),
                         "Spy Rise (a swapper: Spy Rise Top + Spy Rise Bottom)")
        with self.assertRaises(ValueError) as e:
            lp.figure_files(f, "Trap Shadow")  # a top half alone is no swapper
        self.assertIn("its swappers: Spy Rise", str(e.exception))

    def test_d_gets_both_halves_on_the_portal(self) -> None:
        p = lp.make_plan(content(), d_lobby=True)
        p.figures_d = ["Spy Rise Top.dump", "Spy Rise Bottom.dump"]
        d = lp.script_d(p).splitlines()
        k = d.index("place 0 {figures}/Spy Rise Top.dump")
        self.assertEqual(d[k + 1], "place 1 {figures}/Spy Rise Bottom.dump")
        self.assertNotIn(f"place 0 {{figures}}/{lp.FIGURE_D}", d)
        self.assertIn(f"place 0 {{figures}}/{lp.FIGURE_H}", lp.script_h(p, None).splitlines())

class HeldFights(unittest.TestCase):
    """A fight is planned only when its held enemies' release is on the route before it (levelpass_holds)."""

    def obj(self, name: str, state: str, x: float, cls: str = "CActor", comps=()) -> "lp.Obj":
        o = lp.Obj(name=name, cls=cls, pos=(x, 0.0, 0.0), group=lp.STATIC_GROUPS[state], ident="u", kind="")
        o.state, o.ref, o.components = state, f"auth:madeup:{int(x)}", list(comps)
        return o

    def stop(self, *objs) -> "lp.Stop":
        fight = lp.Action(action=lp.FIGHT, obj=objs[0], why="an enemy")
        return lp.Stop(label="v3", cluster=lp.Cluster(center=objs[0].pos, objects=list(objs), index=2),
                       actions=[fight], visit_only=[])

    def hold(self, o, *releases) -> "lh.Hold":
        return lh.Hold(lp.obj_key(o), o.name, "u", o.pos, "job state Idle", list(releases))

    def test_the_release_trigger_is_entered_before_the_fight(self) -> None:
        g = self.obj("Greeble", "enemy", 100)
        trig = self.obj("Wake_Trigger", "trigger", 9000, cls="CScriptTriggerEntity")
        s = self.stop(g)
        holds = {lp.obj_key(g): self.hold(g, lh.Release("u.Wake_Trigger", lh.ENTER, lp.obj_key(trig), ["Wake_Trigger"]))}
        notes = lp.settle_held_fight(s, [g, trig], holds, {lp.obj_key(o): o for o in (g, trig)}, set(), set())
        self.assertEqual([(a.action, a.obj.name) for a in s.actions], [(lp.ENTER, "Wake_Trigger"), (lp.FIGHT, "Greeble")])
        self.assertIn("after enter Wake_Trigger", notes[0])

    def test_a_lock_leaves_the_fight_out_and_says_so(self) -> None:
        g, g1 = self.obj("Greeble", "enemy", 100), self.obj("Greeble001", "enemy", 200)
        gate = self.obj("Lock_Gate", "lock", 5000, cls="CGameEntity")
        r = lh.Release("u.Scene_Manager", lh.LOCK, lp.obj_key(gate), ["Lock_Gate", "Scene_Manager"], 0.5)
        holds = {lp.obj_key(g): self.hold(g, r), lp.obj_key(g1): self.hold(g1, r)}
        s = self.stop(g, g1)
        notes = lp.settle_held_fight(s, [g, g1, gate], holds, {lp.obj_key(o): o for o in (g, g1, gate)}, set(), set())
        self.assertEqual(s.actions, [])
        self.assertIn("left out: every enemy in its reach is held: needs a lock opened: Lock_Gate (holds Greeble, "
                      "Greeble001", notes[0])
        self.assertIn("lock_move / lock_solve", notes[0])

    def test_ambient_enemies_are_no_reason_to_fight_but_free_ones_are(self) -> None:
        amb, free = self.obj("Ambi_Chompy_03", "enemy", 100), self.obj("Chompy", "enemy", 300)
        holds = {lp.obj_key(amb): lh.Hold(lp.obj_key(amb), amb.name, "u", amb.pos, "its AI component disabled")}
        s = self.stop(amb)
        notes = lp.settle_held_fight(s, [amb], holds, {}, set(), set())
        self.assertEqual(s.actions, [])
        self.assertIn("ambient, nothing in the level's logic releases them: Ambi_Chompy_03", notes[0])
        s = self.stop(amb, free)
        notes = lp.settle_held_fight(s, [amb, free], holds, {}, set(), set())
        self.assertEqual([a.action for a in s.actions], [lp.FIGHT])
        self.assertIn("kept for the enemies it can hurt", notes[0])

    def test_a_release_on_the_ends_path_is_never_entered_in_the_sweep(self) -> None:
        g = self.obj("Lobber", "enemy", 100)
        trig = self.obj("Start_Fight", "trigger", 900, cls="CScriptTriggerEntity")
        holds = {lp.obj_key(g): self.hold(g, lh.Release("u.Start_Fight", lh.ENTER, lp.obj_key(trig), ["Start_Fight"]))}
        s = self.stop(g)
        lp.settle_held_fight(s, [g, trig], holds, {lp.obj_key(trig): trig}, set(), {lp.obj_key(trig)})
        self.assertEqual(s.actions, [])


if __name__ == "__main__":
    unittest.main()
