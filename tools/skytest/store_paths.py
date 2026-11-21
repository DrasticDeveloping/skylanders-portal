"""Which folders the test runner (skytest.py) and the game repository's store janitor
(tools/test_store_janitor.py) may clean, and the checks right before a delete. One module for both: the
janitor imports it from this portal (thirdparty/skylanders-portal/tools/skytest).

Ownership is a marker, never an inference: a store that is a directory junction (its target on another
drive) and an outside snapshots folder are cleaned only when the folder holds MARKER, put there on purpose,
whose one line is the absolute path of the store (the `.replication-tests` link or folder) it belongs to.
One outside snapshots folder belongs to one store. On top, a target is refused when it is equal to, inside
or around: any git checkout, a disc folder, the owner's data, the user profile's root, a drive root
(protected_reason). Paths are compared as os.path.realpath forms; a guard root that cannot be resolved
refuses (fails closed).

Right before every delete, check_victim: the victim's realpath is still inside the pinned root and the
victim is no link; else the whole cleanup stops.

Live games: data_roots_in_use reads the command lines of the running games (Windows' process command line,
NtQueryInformationProcess class 60) and their `--user_data_root`; None when that cannot be read (a caller
then keeps everything a live game could be using).

Standard library only.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

MARKER = "SFO-TEST-STORE.txt"
SNAPSHOTS_FILE = "snapshots-folder.txt"
KEEP_FILE = "KEEP"
KEEP_LIST = "keep.txt"
# Names no cleanup policy deletes, at any level it looks at.
RESERVED = {MARKER.lower(), SNAPSHOTS_FILE.lower(), KEEP_FILE.lower(), KEEP_LIST.lower()}
GAME_EXE = "swapforcerecompiled.exe"
DISC_FILE = "default.xex"

# Reparse tags that make a folder a link: a junction (mount point) and a symbolic link.
LINK_REPARSE_TAGS = {0xA0000003, 0xA000000C}


def is_link(p: str | Path) -> bool:
    """A junction or a symbolic link (not other reparse points: OneDrive placeholders are real)."""
    if hasattr(os.path, "isjunction") and os.path.isjunction(p):
        return True
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return getattr(st, "st_reparse_tag", 0) in LINK_REPARSE_TAGS
    return False


def real(p: str | Path) -> str:
    """The canonical comparison form: realpath (links followed), normcase, no `\\\\?\\` prefix."""
    r = os.path.realpath(str(p))
    if r.startswith("\\\\?\\UNC\\"):
        r = "\\\\" + r[8:]
    elif r.startswith("\\\\?\\"):
        r = r[4:]
    return os.path.normcase(r)


def within(path: str, root: str) -> bool:
    """`path` equal to or inside `root`, both real() forms."""
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def overlaps(a: str, b: str) -> bool:
    """Equal, inside or around (real() forms)."""
    return within(a, b) or within(b, a)


def bad_spelling(text: str) -> str:
    """Spellings refused before any lookup: UNC shares and the `\\\\?\\` and `\\\\.\\` namespaces."""
    t = text.strip().replace("/", "\\")
    if t.startswith("\\\\"):
        return f"{text!r} is a UNC or device path (\\\\server, \\\\?\\ or \\\\.\\): give a plain drive path"
    p = Path(text)
    if not p.is_absolute() or not p.drive:
        return f"{text!r} is not an absolute path with its drive"
    return ""


def _parents(path: str) -> list[str]:
    out, cur = [], path
    while True:
        out.append(cur)
        up = os.path.dirname(cur)
        if up == cur:
            return out
        cur = up


def disc_near(path: str) -> str:
    """A disc folder at, above or one level below `path` (default.xex there, or a folder named rom)."""
    for p in _parents(path):
        if os.path.isfile(os.path.join(p, DISC_FILE)):
            return f"{p} holds the disc ({DISC_FILE})"
        if os.path.basename(p).lower() == "rom":
            return f"{p} is a folder named rom"
    try:
        for e in os.scandir(path):
            if e.is_dir(follow_symlinks=False) and (e.name.lower() == "rom" or os.path.isfile(os.path.join(e.path, DISC_FILE))):
                return f"{e.path} below it is a disc folder"
    except OSError:
        pass
    return ""


def is_bare_repository(folder: str) -> bool:
    """A bare repository: HEAD, objects and refs at its root."""
    return (os.path.isfile(os.path.join(folder, "HEAD")) and os.path.isdir(os.path.join(folder, "objects"))
            and os.path.isdir(os.path.join(folder, "refs")))


def is_checkout(folder: str) -> bool:
    """A git checkout: its `.git` is a git directory (it has HEAD) or a worktree's `gitdir:` file, or the
    folder is a bare repository. An empty `.git` folder is none (this machine's profile has one: git itself
    says "not a git repository")."""
    if is_bare_repository(folder):
        return True
    g = os.path.join(folder, ".git")
    if os.path.isdir(g):
        return os.path.isfile(os.path.join(g, "HEAD"))
    if os.path.isfile(g):
        try:
            with open(g, encoding="utf-8", errors="replace") as fh:
                return fh.read(8).startswith("gitdir:")
        except OSError:
            return True  # unreadable: counted as one (fail closed)
    return False


def checkout_near(path: str) -> str:
    """A git checkout at or above `path`, or one at any depth inside it (links not followed)."""
    for p in _parents(path):
        if is_checkout(p):
            return f"{p} is a git checkout"
    for root, dirs, _files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not is_link(os.path.join(root, d))]
        for d in dirs:
            if is_checkout(os.path.join(root, d)):
                return f"{os.path.join(root, d)} inside it is a git checkout"
    return ""


def danger_inside(path: str | Path) -> str:
    """What makes a folder no victim ('' when nothing does): a `default.xex`, a folder named `rom`, a `.git`
    (folder or file) or a bare repository at any depth below it (links not followed; an unreadable part
    counts: fail closed). The cleanup then stops whole."""
    p = str(path)
    if os.path.isfile(p):
        return f"{p} is a disc file" if os.path.basename(p).lower() == DISC_FILE else ""
    if is_bare_repository(p):
        return f"{p} is a bare git repository"
    errors: list[str] = []
    for root, dirs, files in os.walk(p, followlinks=False, onerror=lambda e: errors.append(str(e))):
        dirs[:] = [d for d in dirs if not is_link(os.path.join(root, d))]
        for name in files:
            low = name.lower()
            if low == DISC_FILE:
                return f"{os.path.join(root, name)} is a disc file"
            if low == ".git":
                return f"{os.path.join(root, name)} is a git checkout's .git"
        for d in dirs:
            low = d.lower()
            full = os.path.join(root, d)
            if low == "rom":
                return f"{full} is a folder named rom"
            if low == ".git":
                return f"{full} is a git checkout's .git"
            if is_bare_repository(full):
                return f"{full} is a bare git repository"
    if errors:
        return f"a part of {p} could not be read ({errors[0]})"
    return ""


def owner_data_roots() -> list[Path]:
    """Every place the owner's Skylanders data folder can be: Documents under the profile, Documents as
    Windows records it, and Documents inside each OneDrive folder."""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    docs = [home / "Documents", home / "OneDrive" / "Documents"]
    if os.name == "nt":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _ = winreg.QueryValueEx(k, "Personal")
            if value:
                docs.append(Path(os.path.expandvars(value)))
        except OSError:
            pass
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(var):
            docs.append(Path(os.environ[var]) / "Documents")
    return [d / "Skylanders" for d in docs]


def protected_reason(target: str | Path, extra: list[tuple[str, str | Path]] = (),
                     owner_roots: list[Path] | None = None) -> str:
    """Why `target` may not be a cleaned folder ('' when it may): a drive root, the user profile's root or
    around it, the owner's data (equal, inside, around), a disc folder near it, a git checkout near it, or
    one of `extra` (what, path) equal, inside or around. Fails closed on a root it cannot resolve."""
    try:
        t = real(target)
    except (OSError, ValueError) as e:
        return f"{target} cannot be resolved ({e})"
    if os.path.dirname(t) == t:
        return f"{target} is a drive root"
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    try:
        if within(real(profile), t):
            return f"{target} is the user profile {profile} or around it"
    except (OSError, ValueError) as e:
        return f"the user profile {profile} cannot be resolved ({e})"
    for root in (owner_data_roots() if owner_roots is None else owner_roots):
        try:
            if overlaps(t, real(root)):
                return f"{target} is inside or around the owner's data {root}"
        except (OSError, ValueError) as e:
            return f"the owner's data {root} cannot be resolved ({e})"
    why = disc_near(t) or checkout_near(t)
    if why:
        return f"{target}: {why}"
    for what, root in extra:
        try:
            if overlaps(t, real(root)):
                return f"{target} is inside or around {what} {root}"
        except (OSError, ValueError) as e:
            return f"{what} {root} cannot be resolved ({e})"
    return ""


def read_marker(folder: str | Path) -> str | None:
    """The marker's one line (the store it belongs to), None without a marker."""
    f = Path(folder) / MARKER
    if not f.is_file() or is_link(f):
        return None
    lines = [ln.strip() for ln in f.read_text(encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]
    return lines[0] if len(lines) == 1 else ""


def marker_error(folder: str | Path, store: str | Path) -> str:
    """'' when `folder` holds a marker naming `store`; else what to create, or whose folder it is."""
    want = os.path.abspath(str(store))
    line = read_marker(folder)
    if line is None:
        return (f"{folder} holds no {MARKER}: if it belongs to this store, create {Path(folder) / MARKER} with the "
                f"one line {want}")
    if not line:
        return f"{Path(folder) / MARKER} must hold exactly one line, the store's path {want}"
    try:
        if real(line) != real(want):
            return f"{folder} belongs to the store {line} ({MARKER}), not to {want}: one folder serves one store"
    except (OSError, ValueError) as e:
        return f"{Path(folder) / MARKER}: {line} cannot be resolved ({e})"
    return ""


def validate_snapshots_folder(text: str, store: str | Path, guards: list[tuple[str, str | Path]],
                              owner_roots: list[Path] | None = None) -> tuple[Path | None, str]:
    """The outside snapshots folder `text` names (snapshots-folder.txt's one line) for the store `store`:
    (its real path, '') or (None, why it is refused). It must exist as a folder (not a file), with no link
    on its way, hold this store's MARKER, and pass protected_reason with `guards` (the repository, the store,
    the disc). The runner and the janitor both call this."""
    why = bad_spelling(text)
    if why:
        return None, why
    given = os.path.normpath(text.strip())
    walk = os.path.splitdrive(given)[0] + os.sep
    for part in given[len(walk):].split(os.sep):
        if not part:
            continue
        walk = os.path.join(walk, part)
        if not os.path.lexists(walk):
            return None, (f"{given} does not exist: create the folder and {os.path.join(given, MARKER)} with the one "
                          f"line {os.path.abspath(str(store))}")
        if is_link(walk):
            return None, f"{walk} on the way to {given} is a junction or link"
    if not os.path.isdir(given):
        return None, f"{given} is a file, not a folder"
    why = protected_reason(given, guards, owner_roots)
    if why:
        return None, why
    why = marker_error(given, store)
    if why:
        return None, why
    return Path(real(given)), ""


def check_victim(victim: str | Path, root: str) -> str:
    """Right before a delete: '' when `victim` is still inside the pinned `root` (a real() form) and is no
    link itself; else why the cleanup must stop."""
    v = str(victim)
    if is_link(v):
        return f"{v} is now a junction or link"
    try:
        r = real(v)
    except (OSError, ValueError) as e:
        return f"{v} cannot be resolved ({e})"
    if r == root or not within(r, root):
        return f"{v} resolves to {r}, outside the pinned folder {root}"
    return ""


# --- the running games' data folders ---------------------------------------------------------------------

def command_lines(exe: str) -> list[str] | None:
    """The command lines of the processes named `exe` (case-insensitive), None when they cannot be read
    (not Windows, an API failure, a process that does not answer: the caller keeps what a game may use)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    pids = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe.lower():
                pids.append(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        # The list ends only with ERROR_NO_MORE_FILES (18); any other error leaves it partial: cannot tell.
        if ctypes.get_last_error() != 18:
            return None
    finally:
        k32.CloseHandle(snap)
    out = []
    for pid in pids:
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: it ended after the snapshot
                continue
            return None  # access denied or anything else: not readable, the caller keeps what it may use
        try:
            size = wintypes.ULONG(0)
            ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if ntdll.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) != 0:
                return None
            us = UNICODE_STRING.from_buffer(buf)
            out.append(ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else "")
        finally:
            k32.CloseHandle(h)
    return out


def pid_command_line(pid: int) -> str | None:
    """One process's command line, None when it cannot be read (not Windows, it ended, access denied)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        size = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) != 0:
            return None
        us = UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
    finally:
        k32.CloseHandle(h)


def split_command_line(line: str) -> list[str] | None:
    """A command line's arguments as the program sees them (Windows' CommandLineToArgvW: a token quoted as a
    whole, `"--user_data_root=C:/a b"`, is one argument)."""
    if os.name != "nt":
        return None  # no Windows splitting: cannot tell (the reader is Windows-only anyway)
    import ctypes
    from ctypes import wintypes
    shell32 = ctypes.WinDLL("shell32")
    k32 = ctypes.WinDLL("kernel32")
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    n = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(line, ctypes.byref(n))
    if not argv:
        return None  # it could not be split: cannot tell (a quoted data folder would be lost)
    try:
        return [argv[i] for i in range(n.value)]
    finally:
        k32.LocalFree(argv)


def data_roots_in_use(exe: str = GAME_EXE) -> list[str] | None:
    """The real() forms of the running games' `--user_data_root` folders, None when unreadable."""
    lines = command_lines(exe)
    if lines is None:
        return None
    out = []
    for line in lines:
        args = split_command_line(line)
        if args is None:
            return None
        for k, arg in enumerate(args):
            if arg.startswith("--user_data_root="):
                out.append(real(arg.split("=", 1)[1]))
            elif arg == "--user_data_root" and k + 1 < len(args):
                out.append(real(args[k + 1]))
    return out


if __name__ == "__main__":  # a quick look: python -I store_paths.py [exe]
    print(data_roots_in_use(sys.argv[1] if len(sys.argv) > 1 else GAME_EXE))
