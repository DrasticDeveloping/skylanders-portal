"""The installer's contents check (stage 8 item 8, design 3.8): nothing ships that the release-file
manifest does not name, and the RC's payload record says exactly what shipped.

    python -I tools/package_check.py stage   --repo <game checkout>
    python -I tools/package_check.py check   --repo <game checkout> --record <payload record .json>
    python -I tools/package_check.py compare-installed --record <record> --app <install folder> [--user-file NAME]
    python -I tools/package_check.py compare-tmp --record <record> --seen <watcher listing .json>

The manifest is the game's `installer/release-files.toml`: one [[file]] per `Source:` line of the
`.iss`, with its `iss_source` (as the .iss names it, relative to installer/), `dest` ({app}, {app}\\sub
or {tmp}), `provenance` (build:<target>, tracked, redist, licence), `type` (pe-x64-exe, pe-x64-dll,
toml, text), `from` (where `stage` copies a staging\\ entry from, relative to the checkout), an
optional launch `role` (game, synthetic-peer) and `extracted_to_tmp` for an {app} file the [Code]
also extracts to {tmp} (ExtractTemporaryFile).

`check`: (1) installer/staging holds exactly the manifest's staging entries, each of its declared type,
any other file fails whatever its name; every non-staging entry is a tracked file whose content is the
committed blob at HEAD; (2) every `Source:` line resolves to a manifest entry and every entry is named
by one (no wildcards); (3) it writes the payload record: each file's name, destination, size, SHA-256
and role, which the agent and installer-clean compare against.

`compare-tmp` keeps the watcher's fail-closed result (tools/tmp_payload_watch.py): a listing that is not
the watcher's format 1, that carries watcher errors, a payload file that changed while the watcher had
it, or an installer that exited with an error fails, and its final hashes must match the record too.

Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tomllib
from pathlib import Path

TYPES = ("pe-x64-exe", "pe-x64-dll", "toml", "text")
PROVENANCE = re.compile(r"build:[A-Za-z0-9_.-]+|tracked|redist|licence")
ROLES = ("game", "synthetic-peer")
FORBIDDEN_SUFFIXES = (".xex", ".iso", ".xiso", ".sky", ".bin", ".god")
SOURCE_LINE = re.compile(r'^\s*Source:\s*(?:"([^"]+)"|([^";]+?))\s*;(.*)$', re.IGNORECASE)
DEST = re.compile(r'DestDir:\s*"([^"]+)"', re.IGNORECASE)


class CheckError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pe_kind(data: bytes) -> str | None:
    """pe-x64-exe or pe-x64-dll by the PE header (MZ, PE\\0\\0, machine 0x8664, the DLL flag)."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if off + 24 > len(data) or data[off:off + 4] != b"PE\0\0":
        return None
    machine, = struct.unpack_from("<H", data, off + 4)
    chars, = struct.unpack_from("<H", data, off + 22)
    if machine != 0x8664:
        return None
    return "pe-x64-dll" if chars & 0x2000 else "pe-x64-exe"


def type_ok(kind: str, data: bytes) -> bool:
    if kind.startswith("pe-"):
        return pe_kind(data) == kind
    if b"\0" in data or data[:2] == b"MZ":
        return False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if kind == "toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return False
    return True


def norm_source(s: str) -> str:
    return s.replace("/", "\\").lower()


def installed_name(entry: dict) -> str:
    """The file's path under its destination: {app}\\routes + unsupported.toml -> routes\\unsupported.toml."""
    base = entry["iss_source"].replace("/", "\\").rsplit("\\", 1)[-1]
    dest = entry["dest"]
    sub = dest.split("\\", 1)[1] if "\\" in dest else ""
    return f"{sub}\\{base}" if sub else base


