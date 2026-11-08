"""rc_identity.py and sandbox_config.py, the two RC tools of Swap Force Online stage 8 (items 9 and 8b):
an RC is one commit (a changed tracked file is refused, the files a build rewrites are not), the
record's hashes are the files', and the sandbox maps only its inputs read-only, never an owner data
folder or a repository, with networking off. Run:
python -I -m unittest discover -s tools -p "rc_identity_test.py"
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rc_identity  # noqa: E402
import sandbox_config  # noqa: E402


def run(*args: str, cwd: Path) -> None:
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True)


class RcIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "game"
        self.repo.mkdir()
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.name", "t", cwd=self.repo)
        run("git", "config", "user.email", "t@example.invalid", cwd=self.repo)
        (self.repo / "config").mkdir()
        (self.repo / "config" / "default.toml").write_text("[x]\n", encoding="utf-8")
        (self.repo / "generated").mkdir()
        (self.repo / "generated" / "rexglue.cmake").write_text("a\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.repo)
        run("git", "commit", "-q", "-m", "c", cwd=self.repo)
        self.installer = base / "SwapForceOnlineSetup-1.0.0-online.1.exe"
        self.installer.write_bytes(b"MZ setup")
        self.record = base / "payload-record.json"
        self.record.write_text(json.dumps({"format": 1, "files": [{"name": "a"}, {"name": "b"}]}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self) -> dict:
        return rc_identity.make_identity(self.repo, self.installer, self.record, "win-amd64-release", "Release",
                                         None, None)

    def test_the_record(self) -> None:
        ident = self.make()
        self.assertEqual(ident["installer"]["sha256"], hashlib.sha256(b"MZ setup").hexdigest())
        self.assertEqual(ident["payload_record"]["sha256"], hashlib.sha256(self.record.read_bytes()).hexdigest())
        self.assertEqual(ident["payload_record"]["files"], 2)
        self.assertEqual(len(ident["commit"]), 40)
        self.assertEqual(ident["codegen"]["config_default_toml_sha256"], hashlib.sha256(b"[x]\n").hexdigest())

    def test_one_commit(self) -> None:
        (self.repo / "generated" / "rexglue.cmake").write_text("rewritten by a configure\n", encoding="utf-8")
        self.make()  # a build's own rewrite is not a change of the RC
        (self.repo / "config" / "default.toml").write_text("[y]\n", encoding="utf-8")
        with self.assertRaises(rc_identity.IdentityError):
            self.make()

    def test_not_a_record(self) -> None:
        self.record.write_text("{}", encoding="utf-8")
        with self.assertRaises(rc_identity.IdentityError):
            self.make()


class SandboxConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.inputs = {}
        for n in sandbox_config.INPUTS:
            (self.base / n).mkdir()
            self.inputs[n] = self.base / n
        self.output = self.base / "out"
        self.output.mkdir()
        self.owner = self.base / "Documents" / "Skylanders"
        self.owner.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, **over) -> str:
        inputs = dict(self.inputs, **{k: v for k, v in over.items() if k != "output"})
        return sandbox_config.build_wsb(inputs, over.get("output", self.output), owners=[self.owner])

    def test_inputs_read_only_output_writable_no_network(self) -> None:
        text = self.build()
        self.assertIn("<Networking>Disable</Networking>", text)
        self.assertEqual(text.count("<ReadOnly>true</ReadOnly>"), len(sandbox_config.INPUTS))
        self.assertEqual(text.count("<ReadOnly>false</ReadOnly>"), 1)
        self.assertIn(f"<HostFolder>{self.output.resolve()}</HostFolder>\n"
                      "      <SandboxFolder>C:\\Users\\WDAGUtilityAccount\\Desktop\\output</SandboxFolder>\n"
                      "      <ReadOnly>false</ReadOnly>", text)

    def test_refusals(self) -> None:
        with self.assertRaises(sandbox_config.ConfigError):
            self.build(figures=self.owner)                    # an owner data folder
        with self.assertRaises(sandbox_config.ConfigError):
            self.build(figures=self.owner.parent)             # a folder holding one
        repo = self.base / "repo"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(sandbox_config.ConfigError):
            self.build(tools=repo)                            # a repository folder
        with self.assertRaises(sandbox_config.ConfigError):
            self.build(disc=self.inputs["tools"])             # the same folder twice
        with self.assertRaises(sandbox_config.ConfigError):
            self.build(disc=self.base / "missing")
        (self.output / "old.log").write_text("x", encoding="utf-8")
        with self.assertRaises(sandbox_config.ConfigError):
            self.build()                                      # not a fresh output folder


if __name__ == "__main__":
    unittest.main()
