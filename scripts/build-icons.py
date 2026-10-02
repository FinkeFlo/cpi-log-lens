#!/usr/bin/env python3
"""Build the offline Lucide sprite and CPI Log Lens logo/favicon assets."""

from __future__ import annotations

import hashlib
import math
import struct
import tarfile
import urllib.request
import zlib
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
LUCIDE_VERSION = "0.468.0"
LUCIDE_URL = (
    "https://registry.npmjs.org/lucide-static/-/"
    f"lucide-static-{LUCIDE_VERSION}.tgz"
)
LUCIDE_SHA256 = "d87c47325f0b2ac3ad2d6f056889da32b8f1216eafe4ed6534bc02786effe8cc"
ICON_NAMES = (
    "calendar-clock",
    "chart-column",
    "chevron-down",
    "chevron-left",
    "chevron-right",
    "chevron-up",
    "circle-check",
    "circle-dashed",
    "circle-x",
    "copy",
    "download",
    "eraser",
    "filter-x",
    "hourglass",
    "inbox",
    "info",
    "key-round",
    "link",
    "pencil",
    "play",
    "rotate-ccw",
    "save",
    "search",
    "search-x",
    "server-off",
    "settings",
    "square",
    "trash-2",
    "triangle-alert",
    "x",
)
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def lucide_archive() -> bytes:
    request = urllib.request.Request(
        LUCIDE_URL, headers={"User-Agent": "cpi-log-lens-icon-builder"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        archive = response.read(25_000_001)
    if len(archive) > 25_000_000:
        raise RuntimeError("Pinned Lucide archive is unexpectedly large")
    actual = hashlib.sha256(archive).hexdigest()
    if actual != LUCIDE_SHA256:
        raise RuntimeError(
            f"Lucide {LUCIDE_VERSION} checksum mismatch: expected "
            f"{LUCIDE_SHA256}, received {actual}"
        )
    return archive


def make_sprite(archive: bytes) -> bytes:
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {"aria-hidden": "true"},
    )
    with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as package:
        for name in ICON_NAMES:
            member = package.getmember(f"package/icons/{name}.svg")
            source = ET.fromstring(package.extractfile(member).read())
            symbol = ET.SubElement(
                root,
                f"{{{SVG_NS}}}symbol",
                {
                    "id": name,
                    "viewBox": source.attrib.get("viewBox", "0 0 24 24"),
                    "fill": source.attrib.get("fill", "none"),
                    "stroke": source.attrib.get("stroke", "currentColor"),
                    "stroke-width": source.attrib.get("stroke-width", "2"),
                    "stroke-linecap": source.attrib.get("stroke-linecap", "round"),
                    "stroke-linejoin": source.attrib.get("stroke-linejoin", "round"),
                },
            )
            symbol.extend(list(source))
        license_member = package.getmember("package/LICENSE")
        license_text = package.extractfile(license_member).read()
    (FRONTEND / "lucide.LICENSE").write_bytes(license_text)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def logo_svg(title: str | None = None) -> bytes:
    title_markup = f"<title>{title}</title>" if title else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 32 32" width="32" height="32">'
        f"{title_markup}"
        '<rect x="1" y="1" width="30" height="30" rx="7.5" fill="#101827"/>'
        '<circle cx="12.5" cy="12.5" r="7.25" fill="none" '
        'stroke="#dfe7ff" stroke-width="2.1"/>'
        '<path d="M18 18 25 25" fill="none" stroke="#dfe7ff" '
        'stroke-width="2.5" stroke-linecap="round"/>'
        '<path d="M9 10.2h7 M9 15.9h5" fill="none" stroke="#8bb1ff" '
        'stroke-width="1.5" stroke-linecap="round"/>'
        '<path d="M9 13.05h7" fill="none" stroke="#ff736f" '
        'stroke-width="1.5" stroke-linecap="round"/>'
        "</svg>\n"
    ).encode("utf-8")


def segment_distance(
    x: float,
    y: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    t = max(
        0.0,
        min(1.0, ((x - start[0]) * dx + (y - start[1]) * dy) / length_squared),
    )
    return math.hypot(x - (start[0] + t * dx), y - (start[1] + t * dy))


def rounded_rectangle_contains(x: float, y: float) -> bool:
    half_extent = 7.5
    dx = max(8.5 - x, 0.0, x - 23.5)
    dy = max(8.5 - y, 0.0, y - 23.5)
    return (
        1.0 <= x <= 31.0
        and 1.0 <= y <= 31.0
        and dx * dx + dy * dy <= half_extent**2
    )


def logo_color(x: float, y: float) -> tuple[int, int, int, int]:
    if not rounded_rectangle_contains(x, y):
        return 0, 0, 0, 0
    color = (16, 24, 39, 255)
    if abs(math.hypot(x - 12.5, y - 12.5) - 7.25) <= 1.05:
        color = (223, 231, 255, 255)
    for y_line, x_end, line_color in (
        (10.2, 16.0, (139, 177, 255, 255)),
        (13.05, 16.0, (255, 115, 111, 255)),
        (15.9, 14.0, (139, 177, 255, 255)),
    ):
        if segment_distance(x, y, (9.0, y_line), (x_end, y_line)) <= 0.75:
            color = line_color
    if segment_distance(x, y, (18.0, 18.0), (25.0, 25.0)) <= 1.25:
        color = (223, 231, 255, 255)
    return color


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_png(size: int) -> bytes:
    samples = 4
    scale = size * samples
    supersampled = bytearray()
    for y in range(scale):
        py = (y + 0.5) * 32 / scale
        for x in range(scale):
            px = (x + 0.5) * 32 / scale
            supersampled.extend(logo_color(px, py))

    pixels = bytearray()
    for y in range(size):
        for x in range(size):
            red = green = blue = alpha = 0
            for sy in range(samples):
                start = ((y * samples + sy) * scale + x * samples) * 4
                for sx in range(samples):
                    offset = start + sx * 4
                    pixel_alpha = supersampled[offset + 3]
                    red += supersampled[offset] * pixel_alpha
                    green += supersampled[offset + 1] * pixel_alpha
                    blue += supersampled[offset + 2] * pixel_alpha
                    alpha += pixel_alpha
            if alpha:
                pixels.extend(
                    (
                        red // alpha,
                        green // alpha,
                        blue // alpha,
                        alpha // (samples * samples),
                    )
                )
            else:
                pixels.extend((0, 0, 0, 0))

    rows = b"".join(
        b"\0" + pixels[row * size * 4 : (row + 1) * size * 4]
        for row in range(size)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(rows, level=9))
        + png_chunk(b"IEND", b"")
    )


def main() -> None:
    archive = lucide_archive()
    (FRONTEND / "icons.svg").write_bytes(make_sprite(archive))
    (FRONTEND / "logo.svg").write_bytes(logo_svg())
    (FRONTEND / "favicon.svg").write_bytes(logo_svg("CPI Log Lens"))
    (FRONTEND / "favicon-32.png").write_bytes(make_png(32))
    (FRONTEND / "apple-touch-icon.png").write_bytes(make_png(180))
    print(f"Built {len(ICON_NAMES)} Lucide icons from lucide-static {LUCIDE_VERSION}")


if __name__ == "__main__":
    main()
