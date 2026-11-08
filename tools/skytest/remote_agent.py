"""The remote agent: runs test games on the second PC (R) for the runner (stage 8 item 6, design D21).

    python -I remote_agent.py serve --bind <R's LAN address> --port <port> --subnet <LAN/prefix>
        --store <R's test store> --install <the RC's install folder> --manifest-sha256 <from the RC record>
        --allow-list <launch_allow.toml> --command-table <script_commands.toml>
    python -I remote_agent.py run-local <staged run> --manifest-sha256 <hash> --store ... --install ...
        --allow-list ... --command-table ...

What it does and refuses:
- Listens on one interface address (never every address), closes connections from outside the LAN
  subnet unread, holds at most four connections, at most one of them the authenticated runner's.
- Every request is a frame: a 4-byte big-endian length, an 8-byte counter, an HMAC-SHA256 over the
  counter and the body under the 128-bit token shown on R's console, the body (JSON). A wrong HMAC or a
  counter not above the last closes the connection; the counter moves only on a valid HMAC.
- Deadlines: an accepted connection delivers one complete verified frame within 2 s or is closed with no
  reply; on the authenticated connection a frame completes within 5 s of its first byte; an idle
  authenticated connection closes after 30 s. Closing a connection never ends a run by itself: three
  missed heartbeats (15 s without a valid request) do.
- Bounds: a request at most 64 KiB, at most 200 requests a minute, logs back in 1 MiB chunks, at most
  2 GiB per run; a reply at most 2 MiB (a file listing comes in pages), which the runner's side checks
  before it reads a reply's body, and a reply complete within an absolute deadline.
- It starts only the executables the RC's payload record gives a launch role (`game`,
  `synthetic-peer`), at the fixed install, each hashed against the record before every start; a request
  names a role, never a path. One run plan is one game and at most two synthetic peers. No executable
  or DLL is ever uploaded. Every command line and script goes through launch_args.py; the processes
  start without a shell, suspended, in one kill-on-close job, then resume.
- One run at a time, under a run lock file in the store that `run-local` takes too.

Operations: hello, verify, fixtures, stage, start, stop, status, collect, clean. Nothing else.

Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import selectors
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import launch_args as la  # noqa: E402
import winjob  # noqa: E402

VERSION = 1
MAX_REQUEST = 64 * 1024            # the body and the counter and MAC together
MAX_CONNECTIONS = 4
AUTH_DEADLINE_S = 2.0
FRAME_DEADLINE_S = 5.0
IDLE_DEADLINE_S = 30.0
HEARTBEAT_S = 5.0
MISSED_HEARTBEATS = 3
REQUESTS_PER_MINUTE = 200
CHUNK = 1 << 20
# A reply frame at most (its counter, MAC and body): a collect chunk is CHUNK bytes as base64 (4/3, so
# 1,398,104 bytes) in a small JSON object. The runner refuses a larger declared length unread.
MAX_REPLY = 2 << 20
# A file listing comes in pages of at most LIST_MAX entries and LIST_BYTES of JSON, well inside MAX_REPLY.
LIST_MAX = 4096
LIST_BYTES = 1 << 20
# A reply must be complete within this long of its request (the slowest is `start`, which copies up to
# 64 MiB of fixtures); the socket timeout only bounds each single read.
REPLY_DEADLINE_S = 120.0
COLLECT_LIMIT = 2 << 30
FIXTURE_LIMIT = 64 << 20
MAX_RUN_S = 4 * 3600
CTR_LEN, MAC_LEN = 8, 32
OPERATIONS = ("hello", "verify", "fixtures", "stage", "start", "stop", "status", "collect", "clean")
ROLES = ("game", "synthetic-peer")
MAX_SYNTHETIC = 2
PLAN_KEYS = {"name", "timeout_s", "server", "entries"}
ENTRY_KEYS = {"role", "peer", "args", "script", "hidden"}
FIXTURE_NAME = re.compile(r"(saves|figures)(/[A-Za-z0-9 _().'+-]{1,100})+")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A run plan's name: its run folder adds "-<stamp>-<4 hex>" (21 characters) and must stay a NAME, or
# collect and clean could not name it.
PLAN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")


class Refused(Exception):
    """A request the agent refuses; the reply names the reason."""


# --- the store ------------------------------------------------------------------------------------
# The agent writes only inside its store: manifest.json, run.lock, fixtures\, fixtures-incoming\,
# staged\, runs\ and the collection records. The store is admitted once, before the first write
# (admit_store), and every path the agent opens, replaces or deletes there is checked again right
# before it does (in_store): the store still resolves to the admitted folder, and no component from
# the store down is a junction, link or other reparse point.

DRIVE_FIXED = 3


def drive_type(root: str) -> int:
    """The drive's type by GetDriveTypeW (3: a fixed local disk). A module function so tests can
    stand in another drive."""
    if os.name != "nt":
        return DRIVE_FIXED
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW(root)


def _resolved(p: Path | str, what: str, strict: bool) -> Path:
    """The real path, following junctions and links; an error refuses (fail closed)."""
    try:
        return Path(os.path.realpath(p, strict=strict))
    except (OSError, ValueError) as e:
        raise Refused(f"{what} {p}: cannot be resolved ({e})") from None


def _reparse(p: Path) -> bool:
    """Whether p is a junction, link or other reparse point. A missing p is not; any other error
    refuses (fail closed)."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise Refused(f"{p} cannot be checked ({e})") from None
    return bool(getattr(st, "st_file_attributes", 0) & la.FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(st.st_mode)


def admit_store(store: Path | str, install: Path | str, owner_roots: list[Path] | None = None) -> tuple[Path, Path]:
    """The store and the install the agent works with, both resolved (junctions and links followed).
    Refuses a store spelled as a device, stream or UNC path, one that does not resolve, one that is
    not on a fixed local drive (removable, network, optical), and one that is, lies inside or contains
    an owner data folder (the real ones and `owner_roots`), the install or its rom\\, each compared
    both as spelled and as resolved."""
    text = str(store)
    try:
        la.check_path_text(text, "the store")
    except la.LaunchError as e:
        raise Refused(str(e)) from None
    given = Path(os.path.abspath(text))
    real = _resolved(given, "the store", strict=True)
    if str(real).startswith(("\\\\", "//")):
        raise Refused(f"the store {given} resolves to {real}: a network or device path")
    if not real.is_dir():
        raise Refused(f"the store {real} is not a folder")
    if os.name == "nt":
        kind = drive_type(real.anchor)
        if kind != DRIVE_FIXED:
            raise Refused(f"the store {real} is not on a fixed local drive (drive type {kind})")
    inst_given = Path(os.path.abspath(str(install)))
    inst = _resolved(inst_given, "the install", strict=True)
    if not inst.is_dir():
        raise Refused(f"the install {inst} is not a folder")
    guarded: list[tuple[str, Path]] = []
    for r in [*la.owner_data_roots(), *(owner_roots or [])]:
        guarded += [("the owner's data folder", Path(r)),
                    ("the owner's data folder", _resolved(r, "the owner's data folder", strict=False))]
    guarded += [("the install", inst_given), ("the install", inst), ("the disc", inst_given / "rom"),
                ("the disc", inst / "rom"), ("the disc", _resolved(inst / "rom", "the disc", strict=False))]
    for spelling in (given, real):
        for what, g in guarded:
            if la.within(spelling, g) or la.within(g, spelling):
                raise Refused(f"the store {spelling} is, lies inside or contains {what} {g}")
    return real, inst


def in_store(store: Path, *parts: str) -> Path:
    """A path the agent is about to open, replace or delete: `parts` (plain names, '/' or '\\' between
    them) under the admitted store, refused unless the store still resolves to itself, no component
    from the store down is a reparse point and the whole resolves inside the store."""
    now = _resolved(store, "the store", strict=True)
    if not (la.within(now, store) and la.within(store, now)) or _reparse(store):
        raise Refused(f"the store {store} is no longer the admitted folder (it resolves to {now})")
    p = store
    for part in parts:
        for sub in re.split(r"[\\/]+", str(part)):
            if not sub or sub in (".", "..") or ":" in sub:
                raise Refused(f"{part!r} is not a plain name inside the store")
            p = p / sub
            if _reparse(p):
                raise Refused(f"{p} is a junction, link or other reparse point: the agent writes only "
                              "inside its store")
    if not la.within(_resolved(p, "a store path", strict=False), store):
        raise Refused(f"{p} resolves outside the store {store}")
    return p


def write_in_store(store: Path, rel: str, data: bytes) -> Path:
    """A whole file the agent owns, written to a fresh name and moved over the old one: a hard link at
    the name is replaced, never written through. Checked before the write and again before the move."""
    p = in_store(store, rel)
    tmp = in_store(store, f"{rel}.tmp-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, in_store(store, rel))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def append_in_store(store: Path, rel: str, data: bytes) -> Path:
    """Appends to a file the agent made (a fixture arriving in chunks), its folders made as needed;
    a file with another hard link is refused."""
    p = in_store(store, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p = in_store(store, rel)
    if os.path.lexists(p) and os.lstat(p).st_nlink > 1:
        raise Refused(f"{p} has another hard link: the agent writes only its own files")
    with open(p, "ab") as fh:
        fh.write(data)
    return p


def remove_in_store(store: Path, rel: str) -> None:
    """Deletes a folder or file the agent owns, after the check (shutil.rmtree does not follow the
    junctions inside it)."""
    p = in_store(store, rel)
    if os.path.lexists(p):
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


# --- frames ---------------------------------------------------------------------------------------


def mac(token: bytes, counter: int, body: bytes) -> bytes:
    return hmac.new(token, struct.pack(">Q", counter) + body, hashlib.sha256).digest()


def encode_frame(token: bytes, counter: int, body: dict) -> bytes:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    payload = struct.pack(">Q", counter) + mac(token, counter, data) + data
    return struct.pack(">I", len(payload)) + payload


def decode_payload(token: bytes, payload: bytes) -> tuple[int, bytes] | None:
    """(counter, body) if the MAC verifies, else None."""
    if len(payload) < CTR_LEN + MAC_LEN:
        return None
    counter = struct.unpack(">Q", payload[:CTR_LEN])[0]
    body = payload[CTR_LEN + MAC_LEN:]
    if not hmac.compare_digest(payload[CTR_LEN:CTR_LEN + MAC_LEN], mac(token, counter, body)):
        return None
    return counter, body


# --- the payload record ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class Manifest:
    """The RC's payload record (tools/package_check.py writes it): every file, its destination, size,
    SHA-256 and, for the two executables the agent may start, their launch role."""
    sha256: str
    files: list[dict]
    roles: dict[str, dict]

    @staticmethod
    def parse(text: str) -> "Manifest":
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("files"), list):
            raise Refused("the payload record is not format 1")
        roles: dict[str, dict] = {}
        for f in data["files"]:
            name = f.get("name", "")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(\\[A-Za-z0-9_.-]+)*", name) or ".." in name:
                raise Refused(f"payload record: file name {name!r}")
            if f.get("dest") not in ("{app}", "{tmp}") or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))):
                raise Refused(f"payload record: {name} needs dest {{app}} or {{tmp}} and a SHA-256")
            role = f.get("role")
            if role is not None:
                if role not in ROLES or role in roles or f["dest"] != "{app}" or not name.lower().endswith(".exe"):
                    raise Refused(f"payload record: {name} has role {role!r} (one game and one synthetic-peer "
                                  "executable at most, installed in {app})")
                roles[role] = f
        return Manifest(hashlib.sha256(text.encode("utf-8")).hexdigest(), data["files"], roles)


