"""One validated argument builder for every game launch (stage 8 item 6, design D21).

The local runner, the remote agent, `remote_agent.py run-local` and the sandbox session all build a
game's command line here, and nothing starts a game with a line this module did not return. It refuses
to return one unless every test-data rule of docs/RULES.md holds:

- no setting with an empty value (`--name=`): the game would drop its whole command line and run on the
  owner's real data;
- each runner-owned setting exactly once, every root canonical (no reparse point on any component, no
  `\\\\?\\`, UNC, device or stream path) and inside the fresh run folder the builder made, outside every
  owner data folder, the install and the disc;
- `--config_file` the settings file the builder copied from the template into the run folder;
- `--game_data_root` exactly the verified install's `rom\\` (an RC run) or the runner's disc (development);
- exactly one `--portal_mode=software`; `--audio_mute=true` on a hidden run;
- every other setting on the explicit per-setting allow list (no prefix rule), its value checked
  against its kind; path kinds go through the same containment as the roots;
- the input script parsed with the game's own grammar (portal src/dev/input_script.cpp ParseScript),
  every command classified by the command table, every path argument contained, `renderdoc` refused.

Standard library only; `python -I`.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

# Settings the builder sets for each game itself; a spec may not set them (the game takes the last of
# two, so a second --user_data_root would point a test at any folder).
RUNNER_OWNED = ("user_data_root", "cache_root", "config_file", "log_file", "portal_figures_dir",
                "game_data_root", "input_script")
# The runner-owned settings that name a place the game writes: canonical and inside the run folder.
ROOT_SETTINGS = ("user_data_root", "cache_root", "config_file", "log_file", "portal_figures_dir",
                 "input_script")
SETTINGS_FILE = "settings.toml"
SCRIPT_FILE = "script.txt"

# Value kinds of the allow list (tests/launch_allow.toml in the game repository).
KINDS = ("bool", "int", "float", "enum", "string", "read_path", "write_path", "figure_path", "runner",
         "refused")
MAX_STRING = 256

# Windows reserved device names: a path component named like one opens the device, not a file.
DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$",
                *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10)),
                "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class LaunchError(Exception):
    """A command line or script the builder refuses to hand to a game."""


# --- owner folders --------------------------------------------------------------------------------


def owner_data_roots(environ: dict[str, str] | None = None, registry: bool = True) -> list[Path]:
    """The owner's data folders: each `Documents\\Skylanders` the user can have (the profile's, the
    OneDrive ones, the registry's Personal folder). Moved here from skytest.py."""
    env = os.environ if environ is None else environ
    home = Path(env.get("USERPROFILE") or Path.home())
    docs = [home / "Documents", home / "OneDrive" / "Documents"]
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if env.get(var):
            docs.append(Path(env[var]) / "Documents")
    if registry and os.name == "nt":
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _ = winreg.QueryValueEx(k, "Personal")
            if value:
                docs.append(Path(os.path.expandvars(value)))
        except OSError:
            pass
    return [d / "Skylanders" for d in docs]


# --- paths ----------------------------------------------------------------------------------------


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def within(path: Path | str, folder: Path | str) -> bool:
    """Component-wise containment on normalized text (no filesystem access)."""
    p, f = _norm(path), _norm(folder)
    if p == f:
        return True
    return p.startswith(f.rstrip("\\/") + os.sep)


