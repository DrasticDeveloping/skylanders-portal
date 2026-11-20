"""skytest's `drop_tiles` grade (stage 8, the black and cyan squares): reads a run's PICTURES for the reader,
who still decides. A dropped draw that overwrites its render target (rexglue-sdk, with async shader
compilation) leaves the target's old contents; in a game that lights its 3D scene in screen tiles that is a
flat black or cyan tile of 64x64 scene pixels.

    [grade.drop_tiles]
    expect = "seen"            # "seen": the fix off; "none": the fix on, or the synchronous control
    marker = "gpu-pipeline-drops"
    # peer = "H"               # whose folder, log and script

The attack rounds and their prescribed pictures come from the peer's own script (script.txt in its folder):
the `shot <name>` commands after `log <marker> round <k>` belong to round k, up to the next marker or
`log <marker> end`. The frames rexglue-sdk's pipeline_drop_capture saved (`Pipeline wait: frame <n> saved as
pipeline-drop-f<n>-<HHMMSS>-<ms>.png (<o> overwriting, ...`) belong to the round whose marker lines enclose
their CAPTURE time (in the file's name, taken at the frame's readback), not the time the writer logged them.

Outcomes (graded["verdict"], and a page drop_tiles.md beside the pictures with every candidate's crop):
- "seen" (expect seen: the pass): a tile in a saved frame of a round whose OWN line counts at least one
  overwriting drop.
- "none" (expect none: the pass): every prescribed picture of every round is a usable gameplay picture and no
  picture of the rounds (prescribed shots and saved frames) shows a tile. For expect seen it is a failure
  (NOT SEEN: frames with overwriting drops in the rounds, no tile).
- "needs reading": a candidate tile without a matching drop line (expect seen), or any candidate tile (expect
  none). Crops of each candidate are saved (drop_tiles/<picture>-c<col>r<row>.png) and the case is neither
  passed nor failed by this grade: the run's reader decides, and the page and the report's note say so. A dark
  sky or a black or cyan UI panel costs a look, not a wrong verdict.
- "inconclusive" (a failure, never a pass): expect none with a prescribed picture missing, unreadable or not
  gameplay (a fade, a loading screen: fewer than half of its whole cells textured), or expect seen without a
  usable saved frame with an overwriting drop in the rounds; "inconclusive (geometry)" when a picture is not
  of a whole scene's shape (the grid would be guessed).

A tile: a whole cell of the scene's 64-pixel grid (64*W/1040 x 64*H/620 pixels of a W x H picture, origin
0,0: the 1040x620 scene stretched to the 16:9 front buffer; cells cut by the edge are left out), flat
(standard deviation <= 3 in every channel over its inner 80%), black (every mean <= 6) or cyan (0,254,255
within 3), beside a whole cell that is not flat and differs by more than 20. A hole half covered by a later
effect (lightning) is not flat and is missed. The grid assumes the whole scene fills the picture from 0,0
(16:9, or the scene's own 1040:620); any other shape is "inconclusive (geometry)". Standard library only
(PNG: zlib and the five filters; a malformed or truncated file is an unreadable picture, never an
exception)."""
from __future__ import annotations

import re
import struct
import zlib
from datetime import datetime, timedelta
from pathlib import Path

NAME = "drop_tiles"
SCENE_W, SCENE_H, CELL = 1040, 620, 64
# The shapes a whole-scene picture has: the 16:9 front buffer the scene is stretched to, or the scene itself.
ASPECTS = (1280 / 720, SCENE_W / SCENE_H)
STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]")
SAVED = re.compile(r"Pipeline wait: frame (\d+) saved as (pipeline-drop-f\d+-(\d{6})-(\d{3})\.png) \((\d+) "
                   r"overwriting, (\d+) depth-writing draws dropped in it;.*?window (\d+);")


class Unreadable(Exception):
    pass


def check_spec(g: object, names: list[str]) -> str | None:
    """Why the grade's table is wrong, or None."""
    if not isinstance(g, dict):
        return "must be a table"
    if g.get("expect") not in ("seen", "none"):
        return '`expect` must be "seen" or "none"'
    if not isinstance(g.get("marker"), str) or not g["marker"].strip():
        return "`marker` must name the script's round markers (`log <marker> round <k>`)"
    if g.get("peer", "H") not in names:
        return f"`peer` must be one of {names}"
    return None


# --- pictures -------------------------------------------------------------------------------------------------


