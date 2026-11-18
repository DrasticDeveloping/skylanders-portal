"""Tests for the level pass's predictions mode (levelpass_predictions.py) on made-up recipes and logs: the
recipe reader, the scripts (no position, one mark per bug), the snapshots per bug on H, S and D, D's
positions, and each check's verdict.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_levelpass_predictions.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import levelpass as lp  # noqa: E402
import levelpass_predictions as lpp  # noqa: E402

RECIPES = """
[level.madeup]
d = "early"

[[bug]]
n = 3
level = "madeup"
title = "blocks move only for the pusher"
steps = ["lean_into Block_A 6"]
snap = ["Block_A", "Clip_A"]
checks = ["position_differs Block_A 50", "exists_differs Clip_A"]

[[bug]]
n = 8
level = "madeup"
title = "a teleport pulls everyone"
steps = ["enter_trigger auth:madeup:12"]
checks = ["d_jump 1000"]

[[bug]]
n = 13
level = "madeup"
title = "needs a cross-client order"
could_not_test = "no cross-client step order"
"""


def line(t: str, text: str) -> str:
    return f"[2026-11-18 10:{t}.000] [info] [core] [t1] {text}"


H = "\n".join([
    line("00:00", "Input script: LEVELPASS pred 3 begin"),
    line("00:05", "Dev: lean_into Block_A done (Block_A#7 moved 300 from 1,1,1 to 301,1,1 after 20 pushes)"),
    line("00:06", "Input script: LEVELPASS pred 3 snap"),
    line("00:06", "Online: entity 7 Block_A class CPhysicalEntity at 301.0,1.0,1.0 team 26"),
    line("00:07", "Online: entities 1 listed"),
    line("00:08", "Online: entities 0 listed"),
    line("00:09", "Input script: LEVELPASS pred 3 end"),
    line("00:10", "Input script: LEVELPASS pred 8 begin"),
    line("00:12", "Input script: LEVELPASS pred 8 snap"),
    line("00:12", "Input script: LEVELPASS pred 8 end"),
    line("00:13", "Input script: LEVELPASS pred 13 skipped"),
])
S = "\n".join([
    line("00:06", "Online: the owner asked for the entity list: Block_A"),
    line("00:06", "Online: entity 9 Block_A class CPhysicalEntity at 1.0,1.0,1.0 team 26"),
    line("00:07", "Online: the owner asked for the entity list: Clip_A"),
    line("00:07", "Online: entity 10 Clip_A class CDynamicClipEntity at 5.0,5.0,5.0 team 26"),
])
D = "\n".join([
    line("00:09", "Where: madeup at 0.0 0.0 0.0"),
    line("00:10", "Where: madeup at 0.0 0.0 0.0"),
    line("00:12", "Online: where madeup at 5000.0,0.0,0.0"),  # the trace's own form
])


class Recipes(unittest.TestCase):
    def test_load(self):
        r = lpp.load_recipes(RECIPES)
        self.assertEqual([b.n for b in r.bugs], [3, 8, 13])
        self.assertEqual(r.d_mode, {"madeup": "early"})
        with self.assertRaises(ValueError):
            lpp.load_recipes('[[bug]]\nn = 1\nlevel = "x"\nchecks = ["guess X"]\n')
        with self.assertRaises(ValueError):
            lpp.load_recipes('[level.x]\nd = "sometimes"\n')
        with self.assertRaises(ValueError):
            lpp.load_recipes('[[bug]]\nn = 1\nlevel = "x"\nsteps = ["interact A;B"]\n')

    def test_scripts(self):
        r = lpp.load_recipes(RECIPES)
        h, d = lpp.scripts("madeup", r.bugs, "early")
        self.assertEqual(lp.position_literals(h) + lp.position_literals(d), [])
        self.assertIn("log LEVELPASS pred 3 begin", h)
        self.assertIn("online_server_dump_entities Clip_A", h)
        self.assertIn("log LEVELPASS pred 13 skipped", h)
        self.assertLess(h.index("LEVELPASS late-join start"), h.index("pred 3 begin"))
        self.assertLess(h.index("waitfor online_peer_shown"), h.index("pred 3 begin"))  # D in play first
        self.assertIn("waitfor online_left 1200", d)
        # Early D snapshots the bugs' names after the session, the world it watched H change.
        self.assertLess(d.index("waitfor online_left 1200"), d.index("dump_entities Block_A"))
        h2, d2 = lpp.scripts("madeup", r.bugs, "late")
        self.assertLess(h2.index("pred 8 end"), h2.index("LEVELPASS late-join start"))
        self.assertIn("dump_entities Block_A", d2)
        self.assertIsNone(lpp.scripts("madeup", r.bugs, "none")[1])


class Verdicts(unittest.TestCase):
    def test_each_check(self):
        r = lpp.load_recipes(RECIPES)
        rows = {b.n: (v, why) for b, v, why in lpp.verdicts(r.bugs, {"H": H, "S": S, "D": D})}
        v3, why3 = rows[3]
        self.assertEqual(v3, lpp.CONFIRMED)
        self.assertIn("apart 300", " ".join(why3))
        self.assertIn("Clip_A live on ['S'], not on ['H']", " ".join(why3))
        self.assertEqual(rows[8][0], lpp.CONFIRMED)  # D jumped 5000 within 8 s of the step
        self.assertEqual(rows[13], (lpp.COULD_NOT, ["no cross-client step order"]))
        text = lpp.verdict_text(lpp.verdicts(r.bugs, {"H": H, "S": S, "D": D}))
        self.assertIn("#3 CONFIRMED", text)

    def test_d_jump_window_ends_at_the_next_bug(self):
        """A jump of D after the next bug began is that bug's, not this one's."""
        dpos = [(100.0, (0.0, 0.0, 0.0)), (101.0, (0.0, 0.0, 0.0)), (104.0, (5000.0, 0.0, 0.0))]
        self.assertEqual(lpp.check_one("d_jump 1000", {}, 100.0, {}, dpos)[0], lpp.CONFIRMED)
        self.assertEqual(lpp.check_one("d_jump 1000", {}, 100.0, {}, dpos, until=103.0)[0], lpp.NOT_SEEN)

    def test_not_seen_and_could_not(self):
        b = lpp.Bug(n=1, level="x", title="t", steps=["s"], snap=["Block_A"], checks=["position_differs Block_A 500"])
        rows = lpp.verdicts([b], {"H": H.replace("pred 3", "pred 1"), "S": S})
        self.assertEqual(rows[0][1], lpp.NOT_SEEN)
        b2 = lpp.Bug(n=2, level="x", title="t", steps=["s"], snap=["Gone"], checks=["count_differs Gone"])
        rows2 = lpp.verdicts([b2], {"H": H.replace("pred 3", "pred 2"), "S": S})
        self.assertEqual(rows2[0][1], lpp.COULD_NOT)
        b3 = lpp.Bug(n=4, level="x", title="t", steps=["s"], checks=["log H moved 300"])
        rows3 = lpp.verdicts([b3], {"H": H.replace("pred 3", "pred 4"), "S": S})
        self.assertEqual(rows3[0][1], lpp.CONFIRMED)
        b4 = lpp.Bug(n=5, level="x", title="never reached", steps=["s"], checks=["log H x"])
        self.assertEqual(lpp.verdicts([b4], {"H": H, "S": S})[0][1], lpp.COULD_NOT)


if __name__ == "__main__":
    unittest.main()
