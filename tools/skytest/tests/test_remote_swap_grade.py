"""Tests for skytest's remote_swap grade (remote_swap_grade.py): the spec's ordered swaps and receivers
over swap_pairs.py. Run: python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import remote_swap_grade as rsg  # noqa: E402

NAMES = ["S", "H", "D"]
TH = {"remote_swap": {"max_ms": 500.0}}


def connected(peer: int) -> str:
    return f"[info] Online: connected as peer {peer} (server tick 1, level 3 hub)"


def swap(n: int, hero: str, tick: int) -> str:
    return f"[info] Online: swap {n} hero {hero} epoch 1 gen 3 tick {tick}"


def body(peer: int, hero: str, n: int, tick: int) -> str:
    return f"[info] Online: puppet {peer} epoch 1 gen 3 body {hero} (real) swap {n} tick {tick}"


def run(texts: dict[str, str], g: dict) -> tuple[dict, list[str]]:
    rsg.check_remote_swap("spec.toml", g, NAMES)
    return rsg.grade_remote_swap(texts, g, TH, NAMES)


class RemoteSwapGrade(unittest.TestCase):
    def test_ordered_swaps_pass(self) -> None:
        texts = {
            "S": "",
            "H": "\n".join([connected(1), swap(1, "Wash Buckler", 600), swap(2, "Boom Jet", 1200)]),
            "D": "\n".join([connected(2), body(1, "Wash Buckler", 1, 610), body(1, "Boom Jet", 2, 1220)]),
        }
        graded, why = run(texts, {"swappers": ["H"], "swaps": 2, "receivers": ["D"]})
        self.assertEqual(why, [])
        self.assertEqual(graded["expected"], 2)
        self.assertEqual(graded["graded"], 2)

    def test_empty_logs_fail(self) -> None:
        _, why = run({"S": "", "H": "", "D": ""}, {"swappers": ["H"], "swaps": 1})
        self.assertTrue(any("never connected" in w for w in why))

    def test_zero_swaps_fail(self) -> None:
        texts = {"S": "", "H": connected(1), "D": connected(2)}
        _, why = run(texts, {"swappers": ["H"], "swaps": 1, "receivers": ["D"]})
        self.assertTrue(any("logged 0 swap(s), the spec orders 1" in w for w in why))
        self.assertTrue(any("not reached" in w for w in why))

    def test_fewer_swaps_than_ordered_fail(self) -> None:
        texts = {
            "S": "",
            "H": "\n".join([connected(1), swap(1, "Wash Buckler", 600)]),
            "D": "\n".join([connected(2), body(1, "Wash Buckler", 1, 610)]),
        }
        _, why = run(texts, {"swappers": ["H"], "swaps": 2, "receivers": ["D"]})
        self.assertTrue(any("logged 1 swap(s), the spec orders 2" in w for w in why))

    def test_missing_receiver_pair_fails(self) -> None:
        texts = {"S": "", "H": "\n".join([connected(1), swap(1, "Wash Buckler", 600)]), "D": connected(2)}
        _, why = run(texts, {"swappers": ["H"], "swaps": 1, "receivers": ["D"]})
        self.assertTrue(any("missing" in w for w in why))

    def test_slow_pair_fails(self) -> None:
        texts = {
            "S": "",
            "H": "\n".join([connected(1), swap(1, "Wash Buckler", 600)]),
            "D": "\n".join([connected(2), body(1, "Wash Buckler", 1, 600 + 31)]),  # 516 ms at 60 Hz
        }
        _, why = run(texts, {"swappers": ["H"], "swaps": 1, "receivers": ["D"]})
        self.assertTrue(any("max 500" in w for w in why))

    def test_spec_checks(self) -> None:
        for bad in ({"swaps": 1}, {"swappers": ["S"], "swaps": 1}, {"swappers": ["H"], "swaps": 0},
                    {"swappers": ["H"], "swaps": 1, "receivers": ["H"]}, {"swappers": ["H"], "swaps": 1, "x": 1},
                    {"swappers": ["H"], "swaps": True}):
            with self.assertRaises(rsg.RemoteSwapSpecError):
                rsg.check_remote_swap("spec.toml", bad, NAMES)
        rsg.check_remote_swap("spec.toml", {"swappers": ["H", "D"], "swaps": 10}, NAMES)


if __name__ == "__main__":
    unittest.main()
