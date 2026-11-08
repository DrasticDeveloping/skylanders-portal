"""Tests for the package check (stage 8 item 8, `package-contents` and its negative controls) on a
throwaway game checkout. Run: python -I tools/package_check_test.py"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import package_check as pc  # noqa: E402


def pe(dll: bool, tag: bytes = b"") -> bytes:
    """A minimal PE header: MZ, e_lfanew at 0x3C, PE\\0\\0, machine x64, the DLL flag."""
    data = bytearray(0x100)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, 0x8664)
    struct.pack_into("<H", data, 0x80 + 22, 0x2022 if dll else 0x0022)
    return bytes(data) + tag


MANIFEST = r"""
[[file]]
iss_source = 'staging\game.exe'
dest = '{app}'
provenance = 'build:game'
type = 'pe-x64-exe'
role = 'game'
from = 'out/game.exe'

[[file]]
iss_source = 'staging\engine.dll'
dest = '{app}'
provenance = 'build:game'
type = 'pe-x64-dll'
from = 'out/engine.dll'

[[file]]
iss_source = 'staging\game_xexcheck.exe'
dest = '{tmp}'
provenance = 'build:game_xexcheck'
type = 'pe-x64-exe'
from = 'out/game_xexcheck.exe'

[[file]]
iss_source = 'settings_template.toml'
dest = '{tmp}'
provenance = 'tracked'
type = 'toml'

[[file]]
iss_source = 'redist\vcruntime140_1.dll'
dest = '{app}'
provenance = 'redist'
type = 'pe-x64-dll'
extracted_to_tmp = true

