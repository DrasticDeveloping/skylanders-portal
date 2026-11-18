"""Tests for the live sessions' pure parts (live_session.py): splitting a send, the numbers the game gives
appended lines, finding a line's start, end or refusal in a log, the idle stop, and the files a session
keeps. The game's own reader (complete lines only, numbering, appended while read) is tested in C++
(tests/live_lines_test.cpp).
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_live_session.py"
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import live_session as ls  # noqa: E402

PREFIX = "[2026-11-18 10:00:00.000] [info] [core] [t1] "


class SplitTest(unittest.TestCase):
    def test_split_like_the_game(self) -> None:
        self.assertEqual(ls.split_send(["goto 1 2 3; waitfor goto_done 30", "tap x"]),
                         ["goto 1 2 3", "waitfor goto_done 30", "tap x"])
        self.assertEqual(ls.split_send(["  ;; \n log a\r\n"]), ["log a"])
        self.assertEqual(ls.split_send(["# note"]), ["# note"])

    def test_placeholders(self) -> None:
        self.assertEqual(ls.fill_placeholders("place 1 {figures}/A.dump", "C:/r/D/figures", ""),
                         "place 1 C:/r/D/figures/A.dump")
        self.assertEqual(ls.fill_placeholders("online_join {join}", "", "127.0.0.1:5"), "online_join 127.0.0.1:5")
        with self.assertRaises(ls.LiveError):
            ls.fill_placeholders("online_join {join}", "", "")

    def test_silent_lines(self) -> None:
        self.assertTrue(ls.is_silent("# only a note"))
        self.assertFalse(ls.is_silent("tap a # with a note"))


class AppendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "live.txt"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_numbers_follow_the_file(self) -> None:
        self.assertEqual(ls.append_lines(self.file, ["log a", "log b"]), [1, 2])
        self.assertEqual(ls.append_lines(self.file, ["log c"]), [3])
        self.assertEqual(self.file.read_bytes(), b"log a\nlog b\nlog c\n")

    def test_a_piece_without_newline_is_closed_first(self) -> None:
        # Never written so by the runner, but a hand edit could: that piece becomes its own line (the game
        # numbers it), so the new line keeps its own number and is not glued to it.
        self.file.write_bytes(b"log a\nlog b")
        self.assertEqual(ls.append_lines(self.file, ["log c"]), [3])
        self.assertEqual(self.file.read_bytes(), b"log a\nlog b\nlog c\n")

    def test_lines_in_counts_complete_lines(self) -> None:
        self.assertEqual(ls.lines_in(b""), 0)
        self.assertEqual(ls.lines_in(b"a\n\nb"), 2)


class MatchTest(unittest.TestCase):
    def outcomes(self, *numbers: int) -> dict[int, ls.LineOutcome]:
        return {n: ls.LineOutcome(n) for n in numbers}

    def test_start_done_refused(self) -> None:
        o = self.outcomes(12, 13)
        ls.match_outcomes([PREFIX + "Input script: live 12: goto 100 200 300"], o)
        self.assertTrue(o[12].started)
        self.assertFalse(ls.all_over(o))
        ls.match_outcomes([PREFIX + "Input script: live 12 done",
                           "[2026-11-18 10:00:01.000] [warning] [core] [t1] Input script: live 13 refused: "
                           "cannot parse 'tap q'"], o)
        self.assertTrue(o[12].done)
        self.assertEqual(o[13].refused, "cannot parse 'tap q'")
        self.assertTrue(ls.all_over(o))

    def test_other_numbers_do_not_count(self) -> None:
        # live 1 done is not live 12's end, nor is live 120's.
        o = self.outcomes(12)
        ls.match_outcomes([PREFIX + "Input script: live 1 done", PREFIX + "Input script: live 120 done",
                           PREFIX + "Input script: live 12: log done"], o)
        self.assertFalse(o[12].done)
        ls.match_outcomes([PREFIX + "Input script: live 12 done"], o)
        self.assertTrue(o[12].done)

    def test_waitfor_lines_stay_as_they_are(self) -> None:
        o = self.outcomes(4)
        ls.match_outcomes([PREFIX + "Input script: waitfor goto_done met after 0.4 s"], o)
        self.assertFalse(o[4].over)


class IdleTest(unittest.TestCase):
    def test_idle_stop(self) -> None:
        self.assertFalse(ls.idle_due(1000.0, 1000.0 + 19 * 60, 20))
        self.assertTrue(ls.idle_due(1000.0, 1000.0 + 20 * 60, 20))
        self.assertFalse(ls.idle_due(1000.0, 1000.0 + 99 * 3600, 0))  # 0: never
        self.assertEqual(ls.idle_seconds(10.0, 5.0), 0.0)


class FilesTest(unittest.TestCase):
    def test_state_round_trip_and_concurrent_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            st = ls.SessionState(state="live", following=["H"], problems=["H: x"])
            ls.save_state(folder, st)
            got = ls.load_state(folder)
            self.assertEqual((got.state, got.following, got.problems), ("live", ["H"], ["H: x"]))
            # A reader never sees half a file while the writer replaces it.
            stop = threading.Event()
            errors: list[Exception] = []

            def write() -> None:
                i = 0
                while not stop.is_set():
                    i += 1
                    ls.save_state(folder, ls.SessionState(state="live", note="n" * (i % 500)))

            t = threading.Thread(target=write)
            t.start()
            try:
                t0 = time.monotonic()
                while time.monotonic() - t0 < 1.0:
                    try:
                        self.assertEqual(ls.load_state(folder).state, "live")
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)
            finally:
                stop.set()
                t.join()
            self.assertEqual(errors, [])

    def test_session_names(self) -> None:
        with self.assertRaises(ls.LiveError):
            ls.session_dir(Path("x"), "..\\owner")
        self.assertEqual(ls.session_dir(Path("s"), "20261118-101010-a").name, "20261118-101010-a")


class SpecTest(unittest.TestCase):
    def info(self) -> ls.SessionInfo:
        clients = ["H", "D"]
        return ls.SessionInfo(name="20261118-1-x", layout="server+2", level="tutorial_b", clients=clients, late=["D"],
                              hide_server=True, server_live=True, idle_minutes=20,
                              prologue=ls.prologues(clients, "tutorial_b", {"D"}),
                              args={"H": list(ls.HOST_ARGS), "D": []}, worktree="")

    def test_prologues(self) -> None:
        p = ls.prologues(["H", "D"], "tutorial_b", set())
        self.assertIn("online_wait_peers 2 600", p["H"])
        self.assertLess(p["H"].index("online_mark_level"), p["H"].index("level tutorial_b"))
        self.assertIn("online_join {join}", p["D"])
        self.assertIn("waitfor online_next_level 900", p["D"])
        late = ls.prologues(["H", "D"], "tutorial_b", {"D"})
        self.assertNotIn("online_join {join}", late["D"])
        self.assertFalse(any(x.startswith("online_wait_peers") for x in late["H"]))
        hub = ls.prologues(["H"], None, set())
        self.assertFalse(any(x.startswith("level ") for x in hub["H"]))

    def test_session_spec_ends_in_follow_then_quit(self) -> None:
        info = self.info()
        text = ls.session_spec_text("live-x", info.layout, info.clients, info.prologue, info.args, False, "d")
        self.assertIn('follow live.txt\nquit\n"""', text)
        self.assertIn("fail_fast = false", text)
        self.assertIn("show_server_window = false", text)

    def test_skeleton_from_sends(self) -> None:
        sends = [ls.Sent("H", 1, "goto 1 2 3", 0.0), ls.Sent("H", 2, "waitfor goto_done 30", 0.0),
                 ls.Sent("D", 1, "online_join {join}", 0.0), ls.Sent("S", 1, "dump_entities Switch1", 0.0),
                 ls.Sent("H", 3, "tap x", 0.0)]
        text = ls.skeleton_spec("my-case", self.info(), sends)
        h = text.split("[peer.H]", 1)[1].split("[peer.D]", 1)[0]
        self.assertIn("wait_free 120\ngoto 1 2 3\nwaitfor goto_done 30\ntap x\nquit\n", h)
        self.assertIn("online_join {join}\nquit", text)
        self.assertIn("#   dump_entities Switch1", text)
        self.assertIn("show_server_window = false", text)
        ranged = ls.skeleton_spec("my-case", self.info(), sends, first=2, last=2)
        self.assertIn("waitfor goto_done 30\nquit", ranged)
        self.assertNotIn("goto 1 2 3", ranged)
        self.assertNotIn("tap x", ranged)


if __name__ == "__main__":
    unittest.main()
