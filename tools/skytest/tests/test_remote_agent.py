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

from test_launch_args import ALLOW, COMMANDS, junction  # noqa: E402


def unlink_junction(p: Path) -> None:
    """Removes the link itself, never what it points at."""
    if os.path.islink(p):
        os.unlink(p)
    else:
        os.rmdir(p)


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
        syn = lambda p: {"role": "synthetic-peer", "peer": p, "script": "swap 1 Wash 600\n"}  # noqa: E731
        cases = {
            "executable path": self.plan(entries=[{**game, "exe": "C:\\Windows\\System32\\cmd.exe"}]),
            "path key": self.plan(entries=[{**game, "path": "x.exe"}]),
            "unknown role": self.plan(entries=[{"role": "shell", "peer": "D"}]),
            "two games": self.plan(entries=[game, {**game, "peer": "E"}]),
            "three synthetic": self.plan(server="10.0.0.1:5", entries=[game, syn("P3"), syn("P4"), syn("P5")]),
            "plan key": {**self.plan(), "exe": "x"},
            "long name": {**self.plan(), "name": "x" * 41},  # its run folder's name would pass 64
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
                                                        "script": "swap 1 Wash 600\n"}])
        r = self.call(c, "start", plan=plan)
        self.assertFalse(r["ok"])
        self.assertIn("synthetic_peer.exe", r["error"])

    def test_synthetic_peer_by_role(self) -> None:
        c = self.verified()
        self.fixtures(c)
        plan = self.plan(server="10.0.0.1:5", entries=[{"role": "game", "peer": "D", "script": "quit"},
                                                       {"role": "synthetic-peer", "peer": "P3",
                                                        "script": "swap 1 Wash 600\n"}])
        r = self.call(c, "start", plan=plan)
        self.assertTrue(r["ok"], r)
        argv = FakeJob.instances[-1].procs[1].argv
        self.assertEqual(argv[0], str(self.install / "synthetic_peer.exe"))
        # Stage 4's line: the plan's session address, its script in the peer's run folder, and the digest
        # contract (the payload record's `game` SHA-256, the build digest S checks in a Hello).
        script = Path(argv[argv.index("--script") + 1])
        self.assertEqual(argv[1:5], ["--server", "10.0.0.1", "--port", "5"])
        self.assertEqual(argv[argv.index("--digest") + 1], sha(b"MZ game"))
        self.assertEqual(script.name, "script.txt")
        self.assertEqual(script.parent.name, "P3")
        self.assertEqual(script.read_text(encoding="utf-8"), "swap 1 Wash 600\n")

    def test_synthetic_peer_line_refusals(self) -> None:
        c = self.verified()
        self.fixtures(c)
        game = {"role": "game", "peer": "D", "script": "quit"}
        for what, (server, extra) in {
            "the peer's own --game": ("10.0.0.1:5", {"args": ["--game", "C:\\x.exe"]}),
            "a second --port": ("10.0.0.1:5", {"args": ["--port", "6"]}),
            "a name=value setting": ("10.0.0.1:5", {"args": ["--server=10.0.0.1:5"]}),
            "a control character in its script": ("10.0.0.1:5", {"script": "swap\x00\n"}),
            "a session address without a port": ("10.0.0.1", {}),
        }.items():
            with self.subTest(what=what):
                syn = {"role": "synthetic-peer", "peer": "P3", "script": "swap 1 Wash 600\n", **extra}
                r = self.call(c, "start", plan=self.plan(server=server, entries=[game, syn]))
                self.assertFalse(r["ok"], what)
                self.assertIsNone(self.agent.run)
                self.assertFalse((self.store / "run.lock").exists())

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

    def test_a_listing_comes_in_bounded_pages(self) -> None:
        saved = ra.LIST_MAX
        self.addCleanup(setattr, ra, "LIST_MAX", saved)
        ra.LIST_MAX = 2
        c = self.verified()
        self.fixtures(c)
        r = self.call(c, "start", plan=self.plan())
        self.call(c, "stop")
        folder = self.store / "runs" / r["run"]
        for i in range(5):
            (folder / "D" / f"out{i}.txt").write_text(str(i), encoding="utf-8")
        junction(folder / "D" / "alias", self.owner)  # neither entered nor listed
        (self.owner / "secret.txt").write_text("owner", encoding="utf-8")
        seen, start = [], 0
        while True:
            page = self.call(c, "collect", run=r["run"], start=start)
            self.assertLessEqual(len(page["files"]), 2)
            seen += [f["path"] for f in page["files"]]
            if page["next"] is None:
                break
            start = page["next"]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertTrue({f"D/out{i}.txt" for i in range(5)} <= set(seen))
        self.assertFalse(any("alias" in p or "secret" in p for p in seen))
        self.assertFalse(self.call(c, "collect", run=r["run"], start=-1)["ok"])
        unlink_junction(folder / "D" / "alias")

    def test_collection_is_counted_per_run_across_runs_and_restarts(self) -> None:
        saved = ra.COLLECT_LIMIT
        self.addCleanup(setattr, ra, "COLLECT_LIMIT", saved)
        ra.COLLECT_LIMIT = 16
        c = self.verified()
        self.fixtures(c)
        a = self.call(c, "start", plan=self.plan(name="runA"))["run"]
        self.call(c, "stop")
        (self.store / "runs" / a / "D" / "big.bin").write_bytes(b"x" * 40)
        b = self.call(c, "start", plan=self.plan(name="runB"))["run"]  # B is the current run now
        self.call(c, "stop")
        got = self.call(c, "collect", run=a, path="D/big.bin", offset=0)
        self.assertTrue(got["ok"], got)
        self.assertEqual(len(base64.b64decode(got["data"])), 16)
        self.assertFalse(got["eof"])
        for off in (16, 0, 24):  # A's budget is spent, whatever is asked for
            r = self.call(c, "collect", run=a, path="D/big.bin", offset=off)
            self.assertFalse(r["ok"])
            self.assertIn("collection limit", r["error"])
        r = self.call(c, "collect", run=b, path="D/script.txt", offset=0)  # B counts on its own
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["eof"])
        # A restarted agent keeps the counts.
        again = ra.Agent("127.0.0.1", 0, "127.0.0.0/8", self.mach, self.msha, token=self.token)
        self.addCleanup(again.shutdown)
        with self.assertRaises(ra.Refused):
            again.op_collect({"run": a, "path": "D/big.bin", "offset": 0}, 0.0)
        # A run with no record (an older agent's) is not collected.
        legacy = self.store / "runs" / "legacy"
        legacy.mkdir()
        (legacy / "x.txt").write_text("1", encoding="utf-8")
        with self.assertRaises(ra.Refused) as cm:
            again.op_collect({"run": "legacy", "path": "x.txt", "offset": 0}, 0.0)
        self.assertIn("no collection record", str(cm.exception))
        # clean takes a run's record with it.
        self.assertTrue(self.call(c, "clean", run=a)["ok"])
        self.assertFalse((self.store / "collected" / f"{a}.json").exists())
        self.assertTrue((self.store / "collected" / f"{b}.json").exists())

    # --- the agent's own writes stay in its store (Sol r1 F1) ----------------------------------------

    def test_a_staged_junction_to_an_owner_folder(self) -> None:
        sentinel = self.owner / "sentinel.txt"
        sentinel.write_bytes(b"owner")
        c = self.verified()
        junction(self.store / "staged", self.owner)
        r = self.call(c, "stage", plan=self.plan(name="local1"))
        self.assertFalse(r["ok"])
        self.assertIn("reparse point", r["error"])
        with self.assertRaises(ra.Refused):  # run-local does not read a plan through it either
            ra.run_local(self.mach, "sentinel", self.msha)
        unlink_junction(self.store / "staged")
        # The fixtures' incoming folder as a junction: never emptied, never written through.
        junction(self.store / "fixtures-incoming", self.owner)
        r = self.fixtures(c)
        self.assertFalse(r["ok"])
        self.assertIn("reparse point", r["error"])
        unlink_junction(self.store / "fixtures-incoming")
        self.assertEqual(sorted(os.listdir(self.owner)), ["sentinel.txt"])
        self.assertEqual(sentinel.read_bytes(), b"owner")

    def test_a_hard_linked_manifest_is_replaced_not_written_through(self) -> None:
        owner_file = self.owner / "profile.sav"
        owner_file.write_bytes(b"owner")
        os.link(owner_file, self.store / "manifest.json")
        self.verified()
        self.assertEqual(owner_file.read_bytes(), b"owner")
        self.assertEqual((self.store / "manifest.json").read_text(encoding="utf-8"), self.manifest)

    def test_run_local_refuses_a_changed_install(self) -> None:
        c = self.verified()
        self.fixtures(c)
        self.call(c, "stage", plan=self.plan(name="local2"))
        (self.install / "swapforcerecompiled.exe").write_bytes(b"MZ gam3")
        with self.assertRaises(ra.Refused):
            ra.run_local(self.mach, "local2", self.msha)


