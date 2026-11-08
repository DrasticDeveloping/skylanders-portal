"""Tests for skytest on a throwaway game repository with a fake game. Run:
python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import skytest  # noqa: E402
import winjob  # noqa: E402

FAKE = HERE / "fake_game.py"
_REAL_TOKEN_RESTRICTION = skytest.token_restriction


def setUpModule() -> None:
    # The runner's own token is not what these tests check (a test run from a sandbox would be refused
    # at every case); the preflight's tests set it themselves.
    skytest.token_restriction = lambda: ""
    # The load sample's CPU window (PS12) at each case's start: short here, the fake games end at once.
    skytest.CPU_SAMPLE_S = 0.1
    # The machine budget's memory check (PS15) reads the real machine's free memory: off here (another
    # program's load must not make a test a capacity result); MachineTest sets it.
    skytest.GB_PER_GAME = 0.0


def tearDownModule() -> None:
    skytest.token_restriction = _REAL_TOKEN_RESTRICTION


def toml_str(p: Path | str) -> str:
    return "'" + str(p) + "'"  # literal string: backslashes stay


class SkytestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "game"
        (self.repo / "tests" / "runtime").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        build = self.repo / "out" / "build"
        build.mkdir(parents=True)
        (build / "game.exe").write_bytes(b"MZ fake")
        (build / "runtime.dll").write_bytes(b"MZ dll")
        (self.repo / "rom").mkdir()
        (self.repo / "rom" / "default.xex").write_bytes(b"XEX2")
        (self.repo / "settings.example").write_text("# settings\n", encoding="utf-8")
        owner = root / "owner"
        (owner / "saves" / "B13EBABEBABEBABE").mkdir(parents=True)
        (owner / "saves" / "B13EBABEBABEBABE" / "SAVE.xsg").write_bytes(b"save")
        (owner / "saves" / "cache").mkdir()
        title = owner / "saves" / "B13EBABEBABEBABE" / "415608E9"
        (title / "00000001" / "SWAPFORCE_0").mkdir(parents=True)
        (title / "00000001" / "SWAPFORCE_0" / "SWAPFORCE_0.xsg").write_bytes(b"slot 0")
        (title / "Headers" / "00000001").mkdir(parents=True)
        (title / "Headers" / "00000001" / "SWAPFORCE_0.header").write_bytes(b"header")
        (owner / "figures").mkdir(parents=True)
        (owner / "figures" / "Roller Brawl.dump").write_bytes(b"\0" * 1024)
        (self.repo / "tests" / "skytest.toml").write_text(f"""
[game]
exe = "game.exe"
build = "out/build"
rom = "rom"
settings_template = "settings.example"
title_id = "415608E9"
launcher = [{toml_str(sys.executable)}, "-I", {toml_str(FAKE)}]

[fixtures]
owner_saves = {toml_str(owner / 'saves')}
owner_figures = {toml_str(owner / 'figures')}

[defaults]
args = ["--portal_mode=software", "--audio_mute=true", "--fullscreen=false"]
timeout = 20
machine_games = 16
""", encoding="utf-8")
        self.cfg = skytest.load_config(self.repo)
        self.cfg.start_gap = 0.0
        with contextlib.redirect_stdout(io.StringIO()):
            skytest.cmd_fixtures(self.cfg, argparse.Namespace(from_owner=True))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def spec(self, name: str, body: str, scenes: bool = True) -> Path:
        """A spec file; it says `scenes = "skip"` (a spec must say its scene policy) unless the body says
        one (the key or the setting) or `scenes` is False."""
        p = self.repo / "tests" / "runtime" / f"{name}.toml"
        if scenes and "scenes" not in body:
            body = 'scenes = "skip"\n' + body
        p.write_text(f'name = "{name}"\n' + body, encoding="utf-8")
        return p

    def run_cases(self, *names: str, max_games: int = 5) -> dict:
        a = argparse.Namespace(cases=list(names), stage=None, max_games=max_games)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        report = out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]
        results = json.loads((Path(report).parent / "results.json").read_text(encoding="utf-8"))
        results["exit"] = code
        results["folder"] = Path(report).parent
        return results

    HUB = '''script = """
