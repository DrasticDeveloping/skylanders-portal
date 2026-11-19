"""The build snapshots outside the store (the store's snapshots-folder.txt), the cleanup after a run and
`prune` (both through the game repository's janitor), and a store behind a directory junction.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_snapshots_folder.py"
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import skytest  # noqa: E402
import test_skytest as T  # noqa: E402

# The game repository's janitor, when this portal is a game repository's submodule (thirdparty/<portal>).
JANITOR = HERE.parents[4] / "tools" / "test_store_janitor.py"


def setUpModule() -> None:
    T.setUpModule()


def tearDownModule() -> None:
    T.tearDownModule()


def junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def fake_snapshot(folder: Path, name: str) -> Path:
    d = folder / name
    d.mkdir(parents=True)
    (d / "swapforcerecompiled.exe").write_bytes(b"MZ")
    (d / "runtime.dll").write_bytes(b"MZ")
    return d


class SnapshotsFolderTest(unittest.TestCase):
    HUB = T.SkytestTest.HUB
    setUp = T.SkytestTest.setUp
    tearDown = T.SkytestTest.tearDown
    spec = T.SkytestTest.spec
    run_cases = T.SkytestTest.run_cases
    routes_in_the_build = T.SkytestTest.routes_in_the_build

    def name(self, text: str) -> None:
        (self.cfg.store / skytest.SNAPSHOTS_FILE).write_text(text, encoding="utf-8")

    def reload(self) -> skytest.Config:
        launcher = self.cfg.launcher
        cfg = skytest.load_config(self.repo)
        cfg.start_gap, cfg.launcher = 0.0, launcher
        return cfg

    def test_without_the_file_the_snapshots_stay_in_the_store(self) -> None:
        self.assertIsNone(self.cfg.snapshots)
        self.assertEqual(skytest.snapshots_root(self.cfg), self.cfg.store / "snapshots")

    def test_a_run_takes_its_snapshot_and_its_build_copy_from_the_named_folder(self) -> None:
        outside = Path(self.tmp.name) / "internal" / "snapshots" / "wt"
        self.name(f"# the snapshots stay on the internal drive\n{outside}\n")
        with self.assertRaises(SystemExit) as no_marker:  # no folder, no marker: refused, with what to create
            self.reload()
        self.assertIn(store_marker := "SFO-TEST-STORE.txt", str(no_marker.exception))
        outside.mkdir(parents=True)
        (outside / store_marker).write_text(f"{os.path.abspath(self.cfg.store)}\n", encoding="utf-8")
        self.cfg = self.reload()
        self.assertEqual(self.cfg.snapshots, Path(skytest.store_paths.real(outside)))
        self.assertTrue(outside.is_dir())
        self.routes_in_the_build()
        self.spec("copied", 'layout = "solo"\nstage = "8"\n'
                  '[build_copy]\nremove = [\'routes\\tutorial_b.toml\']\n[peer.H]\n' + self.HUB)
        self.spec("shared", 'layout = "solo"\nstage = "8"\n[peer.H]\n' + self.HUB)
        r = self.run_cases("copied", "shared")
        self.assertEqual({c["case"]: c["status"] for c in r["cases"]}, {"copied": "pass", "shared": "pass"}, r)
        snap = Path(r["snapshot"])
        self.assertEqual(skytest.store_paths.real(snap.parent), skytest.store_paths.real(outside))
        self.assertTrue((snap / "game.exe").is_file())
        self.assertFalse((self.cfg.store / "snapshots").exists())
        # The guard takes the named folder and the store, nothing else.
        skytest.check_output_path(self.cfg, outside / "x")
        with self.assertRaises(SystemExit):
            skytest.check_output_path(self.cfg, outside.parent / "other")

    def test_a_bad_snapshots_folder_is_refused(self) -> None:
        root = Path(self.tmp.name)
        owner = root / "owner-docs" / "Skylanders"
        owner.mkdir(parents=True)
        self.addCleanup(setattr, skytest, "owner_data_roots", skytest.owner_data_roots)
        skytest.owner_data_roots = lambda: [owner]
        (root / "linked-target").mkdir()
        junction(root / "linked", root / "linked-target")
        bad = {
            "relative": "snaps\\wt\n",
            "two lines": f"{root / 'a'}\n{root / 'b'}\n",
            "a drive root": f"{Path(root.anchor)}\n",
            "inside the repository": f"{self.repo / 'snaps'}\n",
            "around the repository": f"{root}\n",
            "inside the store": f"{self.cfg.store / 'snaps'}\n",
            "inside the disc": f"{self.cfg.rom / 'snaps'}\n",
            "inside the owner's data": f"{owner / 'Swap Force' / 'snaps'}\n",
            "through a junction": f"{root / 'linked' / 'snaps'}\n",
        }
        for why, text in bad.items():
            with self.subTest(why):
                self.name(text)
                with self.assertRaises(SystemExit):
                    skytest.load_config(self.repo)

    def session(self, stamp: str, snapshot: str | None, in_progress: bool = False) -> None:
        d = self.cfg.store / "runs" / f"{stamp}-session"
        d.mkdir(parents=True)
        if snapshot is not None:
            payload = {"snapshot": str(self.cfg.store / "snapshots" / snapshot)}
            if in_progress:
                payload["in_progress"] = "1 of 3 case(s) ended"
            (d / "results.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_the_snapshots_in_use(self) -> None:
        now = time.time()
        stamp = lambda hours: time.strftime("%Y%m%d-%H%M%S", time.localtime(now - hours * 3600))  # noqa: E731
        self.session(stamp(50), "old")
        self.session(stamp(40), "prev")
        self.session(stamp(30), "cur")
        self.session(stamp(2), "lane", in_progress=True)
        self.session(stamp(1), "cur")
        self.assertEqual(skytest.snapshots_in_use(self.cfg, "cur"), ["cur", "prev", "lane"])
        # A second session started in the same second is `-session-2` (fresh_dir): its snapshot is kept too.
        d = self.cfg.store / "runs" / f"{stamp(1.5)}-session-2"
        d.mkdir(parents=True)
        (d / "results.json").write_text(json.dumps({"snapshot": "x/lane2", "in_progress": "starting"}), encoding="utf-8")
        self.assertEqual(skytest.snapshots_in_use(self.cfg, "cur"), ["cur", "prev", "lane2", "lane"])  # newest session first
        # A young session with no results.json yet: its snapshot is unknown, nothing goes.
        self.session(stamp(0.5), None)
        self.assertIsNone(skytest.snapshots_in_use(self.cfg, "cur"))

    def test_prune_hours_must_be_finite_and_above_zero(self) -> None:
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            with self.assertRaises(SystemExit, msg=str(bad)):
                skytest.cmd_prune(self.cfg, argparse.Namespace(older_than=bad, dry_run=True))

    def with_janitor(self) -> None:
        if not JANITOR.is_file():
            self.skipTest("no game repository janitor beside this portal")
        (self.repo / "tools").mkdir(exist_ok=True)
        shutil.copy2(JANITOR, self.repo / "tools" / JANITOR.name)
        # Its store_paths import, as in a game repository (a copy: no link to the real portal in a temp folder).
        portal = self.repo / "thirdparty" / "skylanders-portal" / "tools" / "skytest"
        portal.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE.parent / "store_paths.py", portal / "store_paths.py")

    def test_after_a_run_only_the_current_build_and_the_one_before_stay(self) -> None:
        self.with_janitor()
        snaps = self.cfg.store / "snapshots"
        fake_snapshot(snaps, "aaaaaaaaaaaa")
        fake_snapshot(snaps, "bbbbbbbbbbbb")
        self.session(time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 7200)), "bbbbbbbbbbbb")
        self.spec("solo", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        r = self.run_cases("solo")
        current = Path(r["snapshot"]).name
        self.assertEqual(sorted(p.name for p in snaps.iterdir()), sorted([current, "bbbbbbbbbbbb"]))

    def test_prune_removes_old_runs_only(self) -> None:
        self.with_janitor()
        runs = self.cfg.store / "runs"
        old, new = runs / "20200101-000000-old", runs / time.strftime("%Y%m%d-%H%M%S-new")
        for d in (old, new):
            d.mkdir(parents=True)
            (d / "log.txt").write_text("x", encoding="utf-8")
            (d / "result.json").write_text("{}", encoding="utf-8")  # finished runs
        t = time.time() - 3 * 86400
        os.utime(old / "log.txt", (t, t))
        os.utime(old / "result.json", (t, t))
        snap = fake_snapshot(self.cfg.store / "snapshots", "cccccccccccc")
        os.utime(snap / "runtime.dll", (t, t))
        self.assertEqual(skytest.cmd_prune(self.cfg, argparse.Namespace(older_than=24.0, dry_run=False)), 0)
        self.assertFalse(old.exists())
        self.assertTrue(new.is_dir())
        self.assertTrue(snap.is_dir())

    def test_a_store_behind_a_junction_runs_and_cleans_in_its_target(self) -> None:
        """The owner's stores since 19 November: .replication-tests is a junction to another drive."""
        self.with_janitor()
        target = Path(self.tmp.name) / "external" / "tests" / "wt"
        shutil.move(str(self.cfg.store), str(target))
        junction(self.cfg.store, target)
        # The owner's marker in the target: without it the cleanup is refused (store cleanup review, item 1).
        (target / "SFO-TEST-STORE.txt").write_text(f"{os.path.abspath(self.cfg.store)}\n", encoding="utf-8")
        fake_snapshot(target / "snapshots", "dddddddddddd")
        self.spec("solo", 'layout = "solo"\nstage = "0"\n[peer.H]\nscript = "quit\\n"\n')
        r = self.run_cases("solo")
        self.assertEqual(r["cases"][0]["status"], "pass", r)
        current = Path(r["snapshot"]).name
        self.assertEqual([p.name for p in (target / "snapshots").iterdir()], [current])
        self.assertTrue((target / "SFO-TEST-STORE.txt").is_file())
        self.assertTrue(os.path.isjunction(self.cfg.store))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(skytest.cmd_prune(self.cfg, argparse.Namespace(older_than=1000.0, dry_run=True)), 0)


if __name__ == "__main__":
    unittest.main()
