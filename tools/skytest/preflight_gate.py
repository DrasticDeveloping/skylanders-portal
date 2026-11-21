"""The runner's preflight gate (skytest.py test and levelpass): before a case starts its games, the case's
preflight (preflight.py: its objects against the stored level data, its waits, its log patterns against
the code, its settings, a replay on its newest recorded run) is asked. An error starts no game: the result
is `preflight` with the problem lines (exit 6 when nothing else failed). Warnings are printed and the run
goes on. `--ignore-preflight "<why>"` starts it anyway and the result records the why.

Pure: `decide` takes the problems as (severity, line) pairs; unit-tested in tests/test_preflight_gate.py."""
from __future__ import annotations

from dataclasses import dataclass, field

PREFLIGHT = "preflight"


@dataclass
class Gate:
    start: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ignored: str = ""        # the --ignore-preflight why, when it started a case with errors
    reason: str = ""         # the result's first reason when not started
    printed: list[str] = field(default_factory=list)  # what the runner prints before the case

    def record(self) -> dict:
        """The result's `preflight` field; {} when the preflight found nothing."""
        if not (self.errors or self.warnings):
            return {}
        out: dict = {"errors": self.errors, "warnings": self.warnings}
        if self.ignored:
            out["ignored"] = self.ignored
        return out


def decide(problems: list[tuple[str, str]], ignore_why: str = "") -> Gate:
    """`problems`: (severity "error" or "warn", the problem's line). Errors stop the case unless `ignore_why`."""
    errors = [line for sev, line in problems if sev == "error"]
    warnings = [line for sev, line in problems if sev != "error"]
    printed = [f"preflight warning: {w}" for w in warnings]
    if not errors:
        return Gate(start=True, warnings=warnings, printed=printed)
    if ignore_why:
        printed += [f"preflight error (--ignore-preflight: {ignore_why}): {e}" for e in errors]
        return Gate(start=True, errors=errors, warnings=warnings, ignored=ignore_why, printed=printed)
    reason = (f"not started: its preflight found {len(errors)} error(s); fix the spec (or the level data), or run it"
              " with --ignore-preflight \"<why>\"")
    printed += [f"preflight error: {e}" for e in errors] + [reason]
    return Gate(start=False, errors=errors, warnings=warnings, reason=reason, printed=printed)
