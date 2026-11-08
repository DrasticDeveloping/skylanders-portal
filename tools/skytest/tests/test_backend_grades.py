"""Tests for tools/skytest/backend_grades.py on recorded lines.
Run: python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend_grades as bg  # noqa: E402

D0 = "0" * 16
D1 = "1" * 16
GATE = "a00000a36.601af314.ada5591b@3"
RING = "a00000b10.11111111.22222222@3"


def local(peer: int, kind: str, entity: str, rev: int, digest: str, result: str, extra: str = "") -> str:
    return f"[12:00:00.000] i> 0001 Online: backend local {peer} {kind} {entity} slot 0 rev {rev} digest {digest} {result}{extra}"


class Compare(unittest.TestCase):
    def test_equal_and_repaired_pass(self):
        texts = {
            "S": "\n".join([local(0, "gate", GATE, 2, D1, "equal"), local(0, "prop", GATE, 1, D0, "equal")]),
            "H": "\n".join([local(1, "gate", GATE, 2, D1, "equal"), local(1, "prop", GATE, 1, D0, "repaired")]),
            "D": "\n".join([local(2, "gate", GATE, 2, D1, "equal"), local(2, "prop", GATE, 1, D0, "equal")]),
        }
        graded, reasons = bg.grade_compare(texts, {"kinds": ["gate", "prop"]})
        self.assertEqual(reasons, [])
        self.assertEqual(graded["rows"], 4)
        self.assertEqual(graded["repaired"], 1)

    def test_behind_stuck_and_pending(self):
        texts = {
            "S": "\n".join([local(0, "gate", GATE, 3, D1, "equal"), local(0, "prop", RING, 1, D0, "equal")]),
            "H": "\n".join([local(1, "gate", GATE, 2, D0, "equal"),
                            local(1, "prop", RING, 1, D0, "pending", " pending 1200")]),
        }
        graded, reasons = bg.grade_compare(texts, {"kinds": ["gate", "prop"]})
        self.assertEqual(len(reasons), 2)
        self.assertIn("replica behind", reasons[0])
        self.assertIn("pending", reasons[1])
        # pending allowed for prop: only the gate fails
        _, reasons = bg.grade_compare(texts, {"kinds": ["gate", "prop"], "pending": ["prop"]})
        self.assertEqual(len(reasons), 1)

    def test_last_line_counts_and_not_reached(self):
        texts = {
            "S": local(0, "gate", GATE, 2, D1, "equal"),
            "H": "\n".join([local(1, "gate", GATE, 1, D0, "stuck"), local(1, "gate", GATE, 2, D1, "equal")]),
        }
        self.assertEqual(bg.grade_compare(texts, {"kinds": ["gate"]})[1], [])
        reasons = bg.grade_compare(texts, {"kinds": ["kill_count"]})[1]
        self.assertTrue(reasons and "not reached" in reasons[0])
        reasons = bg.grade_compare({"S": local(0, "gate", GATE, 2, D1, "equal")}, {"kinds": ["gate"]})[1]
        self.assertTrue(reasons and "no client" in reasons[0])

    def test_unsupported_only_when_listed(self):
        texts = {"S": local(0, "prop", GATE, 1, D0, "unsupported"), "H": local(1, "prop", GATE, 1, D0, "unsupported")}
        self.assertEqual(len(bg.grade_compare(texts, {"kinds": ["prop"]})[1]), 1)
        self.assertEqual(bg.grade_compare(texts, {"kinds": ["prop"], "unsupported": ["prop"]})[1], [])


class Faults(unittest.TestCase):
    LOG = "\n".join([
        "Online: backend fault 1 armed apply:hold kind=gate key=x newer nth=1 ms=8000",
        "Online: backend fault 2 message:kill peer 0 s2c state delta gate x slot 0",
        "compare window opens",
        "Online: backend fault 1 apply:hold peer 0 gate x 8000 ms",
    ])

    def test_fired_and_before(self):
        graded, reasons = bg.grade_faults(self.LOG, {"rules": [1, 2]})
        self.assertEqual(reasons, [])
        self.assertEqual(graded["fired"], [1, 2])
        reasons = bg.grade_faults(self.LOG, {"rules": [1, 2], "before": "compare window opens"})[1]
        self.assertEqual(len(reasons), 1)
        self.assertIn("rule 1 fired after", reasons[0])

    def test_armed_is_not_fired(self):
        reasons = bg.grade_faults("Online: backend fault 3 armed link:full ms=10", {"rules": [3]})[1]
        self.assertTrue(reasons and "never fired" in reasons[0])


def memory(peer: int, log: int, out: int = 0) -> str:
    return (f"Online: backend memory {peer} log {log} staging 0 batcher 1 held 0 bridge_out {out} bridge_in 10 "
            f"obligations 0 requests 0")


class Memory(unittest.TestCase):
    def test_bounds_and_steady(self):
        text = "\n".join(memory(1, 100 + (i % 3)) for i in range(30))
        graded, reasons = bg.grade_memory(text, {"bounds": {"log": 200}, "window": 10})
        self.assertEqual(reasons, [])
        self.assertEqual(graded["max"]["1"]["log"], 102)
        reasons = bg.grade_memory(text, {"bounds": {"log": 101}, "window": 10})[1]
        self.assertTrue(reasons and "over its bound" in reasons[0])

    def test_rising(self):
        text = "\n".join(memory(1, 100 + i) for i in range(30))
        reasons = bg.grade_memory(text, {"window": 10})[1]
        self.assertTrue(any("log rising" in r for r in reasons))

    def test_not_reached_and_from(self):
        reasons = bg.grade_memory("\n".join(memory(1, 1) for _ in range(12)), {"window": 10})[1]
        self.assertTrue(reasons and "not reached" in reasons[0])
        text = "\n".join([memory(1, 999)] + ["steady"] + [memory(1, 5) for _ in range(30)])
        graded, reasons = bg.grade_memory(text, {"bounds": {"log": 10}, "from": "steady"})
        self.assertEqual(reasons, [])
        self.assertEqual(graded["max"]["1"]["log"], 5)


class Check(unittest.TestCase):
    def test_refusals(self):
        names = ["S", "H"]
        bg.check("x.toml", "backend_compare", {"kinds": ["gate"]}, names)
        bg.check("x.toml", "backend_faults", {"rules": [1]}, names)
        bg.check("x.toml", "backend_memory", {"bounds": {"log": 1}}, names)
        for gname, g in [("backend_compare", {}), ("backend_compare", {"kinds": ["gate"], "peer": "D"}),
                         ("backend_faults", {"rules": [0]}), ("backend_memory", {"bounds": {"nope": 1}}),
                         ("backend_memory", {"window": 0}), ("backend_compare", "x")]:
            with self.assertRaises(bg.GradeSpecError):
                bg.check("x.toml", gname, g, names)


if __name__ == "__main__":
    unittest.main()
