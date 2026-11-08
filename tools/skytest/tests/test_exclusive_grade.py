"""Tests for skytest's exclusive grade (exclusive_grade.py): one winner with one execution and one
microgame open, every other client refused with neither. Run: python -I -m unittest discover -s
tools/skytest/tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import exclusive_grade as eg  # noqa: E402

NAMES = ["S", "H", "D"]
G = {
    "peers": ["H", "D"],
    "winner": [r"Online: start ran start\.personal .*\(lease [0-9]+\)", r"Online: start lease [0-9]+ activity open"],
    "loser": [r"Online: start refused start\.personal .*\(leased\)"],
}
RAN = "[info] Online: start ran start.personal Chest#410 (lease 3)"
OPEN = "[info] Online: start lease 3 activity open"
REFUSED = "[info] Online: start refused start.personal Chest#410 (leased)"


class ExclusiveGrade(unittest.TestCase):
    def test_one_winner(self) -> None:
        eg.check_exclusive("lease-race.toml", G, NAMES)
        graded, reasons = eg.grade_exclusive({"H": f"{RAN}\n{OPEN}", "D": REFUSED}, G)
        self.assertEqual(reasons, [])
        self.assertEqual(graded["winner"], "H")

    def test_grant_without_microgame_fails(self) -> None:
        _, reasons = eg.grade_exclusive({"H": RAN, "D": REFUSED}, G)
        self.assertTrue(reasons)

    def test_two_runs_fail(self) -> None:
        _, reasons = eg.grade_exclusive({"H": f"{RAN}\n{OPEN}", "D": f"{RAN}\n{OPEN}"}, G)
        self.assertTrue(any("2 winners" in r for r in reasons))

    def test_refused_client_that_also_ran_fails(self) -> None:
        _, reasons = eg.grade_exclusive({"H": f"{RAN}\n{OPEN}", "D": f"{REFUSED}\n{RAN}"}, G)
        self.assertTrue(any("D neither" in r for r in reasons))

    def test_grant_and_refusal_with_no_start_fails(self) -> None:
        # S's one grant and one refusal are not enough: nothing ran on any client.
        _, reasons = eg.grade_exclusive({"H": "", "D": REFUSED}, G)
        self.assertTrue(reasons)

    def test_not_reached_fails(self) -> None:
        _, reasons = eg.grade_exclusive({"H": "", "D": ""}, G)
        self.assertTrue(reasons)

    def test_spec_checks(self) -> None:
        for bad in ({"peers": ["H"], "winner": ["a"], "loser": ["b"]},
                    {"peers": ["H", "S"], "winner": ["a"], "loser": ["b"]},
                    {"peers": ["H", "D"], "winner": [], "loser": ["b"]},
                    {"peers": ["H", "D"], "winner": ["("], "loser": ["b"]},
                    {"peers": ["H", "H"], "winner": ["a"], "loser": ["b"]}):
            with self.assertRaises(eg.ExclusiveSpecError):
                eg.check_exclusive("x.toml", bad, NAMES)


if __name__ == "__main__":
    unittest.main()