wait 2
place 0 {figures}/Roller Brawl.dump
wait 1
fps 8
quit
"""
'''

    def test_a_layout_runs_every_peer_and_ends_them(self) -> None:
        self.spec("two", 'layout = "server+2"\nstage = "0"\nstandin_seconds = 1\n[peer.all]\n' + self.HUB +
                  'expect = ["Where: town_state10"]\n')
        r = self.run_cases("two")
        case = r["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        self.assertEqual([p["peer"] for p in case["peers"]], ["S", "H", "D"])
        self.assertEqual(case["leftover_processes"], 0)
        for p in case["peers"]:
            self.assertEqual(p["exit_code"], 0)
            self.assertEqual(p["build"], "fake123")
        h = next(p for p in case["peers"] if p["peer"] == "H")
        self.assertEqual(h["levels"], ["town_state10"])
        self.assertEqual(h["fps"], [44.7])
        self.assertEqual(h["peak_private_mb"], 3113)
        run = Path(case["folder"])
        for peer in ("S", "H", "D"):
            self.assertTrue((run / peer / "data" / "B13EBABEBABEBABE" / "SAVE.xsg").is_file())
            self.assertFalse((run / peer / "data" / "cache").exists(), "the owner's cache is not copied")
            self.assertTrue((run / peer / "game.log").is_file())
        self.assertIn("--window_hidden=true", next(p for p in case["peers"] if p["peer"] == "S")["command"])
        self.assertTrue((r["folder"] / "report.md").is_file())
        self.assertTrue((r["folder"] / "findings.md").is_file())
        self.assertEqual(r["exit"], 0)

    def test_a_measure_counts_seconds_between_two_lines(self) -> None:
        self.spec("solo", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + self.HUB +
                  '[measure.hub_load]\nfrom = "Input script: placed in slot 0"\nto = "Where: town_state"\n')
        h = self.run_cases("solo")["cases"][0]["peers"][0]
        self.assertIsNotNone(h["measures"]["hub_load"])
        self.assertGreaterEqual(h["measures"]["hub_load"], 0.0)
        self.assertLess(h["measures"]["hub_load"], 5.0)

    def test_a_hung_peer_is_ended_and_nothing_is_left(self) -> None:
        self.spec("hang", 'layout = "solo"\nstage = "0"\ntimeout = 3\n[peer.H]\nscript = "hang\\nquit\\n"\n')
        t0 = time.monotonic()
        r = self.run_cases("hang")
        case = r["cases"][0]
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(case["status"], "fail")
        self.assertTrue(case["peers"][0]["ended_by_runner"])
        self.assertEqual(case["leftover_processes"], 0)
        self.assertEqual(r["exit"], 1)

    def test_a_frozen_game_fails_even_with_its_fps_line(self) -> None:
        self.spec("frozen", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "fps0\\nquit\\n"\nexpect = ["guest FPS"]\n')
        p = self.run_cases("frozen")["cases"][0]["peers"][0]
        self.assertEqual(p["status"], "fail")
        self.assertTrue(any("no frame" in w for w in p["reasons"]), p["reasons"])

    def test_games_start_a_gap_apart(self) -> None:
        self.cfg.start_gap = 0.4
        self.spec("gap", 'layout = "server+2"\nstage = "0"\nstandin_seconds = 1\n[peer.all]\n' + self.HUB)
        peers = self.run_cases("gap")["cases"][0]["peers"]
        offsets = [p["started_after_s"] for p in peers]
        self.assertEqual(offsets, sorted(offsets))
        self.assertGreaterEqual(offsets[2] - offsets[0], 0.7)

    def test_a_crash_fails_the_case(self) -> None:
        self.spec("crash", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "crash\\nquit\\n"\n')
        p = self.run_cases("crash")["cases"][0]["peers"][0]
        self.assertEqual(p["status"], "fail")
        self.assertTrue(any("Crash: exception" in w for w in p["reasons"]), p["reasons"])

    def test_fail_fast_stops_the_run_at_a_forbidden_line(self) -> None:
        # D logs a line its spec forbids and plays on; S (a stand-in for 3000 s) and D are stopped a
        # grace after it, long before the timeout, and the case is graded as usual.
        self.cfg.fail_fast_grace = 0.5
        self.spec("ff", 'layout = "server+2"\nstage = "0"\nstandin_seconds = 3000\ntimeout = 60\n'
                  '[peer.H]\nscript = "quit\\n"\n'
                  '[peer.D]\nscript = "wait 50\\nlog boom here\\nhang\\nquit\\n"\nforbid = ["boom"]\n')
        t0 = time.monotonic()
        r = self.run_cases("ff")
        self.assertLess(time.monotonic() - t0, 30)
        case = r["cases"][0]
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertEqual(case["status"], "fail")
        self.assertRegex(case["stopped_early"], r"^stopped early: D \[.*\] Input script: boom here \(fail fast\)$")
        self.assertIn(case["stopped_early"], case["reasons"])
        self.assertEqual(case["leftover_processes"], 0)
        self.assertEqual(peers["H"]["status"], "pass")  # it quit on its own before the stop
        self.assertFalse(peers["H"]["ended_by_runner"])
        for n in ("S", "D"):
            self.assertTrue(peers[n]["ended_by_runner"])
            self.assertEqual(peers[n]["stopped_by"], case["stopped_early"])
            self.assertIn(f"ended by the runner: {case['stopped_early']}", peers[n]["reasons"])
            self.assertFalse(any("did not quit within" in w for w in peers[n]["reasons"]), peers[n]["reasons"])
        self.assertTrue(any(w.startswith("log has forbidden /boom/") for w in peers["D"]["reasons"]))
        self.assertIn("stopped early: D", (r["folder"] / "report.md").read_text(encoding="utf-8"))

    def test_fail_fast_stops_the_run_at_a_crash(self) -> None:
        # A Crash: line (always forbidden), and a game that exits with an exception code without one.
        self.cfg.fail_fast_grace = 0.5
        for name, script, hit in (("ffcrash", "crash", r"\[.*\] \[error\] .*Crash: exception C0000005"),
                                  ("ffcode", "exit -1073741819", r"exit code 0xC0000005")):
            self.spec(name, 'layout = "server+1"\nstage = "0"\nstandin_seconds = 3000\ntimeout = 60\n'
                      f'[peer.H]\nscript = "{script}\\nquit\\n"\n')
            t0 = time.monotonic()
            case = self.run_cases(name)["cases"][0]
            self.assertLess(time.monotonic() - t0, 30, name)
            self.assertRegex(case["stopped_early"], rf"^stopped early: H {hit}.* \(fail fast\)$")
            peers = {p["peer"]: p for p in case["peers"]}
            self.assertTrue(peers["S"]["ended_by_runner"], name)
            self.assertFalse(peers["H"]["ended_by_runner"], name)
            self.assertEqual(case["status"], "fail")

    def test_fail_fast_reads_a_hosted_servers_log(self) -> None:
        # S is H's child: its crash line is in H's session folder, and H and D are stopped.
        self.cfg.fail_fast_grace = 0.5
        spec = self.HOSTED.replace("place 0 {figures}/Roller Brawl.dump\nfps 8\nquit",
                                   "serverlog Crash: exception C0000005 on thread 7\nhang\nquit")
        self.spec("ffhost", "timeout = 60\n" + spec.replace("fps 8\nquit", "hang\nquit"))
        t0 = time.monotonic()
        case = self.run_cases("ffhost")["cases"][0]
        self.assertLess(time.monotonic() - t0, 30)
        self.assertRegex(case["stopped_early"], r"^stopped early: S \[.*\] Crash: exception C0000005 on thread 7")
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertTrue(peers["H"]["ended_by_runner"])
        self.assertTrue(peers["D"]["ended_by_runner"])
        self.assertEqual(case["status"], "fail")

    def test_fail_fast_off_waits_for_the_timeout_and_a_missing_expect_never_stops(self) -> None:
        self.cfg.fail_fast_grace = 0.5
        self.spec("ffoff", 'layout = "solo"\nstage = "0"\ntimeout = 3\nfail_fast = false\n'
                  '[peer.H]\nscript = "log boom\\nhang\\nquit\\n"\nforbid = ["boom"]\n')
        # Only fatal lines stop a run: an expected line that has not come yet is no reason.
        self.spec("ffexpect", 'layout = "solo"\nstage = "0"\ntimeout = 3\n'
                  '[peer.H]\nscript = "log hello\\nhang\\nquit\\n"\nexpect = ["never logged"]\n')
        t0 = time.monotonic()
        cases = {c["case"]: c for c in self.run_cases("ffoff", "ffexpect")["cases"]}
        self.assertGreaterEqual(time.monotonic() - t0, 5.5)  # both ran to their 3 s timeout
        for name in ("ffoff", "ffexpect"):
            case = cases[name]
            self.assertEqual(case["stopped_early"], "", name)
            h = case["peers"][0]
            self.assertTrue(h["ended_by_runner"], name)
            self.assertEqual(h["stopped_by"], "", name)
            self.assertIn("did not quit within 3 s; ended by the runner", h["reasons"])
        self.assertTrue(any(w.startswith("log has forbidden /boom/") for w in cases["ffoff"]["peers"][0]["reasons"]))

    def test_fail_fast_settings(self) -> None:
        self.assertEqual(self.cfg.fail_fast_grace, 10.0, "the default grace")
        on = skytest.load_spec(self.spec("on", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'), 20)
        self.assertTrue(on.fail_fast, "on by default")
        off = skytest.load_spec(self.spec("off", 'layout = "solo"\nstage = "0"\nfail_fast = false\n'
                                                 '[peer.H]\nscript = "quit\\n"\n'), 20)
        self.assertFalse(off.fail_fast)
        # A compared case's runs follow their spec.
        self.assertFalse(skytest.variant_spec(off, {"name": "off1"}).fail_fast)
        self.assertTrue(skytest.variant_spec(on, {"name": "off1"}).fail_fast)
        with self.assertRaisesRegex(skytest.SpecError, "fail_fast is true or false"):
            skytest.load_spec(self.spec("bad", 'layout = "solo"\nstage = "0"\nfail_fast = "no"\n'
                                               '[peer.H]\nscript = "quit\\n"\n'), 20)
        toml = self.repo / "tests" / "skytest.toml"
        text = toml.read_text(encoding="utf-8")
        toml.write_text(text.replace("timeout = 20\n", "timeout = 20\nfail_fast_grace_seconds = 2.5\n"), encoding="utf-8")
        self.assertEqual(skytest.load_config(self.repo).fail_fast_grace, 2.5)
        toml.write_text(text.replace("timeout = 20\n", "timeout = 20\nfail_fast_grace_seconds = -1\n"), encoding="utf-8")
        with self.assertRaises(SystemExit):
            skytest.load_config(self.repo)

    def test_a_dead_sessions_default_lines_stop_the_run(self) -> None:
        # PS5: a waitfor of a session condition that timed out, on any peer, is fatal without a forbid:
        # S (a stand-in for 3000 s) is stopped a grace after it, and the peer fails on it.
        self.cfg.fail_fast_grace = 0.5
        self.spec("dead", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 3000\ntimeout = 60\n'
                  '[peer.H]\nscript = "log waitfor online_barrier timed out after 600.0 s\\nhang\\nquit\\n"\n')
        t0 = time.monotonic()
        case = self.run_cases("dead")["cases"][0]
        self.assertLess(time.monotonic() - t0, 30)
        self.assertRegex(case["stopped_early"],
                         r"^stopped early: H .*Input script: waitfor online_barrier timed out after 600.0 s \(fail fast\)$")
        h = {p["peer"]: p for p in case["peers"]}["H"]
        self.assertTrue(any(w.startswith("log has forbidden /Input script: waitfor (online_connected|") and
                            "(a dead session's default fatal line)" in w for w in h["reasons"]), h["reasons"])
        self.assertEqual(case["status"], "fail")

    def test_the_default_fatal_lines_per_peer_and_their_opt_outs(self) -> None:
        def fatal(spec, peer: str, line: str) -> bool:
            return any(re.search(rx, line) for rx in skytest.fail_fast_patterns(spec, peer))

        base = 'layout = "server+2"\nstage = "0"\n[peer.all]\nscript = "quit\\n"\n'
        plain = skytest.load_spec(self.spec("plain", base), 20)
        lines = {
            "S": ["[t] [error] Online: the server's session did not start: bind failed",
                  "[t] [error] Online: could not connect the control pipe \\\\.\\pipe\\x (Windows error 2); the server ends"],
            "H": ["[t] [warning] Online: server: error the server did not reach its hub",
                  "[t] [warning] Online: the server ended by itself; the session is over"],
            "*": ["[t] Input script: waitfor online_connected timed out after 60.0 s",
                  "[t] Input script: waitfor menu_ready timed out after 8.0 s",
                  "[t] Input script: waitfor front_end_left timed out after 30.0 s",
                  "[t] [error] Dev: level tutorial_b: no player frame within 30 s; not requested",
                  "[t] [error] Online: x19 refused: dev_trigger_commands is off"],
        }
        for peer in ("S", "H", "D"):
            for who, ls in lines.items():
                for line in ls:
                    self.assertEqual(fatal(plain, peer, line), who in ("*", peer), (peer, line))
        # title_ready and level_ready timeouts stay out (passes recovered from them).
        for line in ("Input script: waitfor title_ready timed out after 75.0 s",
                     "Input script: waitfor level_ready timed out after 90.0 s",
                     "Input script: waitfor online_barrier met after 3.0 s"):
            self.assertFalse(fatal(plain, "H", line), line)
        # tolerate takes the lines it matches out, for the defaults only; forbid and crashes stay.
        tol = skytest.load_spec(self.spec("tol", "tolerate = ['waitfor menu_ready', 'dev_\\w+ is off']\n" + base
                                          + "forbid = ['menu_ready timed out after 9']\n"), 20)
        self.assertEqual(tol.tolerate, ["waitfor menu_ready", "dev_\\w+ is off"])
        self.assertFalse(fatal(tol, "H", "Input script: waitfor menu_ready timed out after 8.0 s"))
        self.assertFalse(fatal(tol, "D", "Online: x19 refused: dev_trigger_commands is off"))
        self.assertTrue(fatal(tol, "H", "Input script: waitfor online_barrier timed out after 3.0 s"))
        self.assertTrue(fatal(tol, "H", "Input script: waitfor menu_ready timed out after 9.0 s"))  # its forbid
        self.assertTrue(fatal(tol, "H", "[t] [error] [core] [t1] Crash: exception"))
        # The grade agrees with fail fast (the whole log searched with re.M).
        r = skytest.PeerResult(peer="H", exit_code=0)
        text = ("[t] Input script: waitfor menu_ready timed out after 8.0 s\n"
                "[t] Input script: Online: x19 refused: dev_trigger_commands is off\n")
        skytest.grade_peer(tol, r, text, skytest.parse_log(text))
        self.assertEqual(r.status, "pass", r.reasons)
        skytest.grade_peer(plain, r, text, skytest.parse_log(text))
        self.assertEqual(len([w for w in r.reasons if "default fatal line" in w]), 2, r.reasons)
        # A teardown case: the server ending by itself is not fatal there.
        down = skytest.load_spec(self.spec("session-teardown", base), 20)
        self.assertFalse(fatal(down, "H", "Online: the server ended by itself; the session is over"))
        self.assertTrue(fatal(down, "H", "Online: server: error the server did not reach its hub"))
        for bad, why in (("tolerate = 'x'\n", "tolerate is a list"), ("tolerate = ['(']\n", "tolerate: bad pattern")):
            with self.assertRaisesRegex(skytest.SpecError, why):
                skytest.load_spec(self.spec("bad", bad + base), 20)

    def test_a_case_that_is_not_ready_starts_no_game(self) -> None:
        # PS2: ready = false with a reason, or a PLACEHOLDER comment without a ready key.
        self.spec("waits", 'layout = "solo"\nstage = "0"\nready = false\nreason = "legs 11+ (X11)"\n'
                  '[peer.H]\nscript = "hang\\nquit\\n"\n')
        self.spec("marked", 'layout = "server+1"\nstage = "0"\n[peer.H]\nscript = """\nwait 1\n'
                  '# PLACEHOLDER legs 11+ (P1j case 8, X11): the winch\nhang\nquit\n"""\n',
                  scenes=False)  # its line numbers as written; it never runs
        self.spec("fine", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        t0 = time.monotonic()
        a = argparse.Namespace(cases=["waits", "marked", "fine"], stage=None, max_games=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        self.assertLess(time.monotonic() - t0, 15)  # neither hang ran
        text = out.getvalue()
        self.assertEqual(code, 1)  # not ready is not met
        self.assertIn("waits: NOT READY: legs 11+ (X11) (no game started)", text)
        self.assertIn("marked: NOT READY: PLACEHOLDER at line 7: legs 11+ (P1j case 8, X11) (no game started)", text)
        self.assertIn("2 case(s) not ready (no game started; not met): marked (PLACEHOLDER at line 7: legs 11+ (P1j"
                      " case 8, X11)); waits (legs 11+ (X11))", text)
        folder = Path(text.strip().splitlines()[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        cases = {c["case"]: c for c in results["cases"]}
        self.assertEqual([c["case"] for c in results["cases"]], ["fine", "marked", "waits"])  # the run's order
        for name in ("waits", "marked"):
            self.assertEqual(cases[name]["status"], "not ready")
            self.assertEqual(cases[name]["seconds"], 0.0)
            self.assertEqual(cases[name]["peers"], [])
            self.assertEqual(cases[name]["reasons"], [f"not ready: {cases[name]['not_ready']}"])
        self.assertEqual(cases["fine"]["status"], "pass")
        self.assertFalse(any(p.name.endswith(("-waits", "-marked")) for p in (self.cfg.store / "runs").iterdir()))
        report = (folder / "report.md").read_text(encoding="utf-8")
        self.assertIn("| waits | solo | 0 | NOT READY | 0 s | not ready: legs 11+ (X11) |", report)
        # --run-not-ready runs it anyway; ready = true overrides a PLACEHOLDER; ready = false needs a reason.
        self.spec("waits", 'layout = "solo"\nstage = "0"\nready = false\nreason = "legs 11+"\n'
                  '[peer.H]\nscript = "quit\\n"\n')
        a = argparse.Namespace(cases=["waits"], stage=None, max_games=5, run_not_ready=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(skytest.cmd_test(self.cfg, a), 0)
        self.assertNotIn("NOT READY", out.getvalue())
        over = skytest.load_spec(self.spec("over", 'layout = "solo"\nstage = "0"\nready = true\n[peer.H]\n'
                                                   'script = """\n# PLACEHOLDER x\nquit\n"""\n'), 20)
        self.assertEqual(over.not_ready, "")
        # The marker's own line, also after a blank line.
        blank = skytest.load_spec(self.spec("blank", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = """\n\n'
                                                     '# PLACEHOLDER legs\nquit\n"""\n', scenes=False), 20)
        self.assertEqual(blank.not_ready, "PLACEHOLDER at line 7: legs")
        for bad, why in (('ready = false\n', "ready = false needs a `reason`"), ('ready = "no"\n', "ready is true or false"),
                         ('ready = false\nreason = 3\n', "reason is a string")):
            with self.assertRaisesRegex(skytest.SpecError, why):
                skytest.load_spec(self.spec("bad", bad + 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'), 20)
        # PLACEHOLDERS (another word) is no marker.
        other = skytest.load_spec(self.spec("other", '# PLACEHOLDERS filled from the research\nlayout = "solo"\n'
                                                     'stage = "0"\n[peer.H]\nscript = "quit\\n"\n'), 20)
        self.assertEqual(other.not_ready, "")
        # list names it.
        (self.repo / "tests" / "runtime" / "bad.toml").unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_list(self.cfg, argparse.Namespace())
        self.assertIn("(not ready: PLACEHOLDER at line 7: legs 11+ (P1j case 8, X11))", out.getvalue())

    def test_a_not_ready_case_is_not_met_in_the_checkpoint(self) -> None:
        manifest = {"stages": ["0"], "case": [{"id": "r", "stage": "0", "kind": "required", "spec": True}]}
        r = skytest.CaseResult(case="r", layout="solo", stage="0", status="not ready", not_ready="legs 11+")
        v = skytest.checkpoint_verdict(manifest, "0", ["0"], [r])
        self.assertFalse(v["met"])
        self.assertEqual(v["open"], ["r (not ready: legs 11+)"])

    def test_the_snapshot_survives_a_runner_that_put_it_there_first(self) -> None:
        # PS11: each runner copies into its own partial folder; when another runner renamed the same
        # snapshot into place first, the rename fails and that snapshot is used.
        first = skytest.snapshot_build(self.cfg)
        import shutil
        shutil.rmtree(first)
        real_copy = skytest.shutil.copy2
        partials = []

        def copy_and_race(src, dst, *args, **kw):
            partials.append(Path(dst).parent.name)
            if not first.exists():  # the other lane finishes first, during this one's copy
                first.mkdir(parents=True)
                for f in [self.cfg.build / self.cfg.exe, *sorted(self.cfg.build.glob("*.dll"))]:
                    real_copy(f, first / f.name)
            return real_copy(src, dst, *args, **kw)

        skytest.shutil.copy2 = copy_and_race
        try:
            snap = skytest.snapshot_build(self.cfg)
        finally:
            skytest.shutil.copy2 = real_copy
        self.assertEqual(snap, first)
        self.assertTrue(all(n.startswith(f"{first.name}.partial-{os.getpid()}-") for n in partials), partials)
        self.assertEqual([p.name for p in first.parent.iterdir()], [first.name])  # no partial folder left
        # Two runners at once (threads here): both get the one snapshot, nothing fails.
        shutil.rmtree(first)
        got, errors = [], []

        def build() -> None:
            try:
                got.append(skytest.snapshot_build(self.cfg))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=build) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(got, [first] * 4)
        self.assertTrue((first / "game.exe").is_file() and (first / "runtime.dll").is_file())
        self.assertEqual([p.name for p in first.parent.iterdir()], [first.name])

    def test_results_record_what_ran_and_the_load(self) -> None:
        # PS12: per case the spec's sha256, the args hash, the fixture hash and the runner's version,
        # and the load beside it; report.md prints one load line per case.
        path = self.spec("prov", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 1\n[peer.all]\n' + self.HUB)
        r = self.run_cases("prov")
        case = r["cases"][0]
        import hashlib
        prov = case["provenance"]
        self.assertEqual(prov["spec_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(len(prov["args_sha256"]), 64)
        self.assertEqual(len(prov["fixture_sha256"]), 64)
        self.assertRegex(prov["runner"], r"^skytest [0-9a-f]{12}$")
        self.assertEqual(r["runner"], prov["runner"])
        load = case["load"]
        self.assertGreaterEqual(load["samples"], 1)
        if os.name == "nt":
            self.assertIn("games_alive_mean", load)
            self.assertEqual(load["games_alive_max"], 0)  # the fake game is python, not game.exe
            self.assertEqual(load["outside_games_max"], 0)
            self.assertGreater(load["memory_available_gb_start"], 0)
            self.assertIn("cpu_busy_percent", load)
            self.assertIsInstance(load["top_cpu"], list)
        report = (r["folder"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("Load: games alive 0.0 mean, 0 max" if os.name == "nt" else "Load: ", report)
        # The args hash follows the spec's args, not the run's own folders.
        spec = skytest.load_spec(path, 20)
        self.assertEqual(skytest.args_sha256(self.cfg, spec), prov["args_sha256"])
        spec.peers["H"].args.append("--dev_x=1")
        self.assertNotEqual(skytest.args_sha256(self.cfg, spec), prov["args_sha256"])

    def write_janitor(self, body: str) -> None:
        tools = self.repo / "tools"
        tools.mkdir(exist_ok=True)
        (tools / "test_store_janitor.py").write_text(body, encoding="utf-8")

    def test_the_store_line_comes_after_the_cases_and_never_holds_them(self) -> None:
        # PS1: the janitor runs beside the cases and its line comes after them, before the report line.
        stamp = Path(self.tmp.name) / "janitor-started.txt"
        self.write_janitor("import sys, time\n"
                           f"open({str(stamp)!r}, 'w').write(str(time.time()))\n"
                           "print('test store: 1.00 GB in 3 run(s); `just janitor --apply` would reclaim 0.00 GB')\n")
        self.spec("quick", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=["quick"], stage=None, max_games=5))
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(lines[-2], "test store: 1.00 GB in 3 run(s); `just janitor --apply` would reclaim 0.00 GB")
        # PS14: the report's path first (it is rewritten after every case), then the case.
        self.assertTrue(lines[0].startswith("report (rewritten after every case): "), lines)
        self.assertTrue(lines[1].startswith("quick (solo) ..."), lines)
        # One that takes too long is ended and named; the run does not wait for it.
        self.write_janitor("import time\ntime.sleep(60)\nprint('never')\n")
        t0 = time.monotonic()
        j = skytest.StoreJanitor(self.cfg).start()
        line = j.line(1.0)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertRegex(line, r"^test store: not measured \(the janitor's walk was still running after \d+ s; ")
        self.assertIsNotNone(j.proc.poll())

    def test_a_run_with_a_case_that_runs_alone_skips_the_janitors_walk(self) -> None:
        # The janitor never loads a measured case, and the run never waits for it at its end: a run with
        # a case that runs alone (anywhere in it) does not start it.
        stamp = Path(self.tmp.name) / "janitor-started.txt"
        self.write_janitor("import time\n"
                           f"open({str(stamp)!r}, 'w').write(str(time.time()))\n"
                           "print('test store: ok')\n")
        self.spec("quick", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        self.spec("timed", 'layout = "solo"\nstage = "0"\nexclusive = true\n[peer.H]\nscript = "wait 100\\nquit\\n"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=["quick", "timed"], stage=None, max_games=5))
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(lines[-2], "test store: not measured (a case of this run runs alone: the janitor's walk never"
                                    " runs beside a measured case; `just janitor` reports the store)")
        self.assertFalse(stamp.exists())
        # Another runner's case holds every slot: the walk is not started either.
        budget = skytest.machine_budget(self.cfg)
        lease = skytest.MachineBudget(budget.folder, budget.games, "other runner").acquire(budget.games, "timed-x")
        try:
            j = skytest.StoreJanitor(self.cfg).start(budget)
            self.assertIsNone(j.proc)
            self.assertEqual(j.line(), "test store: not measured (every machine slot is held by timed-x (other runner):"
                                       " the walk never runs beside a measured case; `just janitor` reports the store)")
        finally:
            lease.release()
        self.assertFalse(stamp.exists())
        # A walk still running when a case that runs alone starts (one the queue added) is ended.
        self.write_janitor("import time\ntime.sleep(60)\nprint('never')\n")
        j = skytest.StoreJanitor(self.cfg).start(budget)
        self.assertIsNotNone(j.proc)
        j.stop("a case that runs alone started")
        self.assertIsNotNone(j.proc.poll())
        self.assertEqual(j.line(), "test store: not measured (the janitor's walk was ended: a case that runs alone"
                                   " started; `just janitor` reports the store)")

    def test_the_janitor_runs_in_background_mode_with_its_own_command_line(self) -> None:
        # JANITOR_BOOTSTRAP: the janitor script gets its own argv and __name__, its output is the line.
        self.write_janitor("import sys\nprint('argv', sys.argv[0].endswith('test_store_janitor.py'), sys.argv[1:],"
                           " __name__)\n")
        line = skytest.StoreJanitor(self.cfg).start().line(30)
        self.assertEqual(line, f"argv True ['--repo', {str(self.cfg.repo)!r}, '--line'] __main__")

    def test_the_load_helpers(self) -> None:
        # count_games: the games machine-wide, and those that do not descend from this runner.
        me = 100
        table = [(100, 1, "python.exe"), (200, 100, "Game.exe"), (300, 200, "game.exe"),  # H and its hosted S
                 (400, 50, "game.exe"), (50, 1, "python.exe"), (500, 100, "other.exe"),
                 (600, 700, "game.exe"), (700, 600, "x.exe")]  # a reused pid making a loop
        self.assertEqual(skytest.count_games(table, "game.exe", me), (4, 2))
        self.assertEqual(skytest.load_line({}), "Load: not recorded.")
        line = skytest.load_line({"games_alive_mean": 2.5, "games_alive_max": 3, "outside_games_max": 1,
                                  "memory_available_gb_start": 20.0, "memory_available_gb_min": 18.5,
                                  "cpu_busy_percent": 23.4, "top_cpu": [{"name": "Discord.exe", "cores": 1.8}],
                                  "power_scheme": "Balanced (381b4222-f694-41f0-9685-ff5bb260df2e)"})
        self.assertEqual(line, "Load: games alive 2.5 mean, 3 max (1 max not started by this runner); memory 20.0 GB"
                               " free at the start, 18.5 GB lowest; CPU 23.4 % busy at the start, top: Discord.exe"
                               " 1.80 cores; power: Balanced (381b4222-f694-41f0-9685-ff5bb260df2e).")
        if os.name == "nt":
            self.assertGreater(len(skytest.process_table() or []), 1)
            self.assertIsNotNone(skytest.available_memory_gb())
            s = skytest.cpu_sample("game.exe", 0.2)
            self.assertIn("cpu_busy_percent", s)
            self.assertIsInstance(s["top_cpu"], list)

    def test_a_log_tail_reads_only_new_whole_lines(self) -> None:
        log = Path(self.tmp.name) / "tail.log"
        tail = skytest.LogTail(log)
        self.assertEqual(tail.new_lines(), [], "no log yet")
        log.write_bytes(b"one\r\ntwo\nthr")
        self.assertEqual(tail.new_lines(), ["one", "two"])
        self.assertEqual(tail.new_lines(), [])
        with open(log, "ab") as f:
            f.write(b"ee\nfour\n")
        self.assertEqual(tail.new_lines(), ["three", "four"])
        log.write_bytes(b"new\n")  # a shorter file: a new log in its place
        self.assertEqual(tail.new_lines(), ["new"])
        g = tail.generation
        log.unlink()  # Host empties the session folder: a log that is gone ...
        self.assertEqual(tail.new_lines(), [])
        self.assertGreater(tail.generation, g)
        log.write_bytes(b"a new server's first line\n")  # ... and the next one from its start
        self.assertEqual(tail.new_lines(), ["a new server's first line"])
        other = log.with_name("other.log")
        other.write_bytes(b"x" * 40 + b"\nanother file, longer than what was read\n")
        g = tail.generation
        os.replace(other, log)  # another file in its place, longer: told apart by its identity
        self.assertEqual(tail.new_lines(), ["x" * 40, "another file, longer than what was read"])
        self.assertGreater(tail.generation, g)
        # A fatal line in a log that is replaced before the stop is dropped; one in a kept log stays.
        watch = skytest.FailFast({"S": log}, {"S": [r"Online: could not"]})
        with open(log, "ab") as f:
            f.write(b"Online: could not open the port\n")
        self.assertEqual(watch.check(), ("S", "Online: could not open the port"))
        with open(log, "ab") as f:
            f.write(b"more\n")
        self.assertEqual(watch.check(), ("S", "Online: could not open the port"))
        log.unlink()
        self.assertIsNone(watch.check())
        log.write_bytes(b"the next session's server, clean\n")
        self.assertIsNone(watch.check())
        self.assertTrue(skytest.crash_exit_code(0xC0000005))
        self.assertTrue(skytest.crash_exit_code(-1073741819))
        self.assertTrue(skytest.crash_exit_code(0x80000003))
        for code in (None, 0, 1, 3, -1, 0xFFFFFFFF):
            self.assertFalse(skytest.crash_exit_code(code), code)

    def test_capacity_is_reported_not_skipped(self) -> None:
        self.spec("five", 'layout = "server+4"\nstage = "0"\n[peer.all]\n' + self.HUB)
        case = self.run_cases("five", max_games=2)["cases"][0]
        self.assertEqual(case["status"], "capacity")
        self.assertIn("needs 5 games", case["reasons"][0])

    def test_the_snapshot_is_reused_and_the_build_is_never_run(self) -> None:
        a = skytest.snapshot_build(self.cfg)
        b = skytest.snapshot_build(self.cfg)
        self.assertEqual(a, b)
        self.assertTrue((a / "game.exe").is_file() and (a / "runtime.dll").is_file())
        self.assertTrue(skytest.inside(a, self.cfg.store))

    def owned(self) -> list[str]:
        return [f"--{k}=x" for k in skytest.RUNNER_OWNED] + ["--portal_mode=software"]

    def test_refusals(self) -> None:
        skytest.check_args(self.owned())
        with self.assertRaises(skytest.SpecError):
            skytest.check_args(self.owned() + ["--audio_mute="])  # an empty setting
        with self.assertRaises(skytest.SpecError):
            skytest.check_args(self.owned()[1:])  # no data folder
        with self.assertRaises(skytest.SpecError):
            skytest.check_args(self.owned() + ["--user_data_root=C:/owner"])  # a second data folder
        with self.assertRaises(skytest.SpecError):
            skytest.check_args(self.owned() + ["--portal_mode=usb"])  # the real portal
        with self.assertRaises(SystemExit):
            skytest.check_output_path(self.cfg, Path(self.tmp.name) / "elsewhere")
        with self.assertRaises(SystemExit):
            skytest.check_output_path(self.cfg, self.cfg.rom / "x")

    def test_a_run_folder_taken_in_the_same_second_gets_the_next_suffix(self) -> None:
        # P1m: two runners started in the same second both picked `<stamp>-session`; the second one's
        # mkdir failed with FileExistsError. The loser of the mkdir now takes the next suffix.
        base = self.cfg.store / "runs" / "20990101-000000-session"
        real_fresh = skytest.fresh_dir
        calls = []

        def racing_fresh(path):
            calls.append(path)
            if len(calls) == 1:
                path.mkdir(parents=True)  # another runner creates it after the check
                return path
            return real_fresh(path)

        skytest.fresh_dir = racing_fresh
        try:
            got = skytest.make_run_dir(self.cfg, base)
        finally:
            skytest.fresh_dir = real_fresh
        self.assertEqual(got.name, "20990101-000000-session-2")
        self.assertTrue(got.is_dir())
        self.assertEqual(skytest.make_run_dir(self.cfg, base).name, "20990101-000000-session-3")
        with self.assertRaises(SystemExit):
            skytest.make_run_dir(self.cfg, self.cfg.rom / "runs" / "x")  # still refuses the disc

    def test_a_case_cannot_override_the_runner_settings(self) -> None:
        self.spec("override", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
                  'args = ["--portal_mode=usb"]\n')
        case = self.run_cases("override")["cases"][0]
        self.assertEqual(case["status"], "error")
        self.assertIn("portal_mode", case["reasons"][0])

    def test_repo_is_substituted_in_a_peers_args(self) -> None:
        # G3 (stage 1B): a spec names a file of the checkout under test, e.g. the route file.
        self.spec("routed", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
                  'args = ["--online_route_file={repo}/tests/routes/r.toml", "--trace_saves=true"]\n')
        case = self.run_cases("routed")["cases"][0]
        command = case["peers"][0]["command"]
        self.assertIn(f"--online_route_file={self.cfg.repo.as_posix()}/tests/routes/r.toml", command)
        self.assertIn("--trace_saves=true", command)
        self.assertFalse(any("{repo}" in a for a in command))
        self.assertEqual(skytest.peer_args(self.cfg, ["--x={repo}/a", "--y=1"]),
                         [f"--x={self.cfg.repo.as_posix()}/a", "--y=1"])
        # Only {repo} is substituted in args: anything else in braces is a spec error, not passed on.
        with self.assertRaises(skytest.SpecError):
            skytest.load_spec(self.spec("unknown", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
                                        'args = ["--online_route_file={figures}/r.toml"]\n'), 10)

    def env_run(self, name: str, **flags) -> tuple[dict, str]:
        """cmd_test on one case with extra flags: (results.json, what it printed)."""
        a = argparse.Namespace(cases=[name], stage=None, max_games=5, **flags)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        printed = out.getvalue()
        report = printed.strip().splitlines()[-1].split("report: ", 1)[1]
        results = json.loads((Path(report).parent / "results.json").read_text(encoding="utf-8"))
        results["exit"], results["folder"] = code, Path(report).parent
        return results, printed

    def test_a_restricted_token_starts_no_game_unless_allowed(self) -> None:
        self.addCleanup(setattr, skytest, "token_restriction", skytest.token_restriction)
        self.spec("solo", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        skytest.token_restriction = lambda: "a restricted token (IsTokenRestricted)"
        started = []
        real_popen = skytest.subprocess.Popen

        def recording_popen(*args, **kw):
            started.append(args)
            return real_popen(*args, **kw)

        skytest.subprocess.Popen = recording_popen
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                skytest.cmd_test(self.cfg, argparse.Namespace(cases=["solo"], stage=None, max_games=5))
        finally:
            skytest.subprocess.Popen = real_popen
        why = str(e.exception.code)
        self.assertNotIn("\n", why)
        self.assertIn("a restricted token (IsTokenRestricted)", why)
        self.assertIn("run skytest outside the sandbox", why)
        self.assertIn("--allow-restricted", why)
        self.assertIn("not comparable", why)
        self.assertEqual(started, [], "no game may start")
        runs = self.cfg.store / "runs"
        self.assertFalse(runs.is_dir() and any(runs.iterdir()), "no run folder, no snapshot copy")
        # --allow-restricted: the run goes on and says so in results.json and report.md.
        r, printed = self.env_run("solo", allow_restricted=True)
        self.assertEqual(r["cases"][0]["status"], "pass", r["cases"][0])
        self.assertIs(r["restricted_token"], True)
        self.assertEqual(r["restricted_token_cause"], "a restricted token (IsTokenRestricted)")
        self.assertIn("WARNING: running with a restricted token (IsTokenRestricted) (--allow-restricted)", printed)
        report = (r["folder"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("Token: RESTRICTED (a restricted token (IsTokenRestricted)); started with --allow-restricted",
                      report)

    def test_an_unrestricted_and_an_unreadable_token(self) -> None:
        self.addCleanup(setattr, skytest, "token_restriction", skytest.token_restriction)
        self.spec("solo", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        r, _ = self.env_run("solo")
        self.assertIs(r["restricted_token"], False)
        self.assertEqual(r["restricted_token_cause"], "")
        self.assertIn("Token: not restricted.", (r["folder"] / "report.md").read_text(encoding="utf-8"))

        def unreadable() -> str:
            raise OSError(5, "OpenProcessToken failed")

        skytest.token_restriction = unreadable
        r, printed = self.env_run("solo")
        self.assertEqual(r["cases"][0]["status"], "pass", "a check that cannot run never stops the run")
        self.assertIsNone(r["restricted_token"])
        self.assertTrue(r["restricted_token_cause"].startswith("not checked: OSError"), r["restricted_token_cause"])
        self.assertIn("skytest: could not check this runner's token (OSError", printed)
        self.assertIn("Token: not checked (OSError", (r["folder"] / "report.md").read_text(encoding="utf-8"))

    def test_the_startup_stall_per_peer(self) -> None:
        # The fake game logs the two lines through `log`; `wait 130` is 1.3 s for it.
        self.spec("stall", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 1\n[peer.H]\nscript = """\n'
                  "log Initializing shader storage for title 415608E9...\nwait 130\nlog VFS: 'game:' -> mounted\n"
                  'log SetInterruptCallback(82F58198, 40003980)\nquit\n"""\n')
        r, _ = self.env_run("stall")
        case = r["cases"][0]
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertEqual(case["status"], "pass", "a stall is reported, never graded")
        self.assertGreaterEqual(peers["H"]["startup_stall_s"], 1.2)
        self.assertLess(peers["H"]["startup_stall_s"], 5.0)
        self.assertIsNone(peers["S"]["startup_stall_s"], "the stand-in logs no shader storage")
        report = (r["folder"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("| Peer | Status | Exit | Time | Startup stall |", report)
        self.assertIn(f"| {peers['H']['startup_stall_s']:.2f} s |", report)
        self.assertRegex(report, r"\| S \| PASS \| 0 \| \d+ s \| - \|")
        self.assertIn(f"- WARNING: H: startup stall {peers['H']['startup_stall_s']:.1f} s (shader storage to the "
                      "guest's first line, over 1 s)", report)
        self.assertNotIn("WARNING: S:", report)

    @unittest.skipUnless(skytest.WINDOWS, "power throttling is Windows only")
    def test_every_started_game_is_opted_out_of_power_throttling(self) -> None:
        self.spec("two", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 1\n[peer.H]\nscript = "quit\\n"\n')
        calls = []
        self.addCleanup(setattr, skytest, "set_process_information", skytest.set_process_information)
        self.addCleanup(skytest._THROTTLING_LOGGED.clear)
        skytest.set_process_information = lambda h, c, st: calls.append((h, c, st)) or 0
        r, _ = self.env_run("two")
        self.assertEqual(len(calls), 2, "one call per started game (S and H)")
        self.assertEqual({(c, st) for _, c, st in calls}, {(4, (1, 0x5, 0))})
        self.assertTrue(all(h for h, _, _ in calls))
        self.assertNotEqual(calls[0][0], calls[1][0])
        self.assertEqual({p["power_throttling"] for p in r["cases"][0]["peers"]}, {"opted out"})
        # Windows refuses every call: printed once, the run goes on.
        skytest._THROTTLING_LOGGED.clear()
        skytest.set_process_information = lambda h, c, st: 5
        r, printed = self.env_run("two")
        self.assertEqual(r["cases"][0]["status"], "pass", r["cases"][0])
        self.assertEqual(printed.count("could not opt a game out of Windows power throttling"), 1)
        self.assertEqual({p["power_throttling"] for p in r["cases"][0]["peers"]}, {"failed: Windows error 5"})

    def test_a_game_that_cannot_join_the_job_is_ended(self) -> None:
        self.spec("nojob", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "hang\\nquit\\n"\n')
        started = []
        real_popen, real_add = skytest.subprocess.Popen, winjob.Job.add

        def recording_popen(*args, **kw):
            p = real_popen(*args, **kw)
            started.append(p)
            return p

        def failing_add(self, proc):
            raise OSError("AssignProcessToJobObject failed")

        skytest.subprocess.Popen, winjob.Job.add = recording_popen, failing_add
        try:
            case = self.run_cases("nojob")["cases"][0]
        finally:
            skytest.subprocess.Popen, winjob.Job.add = real_popen, real_add
        self.assertEqual(case["status"], "error")
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll(), "the game must not keep running outside the job")

    def test_spec_errors(self) -> None:
        bad = {
            "server-script": 'layout = "server+1"\nstage = "0"\n[peer.S]\nscript = "quit\\n"\n[peer.H]\nscript = "quit\\n"\n',
            "no-quit": 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "wait 1\\n"\n',
            "semicolon": 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "wait 1; quit\\nquit\\n"\n',
            "layout": 'layout = "server+9"\nstage = "0"\n',
            "stray-peer": 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n[peer.D]\nscript = "quit\\n"\n',
        }
        for name, body in bad.items():
            with self.subTest(name):
                with self.assertRaises(skytest.SpecError):
                    skytest.load_spec(self.spec(name, body), 10)

    def test_lint_checks_the_manifest(self) -> None:
        self.spec("ok", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        (self.repo / "tests" / "manifest.toml").write_text(
            '[[case]]\nid = "ok"\nstage = "0"\nlayout = "solo"\nkind = "required"\nspec = true\n'
            '[[case]]\nid = "later"\nstage = "1A"\nlayout = "server+1"\nkind = "accepted-failure"\n',
            encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("accepted-failure case needs a reason", out.getvalue())

    def test_lint_notes_a_tolerated_line_in_the_last_pass_and_a_case_not_ready(self) -> None:
        # PS5: a spec whose last pass logged a default fatal line it tolerates is named (a note, not a
        # problem); so is a case that is not ready (PS2).
        self.spec("tol", "tolerate = ['menu_ready']\nlayout = \"server+1\"\nstage = \"0\"\n[peer.H]\nscript = \"quit\\n\"\n")
        self.spec("later", 'layout = "solo"\nstage = "0"\nready = false\nreason = "legs 11+"\n[peer.H]\nscript = "quit\\n"\n')
        old = self.cfg.store / "runs" / "20261101-100000-tol"
        new = self.cfg.store / "runs" / "20261102-100000-tol"
        for folder, line in ((old, "Input script: waitfor menu_ready timed out after 8.0 s (old)"),
                             (new, "Input script: waitfor menu_ready timed out after 8.0 s")):
            (folder / "H").mkdir(parents=True)
            (folder / "H" / "game.log").write_text(f"[2026-11-06 10:00:00.000] [info] [core] [t1] {line}\n"
                                                   "[2026-11-06 10:00:01.000] [info] [core] [t1] Input script: waitfor"
                                                   " online_barrier met after 3.0 s\n", encoding="utf-8")
            session = folder.parent / (folder.name.replace("-tol", "-session"))
            session.mkdir()
            (session / "results.json").write_text(json.dumps({"cases": [{"case": "tol", "status": "pass",
                                                                         "folder": str(folder)}]}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("note: tol.toml: its last pass (20261102-100000-tol) logged a line it tolerates: H [2026-11-06"
                      " 10:00:00.000] [info] [core] [t1] Input script: waitfor menu_ready timed out after 8.0 s\n", text)
        self.assertNotIn("(old)", text)
        self.assertIn("note: later.toml: not ready (legs 11+)", text)
        self.assertIn("0 problem(s)", text)

    def test_lint_catches_a_stage_that_drifted(self) -> None:
        self.spec("ok", 'layout = "solo"\nstage = "1A"\n[peer.H]\nscript = "quit\\n"\n')
        (self.repo / "tests" / "manifest.toml").write_text(
            '[[case]]\nid = "ok"\nstage = "0"\nlayout = "solo"\nkind = "required"\nspec = true\n', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("stage 0 but its spec says 1A", out.getvalue())


    def test_capacity_alone_does_not_fail_the_run(self) -> None:
        self.spec("five", 'layout = "server+4"\nstage = "0"\n[peer.all]\n' + self.HUB)
        r = self.run_cases("five", max_games=2)
        self.assertEqual(r["cases"][0]["status"], "capacity")
        self.assertEqual(r["exit"], 0)

    def manifest(self, body: str) -> None:
        (self.repo / "tests" / "manifest.toml").write_text('stages = ["0", "1-pre", "1A"]\n' + body, encoding="utf-8")

    def test_stage_is_cumulative_and_unwritten_cases_are_named(self) -> None:
        self.spec("zero", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        self.spec("one", 'layout = "solo"\nstage = "1A"\n[peer.H]\nscript = "quit\\n"\n')
        self.manifest('[[case]]\nid = "zero"\nstage = "0"\nlayout = "solo"\nkind = "required"\nspec = true\n'
                      '[[case]]\nid = "one"\nstage = "1A"\nlayout = "solo"\nkind = "required"\nspec = true\n'
                      '[[case]]\nid = "later"\nstage = "1A"\nlayout = "server+1"\nkind = "required"\n')
        a = argparse.Namespace(cases=[], stage="1A", max_games=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_test(self.cfg, a)
        folder = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(c["case"] for c in results["cases"]), ["one", "zero"])
        self.assertEqual(results["required_without_spec"], ["later"])
        self.assertIn("later", (folder / "report.md").read_text(encoding="utf-8"))
        a0 = argparse.Namespace(cases=[], stage="0", max_games=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_test(self.cfg, a0)
        folder = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual([c["case"] for c in results["cases"]], ["zero"])

    def test_all_with_a_stage_still_names_unwritten_cases(self) -> None:
        self.spec("zero", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        self.manifest('[[case]]\nid = "zero"\nstage = "0"\nlayout = "solo"\nkind = "required"\nspec = true\n'
                      '[[case]]\nid = "later"\nstage = "0"\nlayout = "server+1"\nkind = "required"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=["all"], stage="0", max_games=5))
        folder = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(results["required_without_spec"], ["later"])

    def test_an_owner_folder_created_during_the_run_fails_it(self) -> None:
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        saved = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = str(home)  # the runner and the fake game both read it
        try:
            self.spec("owner", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "ownerwrite\\nquit\\n"\n')
            r = self.run_cases("owner")
        finally:
            if saved is None:
                del os.environ["USERPROFILE"]
            else:
                os.environ["USERPROFILE"] = saved
        self.assertEqual(r["exit"], 1)
        touched = [g for g in r["guarded_folders"] if not g["unchanged"]]
        self.assertEqual(len(touched), 1)
        self.assertIn(str(home), touched[0]["folder"])

    def test_the_checkpoint_line(self) -> None:
        self.spec("zero", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        self.spec("five", 'layout = "server+4"\nstage = "0"\n[peer.all]\n' + self.HUB)
        base = ('[[case]]\nid = "zero"\nstage = "0"\nlayout = "solo"\nkind = "required"\nspec = true\n'
                '[[case]]\nid = "five"\nstage = "0"\nlayout = "server+4"\nkind = "required"\nspec = true\n')

        def checkpoint(manifest_body: str) -> dict:
            self.manifest(manifest_body)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                skytest.cmd_test(self.cfg, argparse.Namespace(cases=[], stage="0", max_games=2))
            folder = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]).parent
            return json.loads((folder / "results.json").read_text(encoding="utf-8"))["checkpoint"]

        self.assertFalse(checkpoint(base)["met"])  # five only reported capacity
        self.assertTrue(checkpoint(base + 'capacity_ok = true\n')["met"])
        self.assertIn("(no spec)", " ".join(checkpoint(
            base + 'capacity_ok = true\n[[case]]\nid = "x"\nstage = "0"\nlayout = "solo"\nkind = "required"\n')["open"]))

    def test_a_touched_disc_means_the_checkpoint_is_not_met(self) -> None:
        self.spec("scribble", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "scribble\\nquit\\n"\n')
        self.manifest('[[case]]\nid = "scribble"\nstage = "0"\nlayout = "solo"\nkind = "required"\nspec = true\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, argparse.Namespace(cases=["all"], stage="0", max_games=5))
        folder = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]).parent
        cp = json.loads((folder / "results.json").read_text(encoding="utf-8"))["checkpoint"]
        self.assertEqual(code, 1)
        self.assertFalse(cp["met"])
        self.assertTrue(any(o.startswith("guard: disc") for o in cp["open"]), cp["open"])

    def test_a_crashed_peer_keeps_its_own_time(self) -> None:
        self.spec("mixed", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 3000\ntimeout = 6\n'
                  '[peer.H]\nscript = "crash\\nquit\\n"\n')
        peers = {p["peer"]: p for p in self.run_cases("mixed")["cases"][0]["peers"]}
        self.assertTrue(peers["S"]["ended_by_runner"])
        self.assertLess(peers["H"]["seconds"], peers["S"]["seconds"])
        self.assertLess(peers["H"]["seconds"], 3.0)

    def test_a_peer_that_ends_during_the_start_gaps_keeps_its_own_time(self) -> None:
        self.cfg.start_gap = 1.0
        self.spec("early", 'layout = "server+2"\nstage = "0"\nstandin_seconds = 100\n'
                  '[peer.H]\nscript = "crash\\nquit\\n"\n[peer.D]\nscript = "wait 50\\nquit\\n"\n')
        peers = {p["peer"]: p for p in self.run_cases("early")["cases"][0]["peers"]}
        self.assertLess(peers["H"]["seconds"], 0.9)  # it crashed while D was still waiting to start

    def test_the_guard_sees_a_rename(self) -> None:
        d = Path(self.tmp.name) / "guarded"
        d.mkdir()
        (d / "a.txt").write_text("1", encoding="utf-8")
        before = skytest.folder_state(d)
        (d / "a.txt").rename(d / "b.txt")
        after = skytest.folder_state(d)
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before, after)

    def test_a_game_writing_into_the_disc_fails_the_run(self) -> None:
        self.spec("scribble", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "scribble\\nquit\\n"\n')
        r = self.run_cases("scribble")
        self.assertEqual(r["exit"], 1)
        disc = [g for g in r["guarded_folders"] if g["folder"].startswith("disc")]
        self.assertEqual(len(disc), 1)
        self.assertFalse(disc[0]["unchanged"])

    def test_a_spec_error_in_a_later_peer_starts_no_game(self) -> None:
        self.spec("late", 'layout = "server+2"\nstage = "0"\nstandin_seconds = 1\n[peer.all]\n' + self.HUB +
                  '[peer.D]\nargs = ["--portal_mode=usb"]\n')
        started = []
        real_popen = skytest.subprocess.Popen

        def recording_popen(*args, **kw):
            p = real_popen(*args, **kw)
            started.append(p)
            return p

        skytest.subprocess.Popen = recording_popen
        try:
            case = self.run_cases("late")["cases"][0]
        finally:
            skytest.subprocess.Popen = real_popen
        self.assertEqual(case["status"], "error")
        self.assertEqual(started, [])

    def test_no_disc_is_refused(self) -> None:
        self.spec("solo", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        (self.repo / "rom" / "default.xex").unlink()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()):
                skytest.cmd_test(self.cfg, argparse.Namespace(cases=["solo"], stage=None, max_games=5))

    def test_lint_wants_the_file_named_like_the_case(self) -> None:
        p = self.repo / "tests" / "runtime" / "wrong-file.toml"
        p.write_text('name = "right-name"\nlayout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n',
                     encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("named wrong-file but the case right-name", out.getvalue())


    HOSTED = '''layout = "server+2"
stage = "1A"
server = "host"
[peer.H]
script = """
online_host 0
place 0 {figures}/Roller Brawl.dump
fps 8
quit
"""
[peer.D]
script = """
wait 1
online_join {join}
fps 8
quit
"""
[peer.S]
expect = ["Online: starting as the server"]
'''

    def test_a_hosted_server_is_graded_from_the_owners_session_folder(self) -> None:
        self.spec("hosted", self.HOSTED)
        r = self.run_cases("hosted")
        case = r["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertEqual(sorted(peers), ["D", "H", "S"])
        self.assertIsNone(peers["S"]["exit_code"], "the runner did not start the hosted server")
        self.assertEqual(peers["S"]["command"], [])
        self.assertEqual(peers["S"]["frame_p95_ms"], [12.0])
        self.assertEqual(peers["S"]["peak_video_mb"], 410)
        port = next(a.split("=", 1)[1] for a in peers["H"]["command"] if a.startswith("--online_port="))
        self.assertTrue(port.isdigit() and int(port) > 0)
        d_script = (Path(case["folder"]) / "D" / "script.txt").read_text(encoding="utf-8")
        self.assertIn(f"online_join 127.0.0.1:{port}", d_script)
        self.assertFalse((Path(case["folder"]) / "S").exists(), "no folder of its own for a hosted server")

    def test_the_server_frame_time_and_client_fps_are_graded(self) -> None:
        grades = ('\n[grade.server_frame_time]\nafter = "Level watch: tutorial_b: ready"\n'
                  '[grade.client_fps]\npeer = "H"\n')
        self.spec("graded", self.HOSTED + grades)
        case = self.run_cases("graded")["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertEqual(peers["S"]["graded"]["server_frame_time"]["value_ms"], 12.0)
        self.assertEqual(peers["S"]["graded"]["server_frame_time"]["windows"], 1, "the empty window does not count")
        self.assertEqual(peers["H"]["graded"]["client_fps"]["value_fps"], 44.7)
        # A slow server fails its grade; so does a client under the floor (44.7 < 0.8 x 60).
        self.spec("slow", self.HOSTED.replace("online_host 0", "serverframes 50\nonline_host 0") + grades)
        slow = self.run_cases("slow")["cases"][0]
        self.assertEqual(slow["status"], "fail")
        s = next(p for p in slow["peers"] if p["peer"] == "S")
        self.assertTrue(any("p95 50.0 ms over 33 ms" in why for why in s["reasons"]), s["reasons"])

    def test_a_hosted_server_spec_is_checked(self) -> None:
        self.spec("bad1", 'layout = "solo"\nstage = "1A"\nserver = "host"\n[peer.H]\nscript = "quit\\n"\n')
        self.spec("bad2", self.HOSTED.replace('expect = ["Online: starting as the server"]',
                                              'args = ["--x=1"]'))
        self.spec("bad3", self.HOSTED + '\n[grade.speed]\n')
        for name in ("bad1", "bad2", "bad3"):
            with self.assertRaises(skytest.SpecError, msg=name):
                skytest.load_spec(self.repo / "tests" / "runtime" / f"{name}.toml", 20)

    def test_show_server_window_settings(self) -> None:
        self.assertFalse(self.cfg.show_server_window, "missing in [defaults]: the server stays hidden")
        standin = 'layout = "server+1"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
        plain = skytest.load_spec(self.spec("plain", standin), 20)
        self.assertIsNone(plain.show_server_window, "no key: the defaults decide")
        hosted = skytest.load_spec(self.spec("hostedplain", self.HOSTED), 20)
        on = skytest.load_spec(self.spec("on", 'show_server_window = true\n' + standin), 20)
        off = skytest.load_spec(self.spec("off", 'show_server_window = false\n' + self.HOSTED), 20)
        self.assertTrue(on.show_server_window)
        self.assertFalse(off.show_server_window)
        show = "--dev_show_server_window=true"
        # Hidden by default: the standalone S as before, nothing on H.
        self.assertEqual(skytest.show_server_args(self.cfg, plain, "S"), ["--window_hidden=true"])
        self.assertEqual(skytest.show_server_args(self.cfg, hosted, "H"), [])
        # The spec's key shows it: S gets the setting instead of --window_hidden=true; a client of a
        # standalone server gets nothing.
        self.assertEqual(skytest.show_server_args(self.cfg, on, "S"), [show])
        self.assertEqual(skytest.show_server_args(self.cfg, on, "H"), [])
        # [defaults] shows it: a hosted layout's H gets the setting (Host passes it to its server),
        # D does not; the spec's false wins over the defaults.
        self.cfg.show_server_window = True
        self.assertEqual(skytest.show_server_args(self.cfg, hosted, "H"), [show])
        self.assertEqual(skytest.show_server_args(self.cfg, hosted, "D"), [])
        self.assertEqual(skytest.show_server_args(self.cfg, plain, "S"), [show])
        self.assertEqual(skytest.show_server_args(self.cfg, off, "H"), [])
        self.assertEqual(skytest.show_server_args(self.cfg, off, "S"), ["--window_hidden=true"])
        # A compared case's runs follow their spec.
        self.assertFalse(skytest.variant_spec(off, {"name": "r1"}).show_server_window)
        # Lint: the key is a bool, and the setting itself is the key's, not a spec arg's.
        self.spec("bad1", 'show_server_window = "yes"\n' + standin)
        self.spec("bad2", standin.replace('script = "quit\\n"', 'script = "quit\\n"\nargs = ["' + show + '"]'))
        for name, why in (("bad1", "show_server_window is true or false"),
                          ("bad2", "sets --dev_show_server_window in its args")):
            with self.assertRaisesRegex(skytest.SpecError, why, msg=name):
                skytest.load_spec(self.repo / "tests" / "runtime" / f"{name}.toml", 20)
        toml = self.repo / "tests" / "skytest.toml"
        text = toml.read_text(encoding="utf-8")
        toml.write_text(text.replace("timeout = 20\n", "timeout = 20\nshow_server_window = true\n"), encoding="utf-8")
        self.assertTrue(skytest.load_config(self.repo).show_server_window)
        toml.write_text(text.replace("timeout = 20\n", "timeout = 20\nshow_server_window = 1\n"), encoding="utf-8")
        with self.assertRaises(SystemExit):
            skytest.load_config(self.repo)

    def test_a_shown_server_runs_with_the_setting_on_its_command_line(self) -> None:
        self.cfg.show_server_window = True
        self.spec("shown", self.HOSTED)
        self.spec("standin", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 1\n[peer.H]\n' + self.HUB)
        r = self.run_cases("shown", "standin")
        cases = {c["case"]: c for c in r["cases"]}
        for name in ("shown", "standin"):
            self.assertEqual(cases[name]["status"], "pass", cases[name])
        hosted = {p["peer"]: p["command"] for p in cases["shown"]["peers"]}
        self.assertIn("--dev_show_server_window=true", hosted["H"])
        self.assertNotIn("--dev_show_server_window=true", hosted["D"])
        standin = {p["peer"]: p["command"] for p in cases["standin"]["peers"]}
        self.assertIn("--dev_show_server_window=true", standin["S"])
        self.assertNotIn("--window_hidden=true", standin["S"])
        self.assertNotIn("--dev_show_server_window=true", standin["H"])

    def test_a_level_load_is_graded_against_the_baseline(self) -> None:
        (self.repo / "tests" / "baseline.toml").write_text("[solo.hub]\nhub_load_seconds = 0.001\n", encoding="utf-8")
        self.cfg = skytest.load_config(self.repo)
        self.cfg.start_gap = 0.0
        self.spec("load", self.HOSTED.replace("online_host 0", "online_host 0\nwait 20") +
                  '\n[measure.hub_load]\nfrom = "Online: hosting"\nto = "Input script: placed in slot 0"\n'
                  '[grade.level_load]\npeer = "H"\nmeasure = "hub_load"\nbaseline = "solo.hub.hub_load_seconds"\n')
        case = self.run_cases("load")["cases"][0]
        h = next(p for p in case["peers"] if p["peer"] == "H")
        self.assertIsNotNone(h["graded"]["level_load"]["ratio"])
        self.assertGreater(h["graded"]["level_load"]["ratio"], 1.5)
        self.assertTrue(any("grade level_load" in why for why in h["reasons"]), h["reasons"])
        self.spec("load2", self.HOSTED + '\n[grade.level_load]\npeer = "H"\nmeasure = "nope"\nbaseline = "x"\n')
        with self.assertRaises(skytest.SpecError):
            skytest.load_spec(self.repo / "tests" / "runtime" / "load2.toml", 20)

    def test_the_fps_floor_follows_the_peers_frame_limit(self) -> None:
        settings = self.repo / "s.toml"
        settings.write_text('["Graphics"]\nframe_rate_limit = 30\n', encoding="utf-8")
        self.assertEqual(skytest.frame_rate_limit([], settings), 30.0)
        self.assertEqual(skytest.frame_rate_limit(["--frame_rate_limit=120"], settings), 120.0)
        self.assertEqual(skytest.frame_rate_limit([], self.repo / "missing.toml"), 60.0)
        settings.write_text('["Graphics"]\nframe_rate_limit = 0\n', encoding="utf-8")
        self.assertEqual(skytest.frame_rate_limit([], settings), 60.0)
        # A 30 FPS peer at 25 FPS is over its floor of 21; at 60 it would be under 42.
        (self.repo / "settings.example").write_text('["Graphics"]\nframe_rate_limit = 30\n', encoding="utf-8")
        self.cfg = skytest.load_config(self.repo)
        self.cfg.start_gap = 0.0
        self.spec("slow30", self.HOSTED + '\n[grade.client_fps]\npeer = "H"\n')
        h = next(p for p in self.run_cases("slow30")["cases"][0]["peers"] if p["peer"] == "H")
        self.assertEqual(h["graded"]["client_fps"]["floor_fps"], 21.0)
        self.assertNotIn("client_fps", " ".join(h["reasons"]))

    def test_an_accepted_grade_is_named_and_a_regression_is_open(self) -> None:
        graded = self.HOSTED.replace('stage = "1A"', 'stage = "0"').replace(
            "online_host 0", "serverframes 50\nonline_host 0") + '\n[grade.server_frame_time]\n'
        self.spec("slowsrv", graded)
        entry = ('[[case]]\nid = "slowsrv"\nstage = "0"\nlayout = "server+2"\nkind = "required"\nspec = true\n')

        def checkpoint(manifest_body: str) -> dict:
            self.manifest(manifest_body)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                skytest.cmd_test(self.cfg, argparse.Namespace(cases=[], stage="0", max_games=3))
            folder = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]).parent
            return json.loads((folder / "results.json").read_text(encoding="utf-8"))["checkpoint"]

        self.assertFalse(checkpoint(entry)["met"])  # the server's p95 of 50 ms fails
        cp = checkpoint(entry + 'accepted_grades = ["server_frame_time"]\nreason = "one PC"\n')
        self.assertTrue(cp["met"], cp)
        self.assertIn("accepted: slowsrv: grade server_frame_time: p95 50.0 ms over 33 ms", cp["line"])
        # It passed in the run before; now its flow breaks (the owner never hosts): open as a regression.
        self.spec("slowsrv", graded.replace("online_host 0", "crash"))
        cp = checkpoint(entry + 'accepted_grades = ["server_frame_time"]\nreason = "one PC"\n')
        self.assertFalse(cp["met"])
        self.assertIn("slowsrv (passed before, fails now)", cp["open"])

    def test_an_accepted_grade_still_needs_its_measurement(self) -> None:
        # client_fps accepted, but H measures no fps window: that is missing evidence, not accepted.
        no_fps = self.HOSTED.replace('stage = "1A"', 'stage = "0"').replace("fps 8\nquit", "quit", 1) + \
            '\n[grade.client_fps]\npeer = "H"\n'
        self.spec("nofps", no_fps)
        self.manifest('[[case]]\nid = "nofps"\nstage = "0"\nlayout = "server+2"\nkind = "required"\nspec = true\n'
                      'accepted_grades = ["client_fps"]\nreason = "one PC"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=[], stage="0", max_games=3))
        folder = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertFalse(results["checkpoint"]["met"])
        h = next(p for p in results["cases"][0]["peers"] if p["peer"] == "H")
        self.assertEqual(h["accepted"], [])
        self.assertTrue(any("no fps window" in why for why in h["reasons"]), h["reasons"])

    def test_a_case_that_errors_after_passing_is_a_regression(self) -> None:
        r = skytest.CaseResult(case="x", layout="solo", stage="0", status="error")
        cp = skytest.checkpoint_verdict({"case": []}, "0", ["0"], [r], [], {"x": "pass"})
        self.assertIn("x (passed before, fails now)", cp["open"])

    def test_save_copy_passes_when_no_save_is_written(self) -> None:
        # H hosts (its session folder goes into its data folder, outside the save folder) and D writes
        # only into the SDK's save-backup: neither changes a save.
        self.spec("nosave", self.HOSTED.replace("online_join {join}", "online_join {join}\nsavewrite backup") +
                  '\n[grade.save_copy]\n')
        case = self.run_cases("nosave")["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        peers = {p["peer"]: p for p in case["peers"]}
        for n in ("H", "D"):
            self.assertEqual(peers[n]["graded"]["save_copy"]["files"], 2, peers[n]["graded"])
            self.assertEqual(peers[n]["graded"]["save_copy"]["changed"], [])
        self.assertNotIn("save_copy", peers["S"]["graded"], "a hosted server has no save copy of its own")
        self.assertTrue((Path(case["folder"]) / "H" / skytest.HOSTED_SERVER_LOG).is_file())

    def test_save_copy_fails_when_a_save_is_written(self) -> None:
        self.spec("save", self.HOSTED.replace("online_join {join}", "online_join {join}\nsavewrite") +
                  '\n[grade.save_copy]\n')
        case = self.run_cases("save")["cases"][0]
        self.assertEqual(case["status"], "fail")
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertEqual(peers["H"]["status"], "pass", peers["H"]["reasons"])
        d = peers["D"]
        self.assertTrue(d["graded"]["save_copy"]["violation"])
        self.assertEqual(d["graded"]["save_copy"]["changed"],
                         ["B13EBABEBABEBABE/415608E9/00000001/SWAPFORCE_0/SWAPFORCE_0.xsg"])
        self.assertTrue(any(why.startswith("grade save_copy: the save copy changed: changed B13EBABEBABEBABE/")
                            for why in d["reasons"]), d["reasons"])
        # Only the named peers are graded.
        self.spec("saveh", self.HOSTED.replace("online_join {join}", "online_join {join}\nsavewrite") +
                  '\n[grade.save_copy]\npeers = ["H"]\n')
        case = self.run_cases("saveh")["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        self.assertNotIn("save_copy", next(p for p in case["peers"] if p["peer"] == "D")["graded"])

    def test_save_copy_needs_a_save_to_compare(self) -> None:
        self.spec("nosaves", self.HOSTED + '\n[grade.save_copy]\npeers = ["H"]\ntitle = "DEADBEEF"\n')
        case = self.run_cases("nosaves")["cases"][0]
        h = next(p for p in case["peers"] if p["peer"] == "H")
        self.assertEqual(h["status"], "fail")
        self.assertIn("grade save_copy: no save in <data>/<profile>/DEADBEEF before the run (nothing to compare)",
                      h["reasons"])
        self.assertNotIn("violation", h["graded"]["save_copy"], "missing evidence is never an accepted failure")

    def test_a_save_copy_spec_is_checked(self) -> None:
        bad = {"s": 'peers = ["S"]', "x": 'peers = ["X"]', "none": "peers = []", "title": 'title = "save"'}
        for name, body in bad.items():
            self.spec(f"bad-{name}", self.HOSTED + f"\n[grade.save_copy]\n{body}\n")
            with self.assertRaises(skytest.SpecError, msg=name):
                skytest.load_spec(self.repo / "tests" / "runtime" / f"bad-{name}.toml", 20)
        # A server the runner starts itself (stand-in) has its own data folder and may be named.
        self.spec("standin", 'layout = "server+1"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
                  '[grade.save_copy]\npeers = ["S", "H"]\n')
        spec = skytest.load_spec(self.repo / "tests" / "runtime" / "standin.toml", 20)
        self.assertEqual(skytest.save_copy_peers(spec.grades["save_copy"], list(spec.peers)), ["S", "H"])


    COMPARED = '''layout = "server+1"
stage = "1B"
server = "host"
[peer.H]
script = """
online_host 0
serverlog Online: the server is in its hub (town_state10)
serverlog Online: level 2 tutorial_b: open
serverlog Online: level 2: the server has it up
serverlog Online: barrier 2 released at tick 1234 (1 client)
serverlog Trace: message CTickMessage to Hero#5 from - (#1)
serverlog GPU: draws 812 memexport 0 copies 121 swaps 60 zpd 0 viz 0 readbacks 0 skipped 0/0 in 10.0 s
serverlog Online: End session: closing (1 client to drain)
log the session ended
quit
"""
[compare]
noise = ["off1", "off2"]
test = ["on"]
window_end = "Online: End session: closing"
load_floor_s = 1.0
reached = { S = ['Online: barrier 2 released at tick \\d+ \\(1 client\\)'], H = ["the session ended"] }
[[compare.run]]
name = "off1"
[compare.run.peer.S]
forbid = ["GPU: draw skip on"]
[[compare.run]]
name = "off2"
[compare.run.peer.S]
forbid = ["GPU: draw skip on"]
[[compare.run]]
name = "on"
[compare.run.peer.H]
args = ["--render_hidden_draw=false"]
[compare.run.peer.S]
expect = ["GPU: draw skip on"]
'''

    def test_a_compared_case_runs_its_workload_per_run_and_compares(self) -> None:
        self.spec("ab", self.COMPARED)
        r = self.run_cases("ab")
        case = r["cases"][0]
        self.assertEqual(case["status"], "pass", case["reasons"])
        self.assertEqual([p["peer"] for p in case["peers"]], ["off1:S", "off1:H", "off2:S", "off2:H", "on:S", "on:H"])
        folder = Path(case["folder"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["compare.json", "compare.md", "off1", "off2", "on", "result.json"])
        on_h = next(p for p in case["peers"] if p["peer"] == "on:H")
        self.assertIn("--render_hidden_draw=false", on_h["command"])
        self.assertEqual(case["compare"]["status"], "pass")
        self.assertEqual(case["compare"]["messages"]["mode"], "sampled")
        report = (r["folder"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("### Comparison of S's logs: PASS", report)
        # The same runs through `compare`: the first two are the A/A.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_compare(self.cfg, argparse.Namespace(
                runs=[str(folder / n) for n in ("off1", "off2", "on")], spec="ab", noise=0))
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("compare ab: PASS", out.getvalue())
        report = Path(out.getvalue().strip().splitlines()[-1].split("report: ", 1)[1])
        self.assertTrue(report.is_file() and skytest.inside(report, self.cfg.store))
        cmp = json.loads((report.parent / "compare.json").read_text(encoding="utf-8"))
        self.assertEqual((cmp["noise"], cmp["test"]), (["off1", "off2"], ["on"]))

    def test_a_compared_case_without_its_evidence_is_not_reached(self) -> None:
        # The workload stops before tutorial_b's barrier: the first run is not reached, and the case
        # stops there (no second or third run) and fails; the comparison is never a pass.
        self.spec("stopped", self.COMPARED.replace("serverlog Online: barrier 2 released at tick 1234 (1 client)\n", ""))
        case = self.run_cases("stopped")["cases"][0]
        self.assertEqual(case["status"], "fail")
        self.assertEqual(case["compare"]["status"], "not reached")
        self.assertEqual([p["peer"] for p in case["peers"]], ["off1:S", "off1:H"])
        self.assertIn("compare: not reached: run off1: S lacks /Online: barrier 2 released at tick \\d+ \\(1 client\\)/",
                      case["reasons"])
        self.assertIn("compare: not reached: run(s) off2, on not run", case["reasons"])

    def test_a_run_of_a_compared_case_keeps_its_own_expectations(self) -> None:
        # The skip-on run's server must say the skip is on: without the setting it fails its run.
        self.spec("noskip", self.COMPARED.replace('args = ["--render_hidden_draw=false"]', 'args = ["--x=1"]'))
        case = self.run_cases("noskip")["cases"][0]
        self.assertEqual(case["status"], "fail")
        self.assertEqual(case["compare"]["status"], "pass", "the logs agree; the run's own expect fails it")
        self.assertIn("run on: peers failed: S", case["reasons"])

    def test_a_compare_spec_is_checked(self) -> None:
        one_run = self.COMPARED.split("[[compare.run]]")[0] + '[[compare.run]]\nname = "off1"\n'
        self.spec("bad-one-run", one_run)
        with self.assertRaisesRegex(skytest.SpecError, "two or more"):
            skytest.load_spec(self.repo / "tests" / "runtime" / "bad-one-run.toml", 20)
        bad = {
            "noise1": ('noise = ["off1", "off2"]', 'noise = ["off1"]', 1),
            "overlap": ('test = ["on"]', 'test = ["on", "off1"]', 1),
            "orphan": ('test = ["on"]', "test = []", 1),
            "noreach": ("reached = {", "x = 1\nreachedx = {", 1),
            "srvargs": ('[compare.run.peer.S]\nexpect = ["GPU: draw skip on"]',
                        '[compare.run.peer.S]\nargs = ["--y=1"]', 1),
            "badname": ('name = "on"', 'name = "o n"', 1),
            "floor": ("load_floor_s = 1.0", "load_floor_s = -1", 1),
            "ratefloor": ("load_floor_s = 1.0", "load_floor_s = 1.0\nrate_floor = -0.01", 1),
            "pattern": ('window_end = "Online: End session: closing"', 'window_end = "("', 1),
        }
        for name, (old, new, count) in bad.items():
            body = self.COMPARED.replace(old, new, count)
            self.assertNotEqual(body, self.COMPARED, name)
            self.spec(f"bad-{name}", body)
            with self.assertRaises(skytest.SpecError, msg=name):
                skytest.load_spec(self.repo / "tests" / "runtime" / f"bad-{name}.toml", 20)


def stamped(*lines: tuple[float, str]) -> str:
    """A recorded-style log written by hand: (seconds after 10:00:00, text) per line."""
    base = time.mktime(time.strptime("2026-11-06 10:00:00", "%Y-%m-%d %H:%M:%S"))
    out = []
    for sec, text in lines:
        whole = int(sec)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + whole))
        out.append(f"[{stamp}.{int(round((sec - whole) * 1000)):03d}] [info] [core] [t2] {text}")
    return "\n".join(out) + "\n"


def sampled(at: float, key_line: str, total: int) -> list[tuple[float, str]]:
    """The trace lines of one key that came `total` times: the first 20 and every 500th (traces.cpp)."""
    shown = [n for n in range(1, total + 1) if n <= 20 or n % 500 == 0]
    return [(at + i * 0.01, key_line.format(n=n)) for i, n in enumerate(shown)]


def server_log(skip: bool = False, level_load: float = 8.0, ticks: int = 7000, barriers: tuple[int, ...] = (2,),
               stop_before_level: bool = False, readbacks: int = 0, memexport: int = 0, swap: bool = False,
               interleave: bool = False, dumps: tuple[dict, dict] | None = None, spawns: int = 3, fps: int = 60,
               acts: int = 0, gpu_lines: bool = True) -> str:
    """S's log of the item-3 workload as the server writes it (the lines' forms from session_host.cpp,
    level_watch.cpp, traces.cpp and the SDK's draw_skip.h): the hub, tutorial_b through barrier 2,
    two minutes at the spawn, End session. `swap`: the barrier released before the server has the
    level up (the session's own order changed); `interleave`: the level watch's load line after the
    session's (only the two threads' interleaving changed). `fps`: the server's frames per second (the
    GPU lines' swaps per 10 s window); `acts`: a per-frame trigger key's sampled count in the window;
    `gpu_lines`: False leaves the GPU lines out."""
    skipped = "7900/1200" if skip else "0/0"
    gpu = (f"GPU: draws 8123 memexport {memexport} copies 1210 swaps {fps * 10} zpd 0 viz 0 readbacks {readbacks} "
           f"skipped {skipped} in 10.0 s")
    lines: list[tuple[float, str]] = [
        (0.0, "Skylanders: Swap Force test (build abc1234); Windows"),
        (0.5, "Online: starting as the server"),
        (18.0, "Online: peer 1 joined (Owner, owner) from 127.0.0.1:50123"),
        (24.0, "Online: the server is in its hub (town_state10)"),
    ] + ([(30.0, gpu)] if gpu_lines else [])
    if skip:
        lines.insert(2, (1.0, "GPU: draw skip on (render_hidden_draw off, window hidden): draws without memexport, "
                              "resolves and presents are skipped"))
    if stop_before_level:
        return stamped(*lines)
    t = 40.0
    level = [
        (t, "Online: the owner asked for tutorial_b"),
        (t, "Online: level 2 tutorial_b: open"),
        (t + 0.2, "Level watch: tutorial_b: requested by the game (request 1, zone 0x8312ABCD)"),
        (t + 0.4, "Level watch: tutorial_b: loading screen up (request 1)"),
        (t + level_load, f"Level watch: tutorial_b: the game's load call returned after {int(level_load * 1000) - 300} "
                         "ms (request 1)"),
        (t + level_load + 0.1, "Online: level 2: the server has it up"),
    ]
    if interleave:
        level[4], level[5] = (level[4][0], level[5][1]), (level[5][0], level[4][1])
    lines += level
    t += level_load + (0.05 if swap else 2.0)
    for g in barriers:
        lines.append((t, f"Online: barrier {g} released at tick {1234 + int(level_load * 60)} (1 client)"))
    lines.append((t + 1.0, "Level watch: tutorial_b: ready (request 1, a player is in it)"))
    if dumps:
        lines.append((t + 1.1, f"Trace counts: {len(dumps[0])} keys"))
        lines += [(t + 1.1, f"Trace count: {k} {n}") for k, n in dumps[0].items()]
    lines += sampled(t + 2.0, "Trace: trigger SpawnVolume#12 enter by Hero#5 (bits 0x21, #{n})", 1)
    lines += sampled(t + 3.0, "Trace: message CTickMessage to Hero#5 from - (#{n})", ticks)
    lines += sampled(t + 4.0, "Trace: telegram CSpawnMessage sender Spawner#9 activator Hero#5 (#{n})", spawns)
    lines += sampled(t + 5.0, "Trace: trigger Gate#7 act by Hero#5 (bits 0x3C, #{n})", acts)
    lines += [(t + 10.0 * k, gpu) for k in range(1, 13)] if gpu_lines else []
    if dumps:
        lines.append((t + 120.0, f"Trace counts: {len(dumps[1])} keys"))
        lines += [(t + 120.0, f"Trace count: {k} {n}") for k, n in dumps[1].items()]
    lines.append((t + 120.5, "Online: End session: closing (1 client to drain)"))
    lines += sampled(t + 121.0, "Trace: message CShutdownMessage to Hero#5 from - (#{n})", 4)
    return stamped(*sorted(lines, key=lambda x: x[0]))


COMPARE = {"peer": "S", "noise": ["off1", "off2"], "test": ["on"], "window_end": "Online: End session: closing",
           "load_floor_s": 0.5, "reached": {"S": [r"Online: barrier 2 released at tick \d+ \(1 client\)"],
                                            "H": ["Online: the session ended"]}}


class GateTest(unittest.TestCase):
    """`--gate` (stage 1B item 15): the manifest's gate cases of the stages up to S and their expected
    minutes, from each case's newest pass, else its spec's timeout, else `no spec`. The throwaway
    repository of SkytestTest, without its tests."""

    tearDown = SkytestTest.tearDown
    spec = SkytestTest.spec

    QUIT = 'layout = "solo"\nstage = "{stage}"\ntimeout = {timeout}\n[peer.H]\nscript = "quit\\n"\n'

    def setUp(self) -> None:
        SkytestTest.setUp(self)
        self.spec("a", self.QUIT.format(stage="0", timeout=600))
        self.spec("b", self.QUIT.format(stage="1A", timeout=1800))
        self.spec("plain", self.QUIT.format(stage="0", timeout=60))
        self.spec("later", self.QUIT.format(stage="1B", timeout=60))
        case = 'id = "{id}"\nstage = "{stage}"\nlayout = "{layout}"\nkind = "required"\n'
        (self.repo / "tests" / "manifest.toml").write_text(
            'stages = ["0", "1A", "1B"]\n'
            + "[[case]]\n" + case.format(id="a", stage="0", layout="solo") + "gate = true\nspec = true\n"
            + "[[case]]\n" + case.format(id="b", stage="1A", layout="solo") + "gate = true\nspec = true\n"
            + "[[case]]\n" + case.format(id="nospec", stage="1A", layout="server+3") + "gate = true\n"
            + "[[case]]\n" + case.format(id="plain", stage="0", layout="solo") + "spec = true\n"
            + "[[case]]\n" + case.format(id="later", stage="1B", layout="solo") + "gate = true\nspec = true\n",
            encoding="utf-8")
        # Recorded runs: a's newest pass wins over its newer fail; an error, a capacity result and a run
        # with no time say nothing about its length; b never passed: its short fail (an early crash)
        # never stands for its length.
        self.results("20261101-100000-session", [("a", "pass", 300.0), ("b", "error", 2.0)])
        self.results("20261102-100000-session", [("a", "fail", 420.0), ("plain", "pass", 30.0)])
        self.results("20261103-100000-session", [("a", "error", 1.0), ("b", "capacity", 0.0)])
        self.results("20261104-100000-session", [("a", "pass", 0.0), ("b", "fail", 42.0)])
        (self.cfg.store / "runs" / "20261105-100000-session").mkdir(parents=True)
        (self.cfg.store / "runs" / "20261105-100000-session" / "results.json").write_text("{not json", encoding="utf-8")

    def results(self, folder: str, cases: list[tuple[str, str, float]], jobs: int | None = None,
                **extra) -> None:
        d = self.cfg.store / "runs" / folder
        d.mkdir(parents=True)
        payload = {"cases": [{"case": c, "status": s, "seconds": t, **extra} for c, s, t in cases]}
        if jobs is not None:
            payload["jobs"] = jobs
        (d / "results.json").write_text(json.dumps(payload), encoding="utf-8")

    def estimate(self, stage: str | None) -> dict:
        manifest = skytest.load_manifest(self.cfg)
        stages = skytest.stages_up_to(manifest, stage) if stage else None
        return skytest.gate_estimate(self.cfg, manifest, stages, skytest.all_specs(self.cfg))

    def test_the_median_of_passes_alone_and_a_timeout_apart(self) -> None:
        # PS13: a's passes known to have run alone (300 s, and 340 s below: load sampled, no game of
        # another runner, no case beside it, --jobs 1) make its median; the passes beside another case
        # (ran_with), in a --jobs 2 run, with games of another runner alive (PS12's load), or with no
        # load recorded (the 20261101 pass and the one without samples: unknown, not alone) do not.
        self.results("20261101-110000-session", [("a", "pass", 300.0)], jobs=1, ran_with=[], load={"samples": 3, "outside_games_max": 0})
        self.results("20261106-100000-session", [("a", "pass", 900.0)], ran_with=["x"])
        self.results("20261106-110000-session", [("a", "pass", 800.0)], jobs=2, ran_with=[],
                     load={"samples": 3, "outside_games_max": 0})
        self.results("20261106-120000-session", [("a", "pass", 700.0)], load={"outside_games_max": 2})
        self.results("20261106-125000-session", [("a", "pass", 600.0)], jobs=1, ran_with=[],
                     load={"samples": 0, "outside_games_max": 0})
        self.results("20261106-130000-session", [("a", "pass", 340.0)], jobs=1, ran_with=[], load={"samples": 3, "outside_games_max": 0})
        g = self.estimate("1A")
        rows = {r["case"]: r for r in g["cases"]}
        self.assertEqual(sorted(rows), ["a", "b", "nospec"])  # gate cases of 0 and 1A only
        self.assertEqual(rows["a"]["seconds"], 320.0)
        self.assertEqual(rows["a"]["basis"], "alone")
        self.assertEqual(rows["a"]["source"], "median of 2 pass(es) alone, newest 20261106-130000-session")
        self.assertEqual(rows["b"]["seconds"], 1800.0)
        self.assertEqual(rows["b"]["basis"], "timeout")
        self.assertEqual(rows["b"]["source"],
                         "spec timeout (never passed; longest fail 0.7 min, 20261104-100000-session)")
        self.assertIsNone(rows["nospec"]["seconds"])
        self.assertEqual(rows["nospec"]["source"], "no spec")
        # The measured minutes apart from the timeouts (an upper bound for the case that never passed).
        self.assertAlmostEqual(g["minutes"], 5.3)
        self.assertEqual(g["timeout_minutes"], 30.0)
        self.assertFalse(g["over"])
        self.assertEqual(g["from_runs"], ["a"])
        self.assertEqual(g["from_timeouts"], ["b"])
        self.assertEqual(g["no_spec"], ["nospec"])
        line = skytest.gate_line(g)
        self.assertIn("gate: 3 cases, expected 5 min measured (1 from the median of their passes: 1 run alone);"
                      " + up to 30 min for 1 never passed (spec timeouts: b)", line)
        self.assertIn("no spec, not counted: nospec", line)
        self.assertIn("the plan's subset is 30 min (up to 35 min with the timeouts)", line)
        self.assertIn("<= 30 min", "\n".join(skytest.gate_table(g)))

    def test_without_a_pass_alone_the_median_of_all_passes_is_named(self) -> None:
        (self.cfg.store / "runs" / "20261101-100000-session" / "results.json").unlink()
        self.results("20261106-100000-session", [("a", "pass", 600.0)], ran_with=["x"])
        self.results("20261106-110000-session", [("a", "pass", 1200.0)], jobs=3, ran_with=["z"])
        self.results("20261106-120000-session", [("a", "pass", 2400.0)], ran_with=["y"])
        rows = {r["case"]: r for r in self.estimate("1A")["cases"]}
        self.assertEqual(rows["a"]["seconds"], 1200.0)
        self.assertEqual(rows["a"]["basis"], "beside")
        self.assertIn("beside other games, none alone", rows["a"]["source"])
        line = skytest.gate_line(self.estimate("1A"))
        self.assertIn("(1 from the median of their passes: 0 run alone, 1 beside other games: a)", line)

    def test_a_pass_whose_load_was_not_recorded_is_unknown_not_alone(self) -> None:
        # PS12 round 2 (S2): every pass from before the load was recorded (the setUp's 300 s for a) and
        # a pass with a load sampled only by tasklist (no outside count) say nothing about other games.
        self.results("20261106-100000-session", [("a", "pass", 500.0)], jobs=1, ran_with=[], load={"samples": 4})
        self.results("20261106-110000-session", [("a", "pass", 900.0)], ran_with=["x"])
        g = self.estimate("1A")
        rows = {r["case"]: r for r in g["cases"]}
        self.assertEqual(rows["a"]["seconds"], 500.0)
        self.assertEqual(rows["a"]["basis"], "unknown")
        self.assertEqual(rows["a"]["source"], "median of 3 pass(es), none known alone (2 with no recorded load,"
                                              " 1 beside other games), newest 20261106-110000-session")
        self.assertEqual(g["from_unknown"], ["a"])
        self.assertEqual(g["from_runs"], ["a"])
        self.assertAlmostEqual(g["minutes"], 8.3)
        self.assertIn("(1 from the median of their passes: 0 run alone, 1 of unknown load (no pass known"
                      " alone): a)", skytest.gate_line(g))
        self.assertIn("~ 8.3 min", "\n".join(skytest.gate_table(g)))
        self.assertEqual(skytest.pass_basis({}, {"case": "a"}), "unknown")
        self.assertEqual(skytest.pass_basis({"jobs": 1}, {"ran_with": [], "load": {"samples": 2,
                                                                                    "outside_games_max": 0}}), "alone")
        self.assertEqual(skytest.pass_basis({"jobs": 1}, {"ran_with": [], "load": {"samples": 2,
                                                                                    "outside_games_max": 1}}), "beside")

    def test_passes_of_another_spec_text_or_other_arguments_are_left_out(self) -> None:
        # PS12's provenance: a pass recorded with another spec sha256 or args hash than the spec's now
        # does not stand for its length; passes without provenance stay.
        spec = {s.name: s for s in skytest.all_specs(self.cfg)}["a"]
        now = {"spec_sha256": spec.sha256, "args_sha256": skytest.args_sha256(self.cfg, spec)}
        self.results("20261106-100000-session", [("a", "pass", 900.0)], provenance={**now, "spec_sha256": "old"},
                     jobs=1, ran_with=[], load={"samples": 3, "outside_games_max": 0})
        self.results("20261106-110000-session", [("a", "pass", 800.0)], provenance={**now, "args_sha256": "old"},
                     jobs=1, ran_with=[], load={"samples": 3, "outside_games_max": 0})
        self.results("20261106-120000-session", [("a", "pass", 340.0)], provenance=now, jobs=1, ran_with=[], load={"samples": 3, "outside_games_max": 0})
        rows = {r["case"]: r for r in self.estimate("1A")["cases"]}
        self.assertEqual(rows["a"]["seconds"], 340.0)
        self.assertEqual(rows["a"]["source"], "median of 1 pass(es) alone, newest 20261106-120000-session; 2 pass(es)"
                                              " of another spec text or arguments left out")
        # Only passes of another version: the spec's timeout stands, and says why.
        (self.cfg.store / "runs" / "20261101-100000-session" / "results.json").unlink()
        (self.cfg.store / "runs" / "20261106-120000-session" / "results.json").unlink()
        rows = {r["case"]: r for r in self.estimate("1A")["cases"]}
        self.assertEqual(rows["a"]["basis"], "timeout")
        self.assertEqual(rows["a"]["source"], "spec timeout (only passes of another spec text or arguments; longest"
                                              " fail 7.0 min, 20261102-100000-session)")
        # The spec's sha256 is the text load_spec read, not the file's later state.
        path = self.repo / "tests" / "runtime" / "a.toml"
        self.assertEqual(spec.sha256, skytest.hashlib.sha256(path.read_bytes()).hexdigest())
        path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        self.assertEqual(skytest.case_provenance(self.cfg, spec, "f")["spec_sha256"], spec.sha256)

    def test_a_case_that_is_not_ready_counts_0_min_and_is_named(self) -> None:
        # PS2 in the estimate: b (never passed, its timeout 30 min) is not ready: 0 min, named apart.
        self.spec("b", 'ready = false\nreason = "legs 11+"\n' + self.QUIT.format(stage="1A", timeout=1800))
        g = self.estimate("1A")
        rows = {r["case"]: r for r in g["cases"]}
        self.assertEqual(rows["b"]["seconds"], 0.0)
        self.assertEqual(rows["b"]["basis"], "not ready")
        self.assertEqual(rows["b"]["source"], "not ready: legs 11+")
        self.assertEqual(g["not_ready"], ["b"])
        self.assertEqual(g["timeout_minutes"], 0.0)
        self.assertIn("; not ready, 0 min: b;", skytest.gate_line(g))
        # A gate run reports it at 0 s, starts no game for it, and its verdict is not met.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, argparse.Namespace(cases=["a", "b"], stage="1A", max_games=5, gate=True))
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(code, 1)
        self.assertIn("b: NOT READY: legs 11+ (no game started)", lines)
        self.assertTrue(any(line.startswith("gate: ") and line.endswith("NOT met: b (not ready: legs 11+)")
                            for line in lines), lines)
        folder = Path(lines[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        cases = {c["case"]: c for c in results["cases"]}
        self.assertEqual(cases["b"]["status"], "not ready")
        self.assertEqual(cases["b"]["seconds"], 0.0)
        self.assertEqual(cases["b"]["not_ready"], "legs 11+")
        self.assertEqual(cases["b"]["folder"], "")
        self.assertEqual(cases["a"]["status"], "pass")
        self.assertFalse(results["gate"]["verdict"]["met"])
        self.assertEqual(results["gate"]["verdict"]["open"], ["b (not ready: legs 11+)"])
        self.assertIn("| b | solo | 1A | NOT READY | 0 s | not ready: legs 11+ |",
                      (folder / "report.md").read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in (self.cfg.store / "runs").iterdir() if p.name.endswith("-b")], [])

    def test_the_stage_filter(self) -> None:
        self.assertEqual([r["case"] for r in self.estimate("0")["cases"]], ["a"])
        self.assertFalse(self.estimate("0")["over"])
        self.assertEqual([r["case"] for r in self.estimate(None)["cases"]], ["a", "b", "nospec", "later"])

    def test_list_gate_starts_no_game(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_list(self.cfg, argparse.Namespace(gate=True, stage="1A"))
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("median of 1 pass(es), none known alone (1 with no recorded load), newest"
                      " 20261101-100000-session", text)
        self.assertIn("no spec", text)
        self.assertIn("gate: 3 cases, expected 5 min measured", text)
        self.assertNotIn("later", text)
        self.assertFalse(any((self.cfg.store / "runs").glob("*/report.md")))
        # Without --gate, list is as before.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_list(self.cfg, argparse.Namespace())
        self.assertIn("manifest: 5 case(s)", out.getvalue())

    def test_a_gate_run_runs_only_the_gate_cases_and_is_no_checkpoint(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, argparse.Namespace(cases=[], stage="1A", max_games=5, gate=True))
        lines = out.getvalue().strip().splitlines()
        self.assertTrue(lines[0].startswith("gate: 3 cases, expected 5 min measured"), lines[0])
        self.assertTrue(any(line.startswith("gate: ") and "(the cases" in line for line in lines), lines)
        folder = Path(lines[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(c["case"] for c in results["cases"]), ["a", "b"])
        self.assertIsNone(results["checkpoint"])
        self.assertEqual(results["required_without_spec"], ["nospec"])
        self.assertEqual(results["gate"]["no_spec"], ["nospec"])
        self.assertIn("actual_minutes", results["gate"])
        self.assertIn("## Gate", (folder / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertFalse(results["gate"]["verdict"]["met"])  # nospec has no spec
        self.assertEqual(results["gate"]["verdict"]["open"], ["nospec (no spec)"])
        # The run just made is the newest pass: a's median now takes it in, and b has a pass.
        g = self.estimate("1A")
        self.assertTrue(all(r["source"].endswith(f"newest {folder.name}") for r in g["cases"] if r["case"] in ("a", "b")))

    def test_a_gate_run_of_named_cases_counts_only_those(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, argparse.Namespace(cases=["a"], stage="1A", max_games=5, gate=True))
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("gate: 1 cases, expected 5 min measured (1 from the median of their"
                                            " passes: 0 run alone, 1 of unknown load (no pass known alone):"
                                            " a)"), lines[0])
        self.assertNotIn("over by", lines[0])
        folder = Path(lines[-1].split("report: ", 1)[1]).parent
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual([r["case"] for r in results["gate"]["cases"]], ["a"])
        self.assertEqual(results["required_without_spec"], [])
        # A spec-less gate case a pattern names stays named.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=["a", "nospec"], stage="1A", max_games=5, gate=True))
        first = out.getvalue().splitlines()[0]
        self.assertTrue(first.startswith("gate: 2 cases"), first)
        self.assertIn("no spec, not counted: nospec", first)

    # --- the route per build (the owner's decision 3, 2026-11-07) -------------------------------------

    def add_route(self, gate: str = '"per-build"') -> None:
        self.spec("route", self.QUIT.format(stage="1A", timeout=2400))
        manifest = self.repo / "tests" / "manifest.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8")
                            + '[[case]]\nid = "route"\nstage = "1A"\nlayout = "solo"\nkind = "required"\n'
                            + f"gate = {gate}\nspec = true\n", encoding="utf-8")

    def gate_run(self, *cases: str, per_build: bool = False) -> tuple[int, list[str], dict]:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, argparse.Namespace(cases=list(cases), stage="1A", max_games=5, gate=True,
                                                                 per_build=per_build))
        lines = out.getvalue().strip().splitlines()
        folder = Path(lines[-1].split("report: ", 1)[1]).parent
        return code, lines, json.loads((folder / "results.json").read_text(encoding="utf-8"))

    def test_the_per_build_verdict(self) -> None:
        # gate_verdict alone: a per-build case is met when its newest result on this build is a pass (per_build_state), else open
        # with the words of the owner's decision; not ready (PS2) wins over a pass.
        g = {"cases": [{"case": "a", "basis": "alone"}], "cadence": "daily", "build": "abc123",
             "per_build": [{"case": "route", "basis": "timeout", "source": "spec timeout"}]}
        a = [skytest.CaseResult(case="a", layout="solo", stage="0", status="pass")]
        met = {"met": True, "pass": "20261107-100000-session", "last": ["pass", "20261107-100000-session"], "other": 0}
        v = skytest.gate_verdict(g, a, {"route": None})
        self.assertFalse(v["met"])
        self.assertEqual(v["line"], "gate: NOT met: per-build case route has no pass for this build")
        v = skytest.gate_verdict(g, a, {"route": {"met": False, "pass": None, "last": ["fail", "r2"], "other": 1}})
        self.assertEqual(v["open"], ["per-build case route has no pass for this build (last result on it: fail in"
                                     " r2; 1 pass(es) of another spec text or arguments, or with no provenance,"
                                     " left out)"])
        # A later fail undid an earlier pass on the same build: open, and it says so.
        v = skytest.gate_verdict(g, a, {"route": {"met": False, "pass": None, "last": ["fail", "r3"], "undone": "r2",
                                                  "other": 0}})
        self.assertEqual(v["open"], ["per-build case route's last result on this build is not a pass (last result on"
                                     " it: fail in r3; its pass in r2 came before that result, the newest result"
                                     " decides)"])
        v = skytest.gate_verdict(g, a, {"route": met})
        self.assertTrue(v["met"])
        self.assertEqual(v["line"], "gate: met (per-build, passed on build abc123: route (20261107-100000-session))")
        # A daily case that failed keeps the gate open even when the route passed on this build.
        v = skytest.gate_verdict(g, [replace(a[0], status="fail")], {"route": met})
        self.assertEqual(v["open"], ["a (fail)"])
        # Not ready wins: the route's spec says ready = false, whatever passed on this build before.
        nr = {**g, "per_build": [{"case": "route", "basis": "not ready", "source": "not ready: legs 11+"}]}
        v = skytest.gate_verdict(nr, a, {"route": met})
        self.assertEqual(v["open"], ["route (not ready: legs 11+)"])
        # list --gate's row of a not-ready per-build case: no minutes, its reason once.
        row = {"case": "route", "layout": "solo", "stage": "1B", "seconds": 0, "basis": "not ready",
               "source": "not ready: legs 11+"}
        table = skytest.per_build_table({**nr, "per_build": [row]})
        self.assertEqual(len(table), 2)
        self.assertIn("    - min  not ready: legs 11+", table[1])
        self.assertEqual(table[1].count("legs 11+"), 1)
        self.assertNotIn("|", table[1])
        nospec = {**g, "per_build": [{"case": "route", "basis": "no spec", "source": "no spec"}]}
        self.assertEqual(skytest.gate_verdict(nospec, a, {"route": met})["open"], ["route (no spec)"])
        # A --per-build run's verdict is over its own cases, with its own label.
        pb = {"cases": [{"case": "route", "basis": "timeout"}], "cadence": "per-build", "per_build": []}
        route = skytest.CaseResult(case="route", layout="solo", stage="1A", status="pass")
        self.assertEqual(skytest.gate_verdict(pb, [route])["line"], "gate (per-build): met")
        self.assertEqual(skytest.gate_verdict(pb, [])["line"], "gate (per-build): NOT met: route (not run)")

    def test_a_per_build_pass_counts_only_on_the_same_build_and_spec(self) -> None:
        self.add_route()
        spec = {s.name: s for s in skytest.all_specs(self.cfg)}["route"]
        now = {"spec_sha256": spec.sha256, "args_sha256": skytest.args_sha256(self.cfg, spec)}
        build = "0123456789ab"

        def snap(h: str) -> dict:
            return {"snapshot": str(self.cfg.store / "snapshots" / h)}

        def state() -> dict:
            return skytest.per_build_state(self.cfg, spec, skytest.build_results(self.cfg, build).get("route", []),
                                           build)

        d = self.cfg.store / "runs"
        for name, h, status, prov in [("20261107-090000-session", "ffffffffffff", "pass", now),  # another build
                                      ("20261107-100000-session", build, "fail", now),
                                      ("20261107-110000-session", build, "pass", {**now, "spec_sha256": "old"})]:
            (d / name).mkdir(parents=True)
            (d / name / "results.json").write_text(json.dumps({**snap(h), "cases": [
                {"case": "route", "status": status, "seconds": 1500.0, "provenance": prov}]}), encoding="utf-8")
        s = state()
        self.assertFalse(s["met"])
        self.assertEqual(s["last"], ["fail", "20261107-100000-session"])
        self.assertEqual(s["other"], 1)
        left_out = "; 1 pass(es) of another spec text or arguments, or with no provenance, left out"
        self.assertEqual(s["text"], "no pass on this build: last result on it: fail (20261107-100000-session)"
                         + left_out)

        def add(name: str, status: str, prov: dict) -> None:
            (d / name).mkdir(parents=True)
            (d / name / "results.json").write_text(json.dumps({**snap(build), "cases": [
                {"case": "route", "status": status, "seconds": 1500.0, "provenance": prov}]}), encoding="utf-8")

        # A pass of the spec as it is now on this build meets it. A fail with no provenance, and a result
        # that never started the game (capacity), are left out: the pass stands.
        add("20261107-120000-session", "pass", now)
        add("20261107-121000-session", "fail", {})
        add("20261107-122000-session", "capacity", now)
        s = state()
        self.assertTrue(s["met"])
        self.assertEqual(s["pass"], "20261107-120000-session")
        self.assertEqual(s["text"], "last result on this build: pass (20261107-120000-session)" + left_out)
        # The owner's decision 3: the newest result decides. A later fail of the same spec text and arguments
        # on this build undoes the pass; a pass with no provenance does not count.
        add("20261107-123000-session", "fail", now)
        add("20261107-124000-session", "pass", {})
        s = state()
        self.assertFalse(s["met"])
        self.assertIsNone(s["pass"])
        self.assertEqual(s["undone"], "20261107-120000-session")
        self.assertEqual(s["other"], 2)
        self.assertEqual(s["text"], "last result on this build: fail (20261107-123000-session), after its pass in"
                                    " 20261107-120000-session (the newest result decides); 2 pass(es) of another"
                                    " spec text or arguments, or with no provenance, left out")
        # A new pass of it decides again.
        add("20261107-125000-session", "pass", now)
        s = state()
        self.assertTrue(s["met"])
        self.assertEqual(s["pass"], "20261107-125000-session")
        self.assertIsNone(s["undone"])
        # A snapshot path written with slashes or backslashes names the same build; no build: never met.
        (d / "20261107-140000-session").mkdir(parents=True)
        (d / "20261107-140000-session" / "results.json").write_text(json.dumps({
            "snapshot": f"C:/store/snapshots/{build}", "cases": [{"case": "other", "status": "pass"}]}), encoding="utf-8")
        (d / "20261107-150000-session").mkdir(parents=True)
        (d / "20261107-150000-session" / "results.json").write_text(json.dumps({
            "snapshot": f"C:\\store\\snapshots\\{build}", "cases": [{"case": "third", "status": "fail"}]}),
            encoding="utf-8")
        found = skytest.build_results(self.cfg, build)
        self.assertEqual(found["other"], [("20261107-140000-session", "pass", {})])
        self.assertEqual(found["third"], [("20261107-150000-session", "fail", {})])
        self.assertEqual(skytest.build_results(self.cfg, ""), {})
        self.assertFalse(skytest.per_build_state(self.cfg, spec, [], "")["met"])

    def test_the_daily_gate_leaves_the_route_out_and_counts_its_pass_on_this_build(self) -> None:
        self.add_route()
        # The daily gate: route is not run, listed apart, and has no pass on this build yet.
        code, lines, results = self.gate_run()
        self.assertIn("per-build, not run here (once per build, `test --gate --per-build`): route (no pass on this"
                      " build)", lines[0])
        self.assertTrue(lines[0].startswith("gate: 3 cases"), lines[0])  # a, b, nospec: route's minutes apart
        self.assertEqual(sorted(c["case"] for c in results["cases"]), ["a", "b"])
        build = skytest.build_hash(self.cfg)
        self.assertEqual(Path(results["snapshot"]).name, build)
        self.assertEqual(results["gate"]["verdict"]["open"],
                         ["nospec (no spec)", "per-build case route has no pass for this build"])
        # PS118 in a gate run: b (never passed: its 30 min timeout) starts before a (about 5 min).
        self.assertTrue(any(line.startswith("order (exclusive first, then longest first): b (30.0 min), a (") for line in lines), lines)
        self.assertLess(lines.index("b (solo) ..."), lines.index("a (solo) ..."))
        # The idle slot's run: only the per-build cases, its own label and verdict.
        code, lines, results = self.gate_run(per_build=True)
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("gate (per-build): 1 cases"), lines[0])
        self.assertNotIn("the plan's subset", lines[0])
        self.assertEqual([c["case"] for c in results["cases"]], ["route"])
        self.assertTrue(results["gate"]["verdict"]["met"])
        self.assertTrue(any(line.startswith("gate (per-build): ") and line.endswith("); met") for line in lines), lines)
        # The daily gate again: route counts as met by that pass on the same build.
        code, lines, results = self.gate_run()
        self.assertIn("route (passed on this build (", lines[0])
        self.assertEqual(results["gate"]["verdict"]["open"], ["nospec (no spec)"])
        self.assertTrue(results["gate"]["verdict"]["per_build"]["route"]["met"])
        self.assertEqual(results["gate"]["build"], build)
        # list --gate shows it apart with its last result on this build.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_list(self.cfg, argparse.Namespace(gate=True, stage="1A"))
        text = out.getvalue()
        self.assertIn(f"per-build (once per build in an idle slot, not in the daily gate; this build: {build}):", text)
        self.assertRegex(text, r"route +solo +stage 1A .*\| last result on this build: pass \(\d{8}-\d{6}-session\)")
        # A new build is a new snapshot: no pass on it yet.
        exe = self.repo / "out" / "build" / "game.exe"
        exe.write_bytes(b"MZ fake, rebuilt")
        self.assertNotEqual(skytest.build_hash(self.cfg), build)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            skytest.cmd_list(self.cfg, argparse.Namespace(gate=True, stage="1A"))
        self.assertIn("| no result on this build", out.getvalue())
        # Naming the route in a daily gate run says where it runs; --per-build needs --gate.
        code, lines, results = self.gate_run("a", "route")
        self.assertIn("gate: route is a per-build case: not run in the daily gate (`test --gate --per-build` runs it);"
                      " its pass on this build counts", lines)
        self.assertEqual([c["case"] for c in results["cases"]], ["a"])
        self.assertEqual(results["gate"]["verdict"]["open"], ["per-build case route has no pass for this build"])
        with self.assertRaises(SystemExit):
            skytest.cmd_test(self.cfg, argparse.Namespace(cases=[], stage=None, max_games=5, per_build=True))

    def test_lint_refuses_another_gate_value(self) -> None:
        self.add_route('"weekly"')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("manifest: route: gate must be true, false or \"per-build\" (not 'weekly')", out.getvalue())
        self.assertEqual(skytest.gate_cadence({"gate": True}), "daily")
        self.assertEqual(skytest.gate_cadence({"gate": "per-build"}), "per-build")
        self.assertEqual(skytest.gate_cadence({"gate": 1}), "")
        self.assertEqual(skytest.gate_cadence({}), "")

    def test_the_run_order_is_exclusive_first_then_longest_by_the_estimate(self) -> None:
        # RunOrder reads the gate estimate's basis: a's passes (about 5 min), b's timeout (never passed,
        # 30 min), plain's pass (0.5 min); x is exclusive and short, and still goes first.
        self.spec("x", 'exclusive = true\n' + self.QUIT.format(stage="0", timeout=60))
        specs = {s.name: s for s in skytest.all_specs(self.cfg)}
        order = skytest.RunOrder(self.cfg)
        self.assertEqual(order.seconds(specs["b"]), 1800.0)
        self.assertEqual(order.seconds(specs["a"]), 300.0)
        ranked = sorted([specs[n] for n in ("plain", "a", "x", "b")], key=order)
        self.assertEqual([s.name for s in ranked], ["x", "b", "a", "plain"])
        self.assertEqual(order.line([specs["a"], specs["x"]]),
                         "order (exclusive first, then longest first): x (exclusive, 1.0 min), a (5.0 min)")


class CompareTest(unittest.TestCase):
    """The [compare] grade on hand-written server logs (no game log from the disc)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_folder(self, name: str, s_log: str, ended: bool = True) -> Path:
        """A hosted run's folder: H's log, and S's in H's session folder."""
        d = skytest.fresh_dir(self.root / name)
        (d / "H" / skytest.HOSTED_SERVER_LOG).parent.mkdir(parents=True)
        (d / "H" / skytest.HOSTED_SERVER_LOG).write_text(s_log, encoding="utf-8")
        (d / "H" / "game.log").write_text(stamped((1.0, "Online: hosting: the server is on port 5000, join code X"))
                                          + (stamped((200.0, "Online: the session ended")) if ended else ""),
                                          encoding="utf-8")
        return d

    def compare(self, logs: dict[str, str], c: dict | None = None, ended: dict[str, bool] | None = None) -> dict:
        c = dict(c or COMPARE)
        spec = skytest.Spec(name="draw-skip-ab", path=Path("draw-skip-ab.toml"), layout="server+1", stage="1B",
                            description="", timeout=900, peers={}, measures={}, standin_seconds=0, server="host",
                            compare=c)
        runs = [skytest.gather_run(n, self.run_folder(n, text, (ended or {}).get(n, True)), spec)
                for n, text in logs.items()]
        return skytest.compare_runs(runs, c)

    def test_the_keys_of_a_recorded_log(self) -> None:
        k = skytest.compare_keys(server_log(skip=True), COMPARE)
        self.assertEqual(k["sequences"]["session"], [
            "the server is in its hub (town_state10)", "the owner asked for tutorial_b", "level 2 tutorial_b: open",
            "level 2: the server has it up", "barrier 2 released (1 client(s))", "End session: closing (1 to drain)"])
        self.assertEqual(k["sequences"]["level_watch"], [
            "tutorial_b: requested by the game (request 1)", "tutorial_b: loading screen up (request 1)",
            "tutorial_b: the game's load call returned (request 1)", "tutorial_b: ready (request 1)"])
        # No SetInterruptCallback line: the guest's start falls back to the log's first line. S's own
        # request line comes 0.2 s after the open: the level's spans start there.
        self.assertEqual(k["loads"], {"guest start -> hub": 24.0, "level 2 tutorial_b: request -> server up": 7.9,
                                      "level 2 tutorial_b: request -> barrier": 9.8,
                                      "level watch tutorial_b: load call": 7.7})
        # Sampled: the highest running number seen in the window (from barrier 2 to End session); the
        # shutdown's messages after End session do not count.
        self.assertEqual(k["messages"]["mode"], "sampled")
        self.assertEqual(k["messages"]["keys"], {"message CTickMessage": 7000, "trigger enter SpawnVolume#12": 1,
                                                 "telegram CSpawnMessage": 3})
        self.assertEqual(k["messages"]["window_s"], 120.5)
        self.assertEqual(k["gpu"]["windows"], 13)
        self.assertEqual((k["gpu"]["draws"], k["gpu"]["readbacks"], k["gpu"]["skipped_draws"]), (13 * 8123, 0, 13 * 7900))

    def test_exact_counts_come_from_two_dumps_in_the_window(self) -> None:
        dumps = ({"message CTickMessage": 2000, "telegram send Spawner#3": 5, "ai pick X -> Y": 9},
                 {"message CTickMessage": 9123, "trigger enter SpawnVolume#12": 1, "telegram send Spawner#3": 6,
                  "ai pick X -> Y": 10})
        k = skytest.compare_keys(server_log(dumps=dumps), COMPARE)
        self.assertEqual(k["messages"]["mode"], "exact")
        # The difference between the two dumps, world-message keys only (not trace_telegrams' or the AI's).
        self.assertEqual(k["messages"]["keys"], {"message CTickMessage": 7123, "trigger enter SpawnVolume#12": 1})

    def test_a_dump_after_the_window_end_is_not_used(self) -> None:
        # A dump written after `window_end` (the shutdown) must not stand in for the window's last one.
        dumps = ({"message CTickMessage": 2000}, {"message CTickMessage": 9123})
        late = stamped((400.0, "Trace counts: 1 keys"), (400.0, "Trace count: message CTickMessage 99999"))
        k = skytest.compare_keys(server_log(dumps=dumps) + late, COMPARE)
        self.assertEqual(k["messages"]["mode"], "exact")
        self.assertEqual(k["messages"]["keys"], {"message CTickMessage": 7123})

    def test_the_aa_pair_alone(self) -> None:
        c = {**COMPARE, "test": []}
        cmp = self.compare({"off1": server_log(level_load=8.0), "off2": server_log(level_load=8.4, ticks=7500)}, c)
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        self.assertEqual(cmp["messages"]["noisy"], 1)
        self.assertEqual(cmp["loads"]["level 2 tutorial_b: request -> server up"]["tolerance"], 0.5, "the floor")
        md = "\n".join(skytest.compare_markdown(cmp))
        self.assertIn("A/A: off1, off2.", md)
        self.assertIn("| message CTickMessage | 7000 / 7500 |", md)

    def test_the_ab_passes_with_equal_lines_and_loads_within_the_noise(self) -> None:
        cmp = self.compare({"off1": server_log(level_load=8.0, ticks=7000),
                            "off2": server_log(level_load=9.0, ticks=7500),
                            # The skip on: a faster load passes; 8000 is within 7000-7500 widened by 500.
                            "on": server_log(skip=True, level_load=6.0, ticks=8000)})
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        self.assertEqual(cmp["loads"]["level 2 tutorial_b: request -> server up"]["tolerance"], 1.0, "the A/A spread")
        md = "\n".join(skytest.compare_markdown(cmp))
        self.assertIn("### Comparison of S's logs: PASS", md)
        self.assertIn("| on | yes | 6 | 4 | 3 keys (sampled) | 120.5 s | 13 | 105599 | 0 | 0 | 102700/15600 |", md)

    def test_the_ab_fails_on_counts_order_messages_loads_and_readbacks(self) -> None:
        aa = {"off1": server_log(), "off2": server_log()}
        cases = {
            "an extra barrier line": (server_log(barriers=(2, 2)),
                                      "compare: barrier and level lines: on and off1 differ from the A/A: counts "
                                      "'barrier 2 released (1 client(s))' 1 vs 2"),
            "the order": (server_log(swap=True),
                          "compare: barrier and level lines: on and off1 differ from the A/A: order at line 4: "
                          "'level 2: the server has it up' vs 'barrier 2 released (1 client(s))'"),
            "a message count": (server_log(ticks=8000),
                                "compare: world messages outside the A/A noise (sampled): message CTickMessage on "
                                "8000 (A/A 7000/7000; sampled, one step 500)"),
            "a slower load": (server_log(level_load=9.0),
                              "compare: load level 2 tutorial_b: request -> server up: on 8.900 s, over the A/A "
                              "7.900-7.900 s by more than 0.500 s"),
            "a readback": (server_log(readbacks=2), "compare: run on: 26 resolve readback(s), expected 0"),
            "memexport": (server_log(memexport=1), "compare: run on: 13 memexport draw(s)"),
        }
        for what, (on, reason) in cases.items():
            cmp = self.compare({**aa, "on": on})
            self.assertEqual(cmp["status"], "fail", what)
            self.assertTrue(any(why.startswith(reason) for why in cmp["reasons"]), (what, cmp["reasons"]))
        # The level watch lines are their own family: a different interleaving with the session's
        # lines is not graded; only the order inside a family is.
        cmp = self.compare({**aa, "on": server_log(interleave=True)})
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        # An A/A that disagrees on the lines gives no baseline: that fails too.
        cmp = self.compare({"off1": server_log(), "off2": server_log(barriers=(2, 2)), "on": server_log()})
        self.assertTrue(any("off2 and off1 the A/A runs disagree" in why for why in cmp["reasons"]), cmp["reasons"])
        # memexport_zero = false reports memexport draws without failing on them.
        self.assertEqual(self.compare({**aa, "on": server_log(memexport=1)}, {**COMPARE, "memexport_zero": False})
                         ["status"], "pass")

    def test_load_spans_start_at_the_guest_and_at_the_request(self) -> None:
        # P1j C2: the host stalled 4-20 s before the guest's first line, and the session opened the
        # boot hub 47 s before S's game asked for it. Neither wait is a load.
        log = stamped(
            (0.0, "Skylanders: Swap Force test (build abc1234); Windows"),
            (3.0, "Initializing shader storage"),
            (15.0, "SetInterruptCallback(82F58198, 40003980)"),
            (15.01, "Online: level 1 town/town_state10/town_state10: open"),
            (40.0, "SetInterruptCallback(82F58198, 40003980)"),  # only the first one is the guest's start
            (62.0, "Level watch: town_state10: requested by the game (request 1, zone 0xB52F6C70)"),
            (79.0, "Online: the server is in its hub (town_state10)"),
            (80.0, "Online: level 1: the server has it up"),
            (89.0, "Online: barrier 1 released at tick 5226 (1 client)"),
            # A request of another zone before this level is up, and its own request only after it is
            # up: from the open.
            (90.0, "Online: level 2 tutorial/tutorial_b/tutorial_b: open"),
            (90.5, "Level watch: town_state10: requested by the game (request 2, zone 0xB52F6C70)"),
            (98.0, "Online: level 2: the server has it up"),
            (99.0, "Level watch: tutorial_b: requested by the game (request 3, zone 0xB52D7D38)"),
            (100.0, "Online: barrier 2 released at tick 6232 (1 client)"),
            # The game asked first, then the session opened it: from the open.
            (110.0, "Level watch: ww1: requested by the game (request 4, zone 0xB52D0000)"),
            (110.2, "Online: level 3 ww/ww1/WW1: open"),
            (120.0, "Online: level 3: the server has it up"),
            (121.0, "Online: barrier 3 released at tick 7000 (1 client)"),
        )
        loads = skytest.compare_keys(log, COMPARE)["loads"]
        self.assertEqual(loads, {
            "guest start -> hub": 64.0,
            "level 1 town/town_state10/town_state10: request -> server up": 18.0,
            "level 1 town/town_state10/town_state10: request -> barrier": 27.0,
            "level 2 tutorial/tutorial_b/tutorial_b: open -> server up": 8.0,
            "level 2 tutorial/tutorial_b/tutorial_b: open -> barrier": 10.0,
            "level 3 ww/ww1/WW1: open -> server up": 9.8,
            "level 3 ww/ww1/WW1: open -> barrier": 10.8,
        })

    def test_a_startup_stall_does_not_fail_the_load_grade(self) -> None:
        # The C2 run: the on-run's host stalled 3.5 s longer before its guest started; from the guest's
        # start on it was as fast as the A/A. Anchored at the log's first line it failed.
        def run(stall: float) -> str:
            return stamped(
                (0.0, "Skylanders: Swap Force test (build abc1234); Windows"),
                (11.5 + stall, "SetInterruptCallback(82F58198, 40003980)"),
                (11.5 + stall + 0.01, "Online: level 1 town/town_state10/town_state10: open"),
                (40.0 + stall, "Level watch: town_state10: requested by the game (request 1, zone 0xB52F6C70)"),
                (77.5 + stall, "Online: the server is in its hub (town_state10)"),
                (78.5 + stall, "Online: level 1: the server has it up"),
                (90.0 + stall, "Online: barrier 1 released at tick 5226 (1 client)"),
            )
        c = {**COMPARE, "reached": {"S": [r"Online: barrier 1 released"]}}
        cmp = self.compare({"off1": run(0.0), "off2": run(0.2), "on": run(3.5)}, c)
        # (No trace or GPU lines in these logs: only the load grade is looked at.)
        self.assertEqual([why for why in cmp["reasons"] if why.startswith("compare: load")], [])
        self.assertTrue(all(e["ok"] for e in cmp["loads"].values()), cmp["loads"])
        self.assertEqual(sorted(cmp["loads"]), ["guest start -> hub",
                                                "level 1 town/town_state10/town_state10: request -> barrier",
                                                "level 1 town/town_state10/town_state10: request -> server up"])
        # A run whose request came before the open measures from the open: the anchor differs, said
        # once per span, not as two "not in run(s)" lines.
        early = stamped(
            (0.0, "Skylanders: Swap Force test (build abc1234); Windows"),
            (11.5, "SetInterruptCallback(82F58198, 40003980)"),
            (12.0, "Level watch: town_state10: requested by the game (request 1, zone 0xB52F6C70)"),
            (40.0, "Online: level 1 town/town_state10/town_state10: open"),
            (77.5, "Online: the server is in its hub (town_state10)"),
            (78.5, "Online: level 1: the server has it up"),
            (90.0, "Online: barrier 1 released at tick 5226 (1 client)"),
        )
        cmp = self.compare({"off1": run(0.0), "off2": run(0.2), "on": early}, c)
        loads = [why for why in cmp["reasons"] if why.startswith("compare: load")]
        self.assertEqual(loads, [
            "compare: load level 1 town/town_state10/town_state10 -> barrier: the anchor differs between runs: "
            "the request in run(s) off1, off2, the open in run(s) on",
            "compare: load level 1 town/town_state10/town_state10 -> server up: the anchor differs between runs: "
            "the request in run(s) off1, off2, the open in run(s) on"], cmp["reasons"])

    def test_a_sampled_count_is_known_to_within_one_step(self) -> None:
        # traces.cpp logs a key's first 20 events and every 500th: a highest #n of 20 or more says the
        # true count lies between it and the next logged number.
        self.assertEqual(skytest.sampled_range(19), (19, 19))
        self.assertEqual(skytest.sampled_range(20), (20, 500))
        self.assertEqual(skytest.sampled_range(6500), (6500, 7000))
        self.assertEqual(skytest.counts_within_noise([6500, 6500], 7000, True), (True, 500))
        self.assertEqual(skytest.counts_within_noise([6500, 6500], 6000, True), (True, 500))
        self.assertEqual(skytest.counts_within_noise([6500, 6500], 7500, True), (False, 500))
        self.assertEqual(skytest.counts_within_noise([6500, 6500], 5500, True), (False, 500))
        self.assertEqual(skytest.counts_within_noise([3, 3], 4, True), (False, 0), "under 20 stays exact")
        self.assertEqual(skytest.counts_within_noise([19, 19], 20, True)[0], False, "19 is exact, 20 is at least 20")
        self.assertEqual(skytest.counts_within_noise([6500, 6500], 7000, False), (False, 0), "exact stays exact")
        # The A/A spread still widens the range on top of the step.
        self.assertTrue(skytest.counts_within_noise([6000, 6500], 7500, True)[0])
        self.assertFalse(skytest.counts_within_noise([6000, 6500], 8000, True)[0])

    def test_the_p1e_case_passes_within_one_step(self) -> None:
        # P1e's draw-skip-ab: two trigger keys at 6500 / 6500 / 7000, one event apart at most.
        aa = {"off1": server_log(ticks=6500), "off2": server_log(ticks=6500)}
        cmp = self.compare({**aa, "on": server_log(skip=True, ticks=7000)})
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        e = cmp["messages"]["keys"]["message CTickMessage"]
        self.assertEqual((e["ok"], e["within_step"], e["step"]), (True, True, 500))
        self.assertEqual(cmp["messages"]["within_step"], 1)
        md = "\n".join(skytest.compare_markdown(cmp))
        self.assertIn("| message CTickMessage (sampled, within one step) | 6500 / 6500 | 7000 |", md)
        self.assertIn("1 key(s) passed that way (marked 'sampled, within one step')", md)
        # Two steps apart fails.
        cmp = self.compare({**aa, "on": server_log(skip=True, ticks=7500)})
        self.assertEqual(cmp["status"], "fail")
        self.assertIn("compare: world messages outside the A/A noise (sampled): message CTickMessage on 7500 "
                      "(A/A 6500/6500; sampled, one step 500)", cmp["reasons"])
        self.assertIn("| message CTickMessage (FAIL) | 6500 / 6500 | 7500 |", "\n".join(skytest.compare_markdown(cmp)))
        # A key under 20 stays exact: 3 / 3 / 4 fails.
        cmp = self.compare({"off1": server_log(), "off2": server_log(), "on": server_log(spawns=4)})
        self.assertEqual(cmp["status"], "fail")
        self.assertIn("compare: world messages outside the A/A noise (sampled): telegram CSpawnMessage on 4 "
                      "(A/A 3/3)", cmp["reasons"])

    def test_exact_counts_stay_exact(self) -> None:
        # Two `Trace counts` dumps in the window: counts are exact, one event off fails.
        def dumped(n: int) -> str:
            return server_log(dumps=({"message CTickMessage": 100}, {"message CTickMessage": 100 + n}))
        cmp = self.compare({"off1": dumped(6500), "off2": dumped(6500), "on": dumped(6501)})
        self.assertEqual(cmp["messages"]["mode"], "exact")
        self.assertEqual(cmp["status"], "fail")
        self.assertIn("compare: world messages outside the A/A noise (exact): message CTickMessage on 6501 "
                      "(A/A 6500/6500)", cmp["reasons"])
        md = "\n".join(skytest.compare_markdown(cmp))
        self.assertNotIn("Sampled counts", md)
        self.assertEqual(self.compare({"off1": dumped(6500), "off2": dumped(6500), "on": dumped(6500)})["status"],
                         "pass")

    def test_a_stopped_run_is_not_reached(self) -> None:
        # The skip-on run stopped in the hub: no barrier 2, and its owner never ended the session.
        cmp = self.compare({"off1": server_log(), "off2": server_log(), "on": server_log(stop_before_level=True)},
                           ended={"on": False})
        self.assertEqual(cmp["status"], "not reached")
        self.assertEqual(cmp["reasons"], [
            "compare: not reached: run on: S lacks /Online: barrier 2 released at tick \\d+ \\(1 client\\)/, "
            "H lacks /Online: the session ended/"])
        self.assertEqual(cmp["loads"], {}, "nothing is graded on a run without its evidence")
        self.assertIn("### Comparison of S's logs: NOT REACHED", "\n".join(skytest.compare_markdown(cmp)))
        # A run that reached its evidence but logs no counters or world messages fails (not reached is
        # only for missing evidence).
        bare = stamped((0.0, "Online: the server is in its hub (town_state10)"),
                       (40.0, "Online: level 2 tutorial_b: open"), (48.0, "Online: level 2: the server has it up"),
                       (50.0, "Online: barrier 2 released at tick 1714 (1 client)"))
        cmp = self.compare({"off1": bare, "off2": bare, "on": bare})
        self.assertEqual(cmp["status"], "fail")
        self.assertIn("compare: world messages: no count in the window (trace_world_messages on the compared peer?)",
                      cmp["reasons"])
        self.assertIn("compare: run on: no `GPU: draws` line (gpu_draw_counters on the compared peer?)", cmp["reasons"])

    def test_the_frames_in_a_window_come_from_the_swaps(self) -> None:
        # Each GPU line counts the swaps of the 10 s before it; a line that covers the window in part
        # counts that part.
        log = skytest.parse_log(stamped(
            (10.0, "GPU: draws 1 memexport 0 copies 1 swaps 600 zpd 0 viz 0 readbacks 0 skipped 0/0 in 10.0 s"),
            (20.0, "GPU: draws 1 memexport 0 copies 1 swaps 500 zpd 0 viz 0 readbacks 0 skipped 0/0 in 10.0 s")))
        base = log["lines"][0][0] - 10.0
        self.assertEqual(skytest.window_frames(log["lines"], base + 5.0, base + 18.0), 700.0)
        # The log ends at 20 s: 20-25 s is filled at the covered part's rate (800 swaps in 15 s).
        self.assertEqual(skytest.window_frames(log["lines"], base + 5.0, base + 25.0), 1066.7)
        self.assertIsNone(skytest.window_frames(log["lines"], base + 30.0, base + 40.0))
        self.assertEqual(skytest.compare_keys(server_log(fps=57), COMPARE)["messages"]["frames"], 57 * 120.5)

    def test_per_frame_keys_are_graded_as_rates(self) -> None:
        # traces.cpp: act and touch come every server frame while the hero stands in the volume. With
        # exact counts (two dumps, 118.9 s apart) and 0.9 events per frame, 57 fps gives 6100 and
        # 60 fps 6421: unequal counts, equal rates.
        def dumped(fps: int, per_frame: float, enters: int = 1) -> str:
            n = round(per_frame * fps * 118.9)
            return server_log(fps=fps, dumps=({"trigger act Gate#7": 0, "trigger enter Gate#7": 0},
                                              {"trigger act Gate#7": n, "trigger enter Gate#7": enters}))
        aa = {"off1": dumped(57, 0.9), "off2": dumped(57, 0.9)}
        cmp = self.compare({**aa, "on": dumped(60, 0.9)})
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        msgs = cmp["messages"]
        self.assertEqual(msgs["rate_unit"], "frame")
        self.assertEqual(sorted(msgs["rates"]), ["trigger act Gate#7"])
        self.assertEqual(sorted(msgs["keys"]), ["trigger enter Gate#7"], "enter stays a count")
        e = msgs["rates"]["trigger act Gate#7"]
        self.assertEqual((e["noise"], e["test"]), ([6100, 6100], {"on": 6421}))
        self.assertAlmostEqual(e["test_rate"]["on"][0], 0.9, places=3)
        self.assertAlmostEqual(e["tolerance"], 0.009, places=4, msg="no A/A spread: 1% of the A/A rate")
        md = "\n".join(skytest.compare_markdown(cmp))
        self.assertIn("1 per-frame key(s) graded as rates (below)", md)
        self.assertIn("Frames in the window: off1 6777.3 (57.00 per s over 118.9 s), off2 6777.3", md)
        self.assertIn("| trigger act Gate#7 | 6100 / 6100 | 0.9001 / 0.9001 | 0.0090 | 6421: 0.9001 |", md)
        # The per-frame rate doubled fails.
        cmp = self.compare({**aa, "on": dumped(60, 1.8)})
        self.assertEqual(cmp["status"], "fail")
        self.assertIn("compare: per-frame keys outside the A/A noise (events per frame): trigger act Gate#7 on "
                      "1.8000 (A/A 0.9001/0.9001, tolerance 0.0090)", cmp["reasons"])
        self.assertIn("| trigger act Gate#7 (FAIL) |", "\n".join(skytest.compare_markdown(cmp)))
        # Counts are unchanged: an extra enter fails as before, whatever the frame rate.
        cmp = self.compare({**aa, "on": dumped(60, 0.9, enters=2)})
        self.assertEqual(cmp["status"], "fail")
        self.assertIn("compare: world messages outside the A/A noise (exact): trigger enter Gate#7 on 2 (A/A 1/1)",
                      cmp["reasons"])
        # A floor of 0 and a 1% faster rate: outside an A/A with no spread.
        cmp = self.compare({**aa, "on": dumped(60, 0.91)}, {**COMPARE, "rate_floor": 0.0})
        self.assertEqual(cmp["status"], "fail")
        self.assertEqual(self.compare({**aa, "on": dumped(60, 0.905)})["status"], "pass", "within 1%")

    def test_sampled_rates_are_ranges_and_seconds_without_frames(self) -> None:
        # P1e: 6500 act events at 57 fps (twice) and 7000 at 60 fps, each known to one step of 500.
        aa = {"off1": server_log(fps=57, acts=6500), "off2": server_log(fps=57, acts=6500)}
        cmp = self.compare({**aa, "on": server_log(fps=60, acts=7000)})
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        e = cmp["messages"]["rates"]["trigger act Gate#7"]
        self.assertEqual([round(x, 4) for x in e["test_rate"]["on"]], [round(7000 / 7230, 4), round(7500 / 7230, 4)])
        self.assertIn("(a sampled count gives a range of rates, low-high)", "\n".join(skytest.compare_markdown(cmp)))
        cmp = self.compare({**aa, "on": server_log(fps=60, acts=14000)})
        self.assertEqual(cmp["status"], "fail")
        self.assertTrue(any(w.startswith("compare: per-frame keys outside the A/A noise (events per frame): "
                                         "trigger act Gate#7 on 1.9364-2.0055") for w in cmp["reasons"]), cmp["reasons"])
        # No GPU line in a window: every run's rate is per second of its window.
        cmp = self.compare({**aa, "on": server_log(acts=7000, gpu_lines=False)})
        self.assertEqual(cmp["messages"]["rate_unit"], "second")
        self.assertAlmostEqual(cmp["messages"]["rates"]["trigger act Gate#7"]["noise_rate"][0][0], 6500 / 120.5,
                               places=3)
        self.assertIn("Seconds in the window: off1 120.5, off2 120.5, on 120.5.",
                      "\n".join(skytest.compare_markdown(cmp)))

    def test_count_and_order_expectations(self) -> None:
        text = server_log()
        ps = skytest.PeerSpec(name="S", expect_count={r"Online: barrier \d+ released": 1, r"GPU: draws": 13},
                              expect_order=[r"in its hub", r"level 2 \S+: open", r"barrier 2 released"])
        self.assertEqual(skytest.check_count_and_order(text, ps), [])
        ps = skytest.PeerSpec(name="S", expect_count={r"Online: barrier \d+ released": 2},
                              expect_order=[r"barrier 2 released", r"level 2 \S+: open"])
        self.assertEqual(skytest.check_count_and_order(text, ps), [
            r"log has /Online: barrier \d+ released/ 1 time(s), expected 2",
            r"log lacks /level 2 \S+: open/ after /barrier 2 released/ (expect_order)"])


THRESHOLDS = {"press_to_reaction": {"fixed_ms": 250.0, "plus": ["send_interval", "rtt"]},
              "send_rates": {"players_hz": 20.0}}


def requester_log(pairs: list[tuple[int, float]], others: list[tuple[int, str]] = (), rtt_us: int = 1500,
                  generation: int = 2, peer: int = 1) -> str:
    """A client's log as the game writes it (online/client/attack_presses.cpp): a barrier line with
    its round trip, then per press its press line and its reaction line."""
    t = 0
    lines = [f"[2026-11-06 10:00:00.000] [info] [core] [t1] Online: barrier {generation} released at tick 900",
             f"[2026-11-06 10:00:00.001] [info] [core] [t1] Online: tick estimate off by 0.10 ticks "
             f"(round trip {rtt_us} us)"]
    for seq, ms in pairs:
        t += 1
        lines.append(f"[2026-11-06 10:00:{t:02d}.000] [info] [core] [t1] Online: press {peer}:{seq} slot 0 at pose "
                     f"{seq * 14} (generation {generation})")
        lines.append(f"[2026-11-06 10:00:{t:02d}.300] [info] [core] [t2] Online: reaction {peer}:{seq} after {ms:.1f} "
                     f"ms (interim) landed victim w00000f2c.1a2b3c4d.5e6f7a8b (generation {generation})")
    for seq, outcome in others:
        t += 1
        lines.append(f"[2026-11-06 10:00:{t:02d}.000] [info] [core] [t1] Online: press {peer}:{seq} slot 1 at pose "
                     f"{seq * 14} (generation {generation})")
        if outcome:
            lines.append(f"[2026-11-06 10:00:{t:02d}.900] [info] [core] [t2] Online: reaction {peer}:{seq} {outcome} "
                         f"after 2003.0 ms (interim) (generation {generation})")
    return "\n".join(lines) + "\n"


class PressToReactionTest(unittest.TestCase):
    """The press_to_reaction grade (stage 1B item 7) on hand-written requester logs."""

    def grade(self, text: str, g: dict | None = None) -> tuple[dict, list[str]]:
        lines = [line for _, line in skytest.parse_log(text)["lines"]]
        return skytest.grade_press_to_reaction(lines, g or {}, THRESHOLDS)

    def test_landed_pairs_pass_under_the_limit(self) -> None:
        pairs = [(i, 100.0 + i) for i in range(1, 13)]
        graded, reasons = self.grade(requester_log(pairs, [(13, "missed"), (14, "refused"), (15, "")]))
        self.assertEqual(reasons, [])
        self.assertTrue(graded["interim"])
        self.assertEqual((graded["presses"], graded["pairs"], graded["missed"], graded["refused"],
                          graded["unanswered"]), (15, 12, 1, 1, 1))
        # 250 + one send interval (1000 / 20 Hz) + the logged round trip (1.5 ms).
        self.assertAlmostEqual(graded["limit_ms"], 301.5)
        self.assertEqual((graded["send_interval_ms"], graded["rtt_ms"], graded["rtt_source"]), (50.0, 1.5, "log"))
        self.assertEqual((graded["min_ms"], graded["p50_ms"], graded["p95_ms"], graded["max_ms"]),
                         (101.0, 106.0, 112.0, 112.0))
        self.assertEqual(graded["value_ms"], 112.0)
        self.assertNotIn("violation", graded)

    def test_the_p95_over_the_limit_fails_as_interim(self) -> None:
        pairs = [(i, 120.0) for i in range(1, 19)] + [(19, 350.0), (20, 360.0)]
        graded, reasons = self.grade(requester_log(pairs))
        self.assertEqual(graded["p95_ms"], 350.0)
        self.assertTrue(graded["violation"])
        self.assertEqual(reasons, ["grade press_to_reaction: (interim) p95 350.0 ms over 301.5 ms"])
        # The reason names the grade the way the runner's accepted-grade check reads it.
        self.assertEqual(reasons[0].split(":", 1)[0].removeprefix("grade "), "press_to_reaction")
        # The median passes the same log.
        _, reasons = self.grade(requester_log(pairs), {"statistic": "p50"})
        self.assertEqual(reasons, [])

    def test_too_few_pairs_and_no_presses_fail(self) -> None:
        graded, reasons = self.grade(requester_log([(1, 90.0), (2, 95.0)], [(3, "missed")]))
        self.assertEqual(graded["pairs"], 2)
        self.assertEqual(reasons, ["grade press_to_reaction: (interim) 2 landed pairs, fewer than min_pairs 10 "
                                   "(3 presses, 1 missed, 0 refused, 0 unanswered)"])
        _, reasons = self.grade(requester_log([(1, 90.0), (2, 95.0)]), {"min_pairs": 2})
        self.assertEqual(reasons, [])
        graded, reasons = self.grade(requester_log([]))
        self.assertEqual(reasons, ["grade press_to_reaction: (interim) no press line"])
        self.assertNotIn("p95_ms", graded)

    def test_a_reaction_pairs_only_with_its_own_press(self) -> None:
        text = requester_log([(1, 80.0), (2, 85.0)])
        # A second reaction for press 2, one for a press never made, and the server's lines (S's
        # `press <peer>:<seq> replayed ...`) are not pairs.
        text += ("[2026-11-06 10:01:00.000] [info] [core] [t2] Online: reaction 1:2 after 99.0 ms (interim) landed "
                 "victim w1.2.3 (generation 2)\n"
                 "[2026-11-06 10:01:01.000] [info] [core] [t2] Online: reaction 1:7 after 99.0 ms (interim) landed "
                 "victim w1.2.3 (generation 2)\n"
                 "[2026-11-06 10:01:02.000] [info] [core] [t3] Online: press 1:8 replayed slot 0 on PeerPuppet_1 "
                 "(facing pose 57)\n")
        graded, _ = self.grade(text, {"min_pairs": 1})
        self.assertEqual((graded["presses"], graded["pairs"], graded["unpaired_reactions"]), (2, 2, 2))
        self.assertEqual(graded["max_ms"], 85.0)

    def test_the_generation_keeps_equal_sequences_apart(self) -> None:
        text = requester_log([(1, 80.0)], generation=2) + requester_log([(1, 90.0)], generation=3)
        graded, _ = self.grade(text, {"min_pairs": 1})
        self.assertEqual((graded["presses"], graded["pairs"]), (2, 2))
        # A reaction of another generation does not answer the press.
        text = requester_log([], [(1, "")], generation=2)
        text += ("[2026-11-06 10:02:00.000] [info] [core] [t2] Online: reaction 1:1 after 70.0 ms (interim) landed "
                 "victim w1.2.3 (generation 3)\n")
        graded, _ = self.grade(text, {"min_pairs": 1})
        self.assertEqual((graded["pairs"], graded["unanswered"], graded["unpaired_reactions"]), (0, 1, 1))

    def test_the_limit_parts_can_be_set_by_the_spec(self) -> None:
        graded, _ = self.grade(requester_log([(1, 80.0)], rtt_us=40000), {"min_pairs": 1})
        self.assertAlmostEqual(graded["limit_ms"], 340.0)
        graded, _ = self.grade(requester_log([(1, 80.0)]), {"min_pairs": 1, "send_interval_ms": 100, "rtt_ms": 0})
        self.assertEqual((graded["limit_ms"], graded["rtt_source"]), (350.0, "spec"))
        lines = ["Online: press 1:1 slot 0 at pose 3 (generation 2)",
                 "Online: reaction 1:1 after 10.0 ms (interim) landed victim none (generation 2)"]
        graded, _ = skytest.grade_press_to_reaction(lines, {"min_pairs": 1}, THRESHOLDS)
        self.assertEqual((graded["rtt_ms"], graded["rtt_source"], graded["limit_ms"]), (0.0, "none", 300.0))

    def test_the_grade_runs_from_the_spec_on_its_peer(self) -> None:
        spec = argparse.Namespace(grades={"press_to_reaction": {"peer": "H", "min_pairs": 1,
                                                                "after": "Online: barrier 2 released"}})
        text = requester_log([(1, 80.0), (2, 500.0)])
        r = skytest.PeerResult(peer="H")
        reasons = skytest.grade_thresholds(spec, r, skytest.parse_log(text), THRESHOLDS, 60.0)
        self.assertEqual(reasons, ["grade press_to_reaction: (interim) p95 500.0 ms over 301.5 ms"])
        self.assertEqual(r.graded["press_to_reaction"]["pairs"], 2)
        other = skytest.PeerResult(peer="S")
        self.assertEqual(skytest.grade_thresholds(spec, other, skytest.parse_log(text), THRESHOLDS, 60.0), [])

    def test_a_press_to_reaction_spec_is_checked(self) -> None:
        skytest.check_press_to_reaction("x.toml", {"min_pairs": 10, "statistic": "max", "rtt_ms": 1.5})
        for bad in ({"min_pairs": 0}, {"min_pairs": True}, {"statistic": "mean"}, {"send_interval_ms": -1},
                    {"rtt_ms": "1"}):
            with self.assertRaises(skytest.SpecError, msg=str(bad)):
                skytest.check_press_to_reaction("x.toml", bad)
        self.assertIn("press_to_reaction", skytest.GRADES)


def rq(line: str, t: str = "12:00:00.000") -> str:
    """One `Online:` line as the game writes it."""
    return f"[2026-11-06 {t}] [info] [core] [t1] Online: {line}"


class RequestsAnsweredTest(unittest.TestCase):
    """The requests_answered grade (stage 1B item 9) on hand-written logs."""

    E1 = "2/1/enter/1/w00000962.1a2b3c4d.5e6f7a8b"
    X1 = "2/1/exit/1/w00000962.1a2b3c4d.5e6f7a8b"
    T1 = "2/1/touch/1/w00000962.1a2b3c4d.5e6f7a8b"
    E2 = "2/1/enter/2/w00000999.1a2b3c4d.5e6f7a8b"

    def test_one_final_per_key_passes_with_retries_and_cached_apart(self) -> None:
        log = "\n".join([
            rq(f"request 1:1 enter {self.E1}"),
            rq(f"decided {self.E1} pending"),
            rq(f"refused {self.E1} (too_far)"),           # retryable: not final
            rq(f"resend {self.E1}"),
            rq(f"decided {self.E1} applied seq 4"),
            rq(f"already applied {self.E1}"),             # a cached repeat
            rq(f"request 1:1 exit {self.X1}"),
            rq(f"refused {self.X1} (exit before enter)"),  # retryable for an exit
            rq(f"final {self.X1} refused (exit before enter)"),
            rq(f"request 1:2 enter {self.E2}"),
            rq(f"refused {self.E2} (already fired)"),     # terminal (amendment l)
            rq(f"refused {self.E2} (already fired) cached"),
        ])
        graded, reasons = skytest.grade_requests_answered(log, set())
        self.assertEqual(reasons, [])
        self.assertEqual((graded["keys"], graded["finals"], graded["applied"], graded["cached_answers"],
                          graded["retried_refusals"]), (3, 3, 1, 2, 2))

    def test_no_final_two_finals_and_two_applies_fail(self) -> None:
        log = "\n".join([
            rq(f"request 1:1 enter {self.E1}"),
            rq(f"refused {self.E1} (too_far)"),            # only retryable: no final
            rq(f"request 1:1 touch {self.T1}"),
            rq(f"decided {self.T1} applied seq 2"),
            rq(f"final {self.T1} refused (generation ended)"),
            rq(f"request 1:2 enter {self.E2}"),
            rq(f"decided {self.E2} applied seq 3"),
            rq(f"decided {self.E2} applied seq 5"),
        ])
        graded, reasons = skytest.grade_requests_answered(log, set())
        self.assertTrue(graded["violation"])
        self.assertEqual(len(reasons), 3)
        self.assertIn("1 key(s) without a final decision: " + self.E1, reasons[0])
        self.assertIn("more than one final decision", reasons[1])
        self.assertIn("applied twice: " + self.E2, reasons[2])
        self.assertEqual(reasons[0].split(":", 1)[0].removeprefix("grade "), "requests_answered")

    def test_a_key_not_sent_at_end_session_is_left_out(self) -> None:
        log = "\n".join([
            rq(f"request 1:1 enter {self.E1}"),
            rq(f"decided {self.E1} applied seq 1"),
            rq(f"request 1:2 enter {self.E2}"),
            rq(f"request {self.E2} not sent (End session or no connection)"),
        ])
        graded, reasons = skytest.grade_requests_answered(log, set())
        self.assertEqual(reasons, [])
        self.assertEqual((graded["keys"], graded["left_out_not_sent"]), (1, 1))
        # A `not sent` line does not excuse a key that was sent (and answered) another time.
        log += "\n" + rq(f"request 1:1 enter {self.E1}") + "\n" + rq(f"request {self.E1} not sent (End session)")
        graded, reasons = skytest.grade_requests_answered(log, set())
        self.assertEqual((graded["keys"], graded["left_out_not_sent"], reasons), (1, 1, []))
        # Sol council batch r2 C1: a key that got an answer (pending, then a retryable too_far)
        # and whose resend was `not sent` is graded, and fails with no final.
        e3 = "2/1/enter/3/w00000999.1a2b3c4d.5e6f7a8c"
        log += "\n" + "\n".join([rq(f"request 1:3 enter {e3}"), rq(f"decided {e3} pending"),
                                 rq(f"refused {e3} (too_far)"),
                                 rq(f"request {e3} not sent (End session or no connection)")])
        graded, reasons = skytest.grade_requests_answered(log, set())
        self.assertEqual((graded["keys"], graded["left_out_not_sent"]), (2, 1))
        self.assertEqual(reasons, [f"grade requests_answered: 1 key(s) without a final decision: {e3}"])

    def test_a_peer_gone_at_close_is_left_out_and_no_request_is_not_reached(self) -> None:
        log = rq(f"request 1:1 enter {self.E1}")
        graded, reasons = skytest.grade_requests_answered(log, {"1"})
        self.assertEqual(graded["left_out_gone"], 1)
        self.assertEqual(reasons, ["grade requests_answered: no request line (not reached)"])
        _, reasons = skytest.grade_requests_answered(rq("frames 600 fps 60.0"), set())
        self.assertEqual(reasons, ["grade requests_answered: no request line (not reached)"])

    def test_gone_at_close_leaves_out_keys_on_the_server_only(self) -> None:
        # S's `peer 1 gone at close` explains S's own unanswered keys of peer 1; peer 1's own log
        # still holds the answers it got, so its keys stay graded there.
        spec = skytest.Spec(name="r", path=Path("r.toml"), layout="server+1", stage="1B", description="",
                            timeout=1, peers={}, measures={}, standin_seconds=0,
                            grades={"requests_answered": {"peers": ["S", "H"]}})
        s_log = rq(f"request 1:1 enter {self.E1}") + "\n" + rq("peer 1 gone at close")
        h_log = rq(f"request 1:1 enter {self.E1}") + "\n" + rq(f"decided {self.E1} applied seq 4")
        for peer, expect_keys, expect_gone in (("S", 0, 1), ("H", 1, 0)):
            r = skytest.PeerResult(peer=peer)
            skytest.grade_cross_peer(spec, r, {"S": s_log, "H": h_log})
            self.assertEqual((r.graded["requests_answered"]["keys"], r.graded["requests_answered"]["left_out_gone"]),
                             (expect_keys, expect_gone), peer)

    def test_spec_options(self) -> None:
        skytest.check_requests_answered("x.toml", {}, ["S", "H", "D"])
        skytest.check_requests_answered("x.toml", {"peers": ["S", "H"]}, ["S", "H", "D"])
        for bad in ({"peers": []}, {"peers": ["E"]}, {"peers": "H"}):
            with self.assertRaises(skytest.SpecError, msg=str(bad)):
                skytest.check_requests_answered("x.toml", bad, ["S", "H", "D"])
        self.assertIn("requests_answered", skytest.GRADES)


class SymmetryTest(unittest.TestCase):
    """The symmetry grade (stage 1B item 9) on hand-written server logs: two halves after
    `symmetry reset`, generations and sequences normalized, the peer ids swapped."""

    T = "w00000962.1a2b3c4d.5e6f7a8b"
    U = "w00000999.1a2b3c4d.5e6f7a8b"

    def half(self, gen: int, peer: int, first_seq: int, digest: str = "d1g", extra: list[str] | None = None) -> list[str]:
        e1 = f"{gen}/{peer}/enter/{first_seq}/{self.T}"
        x1 = f"{gen}/{peer}/exit/{first_seq}/{self.T}"
        e2 = f"{gen}/{peer}/enter/{first_seq + 1}/{self.U}"
        lines = [
            rq(f"symmetry reset {gen} {digest}"),
            rq(f"request {peer}:{first_seq} enter {e1}"),
            rq(f"decided {e1} pending"),
            rq(f"applied step {e1} seq {gen * 10} steps 2: enter 0x3D; telegram w000003f1.0a0b0c0d.0e0f1011#2.cafe0001 other"),
            rq(f"decided {e1} applied seq {gen * 10}"),
            rq(f"request {peer}:{first_seq} exit {x1}"),
            rq(f"decided {x1} applied seq {gen * 10 + 1}"),
            rq(f"request {peer}:{first_seq + 1} enter {e2}"),
            rq(f"refused {e2} (already fired)"),
        ]
        return lines + (extra or [])

    def test_equal_halves_pass(self) -> None:
        text = "\n".join(self.half(3, 1, 1) + self.half(4, 2, 7))
        graded, reasons = skytest.grade_symmetry(text, {})
        self.assertEqual(reasons, [])
        self.assertEqual(graded["generations"], ["3", "4"])
        self.assertEqual(graded["keys"], [3, 3])
        # Timing-dependent answers are left out: a retryable refusal, cached repeats.
        text = "\n".join(self.half(3, 1, 1) + self.half(4, 2, 7) + [
            rq(f"refused 4/2/enter/8/{self.U} (too_far)"),
            rq(f"pending 4/2/enter/7/{self.T}"),
        ])
        graded, reasons = skytest.grade_symmetry(text, {})
        self.assertEqual(reasons, [])
        self.assertEqual(graded["retried_refusals"], [0, 1])

    def test_a_different_decision_fails_with_where(self) -> None:
        second = self.half(4, 2, 7)
        second[-1] = second[-1].replace("(already fired)", "(unsupported)")
        graded, reasons = skytest.grade_symmetry("\n".join(self.half(3, 1, 1) + second), {})
        self.assertTrue(graded["violation"])
        self.assertEqual(len(reasons), 1)
        self.assertIn("grade symmetry: peer 1 decisions differ at 2", reasons[0])
        self.assertIn("(already fired)", reasons[0])
        self.assertIn("(unsupported)", reasons[0])

    def test_unequal_digests_and_a_missing_reset_are_not_reached(self) -> None:
        _, reasons = skytest.grade_symmetry("\n".join(self.half(3, 1, 1) + self.half(4, 2, 7, digest="other")), {})
        self.assertEqual(reasons, ["grade symmetry: not reached (the halves' digests differ: d1g and other)"])
        _, reasons = skytest.grade_symmetry("\n".join(self.half(3, 1, 1)), {})
        self.assertEqual(reasons, ["grade symmetry: not reached (1 `symmetry reset` line(s), two needed)"])

    def test_the_halves_by_generation_and_the_hub_reset_before_them(self) -> None:
        hub = [rq("symmetry reset 1 0000hub0")]
        text = "\n".join(hub + self.half(3, 1, 1) + self.half(4, 2, 7))
        graded, reasons = skytest.grade_symmetry(text, {})  # the last two
        self.assertEqual(reasons, [])
        graded, reasons = skytest.grade_symmetry(text, {"generations": [3, 4]})
        self.assertEqual((reasons, graded["generations"]), ([], ["3", "4"]))
        _, reasons = skytest.grade_symmetry(text, {"generations": [3, 5]})
        self.assertEqual(reasons, ["grade symmetry: not reached (no `symmetry reset` line for generation(s) 5)"])
        _, reasons = skytest.grade_symmetry(text, {"generations": [1, 3]})
        self.assertIn("not reached (the halves' digests differ", reasons[0])

    def test_the_wrong_peer_in_the_second_half_fails(self) -> None:
        # The owner played both halves: after the swap its second half reads as peer 2's.
        graded, reasons = skytest.grade_symmetry("\n".join(self.half(3, 1, 1) + self.half(4, 1, 7)), {})
        self.assertTrue(graded["violation"])
        self.assertTrue(any("peer 1 requests differ" in r for r in reasons))
        self.assertTrue(any("peer 2 requests differ" in r for r in reasons))

    def test_spec_options(self) -> None:
        skytest.check_symmetry("x.toml", {}, ["S", "H", "D"])
        skytest.check_symmetry("x.toml", {"owner": 1, "client": 3}, ["S", "H", "D", "E"])
        skytest.check_symmetry("x.toml", {"generations": [2, 3]}, ["S", "H", "D"])
        for bad in ({"peer": "E"}, {"owner": 2, "client": 2}, {"owner": 0}, {"client": True},
                    {"generations": [2]}, {"generations": [2, "3"]}, {"generations": [0, 1]}):
            with self.assertRaises(skytest.SpecError, msg=str(bad)):
                skytest.check_symmetry("x.toml", bad, ["S", "H", "D"])
        self.assertIn("symmetry", skytest.GRADES)


ROUTE_FILE = """
[[trigger]]
name = "JumpHint_Trigger"
class = "two_player"

[[trigger]]
name = "CScriptTriggerEntity#2392"
class = "request"

[[trigger]]
name = "LocalHint"
class = "local"

[level_end]
class = "COpenResultsScreenMessage"
recipient = "WorldInstance#2048"
next_zone = "town_state10"

[[encounter]]
manager = "EA_Manager_W1"

[[checkpoint]]
trigger = "CScriptTriggerEntity#2651"
expect = 'Trace: message CCheckPoint\\w+Message to '

[[scene]]
name = "TutorialB_Intro"
"""


class RouteTest(unittest.TestCase):
    """The route grade (stage 1B item 13) on hand-written logs and a hand-written route file."""

    def checklist(self, text: str = ROUTE_FILE) -> dict:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tutorial_b.toml"
            f.write_text(text, encoding="utf-8")
            checklist, why = skytest.load_route_checklist(f)
        self.assertEqual(why, "")
        return checklist

    def server(self, end: bool = True, twice: bool = False, next_level: bool = True) -> str:
        lines = [
            rq("level 2 tutorial/tutorial_b/tutorial_b: open"),
            rq("barrier 2 released at tick 100 (3 clients)"),
            rq("scene 2/1.w00000001.00000002.0000abcd start cutscene TutorialB_Intro recipient Tessa#12 from x"),
            rq("scene 2/1.w00000001.00000002.0000abcd end by its timeline after 3.0 s (x)"),
            rq("replay 2.0:2/2/enter/0/w1 JumpHint_Trigger#2394 by PeerPuppet_1#3830 flags 0x3D"),
            rq("replay done 2.0:2/2/enter/0/w1 activated 1 depth 0"),
            rq("replay 2.1:2/2/enter/1/w2 CScriptTriggerEntity#2392 by PeerPuppet_1#3830 flags 0x3D"),
            rq("replay done 2.1:2/2/enter/1/w2 activated 1 depth 0"),
            rq("encounter start EA_Manager_W1#2688 (from 0x82A0CE88)"),
            rq("encounter complete EA_Manager_W1#2688"),
            rq("replay 2.2:2/3/enter/0/w3 CScriptTriggerEntity#2651 by PeerPuppet_2#3831 flags 0x3D"),
            rq("replay done 2.2:2/3/enter/0/w3 activated 1 depth 0"),
            "[2026-11-06 12:00:00.000] [info] [core] [t1] Trace: message CCheckPointTriggeredMessage to Hero#1 from x (#1)",
        ]
        if end:
            lines += [
                rq("level end COpenResultsScreenMessage to WorldInstance#2048 (generation 2)"),
                rq("level end COpenResultsScreenMessage sent to 3 client(s) (generation 2)"),
                rq("level end CLevelCompleteMessage to WorldInstance#2048 (generation 2)"),
                rq("level end CLevelCompleteMessage to WorldInstance#2048 not sent (it follows the results screen)"
                   " (generation 2)"),
                rq("level end CLevelCompleteMessage to WorldInstance#2048 duplicate (generation 2; CDDAManager"
                   " sub_82A0AD78 skipped)"),
                rq("results screen LevelStars up (+0.5 s)"),
                rq("results screen LevelStars pressed A (press 1, 10.0 s without progress)"),
                rq("results screen LevelStars advanced by press to LevelSummary (+11.0 s)"),
                rq("results screen LevelSummary advanced by native to the next level (+14.0 s)"),
                rq("results screens done: the game asked for town_state10 from 0x82BEA3FC (x) after 14.0 s, 1 press(es)"),
            ]
        if twice:
            lines.append(rq("level end COpenResultsScreenMessage to WorldInstance#2048 (generation 3)"))
        if end and next_level:
            lines += [
                rq("level 3 town/town_state10/town_state10: open (the server's own level change from 0x82BEA3FC, after"
                   " the level end, x)"),
                rq("level 3: the server has it up"),
                rq("barrier 3 released at tick 900 (3 clients)"),
            ]
        return "\n".join(lines)

    def client(self, extra: str = "", played: str = "played here (generation 2, recipient WorldInstance#2048)") -> str:
        lines = [rq("level request 0x00000001 passed tutorial_b (the session: the session's own request)"),
                 rq("level end COpenResultsScreenMessage to WorldInstance#2048 suppressed here (the server owns it;"
                    " CJuiceComponent sub_82A280E8)"),
                 rq(f"level end COpenResultsScreenMessage from the server {played}"),
                 rq("level request 0x82BEA3FC blocked town_state10 (sub_82BEA370, hub map action: the server owns the"
                    " level end (a request after the level end of generation 2))"),
                 rq("level request 0x00000001 passed town_state10 (the session: the session's own request)")]
        if extra:
            lines.append(rq(extra))
        return "\n".join(lines)

    def test_full_route_passes(self) -> None:
        texts = {"S": self.server(), "H": self.client(), "D": self.client()}
        graded, reasons = skytest.grade_route(texts, self.checklist(), {"file": "tests/routes/tutorial_b.toml"})
        self.assertEqual(reasons, [])
        self.assertEqual(graded["level_end"], {"class": "COpenResultsScreenMessage", "recipient": "WorldInstance#2048",
                                               "generation": 2})
        self.assertEqual(graded["results_advanced"], {"native": 1, "press": 1})
        self.assertTrue(graded["results_done"])
        self.assertEqual(graded["next_level"], {"zone": "town_state10", "generation": 3, "up": True, "released": True})
        self.assertEqual(graded["triggers"], [2, 2])  # the local one is not graded
        self.assertEqual(graded["level_end_duplicates_skipped"], 1)
        self.assertEqual(graded["clients_played"], {"H": 1, "D": 1})

    def test_every_client_plays_the_servers_level_end_once(self) -> None:
        texts = {"S": self.server(), "H": self.client(),
                 "D": self.client(played="not played here (unresolved recipient: no entity)")}
        graded, reasons = skytest.grade_route(texts, self.checklist(), {"file": "x"})
        self.assertEqual(reasons, ["grade route: client D played the server's level end COpenResultsScreenMessage of"
                                   " generation 2 0 times (exactly once)"])
        self.assertEqual(graded["clients_played"], {"H": 1, "D": 0})
        self.assertTrue(graded["violation"])
        # Twice, or once in another generation, fails as well.
        texts["D"] = self.client("level end COpenResultsScreenMessage from the server played here (generation 2,"
                                 " recipient WorldInstance#2048)")
        _, reasons = skytest.grade_route(texts, self.checklist(), {"file": "x"})
        self.assertEqual(reasons, ["grade route: client D played the server's level end COpenResultsScreenMessage of"
                                   " generation 2 2 times (exactly once)"])
        texts["D"] = self.client(played="played here (generation 3, recipient WorldInstance#2048)")
        _, reasons = skytest.grade_route(texts, self.checklist(), {"file": "x"})
        self.assertEqual(len(reasons), 1)
        self.assertIn("0 times", reasons[0])

    def test_a_stopped_run_is_not_reached(self) -> None:
        texts = {"S": self.server(end=False), "H": self.client()}
        graded, reasons = skytest.grade_route(texts, self.checklist(), {"file": "x"})
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("grade route: not reached (no `level end COpenResultsScreenMessage to"
                                              " WorldInstance#2048` line on S; seen before the run stopped: "))
        self.assertIn("triggers 2 of 2", reasons[0])
        self.assertNotIn("violation", graded)
        # An empty server log (the run died before anything): not reached as well.
        _, reasons = skytest.grade_route({"S": ""}, self.checklist(), {"file": "x"})
        self.assertTrue(reasons[0].startswith("grade route: not reached"))

    def test_a_level_end_twice_fails(self) -> None:
        graded, reasons = skytest.grade_route({"S": self.server(twice=True)}, self.checklist(), {"file": "x",
                                                                                                 "clients": []})
        self.assertIn("grade route: level end COpenResultsScreenMessage to WorldInstance#2048 2 times (once per class"
                      " and recipient)", reasons)
        self.assertTrue(graded["violation"])

    def test_next_level_must_open_as_the_servers_own(self) -> None:
        _, reasons = skytest.grade_route({"S": self.server(next_level=False)}, self.checklist(), {"file": "x",
                                                                                                  "clients": []})
        self.assertEqual(reasons, ["grade route: the next level town_state10 did not open as the server's own level"
                                   " change after the level end"])

    def test_a_client_leaving_by_itself_fails(self) -> None:
        texts = {"S": self.server(), "H": self.client(),
                 "D": self.client("level request 0x829C880C passed - (sub_829C87A0, main-menu return: out of the"
                                  " session)")}
        _, reasons = skytest.grade_route(texts, self.checklist(), {"file": "x"})
        self.assertEqual(reasons, ["grade route: client D left the level by itself (sub_829C87A0, main-menu return:"
                                   " out of the session)"])
        texts["D"] = self.client("level end COpenResultsScreenMessage to WorldInstance#2048 played here (not connected"
                                 " to the server)")
        _, reasons = skytest.grade_route(texts, self.checklist(), {"file": "x"})
        self.assertEqual(reasons, ["grade route: client D ran its own level end (COpenResultsScreenMessage to"
                                   " WorldInstance#2048)"])

    def test_missing_checklist_items_fail(self) -> None:
        server = self.server().replace("encounter complete EA_Manager_W1#2688", "encounter waiting")
        server = server.replace("replay done 2.1:2/2/enter/1/w2 activated 1", "replay done 2.1:2/2/enter/1/w2 activated 0")
        server = server.replace("TutorialB_Intro recipient", "OtherScene recipient")
        server = server.replace("Trace: message CCheckPointTriggeredMessage", "Trace: message Other")
        _, reasons = skytest.grade_route({"S": server}, self.checklist(), {"file": "x", "clients": []})
        self.assertEqual(reasons, [
            "grade route: trigger CScriptTriggerEntity#2392 never applied on the server",
            "grade route: encounter EA_Manager_W1 never completed on the server",
            "grade route: checkpoint CScriptTriggerEntity#2651: /Trace: message CCheckPoint\\w+Message to / not on"
            " the server",
            "grade route: scene TutorialB_Intro never started on the server",
        ])

    def test_route_file_without_level_end(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "r.toml"
            f.write_text("[[trigger]]\nname = \"x\"\nclass = \"request\"\n", encoding="utf-8")
            checklist, why = skytest.load_route_checklist(f)
            self.assertIsNone(checklist)
            self.assertEqual(why, "the route file r.toml has no [level_end] class (X11 names it)")
            checklist, why = skytest.load_route_checklist(Path(d) / "missing.toml")
            self.assertIsNone(checklist)
            self.assertTrue(why.startswith("the route file missing.toml cannot be read"))

    def test_every_request_key_ends_applied_or_expected(self) -> None:
        # Council 1B, Opus O5: H's keys from its own request lines, their ends from S's decisions.
        h_keys = ["2/1/enter/1/wA", "2/1/touch/1/wA", "2/1/enter/2/wB", "2/1/enter/3/wC", "2/1/exit/3/wC",
                  "2/1/telegram/4/wD#0.x", "2/1/enter/5/wE"]
        client = self.client() + "\n" + "\n".join(rq(f"request 1:{k.split('/')[3]} {k.split('/')[2]} {k}")
                                                 for k in h_keys)
        client += "\n" + rq("request 2/1/enter/5/wE not sent (End session or no connection)")
        server = self.server() + "\n" + "\n".join([
            rq("decided 2/1/enter/1/wA pending"),
            rq("refused 2/1/enter/1/wA (too_far)"),         # retryable: the resend below applies it
            rq("decided 2/1/enter/1/wA applied seq 4"),
            rq("decided 2/1/touch/1/wA applied seq 5"),
            rq("already applied 2/1/touch/1/wA"),           # a cached repeat changes nothing
            rq("replay 1.2:2/1/enter/2/wB Fence_Trigger#77 by PeerPuppet_1#3830 flags 0x3D"),
            rq("refused 2/1/enter/2/wB (already fired)"),
            rq("refused 2/1/enter/3/wC (unresolved)"),
            rq("refused 2/1/exit/3/wC (enter refused)"),
            rq("final 2/1/telegram/4/wD#0.x refused (unsupported)"),
        ])
        route = ROUTE_FILE + ('\n[[expected_refusal]]\nreason = "already fired"\nkind = "enter"\n'
                              'name = "Fence_Trigger"\nwhy = "H walks back through it"\n')
        g = {"file": "x", "clients": ["H"]}
        graded, reasons = skytest.grade_route({"S": server, "H": client}, self.checklist(route), g)
        self.assertEqual(reasons, [
            "grade route: 3 request key(s) of H refused on the server and not listed as an expected refusal:"
            " 2/1/enter/3/wC (unresolved), 2/1/exit/3/wC (enter refused), 2/1/telegram/4/wD#0.x (unsupported)"])
        self.assertEqual(graded["requests"]["H"], {"keys": 6, "applied": 2, "expected_refusals": 1,
                                                   "unexpected_refusals": 3, "without_end": 0,
                                                   "left_out_not_sent": 1})
        self.assertTrue(graded["violation"])
        # Listing both refusals accepts the exit that followed its enter's refusal as well.
        route += ('\n[[expected_refusal]]\nreason = "unresolved"\nwhy = "w"\n'
                  '\n[[expected_refusal]]\nreason = "unsupported"\nkind = "telegram"\nwhy = "w"\n')
        graded, reasons = skytest.grade_route({"S": server, "H": client}, self.checklist(route), g)
        self.assertEqual(reasons, [])
        self.assertEqual(graded["requests"]["H"]["expected_refusals"], 4)
        # Sol council batch C1: S answered the key (pending, then a retryable too_far), the resend
        # logged `not sent` as the session closed, and S never decided it. It is not left out: it
        # fails as a key without an end.
        c1_client = client + "\n" + rq("request 1:6 enter 2/1/enter/6/wF") + "\n" + rq(
            "request 2/1/enter/6/wF not sent (End session or no connection)")
        c1_server = server + "\n" + rq("decided 2/1/enter/6/wF pending") + "\n" + rq(
            "refused 2/1/enter/6/wF (too_far)")
        graded, reasons = skytest.grade_route({"S": c1_server, "H": c1_client}, self.checklist(route), g)
        self.assertEqual(graded["requests"]["H"]["left_out_not_sent"], 1)  # only wE, which S never saw
        self.assertEqual(graded["requests"]["H"]["without_end"], 1)
        self.assertEqual(reasons, ["grade route: 1 request key(s) of H without an end on the server: 2/1/enter/6/wF"])
        # A name that does not match S's replay name, or a kind that differs, does not accept it.
        wrong = route.replace('name = "Fence_Trigger"', 'name = "Fence_Trigger#78"')
        _, reasons = skytest.grade_route({"S": server, "H": client}, self.checklist(wrong), g)
        self.assertEqual(reasons, ["grade route: 1 request key(s) of H refused on the server and not listed as an"
                                   " expected refusal: 2/1/enter/2/wB (already fired; Fence_Trigger#77)"])
        # A key S never decided (only a retryable refusal) has no end; `requesters` narrows the peers.
        _, reasons = skytest.grade_route({"S": server.replace(rq("decided 2/1/enter/1/wA applied seq 4"), ""),
                                          "H": client}, self.checklist(route), g)
        self.assertEqual(reasons, ["grade route: 1 request key(s) of H without an end on the server: 2/1/enter/1/wA"])
        graded, reasons = skytest.grade_route({"S": server, "H": client, "D": self.client()}, self.checklist(route),
                                              {"file": "x", "clients": ["H", "D"], "requesters": ["D"]})
        self.assertEqual(reasons, [])
        self.assertEqual(list(graded["requests"]), ["D"])
        # A touch refused `enter refused` while its enter was later applied (a resend) was lost.
        lost = server.replace(rq("decided 2/1/touch/1/wA applied seq 5"), rq("refused 2/1/touch/1/wA (enter refused)"))
        lost = lost.replace(rq("already applied 2/1/touch/1/wA"), "")
        _, reasons = skytest.grade_route({"S": lost, "H": client}, self.checklist(route), g)
        self.assertEqual(reasons, ["grade route: 1 request key(s) of H refused on the server and not listed as an"
                                   " expected refusal: 2/1/touch/1/wA (enter refused)"])

    def test_expected_refusal_rows_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "r.toml"
            for row, why in (('reason = "already fired"', "expected_refusal 1 needs a `reason` and a `why`"),
                             ('reason = "x"\nwhy = "y"\nkind = "jump"', "expected_refusal 1: kind 'jump' is not one"
                              " of enter, exit, touch, telegram, skip"),
                             ('reason = "x"\nwhy = "y"\nname = ""', "expected_refusal 1: `name` must be a nonempty"
                              " name")):
                f.write_text(ROUTE_FILE + "\n[[expected_refusal]]\n" + row + "\n", encoding="utf-8")
                checklist, got = skytest.load_route_checklist(f)
                self.assertIsNone(checklist)
                self.assertEqual(got, "the route file r.toml: " + why)
        with self.assertRaises(skytest.SpecError):
            skytest.check_route("s", {"file": "tests/routes/x.toml", "requesters": ["S"]}, ["S", "H"])

    def test_cross_peer_reads_the_route_file_from_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / "tests" / "routes").mkdir(parents=True)
            (repo / "tests" / "routes" / "tutorial_b.toml").write_text(ROUTE_FILE, encoding="utf-8")
            spec = skytest.Spec(name="r", path=repo / "r.toml", layout="server+1", stage="1B", description="",
                                timeout=1, peers={}, measures={}, standin_seconds=0,
                                grades={"route": {"file": "tests/routes/tutorial_b.toml", "clients": ["H"]}})
            r = skytest.PeerResult(peer="S")
            reasons = skytest.grade_cross_peer(spec, r, {"S": self.server(), "H": self.client()}, repo, {})
            self.assertEqual(reasons, [])
            self.assertEqual(r.graded["route"]["level_end"]["generation"], 2)
            reasons = skytest.grade_cross_peer(spec, r, {"S": self.server(end=False)}, repo, {})
            self.assertTrue(reasons[0].startswith("grade route: not reached"))

    def test_route_name_matching(self) -> None:
        self.assertTrue(skytest.route_name_matches("JumpHint_Trigger", "jumphint_trigger#2394"))
        self.assertTrue(skytest.route_name_matches("CScriptTriggerEntity#2392", "CScriptTriggerEntity#2392"))
        self.assertFalse(skytest.route_name_matches("CScriptTriggerEntity#2392", "CScriptTriggerEntity#2398"))
        self.assertFalse(skytest.route_name_matches("JumpHint", "JumpHint_Trigger#2394"))

    def test_spec_options(self) -> None:
        skytest.check_route("x.toml", {"file": "tests/routes/tutorial_b.toml"}, ["S", "H", "D"])
        skytest.check_route("x.toml", {"file": "tests/routes/tutorial_b.toml", "clients": ["D"]}, ["S", "H", "D"])
        for bad in ({}, {"file": ""}, {"file": "/abs/route.toml"}, {"file": "../route.toml"},
                    {"file": "r.toml", "peer": "E"}, {"file": "r.toml", "clients": ["S"]},
                    {"file": "r.toml", "clients": "H"}):
            with self.assertRaises(skytest.SpecError, msg=str(bad)):
                skytest.check_route("x.toml", bad, ["S", "H", "D"])
        self.assertIn("route", skytest.GRADES)


class BandwidthTest(unittest.TestCase):
    """The bandwidth grade (stage 1B item 13) on hand-written server traffic lines."""

    THRESHOLDS = {"bandwidth_per_client": {"up_kilobytes_per_s": 4.0, "steady_down_kilobytes_per_s": 20.0}}

    @staticmethod
    def traffic(rows: list[tuple[str, int, int, int]], before: list[str] | None = None) -> list:
        """rows: (time, peer, up bytes so far, down bytes so far), up and down on the fast lane."""
        text = list(before or [])
        for t, peer, up, down in rows:
            text.append(rq(f"traffic peer {peer} gameplay 0/0 state 0/0 fast {down}/{up} bytes", t))
        return skytest.parse_log("\n".join(text))["lines"]

    def test_within_the_floor_passes(self) -> None:
        rows = [(f"12:00:{10 * i:02d}.000", 2, 30000 * i, 100000 * i) for i in range(5)]
        graded, reasons = skytest.grade_bandwidth(self.traffic(rows), {}, self.THRESHOLDS)
        self.assertEqual(reasons, [])
        self.assertEqual(graded["peers"]["2"]["windows"], 4)
        self.assertAlmostEqual(graded["peers"]["2"]["up_kbps"], 3.0)
        self.assertAlmostEqual(graded["peers"]["2"]["down_kbps"], 10.0)

    def test_over_the_up_limit_fails(self) -> None:
        rows = [(f"12:00:{10 * i:02d}.000", 3, 50000 * i, 1000 * i) for i in range(5)]
        graded, reasons = skytest.grade_bandwidth(self.traffic(rows), {"statistic": "max"}, self.THRESHOLDS)
        self.assertEqual(reasons, ["grade bandwidth: peer 3 up 5.00 KB/s (max) over 4.0 KB/s"])
        self.assertTrue(graded["violation"])

    def test_not_reached(self) -> None:
        _, reasons = skytest.grade_bandwidth(self.traffic([]), {}, self.THRESHOLDS)
        self.assertEqual(reasons, ["grade bandwidth: not reached (no `traffic peer` line in the window)"])
        rows = [("12:00:00.000", 2, 0, 0), ("12:00:10.000", 2, 100, 100)]
        _, reasons = skytest.grade_bandwidth(self.traffic(rows), {}, self.THRESHOLDS)
        self.assertEqual(reasons, ["grade bandwidth: not reached (peer 2: 1 window(s), 3 needed)"])

    def test_window_bounds_and_a_counter_reset(self) -> None:
        early = [("11:59:00.000", 2, 0, 0), ("11:59:10.000", 2, 900000, 0)]  # before `after`: not graded
        rows = [("12:00:00.000", 2, 1000, 0), ("12:00:10.000", 2, 21000, 0), ("12:00:20.000", 2, 100, 0),
                ("12:00:30.000", 2, 20100, 0), ("12:00:40.000", 2, 40100, 0)]
        lines = self.traffic(early) + self.traffic(rows, before=[rq("barrier 2 released at tick 1 (1 client)")])
        graded, reasons = skytest.grade_bandwidth(lines, {"after": "barrier 2 released", "min_windows": 3},
                                                  self.THRESHOLDS)
        self.assertEqual(reasons, [])
        self.assertEqual(graded["peers"]["2"]["windows"], 3)  # the window across the reset is left out
        self.assertAlmostEqual(graded["peers"]["2"]["up_max_kbps"], 2.0)

    def test_spec_options(self) -> None:
        skytest.check_bandwidth("x.toml", {}, ["S", "H"])
        skytest.check_bandwidth("x.toml", {"statistic": "max", "after": "barrier 2", "min_windows": 6}, ["S", "H"])
        for bad in ({"peer": "D"}, {"statistic": "mean"}, {"min_windows": 0}, {"min_windows": True},
                    {"after": "("}):
            with self.assertRaises(skytest.SpecError, msg=str(bad)):
                skytest.check_bandwidth("x.toml", bad, ["S", "H"])
        self.assertIn("bandwidth", skytest.GRADES)


class RunnerEnvironmentTest(unittest.TestCase):
    """The runner's environment: the token preflight, the startup stall, the power throttling opt-out."""

    def tearDown(self) -> None:
        skytest._THROTTLING_LOGGED.clear()

    @staticmethod
    def log(*lines: str) -> str:
        return "".join(f"{line}\n" for line in lines)

    def test_the_stall_parser_on_hand_written_logs(self) -> None:
        storage = "[2026-11-06 14:38:16.100] [info] [core] [t15248] Initializing shader storage for title 415608E9..."
        vfs = "[2026-11-06 14:38:23.991] [warning] [fs] [t26924] VFS: 'E:\\' -> [no device]"
        sic = "[2026-11-06 14:38:24.400] [info] [gpu] [t26924] SetInterruptCallback(82F58198, 40003980)"

        def stall(text: str) -> float | None:
            return skytest.parse_log(text)["startup_stall"]

        # The guest's first line after the storage: the VFS mount (0.4-0.9 s before SetInterruptCallback).
        self.assertEqual(stall(self.log(storage, "no stamp: a wrapped line", vfs, sic)), 7.89)
        self.assertEqual(stall(self.log(storage, sic)), 8.3, "SetInterruptCallback alone")
        self.assertEqual(skytest.startup_stall(skytest.parse_log(self.log(storage, sic))["lines"]), 8.3)
        early_vfs = "[2026-11-06 14:38:15.000] [info] [fs] [t1] VFS: 'game:' -> mounted"
        self.assertEqual(stall(self.log(early_vfs, storage, vfs)), 7.89, "a guest line before the storage is no end")
        fast = "[2026-11-06 14:38:16.130] [warning] [fs] [t26924] VFS: 'E:\\' -> [no device]"
        self.assertEqual(stall(self.log(storage, fast)), 0.03)
        self.assertIsNone(stall(self.log(vfs, sic)), "no storage line")
        self.assertIsNone(stall(self.log(storage)), "no guest line after it")
        self.assertIsNone(stall(""))

    @unittest.skipUnless(skytest.WINDOWS, "power throttling is Windows only")
    def test_the_throttling_calls_arguments(self) -> None:
        calls = []
        self.addCleanup(setattr, skytest, "set_process_information", skytest.set_process_information)

        def answering(*codes: int):
            it = iter(codes)

            def fake(handle: int, info_class: int, state: tuple[int, int, int]) -> int:
                calls.append((handle, info_class, state))
                return next(it)
            return fake

        # ProcessPowerThrottling (4); Version 1; control: execution speed (0x1) and timer resolution
        # (0x4); state 0: both kinds of throttling off.
        skytest.set_process_information = answering(0)
        self.assertEqual(skytest.opt_out_of_power_throttling(77), "opted out")
        self.assertEqual(calls, [(77, 4, (1, 0x5, 0))])
        # A Windows without the timer flag: the execution speed alone.
        calls.clear()
        skytest.set_process_information = answering(87, 0)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = skytest.opt_out_of_power_throttling(77)
        self.assertEqual(calls, [(77, 4, (1, 0x5, 0)), (77, 4, (1, 0x1, 0))])
        self.assertEqual(status, "opted out (execution speed only; the timer resolution flag was refused, error 87)")
        self.assertEqual(out.getvalue(), "", "a partial opt-out is no failure")
        # Refused twice, and a call that raises: one printed line for the runner process, never raised.
        skytest.set_process_information = answering(5, 5, 6, 6)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(skytest.opt_out_of_power_throttling(77), "failed: Windows error 5")
            self.assertEqual(skytest.opt_out_of_power_throttling(78), "failed: Windows error 6")

            def raising(*_):
                raise OSError("no SetProcessInformation")
            skytest.set_process_information = raising
            self.assertTrue(skytest.opt_out_of_power_throttling(79).startswith("failed: OSError"))
        self.assertEqual(out.getvalue(), "skytest: could not opt a game out of Windows power throttling (Windows "
                         "error 5); the run goes on, a busy machine may slow its games\n")

    @unittest.skipUnless(skytest.WINDOWS, "power throttling is Windows only")
    def test_the_opt_out_takes_on_a_suspended_process(self) -> None:
        ctypes, wintypes, _, k32 = skytest._win()
        p = subprocess.Popen([sys.executable, "-I", "-c", "pass"], creationflags=winjob.SUSPENDED_START)
        try:
            self.assertEqual(skytest.opt_out_of_power_throttling(int(p._handle)), "opted out")
            k32.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            state = (wintypes.ULONG * 3)(1, 0, 0)
            self.assertTrue(k32.GetProcessInformation(int(p._handle), 4, ctypes.byref(state), ctypes.sizeof(state)))
            self.assertEqual(list(state), [1, 0x5, 0])
        finally:
            p.kill()
            p.wait(timeout=10)

    @unittest.skipUnless(skytest.WINDOWS, "tokens are Windows only")
    def test_the_token_check_reads_real_tokens(self) -> None:
        ctypes, wintypes, adv, k32 = skytest._win()
        self.assertIsInstance(skytest.token_restriction(), str)
        TOKEN_ALL_ACCESS = 0xF01FF
        own = wintypes.HANDLE()
        self.assertTrue(adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_ALL_ACCESS, ctypes.byref(own)))
        self.addCleanup(k32.CloseHandle, own)

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        everyone, low = ctypes.c_void_p(), ctypes.c_void_p()
        self.assertTrue(adv.ConvertStringSidToSidW("S-1-1-0", ctypes.byref(everyone)))
        self.assertTrue(adv.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(low)))  # Low Mandatory Level
        # A copy with one restricting SID: what a sandbox's CreateRestrictedToken gives.
        adv.CreateRestrictedToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.HANDLE)]
        restricted = wintypes.HANDLE()
        sid = SidAndAttributes(everyone, 0)
        self.assertTrue(adv.CreateRestrictedToken(own, 0, 0, None, 0, None, 1, ctypes.byref(sid),
                                                  ctypes.byref(restricted)))
        self.addCleanup(k32.CloseHandle, restricted)
        self.assertIn("a restricted token (IsTokenRestricted)", skytest.token_handle_restriction(restricted))
        # A copy at low integrity.
        adv.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                         ctypes.POINTER(wintypes.HANDLE)]
        adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        lowered = wintypes.HANDLE()
        self.assertTrue(adv.DuplicateTokenEx(own, TOKEN_ALL_ACCESS, None, 2, 1, ctypes.byref(lowered)))
        self.addCleanup(k32.CloseHandle, lowered)
        label = SidAndAttributes(low, 0x20)  # SE_GROUP_INTEGRITY
        self.assertTrue(adv.SetTokenInformation(lowered, skytest.TOKEN_INTEGRITY_LEVEL, ctypes.byref(label),
                                                ctypes.sizeof(label)))
        self.assertEqual(skytest.token_handle_restriction(lowered), "a low integrity level (0x1000)")

    def test_the_preflight_and_its_report_line(self) -> None:
        self.addCleanup(setattr, skytest, "token_restriction", skytest.token_restriction)
        skytest.token_restriction = lambda: ""
        self.assertEqual(skytest.token_preflight(False), {"restricted": False, "cause": ""})
        self.assertEqual(skytest.token_report_line({"restricted": False, "cause": ""}), "Token: not restricted.")
        skytest.token_restriction = lambda: "an AppContainer token"
        with self.assertRaises(SystemExit) as e:
            skytest.token_preflight(False)
        self.assertEqual(str(e.exception.code),
                         "skytest: refused to start games: this runner has an AppContainer token, as in an agent "
                         "sandbox, so its games could not use the GPU shader caches and would compile every pipeline "
                         "cold at each start (4-32 s); run skytest outside the sandbox, or pass --allow-restricted to "
                         "run anyway (timings then not comparable)")
        with contextlib.redirect_stdout(io.StringIO()):
            t = skytest.token_preflight(True)
        self.assertEqual(t, {"restricted": True, "cause": "an AppContainer token"})
        self.assertTrue(skytest.token_report_line(t).startswith("Token: RESTRICTED (an AppContainer token)"))

        def failing() -> str:
            raise AttributeError("function 'IsTokenRestricted' not found")

        skytest.token_restriction = failing
        with contextlib.redirect_stdout(io.StringIO()) as out:
            t = skytest.token_preflight(False)
        self.assertIsNone(t["restricted"])
        self.assertIn("could not check this runner's token (AttributeError", out.getvalue())
        self.assertEqual(skytest.token_report_line(t),
                         "Token: not checked (AttributeError: function 'IsTokenRestricted' not found).")


def fake_spec(name: str, games: int = 2, exclusive: bool = False, grades: dict | None = None) -> skytest.Spec:
    """A case for the scheduler's tests: `games` peers (S and clients), nothing to run."""
    names = ["H"] if games == 1 else (["S"] + skytest.CLIENTS + ["X1", "X2", "X3"])[:games]
    return skytest.Spec(name=name, path=Path(f"{name}.toml"), layout="solo" if games == 1 else f"server+{games - 1}",
                        stage="0", description="", timeout=10, peers={n: skytest.PeerSpec(name=n) for n in names},
                        measures={}, standin_seconds=1, exclusive=exclusive, grades=grades or {})


class SchedulerTest(unittest.TestCase):
    """`test --jobs N` (schedule_cases, StartPacer, free_loopback_port) on fake cases with fake
    durations: no game starts."""

    def run_fake(self, specs: list, jobs: int, max_games: int = 5, durations: dict | None = None,
                 pacer: "skytest.StartPacer | None" = None, fail: dict | None = None) -> tuple[list, list[str]]:
        lock = threading.Lock()
        live: dict[str, int] = {}
        self.spans: dict[str, tuple[float, float]] = {}
        self.peak_games = self.peak_cases = 0

        def run_one(spec: skytest.Spec) -> skytest.CaseResult:
            over = len(spec.peers) > max_games
            with lock:
                live[spec.name] = 0 if over else len(spec.peers)
                self.peak_games = max(self.peak_games, sum(live.values()))
                self.peak_cases = max(self.peak_cases, len([n for n, g in live.items() if g]))
            t0 = time.monotonic()
            try:
                if fail and spec.name in fail:
                    raise fail[spec.name]
                if not over:
                    for _ in spec.peers:
                        if pacer is not None:
                            pacer.wait_turn()
                    time.sleep((durations or {}).get(spec.name, 0.1))
            finally:
                with lock:
                    del live[spec.name]
                    self.spans[spec.name] = (t0, time.monotonic())
            return skytest.CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage,
                                      status="capacity" if over else "pass")

        lines: list[str] = []
        results = skytest.schedule_cases(specs, jobs, max_games, run_one, say=lines.append, poll=0.01)
        return results, lines

    def overlap(self, a: str, b: str) -> bool:
        (s0, e0), (s1, e1) = self.spans[a], self.spans[b]
        return s0 < e1 and s1 < e0

    def test_one_job_runs_the_cases_one_after_another_as_before(self) -> None:
        specs = [fake_spec("a"), fake_spec("b", 1), fake_spec("c")]
        results, lines = self.run_fake(specs, jobs=1)
        self.assertEqual([r.case for r in results], ["a", "b", "c"])
        self.assertEqual(lines, ["a (server+1) ...", "  PASS", "b (solo) ...", "  PASS", "c (server+1) ...", "  PASS"])
        self.assertFalse(any(self.overlap(x, y) for x, y in (("a", "b"), ("b", "c"), ("a", "c"))))
        self.assertTrue(all(r.ran_with == [] for r in results))
        self.assertEqual(self.peak_cases, 1)

    def test_two_jobs_run_two_cases_at_once_and_keep_the_order(self) -> None:
        specs = [fake_spec(n) for n in "abcd"]
        t0 = time.monotonic()
        results, lines = self.run_fake(specs, jobs=2, durations={"a": 0.6, "b": 0.1, "c": 0.1, "d": 0.1})
        self.assertLess(time.monotonic() - t0, 0.6 + 0.1 + 0.1 + 0.1)
        self.assertEqual([r.case for r in results], ["a", "b", "c", "d"], "results in the specs' order")
        self.assertEqual(self.peak_cases, 2)
        by = {r.case: r for r in results}
        self.assertEqual(by["a"].ran_with, ["b", "c", "d"], "the long case ran beside the three short ones")
        self.assertEqual(by["b"].ran_with[0], "a")
        for r in results:
            for other in r.ran_with:
                self.assertIn(r.case, by[other].ran_with, "ran_with is symmetric")
        self.assertIn("b (server+1) ... started beside a, 4 of 5 games", lines)
        self.assertIn("a: PASS", lines)

    def test_the_games_of_the_running_cases_stay_within_max_games(self) -> None:
        specs = [fake_spec(n) for n in "abcd"] + [fake_spec("five", 5), fake_spec("e")]
        results, lines = self.run_fake(specs, jobs=4, max_games=5)
        self.assertLessEqual(self.peak_games, 5)
        self.assertEqual(self.peak_cases, 2, "two cases of two games fit in five, a third does not")
        self.assertTrue(any(line.startswith("c: waits for games: needs 2, 1 of 5 free") for line in lines), lines)
        five = next(r for r in results if r.case == "five")
        self.assertEqual(five.ran_with, [], "a case of five games in five runs alone")
        self.assertFalse(any(self.overlap("five", n) for n in "abcde"))

    def test_an_exclusive_case_waits_and_runs_alone(self) -> None:
        specs = [fake_spec("a"), fake_spec("b"), fake_spec("x", exclusive=True), fake_spec("c"), fake_spec("d")]
        results, lines = self.run_fake(specs, jobs=3, max_games=20, durations={"a": 0.3, "b": 0.2, "x": 0.2})
        for n in "abcd":
            self.assertFalse(self.overlap("x", n), f"x ran beside {n}")
        x = next(r for r in results if r.case == "x")
        self.assertEqual(x.ran_with, [])
        self.assertTrue(x.exclusive)
        self.assertTrue(self.overlap("a", "b"))
        self.assertTrue(self.overlap("c", "d"), "after the exclusive case the next ones run at once again")
        self.assertGreaterEqual(self.spans["c"][0], self.spans["x"][1], "no case jumps ahead of a waiting one")
        self.assertIn("x: waits for a, b to end (exclusive: it runs alone)", lines)
        self.assertIn("c: waits: x runs alone (exclusive)", lines)
        self.assertIn("x (server+1) ... started alone (exclusive), 2 of 20 games", lines)

    def test_a_timed_case_runs_alone_without_the_mark(self) -> None:
        # A spec that skipped lint: a time or load grade and no `exclusive = true` still runs alone.
        specs = [fake_spec("a"), fake_spec("t", grades={"client_fps": {"peer": "C1"}}), fake_spec("b")]
        results, lines = self.run_fake(specs, jobs=3, max_games=20, durations={"a": 0.2, "t": 0.2})
        by = {r.case: r for r in results}
        self.assertFalse(self.overlap("t", "a"))
        self.assertFalse(self.overlap("t", "b"))
        self.assertEqual(by["t"].ran_with, [])
        self.assertTrue(by["t"].exclusive)
        self.assertFalse(by["a"].exclusive)
        self.assertIn("t: waits for a to end (exclusive: it runs alone)", lines)
        self.assertIn("t (server+1) ... started alone (exclusive), 2 of 20 games", lines)

    def test_a_failing_print_never_hangs_the_run(self) -> None:
        # The result line's print raises (a cp1252 console, a closed pipe): the case still leaves the
        # running set and the run ends.
        def say(text: str) -> None:
            if text.endswith(": PASS"):
                raise UnicodeEncodeError("charmap", text, 0, 1, "fake")

        def run_one(spec: skytest.Spec) -> skytest.CaseResult:
            time.sleep(0.05)
            return skytest.CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, status="pass")

        done: list = []
        hook = threading.excepthook
        threading.excepthook = lambda args: None  # the worker thread's print error, expected here
        try:
            t = threading.Thread(target=lambda: done.append(
                skytest.schedule_cases([fake_spec(n) for n in "abc"], 2, 5, run_one, say=say, poll=0.01)),
                daemon=True)
            t.start()
            t.join(timeout=5)
        finally:
            threading.excepthook = hook
        self.assertFalse(t.is_alive(), "the runner hung on a failing print")
        self.assertEqual([r.case for r in done[0]], ["a", "b", "c"])

    def test_a_case_over_max_games_does_not_wait(self) -> None:
        specs = [fake_spec("a"), fake_spec("big", 5), fake_spec("b")]
        results, _ = self.run_fake(specs, jobs=2, max_games=4, durations={"a": 0.3})
        by = {r.case: r for r in results}
        self.assertEqual(by["big"].status, "capacity")
        self.assertLess(self.spans["big"][0], self.spans["a"][1], "the capacity case did not wait for a")
        self.assertEqual(by["big"].ran_with, [])
        self.assertNotIn("big", by["a"].ran_with, "a capacity case ran no game beside anything")

    def test_a_runner_error_is_that_cases_error_and_the_others_run(self) -> None:
        specs = [fake_spec("a"), fake_spec("b"), fake_spec("c")]
        results, _ = self.run_fake(specs, jobs=2, fail={"b": ValueError("boom")})
        by = {r.case: r for r in results}
        self.assertEqual(by["b"].status, "error")
        self.assertEqual(by["b"].reasons, ["runner error: ValueError: boom"])
        self.assertEqual((by["a"].status, by["c"].status), ("pass", "pass"))

    def test_a_system_exit_starts_nothing_more_and_waits_for_the_running(self) -> None:
        specs = [fake_spec("a"), fake_spec("b"), fake_spec("c")]
        with self.assertRaises(SystemExit):
            self.run_fake(specs, jobs=2, durations={"a": 0.3}, fail={"b": SystemExit("refusing to write")})
        self.assertIn("a", self.spans, "the running case ended before the exit was raised")
        self.assertNotIn("c", self.spans, "nothing started after it")

    def test_the_start_gap_holds_across_the_cases_running_at_once(self) -> None:
        pacer = skytest.StartPacer(0.15)
        specs = [fake_spec(n) for n in "abc"]
        self.run_fake(specs, jobs=3, max_games=10, pacer=pacer, durations={n: 0.05 for n in "abc"})
        self.assertEqual(self.peak_cases, 3)
        self.assertEqual(len(pacer.starts), 6)
        gaps = [b - a for a, b in zip(pacer.starts, pacer.starts[1:])]
        self.assertGreaterEqual(min(gaps), 0.15 - 0.005, gaps)

    def test_a_pacer_without_a_gap_never_waits(self) -> None:
        pacer = skytest.StartPacer(0)
        t0 = time.monotonic()
        for _ in range(5):
            pacer.wait_turn()
        self.assertLess(time.monotonic() - t0, 0.1)

    def test_hosted_ports_are_never_handed_out_twice(self) -> None:
        got: list[int] = []
        lock = threading.Lock()

        def take() -> None:
            for _ in range(5):
                p = skytest.free_loopback_port()
                with lock:
                    got.append(p)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 40)
        self.assertEqual(len(set(got)), 40)

    def test_a_port_windows_returns_again_is_skipped(self) -> None:
        handed = [50001, 50001, 50002]

        class FakeSocket:
            def __init__(self, *a) -> None:
                self.port = handed.pop(0) if handed else 50001

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                pass

            def bind(self, addr) -> None:
                pass

            def getsockname(self):
                return ("127.0.0.1", self.port)

        real, given = skytest.socket.socket, set(skytest._PORTS_GIVEN)
        skytest._PORTS_GIVEN.discard(50001)
        skytest._PORTS_GIVEN.discard(50002)
        skytest.socket.socket = FakeSocket
        try:
            self.assertEqual(skytest.free_loopback_port(), 50001)
            self.assertEqual(skytest.free_loopback_port(), 50002)
            with self.assertRaises(OSError):
                skytest.free_loopback_port()  # only 50001 from now on
        finally:
            skytest.socket.socket = real
            skytest._PORTS_GIVEN.clear()
            skytest._PORTS_GIVEN.update(given)

    # --- PS118: exclusive first, then longest first ---------------------------------------------------

    def test_the_rank_starts_the_exclusive_case_first_then_the_longest(self) -> None:
        seconds = {"s": 10.0, "l": 900.0, "x": 60.0, "m": 300.0, "y": 30.0}
        specs = [fake_spec("s"), fake_spec("l"), fake_spec("x", exclusive=True), fake_spec("m"),
                 fake_spec("y", grades={"level_load": {}})]  # y: a timed grade runs alone like an exclusive
        lock = threading.Lock()
        started: list[str] = []

        def run_one(spec):
            with lock:
                started.append(spec.name)
            time.sleep(0.05)
            return skytest.CaseResult(case=spec.name, layout=spec.layout, stage="0", status="pass")

        rank = lambda sp: skytest.order_key(skytest.runs_alone(sp), seconds[sp.name])  # noqa: E731
        lines: list[str] = []
        results = skytest.schedule_cases(specs, 2, 20, run_one, say=lines.append, poll=0.01, rank=rank)
        self.assertEqual(started, ["x", "y", "l", "m", "s"])  # alone ones first (longer first), then the longest
        self.assertEqual([r.case for r in results], ["s", "l", "x", "m", "y"], "results in the specs' order")
        by = {r.case: r for r in results}
        self.assertEqual(by["x"].ran_with, [])
        self.assertEqual(by["y"].ran_with, [])
        self.assertIn("l", by["m"].ran_with)  # the rest still packs two at once
        # Without a rank the given order stands (one job, and --jobs runs that pass none).
        started.clear()
        skytest.schedule_cases(specs, 2, 20, run_one, say=lines.append, poll=0.01)
        self.assertEqual(started[0], "s")

    def test_the_rank_orders_each_queue_take_among_itself_and_never_lets_a_case_overtake(self) -> None:
        seconds = {"a": 500.0, "p": 10.0, "q": 900.0, "big": 100.0, "t": 5.0}
        given = [False]

        def more() -> list:
            if given[0]:
                return []
            given[0] = True
            return [fake_spec("p"), fake_spec("q")]

        lock = threading.Lock()
        started: list[str] = []

        def run_one(spec):
            with lock:
                started.append(spec.name)
            time.sleep(0.05)
            return skytest.CaseResult(case=spec.name, layout=spec.layout, stage="0", status="pass")

        rank = lambda sp: skytest.order_key(skytest.runs_alone(sp), seconds[sp.name])  # noqa: E731
        skytest.schedule_cases([fake_spec("a")], 2, 20, run_one, say=[].append, poll=0.01, more=more, rank=rank)
        self.assertEqual(started, ["q", "p", "a"])  # the take runs next, its longest first
        # No overtaking in the order chosen: `big` (4 games) is next and waits for room; the short case
        # behind it that would fit beside the running one does not start first.
        started.clear()
        specs = [fake_spec("a", 2), fake_spec("big", 4), fake_spec("t", 1)]
        skytest.schedule_cases(specs, 3, 5, run_one, say=[].append, poll=0.01, rank=rank)
        self.assertEqual(started, ["a", "big", "t"])


class ParallelRunTest(unittest.TestCase):
    """`test --jobs N` through cmd_test with the fake game."""

    setUp = SkytestTest.setUp
    tearDown = SkytestTest.tearDown
    spec = SkytestTest.spec

    HOSTED2 = """layout = "server+2"
stage = "1A"
server = "host"
[peer.H]
script = \"\"\"
online_host 0
place 0 {figures}/Roller Brawl.dump
fps 8
quit
\"\"\"
[peer.D]
script = \"\"\"
wait 1
online_join {join}
fps 8
quit
\"\"\"
[peer.S]
expect = ["Online: starting as the server"]
"""

    def run_jobs(self, *names: str, jobs: int, max_games: int = 5) -> dict:
        a = argparse.Namespace(cases=list(names), stage=None, max_games=max_games, jobs=jobs)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        printed = out.getvalue().strip().splitlines()
        report = printed[-1].split("report: ", 1)[1]
        results = json.loads((Path(report).parent / "results.json").read_text(encoding="utf-8"))
        results["exit"], results["folder"], results["printed"] = code, Path(report).parent, printed
        return results

    def test_two_hosted_cases_run_at_once_on_their_own_ports(self) -> None:
        self.cfg.start_gap = 0.3
        pacers: list = []
        real = skytest.StartPacer

        def recording(gap: float, *rest):
            pacers.append(real(gap, *rest))
            return pacers[-1]

        self.spec("one", self.HOSTED2)
        self.spec("two", self.HOSTED2)
        skytest.StartPacer = recording
        try:
            r = self.run_jobs("one", "two", jobs=2, max_games=6)
        finally:
            skytest.StartPacer = real
        self.assertEqual(r["exit"], 0, r["printed"])
        self.assertEqual(r["jobs"], 2)
        cases = {c["case"]: c for c in r["cases"]}
        self.assertEqual([c["case"] for c in r["cases"]], ["one", "two"])
        self.assertEqual(cases["one"]["ran_with"], ["two"])
        self.assertEqual(cases["two"]["ran_with"], ["one"])
        ports = []
        for c in r["cases"]:
            self.assertEqual(c["status"], "pass", c)
            h = next(p for p in c["peers"] if p["peer"] == "H")
            port = next(x.split("=", 1)[1] for x in h["command"] if x.startswith("--online_port="))
            d_script = (Path(c["folder"]) / "D" / "script.txt").read_text(encoding="utf-8")
            self.assertIn(f"online_join 127.0.0.1:{port}", d_script)
            ports.append(port)
        self.assertNotEqual(ports[0], ports[1])
        self.assertNotEqual(cases["one"]["folder"], cases["two"]["folder"])
        self.assertEqual(len(pacers), 1, "one start gap for the whole run")
        starts = pacers[0].starts
        self.assertEqual(len(starts), 4, "H and D of each case (the hosted servers start from H)")
        self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 0.3 - 0.005)
        report = (r["folder"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("At most 6 game(s) and 2 case(s) at once (--jobs 2)", report)
        self.assertIn("| Case | Layout | Stage | Status | Time | With | Why |", report)
        self.assertRegex(report, r"\| one \| server\+2 \| 1A \| PASS \| \d+ s \| two \|")

    def test_an_exclusive_case_runs_alone_in_a_run(self) -> None:
        self.spec("lone", 'layout = "solo"\nstage = "0"\nexclusive = true\n[peer.H]\n' + SkytestTest.HUB)
        self.spec("other", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + SkytestTest.HUB)
        r = self.run_jobs("lone", "other", jobs=2)
        self.assertEqual(r["exit"], 0, r["printed"])
        for c in r["cases"]:
            self.assertEqual(c["ran_with"], [], c["case"])
        self.assertTrue(r["cases"][0]["exclusive"])
        self.assertIn("other: waits: lone runs alone (exclusive)", r["printed"])
        self.assertIn("| lone | solo | 0 | PASS |", (r["folder"] / "report.md").read_text(encoding="utf-8"))
        self.assertIn("| alone (exclusive) |", (r["folder"] / "report.md").read_text(encoding="utf-8"))

    def test_one_job_is_the_run_as_before(self) -> None:
        self.spec("solo1", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + SkytestTest.HUB)
        r = self.run_jobs("solo1", jobs=1)
        self.assertEqual(r["printed"][-4:-2], ["solo1 (solo) ...", "  PASS"])
        self.assertTrue(r["printed"][-2].startswith("  report: "), r["printed"])  # PS14: after each case
        report = (r["folder"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("At most 5 game(s) at once.\n", report)
        self.assertEqual(r["cases"][0]["ran_with"], [])
        # The case table as before --jobs: no With column.
        self.assertIn("\n| Case | Layout | Stage | Status | Time | Why |\n|---|---|---|---|---|---|\n", report)
        self.assertNotIn("With", report)
        self.assertRegex(report, r"\n\| solo1 \| solo \| 0 \| PASS \| \d+ s \|  \|\n")

    def test_jobs_below_one_is_refused(self) -> None:
        self.spec("solo1", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + SkytestTest.HUB)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()):
                skytest.cmd_test(self.cfg, argparse.Namespace(cases=["solo1"], stage=None, max_games=5, jobs=0))

    def test_the_exclusive_key_and_the_settings_cases_may_not_share(self) -> None:
        bad = {
            "excl": 'layout = "solo"\nstage = "0"\nexclusive = "yes"\n[peer.H]\nscript = "quit\\n"\n',
            "port": 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\nargs = ["--online_port=7777"]\n',
            "pipe": 'layout = "server+1"\nstage = "0"\n[peer.all]\nscript = "quit\\n"\n'
                    'args = ["--online_control_pipe=x"]\n',
        }
        for name, body in bad.items():
            with self.subTest(name):
                with self.assertRaises(skytest.SpecError):
                    skytest.load_spec(self.spec(name, body), 10)
        ok = skytest.load_spec(self.spec("fine", 'layout = "solo"\nstage = "0"\nexclusive = true\n[peer.H]\n'
                                                 'script = "quit\\n"\n'), 10)
        self.assertTrue(ok.exclusive)
        self.assertFalse(skytest.load_spec(self.spec("plain", 'layout = "solo"\nstage = "0"\n[peer.H]\n'
                                                              'script = "quit\\n"\n'), 10).exclusive)

    def test_lint_wants_a_timed_case_exclusive(self) -> None:
        body = ('layout = "server+1"\nstage = "0"\n{excl}[peer.all]\nscript = "quit\\n"\n'
                '[grade.client_fps]\npeer = "H"\n')
        self.spec("timed", body.format(excl=""))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("timed.toml: grades time or load (client_fps) but is not `exclusive = true`", out.getvalue())
        self.spec("timed", body.format(excl="exclusive = true\n"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(skytest.timed_grades(skytest.load_spec(self.repo / "tests" / "runtime" / "timed.toml", 10)),
                         ["client_fps"])

    def test_a_unit_case_runs_beside_a_game_case_without_a_game_slot(self) -> None:
        # --max-games 1 with a solo case: the unit case starts at once beside it (it holds no game and no
        # machine slot), and the game case still takes its slot, the game's snapshot and the disc check.
        build = self.repo / "out" / "build"
        (build / "bin").mkdir()
        (build / "bin" / "unit_test.py").write_bytes((HERE / "fake_unit.py").read_bytes())
        self.cfg.unit_launcher = [sys.executable, "-I"]
        self.spec("game", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + SkytestTest.HUB)
        self.spec("unit", 'layout = "unit"\nstage = "2"\n[unit]\nbinary = "bin/unit_test.py"\n'
                  'pass_line = "all backend tests passed"\nargs = ["--sleep=2"]\n', scenes=False)
        r = self.run_jobs("game", "unit", jobs=2, max_games=1)
        self.assertEqual(r["exit"], 0, r["printed"])
        self.assertTrue(r["snapshot"])
        cases = {c["case"]: c for c in r["cases"]}
        self.assertEqual(cases["game"]["status"], "pass", cases["game"])
        self.assertEqual(cases["unit"]["status"], "pass", cases["unit"])
        self.assertEqual(cases["unit"]["ran_with"], ["game"])
        self.assertTrue(cases["game"]["machine"].get("slots"), cases["game"]["machine"])
        self.assertNotIn("slots", cases["unit"]["machine"])


def waitfor_log(*waits: tuple[str, str, float]) -> str:
    """A game.log with `Input script: waitfor <condition> met|timed out after <s> s` lines."""
    return "".join(f"[2026-11-06 10:00:{i:02d}.000] [info] [core] [t1] Input script: waitfor {c} {kind} after {v} s\n"
                   for i, (c, kind, v) in enumerate(waits))


class GroupBTest(unittest.TestCase):
    """PS14 (results after every case, --queue), PS6 (the scenes key), C10 (fps windows nothing grades)
    and C6 (waitfor timeouts against their history)."""

    setUp = SkytestTest.setUp
    tearDown = SkytestTest.tearDown
    spec = SkytestTest.spec
    QUIT = '[peer.H]\nscript = "quit\\n"\n'

    def run_test(self, a: argparse.Namespace) -> tuple[int, list[str], Path]:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        printed = out.getvalue().strip().splitlines()
        return code, printed, Path(printed[-1].split("report: ", 1)[1]).parent

    def lint(self, **kw) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace(**kw))
        return code, out.getvalue()

    def command(self, case: dict, peer: str = "H") -> list[str]:
        return next(p for p in case["peers"] if p["peer"] == peer)["command"]

    # --- PS6 ---------------------------------------------------------------------------------------

    def test_the_runner_passes_the_scene_skip_itself_and_refuses_a_spec_without_a_policy(self) -> None:
        self.spec("pair", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 1\nscenes = "skip"\n' + self.QUIT)
        self.spec("watch", 'layout = "solo"\nstage = "0"\nscenes = "play"\nscenes_reason = "the conversation"\n'
                  + self.QUIT)
        self.spec("skips", 'layout = "solo"\nstage = "0"\nscenes = "skip"\n' + self.QUIT)
        self.spec("plain", 'layout = "solo"\nstage = "0"\n' + self.QUIT, scenes=False)
        code, printed, folder = self.run_test(argparse.Namespace(cases=["pair", "watch", "skips", "plain"], stage=None,
                                                             max_games=5))
        self.assertEqual(code, 1, printed)  # plain says no policy: an error, no game started
        cases = {c["case"]: c for c in json.loads((folder / "results.json").read_text(encoding="utf-8"))["cases"]}
        for peer in ("S", "H"):  # every game the runner starts
            self.assertEqual([a for a in self.command(cases["pair"], peer) if "dev_skip_scenes" in a],
                             ["--dev_skip_scenes=true"])
        self.assertEqual([a for a in self.command(cases["watch"]) if "dev_skip_scenes" in a], ["--dev_skip_scenes=false"])
        self.assertEqual([a for a in self.command(cases["skips"]) if "dev_skip_scenes" in a], ["--dev_skip_scenes=true"])
        self.assertEqual(cases["pair"]["scenes"], "skip")
        self.assertEqual(cases["plain"]["status"], "error")
        self.assertTrue(all(not p["command"] for p in cases["plain"]["peers"]))
        self.assertIn('plain.toml: no scenes key: say scenes = "skip"', "; ".join(cases["plain"]["reasons"]))
        self.assertEqual(cases["plain"]["scenes"], "none (no scenes key, no --dev_skip_scenes): refused")
        self.assertEqual(cases["watch"]["scenes"], "play (the conversation)")
        self.assertIn("Scenes: play (the conversation)", (folder / "report.md").read_text(encoding="utf-8"))
        # What ran differs, so the args hash does.
        specs = {s.name: s for s in skytest.all_specs(self.cfg)}
        self.assertNotEqual(skytest.args_sha256(self.cfg, specs["watch"]), skytest.args_sha256(self.cfg, specs["skips"]))

    def test_a_spec_that_sets_the_skip_in_its_args_keeps_it_without_the_key(self) -> None:
        self.spec("old", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\nargs = ["--dev_skip_scenes=false"]\n')
        spec = skytest.load_spec(self.repo / "tests" / "runtime" / "old.toml", 20)
        self.assertEqual(skytest.scene_args(self.cfg, spec), [])
        _, args = skytest.prepare_peer(self.cfg, spec, "H", skytest.make_run_dir(self.cfg, self.cfg.store / "runs" / "x"))
        self.assertEqual([a for a in args if "dev_skip_scenes" in a], ["--dev_skip_scenes=false"])  # never two
        self.assertEqual(skytest.scene_policy(self.cfg, spec), "the spec's args (--dev_skip_scenes), no scenes key")
        # The args hash of a spec without scene args is the one it had before the key (comparable runs).
        self.assertEqual(skytest.args_sha256(self.cfg, spec), skytest.hashlib.sha256(skytest.json.dumps(
            [self.cfg.default_args, ["H", [], ["--dev_skip_scenes=false"]]], sort_keys=True).encode()).hexdigest())

    def test_the_scenes_key_is_checked(self) -> None:
        body = 'layout = "solo"\nstage = "0"\n'
        for extra, why in (('scenes = "some"\n', r"scenes = 'some' \(skip, play or movies\)"),
                           ('scenes = "play"\n', r'scenes = "play" needs a `scenes_reason`'),
                           ('scenes = "skip"\nscenes_reason = "x"\n', r'scenes_reason without scenes = "play"'),
                           ('scenes = "play"\nscenes_reason = 3\n', "scenes_reason is a string")):
            with self.assertRaisesRegex(skytest.SpecError, why):
                skytest.load_spec(self.spec("bad", body + extra + self.QUIT), 20)
        # The key and the setting in the args do not go together.
        with self.assertRaisesRegex(skytest.SpecError, "sets --dev_skip_scenes in its args beside the spec's scenes key"):
            skytest.load_spec(self.spec("bad", body + 'scenes = "skip"\n[peer.H]\nscript = "quit\\n"\n'
                                        'args = ["--dev_skip_scenes=true"]\n'), 20)
        (self.repo / "tests" / "runtime" / "bad.toml").unlink()
        self.spec("both", body + 'scenes = "skip"\n[peer.H]\nscript = "quit\\n"\nargs = ["--dev_skip_scenes=true"]\n')
        code, text = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("both.toml: peer H sets --dev_skip_scenes in its args beside the spec's scenes key", text)

    def test_movies_needs_the_games_movies_skip(self) -> None:
        self.spec("movie", 'layout = "solo"\nstage = "0"\nscenes = "movies"\n' + self.QUIT)
        code, text = self.lint()
        self.assertEqual(code, 1)
        self.assertIn('movie.toml: scenes = "movies" needs the game\'s movies skip', text)
        code, printed, folder = self.run_test(argparse.Namespace(cases=["movie"], stage=None, max_games=5))
        case = json.loads((folder / "results.json").read_text(encoding="utf-8"))["cases"][0]
        self.assertEqual(case["status"], "error")  # refused before any game started
        self.assertTrue(all(not p["command"] for p in case["peers"]))
        self.assertIn("needs the game's movies skip", "; ".join(case["reasons"]))
        self.cfg.movies_skip = True
        code, printed, folder = self.run_test(argparse.Namespace(cases=["movie"], stage=None, max_games=5))
        case = json.loads((folder / "results.json").read_text(encoding="utf-8"))["cases"][0]
        self.assertEqual(case["status"], "pass", case["reasons"])
        self.assertIn("--dev_skip_scenes=movies", self.command(case))
        self.assertEqual(self.lint()[0], 0)

    def test_a_defaults_skip_may_not_reach_a_play_spec(self) -> None:
        self.cfg.default_args = self.cfg.default_args + ["--dev_skip_scenes=true"]
        self.spec("watch", 'layout = "solo"\nstage = "0"\nscenes = "play"\nscenes_reason = "the intro"\n' + self.QUIT)
        self.spec("skips", 'layout = "solo"\nstage = "0"\nscenes = "skip"\n' + self.QUIT)
        self.spec("plain", 'layout = "solo"\nstage = "0"\n' + self.QUIT, scenes=False)  # [defaults].args says it
        self.spec("old", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\nargs = ["--dev_skip_scenes=false"]\n')
        code, text = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("watch.toml: [defaults].args sets --dev_skip_scenes=true, which would reach this scenes = \"play\""
                      " spec", text)
        self.assertIn("old.toml: sets --dev_skip_scenes=false in its args and [defaults].args sets"
                      " --dev_skip_scenes=true", text)
        self.assertNotIn("skips.toml", text)
        self.assertNotIn("plain.toml", text)
        specs = {s.name: s for s in skytest.all_specs(self.cfg)}
        self.assertEqual(skytest.scene_args(self.cfg, specs["skips"]), [])  # [defaults].args says it already
        self.assertEqual(skytest.scene_args(self.cfg, specs["plain"]), [])

    def test_lint_refuses_a_spec_without_a_scene_policy(self) -> None:
        self.spec("plain", 'layout = "solo"\nstage = "0"\n' + self.QUIT, scenes=False)
        self.spec("old", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\nargs = ["--dev_skip_scenes=true"]\n')
        self.spec("keyed", 'layout = "solo"\nstage = "0"\nscenes = "skip"\n' + self.QUIT)
        code, text = self.lint()
        self.assertEqual(code, 1, text)
        self.assertIn('plain.toml: no scenes key: say scenes = "skip" (playtests skip their scenes) or scenes = "play"'
                      " with a scenes_reason (a case whose subject is a scene); the runner assumes neither\n", text)
        self.assertIn("note: 1 spec(s) set --dev_skip_scenes in their args instead of the scenes key (scenes = \"skip\""
                      " or \"play\" says it): old\n", text)
        self.assertNotIn("keyed", text)
        self.assertIn("1 problem(s)", text)

    # --- C10 ---------------------------------------------------------------------------------------

    def test_lint_warns_of_an_fps_window_nothing_grades(self) -> None:
        self.spec("idle", 'layout = "server+1"\nstage = "0"\n[peer.H]\nscript = "wait 1\\nfps 5\\nlog x\\nquit\\n"\n')
        self.spec("expects", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "fps 5\\nquit\\n"\n'
                  'expect = ["guest FPS"]\n')
        self.spec("graded", 'layout = "solo"\nstage = "0"\nexclusive = true\n[peer.H]\nscript = "fps 5\\nquit\\n"\n'
                  '[grade.client_fps]\npeer = "H"\n')
        # S14: a 2 s window is the liveness check PS67 keeps; nothing to shorten, no warning.
        self.spec("short", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "fps 2\\nquit\\n"\n')
        code, text = self.lint()
        self.assertEqual(code, 0, text)  # a warning, not a problem
        self.assertIn("warning: idle.toml: fps window(s) with no FPS grade or expect: H line 2 (5 s); only the"
                      " frozen-game check reads them (a window of 0 FPS fails the peer), so keep each window: shorten"
                      " it to `fps 2`, enough for that check (3 s per run less), or grade it (client_fps, or an"
                      " expect on `guest FPS`)", text)
        # The window feeds the zero-FPS grade: the text never says to delete one.
        for word in ("drop", "delete", "remove"):
            self.assertNotIn(word, text.split("idle.toml", 1)[1].splitlines()[0])
        self.assertNotIn("expects.toml", text)
        self.assertNotIn("graded.toml", text)
        self.assertNotIn("short.toml", text)
        self.assertIn("0 problem(s), 1 warning(s)", text)

    # --- C6 ----------------------------------------------------------------------------------------

    def store_log(self, rel: str, *waits: tuple[str, str, float]) -> None:
        f = self.cfg.store / "runs" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(waitfor_log(*waits), encoding="utf-8")

    def test_timeouts_reads_the_waitfor_history_of_the_store(self) -> None:
        barrier = [("online_barrier", "met", float(v)) for v in (10, 12, 14, 16, 20)]
        self.store_log("20261105-100000-a/H/game.log", *barrier[:3], ("title_ready", "met", 49.9))
        self.store_log("20261105-100000-a/H/data/Online/session/game.log", barrier[3])  # a hosted server
        self.store_log("20261105-110000-c/off1/H/game.log", barrier[4], ("online_barrier", "timed out", 300.0))
        self.store_log("20261105-100000-a/H/figures/game.log", ("online_barrier", "met", 999.0))  # never read
        self.store_log("20261104-090000-old/H/game.log", ("online_barrier", "met", 500.0))
        self.spec("waits", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "waitfor title_ready 35\\n'
                  'waitfor online_barrier 600\\nwaitfor online_barrier 70\\nquit\\n"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_timeouts(self.cfg, argparse.Namespace(since="20261105"))
        text = out.getvalue()
        self.assertEqual(code, 0, text)  # advisory
        self.assertIn("waitfor history: 3 game.log file(s)", text)
        self.assertIn("(runs from 20261105 on): 6 met, 1 timed out", text)
        self.assertIn("| online_barrier | 5 | 1 | 14 s | 20 s | 20 s | 20261105-110000-c | 70, 600 |", text)
        self.assertIn("warning: `waitfor online_barrier 600` is over max(3 x p95 20, max 20 + 60) = 80 s of 5 met"
                      " waits: a silent hang waits 600 s for its timed-out line; it fits 20-80 s; in 1 spec line(s):"
                      " waits (H line 2)", text)
        self.assertIn("warning: `waitfor title_ready 35` is below the longest met wait seen, 49.9 s"
                      " (20261105-100000-a): a slow but healthy run times out there; it fits 50 s or more; in 1 spec"
                      " line(s): waits (H line 1)", text)
        self.assertNotIn("online_barrier 70`", text)  # 70 s fits
        self.assertIn("1 spec(s), 2 warning(s)", text)
        # Without --since the old run counts (its 500 s is the max now); lint --timeouts warns the same way.
        code, text = self.lint(timeouts=True, since=None)
        self.assertEqual(code, 0, text)
        self.assertIn("warning: `waitfor online_barrier 70` is below the longest met wait seen, 500 s", text)
        self.assertNotIn("999", text)

    # --- PS14 --------------------------------------------------------------------------------------

    def test_results_are_written_after_every_case(self) -> None:
        self.spec("one", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + SkytestTest.HUB)
        self.spec("two", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + SkytestTest.HUB)
        seen: list[tuple[str, str, dict]] = []
        real = skytest.run_case

        def spy(cfg, spec, *rest, **kw):
            runs = sorted((self.cfg.store / "runs").glob("*-session"))
            if runs:
                seen.append((spec.name, (runs[-1] / "report.md").read_text(encoding="utf-8"),
                             json.loads((runs[-1] / "results.json").read_text(encoding="utf-8"))))
            return real(cfg, spec, *rest, **kw)

        skytest.run_case = spy
        try:
            code, printed, folder = self.run_test(argparse.Namespace(cases=["one", "two"], stage=None, max_games=5))
        finally:
            skytest.run_case = real
        self.assertEqual(code, 0, printed)
        self.assertEqual(printed[0], f"report (rewritten after every case): {folder / 'report.md'}")
        self.assertEqual(printed.count(f"  report: {folder / 'report.md'}"), 2)
        # When the second case started, the first one's rows were there, and said the run goes on.
        name, report, results = seen[1]
        self.assertEqual(name, "two")
        self.assertIn("**In progress: 1 of 2 case(s) ended.**", report)
        self.assertIn("| one | solo | 0 | PASS |", report)
        self.assertNotIn("| two |", report)
        self.assertNotIn("## Left alone", report)
        self.assertEqual([c["case"] for c in results["cases"]], ["one"])
        self.assertEqual(results["in_progress"], "1 of 2 case(s) ended")
        # The end: the whole report, results without in_progress, and each case's result.json in its folder.
        final = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertNotIn("in_progress", final)
        self.assertNotIn("In progress", (folder / "report.md").read_text(encoding="utf-8"))
        self.assertIn("## Left alone", (folder / "report.md").read_text(encoding="utf-8"))
        for c in final["cases"]:
            own = json.loads((Path(c["folder"]) / "result.json").read_text(encoding="utf-8"))
            self.assertEqual((own["case"], own["status"], own["session"]), (c["case"], "pass", str(folder)))
        self.assertFalse(list(folder.glob("*.tmp-*")))

    def test_a_queue_file_is_read_before_each_case_and_its_new_cases_run_next(self) -> None:
        for name in ("one", "two", "three"):
            self.spec(name, 'layout = "solo"\nstage = "0"\n' + self.QUIT)
        self.spec("later", 'layout = "solo"\nstage = "0"\nready = false\nreason = "legs 11+"\n' + self.QUIT)
        queue = Path(self.tmp.name) / "lane.txt"
        queue.write_text("# lane 1\none nope\n", encoding="utf-8")
        real = skytest.run_case
        started: list[str] = []

        def spy(cfg, spec, *rest, **kw):
            started.append(spec.name)
            if spec.name == "one":  # the tester adds cases while one runs
                with open(queue, "a", encoding="utf-8") as f:
                    f.write("two  # after triage\nlater\n")
            return real(cfg, spec, *rest, **kw)

        skytest.run_case = spy
        try:
            code, printed, folder = self.run_test(argparse.Namespace(cases=["three"], stage=None, max_games=5,
                                                                 queue=str(queue)))
        finally:
            skytest.run_case = real
        self.assertEqual(code, 1, printed)  # an entry that names no case
        self.assertEqual(started, ["three", "one", "two"])
        self.assertIn("queue: nope matches no case (skipped); `skytest.py list` names them", printed)
        self.assertIn("later: NOT READY: legs 11+ (no game started)", printed)
        final = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual([(c["case"], c["status"]) for c in final["cases"]],
                         [("three", "pass"), ("one", "pass"), ("two", "pass"), ("later", "not ready")])
        self.assertEqual(final["queue"], {"file": str(queue), "unmatched": ["nope"]})
        self.assertIsNone(final["checkpoint"])
        self.assertIn("## Queue entries that matched no case\n\nnope", (folder / "report.md").read_text(encoding="utf-8"))
        # Only the queue (no case named): not every case; a case named twice runs twice.
        queue.write_text("two\ntwo\n", encoding="utf-8")
        code, printed, folder = self.run_test(argparse.Namespace(cases=[], stage=None, max_games=5, queue=str(queue)))
        self.assertEqual(code, 0, printed)
        final = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual([c["case"] for c in final["cases"]], ["two", "two"])
        self.assertEqual(len({c["folder"] for c in final["cases"]}), 2)
        # Refused: with --gate, or a file that is not there.
        for kw, why in (({"gate": True}, "--queue and --gate"), ({}, "no such file")):
            a = argparse.Namespace(cases=[], stage=None, max_games=5, queue=str(queue) if kw else str(queue) + ".x", **kw)
            with self.assertRaisesRegex(SystemExit, why):
                skytest.cmd_test(self.cfg, a)

    def test_a_queue_takes_a_new_spec_file_and_an_edited_spec_s_new_text(self) -> None:
        # R2: the queue's entries run the spec files as they are when taken; provenance records the
        # text that ran; a case named on the command line runs the text read at the start and says so.
        self.spec("one", 'layout = "solo"\nstage = "0"\n' + self.QUIT)
        self.spec("edited", 'layout = "solo"\nstage = "0"\ntimeout = 20\n' + self.QUIT)
        queue = Path(self.tmp.name) / "lane.txt"
        queue.write_text("", encoding="utf-8")
        real = skytest.run_case
        ran: dict[str, float] = {}

        def spy(cfg, spec, *rest, **kw):
            ran[spec.name] = spec.timeout
            if spec.name == "one":
                self.spec("fresh", 'layout = "solo"\nstage = "0"\n' + self.QUIT)  # written after the start
                self.spec("edited", 'layout = "solo"\nstage = "0"\ntimeout = 30\n' + self.QUIT)
                queue.write_text("fresh\nedited\n", encoding="utf-8")
            return real(cfg, spec, *rest, **kw)

        skytest.run_case = spy
        try:
            code, printed, folder = self.run_test(argparse.Namespace(cases=["one"], stage=None, max_games=5,
                                                                 queue=str(queue)))
        finally:
            skytest.run_case = real
        self.assertEqual(code, 0, printed)
        self.assertEqual(ran, {"one": 20.0, "fresh": 20.0, "edited": 30.0})
        self.assertIn("queue: edited.toml changed since it was read; the queue's entries run its new text", printed)
        final = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        prov = {c["case"]: c["provenance"]["spec_sha256"] for c in final["cases"]}
        path = self.repo / "tests" / "runtime" / "edited.toml"
        self.assertEqual(prov["edited"], skytest.hashlib.sha256(path.read_bytes()).hexdigest())
        # A named case whose file changed after the start: the text read at the start runs, and it says so.
        self.spec("two", 'layout = "solo"\nstage = "0"\n' + self.QUIT)

        def edit_two(cfg, spec, *rest, **kw):
            if spec.name == "one":
                self.spec("two", 'layout = "solo"\nstage = "0"\ntimeout = 40\n' + self.QUIT)
            ran[spec.name] = spec.timeout
            return real(cfg, spec, *rest, **kw)

        skytest.run_case = edit_two
        try:
            code, printed, folder = self.run_test(argparse.Namespace(cases=["one", "two"], stage=None, max_games=5))
        finally:
            skytest.run_case = real
        self.assertEqual(ran["two"], 20.0)
        self.assertIn("two: two.toml changed since it was read; the case runs the text read before (provenance records"
                      " that text's sha256)", printed)

    def test_the_case_queue_takes_each_entry_once(self) -> None:
        f = Path(self.tmp.name) / "q.txt"
        f.write_text("a b # two\n\n# none\nc\n", encoding="utf-8")
        q = skytest.CaseQueue(f)
        self.assertEqual(q.take(), ["a", "b", "c"])
        self.assertEqual(q.take(), [])
        f.write_text("a b\nc\nd*\n", encoding="utf-8")
        self.assertEqual(q.take(), ["d*"])
        # An entry taken and then removed, with a new one added: the new one is taken (the count of lines
        # does not decide), and the removed one comes back only as a new occurrence.
        f.write_text("b\nc\ne\n", encoding="utf-8")
        self.assertEqual(q.take(), ["e"])
        f.write_text("x\nb\nc\ne\na\na\n", encoding="utf-8")
        self.assertEqual(q.take(), ["x", "a"])  # the second a is a second occurrence
        f.unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(q.take(), [])
            self.assertEqual(q.take(), [])
        self.assertEqual(out.getvalue().count("nothing more is taken from it"), 1)

    def test_the_scheduler_runs_new_cases_next_and_reports_each_as_it_ends(self) -> None:
        a, b, c = fake_spec("a", 1), fake_spec("b", 1), fake_spec("c", 1)
        given = [False]
        ended: list[tuple[str, list[str], bool]] = []

        def more() -> list:
            if given[0] or not ended:
                return []
            given[0] = True
            return [c]

        def run_one(spec):
            time.sleep(0.05)
            return skytest.CaseResult(case=spec.name, layout=spec.layout, stage="0", status="pass")

        specs = [a, b]
        lines: list[str] = []
        results = skytest.schedule_cases(specs, 1, 5, run_one, say=lines.append, poll=0.01,
                                         done=lambda sp, r: ended.append((sp.name, list(r.ran_with), r.exclusive)),
                                         more=more)
        self.assertEqual([r.case for r in results], ["a", "b", "c"])  # the list's order (c was added)
        self.assertEqual([n for n, _, _ in ended], ["a", "c", "b"])  # c ran next
        self.assertEqual(specs, [a, b, c])
        # Two jobs: ran_with is known when a case ends.
        ended.clear()
        skytest.schedule_cases([fake_spec("x", 1), fake_spec("y", 1)], 2, 5, run_one, say=lines.append, poll=0.01,
                               done=lambda sp, r: ended.append((sp.name, list(r.ran_with), r.exclusive)))
        self.assertEqual(sorted((n, w) for n, w, _ in ended), [("x", ["y"]), ("y", ["x"])])
        # A done() that fails leaves the case's result alone.
        out = skytest.schedule_cases([fake_spec("z", 1)], 1, 5, run_one, say=lines.append, poll=0.01,
                                     done=lambda sp, r: 1 / 0)
        self.assertEqual(out[0].status, "pass")
        self.assertTrue(any("its results were not written now (ZeroDivisionError" in s for s in lines), lines)

class ReviewFixesTest(unittest.TestCase):
    """The review of groups A, B and C: the scheduler's wait, a report a reader holds, tolerate patterns
    that take nothing out, a timed case beside games of another runner."""

    setUp = SkytestTest.setUp
    tearDown = SkytestTest.tearDown
    spec = SkytestTest.spec

    def test_a_case_that_only_waited_for_the_machine_ran_beside_nothing(self) -> None:
        a, b = fake_spec("a", 1), fake_spec("b", 1)
        gate = threading.Event()

        def run_one(spec):
            skytest.case_waits()
            if spec.name == "b":
                gate.wait(10)  # b waits for a's slots while a runs
            skytest.case_begins()
            if spec.name == "a":
                time.sleep(0.3)
                gate.set()
            else:
                time.sleep(0.05)
            return skytest.CaseResult(case=spec.name, layout=spec.layout, stage="0", status="pass")

        out = skytest.schedule_cases([a, b], 2, 5, run_one, say=lambda s: None, poll=0.01)
        self.assertEqual([(r.case, r.ran_with) for r in out], [("a", []), ("b", [])])
        # A case that ends while it waits (a capacity or breaker result) overlaps nothing.
        def never_begins(spec):
            skytest.case_waits()
            time.sleep(0.2 if spec.name == "c" else 0.05)
            if spec.name == "c":
                return skytest.CaseResult(case=spec.name, layout=spec.layout, stage="0", status="breaker")
            skytest.case_begins()
            time.sleep(0.3)
            return skytest.CaseResult(case=spec.name, layout=spec.layout, stage="0", status="pass")

        out = skytest.schedule_cases([fake_spec("c", 1), fake_spec("d", 1)], 2, 5, never_begins, say=lambda s: None,
                                     poll=0.01)
        self.assertEqual([(r.case, r.ran_with) for r in out], [("c", []), ("d", [])])

    def test_a_report_a_reader_holds_is_not_written_in_place(self) -> None:
        target = Path(self.tmp.name) / "report.md"
        target.write_text("old, whole\n", encoding="utf-8")
        real = skytest.os.replace

        def held(src, dst):
            raise PermissionError(13, "in use", str(dst))

        skytest.os.replace = held
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertFalse(skytest.write_text_replacing(target, "new\n", retry_s=0.2))
        finally:
            skytest.os.replace = real
        self.assertEqual(target.read_text(encoding="utf-8"), "old, whole\n")
        left = list(target.parent.glob("report.md.tmp-*"))
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0].read_text(encoding="utf-8"), "new\n")
        self.assertIn("report.md: held open by a reader, not replaced; the previous file stays whole", out.getvalue())
        # The next write replaces the file and takes the text left over with it.
        self.assertTrue(skytest.write_text_replacing(target, "newer\n"))
        self.assertEqual(target.read_text(encoding="utf-8"), "newer\n")
        self.assertEqual(list(target.parent.glob("report.md.tmp-*")), [])

    def test_lint_warns_of_a_tolerate_pattern_that_takes_nothing_out(self) -> None:
        self.spec("stall", 'layout = "solo"\nstage = "0"\ntolerate = ["Online: disconnected \\\\(timeout\\\\)",'
                  ' "menu_ready"]\n[peer.H]\nscript = "quit\\n"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("warning: stall.toml: tolerate pattern(s) that match no dead session's default fatal line:"
                      " 'Online: disconnected \\\\(timeout\\\\)'; they take nothing out", text)
        self.assertNotIn("'menu_ready'", text)

    def test_a_timed_case_beside_games_of_another_runner_is_marked(self) -> None:
        timed = fake_spec("t", 2, grades={"client_fps": {}})
        self.assertIn("2 game(s) not started by this runner ran beside this exclusive or timed case",
                      skytest.not_alone_note(timed, {"outside_games_max": 2}))
        self.assertEqual(skytest.not_alone_note(timed, {"outside_games_max": 0}), "")
        self.assertEqual(skytest.not_alone_note(fake_spec("plain", 2), {"outside_games_max": 2}), "")
        line = skytest.machine_line({"slots": 4, "of": 4, "all": True, "waited_s": 0.0, "boot_wait_s": 7.5,
                                     "not_alone": "x"})
        self.assertEqual(line, "Machine: 4 of 4 game slots (all: nothing of another runner beside it); its games waited"
                               " 8 s in all for the machine's boot turn (in its seconds); NOT ALONE: x.")


class FakeProc:
    """A process for the boot token's watch: alive until ended."""

    def __init__(self) -> None:
        self.code = None

    def poll(self):
        return self.code


class ThreadOut(io.TextIOBase):
    """sys.stdout for runners in threads: each thread's prints go to its own buffer."""

    def __init__(self) -> None:
        self.buffers: dict[int, io.StringIO] = {}
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            return self.buffers.setdefault(threading.get_ident(), io.StringIO()).write(text)

    def flush(self) -> None:
        pass


CRASH_A = ("[2026-11-06 03:16:20.031] [error] [core] [t39816] Crash: exception C0000005, read of guest 00000024 on"
           " thread 39816; call chain: sub_8298D2E8+0xF1 <- sub_829D5478+0x19E <- sub_829D97C0+0xF1")


class MachineTest(unittest.TestCase):
    """PS15 (the machine's game budget, the boot token) and C7 (the circuit breaker)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.slots = Path(self.tmp.name) / skytest.STORE / "slots"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def budget(self, games: int = 4, owner: str = "") -> "skytest.MachineBudget":
        return skytest.MachineBudget(self.slots, games, owner)

    def acquire_in_thread(self, budget, need: int, case: str, said: list, memory_games: int = 0) -> tuple:
        box: dict = {}

        def go() -> None:
            box["lease"] = budget.acquire(need, case, memory_games=memory_games, say=said.append, poll=0.02)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t, box

    def wait_until(self, cond, seconds: float = 10.0) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.02)
        self.fail("condition not met in time")

    def test_two_runners_share_one_budget_and_the_waiter_says_for_what(self) -> None:
        a, b = self.budget(owner="runner A"), self.budget(owner="runner B")
        held = a.acquire(3, "big")
        self.assertEqual(len(held.slots), 3)
        said: list[str] = []
        t, box = self.acquire_in_thread(b, 2, "small", said)
        self.wait_until(lambda: said)
        self.assertNotIn("lease", box)
        self.assertEqual(said[0], "machine slots: needs 2 of 4, 1 free; held by big (runner A, 3)")
        held.release()
        t.join(timeout=10)
        lease = box["lease"]
        self.assertEqual(len(lease.slots), 2)
        self.assertGreater(lease.waited_s, 0.0)
        self.assertEqual(lease.summary()["waited_for"], said)
        self.assertFalse(lease.summary()["all"])
        lease.release()
        # Every slot is free again: a whole-machine case takes them all at once.
        whole = a.acquire(4, "timed")
        self.assertEqual(whole.summary()["all"], True)
        whole.release()

    @unittest.skipUnless(os.name == "nt", "the slot locks are Windows byte-range locks")
    def test_a_dead_runner_process_frees_its_slots(self) -> None:
        code = ("import sys, time; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import skytest; "
                "skytest.GB_PER_GAME = 0.0; b = skytest.MachineBudget(Path(sys.argv[2]), 2, 'child runner'); b.acquire(2, 'child-case'); "
                "print('held', flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-I", "-c", code, str(HERE.parent), str(self.slots)],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            said: list[str] = []
            t, box = self.acquire_in_thread(self.budget(2, "parent"), 1, "mine", said)
            self.wait_until(lambda: said)
            self.assertIn("held by child-case (child runner, 2)", said[0])
            self.assertNotIn("lease", box)
            child.kill()  # no release: its handles go with it
            child.wait(timeout=10)
            t.join(timeout=10)
            self.assertEqual(len(box["lease"].slots), 1)
            box["lease"].release()
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()

    def test_a_case_that_needs_every_slot_is_not_starved_by_smaller_ones(self) -> None:
        a = self.budget(owner="lane 1")
        first = a.acquire(1, "solo-a")
        said_x: list[str] = []
        tx, bx = self.acquire_in_thread(self.budget(owner="lane 2"), 4, "timed-x", said_x)
        self.wait_until(lambda: said_x)
        self.assertIn("needs every slot, 3 free", said_x[0])
        said_c: list[str] = []
        tc, bc = self.acquire_in_thread(self.budget(owner="lane 3"), 1, "solo-c", said_c)
        self.wait_until(lambda: said_c)
        self.assertEqual(said_c[0], "machine slots: timed-x (lane 2) waits first for 4")
        self.assertNotIn("lease", bc)  # three slots are free, but timed-x is first
        first.release()
        tx.join(timeout=10)
        self.assertEqual(len(bx["lease"].slots), 4)
        time.sleep(0.2)
        self.assertNotIn("lease", bc)  # nothing beside the timed case
        bx["lease"].release()
        tc.join(timeout=10)
        self.assertEqual(len(bc["lease"].slots), 1)
        bc["lease"].release()

    def test_memory_is_checked_at_each_case_start(self) -> None:
        real_mem, real_gb, real_recheck = skytest.available_memory_gb, skytest.GB_PER_GAME, skytest.MEMORY_RECHECK_S
        skytest.GB_PER_GAME = 1.5
        skytest.MEMORY_RECHECK_S = 0.3
        free = [2.0]
        skytest.available_memory_gb = lambda: free[0]
        try:
            other = self.budget(owner="lane 1").acquire(1, "running")
            said: list[str] = []
            t, box = self.acquire_in_thread(self.budget(owner="lane 2"), 2, "pair", said)
            self.wait_until(lambda: said)
            self.assertEqual(said[0], "memory: needs 3.0 GB free for 2 game(s), 2.0 GB free; slots held by running"
                                      " (lane 1, 1)")
            free[0] = 4.0  # the other lane's games ended or shrank
            t.join(timeout=10)
            self.assertEqual(len(box["lease"].slots), 2)
            box["lease"].release()
            other.release()
            # Short with nothing else running: a capacity result after the re-check, not a wait for ever.
            free[0] = 1.0
            t0 = time.monotonic()
            lease = self.budget().acquire(1, "lonely")
            self.assertGreaterEqual(time.monotonic() - t0, 0.25)
            self.assertEqual(lease.slots, [])
            self.assertIn("needs 1.5 GB of free memory for 1 game(s), 1.0 GB free with no other runner's game", lease.capacity)
            # Memory that comes back within the re-check (the last case's games just exited): no capacity.
            samples = iter([1.0, 1.0, 3.0])
            skytest.available_memory_gb = lambda: next(samples, 3.0)
            lease = self.budget().acquire(1, "after")
            self.assertEqual(len(lease.slots), 1)
            self.assertEqual(lease.capacity, "")
            lease.release()
            skytest.available_memory_gb = lambda: free[0]
            # An exclusive case takes every slot but needs memory only for its own games.
            free[0] = 3.5
            lease = self.budget().acquire(4, "timed", memory_games=2)
            self.assertEqual(len(lease.slots), 4)
            lease.release()
        finally:
            skytest.available_memory_gb, skytest.GB_PER_GAME, skytest.MEMORY_RECHECK_S = real_mem, real_gb, real_recheck

    def test_the_need_of_a_case(self) -> None:
        self.assertEqual(skytest.machine_need(fake_spec("pair", 2), 4), 2)
        self.assertEqual(skytest.machine_need(fake_spec("lone", 2, exclusive=True), 4), 4)
        self.assertEqual(skytest.machine_need(fake_spec("timed", 2, grades={"client_fps": {}}), 4), 4)
        self.assertEqual(skytest.machine_need(fake_spec("five", 5), 4), 4)  # over the budget: alone

    def test_one_game_boots_at_a_time_on_the_machine(self) -> None:
        log = Path(self.tmp.name) / "game.log"
        log.write_text("[2026-11-06 10:00:00.000] [info] [core] [t1] Skylanders\n", encoding="utf-8")
        a, b = self.budget(owner="lane 1"), self.budget(owner="lane 2")
        hold = a.boot_turn(0.0)
        proc = FakeProc()
        hold.watch(log, proc)
        box: dict = {}
        t = threading.Thread(target=lambda: box.update(hold=b.boot_turn(0.0)), daemon=True)
        t.start()
        time.sleep(0.4)
        self.assertNotIn("hold", box)  # the first game has not shown a frame yet
        with open(log, "a", encoding="utf-8") as f:
            f.write("[2026-11-06 10:00:01.000] [info] [core] [t1] Swap: game presentation interval 2 (swap word"
                    " 00000200)\n")
        t.join(timeout=5)
        self.assertIn("hold", box)
        self.assertRegex(hold.outcome, r"^first frame after \d+\.\d s$")
        self.assertGreater(box["hold"].waited_s, 0.3)
        # A game that never logs its first frame holds the token only until the timeout; one that exits
        # frees it at once.
        box["hold"].watch(log.with_name("none.log"), FakeProc(), timeout=1.0)
        third = a.boot_turn(0.0)
        self.assertEqual(box["hold"].outcome, "no first frame line within 1 s")
        gone = FakeProc()
        gone.code = 0
        third.watch(log.with_name("none.log"), gone)
        fourth = b.boot_turn(0.0)
        self.assertRegex(third.outcome, r"^exited after")
        fourth.release()
        fourth.release()  # twice is harmless

    def test_the_start_gap_holds_across_runners(self) -> None:
        a, b = self.budget(owner="lane 1"), self.budget(owner="lane 2")
        a.boot_turn(0.5).release()
        t0 = time.monotonic()
        b.boot_turn(0.5).release()
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)

    def test_a_worktree_shares_the_main_checkouts_slots(self) -> None:
        main = Path(self.tmp.name) / "main"
        main.mkdir()
        subprocess.run(["git", "init", "-q", str(main)], check=True)
        subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit",
                        "-q", "--allow-empty", "-m", "init"], check=True)
        wt = Path(self.tmp.name) / "wt"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
        self.assertEqual(skytest.main_store(wt), (main / skytest.STORE).resolve())
        self.assertEqual(skytest.main_store(main), (main / skytest.STORE).resolve())

    # --- the circuit breaker (C7) ---

    def test_a_signature_drops_times_threads_and_heap_addresses_and_keeps_the_crash_site(self) -> None:
        a = skytest.normalize_fatal(CRASH_A)
        self.assertEqual(a, "Crash: exception C0000005, read of guest 00000024 on thread <n>; call chain:"
                            " sub_8298D2E8+0xF1 <- sub_829D5478+0x19E <- sub_829D97C0+0xF1")
        b = CRASH_A.replace("03:16:20.031", "09:01:02.999").replace("t39816", "t4764").replace("39816;", "4764;")
        self.assertEqual(skytest.normalize_fatal(b), a)
        heap1 = CRASH_A.replace("00000024", "B4737AB0")
        heap2 = CRASH_A.replace("00000024", "9C112340")
        self.assertEqual(skytest.normalize_fatal(heap1), skytest.normalize_fatal(heap2))
        self.assertIn("read of guest <addr>", skytest.normalize_fatal(heap1))
        self.assertNotEqual(skytest.normalize_fatal(heap1), a)  # a null offset is not a heap address
        other_site = CRASH_A.replace("sub_829D5478+0x19E", "sub_829D5478+0x1A2")
        self.assertNotEqual(skytest.normalize_fatal(other_site), a)
        host = CRASH_A.replace("sub_829D97C0+0xF1", "0x00007FF6A1B2C3D4")
        self.assertTrue(skytest.normalize_fatal(host).endswith("<- <addr>"))
        self.assertEqual(skytest.normalize_fatal("[2026-11-06 10:00:00.000] [error] [core] [t1] Online: server: error:"
                                                 " bind 127.0.0.1:53412 failed (pid 4410)"),
                         "Online: server: error: bind 127.0.0.1:<port> failed (pid <n>)")
        long_chain = CRASH_A + "".join(f" <- sub_8200{i:04X}+0x1" for i in range(20))
        self.assertEqual(skytest.normalize_fatal(long_chain).count(" <- "), skytest.BREAKER_FRAMES - 1)

    def test_the_case_signature_is_its_crash_else_its_first_specific_fatal_line(self) -> None:
        sp = fake_spec("c", 2)
        timed_out = "[2026-11-06 10:00:01.000] [info] [core] [t1] Input script: waitfor online_barrier timed out after 600 s"
        ended = "[2026-11-06 10:00:02.000] [info] [core] [t1] Online: the server ended by itself"
        crash = CRASH_A.replace("2026-11-06 03:16:20.031", "2026-11-06 10:00:03.000")
        sig = skytest.case_signature(sp, {"S": crash + "\n", "H": timed_out + "\n" + ended + "\n"})
        self.assertEqual((sig["peer"], sig["kind"]), ("S", "crash"))
        self.assertTrue(sig["signature"].startswith("Crash: exception C0000005"))
        # A symptom that many causes produce (a timed-out wait, a dead session's default line, the
        # missing hub, a script that cannot be parsed) is no signature, also when the spec forbids it.
        weak = [timed_out, ended,
                "[2026-11-06 10:00:02.500] [error] [core] [t1] Online: server: error: bind failed",
                "[2026-11-06 10:00:02.600] [info] [core] [t1] Dev: navto: no player frame within 5 s",
                "[2026-11-06 10:00:02.700] [info] [core] [t1] refused: dev_navto is off",
                "[2026-11-06 10:00:02.800] [info] [core] [t1] Online: the server did not reach its hub",
                "[2026-11-06 10:00:02.900] [info] [core] [t1] Input script: cannot parse line 3"]
        forbids = replace(sp, peers={**sp.peers, "H": replace(sp.peers["H"], forbid=["ended by itself", "server: error"])})
        self.assertEqual(skytest.case_signature(forbids, {"S": "", "H": "\n".join(weak) + "\n"}), {})
        # A [FATAL] line and a spec's own forbid line that is not weak are signatures, the earliest first.
        own = replace(sp, peers={**sp.peers, "H": replace(sp.peers["H"], forbid=["(?i)DESYNC at"])})
        fatal = "[2026-11-06 10:00:04.000] [critical] [core] [t1] [FATAL] out of guest memory"
        desync = "[2026-11-06 10:00:03.500] [info] [core] [t1] Online: desync at tick 812"
        sig = skytest.case_signature(own, {"S": fatal + "\n", "H": timed_out + "\n" + desync + "\n"})
        self.assertEqual((sig["peer"], sig["kind"], sig["signature"]), ("H", "line", "Online: desync at tick 812"))
        sig = skytest.case_signature(own, {"S": fatal + "\n", "H": ""})
        self.assertEqual((sig["peer"], sig["signature"]), ("S", "out of guest memory"))  # the prefix goes

    def test_a_tolerate_pattern_with_a_global_flag_is_refused_at_load(self) -> None:
        # default_fatal puts the tolerate patterns in a lookahead: a global (?i) there would break every grade.
        d = Path(self.tmp.name)
        path = d / "t.toml"
        path.write_text('name = "t"\nlayout = "solo"\nstage = "0"\ntolerate = ["(?i)menu_ready"]\n'
                        '[peer.H]\nscript = "quit\\n"\n', encoding="utf-8")
        with self.assertRaisesRegex(skytest.SpecError, r"tolerate: .*global flag.*\(\?i:\.\.\.\)"):
            skytest.load_spec(path, 20)
        path.write_text(path.read_text(encoding="utf-8").replace("(?i)menu_ready", "(?i:MENU)_ready"), encoding="utf-8")
        sp = skytest.load_spec(path, 20)
        self.assertEqual(sp.tolerate, ["(?i:MENU)_ready"])
        for rx, _ in skytest.default_fatal(sp, "H"):
            re.compile(rx)

    def test_the_default_fatal_examples_match_their_patterns(self) -> None:
        for who, rx in skytest.DEFAULT_FATAL:
            self.assertTrue(any(re.search(rx, ex) for ex in skytest.DEFAULT_FATAL_EXAMPLES), rx)

    def test_the_breaker_trips_at_three_distinct_cases_of_one_build(self) -> None:
        br = skytest.CircuitBreaker(self.budget(), "abc123")
        sig = {"signature": "Crash: x", "peer": "S", "line": "...", "kind": "crash"}
        br.record("one", "r1", sig)
        br.record("one", "r2", sig)  # the same case twice is one case
        br.record("two", "r3", sig)
        br.record("three", "r4", {**sig, "signature": "Crash: y"})
        self.assertIsNone(br.tripped())
        other_build = skytest.CircuitBreaker(self.budget(), "def456")
        other_build.record("three", "r5", sig)
        self.assertIsNone(br.tripped())
        br.record("three", "r6", sig)
        t = br.tripped()
        self.assertEqual((t["signature"], t["cases"], t["peers"]), ("Crash: x", ["one", "two", "three"], ["S"]))
        self.assertIsNone(other_build.tripped())
        self.assertIn("circuit breaker: 3 cases on build abc123 stopped on `Crash: x` (one, two, three)",
                      skytest.breaker_line(t))
        self.assertIn(f"`skytest.py breaker --reset` clears it ({br.path}; rows older than 12 h do not count)",
                      skytest.breaker_line(t))
        # Rows older than the window do not count: 13 h later the breaker is clear again.
        self.assertIsNone(br.tripped(now=time.time() + 13 * 3600))
        # A row with no readable time does not count either; an `at` stamp alone reads.
        with open(br.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"case": "four", "signature": "Crash: z", "at": "garbage"}) + "\n")
            f.write(json.dumps({"case": "five", "signature": "Crash: z", "at": time.strftime("%Y%m%d-%H%M%S")}) + "\n")
        self.assertEqual(skytest.breaker_row_time({"at": "garbage"}), None)
        self.assertIsNotNone(skytest.breaker_row_time({"at": time.strftime("%Y%m%d-%H%M%S")}))

    def test_the_breaker_command_shows_and_resets_the_files(self) -> None:
        repo = Path(self.tmp.name)
        cfg = argparse.Namespace(slots=self.slots, machine_games=4, rom=repo / "rom")
        br = skytest.CircuitBreaker(self.budget(), "abc123")
        for n in ("one", "two", "three"):
            br.record(n, "r", {"signature": "Crash: x", "peer": "S", "line": "...", "kind": "crash"})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(skytest.cmd_breaker(cfg, argparse.Namespace(reset=False)), 0)
        self.assertIn("circuit breaker: 3 cases on build abc123", out.getvalue())
        self.assertIn("1 breaker file(s) in", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(skytest.cmd_breaker(cfg, argparse.Namespace(reset=True)), 0)
        self.assertIn("circuit breaker: reset, 1 file(s) deleted", out.getvalue())
        self.assertIsNone(br.tripped())

    # --- R2 review fixes: stale and failing lock files ---

    def test_a_boot_note_in_the_future_never_blocks(self) -> None:
        a = self.budget(owner="lane 1")
        a.boot_turn(0.0).release()
        with open(self.slots / "boot.lock", "r+b") as f:  # a clock set back by a day, or a garbage float
            f.write(json.dumps({"last_start": time.time() + 86400}).encode().ljust(skytest.SLOT_NOTE_BYTES))
        t0 = time.monotonic()
        a.boot_turn(0.5).release()
        self.assertLess(time.monotonic() - t0, 0.4)
        with open(self.slots / "boot.lock", "r+b") as f:
            f.write(b'{"last_start": NaN}'.ljust(skytest.SLOT_NOTE_BYTES))
        t0 = time.monotonic()
        a.boot_turn(0.5).release()
        self.assertLess(time.monotonic() - t0, 0.4)
        # A recent start still holds the gap (and never more than the gap).
        t0 = time.monotonic()
        a.boot_turn(0.5).release()
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_a_lock_file_that_cannot_be_opened_frees_what_was_taken(self) -> None:
        b = self.budget(owner="lane 1")
        real = skytest._open_lock_file
        calls = [0]

        def failing(path):
            calls[0] += 1
            if path.name in ("boot.lock", "slot-02"):
                raise PermissionError(13, "sharing violation", str(path))
            return real(path)

        skytest._open_lock_file = failing
        try:
            with self.assertRaises(PermissionError):
                b.boot_turn(0.0)
            with self.assertRaises(PermissionError):
                with b.mutex():
                    b._probe()
        finally:
            skytest._open_lock_file = real
        # The runner's boot lock was given back, and slots 00 and 01 (locked before 02 failed) are free.
        b.boot_turn(0.0).release()
        lease = self.budget(owner="lane 2").acquire(4, "all")
        self.assertEqual(sorted(lease.slots), [0, 1, 2, 3])
        lease.release()

    def test_a_holder_of_every_slot_leaves_none_to_a_runner_with_a_larger_budget(self) -> None:
        small, big = self.budget(4, "lane 1"), self.budget(8, "lane 2")
        timed = small.acquire(4, "timed")
        self.assertTrue(timed.summary()["all"])
        self.assertEqual(big.held_all(), "timed (lane 1)")
        said: list[str] = []
        t, box = self.acquire_in_thread(big, 1, "solo", said)
        self.wait_until(lambda: said)
        self.assertEqual(said[0], "machine slots: needs 1 of 8, 0 free; held by timed (lane 1, 4)")
        timed.release()
        t.join(timeout=10)
        self.assertEqual(len(box["lease"].slots), 1)
        self.assertEqual(big.held_all(), "")
        box["lease"].release()

    @unittest.skipUnless(os.name == "nt", "the slot locks are Windows byte-range locks")
    def test_a_runner_waiting_for_the_machine_mutex_says_so(self) -> None:
        b = self.budget(owner="lane 1")
        fd = skytest._open_lock_file(self.slots / "machine.lock")
        self.assertTrue(skytest._try_lock(fd))  # another runner, frozen while it holds the mutex
        real = skytest.MUTEX_TELL_S
        skytest.MUTEX_TELL_S = 0.2
        out = io.StringIO()
        try:
            def free_later() -> None:
                time.sleep(0.6)
                skytest._unlock_close(fd)

            threading.Thread(target=free_later, daemon=True).start()
            with contextlib.redirect_stdout(out):
                with b.mutex():
                    pass
        finally:
            skytest.MUTEX_TELL_S = real
        self.assertIn("machine.lock for 0 s: another runner holds it", out.getvalue())


class MachineRunTest(unittest.TestCase):
    """PS15 and C7 through cmd_test with the fake game."""

    setUp = SkytestTest.setUp
    tearDown = SkytestTest.tearDown
    spec = SkytestTest.spec

    def run_runner(self, cfg, names: list[str], box: dict, key: str, **extra) -> None:
        a = argparse.Namespace(cases=names, stage=None, max_games=5, jobs=1, **extra)
        box[key] = skytest.cmd_test(cfg, a)

    def test_two_runners_never_run_more_games_than_the_machine_budget(self) -> None:
        self.cfg.machine_games = 2
        script = '[peer.H]\nscript = "wait 150\\nquit\\n"\n'
        self.spec("lane-a", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 150\n' + script)
        self.spec("lane-b", 'layout = "server+1"\nstage = "0"\nstandin_seconds = 150\n' + script)
        out, box = ThreadOut(), {}
        real = sys.stdout
        sys.stdout = out
        try:
            ta = threading.Thread(target=self.run_runner, args=(self.cfg, ["lane-a"], box, "a"))
            tb = threading.Thread(target=self.run_runner, args=(replace(self.cfg), ["lane-b"], box, "b"))
            ta.start()
            time.sleep(0.3)
            tb.start()
            ta.join(timeout=60)
            tb.join(timeout=60)
        finally:
            sys.stdout = real
        self.assertEqual((box.get("a"), box.get("b")), (0, 0))
        text = "\n".join(b.getvalue() for b in out.buffers.values())
        runs = {}
        for line in text.splitlines():
            if line.startswith("report: "):
                data = json.loads((Path(line[8:]).parent / "results.json").read_text(encoding="utf-8"))
                runs[data["cases"][0]["case"]] = (data, Path(line[8:]))
        cases = {k: v[0]["cases"][0] for k, v in runs.items()}
        # Whichever runner came second waited for the other's slots; the two never overlapped.
        waiter = max(cases, key=lambda k: cases[k]["machine"]["waited_s"])
        first = "lane-a" if waiter == "lane-b" else "lane-b"
        self.assertRegex(text, rf"{waiter}: waits for machine slots: needs every slot, 0 free; held by {first} "
                               r"\(skytest pid \d+, \d{8}-\d{6}-session(-\d+)?, 2\)")
        self.assertGreater(cases[waiter]["machine"]["waited_s"], 0.3)
        self.assertIn(f"held by {first}", cases[waiter]["machine"]["waited_for"][0])
        self.assertEqual(cases[first]["machine"]["slots"], 2)
        self.assertEqual(runs["lane-a"][0]["machine"]["games"], 2)
        self.assertTrue(all(p["boot"] for c in cases.values() for p in c["peers"]), cases)
        self.assertTrue(all(p["boot"].startswith("first frame after") for c in cases.values() for p in c["peers"]
                            if p["peer"] == "H"), cases)
        md = runs[waiter][1].read_text(encoding="utf-8")
        self.assertIn("The machine's budget, shared with every other runner: 2 game(s)", md)
        self.assertRegex(md, r"Machine: 2 of 2 game slots \(all: nothing of another runner beside it\); "
                             r"waited \d+ s \(machine slots: needs every slot")

    def test_the_slot_folder_and_machine_games_are_checked(self) -> None:
        self.assertEqual(self.cfg.machine_games, 16)
        self.assertEqual(self.cfg.slots, (self.repo / skytest.STORE / "slots").resolve())
        toml = self.repo / "tests" / "skytest.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace("machine_games = 16", "machine_games = 0"),
                        encoding="utf-8")
        with self.assertRaises(SystemExit) as e:
            skytest.load_config(self.repo)
        self.assertIn("machine_games is a whole number from 1 to 32", str(e.exception))
        toml.write_text(toml.read_text(encoding="utf-8").replace("machine_games = 0\n", ""), encoding="utf-8")
        self.assertEqual(skytest.load_config(self.repo).machine_games, skytest.MACHINE_GAMES)
        with self.assertRaises(SystemExit):
            skytest.check_slots_folder(self.cfg, Path(self.tmp.name) / "elsewhere" / "slots")
        with self.assertRaises(SystemExit):
            skytest.check_slots_folder(self.cfg, self.cfg.rom / skytest.STORE / "slots")

    def test_a_tripped_breaker_starts_no_further_case(self) -> None:
        crash = 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "crash\\nquit\\n"\n'
        for n in ("c1", "c2", "c3"):
            self.spec(n, crash)
        self.spec("fine", 'layout = "solo"\nstage = "0"\n[peer.H]\n' + SkytestTest.HUB)
        self.spec("crashtest", 'layout = "solo"\nstage = "0"\nbreaker = false\n[peer.H]\n' + SkytestTest.HUB)
        a = argparse.Namespace(cases=["c1", "c2", "c3", "fine", "crashtest"], stage=None, max_games=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        printed = out.getvalue()
        report = printed.strip().splitlines()[-1].split("report: ", 1)[1]
        r = json.loads((Path(report).parent / "results.json").read_text(encoding="utf-8"))
        st = {c["case"]: c for c in r["cases"]}
        self.assertEqual(code, 1)
        self.assertEqual([st[n]["status"] for n in ("c1", "c2", "c3")], ["fail"] * 3)
        self.assertEqual(st["c1"]["signature"]["signature"], "Crash: exception C0000005 on thread <n>; call chain:"
                                                             " sub_82000000")
        self.assertEqual(st["fine"]["status"], "breaker")
        self.assertEqual(st["fine"]["seconds"], 0.0)
        self.assertIn("not started: circuit breaker: 3 cases on build", st["fine"]["reasons"][0])
        self.assertEqual(st["crashtest"]["status"], "pass")  # breaker = false still runs
        self.assertEqual(r["breaker"]["cases"], ["c1", "c2", "c3"])
        self.assertIn("circuit breaker: 3 cases on build", printed)
        self.assertIn("1 case(s) not started by the circuit breaker (not met): fine", printed)
        md = Path(report).read_text(encoding="utf-8")
        self.assertIn("| fine | solo | 0 | BREAKER | 0 s |", md)
        self.assertIn("Fatal signature (H, crash): `Crash: exception C0000005 on thread <n>", md)
        # Another runner on the same build: nothing starts; --no-breaker starts it.
        a = argparse.Namespace(cases=["fine"], stage=None, max_games=5)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(skytest.cmd_test(self.cfg, a), 1)
        a = argparse.Namespace(cases=["fine"], stage=None, max_games=5, no_breaker=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(skytest.cmd_test(self.cfg, a), 0)
        self.assertIn("--no-breaker: cases go on starting", out.getvalue())
        # A spec's breaker key is checked.
        self.spec("bad", 'layout = "solo"\nstage = "0"\nbreaker = "no"\n[peer.H]\n' + SkytestTest.HUB)
        with self.assertRaises(skytest.SpecError) as e:
            skytest.load_spec(self.repo / "tests" / "runtime" / "bad.toml", 20)
        self.assertIn("breaker is true or false", str(e.exception))


class UnitLayoutTest(unittest.TestCase):
    """The `unit` layout (stage 2 item 7): a test binary from its snapshot and named ctests, no game.
    The repository here has no disc and no game exe: a unit case needs neither."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "game"
        (self.repo / "tests" / "runtime").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.build = self.repo / "out" / "build"
        (self.build / "bin").mkdir(parents=True)
        (self.build / "bin" / "unit_test.py").write_bytes((HERE / "fake_unit.py").read_bytes())
        (self.build / "bin" / "helper.dll").write_bytes(b"MZ dll")
        self.ctest_dir = self.build / "portal" / "backend"
        self.ctest_dir.mkdir(parents=True)
        self.ctests("backend.include_rule pass\nskylanders_backend_test pass\n")
        (self.repo / "tests" / "skytest.toml").write_text(f"""
[game]
exe = "game.exe"
build = "out/build"
settings_template = "settings.example"
unit_launcher = [{toml_str(sys.executable)}, "-I"]
ctest = [{toml_str(sys.executable)}, "-I", {toml_str(HERE / 'fake_ctest.py')}]

[fixtures]
owner_saves = {toml_str(root / 'owner' / 'saves')}
owner_figures = {toml_str(root / 'owner' / 'figures')}

[defaults]
timeout = 20
""", encoding="utf-8")
        self.cfg = skytest.load_config(self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ctests(self, listing: str) -> None:
        (self.ctest_dir / "fake_ctest.txt").write_text(listing, encoding="utf-8")

    UNIT = ('layout = "unit"\nstage = "2"\n[unit]\nbinary = "bin/unit_test.py"\n'
            'pass_line = "all backend tests passed"\n')
    CTEST = 'ctest = ["backend.include_rule"]\nctest_dir = "portal/backend"\n'

    def spec(self, name: str, body: str) -> Path:
        p = self.repo / "tests" / "runtime" / f"{name}.toml"
        p.write_text(f'name = "{name}"\n' + body, encoding="utf-8")
        return p

    def run_cases(self, *names: str, stage: str | None = None, jobs: int = 1) -> dict:
        a = argparse.Namespace(cases=list(names), stage=stage, max_games=1, jobs=jobs)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_test(self.cfg, a)
        printed = out.getvalue().strip().splitlines()
        report = printed[-1].split("report: ", 1)[1]
        results = json.loads((Path(report).parent / "results.json").read_text(encoding="utf-8"))
        results["exit"], results["folder"], results["printed"] = code, Path(report).parent, printed
        return results

    def test_a_unit_case_passes_on_its_exit_code_and_pass_line_and_meets_the_checkpoint(self) -> None:
        self.spec("backend-unit", self.UNIT + self.CTEST)
        (self.repo / "tests" / "manifest.toml").write_text(
            'stages = ["0", "2"]\n[[case]]\nid = "backend-unit"\nstage = "2"\nlayout = "unit"\nkind = "required"\n'
            'spec = true\n', encoding="utf-8")
        r = self.run_cases(stage="2")
        case = r["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        self.assertEqual(r["exit"], 0)
        self.assertEqual([p["peer"] for p in case["peers"]], ["test", "ctest backend.include_rule"])
        test = case["peers"][0]
        self.assertEqual(test["graded"]["unit"], {"pass_line": True, "tests": 3, "failed": 0, "ms": 5})
        # Run from the snapshot, never from the build folder; the DLLs beside it went along.
        exe = Path(test["command"][2])
        self.assertTrue(skytest.inside(exe, self.repo / ".replication-tests" / "snapshots"), exe)
        self.assertTrue(exe.parent.name.startswith("unit-"))
        self.assertTrue((exe.parent / "helper.dll").is_file())
        self.assertIn(f"ran from {exe}", (Path(case["folder"]) / "test.txt").read_text(encoding="utf-8"))
        ctest = case["peers"][1]["command"]
        self.assertEqual(ctest[ctest.index("-R") + 1], "^backend\\.include_rule$")
        self.assertIn("--no-tests=error", ctest)
        self.assertIn("checkpoint 2: met", r["printed"])
        self.assertEqual(r["snapshot"], "")  # no game snapshot was taken

    def test_a_failing_or_silent_binary_fails(self) -> None:
        self.spec("fails", self.UNIT + 'args = ["--mode=fail"]\n')
        case = self.run_cases("fails")["cases"][0]
        self.assertEqual(case["status"], "fail")
        reasons = case["peers"][0]["reasons"]
        self.assertIn("exit code 1", reasons)
        self.assertIn("FAIL tests/backend_entity_id_test.cpp:42: id.Valid()", reasons)
        self.assertIn("no 'all backend tests passed' line", reasons)
        self.assertEqual(case["peers"][0]["graded"]["unit"]["failed"], 1)
        self.spec("silent", self.UNIT + 'args = ["--mode=silent"]\n')
        case = self.run_cases("silent")["cases"][0]
        self.assertEqual(case["status"], "fail")
        self.assertEqual(case["peers"][0]["reasons"], ["no 'all backend tests passed' line"])

    def test_a_missing_or_failing_ctest_fails(self) -> None:
        self.spec("missing", self.UNIT + 'ctest = ["backend.other_rule"]\nctest_dir = "portal/backend"\n')
        case = self.run_cases("missing")["cases"][0]
        self.assertEqual(case["status"], "fail")
        self.assertEqual(case["peers"][1]["exit_code"], 8)
        self.ctests("backend.include_rule fail\n")
        self.spec("failing", self.UNIT + self.CTEST)
        case = self.run_cases("failing")["cases"][0]
        self.assertEqual(case["status"], "fail")
        self.assertEqual(case["reasons"], ["failed: ctest backend.include_rule"])
        # A name is matched whole: neither backendXinclude_rule nor backend.include_rule2 runs.
        self.ctests("backendXinclude_rule pass\nbackend.include_rule2 pass\n")
        case = self.run_cases("failing")["cases"][0]
        self.assertEqual(case["status"], "fail")

    def test_a_hung_binary_is_ended(self) -> None:
        self.spec("hang", self.UNIT.replace('stage = "2"\n', 'stage = "2"\ntimeout = 2\n') + 'args = ["--mode=hang"]\n')
        t0 = time.monotonic()
        case = self.run_cases("hang")["cases"][0]
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(case["status"], "fail")
        self.assertTrue(case["peers"][0]["ended_by_runner"])
        self.assertIn("did not end within 2 s; ended", case["peers"][0]["reasons"])

    def test_no_binary_or_no_ctest_folder_is_an_error(self) -> None:
        self.spec("nobin", self.UNIT.replace("bin/unit_test.py", "bin/missing.exe"))
        case = self.run_cases("nobin")["cases"][0]
        self.assertEqual(case["status"], "error")
        self.assertIn("no unit test binary", case["reasons"][0])
        self.spec("nodir", self.UNIT + 'ctest = ["backend.include_rule"]\nctest_dir = "portal/none"\n')
        case = self.run_cases("nodir")["cases"][0]
        self.assertEqual(case["status"], "error")
        self.assertIn("no ctest folder", case["reasons"][0])

    def test_unit_spec_errors(self) -> None:
        bad = {
            "needs a [unit] table": 'layout = "unit"\nstage = "2"\n',
            "pass_line": 'layout = "unit"\nstage = "2"\n[unit]\nbinary = "bin/unit_test.py"\n',
            "relative to the build folder": self.UNIT.replace("bin/unit_test.py", "C:/x/unit_test.exe"),
            "relative to the build folder, inside it": self.UNIT.replace("bin/unit_test.py", "../x/unit_test.exe"),
            "refusing --update-golden": self.UNIT + 'args = ["--update-golden"]\n',
            "refusing --diff": self.UNIT + 'args = ["--diff"]\n',
            "refusing an empty argument": self.UNIT + 'args = ["--filter="]\n',
            "ctest_dir must be a path": self.UNIT + 'ctest = ["backend.include_rule"]\n',
            "need ctest names": self.UNIT + 'ctest_dir = "portal/backend"\n',
            "list of test names": self.UNIT + 'ctest = ["backend include_rule"]\n',
            "names a test twice": self.UNIT + 'ctest = ["a", "a"]\nctest_dir = "portal/backend"\n',
            "does not know": self.UNIT + 'binaries = "x"\n',
            "runs no game; peer": 'layout = "unit"\nstage = "2"\n[peer.H]\nscript = "quit"\n',
            "runs no game; grade": self.UNIT.replace("[unit]", '[grade.symmetry]\n[unit]'),
        }
        for want, body in bad.items():
            with self.subTest(want=want):
                p = self.spec("bad", body)
                with self.assertRaises(skytest.SpecError) as e:
                    skytest.load_spec(p, 20)
                self.assertIn(want, str(e.exception))
        s = skytest.load_spec(self.spec("good", self.UNIT + self.CTEST), 20)
        self.assertEqual((s.layout, s.peers, s.unit["ctest"]), ("unit", {}, ["backend.include_rule"]))

    def test_unit_cases_run_through_the_scheduler_without_machine_slots(self) -> None:
        # Two unit cases under --jobs 2 run at once (each holds no game), take no machine slot, write
        # their own result.json, share one unit snapshot and take no game snapshot (no disc, no exe here).
        self.spec("one", self.UNIT + 'args = ["--sleep=1.5"]\n')
        self.spec("two", self.UNIT + 'args = ["--sleep=1.5"]\n')
        r = self.run_cases("one", "two", jobs=2)
        self.assertEqual(r["exit"], 0, r["printed"])
        self.assertEqual(r["snapshot"], "")
        self.assertEqual(r["jobs"], 2)
        cases = {c["case"]: c for c in r["cases"]}
        for name, other in (("one", "two"), ("two", "one")):
            c = cases[name]
            self.assertEqual(c["status"], "pass", c)
            self.assertEqual(c["ran_with"], [other])
            self.assertNotIn("slots", c["machine"])  # no lease of the machine's game slots
            self.assertEqual(c["scenes"], "none (a unit case starts no game)")
            self.assertTrue(c["provenance"]["spec_sha256"])
            own = json.loads((Path(c["folder"]) / "result.json").read_text(encoding="utf-8"))
            self.assertEqual((own["case"], own["status"]), (name, "pass"))
        self.assertEqual(cases["one"]["peers"][0]["command"][2], cases["two"]["peers"][0]["command"][2])
        self.assertFalse(list((self.repo / ".replication-tests" / "snapshots").glob("*.partial*")))

    def test_a_unit_case_not_ready_or_exclusive(self) -> None:
        self.spec("later", self.UNIT.replace('stage = "2"\n', 'stage = "2"\nready = false\nreason = "item 6"\n'))
        r = self.run_cases("later")
        self.assertEqual(r["cases"][0]["status"], "not ready")
        self.assertEqual(r["snapshot"], "")
        self.assertFalse((self.repo / ".replication-tests" / "snapshots").exists())
        s = skytest.load_spec(self.spec("alone", 'exclusive = true\n' + self.UNIT), 20)
        self.assertTrue(s.exclusive)
        self.assertFalse(s.breaker)  # never counted toward, nor stopped by, the circuit breaker
        for key in ('scenes = "skip"\n', 'breaker = true\n', 'fail_fast = false\n', 'tolerate = ["x"]\n'):
            with self.subTest(key=key), self.assertRaises(skytest.SpecError) as e:
                skytest.load_spec(self.spec("bad", key + self.UNIT), 20)
            self.assertIn("runs no game", str(e.exception))

    def test_lint_takes_a_unit_case(self) -> None:
        self.spec("backend-unit", self.UNIT + self.CTEST)
        (self.repo / "tests" / "manifest.toml").write_text(
            'stages = ["2"]\n[[case]]\nid = "backend-unit"\nstage = "2"\nlayout = "unit"\nkind = "required"\n'
            'spec = true\n', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skytest.cmd_lint(self.cfg, argparse.Namespace())
        self.assertEqual(code, 0, out.getvalue())


class JobTest(unittest.TestCase):
    @unittest.skipUnless(winjob.WINDOWS, "job objects are Windows only")
    def test_closing_the_job_ends_its_processes(self) -> None:
        p = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        job = winjob.Job()
        job.add(p)
        self.assertEqual(job.active(), 1)
        job.close()
        p.wait(timeout=10)
        self.assertIsNotNone(p.returncode)


if __name__ == "__main__":
    unittest.main()
