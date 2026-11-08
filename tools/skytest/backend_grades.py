"""Stage 3's backend grades for skytest (future/online-3.md section 3.7, "skytest"): compare, faults
and memory, graded on the game's recorded lines. Standard library only; skytest.py dispatches them
(GRADES, its spec check and grade_cross_peer). The lines are the game's (src/online/backend/
bridge_tools.h): `Online: backend local ...` (the library's LocalLine), `Online: backend fault <n>
<layer>:<action> ...`, `Online: backend memory <peer> log <b> ...`.

  [grade.backend_compare]  peer = "S" (whose result carries it), kinds = ["prop", "gate"],
      pending = [kinds allowed pending], unsupported = [kinds allowed unsupported],
      clients = ["H", "D"] (optional; default every client of the layout)
      Joins S's local lines (peer 0) with each expected client's over the union of their keys, as
      map_diff.py join and the library's JoinCompare do (a key's last line on a peer counts). A
      client's lines come from its own log only. Every row of a listed kind must be equal or
      repaired; pending only for a kind in `pending`, unsupported only for a kind in `unsupported`.
      A listed kind with no local line on S is "not reached", and so is a listed kind an expected
      client did not compare (neither a local line nor its `backend query compare` line of that
      kind in its log): every peer is compared, a silent one never passes.
  [grade.backend_faults]  peer = "H", rules = [1, 2], before = "<regex>" (optional)
      Each listed rule number fired on that peer (a fault line, not the `armed` line), and, with
      `before`, fired before the first line matching it (the grade's window opening); else "not
      reached".
  [grade.backend_memory]  peer = "S", bounds = {log = <n>, bridge_out = <n>, ...}, from = "<regex>"
      (optional, the steady phase's start), window = 10 (memory lines, about one a second)
      Every memory line from `from` on keeps each bounded counter within its bound, and no counter's
      window maximum rises across the last three whole windows of each memory peer (a queue that
      only grows). Fewer than three windows is "not reached".
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import map_diff  # noqa: E402

NAMES = ("backend_compare", "backend_faults", "backend_memory")
FAULT = re.compile(r"Online: backend fault (\d+) [a-z]+:[a-z]+\b")
MEMORY_FIELDS = ("log", "staging", "batcher", "held", "bridge_out", "bridge_in", "obligations", "requests")
MEMORY = re.compile(r"Online: backend memory (\d+) " + " ".join(f"{f} (\\d+)" for f in MEMORY_FIELDS) + r"\b")
OK_RESULTS = ("equal", "repaired")
# bridge_tools.cpp RunCompare's summary: a compare of a kind that may have no row at all.
QUERY = re.compile(r"^backend query compare (\d+) (\S+) rows (\d+) ")


class GradeSpecError(ValueError):
    pass


def check(spec: str, gname: str, g: object, names: list[str]) -> None:
    """A spec's backend grade table; GradeSpecError (skytest wraps it) for what it refuses."""
    if not isinstance(g, dict):
        raise GradeSpecError(f"{spec}: grade {gname} must be a table")
    peer = g.get("peer", "S" if gname != "backend_faults" else "H")
    if peer not in names:
        raise GradeSpecError(f"{spec}: grade {gname} names peer {peer}, not one of {names}")
    if gname == "backend_compare":
        kinds = g.get("kinds")
        if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
            raise GradeSpecError(f"{spec}: grade backend_compare needs `kinds`, a list of kind names")
        for key in ("pending", "unsupported"):
            if not isinstance(g.get(key, []), list):
                raise GradeSpecError(f"{spec}: grade backend_compare: `{key}` is a list of kind names")
        clients = g.get("clients", [n for n in names if n != "S"])
        if (not isinstance(clients, list) or not clients
                or not all(isinstance(c, str) and c in names and c != "S" for c in clients)):
            raise GradeSpecError(f"{spec}: grade backend_compare: `clients` is a list of the layout's clients"
                                 f" ({[n for n in names if n != 'S']})")
    elif gname == "backend_faults":
        rules = g.get("rules")
        if not isinstance(rules, list) or not rules or not all(isinstance(r, int) and r > 0 for r in rules):
            raise GradeSpecError(f"{spec}: grade backend_faults needs `rules`, rule numbers from 1")
        if "before" in g:
            re.compile(str(g["before"]))
    elif gname == "backend_memory":
        bounds = g.get("bounds", {})
        if not isinstance(bounds, dict) or any(k not in MEMORY_FIELDS for k in bounds):
            raise GradeSpecError(f"{spec}: grade backend_memory: `bounds` names counters of {MEMORY_FIELDS}")
        if not isinstance(g.get("window", 10), int) or g.get("window", 10) < 1:
            raise GradeSpecError(f"{spec}: grade backend_memory: `window` is a count of memory lines")
    else:
        raise GradeSpecError(f"{spec}: grade {gname} is not a backend grade")