def load_manifest(path: Path) -> list[dict]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    files = data.get("file")
    if not isinstance(files, list) or not files:
        raise CheckError(f"{path}: no [[file]] entries")
    seen: set[tuple[str, str]] = set()
    roles: set[str] = set()
    for f in files:
        src = f.get("iss_source", "")
        if not src or "*" in src or "?" in src or ".." in src.replace("/", "\\").split("\\"):
            raise CheckError(f"{path}: iss_source {src!r} (explicit names only)")
        dest = f.get("dest", "")
        if not re.fullmatch(r"\{app\}(\\[A-Za-z0-9_.-]+)*|\{tmp\}", dest):
            raise CheckError(f"{path}: {src}: dest {dest!r} is {{app}}[\\sub] or {{tmp}}")
        if not PROVENANCE.fullmatch(str(f.get("provenance", ""))):
            raise CheckError(f"{path}: {src}: provenance build:<target>, tracked, redist or licence")
        if f.get("type") not in TYPES:
            raise CheckError(f"{path}: {src}: type one of {', '.join(TYPES)}")
        staged = norm_source(src).startswith("staging\\")
        if staged != str(f["provenance"]).startswith("build:"):
            raise CheckError(f"{path}: {src}: build outputs come from staging\\, nothing else does")
        if staged and not f.get("from"):
            raise CheckError(f"{path}: {src}: a staging entry names where it is copied from")
        role = f.get("role")
        if role is not None:
            if role not in ROLES or role in roles or dest != "{app}" or f["type"] != "pe-x64-exe":
                raise CheckError(f"{path}: {src}: role {role!r} (one game, one synthetic-peer, exes in {{app}})")
            roles.add(role)
        key = (norm_source(src), dest.lower())
        if key in seen:
            raise CheckError(f"{path}: {src} -> {dest} twice")
        seen.add(key)
    return files


def iss_sources(iss: Path) -> list[tuple[str, str]]:
    out = []
    section = ""
    for no, line in enumerate(iss.read_text(encoding="utf-8-sig").splitlines(), 1):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
            continue
        if section != "files" or not s or s.startswith(";"):
            continue
        # Every [Files] line is read, quoted or not: a line this check cannot read is a failure, not
        # a line it skips (an unquoted wildcard Source would otherwise ship unchecked).
        m = SOURCE_LINE.match(line)
        if not m:
            raise CheckError(f"{iss}:{no}: a [Files] line that does not start with Source: {s}")
        src = m.group(1) if m.group(1) is not None else m.group(2)
        d = DEST.search(m.group(3)) or re.search(r'DestDir:\s*([^";]+?)\s*(?:;|$)', m.group(3), re.IGNORECASE)
        if not d:
            raise CheckError(f"{iss}:{no}: a Source line without DestDir")
        if "*" in src or "?" in src:
            raise CheckError(f"{iss}:{no}: a wildcard Source ({src}): every file is named")
        out.append((src, d.group(1)))
    return out


