"""The acceptance list's estimate and page (acceptance.py): the parts that can break without a game."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import acceptance  # noqa: E402


class Acceptance(unittest.TestCase):
    def test_estimate_from_history_else_the_spec_timeout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skytest-acceptance-") as d:
            root = Path(d)
            (root / "tests" / "runtime").mkdir(parents=True)
            (root / "tests" / "runtime" / "b-case.toml").write_text('name = "b-case"\ntimeout = 300\n', encoding="utf-8")
            runs = root / "runs"
            for i, (secs, status) in enumerate([(100, "pass"), (300, "fail"), (5, "not ready")]):
                f = runs / f"2026112{i}-000000-a-case"
                f.mkdir(parents=True)
                (f / "result.json").write_text(json.dumps({"seconds": secs, "status": status}), encoding="utf-8")
            est = acceptance.estimate(root, runs, ["a-case", "b-case", "c-case"], ["tutorial_a"])
            self.assertEqual(est[0], ("a-case", 200.0, "median of 2 earlier run(s)"))
            self.assertEqual(est[1], ("b-case", 300.0, "its spec's timeout (no earlier run)"))
            self.assertEqual(est[2], ("c-case", None, "no spec in this tree"))
            acceptance.OWED["owed-case"] = "no spec yet (its author has not named it)"
            try:
                owed = acceptance.estimate(root, runs, ["owed-case"], [])
            finally:
                acceptance.OWED.pop("owed-case")
            self.assertEqual(owed[0][2], "no spec yet (its author has not named it)")
            self.assertIn("element-gate-dual-tb", acceptance.CASES)
            self.assertEqual(est[3][1], acceptance.LEVEL_DEFAULT_S)
            self.assertIn("estimated 23 min in all", acceptance.estimate_text(est))

    def test_page_rows_from_a_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skytest-acceptance-") as d:
            run = Path(d) / "20261120-010000-a-case"
            (run / "H").mkdir(parents=True)
            (run / "H" / "game.log").write_text("x\n", encoding="utf-8")
            (run / "result.json").write_text(json.dumps({"status": "fail", "reasons": ["peers failed: H"], "peers": [
                {"peer": "H", "reasons": ["log lacks /Online: coin taken here/"]}]}), encoding="utf-8")
            row = acceptance.case_row("a-case", run, "", 61.0)
            self.assertEqual(row.result, "FAIL")
            self.assertEqual(row.lines, ["peers failed: H", "H: log lacks /Online: coin taken here/"])
            self.assertIn(str(run / "H" / "game.log"), row.logs)
            text = acceptance.page_text([row, acceptance.case_row("b-case", None, "  NOT READY: waits\n", 0.0)],
                                        "est", "game x")
            self.assertIn("| a-case | FAIL | 1.0 min | peers failed: H<br>H: log lacks /Online: coin taken here/ |", text)
            self.assertIn("| b-case | no run | 0.0 min |   NOT READY: waits | - |", text)
            self.assertIn("0 of 2 passed; each case ran once.", text)


if __name__ == "__main__":
    unittest.main()
