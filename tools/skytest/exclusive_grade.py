"""skytest's `exclusive` grade (stage 4 item 7, Sol 4-rest F7): a race between named clients that
exactly one of them wins. Exactly one of `peers` has every `winner` pattern exactly once and no
`loser` pattern (the grant: one client execution, one microgame open), and every other one of them
has every `loser` pattern exactly once and no `winner` pattern (the refusal: nothing executed, nothing
opened). Graded on the result of `peer` (S by default), with every client's log. Standard library
only."""
from __future__ import annotations

import re


class ExclusiveSpecError(ValueError):
    pass


def check_exclusive(spec_name: str, g: object, names: list[str]) -> None:
    """The grade's table: `peers` (two or more of the spec's clients), `winner` and `loser` (each a
    non-empty list of patterns), optional `peer` (the result it is graded on)."""
    def bad(why: str) -> ExclusiveSpecError:
        return ExclusiveSpecError(f"{spec_name}: grade exclusive: {why}")
    if not isinstance(g, dict):
        raise bad("must be a table")
    peers = g.get("peers")
    if not isinstance(peers, list) or len(peers) < 2 or any(p not in names or p == "S" for p in peers):
        raise bad(f"`peers` must name two or more clients of {names}")
    if len(set(peers)) != len(peers):
        raise bad("`peers` names a client twice")
    if g.get("peer", "S") not in names:
        raise bad(f"`peer` must be one of {names}")
    for key in ("winner", "loser"):
        pats = g.get(key)
        if not isinstance(pats, list) or not pats or not all(isinstance(p, str) and p for p in pats):
            raise bad(f"`{key}` must be a non-empty list of patterns")
        for p in pats:
            try:
                re.compile(p)
            except re.error as e:
                raise bad(f"bad pattern {p!r}: {e}") from e


def _counts(text: str, pats: list[str]) -> list[int]:
    lines = text.splitlines()
    return [sum(1 for line in lines if re.search(p, line)) for p in pats]


def grade_exclusive(texts: dict[str, str], g: dict) -> tuple[dict, list[str]]:
    """(graded, reasons): graded names the winner and each client's counts; reasons is empty when the
    race had exactly one winner and every other client lost cleanly."""
    winner_pats, loser_pats = list(g["winner"]), list(g["loser"])
    graded: dict = {"winner": None, "peers": {}}
    winners, losers, reasons = [], [], []
    for p in g["peers"]:
        text = texts.get(p, "")
        w, l = _counts(text, winner_pats), _counts(text, loser_pats)
        graded["peers"][p] = {"winner": w, "loser": l}
        if all(c == 1 for c in w) and all(c == 0 for c in l):
            winners.append(p)
        elif all(c == 0 for c in w) and all(c == 1 for c in l):
            losers.append(p)
        else:
            reasons.append(f"grade exclusive: {p} neither won nor lost cleanly (winner counts {w}, loser counts {l})")
    if len(winners) != 1:
        reasons.append(f"grade exclusive: {len(winners)} winners ({', '.join(winners) or 'none'}), one expected")
    else:
        graded["winner"] = winners[0]
    if len(winners) + len(losers) != len(g["peers"]) and not reasons:
        reasons.append("grade exclusive: not every client won or lost")
    return graded, reasons
