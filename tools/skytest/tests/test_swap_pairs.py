"""Tests for the remote swap event pair grade (swap_pairs.py) on recorded lines. Run:
python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import swap_pairs  # noqa: E402


def swap(n: int, hero: str, tick: int, epoch: int = 1, gen: int = 3) -> str:
    return f"[info] Online: swap {n} hero {hero} epoch {epoch} gen {gen} tick {tick}"


def body(peer: int, hero: str, kind: str, n: int, tick: int, epoch: int = 1, gen: int = 3) -> str:
    return f"[info] Online: puppet {peer} epoch {epoch} gen {gen} body {hero} ({kind}) swap {n} tick {tick}"


def freed(peer: int, n: int, tick: int, epoch: int = 1, gen: int = 3) -> str:
    return f"[info] Online: body slot 0 freed for puppet {peer} epoch {epoch} gen {gen} swap {n} tick {tick}"


class SwapPairs(unittest.TestCase):
    def test_both_directions_pass(self) -> None:
        logs = {
            1: "\n".join([swap(1, "Wash Buckler", 600), body(2, "Magna Charge", "real", 1, 900)]),
            2: "\n".join([body(1, "Wash Buckler", "real", 1, 618), swap(1, "Magna Charge", 880)]),
        }
        r = swap_pairs.grade_swap_pairs(logs)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual((r.expected, r.graded), (2, 2))
        self.assertAlmostEqual(r.max_lag_ms, 333.33, places=1)

    def test_slow_pair_fails(self) -> None:
        logs = {1: swap(1, "Rubble Rouser", 600), 2: body(1, "Rubble Rouser", "real", 1, 631)}
        r = swap_pairs.grade_swap_pairs(logs)
        self.assertFalse(r.passed)
        self.assertIn("517 ms", r.failures[0])

    def test_missing_pair_fails(self) -> None:
        logs = {1: swap(1, "Rubble Rouser", 600), 2: body(1, "Rubble Rouser", "real", 1, 610), 3: ""}
        r = swap_pairs.grade_swap_pairs(logs)
        self.assertEqual(r.expected, 2)
        self.assertEqual(r.graded, 1)
        self.assertTrue(any("missing" in f and "on peer 3" in f for f in r.failures))

    def test_duplicate_and_wrong_body_fail(self) -> None:
        dup = {1: swap(1, "A", 600), 2: "\n".join([body(1, "A", "real", 1, 601), body(1, "A", "real", 1, 640)])}
        self.assertTrue(any("duplicate" in f for f in swap_pairs.grade_swap_pairs(dup).failures))
        wrong = {1: swap(1, "A", 600), 2: body(1, "B", "real", 1, 601)}
        self.assertTrue(any("wrong body" in f for f in swap_pairs.grade_swap_pairs(wrong).failures))

    def test_early_preload_counts_as_zero(self) -> None:
        logs = {1: swap(2, "A", 600), 2: body(1, "A", "real", 2, 580)}
        r = swap_pairs.grade_swap_pairs(logs)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.max_lag_ms, 0.0)

    def test_keys_never_mix_epochs_or_generations(self) -> None:
        # The receiver's line of another generation (or peer epoch) is not this key's completion.
        logs = {1: swap(1, "A", 600, gen=3), 2: "\n".join([body(1, "A", "real", 1, 610, gen=2),
                                                           body(1, "A", "real", 1, 612, epoch=2)])}
        r = swap_pairs.grade_swap_pairs(logs)
        self.assertTrue(any("missing" in f for f in r.failures))

    def test_pressure_pair(self) -> None:
        ok = {1: swap(1, "E", 600), 2: "\n".join([body(1, "C", "stand-in", 1, 620), freed(1, 1, 1200),
                                                  body(1, "E", "real", 1, 1225)])}
        r = swap_pairs.grade_swap_pairs(ok)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.pressure_pairs, 1)
        slow_stand = {1: swap(1, "E", 600), 2: "\n".join([body(1, "C", "stand-in", 1, 640), freed(1, 1, 1200),
                                                          body(1, "E", "real", 1, 1210)])}
        self.assertTrue(any("stand-in after" in f for f in swap_pairs.grade_swap_pairs(slow_stand).failures))
        slow_real = {1: swap(1, "E", 600), 2: "\n".join([body(1, "C", "stand-in", 1, 610), freed(1, 1, 1200),
                                                         body(1, "E", "real", 1, 1240)])}
        self.assertTrue(any("after the slot freed" in f for f in swap_pairs.grade_swap_pairs(slow_real).failures))
        none = {1: swap(1, "E", 600), 2: "\n".join([freed(1, 1, 1200), body(1, "E", "real", 1, 1210)])}
        self.assertTrue(any("no stand-in" in f for f in swap_pairs.grade_swap_pairs(none).failures))


if __name__ == "__main__":
    unittest.main()
