#!/usr/bin/env python3
"""Check the declared theme pairs against WCAG contrast thresholds."""

import re
from pathlib import Path

source = (Path(__file__).resolve().parent / "tailwind.input.css").read_text()
light, dark = (
    dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-f]{6});", block))
    for block in (
        source.split(':root, [data-theme="lens-light"] {', 1)[1].split("}", 1)[0],
        source.split('[data-theme="lens-dark"] {', 1)[1].split("}", 1)[0],
    )
)


def luminance(color):
    channels = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    linear = (x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in channels)
    return sum(x * weight for x, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def ratio(foreground, background):
    a, b = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)


failures = []
for name, palette in (("lens-light", light), ("lens-dark", dark)):
    checks = []
    status_colors = {palette[f"--color-{role}"] for role in ("info", "success", "warning", "error")}
    for level in ("error", "warn", "info", "debug"):
        if palette[f"--level-{level}"] in status_colors:
            failures.append(f"{name}: level-{level} must not reuse a status colour")
    for base in ("base-100", "base-200", "base-300"):
        for foreground in ("color-base-content", "lens-muted"):
            checks.append((foreground, f"color-{base}", 4.5))
    for role in ("primary", "secondary", "accent", "neutral", "info", "success", "warning", "error"):
        checks.append((f"color-{role}-content", f"color-{role}", 4.5))
        checks.append((f"color-{role}", "color-base-100", 3))
    for level in ("error", "warn", "info", "debug"):
        checks.extend(
            (
                (f"level-{level}", f"level-{level}-bg", 4.5),
                (f"level-{level}", "color-base-100", 4.5),
            )
        )
    for control in ("lens-chart-grid", "lens-control-border"):
        checks.append((control, "color-base-100", 3))
    values = {4.5: [], 3: []}
    for fg, bg, minimum in checks:
        measured = ratio(palette[f"--{fg}"], palette[f"--{bg}"])
        values[minimum].append(measured)
        if measured < minimum:
            failures.append(f"{name}: {fg}/{bg} {measured:.2f}:1 < {minimum}:1")
    print(
        f"{name}: {len(checks)} pairs; minimum text {min(values[4.5]):.2f}:1, "
        f"minimum graphical {min(values[3]):.2f}:1 (individual ratios below)"
    )
    for fg, bg, minimum in checks:
        print(f"  {fg} / {bg}: {ratio(palette[f'--{fg}'], palette[f'--{bg}']):.2f}:1 (>= {minimum}:1)")

if failures:
    raise SystemExit("\n".join(failures))
