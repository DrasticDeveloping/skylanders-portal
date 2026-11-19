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
    def __init__(self, s: list, h: list, d: list | None = None, peers: list | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        FOLDERS.append(self.tmp)
        self.path = Path(self.tmp.name)
        (self.path / "H" / "data" / "Online" / "session").mkdir(parents=True)
        (self.path / "H" / "data" / "Online" / "session" / "game.log").write_text(log(s), encoding="utf-8")
        (self.path / "H" / "game.log").write_text(log(h), encoding="utf-8")
        if d is not None:
            (self.path / "D").mkdir()
            (self.path / "D" / "game.log").write_text(log(d), encoding="utf-8")
        if peers is not None:
            import json
            (self.path / "result.json").write_text(json.dumps({"peers": peers}), encoding="utf-8")

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
                       [(45, f"Online: enemy replica {K1} at b8 asleep (mID 0x1): the binding waits for its wake"),
                        (61, f"Online: enemy life {K1} alive -> dead (its copy made here was asleep: removed)")])
        self.assertEqual(v.status, lc.PASS, v)
        self.assertIn("H: attached 1 of 1 it should have, deaths seen 1 of 1", v.counts)
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
        self.assertIn("D: attached 0 of 0", v.counts)

    def test_no_enemy_is_not_a_pass(self) -> None:
        self.assertEqual(self.grade([], []).status, lc.NOT_TESTED)


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

    def test_a_lost_and_a_short_delivery(self) -> None:
        h = [(51, "Online: spill waits EA_Manager_W1_actor_0 w00000f2c.09389445 0#1 (the carrier is not resolved here yet)"),
             (56, "Online: spill fulfilled EA_Manager_W1_actor_2 w00000f2e.0738911f 0#1 (S selected 3 items, here spawned 3)")]
        d = [(56, "Online: spill fulfilled EA_Manager_W1_actor_0 w00000f2c.09389445 0#1 (S selected 3 items, here spawned 3)"),
             (57, "Online: spill fulfilled EA_Manager_W1_actor_2 w00000f2e.0738911f 0#1 (S selected 3 items, here spawned 1)")]
        v = lc.grade_loot(RunFolder(self.S, H_JOIN + h + LEVEL_END, D_JOIN + d + LEVEL_END).run())
        self.assertEqual(v.status, lc.FAIL)
        self.assertIn("delivered in full 2, short or lost 2", v.counts)
        whats = sorted(f.what for f in v.findings)
        self.assertEqual(whats, ["D spawned 1 of the 3 items S selected for w00000f2e",
                                 "H never ran S's selection for w00000f2c (obligation 1)"])
        lost = next(f for f in v.findings if f.what.startswith("H"))
        self.assertEqual(len(lost.lines), 3)  # S's obligation, H's last spill line, S's end of it
        self.assertIn("spill waits", lost.lines[1])

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


if __name__ == "__main__":
    unittest.main()
