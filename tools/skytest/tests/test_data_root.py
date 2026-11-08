"""The data_root_untouched grade (stage 7 item 9, future/online-7.md 3.9): its entries, its spec check,
the tree it hashes, each phase's verdict, the phase lines, and whole runs of the fake game."""
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

TITLE = "415608E9"
P1, P2 = "B13EBABEBABEBABE", "E000000000000002"
SLOT0 = f"data/{P1}/{TITLE}/00000001/SWAPFORCE_0/SWAPFORCE_0.xsg"
HEADER0 = f"data/{P1}/{TITLE}/Headers/00000001/SWAPFORCE_0.header"
OPTIONS = f"data/{P1}/{TITLE}/00000001/SWAPFORCE_OPTIONS/SWAPFORCE_OPTIONS.xsg"
SLOT0_P2 = f"data/{P2}/{TITLE}/00000001/SWAPFORCE_0/SWAPFORCE_0.xsg"


def setUpModule() -> None:
    test_skytest.setUpModule()


def tearDownModule() -> None:
    test_skytest.tearDownModule()


def result(before: dict, after: dict, allow: list[str], require: list[str] | None = None) -> dict:
    return skytest.data_root_phase_result(before, after, allow, require or [], TITLE)


class EntryTest(unittest.TestCase):
    def test_entries_parse(self) -> None:
        self.assertEqual(skytest.data_root_entry("slot SWAPFORCE_0"), ("slot", "SWAPFORCE_0"))
        self.assertEqual(skytest.data_root_entry(f"header {P1}/SWAPFORCE_1"), ("header", f"{P1}/SWAPFORCE_1"))
        self.assertEqual(skytest.data_root_entry("online"), ("online", ""))
        self.assertEqual(skytest.data_root_entry("figure Roller Brawl"), ("figure", "Roller Brawl"))
        self.assertEqual(skytest.data_root_entry("file data/x/y.bin"), ("file", "data/x/y.bin"))

    def test_bad_entries_are_refused(self) -> None:
        for bad in ("slots SWAPFORCE_0", "slot", "slot ../SWAPFORCE_0", "slot ABC/SWAPFORCE_0", "online x",
                    "figure", "figure a/b", "file ../outside", "file C:/abs", "file data\\x", "ignore data/", 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                skytest.data_root_entry(bad)


class PhaseResultTest(unittest.TestCase):
    BEFORE = {SLOT0: "a", HEADER0: "h", OPTIONS: "o", "settings.toml": "s", "figures/Roller Brawl.dump": "f"}

    def test_nothing_changed_passes(self) -> None:
        r = result(self.BEFORE, dict(self.BEFORE), [])
        self.assertEqual((r["changes"], r["not_allowed"], r["problems"]), ([], [], []))

    def test_the_session_slot_and_online_are_allowed_by_name(self) -> None:
        after = {**self.BEFORE, SLOT0: "b", "data/Online/handback/1/x.xsg": "n",
                 "data/save-backup/import/7/txn": "t", "data/save-backup/previous-import/6/old.xsg": "p"}
        r = result(self.BEFORE, after, ["slot SWAPFORCE_0", "online", "import_journal"])
        self.assertEqual(r["not_allowed"], [])
        self.assertEqual(len(r["changes"]), 4)

    def test_the_header_options_and_settings_are_never_allowed_by_the_slot(self) -> None:
        for path in (HEADER0, OPTIONS, "settings.toml"):
            r = result(self.BEFORE, {**self.BEFORE, SLOT0: "b", path: "x"}, ["slot SWAPFORCE_0", "online"])
            self.assertEqual(r["not_allowed"], [f"changed {path}"], path)

    def test_a_slot_entry_allows_only_a_change_in_place(self) -> None:
        removed = {k: v for k, v in self.BEFORE.items() if k != SLOT0}
        r = result(self.BEFORE, removed, ["slot SWAPFORCE_0"])
        self.assertEqual(r["not_allowed"], [f"removed {SLOT0}"])
        # A slot the phase did not start with: the entry names no profile that has it.
        r = result(removed, self.BEFORE, ["slot SWAPFORCE_0"])
        self.assertEqual(r["not_allowed"], [f"added {SLOT0}"])
        self.assertTrue(r["problems"] and "0 profiles" in r["problems"][0], r["problems"])
        # Another file in the slot's folder is not the slot.
        r = result(self.BEFORE, {**self.BEFORE, SLOT0.replace(".xsg", ".tmp"): "x"}, ["slot SWAPFORCE_0"])
        self.assertEqual(r["not_allowed"], [f"added {SLOT0.replace('.xsg', '.tmp')}"])

    def test_two_profiles_need_the_profile_named(self) -> None:
        before = {**self.BEFORE, SLOT0_P2: "q"}
        r = result(before, {**before, SLOT0: "b"}, ["slot SWAPFORCE_0"])
        self.assertTrue(r["problems"] and "2 profiles" in r["problems"][0], r["problems"])
        self.assertEqual(r["not_allowed"], [f"changed {SLOT0}"])
        r = result(before, {**before, SLOT0: "b"}, [f"slot {P1}/SWAPFORCE_0"])
        self.assertEqual((r["not_allowed"], r["problems"]), ([], []))
        # The second profile's slot stays graded.
        r = result(before, {**before, SLOT0: "b", SLOT0_P2: "z"}, [f"slot {P1}/SWAPFORCE_0"])
        self.assertEqual(r["not_allowed"], [f"changed {SLOT0_P2}"])

    def test_an_orphan_fails_unless_the_case_makes_one(self) -> None:
        after = {**self.BEFORE, "data/save-backup/import-orphaned/7-20261108/txn": "t"}
        r = result(self.BEFORE, after, ["import_journal", "online"])
        self.assertEqual(r["not_allowed"], ["added data/save-backup/import-orphaned/7-20261108/txn"])
        self.assertEqual(result(self.BEFORE, after, ["import_orphaned"])["not_allowed"], [])

    def test_the_native_journal_and_header_only_where_named(self) -> None:
        after = {**self.BEFORE, SLOT0: "b", HEADER0: "i", "data/save-backup/pending/1/manifest.txt": "m"}
        self.assertEqual(len(result(self.BEFORE, after, ["slot SWAPFORCE_0"])["not_allowed"]), 2)
        r = result(self.BEFORE, after, ["slot SWAPFORCE_0", "header SWAPFORCE_0", "native_journal"])
        self.assertEqual(r["not_allowed"], [])

    def test_the_figure_played_and_its_backups(self) -> None:
        after = {**self.BEFORE, "figures/Roller Brawl.dump": "g", "Backups/Figures/Roller Brawl/1.dump": "c"}
        self.assertEqual(result(self.BEFORE, after, ["figure Roller Brawl"])["not_allowed"], [])
        r = result(self.BEFORE, after, ["figure Blast Zone Top"])
        self.assertEqual(len(r["not_allowed"]), 2)
        # A figure file the phase did not start with is not a change of the figure played.
        r = result(self.BEFORE, {**self.BEFORE, "figures/Other.dump": "n"}, ["figure Other"])
        self.assertEqual(r["not_allowed"], ["added figures/Other.dump"])

    def test_a_required_change_must_happen(self) -> None:
        r = result(self.BEFORE, dict(self.BEFORE), ["slot SWAPFORCE_0"], ["slot SWAPFORCE_0"])
        self.assertEqual(r["missing_required"], ["slot SWAPFORCE_0"])
        r = result(self.BEFORE, {**self.BEFORE, SLOT0: "b"}, ["slot SWAPFORCE_0"], ["slot SWAPFORCE_0"])
        self.assertEqual(r["missing_required"], [])


class TreeTest(unittest.TestCase):
    def test_the_tree_is_the_data_root_figures_backups_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            peer = Path(tmp)
            for rel in ("data/a/b.xsg", "data/Online/session/game.log", "figures/F.dump", "Backups/Figures/F/1.dump",
                        "settings.toml", "script.txt", "game.log", "stderr.txt", "cache/x.bin", "shot.png"):
                (peer / rel).parent.mkdir(parents=True, exist_ok=True)
                (peer / rel).write_bytes(rel.encode())
            state = skytest.tree_state(peer)
            self.assertEqual(sorted(state), ["Backups/Figures/F/1.dump", "data/a/b.xsg", "figures/F.dump",
                                             "settings.toml"])


class WatchTest(unittest.TestCase):
    G = {"phase": [{"name": "session", "allow": {}}, {"name": "offline", "allow": {"H": ["slot SWAPFORCE_0"]}}]}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name)
        self.slot = self.run / "H" / SLOT0
        self.slot.parent.mkdir(parents=True)
        self.slot.write_bytes(b"start")
        self.log = self.run / "H" / "game.log"
        self.log.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def say(self, text: str) -> None:
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(f"[2026-11-08 10:00:00.000] [info] [core] [t1] {text}\n")

    def grade(self, watch: skytest.DataRootWatch) -> tuple[skytest.PeerResult, list[str]]:
        r = skytest.PeerResult(peer="H")
        return r, skytest.grade_data_root(r, watch, TITLE)

    def test_each_phase_is_graded_against_its_own_list(self) -> None:
        w = skytest.DataRootWatch(self.run, self.G, ["S", "H"], self.log)
        self.say("Input script: data root phase session done")
        w.check()
        self.slot.write_bytes(b"offline save")
        w.finish()
        r, why = self.grade(w)
        self.assertEqual(why, [])
        self.assertEqual([p["name"] for p in r.graded["data_root_untouched"]["phases"]], ["session", "offline"])
        self.assertEqual(r.graded["data_root_untouched"]["phases"][1]["changes"], [f"changed {SLOT0}"])

    def test_a_change_before_the_phase_line_fails_the_session_phase(self) -> None:
        w = skytest.DataRootWatch(self.run, self.G, ["S", "H"], self.log)
        self.slot.write_bytes(b"session save")
        self.say("Input script: data root phase session done")
        w.check()
        w.finish()
        r, why = self.grade(w)
        self.assertEqual(why, [f"grade data_root_untouched: phase session: not allowed: changed {SLOT0}"])
        self.assertTrue(r.graded["data_root_untouched"]["violation"])

    def test_a_missing_or_wrong_phase_line_fails(self) -> None:
        w = skytest.DataRootWatch(self.run, self.G, ["S", "H"], self.log)
        self.say("Input script: data root phase offline done")
        self.slot.write_bytes(b"save")
        w.finish()
        _, why = self.grade(w)
        self.assertIn("grade data_root_untouched: phase line `offline` out of order (expected session)", why)
        self.assertIn("grade data_root_untouched: phase session did not end: no `data root phase session done`"
                      " line", why)
        # The run's end closes the phase it was in: the session phase is graded with the change.
        self.assertIn(f"grade data_root_untouched: phase session: not allowed: changed {SLOT0}", why)

    def test_nothing_to_compare_fails(self) -> None:
        self.slot.unlink()
        w = skytest.DataRootWatch(self.run, {"allow": {}}, ["S", "H"], self.log)
        w.finish()
        _, why = self.grade(w)
        self.assertTrue(any("nothing to compare" in x for x in why), why)