def is_reparse_point(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return os.path.islink(p)


def check_path_text(text: str, what: str) -> None:
    """Refuses a path spelling the game and the checker could read differently, or a device path."""
    if not text:
        raise LaunchError(f"{what}: an empty path")
    if any(ord(c) < 32 for c in text):
        raise LaunchError(f"{what}: a control character in {text!r}")
    if text.startswith(("\\\\", "//")):
        raise LaunchError(f"{what}: a UNC or device path {text!r} (\\\\?\\, \\\\.\\, \\\\server)")
    w = PureWindowsPath(text)
    drive = w.drive
    rest = text[len(drive):]
    if ":" in rest:
        raise LaunchError(f"{what}: a stream suffix or stray ':' in {text!r}")
    if drive and not w.root:
        raise LaunchError(f"{what}: a drive-relative path {text!r}")
    for part in re.split(r"[\\/]+", rest):
        if not part:
            continue
        if part != part.rstrip(" ."):
            if part not in (".", ".."):
                raise LaunchError(f"{what}: a component ending in a space or dot in {text!r} (Windows strips it)")
        stem = part.split(".")[0].rstrip(" ").upper()
        if stem in DEVICE_NAMES:
            raise LaunchError(f"{what}: a device name {part!r} in {text!r}")


def _existing_prefix(p: Path) -> tuple[Path, list[str]]:
    """The longest existing prefix of an absolute path, and the missing components after it."""
    missing: list[str] = []
    cur = p
    while not os.path.lexists(cur):
        if cur.parent == cur:
            break
        missing.insert(0, cur.name)
        cur = cur.parent
    return cur, missing


def canonical(text: str, base: Path, what: str, *, must_exist: bool = False) -> Path:
    """The path `text` names, resolved against `base` as the game resolves a relative path (its working
    directory is the run folder), refused unless it is canonical: no reparse point on any existing
    component, the existing part spelled as the filesystem spells it (no 8.3 or case alias), at most
    the last component missing (its parent must exist)."""
    check_path_text(text, what)
    p = Path(text)
    if not p.is_absolute():
        if PureWindowsPath(text).root:  # "\x": the base's drive root
            raise LaunchError(f"{what}: a root-relative path {text!r}")
        p = base / p
    p = Path(os.path.normpath(p))
    prefix, missing = _existing_prefix(p)
    if len(missing) > 1 or (must_exist and missing):
        raise LaunchError(f"{what}: {p} does not exist" + ("" if must_exist else " (nor does its folder)"))
    # Every existing component, from the drive down: none is a junction, symlink or other reparse point.
    walk = Path(prefix.anchor)
    for part in prefix.parts[1:]:
        walk = walk / part
        if is_reparse_point(walk):
            raise LaunchError(f"{what}: {walk} is a junction, link or other reparse point")
    real = os.path.realpath(prefix)
    if _norm(real) != _norm(prefix) or (os.name == "nt" and not _same_spelling(real, prefix)):
        raise LaunchError(f"{what}: {prefix} is an alias of {real}")
    return p


def _same_spelling(real: str, given: Path) -> bool:
    """Case-insensitive file systems: an 8.3 short name normalizes differently from its long name, and
    normcase hides case only; compare the long spelling component by component."""
    a = [x.lower() for x in Path(real).parts]
    b = [x.lower() for x in given.parts]
    return a == b


# --- the allow list and the command table ---------------------------------------------------------


@dataclass
class SettingRule:
    name: str
    kind: str
    lo: float | None = None
    hi: float | None = None
    values: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class AllowList:
    rules: dict[str, SettingRule]

    @staticmethod
    def parse(text: str, origin: str = "allow list") -> "AllowList":
        data = tomllib.loads(text)
        rules: dict[str, SettingRule] = {}
        for name, row in (data.get("settings") or {}).items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise LaunchError(f"{origin}: setting name {name!r}")
            if not isinstance(row, dict) or row.get("kind") not in KINDS:
                raise LaunchError(f"{origin}: {name} needs a kind out of {', '.join(KINDS)}")
            kind = row["kind"]
            r = SettingRule(name, kind, row.get("min"), row.get("max"), tuple(row.get("values", ())),
                            str(row.get("reason", "")))
            if kind in ("int", "float") and (r.lo is None or r.hi is None or r.lo > r.hi):
                raise LaunchError(f"{origin}: {name} ({kind}) needs min <= max")
            if kind == "enum" and not r.values:
                raise LaunchError(f"{origin}: {name} (enum) needs values")
            if kind == "refused" and not r.reason:
                raise LaunchError(f"{origin}: {name} (refused) needs a reason")
            rules[name] = r
        for name in RUNNER_OWNED:
            if name in rules and rules[name].kind != "runner":
                raise LaunchError(f"{origin}: {name} is runner-owned (kind = \"runner\")")
        return AllowList(rules)

    @staticmethod
    def load(path: Path) -> "AllowList":
        return AllowList.parse(path.read_text(encoding="utf-8"), str(path))


# The portal's own script commands (src/dev/input_script.cpp ParseScript), by what they do with a path:
#   none        no path; `trap` writes only under the runner-owned --portal_figures_dir
#               (portal_rex.cpp:715-734)
#   write_path  `save` opens its file with ofstream (:364-373, 615-634); `shot` writes <name>.png
#               joined to the script's folder (:697)
#   figure_path `place` reads a figure file and the software portal saves the game's writes back into
#               it (portal_rex.cpp:400, 665-671): inside the run's figures copy
#   refused     `renderdoc`: its captures go wherever RenderDoc is set to write, which the game
#               neither sets nor checks (:481-509); refused in every validated run
BUILTIN_COMMANDS = {
    "wait": "none", "tap": "none", "hold": "none", "stick": "none", "log": "none", "fps": "none",
    "scan": "none", "poke": "none", "dump": "none", "watch": "none", "watchread": "none",
    "pollwatch": "none", "unwatch": "none", "stacks": "none", "player": "none", "key": "none",
    "remove": "none", "putback": "none", "trap": "none", "waitfor": "none", "quit": "none",
    "save": "write_path", "shot": "write_path", "place": "figure_path", "renderdoc": "refused",
}
# The game's registered commands (RegisterScriptCommand), by what they do with a path. The game hands
# such a command the rest of its line; a relative path is resolved against the game's working
# directory, which the builder sets to the run folder.
#   none              no path
#   read_path         `<path> <word>`: everything before the last word is one file the game reads,
#                     outer double quotes dropped (Swap Force Online's save_import <path> <slot>,
#                     save_import_dev.h ParseDevImportArgs)
#   read_write_paths  `<file> <out> ...`: the first word is a file the game reads, the second one it
#                     writes (save_dump <file> <out> [--inventory], save_dump.cpp DumpCommand)
#   refused           never in a validated run
# A read path lies inside the run folder (it may not exist yet: a file the run makes) or is an
# existing file inside a read root; a written path lies inside the run folder.
REGISTERED_CLASSES = ("none", "read_path", "read_write_paths", "refused")


@dataclass
class CommandTable:
    """The game's registered commands (RegisterScriptCommand), each classified by REGISTERED_CLASSES. A
    registered command with no row is refused, as is any name the game does not know."""
    registered: dict[str, str]
    conditions: frozenset[str] | None = None

    @staticmethod
    def parse(text: str, origin: str = "command table") -> "CommandTable":
        data = tomllib.loads(text)
        reg: dict[str, str] = {}
        for name, cls in (data.get("commands") or {}).items():
            if cls not in REGISTERED_CLASSES:
                raise LaunchError(f"{origin}: command {name} is {cls!r}; registered commands are "
                                  f"{', '.join(REGISTERED_CLASSES)}")
            if name in BUILTIN_COMMANDS:
                raise LaunchError(f"{origin}: {name} is a portal command (the portal's meaning wins)")
            reg[name] = cls
        conds = data.get("conditions")
        return CommandTable(reg, frozenset(conds) if conds is not None else None)

    @staticmethod
    def load(path: Path) -> "CommandTable":
        return CommandTable.parse(path.read_text(encoding="utf-8"), str(path))


# --- the run context ------------------------------------------------------------------------------


@dataclass
class RunContext:
    kind: str                      # "rc" (a verified install) or "dev" (a development build)
    store: Path                    # the machine's test store
    run_folder: Path               # the fresh run folder this launch owns, inside the store
    rom: Path                      # the only accepted --game_data_root
    template: Path                 # the settings template the builder copies
    allow: AllowList
    commands: CommandTable
    install: Path | None = None    # an RC run: the verified install folder
    read_roots: list[Path] = field(default_factory=list)   # where read-path settings may point
    owner_roots: list[Path] | None = None

    @property
    def figures(self) -> Path:
        return self.run_folder / "figures"

    def owners(self) -> list[Path]:
        return owner_data_roots() if self.owner_roots is None else self.owner_roots


def make_run_folder(store: Path, name: str, owner_roots: list[Path] | None = None) -> Path:
    """A fresh run folder inside the store; refuses an existing one (a run never reuses a folder)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name) or ".." in name:
        raise LaunchError(f"run folder name {name!r}")
    store_c = canonical(str(store), store, "the test store", must_exist=True)
    run = store_c / name
    _refuse_owner(run, owner_data_roots() if owner_roots is None else owner_roots, "the run folder")
    try:
        run.mkdir(parents=False)
    except FileExistsError:
        raise LaunchError(f"the run folder {run} exists already (a run gets a fresh folder)") from None
    return canonical(str(run), store_c, "the run folder", must_exist=True)


def _refuse_owner(p: Path, owners: list[Path], what: str) -> None:
    for root in owners:
        if within(p, root):
            raise LaunchError(f"{what}: {p} is inside the owner's data folder {root}")
        try:
            real = os.path.realpath(root)
        except OSError:
            continue
        if within(p, real):
            raise LaunchError(f"{what}: {p} is inside the owner's data folder {root} ({real})")


def contained(ctx: RunContext, text: str, what: str, inside: Path, base: Path | None = None,
              *, must_exist: bool = False) -> Path:
    """The canonical path, refused unless inside `inside`, outside the owner folders, the install and
    the disc."""
    p = canonical(text, base or ctx.run_folder, what, must_exist=must_exist)
    if not within(p, ctx.store):
        raise LaunchError(f"{what}: {p} is outside the test store {ctx.store}")
    if not within(p, inside):
        raise LaunchError(f"{what}: {p} is outside {inside}")
    _refuse_owner(p, ctx.owners(), what)
    if ctx.install is not None and within(p, ctx.install):
        raise LaunchError(f"{what}: {p} is inside the install {ctx.install}")
    if within(p, ctx.rom) or within(p, os.path.realpath(ctx.rom)):
        raise LaunchError(f"{what}: {p} is inside the disc {ctx.rom}")
    return p


def _read_contained(ctx: RunContext, text: str, what: str) -> Path:
    p = canonical(text, ctx.run_folder, what, must_exist=True)
    _refuse_owner(p, ctx.owners(), what)
    for root in [ctx.run_folder, *ctx.read_roots]:
        if within(p, root):
            return p
    raise LaunchError(f"{what}: {p} is outside the run folder and the read roots")


# --- the input script -----------------------------------------------------------------------------

BUTTONS = {"a", "b", "x", "y", "start", "back", "guide", "up", "down", "left", "right", "lb", "rb",
           "ls", "rs", "lt", "rt"}
HEX = re.compile(r"(0[xX])?[0-9a-fA-F]+")
ADDRESS = re.compile(r"(0[xX])?[0-9a-fA-F]+|hit\d+(\+(0[xX])?[0-9a-fA-F]+)?|\*(0[xX])?[0-9a-fA-F]+(\+(0[xX])?[0-9a-fA-F]+)?")
NAME = re.compile(r"[A-Za-z0-9_]+")
# istream >> splits at the C locale's whitespace.
_WS = " \t\n\v\f\r"


@dataclass
class ScriptCommand:
    line: int
    name: str
    words: list[str]
    rest: str          # the rest of the line after the command word, as getline(words >> ws) reads it
    cls: str


def _words(line: str) -> list[str]:
    """Words as istream >> reads them: split at ASCII whitespace only (not at U+00A0 or U+0085, which
    Python's str.split would also take)."""
    stripped = line.strip(_WS)
    return re.split("[" + _WS + "]+", stripped) if stripped else []


def _number(word: str) -> float | None:
    try:
        v = float(word)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _int(word: str) -> int | None:
    return int(word) if re.fullmatch(r"[+-]?\d+", word) else None


def _rest_after(line: str, n_words: int) -> str:
    """What `getline(words >> std::ws, text)` reads after `n_words` words were extracted: skip the
    whitespace after them, then everything to the end of the line (trailing whitespace included)."""
    i = 0
    for _ in range(n_words):
        while i < len(line) and line[i] in _WS:
            i += 1
        while i < len(line) and line[i] not in _WS:
            i += 1
    while i < len(line) and line[i] in _WS:
        i += 1
    return line[i:]


def parse_script(text: str, commands: CommandTable) -> list[ScriptCommand]:
    """The script split and read exactly as the game's ParseScript does: the file is read in text mode
    (CRLF becomes LF), every ';' becomes a line break (comments included, so `# x; save ...` holds a
    `save`), '#' starts a comment, words split at whitespace. A line the game would refuse is refused
    here too, and so is anything the game could read differently (a control character, which text
    mode or the parser would treat specially)."""
    for ch in text:
        if ord(ch) < 32 and ch not in "\t\r\n":
            raise LaunchError(f"input script: control character {ord(ch):#04x} (the game's text-mode read "
                              "could end or split the script there)")
    text = text.replace("\r\n", "\n").replace(";", "\n")
    out: list[ScriptCommand] = []
    for no, raw in enumerate(text.split("\n"), 1):
        line = raw.split("#", 1)[0]
        words = _words(line)
        if not words:
            continue
        name = words[0]
        args = words[1:]
        cls = BUILTIN_COMMANDS.get(name)
        if cls is None:
            cls = commands.registered.get(name)
            if cls is None:
                raise LaunchError(f"input script line {no}: {name!r} is no command in the table (an "
                                  "unclassified or unknown command is refused)")
        ok = _syntax_ok(name, args, commands)
        if not ok:
            raise LaunchError(f"input script line {no}: the game cannot parse {raw.strip()!r}")
        out.append(ScriptCommand(no, name, args, _rest_after(line, 1), cls))
    return out


def _syntax_ok(name: str, a: list[str], commands: CommandTable) -> bool:
    def buttons(w: str) -> bool:
        return all(b in BUTTONS for b in w.split("+"))

    if name == "wait":
        return True  # the game ignores a bad number here (it never fails a wait)
    if name == "tap":
        return len(a) >= 1 and buttons(a[0])
    if name == "hold":
        return len(a) >= 2 and buttons(a[0]) and _number(a[1]) is not None
    if name == "stick":
        return len(a) >= 3 and all(_number(x) is not None for x in a[:3])
    if name == "log":
        return True
    if name == "fps":
        return len(a) >= 1 and (_number(a[0]) or 0) > 0
    if name == "scan":
        return len(a) >= 1 and all(HEX.fullmatch(x) for x in a)
    if name == "poke":
        return len(a) >= 2 and bool(ADDRESS.fullmatch(a[0])) and bool(HEX.fullmatch(a[1]))
    if name == "renderdoc":
        return True  # refused by class, whatever its arguments
    if name == "player":
        return len(a) >= 1 and (_int(a[0]) or 0) in (1, 2, 3, 4)
    if name in ("watch", "watchread"):
        return len(a) in (1, 2, 4) and bool(ADDRESS.fullmatch(a[0])) and all(HEX.fullmatch(x) for x in a[1:])
    if name == "pollwatch":
        n = _int(a[1]) if len(a) > 1 else 16
        return len(a) in (1, 2) and bool(ADDRESS.fullmatch(a[0])) and n is not None and 0 < n <= 256
    if name in ("unwatch", "putback", "quit"):
        return True
    if name == "stacks":
        n = _int(a[0]) if a else 3
        return len(a) <= 1 and n is not None and 0 < n <= 50
    if name == "dump":
        n = _int(a[1]) if len(a) > 1 else None
        return len(a) == 2 and bool(ADDRESS.fullmatch(a[0])) and n is not None and 0 < n <= 4096
    if name == "key":
        return len(a) >= 1 and bool(NAME.fullmatch(a[0]))
    if name == "save":
        return len(a) >= 3 and bool(HEX.fullmatch(a[0])) and bool(HEX.fullmatch(a[1]))
    if name in ("place", "trap"):
        s = _int(a[0]) if a else None
        return len(a) >= 2 and s is not None and 0 <= s < 16
    if name == "remove":
        s = _int(a[0]) if a else None
        return len(a) >= 1 and s is not None and 0 <= s < 16
    if name == "shot":
        return len(a) >= 1
    if name == "waitfor":
        if len(a) < 2 or (_number(a[1]) or 0) <= 0 or not NAME.fullmatch(a[0]):
            return False
        return commands.conditions is None or a[0] in commands.conditions
    return True  # a registered command: the game hands it the rest of the line


def decode_script(data: bytes) -> str:
    """The script's exact bytes as the game reads them (UTF-8; the path commands go through u8path)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise LaunchError(f"input script: not UTF-8 ({e})") from None


def check_script(text: str, ctx: RunContext, script_dir: Path | None = None) -> list[ScriptCommand]:
    """Every command classified and every path argument contained. `script_dir` is the folder the
    script is written to (the run folder): `shot` joins its name to it, `save` and `place` resolve a
    relative path against the game's working directory, which the builder sets to the run folder."""
    sd = script_dir or ctx.run_folder
    cmds = parse_script(text, ctx.commands)
    for c in cmds:
        where = f"input script line {c.line} ({c.name})"
        if c.cls == "refused":
            if c.name == "renderdoc":
                raise LaunchError(f"{where}: renderdoc is refused in every validated run (its captures go "
                                  "wherever RenderDoc writes, which the game neither sets nor checks)")
            raise LaunchError(f"{where}: refused by the command table")
        if c.name == "save":
            dest = _rest_after(c.rest, 2)
            contained(ctx, dest, where, ctx.run_folder, ctx.run_folder)
        elif c.name == "shot":
            name = c.words[0]
            if ":" in name or name.startswith(("\\", "/")):
                raise LaunchError(f"{where}: {name!r} would replace the script's folder")
            contained(ctx, name + ".png", where, ctx.run_folder, sd)
        elif c.name == "place":
            dest = _rest_after(c.rest, 1)
            contained(ctx, dest, where, ctx.figures, ctx.run_folder, must_exist=True)
        elif c.cls == "read_path":
            path = _path_before_last_word(c.rest)
            if path is None:
                raise LaunchError(f"{where}: the game cannot parse {c.rest.strip()!r} (<path> <word>)")
            _registered_read(ctx, path, where)
        elif c.cls == "read_write_paths":
            if len(c.words) < 2:
                raise LaunchError(f"{where}: the game reads <file> <out> (two words)")
            _registered_read(ctx, c.words[0], where)
            contained(ctx, c.words[1], where, ctx.run_folder, ctx.run_folder)
    return cmds


def _path_before_last_word(rest: str) -> str | None:
    """A read_path command's file as the game takes it (save_import_dev.h ParseDevImportArgs): spaces
    trimmed, everything before the last space, one pair of outer double quotes dropped."""
    a = rest.strip(" ")
    sp = a.rfind(" ")
    if sp < 0:
        return None
    path = a[:sp].rstrip(" ")
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
    return path or None


def _registered_read(ctx: RunContext, text: str, what: str) -> Path:
    """A file a registered command reads: inside the run folder (it may be one the run makes), else an
    existing file inside a read root."""
    try:
        return contained(ctx, text, what, ctx.run_folder, ctx.run_folder)
    except LaunchError as outside:
        try:
            return _read_contained(ctx, text, what)
        except LaunchError:
            raise outside from None


# --- the argument check ---------------------------------------------------------------------------


def split_setting(arg: str) -> tuple[str, str]:
    if not arg.startswith("--") or "=" not in arg:
        raise LaunchError(f"refusing {arg!r}: every setting is --name=value")
    name, value = arg[2:].split("=", 1)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise LaunchError(f"refusing {arg!r}: not a setting name")
    if value == "":
        raise LaunchError(f"refusing an empty setting {arg!r}: the game would ignore its whole command line")
    return name, value


def check_value(ctx: RunContext, rule: SettingRule, value: str) -> None:
    what = f"--{rule.name}"
    k = rule.kind
    if k == "refused":
        raise LaunchError(f"{what} is refused in specs: {rule.reason}")
    if k == "runner":
        raise LaunchError(f"{what} is runner-owned: a spec may not set it")
    if k == "bool":
        if value not in ("true", "false"):
            raise LaunchError(f"{what}={value}: true or false")
    elif k == "int":
        n = _int(value)
        if n is None or not rule.lo <= n <= rule.hi:
            raise LaunchError(f"{what}={value}: a whole number from {rule.lo} to {rule.hi}")
    elif k == "float":
        x = _number(value)
        if x is None or not rule.lo <= x <= rule.hi:
            raise LaunchError(f"{what}={value}: a number from {rule.lo} to {rule.hi}")
    elif k == "enum":
        if value not in rule.values:
            raise LaunchError(f"{what}={value}: one of {', '.join(rule.values)}")
    elif k == "string":
        if len(value) > MAX_STRING or any(ord(c) < 32 for c in value) or re.search(r"[\\/:]", value):
            raise LaunchError(f"{what}={value!r}: a plain string (no path, no control characters)")
    elif k == "write_path":
        contained(ctx, value, what, ctx.run_folder)
    elif k == "figure_path":
        contained(ctx, value, what, ctx.figures, must_exist=True)
    elif k == "read_path":
        _read_contained(ctx, value, what)


def check_args(ctx: RunContext, args: list[str]) -> None:
    """The whole command line, however it was assembled: refuses it unless every rule holds."""
    counts: dict[str, int] = {}
    values: dict[str, list[str]] = {}
    for a in args:
        name, value = split_setting(a)
        counts[name] = counts.get(name, 0) + 1
        values.setdefault(name, []).append(value)
    for key in RUNNER_OWNED:
        if counts.get(key, 0) != 1:
            raise LaunchError(f"refusing to start a game with --{key} set {counts.get(key, 0)} times (the "
                              "builder sets it once, for the run's own copy)")
    if values.get("portal_mode") != ["software"]:
        raise LaunchError("refusing to start a game without exactly one --portal_mode=software")
    if "true" in values.get("window_hidden", []) and values.get("audio_mute") != ["true"]:
        raise LaunchError("refusing a hidden run without --audio_mute=true")
    for key in ROOT_SETTINGS:
        contained(ctx, values[key][0], f"--{key}", ctx.run_folder)
    cfg = canonical(values["config_file"][0], ctx.run_folder, "--config_file", must_exist=True)
    if _norm(cfg) != _norm(ctx.run_folder / SETTINGS_FILE):
        raise LaunchError(f"--config_file {cfg}: only the settings file the builder copied into the run folder")
    script = canonical(values["input_script"][0], ctx.run_folder, "--input_script", must_exist=True)
    check_script(decode_script(script.read_bytes()), ctx, script.parent)
    rom = values["game_data_root"][0]
    want = os.path.realpath(ctx.rom)
    if ctx.kind == "rc":
        if ctx.install is None or _norm(want) != _norm(os.path.realpath(ctx.install / "rom")):
            raise LaunchError("an RC run's disc is the verified install's rom\\ (the context names another)")
    if _norm(rom) not in (_norm(ctx.rom), _norm(want)) or _norm(os.path.realpath(rom)) != _norm(want):
        raise LaunchError(f"--game_data_root={rom}: only {ctx.rom} ({'the verified install' if ctx.kind == 'rc' else 'the configured disc'})")
    for name, vals in values.items():
        if name in RUNNER_OWNED or name == "portal_mode":
            continue
        rule = ctx.allow.rules.get(name)
        if rule is None:
            raise LaunchError(f"--{name} is not on the allow list (each setting is listed with its kind)")
        for v in vals:
            check_value(ctx, rule, v)


def copy_fixtures(ctx: RunContext, saves: Path, figures: Path) -> None:
    """The run's own copies of the fixture saves and figures (copies, never links)."""
    for src, dst in ((saves, ctx.run_folder / "data"), (figures, ctx.figures)):
        for root, dirs, files in os.walk(src):
            for n in dirs + files:
                if is_reparse_point(Path(root) / n):
                    raise LaunchError(f"fixture {Path(root) / n} is a link or junction (a run copies files only)")
        contained(ctx, str(dst), "a fixture copy", ctx.run_folder)
        shutil.copytree(src, dst, symlinks=False)


def build_game(ctx: RunContext, spec_args: list[str], script_text: str, *, fixtures: tuple[Path, Path] | None = None,
               runner_args: list[str] | None = None) -> list[str]:
    """Prepares the run folder (fixture copies, the settings copy, the script) and returns the game's
    settings, or raises LaunchError. `runner_args` are the runner's own fixed settings (the defaults,
    scenes, window); they pass the same allow list as the spec's."""
    for a in spec_args:
        name, _ = split_setting(a)
        if name in RUNNER_OWNED:
            raise LaunchError(f"--{name} is runner-owned: a spec may not set it")
    if fixtures is not None:
        copy_fixtures(ctx, *fixtures)
    else:
        ctx.figures.mkdir(exist_ok=True)
        (ctx.run_folder / "data").mkdir(exist_ok=True)
    (ctx.run_folder / "cache").mkdir(exist_ok=True)
    settings = ctx.run_folder / SETTINGS_FILE
    shutil.copyfile(ctx.template, settings)
    data = script_text.encode("utf-8")
    script = ctx.run_folder / SCRIPT_FILE
    check_script(decode_script(data), ctx)  # before anything lands in the folder under the script's name
    script.write_bytes(data)
    args = [
        f"--user_data_root={ctx.run_folder / 'data'}",
        f"--cache_root={ctx.run_folder / 'cache'}",
        f"--config_file={settings}",
        f"--log_file={ctx.run_folder / 'game.log'}",
        f"--portal_figures_dir={ctx.figures}",
        f"--game_data_root={ctx.rom}",
        f"--input_script={script}",
        *(runner_args or []),
        *spec_args,
    ]
    check_args(ctx, args)
    return args


# --- the synthetic peer (stage 4's program) -------------------------------------------------------


def build_synthetic_peer(ctx: RunContext, args: list[str], server: str,
                         allow: dict[str, str] | None = None) -> list[str]:
    """The synthetic peer's arguments: an explicit allow list of its own settings, no empty value,
    `--server` exactly the session address of the run plan, every path inside the run folder.

    TODO(stage 4): the program and its settings are stage 4's (online-4.md D4, item 2); until stage 4
    names them `allow` is empty and every setting but --server and --log_file is refused."""
    allow = dict(allow or {})
    allow.setdefault("log_file", "write_path")
    seen_server = 0
    for a in args:
        name, value = split_setting(a)
        if name == "server":
            seen_server += 1
            if value != server:
                raise LaunchError(f"--server={value}: only the run plan's session address {server}")
            continue
        kind = allow.get(name)
        if kind is None:
            raise LaunchError(f"--{name} is not on the synthetic peer's allow list (stage 4 names it)")
        if kind not in ("bool", "string", "write_path", "read_path"):
            raise LaunchError(f"--{name}: the synthetic peer's allow list takes bool, string or path kinds")
        check_value(ctx, SettingRule(name, kind), value)
    if seen_server != 1:
        raise LaunchError("the synthetic peer needs exactly one --server")
    return list(args)
