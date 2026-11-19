"""Tests for script_checks.py and its use in the runner: a hosted case whose client waits on a session it
never starts is refused before launch (R2); a wait whose needed peer's game exited ends the run (R1).
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_script_checks.py"
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import script_checks as sc  # noqa: E402
import skytest  # noqa: E402

# The expanded H script of figure-change-crash 20261118-171834 (its first lines).
FIGCR_H = "online_wait_peers 2 240\nlevel tutorial_b\nwaitfor online_barrier 600\nquit\n"
BOOT = "waitfor online_title 300\nwaitfor title_ready 240\n"


class HostProblem(unittest.TestCase):
    def test_the_figcr_script_is_refused(self):
        why = sc.host_problem(FIGCR_H)
        self.assertIn("`online_wait_peers 2 240` waits on a session before the script hosts or joins one", why)

    def test_hosting_or_joining_first_is_fine(self):
        self.assertEqual(sc.host_problem(BOOT + "online_host 0\nwaitfor online_connected 60\nonline_wait_peers 2 240\nquit\n"), "")
        self.assertEqual(sc.host_problem(BOOT + "waitfor online_in_play 300\nonline_join {join}\nwaitfor online_barrier 300\n"), "")
        self.assertEqual(sc.host_problem("wait 40\nkey F4\ntap a\nwaitfor online_connected 150\nquit\n"), "")  # the menus' host

    def test_a_local_or_unknown_wait_first_is_no_refusal(self):
        self.assertEqual(sc.host_problem(BOOT + "waitfor online_in_play 300\nquit\n"), "")
        self.assertEqual(sc.host_problem("waitfor online_something_new 30\nquit\n"), "")
        self.assertEqual(sc.host_problem("quit\n"), "")

    def test_each_session_wait_is_one(self):
        for line in ("waitfor online_connected 60", "waitfor online_server_hub 300", "waitfor online_barrier 300",
                     "online_wait_peer_near_entity X 200", "waitfor online_standin_live 240", "online_wait_peers_at_most 1 30"):
            self.assertTrue(sc.host_problem(line + "\nquit\n"), line)


class NeededPeerGone(unittest.TestCase):
    NEEDS = {"H": {"online_standin_live": ["D"]}}
    PLANNED = {"H": {"online_standin_live": 1}}

    def test_d_gone_while_h_still_waits_for_its_stand_in(self):
        got = sc.needed_peer_gone(self.NEEDS, self.PLANNED, {"H": {}}, {"S": None, "H": None, "D": 0})
        self.assertEqual(got[0], "H")
        self.assertIn("waitfor online_standin_live cannot be met: it needs D", got[1])
        self.assertIn("exit code 0", got[1])

    def test_no_stop_when_the_wait_is_met_the_peer_runs_or_the_waiter_ended(self):
        self.assertIsNone(sc.needed_peer_gone(self.NEEDS, self.PLANNED, {"H": {"online_standin_live": 1}},
                                              {"H": None, "D": 0}))
        self.assertIsNone(sc.needed_peer_gone(self.NEEDS, self.PLANNED, {"H": {}}, {"H": None, "D": None}))
        self.assertIsNone(sc.needed_peer_gone(self.NEEDS, self.PLANNED, {"H": {}}, {"H": 0, "D": 0}))
        self.assertIsNone(sc.needed_peer_gone(self.NEEDS, self.PLANNED, {"H": {}}, {"H": None}))  # D not started
        # A spec that ends D early on purpose names no such wait: nothing stops.
        self.assertIsNone(sc.needed_peer_gone({}, {}, {}, {"H": None, "D": 0}))

    def test_a_wait_planned_twice_is_pending_until_both_ended(self):
        planned = {"H": {"online_standin_live": 2}}
        self.assertIsNotNone(sc.needed_peer_gone(self.NEEDS, planned, {"H": {"online_standin_live": 1}}, {"H": None, "D": 0}))

    def test_counts(self):
        self.assertEqual(sc.wait_counts("waitfor a 1\nwaitfor b 2\nwaitfor a 3\n# waitfor c 1\n"), {"a": 2, "b": 1})
        self.assertEqual(sc.wait_done("[t1] Input script: waitfor online_standin_live timed out after 240.0 s"),
                         "online_standin_live")
        self.assertEqual(sc.wait_done("[t1] Input script: waitfor online_barrier met after 0.0 s"), "online_barrier")
        self.assertEqual(sc.wait_done("[t1] Input script: STANDIN D left"), "")


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text: str):
        f = self.dir / "case.toml"
        f.write_text(text, encoding="utf-8")
        return skytest.load_spec(f, 300)

    def spec(self, h: str, d: str = "waitfor online_in_play 300\nonline_join {join}\nquit\n", extra: str = "") -> str:
        return "\n".join(['name = "x"', 'layout = "server+2"', 'stage = "8"', 'server = "host"', 'scenes = "skip"',
                          "[peer.H]", "script = '''\n" + h + "'''", extra,
                          "[peer.D]", "script = '''\n" + d + "'''", ""])

    def test_load_spec_refuses_the_figcr_script(self):
        with self.assertRaises(skytest.SpecError) as e:
            self.load(self.spec(FIGCR_H))
        self.assertIn("peer H: `online_wait_peers 2 240` waits on a session", str(e.exception))
        self.load(self.spec("online_host 0\n" + FIGCR_H))

    def test_wait_needs_is_read_and_checked(self):
        h = "online_host 0\nwaitfor online_standin_live 240\nquit\n"
        s = self.load(self.spec(h, extra='wait_needs = { online_standin_live = ["D"] }'))
        self.assertEqual(s.peers["H"].wait_needs, {"online_standin_live": ["D"]})
        for bad in ('wait_needs = { online_barrier = ["D"] }', 'wait_needs = { online_standin_live = ["E"] }',
                    'wait_needs = { online_standin_live = ["S"] }', 'wait_needs = { online_standin_live = "D" }'):
            with self.assertRaises(skytest.SpecError, msg=bad):
                self.load(self.spec(h, extra=bad))

    def test_fail_fast_stops_when_the_needed_peer_exited(self):
        log = self.dir / "H.log"
        log.write_text("[t1] Input script: waitfor online_barrier met after 0.0 s\n", encoding="utf-8")

        class Proc:
            def __init__(self, code):
                self.code = code

            def poll(self):
                return self.code
        watch = skytest.FailFast({"H": log}, {"H": []}, needs={"H": {"online_standin_live": ["D"]}},
                                 scripts={"H": "online_host 0\nwaitfor online_standin_live 240\nquit\n"})
        self.assertIsNone(watch.check({"H": Proc(None), "D": Proc(None)}))
        hit = watch.check({"H": Proc(None), "D": Proc(0)})
        self.assertEqual(hit[0], "H")
        self.assertIn("needs D", hit[1])
        # Met before D left: no stop.
        log.write_text("[t1] Input script: waitfor online_standin_live met after 3.0 s\n", encoding="utf-8")
        watch = skytest.FailFast({"H": log}, {"H": []}, needs={"H": {"online_standin_live": ["D"]}},
                                 scripts={"H": "online_host 0\nwaitfor online_standin_live 240\nquit\n"})
        self.assertIsNone(watch.check({"H": Proc(None), "D": Proc(0)}))



class WhenLines(unittest.TestCase):
    """`when <condition> <line>` / `unless <condition> <line>`: a spec line kept by the checkout's command table."""
    SCRIPT = "\n".join(["tap y", "when scene_skip_prompt_up waitfor scene_skip_prompt_up 10",
                         "unless scene_skip_prompt_up wait 0.5", "  when other_cond log x", "tap y", "quit"])

    def test_listed_and_not_listed(self):
        self.assertEqual(skytest.expand_when_lines(self.SCRIPT, {"scene_skip_prompt_up"}),
                         "tap y\nwaitfor scene_skip_prompt_up 10\ntap y\nquit")
        self.assertEqual(skytest.expand_when_lines(self.SCRIPT, set()), "tap y\nwait 0.5\ntap y\nquit")
        self.assertEqual(skytest.expand_when_lines(self.SCRIPT, None), "tap y\nwait 0.5\ntap y\nquit")
        self.assertEqual(skytest.expand_when_lines("log when this\nwhen", {"x"}), "log when this\nwhen")

    def test_load_spec_expands_with_the_table(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "case.toml"
        f.write_text("\n".join(['name = "x"', 'layout = "server+1"', 'stage = "8"', 'server = "host"',
                                 'scenes = "skip"', "[peer.H]", "script = '''\nonline_host 0\n" + self.SCRIPT +
                                 "\n'''", ""]), encoding="utf-8")
        old = skytest.SCRIPT_CONDITIONS
        self.addCleanup(setattr, skytest, "SCRIPT_CONDITIONS", old)
        skytest.SCRIPT_CONDITIONS = {"scene_skip_prompt_up"}
        self.assertIn("waitfor scene_skip_prompt_up 10\ntap y", skytest.load_spec(f, 300).peers["H"].script)
        skytest.SCRIPT_CONDITIONS = set()
        script = skytest.load_spec(f, 300).peers["H"].script
        self.assertIn("tap y\nwait 0.5\ntap y", script)
        self.assertNotIn("when", script)
        self.assertNotIn("unless", script)

if __name__ == "__main__":
    unittest.main()
