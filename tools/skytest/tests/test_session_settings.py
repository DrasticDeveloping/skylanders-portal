"""The owner's session settings reader (session_settings.py): the file's rules (never an empty value, names only,
no name both on and not passed) and the merge onto one game's command line (the spec's value wins, no name twice).
Run: python -I -m unittest discover -s tools/skytest/tests"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import session_settings as sessions  # noqa: E402

GOOD = """version = "v1"
[on]
online_shared_coins = true
online_authored_ids = 2
online_pause_safe = false
[not_passed]
online_scene_lines = "until its spec is read"
"""


class SessionSettingsTest(unittest.TestCase):
    def test_the_file_and_its_arguments(self) -> None:
        ss = sessions.parse(GOOD)
        self.assertEqual(ss.args(), ["--online_shared_coins=true", "--online_authored_ids=2",
                                     "--online_pause_safe=false"])
        self.assertEqual(ss.not_passed, {"online_scene_lines": "until its spec is read"})
        self.assertTrue(ss.label().startswith("v1 (sha256 "))
        self.assertEqual(sessions.parse(GOOD.replace("\n", "\r\n")).digest, ss.digest)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tests").mkdir()
            (Path(d) / "tests" / "session_settings.toml").write_text(GOOD, encoding="utf-8")
            self.assertEqual(sessions.load(Path(d)).on, ss.on)
            with self.assertRaises(sessions.SessionSettingsError):
                sessions.load(Path(d) / "nowhere")

    def test_what_the_file_may_not_say(self) -> None:
        bad = {
            "empty value": GOOD.replace("online_authored_ids = 2", 'online_authored_ids = ""'),
            "no version": GOOD.replace('version = "v1"', ""),
            "on and off": GOOD.replace('online_scene_lines = "until', 'online_shared_coins = "until'),
            "no why": GOOD.replace('"until its spec is read"', '""'),
            "a table": GOOD + "[on.x]\ny = 1\n",
            "unknown key": GOOD + "[extra]\na = 1\n",
            "a bad name": GOOD.replace("online_pause_safe", '"--online_pause_safe"'),
            "a float": GOOD.replace("= 2", "= 2.5"),
            "not TOML": GOOD + "[on\n",
            "nothing on": 'version = "v1"\n[on]\n',
        }
        for why, text in bad.items():
            with self.subTest(why):
                with self.assertRaises(sessions.SessionSettingsError):
                    sessions.parse(text)

    def test_one_games_arguments_keep_the_specs_value(self) -> None:
        ss = sessions.parse(GOOD)
        got = sessions.for_game(ss, ["--audio_mute=true"], ["--online_pause_safe=true", "--trace_saves=true"])
        self.assertEqual(got, ["--online_shared_coins=true", "--online_authored_ids=2"])
        self.assertEqual(sessions.unknown_names(ss, {"online_shared_coins", "online_authored_ids",
                                                     "online_pause_safe"}), ["online_scene_lines"])


if __name__ == "__main__":
    unittest.main()
