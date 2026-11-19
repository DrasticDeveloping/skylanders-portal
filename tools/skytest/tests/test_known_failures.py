"""Tests for the runner's refusal of a pointless rerun (known_failures.py): same and different build,
same and different case content, different reasons, one failure only, a pass between, older results
without the new fields, and the runner's own refusal and override (`test --rerun-known-failure`).
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_known_failures.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import known_failures as kf  # noqa: E402

WAIT = ("stopped early: H [2026-11-18 16:51:27.131] [info] [core] [t34956] Input script: waitfor online_level_end "
        "timed out after 60.0 s (fail fast)")
WAIT_PEER = "ended by the runner: " + WAIT


def result(status: str, started: str, build: str = "b1", spec: str = "s1", reason: str = WAIT_PEER,
           new_fields: bool = True) -> dict:
    prov = {"spec_sha256": spec, "args_sha256": "a1", "fixture_sha256": "f1"}
    if new_fields:
        prov |= {"build": build, "case_sha256": kf.case_sha256(spec, "a1", "f1")}
    peers = [{"peer": "S", "status": "pass", "reasons": []},
             {"peer": "H", "status": status, "reasons": [reason] if status != "pass" else []}]
    return {"case": "levelpass-ww1", "status": status, "started": started, "seconds": 300.0,
            "reasons": [WAIT] if status != "pass" else [], "peers": peers, "provenance": prov}


def prior(status: str, started: str, **kw) -> kf.Prior:
    return kf.prior_from_result(f"runs/{started}-levelpass-ww1", result(status, started, **kw))


HASH = kf.case_sha256("s1", "a1", "f1")


class KnownFailureTest(unittest.TestCase):
    def test_two_failures_alike_on_the_same_build_and_case_are_refused(self):
        h = [prior("fail", "20261118-160000"), prior("fail", "20261118-163634")]
        r = kf.known_failure(h, "b1", HASH)
        self.assertIsNotNone(r)
        self.assertEqual(r.folders, ["runs/20261118-160000-levelpass-ww1", "runs/20261118-163634-levelpass-ww1"])
        self.assertIn("waitfor online_level_end timed out after N s", r.reason)
        self.assertTrue(r.reason.startswith("H: "))
        line = r.line("levelpass-ww1")
        self.assertIn("KNOWN FAILURE", line)
        self.assertIn("--rerun-known-failure", line)
        self.assertIn("runs/20261118-160000-levelpass-ww1", line)

    def test_the_time_stamps_thread_ids_and_numbers_do_not_make_reasons_differ(self):
        other = WAIT_PEER.replace("16:51:27.131", "17:02:11.004").replace("t34956", "t1200").replace("60.0", "61.2")
        h = [prior("fail", "20261118-160000"), prior("fail", "20261118-163634", reason=other)]
        self.assertIsNotNone(kf.known_failure(h, "b1", HASH))

    def test_any_build_counts_and_the_message_names_the_builds(self):
        # The owner's call of 18 November: today's reruns were each on a new build.
        h = [prior("fail", "20261118-160000", build="b1"), prior("fail", "20261118-163634", build="b2")]
        r = kf.known_failure(h, "b3", HASH)
        self.assertIsNotNone(r)
        self.assertEqual(r.builds, ["b1", "b2"])
        self.assertIn("on builds b1 and b2 (this one: b3)", r.line("levelpass-ww1"))
        self.assertIn("a new build: what it changes that is the cause", r.line("levelpass-ww1"))
        same = kf.known_failure([prior("fail", "20261118-160000"), prior("fail", "20261118-163634")], "b1", HASH)
        self.assertIn("on this build (b1)", same.line("levelpass-ww1"))

    def test_the_part_a_failure_belongs_to(self):
        """A phased script (the level pass): another sweep, the same exit phase; the failure at the exit."""
        def script(sweep: str, exit_: str) -> str:
            return ("waitfor online_title 300\nlog LEVELPASS phase sweep\n" + sweep +
                    "\nlog LEVELPASS phase exit\n" + exit_ + "\nlog LEVELPASS done\nquit\n")
        exit_lines = "enter_trigger auth:ww1:13618\nwaitfor online_level_end 150"
        log = "[t1] Input script: LEVELPASS phase sweep\n[t1] Input script: LEVELPASS phase exit\n[t1] Input script: waitfor online_level_end timed out after 150.0 s\n"
        runs = [kf.prior_from_result(f"runs/{s}-levelpass-ww1", result("fail", s, build=b, spec=sp), "",
                                     script(sw, exit_lines), log)
                for s, b, sp, sw in (("20261118-160000", "b1", "s1", "goto_entity auth:ww1:1"),
                                     ("20261118-163634", "b2", "s2", "goto_entity auth:ww1:2"))]
        self.assertEqual(runs[0].part[0], "exit")
        self.assertEqual(runs[0].part, runs[1].part)
        now = kf.script_parts(script("goto_entity auth:ww1:3", exit_lines), "a1", "f1")
        r = kf.known_failure(runs, "b3", kf.case_sha256("s3", "a1", "f1"), now)
        self.assertIsNotNone(r)
        self.assertIn("exit phase", r.line("levelpass-ww1"))
        # Another exit: another case.
        other = kf.script_parts(script("goto_entity auth:ww1:3", "enter_trigger auth:ww1:9\nwaitfor online_level_end 150"), "a1", "f1")
        self.assertIsNone(kf.known_failure(runs, "b3", kf.case_sha256("s3", "a1", "f1"), other))
        # Other arguments (another --game-arg): another case.
        self.assertIsNone(kf.known_failure(runs, "b3", kf.case_sha256("s3", "a2", "f1"),
                                           kf.script_parts(script("x", exit_lines), "a2", "f1")))
        # A script with no phases has no parts; a log with no phase line gives the result none.
        self.assertEqual(kf.script_parts("waitfor title_ready 75\nquit\n"), {})
        self.assertEqual(kf.last_phase("[t1] Input script: hello\n"), "")

    def test_another_case_content_is_not_refused(self):
        h = [prior("fail", "20261118-160000"), prior("fail", "20261118-163634")]
        self.assertIsNone(kf.known_failure(h, "b1", kf.case_sha256("s2", "a1", "f1")))
        h = [prior("fail", "20261118-160000", spec="s1"), prior("fail", "20261118-163634", spec="s2")]
        self.assertIsNone(kf.known_failure(h, "b1", kf.case_sha256("s2", "a1", "f1")))

    def test_different_first_reasons_are_not_refused(self):
        crash = "log has forbidden /Crash/: [2026-11-18 16:51:27.131] Crash: exception C0000005"
        h = [prior("fail", "20261118-160000"), prior("fail", "20261118-163634", reason=crash)]
        self.assertIsNone(kf.known_failure(h, "b1", HASH))

    def test_one_failure_only_is_not_refused(self):
        self.assertIsNone(kf.known_failure([prior("fail", "20261118-163634")], "b1", HASH))
        self.assertIsNone(kf.known_failure([], "b1", HASH))

    def test_a_pass_last_or_between_is_not_refused(self):
        h = [prior("fail", "20261118-150000"), prior("fail", "20261118-160000"), prior("pass", "20261118-163634")]
        self.assertIsNone(kf.known_failure(h, "b1", HASH))
        h = [prior("fail", "20261118-150000"), prior("pass", "20261118-160000"), prior("fail", "20261118-163634")]
        self.assertIsNone(kf.known_failure(h, "b1", HASH))

    def test_the_last_two_count_whatever_order_the_folders_come_in(self):
        h = [prior("fail", "20261118-163634"), prior("pass", "20261118-150000"), prior("fail", "20261118-160000")]
        self.assertIsNotNone(kf.known_failure(h, "b1", HASH))

    def test_results_that_started_no_game_say_nothing(self):
        h = [prior("fail", "20261118-150000"), prior("capacity", "20261118-155000"),
             prior(kf.KNOWN_FAILURE, "20261118-158000"), prior("fail", "20261118-160000")]
        self.assertIsNotNone(kf.known_failure(h, "b1", HASH))
        h = [prior("fail", "20261118-150000"), prior("breaker", "20261118-160000")]
        self.assertIsNone(kf.known_failure(h, "b1", HASH))

    def test_an_older_result_takes_its_sessions_snapshot_and_the_hash_from_its_fields(self):
        old = result("fail", "20261118-160000", new_fields=False)
        p = kf.prior_from_result("runs/x", old, "C:\\store\\snapshots\\b1")
        self.assertEqual((p.build, p.case_hash), ("b1", HASH))
        h = [p, prior("fail", "20261118-163634")]
        self.assertIsNotNone(kf.known_failure(h, "b1", HASH))

    def test_no_hash_is_never_refused_an_unknown_build_still_is(self):
        h = [prior("fail", "20261118-160000"), prior("fail", "20261118-163634")]
        self.assertIsNotNone(kf.known_failure(h, "", HASH))  # the build only names it
        self.assertIsNone(kf.known_failure(h, "b1", ""))
        self.assertEqual(kf.case_sha256("", "a1", "f1"), "")

    def test_a_reason_of_the_case_alone_counts_when_no_peer_has_one(self):
        r = {"status": "error", "reasons": ["runner error: OSError 5"], "peers": []}
        self.assertEqual(kf.first_reason(r), "runner error: OSError N")


if __name__ == "__main__":
    unittest.main()
