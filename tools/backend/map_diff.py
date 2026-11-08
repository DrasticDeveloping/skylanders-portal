#!/usr/bin/env python3
"""map_diff: the backend's map dumps and compare lines, read from dump files or game logs.

The stdlib twin of the backend library's ParseMap, DiffMaps and JoinCompare (src/backend/map_dump.h
and compare.h; stage 2 design sections 7.1 and 7.2). The identity census report (stage 2 item 6)
and stage 3's cases use it on game logs; the library's own tests use the C++ side
(`skylanders_backend_test --diff`), and this tool's test (MD3, test_map_diff.py) holds the two to
the same answers on the golden files.

    map_diff.py diff A B [--a-index N] [--b-index N]
        Diffs two map dumps. A and B are dump files (DumpMap's text) or game logs, whose
        `Online: map ...` lines and the dump lines after them make one dump per header; the index
        picks one (0 the first, -1 the last, the default). Prints one line per differing row
        ("<kind> <entity> slot <n> <what> a <...> b <...>") and then
        "map diff <who a> <who b>: <n> differences", exactly as `skylanders_backend_test --diff`
        does. Exit 0 when equal, 1 when they differ, 2 when a dump cannot be read.

    map_diff.py join FILE [FILE ...] [--check]
        Joins the `backend local` lines of S (peer 0) with each client's, over the union of their
        keys, and prints the `backend compare` and `backend compared` lines the library's
        JoinCompare and Summarize give. A key's last local line on a peer counts (a later query
        replaces an earlier one). With --check, the files' own `backend compare` and
        `backend compared` lines must be exactly the joined ones; exit 1 when they are not.

A game log line is found by its "Online: " (the game's prefix for the online code's lines); what
follows it is the library's line. Standard library only; run with `python -I`.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
U16, U32, U64 = (1 << 16) - 1, (1 << 32) - 1, (1 << 64) - 1
ONLINE = "Online: "


class MapError(ValueError):
    """A dump that does not parse: "line <n>: <what>", the library's ParseMap words."""


def value_digest(value: bytes) -> int:
    """record.h ValueDigest: FNV-1a 64 over the value's bytes."""
    h = FNV_OFFSET
    for b in value:
        h ^= b
        h = (h * FNV_PRIME) & U64
    return h


def hex64(v: int) -> str:
    return f"{v:016x}"


# --- map dumps -------------------------------------------------------------------------------------


@dataclass
class MapRow:
    kind: str
    entity: str
    slot: int = 0
    revision: int = 0
    lsn: int = 0
    epoch: int = 0
    writer: int = 0
    tomb: bool = False
    server_only: bool = False
    value_hex: str = ""  # "" for an empty value (written `-`)
    digest: int = 0


@dataclass
class MapDump:
    who: str = ""
    head: int = 0
    prefix: int = 0
    epoch: int = 0
    rows: int = 0
    bytes: int = 0
    table: int = 0
    row_lines: list[MapRow] = field(default_factory=list)
    other_lines: list[str] = field(default_factory=list)  # obligation, lease and peer lines


DECIMAL = re.compile(r"[0-9]+")
HEX = re.compile(r"[0-9a-fA-F]+")


def number(text: str, limit: int, base: int = 10) -> int | None:
    """std::from_chars into an unsigned type: digits only (no sign, no 0x), the whole token, in
    range."""
    if not (DECIMAL if base == 10 else HEX).fullmatch(text):
        return None
    v = int(text, base)
    return v if v <= limit else None


def split(line: str) -> list[str]:
    """map_dump.cpp Split: runs of spaces, tabs and '\\r' separate the tokens."""
    return [t for t in re.split(r"[ \t\r]+", line) if t]


