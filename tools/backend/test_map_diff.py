"""MD3 (stage 2 design section 10): map_diff.py parses the library's golden map dump and agrees with
`skylanders_backend_test --diff` on it, and joins the golden compare lines to the same results as
the library's JoinCompare. Run:

    python -I -m unittest discover -s tools/backend

With SKYLANDERS_BACKEND_TEST set to a built skylanders_backend_test(.exe), the agreement with
--diff is also checked by running the binary on the same dumps; without it that one test is skipped
and says so.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import map_diff  # noqa: E402

GOLDEN = HERE.parent.parent / "tests" / "backend" / "golden"
GATE_A = "a00000010.c17d6562.968d3732@1"
GATE_B = "a00000011.c07d63cf.968d3732@1"


def golden(name: str) -> str:
    # A checkout may have CRLF line ends; the library writes LF (backend_main.cpp CheckGolden).
    return (GOLDEN / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def row(kind: str, entity: str, slot: int, rev: int, value: bytes, tomb: bool = False,
        server_only: bool = False) -> str:
    return (f"row {kind} {entity} slot {slot} rev {rev} lsn 1 epoch 1 writer 0"
            f"{' tomb' if tomb else ''}{' server-only' if server_only else ''} "
            f"value {value.hex() or '-'} digest {map_diff.hex64(map_diff.value_digest(value))}")


def variant_b() -> str:
    """The golden dump with one change of each kind DiffMaps names, and a server-only change it
    must leave out."""
    lines = golden("map.txt").rstrip("\n").split("\n")
    out = [lines[0]]  # still 5 rows: one dropped, one added
    for line in lines[1:]:
        t = line.split()
        if t[0] != "row":
            out.append(line)
            continue
        key = (t[1], t[2], int(t[4]))
        if key == ("md.count", "p1/1", 0):
            continue  # only-a
        if key == ("md.count", "p1/1", 1):
            out.append(row("md.count", "p1/1", 1, 1, b"\x08\x00"))  # value
        elif key == ("md.gate", GATE_A, 0):
            out.append(row("md.gate", GATE_A, 0, 3, b"\x02"))  # revision
        elif key == ("md.gate", GATE_B, 0):
            out.append(row("md.gate", GATE_B, 0, 2, b"\x01"))  # tomb (live in b)
        elif t[1] == "md.secret":
            out.append(row("md.secret", t[2], 0, 9, b"\xff", server_only=True))  # left out
            out.append(row("md.zz", "q/1", 0, 1, b"\x01"))  # only-b
        else:
            out.append(line)
    return "\n".join(out) + "\n"


EXPECTED_B = [
    "md.count p1/1 slot 0 only-a a rev 1 b -",
    "md.count p1/1 slot 1 value a 08285607b4e2c672 b "
    + map_diff.hex64(map_diff.value_digest(b"\x08\x00")),
    f"md.gate {GATE_A} slot 0 revision a 2 b 3",
    f"md.gate {GATE_B} slot 0 tomb a tomb b live",
    "md.zz q/1 slot 0 only-b a - b rev 1",
]


class MapDumpTest(unittest.TestCase):
    def test_the_golden_dump_parses_as_the_library_wrote_it(self) -> None:
        d = map_diff.parse_map(golden("map.txt"))
        self.assertEqual((d.who, d.head, d.prefix, d.epoch, d.rows, d.bytes), ("S", 8, 8, 1, 5, 7))
        self.assertEqual(d.table, 0xC1FF1DF2E76B85C6)
        self.assertEqual([(r.kind, r.entity, r.slot) for r in d.row_lines],
                         [("md.count", "p1/1", 0), ("md.count", "p1/1", 1), ("md.gate", GATE_A, 0),
                          ("md.gate", GATE_B, 0), ("md.secret", "a00000020.45496baf.d432914f@1", 0)])
        self.assertEqual([r.server_only for r in d.row_lines], [False, False, False, False, True])
        self.assertEqual([r.tomb for r in d.row_lines], [False, False, False, True, False])
        self.assertEqual(d.row_lines[3].value_hex, "")
        self.assertEqual(d.row_lines[4].value_hex, "abcd")
        self.assertEqual(len(d.other_lines), 4)
        self.assertTrue(d.other_lines[0].startswith("obligation 1 to 1 md.gate "))
        # The empty value's digest is FNV-1a's offset basis (the tomb row).
        self.assertEqual(d.row_lines[3].digest, 0xCBF29CE484222325)

    def test_the_golden_dump_against_itself(self) -> None:
        d = map_diff.parse_map(golden("map.txt"))
        self.assertEqual(map_diff.diff_text(d, d), ("map diff S S: 0 differences\n", 0))

    def test_one_difference_per_key_in_key_order(self) -> None:
        a = map_diff.parse_map(golden("map.txt"))
        b = map_diff.parse_map(variant_b())
        diffs = map_diff.diff_maps(a, b)
        self.assertEqual([x.text for x in diffs], EXPECTED_B)
        self.assertEqual([x.what for x in diffs], ["only-a", "value", "revision", "tomb", "only-b"])
        text, code = map_diff.diff_text(a, b)
        self.assertEqual(code, 1)
        self.assertEqual(text.splitlines()[-1], "map diff S S: 5 differences")
        # lsn, epoch and writer are not compared (two peers' logs differ there).
        moved = golden("map.txt").replace(" lsn 4 epoch 0 writer 0 ", " lsn 40 epoch 3 writer 2 ")
        self.assertEqual(map_diff.diff_maps(a, map_diff.parse_map(moved)), [])

    def test_parse_errors_name_the_line_in_the_librarys_words(self) -> None:
        text = golden("map.txt")
        lines = text.split("\n")
        cases = {
            "line 2: digest is not the value's": text.replace("value 0500 digest", "value 0501 digest"),
            "line 11: the header says rows 6, the dump has 5": text.replace("rows 5", "rows 6"),
            "line 1: a line before the map header": "\n".join(lines[1:2] + lines),
            "line 2: unknown line 'rows'": text.replace("\nrow ", "\nrows ", 1),
            "line 2: value hex": text.replace("value 0500", "value 050"),
            "line 2: slot": text.replace("p1/1 slot 0", "p1/1 slot 65536", 1),
            "line 2: rev": text.replace("slot 0 rev 1 lsn 4", "slot 0 rev -1 lsn 4"),
            "line 1: who (S or peer <n>)": text.replace("map S", "map X"),
            "line 1: table": text.replace("table c1ff1df2e76b85c6", "table 0xc1ff1df2e76b85c6"),
            "line 11: a second map header": text + lines[0] + "\n",
            "line 1: no map header": "",
        }
        for want, bad in cases.items():
            with self.subTest(want=want):
                with self.assertRaises(map_diff.MapError) as e:
                    map_diff.parse_map(bad)
                self.assertEqual(str(e.exception), want)
        # Upper-case hex is read, and kept lower-case.
        d = map_diff.parse_map(text.replace("value abcd", "value ABCD"))
        self.assertEqual(d.row_lines[4].value_hex, "abcd")
        # A peer's dump.
        self.assertEqual(map_diff.parse_map(text.replace("map S", "map peer 2")).who, "peer 2")

    def test_dumps_in_a_game_log(self) -> None:
        def log(lines: list[str]) -> str:
            return "".join(f"[2026-11-07 12:00:00.{i:03d}] [info] [core] [t1] {x}\n" for i, x in enumerate(lines))

        first = golden("map.txt").rstrip("\n").split("\n")
        second = variant_b().rstrip("\n").split("\n")
        lines = (["Skylanders: Swap Force (build x)", "Online: peer 2 gone at close"]
                 + [f"Online: {x}" for x in first[:3]]
                 + ["Online: frames 600 fps 60.0 p50 16.0 p95 17.0 max 20.0 private 1 working 1 video 1",
                    "Online: peer 3 joined"]
                 + [f"Online: {x}" for x in first[3:]]
                 + [f"Online: {x}" for x in second])
        text = log(lines)
        dumps = map_diff.dumps_in_log(text)
        self.assertEqual(len(dumps), 2)
        self.assertEqual(map_diff.diff_maps(dumps[0], map_diff.parse_map(golden("map.txt"))), [])
        self.assertEqual([x.text for x in map_diff.diff_maps(dumps[0], dumps[1])], EXPECTED_B)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "game.log"
            p.write_text(text, encoding="utf-8")
            self.assertEqual(map_diff.load_dump(p, 0).rows, 5)
            self.assertEqual(map_diff.load_dump(p).row_lines[-1].kind, "md.zz")
            with self.assertRaises(map_diff.MapError):
                map_diff.load_dump(p, 2)
            # An error names the log's own line number.
            p.write_text(text.replace("value 0500 digest", "value 0501 digest"), encoding="utf-8")
            with self.assertRaises(map_diff.MapError) as e:
                map_diff.load_dump(p, 0)
            self.assertEqual(str(e.exception), "line 4: digest is not the value's")

    def test_the_command_line_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.txt", Path(tmp) / "b.txt"
            a.write_text(golden("map.txt"), encoding="utf-8")
            b.write_text(variant_b(), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = map_diff.main(["diff", str(a), str(b)])
            self.assertEqual(code, 1)
            self.assertEqual(out.getvalue().splitlines(), EXPECTED_B + ["map diff S S: 5 differences"])
            b.write_text("map S head 1\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = map_diff.main(["diff", str(a), str(b)])
            self.assertEqual(code, 2)
            self.assertEqual(err.getvalue().strip(), f"{b}: line 1: prefix")

    @unittest.skipUnless(os.environ.get("SKYLANDERS_BACKEND_TEST"),
                         "set SKYLANDERS_BACKEND_TEST to a built skylanders_backend_test to compare with --diff")
    def test_agrees_with_the_backend_binary(self) -> None:
        exe = os.environ["SKYLANDERS_BACKEND_TEST"]
        with tempfile.TemporaryDirectory() as tmp:
            a, b, bad = Path(tmp) / "a.txt", Path(tmp) / "b.txt", Path(tmp) / "bad.txt"
            a.write_text(golden("map.txt"), encoding="utf-8", newline="\n")
            b.write_text(variant_b(), encoding="utf-8", newline="\n")
            bad.write_text(golden("map.txt").replace("rows 5", "rows 6"), encoding="utf-8", newline="\n")
            for x, y in ((a, a), (a, b), (b, a), (b, b)):
                with self.subTest(a=x.name, b=y.name):
                    r = subprocess.run([exe, "--diff", str(x), str(y)], capture_output=True, text=True, timeout=60)
                    text, code = map_diff.diff_text(map_diff.load_dump(x), map_diff.load_dump(y))
                    self.assertEqual((r.stdout, r.returncode), (text, code))
            r = subprocess.run([exe, "--diff", str(a), str(bad)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            with self.assertRaises(map_diff.MapError) as e:
                map_diff.load_dump(bad)
            self.assertEqual(r.stderr.strip(), f"{bad}: {e.exception}")


def sections(text: str) -> dict[str, list[str]]:
    """The golden compare file's cases: a `# <id> <name>` line and the lines under it."""
    out: dict[str, list[str]] = {}
    name = ""
    for line in map_diff.library_lines(text):
        if line.startswith("# "):
            name = line[2:]
            out[name] = []
        elif line and name:
            out[name].append(line)
    return out


