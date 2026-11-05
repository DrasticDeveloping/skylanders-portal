#!/usr/bin/env python3
"""skytest: the multiplayer test runner of the Skylanders ports' online forks.

Run from a game repository (its `just test-online` calls this). A case is a TOML spec in the game's
tests/runtime/; it names a layout and what each peer's game does (an input script) and what its log
must show. The runner starts the layout's game processes, each on its own copies of the test data,
ends them, and writes report.md and results.json in a new run folder of the worktree's test store.

    skytest.py test [CASE ...] [--stage S] [--max-games N]   run cases (a name, a glob, or all)
    skytest.py list                                         the cases and the manifest's state
    skytest.py lint                                         check every spec and the manifest
    skytest.py fixtures --from-owner                        copy the owner's saves and figures into
                                                            the fixture store (read only on their side)

Layouts (the game repository's docs/testing-guide.md has the details):

    solo        H alone: the plain game, offline (reference measurements)
    server+N    the server peer S and N clients H, D, E, F (N = 1..4); H is the owner's client

Until stage 1A gives the game a server role, S is a plain game instance with the runner's own
lifecycle script (it boots hidden and quits); it runs no case script.

Rules the runner enforces (docs/RULES.md of the game): outputs only inside the worktree's
.replication-tests/; never under the owner's data folder, never into the disc; the build is copied
into a snapshot and run from there, never from the build folder; every setting passed is non-empty;
every game is in a job object, so none outlives the run; a case that cannot run says why in its
report (capacity, missing fixture, spec error) instead of being skipped silently.

Standard library only; run with `python -I`.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import winjob  # noqa: E402

STORE = ".replication-tests"
CLIENTS = ["H", "D", "E", "F"]
LAYOUT = re.compile(r"^(solo|server\+([1-4]))$")
PEER_DIRS = ("data", "figures", "cache")
# Layouts the manifest may name that one PC cannot run: a second PC, other networks, a test binary.
MANIFEST_ONLY_LAYOUTS = ("server+3+LAN", "internet+2", "unit")

# Log lines the game writes (skylanders-portal src/dev, the game's where hook).
TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]")
FPS = re.compile(r"Input script: ([\d.]+) guest FPS \((\d+) frames in ([\d.]+) s\), longest frame (\d+) ms, "
                 r"(\d+) over 50 ms")
WHERE = re.compile(r"Where: (\w+) at (-?[\d.]+) (-?[\d.]+) (-?[\d.]+)")
MEMORY = re.compile(r"Input script: memory (\d+) MB private, (\d+) MB working set")
BUILD = re.compile(r"\(build (\w+)\)")
ALWAYS_FORBIDDEN = [r"\] Crash: exception", r"Input script: cannot (parse|use)"]

# The lifecycle script of the stand-in server peer until stage 1A: boots, stays up, quits.
STANDIN_SERVER_SCRIPT = "wait {seconds}\nfps 5\nquit\n"


class SpecError(Exception):
    pass


# --- configuration --------------------------------------------------------------------------------


@dataclass
class Config:
    repo: Path
    store: Path
    exe: str
    build: Path
    rom: Path
    settings_template: Path
    default_args: list[str]
    timeout: float
    cases_dir: Path
    manifest: Path
    thresholds: Path
    baseline: Path
    owner_saves: Path
    owner_figures: Path
    launcher: list[str]


def repo_root(start: Path) -> Path:
    r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       encoding="utf-8", errors="surrogateescape")
    if r.returncode != 0:
        raise SystemExit(f"{start} is not inside a git checkout of a game")
    return Path(r.stdout.strip()).resolve()


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def load_config(repo: Path) -> Config:
    f = repo / "tests" / "skytest.toml"
    if not f.is_file():
        raise SystemExit(f"{f} is missing: the game repository names its exe, build and fixtures there")
    c = tomllib.loads(f.read_text(encoding="utf-8"))
    g, fx, d = c.get("game", {}), c.get("fixtures", {}), c.get("defaults", {})
    return Config(
        repo=repo,
        store=repo / STORE,
        exe=g["exe"],
        build=repo / g["build"],
        rom=(repo / g.get("rom", "rom")),
        settings_template=repo / g["settings_template"],
        default_args=list(d.get("args", [])),
        timeout=float(d.get("timeout", 300)),
        cases_dir=repo / c.get("cases", "tests/runtime"),
        manifest=repo / c.get("manifest", "tests/manifest.toml"),
        thresholds=repo / c.get("thresholds", "tests/thresholds.toml"),
        baseline=repo / c.get("baseline", "tests/baseline.toml"),
        owner_saves=expand(fx["owner_saves"]),
        owner_figures=expand(fx["owner_figures"]),
        # Only the runner's own tests set this: a command that stands in for the game's exe.
        launcher=[os.path.expandvars(x) for x in g.get("launcher", [])],
    )


# --- safety ---------------------------------------------------------------------------------------


def owner_data_roots() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    docs = [home / "Documents", home / "OneDrive" / "Documents"]
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(var):
            docs.append(Path(os.environ[var]) / "Documents")
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
    return [d / "Skylanders" for d in docs]


def inside(path: Path, folder: Path) -> bool:
    p, f = path.resolve(), folder.resolve()
    return p == f or f in p.parents


def check_output_path(cfg: Config, path: Path) -> None:
    """Everything a run writes goes inside the worktree's store, never the owner's data or the disc."""
    if not inside(path, cfg.store):
        raise SystemExit(f"refusing to write {path}: outside the test store {cfg.store}")
    for root in owner_data_roots():
        if inside(path, root):
            raise SystemExit(f"refusing to write {path}: inside the owner's data folder {root}")
    if inside(path, cfg.rom):
        raise SystemExit(f"refusing to write {path}: inside the disc")


# Settings the runner sets for each peer itself. A case or the defaults may not set them again: the
# game takes the last of two, so a second --user_data_root would point a test at any folder.
RUNNER_OWNED = ("user_data_root", "cache_root", "config_file", "log_file", "portal_figures_dir",
                "game_data_root", "input_script")


def check_args(args: list[str]) -> None:
    """A setting with an empty value makes the game drop its whole command line (and run on the
    owner's real data): refuse it. Each runner-owned setting appears exactly once, and the portal is
    the software one (a real portal would get the test's writes)."""
    counts: dict[str, int] = {}
    for a in args:
        if a.startswith("--") and a.endswith("="):
            raise SpecError(f"refusing an empty setting {a!r}: the game would ignore its whole command line")
        if a.startswith("--"):
            key = a[2:].split("=", 1)[0]
            key = key[3:] if key.startswith("no-") else key
            counts[key] = counts.get(key, 0) + 1
    for key in RUNNER_OWNED:
        if counts.get(key, 0) != 1:
            raise SpecError(f"refusing to start a game with --{key} set {counts.get(key, 0)} times (the runner"
                             " sets it once, for the peer's own copy)")
    if counts.get("portal_mode", 0) != 1 or "--portal_mode=software" not in args:
        raise SpecError("refusing to start a game without exactly one --portal_mode=software")


# --- specs ----------------------------------------------------------------------------------------


@dataclass
class PeerSpec:
    name: str
    script: str = ""
    args: list[str] = field(default_factory=list)
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)


@dataclass
class Spec:
    name: str
    path: Path
    layout: str
    stage: str
    description: str
    timeout: float
    peers: dict[str, PeerSpec]
    measures: dict[str, dict]
    standin_seconds: float


def peers_of(layout: str) -> list[str]:
    m = LAYOUT.match(layout)
    if not m:
        raise SpecError(f"unknown layout {layout!r} (solo, server+1 .. server+4)")
    if layout == "solo":
        return ["H"]
    return ["S"] + CLIENTS[: int(m.group(2))]


def load_spec(path: Path, default_timeout: float) -> Spec:
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SpecError(f"{path.name}: {e}") from e
    for key in ("name", "layout", "stage"):
        if key not in d:
            raise SpecError(f"{path.name}: `{key}` is missing")
    names = peers_of(d["layout"])
    table = d.get("peer", {})
    unknown = set(table) - set(names) - {"all"}
    if unknown:
        raise SpecError(f"{path.name}: peers {sorted(unknown)} are not in layout {d['layout']} ({names})")
    # [peer.all] is every client (H, D, E, F); the server peer S takes only its own [peer.S].
    clients_common = table.get("all", {})
    peers: dict[str, PeerSpec] = {}
    for n in names:
        own = table.get(n, {})
        common = clients_common if n != "S" else {}
        if n == "S" and own.get("script"):
            raise SpecError(f"{path.name}: the server peer S runs no case script")
        script = own.get("script", common.get("script", "")) if n != "S" else ""
        if n != "S" and not script.strip():
            raise SpecError(f"{path.name}: peer {n} has no script")
        if script and not re.search(r"(?m)^\s*quit\s*$", script):
            raise SpecError(f"{path.name}: peer {n}'s script does not end the game with `quit`")
        if ";" in script:
            raise SpecError(f"{path.name}: peer {n}'s script has a ';' (the game splits lines there, comments"
                            " included): one command per line")
        peers[n] = PeerSpec(
            name=n,
            script=script,
            args=list(common.get("args", [])) + list(own.get("args", [])),
            expect=list(common.get("expect", [])) + list(own.get("expect", [])),
            forbid=list(common.get("forbid", [])) + list(own.get("forbid", [])),
        )
        for rx in peers[n].expect + peers[n].forbid:
            try:
                re.compile(rx)
            except re.error as e:
                raise SpecError(f"{path.name}: peer {n}: bad pattern {rx!r}: {e}") from e
    measures = d.get("measure", {})
    for mname, m in measures.items():
        if not isinstance(m, dict) or "to" not in m:
            raise SpecError(f"{path.name}: measure {mname} needs `to` (and `from`, or it counts from the start)")
        if m.get("peer", "H") not in names:
            raise SpecError(f"{path.name}: measure {mname} is for peer {m.get('peer', 'H')}, not in {names}")
    return Spec(
        name=d["name"], path=path, layout=d["layout"], stage=str(d["stage"]),
        description=d.get("description", ""), timeout=float(d.get("timeout", default_timeout)),
        peers=peers, measures=measures, standin_seconds=float(d.get("standin_seconds", 45)),
    )


def all_specs(cfg: Config) -> list[Spec]:
    if not cfg.cases_dir.is_dir():
        return []
    return [load_spec(p, cfg.timeout) for p in sorted(cfg.cases_dir.glob("*.toml"))]


def load_manifest(cfg: Config) -> dict:
    if not cfg.manifest.is_file():
        return {"case": []}
    return tomllib.loads(cfg.manifest.read_text(encoding="utf-8"))


# --- snapshot and fixtures ------------------------------------------------------------------------


def snapshot_build(cfg: Config) -> Path:
    """Copy the exe and DLLs into the store once per build, so a rebuild during a run changes
    nothing. Runs never start the build folder's exe."""
    exe = cfg.build / cfg.exe
    if not exe.is_file():
        raise SystemExit(f"no build at {exe}: build the release first (`just build-release`)")
    files = [exe] + sorted(cfg.build.glob("*.dll"))
    h = hashlib.sha256()
    for f in files:
        st = f.stat()
        h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    snap = cfg.store / "snapshots" / h.hexdigest()[:12]
    check_output_path(cfg, snap)
    if not (snap / cfg.exe).is_file():
        tmp = snap.with_name(snap.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for f in files:
            shutil.copy2(f, tmp / f.name)
        tmp.rename(snap)
    return snap


def fixture_dirs(cfg: Config) -> tuple[Path, Path]:
    return cfg.store / "store" / "saves", cfg.store / "store" / "figures"


def cmd_fixtures(cfg: Config, a: argparse.Namespace) -> int:
    saves, figures = fixture_dirs(cfg)
    if not a.from_owner:
        print(f"fixtures: saves {'present' if saves.is_dir() else 'missing'} ({saves}), figures "
              f"{'present' if figures.is_dir() else 'missing'} ({figures})")
        print("`fixtures --from-owner` copies the owner's saves and figures in (read only on their side)")
        return 0
    for src, dst in ((cfg.owner_saves, saves), (cfg.owner_figures, figures)):
        if not src.is_dir():
            raise SystemExit(f"no {src} to copy from")
        check_output_path(cfg, dst)
        if dst.exists():
            print(f"{dst}: already there, kept (delete it by hand to take a newer copy)")
            continue
        # Only the saves themselves: not the shader cache, the owner's achievements, backups, logs, or
        # files other builds left there (an earlier online build's reward ledger).
        ignore = shutil.ignore_patterns("cache", "achievements", "save-backup", "*.log", "online-*.txt")
        shutil.copytree(src, dst, ignore=ignore)
        print(f"copied {src} -> {dst}")
    return 0


# --- running a case -------------------------------------------------------------------------------


@dataclass
class PeerResult:
    peer: str
    exit_code: int | None = None
    ended_by_runner: bool = False
    seconds: float = 0.0
    status: str = "not run"
    reasons: list[str] = field(default_factory=list)
    build: str = ""
    levels: list[str] = field(default_factory=list)
    fps: list[float] = field(default_factory=list)
    longest_frame_ms: list[int] = field(default_factory=list)
    peak_private_mb: int = 0
    peak_working_set_mb: int = 0
    errors: int = 0
    measures: dict[str, float | None] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case: str
    layout: str
    stage: str
    status: str = "not run"
    reasons: list[str] = field(default_factory=list)
    folder: str = ""
    started: str = ""
    seconds: float = 0.0
    peers: list[PeerResult] = field(default_factory=list)
    leftover_processes: int = 0


def peer_script(spec: Spec, peer: str, figures: Path) -> str:
    if peer == "S":
        return STANDIN_SERVER_SCRIPT.format(seconds=int(spec.standin_seconds))
    return spec.peers[peer].script.replace("{figures}", str(figures)).replace("{peer}", peer)


def prepare_peer(cfg: Config, spec: Spec, peer: str, run: Path) -> tuple[Path, list[str]]:
    d = run / peer
    check_output_path(cfg, d)
    saves, figures = fixture_dirs(cfg)
    if not saves.is_dir() or not figures.is_dir():
        raise SpecError(f"missing fixtures ({saves}, {figures}): run `skytest.py fixtures --from-owner`")
    shutil.copytree(saves, d / "data")
    shutil.copytree(figures, d / "figures")
    (d / "cache").mkdir()
    shutil.copy2(cfg.settings_template, d / "settings.toml")
    script = d / "script.txt"
    script.write_text(peer_script(spec, peer, d / "figures"), encoding="utf-8")
    args = [
        f"--user_data_root={d / 'data'}",
        f"--cache_root={d / 'cache'}",
        f"--config_file={d / 'settings.toml'}",
        f"--log_file={d / 'game.log'}",
        f"--portal_figures_dir={d / 'figures'}",
        f"--game_data_root={cfg.rom.resolve()}",
        f"--input_script={script}",
        *cfg.default_args,
        *(["--window_hidden=true"] if peer == "S" else []),
        *spec.peers[peer].args,
    ]
    check_args(args)
    return d, args


def parse_log(text: str) -> dict:
    out = {"levels": [], "fps": [], "longest": [], "private": 0, "working": 0, "errors": 0, "build": "",
           "lines": []}
    for line in text.splitlines():
        m = TS.match(line)
        t = time.mktime(time.strptime(m.group(1)[:19], "%Y-%m-%d %H:%M:%S")) + float("0" + m.group(1)[19:]) \
            if m else None
        out["lines"].append((t, line))
        if "[error]" in line:
            out["errors"] += 1
        if (b := BUILD.search(line)) and not out["build"]:
            out["build"] = b.group(1)
        if w := WHERE.search(line):
            if not out["levels"] or out["levels"][-1] != w.group(1):
                out["levels"].append(w.group(1))
        if f := FPS.search(line):
            out["fps"].append(float(f.group(1)))
            out["longest"].append(int(f.group(4)))
        if mem := MEMORY.search(line):
            out["private"] = max(out["private"], int(mem.group(1)))
            out["working"] = max(out["working"], int(mem.group(2)))
    return out


def measure(lines: list[tuple[float | None, str]], start: str | None, end: str) -> float | None:
    """Seconds from the first line matching `start` (or the first line) to the next matching `end`."""
    t0 = None
    i = 0
    if start is None:
        for i, (t, _) in enumerate(lines):
            if t is not None:
                t0 = t
                break
    else:
        rx = re.compile(start)
        for i, (t, line) in enumerate(lines):
            if t is not None and rx.search(line):
                t0 = t
                break
    if t0 is None:
        return None
    rx = re.compile(end)
    for t, line in lines[i + 1:]:
        if t is not None and rx.search(line):
            return round(t - t0, 3)
    return None


def grade_peer(spec: Spec, r: PeerResult, text: str, parsed: dict) -> None:
    reasons = []
    if r.ended_by_runner:
        reasons.append(f"did not quit within {spec.timeout:.0f} s; ended by the runner")
    elif r.exit_code != 0:
        reasons.append(f"exit code {r.exit_code}")
    if not text:
        reasons.append("no log written")
    ps = spec.peers.get(r.peer)
    expect = ps.expect if ps else []
    forbid = (ps.forbid if ps else []) + ALWAYS_FORBIDDEN
    for rx in expect:
        if not re.search(rx, text, re.M):
            reasons.append(f"log lacks /{rx}/")
    for rx in forbid:
        if m := re.search(rx, text, re.M):
            reasons.append(f"log has forbidden /{rx}/: {m.group(0)[:80]}")
    for name, m in spec.measures.items():
        if m.get("peer", "H") == r.peer:
            r.measures[name] = measure(parsed["lines"], m.get("from"), m["to"])
            if r.measures[name] is None:
                reasons.append(f"measure {name}: its lines were not found")
    r.reasons = reasons
    r.status = "pass" if not reasons else "fail"


def run_case(cfg: Config, spec: Spec, snap: Path, max_games: int, stamp: str) -> CaseResult:
    names = list(spec.peers)
    res = CaseResult(case=spec.name, layout=spec.layout, stage=spec.stage, started=stamp)
    if len(names) > max_games:
        res.status = "capacity"
        res.reasons = [f"needs {len(names)} games at once, this run allows {max_games} (--max-games)"]
        return res
    run = cfg.store / "runs" / f"{stamp}-{spec.name}"
    check_output_path(cfg, run)
    run.mkdir(parents=True)
    res.folder = str(run)
    exe = snap / cfg.exe
    t_start = time.monotonic()
    procs: dict[str, subprocess.Popen] = {}
    results = {n: PeerResult(peer=n) for n in names}
    try:
        with winjob.Job() as job:
            for n in names:
                d, args = prepare_peer(cfg, spec, n, run)
                cmd = [*cfg.launcher, *args] if cfg.launcher else [str(exe), *args]
                results[n].command = cmd
                # The game starts suspended, joins the job, and only then runs: it never runs outside
                # the job. If any step fails it is ended before the error goes on.
                with open(d / "stderr.txt", "wb") as err:
                    p = subprocess.Popen(cmd, cwd=str(d), stdout=subprocess.DEVNULL, stderr=err,
                                         stdin=subprocess.DEVNULL, creationflags=winjob.SUSPENDED_START)
                try:
                    job.add(p)
                    winjob.resume(p)
                except BaseException:
                    p.kill()
                    p.wait(timeout=30)
                    raise
                procs[n] = p
            started = {n: time.monotonic() for n in names}
            deadline = time.monotonic() + spec.timeout
            while any(p.poll() is None for p in procs.values()) and time.monotonic() < deadline:
                time.sleep(0.5)
            for n, p in procs.items():
                if p.poll() is None:
                    p.kill()
                    results[n].ended_by_runner = True
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                results[n].exit_code = p.returncode
                results[n].seconds = round(time.monotonic() - started[n], 1)
            res.leftover_processes = job.active()
            if res.leftover_processes:
                job.terminate()
    except (SpecError, OSError) as e:
        # Every game started so far went with the job when it closed.
        res.status, res.reasons = "error", [str(e)]
        return res
    res.seconds = round(time.monotonic() - t_start, 1)
    for n in names:
        log = run / n / "game.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        parsed = parse_log(text)
        r = results[n]
        r.build, r.levels, r.fps, r.longest_frame_ms = parsed["build"], parsed["levels"], parsed["fps"], parsed["longest"]
        r.peak_private_mb, r.peak_working_set_mb, r.errors = parsed["private"], parsed["working"], parsed["errors"]
        grade_peer(spec, r, text, parsed)
    res.peers = [results[n] for n in names]
    failed = [r.peer for r in res.peers if r.status != "pass"]
    if res.leftover_processes:
        res.reasons.append(f"{res.leftover_processes} game process(es) still running after the case; ended")
    if failed:
        res.reasons.append(f"peers failed: {', '.join(failed)}")
    res.status = "pass" if not failed and not res.leftover_processes else "fail"
    return res


# --- reports --------------------------------------------------------------------------------------


def write_report(cfg: Config, out: Path, results: list[CaseResult], manifest: dict, thresholds: dict,
                 max_games: int) -> None:
    lines = [f"# skytest run {out.name}", "", f"At most {max_games} game(s) at once.", ""]
    lines += ["| Case | Layout | Stage | Status | Time | Why |", "|---|---|---|---|---|---|"]
    for r in results:
        why = "; ".join(r.reasons) or ""
        lines.append(f"| {r.case} | {r.layout} | {r.stage} | {r.status.upper()} | {r.seconds:.0f} s | {why} |")
    for r in results:
        if not r.peers:
            continue
        lines += ["", f"## {r.case} ({r.layout})", "", f"Folder: `{r.folder}`", "",
                  "| Peer | Status | Exit | Time | Levels | FPS | Longest frame | Peak private MB | Errors | Measures |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for p in r.peers:
            ms = ", ".join(f"{k} {v} s" if v is not None else f"{k} -" for k, v in p.measures.items())
            fps = ", ".join(f"{v:.1f}" for v in p.fps)
            lf = ", ".join(str(v) for v in p.longest_frame_ms)
            exit_ = "ended" if p.ended_by_runner else str(p.exit_code)
            lines.append(f"| {p.peer} | {p.status.upper()} | {exit_} | {p.seconds:.0f} s | {' > '.join(p.levels)} "
                         f"| {fps} | {lf} | {p.peak_private_mb} | {p.errors} | {ms} |")
        for p in r.peers:
            for why in p.reasons:
                lines.append(f"- {p.peer}: {why}")
    written = {s for s in (c.get("id") for c in manifest.get("case", []))}
    ran = {r.case for r in results}
    missing = sorted(c["id"] for c in manifest.get("case", []) if c.get("spec") and c["id"] not in ran)
    if missing:
        lines += ["", "## Manifest cases not run in this run", "", ", ".join(missing)]
    lines += ["", f"Thresholds: {cfg.thresholds.name} (version {thresholds.get('version', '?')}); manifest cases: "
              f"{len(written)}."]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def previous_statuses(cfg: Config, current: Path) -> dict[str, str]:
    """The last recorded status of each case before this run, from earlier results.json files."""
    seen: dict[str, str] = {}
    runs = cfg.store / "runs"
    if not runs.is_dir():
        return seen
    for f in sorted(runs.glob("*/results.json")):
        if f.parent == current:
            continue
        try:
            for c in json.loads(f.read_text(encoding="utf-8")).get("cases", []):
                seen[c["case"]] = c["status"]
        except (OSError, ValueError, KeyError):
            continue
    return seen


def write_findings(out: Path, results: list[CaseResult], before: dict[str, str]) -> None:
    lines = ["# Findings: what changed since the last run of each case", ""]
    changes = 0
    for r in results:
        old = before.get(r.case)
        if old != r.status:
            changes += 1
            lines.append(f"- {r.case}: {old or 'never run'} -> {r.status}" + (f" ({'; '.join(r.reasons)})" if r.reasons else ""))
    if not changes:
        lines.append("Nothing new.")
    (out / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- commands -------------------------------------------------------------------------------------


def default_max_games() -> int:
    cores = os.cpu_count() or 4
    free_gb = 8.0
    if os.name == "nt":
        try:
            import ctypes

            class MEMSTATUS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMSTATUS()
            st.dwLength = ctypes.sizeof(MEMSTATUS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                free_gb = st.ullAvailPhys / 1024**3
        except (OSError, AttributeError):
            pass
    # A game takes about 1 GB of working set and 3 GB of commit in a level, and two threads' worth of CPU.
    return max(1, min(5, cores // 3, int(free_gb // 1.5)))


def select(specs: list[Spec], patterns: list[str], stage: str | None) -> list[Spec]:
    if not patterns or patterns == ["all"]:
        chosen = list(specs)
    else:
        chosen = [s for s in specs if any(fnmatch.fnmatch(s.name, p) or s.path.stem == p for p in patterns)]
        if not chosen:
            raise SystemExit(f"no case matches {patterns}; `skytest.py list` names them")
    if stage:
        chosen = [s for s in chosen if s.stage == stage]
    return chosen


def janitor_line(cfg: Config) -> None:
    j = cfg.repo / "tools" / "test_store_janitor.py"
    if j.is_file():
        r = subprocess.run([sys.executable, "-I", str(j), "--repo", str(cfg.repo), "--line"], capture_output=True,
                           text=True)
        if r.stdout.strip():
            print(r.stdout.strip())


def cmd_test(cfg: Config, a: argparse.Namespace) -> int:
    janitor_line(cfg)
    specs = select(all_specs(cfg), a.cases, a.stage)
    if not specs:
        raise SystemExit("no case to run")
    max_games = a.max_games or default_max_games()
    snap = snapshot_build(cfg)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = cfg.store / "runs" / f"{stamp}-session"
    check_output_path(cfg, out)
    out.mkdir(parents=True)
    manifest = load_manifest(cfg)
    thresholds = tomllib.loads(cfg.thresholds.read_text(encoding="utf-8")) if cfg.thresholds.is_file() else {}
    results = []
    for spec in specs:
        print(f"{spec.name} ({spec.layout}) ...", flush=True)
        r = run_case(cfg, spec, snap, max_games, time.strftime("%Y%m%d-%H%M%S"))
        print(f"  {r.status.upper()}" + (f": {'; '.join(r.reasons)}" if r.reasons else ""), flush=True)
        results.append(r)
    before = previous_statuses(cfg, out)
    payload = {"snapshot": str(snap), "max_games": max_games, "thresholds": thresholds,
               "cases": [asdict(r) for r in results]}
    (out / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_report(cfg, out, results, manifest, thresholds, max_games)
    write_findings(out, results, before)
    print(f"report: {out / 'report.md'}")
    bad = [r for r in results if r.status not in ("pass",)]
    return 1 if bad else 0


def cmd_list(cfg: Config, a: argparse.Namespace) -> int:
    specs = {s.name: s for s in all_specs(cfg)}
    for s in specs.values():
        print(f"{s.name:<32} {s.layout:<9} stage {s.stage:<4} {s.description}")
    man = load_manifest(cfg).get("case", [])
    if man:
        todo = [c for c in man if c["id"] not in specs]
        print(f"\nmanifest: {len(man)} case(s); {len(man) - len(todo)} with a spec, {len(todo)} still to write")
    return 0


def cmd_lint(cfg: Config, a: argparse.Namespace) -> int:
    problems = []
    specs = {}
    for p in sorted(cfg.cases_dir.glob("*.toml")) if cfg.cases_dir.is_dir() else []:
        try:
            s = load_spec(p, cfg.timeout)
            if s.name in specs:
                problems.append(f"{p.name}: case name {s.name} also in {specs[s.name].path.name}")
            specs[s.name] = s
        except SpecError as e:
            problems.append(str(e))
    man = load_manifest(cfg).get("case", [])
    ids = set()
    for c in man:
        for key in ("id", "stage", "layout", "kind"):
            if key not in c:
                problems.append(f"manifest: a case lacks `{key}`: {c}")
        if c.get("id") in ids:
            problems.append(f"manifest: {c.get('id')} twice")
        ids.add(c.get("id"))
        if c.get("kind") not in ("required", "unsupported", "accepted-failure"):
            problems.append(f"manifest: {c.get('id')}: kind must be required, unsupported or accepted-failure")
        if c.get("layout") and not LAYOUT.match(c["layout"]) and c["layout"] not in MANIFEST_ONLY_LAYOUTS:
            problems.append(f"manifest: {c.get('id')}: unknown layout {c['layout']}")
        if c.get("kind") in ("accepted-failure", "unsupported") and not c.get("reason"):
            problems.append(f"manifest: {c.get('id')}: an {c['kind']} case needs a reason")
        if c.get("spec") and c.get("id") not in specs:
            problems.append(f"manifest: {c.get('id')}: spec = true but tests/runtime has no such case")
        if c.get("id") in specs and not c.get("spec"):
            problems.append(f"manifest: {c.get('id')}: has a spec; mark it spec = true")
        if c.get("id") in specs and specs[c["id"]].layout != c.get("layout"):
            problems.append(f"manifest: {c.get('id')}: layout {c.get('layout')} but its spec says {specs[c['id']].layout}")
    for name in specs:
        if man and name not in ids:
            problems.append(f"{name}: a spec with no manifest entry")
    for p in problems:
        print(p)
    print(f"{len(specs)} spec(s), {len(man)} manifest case(s), {len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="The Skylanders online forks' test runner.")
    p.add_argument("--repo", default=".", help="the game checkout (default: the current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("cases", nargs="*", help="case names or globs; none or `all` runs every case")
    t.add_argument("--stage", default=None, help="only the cases of this stage")
    t.add_argument("--max-games", type=int, default=0, help="most games at once (default: from cores and memory)")
    t.set_defaults(fn=cmd_test)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("lint").set_defaults(fn=cmd_lint)
    f = sub.add_parser("fixtures")
    f.add_argument("--from-owner", action="store_true")
    f.set_defaults(fn=cmd_fixtures)
    a = p.parse_args(argv)
    cfg = load_config(repo_root(Path(a.repo)))
    return a.fn(cfg, a)


if __name__ == "__main__":
    sys.exit(main())
