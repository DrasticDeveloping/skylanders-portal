"""Tests for the level pass's generator and report (levelpass.py) on made-up content: the manifest
readers, the classification, clustering and order, sampling and the budget, the generated scripts (no
fixed wait, every wait a condition with a timeout, unique audit labels), and the report's folding.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_levelpass.py"
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import levelpass as lp  # noqa: E402

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
        self.assertIn(f"waitfor online_level_end {lp.TIMED_END_S}", h)
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
        p = lp.make_plan(c, late_join=False, budget_s=lp.BOOT_S + lp.LEVEL_LOAD_S + lp.EXIT_S + 10 * lp.VISIT_S)
        self.assertLessEqual(p.estimate_s, lp.BOOT_S + lp.LEVEL_LOAD_S + lp.EXIT_S + 10 * lp.VISIT_S)
        self.assertEqual(len(p.stops), 10)
        self.assertTrue(any("over the budget" in x for x in p.left_out))
        # The nearest stay.
        self.assertEqual(p.stops[0].cluster.center, (5000.0, 0.0, 0.0))

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
        self.assertIn("online_host 0", h)
        self.assertIn("level madeup", h)
        self.assertIn("log LEVELPASS late-join start", h)
        self.assertIn("waitfor online_peer_was_in 300", h)  # D may have come and gone already
        self.assertIn("waitfor online_past_level_end 240", h)
        self.assertLess(h.index("LEVELPASS phase late-join"), h.index("LEVELPASS phase exit"))
        self.assertIn("attack_until auth:madeup:6 12", h)
        self.assertIn("interact auth:madeup:10 10", h)
        self.assertIn("goto_entity auth:madeup:", h)
        self.assertIn("waitfor online_alone 300", h)
        d = lp.script_d(p)
        self.check_script(d)
        self.assertIn("online_join {join}", d)
        self.assertNotIn("online_host", d)
        self.assertIn("goto_entity auth:madeup:", d)
        self.assertLess(d.index("online_end"), d.index("quit"))

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
        self.assertNotIn("online_level_end", h)
        self.assertIn("LEVELPASS exit none", h)
        self.assertNotIn("online_wait_peers", h)

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

    def test_generated_case_reads(self):
        p = lp.make_plan(content())
        s = self.load(lp.spec_toml(p))
        self.assertEqual((s.name, s.layout, s.server, s.shader_cache, s.show_server_window),
                         ("levelpass-madeup", "server+2", "host", "snapshot", False))
        self.assertEqual(s.peers["D"].start_after, lp.LATE_JOIN_MARK)
        self.assertEqual(s.peers["D"].start_peer, "H")
        self.assertIn("--dev_backend_commands=true", s.peers["H"].args)
        solo = self.load(lp.spec_toml(lp.make_plan(content(), late_join=False)))
        self.assertEqual(solo.layout, "server+1")
        self.assertNotIn("D", solo.peers)

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


if __name__ == "__main__":
    unittest.main()
