"""The suite's temp folder sweep (temp_sweep.py): only its own names, only older than a day, never through a link."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import temp_sweep  # noqa: E402


class Sweep(unittest.TestCase):
    def test_old_own_folders_go_the_rest_stays(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = root / "skytest-no-builds-old"
            new = root / "skytest-no-builds-new"
            other = root / "someone-else-old"
            for p in (old, new, other):
                p.mkdir()
            (old / "f.txt").write_text("x", encoding="utf-8")
            past = time.time() - 2 * 86400
            for p in (old, other):
                os.utime(p, (past, past))
            linked = root / "skytest-junction-target-old"
            linked.mkdir()
            target = root / "target"
            target.mkdir()
            (target / "keep.txt").write_text("x", encoding="utf-8")
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked / "j"), str(target)], capture_output=True)
            os.utime(linked, (past, past))
            n = temp_sweep.sweep(root=root)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists() and other.exists())
            if made.returncode == 0:
                self.assertTrue(linked.exists(), "a folder with a junction inside is left alone")
                self.assertTrue((target / "keep.txt").exists())
                self.assertEqual(n, 1)
                os.rmdir(linked / "j")


if __name__ == "__main__":
    unittest.main()
