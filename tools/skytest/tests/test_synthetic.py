"""Tests for skytest's server+N+hK layouts (synthetic peers) and the remote_swap grade. Run:
python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import skytest  # noqa: E402
import test_skytest  # noqa: E402

FAKE_SYNTHETIC = HERE / "fake_synthetic.py"


def setUpModule() -> None:
    test_skytest.setUpModule()


def tearDownModule() -> None:
    test_skytest.tearDownModule()


class LayoutTest(unittest.TestCase):
    def test_synthetic_layouts(self) -> None:
        self.assertEqual(skytest.peers_of("server+2+h2"), ["S", "H", "D"])
        self.assertEqual(skytest.synthetic_of("server+2+h2"), ["h1", "h2"])
        self.assertEqual(skytest.synthetic_of("server+1+h1"), ["h1"])
        self.assertEqual(skytest.synthetic_of("server+2"), [])
        self.assertEqual(skytest.synthetic_of("solo"), [])
        for bad in ("server+2+h3", "server+5+h1", "solo+h1", "server+2+h0", "server+2+H1"):
            self.assertIsNone(skytest.LAYOUT.match(bad), bad)
            with self.assertRaises(skytest.SpecError):
                skytest.peers_of(bad)


class SyntheticRunTest(unittest.TestCase):
    """Runs on the fake game repository of test_skytest.SkytestTest, with a [synthetic] table."""

    setUp_base = test_skytest.SkytestTest.setUp
    tearDown = test_skytest.SkytestTest.tearDown
    spec = test_skytest.SkytestTest.spec
    run_cases = test_skytest.SkytestTest.run_cases

    def setUp(self) -> None:
        self.setUp_base()
        f = self.repo / "tests" / "skytest.toml"
        f.write_text(f.read_text(encoding="utf-8") + f"""
