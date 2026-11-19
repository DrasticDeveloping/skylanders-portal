"""The output guard's containment test (skytest.inside) on paths being made by another thread: the flip of
test_unit_cases_run_through_the_scheduler_without_machine_slots (18 November). Two unit cases of one run
make their snapshots at once; Python's realpath of the second one's partial folder, while the first made
`snapshots`, kept the \\\\?\\ prefix and the guard called the folder "outside the test store".
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_inside.py"
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import skytest  # noqa: E402


class Inside(unittest.TestCase):
    def test_the_prefix_forms(self):
        self.assertEqual(skytest.plain_path(Path("\\\\?\\C:\\a\\b")), Path("C:\\a\\b"))
        self.assertEqual(skytest.plain_path(Path("\\\\?\\UNC\\srv\\share\\x")), Path("\\\\srv\\share\\x"))
        self.assertEqual(skytest.plain_path(Path("C:\\a")), Path("C:\\a"))

    def test_a_path_not_made_yet_is_inside_its_store(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / ".replication-tests"
            store.mkdir()
            self.assertTrue(skytest.inside(store / "snapshots" / "unit-x.partial-1-2", store))
            self.assertFalse(skytest.inside(Path(d) / "elsewhere" / "y", store))
            self.assertFalse(skytest.inside(store / ".." / "elsewhere", store))

    @unittest.skipUnless(os.name == "nt", "the race is in Windows' realpath")
    def test_inside_while_another_thread_makes_the_parent(self):
        refused = checks = 0
        for _ in range(150):
            root = Path(tempfile.mkdtemp(prefix="inside-race-"))
            try:
                store = root / ".replication-tests"
                store.mkdir()
                partial = store / "snapshots" / "unit-abc.partial-1-2"
                t = threading.Thread(target=lambda: (store / "snapshots" / "unit-abc.partial-9-9").mkdir(parents=True))
                t.start()
                while True:
                    checks += 1
                    refused += not skytest.inside(partial, store)
                    if not t.is_alive():
                        break
                t.join()
            finally:
                shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(refused, 0, f"{refused} of {checks} checks called a folder of the store outside it")


if __name__ == "__main__":
    unittest.main()
