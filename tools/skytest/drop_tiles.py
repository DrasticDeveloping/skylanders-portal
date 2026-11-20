"""skytest's `drop_tiles` grade (stage 8, the black and cyan squares): judges a run by its PICTURES, not
only by its log lines. A dropped draw that overwrites its render target (rexglue-sdk, with async shader
compilation) leaves the target's old contents; in a game that lights its 3D scene in screen tiles that is
a flat black or cyan tile of 64x64 scene pixels. The spec's script marks attack rounds with `log <marker>
round <k>` lines and ends them with `log <marker> end`; the game saves pictures beside its log: `shot`
pictures (`Input script: shot <file>`) and, with rexglue-sdk's pipeline_drop_capture, the guest output of
each frame that dropped a hole-making draw (`Pipeline wait: frame <n> saved as <file> (<o> overwriting, <d>
depth-writing draws dropped in it; ...; window <w>; ...)`).

    [grade.drop_tiles]
    expect = "seen"            # "seen": the fix off; "none": the fix on, or the synchronous control
    marker = "gpu-pipeline-drops"
    # peer = "H"               # whose folder and log
    # min_frames = 6           # "none": fewer usable pictures inside the rounds is inconclusive

- expect = "seen": the pictures saved for frames whose OWN line counts at least one overwriting draw,
  inside an attack round. At least one of them must show a tile (the drop and the hole in one frame). No
  such picture, or none readable: INCONCLUSIVE (a failure, never a pass). Such pictures and no tile: NOT
  SEEN (a failure).
- expect = "none": every picture inside an attack round (shots and saved frames alike, read without the
  game's own classification) must show no tile; fewer than min_frames readable ones: INCONCLUSIVE.

A tile: a whole cell of the scene's 64-pixel grid (64*W/1040 x 64*H/620 picture pixels of a W x H
picture, origin 0,0; cells cut by the picture's edge are left out), flat (standard deviation <= 3 in every
channel over its inner 80%), black (every channel's mean <= 6) or cyan (0,254,255 within 3), with at
least one whole neighbouring cell that is not flat and differs by more than 20 (so a black screen or a
fade is not a tile). Standard library only (PNG: zlib and the five filters)."""
from __future__ import annotations

import re
import struct
import zlib
from datetime import datetime
from pathlib import Path

NAME = "drop_tiles"
SCENE_W, SCENE_H, CELL = 1040, 620, 64
STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]")
SHOT = re.compile(r"Input script: shot (\S+\.png)$")
SAVED = re.compile(r"Pipeline wait: frame (\d+) saved as (\S+\.png) \((\d+) overwriting, (\d+) depth-writing "
                   r"draws dropped in it;.*?window (\d+);")


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
    min_frames = g.get("min_frames", 6)
    if not isinstance(min_frames, int) or isinstance(min_frames, bool) or min_frames < 1:
        return "`min_frames` must be a whole number >= 1"
    return None