[synthetic]
exe = "peer.exe"
launcher = [{test_skytest.toml_str(sys.executable)}, "-I", {test_skytest.toml_str(FAKE_SYNTHETIC)}]
args = ["--port", "{{port}}", "--script", "{{script}}", "--digest", "{{digest}}"]
server_setting = "dev_synthetic_peers"
""", encoding="utf-8")
        # The fake game's allow list gets the server setting the runner passes for a server+N+hK case
        # (the launch builder checks the runner's own settings too).
        allow = self.repo / "tests" / "launch_allow.toml"
        allow.write_text(allow.read_text(encoding="utf-8") + '[settings.dev_synthetic_peers]\nkind = "bool"\n',
                         encoding="utf-8")
        cfg = skytest.load_config(self.repo)
        cfg.start_gap = 0.0
        self.cfg = cfg

    HOSTED = '''layout = "server+1+h1"
stage = "4"
server = "host"
[peer.H]
script = """
online_host 0
place 0 {figures}/Roller Brawl.dump
wait 100
quit
"""
[peer.S]
expect = ["Online: starting as the server"]
[synthetic.h1]
start_after = "Online: starting as the server"
'''

    def test_a_synthetic_peer_starts_on_its_condition_and_is_graded(self) -> None:
        self.spec("syn", self.HOSTED + 'script = "swap 1 Wash 600\\nsay hello there\\n"\n'
                  'expect = ["hello there"]\nexpect_count = { "Online: swap 1 hero" = 1 }\n')
        case = self.run_cases("syn")["cases"][0]
        self.assertEqual(case["status"], "pass", case)
        peers = {p["peer"]: p for p in case["peers"]}
        self.assertEqual(sorted(peers), ["H", "S", "h1"])
        self.assertIn("--dev_synthetic_peers=true", peers["H"]["command"])
        port = next(a.split("=", 1)[1] for a in peers["H"]["command"] if a.startswith("--online_port="))
        cmd = peers["h1"]["command"]
        self.assertEqual(cmd[cmd.index("--port") + 1], port)
        snap_exe = Path(next(iter(Path(self.cfg.store / "snapshots").glob("*/game.exe"))))
        self.assertEqual(cmd[cmd.index("--digest") + 1], hashlib.sha256(snap_exe.read_bytes()).hexdigest())
        run = Path(case["folder"])
        self.assertIn("swap 1 Wash 600", (run / "h1" / "script.txt").read_text(encoding="utf-8"))
        self.assertIn("synthetic peer 3 connected", (run / "h1" / "peer.log").read_text(encoding="utf-8"))
        self.assertEqual(case["leftover_processes"], 0)

    def test_a_synthetic_peer_that_never_connects_or_whose_condition_never_shows_fails(self) -> None:
        self.spec("noconn", self.HOSTED + 'script = "noconnect\\n"\n')
        h1 = {p["peer"]: p for p in self.run_cases("noconn")["cases"][0]["peers"]}["h1"]
        self.assertEqual(h1["status"], "fail")
        self.assertTrue(any("never joined" in w for w in h1["reasons"]), h1["reasons"])
        self.spec("nostart", self.HOSTED.replace('start_after = "Online: starting as the server"',
                                                 'start_after = "never logged"') + 'script = "say x\\n"\n')
        case = self.run_cases("nostart")["cases"][0]
        h1 = {p["peer"]: p for p in case["peers"]}["h1"]
        self.assertEqual(case["status"], "fail")
        self.assertTrue(any("never started" in w for w in h1["reasons"]), h1["reasons"])

    def test_a_synthetic_peer_left_running_is_ended_and_fails(self) -> None:
        old = skytest.SYNTHETIC_EXIT_GRACE
        skytest.SYNTHETIC_EXIT_GRACE = 0.5
        try:
            self.spec("hangs", self.HOSTED + 'script = "hang\\n"\n')
            case = self.run_cases("hangs")["cases"][0]
        finally:
            skytest.SYNTHETIC_EXIT_GRACE = old
        h1 = {p["peer"]: p for p in case["peers"]}["h1"]
        self.assertEqual(case["status"], "fail")
        self.assertTrue(h1["ended_by_runner"])
        self.assertTrue(any("did not end" in w for w in h1["reasons"]), h1["reasons"])
        self.assertEqual(case["leftover_processes"], 0)

    def test_a_fatal_synthetic_line_and_a_bad_exit_fail(self) -> None:
        self.spec("fatal", self.HOSTED + 'script = "say Online: synthetic peer could not connect: x\\nexit 1\\n"\n')
        h1 = {p["peer"]: p for p in self.run_cases("fatal")["cases"][0]["peers"]}["h1"]
        self.assertEqual(h1["status"], "fail")
        self.assertTrue(any("exit code 1" in w for w in h1["reasons"]), h1["reasons"])
        self.assertTrue(any("could not connect" in w for w in h1["reasons"]), h1["reasons"])

    def test_spec_checks(self) -> None:
        def load(body: str) -> None:
            p = self.spec("chk", body)
            skytest.load_spec(p, 30)

        base = self.HOSTED + 'script = "say x\\n"\n'
        load(base)
        with self.assertRaises(skytest.SpecError):  # a standin server
            load(base.replace('server = "host"\n', ""))
        with self.assertRaises(skytest.SpecError):  # a table missing for h2
            load(base.replace("server+1+h1", "server+1+h2"))
        with self.assertRaises(skytest.SpecError):  # a synthetic table without the layout
            load(base.replace("server+1+h1", "server+1"))
        with self.assertRaises(skytest.SpecError):  # a key it does not know
            load(base + 'args = ["--x=1"]\n')
        with self.assertRaises(skytest.SpecError):  # start_peer not a game
            load(base + 'start_peer = "D"\n')
        with self.assertRaises(skytest.SpecError):  # no script
            load(self.HOSTED)
        with self.assertRaises(skytest.SpecError):  # a remote_swap receiver that has no game
            load(base + '[grade.remote_swap]\nreceivers = ["h1"]\n')
        (self.repo / "tests" / "runtime" / "chk.toml").unlink()
        # The runner owns the server setting: a spec setting it is refused at the run.
        self.spec("owns", base.replace("[peer.H]\n", '[peer.H]\nargs = ["--dev_synthetic_peers=true"]\n'))
        case = self.run_cases("owns")["cases"][0]
        self.assertEqual(case["status"], "error")
        self.assertIn("runner sets it", " ".join(case["reasons"]))

    def test_a_line_off_the_synthetic_peer_contract_starts_nothing(self) -> None:
        # The launch builder's synthetic-peer rules hold for the local runner too: the digest contract
        # (the snapshot game's SHA-256, never the peer's own --game), the session's own port.
        self.spec("syn", self.HOSTED + 'script = "say hello\\n"\n')
        for what, args in {
            "--game": ["--port", "{port}", "--script", "{script}", "--game", "{script}"],
            "another digest": ["--port", "{port}", "--script", "{script}", "--digest", "ab" * 32],
            "another server": ["--server", "10.0.0.1", "--port", "{port}", "--script", "{script}", "--digest",
                               "{digest}"],
        }.items():
            with self.subTest(what=what):
                self.cfg.synthetic_args = args
                case = self.run_cases("syn")["cases"][0]
                self.assertEqual(case["status"], "error", case)
                self.assertIn("synthetic peer h1", " ".join(case["reasons"]))
                self.assertEqual(case["peers"], [])


def swap(n: int, hero: str, tick: int) -> str:
    return f"[info] Online: swap {n} hero {hero} epoch 1 gen 2 tick {tick}"


def body(peer: int, hero: str, n: int, tick: int, kind: str = "real") -> str:
    return f"[info] Online: puppet {peer} epoch 1 gen 2 body {hero} ({kind}) swap {n} tick {tick}"


class RemoteSwapGradeTest(unittest.TestCase):
    NAMES = ["S", "H", "D"]

    def texts(self) -> dict[str, str]:
        return {
            "H": "\n".join(["Online: connected as peer 1", swap(1, "Wash", 600), body(3, "Blast", 1, 1010)]),
            "D": "\n".join(["Online: connected as peer 2", body(1, "Wash", 1, 620), body(3, "Blast", 1, 1020)]),
            "h1": "\n".join(["Online: synthetic peer 3 connected (backend mode 4)", swap(1, "Blast", 1000)]),
        }

    def test_a_synthetic_peer_swaps_but_never_receives(self) -> None:
        graded, why = skytest.grade_remote_swap(self.texts(), {"min_swaps": 2}, {}, self.NAMES, ["h1"])
        self.assertEqual(why, [])
        self.assertEqual((graded["swaps"], graded["expected_pairs"], graded["graded_pairs"]), (2, 3, 3))
        self.assertEqual(graded["peers"], {"H": 1, "D": 2, "h1": 3})

    def test_not_reached_and_a_slow_pair(self) -> None:
        _, why = skytest.grade_remote_swap(self.texts(), {"min_swaps": 3}, {}, self.NAMES, ["h1"])
        self.assertTrue(any("not reached (2 swap(s)" in w for w in why), why)
        t = self.texts()
        t["D"] = t["D"].replace("tick 620", "tick 660")  # 60 ticks = 1000 ms
        graded, why = skytest.grade_remote_swap(t, {}, {"remote_swap": {"max_ms": 500.0}}, self.NAMES, ["h1"])
        self.assertTrue(graded.get("violation"))
        self.assertTrue(any("1000 ms (max 500)" in w for w in why), why)
        t = self.texts()
        t["D"] = t["D"].replace("Online: connected as peer 2", "")
        _, why = skytest.grade_remote_swap(t, {}, {}, self.NAMES, ["h1"])
        self.assertTrue(any("D never logged its peer id" in w for w in why), why)

    def test_swap_pairs_filters(self) -> None:
        import swap_pairs
        logs = {1: swap(1, "Wash", 600), 2: body(1, "Wash", 1, 610), 3: ""}
        r = swap_pairs.grade_swap_pairs(logs, {1: 1}, receivers={2})
        self.assertTrue(r.passed, r.failures)
        self.assertEqual((r.swaps, r.expected), (1, 1))
        # Peer 1 outside `swappers`: its swap line is never read, so the ordered swap has no reference.
        r = swap_pairs.grade_swap_pairs(logs, {1: 1}, receivers={2}, swappers={2})
        self.assertFalse(r.passed)
        self.assertEqual((r.swaps, r.expected), (0, 1))
        self.assertTrue(any("missing reference" in f for f in r.failures), r.failures)
        # A receiver-only peer's own swap, outside `swappers`, is neither graded nor unexpected.
        logs[2] += "\n" + swap(1, "Blast", 700)
        r = swap_pairs.grade_swap_pairs(logs, {1: 1}, receivers={2}, swappers={1})
        self.assertTrue(r.passed, r.failures)
        r = swap_pairs.grade_swap_pairs(logs, {1: 1}, receivers={2})
        self.assertTrue(any("not ordered by the case" in f for f in r.failures), r.failures)


class AmendmentVerdictTest(unittest.TestCase):
    def test_a_frozen_case_on_a_reduced_layout_is_met_only_with_its_amendment(self) -> None:
        case = {"id": "four-characters", "stage": "4", "layout": "server+2+h2", "kind": "required", "spec": True,
                "amendment": "stage 4 amendment a"}
        res = [skytest.CaseResult(case="four-characters", layout="server+2+h2", stage="4", status="pass")]
        v = skytest.checkpoint_verdict({"case": [case]}, "4", ["4"], res)
        self.assertFalse(v["met"])
        self.assertIn("met only with stage 4 amendment a", v["line"])
        v = skytest.checkpoint_verdict({"case": [dict(case, amendment_accepted=True)]}, "4", ["4"], res)
        self.assertTrue(v["met"], v)


if __name__ == "__main__":
    unittest.main()
