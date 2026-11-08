"""The installer's temporary payload as setup really writes it (stage 8 item 8b, design 3.8).

Inno Setup puts its `{tmp}` helpers (the disc check, extract-xiso, the runtime DLLs, the settings
template) into a `%TEMP%\\is-XXXXX.tmp\\` folder, runs them and deletes them when setup ends, so the
installed files alone never show them. This watcher sees them while they exist:

    python -I tools/tmp_payload_watch.py --temp <the sandbox user's TEMP> --out <listing.json>
        [--allow NAME ...] [--mode hold|copy] [--copy-dir DIR] [--settle S] [--seconds S]
        [-- <installer> <installer arguments ...>]

- It watches `--temp` (ReadDirectoryChangesW, the whole subtree) and takes every file that appears
  under an `is-*.tmp` folder, named relative to that folder (`_isetup\\_setup64.tmp`,
  `swapforcerecomp_xexcheck.exe`).
- `--mode hold` (the default): it opens each file the moment setup's own handle allows it
  (CreateFileW, read access, read and write sharing, never delete sharing, so setup cannot delete it
  while the watcher holds it), hashes it then and again at the end, and keeps the handle until setup
  ends. `--mode copy` (the fallback if setup cannot go on with a held file): it copies each file into
  `--copy-dir` the moment it can read it, releases it at once and hashes the copy (again on each later
  change).
- With a command after `--` it starts the installer itself (no shell), watches until it exits and
  `--settle` seconds more; without one it watches for `--seconds` (or until Ctrl+C).
- A file seen but never read is listed with no hash ("seen, not hashed"), and lost notifications (the
  buffer overflowed) add a `<events lost ...>` entry with no hash: a race can only fail the comparison
  (`package_check.py compare-tmp`), never pass it.

The listing (`--out`, JSON) is what `package_check.py compare-tmp --seen` reads: `seen` ({name: {size,
sha256}} or null), `allowed_extra` (the `--allow` names: Inno Setup's own helpers and the files the
[Code] writes, by exact name, with their hash when seen), plus `folders`, `changed`, `errors` and the
installer's exit code for the report. Exit code 0 only when the installer (if started) exited 0 and the
watcher found no error of its own.

Windows only for the watch (ctypes, kernel32); the notification parser and the path rules are plain
Python. Standard library only; `python -I`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# FILE_NOTIFY_INFORMATION actions.
FILE_ACTION_ADDED = 1
FILE_ACTION_REMOVED = 2
FILE_ACTION_MODIFIED = 3
FILE_ACTION_RENAMED_OLD_NAME = 4
FILE_ACTION_RENAMED_NEW_NAME = 5
APPEARS = (FILE_ACTION_ADDED, FILE_ACTION_MODIFIED, FILE_ACTION_RENAMED_NEW_NAME)
GONE = (FILE_ACTION_REMOVED, FILE_ACTION_RENAMED_OLD_NAME)

# Inno Setup's temporary folders: is- and five letters or digits, .tmp (the setup loader's own folder,
# which holds the setup engine `<setup name>.tmp`, and the engine's `{tmp}`).
SETUP_FOLDER = re.compile(r"is-[A-Za-z0-9]{5}\.tmp", re.IGNORECASE)
LOST = "<events lost: the notification buffer overflowed>"
POLL_S = 0.005
BUFFER = 64 * 1024


class WatchError(Exception):
    pass


# --- pure parts ------------------------------------------------------------------------------------


def parse_notify(buf: bytes) -> list[tuple[int, str]]:
    """The FILE_NOTIFY_INFORMATION records of one ReadDirectoryChangesW result: (action, name relative
    to the watched folder). Refuses a buffer whose offsets or lengths run outside it."""
    out: list[tuple[int, str]] = []
    off = 0
    while True:
        if off + 12 > len(buf):
            raise WatchError(f"notification record at {off} runs past the buffer ({len(buf)} bytes)")
        nxt, action, n = struct.unpack_from("<III", buf, off)
        if n % 2 or off + 12 + n > len(buf):
            raise WatchError(f"notification name of {n} bytes at {off} runs past the buffer")
        out.append((action, buf[off + 12:off + 12 + n].decode("utf-16-le", errors="surrogatepass")))
        if nxt == 0:
            return out
        if nxt % 4 or nxt < 12 + n or off + nxt >= len(buf):
            raise WatchError(f"notification record at {off} points to {off + nxt} (not past its name, or out of the buffer)")
        off += nxt


def setup_name(rel: str) -> tuple[str, str] | None:
    """(the is-*.tmp folder, the file's name inside it with backslashes) for a path relative to the
    watched folder, or None when it is not inside a setup folder (or is the folder itself)."""
    parts = [p for p in re.split(r"[\\/]+", rel) if p]
    if len(parts) < 2 or not SETUP_FOLDER.fullmatch(parts[0]):
        return None
    if any(p in (".", "..") for p in parts):
        return None
    return parts[0], "\\".join(parts[1:])


@dataclass
class Seen:
    folder: str
    name: str
    path: Path
    first: tuple[int, str] | None = None   # (size, sha256) at the first read
    last: tuple[int, str] | None = None    # at the latest read
    gone: bool = False
    handle: int | None = None              # hold mode: the handle that keeps setup from deleting it


@dataclass
class Listing:
    seen: dict[str, Seen] = field(default_factory=dict)
    folders: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lost: bool = False

    def note(self, temp: Path, action: int, rel: str) -> Seen | None:
        """One notification: a file that appears under a setup folder is entered (and returned, to be
        read); a removal marks it gone. A name seen in two setup folders is an error (the second is not
        taken)."""
        where = setup_name(rel)
        if where is None:
            first = [p for p in re.split(r"[\\/]+", rel) if p]
            if len(first) == 1 and SETUP_FOLDER.fullmatch(first[0]) and first[0] not in self.folders:
                if action in APPEARS:
                    self.folders.append(first[0])
            return None
        folder, name = where
        if folder not in self.folders:
            self.folders.append(folder)
        key = name.lower()
        s = self.seen.get(key)
        if s is not None and s.folder != folder:
            msg = f"{name}: in two setup folders ({s.folder}, {folder}); the second is not compared"
            if msg not in self.errors:
                self.errors.append(msg)
            return None
        if action in GONE:
            if s is not None:
                s.gone = True
            return None
        if action not in APPEARS:
            return None
        if s is None:
            s = Seen(folder, name, temp / folder / Path(*name.split("\\")))
            self.seen[key] = s
        s.gone = False
        return s

    def result(self, allow: list[str], mode: str, temp: Path, installer_exit: int | None) -> dict:
        allowed = {a.lower() for a in allow}
        seen: dict[str, dict | None] = {}
        changed: dict[str, list[dict]] = {}
        errors = list(self.errors)
        for s in self.seen.values():
            seen[s.name] = None if s.last is None else {"size": s.last[0], "sha256": s.last[1]}
            if s.first is not None and s.last is not None and s.first != s.last:
                changed[s.name] = [{"size": s.first[0], "sha256": s.first[1]}, {"size": s.last[0], "sha256": s.last[1]}]
                if s.name.lower() not in allowed:
                    errors.append(f"{s.name}: changed while the watcher had it (the bytes setup ran may differ "
                                  "from the ones compared)")
            if s.last is None:
                errors.append(f"{s.name}: seen, not hashed")
        if self.lost:
            seen[LOST] = None
            errors.append(LOST)
        extra = {}
        for a in allow:
            s = self.seen.get(a.lower())
            extra[a] = s.last[1] if s is not None and s.last is not None else None
        return {"format": 1, "temp": str(temp), "mode": mode, "folders": self.folders, "seen": seen,
                "allowed_extra": extra, "changed": changed, "errors": errors, "installer_exit": installer_exit}


# --- the Windows side ------------------------------------------------------------------------------

GENERIC_READ = 0x80000000
FILE_LIST_DIRECTORY = 0x1
FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE = 0x1, 0x2, 0x4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
NOTIFY_FILTER = 0x1 | 0x2 | 0x8 | 0x10 | 0x40  # file name, dir name, size, last write, creation
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
# Why an open can fail while the file is still there: setup's own handle denies sharing (32), or the
# file is being deleted (5, a delete-pending file). Gone: 2, 3.
RETRY_ERRORS = (5, 32, 33)
GONE_ERRORS = (2, 3)


def _api():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                          ctypes.POINTER(OVERLAPPED), ctypes.c_void_p]
    k32.ReadDirectoryChangesW.restype = wintypes.BOOL
    k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                        ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    k32.GetOverlappedResult.restype = wintypes.BOOL
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    k32.CancelIoEx.restype = wintypes.BOOL
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                             ctypes.c_void_p]
    k32.ReadFile.restype = wintypes.BOOL
    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
                                     wintypes.DWORD]
    k32.SetFilePointerEx.restype = wintypes.BOOL
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.ResetEvent.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, k32, OVERLAPPED


class Watcher:
    """ReadDirectoryChangesW on one folder (overlapped, so a poll tick can retry the files setup's
    handle still holds); every file under a setup folder is read as soon as it can be."""

    def __init__(self, temp: Path, mode: str = "hold", copy_dir: Path | None = None) -> None:
        if os.name != "nt":
            raise WatchError("the watcher runs on Windows (ReadDirectoryChangesW)")
        if mode not in ("hold", "copy") or (mode == "copy") != (copy_dir is not None):
            raise WatchError("--mode hold, or --mode copy with --copy-dir")
        self.ctypes, self.wt, self.k32, OVERLAPPED = _api()
        self.temp = Path(os.path.realpath(temp))
        self.mode, self.copy_dir = mode, copy_dir
        self.listing = Listing()
        self.invalid = self.wt.HANDLE(-1).value
        self.dir = self.k32.CreateFileW(str(self.temp), FILE_LIST_DIRECTORY,
                                        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, None, OPEN_EXISTING,
                                        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED, None)
        if self.dir in (None, 0, self.invalid):
            raise WatchError(f"cannot watch {self.temp} (error {self.ctypes.get_last_error()})")
        self.buf = (self.ctypes.c_uint32 * (BUFFER // 4))()
        self.ov = OVERLAPPED()
        self.ov.hEvent = self.k32.CreateEventW(None, True, False, None)
        self.pending: dict[str, Seen] = {}
        self.armed = False
        self._arm()

    # -- the directory --

    def _arm(self) -> None:
        got = self.wt.DWORD(0)
        self.ov.Internal = self.ov.InternalHigh = self.ov.Offset = self.ov.OffsetHigh = 0
        self.k32.ResetEvent(self.ov.hEvent)
        ok = self.k32.ReadDirectoryChangesW(self.dir, self.buf, BUFFER, True, NOTIFY_FILTER, self.ctypes.byref(got),
                                            self.ctypes.byref(self.ov), None)
        if not ok and self.ctypes.get_last_error() != ERROR_IO_PENDING:
            raise WatchError(f"ReadDirectoryChangesW failed (error {self.ctypes.get_last_error()})")
        self.armed = True

    def step(self, timeout_s: float = POLL_S) -> None:
        """Waits up to `timeout_s` for notifications, takes them, then retries every pending file."""
        r = self.k32.WaitForSingleObject(self.ov.hEvent, max(0, int(timeout_s * 1000)))
        if r == WAIT_OBJECT_0:
            got = self.wt.DWORD(0)
            if self.k32.GetOverlappedResult(self.dir, self.ctypes.byref(self.ov), self.ctypes.byref(got), False):
                n = got.value
                self.armed = False
                if n == 0:
                    # The system's buffer overflowed: what changed meanwhile is unknown. A rescan finds
                    # what is still there; what came and went is lost, which the listing says.
                    self.listing.lost = True
                    self.rescan()
                else:
                    data = self.ctypes.string_at(self.buf, n)
                    for action, rel in parse_notify(data):
                        s = self.listing.note(self.temp, action, rel)
                        if s is not None and s.handle is None:  # a held file cannot change or go
                            self.pending[s.name.lower()] = s
                self._arm()
            else:
                raise WatchError(f"the watch ended (error {self.ctypes.get_last_error()})")
        self.retry()

    def rescan(self) -> None:
        for entry in os.scandir(self.temp):
            if entry.is_dir(follow_symlinks=False) and SETUP_FOLDER.fullmatch(entry.name):
                for root, _, files in os.walk(entry.path):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), self.temp)
                        s = self.listing.note(self.temp, FILE_ACTION_ADDED, rel)
                        if s is not None and s.handle is None and s.last is None:
                            self.pending[s.name.lower()] = s

    # -- the files --

    def retry(self) -> None:
        for key, s in list(self.pending.items()):
            if os.path.isdir(s.path):  # a folder inside a setup folder (_isetup): not a file of the payload
                del self.pending[key]
                if s.last is None:
                    self.listing.seen.pop(key, None)
                continue
            state = self.take(s)
            if state != "retry":
                del self.pending[key]

    def _open(self, path: Path) -> tuple[int | None, int]:
        h = self.k32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                                 FILE_ATTRIBUTE_NORMAL, None)
        if h in (None, 0, self.invalid):
            return None, self.ctypes.get_last_error()
        return h, 0

    def _read_all(self, h: int) -> bytes:
        self.k32.SetFilePointerEx(h, 0, None, 0)
        out = bytearray()
        chunk = self.ctypes.create_string_buffer(1 << 20)
        got = self.wt.DWORD(0)
        while True:
            if not self.k32.ReadFile(h, chunk, 1 << 20, self.ctypes.byref(got), None):
                raise WatchError(f"ReadFile failed (error {self.ctypes.get_last_error()})")
            if got.value == 0:
                return bytes(out)
            out += chunk.raw[:got.value]

    def take(self, s: Seen) -> str:
        """Reads one file: `held` (hold mode: hashed, the handle kept), `copied`, `gone` or `retry`."""
        h, err = self._open(s.path)
        if h is None:
            if err in GONE_ERRORS:
                s.gone = True
                return "gone"
            return "retry"
        try:
            data = self._read_all(h)
        except WatchError:
            self.k32.CloseHandle(h)
            return "retry"
        digest = (len(data), hashlib.sha256(data).hexdigest())
        if s.first is None:
            s.first = digest
        s.last = digest
        if self.mode == "hold":
            s.handle = h
            return "held"
        self.k32.CloseHandle(h)
        dest = self.copy_dir / s.folder / Path(*s.name.split("\\"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return "copied"

    def finish(self) -> None:
        """Setup has ended: the files still pending get a last try, every held file is hashed again (its
        bytes at the end) and released."""
        self.retry()
        for s in self.listing.seen.values():
            if s.handle is not None:
                try:
                    data = self._read_all(s.handle)
                    s.last = (len(data), hashlib.sha256(data).hexdigest())
                except WatchError as e:
                    self.listing.errors.append(f"{s.name}: the last read failed ({e})")
                self.k32.CloseHandle(s.handle)
                s.handle = None
            elif self.mode == "copy" and s.last is not None and self.copy_dir is not None:
                copy = self.copy_dir / s.folder / Path(*s.name.split("\\"))
                if copy.is_file():
                    data = copy.read_bytes()
                    s.last = (len(data), hashlib.sha256(data).hexdigest())

    def close(self) -> None:
        if self.armed:
            self.k32.CancelIoEx(self.dir, self.ctypes.byref(self.ov))
            got = self.wt.DWORD(0)
            self.k32.GetOverlappedResult(self.dir, self.ctypes.byref(self.ov), self.ctypes.byref(got), True)
            self.armed = False
        for s in self.listing.seen.values():
            if s.handle is not None:
                self.k32.CloseHandle(s.handle)
                s.handle = None
        self.k32.CloseHandle(self.ov.hEvent)
        self.k32.CloseHandle(self.dir)


def watch(temp: Path, out: Path, allow: list[str], mode: str = "hold", copy_dir: Path | None = None,
          command: list[str] | None = None, settle_s: float = 2.0, seconds: float | None = None) -> dict:
    """Watches while the installer runs (or for `seconds`), then writes and returns the listing."""
    w = Watcher(temp, mode, copy_dir)
    installer_exit = None
    try:
        proc = subprocess.Popen(command, shell=False) if command else None
        end = None if proc is not None else (time.monotonic() + seconds if seconds is not None else None)
        try:
            while True:
                w.step()
                if proc is not None and proc.poll() is not None:
                    if end is None:
                        installer_exit = proc.returncode
                        end = time.monotonic() + settle_s
                if end is not None and time.monotonic() >= end:
                    break
        except KeyboardInterrupt:
            pass
        w.finish()
    finally:
        w.close()
    result = w.listing.result(allow, mode, w.temp, installer_exit)
    out.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--temp", required=True, help="the folder setup makes its is-*.tmp folders in (the user's TEMP)")
    ap.add_argument("--out", required=True, help="the listing (JSON) package_check.py compare-tmp reads")
    ap.add_argument("--allow", action="append", default=[], help="an exact name besides the record's (Inno "
                    "Setup's own helpers, the files the [Code] writes), relative to its is-*.tmp folder")
    ap.add_argument("--mode", choices=("hold", "copy"), default="hold")
    ap.add_argument("--copy-dir", default=None, help="--mode copy: where each file is copied")
    ap.add_argument("--settle", type=float, default=2.0, help="seconds to keep watching after the installer exits")
    ap.add_argument("--seconds", type=float, default=None, help="without an installer command: how long to watch")
    a = ap.parse_args(argv)
    try:
        r = watch(Path(a.temp), Path(a.out), a.allow, a.mode, Path(a.copy_dir) if a.copy_dir else None,
                  command or None, a.settle, a.seconds)
    except WatchError as e:
        print(f"tmp payload watch: {e}", file=sys.stderr)
        return 2
    for e in r["errors"]:
        print(f"tmp payload watch: {e}", file=sys.stderr)
    hashed = sum(v is not None for v in r["seen"].values())
    print(f"tmp payload watch: {len(r['seen'])} files in {len(r['folders'])} setup folder(s), {hashed} hashed; "
          f"listing {a.out}" + (f"; installer exit {r['installer_exit']}" if command else ""))
    return 0 if not r["errors"] and (not command or r["installer_exit"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