def read_png(path: Path) -> tuple[int, int, int, bytes]:
    """(width, height, bytes per pixel, rows without filter bytes) of an 8-bit RGB or RGBA PNG; Unreadable for
    anything else, a missing file, a malformed or truncated one."""
    try:
        data = path.read_bytes()
    except OSError as e:
        raise Unreadable(f"cannot read: {e}") from None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise Unreadable("not a PNG")
    pos, idat, width, height, bpp = 8, [], 0, 0, 0
    while True:
        if pos + 8 > len(data):
            raise Unreadable("truncated (no IEND)")
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if pos + 12 + n > len(data):
            raise Unreadable(f"truncated {kind!r} chunk")
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            if n < 13:
                raise Unreadable("short IHDR")
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body[:13])
            if depth != 8 or ctype not in (2, 6) or interlace != 0:
                raise Unreadable(f"depth {depth}, colour type {ctype}, interlace {interlace}: not handled")
            bpp = 4 if ctype == 6 else 3
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + n
    if not width or not height or not bpp:
        raise Unreadable("no IHDR")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise Unreadable(f"image data: {e}") from None
    stride = width * bpp
    if len(raw) < (stride + 1) * height:
        raise Unreadable("image data too short")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    i = 0
    for y in range(height):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if f == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif f != 0:
            raise Unreadable(f"filter {f}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bpp, bytes(out)


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Picture:
    """A decoded picture with its cells' statistics, its tiles and whether it is usable gameplay."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.width, self.height, self.bpp, self.px = read_png(path)
        self.geometry_ok = any(abs(self.width / self.height - a) <= 0.02 for a in ASPECTS)
        self.cw, self.ch = CELL * self.width / SCENE_W, CELL * self.height / SCENE_H
        cols, rows = int(self.width // self.cw), int(self.height // self.ch)  # whole cells only
        self.stats: dict[tuple[int, int], tuple[list[float], list[float]]] = {}
        for r in range(rows):
            for c in range(cols):
                x0, x1 = int(c * self.cw), int((c + 1) * self.cw)
                y0, y1 = int(r * self.ch), int((r + 1) * self.ch)
                mx, my = max(1, (x1 - x0) // 10), max(1, (y1 - y0) // 10)
                n, s, q = 0, [0, 0, 0], [0, 0, 0]
                for y in range(y0 + my, y1 - my, 2):
                    base = y * self.width * self.bpp
                    for x in range(x0 + mx, x1 - mx, 2):
                        o = base + x * self.bpp
                        for k in range(3):
                            v = self.px[o + k]
                            s[k] += v
                            q[k] += v * v
                        n += 1
                if n:
                    mean = [s[k] / n for k in range(3)]
                    std = [max(0.0, q[k] / n - mean[k] ** 2) ** 0.5 for k in range(3)]
                    self.stats[(c, r)] = (mean, std)
        textured = sum(1 for _, std in self.stats.values() if max(std) > 3)
        # Gameplay: most of the scene textured (a fade to black or a loading screen is mostly flat).
        self.usable = self.geometry_ok and bool(self.stats) and textured * 2 >= len(self.stats)
        self.tiles: list[tuple[int, int, str]] = []
        for (c, r), (mean, std) in sorted(self.stats.items()):
            if max(std) > 3:
                continue
            if max(mean) <= 6:
                kind = "black"
            elif abs(mean[0]) <= 3 and abs(mean[1] - 254) <= 3 and abs(mean[2] - 255) <= 3:
                kind = "cyan"
            else:
                continue
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                other = self.stats.get((c + dc, r + dr))
                if other and max(other[1]) > 3 and max(abs(other[0][k] - mean[k]) for k in range(3)) > 20:
                    self.tiles.append((c, r, kind))
                    break

    def crop(self, c: int, r: int, out: Path) -> None:
        """The cell with one cell around it, as an RGB PNG."""
        x0, y0 = max(0, int((c - 1) * self.cw)), max(0, int((r - 1) * self.ch))
        x1, y1 = min(self.width, int((c + 2) * self.cw)), min(self.height, int((r + 2) * self.ch))
        rows = []
        for y in range(y0, y1):
            base = y * self.width * self.bpp
            for x in range(x0, x1):
                o = base + x * self.bpp
                rows.append(self.px[o:o + 3])
        write_png(out, x1 - x0, y1 - y0, b"".join(rows))


def tiles(path: Path) -> list[tuple[int, int, str]]:
    """The tiles of a picture: (column, row, "black" | "cyan"). Unreadable for a bad file."""
    return Picture(path).tiles


# --- the script, the log --------------------------------------------------------------------------------------


def prescribed_shots(script: str, marker: str) -> dict[int, list[str]]:
    """Round k's `shot <name>` pictures (as file names), from the script's own lines."""
    start = re.compile(rf"^log {re.escape(marker)} round (\d+)$")
    end = re.compile(rf"^log {re.escape(marker)} end$")
    shot = re.compile(r"^shot (\S+)$")
    out: dict[int, list[str]] = {}
    current = None
    for raw in script.splitlines():
        for part in raw.split(";"):  # the input script splits commands at every ';'
            line = part.split("#", 1)[0].strip()
            if m := start.match(line):
                current = int(m.group(1))
                out.setdefault(current, [])
            elif end.match(line):
                current = None
            elif current is not None and (m := shot.match(line)):
                name = m.group(1)
                out[current].append(name if name.endswith(".png") else name + ".png")
    return out


def _time(line: str) -> datetime | None:
    m = STAMP.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f") if m else None


def round_spans(text: str, marker: str) -> list[tuple[int, datetime, datetime]]:
    """The attack rounds in the log: (k, from its marker, to the next marker or the end marker)."""
    start = re.compile(rf"Input script: {re.escape(marker)} round (\d+)$")
    end = re.compile(rf"Input script: {re.escape(marker)} end$")
    marks: list[tuple[datetime, int | None]] = []
    for line in text.splitlines():
        line = line.rstrip()
        t = _time(line)
        if t is None:
            continue
        if m := start.search(line):
            marks.append((t, int(m.group(1))))
        elif end.search(line):
            marks.append((t, None))
    return [(k, t, marks[i + 1][0]) for i, (t, k) in enumerate(marks) if k is not None and i + 1 < len(marks)]


def saved_frames(text: str, spans: list[tuple[int, datetime, datetime]]) -> list[dict]:
    """pipeline_drop_capture's frames with their capture time (the file's name, at the frame's readback) and
    the round that time falls into."""
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        logged = _time(line)
        m = SAVED.search(line)
        if logged is None or m is None:
            continue
        hhmmss, ms = m.group(3), int(m.group(4))
        captured = logged.replace(hour=int(hhmmss[:2]), minute=int(hhmmss[2:4]), second=int(hhmmss[4:]),
                                  microsecond=ms * 1000)
        if captured > logged + timedelta(minutes=1):  # captured before midnight, logged after it
            captured -= timedelta(days=1)
        k = next((k for k, a, b in spans if a <= captured < b), None)
        out.append({"frame": int(m.group(1)), "file": m.group(2), "captured": captured, "round": k,
                    "overwriting": int(m.group(5)), "depth": int(m.group(6)), "window": int(m.group(7))})
    return out


# --- the grade ------------------------------------------------------------------------------------------------


def grade(folder: Path, text: str, g: dict) -> tuple[dict, list[str]]:
    """The grade on one peer: its run folder (pictures, script.txt) and its log. Returns the graded record
    (with a `note` for the report) and the failure reasons (none for "seen", "none" and "needs reading")."""
    marker = g["marker"]
    try:
        script = (folder / "script.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        script = ""
    shots = prescribed_shots(script, marker)
    spans = round_spans(text, marker)
    frames = saved_frames(text, spans)
    graded: dict = {"expect": g["expect"], "rounds": len(spans), "prescribed": sum(len(v) for v in shots.values()),
                    "saved_frames": len(frames)}
    page: list[str] = [f"# drop_tiles: {folder.name}", "", f"expect: {g['expect']}, marker: `{marker}`", ""]
    crops = folder / "drop_tiles"
    candidates: list[dict] = []
    unusable: list[str] = []
    geometry: list[str] = []

    def look(name: str, where: str) -> Picture | None:
        try:
            pic = Picture(folder / name)
        except Unreadable as e:
            unusable.append(f"{name} ({where}): unreadable: {e}")
            return None
        if not pic.geometry_ok:
            geometry.append(f"{name} ({where}): {pic.width}x{pic.height}, neither 16:9 nor the scene's 1040:620")
            return None
        if not pic.usable:
            unusable.append(f"{name} ({where}): not gameplay (mostly flat: a fade or a loading screen)")
            return pic
        return pic

    def note_candidates(pic: Picture, name: str, info: dict) -> None:
        for c, r, kind in pic.tiles:
            crops.mkdir(exist_ok=True)
            crop_name = f"{Path(name).stem}-c{c}r{r}.png"
            pic.crop(c, r, crops / crop_name)
            candidates.append({**info, "file": name, "cell": [c, r], "kind": kind, "crop": f"drop_tiles/{crop_name}"})

    # Every picture of the rounds: the prescribed shots, then the saved frames.
    usable_shots = 0
    for k in sorted(shots):
        for name in shots[k]:
            pic = look(name, f"round {k} shot")
            if pic is not None and pic.usable:
                usable_shots += 1
                note_candidates(pic, name, {"round": k, "source": "shot"})
    seen = []
    usable_drop_frames = 0
    for f in frames:
        if f["round"] is None:
            continue
        pic = look(f["file"], f"round {f['round']} frame {f['frame']}")
        if pic is None or not pic.usable:
            continue
        before = len(candidates)
        note_candidates(pic, f["file"], {"round": f["round"], "source": "saved frame", "frame": f["frame"],
                                         "window": f["window"], "overwriting": f["overwriting"],
                                         "depth": f["depth"]})
        if f["overwriting"] > 0:
            usable_drop_frames += 1
            seen += [c for c in candidates[before:]]
    graded.update({"usable_shots": usable_shots, "usable_drop_frames": usable_drop_frames,
                   "candidates": candidates, "seen": seen, "unusable": unusable, "geometry": geometry})

    reasons: list[str] = []
    if geometry:
        verdict = "inconclusive (geometry)"
        reasons.append(f"grade {NAME}: INCONCLUSIVE (geometry): {len(geometry)} picture(s) not of a whole scene's "
                       f"shape, the 64-pixel grid would be guessed: {geometry[0]}")
    elif not spans:
        verdict = "inconclusive"
        reasons.append(f"grade {NAME}: INCONCLUSIVE: no `{marker} round <k>` markers in the log")
    elif g["expect"] == "seen":
        if seen:
            verdict = "seen"
        elif candidates:
            verdict = "needs reading"
        elif not usable_drop_frames:
            verdict = "inconclusive"
            reasons.append(f"grade {NAME}: INCONCLUSIVE: no usable picture of a frame with an overwriting drop "
                           f"inside an attack round ({len(frames)} frame(s) saved in all)")
        else:
            verdict = "none"
            reasons.append(f"grade {NAME}: NOT SEEN: {usable_drop_frames} frame(s) with overwriting drops inside "
                           f"the attack rounds, no black or cyan 64x64 tile in any picture")
    else:
        missing = graded["prescribed"] - usable_shots
        if not shots:
            verdict = "inconclusive"
            reasons.append(f"grade {NAME}: INCONCLUSIVE: the script prescribes no `shot` inside `{marker}` rounds")
        elif missing:
            verdict = "inconclusive"
            reasons.append(f"grade {NAME}: INCONCLUSIVE: {missing} of {graded['prescribed']} prescribed pictures "
                           f"missing, unreadable or not gameplay: {unusable[0] if unusable else ''}")
        elif candidates:
            verdict = "needs reading"
        else:
            verdict = "none"
    graded["verdict"] = verdict

    if verdict == "needs reading":
        graded["note"] = (f"{NAME}: NEEDS READING: {len(candidates)} candidate tile(s), crops and cells in "
                          f"drop_tiles.md beside the pictures; this grade neither passes nor fails the case: the "
                          f"run's reader decides")
    elif verdict == "seen":
        graded["note"] = (f"{NAME}: SEEN: {len(seen)} tile(s) in frame(s) with their own overwriting drop "
                          f"(drop_tiles.md)")
    else:
        graded["note"] = f"{NAME}: {verdict.upper()} (drop_tiles.md)"
    page += [f"Verdict: **{verdict}**", "", graded["note"], "",
             f"Rounds {len(spans)}; prescribed pictures {graded['prescribed']}, usable {usable_shots}; saved frames "
             f"{len(frames)}, usable with an overwriting drop in a round {usable_drop_frames}.", ""]
    if candidates:
        page += ["| Picture | Source | Round | Cell | Kind | Frame | Window | Overwriting | Depth | Crop |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for c in candidates:
            page.append(f"| {c['file']} | {c['source']} | {c['round']} | {c['cell'][0]},{c['cell'][1]} | {c['kind']} "
                        f"| {c.get('frame', '-')} | {c.get('window', '-')} | {c.get('overwriting', '-')} "
                        f"| {c.get('depth', '-')} | {c['crop']} |")
        page.append("")
    for title, items in (("Unusable pictures", unusable), ("Geometry", geometry)):
        if items:
            page += [f"## {title}", ""] + [f"- {x}" for x in items] + [""]
    try:
        (folder / "drop_tiles.md").write_text("\n".join(page) + "\n", encoding="utf-8")
    except OSError:
        pass
    return graded, reasons
