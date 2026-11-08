"""Tests for the temporary-payload watcher (stage 8 item 8b): the notification parser and the setup
folder rules everywhere; on Windows also the watch itself, against a stand-in setup that writes into a
folder the test makes (never the real TEMP, no sandbox needed). Run:
python -I -m unittest discover -s tools -p "tmp_payload_watch_test.py"
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import package_check as pc  # noqa: E402
import tmp_payload_watch as tw  # noqa: E402


def record(action: int, name: str, last: bool = False) -> bytes:
    data = name.encode("utf-16-le")
    size = 12 + len(data)
    size += (-size) % 4
    head = struct.pack("<III", 0 if last else size, action, len(data))
    return (head + data).ljust(size, b"\0")


class ParserTest(unittest.TestCase):
    def test_records(self) -> None:
        buf = record(1, r"is-AB12C.tmp\helper.exe") + record(2, "elsewhere.txt", last=True)
        self.assertEqual(tw.parse_notify(buf), [(1, r"is-AB12C.tmp\helper.exe"), (2, "elsewhere.txt")])

    def test_a_buffer_that_runs_out_is_refused(self) -> None:
        good = record(1, "a.txt") + record(3, "b.txt", last=True)
        for bad in (good[:10], good[:20], struct.pack("<III", 0, 1, 400) + b"a\0",
                    struct.pack("<III", 6, 1, 2) + b"a\0" + record(1, "b", last=True),  # next inside the name
                    struct.pack("<III", 4096, 1, 2) + b"a\0\0\0", struct.pack("<III", 0, 1, 3) + b"abc"):
            with self.assertRaises(tw.WatchError, msg=bad):
                tw.parse_notify(bad)

    def test_setup_folders(self) -> None:
        self.assertEqual(tw.setup_name(r"is-AB12C.tmp\_isetup\_setup64.tmp"), ("is-AB12C.tmp", r"_isetup\_setup64.tmp"))
        self.assertEqual(tw.setup_name("IS-ab12c.TMP/x.dll"), ("IS-ab12c.TMP", "x.dll"))
        for other in ("is-AB12C.tmp", r"is-AB12.tmp\x.dll", r"other\x.dll", r"x-is-AB12C.tmp\x", "x.dll"):
            self.assertIsNone(tw.setup_name(other), other)

    def test_the_listing(self) -> None:
        temp = Path("C:/t")
        li = tw.Listing()
        self.assertIsNone(li.note(temp, 1, "is-AAAAA.tmp"))
        a = li.note(temp, 1, r"is-AAAAA.tmp\disccheck.exe")
        a.first = a.last = (3, "a" * 64)
        b = li.note(temp, 1, r"is-AAAAA.tmp\racy.bin")
        li.note(temp, 2, r"is-AAAAA.tmp\racy.bin")
        self.assertTrue(b.gone)
        # The same name in a second setup folder is an error, not a second entry.
        self.assertIsNone(li.note(temp, 1, r"is-BBBBB.tmp\disccheck.exe"))
        c = li.note(temp, 1, r"is-BBBBB.tmp\Setup.tmp")
        c.first, c.last = (1, "b" * 64), (2, "c" * 64)  # changed while held
        out = li.note(temp, 1, r"is-BBBBB.tmp\xexcheck-output.txt")
        out.first, out.last = (1, "d" * 64), (2, "e" * 64)  # allowed: a change is no error
        li.lost = True
        r = li.result(["xexcheck-output.txt", r"_isetup\_setup64.tmp"], "hold", temp, 0)
        self.assertEqual(r["folders"], ["is-AAAAA.tmp", "is-BBBBB.tmp"])
        self.assertEqual(r["seen"]["disccheck.exe"], {"size": 3, "sha256": "a" * 64})
        self.assertIsNone(r["seen"]["racy.bin"])
        self.assertIsNone(r["seen"][tw.LOST])
        self.assertEqual(r["allowed_extra"], {"xexcheck-output.txt": "e" * 64, r"_isetup\_setup64.tmp": None})
        self.assertEqual(set(r["changed"]), {"Setup.tmp", "xexcheck-output.txt"})
        errors = "\n".join(r["errors"])
        for want in ("racy.bin: seen, not hashed", "in two setup folders", "Setup.tmp: changed", tw.LOST):
            self.assertIn(want, errors)
        self.assertNotIn("xexcheck-output.txt: changed", errors)
        # The comparison fails on the unhashed file and the lost events: a race never passes.
        rows = [{"name": "disccheck.exe", "dest": "{tmp}", "size": 3, "sha256": "a" * 64}]
        diffs = pc.compare_tmp(rows, r["seen"], r["allowed_extra"])
        self.assertTrue(any("racy.bin: seen, not hashed" in d for d in diffs), diffs)
        self.assertTrue(any(tw.LOST in d for d in diffs), diffs)


# A stand-in for setup: writes its helpers the way Inno Setup does (one handle that shares nothing,
# in two parts), then tries to delete them; a racy file is gone the moment it is closed.
FAKE_SETUP = r'''
import ctypes, os, sys, time
from ctypes import wintypes
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                            wintypes.DWORD, wintypes.HANDLE]
k32.CreateFileW.restype = wintypes.HANDLE
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
temp, report = sys.argv[1], sys.argv[2]
def write(path, parts, flags=0x80):
    h = k32.CreateFileW(path, 0x40000000, 0, None, 2, flags, None)  # GENERIC_WRITE, share none, CREATE_ALWAYS
    assert h not in (None, 0, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    n = wintypes.DWORD(0)
    for p in parts:
        k32.WriteFile(h, p, len(p), ctypes.byref(n), None)
        time.sleep(0.15)
    k32.CloseHandle(h)
folder = os.path.join(temp, "is-T3ST1.tmp")
os.mkdir(folder)
os.mkdir(os.path.join(folder, "_isetup"))
write(os.path.join(folder, "_isetup", "_setup64.tmp"), [b"inno helper"])
write(os.path.join(folder, "disccheck.exe"), [b"first half ", b"second half"])
write(os.path.join(folder, "racy.bin"), [b"gone at close"], 0x80 | 0x04000000)  # FILE_FLAG_DELETE_ON_CLOSE
with open(os.path.join(temp, "elsewhere.txt"), "w") as f:
    f.write("not in a setup folder")
time.sleep(0.4)
results = []
for name in ("disccheck.exe", os.path.join("_isetup", "_setup64.tmp")):
    try:
        os.remove(os.path.join(folder, name))
        results.append(name + ": deleted")
    except PermissionError:
        results.append(name + ": delete refused")
with open(report, "w") as f:
    f.write("\n".join(results))
'''


@unittest.skipUnless(os.name == "nt", "the watch runs on Windows")
class WatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.temp = root / "temp"
        self.temp.mkdir()
        self.tools = root / "tools"
        self.tools.mkdir()
        self.fake = self.tools / "fake_setup.py"
        self.fake.write_text(FAKE_SETUP, encoding="utf-8")
        self.report = root / "report.txt"
        self.out = root / "listing.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_watch(self, mode: str, copy_dir: Path | None = None) -> dict:
        cmd = [sys.executable, "-I", str(self.fake), str(self.temp), str(self.report)]
        r = tw.watch(self.temp, self.out, [r"_isetup\_setup64.tmp"], mode, copy_dir, cmd, settle_s=0.3)
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8")), r)
        return r

    def check_listing(self, r: dict) -> None:
        self.assertEqual(r["installer_exit"], 0)
        self.assertEqual(r["folders"], ["is-T3ST1.tmp"])
        full = b"first half second half"
        self.assertEqual(r["seen"]["disccheck.exe"], {"size": len(full), "sha256": hashlib.sha256(full).hexdigest()})
        self.assertIsNone(r["seen"]["racy.bin"])
        self.assertEqual(r["allowed_extra"][r"_isetup\_setup64.tmp"], hashlib.sha256(b"inno helper").hexdigest())
        self.assertNotIn("_isetup", r["seen"])  # the folder is no file of the payload
        self.assertNotIn("elsewhere.txt", r["seen"])
        self.assertEqual(r["errors"], ["racy.bin: seen, not hashed"])
        rows = [{"name": "disccheck.exe", "dest": "{tmp}", "size": len(full), "sha256": hashlib.sha256(full).hexdigest()}]
        self.assertEqual(pc.compare_tmp(rows, r["seen"], r["allowed_extra"]), ["racy.bin: seen, not hashed"])
        # A one-byte change in the helper fails the comparison.
        other = [dict(rows[0], sha256=hashlib.sha256(b"first half second hal!").hexdigest())]
        self.assertTrue(any("another hash" in d for d in pc.compare_tmp(other, r["seen"], r["allowed_extra"])))

    def test_hold_keeps_setup_from_deleting_and_hashes_the_whole_file(self) -> None:
        r = self.run_watch("hold")
        self.check_listing(r)
        self.assertEqual(self.report.read_text().splitlines(),
                         ["disccheck.exe: delete refused", r"_isetup\_setup64.tmp: delete refused"])
        self.assertEqual(tw.main(["--temp", str(self.temp), "--out", str(self.out), "--seconds", "0.1"]), 0)

    def test_copy_releases_each_file_and_hashes_the_copy(self) -> None:
        copies = self.temp.parent / "copies"
        r = self.run_watch("copy", copies)
        self.check_listing(r)
        self.assertEqual(self.report.read_text().splitlines(),
                         ["disccheck.exe: deleted", r"_isetup\_setup64.tmp: deleted"])
        self.assertEqual((copies / "is-T3ST1.tmp" / "disccheck.exe").read_bytes(), b"first half second half")

    def test_the_cli_refuses_a_missing_folder_and_a_copy_without_its_folder(self) -> None:
        self.assertEqual(tw.main(["--temp", str(self.temp / "missing"), "--out", str(self.out), "--seconds", "0"]), 2)
        self.assertEqual(tw.main(["--temp", str(self.temp), "--out", str(self.out), "--mode", "copy",
                                  "--seconds", "0"]), 2)


if __name__ == "__main__":
    unittest.main()