class SpecCheckTest(unittest.TestCase):
    NAMES = ["S", "H", "D"]

    def bad(self, g: object, server: str = "host") -> None:
        with self.assertRaises(skytest.SpecError, msg=repr(g)):
            skytest.check_data_root("x.toml", g, self.NAMES, server)

    def test_good_grades(self) -> None:
        skytest.check_data_root("x.toml", {}, self.NAMES, "host")
        skytest.check_data_root("x.toml", {"peers": ["D"], "allow": {"D": ["figure Roller Brawl"]}}, self.NAMES, "host")
        skytest.check_data_root("x.toml", {"peers": ["D"], "phase": [
            {"name": "session", "allow": {"D": []}},
            {"name": "offline", "allow": {"D": ["slot SWAPFORCE_0"]}, "require_changed": {"D": ["slot SWAPFORCE_0"]}}]},
            self.NAMES, "host")

    def test_bad_grades(self) -> None:
        self.bad([])
        self.bad({"peers": ["S"]})
        self.bad({"peers": []})
        self.bad({"peers": ["X"]})
        self.bad({"peers": ["D", "D"]})
        self.bad({"by": "S"})
        self.bad({"ignore": ["data/"]})
        self.bad({"allow": {"H": ["slots SWAPFORCE_0"]}})
        self.bad({"allow": {"D": "online"}})
        self.bad({"peers": ["D"], "allow": {"H": ["online"]}})
        self.bad({"phase": []})
        self.bad({"phase": [{"name": "a"}], "allow": {}})
        self.bad({"phase": [{"name": "a"}, {"name": "a"}]})
        self.bad({"phase": [{"name": "a b"}]})
        self.bad({"phase": [{"name": "a", "until": "x"}]})
        self.bad({"require_changed": {"H": ["slot SWAPFORCE_0"]}})
        self.bad({"title": "save"})


