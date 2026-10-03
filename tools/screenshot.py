#!/usr/bin/env python3
"""Render the demo dashboard to SVG screenshots for the README (docs/*.svg).

Uses the console glyph set, so icons and bar glyphs are drawn pixel-for-pixel
from the same bitmaps that go into the generated console font.
"""
import html
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("glance", ROOT / "glance.py")
glance = importlib.util.module_from_spec(spec)
sys.modules["glance"] = glance
spec.loader.exec_module(glance)
glance.T = glance.Term("console", "truecolor")

CW, LH, FS = 8.4, 17, 14       # cell width/height and font size in the SVG
GW, GH = 14, 28                # console glyph bitmap size
COLS, ROWS = 132, 37
BG = glance.THEME["base"]
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def cells(line: str):
    """Yield (col, char, fg, bg, bold) for every cell of an ANSI line."""
    fg, bg, bold, col = None, None, False, 0
    for tok in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = SGR.fullmatch(tok)
        if m:
            codes = [int(c or 0) for c in m.group(1).split(";")]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    fg, bg, bold = None, None, False
                elif c == 1:
                    bold = True
                elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                    color = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                    fg, bg = (color, bg) if c == 38 else (fg, color)
                    i += 4
                i += 1
            continue
        for ch in tok:
            yield col, ch, fg or glance.THEME["text"], bg, bold
            col += glance.cwidth(ch)


def glyph_rects(bitmap, x0, y0, color):
    """Merge lit pixels into rectangles (horizontal runs, stacked when identical)."""
    out, open_runs = [], {}
    for y, row in enumerate(bitmap + [[False] * GW]):
        runs, x = set(), 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                runs.add((start, x))
            x += 1
        for run in list(open_runs):
            if run not in runs:
                y_start = open_runs.pop(run)
                out.append(f'<rect x="{x0 + run[0] * CW / GW:.2f}" y="{y0 + y_start * LH / GH:.2f}" '
                           f'width="{(run[1] - run[0]) * CW / GW:.2f}" height="{(y - y_start) * LH / GH:.2f}" fill="{color}"/>')
        for run in runs:
            open_runs.setdefault(run, y)
    return out


def to_svg(snap, title):
    lines = glance.render_lines(snap, COLS, ROWS, vt=8)
    pad, bar = 16, 30
    width, height = COLS * CW + pad * 2, ROWS * LH + pad * 2 + bar
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" '
           f'font-family="\'JetBrains Mono\',\'DejaVu Sans Mono\',Menlo,Consolas,monospace" font-size="{FS}" shape-rendering="crispEdges">',
           f'<rect width="100%" height="100%" rx="12" fill="{BG}"/>',
           '<circle cx="20" cy="15" r="5.5" fill="#f38ba8"/><circle cx="38" cy="15" r="5.5" fill="#f9e2af"/>'
           '<circle cx="56" cy="15" r="5.5" fill="#a6e3a1"/>',
           f'<text x="{width / 2:.0f}" y="20" fill="#7f849c" text-anchor="middle" font-size="12">{html.escape(title)}</text>']
    for row, line in enumerate(lines):
        y = bar + pad + row * LH
        text_run = None
        for col, ch, fg, bg, bold in list(cells(line)) + [(COLS, "", None, None, False)]:
            x = pad + col * CW
            if bg and ch:
                out.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{CW + 0.3:.2f}" height="{LH + 0.3:.2f}" fill="{bg}"/>')
            bitmap = glance.custom_glyph(ch, GW, GH) if ch else None
            if bitmap is None and ch == "█":
                bitmap = [[True] * GW for _ in range(GH)]
            plain = ch and bitmap is None and ch != " "
            key = (fg, bold)
            if text_run and (not plain or text_run[2] != key or text_run[3] != col):
                tx, chars, (tfg, tbold), _ = text_run
                weight = ' font-weight="bold"' if tbold else ""
                out.append(f'<text x="{tx:.2f}" y="{y + LH - 4:.2f}" fill="{tfg}"{weight} xml:space="preserve" '
                           f'textLength="{len(chars) * CW:.2f}" lengthAdjust="spacingAndGlyphs">{html.escape(chars)}</text>')
                text_run = None
            if bitmap is not None:
                out += glyph_rects(bitmap, x, y, fg)
            elif plain:
                text_run = (text_run[0], text_run[1] + ch, key, col + 1) if text_run else (x, ch, key, col + 1)
    out.append("</svg>")
    return "\n".join(out)


def healthy():
    snap = glance.demo_snapshot(t=1000)
    d = snap.data
    d["services"]["watched"] = [(n, s, "active") for n, s, _ in d["services"]["watched"]]
    d["disks"][0].update(pct=52, avail_gb=221.4)
    d["docker"]["running"] = 12
    snap.issues = glance.evaluate(d)
    return snap


def needs_attention():
    snap = glance.demo_snapshot(t=1000)
    d = snap.data
    d["power"] = {"battery": {"capacity": 23, "status": "Discharging", "eta": 4140, "health": 91, "watts": 11.2}, "ac": False}
    d["temps"] = [("CPU", 84.0), ("NVMe", 47.0), ("WiFi", 41.0)]
    d["runners"][2] = dict(d["runners"][2], state="down", reason="oom-kill")
    snap.issues = glance.evaluate(d)
    return snap


if __name__ == "__main__":
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "healthy.svg").write_text(to_svg(healthy(), "tty8 · all good"))
    (docs / "issues.svg").write_text(to_svg(needs_attention(), "tty8 · something needs attention"))
    print("wrote docs/healthy.svg, docs/issues.svg")