def parse_lines(numbered: list[tuple[int, str]], last: int) -> MapDump:
    """ParseMap over (line number, line) pairs; `last` is the line number an end-of-dump error
    names (the library counts the empty segment after a final newline as a line)."""
    dump = MapDump()
    have_header = False
    n = last

    def fail(what: str) -> MapError:
        return MapError(f"line {n}: {what}")

    def field_at(t: list[str], i: int, word: str, limit: int, base: int = 10) -> int | None:
        if i + 1 >= len(t) or t[i] != word:
            return None
        return number(t[i + 1], limit, base)

    for n, line in numbered:
        t = split(line)
        if not t:
            continue
        if t[0] == "map":
            if have_header:
                raise fail("a second map header")
            i = 1
            if i < len(t) and t[i] == "S":
                dump.who, i = "S", i + 1
            elif i + 1 < len(t) and t[i] == "peer":
                if number(t[i + 1], U16) is None:
                    raise fail("peer number")
                dump.who, i = "peer " + t[i + 1], i + 2
            else:
                raise fail("who (S or peer <n>)")
            for word, limit, base in (("head", U64, 10), ("prefix", U64, 10), ("epoch", U32, 10),
                                      ("rows", U64, 10), ("bytes", U64, 10), ("table", U64, 16)):
                v = field_at(t, i, word, limit, base)
                if v is None:
                    raise fail(word)
                setattr(dump, word, v)
                i += 2
            if i != len(t):
                raise fail("words after the table digest")
            have_header = True
            continue
        if not have_header:
            raise fail("a line before the map header")
        if t[0] == "row":
            if len(t) < 3:
                raise fail("row kind and entity")
            row = MapRow(kind=t[1], entity=t[2])
            i = 3
            for word, attr, limit in (("slot", "slot", U16), ("rev", "revision", U32), ("lsn", "lsn", U64),
                                      ("epoch", "epoch", U32), ("writer", "writer", U16)):
                v = field_at(t, i, word, limit)
                if v is None:
                    raise fail(word)
                setattr(row, attr, v)
                i += 2
            if i < len(t) and t[i] == "tomb":
                row.tomb, i = True, i + 1
            if i < len(t) and t[i] == "server-only":
                row.server_only, i = True, i + 1
            if i + 1 >= len(t) or t[i] != "value":
                raise fail("value")
            value = b""
            if t[i + 1] != "-":
                if len(t[i + 1]) % 2 != 0 or not HEX.fullmatch(t[i + 1]):
                    raise fail("value hex")
                value = bytes.fromhex(t[i + 1])
                row.value_hex = t[i + 1].lower()
            i += 2
            digest = field_at(t, i, "digest", U64, 16)
            if digest is None:
                raise fail("digest")
            row.digest = digest
            i += 2
            if i != len(t):
                raise fail("words after the digest")
            if row.digest != value_digest(value):
                raise fail("digest is not the value's")
            dump.row_lines.append(row)
            continue
        if t[0] in ("obligation", "lease", "peer"):
            dump.other_lines.append(line.rstrip("\r ").lstrip(" "))
            continue
        raise fail(f"unknown line '{t[0]}'")
    n = last
    if not have_header:
        raise fail("no map header")
    if len(dump.row_lines) != dump.rows:
        raise fail(f"the header says rows {dump.rows}, the dump has {len(dump.row_lines)}")
    return dump


def parse_map(text: str) -> MapDump:
    """ParseMap on a dump's text (DumpMap's output); raises MapError with the library's words."""
    segments = text.split("\n")
    numbered = list(enumerate(segments, start=1))
    return parse_lines(numbered, len(segments))


# A dump's lines inside a game log, after "Online: ". Other online lines share the first words
# ("peer 2 gone at close"), so each kind of line is told by its own second or third word.
DUMP_LINE = re.compile(r"^(?:map (?:S|peer \d+) head |row \S+ \S+ slot |obligation \d+ to |"
                       r"lease \d+ \S+ \S+ holder |peer \d+ cursor )")


def dumps_in_log(text: str) -> list[MapDump]:
    """Every dump in a game log, in order: an `Online: map` header and the dump lines after it, up
    to the next header. Errors name the log's line numbers."""
    groups: list[list[tuple[int, str]]] = []
    for n, raw in enumerate(text.split("\n"), start=1):
        at = raw.find(ONLINE)
        if at < 0:
            continue
        line = raw[at + len(ONLINE):].rstrip("\r")
        if not DUMP_LINE.match(line):
            continue
        if line.startswith("map "):
            groups.append([])
        if groups:
            groups[-1].append((n, line))
    return [parse_lines(g, g[-1][0]) for g in groups]


def is_log(text: str) -> bool:
    return ONLINE in text


def load_dump(path: Path, index: int = -1) -> MapDump:
    """A dump file, or the index-th dump of a game log."""
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    if not is_log(text):
        return parse_map(text)
    dumps = dumps_in_log(text)
    if not dumps:
        raise MapError("no map dump in the log")
    try:
        return dumps[index]
    except IndexError:
        raise MapError(f"the log has {len(dumps)} map dump(s), no index {index}") from None


@dataclass
class MapDiff:
    what: str  # only-a, only-b, revision, tomb, value
    kind: str
    entity: str
    slot: int
    text: str


