"""Tests for skytest on a throwaway game repository with a fake game. Run:
python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import skytest  # noqa: E402
import winjob  # noqa: E402

FAKE = HERE / "fake_game.py"


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
""", encoding="utf-8")
        self.cfg = skytest.load_config(self.repo)
        self.cfg.start_gap = 0.0
        with contextlib.redirect_stdout(io.StringIO()):
            skytest.cmd_fixtures(self.cfg, argparse.Namespace(from_owner=True))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def spec(self, name: str, body: str) -> Path:
        p = self.repo / "tests" / "runtime" / f"{name}.toml"
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

    def test_a_case_cannot_override_the_runner_settings(self) -> None:
        self.spec("override", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n'
                  'args = ["--portal_mode=usb"]\n')
        case = self.run_cases("override")["cases"][0]
        self.assertEqual(case["status"], "error")
        self.assertIn("portal_mode", case["reasons"][0])

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
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["compare.json", "compare.md", "off1", "off2", "on"])
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
        self.assertEqual(k["loads"], {"start -> hub": 24.0, "level 2 tutorial_b: open -> server up": 8.1,
                                      "level 2 tutorial_b: open -> barrier": 10.0,
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

    def test_the_aa_pair_alone(self) -> None:
        c = {**COMPARE, "test": []}
        cmp = self.compare({"off1": server_log(level_load=8.0), "off2": server_log(level_load=8.4, ticks=7500)}, c)
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        self.assertEqual(cmp["messages"]["noisy"], 1)
        self.assertEqual(cmp["loads"]["level 2 tutorial_b: open -> server up"]["tolerance"], 0.5, "the floor")
        md = "\n".join(skytest.compare_markdown(cmp))
        self.assertIn("A/A: off1, off2.", md)
        self.assertIn("| message CTickMessage | 7000 / 7500 |", md)

    def test_the_ab_passes_with_equal_lines_and_loads_within_the_noise(self) -> None:
        cmp = self.compare({"off1": server_log(level_load=8.0, ticks=7000),
                            "off2": server_log(level_load=9.0, ticks=7500),
                            # The skip on: a faster load passes; 8000 is within 7000-7500 widened by 500.
                            "on": server_log(skip=True, level_load=6.0, ticks=8000)})
        self.assertEqual(cmp["status"], "pass", cmp["reasons"])
        self.assertEqual(cmp["loads"]["level 2 tutorial_b: open -> server up"]["tolerance"], 1.0, "the A/A spread")
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
                              "compare: load level 2 tutorial_b: open -> server up: on 9.100 s, over the A/A "
                              "8.100-8.100 s by more than 0.500 s"),
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

    def test_a_peer_gone_at_close_is_left_out_and_no_request_is_not_reached(self) -> None:
        log = rq(f"request 1:1 enter {self.E1}")
        graded, reasons = skytest.grade_requests_answered(log, {"1"})
        self.assertEqual(graded["left_out_gone"], 1)
        self.assertEqual(reasons, ["grade requests_answered: no request line (not reached)"])
        _, reasons = skytest.grade_requests_answered(rq("frames 600 fps 60.0"), set())
        self.assertEqual(reasons, ["grade requests_answered: no request line (not reached)"])

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