def read_png(path: Path) -> tuple[int, int, int, bytes]:
    """(width, height, bytes per pixel, rows without filter bytes) of an 8-bit RGB or RGBA PNG."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width, height, bpp = 8, [], 0, 0, 0
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != 8 or ctype not in (2, 6) or body[12] != 0:
                raise ValueError(f"depth {depth}, colour type {ctype}, interlace {body[12]}: not handled")
            bpp = 4 if ctype == 6 else 3
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + n
    if not width or not height:
        raise ValueError("no IHDR")
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    if len(raw) < (stride + 1) * height:
        raise ValueError("image data too short")
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
            raise ValueError(f"filter {f}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bpp, bytes(out)


def tiles(path: Path) -> list[tuple[int, int, str]]:
    """The tiles of a picture: (column, row, "black" | "cyan")."""
    width, height, bpp, px = read_png(path)
    cw, ch = CELL * width / SCENE_W, CELL * height / SCENE_H
    cols, rows = int(width // cw), int(height // ch)  # whole cells only
    stats: dict[tuple[int, int], tuple[list[float], list[float]]] = {}
    for r in range(rows):
        for c in range(cols):
            x0, x1 = int(c * cw), int((c + 1) * cw)
            y0, y1 = int(r * ch), int((r + 1) * ch)
            mx, my = max(1, (x1 - x0) // 10), max(1, (y1 - y0) // 10)
            n, s, q = 0, [0, 0, 0], [0, 0, 0]
            for y in range(y0 + my, y1 - my, 2):
                base = y * width * bpp
                for x in range(x0 + mx, x1 - mx, 2):
                    o = base + x * bpp
                    for k in range(3):
                        v = px[o + k]
                        s[k] += v
                        q[k] += v * v
                    n += 1
            if n:
                mean = [s[k] / n for k in range(3)]
                std = [max(0.0, q[k] / n - mean[k] ** 2) ** 0.5 for k in range(3)]
                stats[(c, r)] = (mean, std)
    found = []
    for (c, r), (mean, std) in sorted(stats.items()):
        if max(std) > 3:
            continue
        if max(mean) <= 6:
            kind = "black"
        elif abs(mean[0]) <= 3 and abs(mean[1] - 254) <= 3 and abs(mean[2] - 255) <= 3:
            kind = "cyan"
        else:
            continue
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            other = stats.get((c + dc, r + dr))
            if other and max(other[1]) > 3 and max(abs(other[0][k] - mean[k]) for k in range(3)) > 20:
                found.append((c, r, kind))
                break
    return found


def _time(line: str) -> datetime | None:
    m = STAMP.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f") if m else None


def rounds(text: str, marker: str) -> list[tuple[int, datetime, datetime]]:
    """The attack rounds: (k, from its marker, to the next marker or the end marker)."""
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
    out = []
    for i, (t, k) in enumerate(marks):
        if k is not None and i + 1 < len(marks):
            out.append((k, t, marks[i + 1][0]))
    return out


def _round_of(t: datetime, spans: list[tuple[int, datetime, datetime]]) -> int | None:
    for k, a, b in spans:
        if a <= t < b:
            return k
    return None


def grade(folder: Path, text: str, g: dict) -> tuple[dict, list[str]]:
    """The grade on one peer: its run folder (the pictures) and its log."""
    spans = rounds(text, g["marker"])
    saved, shots = [], []
    for line in text.splitlines():
        line = line.rstrip()
        t = _time(line)
        if t is None:
            continue
        if m := SAVED.search(line):
            saved.append({"time": t, "frame": int(m.group(1)), "file": m.group(2), "overwriting": int(m.group(3)),
                          "depth": int(m.group(4)), "window": int(m.group(5)), "round": _round_of(t, spans)})
        elif m := SHOT.search(line):
            shots.append({"time": t, "file": m.group(1), "round": _round_of(t, spans)})
    graded: dict = {"expect": g["expect"], "rounds": len(spans), "saved": len(saved), "shots": len(shots)}
    if not spans:
        graded["verdict"] = "inconclusive"
        return graded, [f"grade {NAME}: INCONCLUSIVE: no `{g['marker']} round <k>` markers in the log"]

    def look(entry: dict) -> list[tuple[int, int, str]] | None:
        try:
            return tiles(folder / entry["file"])
        except (OSError, ValueError, zlib.error) as e:
            entry["unreadable"] = str(e)
            return None

    if g["expect"] == "seen":
        candidates = [e for e in saved if e["round"] is not None and e["overwriting"] > 0]
        graded["candidates"] = len(candidates)
        found, readable = [], 0
        for e in candidates:
            cells = look(e)
            if cells is None:
                continue
            readable += 1
            if cells:
                found.append({"file": e["file"], "frame": e["frame"], "window": e["window"], "round": e["round"],
                              "overwriting": e["overwriting"], "depth": e["depth"], "tiles": cells})
        graded["readable"], graded["seen"] = readable, found
        if found:
            graded["verdict"] = "seen"
            return graded, []
        if not readable:
            graded["verdict"] = "inconclusive"
            return graded, [f"grade {NAME}: INCONCLUSIVE: no readable picture of a frame with an overwriting "
                            f"drop inside an attack round ({len(candidates)} such picture(s), {len(saved)} saved "
                            f"in all)"]
        graded["verdict"] = "not seen"
        return graded, [f"grade {NAME}: NOT SEEN: {readable} picture(s) of frames with overwriting drops inside "
                        f"the attack rounds, no black or cyan 64x64 tile in any"]

    pictures = [e for e in shots + saved if e["round"] is not None]
    min_frames = int(g.get("min_frames", 6))
    bad, readable = [], 0
    for e in pictures:
        cells = look(e)
        if cells is None:
            continue
        readable += 1
        if cells:
            bad.append({"file": e["file"], "round": e["round"], "tiles": cells})
    graded["readable"], graded["tiles"] = readable, bad
    if bad:
        graded["verdict"] = "tiles"
        names = ", ".join(f"{b['file']} (round {b['round']}, {len(b['tiles'])} tile(s))" for b in bad[:6])
        return graded, [f"grade {NAME}: black or cyan 64x64 tiles inside the attack rounds: {names}"
                        + (f" and {len(bad) - 6} more" if len(bad) > 6 else "")]
    if readable < min_frames:
        graded["verdict"] = "inconclusive"
        return graded, [f"grade {NAME}: INCONCLUSIVE: {readable} readable picture(s) inside the attack rounds, "
                        f"fewer than {min_frames}"]
    graded["verdict"] = "none"
    return graded, []
