"""The level check's graders (levelcheck.py) on made-up run folders: each client's time in a level, the
enemy, pickup and loot pairing between S and the clients, the scene window, and that a grader with nothing
to grade never says PASS. The lines are the games' own shapes (copied from the runs of 18-19 November).
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_levelcheck.py"
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
import levelcheck as lc  # noqa: E402

GAME_REPO = HERE.parents[4]  # thirdparty/<portal>/tools/skytest/tests -> the game repository
K1 = "a00000f8d.6cee6ba5.79d73383@2"
K2 = "a00000f92.6f67914d.79d73383@2"
K3 = "sb7fe49cd.52540001#2/0@2"


def at(sec: float) -> str:
    whole = int(sec)
    return f"[2026-11-19 00:{10 + whole // 60:02d}:{whole % 60:02d}.{int(round((sec - whole) * 1000)):03d}]"


def log(lines: list[tuple[float, str]]) -> str:
    return "".join(f"{at(s)} [info] [core] [t1] {text}\n" for s, text in lines)


H_JOIN = [(0, "Online: connected as peer 1 (server tick 10, level 0 none)"),
          (5, "Online: level 1 town/town_state10/town_state10: load"),
          (20, "Online: level 2 tutorial/tutorial_b/tutorial_b: load")]
D_JOIN = [(40, "Online: connected as peer 2 (server tick 900, level 2 tutorial/tutorial_b/tutorial_b)")]
LEVEL_END = [(200, "Online: level end COpenResultsScreenMessage from the server played here (generation 2, x)"),
             (215, "Online: level 3 town/town_state10/town_state10: load")]


FOLDERS: list[tempfile.TemporaryDirectory] = []


def tearDownModule() -> None:
    for t in FOLDERS:
        t.cleanup()


class RunFolder:
    def __init__(self, s: list, h: list, d: list | None = None, peers: list | None = None,
                 result: dict | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        FOLDERS.append(self.tmp)
        self.path = Path(self.tmp.name)
        (self.path / "H" / "data" / "Online" / "session").mkdir(parents=True)
        (self.path / "H" / "data" / "Online" / "session" / "game.log").write_text(log(s), encoding="utf-8")
        (self.path / "H" / "game.log").write_text(log(h), encoding="utf-8")
        if d is not None:
            (self.path / "D").mkdir()
            (self.path / "D" / "game.log").write_text(log(d), encoding="utf-8")
        if peers is not None or result is not None:
            import json
            (self.path / "result.json").write_text(json.dumps({"peers": peers or [], "case": "levelpass-tutorial_b",
                                                               **(result or {})}),
                                                   encoding="utf-8")

    def run(self) -> lc.Run:
        return lc.load_run(self.path)



class RotatedLog(unittest.TestCase):
    """W14: S's log rotated in a long run (`game.1.log` older, `game.log` newest); its early lines are read too, and
    each line is cited in its own file."""

    def test_rotated_files_are_read_in_time_order(self) -> None:
        f = RunFolder([(300, "Online: puppet 1 pose 9 at 1,2,3 client at 1,2,3 off 0 age 1 ms")], H_JOIN)
        session = f.path / "H" / "data" / "Online" / "session"
        (session / "game.1.log").write_text(log([(100, "Online: early S line")]), encoding="utf-8")
        (session / "game.2.log").write_text(log([(50, "Online: earliest S line")]), encoding="utf-8")
        s = f.run().server
        self.assertEqual([l.text for l in (s.line(i) for i in range(len(s.lines)))],
                         ["Online: earliest S line", "Online: early S line",
                          "Online: puppet 1 pose 9 at 1,2,3 client at 1,2,3 off 0 age 1 ms"])
        self.assertEqual(s.find(re.compile(r"early S line"))[0][0].cite(),
                         "H/data/Online/session/game.1.log:1: Online: early S line")
        self.assertTrue(s.find(re.compile(r"puppet 1 pose"))[0][0].cite().startswith("H/data/Online/session/game.log:1:"))


class SkippedForTime(unittest.TestCase):
    """W13: steps the sweep's deadline skipped are no stops where H acted, and a NOT TESTED row they would test says
    so (not a row the run turned off)."""
    H = [(60, "Input script: LEVELPASS act v19_1 pickup SoulGem_lockjaw CPhysicalEntity ref auth:ww1:12086"),
         (60.1, "Dev: goto_entity auth:ww1:12086 skipped: the sweep's time is up"),
         (60.2, "Dev: pickup auth:ww1:12086 10 skipped: the sweep's time is up"),
         (61, "Input script: LEVELPASS visit v20 objects 3 (A, B, C) ref auth:ww1:4666"),
         (61.1, "Dev: goto_entity auth:ww1:4666 skipped: the sweep's time is up"),
         (62, "Input script: LEVELPASS act v22_1 interact TreasureChest002 CPhysicalEntity ref auth:ww1:12198"),
         (62.1, "Dev: goto_entity auth:ww1:12198 skipped: the sweep's time is up")]

    def test_skipped_acts_once_each_and_the_rows_they_test(self) -> None:
        run = RunFolder([], H_JOIN + self.H).run()
        got = [(label, kind, name) for label, kind, name, _l in lc.skipped_acts(run.clients["H"])]
        self.assertEqual(got, [("v19_1", "pickup", "SoulGem_lockjaw"), ("v22_1", "interact", "TreasureChest002")])
        head = lc.skipped_head(run.clients["H"])
        self.assertTrue(head[0].startswith("the sweep skipped 2 act(s) for time (H/game.log:"), head)
        self.assertIn("rows they would test: coins, loot, pickups", head[0])




class FiguresHead(unittest.TestCase):
    def test_the_page_names_each_players_figures(self) -> None:
        f = RunFolder([], H_JOIN, D_JOIN)
        (f.path / "H" / "script.txt").write_text("place 0 C:/x/figures/Roller Brawl.dump\nquit\n", encoding="utf-8")
        (f.path / "D" / "script.txt").write_text("place 0 C:/x/figures/Spy Rise Top.dump\n"
                                                 "place 1 C:/x/figures/Spy Rise Bottom.dump\nquit\n", encoding="utf-8")
        self.assertEqual(lc.figures_head(f.path),
                         ["figures: H Roller Brawl; D Spy Rise (a swapper: Spy Rise Top + Spy Rise Bottom)"])


class Objectives(unittest.TestCase):
    """W22: S's objective rows against each client's applies, per generation and objective (the objectives' author's
    grading)."""
    LIST = "Online: objective list 3 tracker b84c05cc gen 2"
    SHARED = "Online: objectives shared for the session (S's online_shared_objectives, in its rows)"

    def run_of(self, s: list, h: list, d: list) -> lc.Run:
        return RunFolder([(20, self.LIST)] + s, H_JOIN + [(21, self.LIST), (21.5, self.SHARED)] + h,
                         D_JOIN + [(41, self.LIST), (41.5, self.SHARED)] + d).run()

    def test_equal_sequences_pass(self) -> None:
        s = [(50, "Online: objective 00000000 0/1 (S) active"), (80, "Online: objective 00000000 1/1 (S) active")]
        c = [(50.1, "Online: objective applied 00000000 0/1 (active)"), (80.2, "Online: objective applied 00000000 1/1 (active)")]
        v = lc.grade_objectives(self.run_of(s, c, c))
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("generation 2 objective 00000000: S ['0/1 active', '1/1 active'], H ['0/1 active', '1/1 active']",
                      v.notes[0])

    def test_a_missing_step_an_own_run_and_a_missing_cancel_fail(self) -> None:
        s = [(50, "Online: objective 00000000 0/1 (S) active"), (60, "Online: objective 00000000 0/1 (S)")]
        h = [(50.1, "Online: objective applied 00000000 0/1 (active)"), (60.1, "Online: objective applied 00000000 0/1"),
             (55, "Online: objective 00000000 own start run here (S showed nothing in 3000 ms: this game's alone)")]
        d = [(50.1, "Online: objective applied 00000000 0/1 (active)")]
        v = lc.grade_objectives(self.run_of(s, h, d))
        self.assertEqual(v.status, lc.FAIL)
        whats = [f.what for f in v.findings]
        self.assertIn("H ran its own start of objective 00000000, for which S has a row in generation 2", whats)
        self.assertTrue(any(w.startswith("D's applies differ from S's rows: generation 2 objective 00000000: S ['0/1 "
                                         "active', '0/1'], D ['0/1 active']") for w in whats), whats)
        self.assertTrue(any(w.startswith("H: S's objective 00000000 went inactive") for w in whats), whats)

    def test_off_is_not_tested(self) -> None:
        v = lc.grade_objectives(RunFolder([(20, self.LIST)], H_JOIN, D_JOIN).run())
        self.assertEqual(v.status, lc.NOT_TESTED)
        self.assertIn("online_shared_objectives is off", v.counts)


class RouteHead(unittest.TestCase):
    def test_no_floor_and_a_rescue_are_named(self) -> None:
        # W24 (batch 13 case 7): the rail-ride end trigger hangs in the air; a hero stuck twice is taken out.
        h = [(60, "Input script: LEVELPASS visit v100 objects 1 (CScriptTriggerEntityData001) ref auth:tutorial_b:8394"),
             (61, "Dev: route teleport goto_entity failed (no floor: no ground a player can stand on within 600 of it "
                  "(the nearest at 26890,23620,180, 1148 away): a trigger in the air) (target CScriptTriggerEntityData001#2561)"),
             (90, "Dev: route held too long at the same spot twice (28013.73,23385.52,-500.00): taken out to the last "
                  "ground it stood on 14176.00,21295.00,40.00")]
        f = RunFolder([], H_JOIN + h, D_JOIN)
        head = lc.route_head(f.path)
        self.assertTrue(head[0].startswith("not visited on H: no floor (a trigger in the air): v100 "
                                           "CScriptTriggerEntityData001#2561 (no floor: no ground a player can stand on"),
                        head)
        page = lc.page(f.path, [], [])
        self.assertIn("  - CScriptTriggerEntityData001: no ground a player can stand on near it (a target in the air): "
                      "the level reaches it another way (a ride, a launch, a script that moves it) the pass does not take; not "
                      "covered by the pass",
                      page)
        self.assertTrue(head[1].startswith("H held too long at the same spot twice, 1 time(s): at "
                                           "28013.73,23385.52,-500.00: taken out to the last ground it stood on "
                                           "14176.00,21295.00,40.00"), head)


class LaunchRide(unittest.TestCase):
    def test_a_trigger_in_the_air_reached_by_the_ride(self) -> None:
        # W27b: the rail-ride trigger refused "no floor" by its own step, then reached by EpicRailpad001's launch.
        h = [(60, "Input script: LEVELPASS visit v100 objects 1 (CScriptTriggerEntityData001) ref auth:tutorial_b:8394"),
             (61, "Dev: route teleport goto_entity failed (no floor: no ground a player can stand on within 600 of it "
                  "(the nearest at 1,2,3, 2583 away): a trigger in the air) (target CScriptTriggerEntityData001#2561)"),
             (90, "Dev: launch pad (null)#2421: launched (10 presses, 450 from the pad after 1.2 s)"),
             (95, "Online: cutscene run CScriptTriggerEntityData001#2561 starts here (players hidden: no)"),
             (110, "Dev: launch_pad (null)#2421 done (landed at 4,5,6 after 20.0 s, 3100 from the pad) after 30.0 s")]
        f = RunFolder([], H_JOIN + h, D_JOIN)
        head = lc.route_head(f.path)
        self.assertTrue(head[0].startswith("reached by the launch ride on H: CScriptTriggerEntityData001 (the ride of "
                                           "(null)#2421; H/game.log:"), head)
        self.assertFalse([x for x in head if x.startswith("not visited")], head)
        self.assertNotIn("CScriptTriggerEntityData001: no ground", lc.page(f.path, [], []))


class FollowRescue(unittest.TestCase):
    def test_the_page_names_the_rescue_and_the_put_backs(self) -> None:
        # W31, batch 15 case 7: D followed H over Cascade Glade's island spawner, the game put it back again and again.
        d = [(70, "Online: auto follow 1 to -5521,-2859,1484 from 2844,-5706,-70 (8961, teleport 2)"),
             (71, "Stuck falling: player put back after 3.03 s (limit 3.00 s)"),
             (72, "Stuck falling: player put back after 3.03 s (limit 3.00 s)"),
             (73, "Online: auto follow 1: rescued: put back 2 times since the move to -5521,-2859,1484; back to its "
                  "own last ground 2844,-5706,-10 (teleport 3); the follow leaves that spot until the target is 400 "
                  "away from it")]
        f = RunFolder([], H_JOIN, D_JOIN + d)
        head = lc.route_head(f.path)
        self.assertTrue(any(x.startswith("D rescued by the follow (put back twice after a move, taken to its own last "
                                         "ground), 1 time(s): from -5521,-2859,1484 to 2844,-5706,-10 (D/game.log:")
                            for x in head), head)
        self.assertTrue(any(x.startswith("D put back by the game 2 times (`Stuck falling: player put back`; the first "
                                         "D/game.log:") for x in head), head)


class Moves(unittest.TestCase):
    def test_off_is_not_tested(self) -> None:
        f = RunFolder([], H_JOIN, D_JOIN)
        self.assertEqual(lc.grade_moves(lc.load_run(f.path)).status, lc.NOT_TESTED)

    def test_attacks_states_and_a_held_block(self) -> None:
        # W32 (b): H's presses against D's body; one press never taken, an `input expired` during the held block.
        h = [(50, "Online: moves: capture on (hero 0x8a000000#2055)"),
             (51, "Online: press 1:1 slot 2 tick 3120 (announced)"),
             (52, "Online: hero states entered 0x8248A06D,0x11111111 (pose 100)"),
             (53, "Online: press 1:2 slot 2 tick 3130 (announced)")]
        d = [(60, "Online: moves: capture on (hero 0x8b000000#2084)"),
             (61, "Online: puppet 1 attack 1 taken at pose 101 (waited 40 ms)"),
             (62, "Online: puppet 1 states entered 0x11111111 (pose 104)"),
             (63, "Online: puppet 1 held 0x0 -> 0x800 (pose 110)"),
             (64, "Online: puppet 1 input expired (its last command arrived 600 ms ago)"),
             (65, "Online: puppet 1 held 0x800 -> 0x0 (pose 140)")]
        f = RunFolder([], H_JOIN + h, D_JOIN + d)
        v = lc.grade_moves(lc.load_run(f.path))
        self.assertEqual(v.status, lc.FAIL, v)
        self.assertIn("D shows H: attacks taken 1 of 2 pressed; states 1 of 1 matching", v.counts)
        whats = [x.what for x in v.findings]
        self.assertIn("D: 1 of H's attack presses never taken by its body", whats)
        self.assertIn("D: 1 `input expired` while H's button was held", whats)
        self.assertNotIn("NOT TESTED: block", v.counts)
        # W40: no body showed a block (no `input block`, no held 0x800): the page says block is not tested.
        f = RunFolder([], H_JOIN + h, D_JOIN + d[:3])
        v = lc.grade_moves(lc.load_run(f.path))
        self.assertTrue(v.counts.endswith(lc.BLOCK_NOT_TESTED), v.counts)


class AttacksStarted(unittest.TestCase):
    def test_counts_each_players_presses_by_the_servers_done_lines(self) -> None:
        # W40: S's `press <p>:<n> done: ... started yes|no (...)`, one per press; H is peer 1, D peer 2.
        s = [(30, "Online: press 1:1 done: 0 landed, 0 without damage, 0 kills, started yes (missed)"),
             (31, "Online: press 1:2 done: 0 landed, 0 without damage, 0 kills, started no (missed)"),
             (32, "Online: press 1:3 done: 1 landed, 0 without damage, 1 kills, started yes (landed)"),
             (33, "Online: press 2:1 done: 0 landed, 0 without damage, 0 kills, started no (missed)")]
        run = lc.load_run(RunFolder(s, H_JOIN, D_JOIN).path)
        self.assertEqual(lc.attacks_started_line(run),
                         "attacks started on the server: H (peer 1) 2 of 3, D (peer 2) 0 of 1")
        run = lc.load_run(RunFolder([(30, "Online: frames 60 fps 60.0 p50 16")], H_JOIN, D_JOIN).path)
        self.assertEqual(lc.attacks_started_line(run),
                         "attacks started on the server: no press done (no `press <p>:<n> done` line)")


class PlayerDamage(unittest.TestCase):
    def test_one_to_one_and_the_health_read_back(self) -> None:
        # W33: S's two hits on H's puppet, H's one hit by that enemy: one unmatched; H's drop read back by S.
        # W40: H (peer 1) has puppet 2 on S (peer 2's came first).
        s = [(20, "Online: puppet 1 for peer 2 spawned at 0,0,0 (generation 1)"),
             (21, "Online: puppet 2 for peer 1 spawned at 0,0,0 (generation 1)"),
             (70, "Online: sentinel guard refused a hit on sentinel/puppet entry 0 PeerPuppet_2#3829 force 0 damage "
                  "0x41200000 by Chompy#2201 a00000efb.930d1f89.79d73383@2 (sweep) (#1)"),
             (71, "Online: peer 1 vitals 190/200"),
             (90, "Online: sentinel guard refused a hit on sentinel/puppet entry 0 PeerPuppet_2#3829 force 0 damage "
                  "0x41200000 by Chompy#2201 a00000efb.930d1f89.79d73383@2 (sweep) (#2)")]
        h = [(70, "Online: hero hit by Chompy#2102 a00000efb.930d1f89.79d73383@2 (sweep, 10) (S's attack, played here) (#1)"),
             (71, "Online: own vitals 190/200 (was 200; hit by Chompy#2102 a00000efb.930d1f89.79d73383@2)")]
        f = RunFolder(s, H_JOIN + h, D_JOIN)
        v = lc.grade_player_damage(lc.load_run(f.path))
        self.assertEqual(v.status, lc.FAIL, v)
        self.assertIn("H: hit 1 time(s) (sweep 1); S counted 2 on its puppet, 1 matched one to one; health drops 1; "
                      "lowest health 190/200", v.counts)
        self.assertIn("shots not tested: no client made a deferred projectile", v.counts)
        self.assertEqual([x.what for x in v.findings],
                         ["H: 1 hit(s) S counted on its puppet with no hit on its hero by the same enemy within 1 s"])


class Unpublished(unittest.TestCase):
    def test_the_arena_spawns_s_never_published(self) -> None:
        # W34: the nested manager's spawns refused by S (its name cut at 96 characters): a finding naming it.
        s = [(30, 'Online: enemy spawn of a00000a85.0629f659.00001234@2 not published: the new entry is '
                  '"EA_Manager_W1_B_componentDataOverrides_archetype_CEncounterManagerComponentData_encounterAc", not '
                  '"EA_Manager_W1_B_componentDataOverrides_archetype_CEncounterManagerComponentData_encounterActTargets_0_'
                  'targetToActOn_actor_3"')]
        f = lc.unpublished_finding(lc.load_run(RunFolder(s, H_JOIN, D_JOIN).path))
        self.assertIsNotNone(f)
        self.assertTrue(f.what.startswith("S did not publish 1 enemy spawn line(s)"), f.what)
        self.assertIn("EA_Manager_W1_B_componentDataOverrides", f.what)


LEVELPASS_FOLDER = Path(r"C:/Users/daz/Documents/T3Code/SwapForceOnline-work/levelpass")


@unittest.skipUnless((LEVELPASS_FOLDER / "tutorial_b").is_dir(), "the levelpass folder is not here")
class FightGates(unittest.TestCase):
    def test_the_arena_waves_named_with_their_counts(self) -> None:
        # W34: waves 1 and 2 complete on S; the third (W1_B's nested manager) refused by S and never complete.
        nested = ("EA_Manager_W1_B_componentDataOverrides_archetype_CEncounterManagerComponentData_encounterActTargets_0_"
                  "target")
        s = [(100, "Online: encounter complete EA_Manager_W1#2421"),
             (130, "Online: encounter complete EA_Manager_W1_B#2422"),
             (131, f'Online: enemy spawn of a00000a85.0629f659.00001234@2 not published: the new entry is "x", not '
                   f'"{nested}_actor_0"'),
             (132, f'Online: enemy spawn of a00000a85.0629f659.00001235@2 not published: the new entry is "x", not '
                   f'"{nested}_actor_1"')]
        h = [(90, "Input script: LEVELPASS required r1 arena_gate TutorialBattleGate_01 (win its fight: ...)"),
             (99, "Dev: encounter_clear EA_Manager_W1#2421 finished: spawned 6/6, 9 bursts, 0 lowered; 0 rechecks"),
             (129, "Dev: encounter_clear EA_Manager_W1_B#2422 finished: spawned 6/6, 7 bursts, 0 lowered; 0 rechecks"),
             (300, f"Dev: encounter_clear {nested}#2430 time: running 1 alive 8 spawned 9/15, 40 bursts")]
        run = lc.load_run(RunFolder(s, H_JOIN + h, D_JOIN).path)
        v = lc.grade_fight_gates(run, lc.level_data(LEVELPASS_FOLDER, "tutorial_b"))
        self.assertEqual(v.status, lc.FAIL, v)
        self.assertIn("wave 1 (EA_Manager_W1): complete on S ", v.counts)
        self.assertIn("H's copy spawned 6/6 (finished)", v.counts)
        whats = [x.what for x in v.findings]
        self.assertTrue(whats[0].startswith("TutorialBattleGate_01 wave 3 (EA_Manager_W1_B_componentDataOverrides"), whats)
        self.assertIn("never complete on S; S refused 2 of its spawns (`not published`)", whats[0])
        self.assertIn("H's copy spawned 9/15 (not finished)", v.findings[0].lines[0])
        self.assertIn("TutorialBattleGate_01: never opened on S (its last wave never completed there)", whats)

    BATCH_16 = Path(r"D:/SwapForceOnline-data/tests/int2/runs/20261119-202440-fight-gate-tb")
    OWNER = Path(r"D:/SwapForceOnline-data/play/20261119-190053")

    @unittest.skipUnless(BATCH_16.is_dir(), "batch 16's fight-gate-tb run is not here")
    def test_batch_16_fight_gate_tb_passes_by_itself(self) -> None:
        # W35: a spec run has no level pass case: its level is the one H loaded, and the spec's grade passes.
        run = lc.load_run(self.BATCH_16)
        self.assertEqual(lc.run_level(run), "tutorial_b")
        v = lc.grade_fight_gates(run, lc.level_data(LEVELPASS_FOLDER, lc.run_level(run)))
        self.assertEqual(v.status, lc.PASS, [x.what for x in v.findings])
        self.assertIn("wave 3 (EA_Manager_W1_B_componentDataOverrides", v.counts)
        self.assertIn("TutorialBattleGate_01 opened on S at ", v.counts)
        import skytest
        self.assertEqual(skytest.level_check_reasons(self.BATCH_16, {"rows": ["fight gates"], "must_test": True},
                                                     LEVELPASS_FOLDER), [])

    @unittest.skipUnless(OWNER.is_dir(), "the owner's session of 19 November is not here")
    def test_the_owners_session_fails_on_wave_3(self) -> None:
        # W35: the owner fought by hand (no pass step): S started the managers; the third never completed.
        run = lc.load_run(self.OWNER)
        self.assertEqual(sorted(run.clients), ["D", "H"])
        v = lc.grade_fight_gates(run, lc.level_data(LEVELPASS_FOLDER, lc.run_level(run)))
        self.assertEqual(v.status, lc.FAIL, v)
        whats = [x.what for x in v.findings]
        self.assertTrue(whats[0].startswith("TutorialBattleGate_01 wave 3 (EA_Manager_W1_B_componentDataOverrides"), whats)
        self.assertIn("never complete on S", whats[0])
        self.assertIn("wave 1 (EA_Manager_W1): complete on S ", v.counts)


class LaunchRelayHead(unittest.TestCase):
    def test_the_head_says_whether_the_ride_was_relayed(self) -> None:
        f = RunFolder([], H_JOIN, D_JOIN)
        res = {"peers": [{"peer": "H", "command": ["x.exe", "--online_launch_relay=true"]}]}
        (f.path / "result.json").write_text(json.dumps(res), encoding="utf-8")
        self.assertEqual(lc.launch_relay_head(f.path), ["launch pad relay: on (online_launch_relay; off: a ride carries "
                                                        "the presser only)"])
        res["peers"][0]["command"] = ["x.exe"]
        (f.path / "result.json").write_text(json.dumps(res), encoding="utf-8")
        self.assertTrue(lc.launch_relay_head(f.path)[0].startswith("launch pad relay: off"))


class ServerAlive(unittest.TestCase):
    OWNER_2 = Path(r"D:/SwapForceOnline-data/play/20261119-211901")
    GATE_RUN = Path(r"D:/SwapForceOnline-data/tests/int2/runs/20261119-202440-fight-gate-tb")

    @unittest.skipUnless(OWNER_2.is_dir(), "the owner's second session of 19 November is not here")
    def test_the_owners_second_session_stopped_at_21_46_16(self) -> None:
        f = lc.server_stalled(lc.load_run(self.OWNER_2))
        self.assertIsNotNone(f)
        self.assertTrue(f.what.startswith("the server's game stopped at 21:46:16"), f.what)
        self.assertEqual(len(f.lines), 3)

    @unittest.skipUnless(GATE_RUN.is_dir(), "batch 16's fight-gate-tb run is not here")
    def test_the_batch_16_gate_run_has_no_finding(self) -> None:
        self.assertIsNone(lc.server_stalled(lc.load_run(self.GATE_RUN)))

    def test_the_fixs_own_lines_fail_it(self) -> None:
        # W38: S's own [error] lines (online/5-server-never-paused) fail the row whatever the threads did.
        s = [(10, "Online: the server's world has not run for 5.2 s with 2 client(s) connected: no player frame and no "
                  "held or results service frame"),
             (12, "Online: presses of peer 2 refused for 6.0 s (4 refused, the last 9: stale generation); none of its "
                  "attacks reaches the server's hit path")]
        fs = lc.server_hard_findings(lc.load_run(RunFolder(s, H_JOIN, D_JOIN).path))
        self.assertEqual([f.what.split(" (")[0].split(":")[0] for f in fs],
                         ["the server's game stopped", "the server refused the presses of peer 2 for a while"])
        # W39: the level entry's [error], and a recovery press nobody asked for.
        s = [(10, "Online: the server's level entry has made no progress for 31.0 s with 2 client(s) connected: x"),
             (12, "Online: the server's world is paused: pressed A once on its own screen (press 1 of 3, x)")]
        fs = lc.server_hard_findings(lc.load_run(RunFolder(s, H_JOIN, D_JOIN).path))
        self.assertEqual([f.what.split(" (")[0] for f in fs],
                         ["the server's game stopped", "the server pressed A on its own screen without "
                          "--online_server_unstick=true"])

    def test_a_server_that_works_to_the_end_passes(self) -> None:
        s = [(10, "Online: world 2 a00000000.0629f659.00012836@2 written"), (19, "Online: frames 599 fps 59.9 p50 16"),
             (20, "Online: traffic peer 1 gameplay 1/1 state 1/1 fast 1/1 bytes")]
        self.assertIsNone(lc.server_stalled(lc.load_run(RunFolder(s, H_JOIN, D_JOIN).path)))

class Windows(unittest.TestCase):
    def test_a_clients_time_in_each_level(self) -> None:
        f = RunFolder([], H_JOIN + LEVEL_END, D_JOIN + LEVEL_END)
        r = f.run()
        h, d = lc.client_windows(r.clients["H"]), lc.client_windows(r.clients["D"])
        self.assertEqual(sorted(h), [1, 2, 3])
        self.assertEqual((h[2].start.second, h[2].end.minute, h[2].end.second), (20, 13, 20))  # cut at the level end
        self.assertEqual(d[2].start.second, 40)  # joined late, into level 2
        self.assertEqual(lc.peer_numbers(r), {1: "H", 2: "D"})


class Enemies(unittest.TestCase):
    def grade(self, s, h, d=None) -> lc.Verdict:
        return lc.grade_enemies(RunFolder(s, H_JOIN + h + LEVEL_END, None if d is None else D_JOIN + d + LEVEL_END).run())

    def test_attached_and_deaths_seen_pass(self) -> None:
        v = self.grade([(30, f"Online: enemy placed {K1} as Chompy#3981 hp 1 pose 26 (S)"),
                        (60, f"Online: enemy {K1} dead by 1 (S, placed; attacker PeerPuppet_1#3829)")],
                       [(31, f"Online: enemy spawn {K1} as Chompy#b848ac60 (bind placed)"),
                        (61, f"Online: enemy life {K1} alive -> dead")],
                       [(45, f"Online: enemy spawn {K1} as Chompy#b884c740 (spawn other)"),
                        (45, f"Online: enemy bind {K1} (resolved)"),
                        (45, f"Online: enemy replica {K1} at b8 asleep (mID 0x1): the binding waits for its wake"),
                        (61, f"Online: enemy life {K1} alive -> dead (its copy made here was asleep: removed)")])
        self.assertEqual(v.status, lc.PASS, v)
        self.assertIn("H: bound 1 of 1, waiting asleep 0", v.counts)
        self.assertIn("deaths 1: 1 seen on a bound copy, 0 dead before bind", v.counts)
        self.assertIn("H/data/Online/session/game.log:1:", v.example)

    def test_never_attached_never_found_and_a_lost_death(self) -> None:
        v = self.grade([(30, f"Online: enemy placed {K1} as Chompy#1 hp 1 pose 1 (S)"),
                        (30, f"Online: enemy placed {K2} as Chompy002#2 hp 1 pose 2 (S)"),
                        (31, f"Online: enemy run-time spawn {K3} as CrashSP_01_Spawned1#3 hp 45 pose 3 source 1 (S)"),
                        (90, f"Online: enemy {K3} dead by 1 (S, placed; attacker PeerPuppet_1#9)")],
                       [(36, f"Online: enemy bind {K2} (pending past grace: missing)"),
                        (32, f"Online: enemy spawn {K3} as CrashSP_01_Spawned1#b8 (spawn other)")])
        whats = sorted(f.what.split(" (")[0] for f in v.findings)
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(whats, [f"H never attached {K1}", f"H never found {K2}", f"H never saw {K3}"])
        lost = next(f for f in v.findings if "never saw" in f.what)
        self.assertTrue(lost.lines[0].startswith("H/data/Online/session/game.log:4:"), lost.lines)
        self.assertTrue(lost.lines[1].startswith("H/game.log:"), lost.lines)

    def test_a_pending_bind_resolved_later_is_attached(self) -> None:
        v = self.grade([(30, f"Online: enemy placed {K1} as Chompy#1 hp 1 pose 1 (S)")],
                       [(35, f"Online: enemy bind {K1} (pending past grace: missing)"),
                        (50, f"Online: enemy spawn {K1} as Chompy#b8 (bind placed)"),
                        (50, f"Online: enemy bind {K1} (resolved)")])
        self.assertEqual(v.status, lc.PASS, v.findings)

    def test_dead_here_only(self) -> None:
        v = self.grade([(30, f"Online: enemy placed {K1} as Chompy#1 hp 1 pose 1 (S)")],
                       [(31, f"Online: enemy spawn {K1} as Chompy#b8 (bind placed)"),
                        (70, f"Online: enemy life {K1} alive -> dead")])
        self.assertEqual([f.what.split(":")[1].strip()[:25] for f in v.findings], [f"{K1} (Chompy#1) dead"[:25]])

    def test_what_a_client_could_not_see_is_not_graded(self) -> None:
        # Placed 4 s before H's level end (the results screen pauses the game), and streamed out before D came.
        v = self.grade([(196, f"Online: enemy placed {K1} as Finale#1 hp 1 pose 1 (S)"),
                        (25, f"Online: enemy placed {K2} as Ambi#2 hp 1 pose 2 (S)"),
                        (30, f"Online: enemy {K2} streamed out on the server: no end published (its row retired)")],
                       [(26, f"Online: enemy spawn {K2} as Ambi#b8 (bind placed)"),
                        (31, f"Online: enemy retired {K2} (streamed out on the server)")], [])
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("D: bound 0 of 0", v.counts)

    def test_no_enemy_is_not_a_pass(self) -> None:
        self.assertEqual(self.grade([], []).status, lc.NOT_TESTED)


class EnemyKinds(unittest.TestCase):
    """Bound, waiting asleep, never existed here; deaths seen on a bound copy, dead before bind (the confirm
    agent's tutorial_b by hand: 20261119-033620)."""

    def grade(self, s, h, d=None) -> lc.Verdict:
        return lc.grade_enemies(RunFolder(s, H_JOIN + h + LEVEL_END, None if d is None else D_JOIN + d + LEVEL_END).run())

    def test_asleep_far_away_is_no_fault_and_dead_before_bind_is_no_death_seen(self) -> None:
        v = self.grade([(30, f"Online: enemy placed {K1} as GreebleLobber#3887 hp 45 pose 12 (S)"),
                        (30, f"Online: enemy placed {K2} as Chompy#3889 hp 1 pose 14 (S)"),
                        (60, f"Online: enemy {K1} dead by 1 (S, placed; attacker PeerPuppet_2#3833)")],
                       [(31, f"Online: enemy spawn {K1} as GreebleLobber#b8487e80 (bind placed)"),
                        (31, f"Online: enemy bind {K1} (resolved)"),
                        (31, f"Online: enemy spawn {K2} as Chompy#b848ac60 (bind placed)"),
                        (31, f"Online: enemy bind {K2} (resolved)"),
                        (61, f"Online: enemy life {K1} alive -> dead")],
                       [(45, f"Online: enemy bind {K1} by authored id 0629f659.1.12338 waits (asleep here, its copy "
                             "at b8487e80): never counted missing"),
                        (45, f"Online: enemy bind {K2} by authored id 0629f659.1.12375 waits (asleep here, its copy "
                             "at b848ac60): never counted missing"),
                        (61, f"Online: enemy skipped {K1} (dead before bind)")])
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("D: bound 0 of 2, waiting asleep 2 (its copy asleep, far from this player: not a fault by itself), "
                      "never existed here 0; deaths 1: 0 seen on a bound copy, 1 dead before bind", v.counts)
        self.assertIn("H: bound 2 of 2, waiting asleep 0", v.counts)
        # S's puppet name is no player number: the kill is the player of `dead by <peer>` (peer 1, H).
        self.assertIn("by a player: H 1, D 0", v.counts)

    def test_a_bound_copy_that_never_saw_the_death_fails(self) -> None:
        v = self.grade([(30, f"Online: enemy placed {K1} as Chompy#1 hp 1 pose 1 (S)"),
                        (60, f"Online: enemy {K1} dead by 1 (S, placed; attacker PeerPuppet_1#9)")],
                       [(31, f"Online: enemy bind {K1} (resolved)")])
        self.assertEqual([x.what.split(":")[0] for x in v.findings], [f"H never saw {K1} (Chompy#1) die"])

    def test_an_enemy_on_no_client_is_named(self) -> None:
        v = self.grade([(30, f"Online: enemy run-time spawn {K3} as BomberSpawner_02_Spawned3#3951 hp 78 pose 22 "
                             "source e1d82dd6 (CActorData at depth 2, 11 nodes) (S)"),
                        (42, f"Online: enemy {K3} dead by 1 (S, placed; attacker PeerPuppet_2#3833)")],
                       [(42, f"Online: enemy skipped {K3} (dead before bind)")],
                       [(42, f"Online: enemy skipped {K3} (dead before bind)")])
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.findings[0].what, f"existed on no client: {K3} (BomberSpawner_02_Spawned3#3951)")
        self.assertIn("on the clients: H dead before bind (never existed here), D dead before bind (never existed "
                      "here)", v.findings[0].lines[-1])

    def test_a_death_with_no_attacker_right_after_its_spawn(self) -> None:
        # The tutorial_a ship greebles (20261119-033102): made, fell, died with no attacker 2.5 s later.
        v = self.grade([(30, f"Online: enemy run-time spawn {K3} as GreebleSpawner_03_SpawnedInstance1#2408 hp 78 pose "
                             "29 source c927708a (a clone of  at depth 2, 6 nodes) (S)"),
                        (32.5, f"Online: enemy {K3} dead by none (S, placed; attacker -)"),
                        (32.5, f"Online: enemy {K3} died with no attacker at -442,-58676,64 (made at -112,-57191,1014; "
                               "kill height -5000)")],
                       [(32.6, f"Online: enemy skipped {K3} (dead before bind)")])
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.findings[0].what, "1 enemy died on S with no attacker within 10 s of its spawn (1 reached "
                                             "no client)")
        self.assertIn("(2.50 s after its spawn)", v.findings[0].lines[0])
        self.assertIn("died with no attacker at -442,-58676,64", v.findings[0].lines[1])
        self.assertEqual(len(v.findings), 1)  # not named twice as `existed on no client`

    def test_a_spawn_slept_on_s_before_any_copy_is_set_apart(self) -> None:
        # Batch 11 tutorial_b 20261119-061110: CrashSP_04_Spawned1 slept on S 3.04 s after its spawn, far from both
        # players; both clients only retired it. Not graded; a spawn a client had in its interest still is.
        s = [(30, f"Online: enemy run-time spawn {K3} as CrashSP_04_Spawned1#3956 hp 45 pose 22 source e1 (CActorData) (S)"),
             (33.04, f"Online: enemy {K3} streamed out on the server: no end published (its row retired)")]
        retired = [(33.06, f"Online: enemy retired {K3} (streamed out on the server)")]
        v = self.grade(s, retired, retired)
        self.assertEqual(v.status, lc.NOT_TESTED, v.findings)  # nothing else to grade in this made-up run
        self.assertIn("gone to sleep on the server before any player's game made it (not graded): "
                      "CrashSP_04_Spawned1#3956", v.notes[-1])
        seen = [(30.5, f"Online: enemy bind {K3} (pending past grace: missing)")] + retired
        v = self.grade(s, seen, retired)
        self.assertEqual([x.what.split(":")[0] for x in v.findings], [f"H never found {K3} (CrashSP_04_Spawned1#3956)"])

    def test_a_death_by_another_entity_is_counted(self) -> None:
        # ww1 20261119-051557: `dead by none (S, placed; attacker (null)#3457)`, the seventh death the counts missed.
        v = self.grade([(30, f"Online: enemy run-time spawn {K3} as Sawmouth_Spawned1#3 hp 45 pose 3 source 1 (S)"),
                        (60, f"Online: enemy {K3} dead by none (S, placed; attacker (null)#3457)")],
                       [(31, f"Online: enemy spawn {K3} as Sawmouth_Spawned1#b8 (spawn other)"),
                        (61, f"Online: enemy life {K3} alive -> dead")])
        self.assertIn("1 died on S (by a player: H 0; by another entity 1 ((null)#3457); no attacker 0; attacker not "
                      "recorded 0)", v.counts)

    def test_an_encounter_spawn_counts_and_its_unnamed_attacker_is_no_finding(self) -> None:
        # tutorial_a's encounter actors: `enemy spawn <key> ... (S)`, dead `by none (S)` with no attacker field
        # (a player's kill may read so: one died 4 ms after H's press started on S).
        enc = "scbde1a85.00000001#1/0@2"
        v = self.grade([(30, f"Online: enemy spawn {enc} as ECI_Manager_actor_0 payload 0 point 0 pose 1 (S)"),
                        (34.7, f"Online: enemy {enc} dead by none (S)")],
                       [(30.1, f"Online: enemy spawn {enc} as ECI_Manager_actor_0#b78d1070 (live)"),
                        (30.1, f"Online: enemy bind {enc} (resolved)"),
                        (34.8, f"Online: enemy life {enc} alive -> dead")])
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("S 1 (0 placed, 1 encounter, 0 run-time), 1 died on S (by a player: H 0; by another entity 0; no attacker 0; "
                      "attacker not recorded 1)", v.counts)


