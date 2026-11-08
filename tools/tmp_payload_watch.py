"""The installer's temporary payload as setup really writes it (stage 8 item 8b, design 3.8).

Inno Setup puts its `{tmp}` helpers (the disc check, extract-xiso, the runtime DLLs, the settings
template) into a `%TEMP%\\is-XXXXX.tmp\\` folder, runs them and deletes them when setup ends, so the
installed files alone never show them. This watcher sees them while they exist:

    python -I tools/tmp_payload_watch.py --temp <the sandbox user's TEMP> --out <listing.json>
        [--allow NAME ...] [--setup-exe <the installer being run>]
        [--mode hold|copy] [--copy-dir DIR | --copy-to DIR] [--settle S] [--seconds S]
        [--until-exists <stop file made after setup exits>]
        [-- <installer> <installer arguments ...>]

- It watches `--temp` (ReadDirectoryChangesW, the whole subtree) and takes every file that appears
  under an `is-*.tmp` folder, named relative to that folder (`_isetup\\_setup64.tmp`,
  `swapforcerecomp_xexcheck.exe`).
- Start it before setup. An `is-*.tmp` folder already there when it starts is ignored (listed in
  `ignored_folders`): the files it held during an earlier install in the same sandbox stay behind,
  and they must not stand in for this install's files. A name met in a second setup folder is listed
  with that folder in front (`is-XXXXX.tmp\\name`), so compare-tmp fails it instead of the watcher
  dropping it.
- `--mode hold` (the default): it opens each file the moment setup's own handle allows it
  (CreateFileW, read access, read and write sharing, never delete sharing, so setup cannot delete it
  while the watcher holds it), hashes it then and again at the end, and keeps the handle until setup
  ends. `--mode copy` (the fallback if setup cannot go on with a held file; `--copy-to DIR` is short
  for `--mode copy --copy-dir DIR`): it copies each file into the copy folder the moment it can read
  it, releases it at once and hashes the copy (again on each later change).
- With a command after `--` it starts the installer itself (no shell), watches until it exits and
  `--settle` seconds more; without one it watches until `--until-exists` names a file that exists
  (the recipe makes it once setup's process has exited; the held files keep setup's folder, so its
  going away cannot be the signal) or `--seconds` pass (1800 by default).
- A file seen but never read is listed with no hash ("seen, not hashed"), and lost notifications (the
  buffer overflowed) add a `<events lost ...>` entry with no hash: a race can only fail the comparison
  (`package_check.py compare-tmp`), never pass it.

The listing (`--out`, JSON) is what `package_check.py compare-tmp --seen` reads: `seen` ({name: {size,
sha256}} or null) and `allowed_extra` (the files that are not payload but may appear, each seen one
with its hash: Inno Setup's own `_isetup\\` helpers and the [Code]'s xexcheck-output.txt by default,
the `--allow` names, and with --setup-exe the setup program Inno's loader extracts as `<installer
name>.tmp` into an `is-*.tmp` folder of its own), plus `folders`, `ignored_folders`, `changed`,
`errors` and the installer's exit code for the report. Exit code 0 only when the installer (if
started) exited 0 and the watcher found no error of its own.

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

# Inno Setup's temporary folders, is-*.tmp (the setup loader's own folder, which holds the setup
# engine `<setup name>.tmp`, and the engine's `{tmp}`; Inno names them is- and five letters or digits,
# but the rule takes any name so another Inno Setup version cannot hide a folder).
SETUP_FOLDER = re.compile(r"is-[^\\/]+\.tmp", re.IGNORECASE)
LOST = "<events lost: the notification buffer overflowed>"
POLL_S = 0.005
BUFFER = 64 * 1024
DEFAULT_SECONDS = 1800.0

# Inno Setup's own helpers and the [Code]'s output, by exact name (not payload: they come from the
# Inno Setup version the RC record names, and from the installer's own code).
ALLOWED_EXTRA = ("_isetup\\_setup64.tmp", "_isetup\\_isdecmp.dll", "_isetup\\_iscrypt.dll",
                 "_isetup\\_shfoldr.dll", "_isetup\\_RegDLL.tmp", "xexcheck-output.txt")


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


def allowed_names(allow: list[str] | tuple[str, ...] = (), setup_exe: Path | str | None = None) -> list[str]:
    """Every name allowed besides the record's: Inno Setup's helpers and the [Code]'s output
    (ALLOWED_EXTRA), the `--allow` names, and with the installer named, the `<installer name>.tmp` its
    loader extracts. Each once, ignoring case."""
    out: list[str] = []
    names = list(ALLOWED_EXTRA) + list(allow) + ([Path(setup_exe).stem + ".tmp"] if setup_exe else [])
    for n in names:
        if n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out


def existing_setup_folders(temp: Path) -> set[str]:
    """The setup folders already in `temp` (an earlier install's leftovers), lower case."""
    try:
        return {e.name.lower() for e in os.scandir(temp)
                if e.is_dir(follow_symlinks=False) and SETUP_FOLDER.fullmatch(e.name)}
    except OSError:
        return set()


@dataclass
class Seen:
    folder: str
    name: str                              # the file's path inside its setup folder
    path: Path
    key: str = ""                          # its name in the listing (folder in front if in a second one)
    first: tuple[int, str] | None = None   # (size, sha256) at the first read
    last: tuple[int, str] | None = None    # at the latest read
    gone: bool = False
    handle: int | None = None              # hold mode: the handle that keeps setup from deleting it

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.name


@dataclass
class Listing:
    seen: dict[str, Seen] = field(default_factory=dict)      # by key, lower case
    folders: list[str] = field(default_factory=list)
    ignored: set[str] = field(default_factory=set)          # setup folders there before the watch (lower case)
    origin: dict[str, str] = field(default_factory=dict)    # a name's first setup folder (both lower case)
    errors: list[str] = field(default_factory=list)
    lost: bool = False

    def note(self, temp: Path, action: int, rel: str) -> Seen | None:
        """One notification: a file that appears under a setup folder is entered (and returned, to be
        read); a removal marks it gone. A setup folder that was there before the watch is never looked
        at; a name met in a second setup folder is entered with that folder in front, never dropped."""
        where = setup_name(rel)
        if where is None:
            first = [p for p in re.split(r"[\\/]+", rel) if p]
            if len(first) == 1 and SETUP_FOLDER.fullmatch(first[0]) and first[0] not in self.folders \
                    and first[0].lower() not in self.ignored and action in APPEARS:
                self.folders.append(first[0])
            return None
        folder, name = where
        if folder.lower() in self.ignored:
            return None
        if folder not in self.folders:
            self.folders.append(folder)
        key = name
        if self.origin.setdefault(name.lower(), folder.lower()) != folder.lower():
            key = f"{folder}\\{name}"
        s = self.seen.get(key.lower())
        if action in GONE:
            if s is not None:
                s.gone = True
            return None
        if action not in APPEARS:
            return None
        if s is None:
            s = Seen(folder, name, temp / folder / Path(*name.split("\\")), key)
            self.seen[key.lower()] = s
        s.gone = False
        return s

    def result(self, allow: list[str], mode: str, temp: Path, installer_exit: int | None) -> dict:
        allowed = {a.lower() for a in allow}
        seen: dict[str, dict | None] = {}
        changed: dict[str, list[dict]] = {}
        extra: dict[str, str | None] = {}
        errors = list(self.errors)
        for s in self.seen.values():
            seen[s.key] = None if s.last is None else {"size": s.last[0], "sha256": s.last[1]}
            if s.key.lower() in allowed:
                extra[s.key] = None if s.last is None else s.last[1]
            if s.first is not None and s.last is not None and s.first != s.last:
                changed[s.key] = [{"size": s.first[0], "sha256": s.first[1]}, {"size": s.last[0], "sha256": s.last[1]}]
                if s.key.lower() not in allowed:
                    errors.append(f"{s.key}: changed while the watcher had it (the bytes setup ran may differ "
                                  "from the ones compared)")
            if s.last is None:
                errors.append(f"{s.key}: seen, not hashed")
        if self.lost:
            seen[LOST] = None
            errors.append(LOST)
        return {"format": 1, "temp": str(temp), "mode": mode, "folders": self.folders,
                "ignored_folders": sorted(self.ignored), "seen": dict(sorted(seen.items())),
                "allowed_extra": dict(sorted(extra.items())), "changed": changed, "errors": errors,
                "installer_exit": installer_exit}


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

_API = None


def _api():
    global _API
    if _API is not None:
        return _API
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
    _API = (ctypes, wintypes, k32, OVERLAPPED)
    return _API


def open_held(path: Path) -> tuple[int | None, int]:
    """Opens a file for reading with read and write sharing but no delete sharing (setup can finish
    writing it, and cannot delete it while it is held): (handle, 0), or (None, the Win32 error)."""
    ctypes, wt, k32, _ = _api()
    h = k32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                        FILE_ATTRIBUTE_NORMAL, None)
    if h in (None, 0, wt.HANDLE(-1).value):
        return None, ctypes.get_last_error()
    return h, 0


