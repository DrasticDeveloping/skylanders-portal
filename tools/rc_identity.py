"""The release candidate's identity record (Swap Force Online stage 8 item 9, design 3.9, D13): one
commit, one `just package-installer` run, one installer, and this record of what they were.

    python -I tools/rc_identity.py --repo <game checkout> --installer <setup .exe>
        --record <payload record .json> --out <identity .json>
        [--preset win-amd64-release] [--config Release] [--iscc <ISCC.exe>] [--compiler <cl or clang path>]

It records: the installer's SHA-256 and size; the payload record's own SHA-256 (the remote agent is
started with it) and its file count; the commit and whether the tree had changes (a dirty tree is
refused: an RC is one commit); `git submodule status --recursive` (the portal, the SDK and every
nested submodule); the build preset and configuration; the compiler's and Inno Setup's version lines;
the SHA-256 of config/default.toml as committed (the codegen input) and the SDK commit (the codegen tool). Nothing
from the disc or generated/ is read or stored; only hashes and versions. The text is plain JSON
without game data, so it can go into the go section as it is.

Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


class IdentityError(Exception):
    pass


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise IdentityError(f"git {' '.join(args)}: {r.stderr.strip() or r.returncode}")
    return r.stdout.rstrip()  # leading characters count (porcelain status, submodule +/-/U)


def committed_sha256(repo: Path, rel: str) -> str:
    """SHA-256 of a file as committed at HEAD (the same on a CRLF and an LF checkout); "" if absent."""
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"HEAD:{rel}"], capture_output=True)
    return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else ""


def first_line(cmd: list[str]) -> str:
    """A tool's version line ("" when it cannot be run); never fails the record by itself."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        if line.strip():
            return line.strip()
    return ""


def dirty_paths(status: str, allowed: tuple[str, ...]) -> list[str]:
    """`git status --porcelain` lines that are changes, beyond the files a build rewrites (allowed)."""
    out = []
    for line in status.splitlines():
        path = line[3:].strip().strip('"')
        if path and path.replace("\\", "/") not in allowed:
            out.append(line)
    return out


# Files a configure or codegen run rewrites in the game checkout; never part of the payload.
BUILD_REWRITES = ("generated/rexglue.cmake", "swapforcerecomp_manifest.toml")


def make_identity(repo: Path, installer: Path, record: Path, preset: str, config: str,
                  iscc: str | None, compiler: str | None) -> dict:
    for p, what in ((installer, "installer"), (record, "payload record")):
        if not p.is_file():
            raise IdentityError(f"the {what} {p} does not exist")
    rec = json.loads(record.read_text(encoding="utf-8"))
    if rec.get("format") != 1 or not isinstance(rec.get("files"), list):
        raise IdentityError(f"{record}: not a format 1 payload record")
    status = git(repo, "status", "--porcelain", "--untracked-files=no")
    dirty = dirty_paths(status, BUILD_REWRITES)
    if dirty:
        raise IdentityError("the checkout has changes (an RC is one commit): " + "; ".join(dirty))
    inst_sha, inst_size = sha256_file(installer)
    rec_sha, _ = sha256_file(record)
    sdk = repo / "thirdparty" / "rexglue-sdk"
    identity = {
        "format": 1,
        "commit": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "submodules": git(repo, "submodule", "status", "--recursive").splitlines(),
        "installer": {"name": installer.name, "sha256": inst_sha, "size": inst_size},
        "payload_record": {"name": record.name, "sha256": rec_sha, "files": len(rec["files"])},
        "build": {"preset": preset, "config": config},
        "tools": {
            "compiler": first_line([compiler]) if compiler else "",
            "inno_setup": first_line([iscc, "/?"]) if iscc else "",
            "python": sys.version.split()[0],
        },
        "codegen": {
            "config_default_toml_sha256": committed_sha256(repo, "config/default.toml"),
            "sdk_commit": git(sdk, "rev-parse", "HEAD") if (sdk / ".git").exists() else "",
        },
    }
    return identity


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--installer", required=True)
    ap.add_argument("--record", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--preset", default="win-amd64-release")
    ap.add_argument("--config", default="Release")
    ap.add_argument("--iscc", default=None)
    ap.add_argument("--compiler", default=None)
    a = ap.parse_args(argv)
    try:
        ident = make_identity(Path(a.repo).resolve(), Path(a.installer), Path(a.record), a.preset, a.config,
                              a.iscc, a.compiler)
    except IdentityError as e:
        print(f"rc identity: {e}", file=sys.stderr)
        return 1
    text = json.dumps(ident, indent=1, sort_keys=True)
    Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    print(f"rc identity: commit {ident['commit'][:12]}, installer sha256 {ident['installer']['sha256']}, "
          f"payload record sha256 {ident['payload_record']['sha256']} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
