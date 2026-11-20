"""Tests for skytest's drop_tiles grade (drop_tiles.py): the PNG reader, the tile rule (a flat black or
cyan whole cell of the scene's 64-pixel grid beside a cell that isn't), and the verdicts (seen, not seen,
none, tiles, inconclusive) from the round markers and the picture lines. Run: python -I -m unittest
discover -s tools/skytest/tests"""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import drop_tiles as dt  # noqa: E402

# A small picture of the scene's shape: one cell is 64 * 208 / 1040 = 12.8 x 64 * 124 / 620 = 12.8 pixels.
W, H = 208, 124


def png(path: Path, cells: dict[tuple[int, int], tuple[int, int, int]], alpha: bool = False,
        filters: int = 0) -> None:
    """A textured picture (no flat cell), with the given cells filled flat."""
    bpp = 4 if alpha else 3
    rows = []
    prev = bytes(W * bpp)
    for y in range(H):
        line = bytearray()
        for x in range(W):
            colour = cells.get((int(x // 12.8), int(y // 12.8)))
            if colour is None:
                colour = ((x * 37 + y * 11) % 200 + 30, (x * 13 + y * 29) % 180 + 40, (x * 7 + y * 3) % 160 + 50)
            line += bytes(colour) + (b"\xff" if alpha else b"")
        if filters == 2:  # "up", to exercise a filter
            rows.append(b"\x02" + bytes((line[i] - prev[i]) & 255 for i in range(len(line))))
        else:
            rows.append(b"\x00" + bytes(line))
        prev = bytes(line)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", W, H, 8, 6 if alpha else 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows)))
                     + chunk(b"IEND", b""))


def log(*lines: str) -> str:
    return "\n".join(f"[2026-11-20 03:00:{s} ] {t}".replace(" ]", "]") for s, t in lines)


ROUNDS = (("01.000", "[info] Input script: probe round 1"), ("03.000", "[info] Input script: probe round 2"),
          ("05.000", "[info] Input script: probe end"))


class Tiles(unittest.TestCase):
    def test_black_and_cyan_cells_are_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            png(p, {(3, 2): (0, 0, 0), (7, 4): (0, 254, 255)}, filters=2)
            self.assertEqual(dt.tiles(p), [(3, 2, "black"), (7, 4, "cyan")])

    def test_textured_picture_has_none_and_rgba_reads(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            png(p, {}, alpha=True)
            self.assertEqual(dt.tiles(p), [])

    def test_a_black_screen_is_no_tile(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            png(p, {(c, r): (0, 0, 0) for c in range(16) for r in range(10)})
            self.assertEqual(dt.tiles(p), [])


class Verdicts(unittest.TestCase):
    def saved(self, s: str, n: int, name: str, overwriting: int) -> tuple[str, str]:
        return (s, f"[info] [gpu] Pipeline wait: frame {n} saved as {name} ({overwriting} overwriting, 0 "
                   f"depth-writing draws dropped in it; the same swap's guest output, before the host's scaling "
                   f"and presentation; window 3; 1 of 200)")

    def test_seen_needs_the_tile_in_a_frame_with_its_own_overwriting_drop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            png(Path(d) / "f1.png", {(5, 5): (0, 254, 255)})
            png(Path(d) / "f2.png", {(5, 5): (0, 0, 0)})
            text = log(*ROUNDS, self.saved("02.000", 41, "f1.png", 2), self.saved("04.000", 77, "f2.png", 0))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertEqual(why, [])
            self.assertEqual([s["file"] for s in graded["seen"]], ["f1.png"])
            # The depth-only frame alone is not enough.
            text = log(*ROUNDS, self.saved("04.000", 77, "f2.png", 0))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertTrue(why[0].startswith("grade drop_tiles: INCONCLUSIVE"), why)

    def test_seen_without_a_tile_is_not_seen(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            png(Path(d) / "f1.png", {})
            text = log(*ROUNDS, self.saved("02.000", 41, "f1.png", 1))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertTrue(why[0].startswith("grade drop_tiles: NOT SEEN"), why)

    def test_none_reads_every_picture_inside_the_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for i in range(3):
                png(Path(d) / f"s{i}.png", {})
            png(Path(d) / "boot.png", {(2, 2): (0, 0, 0)})  # before round 1: not judged
            lines = [("00.500", "[info] Input script: shot boot.png")] + list(ROUNDS)
            lines += [("01.500", "[info] Input script: shot s0.png"), ("02.500", "[info] Input script: shot s1.png"),
                      ("03.500", "[info] Input script: shot s2.png")]
            text = log(*sorted(lines))
            graded, why = dt.grade(Path(d), text, {"expect": "none", "marker": "probe", "min_frames": 3})
            self.assertEqual((why, graded["readable"]), ([], 3))
            graded, why = dt.grade(Path(d), text, {"expect": "none", "marker": "probe"})
            self.assertTrue(why[0].startswith("grade drop_tiles: INCONCLUSIVE"), why)  # 3 < 6
            png(Path(d) / "s1.png", {(4, 4): (0, 254, 255)})
            graded, why = dt.grade(Path(d), text, {"expect": "none", "marker": "probe", "min_frames": 3})
            self.assertIn("s1.png (round 1, 1 tile(s))", why[0])

    def test_spec_table(self) -> None:
        self.assertIsNone(dt.check_spec({"expect": "none", "marker": "probe"}, ["H"]))
        self.assertIn("expect", dt.check_spec({"expect": "maybe", "marker": "probe"}, ["H"]))
        self.assertIn("marker", dt.check_spec({"expect": "seen"}, ["H"]))
        self.assertIn("peer", dt.check_spec({"expect": "seen", "marker": "p", "peer": "D"}, ["H"]))


if __name__ == "__main__":
    unittest.main()
