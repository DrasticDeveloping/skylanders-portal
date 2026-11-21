"""Builds and game tests keep out of each other's way: the runner starts no session while a build slot
is held (this module), and the game's build wrapper starts no build while a test session runs (the
game's tools/build_low.py, its own half).

A build slot is a file `slot<i>.lock` in one folder per user and machine (SFO_BUILD_SLOT_DIR, default
%LOCALAPPDATA%/SwapForceOnline/build-slots; build_low.py's slot_dir) on which a running build holds an
exclusive one-byte lock at offset 1 GiB; Windows drops it when the build's process ends. The text at
the file's start names the holder (worktree, recipes, pid, since). A probe takes the lock and drops it
at once: a slot it cannot take is held.

Before a case (and a live session) starts its games the runner waits while a slot is held, printing
one line a minute, up to a limit (`--build-wait-min`, default BUILD_WAIT_MIN); past it the case is not
started (status `build running`, exit 4 when nothing else failed). `--ignore-build "<why>"` starts it
anyway and the why goes into the result.

Pure decision (decide) and a wait loop with injected clock, sleep and probe; unit-tested in
tests/test_build_guard.py. A build that takes its slot in the seconds between this check and the
games' start is caught by the build's own half: it checks for test games a second time after a settle."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable

BUILD_RUNNING = "build running"
BUILD_WAIT_MIN = 45.0     # minutes a case waits for the builds to end before it is not started
REPORT_EVERY_S = 60.0
POLL_S = 5.0
LOCK_OFFSET = 1 << 30     # build_low.py's KernelLock byte

START, WAIT, GIVE_UP, OVERRIDE = "start", "wait", "give up", "override"


def slot_dir(env: dict[str, str] | None = None) -> Path:
    """build_low.py's folder: SFO_BUILD_SLOT_DIR, else %LOCALAPPDATA%/SwapForceOnline/build-slots."""
    env = os.environ if env is None else env
    if env.get("SFO_BUILD_SLOT_DIR"):
        return Path(env["SFO_BUILD_SLOT_DIR"])
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SwapForceOnline" / "build-slots"


def _read_info(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
        info = json.loads(raw.decode("utf-8", errors="replace").strip("\0 \r\n") or "{}")
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def _held(path: Path) -> bool:
    """True while another handle holds the slot's byte lock (Windows only; elsewhere never held)."""
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    try:
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def held_slots(folder: Path) -> list[dict]:
    """Who holds a build slot in `folder` now (each holder's text; {} when it has written none yet)."""
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("slot*.lock")):
        if re.fullmatch(r"slot\d+\.lock", p.name) and _held(p):
            out.append(_read_info(p))
    return out


def describe(holder: dict, now: float | None = None) -> str:
    if not holder:
        return "a build that has not said who it is yet"
    extra = ""
    started = holder.get("started")
    if isinstance(started, (int, float)):
        extra = f", {int(((now or time.time()) - started) // 60)} min"
    recipes = holder.get("recipes")
    what = f" ({' '.join(recipes)})" if isinstance(recipes, list) and recipes else ""
    return f"{holder.get('worktree', '?')}{what}, pid {holder.get('pid', '?')}, since {holder.get('since', '?')}{extra}"


def decide(holders: list[dict], waited_s: float, limit_s: float, override: str) -> str:
    """START (no build), OVERRIDE (a build, but `--ignore-build` said why), WAIT (a build, within the
    limit) or GIVE_UP (a build still after the limit)."""
    if not holders:
        return START
    if override:
        return OVERRIDE
    return WAIT if waited_s < limit_s else GIVE_UP


def wait_for_builds(who: str, limit_s: float, override: str, probe: Callable[[], list[dict]],
                    say: Callable[[str], None], clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep, poll_s: float = POLL_S) -> tuple[str, float, str]:
    """Waits until no build slot is held. (decision, seconds waited, the line for a result): START or
    OVERRIDE to go on, GIVE_UP when the limit passed. One line through `say` when the wait begins and one
    a minute after."""
    t0 = clock()
    told = None
    while True:
        holders = probe()
        waited = clock() - t0
        d = decide(holders, waited, limit_s, override)
        names = "; ".join(describe(h) for h in holders)
        if d == START:
            if told is not None:
                say(f"{who}: the build ended after {waited:.0f} s of waiting; starting")
            return d, waited, ""
        if d == OVERRIDE:
            line = f"started beside a build ({names}): --ignore-build: {override}"
            say(f"{who}: {line}")
            return d, waited, line
        if d == GIVE_UP:
            line = (f"not started: a build still held its slot after {waited / 60:.0f} min ({names}); the build"
                    " goes first. Wait for it, raise --build-wait-min, or --ignore-build \"<why>\"")
            say(f"{who}: {line}")
            return d, waited, line
        if told is None or clock() - told >= REPORT_EVERY_S:
            say(f"{who}: waits for a build to end ({names}); {waited / 60:.0f} of {limit_s / 60:.0f} min")
            told = clock()
        sleep(poll_s)
