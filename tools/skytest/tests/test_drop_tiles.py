"""Tests for skytest's drop_tiles grade (drop_tiles.py): the PNG reader (a malformed file is unreadable, never
an exception), the tile rule (a flat black or cyan whole cell of the scene's 64-pixel grid beside a cell that
isn't), and the verdicts (seen, not seen, none, needs reading, inconclusive, inconclusive (geometry)) from the
script's prescribed shots, the round markers and the saved frames' capture times. Run: python -I -m unittest
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
        filters: int = 0, width: int = W) -> None:
    """A textured picture (no flat cell), with the given cells filled flat."""
    W = width  # noqa: N806
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
    def saved(self, logged: str, n: int, name: str, overwriting: int) -> tuple[str, str]:
        return (logged, f"[info] [gpu] Pipeline wait: frame {n} saved as {name} ({overwriting} overwriting, 0 "
                        f"depth-writing draws dropped in it; the same swap's guest output, before the host's "
                        f"scaling and presentation; window 3; 1 of 200)")

    @staticmethod
    def script(folder: Path, per_round: int = 2) -> None:
        lines = ["wait 1", "shot boot", "log probe round 1"] + [f"shot r1-{i}" for i in range(per_round)]
        lines += ["log probe round 2"] + [f"shot r2-{i}" for i in range(per_round)] + ["log probe end"]
        (folder / "script.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_prescribed_shots_come_from_the_script(self) -> None:
        text = "log probe round 1; shot a # one\nshot b\nlog probe round 2\nshot c.png\nlog probe end\nshot after\n"
        self.assertEqual(dt.prescribed_shots(text, "probe"), {1: ["a.png", "b.png"], 2: ["c.png"]})

    def test_seen_needs_the_tile_in_a_frame_with_its_own_overwriting_drop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            png(Path(d) / "pipeline-drop-f41-030002-000.png", {(5, 5): (0, 254, 255)})
            png(Path(d) / "pipeline-drop-f77-030004-000.png", {(5, 5): (0, 0, 0)})
            text = log(*ROUNDS, self.saved("02.100", 41, "pipeline-drop-f41-030002-000.png", 2),
                       self.saved("04.100", 77, "pipeline-drop-f77-030004-000.png", 0))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertEqual((why, graded["verdict"]), ([], "seen"))
            self.assertEqual([s["file"] for s in graded["seen"]], ["pipeline-drop-f41-030002-000.png"])
            self.assertTrue((Path(d) / "drop_tiles.md").is_file())
            # The depth-only frame alone: a tile without its own overwriting drop needs reading, and the
            # grade neither passes nor fails the case.
            text = log(*ROUNDS, self.saved("04.100", 77, "pipeline-drop-f77-030004-000.png", 0))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertEqual((why, graded["verdict"]), ([], "needs reading"))
            self.assertIn("NEEDS READING", graded["note"])
            self.assertTrue((Path(d) / "drop_tiles" / "pipeline-drop-f77-030004-000-c5r5.png").is_file())

    def test_the_capture_time_decides_the_round_not_the_write(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            png(Path(d) / "pipeline-drop-f9-030000-900.png", {(5, 5): (0, 0, 0)})
            # Captured before round 1's marker, written (logged) after it: not an attack frame.
            text = log(*ROUNDS, self.saved("01.200", 9, "pipeline-drop-f9-030000-900.png", 3))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertTrue(why[0].startswith("grade drop_tiles: INCONCLUSIVE"), why)
            # Captured in round 2, logged after the end marker: still round 2's.
            png(Path(d) / "pipeline-drop-f10-030004-950.png", {(5, 5): (0, 0, 0)})
            text = log(*ROUNDS, self.saved("05.300", 10, "pipeline-drop-f10-030004-950.png", 3))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertEqual((why, graded["verdict"], graded["seen"][0]["round"]), ([], "seen", 2))

    def test_the_level_start_window_counts_when_asked(self) -> None:
        # Batch 18d: with the fix off every hole-making drop came before round 1. Without level_start such a frame
        # is not graded (INCONCLUSIVE); with it, the frame from the named line to round 1's marker is round 0.
        with tempfile.TemporaryDirectory() as d:
            png(Path(d) / "pipeline-drop-f5-030000-500.png", {(5, 5): (0, 0, 0)})
            png(Path(d) / "pipeline-drop-f2-025959-000.png", {(4, 4): (0, 0, 0)})
            lines = (("00.100", "[info] Dev: level tutorial_b: requested (request 1)"),
                     self.saved("00.600", 5, "pipeline-drop-f5-030000-500.png", 290)) + ROUNDS
            early = ("00.050", "[info] [gpu] Pipeline wait: frame 2 saved as pipeline-drop-f2-025959-000.png (9 "
                     "overwriting, 0 depth-writing draws dropped in it; window 1; 1 of 200)")
            text = log(early, *lines)
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertTrue(why[0].startswith("grade drop_tiles: INCONCLUSIVE"), why)
            g = {"expect": "seen", "marker": "probe", "level_start": r"Dev: level tutorial_b: requested"}
            graded, why = dt.grade(Path(d), text, g)
            self.assertEqual((why, graded["verdict"]), ([], "seen"))
            self.assertEqual([(s["file"], s["round"]) for s in graded["seen"]],
                             [("pipeline-drop-f5-030000-500.png", 0)])  # the frame before the line stays out
            self.assertIn("round 0 is the level start", (Path(d) / "drop_tiles.md").read_text(encoding="utf-8"))
            # A line that never comes before round 1: no window, as before.
            graded, why = dt.grade(Path(d), text, {**g, "level_start": "never"})
            self.assertTrue(why[0].startswith("grade drop_tiles: INCONCLUSIVE"), why)

    def test_seen_without_a_tile_is_not_seen(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            png(Path(d) / "pipeline-drop-f41-030002-000.png", {})
            text = log(*ROUNDS, self.saved("02.100", 41, "pipeline-drop-f41-030002-000.png", 1))
            graded, why = dt.grade(Path(d), text, {"expect": "seen", "marker": "probe"})
            self.assertTrue(why[0].startswith("grade drop_tiles: NOT SEEN"), why)

    def test_none_needs_every_prescribed_picture_usable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self.script(folder)
            png(folder / "boot.png", {(2, 2): (0, 0, 0)})  # before round 1: not judged
            for name in ("r1-0", "r1-1", "r2-0"):
                png(folder / f"{name}.png", {})
            g = {"expect": "none", "marker": "probe"}
            graded, why = dt.grade(folder, log(*ROUNDS), g)  # r2-1 missing
            self.assertTrue(why[0].startswith("grade drop_tiles: INCONCLUSIVE: 1 of 4"), why)
            # A fade to black at an attack moment is not gameplay.
            png(folder / "r2-1.png", {(c, r): (0, 0, 0) for c in range(16) for r in range(10)})
            graded, why = dt.grade(folder, log(*ROUNDS), g)
            self.assertIn("not gameplay", why[0])
            # A truncated file is unreadable, never an exception.
            (folder / "r2-1.png").write_bytes((folder / "r1-0.png").read_bytes()[:30])
            graded, why = dt.grade(folder, log(*ROUNDS), g)
            self.assertIn("unreadable", why[0])
            png(folder / "r2-1.png", {})
            graded, why = dt.grade(folder, log(*ROUNDS), g)
            self.assertEqual((why, graded["verdict"], graded["usable_shots"]), ([], "none", 4))
            # A tile in a fix-on picture needs reading: the grade does not fail the case on it.
            png(folder / "r1-1.png", {(4, 4): (0, 254, 255)})
            graded, why = dt.grade(folder, log(*ROUNDS), g)
            self.assertEqual((why, graded["verdict"]), ([], "needs reading"))
            self.assertEqual(graded["candidates"][0]["cell"], [4, 4])

    def test_a_picture_not_of_the_scene_shape_is_inconclusive_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / "script.txt").write_text("log probe round 1\nshot sq\nlog probe end\n", encoding="utf-8")
            png(folder / "sq.png", {}, width=124)
            graded, why = dt.grade(folder, log(ROUNDS[0], ROUNDS[2]), {"expect": "none", "marker": "probe"})
            self.assertEqual(graded["verdict"], "inconclusive (geometry)")
            self.assertIn("INCONCLUSIVE (geometry)", why[0])

    def test_malformed_pngs_are_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            png(p, {})
            good = p.read_bytes()
            short_ihdr = good[:8] + struct.pack(">I", 2) + b"IHDR\x00\x00" + struct.pack(">I", 0) + good[33:]
            for bad in (good[:20], short_ihdr, good[:-12], b"junk", good[:8] + b"\xff\xff\xff\xffIDAT"):
                p.write_bytes(bad)
                with self.assertRaises(dt.Unreadable):
                    dt.tiles(p)

    def test_spec_table(self) -> None:
        self.assertIsNone(dt.check_spec({"expect": "none", "marker": "probe"}, ["H"]))
        self.assertIn("expect", dt.check_spec({"expect": "maybe", "marker": "probe"}, ["H"]))
        self.assertIn("marker", dt.check_spec({"expect": "seen"}, ["H"]))
        self.assertIn("peer", dt.check_spec({"expect": "seen", "marker": "p", "peer": "D"}, ["H"]))
        self.assertIn("level_start", dt.check_spec({"expect": "seen", "marker": "p", "level_start": "("}, ["H"]))
        self.assertIsNone(dt.check_spec({"expect": "seen", "marker": "p", "level_start": "Dev: level x"}, ["H"]))


if __name__ == "__main__":
    unittest.main()