def diff_maps(a: MapDump, b: MapDump) -> list[MapDiff]:
    """DiffMaps: one difference per key at most (tomb, then revision, then value), in (kind, entity,
    slot) order; lsn, epoch and writer are not compared; server-only rows are left out."""
    rows: dict[tuple[str, str, int], list[MapRow | None]] = {}
    for r in a.row_lines:
        if not r.server_only:
            rows.setdefault((r.kind, r.entity, r.slot), [None, None])[0] = r
    for r in b.row_lines:
        if not r.server_only:
            rows.setdefault((r.kind, r.entity, r.slot), [None, None])[1] = r
    out = []
    # std::map orders std::string keys bytewise; UTF-8 bytes sort as their code points do.
    for key in sorted(rows):
        ra, rb = rows[key]
        if rb is None:
            what, av, bv = "only-a", f"rev {ra.revision}", "-"
        elif ra is None:
            what, av, bv = "only-b", "-", f"rev {rb.revision}"
        elif ra.tomb != rb.tomb:
            what = "tomb"
            av, bv = ("tomb" if ra.tomb else "live"), ("tomb" if rb.tomb else "live")
        elif ra.revision != rb.revision:
            what, av, bv = "revision", str(ra.revision), str(rb.revision)
        elif ra.digest != rb.digest or ra.value_hex != rb.value_hex:
            what, av, bv = "value", hex64(ra.digest), hex64(rb.digest)
        else:
            continue
        kind, entity, slot = key
        out.append(MapDiff(what, kind, entity, slot, f"{kind} {entity} slot {slot} {what} a {av} b {bv}"))
    return out


def diff_text(a: MapDump, b: MapDump) -> tuple[str, int]:
    """What `skylanders_backend_test --diff` prints, and its exit code."""
    diffs = diff_maps(a, b)
    lines = [d.text for d in diffs] + [f"map diff {a.who} {b.who}: {len(diffs)} differences"]
    return "\n".join(lines) + "\n", (0 if not diffs else 1)


# --- compare lines ---------------------------------------------------------------------------------

RESULTS = ("equal", "repaired", "pending", "stuck", "unsupported")
LOCAL = re.compile(r"^backend local (\d+) (\S+) (\S+) slot (\d+) rev (\d+) digest ([0-9a-f]{16}) "
                   r"(equal|repaired|pending|stuck|unsupported)(?: pending (\d+))?(?: differs (.*))?$")
COMPARE = re.compile(r"^backend compare (\d+) ")
COMPARED = re.compile(r"^backend compared (\d+) ")
# kStuck > kPending > kRepaired > kEqual (compare.cpp Rank).
RANK = {"equal": 0, "repaired": 1, "pending": 2, "stuck": 3}


@dataclass
class LocalLine:
    peer: int
    kind: str
    entity: str
    slot: int
    revision: int
    digest: str
    live: str
    pending_ms: int = 0
    differs: str = ""


@dataclass
class RowCompare:
    peer: int
    kind: str
    entity: str
    slot: int
    result: str
    stored_revision: int
    replica_revision: int
    differs: str = ""

    def line(self) -> str:
        """compare.cpp CompareLine."""
        out = (f"backend compare {self.peer} {self.kind} {self.entity} {self.result} "
               f"rev {self.stored_revision}/{self.replica_revision}")
        return out + (f" differs {self.differs}" if self.differs else "")


def library_lines(text: str) -> list[str]:
    """The library's lines in a text: after "Online: " in a game log, else every line as it is
    (a golden file); trailing '\\r' and spaces dropped."""
    out = []
    log = is_log(text)
    for raw in text.split("\n"):
        if log:
            at = raw.find(ONLINE)
            if at < 0:
                continue
            raw = raw[at + len(ONLINE):]
        out.append(raw.rstrip("\r "))
    return out


def parse_local(line: str) -> LocalLine | None:
    m = LOCAL.match(line)
    if not m:
        return None
    return LocalLine(peer=int(m[1]), kind=m[2], entity=m[3], slot=int(m[4]), revision=int(m[5]), digest=m[6],
                     live=m[7], pending_ms=int(m[8] or 0), differs=m[9] or "")


def side(name: str, c: LocalLine) -> str:
    return f"{name}: {c.differs or c.live}"


