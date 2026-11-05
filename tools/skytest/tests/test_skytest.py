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
        (owner / "figures").mkdir(parents=True)
        (owner / "figures" / "Roller Brawl.dump").write_bytes(b"\0" * 1024)
        (self.repo / "tests" / "skytest.toml").write_text(f"""
[game]
exe = "game.exe"
build = "out/build"
rom = "rom"
settings_template = "settings.example"
launcher = [{toml_str(sys.executable)}, "-I", {toml_str(FAKE)}]

[fixtures]
owner_saves = {toml_str(owner / 'saves')}
owner_figures = {toml_str(owner / 'figures')}

[defaults]
args = ["--portal_mode=software", "--audio_mute=true", "--fullscreen=false"]
timeout = 20
""", encoding="utf-8")
        self.cfg = skytest.load_config(self.repo)
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
