"""Tests for the runner's half of builds-versus-tests (build_guard.py): the decision, the wait loop with
a fake clock (one line a minute, the limit, the override), the slot folder, and a real held slot probed
through a second process's lock (Windows).
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_build_guard.py"
"""
from __future__ import annotations

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
import build_guard as bg  # noqa: E402

BUILD = {"worktree": "C:/w/x", "recipes": ["build-release"], "pid": 42, "since": "14:13:00"}


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class DecideTest(unittest.TestCase):
    def test_no_build_starts(self):
        self.assertEqual(bg.decide([], 0.0, 60.0, ""), bg.START)
        self.assertEqual(bg.decide([], 999.0, 60.0, "why"), bg.START)

    def test_a_build_waits_until_the_limit_then_gives_up(self):
        self.assertEqual(bg.decide([BUILD], 0.0, 60.0, ""), bg.WAIT)
        self.assertEqual(bg.decide([BUILD], 59.9, 60.0, ""), bg.WAIT)
        self.assertEqual(bg.decide([BUILD], 60.0, 60.0, ""), bg.GIVE_UP)

    def test_the_override_starts_beside_a_build(self):
        self.assertEqual(bg.decide([BUILD], 0.0, 60.0, "the owner's measurement"), bg.OVERRIDE)


class WaitTest(unittest.TestCase):
    def run_wait(self, holders_at, limit_s=600.0, override=""):
        ft, said = FakeTime(), []
        got = bg.wait_for_builds("case-x", limit_s, override, lambda: holders_at(ft.t), said.append,
                                 clock=ft.clock, sleep=ft.sleep, poll_s=5.0)
        return got, said

    def test_no_build_starts_at_once_and_says_nothing(self):
        (d, waited, line), said = self.run_wait(lambda t: [])
        self.assertEqual((d, waited, line, said), (bg.START, 0.0, "", []))

    def test_it_waits_with_one_line_a_minute_and_starts_when_the_build_ends(self):
        (d, waited, line), said = self.run_wait(lambda t: [BUILD] if t < 185 else [])
        self.assertEqual(d, bg.START)
        self.assertEqual(waited, 185.0)
        waits = [s for s in said if "waits for a build" in s]
        self.assertEqual(len(waits), 4)  # at 0, 60, 120, 180 s
        self.assertIn("C:/w/x (build-release), pid 42", waits[0])
        self.assertIn("the build ended after 185 s", said[-1])

    def test_past_the_limit_it_gives_up_with_its_own_line(self):
        (d, waited, line), said = self.run_wait(lambda t: [BUILD], limit_s=120.0)
        self.assertEqual(d, bg.GIVE_UP)
        self.assertGreaterEqual(waited, 120.0)
        self.assertTrue(line.startswith("not started: a build still held its slot after 2 min"))
        self.assertIn("--ignore-build", line)

    def test_the_override_goes_on_at_once_and_its_why_is_in_the_line(self):
        (d, waited, line), said = self.run_wait(lambda t: [BUILD], override="measuring the build's load")
        self.assertEqual((d, waited), (bg.OVERRIDE, 0.0))
        self.assertIn("--ignore-build: measuring the build's load", line)


class SlotFolderTest(unittest.TestCase):
    def test_the_folder_follows_build_low(self):
        self.assertEqual(bg.slot_dir({"SFO_BUILD_SLOT_DIR": "D:/x"}), Path("D:/x"))
        self.assertEqual(bg.slot_dir({"LOCALAPPDATA": "C:/L"}), Path("C:/L/SwapForceOnline/build-slots"))

    def test_an_absent_or_unheld_folder_has_no_holder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(bg.held_slots(Path(d) / "absent"), [])
            (Path(d) / "slot0.lock").write_text(json.dumps(BUILD), encoding="utf-8")
            self.assertEqual(bg.held_slots(Path(d)), [])

    @unittest.skipUnless(os.name == "nt", "the byte lock is Windows'")
    def test_a_slot_another_process_holds_is_held(self):
        with tempfile.TemporaryDirectory() as d:
            slot = Path(d) / "slot0.lock"
            holder = ("import json, msvcrt, os, sys, time\n"
                      "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_BINARY)\n"
                      "os.write(fd, json.dumps({'worktree': 'C:/w/y', 'pid': os.getpid()}).encode())\n"
                      "os.lseek(fd, 1 << 30, 0); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
                      "print('held', flush=True); time.sleep(30)\n")
            p = subprocess.Popen([sys.executable, "-I", "-c", holder, str(slot)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(p.stdout.readline().strip(), "held")
                got = bg.held_slots(Path(d))
                self.assertEqual([h.get("worktree") for h in got], ["C:/w/y"])
            finally:
                p.kill()
                p.wait()
            deadline = time.monotonic() + 5
            while bg.held_slots(Path(d)) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(bg.held_slots(Path(d)), [])  # Windows dropped the lock with the process


if __name__ == "__main__":
    unittest.main()
