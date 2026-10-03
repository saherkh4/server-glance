#!/usr/bin/env python3
"""Render the demo dashboard to SVG screenshots for the README (docs/*.svg)."""
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

PALETTE = {30: "#1e1e2e", 31: "#f38ba8", 32: "#a6e3a1", 33: "#f5c542", 36: "#89dceb", 37: "#cdd6f4"}
BG = {41: "#e5484d", 42: "#30a46c", 43: "#f5d90a"}
FG, DIM_FG, WIN_BG = "#cdd6f4", "#6c7086", "#11111b"
CW, LH, FS = 8.4, 18, 14
COLS, ROWS = 108, 27


def frame_cells(frame: str):
    """Yield (row, col, text, fg, bg, bold) runs from an ANSI frame."""
    frame = frame.removeprefix("\x1b[H").removesuffix("\x1b[J")
    for row, line in enumerate(frame.split("\n")):
        col, fg, bg, bold, dim = 0, None, None, False, False
        for token in re.split(r"(\x1b\[[0-9;]*m)", line):
            m = re.fullmatch(r"\x1b\[([0-9;]*)m", token)
            if m:
                for code in [int(c or 0) for c in m.group(1).split(";")]:
                    if code == 0:
                        fg, bg, bold, dim = None, None, False, False
                    elif code == 1:
                        bold = True
                    elif code == 2:
                        dim = True
                    elif code in PALETTE:
                        fg = PALETTE[code]
                    elif code in BG:
                        bg = BG[code]
                continue
            if token:
                color = fg or (DIM_FG if dim else FG)
                if dim and fg:
                    color = DIM_FG if token.strip("█░") else "#313244" if "░" in token else color
                yield row, col, token, color, bg, bold
                col += len(token)


def to_svg(snap, title: str) -> str:
    frame = glance.render(snap, COLS, ROWS, vt=8)
    pad, bar = 18, 34
    width, height = COLS * CW + pad * 2, ROWS * LH + pad * 2 + bar
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
           f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="\'JetBrains Mono\',\'DejaVu Sans Mono\','
           f'Menlo,Consolas,monospace" font-size="{FS}">',
           f'<rect width="100%" height="100%" rx="12" fill="{WIN_BG}"/>',
           '<circle cx="22" cy="17" r="6" fill="#f38ba8"/><circle cx="42" cy="17" r="6" fill="#f9e2af"/>'
           '<circle cx="62" cy="17" r="6" fill="#a6e3a1"/>',
           f'<text x="{width / 2:.0f}" y="22" fill="{DIM_FG}" text-anchor="middle" font-size="12">'
           f'{html.escape(title)}</text>']
    for row, col, text, fg, bg, bold in frame_cells(frame):
        x, y = pad + col * CW, bar + pad + row * LH
        if bg:
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{len(text) * CW:.1f}" height="{LH}" fill="{bg}"/>')
        if text and set(text) <= {"█", "░"}:
            # Draw bar glyphs as solid shapes so they look seamless at any zoom.
            for i, run in enumerate(re.findall(r"█+|░+", text)):
                rx = x + (len(text.split(run)[0]) if i == 0 else text.index(run)) * CW
                color = "#313244" if run[0] == "░" else fg
                out.append(f'<rect x="{rx:.1f}" y="{y + 3:.1f}" width="{len(run) * CW:.1f}" '
                           f'height="{LH - 6}" rx="2" fill="{color}"/>')
            continue
        if text.strip():
            fill = "#11111b" if bg else fg
            weight = ' font-weight="bold"' if bold else ""
            out.append(f'<text x="{x:.1f}" y="{y + LH - 5:.1f}" fill="{fill}"{weight} xml:space="preserve" '
                       f'textLength="{len(text) * CW:.1f}" lengthAdjust="spacingAndGlyphs">'
                       f'{html.escape(text)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def healthy():
    snap = glance.demo_snapshot()
    snap.data["services"]["watched"] = [w if w[0] != "backup" else ("backup", "system", "active")
                                        for w in snap.data["services"]["watched"]]
    snap.data["services"]["failed"] = []
    snap.data["system"]["disks"][0].update(pct=52, avail_gb=221.4)
    snap.data["docker"]["running"] = 12
    snap.issues = glance.evaluate(snap.data)
    return snap


def on_battery():
    snap = glance.demo_snapshot()
    snap.data["battery"].update(capacity=23, status="Discharging", eta=4140, watts=11.2)
    snap.data["ac"] = False
    snap.data["temps"] = [("CPU", 84.0), ("NVMe", 47.0), ("WiFi", 41.0)]
    snap.issues = glance.evaluate(snap.data)
    return snap


if __name__ == "__main__":
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "healthy.svg").write_text(to_svg(healthy(), "tty8 - all good"))
    (docs / "issues.svg").write_text(to_svg(on_battery(), "tty8 - something needs attention"))
    print("wrote docs/healthy.svg, docs/issues.svg")