def local(peer: int, result: str, rev: int = 1, digest: str = "af63bc4c8601b62c", entity: str = "e/1",
          kind: str = "test.toggle", slot: int = 0, extra: str = "") -> str:
    return f"backend local {peer} {kind} {entity} slot {slot} rev {rev} digest {digest} {result}{extra}"


class CompareJoinTest(unittest.TestCase):
    def test_the_golden_compare_lines_join_as_the_library_joined_them(self) -> None:
        cases = sections(golden("compare.txt"))
        self.assertEqual(len(cases), 5)
        for name, lines in cases.items():
            with self.subTest(case=name):
                self.assertEqual(map_diff.join_lines(lines), map_diff.logged_joins(lines))

    def test_the_union_of_the_keys_and_the_order_of_the_results(self) -> None:
        def joined(lines: list[str]) -> list[str]:
            return [x for x in map_diff.join_lines(lines) if x.startswith("backend compare ")]

        e = "test.toggle e/1"
        self.assertEqual(map_diff.join_lines([local(0, "equal")]), [])  # no client's line: no client to join
        self.assertEqual(joined([local(0, "equal"), local(1, "equal", entity="f/1")]),
                         [f"backend compare 1 {e} stuck rev 1/0 differs replica missing",
                          "backend compare 1 test.toggle f/1 stuck rev 0/1 differs replica extra"])
        self.assertEqual(joined([local(0, "equal", rev=2), local(1, "equal")]),
                         [f"backend compare 1 {e} stuck rev 2/1 differs replica behind"])
        self.assertEqual(joined([local(0, "equal"), local(1, "equal", digest="0" * 16)]),
                         [f"backend compare 1 {e} stuck rev 1/1 differs replica differs"])
        # CQ6: S's adapter stuck, the client's read equal.
        self.assertEqual(joined([local(0, "stuck", extra=" differs value 00000000000000aa"), local(1, "equal")]),
                         [f"backend compare 1 {e} stuck rev 1/1 differs server: value 00000000000000aa"])
        # CQ8: one side unsupported, then both.
        self.assertEqual(joined([local(0, "unsupported"), local(1, "equal")]),
                         [f"backend compare 1 {e} stuck rev 1/1 differs server: unsupported"])
        self.assertEqual(joined([local(0, "equal"), local(1, "unsupported")]),
                         [f"backend compare 1 {e} stuck rev 1/1 differs client: unsupported"])
        self.assertEqual(joined([local(0, "unsupported"), local(1, "unsupported")]),
                         [f"backend compare 1 {e} unsupported rev 1/1"])
        # The worse side wins; both sides named when they tie, server first.
        self.assertEqual(joined([local(0, "pending", extra=" pending 20"), local(1, "repaired")]),
                         [f"backend compare 1 {e} pending rev 1/1 differs server: pending"])
        self.assertEqual(joined([local(0, "repaired"), local(1, "repaired")]),
                         [f"backend compare 1 {e} repaired rev 1/1 differs server: repaired, client: repaired"])
        # A stuck pending row (past its grace) is stuck, its words kept.
        self.assertEqual(joined([local(0, "equal"), local(1, "stuck", extra=" differs pending 5000")]),
                         [f"backend compare 1 {e} stuck rev 1/1 differs client: pending 5000"])

    def test_a_later_query_replaces_a_key_and_each_client_is_joined_apart(self) -> None:
        lines = [local(0, "equal"), local(1, "stuck", extra=" differs gone"), local(1, "equal"),
                 local(2, "equal", rev=1, kind="test.gate")]
        out = map_diff.join_lines(lines)
        self.assertEqual(out, [
            "backend compare 1 test.toggle e/1 equal rev 1/1",
            "backend compared 1 test.toggle equal 1 repaired 0 pending 0 stuck 0 unsupported 0",
            "backend compare 2 test.gate e/1 stuck rev 0/1 differs replica extra",
            "backend compared 2 test.gate equal 0 repaired 0 pending 0 stuck 1 unsupported 0",
            "backend compare 2 test.toggle e/1 stuck rev 1/0 differs replica missing",
            "backend compared 2 test.toggle equal 0 repaired 0 pending 0 stuck 1 unsupported 0",
        ])

    def test_the_command_line_join_check(self) -> None:
        cq4 = sections(golden("compare.txt"))["CQ4 CompareStuck"]
        with tempfile.TemporaryDirectory() as tmp:
            s, h = Path(tmp) / "s.log", Path(tmp) / "h.log"
            stamp = "[2026-11-07 12:00:00.000] [info] [core] [t1] "
            s.write_text("".join(f"{stamp}Online: {x}\n" for x in cq4 if x.startswith("backend local 0 ")),
                         encoding="utf-8")
            h.write_text("".join(f"{stamp}Online: {x}\n" for x in cq4 if not x.startswith("backend local 0 ")),
                         encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(map_diff.main(["join", str(s), str(h), "--check"]), 0)
            self.assertIn("join check: 2 line(s) agree", out.getvalue())
            h.write_text(h.read_text(encoding="utf-8").replace(" stuck 1 unsupported", " stuck 0 unsupported"),
                         encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(map_diff.main(["join", str(s), str(h), "--check"]), 1)
            self.assertIn("logged, not joined: backend compared 1 test.toggle", out.getvalue())


if __name__ == "__main__":
    unittest.main()
