"""The owner-data guard by part and the owner's own games (acceptance pass 1, 2026-11-20): classification and the
verdict. No owner data is read here: every folder is a temporary one."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import skytest  # noqa: E402


class OwnerGuard(unittest.TestCase):
    def test_which_part_changed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for p in ("Swap Force", "Figures", "Imaginators"):
                (root / p).mkdir()
                (root / p / "a.bin").write_bytes(b"1")
            before = {str(root): skytest.owner_parts_state(root)}
            (root / "Imaginators" / "b.bin").write_bytes(b"2")
            after = {str(root): skytest.owner_parts_state(root)}
            self.assertEqual(skytest.owner_parts_changed(before, after), [skytest.OWNER_OTHER])
            (root / "Figures" / "c.bin").write_bytes(b"3")
            again = {str(root): skytest.owner_parts_state(root)}
            self.assertEqual(skytest.owner_parts_changed(before, again), ["Figures", skytest.OWNER_OTHER])
            self.assertEqual(skytest.owner_parts_changed(before, before), [])

    def test_the_owners_games_are_not_the_runners(self) -> None:
        table = [(10, 1, "explorer.exe"), (20, 10, "imaginatorsrecompiled.exe"), (30, 1, "python.exe"),
                 (40, 30, "swapforcerecompiled.exe"), (50, 40, "swapforcerecompiled.exe")]
        got = skytest.owner_games_running(30, table=table, started={20: "2026-11-20 03:39:00"},
                                          is_test=lambda pid: False)
        self.assertEqual(got, [("imaginatorsrecompiled.exe", "2026-11-20 03:39:00")])
        # Another runner's test game (its run copy) and the server it hosts are no game of the owner's.
        table += [(60, 1, "python.exe"), (70, 60, "swapforcerecompiled.exe"), (80, 70, "swapforcerecompiled.exe")]
        got = skytest.owner_games_running(30, table=table, started={20: "t20"}, is_test=lambda pid: pid == 70)
        self.assertEqual(got, [("imaginatorsrecompiled.exe", "t20")])

    def test_a_test_game_is_known_by_its_run_copy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "run" / "H" / "data"
            (data / "Online" / "session").mkdir(parents=True)
            (data / skytest.RUN_COPY_MARKER).write_text("a skytest run copy\n", encoding="ascii")
            other = Path(d) / "play" / "host"
            other.mkdir(parents=True)
            lines = {1: f'g.exe "--user_data_root={data}"', 2: f'g.exe "--user_data_root={data / "Online" / "session"}"',
                     3: f'g.exe "--user_data_root={other}"', 4: "g.exe", 5: None}
            old = skytest.store_paths.pid_command_line
            skytest.store_paths.pid_command_line = lambda pid: lines[pid]
            try:
                got = {pid: skytest.test_game(pid, roots=[Path(d) / "owner"]) for pid in lines}
                # under the owner's own folder: never looked into, his game
                lines[6] = f'g.exe "--user_data_root={Path(d) / "owner" / "x"}"'
                got[6] = skytest.test_game(6, roots=[Path(d) / "owner"])
            finally:
                skytest.store_paths.pid_command_line = old
            self.assertEqual(got, {1: True, 2: True, 3: False, 4: False, 5: False, 6: False})

    def test_the_verdict(self) -> None:
        roots = [Path(r"C:\Users\x\Documents\Skylanders")]
        own = [{"peer": "H", "command": ["g.exe", r"--user_data_root=D:\SwapForceOnline-data\tests\int2\runs\r\H\data"]}]
        logs = {"H": r"[t] Data folder: D:\SwapForceOnline-data\tests\int2\runs\r\H\data (--user_data_root)"}
        ok, ev = skytest.case_own_data_folders(own, logs, roots)
        self.assertTrue(ok)
        games = [("imaginatorsrecompiled.exe", "2026-11-20 03:39:00")]
        line, risk = skytest.owner_data_verdict(ok, ev, [skytest.OWNER_OTHER], games)
        self.assertFalse(risk)
        self.assertEqual(line, "owner data: untouched by this case (its games ran with their own data folders: H ok); "
                               "another game of the owner was running: imaginatorsrecompiled.exe (started 2026-11-20 "
                               "03:39:00); owner data changed during the run: only other games' folders")
        # Swap Force changed and nobody else played: at risk.
        line, risk = skytest.owner_data_verdict(True, ev, ["Swap Force"], [])
        self.assertTrue(risk)
        self.assertTrue(line.startswith(skytest.OWNER_AT_RISK), line)
        # A game of the case on the owner's data: at risk whatever else ran.
        bad = [{"peer": "D", "command": ["g.exe", r"--user_data_root=C:\Users\x\Documents\Skylanders"]}]
        ok, ev = skytest.case_own_data_folders(bad, {}, roots)
        self.assertFalse(ok)
        line, risk = skytest.owner_data_verdict(ok, ev, [], games)
        self.assertTrue(risk)
        self.assertIn("D: its --user_data_root is the owner's data", line)
        # A hosted server (no command of its own; a compared run's `off1:S`): its log only.
        self.assertEqual(skytest.case_own_data_folders([{"peer": "off1:S", "command": []}], {}, roots), (True, []))
        missing = [{"peer": "D", "command": ["g.exe"]}]
        self.assertFalse(skytest.case_own_data_folders(missing, {}, roots)[0])
        # A unit case's binary or a synthetic peer is no game: left out.
        unit = [{"peer": "U", "command": ["online_tests.exe", "--gtest"]}]
        self.assertTrue(skytest.case_own_data_folders(unit, {}, roots, "swapforcerecompiled.exe")[0])
        game = [{"peer": "D", "command": [r"C:\snap\swapforcerecompiled.exe"]}]
        self.assertFalse(skytest.case_own_data_folders(game, {}, roots, "swapforcerecompiled.exe")[0])


if __name__ == "__main__":
    unittest.main()
