"""Tests for the validated argument builder and the input script check (stage 8 item 6,
`remote-agent-refusals`' builder and script cases). Every path is a synthetic string or a folder the
test makes in its own temporary store; nothing is read from or written to a real owner folder.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_launch_args.py"
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import launch_args as la  # noqa: E402

ALLOW = """
[settings.user_data_root]
kind = "runner"
[settings.cache_root]
kind = "runner"
[settings.config_file]
kind = "runner"
[settings.log_file]
kind = "runner"
[settings.portal_figures_dir]
kind = "runner"
[settings.game_data_root]
kind = "runner"
[settings.input_script]
kind = "runner"
[settings.audio_mute]
kind = "bool"
[settings.window_hidden]
kind = "bool"
[settings.window_width]
kind = "int"
min = 64
max = 7680
[settings.online_route_file]
kind = "read_path"
[settings.perf_log_csv]
kind = "write_path"
[settings.portal_figure]
kind = "figure_path"
[settings.online_control_pipe]
kind = "refused"
reason = "Host sets it for its own server"
[settings.role]
kind = "enum"
values = ["server"]
[settings.user_name]
kind = "string"
"""

COMMANDS = """
[commands]
level = "none"
online_host = "none"
dev_secret_dump = "refused"
save_import = "read_path"
save_dump = "read_write_paths"
"""


def junction(link: Path, target: Path) -> None:
    """A directory junction (no admin rights needed)."""
    if os.name != "nt":
        os.symlink(target, link, target_is_directory=True)
        return
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        raise unittest.SkipTest(f"cannot make a junction: {r.stdout} {r.stderr}")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.root = root
        self.store = root / "store"
        self.store.mkdir()
        # A stand-in owner folder and a stand-in install with its disc, all inside the temp folder.
        self.owner = root / "Owner" / "Documents" / "Skylanders"
        (self.owner / "Swap Force").mkdir(parents=True)
        self.install = root / "Install"
        (self.install / "rom").mkdir(parents=True)
        (self.install / "routes").mkdir()
        (self.install / "routes" / "unsupported.toml").write_text("x = 1\n", encoding="utf-8")
        (self.install / "settings_template.toml").write_text("[General]\n", encoding="utf-8")
        self.run = la.make_run_folder(self.store, "run1", owner_roots=[self.owner])
        self.ctx = la.RunContext(
            kind="rc", store=self.store, run_folder=self.run, rom=self.install / "rom",
            template=self.install / "settings_template.toml", allow=la.AllowList.parse(ALLOW),
            commands=la.CommandTable.parse(COMMANDS), install=self.install,
            read_roots=[self.install / "routes"], owner_roots=[self.owner])
        fx = root / "fixtures"
        (fx / "saves").mkdir(parents=True)
        (fx / "figures").mkdir()
        (fx / "figures" / "hero.sky").write_bytes(b"\0" * 64)
        self.fixtures = (fx / "saves", fx / "figures")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, spec_args: list[str] | None = None, script: str = "wait 1\nquit\n") -> list[str]:
        if (self.run / "data").exists():  # a run folder takes one build: the next gets a fresh one
            self.n = getattr(self, "n", 1) + 1
            self.run = self.ctx.run_folder = la.make_run_folder(self.store, f"run{self.n}", owner_roots=[self.owner])
        return la.build_game(self.ctx, spec_args or [], script, fixtures=self.fixtures,
                             runner_args=["--portal_mode=software", "--audio_mute=true"])

    def refuses(self, fn, *a, **kw) -> str:
        with self.assertRaises(la.LaunchError) as cm:
            fn(*a, **kw)
        return str(cm.exception)

    def args(self) -> list[str]:
        return self.build()


class BuilderTest(_Base):
    # --- the positive control -----------------------------------------------------------------------

    def test_a_good_run_is_built(self) -> None:
        script = ("wait 2\ntap a+b\nsave 82000000 100 snap one.bin\nshot hub\n"
                  f"place 0 {self.run / 'figures' / 'hero.sky'}\nlevel hub\nwaitfor title_ready 30\n")
        args = self.build(["--window_width=640", f"--online_route_file={self.install / 'routes' / 'unsupported.toml'}",
                           f"--perf_log_csv={self.run / 'perf.csv'}", f"--portal_figure={self.run / 'figures' / 'hero.sky'}"],
                          script)
        self.assertIn(f"--game_data_root={self.install / 'rom'}", args)
        self.assertEqual((self.run / "script.txt").read_bytes(), script.encode())
        self.assertTrue((self.run / "settings.toml").is_file())

    # --- the command line ---------------------------------------------------------------------------

    def test_empty_value(self) -> None:
        self.assertIn("empty", self.refuses(self.build, ["--user_name="]))

    def test_two_user_data_roots(self) -> None:
        args = self.args()
        self.assertIn("2 times", self.refuses(la.check_args, self.ctx, args + [f"--user_data_root={self.run / 'data'}"]))
        self.assertIn("runner-owned", self.refuses(self.build, [f"--user_data_root={self.run}"]))

    def _swap(self, args: list[str], name: str, value: str) -> list[str]:
        return [f"--{name}={value}" if a.startswith(f"--{name}=") else a for a in args]

    def test_root_outside_store(self) -> None:
        args = self._swap(self.args(), "user_data_root", str(self.root / "elsewhere"))
        self.assertIn("outside", self.refuses(la.check_args, self.ctx, args))

    def test_root_is_a_junction_into_the_owner_folder(self) -> None:
        args = self.args()
        junction(self.run / "alias", self.owner)
        args = self._swap(args, "user_data_root", str(self.run / "alias" / "Swap Force"))
        self.assertIn("reparse point", self.refuses(la.check_args, self.ctx, args))

    def test_root_under_a_onedrive_documents(self) -> None:
        od = self.root / "OneDriveX"
        roots = la.owner_data_roots({"USERPROFILE": str(self.root / "prof"), "OneDrive": str(od)}, registry=False)
        self.assertIn(od / "Documents" / "Skylanders", roots)
        # A OneDrive Documents that sits inside the store (the worst case) is still refused.
        od_in = self.store / "OneDrive"
        (od_in / "Documents" / "Skylanders").mkdir(parents=True)
        self.ctx.owner_roots = la.owner_data_roots({"USERPROFILE": str(self.root / "prof"), "OneDrive": str(od_in)},
                                                   registry=False)
        self.assertIn("owner's data", self.refuses(la.contained, self.ctx, str(od_in / "Documents" / "Skylanders" / "x"),
                                                    "root", self.store))

    def test_root_inside_the_install_rom(self) -> None:
        args = self._swap(self.args(), "cache_root", str(self.install / "rom" / "cache"))
        self.refuses(la.check_args, self.ctx, args)
        self.ctx.store = self.root  # even with a store wide enough to hold the install
        self.ctx.run_folder = self.root
        self.assertIn("install", self.refuses(la.contained, self.ctx, str(self.install / "rom" / "c"), "root", self.root))

    def test_config_file_outside_the_run_folder_or_through_a_junction(self) -> None:
        args = self.args()
        other = self.store / "other.toml"
        other.write_text("", encoding="utf-8")
        self.refuses(la.check_args, self.ctx, self._swap(args, "config_file", str(other)))
        (self.root / "cfgdir").mkdir()
        (self.root / "cfgdir" / "settings.toml").write_text("", encoding="utf-8")
        junction(self.run / "cfg", self.root / "cfgdir")
        self.assertIn("reparse", self.refuses(la.check_args, self.ctx,
                                              self._swap(args, "config_file", str(self.run / "cfg" / "settings.toml"))))
        (self.run / "mine.toml").write_text("", encoding="utf-8")
        self.assertIn("only the settings file", self.refuses(la.check_args, self.ctx,
                                                             self._swap(args, "config_file", str(self.run / "mine.toml"))))

    def test_game_data_root_other_than_the_install_rom(self) -> None:
        other = self.root / "otherdisc"
        other.mkdir()
        self.assertIn("game_data_root", self.refuses(la.check_args, self.ctx,
                                                     self._swap(self.args(), "game_data_root", str(other))))

    def test_hardware_portal(self) -> None:
        args = self._swap(self.args(), "portal_mode", "usb")
        self.assertIn("software", self.refuses(la.check_args, self.ctx, args))
        self.assertIn("software", self.refuses(la.check_args, self.ctx, self.args() + ["--portal_mode=software"]))

    def test_no_input_script(self) -> None:
        args = [a for a in self.args() if not a.startswith("--input_script=")]
        self.assertIn("input_script", self.refuses(la.check_args, self.ctx, args))

    def test_setting_off_the_allow_list(self) -> None:
        self.assertIn("allow list", self.refuses(self.build, ["--online_sentinel_magic=true"]))

    def test_path_setting_outside_the_run_folder(self) -> None:
        self.refuses(self.build, [f"--perf_log_csv={self.root / 'perf.csv'}"])

    def test_portal_figure_outside_the_figures_copy(self) -> None:
        (self.run / "loose.sky").write_bytes(b"\0")
        self.refuses(self.build, [f"--portal_figure={self.run / 'loose.sky'}"])

    def test_control_pipe_in_a_spec(self) -> None:
        self.assertIn("Host sets it", self.refuses(self.build, ["--online_control_pipe=x"]))

    def test_hidden_run_without_mute(self) -> None:
        args = [a for a in self.args() if a != "--audio_mute=true"] + ["--window_hidden=true"]
        self.assertIn("audio_mute", self.refuses(la.check_args, self.ctx, args))

    def test_value_kinds(self) -> None:
        self.refuses(self.build, ["--window_width=9"])
        self.refuses(self.build, ["--window_width=6x"])
        self.refuses(self.build, ["--role=client"])
        self.refuses(self.build, ["--user_name=a\\b"])
        self.refuses(self.build, ["--audio_mute"])

    def test_path_spellings(self) -> None:
        for bad in ("\\\\?\\C:\\x", "\\\\server\\share\\x", "C:x", "a:stream", "NUL", "dir\\con.txt", "x. ", "\\x"):
            with self.subTest(bad=bad):
                self.refuses(la.canonical, bad, self.run, "p")

    def test_allow_list_shapes(self) -> None:
        self.refuses(la.AllowList.parse, "[settings.x]\nkind='int'\n")
        self.refuses(la.AllowList.parse, "[settings.x]\nkind='refused'\n")
        self.refuses(la.AllowList.parse, "[settings.user_data_root]\nkind='write_path'\n")
        self.refuses(la.CommandTable.parse, "[commands]\nx = 'write_path'\n")
        self.refuses(la.CommandTable.parse, "[commands]\nsave = 'none'\n")

    def test_fixtures_with_a_junction_are_refused(self) -> None:
        junction(self.fixtures[1] / "link", self.owner)
        self.assertIn("junction", self.refuses(self.build))

    def test_run_folder_is_fresh(self) -> None:
        self.assertIn("exists", self.refuses(la.make_run_folder, self.store, "run1", owner_roots=[self.owner]))
        self.refuses(la.make_run_folder, self.store, "..\\x", owner_roots=[self.owner])


class ScriptTest(_Base):
    def check(self, text: str) -> list:
        return la.check_script(text, self.ctx)

    def test_save_to_an_absolute_owner_path(self) -> None:
        self.refuses(self.check, f"save 82000000 100 {self.owner / 'Swap Force' / 'x.bin'}")
        self.refuses(self.check, "save 82000000 100 C:\\Users\\someone\\Documents\\Skylanders\\Swap Force\\x.bin")

    def test_save_traversal(self) -> None:
        self.assertIn("outside", self.refuses(self.check, "save 82000000 100 ..\\..\\x.bin"))

    def test_save_into_a_junction_to_a_stand_in_owner_folder(self) -> None:
        junction(self.run / "j", self.owner)
        self.assertIn("reparse", self.refuses(self.check, "save 82000000 100 j\\x.bin"))

    def test_safe_script_with_an_unsafe_save(self) -> None:
        # The script itself is stored safely in the run folder; its save points elsewhere.
        script = "wait 1\nsave 82000000 100 " + str(self.root / "x.bin") + "\nquit\n"
        self.refuses(self.build, [], script)
        self.assertFalse((self.run / "script.txt").exists())

    def test_save_hidden_after_a_semicolon_in_a_comment(self) -> None:
        cmds = la.parse_script("# note; save 82000000 100 x.bin\n", self.ctx.commands)
        self.assertEqual([c.name for c in cmds], ["save"])
        self.refuses(self.check, "# note; save 82000000 100 C:\\x.bin")

    def test_shot_escapes(self) -> None:
        self.refuses(self.check, "shot C:\\x")
        self.refuses(self.check, "shot ..\\..\\x")
        self.refuses(self.check, "shot /x")
        self.check("shot hub_ok")

    def test_place_outside_the_figures_copy(self) -> None:
        self.refuses(self.check, "place 0 C:\\elsewhere\\figure.sky")
        (self.run / "loose.sky").write_bytes(b"\0")
        self.refuses(self.check, "place 0 loose.sky")

    def test_renderdoc_on_an_rc_run_and_on_a_development_run(self) -> None:
        self.assertIn("renderdoc", self.refuses(self.check, "renderdoc 2"))
        self.ctx.kind = "dev"
        self.assertIn("renderdoc", self.refuses(self.check, "wait 1; renderdoc"))

    def test_unclassified_registered_command(self) -> None:
        self.assertIn("table", self.refuses(self.check, "dev_new_tool 1 2"))
        self.assertIn("refused", self.refuses(self.check, "dev_secret_dump"))

    def test_registered_commands_with_paths(self) -> None:
        # read_path (save_import <path> <slot>): the file before the last word, read inside the run
        # folder (it may not exist yet) or an existing file in a read root; read_write_paths (save_dump
        # <file> <out>): the first word read so, the second written inside the run folder.
        routes = self.install / "routes" / "unsupported.toml"
        for good in ("save_import handover.xsg 0", 'save_import "hand over.xsg" 1', f"save_import {routes} 2",
                     "save_dump in.xsg out.txt --inventory", f"save_dump {routes} dump.txt"):
            with self.subTest(good=good):
                self.check(good)
        (self.root / "elsewhere.xsg").write_bytes(b"\0")
        (self.owner / "Swap Force" / "x.xsg").write_bytes(b"\0")
        for bad, why in ((f"save_import {self.root / 'elsewhere.xsg'} 0", "outside"),
                         ("save_import ..\\..\\elsewhere.xsg 0", "outside"),
                         (f"save_import {self.owner / 'Swap Force' / 'x.xsg'} 0", "outside"),
                         ("save_import x.xsg", "cannot parse"),
                         (f"save_dump in.xsg {self.root / 'out.txt'}", "outside"),
                         (f"save_dump in.xsg {self.install / 'routes' / 'out.txt'}", "outside"),
                         (f"save_dump {self.root / 'elsewhere.xsg'} out.txt", "outside"),
                         ("save_dump in.xsg", "two words")):
            with self.subTest(bad=bad):
                self.assertIn(why, self.refuses(self.check, bad))
        junction(self.run / "j", self.owner)
        self.refuses(self.check, "save_import j\\x.xsg 0")
        self.refuses(self.check, "save_dump in.xsg j\\out.txt")

    def test_lines_the_game_refuses(self) -> None:
        for bad in ("tap q", "hold a", "player 5", "dump 82000000 5000", "save 1 2", "place 16 x", "fps 0",
                    "waitfor x", "pollwatch 1 300", "scan zz"):
            with self.subTest(bad=bad):
                self.refuses(self.check, bad)

    def test_game_word_rules(self) -> None:
        # CRLF is LF (text mode); a save path runs to the end of the line, spaces included.
        cmds = la.parse_script("save 1 2 my snap.bin\r\nquit\r\n", self.ctx.commands)
        self.assertEqual(la._rest_after(cmds[0].rest, 2), "my snap.bin")
        # A no-break space is part of a word to the game: "save\u00a0..." is one unknown word.
        self.refuses(la.parse_script, "save\u00a01 2 x", self.ctx.commands)
        # A control character the text-mode read treats specially is refused.
        self.refuses(la.parse_script, "wait 1\x1asave 1 2 C:\\x", self.ctx.commands)
        # A save path with a trailing space (Windows would strip it) is refused.
        self.refuses(self.check, "save 1 2 x.bin ")

    def test_conditions_from_the_table(self) -> None:
        t = la.CommandTable.parse("conditions = ['title_ready']\n")
        la.parse_script("waitfor title_ready 5", t)
        self.refuses(la.parse_script, "waitfor nope 5", t)


class SyntheticPeerTest(_Base):
    """Stage 4's synthetic peer (role synthetic-peer): `--name value` pairs, the session address, the
    script in the run folder and the digest contract."""

    DIGEST = "ab" * 32

    def line(self, **over: str) -> list[str]:
        script = self.run / "script.txt"
        if not script.exists():
            script.write_text("swap 1 Wash 600\n", encoding="utf-8")
        values = {"--server": "192.168.1.20", "--port": "52000", "--script": str(script), "--digest": self.DIGEST}
        values.update({f"--{k}": v for k, v in over.items()})
        return [w for k, v in values.items() if v is not None for w in (k, v)]

    def test_build_writes_the_script_and_makes_the_line(self) -> None:
        line = la.build_synthetic_peer(self.ctx, "192.168.1.20:52000", self.DIGEST, "swap 1 Wash 600\n")
        self.assertEqual(line, ["--server", "192.168.1.20", "--port", "52000", "--script",
                                str(self.run / "script.txt"), "--digest", self.DIGEST])
        self.assertEqual((self.run / "script.txt").read_text(encoding="utf-8"), "swap 1 Wash 600\n")
        # A plan's own args: the four settings are set already, anything else is off the list.
        self.refuses(la.build_synthetic_peer, self.ctx, "a:1", self.DIGEST, "x\n", ["--port", "1"])
        self.refuses(la.build_synthetic_peer, self.ctx, "a:1", self.DIGEST, "x\n", ["--figures", "x"])
        self.refuses(la.build_synthetic_peer, self.ctx, "a:1", self.DIGEST, "bad\x01\n")
        self.refuses(la.build_synthetic_peer, self.ctx, "no-port", self.DIGEST, "x\n")
        self.refuses(la.build_synthetic_peer, self.ctx, "a:1", "", "x\n")  # the run names no digest

    def test_the_rules(self) -> None:
        ok = la.check_synthetic_args(self.ctx, self.line(), "192.168.1.20:52000", self.DIGEST)
        self.assertEqual(len(ok), 8)
        # Loopback: --server may be left out (the program's default is 127.0.0.1), nowhere else.
        la.check_synthetic_args(self.ctx, self.line(server=None), "127.0.0.1:52000", self.DIGEST)
        self.refuses(la.check_synthetic_args, self.ctx, self.line(server=None), "192.168.1.20:52000", self.DIGEST)
        session = ("192.168.1.20:52000", self.DIGEST)
        for what, line in {
            "another server": self.line(server="10.0.0.1"),
            "another port": self.line(port="52001"),
            "no port": self.line(port=None),
            "no script": self.line(script=None),
            "no digest": self.line(digest=None),
            "another digest (the contract)": self.line(digest="cd" * 32),
            "a short digest": self.line(digest="ab" * 16),
            "--game (an executable outside the run folder)": self.line(game=str(self.install / "game.exe")),
            "a setting off the list": self.line(figures=str(self.run)),
            "an empty value": self.line(port=""),
            "a script outside the run folder": self.line(script=str(self.root / "x.txt")),
            "a script that does not exist": self.line(script=str(self.run / "none.txt")),
            "a name=value word": ["--server=192.168.1.20", "--port", "52000"],
            "an odd word count": [*self.line(), "--port"],
            "twice": [*self.line(), "--port", "52000"],
        }.items():
            with self.subTest(what=what):
                self.refuses(la.check_synthetic_args, self.ctx, line, *session)
        (self.root / "x.txt").write_text("swap 1 Wash 600\n", encoding="utf-8")
        self.refuses(la.check_synthetic_args, self.ctx, self.line(script=str(self.root / "x.txt")), *session)
        (self.run / "bad.txt").write_bytes(b"swap 1 Wash 600\x00\n")
        self.refuses(la.check_synthetic_args, self.ctx, self.line(script=str(self.run / "bad.txt")), *session)
        # The digest compares without case (the program reads either).
        la.check_synthetic_args(self.ctx, self.line(digest=self.DIGEST.upper()), *session)


if __name__ == "__main__":
    unittest.main()
