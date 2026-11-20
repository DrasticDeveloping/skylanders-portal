"""The owner's session settings (the game repository's tests/session_settings.toml): one list of online settings
for every game of a session, in the tests and in the owner's own play, so the two cannot drift apart.

    version = "2026-11-20.1"
    [on]                      # passed to every game of a session, `--<name>=<value>`
    online_shared_coins = true
    online_authored_ids = 2
    [not_passed]              # left at their default, each with why (checked like the list)
    online_scene_lines = "until scene-lines-tb is read"

A value is true, false, a whole number or a non-empty text, never empty: an empty value makes the game drop its
whole command line, its data folder included. The runner (skytest.py session_args) puts the list on a game's
command line before the spec's own args. A name the spec sets keeps the spec's value, and a name never goes twice
(a bool setting's occurrences sum). Every name must have its launch_allow row (unknown_names).

The owner's play starter reads it the same way:
    sys.path.insert(0, str(repo / "thirdparty/skylanders-portal/tools/skytest"))
    import session_settings
    ONLINE = session_settings.load(repo).args()
`python -I session_settings.py <repo>` prints the version and the arguments. Standard library only."""
from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILE = Path("tests") / "session_settings.toml"
NAME = re.compile(r"^[a-z][a-z0-9_]*$")
KEYS = ("version", "on", "not_passed")


class SessionSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class SessionSettings:
    version: str
    digest: str                                   # sha256 of the file's text (its line ends as \n)
    on: dict[str, str]                            # name -> value as the command line gets it
    not_passed: dict[str, str] = field(default_factory=dict)   # name -> why it is left at its default
    path: Path | None = None

    def args(self) -> list[str]:
        return [f"--{k}={v}" for k, v in self.on.items()]

    def label(self) -> str:
        return f"{self.version} (sha256 {self.digest[:12]})"


def _value(name: str, v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str) and v.strip() and v == v.strip() and "'" not in v and " " not in v:
        return v
    raise SessionSettingsError(f"[on] {name}: true, false, a whole number or a text without spaces, never empty")


def parse(text: str, origin: str = str(FILE)) -> SessionSettings:
    """The list from the file's text, or SessionSettingsError naming what is wrong."""
    text = text.replace("\r\n", "\n")
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SessionSettingsError(f"{origin}: not TOML ({e})") from None
    unknown = sorted(set(d) - set(KEYS))
    if unknown:
        raise SessionSettingsError(f"{origin}: unknown keys {unknown} ({', '.join(KEYS)})")
    version = d.get("version")
    if not isinstance(version, str) or not version.strip():
        raise SessionSettingsError(f"{origin}: `version` is a non-empty text (change it with every change)")
    on, off = d.get("on"), d.get("not_passed", {})
    if not isinstance(on, dict) or not on:
        raise SessionSettingsError(f"{origin}: [on] is a table of at least one setting")
    if not isinstance(off, dict):
        raise SessionSettingsError(f"{origin}: [not_passed] is a table (name = why)")
    values: dict[str, str] = {}
    for name, v in on.items():
        if not NAME.match(name):
            raise SessionSettingsError(f"{origin}: [on] {name!r} is not a setting name")
        if isinstance(v, dict):
            raise SessionSettingsError(f"{origin}: [on] {name}: a value, not a table")
        try:
            values[name] = _value(name, v)
        except SessionSettingsError as e:
            raise SessionSettingsError(f"{origin}: {e}") from None
    why: dict[str, str] = {}
    for name, w in off.items():
        if not NAME.match(name):
            raise SessionSettingsError(f"{origin}: [not_passed] {name!r} is not a setting name")
        if not isinstance(w, str) or not w.strip():
            raise SessionSettingsError(f"{origin}: [not_passed] {name}: say why it is left at its default")
        if name in values:
            raise SessionSettingsError(f"{origin}: {name} is in [on] and in [not_passed]")
        why[name] = w.strip()
    return SessionSettings(version.strip(), hashlib.sha256(text.encode("utf-8")).hexdigest(), values, why)


def load(repo: Path) -> SessionSettings:
    """The repository's list (repo/tests/session_settings.toml), or SessionSettingsError."""
    path = Path(repo) / FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SessionSettingsError(f"no session settings at {path} ({e.strerror or e})") from None
    ss = parse(text, str(path))
    return SessionSettings(ss.version, ss.digest, ss.on, ss.not_passed, path)


def unknown_names(ss: SessionSettings, allowed: set[str] | dict) -> list[str]:
    """The list's names (both tables) without a launch_allow row: a wrong name fails, never passed blind."""
    return [n for n in [*ss.on, *ss.not_passed] if n not in allowed]


def setting_name(arg: str) -> str:
    return arg[2:].split("=", 1)[0] if arg.startswith("--") else ""


def for_game(ss: SessionSettings, *others: list[str]) -> list[str]:
    """The list's arguments for one game, without every name one of `others` (the runner's and the spec's own
    args) already sets: the spec's value wins, and no name goes twice."""
    taken = {setting_name(a) for args in others for a in args}
    return [a for a in ss.args() if setting_name(a) not in taken]


if __name__ == "__main__":
    try:
        s = load(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    except SessionSettingsError as e:
        raise SystemExit(str(e))
    print(f"session settings {s.label()}: {len(s.on)} on, {len(s.not_passed)} not passed")
    for a in s.args():
        print(a)