class ClientTest(unittest.TestCase):
    """The runner's side bounds what an agent (or anything answering on its port) sends back (Sol r1 F2):
    a fake server on loopback stands in."""

    token = bytes(range(16))

    def fake_server(self, reply: bytes, trickle: bool = False) -> int:
        lsock = socket.create_server(("127.0.0.1", 0))
        done = threading.Event()
        self.addCleanup(done.set)
        self.addCleanup(lsock.close)

        def serve() -> None:
            conn, _ = lsock.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(reply)
                while not done.is_set():
                    if trickle:
                        try:
                            conn.sendall(b"x")
                        except OSError:
                            return
                    done.wait(0.1)

        threading.Thread(target=serve, daemon=True).start()
        return lsock.getsockname()[1]

    def test_an_oversized_reply_header_is_refused_before_its_body(self) -> None:
        port = self.fake_server(struct.pack(">I", 0xFFFFFFF0))  # a header and no body, the connection kept open
        c = ra.Client("127.0.0.1", port, self.token, counter=1, timeout=5.0)
        t0 = time.monotonic()
        with self.assertRaises(ConnectionError) as cm:
            c.call("hello")
        self.assertLess(time.monotonic() - t0, 1.0)  # at once: it never waited for the body
        self.assertIn("refused unread", str(cm.exception))
        port = self.fake_server(struct.pack(">I", ra.MAX_REPLY + 1))
        with self.assertRaises(ConnectionError):
            ra.Client("127.0.0.1", port, self.token, counter=1, timeout=5.0).call("hello")

    def test_a_trickled_reply_meets_the_absolute_deadline(self) -> None:
        port = self.fake_server(struct.pack(">I", 100000), trickle=True)  # every read succeeds, slowly
        c = ra.Client("127.0.0.1", port, self.token, counter=1, timeout=5.0, reply_deadline=0.8)
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            c.call("hello")
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_a_full_collect_chunk_fits_a_reply(self) -> None:
        data = base64.b64encode(b"\xff" * ra.CHUNK).decode("ascii")
        frame = ra.encode_frame(self.token, 2 ** 63, {"ok": True, "offset": 2 ** 40, "data": data, "eof": False})
        self.assertLessEqual(len(frame) - 4, ra.MAX_REPLY)
        self.assertLess(ra.LIST_BYTES + 1024, ra.MAX_REPLY)


