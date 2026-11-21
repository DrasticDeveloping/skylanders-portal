"""store_paths.py: the one validator the runner (skytest.py snapshots_setting) and the store janitor
(tools/test_store_janitor.py) share, the check right before a delete, the running games' data folders.
Real junctions (mklink /J) where a link matters.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_store_paths.py"
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import store_paths as sp  # noqa: E402


def junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


class Validator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "game"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.store = self.repo / ".replication-tests"
        self.store.mkdir()
        self.owner = self.root / "owner" / "Skylanders"
        self.owner.mkdir(parents=True)
        self.guards = [("the repository", self.repo), ("the test store", self.store)]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def pool(self, *parts: str, owner_store: Path | None = None) -> Path:
        p = self.root.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        (p / sp.MARKER).write_text(f"{owner_store or self.store}\n", encoding="utf-8")
        return p

    def check(self, text: str) -> tuple[Path | None, str]:
        return sp.validate_snapshots_folder(text, self.store, self.guards, [self.owner])

    def test_the_table(self) -> None:
        good = self.pool("C", "snapshots", "game")
        (self.root / "a-file").write_text("x", encoding="utf-8")
        disc_parent = self.root / "discs"
        (disc_parent / "rom").mkdir(parents=True)
        (disc_parent / "rom" / "default.xex").write_text("x", encoding="utf-8")
        other = self.root / "other"
        (other / ".git").mkdir(parents=True)
        (other / ".git" / "HEAD").write_text("x", encoding="utf-8")
        empty_git = self.root / "home"
        (empty_git / ".git").mkdir(parents=True)  # an empty .git (this machine's profile has one): no checkout
        (self.root / "linked-target").mkdir()
        junction(self.root / "linked", self.root / "linked-target")
        table = {
            str(good): "",
            "relative\\snaps": "not an absolute path",
            "\\\\server\\share\\snaps": "UNC or device",
            "\\\\?\\C:\\snaps": "UNC or device",
            "\\\\.\\C:\\snaps": "UNC or device",
            "//server/share/snaps": "UNC or device",
            str(self.root / "a-file"): "is a file",
            str(Path(self.root.anchor)): "drive root",
            str(self.root / "missing"): "does not exist",
            str(self.pool("game", "snaps")): "git checkout",
            str(self.pool("other", "snaps")): "git checkout",
            str(self.pool("discs")): "disc",  # rom one level down
            str(self.pool("discs", "rom", "snaps")): "holds the disc",
            str(self.pool("owner", "Skylanders", "snaps")): "owner's data",
            str(self.pool("linked-target", "x") and self.root / "linked" / "x"): "junction or link",
            str(self.pool("theirs", owner_store=self.root / "another" / ".replication-tests")): "one folder serves one store",
            str(self.pool("home", "snaps")): "",
        }
        (self.root / "nomarker").mkdir()
        table[str(self.root / "nomarker")] = f"create {self.root / 'nomarker' / sp.MARKER}"
        for text, want in table.items():
            with self.subTest(text):
                folder, why = self.check(text)
                if want:
                    self.assertIsNone(folder, text)
                    self.assertIn(want.lower(), why.lower(), text)
                else:
                    self.assertEqual(why, "", text)
                    self.assertEqual(str(folder), sp.real(text))

    def test_around_a_checkout_and_the_profile(self) -> None:
        around = self.pool("holder")
        inner = around / "repo2"
        (inner / ".git").mkdir(parents=True)
        (inner / ".git" / "HEAD").write_text("x", encoding="utf-8")
        self.assertIn("inside it is a git checkout", self.check(str(around))[1])
        old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = str(self.root / "profile" / "me")
        try:
            (self.root / "profile" / "me").mkdir(parents=True)
            self.assertIn("user profile", sp.protected_reason(self.root / "profile", [], [self.owner]))
            self.assertIn("user profile", sp.protected_reason(self.root / "profile" / "me", [], [self.owner]))
            self.assertEqual(sp.protected_reason(self.pool("profile", "me", "work"), [], [self.owner]), "")
        finally:
            if old is None:
                del os.environ["USERPROFILE"]
            else:
                os.environ["USERPROFILE"] = old


class Victim(unittest.TestCase):
    def test_inside_the_pinned_root_and_no_link(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "pool"
            (root / "a").mkdir(parents=True)
            (Path(d) / "elsewhere").mkdir()
            junction(root / "b", Path(d) / "elsewhere")
            pinned = sp.real(root)
            self.assertEqual(sp.check_victim(root / "a", pinned), "")
            self.assertIn("junction or link", sp.check_victim(root / "b", pinned))
            self.assertIn("outside the pinned folder", sp.check_victim(Path(d) / "elsewhere", pinned))
            self.assertIn("outside the pinned folder", sp.check_victim(root, pinned))  # never the root itself
            os.rmdir(root / "b")


class CannotTell(unittest.TestCase):
    """Re-check 5: a process list or a command line that cannot be read is "cannot tell" (None), never a
    shorter list."""

    def test_unreadable_is_none(self) -> None:
        real_lines, real_split = sp.command_lines, sp.split_command_line
        try:
            sp.command_lines = lambda exe: None
            self.assertIsNone(sp.data_roots_in_use("x.exe"))
            sp.command_lines = lambda exe: ['x.exe "--user_data_root=C:/a b"']
            sp.split_command_line = lambda line: None
            self.assertIsNone(sp.data_roots_in_use("x.exe"))
        finally:
            sp.command_lines, sp.split_command_line = real_lines, real_split
        self.assertEqual(sp.split_command_line('x.exe "--user_data_root=C:/a b" -y'),
                         ["x.exe", "--user_data_root=C:/a b", "-y"])


class DangerInside(unittest.TestCase):
    def test_a_disc_a_rom_folder_or_a_checkout_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel, made in (("a/b/default.xex", "file"), ("a/b/rom", "dir"), ("a/b/.git", "file"),
                              ("a/b/.git", "dir")):
                victim = root / f"v{len(rel)}{made}"
                p = victim / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.mkdir() if made == "dir" else p.write_text("x", encoding="utf-8")
                with self.subTest(rel=rel, made=made):
                    self.assertNotEqual(sp.danger_inside(victim), "")
                import shutil as _sh
                _sh.rmtree(victim)
            plain = root / "plain"
            (plain / "x").mkdir(parents=True)
            self.assertEqual(sp.danger_inside(plain), "")


@unittest.skipUnless(os.name == "nt", "Windows process command lines")
class LiveGames(unittest.TestCase):
    def test_a_running_programs_data_folder(self) -> None:
        """A copy of cmd.exe under a name no other process has, its command line carrying a data folder."""
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "sfo_probe_test.exe"
            shutil.copy2(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "cmd.exe", exe)
            data = Path(d) / "run folder" / "H" / "data"
            data.mkdir(parents=True)
            p = subprocess.Popen([str(exe), "/c", "ping", "-n", "30", "127.0.0.1", ">nul", "&", "rem",
                                  f"--user_data_root={data}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    roots = sp.data_roots_in_use(exe.name)
                    if roots:
                        break
                    time.sleep(0.1)
                self.assertEqual(roots, [sp.real(data)])
            finally:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
                p.wait(10)
        self.assertEqual(sp.data_roots_in_use("no_such_program_here.exe"), [])


if __name__ == "__main__":
    unittest.main()