def read_whole(h: int) -> bytes:
    """A held handle's whole file, from its start."""
    ctypes, wt, k32, _ = _api()
    if not k32.SetFilePointerEx(h, 0, None, 0):
        raise WatchError(f"SetFilePointerEx failed (error {ctypes.get_last_error()})")
    out = bytearray()
    chunk = ctypes.create_string_buffer(1 << 20)
    got = wt.DWORD(0)
    while True:
        if not k32.ReadFile(h, chunk, 1 << 20, ctypes.byref(got), None):
            raise WatchError(f"ReadFile failed (error {ctypes.get_last_error()})")
        if got.value == 0:
            return bytes(out)
        out += chunk.raw[:got.value]


def win_open(path: Path) -> int | None:
    return open_held(path)[0]


def win_read(handle: int) -> bytes | None:
    try:
        return read_whole(handle)
    except WatchError:
        return None


def win_close(handle: int) -> None:
    _api()[2].CloseHandle(handle)


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
        # An earlier install's setup folders (its held files outlive setup's cleanup) are never looked at.
        self.listing.ignored = existing_setup_folders(self.temp)
        self.buf = (self.ctypes.c_uint32 * (BUFFER // 4))()
        self.ov = OVERLAPPED()
        self.ov.hEvent = self.k32.CreateEventW(None, True, False, None)
        self.pending: dict[str, Seen] = {}
        self.armed = False
        self._arm()
        self.rescan()  # a folder made between the sweep above and the watch's start

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
                        if s is not None and s.handle is None:  # a held file cannot go
                            self.pending[s.key.lower()] = s
                self._arm()
            else:
                raise WatchError(f"the watch ended (error {self.ctypes.get_last_error()})")
        self.retry()

    def rescan(self) -> None:
        for entry in os.scandir(self.temp):
            if entry.is_dir(follow_symlinks=False) and SETUP_FOLDER.fullmatch(entry.name) \
                    and entry.name.lower() not in self.listing.ignored:
                for root, _, files in os.walk(entry.path):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), self.temp)
                        s = self.listing.note(self.temp, FILE_ACTION_ADDED, rel)
                        if s is not None and s.handle is None and s.last is None:
                            self.pending[s.key.lower()] = s

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
        return open_held(path)

    def _read_all(self, h: int) -> bytes:
        return read_whole(h)

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
                    self.listing.errors.append(f"{s.key}: the last read failed ({e})")
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
          command: list[str] | None = None, settle_s: float = 2.0, seconds: float | None = None,
          until: Path | None = None) -> dict:
    """Watches while the installer runs (or until `until` exists, or for `seconds`), then writes and
    returns the listing. `allow` is every name allowed besides the record's (allowed_names())."""
    w = Watcher(temp, mode, copy_dir)
    installer_exit = None
    try:
        proc = subprocess.Popen(command, shell=False) if command else None
        end = None if proc is not None else (time.monotonic() + seconds if seconds is not None else None)
        try:
            while True:
                w.step()
                if proc is not None and proc.poll() is not None:
                    if installer_exit is None:
                        installer_exit = proc.returncode
                        end = time.monotonic() + settle_s
                if until is not None and until.exists():
                    break  # setup has ended (the recipe makes this file once setup's process exits)
                if end is not None and time.monotonic() >= end:
                    break
        except KeyboardInterrupt:
            pass
        w.finish()
    finally:
        w.close()
    result = w.listing.result(allow, mode, w.temp, installer_exit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
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
    ap.add_argument("--allow", action="append", default=[], help="an exact name besides the record's and "
                    "Inno Setup's own helpers (the files the [Code] writes), relative to its is-*.tmp folder")
    ap.add_argument("--setup-exe", default=None, help="the installer: its loader's <name>.tmp is allowed")
    ap.add_argument("--mode", choices=("hold", "copy"), default="hold")
    ap.add_argument("--copy-dir", default=None, help="--mode copy: where each file is copied")
    ap.add_argument("--copy-to", default=None, help="short for --mode copy --copy-dir DIR")
    ap.add_argument("--settle", type=float, default=2.0, help="seconds to keep watching after the installer exits")
    ap.add_argument("--seconds", type=float, default=None, help="without an installer command: how long to "
                    f"watch at most (default {DEFAULT_SECONDS:g})")
    ap.add_argument("--until-exists", default=None, help="stop once this file exists (made after setup exits)")
    a = ap.parse_args(argv)
    if a.copy_to:
        if a.copy_dir and Path(a.copy_dir) != Path(a.copy_to):
            print("tmp payload watch: --copy-to and --copy-dir name two folders", file=sys.stderr)
            return 2
        a.mode, a.copy_dir = "copy", a.copy_to
    seconds = a.seconds if a.seconds is not None or command else DEFAULT_SECONDS
    try:
        r = watch(Path(a.temp), Path(a.out), allowed_names(a.allow, a.setup_exe), a.mode,
                  Path(a.copy_dir) if a.copy_dir else None, command or None, a.settle, seconds,
                  Path(a.until_exists) if a.until_exists else None)
    except WatchError as e:
        print(f"tmp payload watch: {e}", file=sys.stderr)
        return 2
    for e in r["errors"]:
        print(f"tmp payload watch: {e}", file=sys.stderr)
    hashed = sum(v is not None for v in r["seen"].values())
    print(f"tmp payload watch: {len(r['seen'])} files in {len(r['folders'])} setup folder(s), {hashed} hashed"
          f"{', ' + str(len(r['ignored_folders'])) + ' earlier folder(s) ignored' if r['ignored_folders'] else ''}; "
          f"listing {a.out}" + (f"; installer exit {r['installer_exit']}" if command else ""))
    return 0 if not r["errors"] and (not command or r["installer_exit"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
