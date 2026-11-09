"""The licence texts under licenses/ are the vendored libraries' own, word for word (line endings
aside), so a submodule bump that changes a licence fails here instead of shipping the old text.
miniupnpc's licence is the repository's top-level LICENSE: miniupnpc/LICENSE is a git symlink to it,
which a Windows checkout without symlinks writes as the 10-byte text "../LICENSE". A copy whose
source is not checked out is skipped. Run:
python -I -m unittest discover -s tools -p "licence_copies_test.py"
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = {
    "licenses/miniupnpc-LICENSE.txt": "thirdparty/miniupnp/LICENSE",
}


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


class LicenceCopiesTest(unittest.TestCase):
    def test_copies_match_their_sources(self) -> None:
        for copy, source in COPIES.items():
            with self.subTest(copy=copy):
                self.assertTrue((ROOT / copy).is_file(), f"{copy} missing")
                if not (ROOT / source).is_file():
                    self.skipTest(f"{source} not checked out")
                self.assertEqual(lines(ROOT / copy), lines(ROOT / source))


if __name__ == "__main__":
    unittest.main()