def join_compare(server: list[LocalLine], peer: list[LocalLine], peer_id: int) -> list[RowCompare]:
    """JoinCompare over the union of the keys (kind, entity, slot), in that order."""
    keys: dict[tuple[str, str, int], list[LocalLine | None]] = {}
    for c in server:
        keys.setdefault((c.kind, c.entity, c.slot), [None, None])[0] = c
    for c in peer:
        keys.setdefault((c.kind, c.entity, c.slot), [None, None])[1] = c
    out = []
    for key in sorted(keys):
        s, p = keys[key]
        row = RowCompare(peer=peer_id, kind=key[0], entity=key[1], slot=key[2], result="stuck",
                         stored_revision=s.revision if s else 0, replica_revision=p.revision if p else 0)
        if p is None:
            row.differs = "replica missing"
        elif s is None:
            row.differs = "replica extra"
        elif p.revision < s.revision:
            row.differs = "replica behind"
        elif p.revision != s.revision or p.digest != s.digest:
            row.differs = "replica differs"
        elif s.live == "unsupported" and p.live == "unsupported":
            row.result = "unsupported"
        elif s.live == "unsupported":
            row.differs = "server: unsupported"
        elif p.live == "unsupported":
            row.differs = "client: unsupported"
        else:
            row.result = s.live if RANK[s.live] >= RANK[p.live] else p.live
            if row.result != "equal":
                parts = []
                if s.live == row.result:
                    parts.append(side("server", s))
                if p.live == row.result:
                    parts.append(side("client", p))
                row.differs = ", ".join(parts)
        out.append(row)
    return out


def compared_line(peer: int, kind: str, rows: list[RowCompare]) -> str:
    """compare.cpp Summarize and ComparedLine for one kind."""
    counts = {r: 0 for r in RESULTS}
    for row in rows:
        if row.kind == kind:
            counts[row.result] += 1
    return f"backend compared {peer} {kind} " + " ".join(f"{r} {counts[r]}" for r in RESULTS)


def join_lines(lines: list[str]) -> list[str]:
    """The compare and compared lines for every client in the lines' local lines (peer 0 is S), per
    client in peer order and per kind in name order. A key's last local line on a peer counts."""
    latest: dict[int, dict[tuple[str, str, int], LocalLine]] = {}
    for line in lines:
        c = parse_local(line)
        if c is not None:
            latest.setdefault(c.peer, {})[(c.kind, c.entity, c.slot)] = c
    server = latest.get(0, {})
    out = []
    for peer_id in sorted(p for p in latest if p != 0):
        peer = latest[peer_id]
        for kind in sorted({k[0] for k in server} | {k[0] for k in peer}):
            rows = join_compare([c for k, c in server.items() if k[0] == kind],
                                [c for k, c in peer.items() if k[0] == kind], peer_id)
            out += [r.line() for r in rows] + [compared_line(peer_id, kind, rows)]
    return out


def logged_joins(lines: list[str]) -> list[str]:
    """The `backend compare` and `backend compared` lines a text already has."""
    return [line for line in lines if COMPARE.match(line) or COMPARED.match(line)]


# --- the command line ------------------------------------------------------------------------------


def cmd_diff(a: argparse.Namespace) -> int:
    dumps = []
    for path, index in ((Path(a.a), a.a_index), (Path(a.b), a.b_index)):
        try:
            dumps.append(load_dump(path, index))
        except OSError as e:
            print(f"cannot read {path}: {e}", file=sys.stderr)
            return 2
        except MapError as e:
            print(f"{path}: {e}", file=sys.stderr)
            return 2
    text, code = diff_text(dumps[0], dumps[1])
    sys.stdout.write(text)
    return code


def cmd_join(a: argparse.Namespace) -> int:
    lines: list[str] = []
    for f in a.files:
        try:
            lines += library_lines(Path(f).read_text(encoding="utf-8", errors="surrogateescape"))
        except OSError as e:
            print(f"cannot read {f}: {e}", file=sys.stderr)
            return 2
    joined = join_lines(lines)
    for line in joined:
        print(line)
    if not a.check:
        return 0
    logged = logged_joins(lines)
    if sorted(logged) == sorted(joined):
        print(f"join check: {len(joined)} line(s) agree")
        return 0
    for line in sorted(set(joined) - set(logged)):
        print(f"joined, not logged: {line}")
    for line in sorted(set(logged) - set(joined)):
        print(f"logged, not joined: {line}")
    print("join check: the logged lines are not the joined ones")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Diff the backend's map dumps and join its compare lines.")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diff", help="diff two map dumps (dump files or game logs)")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--a-index", type=int, default=-1, help="which dump of a log A is (default -1, the last)")
    d.add_argument("--b-index", type=int, default=-1, help="which dump of a log B is (default -1, the last)")
    d.set_defaults(fn=cmd_diff)
    j = sub.add_parser("join", help="join S's and the clients' backend local lines")
    j.add_argument("files", nargs="+")
    j.add_argument("--check", action="store_true", help="the files' own compare lines must be the joined ones")
    j.set_defaults(fn=cmd_join)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