def verify_install(install: Path, m: Manifest) -> list[str]:
    """Every {app} file of the record, hashed in the install. Returns the mismatches."""
    bad = []
    for f in m.files:
        if f["dest"] != "{app}":
            continue
        p = install / f["name"]
        if not p.is_file():
            bad.append(f"{f['name']}: missing")
        elif p.stat().st_size != f.get("size", -1) or sha256_file(p) != f["sha256"]:
            bad.append(f"{f['name']}: another hash")
    return bad


# --- the run lock ---------------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


class RunLock:
    """One run at a time on the machine: the agent's and `run-local`'s runs take the same file."""

    def __init__(self, store: Path) -> None:
        self.store = store
        self.path = store / "run.lock"
        self.held = False
        self._fd: int | None = None

    def acquire(self, who: str) -> None:
        for _ in range(2):
            try:
                fd = os.open(in_store(self.store, "run.lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding="utf-8").split()[0])
                except (OSError, ValueError, IndexError):
                    pid = -1
                if pid == os.getpid() or pid_alive(pid):
                    raise Refused(f"another run holds the run lock ({self.path})") from None
                try:
                    # Its holder is gone. A live holder keeps the file open, and Windows refuses to
                    # delete an open file: a holder whose pid is not written yet is never taken over.
                    self.path.unlink(missing_ok=True)
                except PermissionError:
                    raise Refused(f"another run holds the run lock ({self.path})") from None
                continue
            os.write(fd, f"{os.getpid()} {who}\n".encode("utf-8"))
            self._fd = fd  # open until release
            self.held = True
            return
        raise Refused("the run lock cannot be taken")

    def busy(self) -> bool:
        """True when someone else holds the lock: a `run-local` run (another RunLock of this process included)."""
        if self.held or not self.path.exists():
            return False
        try:
            pid = int(self.path.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return True  # being written by its new holder
        return pid == os.getpid() or pid_alive(pid)

    def release(self) -> None:
        if self.held:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self.path.unlink(missing_ok=True)
            self.held = False


# --- runs -----------------------------------------------------------------------------------------


@dataclass
class Run:
    name: str
    folder: Path
    job: object
    procs: list
    started: float
    timeout_s: float
    last_heartbeat: float
    state: str = "running"
    sent: int = 0
    store: Path | None = None


def check_plan(plan: object, m: Manifest) -> dict:
    """A run plan: one game and at most two synthetic peers, each named by role, never by path."""
    if not isinstance(plan, dict) or set(plan) - PLAN_KEYS:
        raise Refused(f"a run plan has only {sorted(PLAN_KEYS)}")
    if not isinstance(plan.get("name"), str) or not PLAN_NAME.fullmatch(plan["name"]):
        raise Refused("a run plan needs a name (at most 40 letters, digits, '.', '_' or '-')")
    t = plan.get("timeout_s", 600)
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 0 < t <= MAX_RUN_S:
        raise Refused(f"timeout_s: 1 to {MAX_RUN_S}")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise Refused("a run plan needs entries")
    games = synth = 0
    peers = set()
    for e in entries:
        if not isinstance(e, dict) or set(e) - ENTRY_KEYS:
            extra = sorted(set(e) - ENTRY_KEYS) if isinstance(e, dict) else e
            raise Refused(f"a run plan names roles, never executables or paths ({extra})")
        role = e.get("role")
        if role not in ROLES:
            raise Refused(f"unknown role {role!r}")
        if role not in m.roles:
            raise Refused(f"the payload record gives no file the role {role!r}")
        games += role == "game"
        synth += role == "synthetic-peer"
        peer = e.get("peer")
        if not isinstance(peer, str) or not re.fullmatch(r"[A-Z][A-Za-z0-9]{0,7}", peer) or peer in peers:
            raise Refused(f"peer name {peer!r}")
        peers.add(peer)
        if not isinstance(e.get("args", []), list) or not all(isinstance(a, str) for a in e.get("args", [])):
            raise Refused("args are a list of strings")
        if not isinstance(e.get("script", ""), str):
            raise Refused("script is text")
    if games != 1 or synth > MAX_SYNTHETIC:
        raise Refused(f"one run plan is one game and at most {MAX_SYNTHETIC} synthetic peers "
                      f"({games} games, {synth} synthetic peers)")
    if synth and not isinstance(plan.get("server"), str):
        raise Refused("a plan with synthetic peers names the session address (server)")
    return plan


def default_launcher(argv: list[str], cwd: Path, job: object) -> subprocess.Popen:
    """Without a shell, suspended, into the job, then resumed."""
    out = open(cwd / "stdout.txt", "wb")
    try:
        p = subprocess.Popen(argv, shell=False, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, creationflags=winjob.SUSPENDED_START)
    finally:
        out.close()
    try:
        job.add(p)
        winjob.resume(p)
    except OSError:
        p.kill()
        raise
    return p


@dataclass
class Machine:
    """What a launch on this machine needs: the store, the verified install, the tables."""
    store: Path
    install: Path
    allow: la.AllowList
    commands: la.CommandTable
    launcher: object = default_launcher
    job_factory: object = winjob.Job
    owner_roots: list[Path] | None = None

    def __post_init__(self) -> None:
        # Before anything is written: the store and the install, resolved and admitted (admit_store).
        self.store, self.install = admit_store(self.store, self.install, self.owner_roots)

    def runs(self) -> Path:
        in_store(self.store, "runs").mkdir(exist_ok=True)
        return in_store(self.store, "runs")


def launch_plan(mach: Machine, m: Manifest, plan: dict, lock: RunLock, now: float, who: str) -> Run:
    """Verifies the role files, makes the run folder, builds and checks every line, launches the plan
    into one job. The caller holds nothing; the lock is taken here and released on failure."""
    check_plan(plan, m)
    lock.acquire(who)
    job = None
    try:
        app = {f["name"].lower() for f in m.files if f["dest"] == "{app}"}
        if "settings_template.toml" not in app:
            raise Refused("the payload record installs no settings_template.toml in {app} (the builder copies it)")
        for role in {e["role"] for e in plan["entries"]}:
            f = m.roles[role]
            p = mach.install / f["name"]
            if not p.is_file() or sha256_file(p) != f["sha256"]:
                raise Refused(f"the installed {role} file {f['name']} differs from the payload record")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_folder = la.make_run_folder(mach.runs(), f"{plan['name']}-{stamp}-{secrets.token_hex(2)}",
                                        mach.owner_roots)
        fixtures = in_store(mach.store, "fixtures")
        lines = []
        for e in plan["entries"]:
            folder = la.make_run_folder(run_folder, e["peer"], mach.owner_roots)
            ctx = la.RunContext(kind="rc", store=mach.store, run_folder=folder, rom=mach.install / "rom",
                                template=mach.install / "settings_template.toml", allow=mach.allow,
                                commands=mach.commands, install=mach.install,
                                read_roots=[mach.install / "routes"], owner_roots=mach.owner_roots)
            if e["role"] == "game":
                if not (fixtures / "saves").is_dir() or not (fixtures / "figures").is_dir():
                    raise Refused("no fixtures in the store (the `fixtures` operation, A15)")
                runner = ["--portal_mode=software", "--audio_mute=true"]
                if e.get("hidden"):
                    runner.append("--window_hidden=true")
                args = la.build_game(ctx, list(e.get("args", [])), e.get("script", ""),
                                     fixtures=(fixtures / "saves", fixtures / "figures"), runner_args=runner)
            else:
                # TODO(stage 4): the synthetic peer's own allow list and script rules (online-4.md D4).
                args = la.build_synthetic_peer(ctx, list(e.get("args", [])), plan["server"])
            lines.append(([str(mach.install / m.roles[e["role"]]["name"]), *args], folder))
        job = mach.job_factory()
        procs = [mach.launcher(argv, cwd, job) for argv, cwd in lines]
        return Run(plan["name"], run_folder, job, procs, now, float(plan.get("timeout_s", 600)), now,
                   store=mach.store)
    except la.LaunchError as e:
        if job is not None:
            job.terminate()
            job.close()
        lock.release()
        raise Refused(str(e)) from None
    except BaseException:
        if job is not None:
            job.terminate()
            job.close()
        lock.release()
        raise


def end_run(run: Run, lock: RunLock, state: str) -> None:
    """Ends the job (the peers and their children), writes the exit record, releases the lock; the run
    folder stays for collection. The state changes last, so whoever sees it sees the lock released."""
    if run.state == "running":
        run.job.terminate()
        run.job.close()
        record = json.dumps({"state": state, "codes": [p.poll() for p in run.procs]}).encode("utf-8")
        try:
            if run.store is not None:
                write_in_store(run.store, f"{run.folder.relative_to(run.store).as_posix()}/exit.json", record)
            else:
                (run.folder / "exit.json").write_bytes(record)
        except (OSError, ValueError, Refused):
            pass
        lock.release()
        run.state = state
    lock.release()


def walk_files(folder: Path):
    """Every regular file under `folder` with its size, in a fixed order; a junction, link or other
    reparse point is neither entered nor listed."""
    stack = [folder]
    while stack:
        d = stack.pop()
        with os.scandir(d) as it:
            entries = sorted(it, key=lambda e: e.name)
        subdirs = []
        for e in entries:
            p = Path(e.path)
            if _reparse(p):
                continue
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                subdirs.append(p)
            elif stat.S_ISREG(st.st_mode):
                yield p, st.st_size
        stack.extend(reversed(subdirs))


# --- the agent ------------------------------------------------------------------------------------


@dataclass
class Conn:
    sock: socket.socket
    accepted: float
    buf: bytearray = field(default_factory=bytearray)
    out: bytearray = field(default_factory=bytearray)
    authed: bool = False
    frame_start: float | None = None
    last_request: float = 0.0


class Agent:
    def __init__(self, bind: str, port: int, subnet: str, mach: Machine, manifest_sha256: str,
                 token: bytes | None = None, clock=time.monotonic) -> None:
        addr = ipaddress.ip_address(bind)
        if addr.is_unspecified:
            raise SystemExit("the agent listens on one interface address, never every address")
        self.subnet = ipaddress.ip_network(subnet, strict=False)
        if addr not in self.subnet:
            raise SystemExit(f"{bind} is not in {self.subnet}")
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
            raise SystemExit("--manifest-sha256 is the record's SHA-256 (64 hex digits)")
        self.mach = mach
        self.manifest_sha256 = manifest_sha256
        self.token = token or secrets.token_bytes(16)
        self.clock = clock
        self.lsock = socket.socket(socket.AF_INET6 if addr.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.lsock.bind((bind, port))
        self.lsock.listen(8)
        self.lsock.setblocking(False)
        self.port = self.lsock.getsockname()[1]
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.lsock, selectors.EVENT_READ, None)
        self.conns: dict[socket.socket, Conn] = {}
        self.last_counter = 0
        self.requests: deque[float] = deque()
        self.manifest: Manifest | None = None
        self.verified = False
        self.lock = RunLock(mach.store)
        self.run: Run | None = None
        self.incoming: dict | None = None
        self.closed_unread = 0

    # -- the loop --

    def serve(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        try:
            while not stop.is_set():
                self.step(0.05)
        finally:
            self.shutdown()

    def step(self, timeout: float) -> None:
        for key, events in self.sel.select(timeout):
            if key.data is None:
                self._accept()
            else:
                c = key.data
                if events & selectors.EVENT_READ and c.sock in self.conns:
                    self._read(c)
                if events & selectors.EVENT_WRITE and c.sock in self.conns:
                    self._write(c)
        self._tick(self.clock())

    def shutdown(self) -> None:
        for c in list(self.conns.values()):
            self._close(c)
        if self.run is not None:
            end_run(self.run, self.lock, "ended: agent stopped")
        self.sel.close()
        self.lsock.close()

    def _accept(self) -> None:
        try:
            s, peer = self.lsock.accept()
        except OSError:
            return
        try:
            ok = ipaddress.ip_address(peer[0].split("%")[0]) in self.subnet
        except ValueError:
            ok = False
        if not ok or len(self.conns) >= MAX_CONNECTIONS:
            self.closed_unread += 1
            s.close()
            return
        s.setblocking(False)
        c = Conn(s, self.clock())
        self.conns[s] = c
        self.sel.register(s, selectors.EVENT_READ, c)

    def _close(self, c: Conn) -> None:
        if c.sock in self.conns:
            del self.conns[c.sock]
            try:
                self.sel.unregister(c.sock)
            except (KeyError, ValueError):
                pass
            c.sock.close()

    def _read(self, c: Conn) -> None:
        try:
            data = c.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._close(c)
            return
        if not data:
            self._close(c)
            return
        now = self.clock()
        if c.frame_start is None:
            c.frame_start = now
        c.buf += data
        while c.sock in self.conns and len(c.buf) >= 4:
            n = struct.unpack(">I", c.buf[:4])[0]
            if n < CTR_LEN + MAC_LEN or n > MAX_REQUEST:
                self._close(c)  # over 64 KiB, or no room for the counter and MAC
                return
            if len(c.buf) < 4 + n:
                return
            payload = bytes(c.buf[4:4 + n])
            del c.buf[:4 + n]
            c.frame_start = now if c.buf else None
            got = decode_payload(self.token, payload)
            if got is None or got[0] <= self.last_counter:
                self._close(c)  # a wrong HMAC or a replayed counter: nothing else changes
                return
            counter, body = got
            self.last_counter = counter
            if not c.authed:
                for other in list(self.conns.values()):
                    if other.authed and other is not c:
                        self._close(other)  # the runner reconnected: one authenticated connection
                c.authed = True
            c.last_request = now
            if self.run is not None:
                self.run.last_heartbeat = now
            reply = self._handle(body, now)
            # A request ends when its reply is ready: a slow one (start copies the fixtures) does not
            # count against the idle deadline or the heartbeat.
            now = self.clock()
            c.last_request = now
            if self.run is not None:
                self.run.last_heartbeat = now
            frame = encode_frame(self.token, counter, reply)
            if len(frame) - 4 > MAX_REPLY:  # the runner would refuse it unread
                frame = encode_frame(self.token, counter, {"ok": False, "error": "the reply passes the reply limit"})
            c.out += frame
            self.sel.modify(c.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, c)
            self._write(c)

    def _write(self, c: Conn) -> None:
        try:
            n = c.sock.send(c.out)
        except BlockingIOError:
            return
        except OSError:
            self._close(c)
            return
        del c.out[:n]
        if not c.out and c.sock in self.conns:
            self.sel.modify(c.sock, selectors.EVENT_READ, c)

    def _tick(self, now: float) -> None:
        for c in list(self.conns.values()):
            if not c.authed:
                if now - c.accepted > AUTH_DEADLINE_S:
                    self._close(c)
            elif c.frame_start is not None and now - c.frame_start > FRAME_DEADLINE_S:
                self._close(c)
            elif c.frame_start is None and now - c.last_request > IDLE_DEADLINE_S:
                self._close(c)
        r = self.run
        if r is not None and r.state == "running":
            if now - r.last_heartbeat > HEARTBEAT_S * MISSED_HEARTBEATS:
                end_run(r, self.lock, "ended: heartbeat lost")
            elif now - r.started > r.timeout_s:
                end_run(r, self.lock, "ended: run-time limit")
            elif all(p.poll() is not None for p in r.procs):
                end_run(r, self.lock, "exited")

    # -- requests --

    def _handle(self, body: bytes, now: float) -> dict:
        while self.requests and now - self.requests[0] > 60:
            self.requests.popleft()
        if len(self.requests) >= REQUESTS_PER_MINUTE:
            return {"ok": False, "error": "over 200 requests a minute"}
        self.requests.append(now)
        try:
            req = json.loads(body.decode("utf-8"))
            if not isinstance(req, dict) or req.get("op") not in OPERATIONS:
                raise Refused(f"operations: {', '.join(OPERATIONS)}")
            return {"ok": True, **getattr(self, "op_" + req["op"])(req, now)}
        except Refused as e:
            return {"ok": False, "error": str(e)}
        except la.LaunchError as e:
            return {"ok": False, "error": str(e)}
        except (ValueError, UnicodeDecodeError, KeyError, TypeError) as e:
            return {"ok": False, "error": f"bad request: {e!r}"}
        except OSError as e:  # a file the agent could not read or write: the request fails, the agent stays
            return {"ok": False, "error": f"agent: {e}"}

    def _running(self) -> bool:
        return self.run is not None and self.run.state == "running"

    def op_hello(self, req: dict, now: float) -> dict:
        return {"version": VERSION, "manifest_sha256": self.manifest_sha256, "verified": self.verified}

    def op_verify(self, req: dict, now: float) -> dict:
        if self._running():
            raise Refused("nothing that could change the install is accepted while a peer runs")
        text = req.get("manifest")
        if not isinstance(text, str):
            raise Refused("verify carries the payload record")
        m = Manifest.parse(text)
        if m.sha256 != self.manifest_sha256:
            self.verified = False
            raise Refused("the payload record has another hash than the one the agent was started with")
        bad = verify_install(self.mach.install, m)
        if bad:
            self.verified = False
            raise Refused("the install does not match the payload record: " + "; ".join(bad[:8]))
        write_in_store(self.mach.store, "manifest.json", text.encode("utf-8"))
        self.manifest, self.verified = m, True
        return {"verified": True}

    def op_fixtures(self, req: dict, now: float) -> dict:
        if self._running() or self.lock.busy():
            raise Refused("no fixture changes while a peer runs")
        act = req.get("action")
        store = self.mach.store
        if act == "begin":
            files = req.get("files")
            if not isinstance(files, list) or not files:
                raise Refused("fixtures begin lists files")
            total = 0
            names = set()
            for f in files:
                name, size, digest = f.get("path"), f.get("size"), f.get("sha256")
                if not isinstance(name, str) or not FIXTURE_NAME.fullmatch(name) or ".." in name or name in names:
                    raise Refused(f"fixture name {name!r} is outside the fixture layout (saves/..., figures/...)")
                if not isinstance(size, int) or size < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
                    raise Refused(f"fixture {name}: size and SHA-256")
                names.add(name)
                total += size
            if total > FIXTURE_LIMIT:
                raise Refused("fixtures are at most 64 MiB in all")
            remove_in_store(store, "fixtures-incoming")
            in_store(store, "fixtures-incoming").mkdir()
            self.incoming = {"files": files, "got": [0] * len(files)}
            return {"files": len(files)}
        if self.incoming is None:
            raise Refused("fixtures begin first")
        if act == "chunk":
            i, off = req.get("index"), req.get("offset")
            files = self.incoming["files"]
            if not isinstance(i, int) or not 0 <= i < len(files) or off != self.incoming["got"][i]:
                raise Refused("chunk index and offset")
            data = base64.b64decode(req.get("data", ""), validate=True)
            if off == 0 and data[:2] == b"MZ":
                self.incoming = None
                remove_in_store(store, "fixtures-incoming")
                raise Refused(f"fixture {files[i]['path']} starts with an executable header")
            if off + len(data) > files[i]["size"]:
                raise Refused("chunk beyond the declared size")
            append_in_store(store, f"fixtures-incoming/{files[i]['path']}", data)
            self.incoming["got"][i] += len(data)
            return {"got": self.incoming["got"][i]}
        if act == "end":
            for f, got in zip(self.incoming["files"], self.incoming["got"]):
                rel = f"fixtures-incoming/{f['path']}"
                if f["size"] == 0 and not os.path.lexists(in_store(store, rel)):
                    append_in_store(store, rel, b"")
                p = in_store(store, rel)
                if got != f["size"] or sha256_file(p) != f["sha256"]:
                    raise Refused(f"fixture {f['path']} does not match its size and SHA-256")
                with open(p, "rb") as fh:
                    mz = fh.read(2) == b"MZ"  # also when the first chunk carried only "M"
                if mz:
                    self.incoming = None
                    remove_in_store(store, "fixtures-incoming")
                    raise Refused(f"fixture {f['path']} starts with an executable header")
            remove_in_store(store, "fixtures")
            for sub in ("saves", "figures"):
                in_store(store, "fixtures-incoming", sub).mkdir(exist_ok=True)
            in_store(store, "fixtures-incoming").rename(in_store(store, "fixtures"))
            self.incoming = None
            return {"stored": True}
        raise Refused("fixtures action: begin, chunk or end")

    def _need_verified(self) -> Manifest:
        if not self.verified or self.manifest is None:
            raise Refused("verify the install first")
        return self.manifest

    def op_stage(self, req: dict, now: float) -> dict:
        m = self._need_verified()
        plan = check_plan(req.get("plan"), m)
        for e in plan["entries"]:  # the scripts' grammar and table now; their paths again at run-local
            if e["role"] == "game":
                try:
                    la.parse_script(e.get("script", ""), self.mach.commands)
                except la.LaunchError as err:
                    raise Refused(str(err)) from None
        in_store(self.mach.store, "staged").mkdir(exist_ok=True)
        write_in_store(self.mach.store, f"staged/{plan['name']}.json", json.dumps(plan).encode("utf-8"))
        return {"staged": plan["name"]}

    def op_start(self, req: dict, now: float) -> dict:
        m = self._need_verified()
        if self._running():
            raise Refused("a run is going: one at a time")
        bad = verify_install(self.mach.install, m)
        if bad:
            self.verified = False
            raise Refused("the install changed since verify: " + "; ".join(bad[:8]))
        try:
            self.run = launch_plan(self.mach, m, req.get("plan"), self.lock, now, "agent")
        except la.LaunchError as e:
            raise Refused(str(e)) from None
        return {"run": self.run.folder.name}

    def op_stop(self, req: dict, now: float) -> dict:
        if not self._running():
            raise Refused("no run is going")
        end_run(self.run, self.lock, "stopped")
        return {"state": self.run.state}

    def op_status(self, req: dict, now: float) -> dict:
        r = self.run
        if r is None:
            return {"run": None, "verified": self.verified}
        return {"run": r.folder.name, "state": r.state, "alive": sum(p.poll() is None for p in r.procs),
                "verified": self.verified}

    def _run_folder(self, name: object) -> Path:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Refused("run name")
        p = self.mach.runs() / name
        if not p.is_dir():
            raise Refused(f"no run {name}")
        return la.canonical(str(p), self.mach.store, "run folder", must_exist=True)

    def op_collect(self, req: dict, now: float) -> dict:
        folder = self._run_folder(req.get("run"))
        if req.get("path") is None:
            return self._listing(folder, req.get("start", 0))
        try:
            p = la.contained(la.RunContext("rc", self.mach.store, folder, self.mach.install / "rom", Path(),
                                           self.mach.allow, self.mach.commands, self.mach.install,
                                           owner_roots=self.mach.owner_roots),
                             str(req["path"]), "collect", folder, folder, must_exist=True)
        except la.LaunchError as e:
            raise Refused(str(e)) from None
        off = req.get("offset", 0)
        if not isinstance(off, int) or off < 0:
            raise Refused("offset")
        sent = self.run.sent if self.run is not None and self.run.folder == folder else 0
        if sent >= COLLECT_LIMIT:
            raise Refused("2 GiB of this run's files are sent already")
        with open(in_store(self.mach.store, p.relative_to(self.mach.store).as_posix()), "rb") as f:
            f.seek(off)
            data = f.read(min(CHUNK, COLLECT_LIMIT - sent))
        if self.run is not None and self.run.folder == folder:
            self.run.sent += len(data)
        return {"offset": off, "data": base64.b64encode(data).decode("ascii"), "eof": len(data) < CHUNK}

    def _listing(self, folder: Path, start: object) -> dict:
        """One page of the run folder's files from entry `start` on, in a fixed order: at most LIST_MAX
        entries and LIST_BYTES of JSON; `next` is the first entry of the next page, or None."""
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise Refused("start")
        files: list[dict] = []
        used = 0
        for i, (p, size) in enumerate(walk_files(folder)):
            if i < start:
                continue
            entry = {"path": p.relative_to(folder).as_posix(), "size": size}
            cost = len(json.dumps(entry, separators=(",", ":"))) + 1
            if files and (len(files) >= LIST_MAX or used + cost > LIST_BYTES):
                return {"files": files, "next": i}
            files.append(entry)
            used += cost
        return {"files": files, "next": None}

    def op_clean(self, req: dict, now: float) -> dict:
        if self.lock.busy():
            raise Refused("a run-local run holds the run lock: clean after it ends")
        removed = []
        runs = self.mach.runs()
        active = self.run.folder if self._running() else None
        for p in sorted(runs.iterdir()):
            if req.get("run") not in (None, p.name):
                continue
            if active is not None and la.within(p, active):
                continue
            if not la.within(p, runs) or la.is_reparse_point(p):
                raise Refused(f"refusing to delete {p}: not a plain run folder of the store")
            remove_in_store(self.mach.store, f"runs/{p.name}")
            removed.append(p.name)
        if req.get("fixtures"):
            if self._running():
                raise Refused("no fixture changes while a peer runs")
            for sub in ("fixtures", "fixtures-incoming", "staged"):
                remove_in_store(self.mach.store, sub)
        return {"removed": removed}


# --- the runner's side ----------------------------------------------------------------------------


class Client:
    """The runner's connection to an agent."""

    def __init__(self, host: str, port: int, token: bytes, counter: int | None = None, timeout: float = 10.0,
                 reply_deadline: float = REPLY_DEADLINE_S) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.token = token
        self.counter = counter if counter is not None else time.time_ns()
        self.timeout = timeout
        self.reply_deadline = reply_deadline

    def call(self, op: str, **kw) -> dict:
        """One request and its reply. The reply's declared length is checked against MAX_REPLY before
        any of its body is read, and the whole reply must arrive within reply_deadline; a reply that
        fails either closes the connection (its framing is lost)."""
        self.counter += 1
        self.sock.sendall(encode_frame(self.token, self.counter, {"op": op, **kw}))
        end = time.monotonic() + self.reply_deadline
        try:
            n = struct.unpack(">I", self._exact(4, end))[0]
            if n < CTR_LEN + MAC_LEN or n > MAX_REPLY:
                raise ConnectionError(f"the agent's reply declares {n} bytes (a reply is {CTR_LEN + MAC_LEN} "
                                      f"to {MAX_REPLY}): refused unread")
            got = decode_payload(self.token, self._exact(n, end))
        except OSError:
            self.sock.close()
            raise
        if got is None or got[0] != self.counter:
            raise ConnectionError("the agent's reply does not verify")
        return json.loads(got[1].decode("utf-8"))

    def _exact(self, n: int, end: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"the agent's reply was not complete within {self.reply_deadline:g} s")
            self.sock.settimeout(min(self.timeout, left))
            b = self.sock.recv(min(n - len(buf), 1 << 16))
            if not b:
                raise ConnectionError("the agent closed the connection")
            buf += b
        return bytes(buf)

    def close(self) -> None:
        self.sock.close()


# --- run-local ------------------------------------------------------------------------------------


def run_local(mach: Machine, staged: str, manifest_sha256: str, poll_s: float = 0.5,
              stop: threading.Event | None = None) -> dict:
    """A staged run started on R itself: the same checks as `start` (the record's hash, the install's
    payload, the builder, the script check, the run plan's limits), the run lock, one kill-on-close job,
    the run-time limit; Ctrl+C or a closed console ends the job. Returns the exit record."""
    if not NAME.fullmatch(staged):
        raise Refused("staged run name")
    text = in_store(mach.store, "manifest.json").read_text(encoding="utf-8")
    m = Manifest.parse(text)
    if m.sha256 != manifest_sha256:
        raise Refused("the stored payload record has another hash than the one typed")
    bad = verify_install(mach.install, m)
    if bad:
        raise Refused("the install does not match the payload record: " + "; ".join(bad[:8]))
    plan = json.loads(in_store(mach.store, "staged", f"{staged}.json").read_text(encoding="utf-8"))
    lock = RunLock(mach.store)
    run = launch_plan(mach, m, plan, lock, time.monotonic(), "run-local")
    state = "exited"
    try:
        while True:
            if stop is not None and stop.is_set():
                state = "stopped"
                break
            if all(p.poll() is not None for p in run.procs):
                break
            if time.monotonic() - run.started > run.timeout_s:
                state = "ended: run-time limit"
                break
            time.sleep(poll_s)
    except KeyboardInterrupt:
        state = "stopped (Ctrl+C)"
    end_run(run, lock, state)
    return {"run": run.folder.name, "state": run.state}


def machine_from(a: argparse.Namespace) -> Machine:
    """The store as typed goes to admit_store unresolved: it is checked as spelled and as resolved
    before the agent writes anything."""
    allow, commands = la.AllowList.load(Path(a.allow_list)), la.CommandTable.load(Path(a.command_table))
    try:
        return Machine(Path(a.store), Path(a.install), allow, commands)
    except Refused as e:
        raise SystemExit(f"refused: {e}") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "run-local"):
        p = sub.add_parser(name)
        p.add_argument("--store", required=True)
        p.add_argument("--install", required=True)
        p.add_argument("--manifest-sha256", required=True)
        p.add_argument("--allow-list", required=True)
        p.add_argument("--command-table", required=True)
        if name == "serve":
            p.add_argument("--bind", required=True)
            p.add_argument("--port", type=int, required=True)
            p.add_argument("--subnet", required=True)
        else:
            p.add_argument("staged")
    a = ap.parse_args(argv)
    mach = machine_from(a)
    if a.cmd == "run-local":
        try:
            print(json.dumps(run_local(mach, a.staged, a.manifest_sha256)))
        except Refused as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        return 0
    agent = Agent(a.bind, a.port, a.subnet, mach, a.manifest_sha256)
    print(f"remote agent on {a.bind}:{agent.port}; token (type it into the runner): {agent.token.hex()}")
    try:
        agent.serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
