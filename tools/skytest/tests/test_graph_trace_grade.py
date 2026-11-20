"""Tests for the graph trace grade (graph_trace_grade.py): an encounter's own spawned row tied to repeated samples
on the client and the server, over overlapping non-zero ticks inside the client's window.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_graph_trace_grade.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import graph_trace_grade as gt  # noqa: E402

ROW_FROM = r"Online: enemy spawn (\S+) as ECI_Manager_actor_\d+#[0-9a-f]+ \(live\)"
AFTER = r"Input script: TRACE_SHIP_START"
WIN_FROM = r"Input script: TRACE_SHIP_STAND"
WIN_TO = r"Input script: TRACE_SHIP_DONE"


def line(t: str, text: str) -> str:
    return f"[2026-11-19 20:00:{t}] [info] [core] [t1] {text}"


CLIENT = "\n".join([
    line("00.000", "Online: enemy spawn sd1.00000001#1/0@1 as ECI_Manager_actor_0#b78d66e0 (live)"),
    line("00.100", "Online: enemy spawn s99.00000001#1/0@1 as Other_actor_0#b78d0000 (live)"),
    line("01.000", "Input script: TRACE_SHIP_STAND"),
    line("02.000", "Online: graph trace copy sd1.00000001#1/0@1 tick 9000 states none clip none pos 0,0,0"),
    line("03.000", "Online: graph trace copy sd1.00000001#1/0@1 tick 9060 states none clip none pos 0,0,0"),
    line("03.500", "Online: graph trace copy s99.00000001#1/0@1 tick 9090 states none clip none pos 0,0,0"),
    line("21.000", "Input script: TRACE_SHIP_DONE"),
])
SERVER = "\n".join([
    line("02.100", "Online: graph trace enemy sd1.00000001#1/0@1 tick 9010 states none clip none pos 0,0,0 target"),
    line("03.100", "Online: graph trace enemy sd1.00000001#1/0@1 tick 9070 states none clip none pos 0,0,0 target"),
])


class GraphTraceGrade(unittest.TestCase):
    def test_tied(self):
        record, not_reached, failure = gt.grade(CLIENT, SERVER, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertEqual(not_reached, "")
        self.assertEqual(failure, "")
        self.assertEqual([t["row"] for t in record["tied"]], ["sd1.00000001#1/0@1"])
        self.assertEqual(record["row"], "sd1.00000001#1/0@1")  # the first spawn after the start, not sd0

    def test_unrelated_server_enemy_does_not_tie(self):
        server = SERVER.replace("sd1.00000001#1/0@1", "sd2.00000001#1/0@1")
        _, not_reached, _ = gt.grade(CLIENT, server, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("did not have", not_reached)

    def test_other_rows_copy_does_not_count(self):
        client = CLIENT.replace("copy sd1.00000001#1/0@1 tick 9060", "copy s99.00000001#1/0@1 tick 9060")
        _, not_reached, _ = gt.grade(client, SERVER, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("did not have", not_reached)

    def test_ticks_must_overlap_and_be_non_zero(self):
        server = SERVER.replace("tick 9010", "tick 20000").replace("tick 9070", "tick 20060")
        _, not_reached, _ = gt.grade(CLIENT, server, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("did not have", not_reached)
        client = CLIENT.replace("tick 9000", "tick 0").replace("tick 9060", "tick 0")
        _, not_reached, _ = gt.grade(client, SERVER, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("did not have", not_reached)

    def test_samples_outside_the_window_do_not_count(self):
        client = CLIENT.replace("[2026-11-19 20:00:03.000]", "[2026-11-19 20:00:22.000]")
        _, not_reached, _ = gt.grade(client, SERVER, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("did not have", not_reached)

    def test_no_window_or_row(self):
        _, not_reached, _ = gt.grade(CLIENT.replace("TRACE_SHIP_DONE", "x"), SERVER, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("window", not_reached)
        _, not_reached, _ = gt.grade("", SERVER, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("no `row_from` line after", not_reached)

    def test_a_row_from_before_the_start_is_not_graded(self):
        server = SERVER.replace("sd1.00000001#1/0@1", "sd0.00000001#1/0@1")
        client = CLIENT.replace("copy sd1.", "copy sd0.")
        _, not_reached, _ = gt.grade(client, server, ROW_FROM, AFTER, WIN_FROM, WIN_TO)
        self.assertIn("sd1.00000001#1/0@1 did not have", not_reached)

    def test_check_spec(self):
        ok = {"row_from": ROW_FROM, "rows_after": AFTER, "window_from": WIN_FROM, "window_to": WIN_TO}
        self.assertEqual(gt.check_spec(ok, ["H", "S"]), "")
        self.assertIn("one group", gt.check_spec({**ok, "row_from": r"Online: enemy spawn \S+"}, ["H", "S"]))
        self.assertIn("unknown keys", gt.check_spec({**ok, "peer": "H"}, ["H", "S"]))
        self.assertIn("not in", gt.check_spec({**ok, "client": "D"}, ["H", "S"]))
        self.assertIn("min_samples", gt.check_spec({**ok, "min_samples": 0}, ["H", "S"]))


if __name__ == "__main__":
    unittest.main()
