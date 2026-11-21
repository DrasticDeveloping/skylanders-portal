"""The runner's refusal of a pointless rerun (skytest.py test and levelpass, before a case starts games).

A case whose last two results with the same case content both failed with the same first reason fails
a third time for that reason: 38 level passes did on 18 November 2026 (275 minutes of game time, the
same 60 s wait for a level end the pass could not reach), each on a new build. The runner looks at the
case's earlier results in the store's runs folder (each run folder's result.json) and, when that is so,
on any build, starts no game: the case's result is `known failure` with the reason, the two run folders
and their builds. `--rerun-known-failure "<why>"` runs it anyway and the result records the why (a new
build's legitimate why: "this build changes X, which is the cause").

The same case: the same case name, and either the same whole content (case_sha256: the spec's text, the
arguments, the fixtures) or, for a script cut into phases (the level pass: `log LEVELPASS phase <name>`
lines in H's script), the same part the failure belongs to: the phase H's log was in when the run ended
(its last `LEVELPASS phase` line), its script lines with the arguments and fixtures (part_sha256). A
generated pass's sweep changes with every edit of the generator and of the level data while its exit
does not: two failures at the exit are one known failure whatever the sweep was.

Pure code here (the caller reads the result files); unit-tested in tests/test_known_failures.py.

What counts:
- a result: a case that started its games and ended `pass`, `fail` or `error` (a capacity, not-ready,
  breaker, not-in-bulk, known-failure or build-wait result started none and says nothing);
- the build (named in the message only): the result's `provenance.build` (the snapshot name, build_hash),
  else the snapshot of its session's results.json for results written before the field existed;
- the case content: `provenance.case_sha256` (case_sha256: the spec's text, the arguments every peer
  gets, the fixtures), else the same hash made from the older provenance fields;
- the first reason: the first reason of the first peer that has one (as the owner's test watcher reads
  it), else the case's own first reason; time stamps, thread ids and numbers taken out (normal)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

KNOWN_FAILURE = "known failure"
RESULT_STATUSES = ("pass", "fail", "error", "incomplete")   # results of a case that started its games
FAILED = ("fail", "error", "incomplete")   # incomplete: a level pass that did not cover its plan (never a pass)
RUN_NAME = re.compile(r"^(\d{8}-\d{6})-(.+)$")


def normal(reason: str) -> str:
    """A reason without what differs from run to run: log time stamps, thread ids, numbers."""
    reason = re.sub(r"\[\d{4}-\d\d-\d\d[^\]]*\]", "", reason)
    reason = re.sub(r"\[t\d+\]", "", reason)
    reason = re.sub(r"0x[0-9A-Fa-f]+", "N", reason)
    reason = re.sub(r"\d+(\.\d+)?", "N", reason)
    return " ".join(reason.split())[:300]


def first_reason(result: dict) -> str:
    """The result's first reason, normalized: the first peer's that has one, else the case's own."""
    for peer in result.get("peers") or []:
        if not isinstance(peer, dict):
            continue
        for reason in peer.get("reasons") or []:
            return f"{peer.get('peer')}: {normal(str(reason))}"
    for reason in result.get("reasons") or []:
        return normal(str(reason))
    return ""


def case_sha256(spec_sha256: str, args_sha256: str, fixture_sha256: str) -> str:
    """The case content's hash: what the games run (the spec's text, the arguments every peer gets, the
    fixture saves and figures). '' when the spec's hash is unknown."""
    if not spec_sha256:
        return ""
    return hashlib.sha256(f"{spec_sha256}\n{args_sha256}\n{fixture_sha256}".encode("utf-8")).hexdigest()


@dataclass
class Prior:
    """One earlier result of a case."""
    folder: str
    started: str        # the run's stamp (YYYYMMDD-HHMMSS): the order
    status: str
    build: str
    case_hash: str
    reason: str         # first_reason
    part: tuple[str, str] | None = None   # (phase, part_sha256) the run ended in, for a phased script


PHASE_MARK = re.compile(r"^log LEVELPASS phase (\S+)")
LOG_PHASE = re.compile(r"Input script: LEVELPASS phase (\S+)")