def grade_compare(texts: dict[str, str], g: dict) -> tuple[dict, list[str]]:
    """`texts` holds every peer's log by name (skytest reads one for each peer of the layout, empty when
    it wrote none); the expected clients are `clients`, else every name but S."""
    kinds = list(g["kinds"])
    allow_pending = set(g.get("pending", []))
    allow_unsupported = set(g.get("unsupported", []))
    roster = list(g.get("clients", [n for n in texts if n != "S"]))
    server: dict[tuple[str, str, int], map_diff.LocalLine] = {}
    for text in texts.values():
        for line in map_diff.library_lines(text):
            c = map_diff.parse_local(line)
            if c is not None and c.peer == 0:
                server[(c.kind, c.entity, c.slot)] = c
    graded = {r: 0 for r in map_diff.RESULTS}
    graded["rows"] = 0
    reasons: list[str] = []
    # Each expected client's own lines: its local lines (last per key), its peer id, the kinds it compared.
    own: dict[str, dict[tuple[str, str, int], map_diff.LocalLine]] = {}
    ids: dict[str, int] = {}
    compared: dict[str, set] = {}
    for name in roster:
        own[name], compared[name] = {}, set()
        for line in map_diff.library_lines(texts.get(name, "")):
            c = map_diff.parse_local(line)
            if c is not None and c.peer != 0:
                own[name][(c.kind, c.entity, c.slot)] = c
                ids.setdefault(name, c.peer)
                compared[name].add(c.kind)
                continue
            m = QUERY.match(line)
            if m and m[1] != "0":
                ids.setdefault(name, int(m[1]))
                compared[name].add(m[2])
    if not roster:
        reasons.append("grade backend_compare: not reached (no client to compare)")
    for kind in kinds:
        if not any(k[0] == kind for k in server):
            reasons.append(f"grade backend_compare: not reached (no local compare of {kind} on S)")
            continue
        for name in roster:
            if kind not in compared[name]:
                reasons.append(f"grade backend_compare: not reached (client {name} has no compare of {kind})")
                continue
            peer_id = ids.get(name, 0)
            rows = map_diff.join_compare([c for k, c in server.items() if k[0] == kind],
                                         [c for k, c in own[name].items() if k[0] == kind], peer_id)
            for row in rows:
                graded["rows"] += 1
                graded[row.result] += 1
                ok = (row.result in OK_RESULTS or (row.result == "pending" and kind in allow_pending)
                      or (row.result == "unsupported" and kind in allow_unsupported))
                if not ok:
                    reasons.append(f"grade backend_compare: peer {peer_id} ({name}) {kind} {row.entity} slot {row.slot} "
                                   f"{row.result}" + (f" ({row.differs})" if row.differs else ""))
    return graded, reasons


def grade_faults(text: str, g: dict) -> tuple[dict, list[str]]:
    lines = text.splitlines()
    first: dict[int, int] = {}
    for i, line in enumerate(lines):
        m = FAULT.search(line)
        if m:
            first.setdefault(int(m.group(1)), i)
    before = None
    if "before" in g:
        pattern = re.compile(str(g["before"]))
        before = next((i for i, line in enumerate(lines) if pattern.search(line)), None)
    graded = {"fired": sorted(first)}
    reasons = []
    for rule in g["rules"]:
        if rule not in first:
            reasons.append(f"grade backend_faults: not reached (rule {rule} never fired)")
        elif before is not None and first[rule] > before:
            reasons.append(f"grade backend_faults: not reached (rule {rule} fired after the window opened)")
    return graded, reasons


def grade_memory(text: str, g: dict) -> tuple[dict, list[str]]:
    bounds: dict = g.get("bounds", {})
    window = int(g.get("window", 10))
    lines = text.splitlines()
    if "from" in g:
        pattern = re.compile(str(g["from"]))
        start = next((i for i, line in enumerate(lines) if pattern.search(line)), None)
        if start is None:
            return {}, [f"grade backend_memory: not reached (no line matches {g['from']!r})"]
        lines = lines[start:]
    series: dict[int, list[dict[str, int]]] = {}
    for line in lines:
        m = MEMORY.search(line)
        if m:
            series.setdefault(int(m.group(1)), []).append(
                {f: int(m.group(i + 2)) for i, f in enumerate(MEMORY_FIELDS)})
    reasons: list[str] = []
    graded: dict = {"peers": sorted(series), "max": {}}
    if not series:
        return graded, ["grade backend_memory: not reached (no memory line)"]
    for peer, rows in sorted(series.items()):
        top = {f: max(r[f] for r in rows) for f in MEMORY_FIELDS}
        graded["max"][str(peer)] = top
        for field, bound in bounds.items():
            if top[field] > int(bound):
                reasons.append(f"grade backend_memory: peer {peer} {field} {top[field]} over its bound {bound}")
        whole = len(rows) // window
        if whole < 3:
            reasons.append(f"grade backend_memory: not reached (peer {peer} has {whole} whole windows of "
                           f"{window} lines, 3 needed)")
            continue
        last = [rows[(whole - 3 + k) * window:(whole - 2 + k) * window] for k in range(3)]
        for field in MEMORY_FIELDS:
            peaks = [max(r[field] for r in w) for w in last]
            if peaks[0] < peaks[1] < peaks[2]:
                reasons.append(f"grade backend_memory: peer {peer} {field} rising over the last three windows "
                               f"({peaks[0]}, {peaks[1]}, {peaks[2]})")
    return graded, reasons


def grade(gname: str, texts: dict[str, str], g: dict, peer: str) -> tuple[dict, list[str]]:
    """One backend grade on the run's peer logs (`peer` the grade's own peer)."""
    if gname == "backend_compare":
        return grade_compare(texts, g)
    if gname == "backend_faults":
        return grade_faults(texts.get(peer, ""), g)
    return grade_memory(texts.get(peer, ""), g)
