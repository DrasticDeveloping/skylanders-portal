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
        self.assertIn("coins lying in the level", text)

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