[[file]]
iss_source = 'routes\unsupported.toml'
dest = '{app}\routes'
provenance = 'tracked'
type = 'toml'
"""

ISS = r"""[Files]
Source: "staging\game.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "staging\engine.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "staging\game_xexcheck.exe"; DestDir: "{tmp}"; Flags: dontcopy
Source: "settings_template.toml"; DestDir: "{tmp}"; Flags: dontcopy
Source: "redist\vcruntime140_1.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "routes\unsupported.toml"; DestDir: "{app}\routes"; Flags: ignoreversion
"""


class PackageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        r = self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        subprocess.run(["git", "-C", str(r), "config", "user.email", "t@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
        inst = r / "installer"
        (inst / "redist").mkdir(parents=True)
        (inst / "routes").mkdir()
        (inst / "release-files.toml").write_text(MANIFEST, encoding="utf-8")
        (inst / "Game.iss").write_text(ISS, encoding="utf-8")
        (inst / "settings_template.toml").write_text("[General]\nskip_intro = false\n", encoding="utf-8")
        (inst / "routes" / "unsupported.toml").write_text("unsupported = []\n", encoding="utf-8")
        (inst / "redist" / "vcruntime140_1.dll").write_bytes(pe(True, b"vc"))
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "x"], check=True)
        (r / "out").mkdir()
        (r / "out" / "game.exe").write_bytes(pe(False, b"game"))
        (r / "out" / "engine.dll").write_bytes(pe(True, b"engine"))
        (r / "out" / "game_xexcheck.exe").write_bytes(pe(False, b"check"))
        (r / "out" / "other.dll").write_bytes(pe(True, b"other"))
        self.inst = inst
        self.staging = inst / "staging"
        pc.stage(r, inst / "release-files.toml", self.staging)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_check(self) -> tuple[list[dict], list[str]]:
        return pc.check(self.repo, self.inst / "release-files.toml", self.inst / "Game.iss", self.staging)

    def test_the_staged_rc_passes_and_the_record_is_complete(self) -> None:
        rows, errors = self.run_check()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(os.listdir(self.staging)), ["engine.dll", "game.exe", "game_xexcheck.exe"])
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["game.exe"]["role"], "game")
        self.assertEqual(by["routes\\unsupported.toml"]["dest"], "{app}")
        self.assertTrue(by["vcruntime140_1.dll"]["extracted_to_tmp"])
        rec = self.repo / "record.json"
        digest = pc.write_record(rows, rec)
        self.assertEqual(digest, hashlib.sha256(rec.read_bytes()).hexdigest())

    def test_a_staging_folder_elsewhere_is_the_one_checked(self) -> None:
        other = self.repo / "elsewhere"
        pc.stage(self.repo, self.inst / "release-files.toml", other)
        (other / "engine.dll").write_text("text", encoding="utf-8")
        _, errors = pc.check(self.repo, self.inst / "release-files.toml", self.inst / "Game.iss", other)
        self.assertTrue(any("engine.dll: not a pe-x64-dll" in e for e in errors))

    # The five negative controls: each must fail.

    def test_a_planted_xex(self) -> None:
        (self.staging / "default.xex").write_bytes(b"XEX2")
        self.assertTrue(any("game or disc file" in e for e in self.run_check()[1]))

    def test_an_unexpected_text_file_named_like_a_dll(self) -> None:
        (self.staging / "extra.dll").write_text("not a dll", encoding="utf-8")
        self.assertTrue(any("extra.dll" in e for e in self.run_check()[1]))
        # And a manifest entry whose staged file is text where a DLL belongs.
        (self.staging / "engine.dll").write_text("text", encoding="utf-8")
        self.assertTrue(any("not a pe-x64-dll" in e for e in self.run_check()[1]))

    def test_a_valid_dll_not_in_the_manifest(self) -> None:
        (self.staging / "other.dll").write_bytes(pe(True, b"other"))
        self.assertTrue(any("other.dll: not in the release-file manifest" in e for e in self.run_check()[1]))

    def test_a_tracked_file_with_another_hash(self) -> None:
        (self.inst / "settings_template.toml").write_text("[General]\nskip_intro = true\n", encoding="utf-8")
        self.assertTrue(any("differs from the committed file" in e for e in self.run_check()[1]))

    def test_an_iss_source_outside_the_manifest(self) -> None:
        (self.inst / "Game.iss").write_text(ISS + 'Source: "staging\\other.dll"; DestDir: "{app}"\n', encoding="utf-8")
        self.assertTrue(any("not in the release-file manifest" in e for e in self.run_check()[1]))
        (self.inst / "Game.iss").write_text(ISS + 'Source: "staging\\*.dll"; DestDir: "{app}"\n', encoding="utf-8")
        with self.assertRaises(pc.CheckError):
            self.run_check()
        # Unquoted, the same wildcard is still read (Inno Setup takes unquoted values).
        (self.inst / "Game.iss").write_text(ISS + 'Source: staging\\*.dll; DestDir: {app}\n', encoding="utf-8")
        with self.assertRaises(pc.CheckError):
            self.run_check()
        # A [Files] line in another parameter order is not skipped either.
        (self.inst / "Game.iss").write_text(ISS + 'DestDir: "{app}"; Source: "staging\\x.dll"\n', encoding="utf-8")
        with self.assertRaises(pc.CheckError):
            self.run_check()
        (self.inst / "Game.iss").write_text(ISS.replace('Source: "staging\\engine.dll"; DestDir: "{app}"; Flags: ignoreversion\n', ""),
                                            encoding="utf-8")
        self.assertTrue(any("has no Source line" in e for e in self.run_check()[1]))

    def test_manifest_shapes(self) -> None:
        bad = [
            MANIFEST.replace("dest = '{tmp}'", "dest = 'C:\\x'", 1),
            MANIFEST.replace("provenance = 'tracked'", "provenance = 'download'", 1),
            MANIFEST.replace("role = 'game'", "role = 'shell'"),
            MANIFEST + "\n[[file]]\niss_source = 'staging\\*.dll'\ndest = '{app}'\nprovenance = 'build:x'\ntype = 'pe-x64-dll'\nfrom = 'x'\n",
            MANIFEST.replace("type = 'pe-x64-exe'\nrole = 'game'", "type = 'pe-x64-dll'\nrole = 'game'"),
        ]
        for text in bad:
            with self.subTest(text=text[:60]):
                (self.inst / "release-files.toml").write_text(text, encoding="utf-8")
                with self.assertRaises(pc.CheckError):
                    pc.load_manifest(self.inst / "release-files.toml")

    # installer-clean's comparisons (the sandbox runs them on the compiled installer's real payload).

    def record(self) -> list[dict]:
        rows, errors = self.run_check()
        self.assertEqual(errors, [])
        return rows

    def install_from(self, rows: list[dict]) -> Path:
        app = self.repo / "app"
        for r in rows:
            if r["dest"] != "{app}":
                continue
            src = self.inst / r["iss_source"].replace("\\", os.sep)
            dst = app / r["name"].replace("\\", os.sep)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        return app

    def test_compare_installed(self) -> None:
        rows = self.record()
        app = self.install_from(rows)
        (app / "rom").mkdir()
        (app / "rom" / "default.xex").write_bytes(b"XEX2")
        (app / "unins000.exe").write_bytes(b"MZ")
        (app / "swapforcerecomp.toml").write_text("", encoding="utf-8")
        self.assertEqual(pc.compare_installed(rows, app, ("swapforcerecomp.toml",)), [])
        # Mismatch control (a): an {app} DLL with one byte changed after the record was written.
        dll = app / "engine.dll"
        b = bytearray(dll.read_bytes())
        b[-1] ^= 1
        dll.write_bytes(bytes(b))
        (app / "planted.dll").write_bytes(pe(True))
        errors = pc.compare_installed(rows, app, ("swapforcerecomp.toml",))
        self.assertTrue(any("engine.dll: another hash" in e for e in errors))
        self.assertTrue(any("planted.dll" in e for e in errors))
        (app / "routes" / "unsupported.toml").unlink()
        self.assertTrue(any("missing" in e for e in pc.compare_installed(rows, app)))

    def test_compare_tmp(self) -> None:
        rows = self.record()
        seen = {}
        for r in rows:
            if r["dest"] == "{tmp}" or r.get("extracted_to_tmp"):
                seen[r["name"].rsplit("\\", 1)[-1]] = {"size": r["size"], "sha256": r["sha256"]}
        seen["xexcheck-output.txt"] = {"size": 3, "sha256": "0" * 64}
        extra = {"xexcheck-output.txt": "the [Code]'s disc check output", "_isetup\\_setup64.tmp": "Inno Setup"}
        self.assertEqual(pc.compare_tmp(rows, seen, extra), [])
        # Mismatch control (b): the disc check helper with one byte changed.
        bad = dict(seen)
        bad["game_xexcheck.exe"] = {"size": seen["game_xexcheck.exe"]["size"], "sha256": "1" * 64}
        self.assertTrue(any("another hash" in e for e in pc.compare_tmp(rows, bad, extra)))
        # Seen but gone before it was hashed: a race can only fail.
        racy = dict(seen)
        racy["game_xexcheck.exe"] = None
        self.assertTrue(any("seen, not hashed" in e for e in pc.compare_tmp(rows, racy, extra)))
        # A file outside the record, and a {tmp} entry never seen.
        more = dict(seen)
        more["evil.dll"] = {"size": 1, "sha256": "2" * 64}
        self.assertTrue(any("evil.dll" in e for e in pc.compare_tmp(rows, more, extra)))
        less = {k: v for k, v in seen.items() if k != "settings_template.toml"}
        self.assertTrue(any("never put there" in e for e in pc.compare_tmp(rows, less, extra)))

    def test_cli(self) -> None:
        rec = self.repo / "rec.json"
        self.assertEqual(pc.main(["check", "--repo", str(self.repo), "--record", str(rec)]), 0)
        self.assertEqual(json.loads(rec.read_text(encoding="utf-8"))["format"], 1)
        (self.staging / "default.xex").write_bytes(b"XEX2")
        self.assertEqual(pc.main(["check", "--repo", str(self.repo), "--record", str(rec)]), 1)


if __name__ == "__main__":
    unittest.main()