def worktree_blob(repo: Path, rel: str) -> str:
    """The blob git would store for the file as it is (its eol and clean rules applied), so a CRLF
    checkout of an LF blob compares equal and any change of content does not."""
    r = subprocess.run(["git", "-C", str(repo), "hash-object", f"--path={rel}", "--", rel],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def committed_blob(repo: Path, rel: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", f"HEAD:{rel}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def check(repo: Path, manifest: Path, iss: Path, staging: Path) -> tuple[list[dict], list[str]]:
    """Returns the payload record's file rows and the failures (empty: the package may be built)."""
    files = load_manifest(manifest)
    errors: list[str] = []
    installer = manifest.parent
    want_staged = {norm_source(f["iss_source"]).split("\\", 1)[1] for f in files
                   if norm_source(f["iss_source"]).startswith("staging\\")}
    if staging.is_dir():
        for root, dirs, names in os.walk(staging):
            for n in names + [d for d in dirs if Path(root, d).is_symlink()]:
                rel = os.path.relpath(Path(root) / n, staging).replace("/", "\\").lower()
                if rel.endswith(FORBIDDEN_SUFFIXES):
                    errors.append(f"staging\\{rel}: a game or disc file is never packaged")
                elif rel not in want_staged:
                    errors.append(f"staging\\{rel}: not in the release-file manifest")
    rows = []
    for f in files:
        src = f["iss_source"]
        if norm_source(src).startswith("staging\\"):
            p = staging / src.replace("\\", os.sep).split(os.sep, 1)[1]  # the folder being checked
        else:
            p = installer / src.replace("\\", os.sep)
        if not p.is_file():
            errors.append(f"{src}: missing")
            continue
        data = p.read_bytes()
        if not type_ok(f["type"], data):
            errors.append(f"{src}: not a {f['type']} file")
        if not str(f["provenance"]).startswith("build:"):
            rel = os.path.relpath(p, repo).replace(os.sep, "/")
            want = committed_blob(repo, rel)
            have = worktree_blob(repo, rel)
            if want is None:
                errors.append(f"{src}: not a tracked file at HEAD")
            elif want != have:
                errors.append(f"{src}: differs from the committed file (blob {have[:12]}, HEAD {want[:12]})")
        row = {"name": installed_name(f), "iss_source": src, "dest": "{app}" if f["dest"].startswith("{app}") else "{tmp}",
               "size": len(data), "sha256": sha256(data)}
        if f.get("role"):
            row["role"] = f["role"]
        if f.get("extracted_to_tmp"):
            row["extracted_to_tmp"] = True
        rows.append(row)
    lines = iss_sources(iss) if iss.is_file() else []
    if not iss.is_file():
        errors.append(f"{iss}: missing")
    keys = {(norm_source(f["iss_source"]), f["dest"].lower()) for f in files}
    named = set()
    for src, dest in lines:
        k = (norm_source(src), dest.lower())
        if k not in keys:
            errors.append(f"{iss.name}: Source {src} -> {dest} is not in the release-file manifest")
        named.add(k)
    for k in sorted(keys - named):
        errors.append(f"release-files.toml: {k[0]} -> {k[1]} has no Source line in {iss.name}")
    return rows, errors


def write_record(rows: list[dict], path: Path, extra: dict | None = None) -> str:
    text = json.dumps({"format": 1, **(extra or {}), "files": rows}, indent=1, sort_keys=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return sha256(text.encode("utf-8"))


def stage(repo: Path, manifest: Path, staging: Path) -> list[str]:
    """Copies exactly the manifest's staging entries (never a wildcard of the build folder)."""
    files = load_manifest(manifest)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    copied = []
    for f in files:
        src = norm_source(f["iss_source"])
        if not src.startswith("staging\\"):
            continue
        origin = repo / f["from"]
        if not origin.is_file():
            raise CheckError(f"{f['from']}: missing (build it first)")
        dest = staging / f["iss_source"].replace("\\", os.sep).split(os.sep, 1)[1]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, dest)
        copied.append(str(dest))
    return copied


def load_record(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != 1:
        raise CheckError(f"{path}: not a format 1 payload record")
    return data["files"]


def compare_installed(record: list[dict], app: Path, user_files: tuple[str, ...] = ()) -> list[str]:
    """Every {app} file of the record in the install with its hash; any other file fails, beyond what
    the installer makes for the user (rom\\, the settings file, unins000.*)."""
    errors = []
    want = {r["name"].lower(): r for r in record if r["dest"] == "{app}"}
    for name, r in want.items():
        p = app / r["name"].replace("\\", os.sep)
        if not p.is_file():
            errors.append(f"{r['name']}: missing from the install")
        elif sha256(p.read_bytes()) != r["sha256"]:
            errors.append(f"{r['name']}: another hash than the record")
    allowed = {u.lower() for u in user_files}
    for root, dirs, names in os.walk(app):
        rel_root = os.path.relpath(root, app)
        if rel_root.split(os.sep)[0].lower() == "rom":
            continue
        for n in names:
            rel = os.path.normpath(os.path.join(rel_root, n)).replace(os.sep, "\\").lower()
            if rel in want or rel in allowed or re.fullmatch(r"unins\d{3}\.(exe|dat|msg)", rel):
                continue
            errors.append(f"{rel}: in the install but not in the record")
    return errors


def compare_tmp(record: list[dict], seen: dict[str, dict | None], allowed_extra: dict[str, str] | None = None) -> list[str]:
    """The installer's temporary payload as the watcher saw it ({name: {size, sha256}}, or None for a
    file seen but gone before it was hashed) against the record: every file setup put in {tmp} matches
    a record entry of that name (a {tmp} entry, or an {app} entry the [Code] extracts there), every
    {tmp} entry was seen; the only other files allowed are named in `allowed_extra` (Inno Setup's own
    helpers and the files the [Code] writes), each listed with its hash in the report."""
    errors = []
    want: dict[str, dict] = {}
    for r in record:
        if r["dest"] == "{tmp}" or r.get("extracted_to_tmp"):
            want[r["name"].rsplit("\\", 1)[-1].lower()] = r
    extra = {k.lower() for k in (allowed_extra or {})}
    for name, got in seen.items():
        key = name.replace("/", "\\").lower()
        if got is None:
            errors.append(f"{name}: seen, not hashed")
            continue
        if key in want:
            r = want[key]
            if got.get("size") != r["size"] or got.get("sha256") != r["sha256"]:
                errors.append(f"{name}: another hash than the record")
        elif key in extra:
            continue
        else:
            errors.append(f"{name}: in {{tmp}} but not in the record")
    seen_keys = {n.replace("/", "\\").lower() for n in seen}
    for key, r in want.items():
        if r["dest"] == "{tmp}" and key not in seen_keys:
            errors.append(f"{r['name']}: a {{tmp}} entry setup never put there")
    return errors


SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def check_listing(listing: object) -> list[str]:
    """The watcher's listing (tools/tmp_payload_watch.py Listing.result) as compare-tmp needs it: its
    format first (a listing that does not read is refused whole, `bad format: ...`), then what the
    watcher found itself: its errors, a payload file that changed while it had it (also when the errors
    leave it out), and an installer that exited with an error."""
    if not isinstance(listing, dict) or listing.get("format") != 1:
        return ["bad format: not a format 1 watcher listing"]
    seen, extra = listing.get("seen"), listing.get("allowed_extra", {})
    errors, changed = listing.get("errors"), listing.get("changed")
    if not isinstance(seen, dict) or not all(
            isinstance(k, str) and (v is None or (isinstance(v, dict) and isinstance(v.get("size"), int)
                                                  and not isinstance(v.get("size"), bool)
                                                  and isinstance(v.get("sha256"), str)
                                                  and SHA256_HEX.fullmatch(v["sha256"])))
            for k, v in seen.items()):
        return ["bad format: `seen` is not {name: {size, sha256} or null}"]
    if not isinstance(extra, dict) or not all(isinstance(k, str) and (v is None or isinstance(v, str))
                                              for k, v in extra.items()):
        return ["bad format: `allowed_extra` is not {name: hash, text or null}"]
    if not isinstance(errors, list) or not all(isinstance(e, str) for e in errors):
        return ["bad format: `errors` is not a list of texts (the watcher always writes one)"]
    if not isinstance(changed, dict) or not all(isinstance(k, str) for k in changed):
        return ["bad format: `changed` is not {name: [first, last]} (the watcher always writes one)"]
    exit_code = listing.get("installer_exit")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        return ["bad format: `installer_exit` is not a number or null"]
    out = [f"the watcher: {e}" for e in errors]
    allowed = {k.replace("/", "\\").lower() for k in extra}
    for name in changed:
        if name.replace("/", "\\").lower() not in allowed and not any(e.startswith(f"{name}: changed") for e in errors):
            out.append(f"{name}: changed while the watcher had it (the bytes setup ran may differ from the "
                       "ones compared)")
    if exit_code not in (None, 0):
        out.append(f"the installer exited {exit_code}")
    return out


def compare_listing(record: list[dict], listing: object) -> list[str]:
    """compare-tmp: the listing's own checks (check_listing) and, when its format reads, its final
    hashes against the record (compare_tmp); both must pass."""
    errors = check_listing(listing)
    if errors and errors[0].startswith("bad format"):
        return errors
    return errors + compare_tmp(record, listing["seen"], listing.get("allowed_extra", {}))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("stage", "check"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--manifest", default="installer/release-files.toml")
        p.add_argument("--staging", default="installer/staging")
        p.add_argument("--iss", default=None, help="default: the one .iss in installer/")
        if name == "check":
            p.add_argument("--record", required=True)
    p = sub.add_parser("compare-installed")
    p.add_argument("--record", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--user-file", action="append", default=[])
    p = sub.add_parser("compare-tmp")
    p.add_argument("--record", required=True)
    p.add_argument("--seen", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd in ("stage", "check"):
            repo = Path(a.repo).resolve()
            manifest = repo / a.manifest
            staging = repo / a.staging
            if a.cmd == "stage":
                for c in stage(repo, manifest, staging):
                    print(f"staged {c}")
                return 0
            iss = repo / a.iss if a.iss else next(iter(sorted((repo / "installer").glob("*.iss"))), repo / "installer" / "missing.iss")
            rows, errors = check(repo, manifest, iss, staging)
            for e in errors:
                print(f"package check: {e}", file=sys.stderr)
            if errors:
                return 1
            digest = write_record(rows, Path(a.record))
            print(f"package check: {len(rows)} files as the manifest names them; record {a.record} sha256 {digest}")
            return 0
        record = load_record(Path(a.record))
        if a.cmd == "compare-installed":
            errors = compare_installed(record, Path(a.app), tuple(a.user_file))
        else:
            try:
                listing = json.loads(Path(a.seen).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise CheckError(f"{a.seen}: the watcher listing does not read ({e})") from None
            errors = compare_listing(record, listing)
        for e in errors:
            print(f"payload: {e}", file=sys.stderr)
        print("payload: matches the record" if not errors else f"payload: {len(errors)} differences")
        return 1 if errors else 0
    except CheckError as e:
        print(f"package check: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
