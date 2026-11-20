"""The tool suite's own temporary folders: made with one of these prefixes in the system temp folder, removed by the
test that made them, and any of them older than a day removed at the suite's start (a crashed or killed run's
leftovers; 2026-11-20: 5221 of them had piled up). Standard library only."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

PREFIXES = ("skytest-no-builds-", "skytest-junction-target-", "skytest-acceptance-", "inside-race-")
MAX_AGE_S = 86400.0


def _has_link(folder: Path) -> bool:
    """A symbolic link or a junction anywhere inside (never followed, never removed through)."""
    for root, dirs, files in os.walk(folder, followlinks=False):
        for n in dirs + files:
            p = os.path.join(root, n)
            try:
                st = os.lstat(p)
            except OSError:
                return True
            if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:  # reparse point
                return True
    return False


def sweep(max_age_s: float = MAX_AGE_S, now: float | None = None, root: Path | None = None) -> int:
    """Removes this suite's temp folders older than max_age_s from the temp folder; returns how many."""
    base = Path(root or tempfile.gettempdir())
    now = time.time() if now is None else now
    n = 0
    try:
        entries = list(os.scandir(base))
    except OSError:
        return 0
    for e in entries:
        if not e.name.startswith(PREFIXES):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            continue  # a link or a junction itself: left alone
        if now - st.st_mtime < max_age_s or _has_link(Path(e.path)):
            continue
        shutil.rmtree(e.path, ignore_errors=True)
        n += not os.path.exists(e.path)
    return n


def make(prefix: str) -> str:
    """A temp folder of this suite (prefix one of PREFIXES)."""
    assert prefix in PREFIXES, prefix
    return tempfile.mkdtemp(prefix=prefix)


def remove(path: str | None) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)
