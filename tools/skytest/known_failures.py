"""The runner's refusal of a pointless rerun (skytest.py test and levelpass, before a case starts games).

A case whose last two results on the same build and with the same case content both failed with the
same first reason fails a third time for that reason: 38 level passes did on 18 November 2026 (275
minutes of game time, the same 60 s wait for a level end the pass could not reach). The runner looks
at the case's earlier results in the store's runs folder (each run folder's result.json) and, when that
is so, starts no game: the case's result is `known failure` with the reason and the two run folders.
`--rerun-known-failure "<why>"` runs it anyway and the result records the why.

Pure code here (the caller reads the result files); unit-tested in tests/test_known_failures.py.

What counts:
- a result: a case that started its games and ended `pass`, `fail` or `error` (a capacity, not-ready,
  breaker, not-in-bulk, known-failure or build-wait result started none and says nothing);
- the build: the result's `provenance.build` (the snapshot name, build_hash), else the snapshot of its
  session's results.json for results written before the field existed;
- the case content: `provenance.case_sha256` (case_sha256: the spec's text, the arguments every peer
  gets, the fixtures), else the same hash made from the older provenance fields;
- the first reason: the first reason of the first peer that has one (as the owner's test watcher reads
  it), else the case's own first reason; time stamps, thread ids and numbers taken out (normal)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

KNOWN_FAILURE = "known failure"
RESULT_STATUSES = ("pass", "fail", "error")   # results of a case that started its games
FAILED = ("fail", "error")
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


def prior_from_result(folder: str, result: dict, session_snapshot: str = "") -> Prior:
    """A Prior from a run folder's result.json (and its session's snapshot for an older result)."""
    prov = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    build = str(prov.get("build") or "")
    if not build and session_snapshot:
        build = re.split(r"[\\/]", session_snapshot.rstrip("\\/"))[-1]
    case_hash = str(prov.get("case_sha256") or "") or case_sha256(
        str(prov.get("spec_sha256") or ""), str(prov.get("args_sha256") or ""), str(prov.get("fixture_sha256") or ""))
    return Prior(folder=folder, started=str(result.get("started") or ""), status=str(result.get("status") or ""),
                 build=build, case_hash=case_hash, reason=first_reason(result))


@dataclass
class Refusal:
    reason: str
    folders: list[str]
    build: str

    def line(self, case: str) -> str:
        return (f"{case}: KNOWN FAILURE, not started: its last two results on build {self.build} with this case"
                f" content failed with the same first reason: {self.reason}; runs {self.folders[0]} and"
                f" {self.folders[1]}. Fix the case or the tool first, or run it with --rerun-known-failure"
                " \"<why>\"")


def known_failure(history: list[Prior], build: str, case_hash: str) -> Refusal | None:
    """The refusal when the last two results (RESULT_STATUSES) of `history` on `build` with `case_hash`
    both failed with the same first reason; None otherwise (no build or hash known: never refused)."""
    if not build or not case_hash:
        return None
    same = sorted((p for p in history if p.status in RESULT_STATUSES and p.build == build
                   and p.case_hash == case_hash), key=lambda p: (p.started, p.folder))
    if len(same) < 2:
        return None
    a, b = same[-2], same[-1]
    if a.status in FAILED and b.status in FAILED and a.reason and a.reason == b.reason:
        return Refusal(reason=b.reason, folders=[a.folder, b.folder], build=build)
    return None
