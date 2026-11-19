"""Tests for the runner's preflight gate (preflight_gate.py): errors stop a case, warnings do not, the
override records its why.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_preflight_gate.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import preflight_gate as pg  # noqa: E402

ERR = ("error", "x: error objects H: goto_entity Gem: no run-time name")
WARN = ("warn", "x: warn lines H: pattern /Foo/ names no literal of the code")


class Decide(unittest.TestCase):
    def test_nothing_found_starts_and_records_nothing(self):
        g = pg.decide([])
        self.assertTrue(g.start)
        self.assertEqual((g.printed, g.record()), ([], {}))

    def test_warnings_are_printed_and_the_case_starts(self):
        g = pg.decide([WARN])
        self.assertTrue(g.start)
        self.assertEqual(g.printed, [f"preflight warning: {WARN[1]}"])
        self.assertEqual(g.record(), {"errors": [], "warnings": [WARN[1]]})

    def test_an_error_stops_the_case_with_its_lines(self):
        g = pg.decide([WARN, ERR])
        self.assertFalse(g.start)
        self.assertEqual(g.errors, [ERR[1]])
        self.assertIn("not started: its preflight found 1 error(s)", g.reason)
        self.assertIn("--ignore-preflight", g.reason)
        self.assertIn(f"preflight error: {ERR[1]}", g.printed)
        self.assertEqual(g.printed[-1], g.reason)

    def test_the_override_starts_it_and_records_why(self):
        g = pg.decide([ERR], "the level data is older than the spec")
        self.assertTrue(g.start)
        self.assertEqual(g.record()["ignored"], "the level data is older than the spec")
        self.assertIn("--ignore-preflight: the level data is older than the spec", g.printed[0])


if __name__ == "__main__":
    unittest.main()