class RequiredSteps(unittest.TestCase):
    """The pass teleports: a level's end reached past gates a player must open is "completable by teleport",
    never a bare PASS (levelpass_holds.required_gates)."""

    def data(self) -> lc.LevelData:
        import levelpass_holds as lh
        return lc.LevelData("madeup", {}, [lh.Gate("Intro_LockpickGate", "puzzles", lh.SPARK_LOCK, True, "solve its lock puzzle"),
                                           lh.Gate("Finale_DemonGate", "enc8", lh.BATTLE_GATE, True, "win its fight"),
                                           lh.Gate("ElementalGate", "ez1", "an elemental gate", False, "")])

    PASSED = {"levelpass": {"verdict": "PASS", "not_covered": []}}

    def test_not_performed_names_the_first_step_and_a_battle_gate_opened_everywhere(self) -> None:
        h = [(60, "Input script: LEVELPASS required r1 Spark_Lock Intro_LockpickGate not performed: needs a lock opened: "
                  "this build has no lock_solve / lock_ready (online/6-spark-lock is not in it)"),
             (70, "Input script: LEVELPASS required r2 battle_gate Finale_DemonGate (win its fight: Wave1)"),
             (91, "Online: world gate Finale_DemonGate#2365 open apply: 1 lock(s) set UnLocked first (x)")]
        d = [(92, "Online: world gate Finale_DemonGate#2370 open apply: 1 lock(s) set UnLocked first (x)")]
        run = RunFolder([(90, "Online: world gate Finale_DemonGate#2365 tombstoned (removed for good, written)")],
                        H_JOIN + h + H_NEXT, D_JOIN + d + D_NEXT, result=self.PASSED).run()
        v = lc.grade_completable(run, self.data())
        self.assertEqual(v.status, lc.BY_TELEPORT)
        self.assertTrue(v.counts.startswith("completable by teleport; first step not done: Intro_LockpickGate (Spark Lock): "
                                            "not performed: needs a lock opened"), v.counts)
        self.assertIn("required steps not done: 1 of 2", v.counts)
        self.assertTrue(any(n.startswith("required, done: Finale_DemonGate (battle gate): opened on S, H, D: S ")
                            for n in v.notes), v.notes)
        self.assertEqual(lc.overall([v, lc.Verdict("lobby", lc.PASS, "")]), "BY TELEPORT: completable")
        self.assertEqual(lc.overall([v, lc.Verdict("loot", lc.NOT_TESTED, "")]), "NOT ALL TESTED: loot; BY TELEPORT: completable")

    def test_by_play_when_every_gate_opened_on_all_three(self) -> None:
        import levelpass_holds as lh
        data = lc.LevelData("madeup", {}, [lh.Gate("Finale_DemonGate", "enc8", lh.BATTLE_GATE, True, "win its fight")])
        h = [(70, "Input script: LEVELPASS required r1 battle_gate Finale_DemonGate (win its fight: Wave1)"),
             (91, "Dev: wait_moved Finale_DemonGate#2365 done (gone after moving 300 from 1,2,3)")]
        d = [(92, "Online: world gate Finale_DemonGate#2370 open apply: 1 lock(s) set UnLocked first (x)")]
        run = RunFolder([(90, "Online: availability Finale_DemonGate#2365 removed (script) bd35d565.1.6496")],
                        H_JOIN + h + H_NEXT, D_JOIN + d + D_NEXT, result=self.PASSED).run()
        v = lc.grade_completable(run, data)
        self.assertEqual(v.status, lc.PASS, v.counts)
        self.assertTrue(v.counts.startswith("completable by play: 1 required step(s) performed, every gate opened on S, H, D"))

    def test_no_gates_says_the_gates_were_not_attempted(self) -> None:
        h = [(60, "Input script: LEVELPASS gates not attempted (--no-gates)")]
        run = RunFolder([], H_JOIN + h + H_NEXT, D_JOIN + D_NEXT, result=self.PASSED).run()
        v = lc.grade_completable(run, self.data())
        self.assertEqual(v.status, lc.BY_TELEPORT)
        self.assertTrue(v.counts.startswith("completable by teleport; gates not attempted (--no-gates); required steps "
                                            "not performed: 2;"), v.counts)

    def test_a_carried_key_says_where_it_broke(self) -> None:
        import levelpass_holds as lh
        data = lc.LevelData("madeup", {}, [lh.Gate("TutorialLockedGate", "a2", lh.KEY_GATE, True, "fetch its key K",
                                                   key_data="LockedDoorKey_Intro")])
        h = [(60, "Input script: LEVELPASS required r1 key_gate TutorialLockedGate (fetch its key LockedDoorKey_Intro)"),
             (70, "Dev: pickup TutorialLockedGate_key#2609 done (carried by the hero (Hero#1 12 from it))"),
             (80, "Dev: interact TutorialLockedGate_keylock#2608 done (fired by a tap of rt; state 5 (interacted), player 0)"),
             (85, "Dev: wait_moved TutorialLockedGate#2607 done (settled at 1,2,3, 120 from 1,2,3 (most 120), z +0)")]
        s = [(20, "Online: world unsupported carried_item TutorialLockedGate_key#2609 (spawned without a stable key)")]
        run = RunFolder(s, H_JOIN + h + H_NEXT, D_JOIN + D_NEXT, result=self.PASSED).run()
        v = lc.grade_completable(run, data)
        self.assertEqual(v.status, lc.BY_TELEPORT)
        first = v.counts.split("first step not done: ")[1]
        self.assertTrue(first.startswith("TutorialLockedGate (key gate): attempted by the pass ("), first)
        self.assertIn("opened on H at 00:11:25.000, not on S, D; key touched and following on H: H/game.log:", first)
        self.assertIn("; S for the key: H/data/Online/session/game.log:1: Online: world unsupported carried_item", first)
        self.assertIn("; no held press at the lock on H (", first)
        self.assertIn("; interact on H: H/game.log:", first)
        self.assertIn("; D's own key: no line after its level start", first)

    def test_s_opening_by_a_press_counts_on_s_and_a_press_on_an_earlier_gate_is_named(self) -> None:
        # W9/W10 (tutorial_b 20261119-065111): S writes no `world gate ... open apply`; its own `gate.open applied
        # <h> key <k>`, named by H's `backend press held <Name#id> key <k>`, is S's opening. Step 2 pressed gate 1.
        import levelpass_holds as lh
        g = lh.Gate("TutorialLockedGate", "a2", lh.KEY_GATE, True, "fetch its key K", key_data="LockedDoorKey_Intro")
        data = lc.LevelData("madeup", {}, [g, lh.Gate("TutorialLockedGate", "a4", lh.KEY_GATE, True, "fetch its key K2",
                                                      key_data="LockedDoorKey_2")])
        h = [(60, "Input script: LEVELPASS required r1 key_gate TutorialLockedGate (fetch its key LockedDoorKey_Intro)"),
             (61, "Online: backend press held TutorialLockedGate#2607 key 1/1/2/29"),
             (64, "Online: world gate TutorialLockedGate#2607 open apply: 1 lock(s) set UnLocked first (x)"),
             (70, "Input script: LEVELPASS required r2 key_gate TutorialLockedGate (fetch its key LockedDoorKey_2)"),
             (75, "Online: backend press held TutorialLockedGate#2607 key 1/1/2/43")]
        d = [(62, "Online: world gate TutorialLockedGate#2636 open apply: 1 lock(s) set UnLocked first (x)")]
        s = [(61.1, "Online: backend gate.open run b838f920 key 1/1/2/29"),
             (61.2, "Online: backend gate.open applied b838f920 key 1/1/2/29"),
             (75.1, "Online: backend gate.open refused peer 1 a00000a2f.2d1accae.ada5591b@2 (kind already_open; row 1)")]
        v = lc.grade_completable(RunFolder(s, H_JOIN + h + H_NEXT, D_JOIN + d + D_NEXT, result=self.PASSED).run(), data)
        self.assertEqual(v.status, lc.BY_TELEPORT)
        done = [n for n in v.notes if n.startswith("required, done: ")]
        self.assertEqual(len(done), 1, v.notes)
        self.assertIn("opened on S, H, D: S at ", done[0])
        self.assertIn("Online: backend gate.open applied b838f920 key 1/1/2/29", done[0])
        first = v.counts.split("first step not done: ")[1]
        self.assertIn("opened on no game, not on S, H, D", first)
        self.assertIn("S's decision: H/data/Online/session/game.log:3: Online: backend gate.open refused", first)
        self.assertIn("the press went to TutorialLockedGate#2607, the gate an earlier step pressed", first)

    def test_a_battle_gate_opens_by_its_lock_state_on_each_game(self) -> None:
        # W17 (batch 12 case 7): S writes the gate's row again when it opens, each client logs the lock unlocked
        # under S's reference; D's own entity id differs (0x95a against S's 0x93d), its own authored line names it.
        import levelpass_holds as lh
        data = lc.LevelData("madeup", {}, [lh.Gate("Finale_DemonGate008", "e8", lh.BATTLE_GATE, True, "win its fight")])
        auth = "Online: authored obj bd35d565 1 6496 class ada5591b entity 1 name 4f8d6c1f name0 811c9dc5 mid {} state " \
               "registered flags 28000000 act 1 pos -4074.10 4190.17 733.27 text Finale_DemonGate008"
        row = "Online: world 2 a0000093d.4f8d6c1f.ada5591b@2 written"
        unlocked = "Online: backend key gate a0000093d.4f8d6c1f.ada5591b@2 watched b8146710 (unlocked Scripts.DemonLock=3)"
        locked = "Online: backend key gate a0000093d.4f8d6c1f.ada5591b@2 watched b8146710 (locked Scripts.DemonLock=2)"
        s = [(10, auth.format("93d")), (11, row), (90, row)]
        h = [(10, auth.format("93d")), (70, "Input script: LEVELPASS required r1 battle_gate Finale_DemonGate008 (x)"),
             (89.5, locked), (90.4, unlocked)]
        d = [(10, auth.format("95a")), (89.4, locked), (90.3, unlocked)]
        v = lc.grade_completable(RunFolder(s, H_JOIN + h + H_NEXT, D_JOIN + d + D_NEXT, result=self.PASSED).run(), data)
        self.assertEqual(v.status, lc.PASS, v.notes)
        done = [n for n in v.notes if n.startswith("required, done: ")][0]
        self.assertIn("opened on S, H, D: S at ", done)
        self.assertIn(row, done)
        self.assertIn("D at ", done)
        self.assertNotIn("wait_moved", done)

    def gate_run(self, h_pass: str, d_pass: str, d_pass_t: float) -> lc.Verdict:
        import levelpass_holds as lh
        data = lc.LevelData("madeup", {}, [lh.Gate("Finale_DemonGate008", "e8", lh.BATTLE_GATE, True, "win its fight")])
        auth = "Online: authored obj bd35d565 1 6496 class ada5591b entity 1 name 4f8d6c1f name0 811c9dc5 mid {} state " \
               "registered flags 28000000 act 1 pos -4074.10 4190.17 733.27 text Finale_DemonGate008"
        row = "Online: world 2 a0000093d.4f8d6c1f.ada5591b@2 written"
        unlocked = "Online: backend key gate a0000093d.4f8d6c1f.ada5591b@2 watched b8146710 (unlocked Scripts.DemonLock=3)"
        s = [(10, auth.format("93d")), (11, row), (90, row)]
        h = [(10, auth.format("93d")), (70, "Input script: LEVELPASS required r1 battle_gate Finale_DemonGate008 (x)"),
             (90.4, unlocked), (95, f"Dev: pass_gate Finale_DemonGate008#2365 {h_pass}")]
        d = [(10, auth.format("95a")), (90.3, unlocked), (d_pass_t, f"Dev: pass_gate Finale_DemonGate008#2394 {d_pass}")]
        return lc.grade_completable(RunFolder(s, H_JOIN + h + H_NEXT, D_JOIN + d + D_NEXT, result=self.PASSED).run(), data)

    def test_a_gate_passed_on_foot_by_both(self) -> None:
        ok = "done (passed on foot: from 1,2,3 to 4,5,6, 240 beyond the gate's plane after 1 walk(s) (arrived)) after 6.0 s"
        v = self.gate_run(ok, ok, 96)
        self.assertEqual(v.status, lc.PASS, v.notes)
        done = [n for n in v.notes if n.startswith("required, done: ")][0]
        self.assertIn("; passed on foot by H at 00:11:35.000, D at 00:11:36.000", done)

    def test_open_but_not_passable_and_a_walk_before_the_open(self) -> None:
        ok = "done (passed on foot: from 1,2,3 to 4,5,6, 240 beyond the gate's plane after 1 walk(s) (arrived)) after 6.0 s"
        stuck = ("failed (not passed on foot: stopped at 1,2,3, 41 before the gate's plane (from 0,0,0 toward 9,9,9; 30 "
                 "walk(s), the last stuck)) after 60.0 s")
        v = self.gate_run(ok, stuck, 97)
        self.assertEqual(v.status, lc.BY_TELEPORT)
        first = v.counts.split("first step not done: ")[1]
        self.assertTrue(first.startswith("Finale_DemonGate008 (battle gate): opened on S, H, D but gate open but not "
                                         "passable on D: not passed on foot: stopped at 1,2,3"), first)
        self.assertIn("passed on foot by H at 00:11:35.000", first)
        early = self.gate_run(ok, stuck, 80)  # D's walk ended before the gate opened on D: not counted either way
        self.assertEqual(early.status, lc.PASS, early.notes)
        self.assertIn("D's walk ended before the gate opened there", [n for n in early.notes if "done" in n][0])

    def test_no_level_data_is_said(self) -> None:
        run = RunFolder([], H_JOIN + H_NEXT, D_JOIN + D_NEXT, result={"levelpass": {"verdict": "PASS", "not_covered": []}}).run()
        v = lc.grade_completable(run, None)
        self.assertEqual(v.status, lc.PASS)
        self.assertIn("required steps on the way not checked (no level data for this run)", v.counts)

    def test_a_fight_on_a_held_enemy_names_its_hold_and_release(self) -> None:
        import levelpass_holds as lh
        hold = lh.Hold("intro.GreebleMelee", "GreebleMelee", "intro", (-535.0, -2874.6, 303.8), "job state Idle",
                       [lh.Release("intro.Scene_Manager", lh.LOCK, "puzzles.IntroArea_LockpickGate",
                                   ["IntroArea_LockpickGate", "Scene_Manager"], 0.5)])
        data = lc.LevelData("madeup", {hold.key: hold}, [])
        fg = {"area": "-535,-2875,303", "tries": [lc.Line("H/game.log", 9, "Dev: fight_area -535,-2875,303 try 1: x at "
                                                           "GreebleMelee#3531 d 72: hp 96 -> 96 (2 enemies)", None)]}
        lines = lc.held_lines(RunFolder([], H_JOIN).run(), fg, data)
        self.assertIn("GreebleMelee: held (job state Idle); released by Scene_Manager (IntroArea_LockpickGate -> "
                      "Scene_Manager; a player: open the lock IntroArea_LockpickGate)", lines)
        self.assertIn("    IntroArea_LockpickGate: no line shows it opened: not fired in this run", lines)


