"""Tests for stage 5's grades on recorded lines: the occurrence grade (occurrence_outcomes.py) and the
memory growth grade (memory_growth.py). Run: python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import memory_growth  # noqa: E402
import occurrence_outcomes  # noqa: E402
import skytest  # noqa: E402

REF = "s:1a2b3c4d.0.7.2"


def retired(seq: int, how: str = "delivered all", ref: str = REF) -> str:
    return f"[info] Online: attack {ref} #{seq} ({how})"


def expired(seq: int, peer: int = 1, ref: str = REF) -> str:
    return f"[warning] Online: attack {ref} #{seq} expired unconfirmed peer {peer}"


def outcome(seq: int, what: str, ref: str = REF) -> str:
    return f"[info] Online: attack {ref} #{seq} 9f3c1a20 start {100 + seq} ({what})"


def accepted(seq: int, ref: str = REF) -> str:
    return f"[info] Online: attack {ref} #{seq} accepted"


CONNECTED = "[info] Online: connected as peer 1 (server tick 10, level 2 tutorial/tutorial_b/tutorial_b)"


def client(*lines: str) -> str:
    return "\n".join((CONNECTED,) + lines)


class Occurrences(unittest.TestCase):
    def grade(self, s: list[str], h: list[str], **kw) -> occurrence_outcomes.Result:
        kw.setdefault("min_occurrences", 1)
        return occurrence_outcomes.grade_occurrences("\n".join(s), {"H": client(*h)}, **kw)

    def test_one_outcome_each_passes(self) -> None:
        s = [retired(1), retired(2), retired(3, "claims ended")]
        h = [accepted(1), outcome(1, "forced at 101"), accepted(2), outcome(2, "late 3"),
             outcome(3, "forced at 103"), outcome(3, "duplicate")]
        r = self.grade(s, h)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual((r.occurrences, r.delivered_all), (3, 2))
        self.assertEqual(r.counts["H"], {"forced": 2, "late": 1, "duplicate": 1})

    def test_missing_and_double_outcomes_fail(self) -> None:
        r = self.grade([retired(1), retired(2)], [outcome(1, "forced at 101"), outcome(1, "late 2")])
        self.assertFalse(r.passed)
        self.assertTrue(any("#1 has 2 outcomes" in f for f in r.failures), r.failures)
        self.assertTrue(any("#2 has no outcome" in f for f in r.failures), r.failures)

    def test_missed_and_undelivered_are_limited(self) -> None:
        s = [retired(1), retired(2), retired(3)]
        h = [outcome(1, "missed"), outcome(2, "missed (no telegraph)"), outcome(3, "undelivered")]
        r = self.grade(s, h)
        self.assertFalse(r.passed)
        self.assertTrue(any("2 missed" in f for f in r.failures), r.failures)
        self.assertTrue(any("1 undelivered" in f for f in r.failures), r.failures)
        self.assertTrue(self.grade(s, h, max_missed=2, max_undelivered=1).passed)

    def test_expected_unconfirmed_with_an_owned_entry_passes(self) -> None:
        # P4 (e): H held its accepts past the expiry; S expired the key, H still forced it once.
        s = [retired(1), expired(2)]
        h = [accepted(1), outcome(1, "forced at 101"), accepted(2), outcome(2, "forced at 102")]
        r = self.grade(s, h, expected_unconfirmed={1: 1})
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.expired, {1: 1})

    def test_telegraph_missed_is_the_one_outcome_and_a_miss(self) -> None:
        # The scheduler's kTelegraphMissed (the telegraph after L) is the key's outcome, not a missing one.
        r = self.grade([retired(1)], [accepted(1), outcome(1, "telegraph missed")])
        self.assertFalse(any("has no outcome" in f for f in r.failures), r.failures)
        self.assertTrue(any("1 missed" in f for f in r.failures), r.failures)
        self.assertTrue(self.grade([retired(1)], [accepted(1), outcome(1, "telegraph missed")], max_missed=1).passed)

    def test_min_unconfirmed_for_a_held_hold(self) -> None:
        # P4 (e) in a live fight: any number of expiries, at least one, each for a key H accepted.
        s = [retired(1), expired(2), expired(3)]
        h = [accepted(1), outcome(1, "forced at 101"), accepted(2), outcome(2, "forced at 102"),
             accepted(3), outcome(3, "late 2")]
        self.assertTrue(self.grade(s, h, min_unconfirmed={1: 1}).passed)
        r = self.grade([retired(1)], [outcome(1, "forced at 101")], min_unconfirmed={1: 1})
        self.assertTrue(any("0 expired unconfirmed line(s) for peer 1, at least 1" in f for f in r.failures))
        # An expiry of a key H never accepted is a lost row, not the hold.
        r = self.grade([expired(1)], [outcome(1, "undelivered")], min_unconfirmed={1: 1}, max_undelivered=1)
        self.assertTrue(any("without its `accepted` line" in f for f in r.failures), r.failures)

    def test_unexpected_expiry_fails(self) -> None:
        r = self.grade([expired(1)], [accepted(1), outcome(1, "late 4")])
        self.assertFalse(r.passed)
        self.assertTrue(any("1 expired unconfirmed line(s) for peer 1, expected 0" in f for f in r.failures))

    def test_accepted_then_undelivered_fails(self) -> None:
        r = self.grade([expired(1)], [accepted(1), outcome(1, "undelivered")],
                       expected_unconfirmed={1: 1}, max_undelivered=1)
        self.assertFalse(r.passed)
        self.assertTrue(any("accepted, then undelivered" in f for f in r.failures), r.failures)
        # Never accepted: S's expiry and the client's undelivered agree (a lost row).
        self.assertTrue(self.grade([expired(1)], [outcome(1, "undelivered")],
                                   expected_unconfirmed={1: 1}, max_undelivered=1).passed)

    def test_contradictory_retirements_fail(self) -> None:
        r = self.grade([retired(1), expired(1)], [outcome(1, "forced at 101")], expected_unconfirmed={1: 1})
        self.assertTrue(any("both delivered all and expired" in f for f in r.failures), r.failures)
        r = self.grade([retired(1), retired(1)], [outcome(1, "forced at 101")])
        self.assertTrue(any("retired twice" in f for f in r.failures), r.failures)

    def test_empty_trace_is_not_reached(self) -> None:
        r = occurrence_outcomes.grade_occurrences("", {"H": client()})
        self.assertFalse(r.passed)
        self.assertIn("0 attack occurrence(s)", r.not_reached)
        r = self.grade([retired(1)], [outcome(1, "forced at 101")], min_occurrences=20)
        self.assertFalse(r.passed)

    def test_unretired_outcomes_are_counted_not_graded(self) -> None:
        r = self.grade([retired(1)], [outcome(1, "forced at 101"), outcome(2, "forced at 102")])
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.unretired["H"], 1)

    def test_incarnations_are_separate_keys(self) -> None:
        other = "s:1a2b3c4d.0.7.3"
        r = self.grade([retired(1), retired(1, ref=other)], [outcome(1, "forced at 101"), outcome(1, "late 1", ref=other)])
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.occurrences, 2)

    def test_client_without_a_connection_line_fails(self) -> None:
        r = occurrence_outcomes.grade_occurrences(retired(1), {"H": outcome(1, "forced at 101")}, min_occurrences=1)
        self.assertTrue(any("no `connected as peer` line" in f for f in r.failures), r.failures)


def frames(minute: float, mb: float, base: str = "2026-11-08 10:00") -> str:
    total = int(round(minute * 60))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    hh, mm = base.split(" ")[1].split(":")
    stamp = f"2026-11-08 {int(hh) + h:02d}:{int(mm) + m:02d}:{s:02d}.000"
    return (f"[{stamp}] [info] [core] [t1] Online: frames 600 fps 60.0 p50 16.7 p95 17.1 max 20.3 "
            f"private {int(round(mb))} working 900 video 300")


def soak(mb_at) -> str:
    lines = ["[2026-11-08 10:00:00.000] [info] [core] [t1] enemy-soak live segment"]
    lines += [frames(i / 6.0, mb_at(i / 6.0)) for i in range(1, 6 * 21)]  # every 10 s for 21 minutes
    return "\n".join(lines)


class MemoryGrowth(unittest.TestCase):
    def test_flat_passes(self) -> None:
        r = memory_growth.grade_memory_growth(soak(lambda m: 1500.0), "enemy-soak live segment")
        self.assertTrue(r.passed, r.failure or r.not_reached)
        self.assertEqual(r.samples, 91)
        self.assertAlmostEqual(r.growth_mb_per_10min, 0.0)

    def test_growth_fails(self) -> None:
        r = memory_growth.grade_memory_growth(soak(lambda m: 1500.0 + 0.5 * m), "enemy-soak live segment")
        self.assertFalse(r.passed)
        self.assertAlmostEqual(r.growth_mb_per_10min, 5.0, delta=0.2)
        self.assertIn("grows", r.failure)

    def test_growth_before_minute_5_does_not_count(self) -> None:
        r = memory_growth.grade_memory_growth(soak(lambda m: 1500.0 + (40.0 * m if m < 5 else 200.0)),
                                              "enemy-soak live segment")
        self.assertTrue(r.passed, r.failure)

    def test_no_anchor_or_short_trace_is_not_reached(self) -> None:
        r = memory_growth.grade_memory_growth(soak(lambda m: 1500.0), "no such line")
        self.assertFalse(r.passed)
        self.assertIn("no line matches", r.not_reached)
        short = "\n".join(soak(lambda m: 1500.0).splitlines()[:40])
        r = memory_growth.grade_memory_growth(short, "enemy-soak live segment")
        self.assertFalse(r.passed)
        self.assertIn("at least 30 needed", r.not_reached)


class RunnerSide(unittest.TestCase):
    """The runner's side: both grades are known to skytest, their options checked at load, and their
    grade_spec records and reasons."""

    HEAD = ['name = "case"', 'scenes = "skip"', 'layout = "server+1"', 'stage = "5"', 'server = "host"',
            '[peer.H]', "script = 'quit'"]

    def load(self, *body: str):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "case.toml"
            p.write_text("\n".join(self.HEAD + list(body)) + "\n", encoding="utf-8")
            return skytest.load_spec(p, 20)

    def test_known_and_checked_at_load(self) -> None:
        spec = self.load("[grade.occurrence_outcomes]", 'expected_unconfirmed = { "1" = 1 }',
                         "[grade.memory_growth]", 'anchor = "live segment"', 'peers = ["S", "H"]')
        self.assertIn("occurrence_outcomes", spec.grades)
        self.assertIn("memory_growth", spec.grades)
        for bad in (["[grade.occurrence_outcomes]", 'clients = ["S"]'],
                    ["[grade.occurrence_outcomes]", "min_occurrences = -1"],
                    ["[grade.occurrence_outcomes]", 'expected_unconfirmed = { "H" = 1 }'],
                    ["[grade.occurrence_outcomes]", 'expected_unconfirmed = { "1" = 1 }', 'min_unconfirmed = { "1" = 1 }'],
                    ["[grade.occurrence_outcomes]", "what = 1"],
                    ["[grade.memory_growth]", 'peers = ["H"]'],
                    ["[grade.memory_growth]", 'anchor = "x"', "start_min = 20", "end_min = 5"]):
            with self.assertRaises(skytest.SpecError, msg=str(bad)):
                self.load(*bad)

    def test_occurrence_grade_spec(self) -> None:
        texts = {"S": "\n".join([retired(1), expired(2)]),
                 "H": client(accepted(1), outcome(1, "forced at 101"), accepted(2), outcome(2, "late 2"))}
        record, reasons = occurrence_outcomes.grade_spec(texts, {"min_occurrences": 2,
                                                                 "expected_unconfirmed": {"1": 1}}, ["H", "S"])
        self.assertEqual(reasons, [])
        self.assertEqual((record["occurrences"], record["expired"]), (2, {"1": 1}))
        record, reasons = occurrence_outcomes.grade_spec(texts, {}, ["H", "S"])
        self.assertTrue(any(r.startswith("grade occurrence_outcomes: not reached") for r in reasons), reasons)
        self.assertTrue(any("expected 0" in r for r in reasons), reasons)

    def test_memory_grade_spec_takes_the_anchor_from_its_peer(self) -> None:
        lines = soak(lambda m: 1500.0).splitlines()
        texts = {"H": lines[0], "S": "\n".join(lines[1:])}
        record, reasons = memory_growth.grade_spec(texts, {"anchor": "enemy-soak live segment", "peers": ["S"]},
                                                   ["H", "S"])
        self.assertEqual(reasons, [])
        self.assertEqual(record["S"]["samples"], 91)
        texts["H"] = ""
        record, reasons = memory_growth.grade_spec(texts, {"anchor": "enemy-soak live segment", "peers": ["S"]},
                                                   ["H", "S"])
        self.assertTrue(reasons and "not reached" in reasons[0], reasons)


if __name__ == "__main__":
    unittest.main()