def script_parts(script: str, args_sha: str = "", fixture_sha: str = "") -> dict[str, str]:
    """phase -> part_sha256 of a phased script (each phase's lines up to the next phase's mark, with the
    arguments' and fixtures' hashes); {} for a script with no phase marks."""
    parts: dict[str, list[str]] = {}
    phase = ""
    for line in script.splitlines():
        line = line.strip()
        m = PHASE_MARK.match(line)
        if m:
            phase = m.group(1)
            parts.setdefault(phase, [])
        if phase and line:
            parts[phase].append(line)
    return {p: hashlib.sha256(("\n".join(lines) + f"\n{args_sha}\n{fixture_sha}").encode("utf-8")).hexdigest()
            for p, lines in parts.items()}


def last_phase(log_text: str) -> str:
    """The phase a log was in when it ended: its last `LEVELPASS phase` line ('' for none)."""
    found = LOG_PHASE.findall(log_text)
    return found[-1] if found else ""


def prior_from_result(folder: str, result: dict, session_snapshot: str = "", h_script: str = "",
                      h_log: str = "") -> Prior:
    """A Prior from a run folder's result.json (and its session's snapshot for an older result; H's
    script and log for a phased script's failing part)."""
    prov = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    build = str(prov.get("build") or "")
    if not build and session_snapshot:
        build = re.split(r"[\\/]", session_snapshot.rstrip("\\/"))[-1]
    case_hash = str(prov.get("case_sha256") or "") or case_sha256(
        str(prov.get("spec_sha256") or ""), str(prov.get("args_sha256") or ""), str(prov.get("fixture_sha256") or ""))
    part = None
    phase = last_phase(h_log) if h_script and h_log else ""
    if phase:
        sha = script_parts(h_script, str(prov.get("args_sha256") or ""), str(prov.get("fixture_sha256") or "")).get(phase)
        part = (phase, sha) if sha else None
    return Prior(folder=folder, started=str(result.get("started") or ""), status=str(result.get("status") or ""),
                 build=build, case_hash=case_hash, reason=first_reason(result), part=part)


@dataclass
class Refusal:
    reason: str
    folders: list[str]
    build: str                  # this run's build
    builds: list[str] = field(default_factory=list)   # the two results' builds
    how: str = "case content"   # what made them the same case

    def line(self, case: str) -> str:
        on = (f"on this build ({self.build})" if self.builds and all(b == self.build for b in self.builds)
              else f"on builds {' and '.join(b or '?' for b in self.builds)} (this one: {self.build or '?'})")
        return (f"{case}: KNOWN FAILURE, not started: its last two results with this {self.how} failed with the"
                f" same first reason, {on}: {self.reason}; runs {self.folders[0]} and {self.folders[1]}. Fix the"
                " case or the tool first, or run it with --rerun-known-failure \"<why>\" (a new build: what it"
                " changes that is the cause)")


def known_failure(history: list[Prior], build: str, case_hash: str,
                  parts: dict[str, str] | None = None) -> Refusal | None:
    """The refusal when the last two results (RESULT_STATUSES) of `history` that are this case (the same
    case_hash, or the same part as the one its failure belongs to: `parts`, script_parts of this case's
    script) both failed with the same first reason, on any build; None otherwise (no hash: never)."""
    if not case_hash:
        return None
    parts = parts or {}

    def how(p: Prior) -> str:
        if p.case_hash == case_hash:
            return "case content"
        if p.part and parts.get(p.part[0]) == p.part[1]:
            return f"{p.part[0]} phase"
        return ""
    same = sorted((p for p in history if p.status in RESULT_STATUSES and how(p)), key=lambda p: (p.started, p.folder))
    if len(same) < 2:
        return None
    a, b = same[-2], same[-1]
    if a.status in FAILED and b.status in FAILED and a.reason and a.reason == b.reason:
        kinds = sorted({how(a), how(b)})
        return Refusal(reason=b.reason, folders=[a.folder, b.folder], build=build, builds=[a.build, b.build],
                       how=" / ".join(kinds))
    return None
