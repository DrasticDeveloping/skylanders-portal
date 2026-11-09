"""Tests for skytest's {joincode} placeholder (Swap Force Online stage 8 item 4: join code v2): the hosted
server's code line, the script's stand-in and its swap, the spec checks, and a joiner that starts only
once the code is logged (fails closed without it). Run: python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import skytest  # noqa: E402
import test_skytest  # noqa: E402

# The design's golden vector (kind 1, revision 0, 203.0.113.7, port 50000, secret 0x89ABCDEF), as the
# server logs it: EncodeJoinCodeV2's 18 symbols, no dashes (the portal's tests/online_join_code_test.cpp).
CODE = "8SC0721Y3A24TQKFFQ"
LINE = f"[2026-11-08 12:00:00.000] [info] [core] [t2] Online: the server listens on port 50000, join code {CODE}"


def setUpModule() -> None:
    test_skytest.setUpModule()


def tearDownModule() -> None:
    test_skytest.tearDownModule()


class JoinCodeLineTest(unittest.TestCase):
    def test_the_code_comes_from_the_first_listen_line(self) -> None:
        other = LINE.replace(CODE, "8R2M0256B40000001J")
        self.assertEqual(skytest.join_code_from(["Online: starting as the server", LINE, other]), (CODE, ""))

    def test_no_listen_line_yet_gives_nothing(self) -> None:
        self.assertEqual(skytest.join_code_from([]), ("", ""))
        self.assertEqual(skytest.join_code_from(["Online: starting as the server",
                                                 "Online: hosting (just_me): the server is on port 1, join code "
                                                 + CODE]), ("", ""), "H's line is not the server's")

    def test_a_listen_line_without_a_v2_code_fails_closed(self) -> None:
        for bad in ("8SC0-721Y-3A24-TQKF-FQ",  # the shown form: the server logs no dashes
                    "60N00H9JS0V",  # 1A's 11 symbols
                    CODE[:17], CODE + "0",  # one short, one long
                    CODE[:5] + "U" + CODE[6:],  # U is no symbol
                    CODE.lower(),  # the server writes capitals
                    ""):
            code, problem = skytest.join_code_from([LINE.replace(CODE, bad)])
            self.assertEqual(code, "", bad)
            self.assertIn("not a join code v2", problem, bad)


class FillJoinCodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "script.txt"
        self.before = f"waitfor online_in_play 300\nonline_join {skytest.JOIN_CODE_STANDIN}\nquit\n"
        self.script.write_bytes(self.before.encode("utf-8"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_code_takes_the_stand_ins_place(self) -> None:
        skytest.fill_join_code(self.script, CODE)
        self.assertEqual(self.script.read_bytes().decode("utf-8"),
                         f"waitfor online_in_play 300\nonline_join {CODE}\nquit\n")

    def test_only_a_v2_code_is_written(self) -> None:
        # Anything that could change the script's commands (a space, `;`, `#`, a line break) or is no
        # code at all leaves the script as it was.
        for bad in ("", "8SC0-721Y-3A24-TQKF-FQ", CODE + ";quit", CODE[:9] + " " + CODE[10:],
                    CODE[:17] + "#", CODE[:17] + "\n", "60N00H9JS0V"):
            with self.assertRaises(skytest.SpecError, msg=bad):
                skytest.fill_join_code(self.script, bad)
            self.assertEqual(self.script.read_bytes().decode("utf-8"), self.before)

    def test_a_script_without_the_stand_in_is_refused(self) -> None:
        self.script.write_bytes(b"online_join 127.0.0.1:1\nquit\n")
        with self.assertRaises(skytest.SpecError):
            skytest.fill_join_code(self.script, CODE)
        self.assertEqual(self.script.read_bytes(), b"online_join 127.0.0.1:1\nquit\n")

    def test_the_stand_in_is_no_code(self) -> None:
        self.assertIsNone(skytest.JOIN_CODE_V2.fullmatch(skytest.JOIN_CODE_STANDIN))
        self.assertEqual(len(skytest.JOIN_CODE_STANDIN.split()), 1, "one word, as the code is")


JOINER = '''layout = "server+2"
stage = "8"
server = "host"
[peer.H]
script = """
online_host 0
quit
"""
[peer.D]
script = """
online_join {joincode}
quit
"""
'''


class JoinCodeSpecTest(unittest.TestCase):
    setUp = test_skytest.SkytestTest.setUp
    tearDown = test_skytest.SkytestTest.tearDown
    spec = test_skytest.SkytestTest.spec

    def load(self, name: str, body: str) -> skytest.Spec:
        return skytest.load_spec(self.spec(name, body), 20)

    def test_a_joiners_script_names_the_code(self) -> None:
        s = self.load("ok", JOINER)
        self.assertTrue(skytest.uses_join_code(s, "D"))
        self.assertFalse(skytest.uses_join_code(s, "H"))
        self.assertFalse(skytest.uses_join_code(s, "S"))
        # Substituted like the other placeholders; {join} beside it stays the address.
        both = s.peers["D"].script + "online_join {join}\n"
        s.peers["D"].script = both
        text = skytest.peer_script(s, "D", Path("figures"), "127.0.0.1:7", CODE)
        self.assertIn(f"online_join {CODE}\n", text)
        self.assertIn("online_join 127.0.0.1:7\n", text)
        self.assertNotIn("{", text)

    def test_the_code_needs_a_hosted_server_and_a_local_joiner(self) -> None:
        bad = {
            "standin": JOINER.replace('server = "host"\n', ""),
            "owner": JOINER.replace("online_host 0\n", "online_join {joincode}\n"),
            "remote": JOINER.replace("[peer.D]\n", '[peer.D]\nkind = "remote"\n'),
            "standin-word": JOINER.replace("{joincode}", skytest.JOIN_CODE_STANDIN),
            "in-args": JOINER.replace("[peer.D]\n", '[peer.D]\nargs = ["--online_port={joincode}"]\n'),
        }
        for name, body in bad.items():
            with self.assertRaises(skytest.SpecError, msg=name):
                self.load(name, body)


class JoinCodeRunTest(unittest.TestCase):
    """On the fake game repository of test_skytest.SkytestTest: the fake owner's `serverlog` writes the
    hosted server's lines, as the real server writes its listen line."""

    setUp = test_skytest.SkytestTest.setUp
    tearDown = test_skytest.SkytestTest.tearDown
    spec = test_skytest.SkytestTest.spec
    run_cases = test_skytest.SkytestTest.run_cases

    def body(self, server_line: str | None) -> str:
        log = f"serverlog Online: the server listens on port 50000, join code {server_line}\n" \
            if server_line is not None else ""
        return f'''layout = "server+2"
stage = "8"
server = "host"
timeout = 15
[peer.H]
script = """
online_host 0
{log}wait 100
quit
"""
[peer.D]
script = """
log joining with {{joincode}}
quit
"""
expect = ["Input script: joining with {CODE}"]
'''

    def test_the_joiner_starts_after_the_code_with_it_in_its_script(self) -> None:
        self.spec("joined", self.body(CODE))
        case = self.run_cases("joined")["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertGreaterEqual(peers["D"]["started_after_s"], peers["H"]["started_after_s"])
        script = (Path(case["folder"]) / "D" / "script.txt").read_text(encoding="utf-8")
        self.assertIn(f"log joining with {CODE}", script)
        self.assertNotIn(skytest.JOIN_CODE_STANDIN, script)

    def test_without_the_line_the_joiner_never_starts(self) -> None:
        self.spec("nocode", self.body(None))
        case = self.run_cases("nocode")["cases"][0]
        self.assertEqual(case["status"], "fail")
        d = next(p for p in case["peers"] if p["peer"] == "D")
        self.assertEqual(d["status"], "fail")
        self.assertTrue(d["reasons"][0].startswith("never started: S never logged its join code"), d["reasons"])
        self.assertEqual(d["command"], [], "no game was started for D")
        self.assertFalse((Path(case["folder"]) / "D" / "game.log").exists())

    def test_a_line_without_a_v2_code_starts_no_joiner(self) -> None:
        self.spec("badcode", self.body("8SC0-721Y-3A24-TQKF-FQ"))
        case = self.run_cases("badcode")["cases"][0]
        self.assertEqual(case["status"], "fail")
        d = next(p for p in case["peers"] if p["peer"] == "D")
        self.assertIn("not a join code v2", d["reasons"][0])
        self.assertEqual(d["command"], [])
        script = (Path(case["folder"]) / "D" / "script.txt").read_text(encoding="utf-8")
        self.assertIn(skytest.JOIN_CODE_STANDIN, script, "the script keeps no guess")


if __name__ == "__main__":
    unittest.main()