class Together(unittest.TestCase):
    """With the follow on, each stop where H acted says whether D stood within the follow distance of H on S's
    poses (the puppet of each peer from `puppet <n> for peer <p> spawned`, swapped at a level change)."""
    S = [(20, "Online: puppet 2 for peer 1 spawned at 0,0,0 (generation 2)"),
         (20, "Online: puppet 1 for peer 2 spawned at 0,0,0 (generation 2)"),
         (61, "Online: puppet 2 pose 10 at 1000.00,0.00,0.00 client at 1000.00,0.00,0.00 off 0.00 age 30 ms"),
         (61.5, "Online: puppet 1 pose 11 at 1100.00,50.00,0.00 client at 1100.00,50.00,0.00 off 0.00 age 30 ms"),
         (91, "Online: puppet 2 pose 20 at 9000.00,0.00,0.00 client at 9000.00,0.00,0.00 off 0.00 age 30 ms"),
         (91.5, "Online: puppet 1 pose 21 at 1100.00,50.00,0.00 client at 1100.00,50.00,0.00 off 0.00 age 30 ms")]
    H = [(60, "Input script: LEVELPASS act v3_1 fight Greeble CActor ref auth:x:1"),
         (90, "Input script: LEVELPASS act v4_1 attack Crate CPhysicalEntity ref auth:x:2")]

    def test_near_and_apart_per_stop(self) -> None:
        d = [(45, "Online: auto follow the host (peer 1) (script)")]
        v = lc.grade_together(RunFolder(self.S, H_JOIN + self.H + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.counts, "D near H (within 400 on S's poses) at 1 of 2 stop(s) where H acted; apart at 1")
        self.assertEqual(v.notes, ["near: v3 (112)"])
        self.assertTrue(v.findings[0].what.startswith("v4: D was 7900 from H while H acted"))

    def test_a_follow_with_nothing_to_read_is_not_called_no_follow(self) -> None:
        # Batch 11 run 3: D followed H, H hung before any stop; the rows said "no follow in this run".
        d = [(45, "Online: auto follow the host (peer 1) (script)")]
        rows = lc.grade(RunFolder(self.S, H_JOIN + LEVEL_END, D_JOIN + d + LEVEL_END).path, None)
        notes = [n for v in rows if v.name in ("enemies", "loot") for n in v.notes]
        self.assertTrue(notes and all("no follow in this run" not in n for n in notes), notes)
        self.assertTrue(any(n.startswith("D followed H (its `auto follow ... (script)` line), but where both stood was "
                                         "not read") for n in notes), notes)

    def test_no_follow_is_not_tested_and_the_rows_say_where_they_were_read(self) -> None:
        v = lc.grade_together(RunFolder(self.S, H_JOIN + self.H + LEVEL_END, D_JOIN + LEVEL_END).run())
        self.assertEqual(v.status, lc.NOT_TESTED)
        self.assertIn("D stayed where it entered", v.counts)


class LevelEvents(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")

    def test_a_step_early_on_one_game_and_twice_on_another(self) -> None:
        # The scenes' author's line form (online/6-owner-session3-scenes, written ahead of the build).
        s = [(50, "Online: level event Level_EventManager#2150 step 3 white fade at shared tick 3000 (S's step at 3000)"),
             (60, "Online: level event Level_EventManager#2150 step 4 fly-through at shared tick 3600 (S's step at 3600)")]
        h = H_JOIN + [(44, "Online: level event Level_EventManager#2150 step 3 white fade at shared tick 2640 (S's step at "
                           "3000)"),
                      (60, "Online: level event Level_EventManager#2150 step 4 fly-through at shared tick 3602 (S's step "
                           "at 3600)")]
        d = D_JOIN + [(50, "Online: level event Level_EventManager#2151 step 3 white fade at shared tick 3004 (S's step at "
                           "3000)"),
                      (60, "Online: level event Level_EventManager#2151 step 4 fly-through at shared tick 3601 (S's step "
                           "at 3600)"),
                      (61, "Online: level event Level_EventManager#2151 step 4 fly-through at shared tick 3660 (S's step "
                           "at 3600)")]
        v = lc.grade_level_events(lc.load_run(RunFolder(s, h, d).path))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual([f.what for f in v.findings], [
            "Level_EventManager step 3 white fade: H -360 shared ticks from S's step",
            "Level_EventManager step 4 fly-through: 2 times on D (S 1); D +60 shared ticks from S's step"])

    @unittest.skipUnless(OWNER_3.is_dir(), "the owner's third session is not here")
    def test_the_owners_third_session_predates_the_lines(self) -> None:
        self.assertEqual(lc.grade_level_events(lc.load_run(self.OWNER_3)).status, lc.NOT_TESTED)


class WorldEvents(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")
    LP = Path(r"C:/Users/daz/Documents/T3Code/SwapForceOnline-work/levelpass")

    @unittest.skipUnless(LP.is_dir(), "the level data is not here")
    def test_the_cage_step_lowered_on_h_applied_late_on_d(self) -> None:
        # The world rows' author's line forms (online/6-owner-session3-world, written ahead of the build).
        data = lc.level_data(self.LP, "tutorial_b")
        self.assertIn("A2_CageStep_Hanging", [n for n, _u, _k in data.world_objects])
        h = H_JOIN + [(50, "Online: world event A2_CageStep_Hanging#2653 lowered by H (state up -> down)")]
        d = D_JOIN + [(53, "Online: world event A2_CageStep_Hanging#2701 lowered applied here (state down)")]
        s = [(50, "Online: world event A2_CageStep_Hanging#3001 lowered applied here (state down)")]
        v = lc.grade_world_events(lc.load_run(RunFolder(s, h, d).path), data)
        self.assertEqual(v.status, lc.FAIL)
        self.assertTrue(v.findings[0].what.startswith("A2_CageStep_Hanging lowered by H on H at "), v.findings[0].what)
        self.assertIn("not applied on D within 1 s (later: ", v.findings[0].what)
        self.assertIn(", 1 changed in this run; untested: ", v.counts)
        d_ok = D_JOIN + [(50, "Online: world event A2_CageStep_Hanging#2701 lowered applied here (state down)")]
        self.assertEqual(lc.grade_world_events(lc.load_run(RunFolder(s, h, d_ok).path), data).status, lc.PASS)

    @unittest.skipUnless(OWNER_3.is_dir() and LP.is_dir(), "the owner's third session or the level data is not here")
    def test_the_owners_third_session_predates_the_lines(self) -> None:
        r = lc.load_run(self.OWNER_3)
        v = lc.grade_world_events(r, lc.level_data(self.LP, lc.run_level(r)))
        self.assertEqual(v.status, lc.NOT_TESTED)
        self.assertIn("0 changed in this run", v.counts)


class EnemyServerDamage(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")

    def test_states_and_hits_from_s(self) -> None:
        # The enemies author's line forms (online/5-replica-attacks-land-2 4b9ae45d).
        s = [(50, "Online: enemy hit a1@2 on peer 1 #1 damage 10 occurrence 3 tick 900 (S's hit, sent) (#1)"),
             (51, "Online: enemy hit a1@2 on peer 2 #2 damage 10 occurrence 3 tick 905 (S's hit, sent) (#2)"),
             (52, "Online: enemy hit a1@2 on peer 2 #3 damage 10 occurrence 3 tick 910 (S's hit, sent) (#3)")]
        h = H_JOIN + [(45, "Online: enemy state a1@2 converged 1a2b3c4d Spawn_Cannon -> Idle lag 12 ticks (within 1 s)"),
                      (50, "Online: enemy hit a1@2 #1 damage 10 force 0 occurrence 3 applied here: applied hp 200 -> 190 "
                           "(S tick 900, here 902) (#1)")]
        d = D_JOIN + [(46, "Online: enemy state a1@2 converged 1a2b3c4d Spawn_Cannon -> Idle lag 75 ticks (late)"),
                      (51, "Online: enemy hit a1@2 #2 damage 10 force 0 occurrence 3 applied here: applied hp 200 -> 190 "
                           "(S tick 905, here 906) (#1)"),
                      (51, "Online: enemy hit a1@2 #2 damage 10 force 0 occurrence 3 applied here: applied hp 190 -> 180 "
                           "(S tick 905, here 907) (#2)"),
                      (53, "Online: hero hit by Chompy#2102 a1@2 (sweep, 10) (S's attack, played here) (#1)")]
        v = lc.grade_enemy_server_damage(lc.load_run(RunFolder(s, h, d).path))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual([f.what.split(" (")[0] for f in v.findings], [
            "D: 1 copy state(s) converged late", "1 S hit(s) applied more than once on the player's game",
            "1 S hit(s) lost: no applied or refused line on the player's game",
            "1 hero hit(s) by a copy's own hit while the damage is S's"])

    @unittest.skipUnless(OWNER_3.is_dir(), "the owner's third session is not here")
    def test_the_owners_third_session_predates_the_lines(self) -> None:
        self.assertEqual(lc.grade_enemy_server_damage(lc.load_run(self.OWNER_3)).status, lc.NOT_TESTED)


class DialogSkip(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")
    ON = (1, "Dev: skip scenes on (test-only): dialogs only: conversations are skipped through the game's own skip path")

    def test_a_skipped_conversation_ends_soon_after_its_skip(self) -> None:
        d = D_JOIN + [self.ON,
                      (60, "Online: scene local 2/L2 start conversation conversation recipient Tessa#3811 from x"),
                      (63, "Dev: skip conversation Tessa#3811 line 0 of 2 handed to the server's skip request"),
                      (63, "Online: scene skip conversation (generation 2, name 0x37E6CA06, no recipient, play 1) turn 0 "
                           "echoed here (line 0 -> 3 of 2; the server's line 2)"),
                      (64, "Online: scene local 2/L2 end by natural after 4.0 s (2/2.w1)"),
                      (70, "Online: scene local 2/L3 start conversation conversation recipient Pit#3881 from x"),
                      (70, "Dev: skip conversation Pit#3881 line 0 of 2: the server plays it too, left to its skip"),
                      (79, "Online: scene local 2/L3 end by natural after 9.4 s (2/3.w1)"),
                      (80, "Online: scene local 2/L4 start conversation conversation recipient Pit#2147483648 from x"),
                      (80, "Dev: skip conversation Pit#2147483648 line 0 of 3 is unskippable: waits it out"),
                      (95, "Online: scene local 2/L4 end by natural after 15.8 s (no server scene)")]
        v = lc.grade_dialog_skip(lc.load_run(RunFolder([], H_JOIN, d).path))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.counts, "D: 3 conversation(s), 2 skippable, 1 skipped in time")
        self.assertEqual([f.what for f in v.findings], [
            "D: Pit#3881 (2/L3): never skipped here (the skip said: the server plays it too, left to its skip)"])
        self.assertEqual(lc.grade_dialog_skip(lc.load_run(RunFolder([], H_JOIN, D_JOIN).path)).status, lc.NOT_TESTED)

    @unittest.skipUnless(OWNER_3.is_dir(), "the owner's third session is not here")
    def test_the_owners_third_session_fails(self) -> None:
        # Tessa's two lines: the server's skip came 3 s in and the conversation went on 9.9 s (D) / 9.4 s (H) more;
        # the pit's and Tessa's second talk were never skipped (`the server plays it too, left to its skip`).
        v = lc.grade_dialog_skip(lc.load_run(self.OWNER_3))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.counts, "D: 8 conversation(s), 7 skippable, 3 skipped in time; H: 8 conversation(s), 7 "
                                   "skippable, 3 skipped in time")
        self.assertIn("D: Tessa#3811 (2/L2): it ended 9.9 s after its last skip (13.6 s in all) (the skip said: handed "
                      "to the server's skip request; the server plays it too, left to its skip)",
                      [f.what for f in v.findings])


class GateKinds(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")
    LP = Path(r"C:/Users/daz/Documents/T3Code/SwapForceOnline-work/levelpass")

    @unittest.skipUnless(LP.is_dir(), "the level data is not here")
    def test_tutorial_b_has_three_elemental_gates_and_three_swap_zones(self) -> None:
        d = lc.level_data(self.LP, "tutorial_b")
        self.assertEqual(sorted((g.unit, g.lock) for g in d.gates if g.lock), [
            ("tutorial_b_zone_elemental_01", "dual"), ("tutorial_b_zone_elemental_02", "single"),
            ("tutorial_b_zone_elemental_03", "single")])
        self.assertEqual(sorted(u for _n, u in d.swap_zones), ["tb_climber", "tb_digger", "tb_flyer"])
        self.assertEqual(len([g for g in lc.level_data(self.LP, "ww1").gates if g.lock]), 3)
        v = lc.grade_gate_kinds(lc.load_run(RunFolder([], H_JOIN, D_JOIN).path), d)
        self.assertEqual(v.status, lc.NOT_TESTED)
        self.assertIn("NOT TESTED: LockedElementalGate tutorial_b_zone_elemental_01 (dual element)", v.counts)

    @unittest.skipUnless(OWNER_3.is_dir() and LP.is_dir(), "the owner's third session or the level data is not here")
    def test_the_owners_third_session(self) -> None:
        # "elemental gates dont work": H stood at the dual gate (00:05:41) and at the Life gate (00:06:00), neither
        # opened; "swap force gates dont work": H's press of the Rocket Swap Zone was refused (online_swap_zones off).
        r = lc.load_run(self.OWNER_3)
        v = lc.grade_gate_kinds(r, lc.level_data(self.LP, lc.run_level(r)))
        self.assertEqual([f.what.split(":")[0] for f in v.findings], [
            "LockedElementalGate tutorial_b_zone_elemental_01 (dual element)",
            "LockedElementalGate tutorial_b_zone_elemental_02 (single element)", "H"])
        self.assertIn("Swap Zones are not available in online play yet", v.findings[2].what)


class EnemyKills(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")
    OWNER_2 = Path(r"D:/SwapForceOnline-data/play/20261119-211901")

    def test_hits_that_take_no_health_on_s_and_a_kill_s_did_not_take(self) -> None:
        s = [(10, "Online: enemy placed a00000000.0629f659.00013531@2 as EB_Lobber#3996 hp 75 pose 31 (S)"),
             (11, "Online: enemy placed a00000000.0629f659.0001330d@2 as Chompy#4792 hp 1 pose 63 (S)"),
             (12, "Online: enemy placed a00000000.0629f659.00013290@2 as Chompy#4793 hp 1 pose 64 (S)"),
             (20, "Online: hit 1:3 PeerPuppet_1 -> Chompy#4793 hp 1 -> 0 killed (landed, victim w1, team 28)"),
             (21, "Online: enemy a00000000.0629f659.00013290@2 dead by 1 (S, placed; attacker PeerPuppet_1#3844)"),
             (40, "Online: enemy a00000000.0629f659.0001330d@2 dead by 2 (S, placed; attacker PeerPuppet_2#3844)")]
        h = H_JOIN + [(19, "Online: enemy local health held a00000000.0629f659.00013290@2 (1 -> -20, 8298e4bc)"),
                      (30, "Online: enemy local health held a00000000.0629f659.00013531@2 (75 -> 54, 8298e4bc)")]
        d = D_JOIN + [(41, "Online: enemy kill a00000000.0629f659.0001330d@2 failed (pending, the replica stays bound)"),
                      (75, "Online: enemy life a00000000.0629f659.0001330d@2 alive -> dead")]
        v = lc.grade_enemy_kills(lc.load_run(RunFolder(s, h, d).path))
        self.assertEqual(v.status, lc.FAIL)
        self.assertTrue(v.counts.startswith("2 enemy row(s) hit by the players (2 landed hit(s) on their own games); S "
                                            "took health off 1 row(s), 2 died on S"), v.counts)
        self.assertEqual([f.what for f in v.findings], [
            "H: a00000000.0629f659.00013531@2 took 1 landed hit(s) here and lost no health on S (S applied 0 hit(s) to "
            "it): immortal",
            "D: its kill of a00000000.0629f659.0001330d@2 was not taken (pending, the replica stays bound): S's death of "
            "it -1.0 s from it; its copy here died 34 s later"])

    @unittest.skipUnless(OWNER_3.is_dir(), "the owner's third session is not here")
    def test_the_owners_third_session(self) -> None:
        # "immortal enemies": 4 kills on the clients S had already taken, whose copies stayed alive (D's for 34-36 s,
        # H's to the end), and enemies hit on a client that S never took health off (most presses never started on S).
        v = lc.grade_enemy_kills(lc.load_run(self.OWNER_3))
        kills = [f.what for f in v.findings if "its kill of" in f.what]
        self.assertEqual(len(kills), 4, kills)
        self.assertIn("D: its kill of a00000000.0629f659.0001330d@2 was not taken (pending, the replica stays bound): "
                      "S's death of it -0.1 s from it; its copy here died 34 s later", kills)
        self.assertGreater(len(v.findings), 20)
        self.assertIn("attacks started on the server: H (peer 1) 62 of 490, D (peer 2) 1 of 98", v.counts)

    @unittest.skipUnless(OWNER_2.is_dir(), "the owner's second session is not here")
    def test_the_owners_second_session(self) -> None:
        v = lc.grade_enemy_kills(lc.load_run(self.OWNER_2))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(len(v.findings), 7)


class LevelChanges(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")
    OWNER_2 = Path(r"D:/SwapForceOnline-data/play/20261119-211901")

    def test_each_change_loaded_in_play_present_and_quiet(self) -> None:
        s = [(4, "Online: level 1 town/town_state10/town_state10: open"),
             (19, "Online: level 2 tutorial/tutorial_b/tutorial_b: open"),
             (30, "Online: barrier 2 released at tick 900 (2 clients)"),
             (31, "Online: puppet 1 for peer 2 spawned at 0,0,0 (generation 2)"),
             (31, "Online: puppet 2 for peer 1 spawned at 0,0,0 (generation 2)")]
        h = H_JOIN + [(30, "Online: barrier 2 released at tick 900"),
                      (31, "Online: puppet 2 spawned Scorp at 1,2,3 (epoch 1 gen 2, real)"),
                      (32, "Where: tutorial_b at 1 2 100"), (34, "Where: tutorial_b at 1 2 101")]
        d = [(20, "Online: level 2 tutorial/tutorial_b/tutorial_b: load"), (30, "Online: barrier 2 released at tick 900"),
             (31, "Online: puppet 1 spawned Roller at 1,2,3 (epoch 1 gen 2, real)"),
             (32, "Where: tutorial_b at 1 2 100"), (34, "Where: tutorial_b at 1 2 100")] + D_JOIN
        v = lc.grade_level_changes(lc.load_run(RunFolder(s, h, sorted(d)).path))
        self.assertEqual(v.status, lc.PASS, v)
        self.assertIn("tutorial_b (g2) by S; H loaded", v.counts)
        # A fall, an [error] after the load, and no body of the other player on D.
        d2 = [(20, "Online: level 2 tutorial/tutorial_b/tutorial_b: load"), (30, "Online: barrier 2 released at tick 900"),
              (32, "Where: tutorial_b at 1 2 100"), (34, "Where: tutorial_b at 1 2 -400"),
              (40, "Online: stand-in RemotePuppet_1 0xB8A2DD00 still in the entity table before pool 57 is freed")] + D_JOIN
        f = RunFolder(s, h, sorted(d2))
        text = (f.path / "D" / "game.log").read_text(encoding="utf-8").replace(
            "] [info] [core] [t1] Online: stand-in", "] [error] [core] [t1] Online: stand-in")
        (f.path / "D" / "game.log").write_text(text, encoding="utf-8")
        v = lc.grade_level_changes(lc.load_run(f.path))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.findings[0].what,
                         "tutorial_b (g2): D: 1 [error] line(s) between its load and 30 s after the barrier; D: the other "
                         "player's body not shown within 60 s of the barrier; D: its hero never stood within 60 s of the "
                         "barrier (z 100 -> -400)")

    @unittest.skipUnless(OWNER_3.is_dir(), "the owner's third session is not here")
    def test_the_owners_third_session(self) -> None:
        # "Map changes are bugged": tutorial_a's entry took 67 s to the barrier, and S's own [error] lines say its level
        # entry made no progress for 30 s and its world had not run for 40 s (23:54:00, 23:54:11).
        v = lc.grade_level_changes(lc.load_run(self.OWNER_3))
        self.assertEqual([x.what for x in v.findings],
                         ["tutorial_a (g1): S: 2 [error] line(s) between its open and 30 s after the barrier"])

    @unittest.skipUnless(OWNER_2.is_dir(), "the owner's second session is not here")
    def test_the_owners_second_session(self) -> None:
        # tutorial_b's change: H 14 and D 6 `stand-in ... still in the entity table before pool <n> is freed` errors.
        v = lc.grade_level_changes(lc.load_run(self.OWNER_2))
        self.assertEqual([x.what for x in v.findings],
                         ["tutorial_b (g2): H: 14 [error] line(s) between its load and 30 s after the barrier; D: 6 "
                          "[error] line(s) between its load and 30 s after the barrier"])


class ScenesOnce(unittest.TestCase):
    OWNER_3 = Path(r"D:/SwapForceOnline-data/play/20261119-235225")
    OWNER_2 = Path(r"D:/SwapForceOnline-data/play/20261119-211901")

    def test_a_cutscene_started_twice_in_one_generation_fails(self) -> None:
        h = H_JOIN + [(50, "Online: scene local 1/L3 start cutscene TA_ChompyIntro recipient Event_ChompyIntro#2132 from "
                           "script CutscenePlay"),
                      (61, "Online: scene local 1/L4 start cutscene TA_ChompyIntro recipient Event_ChompyIntro#2132 from "
                           "script CutscenePlay"),
                      (90, "Online: scene local 2/L1 start cutscene TA_ChompyIntro recipient - from the level load"),
                      (95, "Online: scene local 2/L2 start conversation conversation recipient Tessa#3811 from x"),
                      (99, "Online: scene local 2/L3 start conversation conversation recipient Tessa#3811 from x")]
        v = lc.grade_scenes_once(lc.load_run(RunFolder([], h, D_JOIN).path))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.counts, "H: 3 cutscene start(s), 2 distinct")
        self.assertEqual([f.what.split(" (at")[0] for f in v.findings],
                         ["H: TA_ChompyIntro started 2 times in level generation 1"])

    @unittest.skipUnless(OWNER_3.is_dir(), "the owner's third session is not here")
    def test_the_owners_third_session_repeats(self) -> None:
        # His complaint "cutscenes play multiple times", from his own logs: in tutorial_a (generation 1) TA_ChompyIntro
        # started twice on H (23:54:49, 23:55:00) and on D (23:54:49, 23:55:08); D also played TA_CoreFlyThrough and
        # TA_GreebleMeleeIntro twice (23:55:30 and 23:55:53; 23:55:43 and 23:56:00).
        v = lc.grade_scenes_once(lc.load_run(self.OWNER_3))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(sorted(f.what.split(" (at")[0] for f in v.findings), [
            "D: TA_ChompyIntro started 2 times in level generation 1",
            "D: TA_CoreFlyThrough started 2 times in level generation 1",
            "D: TA_GreebleMeleeIntro started 2 times in level generation 1",
            "H: TA_ChompyIntro started 2 times in level generation 1",
            "S: peer 1's 0x00000854 play 0 started 2 times in level generation 1",
            "S: peer 2's 0x00000845 play 0 started 2 times in level generation 1",
            "S: peer 2's 0x00000854 play 0 started 2 times in level generation 1",
            "S: peer 2's 0x00000866 play 0 started 2 times in level generation 1"])

    @unittest.skipUnless(OWNER_2.is_dir(), "the owner's second session is not here")
    def test_the_owners_second_session(self) -> None:
        # No cutscene repeated there: 7 starts on H and on D, 8 reports on S, each once.
        v = lc.grade_scenes_once(lc.load_run(self.OWNER_2))
        self.assertEqual(v.status, lc.PASS, v)


class CoinsShared(unittest.TestCase):
    # W38: each coin taken once, granted once, paid once with the same amount on the other client and removed there
    # (the coins branch's line forms, online/8-all-coins-shared 033f2274).
    S = [(50, "Online: coin claim s1a2b3c4.00000001#0 peer 2 granted (S's base 5.00, its toucher's base 5.00 and factor "
              "1.00: 5.00 to every player; 2 clients told, its toucher's game among them)")]
    D = D_JOIN + [(50, "Online: coin taken here s1a2b3c4.00000001#0 (player 8a001234, base 5.00, factor 1.00: 5.00; gold "
                       "100 -> 105, +5; S is told)")]
    H = H_JOIN + [(51, "Online: coin paid here s1a2b3c4.00000001#0 (taken by another player; player 8b001234 slot 0, "
                       "S's amount 5.00: gold 30 -> 35, +5)"),
                  (51, "Online: coin removed here s1a2b3c4.00000001#0 (taken by another player and paid here: its copy "
                       "here without pay)")]

    def test_a_coin_taken_by_one_is_paid_once_to_the_other(self) -> None:
        v = lc.grade_coins_shared(lc.load_run(RunFolder(self.S, self.H, self.D).path))
        self.assertEqual(v.status, lc.PASS, v)
        self.assertIn("1 coin(s) taken (H 0, D 1); 1 paid once to every player with the same amount; coin money: "
                      "H +5, D +5", v.counts)
        # The coins row's patterns read the new forms too.
        self.assertEqual(lc.COIN_TAKEN.search(self.D[-1][1]).group(1, 2, 3), ("s1a2b3c4.00000001#0", "100", "105"))
        self.assertEqual(lc.COIN_PAID.search(self.H[-2][1]).group(1, 2, 3), ("s1a2b3c4.00000001#0", "30", "35"))

    def test_unpaid_different_or_twice_fail_by_name(self) -> None:
        h = H_JOIN + [(51, "Online: coin paid here s1a2b3c4.00000001#0 (taken by another player; player 8b001234 slot "
                           "0, S's amount 5.00: gold 30 -> 34, +4)")]
        v = lc.grade_coins_shared(lc.load_run(RunFolder(self.S, h, self.D).path))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.findings[0].what, "coin s1a2b3c4.00000001#0: paid +4 on H, taken +5 on D; its copy on H not "
                                             "removed")
        self.assertIn("the players' coin money differs: H +4, D +5", [f.what for f in v.findings])
        v = lc.grade_coins_shared(lc.load_run(RunFolder(self.S, H_JOIN, self.D + [
            (52, "Online: coin taken here s1a2b3c4.00000001#0 (player 8a001234, base 5.00, factor 1.00: 5.00; gold 105 "
                 "-> 110, +5; S is told)")]).path))
        self.assertTrue(v.findings[0].what.startswith("coin s1a2b3c4.00000001#0: taken on 2 games (D, D); paid 0 "
                                                      "time(s) on H"), v.findings[0].what)
        self.assertEqual(lc.grade_coins_shared(lc.load_run(RunFolder(self.S, H_JOIN, D_JOIN).path)).status,
                         lc.NOT_TESTED)


class Coins(unittest.TestCase):
    ON = [(1, "Online: coins on for the session (S's online_shared_coins on)")]

    def test_a_coin_taken_and_paid(self) -> None:
        s = self.ON + [(30, "Online: coins walked bd35d565 20 placed money coins mapped"),
                       (60, "Online: coin claim bd35d565.4 peer 1 granted (1 other clients owed)")]
        h = [(31, "Online: coins walked bd35d565 20 placed money coins mapped"),
             (60, "Online: coin taken here bd35d565.4 (player 0, gold 100 -> 105, +5; S is told)")]
        d = [(41, "Online: coins walked bd35d565 20 placed money coins mapped"),
             (61, "Online: coin paid here bd35d565.4 (taken by another player; player 0, gold 30 -> 35, +5)")]
        v = lc.grade_coins(RunFolder(s, H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("coins taken: H 1, D 0; S handled 1 claim(s); paid on another game 1", v.counts)

    def test_takes_with_no_claim_handled_fail_and_a_coin_taken_twice_is_named(self) -> None:
        # Batch 11 second round (ww1 20261119-111305): H took 3, D 7, three of them the same places; S no claim.
        s = self.ON + [(30, "Online: coins walked bd35d565 241 placed money coins mapped (246 CCollectibles)")]
        walked = [(30, "Online: coins walked bd35d565 241 placed money coins mapped (246 CCollectibles)")]
        h = walked + [(60, "Online: coin taken here bd35d565.8532 (player b662efd0, gold 315 -> 316, +1; S is told)"),
             (60.1, "Online: coin claim sent bd35d565.8532")]
        d = walked + [(61, "Online: coin taken here bd35d565.8532 (player b6642820, gold 9 -> 10, +1; S is told)"),
             (62, "Online: coin taken here bd35d565.8514 (player b6642820, gold 7 -> 8, +1; S is told)")]
        v = lc.grade_coins(RunFolder(s, H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual([x.what for x in v.findings],
                         ["1 coin(s) taken on H, 2 coin(s) taken on D; S handled 0 claim(s): shared coins did nothing",
                          "taken twice: 1 coin(s) taken on more than one game (bd35d565.8532)"])
        self.assertIn("coins taken: H 1, D 2; S handled 0 claim(s)", v.counts)

    def test_none_mapped_with_coins_on_fails_never_untested(self) -> None:
        s = self.ON + [(90, "Online: coins 2: 0 claims granted, 0 refused, 0 placed money coins mapped here (0 this "
                            "level), 0 ambiguous, 0 copies removed here, 0 removals waiting, 0 fan-outs owed (x)")]
        h = [(90, "Online: coins 2: 0 placed money coins mapped, 0 taken here, 0 taken by others and paid here, 0 "
                  "pending, 0 unpaid, 0 collected without pay, 0 claims not sent yet, 0 ambiguous, 0 copies removed here "
                  "(the level changed)")]
        v = lc.grade_coins(RunFolder(s, H_JOIN + h + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertTrue(v.counts.startswith("0 coins mapped on every game"))

    def test_an_unpaid_coin_fails_and_off_is_not_tested(self) -> None:
        s = self.ON + [(30, "Online: coins walked bd35d565 20 placed money coins mapped")]
        h = [(60, "Online: coin taken here bd35d565.4 (player 0, gold 100 -> 105, +5; S is told)")]
        d = [(70, "Online: coin unpaid bd35d565.4 (no paying collect in 3 tries: x)")]
        v = lc.grade_coins(RunFolder(s, H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertTrue(any("unpaid" in x.what for x in v.findings))
        off = lc.grade_coins(RunFolder([(1, "Online: coins off for the session (S's online_shared_coins off)")], H_JOIN).run())
        self.assertEqual(off.status, lc.NOT_TESTED)


# The two pages the confirm agent checked row by row (logs/confirm/TRIAGE.txt, batch 8, runs 4 and 5): the
# enemies row was wrong on both. Run folders on the test store (outside git: they name the disc's objects).
CALIBRATION_RUNS = Path(r"D:/SwapForceOnline-data/tests/int2/runs")
TUTORIAL_A = CALIBRATION_RUNS / "20261119-033102-levelpass-tutorial_a"
TUTORIAL_B = CALIBRATION_RUNS / "20261119-033620-levelpass-tutorial_b"


@unittest.skipUnless(TUTORIAL_A.is_dir() and TUTORIAL_B.is_dir(), "the batch 8 run folders are not on this machine")
class EnemiesCalibration(unittest.TestCase):
    def test_tutorial_a_counts_every_enemy_s_had(self) -> None:
        v = lc.grade_enemies(lc.load_run(TUTORIAL_A))
        self.assertTrue(v.counts.startswith("S 32 (0 placed, 28 encounter, 4 run-time), 32 died on S (by a player: "
                                            "H 0, D 0; by another entity 0; no attacker 4; attacker not recorded 28)"), v.counts)
        for who in "HD":
            self.assertIn(f"{who}: bound 30 of 32, waiting asleep 0 (its copy asleep, far from this player: not a "
                          "fault by itself), never existed here 2; deaths 32: 30 seen on a bound copy, 2 dead before "
                          "bind", v.counts)
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.findings[0].what, "4 enemies died on S with no attacker within 10 s of their spawn (2 "
                                             "reached no client)")

    def test_tutorial_b_bound_asleep_and_dead_before_bind_apart(self) -> None:
        v = lc.grade_enemies(lc.load_run(TUTORIAL_B))
        self.assertIn("S 25 (17 placed, 0 encounter, 8 run-time), 11 died on S (by a player: H 11, D 0;", v.counts)
        self.assertIn("H: bound 22 of 25, waiting asleep 2 (its copy asleep, far from this player: not a fault by "
                      "itself), never existed here 1; deaths 11: 10 seen on a bound copy, 1 dead before bind", v.counts)
        # D stands at the start: 17 copies only waited asleep; its 5 dead-before-bind are 4 asleep copies and the
        # bomber no client had.
        self.assertIn("D: bound 7 of 25, waiting asleep 17 (its copy asleep, far from this player: not a fault by "
                      "itself), never existed here 1; deaths 11: 6 seen on a bound copy, 5 dead before bind", v.counts)
        whats = [x.what for x in v.findings]
        # W34: S's unpublished spawns come first, in any run.
        self.assertTrue(whats[0].startswith("S did not publish "), whats[0])
        self.assertEqual(whats[1], "a fight step failed with no damage done: 17 burst(s) at 3085,-534,3746, no hit "
                                   "took a hit point")
        self.assertIn("S's kills by player (`dead by <peer>`): H 11, D 0", v.findings[1].lines)
        self.assertIn("existed on no client: sb7fe49cd.52540001#3/0@2 (BomberSpawner_02_Spawned3#3951)", whats)
        self.assertEqual(v.status, lc.FAIL)



@unittest.skipUnless((CALIBRATION_RUNS / "20261119-034752-levelpass-tutorial_b").is_dir() and TUTORIAL_B.is_dir(),
                     "the batch 8 run folders are not on this machine")
class LootAndCoinsCalibration(unittest.TestCase):
    """Batch 8 runs 7 and 8 (logs/confirm/TRIAGE.txt): the clients' own accounts, the carriers, the coins."""
    TB = CALIBRATION_RUNS / "20261119-034752-levelpass-tutorial_b"
    WW1 = CALIBRATION_RUNS / "20261119-035316-levelpass-ww1"

    def groups(self, v) -> list[tuple[str, int]]:
        return [(x.what.split(":")[0] + " " + x.what.split(", ", 1)[1].split(" (")[0], len(x.lines)) for x in v.findings]

    def test_tutorial_b(self) -> None:
        v = lc.grade_loot(lc.load_run(self.TB))
        self.assertIn("H: 30 obligations, 26 delivered in full, 1 differ, 3 short, 0 unfinished", v.counts)
        self.assertIn("S to peer 1: 30 made, 27 completed, 3 ended otherwise", v.counts)
        self.assertIn("D: 30 obligations, 0 delivered in full, 0 differ, 30 short", v.counts)
        self.assertIn("S to peer 2: 30 made, 0 completed, 30 ended otherwise", v.counts)
        self.assertEqual(self.groups(v), [("H its carrier has no row here", 2), ("H other", 2),
                                          ("D its carrier's copy is asleep here", 16), ("D its carrier has no row here", 14)])
        c = lc.grade_coins(lc.load_run(self.TB))
        self.assertEqual(c.status, lc.FAIL)
        self.assertTrue(c.counts.startswith("0 coins mapped on every game"))

    def test_ww1(self) -> None:
        v = lc.grade_loot(lc.load_run(self.WW1))
        self.assertIn("H: 8 obligations, 0 delivered in full, 0 differ, 8 short", v.counts)
        self.assertIn("D: 8 obligations, 6 delivered in full, 0 differ, 2 short", v.counts)
        self.assertEqual(self.groups(v), [("H its carrier's copy is asleep here", 6), ("H its carrier has no row here", 2),
                                          ("D its carrier has no row here", 2)])
        self.assertIn("carrier IntroArea_ChompyManager_actor_0", " ".join(v.findings[0].lines))
        self.assertTrue(lc.grade_coins(lc.load_run(self.WW1)).counts.startswith("0 coins mapped on every game"))
        # A night run with coins off says so: not tested, never a pass.
        self.assertEqual(lc.grade_coins(lc.load_run(TUTORIAL_B)).status, lc.NOT_TESTED)



LEVELPASS_DATA = Path.home() / "Documents" / "T3Code" / "SwapForceOnline-work" / "levelpass"
TB_AIM = CALIBRATION_RUNS / "20261119-043508-levelpass-tutorial_b"


@unittest.skipUnless(TB_AIM.is_dir() and TUTORIAL_B.is_dir() and (LEVELPASS_DATA / "tutorial_b" / "logic.json").is_file(),
                     "the batch 8/9 run folders or the level data are not on this machine")
class HeldEnemiesCalibration(unittest.TestCase):
    """The enemies row reads the level's holds (levelpass_holds, the generator's data): scenery is not counted,
    a hold this run did not release is listed, not graded (043508 failed on the Ambi_Chompy scenery)."""

    def grade(self, folder: Path) -> lc.Verdict:
        run = lc.load_run(folder)
        return lc.grade_enemies(run, lc.level_data(LEVELPASS_DATA, lc.run_level(run)))

    def test_the_aim_run_passes_with_its_scenery_and_holds_listed(self) -> None:
        v = self.grade(TB_AIM)
        self.assertEqual([x.what for x in v.findings if not x.what.startswith("S did not publish ")], [])
        self.assertTrue(v.notes[0].startswith("scenery, not counted (the level never releases them): Ambi_Chompy_01,"))
        held = [n for n in v.notes if n.startswith("held, not released in this run: ")]
        self.assertEqual([n.split(": ", 1)[1].split(" (")[0] for n in held], ["Chompy", "Chompy001", "Chompy002", "Chompy003"])
        self.assertIn("SwapPartsHint_RemoveTrigger", held[0])
        self.assertIn("hit only held enemies (Ambi_Chompy_03 scenery)", v.counts)
        self.assertIn("set apart: 9 scenery, 4 held and not released in this run", v.counts)

    @unittest.skipUnless((CALIBRATION_RUNS / "20261119-061110-levelpass-tutorial_b").is_dir(), "batch 11's run is not here")
    def test_batch_11_follow_run_passes_with_its_slept_spawn_apart(self) -> None:
        v = self.grade(CALIBRATION_RUNS / "20261119-061110-levelpass-tutorial_b")
        # W34: only S's unpublished spawns fail it (they fail the row in any run).
        self.assertEqual([x.what for x in v.findings if not x.what.startswith("S did not publish ")], [])
        self.assertIn("D: bound 11 of 11", v.counts)
        self.assertIn("gone to sleep on the server before any player's game made it: 1", v.counts)

    def test_033620_fails_only_on_the_bomber_no_client_had(self) -> None:
        v = self.grade(TUTORIAL_B)
        whats = [x.what for x in v.findings]
        self.assertTrue(whats[0].startswith("S did not publish "), whats)  # W34: S's unpublished spawns, first
        self.assertEqual(whats[1:], ["existed on no client: sb7fe49cd.52540001#3/0@2 (BomberSpawner_02_Spawned3#3951)"])


PROBE = CALIBRATION_RUNS / "20261119-042921-islands-probe-ww1"


class Pieces(unittest.TestCase):
    S = [(10, "Online: world tracked spawner_fires EZ3_IslandSpawner0#2460 (Scripts.SpawnerComponent; its first island)"),
         (50, "Online: world island spawner EZ3_IslandSpawner0#2460 fired 1 (tick 5142): EZ3_IslandSpawner0_SpawnedInstance1#3576"),
         (57, "Online: world island spawner EZ3_IslandSpawner0#2460 fired 2 (tick 5352): EZ3_IslandSpawner0_SpawnedInstance2#3580"),
         (51, "Online: world island EZ3_IslandSpawner0#2460 #1 at -5071.8,-3012.2,-38.7"),
         (58, "Online: world island EZ3_IslandSpawner0#2460 #2 at -5071.8,-3012.2,-38.7"),
         (70, "Online: world island EZ3_IslandSpawner0#2460 #1 gone (lived 20000 ms)")]

    def test_made_placed_and_gone_with_s(self) -> None:
        h = [(11, "Online: world tracked spawner_fires EZ3_IslandSpawner0#2460 (Scripts.SpawnerComponent; its first island)"),
             (50.1, "Online: world island spawner EZ3_IslandSpawner0#2460 made 1 here (S's 0, 0 ticks after S): x#1"),
             (57.2, "Online: world island spawner EZ3_IslandSpawner0#2460 made 2 here (S's 2, 5 ticks after S): x#2"),
             (51.1, "Online: world island EZ3_IslandSpawner0#2460 #1 at -5071.0,-3012.0,-38.0"),
             (58.1, "Online: world island EZ3_IslandSpawner0#2460 #2 at -5071.0,-3012.0,-38.0"),
             (70.3, "Online: world island EZ3_IslandSpawner0#2460 #1 gone (lived 20000 ms)")]
        v = lc.grade_pieces(RunFolder(self.S, H_JOIN + h + LEVEL_END).run())
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("H: tracked 1 spawner(s); made 2 of 2 within 1 s; places 2 pair(s), 0 over 12", v.counts)
        self.assertIn("gone 1 of S's 1", v.counts)

    def test_late_far_missing_and_not_gone(self) -> None:
        h = [(11, "Online: world tracked spawner_fires EZ3_IslandSpawner0#2460 (Scripts.SpawnerComponent; its first island)"),
             (52, "Online: world island spawner EZ3_IslandSpawner0#2460 made 1 here (S's 1, 120 ticks after S): x#1"),
             (51.1, "Online: world island EZ3_IslandSpawner0#2460 #1 at -4900.0,-3012.0,-38.0")]
        v = lc.grade_pieces(RunFolder(self.S, H_JOIN + h + LEVEL_END).run())
        whats = [x.what for x in v.findings]
        self.assertEqual(whats, ["H: 1 of S's island fires never made here", "H: 1 island(s) made more than 1 s from S's fire",
                                 "H: 1 island place(s) more than 12 from S's", "H: 1 island end(s) of S not followed here "
                                 "within 1 s"])

    def test_off_is_not_tested_each_game_its_own(self) -> None:
        import levelpass_holds as lh  # noqa: F401
        data = lc.LevelData("ww1", {}, [], {}, ["EZ3_IslandSpawner0"])
        v = lc.grade_pieces(RunFolder([], H_JOIN + LEVEL_END).run(), data)
        self.assertEqual(v.status, lc.NOT_TESTED)
        self.assertTrue(v.counts.startswith("not tested: each game runs its own islands"))

    @unittest.skipUnless(PROBE.is_dir(), "the islands probe's run folder is not on this machine")
    def test_the_probe_run(self) -> None:
        v = lc.grade_pieces(lc.load_run(PROBE))
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertTrue(v.counts.startswith("S fired 18 island(s) of 5 spawner(s); H: tracked 5 spawner(s); made 18 of 18 "
                                            "within 1 s; places 210 pair(s), 0 over 12 (median 1.6"), v.counts)



B10_TB = CALIBRATION_RUNS / "20261119-050629-levelpass-tutorial_b"


@unittest.skipUnless(B10_TB.is_dir() and (GAME_REPO / "tools" / "scene_sync_check.py").is_file(),
                     "batch 10's run folder or the game repository's scene checker is not here")
class ScenesWhereCalibration(unittest.TestCase):
    def test_the_missing_scenes_say_d_was_not_there(self) -> None:
        v = lc.grade_scenes(lc.load_run(B10_TB), GAME_REPO)
        self.assertIn("missing on one client: 0 with both players there (game), 3 with the other player farther than "
                      "1500 (test)", v.counts)
        self.assertTrue(all(x.what.startswith("D was not there (test): 4287") for x in v.findings), v.findings)
        self.assertTrue(v.findings[0].lines[-1].startswith("H/data/Online/session/game.log:"))



@unittest.skipUnless((CALIBRATION_RUNS / "20261119-065111-levelpass-tutorial_b").is_dir() and
                     (Path(r"C:/Users/daz/Documents/T3Code/SwapForceOnline-work/levelpass/tutorial_b") / "manifest.json").is_file(),
                     "the key-gate run or tutorial_b's data is not here")
class KeyGateCalibration(unittest.TestCase):
    def test_gate_one_opened_on_s_and_gate_two_pressed_gate_ones_lock(self) -> None:
        run = lc.load_run(CALIBRATION_RUNS / "20261119-065111-levelpass-tutorial_b")
        shut, done = lc.required_steps(run, lc.level_data(Path(r"C:/Users/daz/Documents/T3Code/SwapForceOnline-work/levelpass"), "tutorial_b"))
        self.assertEqual(len(done), 1, done)
        self.assertIn("opened on S, H, D: S at 06:54:11.743 ", done[0])  # W17: S's own row of the gate, written again
        # W34: the arena gate is a required fight gate now; this pass never fought it: named, never silent.
        self.assertEqual(len(shut), 2, shut)
        self.assertTrue(shut[0].startswith("TutorialBattleGate_01 (arena gate: win its fight (EA_Manager_W1, "), shut[0])
        self.assertIn("not performed by this pass", shut[0])
        self.assertIn("the press went to TutorialLockedGate#2607, the gate an earlier step pressed", shut[1])


@unittest.skipUnless((CALIBRATION_RUNS / "20261119-124636-levelpass-ww1").is_dir(), "batch 12's ww1 gates run is not here")
class GatesRunCalibration(unittest.TestCase):
    def test_together_reads_the_rotated_server_log_and_skips_the_skipped_steps(self) -> None:
        run = lc.load_run(CALIBRATION_RUNS / "20261119-124636-levelpass-ww1")
        v = lc.grade_together(run)
        self.assertEqual(v.status, lc.PASS, v.counts)
        self.assertIn("at 2 of 2 stop(s) where H acted", v.counts)
        self.assertEqual([a[0] for a in lc.skipped_acts(run.clients["H"])], ["v2_2", "v19_1", "v22_1", "v24_1"])


@unittest.skipUnless((CALIBRATION_RUNS / "20261119-124636-levelpass-ww1").is_dir() and
                     (Path(r"C:/Users/daz/Documents/T3Code/SwapForceOnline-work/levelpass/ww1") / "manifest.json").is_file(),
                     "batch 12's ww1 gates run or ww1's data is not here")
class FightGatesCalibration(unittest.TestCase):
    def test_two_demon_gates_opened_on_every_game(self) -> None:
        run = lc.load_run(CALIBRATION_RUNS / "20261119-124636-levelpass-ww1")
        _shut, done = lc.required_steps(run, lc.level_data(Path(r"C:/Users/daz/Documents/T3Code/SwapForceOnline-work/levelpass"),
                                                           "ww1"))
        names = [d.split(" (")[0] for d in done]
        self.assertEqual(names, ["Finale_DemonGate008", "PuzzleGateSawtooth001"])
        self.assertIn("S at 12:54:21.128", done[0])
        self.assertIn("H at 12:54:21.454", done[0])
        self.assertIn("D at 12:54:21.363", done[0])


@unittest.skipUnless((CALIBRATION_RUNS / "20261119-135125-levelpass-tutorial_b").is_dir(), "batch 13's objectives run is not here")
class ObjectivesCalibration(unittest.TestCase):
    def test_batch_13s_own_start_beside_s_row_fails(self) -> None:
        v = lc.grade_objectives(lc.load_run(CALIBRATION_RUNS / "20261119-135125-levelpass-tutorial_b"))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.counts, "S rows 3 for 2 objective(s) in 2 level(s); H applied 3; D applied 3")
        self.assertEqual([f.what for f in v.findings],
                         ["H ran its own start of objective 00000001, for which S has a row in generation 3",
                          "D ran its own start of objective 00000001, for which S has a row in generation 3"])

@unittest.skipUnless((CALIBRATION_RUNS / "20261119-111305-levelpass-ww1").is_dir(), "batch 11's ww1 coins run is not here")
class CoinsCalibration(unittest.TestCase):
    def test_the_second_rounds_coins(self) -> None:
        v = lc.grade_coins(lc.load_run(CALIBRATION_RUNS / "20261119-111305-levelpass-ww1"))
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.findings[0].what, "3 coin(s) taken on H, 7 coin(s) taken on D; S handled 0 claim(s): shared "
                                             "coins did nothing")
        self.assertTrue(v.findings[1].what.startswith("taken twice: 3 coin(s)"))


FIGHT = "-535,-2875,303"
PLACED = [(30, f"Online: enemy placed {K1} as GreebleMelee#3531 hp 96 pose 26 (S)")]
SEEN = [(31, f"Online: enemy spawn {K1} as GreebleMelee#b848ac60 (bind placed)")]


def fight(hp_after: str, end: str = "failed (time: 2 enemies within 1500, 2 bursts, 2 enemies seen)") -> list:
    return [(50, f"Dev: fight_area {FIGHT} start: radius 1500, 2 enemies within it; hero at 1,2,3, 9 from the area"),
            (51, f"Dev: fight_area {FIGHT} try 1: x at GreebleMelee#3531 d 72: hp 96 -> 96 (2 enemies within 1500)"),
            (52, f"Dev: fight_area {FIGHT} try 2: x at GreebleMelee#3531 d 73: hp 96 -> {hp_after} (2 enemies)"),
            (76, f"Dev: fight_area {FIGHT} {end} after 25.8 s")]


class Fights(unittest.TestCase):
    """A fight step that took no hit point, and a route that fought where nothing died by a player, are
    findings of the enemies row, never its pass (ww1 20261119-035316: 16 bursts, hp 96 -> 96, "enemies PASS")."""

    def grade(self, s, h) -> lc.Verdict:
        return lc.grade_enemies(RunFolder(PLACED + s, H_JOIN + SEEN + h + LEVEL_END).run())

    def test_a_failed_fight_with_no_damage_is_a_finding(self) -> None:
        v = self.grade([], fight("96"))
        self.assertEqual(v.status, lc.FAIL)
        whats = [x.what for x in v.findings]
        self.assertTrue(whats[0].startswith(f"a fight step failed with no damage done: 2 burst(s) at {FIGHT}"), whats)
        self.assertTrue(whats[1].startswith("the route fought 1 time(s) (2 burst(s)) and no enemy died"), whats)
        lines = v.findings[0].lines
        self.assertTrue(lines[0].startswith("H/game.log:") and "failed" in lines[0], lines)
        self.assertIn("try 1", lines[1])
        self.assertIn("damage trace is off", lines[2])  # no S check line: the plain fact
        self.assertIn("fights: 1 step(s), 1 with bursts, 0 took hit points; 0 enemy death(s) by a player", v.counts)

    def test_the_servers_damage_check_is_cited(self) -> None:
        refused = (60, "Online: puppet 1 damage check on GreebleMelee#3531 amount 12 result 0 reason out of range")
        v = self.grade([refused, (61, "Online: puppet 1 damage check on GreebleMelee#3531 result 0 reason x")],
                       fight("96"))
        line = v.findings[0].lines[2]
        self.assertTrue(line.startswith("H/data/Online/session/game.log:2:"), line)
        self.assertTrue(line.endswith("(and 1 more)"), line)

    def test_a_fight_that_hurt_and_killed_passes(self) -> None:
        v = self.grade([(53, f"Online: enemy {K1} dead by 1 (S, placed; attacker PeerPuppet_1#3829)")],
                       fight("gone", "done (no enemy within 1500 of the area for 10 s: 2 bursts, 1 enemies seen)")
                       + [(54, f"Online: enemy life {K1} alive -> dead")])
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("1 took hit points; 1 enemy death(s) by a player on S", v.counts)

    def test_one_players_hits_take_nothing_while_the_others_kill(self) -> None:
        v = lc.grade_enemies(RunFolder(PLACED + [(60, f"Online: enemy {K1} dead by 2 (S, placed; attacker PeerPuppet_1#9)")],
                                       H_JOIN + SEEN + fight("96") + LEVEL_END,
                                       D_JOIN + [(41, f"Online: enemy bind {K1} (resolved)"),
                                                 (61, f"Online: enemy life {K1} alive -> dead")] + LEVEL_END).run())
        whats = [x.what for x in v.findings]
        self.assertIn("H's hits took no hit point in 1 fight(s) (2 burst(s)) while D killed 1 on S: one player cannot "
                      "hurt enemies the other can", whats)
        self.assertNotIn("the route fought", " ".join(whats))  # a player's kill came

    def test_a_fight_that_never_burst_is_counted_not_a_finding_of_its_own(self) -> None:
        held = [(50, f"Dev: fight_area {FIGHT} start: radius 1500, 0 enemies within it"),
                (70, f"Dev: fight_area {FIGHT} failed (held too long: 0 bursts) after 20.0 s")]
        v = self.grade([(80, f"Online: enemy {K1} dead by 1 (S, placed; attacker PeerPuppet_1#3829)")],
                       held + [(81, f"Online: enemy life {K1} alive -> dead")])
        self.assertEqual(v.status, lc.PASS, v.findings)
        self.assertIn("1 failed before any burst (held too long: 0 bursts)", v.counts)


class Pickups(unittest.TestCase):
    def test_the_partners_copy(self) -> None:
        s = [(60, "Online: collect applied 000b0017 for peer 1"), (70, "Online: collect applied 000b0018 for peer 1")]
        h = [(60, "Online: collect native 000b0017 script (reported to S) (827513a0)"),
             (70, "Online: collect native 000b0018 script (reported to S) (827513a0)")]
        d = [(61, "Online: collect from S 000b0017 (the game's collect ran here)")]
        v = lc.grade_pickups(RunFolder(s, H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertIn("the partner's copy went 1 of 2", v.counts)
        self.assertEqual(v.findings[0].what, "D kept pickup 000b0018 that peer 1 collected on S")
        self.assertIn("coins", v.notes[0])

    def test_nothing_collected_is_not_a_pass(self) -> None:
        v = lc.grade_pickups(RunFolder([], H_JOIN, D_JOIN).run())
        self.assertEqual(v.status, lc.NOT_TESTED)


class Loot(unittest.TestCase):
    S = [(50, "Online: spill obliged w00000f2c 00000001 to peer 1 (obligation 1)"),
         (50, "Online: spill obliged w00000f2c 00000001 to peer 2 (obligation 1)"),
         (55, "Online: spill obliged w00000f2e 00000001 to peer 1 (obligation 3)"),
         (55, "Online: spill obliged w00000f2e 00000001 to peer 2 (obligation 3)"),
         (99, "Online: spill obligation 1 w00000f2c 00000001 to peer 1 ended retired peer left")]

    def test_the_clients_own_account_and_each_shortfall_named(self) -> None:
        """A client's own run that matched S's selection is a full delivery (batch 8: the page had called it
        lost); every obligation not delivered in full names its carrier and why."""
        s = self.S + [(50, "Online: spill EA_Manager_W1_actor_0 w00000f2c.09389445 0#1 selected 3 items cap 9: 2 peers"),
                      (55, "Online: spill (null) w00000f2e.930d1f89 0#1 selected 1 items cap 9: 2 peers"),
                      (30, f"Online: enemy placed {K1} as EA_Manager_W1_actor_0#3901 hp 1 pose 1 (S)"),
                      (99, "Online: spill obligations to peer 1: 2 made, 2 completed, 0 ended otherwise, 0 open (it left)"),
                      (99, "Online: spill obligations to peer 2: 2 made, 0 completed, 2 ended otherwise, 0 open (it left)")]
        h = [(51, "Online: spill fulfilled EA_Manager_W1_actor_0 w00000f2c.09389445 0#1 (S selected 3 items, this game's "
                  "own run matched)"),
             (56, "Online: spill fulfilled (null) w00000f2e.930d1f89 0#1 (S selected 1 items, here spawned 1)"),
             (98, "Online: loot delivery: 2 obligations, 2 delivered in full, 0 differ, 0 short, 0 unfinished, 0 own runs "
                  "without S's obligation (S's selections give 4 items, here spawned 4)")]
        d = [(45, f"Online: enemy bind {K1} by authored id 0629f659.1.1 waits (asleep here, its copy at b8): never counted"),
             (68, "Online: spill expired ? w00000f2c.09389445 0#1 (the carrier is not resolved here yet after 18 s: the "
                  "obligation ends unfulfilled, S's selection is withdrawn here)"),
             (73, "Online: spill expired ? w00000f2e.930d1f89 0#1 (the carrier is not resolved here yet after 18 s: the "
                  "obligation ends unfulfilled, S's selection is withdrawn here)"),
             (98, "Online: loot delivery: 2 obligations, 0 delivered in full, 0 differ, 2 short, 0 unfinished, 0 own runs "
                  "without S's obligation (S's selections give 4 items, here spawned 0)")]
        v = lc.grade_loot(RunFolder(s, H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertIn("H: 2 obligations, 2 delivered in full, 0 differ, 0 short, 0 unfinished", v.counts)
        self.assertIn("S to peer 1: 2 made, 2 completed, 0 ended otherwise", v.counts)
        self.assertIn("D: 2 obligations, 0 delivered in full", v.counts)
        whats = [x.what for x in v.findings]
        self.assertEqual(whats, ["D: 1 obligation(s) not delivered in full, its carrier's copy is asleep here (a far "
                                 "player: no orb where it died)",
                                 "D: 1 obligation(s) not delivered in full, its carrier has no row here (S's entity is "
                                 "not bound here: nobody there sees the drop)"])
        self.assertTrue(v.findings[0].lines[0].startswith("w00000f2c (obligation 1) carrier EA_Manager_W1_actor_0: its "
                                                          "copy slept here; D/game.log:"))
        self.assertTrue(v.findings[1].lines[0].startswith("w00000f2e (obligation 3) carrier (null): S's entity is not "
                                                          "bound here"))

    def test_all_in_full_passes(self) -> None:
        s = self.S[:4] + [(99, f"Online: spill obligations to peer {p}: 2 made, 2 completed, 0 ended otherwise, 0 open "
                               "(it left)") for p in (1, 2)]
        acct = (98, "Online: loot delivery: 2 obligations, 2 delivered in full, 0 differ, 0 short, 0 unfinished, 0 own "
                    "runs without S's obligation (S's selections give 4 items, here spawned 4)")
        full = [(51, "Online: spill fulfilled x w00000f2c.09389445 0#1 (S selected 3 items, here spawned 3)"),
                (56, "Online: spill fulfilled x w00000f2e.0738911f 0#1 (S selected 1 items, this game's own run matched)"),
                acct]
        v = lc.grade_loot(RunFolder(s, H_JOIN + full + LEVEL_END, D_JOIN + full + LEVEL_END).run())
        self.assertEqual(v.status, lc.PASS, v.findings)

    def test_run_time_carriers_by_their_row(self) -> None:
        """Batch 11 run 4: a run-time enemy's drop is named by its row (`s<spawner>.<tag>#<n>`); two sibling
        bombers of one spawner are two carriers; one a client could not deliver is named with its line."""
        b1, b2 = "sb7fe49cd.52540001#1", "sb7fe49cd.52540001#2"
        s = [(50, f"Online: spill BomberSpawner_02_Spawned1 {b1} 0#1 selected 3 items cap 9: 2 peers present"),
             (50, f"Online: spill obliged {b1} 00000001 to peer 1 (obligation 4)"),
             (50, f"Online: spill obliged {b1} 00000001 to peer 2 (obligation 4)"),
             (52, f"Online: spill BomberSpawner_02_Spawned2 {b2} 0#1 selected 2 items cap 9: 2 peers present"),
             (52, f"Online: spill obliged {b2} 00000001 to peer 1 (obligation 5)"),
             (52, f"Online: spill obliged {b2} 00000001 to peer 2 (obligation 5)"),
             (99, "Online: spill obligations to peer 1: 2 made, 2 completed, 0 ended otherwise, 0 open (it left)"),
             (99, "Online: spill obligations to peer 2: 2 made, 1 completed, 1 ended otherwise, 0 open (it left)")]
        h = [(51, f"Online: spill fulfilled BomberSpawner_02_Spawned1 {b1} 0#1 (S selected 3 items, here spawned 3)"),
             (53, f"Online: spill fulfilled BomberSpawner_02_Spawned2 {b2} 0#1 (S selected 2 items, here spawned 2)"),
             (98, "Online: loot delivery: 2 obligations, 2 delivered in full, 0 differ, 0 short, 0 unfinished, 0 own runs "
                  "without S's obligation (S's selections give 5 items, here spawned 5)")]
        d = [(51, f"Online: spill fulfilled BomberSpawner_02_Spawned1 {b1} 0#1 (S selected 3 items, here spawned 3)"),
             (53, f"Online: spill lost ? {b2} 0#1 (its run-time row ended here with no copy bound: dead or removed "
                  "before its bind, so no copy here carries its drop)"),
             (98, "Online: loot delivery: 2 obligations, 1 delivered in full, 0 differ, 1 short, 0 unfinished, 0 own runs "
                  "without S's obligation (S's selections give 5 items, here spawned 3)")]
        v = lc.grade_loot(RunFolder(s, H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertTrue(v.counts.startswith("S obliged 2 spill(s), 4 deliveries to the clients"), v.counts)
        self.assertIn("H: 2 obligations, 2 delivered in full", v.counts)
        self.assertEqual(len(v.findings), 1)
        self.assertTrue(v.findings[0].what.startswith("D: 1 obligation(s) not delivered in full"))
        self.assertTrue(v.findings[0].lines[0].startswith(f"{b2} (obligation 5) carrier BomberSpawner_02_Spawned2: "
                                                          "D/game.log:"), v.findings[0].lines)

    def test_loot_off_is_not_tested(self) -> None:
        v = lc.grade_loot(RunFolder([(1, "Online: loot hooks off (online_shared_loot)")], H_JOIN + [
            (30, "Online: collect native ffffffff (not reported: no source) (827513f4)")]).run())
        self.assertEqual(v.status, lc.NOT_TESTED)
        self.assertIn("H 1", v.notes[0])


class LobbyAndSlot(unittest.TestCase):
    def test_a_late_join_does_not_test_the_lobby(self) -> None:
        r = RunFolder([(2, "Online: title entry: continue slot 1 (server: the owner's pick)")], H_JOIN, D_JOIN).run()
        self.assertEqual(lc.grade_lobby(r).status, lc.NOT_TESTED)
        self.assertEqual(lc.grade_slot(r).status, lc.PASS)
        hub = RunFolder([], H_JOIN, [(10, "Online: connected as peer 2 (server tick 9, level 1 town/town_state10/town_state10)")]).run()
        self.assertEqual(lc.grade_lobby(hub).status, lc.PASS)
        self.assertEqual(lc.grade_slot(RunFolder([], H_JOIN).run()).status, lc.NOT_TESTED)


@unittest.skipUnless((GAME_REPO / "tools" / "scene_sync_check.py").is_file(), "no game repository beside this portal")
class Scenes(unittest.TestCase):
    def test_only_the_time_both_were_in_the_level_counts(self) -> None:
        h = [(22, "Online: scene local 2/L1 start cutscene Tutorial_B_Intro recipient - from the level load"),
             (30, "Online: scene local 2/L1 end by natural after 8.0 s (no server scene)"),
             (60, "Online: scene local 2/L2 start cutscene TB_GreebleLobberIntro recipient - from x"),
             (70, "Online: scene local 2/L2 end by natural after 10.0 s (no server scene)")]
        d = [(60.4, "Online: scene local 2/L1 start cutscene TB_GreebleLobberIntro recipient - from x"),
             (73.5, "Online: scene local 2/L1 end by natural after 13.1 s (no server scene)")]
        v = lc.grade_scenes(RunFolder([], H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run(), GAME_REPO)
        self.assertEqual(v.status, lc.FAIL)  # H left the lobber intro 3.5 s before D
        self.assertIn("0 of 1 scene(s) both clients were in", v.counts)
        self.assertIn("1 outside the time both were in the level", v.counts)  # the intro before D came
        self.assertTrue(v.findings[0].lines[0].startswith("H/game.log:"), v.findings[0].lines)
        skipped = RunFolder([], H_JOIN + h, D_JOIN + d, peers=[{"command": "x --dev_skip_scenes=true"}])
        self.assertEqual(lc.grade_scenes(skipped.run(), GAME_REPO).status, lc.NOT_TESTED)


# The next level reached: its load after the level under test and its start released on that game (the game's
# own evidence; the script's marker after it proves nothing by itself).
H_NEXT = [(214, "Online: level 3 town/town_state10/town_state10: load"), (216, "Online: barrier 3 released at tick 900"),
          (216.5, "Input script: LEVELPASS phase next-level")]
D_NEXT = [(214, "Online: level 3 town/town_state10/town_state10: load"), (216, "Online: barrier 3 released at tick 900"),
          (216.5, "Input script: LEVELPASS D next level")]


class Completable(unittest.TestCase):
    def verdict(self, lp: dict | None, h=(), d=()) -> lc.Verdict:
        result = {"levelpass": lp} if lp is not None else {}
        return lc.grade_completable(RunFolder([], H_JOIN + list(h), D_JOIN + list(d), result=result).run())

    def test_the_pass_and_both_marks(self) -> None:
        v = self.verdict({"verdict": "PASS", "not_covered": []}, H_NEXT, D_NEXT)
        self.assertEqual(v.status, lc.PASS)
        self.assertIn("H yes, D yes", v.counts)
        self.assertTrue(v.example.startswith("H/game.log:5: Online: barrier 3 released"), v.example)  # the game's line

    def test_a_marker_after_a_timed_out_wait_is_not_reached(self) -> None:
        # Batch 11 run 4 (tutorial_b 20261119-063529): the marker came right after the wait for the level change
        # timed out; the level never ended.
        h = [(200, "Input script: waitfor online_level_after_mark timed out after 150.0 s"),
             (200.1, "Input script: LEVELPASS phase next-level")]
        v = self.verdict({"verdict": "PASS", "not_covered": []}, h, D_NEXT)
        self.assertEqual(v.status, lc.FAIL)
        self.assertIn("next level reached (the level change and play on that game): H no, D yes", v.counts)
        self.assertTrue(v.findings[0].lines[0].startswith("H: no level loaded after tutorial_b; H/game.log:4: Input "
                                                          "script: waitfor online_level_after_mark timed out"),
                        v.findings[0].lines)

    def test_incomplete_failed_and_no_pass_are_never_a_pass(self) -> None:
        v = self.verdict({"verdict": "INCOMPLETE", "not_covered": ["the level's end and the next level (not reached)"]})
        self.assertEqual(v.status, lc.INCOMPLETE)
        self.assertEqual(v.notes[0], "not covered: the level's end and the next level (not reached)")
        self.assertEqual(self.verdict({"verdict": "PASS", "not_covered": []}, H_NEXT).status, lc.FAIL)  # D no mark
        self.assertEqual(self.verdict(None).status, lc.NOT_TESTED)
        self.assertEqual(lc.overall([v, lc.Verdict("x", lc.PASS, "")]), "INCOMPLETE")
        self.assertEqual(lc.overall([lc.Verdict("x", lc.PASS, ""), lc.Verdict("y", lc.NOT_TESTED, "")]),
                         "NOT ALL TESTED: y")
        # A FAIL names every row nobody tested next to it, so the rest is never read as passed.
        rows = [lc.Verdict("other player", lc.FAIL, ""), lc.Verdict("completable", lc.NOT_TESTED, ""),
                lc.Verdict("lobby", lc.NOT_TESTED, ""), lc.Verdict("pickups", lc.PASS, "")]
        self.assertEqual(lc.overall(rows), "FAIL: other player; NOT TESTED: completable, lobby")
        self.assertEqual(lc.overall([v, rows[1]]), "INCOMPLETE; NOT TESTED: completable")


class Command(unittest.TestCase):
    """`levelcheck <level>`: the level pass (its guards are cmd_levelpass's: refused, it returns its code and
    no page is written), then the graders on its run folder, one page in the levelpass folder."""

    def setUp(self) -> None:
        import types
        import skytest
        self.sk = skytest
        tmp = tempfile.TemporaryDirectory()
        FOLDERS.append(tmp)
        self.lp_folder = Path(tmp.name) / "levelpass"
        self.lp_folder.mkdir()
        self.cfg = types.SimpleNamespace(repo=Path(tmp.name) / "game")
        self.real = skytest.cmd_levelpass
        self.addCleanup(setattr, skytest, "cmd_levelpass", self.real)

    def args(self, **kw):
        import argparse
        base = dict(level="tutorial_b", grade=None, predictions=False, plan_only=False, folder=str(self.lp_folder))
        base.update(kw)
        return argparse.Namespace(**base)

    def test_a_refused_pass_writes_no_page(self) -> None:
        self.sk.cmd_levelpass = lambda cfg, a: self.sk.EXIT_KNOWN_FAILURE
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.sk.cmd_levelcheck(self.cfg, self.args()), self.sk.EXIT_KNOWN_FAILURE)
        self.assertIn("no run to grade", out.getvalue())
        self.assertEqual(list(self.lp_folder.glob("*-check-*.txt")), [])

    def test_the_pass_run_is_graded_on_one_page(self) -> None:
        run = RunFolder([], H_JOIN + H_NEXT, D_JOIN + D_NEXT,
                        result={"levelpass": {"verdict": "PASS", "not_covered": []}, "case": "levelpass-tutorial_b"})

        def fake(cfg, a):
            a.check["run"] = run.path
            a.check["report"] = self.lp_folder / "tutorial_b-pass-x.txt"
            return 0
        self.sk.cmd_levelpass = fake
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.sk.cmd_levelcheck(self.cfg, self.args())
        pages = list(self.lp_folder.glob("tutorial_b-check-*.txt"))
        self.assertEqual(len(pages), 1)
        text = pages[0].read_text(encoding="utf-8")
        self.assertIn("overall: NOT ALL TESTED", text)  # lobby (late join), scenes, enemies ... said, not passed
        self.assertIn("completable  PASS", text)
        self.assertEqual(code, 0)



class OtherPlayer(unittest.TestCase):
    """What a player sees of the other: a stand-in over STANDIN_MAX_S fails, a real body of the character that
    player plays at that moment passes, a swap is followed (the newest own row before each body line)."""
    H_ROW = [(10, "Online: own player row TopsyDerby figure 3012 state 0 swap 2 (epoch 1)")]
    D_ROW = [(41, "Online: own player row Scorpion figure 3003 state 0 swap 0 (epoch 1)")]

    def grade(self, h_extra, d_extra=()) -> lc.Verdict:
        return lc.grade_bodies(RunFolder([], H_JOIN + self.H_ROW + list(h_extra) + LEVEL_END,
                                         D_JOIN + self.D_ROW + list(d_extra) + LEVEL_END).run())

    def test_tonights_stand_in_fails(self) -> None:
        v = self.grade([(50, "Online: puppet 2 spawned scorpion at 1,2,3 (epoch 1 gen 2, stand-in from Hero_topsyderby)"),
                        (50, "Online: puppet 2 epoch 1 gen 2 body scorpion (stand-in) swap 1 tick 19268"),
                        (180, "Online: puppet 2 despawned (generation)")])
        self.assertEqual(v.status, lc.FAIL)
        self.assertIn("H sees D: 2 body line(s), 130 s shown, 130 s of it a stand-in", v.counts)
        self.assertIn("stand-in scorpion for 130 s", v.findings[0].what)
        self.assertTrue(v.findings[0].lines[0].startswith("H/game.log:"), v.findings[0].lines)

    def test_a_short_stand_in_then_the_real_body_passes(self) -> None:
        v = self.grade([(50, "Online: puppet 2 spawned scorpion at 1,2,3 (epoch 1 gen 2, stand-in from Hero_topsyderby)"),
                        (53, "Online: puppet 2 epoch 1 gen 2 body scorpion (real) swap 1 tick 19300")],
                       [(60, "Online: puppet 1 spawned topsyderby at 1,2,3 (epoch 1 gen 2, real)")])
        self.assertEqual(v.status, lc.PASS, v.findings)

    def test_a_swap_is_followed(self) -> None:
        # D swaps to Wash Buckler at 90 s; H's puppet keeps showing scorpion: wrong from then on.
        v = self.grade([(50, "Online: puppet 2 epoch 1 gen 2 body scorpion (real) swap 1 tick 1"),
                        (95, "Online: puppet 2 epoch 1 gen 2 body scorpion (real) swap 2 tick 2")],
                       [(90, "Online: own player row WashBuckler figure 3050 state 0 swap 1 (epoch 1)")])
        self.assertEqual(v.status, lc.FAIL)
        self.assertEqual(v.findings[0].what, "H shows D as scorpion, but that player plays washbuckler")

    def test_no_other_player_is_not_tested(self) -> None:
        self.assertEqual(self.grade([]).status, lc.NOT_TESTED)

    def test_every_page_says_what_it_does_not_look_at(self) -> None:
        text = lc.page(Path("run"), [lc.Verdict("x", lc.PASS, "c")])
        self.assertIn("what this check does NOT look at", text)
        self.assertIn("food and the wallet beyond the coins' gold lines", text)

class StaticTable(unittest.TestCase):
    def test_every_level_with_data_one_row(self) -> None:
        import json
        tmp = tempfile.TemporaryDirectory()
        FOLDERS.append(tmp)
        folder = Path(tmp.name)

        def level(name: str, comps: list[str], when: str, chain: bool = True) -> None:
            (folder / name).mkdir()
            thing = {"key": "u.Thing", "name": "Thing", "unit": "u", "zone": name, "class": "CEntity", "group": "other",
                     "kind": "", "position": [0, 0, 0], "components": [{"key": c, "class": c} for c in comps]}
            spawner = dict(thing, key="u.Sp", name="Sp", components=[{"key": "s", "class": "Scripts.SpawnerComponentData"}])
            logic = {"schema": 1, "nodes": [thing, spawner],
                     "edges": [{"from": "u.Thing", "to": [], "message": "CLoadMapComponentMessage", "fields": {},
                                "when": when, "roles": "4"}],
                     "level_end": ["Thing -[CLoadMapComponentMessage {_fade: true}]-> roles 4"] if chain else []}
            (folder / name / "logic.json").write_text(json.dumps(logic), encoding="utf-8")
        level("verdant1", ["Scripts.EnterVehicleComponentData"], "Scripts.EnterVehicleComponentData.OnExit")
        level("pvp_barrens", [], "", chain=False)
        (folder / "verdant1-manifest.txt").write_text("probe", encoding="utf-8")
        text = lc.static_table(folder)
        lines = text.splitlines()
        self.assertIn("2 levels", lines[0])
        row = next(x for x in lines if x.startswith("verdant1 "))
        self.assertIn("the pass cannot end it", row)
        self.assertIn("a turret or vehicle: Thing", row)
        self.assertTrue(row.rstrip().endswith("yes"))  # probe data
        pvp = next(x for x in lines if x.startswith("pvp_barrens "))
        self.assertIn("versus arena", pvp)
        self.assertIn("no level end in the data", pvp)
        self.assertIn("  verdant1: the end needs a turret or vehicle at Thing", text)


if __name__ == "__main__":
    unittest.main()
