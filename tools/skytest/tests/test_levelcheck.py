"""The level check's graders (levelcheck.py) on made-up run folders: each client's time in a level, the
enemy, pickup and loot pairing between S and the clients, the scene window, and that a grader with nothing
to grade never says PASS. The lines are the games' own shapes (copied from the runs of 18-19 November).
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_levelcheck.py"
"""
from __future__ import annotations

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
            (self.path / "result.json").write_text(json.dumps({"peers": peers or [], **(result or {})}),
                                                   encoding="utf-8")

    def run(self) -> lc.Run:
        return lc.load_run(self.path)


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
        self.assertTrue(first.startswith("TutorialLockedGate (key gate): performed ("), first)
        self.assertIn("opened on H at 00:11:25.000, not on S, D; key touched and following on H: H/game.log:", first)
        self.assertIn("; S for the key: H/data/Online/session/game.log:1: Online: world unsupported carried_item", first)
        self.assertIn("; no held press at the lock on H (", first)
        self.assertIn("; interact on H: H/game.log:", first)
        self.assertIn("; D's own key: no line after its level start", first)

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

    def test_no_follow_is_not_tested_and_the_rows_say_where_they_were_read(self) -> None:
        v = lc.grade_together(RunFolder(self.S, H_JOIN + self.H + LEVEL_END, D_JOIN + LEVEL_END).run())
        self.assertEqual(v.status, lc.NOT_TESTED)
        self.assertIn("D stayed where it entered", v.counts)


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
        self.assertIn("coins taken 1, paid on another game 1", v.counts)

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
        self.assertEqual(whats[0], "a fight step failed with no damage done: 17 burst(s) at 3085,-534,3746, no hit "
                                   "took a hit point")
        self.assertIn("S's kills by player (`dead by <peer>`): H 11, D 0", v.findings[0].lines)
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
        self.assertEqual(v.status, lc.PASS, [x.what for x in v.findings])
        self.assertTrue(v.notes[0].startswith("scenery, not counted (the level never releases them): Ambi_Chompy_01,"))
        held = [n for n in v.notes if n.startswith("held, not released in this run: ")]
        self.assertEqual([n.split(": ", 1)[1].split(" (")[0] for n in held], ["Chompy", "Chompy001", "Chompy002", "Chompy003"])
        self.assertIn("SwapPartsHint_RemoveTrigger", held[0])
        self.assertIn("hit only held enemies (Ambi_Chompy_03 scenery)", v.counts)
        self.assertIn("set apart: 9 scenery, 4 held and not released in this run", v.counts)

    @unittest.skipUnless((CALIBRATION_RUNS / "20261119-061110-levelpass-tutorial_b").is_dir(), "batch 11's run is not here")
    def test_batch_11_follow_run_passes_with_its_slept_spawn_apart(self) -> None:
        v = self.grade(CALIBRATION_RUNS / "20261119-061110-levelpass-tutorial_b")
        self.assertEqual(v.status, lc.PASS, [x.what for x in v.findings])
        self.assertIn("D: bound 11 of 11", v.counts)
        self.assertIn("gone to sleep on the server before any player's game made it: 1", v.counts)

    def test_033620_fails_only_on_the_bomber_no_client_had(self) -> None:
        v = self.grade(TUTORIAL_B)
        self.assertEqual([x.what for x in v.findings],
                         ["existed on no client: sb7fe49cd.52540001#3/0@2 (BomberSpawner_02_Spawned3#3951)"])


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
        self.assertIn("H: tracked 1 spawner(s); made 2 of 2 within 1 s; places 2 pair(s), 0 over 64", v.counts)
        self.assertIn("gone 1 of S's 1", v.counts)

    def test_late_far_missing_and_not_gone(self) -> None:
        h = [(11, "Online: world tracked spawner_fires EZ3_IslandSpawner0#2460 (Scripts.SpawnerComponent; its first island)"),
             (52, "Online: world island spawner EZ3_IslandSpawner0#2460 made 1 here (S's 1, 120 ticks after S): x#1"),
             (51.1, "Online: world island EZ3_IslandSpawner0#2460 #1 at -4900.0,-3012.0,-38.0")]
        v = lc.grade_pieces(RunFolder(self.S, H_JOIN + h + LEVEL_END).run())
        whats = [x.what for x in v.findings]
        self.assertEqual(whats, ["H: 1 of S's island fires never made here", "H: 1 island(s) made more than 1 s from S's fire",
                                 "H: 1 island place(s) more than 64 from S's", "H: 1 island end(s) of S not followed here "
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
                                            "within 1 s; places 210 pair(s), 0 over 64 (median 1.6"), v.counts)



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


H_NEXT = [(216, "Input script: LEVELPASS phase next-level")]
D_NEXT = [(216, "Input script: LEVELPASS D next level")]


class Completable(unittest.TestCase):
    def verdict(self, lp: dict | None, h=(), d=()) -> lc.Verdict:
        result = {"levelpass": lp} if lp is not None else {}
        return lc.grade_completable(RunFolder([], H_JOIN + list(h), D_JOIN + list(d), result=result).run())

    def test_the_pass_and_both_marks(self) -> None:
        v = self.verdict({"verdict": "PASS", "not_covered": []}, H_NEXT, D_NEXT)
        self.assertEqual(v.status, lc.PASS)
        self.assertIn("H yes, D yes", v.counts)
        self.assertTrue(v.example.startswith("H/game.log:4:"), v.example)

    def test_incomplete_failed_and_no_pass_are_never_a_pass(self) -> None:
        v = self.verdict({"verdict": "INCOMPLETE", "not_covered": ["the level's end and the next level (not reached)"]})
        self.assertEqual(v.status, lc.INCOMPLETE)
        self.assertEqual(v.notes, ["not covered: the level's end and the next level (not reached)"])
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
