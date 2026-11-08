"""The Windows Sandbox configuration for installer-clean (Swap Force Online stage 8 item 8b, design
3.8): a .wsb that maps only the check's inputs, read-only, and one fresh output folder, with
networking off.

    python -I tools/sandbox_config.py --out <file.wsb> --output <fresh folder in .replication-tests>
        --installer-dir <folder with the RC's installer> --tools <copy of the RC commit's test tools>
        --python <the Python install folder> --figures <fixture figures copy> --disc <the disc folder>
        [--logon <command run at logon inside the sandbox>]

Refused: a mapped folder inside an owner data folder (the user's Documents, OneDrive Documents, the
registry's Personal folder, each Skylanders folder: launch_args.owner_data_roots), a repository
folder (one with .git) among the read-only inputs, the same folder twice, an output folder that is
not empty, a folder that does not exist. Inside the sandbox each mapped folder is
C:\\Users\\WDAGUtilityAccount\\Desktop\\<name>.

The disc folder is mapped read-only; the installer's own copy of it lives only inside the sandbox and
is gone when it closes (authorization A3 covers that copy). Standard library only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent / "skytest"))
import launch_args  # noqa: E402

SANDBOX_DESKTOP = r"C:\Users\WDAGUtilityAccount\Desktop"
INPUTS = ("installer", "tools", "python", "figures", "disc")


class ConfigError(Exception):
    pass


def _within(path: Path, folder: Path) -> bool:
    return launch_args.within(path, folder)


def build_wsb(inputs: dict[str, Path], output: Path, logon: str | None = None,
              owners: list[Path] | None = None) -> str:
    owners = launch_args.owner_data_roots() if owners is None else owners
    mapped: list[tuple[str, Path, bool]] = []
    for name in INPUTS:
        if name not in inputs:
            raise ConfigError(f"--{name.replace('installer', 'installer-dir')} is required")
        mapped.append((name, inputs[name], True))
    mapped.append(("output", output, False))
    seen: list[Path] = []
    for name, folder, read_only in mapped:
        if not folder.is_dir():
            raise ConfigError(f"{name}: {folder} is not a folder")
        real = folder.resolve()
        for o in owners:
            if _within(real, o) or _within(o, real):
                raise ConfigError(f"{name}: {real} is or holds an owner data folder ({o})")
        if read_only and (real / ".git").exists():
            raise ConfigError(f"{name}: {real} is a repository folder (copy what the check needs instead)")
        for s in seen:
            if _within(real, s) or _within(s, real):
                raise ConfigError(f"{name}: {real} overlaps another mapped folder ({s})")
        seen.append(real)
    if any(output.iterdir()):
        raise ConfigError(f"output: {output} is not empty (each check gets a fresh folder)")
    lines = ["<Configuration>", "  <Networking>Disable</Networking>", "  <vGPU>Enable</vGPU>",
             "  <ClipboardRedirection>Disable</ClipboardRedirection>", "  <PrinterRedirection>Disable</PrinterRedirection>",
             "  <AudioInput>Disable</AudioInput>", "  <VideoInput>Disable</VideoInput>", "  <MappedFolders>"]
    for name, folder, read_only in mapped:
        lines += ["    <MappedFolder>",
                  f"      <HostFolder>{escape(str(folder.resolve()))}</HostFolder>",
                  f"      <SandboxFolder>{escape(SANDBOX_DESKTOP + chr(92) + name)}</SandboxFolder>",
                  f"      <ReadOnly>{'true' if read_only else 'false'}</ReadOnly>",
                  "    </MappedFolder>"]
    lines.append("  </MappedFolders>")
    if logon:
        lines += ["  <LogonCommand>", f"    <Command>{escape(logon)}</Command>", "  </LogonCommand>"]
    lines.append("</Configuration>")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--installer-dir", required=True)
    ap.add_argument("--tools", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--figures", required=True)
    ap.add_argument("--disc", required=True)
    ap.add_argument("--logon", default=None)
    a = ap.parse_args(argv)
    inputs = {"installer": Path(a.installer_dir), "tools": Path(a.tools), "python": Path(a.python),
              "figures": Path(a.figures), "disc": Path(a.disc)}
    try:
        text = build_wsb(inputs, Path(a.output), a.logon)
    except ConfigError as e:
        print(f"sandbox config: {e}", file=sys.stderr)
        return 1
    Path(a.out).write_text(text, encoding="utf-8", newline="\r\n")
    print(f"sandbox config: {a.out} (networking off, {len(INPUTS)} read-only folders, one output folder)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
