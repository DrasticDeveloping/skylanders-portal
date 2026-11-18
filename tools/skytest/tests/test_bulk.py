"""Tests for skytest's bulk mode (`test --bulk`: hidden games with the draw skip, scaled waits, its own
budget and labels). Run: python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import skytest  # noqa: E402
import test_skytest as base  # noqa: E402  (its throwaway game repository; its tests run from their own module)

_REAL = {}


def setUpModule() -> None:
    _REAL.update(token=skytest.token_restriction, cpu=skytest.CPU_SAMPLE_S, gb=skytest.GB_PER_GAME)
    skytest.token_restriction = lambda: ""
    skytest.CPU_SAMPLE_S = 0.1
    skytest.GB_PER_GAME = 0.0


def tearDownModule() -> None:
    skytest.token_restriction = _REAL["token"]
    skytest.CPU_SAMPLE_S = _REAL["cpu"]
    skytest.GB_PER_GAME = _REAL["gb"]


def peer(name: str, script: str = "quit\n", args: list[str] | None = None, expect: list[str] | None = None,
         **kw) -> skytest.PeerSpec:
    return skytest.PeerSpec(name=name, script=script, args=list(args or []), expect=list(expect or []), **kw)


def spec(name: str = "c", layout: str = "server+1", server: str = "host", **kw) -> skytest.Spec:
    peers = kw.pop("peers", None) or {"S": peer("S", ""), "H": peer("H")}
    return skytest.Spec(name=name, path=Path(f"{name}.toml"), layout=layout, stage="8", description="", timeout=100,
                        peers=peers, measures={}, standin_seconds=45, server=server, **kw)


class ScaleWaitsTest(unittest.TestCase):
    def test_every_waitfor_timeout_is_scaled_and_nothing_else(self) -> None:
        script = ("waitfor title_ready 35\ntap start\n  waitfor menu_ready 8\nwait 5\nwaitfor online_barrier 2.5\n"
                  "log waitfor x 9\nfps 5\nquit\n")
        out = skytest.scale_waitfors(script, 2.0)
        self.assertEqual(out, "waitfor title_ready 70\ntap start\n  waitfor menu_ready 16\nwait 5\n"
                              "waitfor online_barrier 5\nlog waitfor x 9\nfps 5\nquit\n")
        self.assertEqual(skytest.scale_waitfors("waitfor menu_ready 8\n", 1.5), "waitfor menu_ready 12\n")
        self.assertEqual(skytest.scale_waitfors("waitfor menu_ready 7\n", 1.5), "waitfor menu_ready 10.5\n")
        self.assertIs(skytest.scale_waitfors(script, 1), script)

    def test_scaled_script_still_parses_for_the_launch_builder(self) -> None:
        out = skytest.scale_waitfors("waitfor menu_ready 7\r\nquit\r\n".replace("\r\n", "\n"), 1.5)
        line = out.splitlines()[0].split()
        self.assertEqual(line[0], "waitfor")
        self.assertGreater(float(line[2]), 0)

    def test_bulk_spec_scales_the_case_timeout_and_a_standin_server(self) -> None:
        s = spec(peers={"S": peer("S", ""), "H": peer("H", "waitfor title_ready 35\nquit\n")})
        b = skytest.bulk_spec(s, skytest.BulkMode(wait_factor=2.0))
        self.assertEqual(b.timeout, 200)
        self.assertEqual(b.standin_seconds, 90)
        self.assertEqual(b.peers["H"].script, "waitfor title_ready 70\nquit\n")
        self.assertEqual(s.peers["H"].script, "waitfor title_ready 35\nquit\n", "the spec itself is unchanged")
        self.assertEqual(b.sha256, s.sha256)


class ExclusionTest(unittest.TestCase):
    def test_a_plain_functional_spec_runs_in_bulk(self) -> None:
        self.assertEqual(skytest.bulk_exclusion(spec()), "")
        self.assertEqual(skytest.bulk_exclusion(spec(layout=skytest.UNIT, peers={}, exclusive=True)), "")

    def test_each_rule(self) -> None:
        cases = [
            (spec(bulk=False, bulk_reason="its subject is X"), "bulk = false: its subject is X"),
            (spec(exclusive=True), "runs alone (exclusive = true)"),
            (spec(grades={"client_fps": {}}), "runs alone (client_fps)"),
            (spec(compare={"run": [{"name": "a"}]}), "runs alone (compare)"),
            (spec(peers={"S": peer("S", ""), "H": peer("H", kind="remote")}), "runs alone (a remote peer)"),
            (spec(show_server_window=True), "show_server_window = true"),
            (spec(peers={"S": peer("S", ""), "H": peer("H", args=["--render_hidden_draw=true"])}),
             "peer H sets --render_hidden_draw itself"),
            (spec(peers={"S": peer("S", ""), "H": peer("H", args=["--dev_server_draw=true"])}),
             "peer H sets --dev_server_draw itself"),
            (spec(peers={"S": peer("S", ""), "H": peer("H", args=["--guest_vblank_rate=120"])}),
             "peer H sets --guest_vblank_rate itself"),
            (spec(peers={"S": peer("S", ""), "H": peer("H", settings_file="none")}), "release defaults"),
            (spec(peers={"S": peer("S", ""), "H": peer("H", expect=[r"Online: panel drawn \d+"])}),
             "Online: panel drawn"),
            (spec(peers={"S": peer("S", ""), "H": peer("H", forbid=[r"\(no picture\)"])}), "grades a picture"),
        ]
        for s, want in cases:
            got = skytest.bulk_exclusion(s)
            if want is None:
                self.assertEqual(got, "", s)
            else:
                self.assertIn(want, got, s)

    def test_shot_and_the_settings_menu_stay_in_bulk(self) -> None:
        s = spec(peers={"S": peer("S", ""), "H": peer("H", "shot hub\nkey F4\ntap rb\nquit\n")})
        self.assertEqual(skytest.bulk_exclusion(s), "")


class PeerArgsTest(unittest.TestCase):
    def test_hidden_with_the_skip_on_every_game(self) -> None:
        b = skytest.BulkMode()
        hosted = spec(peers={"S": peer("S", ""), "H": peer("H"), "D": peer("D")}, layout="server+2")
        self.assertEqual(skytest.bulk_peer_args(b, hosted, "H"), skytest.BULK_HIDDEN)
        self.assertEqual(skytest.bulk_peer_args(b, hosted, "D"), skytest.BULK_HIDDEN)
        standin = spec(server="standin")
        self.assertEqual(skytest.bulk_peer_args(b, standin, "S"), skytest.BULK_HIDDEN)
        self.assertNotIn("--dev_show_server_window=true", skytest.bulk_peer_args(b, hosted, "H"))

    def test_trap_1_an_h_that_keeps_its_server_drawing_gets_no_skip_to_forward(self) -> None:
        s = spec(peers={"S": peer("S", ""), "H": peer("H", args=["--dev_server_draw=true"])})
        self.assertEqual(skytest.bulk_peer_args(skytest.BulkMode(), s, "H"), ["--window_hidden=true"])

    def test_pacing_arms(self) -> None:
        hosted = spec(peers={"S": peer("S", ""), "H": peer("H"), "D": peer("D")}, layout="server+2")
        standin = spec(server="standin")
        v = skytest.BulkMode(pacing="vblank240")
        self.assertEqual(skytest.bulk_peer_args(v, hosted, "H"), [*skytest.BULK_HIDDEN, "--guest_vblank_rate=240"])
        self.assertEqual(skytest.bulk_peer_args(v, standin, "S"), [*skytest.BULK_HIDDEN, "--guest_vblank_rate=240"])
        h = skytest.BulkMode(pacing="hz30")
        self.assertEqual(skytest.bulk_peer_args(h, hosted, "H"),
                         [*skytest.BULK_HIDDEN, "--present_interval=2", "--online_server_hz=30"])
        self.assertEqual(skytest.bulk_peer_args(h, hosted, "D"), [*skytest.BULK_HIDDEN, "--present_interval=2"])
        self.assertEqual(skytest.bulk_peer_args(h, standin, "H"), [*skytest.BULK_HIDDEN, "--present_interval=2"])
        self.assertIn("pacing hz30", h.line())


class RunLoadTest(unittest.TestCase):
    def test_busy_threads(self) -> None:
        self.assertEqual(skytest.busy_threads((100, 1000), (150, 1100), 16), 8.0)
        self.assertIsNone(skytest.busy_threads((100, 1000), (100, 1000), 16))
        self.assertEqual(skytest.busy_threads((100, 1000), (300, 1100), 16), 0.0)

    def test_lowest_client_fps_leaves_the_server_out(self) -> None:
        r1 = skytest.CaseResult(case="a", layout="server+1", stage="8", peers=[
            skytest.PeerResult(peer="S", fps=[10.0], frames_fps=[5.0]),
            skytest.PeerResult(peer="H", fps=[44.0, 31.0], frames_fps=[58.0])])
        r2 = skytest.CaseResult(case="b", layout="server+1", stage="8", peers=[
            skytest.PeerResult(peer="on:H", frames_fps=[40.0, 22.5])])
        got = skytest.lowest_client_fps([r1, r2])
        self.assertEqual((got["fps"]["min"], got["fps"]["case"], got["fps"]["peer"]), (31.0, "a", "H"))
        self.assertEqual((got["frames"]["min"], got["frames"]["case"], got["frames"]["windows"]), (22.5, "b", 3))
        line = skytest.run_load_line({"games_max": 8, "games_mean": 7.5, "threads_max": 12.0, "threads_mean": 10.5,
                                      "threads_of": 16, "client_fps": got})
        self.assertIn("games at once 8 max", line)
        self.assertIn("CPU threads used 10.5 mean, 12.0 max of 16", line)
        self.assertIn("lowest client fps windows 31.0 (a H", line)

    def test_payload_mode(self) -> None:
        self.assertEqual(skytest.payload_mode({"mode": "bulk"}), "bulk")
        self.assertEqual(skytest.payload_mode({}), "")
        self.assertEqual(skytest.payload_mode([]), "")


class AdmissionTest(unittest.TestCase):
    def test_free_memory_per_game_is_the_budget_s_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            slots = Path(tmp) / skytest.STORE / "slots"
            real = skytest.available_memory_gb
            skytest.available_memory_gb = lambda: 10.0
            try:
                small = skytest.MachineBudget(slots, 12, gb_per_game=1.0)
                lease = small.acquire(4, "four", memory_games=4)
                self.assertEqual(len(lease.slots), 4, "4 games x 1 GB fit in 10 GB")
                lease.release()
                big = skytest.MachineBudget(slots, 12, gb_per_game=3.0)
                skytest.memory_recheck = (lambda want, mem, seconds=None, every=1.0: mem)
                lease = big.acquire(4, "four", memory_games=4)
                self.assertEqual(lease.slots, [])
                self.assertIn("needs 12.0 GB of free memory for 4 game(s)", lease.capacity)
            finally:
                skytest.available_memory_gb = real
                skytest.memory_recheck = REAL_RECHECK


REAL_RECHECK = skytest.memory_recheck


class BulkRunTest(unittest.TestCase):
    """A bulk run on the throwaway repository and fake game of test_skytest."""

    setUp = base.SkytestTest.setUp
    tearDown = base.SkytestTest.tearDown
    spec = base.SkytestTest.spec
    HUB = base.SkytestTest.HUB

    def bulk_config(self, extra: str = "bulk_gb_per_game = 0.001\n") -> None:
        toml = self.repo / "tests" / "skytest.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace("machine_games = 16\n", "machine_games = 16\n" + extra),
                        encoding="utf-8")
        allow = self.repo / "tests" / "launch_allow.toml"
        allow.write_text(allow.read_text(encoding="utf-8") + "\n[settings.guest_vblank_rate]\nkind = \"float\"\n"
                         "min = 0\nmax = 1000\n[settings.present_interval]\nkind = \"int\"\nmin = 0\nmax = 4\n"
                         "[settings.online_server_hz]\nkind = \"int\"\nmin = 30\nmax = 60\n", encoding="utf-8")
        start_gap = self.cfg.start_gap
        self.cfg = skytest.load_config(self.repo)
        self.cfg.start_gap = start_gap

    def run_bulk(self, *names: str, **kw) -> dict:
        a = argparse.Namespace(cases=list(names), stage=None, max_games=0, bulk=True, **kw)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        text = out.getvalue()
        report = text.strip().splitlines()[-1].split("report: ", 1)[1]
        results = json.loads((Path(report).parent / "results.json").read_text(encoding="utf-8"))
        results.update(exit=code, folder=Path(report).parent, out=text)
        return results

    def test_config_keys(self) -> None:
        self.assertEqual(self.cfg.bulk_settings, skytest.BulkSettings())
        self.bulk_config("bulk_machine_games = 12\nbulk_wait_factor = 2.5\nbulk_gb_per_game = 1.0\n")
        self.assertEqual(self.cfg.bulk_settings, skytest.BulkSettings(games=12, wait_factor=2.5, gb_per_game=1.0))
        toml = self.repo / "tests" / "skytest.toml"
        good = toml.read_text(encoding="utf-8")
        for bad in ("bulk_machine_games = 0", "bulk_machine_games = 33", "bulk_wait_factor = 0.5",
                    "bulk_wait_factor = true", "bulk_gb_per_game = 0"):
            key = bad.split(" = ")[0]
            text = "\n".join(x for x in good.splitlines() if not x.startswith(key + " ")) + "\n"
            toml.write_text(text.replace("machine_games = 16\n", "machine_games = 16\n" + bad + "\n"), encoding="utf-8")
            with self.assertRaises(SystemExit, msg=bad):
                skytest.load_config(self.repo)

    def test_spec_keys(self) -> None:
        body = 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
        s = skytest.load_spec(self.spec("off", 'bulk = false\nbulk_reason = "a drawn window"\n' + body), 20)
        self.assertEqual((s.bulk, s.bulk_reason), (False, "a drawn window"))
        self.assertTrue(skytest.load_spec(self.spec("on", body), 20).bulk)
        for name, text, why in (("b1", 'bulk = false\n', "needs a `bulk_reason`"),
                                ("b2", 'bulk_reason = "x"\n', "bulk_reason without bulk = false"),
                                ("b3", 'bulk = "no"\nbulk_reason = "x"\n', "bulk is true or false")):
            with self.assertRaisesRegex(skytest.SpecError, why, msg=name):
                skytest.load_spec(self.spec(name, text + body), 20)

    def test_args_hash_is_its_own_and_the_default_s_is_unchanged(self) -> None:
        s = skytest.load_spec(self.spec("h", 'layout = "server+1"\nstage = "0"\nserver = "host"\n[peer.H]\n'
                                        'script = "quit\\n"\n'), 20)
        default = skytest.args_sha256(self.cfg, s)
        self.cfg.bulk = skytest.BulkMode()
        hidden = skytest.args_sha256(self.cfg, s)
        self.cfg.bulk = skytest.BulkMode(pacing="vblank240")
        paced = skytest.args_sha256(self.cfg, s)
        self.cfg.bulk = None
        self.assertEqual(skytest.args_sha256(self.cfg, s), default)
        self.assertEqual(len({default, hidden, paced}), 3)
        self.cfg.show_server_window = True
        self.cfg.bulk = skytest.BulkMode()
        self.assertEqual(skytest.show_server_args(self.cfg, s, "H"), skytest.BULK_HIDDEN,
                         "bulk mode never shows the server, whatever [defaults] says")

    def test_a_bulk_run_hides_every_game_scales_its_waits_and_labels_its_results(self) -> None:
        self.bulk_config()
        self.spec("plain", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 1\n[peer.H]\n'
                  'args = ["--audio_mute=true"]\nscript = """\nwaitfor title_ready 35\nwait 2\n'
                  'place 0 {figures}/Roller Brawl.dump\nfps 8\nquit\n"""\n')
        self.spec("menu", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
                  'expect = ["Online: panel drawn"]\n')
        self.spec("alone", 'layout = "solo"\nstage = "0"\nexclusive = true\n[peer.H]\nscript = "quit\\n"\n')
        r = self.run_bulk("plain", "menu", "alone")
        self.assertEqual(r["exit"], 0, r["out"])
        self.assertEqual(r["mode"], "bulk")
        self.assertEqual(r["bulk"]["games"], skytest.BULK_MACHINE_GAMES)
        self.assertEqual(r["jobs"], skytest.BULK_MACHINE_GAMES // 2, "--jobs follows the bulk budget")
        self.assertIn("run_load", r)
        by = {c["case"]: c for c in r["cases"]}
        self.assertEqual(by["plain"]["status"], "pass", by["plain"])
        self.assertEqual(by["plain"]["mode"], "bulk")
        self.assertEqual(by["menu"]["status"], skytest.NOT_IN_BULK)
        self.assertIn("panel drawn", by["menu"]["reasons"][0])
        self.assertEqual(by["alone"]["status"], skytest.NOT_IN_BULK)
        for p in by["plain"]["peers"]:
            self.assertIn("--window_hidden=true", p["command"], p["peer"])
            self.assertIn("--render_hidden_draw=false", p["command"], p["peer"])
            self.assertNotIn("--dev_show_server_window=true", p["command"])
        folder = Path(by["plain"]["folder"])
        self.assertTrue((folder / skytest.BULK_MARK).is_file())
        self.assertIn("waitfor title_ready 70", (folder / "H" / "script.txt").read_text(encoding="utf-8"))
        report = (r["folder"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("(BULK)", report)
        self.assertIn("Mode: BULK", report)
        self.assertIn("Run load (bulk):", report)
        self.assertIn("they run visible with `skytest.py test alone menu`", report)
        # The two modes' histories stay apart: a default run sees no bulk times or statuses.
        self.cfg.bulk = None
        self.assertNotIn("plain", skytest.last_run_seconds(self.cfg))
        self.assertNotIn("plain", skytest.previous_statuses(self.cfg, self.repo / "none"))
        self.cfg.bulk = skytest.BulkMode()
        self.assertIn("plain", skytest.last_run_seconds(self.cfg))
        self.assertEqual(skytest.previous_statuses(self.cfg, self.repo / "none"), {"plain": "pass"})
        self.assertEqual(skytest.build_results(self.cfg, Path(r["snapshot"]).name), {})
        # The waitfor history (`timeouts`) skips a bulk case's logs.
        self.assertEqual([n for n, _ in skytest.store_game_logs(self.cfg.store) if "plain" in n], [])

    def test_a_pacing_arm_reaches_the_command_line(self) -> None:
        self.bulk_config()
        self.spec("hosted", 'layout = "server+1"\nstage = "0"\nserver = "host"\n[peer.H]\nscript = "quit\\n"\n')
        r = self.run_bulk("hosted", bulk_pacing="hz30")
        self.assertEqual(r["bulk"]["pacing"], "hz30")
        h = next(p for p in r["cases"][0]["peers"] if p["peer"] == "H")
        self.assertIn("--present_interval=2", h["command"])
        self.assertIn("--online_server_hz=30", h["command"])

    def test_refused_combinations(self) -> None:
        self.spec("x", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        with self.assertRaisesRegex(SystemExit, "--bulk and --gate"):
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=["x"], stage=None, max_games=0, bulk=True, gate=True))
        with self.assertRaisesRegex(SystemExit, "--bulk-pacing goes with --bulk"):
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=["x"], stage=None, max_games=0, bulk_pacing="hz30"))


if __name__ == "__main__":
    unittest.main()