class RunTest(unittest.TestCase):
    """Whole runs of the fake game: the runner's snapshots at the start, at phase lines and at the end.
    SkytestTest's fixture and helpers, without its own tests."""
    setUp_base = test_skytest.SkytestTest.setUp
    tearDown = test_skytest.SkytestTest.tearDown
    spec = test_skytest.SkytestTest.spec
    run_cases = test_skytest.SkytestTest.run_cases
    ONE = '''layout = "server+1"
stage = "7"
server = "host"
[peer.H]
script = """
online_host 0
place 0 {figures}/Roller Brawl.dump
%s
quit
"""
'''

    def setUp(self) -> None:
        self.setUp_base()
        # The launch builder checks each script against the game's command table: the fake game's
        # two writers these runs use join the fixture's table (as test_synthetic adds its setting).
        commands = self.repo / "tests" / "script_commands.toml"
        commands.write_text(commands.read_text(encoding="utf-8") + 'datawrite = "none"\nfigurewrite = "none"\n',
                            encoding="utf-8")

    def case(self, name: str, steps: str, grade: str) -> dict:
        self.spec(name, self.ONE % steps + "\n[grade.data_root_untouched]\n" + grade)
        return self.run_cases(name)["cases"][0]

    def h(self, case: dict) -> dict:
        return next(p for p in case["peers"] if p["peer"] == "H")

    def test_the_owners_import_passes_and_anything_else_names_the_file(self) -> None:
        allow = 'allow = { H = ["slot SWAPFORCE_0", "online", "import_journal", "figure Roller Brawl"] }\n'
        case = self.case("droot-ok", "savewrite\ndatawrite save-backup/import/1/txn\nfigurewrite Roller Brawl", allow)
        self.assertEqual(case["status"], "pass", case)
        changes = self.h(case)["graded"]["data_root_untouched"]["phases"][0]["changes"]
        self.assertIn(f"changed {SLOT0}", changes)
        self.assertIn("added Backups/Figures/Roller Brawl/copy.dump", changes)
        case = self.case("droot-bad", "datawrite save-backup/pending/1/manifest.txt", allow)
        self.assertEqual(case["status"], "fail")
        self.assertIn("grade data_root_untouched: phase run: not allowed: added data/save-backup/pending/1/"
                      "manifest.txt", self.h(case)["reasons"])

    def test_phases_end_at_the_scripts_lines(self) -> None:
        grade = ('[[grade.data_root_untouched.phase]]\nname = "session"\nallow = { H = ["online"] }\n'
                 '[[grade.data_root_untouched.phase]]\nname = "offline"\nallow = { H = ["slot SWAPFORCE_0"] }\n'
                 'require_changed = { H = ["slot SWAPFORCE_0"] }\n')
        case = self.case("droot-phases", "log data root phase session done\nwait 200\nsavewrite", grade)
        self.assertEqual(case["status"], "pass", case)
        phases = self.h(case)["graded"]["data_root_untouched"]["phases"]
        self.assertEqual([p["changes"] for p in phases], [[], [f"changed {SLOT0}"]])
        case = self.case("droot-early", "savewrite\nlog data root phase session done\nwait 200", grade)
        self.assertEqual(case["status"], "fail")
        self.assertIn(f"grade data_root_untouched: phase session: not allowed: changed {SLOT0}",
                      self.h(case)["reasons"])
        self.assertIn("grade data_root_untouched: phase offline: 'slot SWAPFORCE_0' must change and did not",
                      self.h(case)["reasons"])


if __name__ == "__main__":
    unittest.main()
