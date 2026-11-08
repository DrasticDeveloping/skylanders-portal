"""Tests for the remote agent (stage 8 item 6, `remote-agent-refusals`: the protocol, the executable
roles, the connection deadlines and the refusals). Loopback only (127.0.0.1); fake processes stand in
for the game, so nothing is started. Run:
python -I -m unittest discover -s tools/skytest/tests -p "test_remote_agent.py"
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import launch_args as la  # noqa: E402
import remote_agent as ra  # noqa: E402

from test_launch_args import ALLOW, COMMANDS  # noqa: E402


class FakeProc:
    def __init__(self, argv: list[str], cwd: Path) -> None:
        self.argv, self.cwd, self.code = argv, cwd, None

    def poll(self):
        return self.code


class FakeJob:
    instances: list["FakeJob"] = []

    def __init__(self) -> None:
        self.procs: list[FakeProc] = []
        self.terminated = False
        FakeJob.instances.append(self)

    def add(self, p: FakeProc) -> None:
        self.procs.append(p)

    def terminate(self, code: int = 1) -> None:
        self.terminated = True
        for p in self.procs:
            p.code = code

    def close(self) -> None:
        pass


def fake_launcher(argv: list[str], cwd: Path, job: FakeJob) -> FakeProc:
    p = FakeProc(argv, cwd)
    job.add(p)
    return p


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class AgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {k: getattr(ra, k) for k in ("AUTH_DEADLINE_S", "FRAME_DEADLINE_S", "IDLE_DEADLINE_S",
                                                  "HEARTBEAT_S")}
        ra.AUTH_DEADLINE_S, ra.FRAME_DEADLINE_S, ra.IDLE_DEADLINE_S, ra.HEARTBEAT_S = 0.6, 0.8, 1.5, 0.4
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.root = root
        self.store = root / "store"
        self.store.mkdir()
        self.install = root / "Install"
        (self.install / "rom").mkdir(parents=True)
        (self.install / "routes").mkdir()
        files = {"swapforcerecompiled.exe": b"MZ game", "synthetic_peer.exe": b"MZ peer",
                 "settings_template.toml": b"[General]\n", "routes\\unsupported.toml": b"x = 1\n",
                 "engine.dll": b"MZ dll"}
        roles = {"swapforcerecompiled.exe": "game", "synthetic_peer.exe": "synthetic-peer"}
        entries = []
        for name, data in files.items():
            p = self.install / name.replace("\\", os.sep)
            p.write_bytes(data)
            e = {"name": name, "dest": "{app}", "size": len(data), "sha256": sha(data)}
            if name in roles:
                e["role"] = roles[name]
            entries.append(e)
        entries.append({"name": "xexcheck.exe", "dest": "{tmp}", "size": 2, "sha256": sha(b"MZ")})
        self.manifest = json.dumps({"format": 1, "files": entries})
        self.msha = sha(self.manifest.encode())
        self.owner = root / "Owner" / "Skylanders"
        self.owner.mkdir(parents=True)
        self.mach = ra.Machine(self.store, self.install, la.AllowList.parse(ALLOW), la.CommandTable.parse(COMMANDS),
                               launcher=fake_launcher, job_factory=FakeJob, owner_roots=[self.owner])
        self.token = bytes(range(16))
        self.agent = ra.Agent("127.0.0.1", 0, "127.0.0.0/8", self.mach, self.msha, token=self.token)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.agent.serve, args=(self.stop,), daemon=True)
        self.thread.start()
        self.counter = 1000

    def tearDown(self) -> None:
        self.stop.set()
        self.thread.join(5)
        for k, v in self.saved.items():
            setattr(ra, k, v)
        self.tmp.cleanup()

    def until(self, cond, limit: float = 10.0) -> None:
        end = time.monotonic() + limit
        while not cond():
            self.assertLess(time.monotonic(), end, "condition not met in time")
            time.sleep(0.05)

    def client(self) -> ra.Client:
        c = ra.Client("127.0.0.1", self.agent.port, self.token, counter=self.counter)
        return c

    def call(self, c: ra.Client, op: str, **kw) -> dict:
        r = c.call(op, **kw)
        self.counter = c.counter
        return r

    def raw(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.agent.port), timeout=5)

    def closed(self, s: socket.socket, within: float = 3.0) -> bool:
        s.settimeout(within)
        try:
            return s.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        except socket.timeout:
            return False

    def verified(self) -> ra.Client:
        c = self.client()
        self.assertTrue(self.call(c, "verify", manifest=self.manifest)["ok"])
        return c

    def fixtures(self, c: ra.Client, files: dict[str, bytes] | None = None) -> dict:
        files = files if files is not None else {"saves/profile/save.bin": b"\1\2\3", "figures/hero.sky": b"\0" * 32}
        r = self.call(c, "fixtures", action="begin",
                      files=[{"path": n, "size": len(d), "sha256": sha(d)} for n, d in files.items()])
        if not r["ok"]:
            return r
        for i, d in enumerate(files.values()):
            if d:
                r = self.call(c, "fixtures", action="chunk", index=i, offset=0, data=base64.b64encode(d).decode())
                if not r["ok"]:
                    return r
        return self.call(c, "fixtures", action="end")

    def plan(self, **over) -> dict:
        p = {"name": "smoke", "timeout_s": 60, "entries": [
            {"role": "game", "peer": "D", "args": ["--window_width=640"], "script": "wait 1\nquit\n"}]}
        p.update(over)
        return p

    # --- protocol -----------------------------------------------------------------------------------

    def test_hello_and_status(self) -> None:
        c = self.client()
        r = self.call(c, "hello")
        self.assertEqual(r["manifest_sha256"], self.msha)
        self.assertEqual(self.call(c, "status")["run"], None)
        self.assertFalse(self.call(c, "launch")["ok"])  # nothing but the nine operations

    def test_bad_hmac_closes_and_leaves_the_counter(self) -> None:
        s = self.raw()
        s.sendall(ra.encode_frame(b"x" * 16, 5000, {"op": "hello"}))
        self.assertTrue(self.closed(s))
        self.assertEqual(self.agent.last_counter, 0)

    def test_replayed_counter_closes(self) -> None:
        c = self.client()
        self.call(c, "hello")
        s = self.raw()
        s.sendall(ra.encode_frame(self.token, c.counter, {"op": "hello"}))
        self.assertTrue(self.closed(s))

    def test_request_over_64_kib(self) -> None:
        s = self.raw()
        s.sendall(ra.encode_frame(self.token, 9999, {"op": "hello", "pad": "x" * (64 * 1024)}))
        self.assertTrue(self.closed(s))
        self.assertEqual(self.agent.last_counter, 0)

    def test_wildcard_bind_and_foreign_subnet(self) -> None:
        with self.assertRaises(SystemExit):
            ra.Agent("0.0.0.0", 0, "0.0.0.0/0", self.mach, self.msha)
        with self.assertRaises(SystemExit):
            ra.Agent("127.0.0.1", 0, "10.0.0.0/8", self.mach, self.msha)
        import ipaddress
        self.agent.subnet = ipaddress.ip_network("127.0.0.2/32")
        s = self.raw()
        self.assertTrue(self.closed(s, 1.0))
        self.agent.subnet = ipaddress.ip_network("127.0.0.0/8")

    # --- deadlines ----------------------------------------------------------------------------------

    def test_stalled_connections_then_a_legitimate_runner(self) -> None:
        silent = self.raw()
        header3 = self.raw()
        header3.sendall(b"\0\0\x03")
        body10 = self.raw()
        body10.sendall(struct.pack(">I", 1000) + b"x" * 10)
        fourth = self.raw()
        time.sleep(0.1)
        fifth = self.raw()
        self.assertTrue(self.closed(fifth, 0.3))  # the fifth at once
        t0 = time.monotonic()
        for s in (silent, header3, body10, fourth):
            self.assertTrue(self.closed(s, 3.0))
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(self.agent.last_counter, 0)
        self.assertIsNone(self.agent.run)
        c = self.client()
        self.assertTrue(self.call(c, "status")["ok"])

    def test_authenticated_frame_must_complete(self) -> None:
        c = self.client()
        self.call(c, "hello")
        c.sock.sendall(b"\0\0")  # a header that never completes
        self.assertTrue(self.closed(c.sock, 3.0))

    def test_idle_authenticated_connection_closes(self) -> None:
        c = self.client()
        self.call(c, "hello")
        self.assertTrue(self.closed(c.sock, 3.0))

    def test_stalls_do_not_starve_heartbeats(self) -> None:
        c = self.verified()
        self.assertTrue(self.fixtures(c)["ok"])
        self.assertTrue(self.call(c, "start", plan=self.plan())["ok"])
        stalls = [self.raw() for _ in range(3)]
        for s in stalls:
            s.sendall(b"\0")
        t_end = time.monotonic() + 2.0  # past the three-heartbeat limit (1.2 s here)
        while time.monotonic() < t_end:
            self.assertEqual(self.call(c, "status")["state"], "running")
            time.sleep(0.1)
        for s in stalls:
            s.close()

    def test_lost_heartbeats_end_the_run_and_keep_its_folder(self) -> None:
        c = self.verified()
        self.fixtures(c)
        r = self.call(c, "start", plan=self.plan())
        self.assertTrue(r["ok"], r)
        job = FakeJob.instances[-1]
        self.until(lambda: self.agent.run.state != "running")
        self.assertTrue(job.terminated)
        self.assertEqual(self.agent.run.state, "ended: heartbeat lost")
        self.assertTrue((self.store / "runs" / r["run"]).is_dir())
        self.assertFalse((self.store / "run.lock").exists())

    # --- refusals -----------------------------------------------------------------------------------

    def test_start_runs_the_game_by_role_with_a_checked_line(self) -> None:
        c = self.verified()
        self.fixtures(c)
        r = self.call(c, "start", plan=self.plan())
        self.assertTrue(r["ok"], r)
        proc = FakeJob.instances[-1].procs[0]
        self.assertEqual(proc.argv[0], str(self.install / "swapforcerecompiled.exe"))
        self.assertIn("--portal_mode=software", proc.argv)
        self.assertIn(f"--game_data_root={self.install / 'rom'}", proc.argv)
        self.assertEqual(proc.cwd.parent.name, r["run"])
        # A second start while one runs.
        self.assertIn("one at a time", self.call(c, "start", plan=self.plan())["error"])
        # Nothing that could change the install while a peer runs.
        self.assertFalse(self.call(c, "verify", manifest=self.manifest)["ok"])
        self.assertTrue(self.call(c, "stop")["ok"])
        listing = self.call(c, "collect", run=r["run"])
        self.assertTrue(any(f["path"] == "D/script.txt" for f in listing["files"]))
        got = self.call(c, "collect", run=r["run"], path="D/script.txt")
        self.assertEqual(base64.b64decode(got["data"]), b"wait 1\nquit\n")
        self.assertFalse(self.call(c, "collect", run=r["run"], path="..\\..\\manifest.json")["ok"])
        self.assertEqual(self.call(c, "clean", fixtures=True)["removed"], [r["run"]])
        self.assertEqual(list((self.store / "runs").iterdir()), [])
        self.assertFalse((self.store / "fixtures").exists())

    def test_start_before_verify(self) -> None:
        c = self.client()
        self.assertIn("verify", self.call(c, "start", plan=self.plan())["error"])

    def test_install_payload_mismatch(self) -> None:
        (self.install / "engine.dll").write_bytes(b"MZ dl1")
        c = self.client()
        self.assertIn("does not match", self.call(c, "verify", manifest=self.manifest)["error"])

    def test_manifest_with_another_hash(self) -> None:
        c = self.client()
        other = self.manifest.replace('"format": 1', '"format":1')
        self.assertIn("another hash", self.call(c, "verify", manifest=other)["error"])

    def test_bad_plans(self) -> None:
        c = self.verified()
        self.fixtures(c)
        game = {"role": "game", "peer": "D", "script": "quit"}
        syn = lambda p: {"role": "synthetic-peer", "peer": p, "args": ["--server=10.0.0.1:5"]}  # noqa: E731
        cases = {
            "executable path": self.plan(entries=[{**game, "exe": "C:\\Windows\\System32\\cmd.exe"}]),
            "path key": self.plan(entries=[{**game, "path": "x.exe"}]),
            "unknown role": self.plan(entries=[{"role": "shell", "peer": "D"}]),
            "two games": self.plan(entries=[game, {**game, "peer": "E"}]),
            "three synthetic": self.plan(server="10.0.0.1:5", entries=[game, syn("P3"), syn("P4"), syn("P5")]),
            "plan key": {**self.plan(), "exe": "x"},
        }
        for what, plan in cases.items():
            with self.subTest(what=what):
                r = self.call(c, "start", plan=plan)
                self.assertFalse(r["ok"], what)
        self.assertIsNone(self.agent.run)
        self.assertFalse((self.store / "run.lock").exists())

    def test_script_refused_through_the_agent(self) -> None:
        c = self.verified()
        self.fixtures(c)
        r = self.call(c, "start", plan=self.plan(entries=[{"role": "game", "peer": "D", "script": "renderdoc"}]))
        self.assertIn("renderdoc", r["error"])
        r = self.call(c, "start", plan=self.plan(entries=[{"role": "game", "peer": "D",
                                                           "script": "save 1 2 C:\\x.bin"}]))
        self.assertFalse(r["ok"])
        self.assertFalse((self.store / "run.lock").exists())

    def test_synthetic_peer_with_another_installed_hash(self) -> None:
        c = self.verified()
        self.fixtures(c)
        (self.install / "synthetic_peer.exe").write_bytes(b"MZ peeR")
        plan = self.plan(server="10.0.0.1:5", entries=[{"role": "game", "peer": "D", "script": "quit"},
                                                       {"role": "synthetic-peer", "peer": "P3",
                                                        "args": ["--server=10.0.0.1:5"]}])
        r = self.call(c, "start", plan=plan)
        self.assertFalse(r["ok"])

    def test_synthetic_peer_by_role(self) -> None:
        c = self.verified()
        self.fixtures(c)
        plan = self.plan(server="10.0.0.1:5", entries=[{"role": "game", "peer": "D", "script": "quit"},
                                                       {"role": "synthetic-peer", "peer": "P3",
                                                        "args": ["--server=10.0.0.1:5"]}])
        r = self.call(c, "start", plan=plan)
        self.assertTrue(r["ok"], r)
        self.assertEqual(FakeJob.instances[-1].procs[1].argv[0], str(self.install / "synthetic_peer.exe"))

    def test_fixture_refusals(self) -> None:
        c = self.verified()
        self.assertIn("executable header", self.fixtures(c, {"figures/evil.sky": b"MZ\x90\0"})["error"])
        self.assertIn("layout", self.fixtures(c, {"..\\x": b"1"})["error"])
        self.assertIn("layout", self.fixtures(c, {"bin/x.dll": b"1"})["error"])
        r = self.call(c, "fixtures", action="begin", files=[{"path": "saves/a", "size": 65 << 20, "sha256": "0" * 64}])
        self.assertIn("64 MiB", r["error"])
        # A file whose bytes differ from its declared hash.
        self.call(c, "fixtures", action="begin", files=[{"path": "saves/a", "size": 1, "sha256": sha(b"2")}])
        self.call(c, "fixtures", action="chunk", index=0, offset=0, data=base64.b64encode(b"1").decode())
        self.assertIn("does not match", self.call(c, "fixtures", action="end")["error"])
        # "MZ" split over two chunks: the first carries only "M".
        self.call(c, "fixtures", action="begin", files=[{"path": "figures/split.sky", "size": 4, "sha256": sha(b"MZ\x90\0")}])
        self.assertTrue(self.call(c, "fixtures", action="chunk", index=0, offset=0, data=base64.b64encode(b"M").decode())["ok"])
        self.assertTrue(self.call(c, "fixtures", action="chunk", index=0, offset=1,
                                  data=base64.b64encode(b"Z\x90\0").decode())["ok"])
        self.assertIn("executable header", self.call(c, "fixtures", action="end")["error"])
        self.assertFalse((self.store / "fixtures").exists())

    def test_record_shapes(self) -> None:
        bad = [
            {"format": 2, "files": []},
            {"format": 1, "files": [{"name": "..\\x.exe", "dest": "{app}", "sha256": "0" * 64}]},
            {"format": 1, "files": [{"name": "a.exe", "dest": "C:\\", "sha256": "0" * 64}]},
            {"format": 1, "files": [{"name": "a.exe", "dest": "{app}", "sha256": "0" * 64, "role": "shell"}]},
            {"format": 1, "files": [{"name": "a.exe", "dest": "{app}", "sha256": "0" * 64, "role": "game"},
                                    {"name": "b.exe", "dest": "{app}", "sha256": "0" * 64, "role": "game"}]},
            {"format": 1, "files": [{"name": "a.dll", "dest": "{app}", "sha256": "0" * 64, "role": "game"}]},
        ]
        for m in bad:
            with self.subTest(m=m):
                with self.assertRaises(ra.Refused):
                    ra.Manifest.parse(json.dumps(m))

    # --- run-local ----------------------------------------------------------------------------------

    def test_run_local(self) -> None:
        c = self.verified()
        self.fixtures(c)
        self.assertTrue(self.call(c, "stage", plan=self.plan(name="local1"))["ok"])
        self.assertFalse(self.call(c, "stage", plan=self.plan(name="bad", entries=[
            {"role": "game", "peer": "D", "script": "dev_unknown"}]))["ok"])
        with self.assertRaises(ra.Refused):
            ra.run_local(self.mach, "local1", "0" * 64)
        # The agent's run holds the lock: run-local is refused meanwhile.
        self.assertTrue(self.call(c, "start", plan=self.plan())["ok"])
        with self.assertRaises(ra.Refused):
            ra.run_local(self.mach, "local1", self.msha)
        self.call(c, "stop")
        stop = threading.Event()
        done = {}
        t = threading.Thread(target=lambda: done.update(ra.run_local(self.mach, "local1", self.msha, 0.05, stop)))
        t.start()
        self.until(lambda: (self.store / "run.lock").exists() or not t.is_alive())
        self.assertTrue(t.is_alive())
        c2 = self.client()
        self.assertIn("run-local", self.call(c2, "clean")["error"])
        self.assertIn("while a peer runs", self.fixtures(c2)["error"])
        self.assertTrue(any((self.store / "runs").iterdir()))
        stop.set()
        t.join(5)
        self.assertEqual(done["state"], "stopped")
        self.assertTrue(FakeJob.instances[-1].terminated)
        self.assertFalse((self.store / "run.lock").exists())
        self.assertTrue((self.store / "runs" / done["run"] / "exit.json").is_file())

    def test_run_local_refuses_a_changed_install(self) -> None:
        c = self.verified()
        self.fixtures(c)
        self.call(c, "stage", plan=self.plan(name="local2"))
        (self.install / "swapforcerecompiled.exe").write_bytes(b"MZ gam3")
        with self.assertRaises(ra.Refused):
            ra.run_local(self.mach, "local2", self.msha)


class LockTest(unittest.TestCase):
    def test_stale_lock_is_taken_over_and_a_live_one_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lock = ra.RunLock(Path(d))
            (Path(d) / "run.lock").write_text("999999999 agent\n", encoding="utf-8")
            lock.acquire("test")
            other = ra.RunLock(Path(d))
            with self.assertRaises(ra.Refused):
                other.acquire("second")
            lock.release()
            self.assertFalse((Path(d) / "run.lock").exists())

    @unittest.skipUnless(os.name == "nt", "Windows refuses to delete an open file")
    def test_a_held_lock_is_never_taken_over(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lock = ra.RunLock(Path(d))
            lock.acquire("test")
            # As a holder looks before it wrote its pid, or after its pid was reused: no live pid.
            (Path(d) / "run.lock").write_text("999999999 agent\n", encoding="utf-8")
            other = ra.RunLock(Path(d))
            with self.assertRaises(ra.Refused):
                other.acquire("second")
            self.assertFalse(other.held)
            self.assertTrue((Path(d) / "run.lock").exists())
            lock.release()
            self.assertFalse((Path(d) / "run.lock").exists())


if __name__ == "__main__":
    unittest.main()
