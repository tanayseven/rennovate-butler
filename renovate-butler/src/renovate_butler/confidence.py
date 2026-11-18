"""Parse Renovate Merge Confidence out of a Renovate PR body.

Renovate PR bodies contain a markdown table with Mend badge images such as
``https://developer.mend.io/api/mc/badges/confidence/npm/i18next/27.1.1/27.1.2?slim=true``.
The confidence *level* is not in the URL: the badge is an SVG wrapping a PNG
with the word ("low", "neutral", "high", "very high") rendered as pixels. We
classify it by the badge background colour (red / grey / green), and use the
badge width to tell "very high" apart from "high".
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote

LEVELS = ["low", "neutral", "high", "very high"]
UNKNOWN = "unknown"

BADGE_RE = re.compile(
    r"https://[^\s)\"']*/badges/confidence/(?P<manager>[^/]+)/(?P<pkg>.+?)/"
    r"(?P<from>[^/]+)/(?P<to>[^/?\s)]+)(?:\?[^\s)\"']*)?"
)
SEMVER_RE = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")

# Badge slim widths observed from developer.mend.io (2026-11): low=49.9,
# high=55.1, neutral=70.4, very high=83.3.
VERY_HIGH_MIN_WIDTH = 72.0


@dataclass
class Update:
    package: str
    manager: str
    from_version: str
    to_version: str
    update_type: str  # major | minor | patch | unknown
    confidence: str  # one of LEVELS or UNKNOWN


@dataclass
class Confidence:
    updates: list[Update] = field(default_factory=list)

    @property
    def level(self) -> str:
        """Weakest confidence across all updates in the PR."""
        if not self.updates:
            return UNKNOWN
        levels = [u.confidence for u in self.updates]
        if UNKNOWN in levels:
            return UNKNOWN
        return min(levels, key=LEVELS.index)

    @property
    def update_types(self) -> set[str]:
        return {u.update_type for u in self.updates}


def update_type(from_v: str, to_v: str) -> str:
    a, b = SEMVER_RE.match(from_v), SEMVER_RE.match(to_v)
    if not a or not b:
        return "unknown"
    pa = [int(x or 0) for x in a.groups()]
    pb = [int(x or 0) for x in b.groups()]
    if pa[0] != pb[0]:
        return "major"
    if pa[0] == 0 and pa[1] != pb[1]:
        return "major"  # 0.x minor bumps are breaking under semver
    if pa[1] != pb[1]:
        return "minor"
    return "patch"


def meets(level: str, minimum: str) -> bool:
    if level not in LEVELS or minimum not in LEVELS:
        return False
    return LEVELS.index(level) >= LEVELS.index(minimum)


# --------------------------------------------------------------------------- badge decoding


def _png_rgba_rows(png: bytes) -> tuple[int, int, list[bytes]]:
    """Minimal PNG decoder for 8-bit RGBA/RGB non-interlaced images."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat = 8, b""
    width = height = bpp = 0
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        ctype = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data)
            if depth != 8 or color not in (2, 6) or interlace:
                raise ValueError("unsupported PNG format")
            bpp = 4 if color == 6 else 3
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows: list[bytes] = []
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        ftype = raw[i]
        line = bytearray(raw[i + 1 : i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            left = line[x - bpp] if x >= bpp else 0
            up = prev[x]
            ul = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + left) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + up) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + (left + up) // 2) & 0xFF
            elif ftype == 4:
                p = left + up - ul
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else ul)
                line[x] = (line[x] + pred) & 0xFF
        rows.append(bytes(line) if bpp == 4 else bytes(_rgb_to_rgba(line)))
        prev = line
    return width, height, rows


def _rgb_to_rgba(line: bytearray) -> bytearray:
    out = bytearray()
    for j in range(0, len(line), 3):
        out += line[j : j + 3] + b"\xff"
    return out


def classify_badge(svg: str) -> str:
    """Return the confidence level shown by a Mend confidence badge SVG."""
    m = re.search(r"base64,([A-Za-z0-9+/=]+)", svg)
    w = re.search(r'<svg[^>]*\bwidth="([\d.]+)"', svg)
    if not m:
        return UNKNOWN
    try:
        width, height, rows = _png_rgba_rows(base64.b64decode(m.group(1)))
    except (ValueError, zlib.error, struct.error):
        return UNKNOWN
    # Sample the value segment's background near the top edge, above the text.
    row = rows[max(1, height // 8)]
    samples = []
    for fx in (0.55, 0.65, 0.75, 0.85):
        x = int(width * fx)
        r, g, b, a = row[x * 4 : x * 4 + 4]
        if a > 200:
            samples.append((r, g, b))
    if not samples:
        return UNKNOWN
    r = sum(s[0] for s in samples) / len(samples)
    g = sum(s[1] for s in samples) / len(samples)
    b = sum(s[2] for s in samples) / len(samples)
    if r > 150 and g < 120 and b < 120:
        return "low"
    if g > 120 and g > r + 40 and g > b + 40:
        svg_width = float(w.group(1)) if w else 0.0
        return "very high" if svg_width >= VERY_HIGH_MIN_WIDTH else "high"
    if abs(r - g) < 25 and abs(g - b) < 25:
        return "neutral"
    return UNKNOWN


# --------------------------------------------------------------------------- body parsing


def parse(body: str, fetch: Callable[[str], str]) -> Confidence:
    """Parse every confidence badge in a Renovate PR body.

    ``fetch`` takes a badge URL and returns the SVG text; it is injected so
    tests can run offline.
    """
    result = Confidence()
    seen: set[str] = set()
    for m in BADGE_RE.finditer(body or ""):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            level = classify_badge(fetch(url))
        except Exception:  # noqa: BLE001 - any fetch/decode failure means "not confident"
            level = UNKNOWN
        result.updates.append(
            Update(
                package=unquote(m.group("pkg")),
                manager=m.group("manager"),
                from_version=unquote(m.group("from")),
                to_version=unquote(m.group("to")),
                update_type=update_type(unquote(m.group("from")), unquote(m.group("to"))),
                confidence=level,
            )
        )
    return result
