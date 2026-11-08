"""Tests for the temporary-payload watcher (stage 8 item 8b): the notification parser, the setup folder
rules, an earlier install's leftover folders, a name in a second setup folder and the allowed names
everywhere; on Windows also the watch itself, against a stand-in setup that writes into a folder the
test makes (never the real TEMP, no sandbox needed), a held file setup cannot delete, and a live watch
stopped by its stop file feeding package_check.py compare-tmp. Run:
python -I -m unittest discover -s tools -p "tmp_payload_watch_test.py"
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import threading
import time
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
        # Any is-*.tmp name: another Inno Setup version's folder name cannot hide it.
        self.assertEqual(tw.setup_name(r"is-AB12.tmp\x.dll"), ("is-AB12.tmp", "x.dll"))
        for other in ("is-AB12C.tmp", r"other\x.dll", r"x-is-AB12C.tmp\x", "x.dll", r"is-x.log\y"):
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
        # The same name in a second setup folder is entered with that folder in front, never dropped.
        second = li.note(temp, 1, r"is-BBBBB.tmp\disccheck.exe")
        self.assertEqual(second.key, r"is-BBBBB.tmp\disccheck.exe")
        self.assertEqual(second.path, temp / "is-BBBBB.tmp" / "disccheck.exe")
        second.first = second.last = (3, "a" * 64)
        c = li.note(temp, 1, r"is-BBBBB.tmp\Setup.tmp")
        c.first, c.last = (1, "b" * 64), (2, "c" * 64)  # changed while held
        out = li.note(temp, 1, r"is-BBBBB.tmp\xexcheck-output.txt")
        out.first, out.last = (1, "d" * 64), (2, "e" * 64)  # allowed: a change is no error
        li.lost = True
        r = li.result(["xexcheck-output.txt", r"_isetup\_setup64.tmp"], "hold", temp, 0)
        self.assertEqual(r["folders"], ["is-AAAAA.tmp", "is-BBBBB.tmp"])
        self.assertEqual(r["seen"]["disccheck.exe"], {"size": 3, "sha256": "a" * 64})
        self.assertEqual(r["seen"][r"is-BBBBB.tmp\disccheck.exe"], {"size": 3, "sha256": "a" * 64})
        self.assertIsNone(r["seen"]["racy.bin"])
        self.assertIsNone(r["seen"][tw.LOST])
        self.assertEqual(r["allowed_extra"], {"xexcheck-output.txt": "e" * 64})  # the allowed names seen
        self.assertEqual(set(r["changed"]), {"Setup.tmp", "xexcheck-output.txt"})
        errors = "\n".join(r["errors"])
        for want in ("racy.bin: seen, not hashed", "Setup.tmp: changed", tw.LOST):
            self.assertIn(want, errors)
        self.assertNotIn("xexcheck-output.txt: changed", errors)
        # The comparison fails on the unhashed file, the second folder's copy and the lost events: a
        # race never passes.
        rows = [{"name": "disccheck.exe", "dest": "{tmp}", "size": 3, "sha256": "a" * 64}]
        diffs = pc.compare_tmp(rows, r["seen"], r["allowed_extra"])
        self.assertTrue(any("racy.bin: seen, not hashed" in d for d in diffs), diffs)
        self.assertIn("is-BBBBB.tmp\\disccheck.exe: in {tmp} but not in the record", diffs)
        self.assertTrue(any(tw.LOST in d for d in diffs), diffs)

    def test_an_earlier_installs_leftovers_do_not_stand_in(self) -> None:
        # The held files of an earlier install in the same sandbox stay in its setup folder; the next
        # install's watch must not take them for its own (a control installer's changed helper would
        # then be dropped as a name already held, and its {tmp} entry counted as seen).
        rec_payload = b"MZ the RC's disc check"
        with tempfile.TemporaryDirectory() as d:
            temp = Path(d)
            (temp / "is-OLD.tmp").mkdir()
            (temp / "is-OLD.tmp" / "swapforcerecomp_xexcheck.exe").write_bytes(rec_payload)
            (temp / "not-setup").mkdir()
            li = tw.Listing(ignored=tw.existing_setup_folders(temp))
            self.assertEqual(li.ignored, {"is-old.tmp"})
            self.assertIsNone(li.note(temp, 1, "is-OLD.tmp"))
            self.assertIsNone(li.note(temp, 1, r"is-OLD.tmp\swapforcerecomp_xexcheck.exe"))
            s = li.note(temp, 1, r"is-NEW.tmp\swapforcerecomp_xexcheck.exe")
            changed = b"MZ one byte changed"
            s.first = s.last = (len(changed), hashlib.sha256(changed).hexdigest())
            r = li.result(tw.allowed_names(), "hold", temp, None)
            self.assertEqual(r["ignored_folders"], ["is-old.tmp"])
            self.assertEqual(r["folders"], ["is-NEW.tmp"])
            rec = [{"name": "swapforcerecomp_xexcheck.exe", "dest": "{tmp}", "size": len(rec_payload),
                    "sha256": hashlib.sha256(rec_payload).hexdigest()}]
            self.assertEqual(pc.compare_tmp(rec, r["seen"], r["allowed_extra"]),
                             ["swapforcerecomp_xexcheck.exe: another hash than the record"])

    def test_allowed_names(self) -> None:
        names = tw.allowed_names(["XEXCHECK-OUTPUT.TXT", "extra.txt"], Path("C:/x/SwapForceOnlineSetup-1.0.exe"))
        self.assertEqual(names, list(tw.ALLOWED_EXTRA) + ["extra.txt", "SwapForceOnlineSetup-1.0.tmp"])
        self.assertIn("_isetup\\_setup64.tmp", tw.allowed_names())
        # The loader's <installer>.tmp is allowed by its exact name, no other .tmp.
        li = tw.Listing()
        for n in ("SwapForceOnlineSetup-1.0.tmp", "Other.tmp"):
            li.note(Path("C:/t"), 1, "is-L.tmp\\" + n).last = (4, "f" * 64)
        r = li.result(tw.allowed_names(setup_exe="SwapForceOnlineSetup-1.0.exe"), "hold", Path("C:/t"), None)
        self.assertEqual(list(r["allowed_extra"]), ["SwapForceOnlineSetup-1.0.tmp"])
        self.assertEqual(pc.compare_tmp([], r["seen"], r["allowed_extra"]), ["Other.tmp: in {tmp} but not in the record"])


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
        # The next watch in the same folder ignores this install's leftovers.
        self.assertEqual(tw.main(["--temp", str(self.temp), "--out", str(self.out), "--seconds", "0.1"]), 0)
        listing = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual((listing["seen"], listing["ignored_folders"]), ({}, ["is-t3st1.tmp"]))

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
        self.assertEqual(tw.main(["--temp", str(self.temp), "--out", str(self.out), "--copy-to", str(self.tools),
                                  "--copy-dir", str(self.temp), "--seconds", "0"]), 2)

    def test_a_held_file_cannot_be_deleted(self) -> None:
        p = self.temp / "helper.exe"
        p.write_bytes(b"MZ helper")
        h = tw.win_open(p)
        self.assertIsNotNone(h)
        try:
            with self.assertRaises(PermissionError):
                os.remove(p)
            self.assertEqual(tw.win_read(h), b"MZ helper")
        finally:
            tw.win_close(h)
        os.remove(p)

    def test_live_watch_stops_at_its_stop_file_and_feeds_compare_tmp(self) -> None:
        stop = self.temp.parent / "setup-ended"
        result = {}

        def run() -> None:
            result["listing"] = tw.watch(self.temp, self.out, tw.allowed_names(), seconds=30.0, until=stop)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.3)
        setup = self.temp / "is-TEST.tmp"
        (setup / "_isetup").mkdir(parents=True)
        payload = b"MZ the disc check"
        (setup / "swapforcerecomp_xexcheck.exe").write_bytes(payload)
        (setup / "_isetup" / "_setup64.tmp").write_bytes(b"inno")
        (setup / "stray.dll").write_bytes(b"not in the record")
        time.sleep(0.5)
        # Setup's cleanup: the held files stay.
        for f in setup.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                except PermissionError:
                    pass
        stop.write_text("done")
        t.join(10)
        self.assertFalse(t.is_alive())
        listing = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(listing, result["listing"])
        self.assertEqual(listing["seen"]["swapforcerecomp_xexcheck.exe"]["sha256"], hashlib.sha256(payload).hexdigest())
        rec = [{"name": "swapforcerecomp_xexcheck.exe", "dest": "{tmp}", "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest()}]
        errors = pc.compare_tmp(rec, listing["seen"], listing["allowed_extra"])
        self.assertEqual(errors, ["stray.dll: in {tmp} but not in the record"])


if __name__ == "__main__":
    unittest.main()
