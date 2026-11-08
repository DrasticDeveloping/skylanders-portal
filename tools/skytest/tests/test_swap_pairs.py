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


def grade(logs: dict[int, str], swaps=None, receivers=(1, 2), pressure=()) -> swap_pairs.Result:
    """One swap by peer 1 received by peer 2 unless the test says otherwise."""
    return swap_pairs.grade_swap_pairs(logs, {1: 1} if swaps is None else swaps, receivers, pressure)


def lines(*parts: str) -> str:
    return "\n".join(parts)


# One pressure pair: peer 1's swap 1 on receiver 2.
PRESSURE = [((1, 1), 2)]


class SwapPairs(unittest.TestCase):
    def test_both_directions_pass(self) -> None:
        logs = {
            1: lines(swap(1, "Wash Buckler", 600), body(2, "Magna Charge", "real", 1, 900)),
            2: lines(body(1, "Wash Buckler", "real", 1, 618), swap(1, "Magna Charge", 880)),
        }
        r = grade(logs, swaps={1: 1, 2: 1})
        self.assertTrue(r.passed, r.failures)
        self.assertEqual((r.expected, r.graded), (2, 2))
        self.assertAlmostEqual(r.max_lag_ms, 333.33, places=1)
        # The same swaps named by ref and by whole key.
        r = grade(logs, swaps=[(1, 1), (2, 1, 3, 1)])
        self.assertTrue(r.passed, r.failures)
        self.assertEqual((r.expected, r.graded), (2, 2))

    def test_slow_pair_fails(self) -> None:
        r = grade({1: swap(1, "Rubble Rouser", 600), 2: body(1, "Rubble Rouser", "real", 1, 631)})
        self.assertFalse(r.passed)
        self.assertIn("517 ms", r.failures[0])

    def test_missing_pair_fails(self) -> None:
        logs = {1: swap(1, "Rubble Rouser", 600), 2: body(1, "Rubble Rouser", "real", 1, 610), 3: ""}
        r = grade(logs, receivers=(1, 2, 3))
        self.assertEqual(r.expected, 2)
        self.assertEqual(r.graded, 1)
        self.assertTrue(any("missing" in f and "on peer 3" in f for f in r.failures))

    def test_duplicate_and_wrong_body_fail(self) -> None:
        dup = {1: swap(1, "A", 600), 2: lines(body(1, "A", "real", 1, 601), body(1, "A", "real", 1, 640))}
        self.assertTrue(any("duplicate" in f for f in grade(dup).failures))
        wrong = {1: swap(1, "A", 600), 2: body(1, "B", "real", 1, 601)}
        self.assertTrue(any("wrong body" in f for f in grade(wrong).failures))

    def test_early_preload_counts_as_zero(self) -> None:
        r = grade({1: swap(2, "A", 600), 2: body(1, "A", "real", 2, 580)}, swaps=[(1, 2)])
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.max_lag_ms, 0.0)

    def test_keys_never_mix_epochs_or_generations(self) -> None:
        # The receiver's line of another generation (or peer epoch) is not this key's completion.
        logs = {1: swap(1, "A", 600, gen=3), 2: lines(body(1, "A", "real", 1, 610, gen=2),
                                                      body(1, "A", "real", 1, 612, epoch=2))}
        r = grade(logs)
        self.assertTrue(any("missing" in f for f in r.failures))

    # --- F4: the expected work comes from the case, never from the logs alone ---------------------

    def test_empty_input_fails(self) -> None:
        r = grade({})
        self.assertFalse(r.passed)
        self.assertEqual((r.expected, r.graded), (1, 0))
        self.assertTrue(any("missing reference" in f for f in r.failures), r.failures)
        self.assertTrue(any("receiver 2: no log" in f for f in r.failures), r.failures)

    def test_zero_swaps_fail(self) -> None:
        # Two client logs without a single swap: the case ordered one swap each.
        r = grade({1: "[info] Online: hello", 2: "[info] Online: hello"}, swaps={1: 1, 2: 1})
        self.assertFalse(r.passed)
        self.assertEqual((r.expected, r.graded), (2, 0))
        self.assertEqual(sum("missing reference" in f for f in r.failures), 2, r.failures)

    def test_fewer_swaps_than_ordered_fail(self) -> None:
        logs = {1: swap(1, "A", 600), 2: body(1, "A", "real", 1, 610)}
        r = grade(logs, swaps={1: 2})
        self.assertFalse(r.passed)
        self.assertEqual((r.expected, r.graded), (2, 1))
        self.assertTrue(any("2 swap(s) ordered, 1 logged" in f for f in r.failures), r.failures)

    def test_missing_reference_fails(self) -> None:
        # The receiver shows the body, but the swapper never logged the swap the case ordered.
        logs = {1: "", 2: body(1, "A", "real", 1, 610)}
        for swaps in ({1: 1}, [(1, 1)], [(1, 1, 3, 1)]):
            r = grade(logs, swaps=swaps)
            self.assertFalse(r.passed, swaps)
            self.assertEqual((r.expected, r.graded), (1, 0))
            self.assertTrue(any("missing reference" in f for f in r.failures), r.failures)
        # A swap ref whose epoch or generation differs from the logged swap is missing too.
        r = grade({1: swap(1, "A", 600), 2: body(1, "A", "real", 1, 610)}, swaps=[(1, 2, 3, 1)])
        self.assertTrue(any("missing reference" in f for f in r.failures), r.failures)

    def test_missing_receiver_fails(self) -> None:
        # Receiver 3's log is absent: its pair counts as expected and is never graded.
        logs = {1: swap(1, "A", 600), 2: body(1, "A", "real", 1, 610)}
        r = grade(logs, receivers=(1, 2, 3))
        self.assertFalse(r.passed)
        self.assertEqual((r.expected, r.graded), (2, 1))
        self.assertTrue(any("receiver 3: no log" in f for f in r.failures), r.failures)

    def test_unordered_swap_fails(self) -> None:
        logs = {1: lines(swap(1, "A", 600), swap(2, "B", 700)),
                2: lines(body(1, "A", "real", 1, 610), body(1, "B", "real", 2, 710))}
        for swaps in ({1: 1}, [(1, 1)]):
            r = grade(logs, swaps=swaps)
            self.assertFalse(r.passed)
            self.assertTrue(any("not ordered by the case" in f and "1, 1, 3, 2" in f for f in r.failures),
                            r.failures)

    def test_case_without_work_is_a_spec_error(self) -> None:
        logs = {1: swap(1, "A", 600), 2: body(1, "A", "real", 1, 610)}
        with self.assertRaises(ValueError):
            grade(logs, swaps={})
        with self.assertRaises(ValueError):
            grade(logs, swaps=[])
        with self.assertRaises(ValueError):
            grade(logs, receivers=())
        with self.assertRaises(ValueError):
            grade(logs, receivers=(1,))  # the swapper is its own only receiver
        with self.assertRaises(ValueError):
            grade(logs, swaps=[(1, 1), (1, 1)])
        with self.assertRaises(ValueError):
            grade(logs, pressure=[((1, 1), 3)])  # not a receiver

    # --- F5: pressure pairs come from the case and need stand-in, slot free, real ----------------

    def test_pressure_pair(self) -> None:
        ok = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 620), freed(1, 1, 1200),
                                             body(1, "E", "real", 1, 1225))}
        r = grade(ok, pressure=PRESSURE)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.pressure_pairs, 1)
        slow_stand = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 640), freed(1, 1, 1200),
                                                     body(1, "E", "real", 1, 1210))}
        self.assertTrue(any("stand-in after" in f for f in grade(slow_stand, pressure=PRESSURE).failures))
        slow_real = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 610), freed(1, 1, 1200),
                                                    body(1, "E", "real", 1, 1240))}
        self.assertTrue(any("after the slot freed" in f for f in grade(slow_real, pressure=PRESSURE).failures))
        none = {1: swap(1, "E", 600), 2: lines(freed(1, 1, 1200), body(1, "E", "real", 1, 1210))}
        self.assertTrue(any("no stand-in" in f for f in grade(none, pressure=PRESSURE).failures))

    def test_pressure_reversed_order_fails(self) -> None:
        # The review's example: the real body before the stand-in and the slot free.
        logs = {1: swap(1, "E", 600), 2: lines(body(1, "E", "real", 1, 605), body(1, "C", "stand-in", 1, 610),
                                               freed(1, 1, 620))}
        r = grade(logs, pressure=PRESSURE)
        self.assertFalse(r.passed)
        self.assertEqual(r.graded, 0)
        self.assertTrue(any("out of order" in f for f in r.failures), r.failures)
        # The slot free before the stand-in.
        logs = {1: swap(1, "E", 600), 2: lines(freed(1, 1, 610), body(1, "C", "stand-in", 1, 615),
                                               body(1, "E", "real", 1, 620))}
        self.assertTrue(any("out of order" in f for f in grade(logs, pressure=PRESSURE).failures))

    def test_pressure_equal_ticks_follow_line_order(self) -> None:
        in_order = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 610), freed(1, 1, 610),
                                                   body(1, "E", "real", 1, 610))}
        r = grade(in_order, pressure=PRESSURE)
        self.assertTrue(r.passed, r.failures)
        reversed_lines = {1: swap(1, "E", 600), 2: lines(body(1, "E", "real", 1, 610), freed(1, 1, 610),
                                                         body(1, "C", "stand-in", 1, 610))}
        self.assertTrue(any("out of order" in f for f in grade(reversed_lines, pressure=PRESSURE).failures))

    def test_pressure_duplicates_fail(self) -> None:
        two_stand = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 610),
                                                    body(1, "D", "stand-in", 1, 612),
                                                    freed(1, 1, 620), body(1, "E", "real", 1, 630))}
        r = grade(two_stand, pressure=PRESSURE)
        self.assertFalse(r.passed)
        self.assertTrue(any("duplicate stand-in" in f for f in r.failures), r.failures)
        two_freed = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 610), freed(1, 1, 620),
                                                    freed(1, 1, 622), body(1, "E", "real", 1, 630))}
        r = grade(two_freed, pressure=PRESSURE)
        self.assertFalse(r.passed)
        self.assertTrue(any("duplicate slot-free" in f for f in r.failures), r.failures)

    def test_pressure_absent_release_fails(self) -> None:
        # A pressure pair without a slot-free line never falls through to the ordinary grade.
        logs = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 610), body(1, "E", "real", 1, 620))}
        r = grade(logs, pressure=PRESSURE)
        self.assertFalse(r.passed)
        self.assertEqual(r.graded, 0)
        self.assertTrue(any("missing release" in f for f in r.failures), r.failures)

    def test_slot_free_outside_pressure_fails(self) -> None:
        logs = {1: swap(1, "E", 600), 2: lines(body(1, "C", "stand-in", 1, 610), freed(1, 1, 620),
                                               body(1, "E", "real", 1, 630))}
        r = grade(logs)
        self.assertFalse(r.passed)
        self.assertTrue(any("does not mark as pressure" in f for f in r.failures), r.failures)


if __name__ == "__main__":
    unittest.main()
