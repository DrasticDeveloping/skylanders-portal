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
    def test_rules(self) -> None:
        ok = la.build_synthetic_peer(self.ctx, ["--server=192.168.1.20:52000", f"--log_file={self.run / 'p3.log'}"],
                                     "192.168.1.20:52000")
        self.assertEqual(len(ok), 2)
        self.refuses(la.build_synthetic_peer, self.ctx, ["--server=10.0.0.1:1"], "192.168.1.20:52000")
        self.refuses(la.build_synthetic_peer, self.ctx, [], "192.168.1.20:52000")
        self.refuses(la.build_synthetic_peer, self.ctx, ["--server=a:1", "--figures=x"], "a:1")
        self.refuses(la.build_synthetic_peer, self.ctx, ["--server=a:1", "--log_file="], "a:1")
        self.refuses(la.build_synthetic_peer, self.ctx, ["--server=a:1", f"--log_file={self.root / 'x.log'}"], "a:1")


if __name__ == "__main__":
    unittest.main()