class StoreTest(unittest.TestCase):
    """The store is admitted before the agent writes anything (Sol r1 F1): stand-in owner folders and a
    stand-in install, each with a sentinel file that must stay as it was."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.owner = self.root / "Owner" / "Skylanders"
        (self.owner / "Swap Force").mkdir(parents=True)
        self.install = self.root / "Install"
        (self.install / "rom").mkdir(parents=True)
        self.sentinels = {self.owner / "sentinel.txt": b"owner", self.install / "rom" / "sentinel.txt": b"disc",
                          self.install / "settings_template.toml": b"[General]\n"}
        for p, data in self.sentinels.items():
            p.write_bytes(data)
        self.saved_drive_type = ra.drive_type

    def tearDown(self) -> None:
        ra.drive_type = self.saved_drive_type
        for p, data in self.sentinels.items():
            self.assertEqual(p.read_bytes(), data, p)
        self.assertEqual(sorted(os.listdir(self.owner)), ["Swap Force", "sentinel.txt"])
        self.assertEqual(os.listdir(self.owner / "Swap Force"), [])
        self.tmp.cleanup()

    def machine(self, store: Path) -> ra.Machine:
        return ra.Machine(store, self.install, la.AllowList.parse(ALLOW), la.CommandTable.parse(COMMANDS),
                          launcher=fake_launcher, job_factory=FakeJob, owner_roots=[self.owner])

    def refused(self, store: Path, why: str) -> None:
        with self.assertRaises(ra.Refused) as cm:
            self.machine(store)
        self.assertIn(why, str(cm.exception))

    def test_an_owner_folder_store_is_refused(self) -> None:
        self.refused(self.owner, "owner's data folder")                 # is
        self.refused(self.owner / "Swap Force", "owner's data folder")  # lies inside
        self.refused(self.owner.parent, "owner's data folder")          # contains
        junction(self.root / "alias", self.owner)                       # resolves to it
        self.refused(self.root / "alias", "owner's data folder")

    def test_an_install_store_is_refused(self) -> None:
        self.refused(self.install, "the install")
        self.refused(self.install / "rom", "the install")
        (self.root / "Games").mkdir()
        self.refused(self.root, "contains")
        # A rom\ that is a junction elsewhere: a store at its target is the disc too.
        disc = self.root / "Disc"
        disc.mkdir()
        other = self.root / "Install2"
        other.mkdir()
        junction(other / "rom", disc)
        with self.assertRaises(ra.Refused) as cm:
            ra.admit_store(disc, other, [self.owner])
        self.assertIn("the disc", str(cm.exception))

    def test_a_store_on_another_kind_of_drive_is_refused(self) -> None:
        store = self.root / "store"
        store.mkdir()
        ra.drive_type = lambda root: 2  # removable
        self.refused(store, "fixed local drive")
        ra.drive_type = lambda root: 4  # network
        self.refused(store, "fixed local drive")
        self.refused(self.root / "missing", "cannot be resolved")
        self.refused(Path("\\\\server\\share\\store"), "UNC")

    def test_an_ordinary_store_is_admitted_resolved(self) -> None:
        store = self.root / "store"
        store.mkdir()
        self.assertEqual(self.machine(store).store, store)
        junction(self.root / "storelink", store)
        m = self.machine(self.root / "storelink")
        self.assertEqual(m.store, store)  # the junction followed: every later check is against the target
        self.assertTrue(m.runs().is_dir())

    def test_the_command_line_refuses_before_writing(self) -> None:
        allow, cmds = self.root / "allow.toml", self.root / "commands.toml"
        allow.write_text(ALLOW, encoding="utf-8")
        cmds.write_text(COMMANDS, encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            ra.main(["run-local", "x", "--store", str(self.install), "--install", str(self.install),
                     "--manifest-sha256", "0" * 64, "--allow-list", str(allow), "--command-table", str(cmds)])
        self.assertIn("refused", str(cm.exception))


class LockTest(unittest.TestCase):
    def test_stale_lock_is_taken_over_and_a_live_one_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
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
            d = os.path.realpath(d)
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
