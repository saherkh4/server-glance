#!/usr/bin/env python3
"""Server Glance - one-glance server status on the local console.

Runs full-screen on a Linux virtual terminal (default tty8). Whenever the lid
is opened or an external screen is plugged in, it switches the console to its
own VT so the status is the first thing you see.

Stdlib only. Read-only: it never executes anything derived from input.
https://github.com/saherkh4/server-glance
"""
from __future__ import annotations

import collections
import fcntl
import glob
import gzip
import json
import math
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

__version__ = "2.0.0"

RENDER_SECONDS = 1
EVENT_POLL_SECONDS = 0.25
HISTORY = 120

DEFAULT_CONFIG = {
    # Units shown in the Services panel, e.g. ["nginx", "docker", "my-app"].
    # Each is looked up as a user unit and a system unit; the healthier one wins.
    "services": [],
    # Failed units matching these regexes are not reported as issues.
    "ignore_failed": [],
    "internet_probes": ["1.1.1.1:443", "8.8.8.8:53"],
    # Show self-hosted GitHub Actions runners (actions.runner.*.service) in their own panel.
    "runners": True,
}

# How often each data source is refreshed (seconds). Things that change by the
# second get a fast cadence; things that rarely change are polled lazily.
CADENCE = {
    "cpu": 1, "memory": 1, "traffic": 1, "diskio": 1, "temps": 2, "power": 10,
    "gpus": 3, "services": 10, "runners": 5, "lan": 10,
    "internet": 30, "docker": 15, "disks": 60, "kernel": 60,
}

VT_ACTIVATE = 0x5606
VT_GETSTATE = 0x5603
# While nobody can see the screen, non-history sources are polled this many times less often.
HIDDEN_SLOWDOWN = 4
HISTORY_SOURCES = {"cpu", "memory", "traffic", "diskio"}
INTERNAL_PANELS = ("eDP", "LVDS", "DSI")

# ---------------------------------------------------------------- theme

THEME = {
    "base": "#11111b", "crit": "#f38ba8", "ok": "#a6e3a1", "warn": "#f9e2af",
    "surface": "#313244", "mauve": "#cba6f7", "accent": "#89dceb", "text": "#cdd6f4",
    "dim": "#7f849c", "peach": "#fab387", "teal": "#94e2d5", "pink": "#f5c2e7",
    "blue": "#89b4fa", "border": "#585b70", "bright": "#ffffff", "white": "#eff1f5",
}
# Console palette slots. Only slots 0-7 can be used as backgrounds on the Linux console.
PALETTE = ["base", "crit", "ok", "warn", "surface", "mauve", "accent", "text",
           "dim", "peach", "teal", "pink", "blue", "border", "bright", "white"]
ANSI_FG = {"base": "30", "crit": "31", "ok": "32", "warn": "33", "surface": "90", "mauve": "35",
           "accent": "36", "text": "37", "dim": "90", "peach": "33", "teal": "36", "pink": "95",
           "blue": "34", "border": "90", "bright": "97", "white": "97"}
ANSI_BG = {"base": "49", "crit": "41", "ok": "42", "warn": "43", "surface": "100",
           "mauve": "45", "accent": "46", "text": "47"}
RESET = "\x1b[0m"

# ---------------------------------------------------------------- glyphs

VBLOCK = " ▁▂▃▄▅▆▇█"
HBLOCK = ["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"]
BLOCK_CHARS = "▁▂▃▄▅▆▇▀▉▊▋▌▍▎▏▐▕"

# 14x14 pixel-art icons. On the console each becomes two custom font glyphs.
ICONS = {
    "host": ["..............", ".############.", ".#..........#.", ".#.##....#..#.", ".#..........#.",
             ".############.", ".#..........#.", ".#.##....#..#.", ".#..........#.", ".############.",
             "......##......", "...########...", "..............", ".............."],
    "battery": ["..............", "..............", "..............", ".###########..", ".#.........#..",
                ".#.#######.##.", ".#.#######.##.", ".#.#######.##.", ".#.#######.##.", ".#.........#..",
                ".###########..", "..............", "..............", ".............."],
    "bolt": ["........###...", ".......###....", "......###.....", ".....###......", "....###.......",
             "...########...", "....########..", ".......###....", "......###.....", ".....###......",
             ".....##.......", "....##........", "....#.........", ".............."],
    "cpu": ["...#.#..#.#...", "...#.#..#.#...", "..##########..", "..#........#..", "###.######.###",
            "..#.#....#.#..", "###.#....#.###", "..#.#....#.#..", "###.######.###", "..#........#..",
            "..##########..", "...#.#..#.#...", "...#.#..#.#...", ".............."],
    "gpu": ["..............", "#.............", "#############.", "#...........#.", "#..####.....#.",
            "#.#....#..#.##", "#.#.##.#..#.##", "#.#....#..#.##", "#..####.....#.", "#...........#.",
            "#############.", "..#.#.#.#.....", "..............", ".............."],
    "wifi": ["..............", "....######....", "..##......##..", ".#..........#.", "#...######...#",
             "...#......#...", "..#..####..#..", ".....#..#.....", "..............", "......##......",
             "......##......", "..............", "..............", ".............."],
    "lan": ["....######....", "....#....#....", "....######....", "......##......", "......##......",
            "..##########..", "..##......##..", "..##......##..", "######..######", "#....#..#....#",
            "######..######", "..............", "..............", ".............."],
    "globe": ["....######....", "..##..##..##..", ".#...#..#...#.", ".#...#..#...#.", "##############",
              "#....#..#....#", "#....#..#....#", "##############", ".#...#..#...#.", ".#...#..#...#.",
              "..##..##..##..", "....######....", "..............", ".............."],
    "gear": ["......##......", "...#..##..#...", "..##########..", "...##....##...", "..##......##..",
             "####..##..####", "####..##..####", "..##......##..", "...##....##...", "..##########..",
             "...#..##..#...", "......##......", "..............", ".............."],
    "github": ["..............", "..#........#..", "..##......##..", "..##########..", ".############.",
               ".##..####..##.", ".##..####..##.", ".############.", "..##########..", "...########...",
               "....##..##....", "....##..##....", "..............", ".............."],
    "disk": ["..............", ".############.", ".#..........#.", ".#..........#.", ".#..........#.",
             ".#..........#.", ".############.", ".#..........#.", ".#.######.#.#.", ".#..........#.",
             ".############.", "..............", "..............", ".............."],
    "thermo": ["......##......", ".....#..#.....", ".....#..#.....", ".....#.##.....", ".....#..#.....",
               ".....#.##.....", ".....#..#.....", ".....####.....", "....######....", "...########...",
               "...########...", "...########...", "....######....", ".............."],
    "ok": ["....######....", "..##......##..", ".#..........#.", ".#.........##.", "#.........##.#",
           "#..##....##..#", "#...##..##...#", "#....####....#", ".#....##....#.", ".#..........#.",
           "..##......##..", "....######....", "..............", ".............."],
    "warn": ["......##......", ".....####.....", ".....#..#.....", "....##..##....", "....#.##.#....",
             "...##.##.##...", "...#..##..#...", "..##..##..##..", "..#........#..", ".##...##...##.",
             ".#....##....#.", "##############", "..............", ".............."],
    "crit": ["....######....", "...########...", "..##########..", ".###.####.###.", ".####.##.####.",
             ".#####..#####.", ".#####..#####.", ".####.##.####.", ".###.####.###.", "..##########..",
             "...########...", "....######....", "..............", ".............."],
    "docker": ["..............", "........##....", ".....##.##....", "..##.##.##....", "..##.##.##.##.",
               "..............", "#############.", "#............#", "#...........#.", ".#..........#.",
               "..##......##..", "....######....", "..............", ".............."],
}
ICON_ORDER = list(ICONS)
ICON_BASE = 0xE100  # Private Use Area codepoints mapped in the generated console font
EMOJI = {"host": "🏠", "battery": "🔋", "bolt": "⚡", "cpu": "🧠", "gpu": "🎮", "wifi": "📶",
         "lan": "🔗", "globe": "🌐", "gear": "🔧", "github": "🐙", "disk": "💾", "thermo": "🔥",
         "ok": "✅", "warn": "🟡", "crit": "🔴", "docker": "🐳"}
ASCII_ICON = {"host": "::", "battery": "=]", "bolt": "+ ", "cpu": "[]", "gpu": "[]", "wifi": "((",
              "lan": "<>", "globe": "()", "gear": "**", "github": "@ ", "disk": "[]", "thermo": "t ",
              "ok": "OK", "warn": "! ", "crit": "X ", "docker": "[]"}
BASIC_MAP = str.maketrans({"▁": " ", "▂": " ", "▃": " ", "▄": "▒", "▅": "▒", "▆": "▒", "▇": "▒",
                           "▀": "▒", "▏": " ", "▎": " ", "▍": " ", "▌": "▒", "▋": "▒", "▊": "▒",
                           "▉": "▒", "▐": "▒", "▕": "│"})

# 5-pixel-tall font for the verdict banner, drawn with half blocks (3 text rows).
BIG_FONT = {
    "A": ".##./#..#/####/#..#/#..#", "B": "###./#..#/###./#..#/###.", "C": ".###/#.../#.../#.../.###",
    "D": "###./#..#/#..#/#..#/###.", "E": "####/#.../###./#.../####", "F": "####/#.../###./#.../#...",
    "G": ".###/#.../#.##/#..#/.###", "H": "#..#/#..#/####/#..#/#..#", "I": "###/.#./.#./.#./###",
    "J": "..##/...#/...#/#..#/.##.", "K": "#..#/#.#./##../#.#./#..#", "L": "#.../#.../#.../#.../####",
    "M": "#...#/##.##/#.#.#/#...#/#...#", "N": "#...#/##..#/#.#.#/#..##/#...#",
    "O": ".##./#..#/#..#/#..#/.##.", "P": "###./#..#/###./#.../#...", "Q": ".##./#..#/#..#/#.#./.#.#",
    "R": "###./#..#/###./#.#./#..#", "S": ".###/#.../.##./...#/###.", "T": "#####/..#../..#../..#../..#..",
    "U": "#..#/#..#/#..#/#..#/.##.", "V": "#...#/#...#/#...#/.#.#./..#..", "W": "#...#/#...#/#.#.#/##.##/#...#",
    "X": "#...#/.#.#./..#../.#.#./#...#", "Y": "#...#/.#.#./..#../..#../..#..", "Z": "####/...#/.##./#.../####",
    "0": ".##./#..#/#..#/#..#/.##.", "1": ".#./##./.#./.#./###", "2": "###./...#/.##./#.../####",
    "3": "###./...#/.##./...#/###.", "4": "#..#/#..#/####/...#/...#", "5": "####/#.../###./...#/###.",
    "6": ".##./#.../###./#..#/.##.", "7": "####/...#/..#./.#../.#..", "8": ".##./#..#/.##./#..#/.##.",
    "9": ".##./#..#/.###/...#/.##.", " ": "../../../../..", "!": "#/#/#/./#", "-": ".../.../###/.../...",
}
BIG_ICONS = {
    "ok": ["......#", ".....##", "#...##.", "##.##..", ".###...", "..#...."],
    "warn": ["...#...", "..#.#..", "..#.#..", ".#.#.#.", ".#...#.", "#######"],
    "crit": ["##...##", ".##.##.", "..###..", "..###..", ".##.##.", "##...##"],
}


def block_bitmap(ch: str, w: int, h: int) -> list[list[bool]] | None:
    o = ord(ch)
    if 0x2581 <= o <= 0x2588:  # lower eighths and full block
        rows = round(h * (o - 0x2580) / 8)
        return [[y >= h - rows] * w for y in range(h)]
    if o == 0x2580:  # upper half
        return [[y < h // 2] * w for y in range(h)]
    if 0x2589 <= o <= 0x258F:  # left eighths
        cols = round(w * (0x2590 - o) / 8)
        return [[x < cols for x in range(w)] for _ in range(h)]
    if o == 0x2590:  # right half
        return [[x >= w // 2 for x in range(w)] for _ in range(h)]
    if o == 0x2595:  # right eighth
        return [[x >= w - max(1, round(w / 8)) for x in range(w)] for _ in range(h)]
    return None


def icon_bitmap(ch: str, w: int, h: int) -> list[list[bool]] | None:
    index = ord(ch) - ICON_BASE
    if not 0 <= index < len(ICON_ORDER) * 2:
        return None
    design, half = ICONS[ICON_ORDER[index // 2]], index % 2
    margin = h // 14
    rows = []
    for y in range(h):
        dy = (y - margin) * 14 // max(1, h - 2 * margin)
        rows.append([0 <= dy < 14 and design[dy][(half * w + x) * 14 // (2 * w)] == "#" for x in range(w)])
    return rows


def custom_glyph(ch: str, w: int, h: int) -> list[list[bool]] | None:
    """Bitmap for the glyphs Server Glance adds to the console font."""
    return block_bitmap(ch, w, h) or icon_bitmap(ch, w, h)


CUSTOM_CHARS = list(BLOCK_CHARS) + [chr(ICON_BASE + i) for i in range(len(ICON_ORDER) * 2)]


def halfblock(rows: list[str]) -> list[str]:
    if len(rows) % 2:
        rows = rows + ["." * len(rows[0])]
    return ["".join("█" if a == "#" and b == "#" else "▀" if a == "#" else "▄" if b == "#" else " "
                    for a, b in zip(top, bottom)) for top, bottom in zip(rows[::2], rows[1::2])]


def big_text(text: str) -> list[str]:
    rows = [""] * 5
    for ch in text.upper():
        glyph = BIG_FONT.get(ch, BIG_FONT[" "]).split("/")
        for i in range(5):
            rows[i] += glyph[i] + "."
    return halfblock([r[:-1] for r in rows])


# ---------------------------------------------------------------- terminal styling


class Term:
    """Colour and glyph capabilities of the output.

    glyphs: console (custom font loaded) | unicode (terminal emulator) | basic (stock console font)
    colors: palette (console, our palette loaded) | truecolor | ansi
    """

    def __init__(self, glyphs: str = "unicode", colors: str = "truecolor") -> None:
        self.glyphs, self.colors = glyphs, colors
        self._cache: dict = {}

    def _code(self, role: str, bg: bool) -> str:
        if self.colors == "truecolor":
            r, g, b = (int(THEME[role][i:i + 2], 16) for i in (1, 3, 5))
            return f"{48 if bg else 38};2;{r};{g};{b}"
        if self.colors == "palette":
            idx = PALETTE.index(role)
            if bg:
                return str(40 + (idx if idx < 8 else 4))
            return str(30 + idx) if idx < 8 else str(90 + idx - 8)
        return ANSI_BG.get(role, "49") if bg else ANSI_FG[role]

    def style(self, fg: str | None = None, bg: str | None = None, bold: bool = False) -> str:
        key = (fg, bg, bold)
        if key not in self._cache:
            codes = ["0"]
            if bold and self.colors != "palette":  # bold switches colour slot on the console
                codes.append("1")
            if fg:
                codes.append(self._code(fg, False))
            if bg:
                codes.append(self._code(bg, True))
            self._cache[key] = f"\x1b[{';'.join(codes)}m"
        return self._cache[key]

    def icon(self, name: str) -> str:
        if self.glyphs == "console":
            i = ICON_ORDER.index(name) * 2
            return chr(ICON_BASE + i) + chr(ICON_BASE + i + 1)
        if self.glyphs == "unicode":
            return EMOJI[name]
        return ASCII_ICON[name]

    def palette_sequence(self) -> str:
        return "".join(f"\x1b]P{i:X}{THEME[role][1:]}" for i, role in enumerate(PALETTE))


T = Term()


def S(fg: str | None = None, bg: str | None = None, bold: bool = False) -> str:
    return T.style(fg, bg, bold)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TOKEN_RE = re.compile(r"(\x1b\[[0-9;]*m)")


def cwidth(ch: str) -> int:
    return 1 if ord(ch) < 0x1100 else 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def vlen(s: str) -> int:
    return sum(cwidth(c) for c in ANSI_RE.sub("", s))


def fit(s: str, width: int) -> str:
    """Pad or truncate a styled string to exactly `width` cells."""
    out, used = [], 0
    for tok in TOKEN_RE.split(s):
        if not tok:
            continue
        if tok.startswith("\x1b["):
            out.append(tok)
            continue
        for ch in tok:
            w = cwidth(ch)
            if used + w > width:
                return "".join(out) + RESET + " " * (width - used)
            out.append(ch)
            used += w
    return "".join(out) + RESET + " " * (width - used)


# ---------------------------------------------------------------- helpers


def run(args: list[str], timeout: float = 3.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def read_int(path: str) -> int | None:
    value = read(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def human_rate(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1000:
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1000
    return f"{bps:.1f} GB/s"


def size_label(gb: float) -> str:
    return f"{gb / 1024:.1f}T" if gb >= 1000 else f"{gb:.0f}G" if gb >= 100 else f"{gb}G"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = os.environ.get("GLANCE_CONFIG") or os.path.expanduser("~/.config/server-glance/config.json")
    data = read(path)
    if data:
        try:
            cfg.update(json.loads(data))
        except json.JSONDecodeError:
            pass
    return cfg


def default_route() -> tuple[str | None, str | None]:
    best = None
    for line in (read("/proc/net/route") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 7 and parts[1] == "00000000" and int(parts[3], 16) & 2:
            metric = int(parts[6])
            gw = socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
            if best is None or metric < best[0]:
                best = (metric, parts[0], gw)
    return (best[1], best[2]) if best else (None, None)


def valid_iface(name: str | None) -> bool:
    return bool(name and re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", name))


@dataclass
class Issue:
    level: str  # "crit" | "warn"
    text: str


@dataclass
class Snapshot:
    issues: list[Issue] = field(default_factory=list)
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------- collectors


def collect_power() -> dict:
    battery, ac = None, None
    for base in sorted(glob.glob("/sys/class/power_supply/*")):
        kind = read(f"{base}/type")
        if kind == "Mains":
            ac = read(f"{base}/online") == "1"
        if kind != "Battery" or read(f"{base}/scope") == "Device" or battery:
            continue
        # Batteries report either energy (uWh, rate in uW) or charge (uAh, rate in uA).
        unit, rate_file = ("energy", "power_now") if os.path.exists(f"{base}/energy_now") else ("charge", "current_now")
        now, full = read_int(f"{base}/{unit}_now"), read_int(f"{base}/{unit}_full")
        design = read_int(f"{base}/{unit}_full_design")
        rate = read_int(f"{base}/{rate_file}") or 0
        status = read(f"{base}/status") or "Unknown"
        eta = None
        if rate and now is not None and full:
            if status == "Discharging":
                eta = now / rate * 3600
            elif status == "Charging":
                eta = (full - now) / rate * 3600
        watts = read_int(f"{base}/power_now")
        battery = {"capacity": read_int(f"{base}/capacity"), "status": status, "eta": eta,
                   "health": round(full / design * 100) if full and design else None,
                   "watts": round(watts / 1e6, 1) if watts else None}
    return {"battery": battery, "ac": ac}


def collect_memory() -> dict:
    mem = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        mem[key] = int(rest.split()[0]) * 1024 if rest.split() else 0
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    swap_total, swap_free = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    return {"pct": round((total - avail) / total * 100) if total else 0,
            "used_gb": round((total - avail) / 1024**3, 1), "total_gb": round(total / 1024**3, 1),
            "swap_pct": round((swap_total - swap_free) / swap_total * 100) if swap_total else 0}


def collect_disks() -> list[dict]:
    disks = []
    for line in run(["df", "-P", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs",
                     "-x", "overlay", "-x", "efivarfs"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[0].startswith("/dev/"):
            disks.append({"mount": parts[5], "pct": int(parts[4].rstrip("%")),
                          "avail_gb": round(int(parts[3]) / 1024 / 1024, 1)})
    return disks


def collect_temps() -> list[tuple[str, float]]:
    temps: dict[str, float] = {}
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(f"{hw}/name") or "hwmon"
        if name in ("BAT0", "BAT1", "ADP0", "ADP1", "AC") or name.startswith("hidpp"):
            continue
        for inp in glob.glob(f"{hw}/temp*_input"):
            value = read_int(inp)
            if value is None or value <= 0:
                continue
            key = {"coretemp": "CPU", "k10temp": "CPU", "zenpower": "CPU", "nvme": "NVMe",
                   "ath10k_hwmon": "WiFi", "iwlwifi_1": "WiFi", "amdgpu": "GPU"}.get(name, name.split("_")[0])
            temps[key] = max(temps.get(key, 0), value / 1000)
    return sorted(temps.items(), key=lambda kv: -kv[1])


def show_units(scope: list[str], units: list[str], props: str) -> list[dict]:
    """`systemctl show` for many units in one call (one property block per unit, in order)."""
    if not units:
        return []
    out = run(["systemctl", *scope, "show", *units, "-p", props])
    blocks = [dict(line.split("=", 1) for line in block.splitlines() if "=" in line) for block in out.split("\n\n")]
    return (blocks + [{}] * len(units))[:len(units)]


_unit_files: dict[str, tuple[float, dict[str, str]]] = {}


def unit_file_states(scope: list[str], units: list[str]) -> dict[str, str]:
    """enabled/disabled per unit; cached for 5 minutes because it rarely changes."""
    key = " ".join(scope + units)
    cached = _unit_files.get(key)
    if not cached or time.monotonic() - cached[0] > 300:
        out = run(["systemctl", *scope, "list-unit-files", "--no-legend", "--plain", *units])
        _unit_files[key] = (time.monotonic(), {p[0]: p[1] for p in (l.split() for l in out.splitlines()) if len(p) >= 2})
    return _unit_files[key][1]


def unit_states(names: list[str]) -> list[tuple[str, str]]:
    """(scope, state) per unit. Checks user and system scope; an active unit in either wins.

    Uses `list-units`, which is several times cheaper than `systemctl show`.
    """
    units = [n if "." in n else f"{n}.service" for n in names]
    found: list[list[tuple[str, str]]] = [[] for _ in units]
    for scope, label in ((["--user"], "user"), ([], "system")):
        if not units:
            break
        out = run(["systemctl", *scope, "list-units", "--all", "--no-legend", "--plain", *units])
        rows = {p[0]: p for p in (line.split() for line in out.splitlines()) if len(p) >= 4}
        files = None
        for i, unit in enumerate(units):
            row = rows.get(unit)
            if not row or row[1] != "loaded":
                # systemd unloads idle units; fall back to the (cached) unit file list.
                files = files if files is not None else unit_file_states(scope, units)
                if unit in files:
                    found[i].append((label, "inactive" if files[unit] == "enabled" else "disabled"))
                continue
            state = row[2]
            if state == "activating" and row[3] == "auto-restart":
                state = "restarting"
            elif state == "inactive":
                files = files if files is not None else unit_file_states(scope, units)
                if files.get(unit) != "enabled":
                    state = "disabled"
            found[i].append((label, state))
    rank = {"active": 0, "reloading": 1, "activating": 2, "disabled": 3}
    return [min(f, key=lambda x: rank.get(x[1], 9)) if f else ("-", "not-found") for f in found]


def problem_units(scope: list[str]) -> list[tuple[str, str]]:
    out = run(["systemctl", *scope, "list-units", "--all", "--no-legend", "--plain",
               "--state=failed,auto-restart"])
    return [(p[0], p[3]) for p in (line.split() for line in out.splitlines()) if len(p) >= 4]


def port_listening(port: int) -> bool:
    """True if any TCP socket (IPv4 or IPv6) is listening on `port` (state 0A in /proc/net/tcp*)."""
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in (read(path) or "").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                return True
    return False


def collect_services(cfg: dict) -> dict:
    watched = [(name, *state) for name, state in zip(cfg["services"], unit_states(cfg["services"]))]
    watched = [w for w in watched if w[2] != "not-found"]
    ignore = [re.compile(p) for p in cfg.get("ignore_failed", [])]
    failed = []
    for scope, label in ((["--user"], "user"), ([], "system")):
        for unit, sub in problem_units(scope):
            if cfg.get("runners", True) and unit.startswith("actions.runner."):
                continue  # shown in the GitHub Runners panel instead
            if not any(p.search(unit) for p in ignore):
                failed.append((label, unit, sub))
    ssh = port_listening(22)
    return {"watched": watched, "failed": failed, "ssh": ssh}


def collect_internet(cfg: dict) -> dict:
    ok, latency = 0, None
    for probe in cfg["internet_probes"]:
        host, _, port = probe.rpartition(":")
        start = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                ok += 1
                ms = round((time.monotonic() - start) * 1000)
                latency = ms if latency is None else min(latency, ms)
        except (OSError, ValueError):
            pass
    total = len(cfg["internet_probes"])
    ts_state, ts_ip = None, None
    if shutil.which("tailscale"):
        try:
            data = json.loads(run(["tailscale", "status", "--json"]) or "{}")
            ts_state = data.get("BackendState")
            ts_ip = next((a for a in data.get("TailscaleIPs") or [] if "." in a), None)
        except json.JSONDecodeError:
            ts_state = "error"
    return {"state": "online" if ok == total else ("degraded" if ok else "offline"),
            "latency": latency, "probes_ok": ok, "probes": total, "ts_state": ts_state, "ts_ip": ts_ip}


def wifi_ssid(iface: str) -> str | None:
    m = re.search(r"Wi-Fi access point: (.+?) \(", run(["networkctl", "status", "--no-pager", iface]))
    if m:
        return m.group(1)
    if shutil.which("iwgetid"):
        ssid = run(["iwgetid", "-r", iface]).strip()
        if ssid:
            return ssid
    if shutil.which("nmcli"):
        for line in run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
            if line.startswith("yes:"):
                return line[4:]
    return None


def gateway_latency(gateway: str) -> float | None:
    """Round-trip time to the gateway in ms, or None if it doesn't answer.

    Uses a TCP handshake to common router ports first: it needs no privileges (unlike
    ping inside a hardened service), and a refused connection still proves the gateway
    is alive. Falls back to the ping binary.
    """
    socket.inet_aton(gateway)  # only ever probe a well-formed IPv4 address
    for port in (53, 80, 443, 22):
        start = time.monotonic()
        try:
            with socket.create_connection((gateway, port), timeout=1):
                pass
        except ConnectionRefusedError:
            pass
        except OSError:
            continue
        return (time.monotonic() - start) * 1000
    m = re.search(r"time=([\d.]+)", run(["ping", "-n", "-c", "1", "-W", "1", gateway], timeout=3))
    return float(m.group(1)) if m else None


def collect_lan() -> dict:
    iface, gateway = default_route()
    nics = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        if not os.path.exists(f"{path}/device") or not valid_iface(name):
            continue  # physical NICs only
        wireless = os.path.isdir(f"{path}/wireless") or os.path.exists(f"{path}/phy80211")
        carrier = read(f"{path}/carrier") == "1"
        nic = {"name": name, "kind": "wifi" if wireless else "ethernet",
               "up": read(f"{path}/operstate") == "up" and carrier, "default": name == iface,
               "ip": None, "prefix": None, "speed": None, "ssid": None, "signal": None, "dbm": None}
        m = re.search(r"inet (\S+)/(\d+)", run(["ip", "-4", "-o", "addr", "show", "dev", name]))
        if m:
            nic["ip"], nic["prefix"] = m.group(1), int(m.group(2))
        if wireless:
            if nic["up"]:
                nic["ssid"] = wifi_ssid(name)
            for line in (read("/proc/net/wireless") or "").splitlines():
                if line.strip().startswith(f"{name}:"):
                    fields = line.split()
                    nic["signal"] = min(100, round(float(fields[2].rstrip(".")) / 70 * 100))
                    nic["dbm"] = int(float(fields[3].rstrip(".")))
        elif carrier:
            speed = read_int(f"{path}/speed")
            nic["speed"] = speed if speed and speed > 0 else None
        nics.append(nic)
    gw_ms = None
    if gateway:
        try:
            gw_ms = gateway_latency(gateway)
        except OSError:
            pass
    states = [line.split()[-1] for line in run(["ip", "-4", "neigh", "show"]).splitlines() if line.strip()]
    seen = [s for s in states if s in ("REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT")]
    return {"nics": nics, "iface": iface, "gateway": gateway, "gw_ms": gw_ms,
            "devices": len(seen), "active": sum(1 for s in seen if s in ("REACHABLE", "DELAY"))}


def collect_docker() -> dict | None:
    if not shutil.which("docker"):
        return None
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"], timeout=5)
    if not out and run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=5) == "":
        return {"available": False}
    rows = [line.split("|", 2) for line in out.splitlines() if line.count("|") == 2]
    return {"available": True, "running": sum(1 for r in rows if r[1] == "running"), "total": len(rows),
            "unhealthy": [r[0] for r in rows if "unhealthy" in r[2]],
            "restarting": [r[0] for r in rows if r[1] == "restarting"]}


def collect_kernel_warnings() -> list[str]:
    out = run(["journalctl", "-k", "-p", "warning", "--since", "-1h", "-o", "cat", "--no-pager", "-q"])
    pattern = re.compile(r"thermal|temperature|overheat|thrott|critical|I/O error|nvme|oom|out of memory", re.I)
    return [line for line in out.splitlines() if pattern.search(line)][-3:]


def job_started(cgroup: str) -> float | None:
    """Start time of the Runner.Worker process (a job in progress) inside a runner's cgroup."""
    if not cgroup:
        return None
    procs = read(f"/sys/fs/cgroup{cgroup}/cgroup.procs") or ""
    btime = None
    for pid in procs.split():
        if read(f"/proc/{pid}/comm") != "Runner.Worker":
            continue
        stat = read(f"/proc/{pid}/stat") or ""
        if ")" not in stat:
            continue
        if btime is None:
            btime = next((int(line.split()[1]) for line in (read("/proc/stat") or "").splitlines()
                          if line.startswith("btime")), 0)
        return btime + int(stat.rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
    return None


# ---------------------------------------------------------------- scheduler


class Collector:
    """Runs every data source on its own cadence in background threads."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.data: dict = {}
        self.lock = threading.Lock()
        self.hist: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY))
        self.prev: dict = {}
        self.gpu_names: dict[str, str] = {}
        self.runner_meta: dict[str, dict] = {}
        self.visible = True
        self.generation = 0  # bumped when the screen becomes visible, to refresh everything at once
        # One thread per group, so a slow source (docker, internet probes) never stalls the 1s ones.
        self.groups = [
            ["cpu", "memory", "traffic", "diskio", "temps", "power"],
            ["gpus", "services", "runners", "lan"],
            ["internet", "docker", "disks", "kernel"],
        ]
        self.funcs = {
            "cpu": self.c_cpu, "memory": collect_memory, "traffic": self.c_traffic, "diskio": self.c_diskio,
            "temps": collect_temps,
            "power": collect_power, "gpus": self.c_gpus, "services": lambda: collect_services(self.cfg),
            "runners": self.c_runners, "lan": collect_lan, "internet": lambda: collect_internet(self.cfg),
            "docker": collect_docker, "disks": collect_disks, "kernel": collect_kernel_warnings,
        }

    def run_task(self, name: str) -> None:
        try:
            value = self.funcs[name]()
        except Exception as exc:  # a broken source must never take the screen down
            print(f"glance: {name} collector failed: {exc}", file=sys.stderr)
            value = None
        with self.lock:
            self.data[name] = value

    def collect_all(self) -> None:
        for group in self.groups:
            for name in group:
                self.run_task(name)

    def start(self) -> None:
        for group in self.groups:
            threading.Thread(target=self._loop, args=(group,), daemon=True).start()

    def set_visible(self, visible: bool) -> None:
        if visible and not self.visible:
            self.generation += 1  # someone just looked: refresh everything right away
        self.visible = visible

    def _loop(self, names: list[str]) -> None:
        due = dict.fromkeys(names, 0.0)
        generation = self.generation
        while True:
            if generation != self.generation:
                generation, due = self.generation, dict.fromkeys(names, 0.0)
            for name in names:
                if time.monotonic() >= due[name]:
                    self.run_task(name)
                    slow = 1 if self.visible or name in HISTORY_SOURCES else HIDDEN_SLOWDOWN
                    due[name] = time.monotonic() + CADENCE[name] * slow
            time.sleep(0.2)

    def snapshot(self) -> Snapshot:
        with self.lock:
            data = dict(self.data)
            data["history"] = {k: list(v) for k, v in self.hist.items()}
        return Snapshot(evaluate(data), data)

    # --- stateful collectors (they need the previous sample) ---

    def c_cpu(self) -> dict:
        times = {}
        for line in (read("/proc/stat") or "").splitlines():
            if line.startswith("cpu"):
                parts = line.split()
                vals = [int(v) for v in parts[1:9]]
                times[parts[0]] = (vals[3] + vals[4], sum(vals))
        prev, self.prev["cpu"] = self.prev.get("cpu", {}), times

        def usage(key: str) -> float:
            if key not in prev:
                return 0.0
            didle, dtotal = times[key][0] - prev[key][0], times[key][1] - prev[key][1]
            return max(0.0, min(100.0, 100 * (1 - didle / dtotal))) if dtotal > 0 else 0.0

        cores = [usage(f"cpu{i}") for i in range(len(times) - 1) if f"cpu{i}" in times]
        total = usage("cpu")
        if prev:
            self.hist["cpu"].append(total)
        freqs = [read_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
        freqs = [f for f in freqs if f]
        return {"host": socket.gethostname(), "uptime": float((read("/proc/uptime") or "0").split()[0]),
                "load": [float(x) for x in (read("/proc/loadavg") or "0 0 0").split()[:3]],
                "count": os.cpu_count() or len(cores) or 1, "cores": cores, "total": total,
                "ghz": round(sum(freqs) / len(freqs) / 1e6, 2) if freqs else None}

    def c_traffic(self) -> dict | None:
        iface, _ = default_route()
        if not valid_iface(iface):
            return None
        rx = read_int(f"/sys/class/net/{iface}/statistics/rx_bytes") or 0
        tx = read_int(f"/sys/class/net/{iface}/statistics/tx_bytes") or 0
        now = time.monotonic()
        prev, self.prev["traffic"] = self.prev.get("traffic"), (iface, rx, tx, now)
        if not prev or prev[0] != iface or now <= prev[3]:
            return {"iface": iface, "rx": 0.0, "tx": 0.0}
        rx_rate, tx_rate = (rx - prev[1]) / (now - prev[3]), (tx - prev[2]) / (now - prev[3])
        self.hist["rx"].append(rx_rate)
        self.hist["tx"].append(tx_rate)
        return {"iface": iface, "rx": rx_rate, "tx": tx_rate}

    def c_diskio(self) -> dict:
        """Read/write throughput summed over whole physical disks (not partitions)."""
        rd = wr = 0
        for line in (read("/proc/diskstats") or "").splitlines():
            p = line.split()
            if len(p) >= 10 and re.fullmatch(r"nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+", p[2]):
                rd += int(p[5]) * 512
                wr += int(p[9]) * 512
        now = time.monotonic()
        prev, self.prev["diskio"] = self.prev.get("diskio"), (rd, wr, now)
        if not prev or now <= prev[2]:
            return {"read": 0.0, "write": 0.0}
        r, w = (rd - prev[0]) / (now - prev[2]), (wr - prev[1]) / (now - prev[2])
        self.hist["dr"].append(r)
        self.hist["dw"].append(w)
        return {"read": r, "write": w}

    def gpu_name(self, slot: str, vendor: str) -> str:
        if slot not in self.gpu_names:
            name = None
            if shutil.which("lspci"):
                fields = re.findall(r'"([^"]*)"', run(["lspci", "-mm", "-s", slot]))
                if len(fields) >= 3:
                    m = re.search(r"\[(.+)\]", fields[2])
                    name = m.group(1) if m else fields[2]
            short = {"0x8086": "Intel", "0x10de": "NVIDIA", "0x1002": "AMD"}.get(vendor, "GPU")
            self.gpu_names[slot] = f"{short} {name}" if name else f"{short} GPU"
        return self.gpu_names[slot]

    def c_gpus(self) -> list[dict]:
        gpus = []
        nvidia = None
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
            if "-" in os.path.basename(card):
                continue
            dev = os.path.realpath(f"{card}/device")
            slot, vendor = os.path.basename(dev), read(f"{card}/device/vendor") or ""
            gpu = {"id": slot, "vendor": vendor, "name": self.gpu_name(slot, vendor), "state": "active",
                   "util": None, "mem_used": None, "mem_total": None, "temp": None, "power": None,
                   "clock": None, "max_clock": None, "pstate": None}
            if vendor == "0x10de":
                if read(f"{dev}/power/runtime_status") == "suspended":
                    gpu["state"] = "sleeping"  # don't wake a power-managed dGPU just to look at it
                elif shutil.which("nvidia-smi"):
                    if nvidia is None:
                        nvidia = self._nvidia_query()
                    info = next((v for k, v in nvidia.items() if k.lower().endswith(slot.lower())), None)
                    if info:
                        gpu.update(info)
            elif vendor == "0x8086":
                gpu["clock"] = read_int(f"{card}/gt_act_freq_mhz") or read_int(f"{card}/gt_cur_freq_mhz")
                gpu["max_clock"] = read_int(f"{card}/gt_max_freq_mhz")
                # Busy % = share of time NOT spent in the RC6 power-saving state.
                rc6 = read_int(f"{card}/gt/gt0/rc6_residency_ms") or read_int(f"{card}/power/rc6_residency_ms")
                now = time.monotonic() * 1000
                prev, self.prev[f"rc6:{slot}"] = self.prev.get(f"rc6:{slot}"), (rc6, now)
                if rc6 is not None and prev and prev[0] is not None and now > prev[1]:
                    gpu["util"] = max(0.0, min(100.0, 100 * (1 - (rc6 - prev[0]) / (now - prev[1]))))
            elif vendor == "0x1002":
                gpu["util"] = read_int(f"{card}/device/gpu_busy_percent")
                used, total = read_int(f"{card}/device/mem_info_vram_used"), read_int(f"{card}/device/mem_info_vram_total")
                if used is not None and total:
                    gpu["mem_used"], gpu["mem_total"] = used / 1024**2, total / 1024**2
                for hw in glob.glob(f"{card}/device/hwmon/hwmon*"):
                    temp, power = read_int(f"{hw}/temp1_input"), read_int(f"{hw}/power1_average")
                    gpu["temp"] = temp / 1000 if temp else None
                    gpu["power"] = power / 1e6 if power else None
            if gpu["util"] is not None:
                self.hist[f"gpu:{slot}"].append(gpu["util"])
            gpus.append(gpu)
        gpus.sort(key=lambda g: g["vendor"] == "0x8086")  # discrete GPUs first
        return gpus

    @staticmethod
    def _nvidia_query() -> dict[str, dict]:
        fields = ("pci.bus_id,name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
                  "power.draw,clocks.gr,clocks.max.gr,pstate")
        out = run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"], timeout=4)

        def num(v: str) -> float | None:
            try:
                return float(v)
            except ValueError:
                return None

        result = {}
        for line in out.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 10:
                result[p[0]] = {"name": p[1], "util": num(p[2]), "mem_used": num(p[3]), "mem_total": num(p[4]),
                                "temp": num(p[5]), "power": num(p[6]), "clock": num(p[7]),
                                "max_clock": num(p[8]), "pstate": p[9]}
        return result

    def c_runners(self) -> list[dict] | None:
        if not self.cfg.get("runners", True):
            return None
        units = []
        for scope in ([], ["--user"]):
            out = run(["systemctl", *scope, "list-units", "--all", "--no-legend", "--plain", "actions.runner.*"])
            for p in (line.split() for line in out.splitlines()):
                if len(p) >= 4 and p[0].startswith("actions.runner.") and p[0].endswith(".service"):
                    units.append((scope, p[0], p[2], p[3]))
        if not units:
            return None
        details = {}
        for scope in ([], ["--user"]):
            names = [u for sc, u, _, _ in units if sc == scope]
            for unit, props in zip(names, show_units(scope, names, "Result,ControlGroup,WorkingDirectory")):
                details[unit] = props
        runners = []
        for scope, unit, active, sub in units:
            props = details.get(unit, {})
            meta = self._runner_meta(unit, props.get("WorkingDirectory", ""))
            since = job_started(props.get("ControlGroup", "")) if active == "active" else None
            if since:
                state = "busy"
            elif active == "active":
                state = "idle"
            elif active == "failed" or sub == "auto-restart":
                state = "down"
            else:
                state = "off"
            reason = props.get("Result") if state == "down" and props.get("Result") != "success" else None
            runners.append({"unit": unit, "repo": meta["repo"], "name": meta["name"], "state": state,
                            "since": since, "reason": reason})
        order = {"busy": 0, "idle": 1, "down": 2, "off": 3}
        runners.sort(key=lambda r: (order[r["state"]], r["repo"], r["name"]))
        return runners

    def _runner_meta(self, unit: str, workdir: str) -> dict:
        if unit not in self.runner_meta:
            label = unit.removeprefix("actions.runner.").removesuffix(".service")
            repo, _, name = label.partition(".")
            meta = {"repo": repo, "name": name or label}
            try:
                info = json.loads(read(os.path.join(workdir, ".runner")) or "{}") if workdir else {}
                if info.get("gitHubUrl"):
                    meta["repo"] = info["gitHubUrl"].rstrip("/").rsplit("/", 1)[-1]
                if info.get("agentName"):
                    meta["name"] = info["agentName"]
            except (json.JSONDecodeError, AttributeError):
                pass
            self.runner_meta[unit] = meta
        return self.runner_meta[unit]


# ---------------------------------------------------------------- evaluation


def evaluate(d: dict) -> list[Issue]:
    issues: list[Issue] = []
    add = lambda level, text: issues.append(Issue(level, text))  # noqa: E731

    power = d.get("power") or {}
    bat, ac = power.get("battery"), power.get("ac")
    if bat:
        cap = bat["capacity"] or 0
        if ac is False or bat["status"] == "Discharging":
            add("crit" if cap < 30 else "warn", f"On battery power ({cap}%) - charger unplugged?")
        if cap < 15:
            add("crit", f"Battery critically low: {cap}%")
        elif cap < 30 and bat["status"] != "Charging":
            add("warn", f"Battery low: {cap}%")
        if bat["health"] is not None and bat["health"] < 60:
            add("warn", f"Battery health degraded: {bat['health']}% of design capacity")

    for disk in d.get("disks") or []:
        if disk["pct"] >= 90:
            add("crit", f"Disk {disk['mount']} {disk['pct']}% full ({disk['avail_gb']} GB left)")
        elif disk["pct"] >= 80:
            add("warn", f"Disk {disk['mount']} {disk['pct']}% full")
    mem = d.get("memory") or {}
    if mem.get("pct", 0) >= 92:
        add("crit", f"Memory {mem['pct']}% used")
    elif mem.get("pct", 0) >= 85:
        add("warn", f"Memory {mem['pct']}% used")
    if mem.get("swap_pct", 0) >= 80:
        add("warn", f"Swap {mem['swap_pct']}% used")
    cpu = d.get("cpu") or {}
    if cpu and cpu["load"][1] > cpu["count"] * 1.5:
        add("warn", f"High load: {cpu['load'][1]:.1f} on {cpu['count']} threads")

    for label, temp in d.get("temps") or []:
        if temp >= 90:
            add("crit", f"{label} temperature {temp:.0f}°C")
        elif temp >= 80:
            add("warn", f"{label} temperature {temp:.0f}°C")
    for gpu in d.get("gpus") or []:
        if gpu.get("temp") is not None and gpu["temp"] >= 85 and "GPU" not in dict(d.get("temps") or []):
            add("crit" if gpu["temp"] >= 92 else "warn", f"{gpu['name']} at {gpu['temp']:.0f}°C")
        if gpu.get("mem_total") and gpu.get("mem_used") is not None and gpu["mem_used"] / gpu["mem_total"] >= 0.95:
            add("warn", f"{gpu['name']} VRAM almost full")

    svc = d.get("services")
    if svc:
        for name, _scope, state in svc["watched"]:
            if state not in ("active", "disabled"):
                add("crit", f"Service {name} is {state}")
        watched_units = {f"{n}.service" for n, _, _ in svc["watched"]}
        others = [(s, u, sub) for s, u, sub in svc["failed"] if u not in watched_units]
        if len(others) > 3:
            names = ", ".join(u.removesuffix(".service") for _, u, _ in others[:3])
            add("warn", f"{len(others)} systemd units failed: {names}, …")
        else:
            for scope, unit, sub in others:
                add("warn", f"{scope} unit {unit.removesuffix('.service')} {sub}")
        if not svc["ssh"]:
            add("crit", "SSH is not listening on port 22")

    down = [r for r in d.get("runners") or [] if r["state"] == "down"]
    if down:
        reasons = sorted({r["reason"] for r in down if r["reason"] and r["reason"] != "exit-code"})
        extra = f" ({', '.join(reasons)})" if reasons else ""
        add("warn", f"{len(down)} GitHub runner{'s' if len(down) > 1 else ''} down{extra}")

    lan = d.get("lan")
    if lan:
        if not lan["gateway"]:
            add("crit", "No default gateway - LAN is down")
        elif lan["gw_ms"] is None:
            add("crit", f"Gateway {lan['gateway']} is not answering")
        for nic in lan["nics"]:
            if nic["default"] and nic["kind"] == "wifi" and nic["signal"] is not None and nic["signal"] < 30:
                add("warn", f"Weak Wi-Fi signal: {nic['signal']}%")
            if nic["kind"] == "ethernet" and nic["up"]:
                if nic["speed"] is not None and nic["speed"] < 100:
                    add("warn", f"Ethernet {nic['name']} linked at only {nic['speed']} Mb/s - check the cable")
                if not nic["ip"]:
                    add("warn", f"Ethernet {nic['name']} has a link but no IP address")
    net = d.get("internet")
    if net:
        if net["state"] == "offline":
            add("crit", "No internet connectivity")
        elif net["state"] == "degraded":
            add("warn", "Internet connectivity degraded")
        if net["ts_state"] not in (None, "Running"):
            add("warn", f"Tailscale is {net['ts_state']}")

    dk = d.get("docker")
    if dk:
        if not dk["available"]:
            add("warn", "Docker daemon not reachable")
        for name in dk.get("unhealthy", []):
            add("warn", f"Container {name} unhealthy")
        for name in dk.get("restarting", []):
            add("warn", f"Container {name} restart-looping")

    for line in d.get("kernel") or []:
        add("warn", f"Kernel: {line[:90]}")

    issues.sort(key=lambda i: 0 if i.level == "crit" else 1)
    return issues


# ---------------------------------------------------------------- drawing primitives


def level_role(pct: float, warn: float, crit: float) -> str:
    return "crit" if pct >= crit else "warn" if pct >= warn else "ok"


def hbar(pct: float, width: int, role: str) -> str:
    """Smooth horizontal bar with 1/8-cell resolution."""
    pct = max(0.0, min(100.0, pct or 0))
    full, part = divmod(round(pct / 100 * width * 8), 8)
    s = S(role, "surface") + "█" * full
    if full < width:
        s += (HBLOCK[part] if part else " ") + " " * (width - full - 1)
    return s + RESET


def spark(values: list[float], width: int, role: str, top: float | None = None) -> str:
    values = list(values)[-width:]
    if not values:
        return S("dim") + "·" * width + RESET
    top = top or max(max(values), 1e-9)
    chars = "".join(VBLOCK[max(1 if v > 0 else 0, min(8, round(v / top * 8)))] for v in values)
    return S("dim") + "·" * (width - len(values)) + S(role) + chars + RESET


def area_chart(values: list[float], width: int, height: int, role: str, top: float | None = None) -> list[str]:
    """Multi-row bar chart with 1/8-row resolution, newest sample on the right."""
    values = list(values)[-width:]
    values = [0.0] * (width - len(values)) + values
    top = top or max(max(values), 1e-9)
    rows = []
    for r in range(height):
        b = height - 1 - r
        cells = "".join(VBLOCK[max(0, min(8, round(v / top * height * 8) - b * 8))] for v in values)
        rows.append(S(role) + cells + RESET)
    return rows


def dual_chart(a: list[float], b: list[float], la: tuple[str, str], lb: tuple[str, str], iw: int, rows: int) -> list[str]:
    """Two stacked area charts sharing one scale, filling `rows` rows (used to put spare space to work)."""
    if rows < 3:
        return []
    cw = max(10, iw - 9)
    top = max(list(a) + list(b) + [1.0])
    ha = (rows - 1 + 1) // 2
    out = [S("dim") + "peak   " + RESET + S("text") + human_rate(top) + RESET + S("dim") + f" · last {cw}s" + RESET]
    chart = area_chart(a, cw, ha, la[1], top) + area_chart(b, cw, rows - 1 - ha, lb[1], top)
    for i, row in enumerate(chart):
        label = (S(la[1]) + f"{la[0]:<7}" if i == 0 else S(lb[1]) + f"{lb[0]:<7}" if i == ha else " " * 7) + RESET
        out.append(label + "  " + row)
    return out


def signal_bars(pct: int | None) -> str:
    if pct is None:
        return ""
    role = level_role(100 - pct, 50, 70)
    return "".join(S(role if pct >= t else "surface") + c for c, t in zip("▂▄▆█", (1, 30, 55, 80))) + RESET


def dot(role: str) -> str:
    return S(role) + "●" + RESET


def state_role(state: str) -> str:
    return {"active": "ok", "disabled": "dim", "activating": "warn", "reloading": "warn"}.get(state, "crit")


@dataclass
class Panel:
    icon: str
    title: str
    lines: list[str]
    note: str = ""
    role: str = "accent"
    min_lines: int = 1
    grow: object = None  # optional callable(extra_rows) -> lines, to use spare space well


def draw_panel(p: Panel, width: int, nlines: int) -> list[str]:
    head = f" {T.icon(p.icon)} {S(p.role, bold=True)}{p.title.upper()}{S('border')} "
    note = f" {S('dim')}{p.note}{S('border')} " if p.note else ""
    fill = max(0, width - 3 - vlen(head) - vlen(note) - 1)
    out = [S("border") + "╭─" + head + "─" * fill + note + "─╮" + RESET]
    body = p.lines[:nlines]
    if len(p.lines) > nlines > 0:
        body[-1] = S("dim") + f"… {len(p.lines) - nlines + 1} more" + RESET
    body += [""] * (nlines - len(body))
    for line in body:
        out.append(S("border") + "│" + RESET + " " + fit(line, width - 4) + " " + S("border") + "│" + RESET)
    out.append(S("border") + "╰" + "─" * (width - 2) + "╯" + RESET)
    return out


def stack(panels: list[Panel], width: int, avail: int | None, fill_to: int | None = None) -> list[str]:
    """Stack panels vertically, shrinking low-priority (later) panels first to fit `avail` rows."""
    sizes = [len(p.lines) for p in panels]
    if avail is not None:
        while panels and sum(sizes) + 2 * len(panels) > avail:
            surplus = [(sizes[i] - panels[i].min_lines, i) for i in range(len(panels)) if sizes[i] > panels[i].min_lines]
            if surplus:
                sizes[max(surplus)[1]] -= 1
            else:
                panels, sizes = panels[:-1], sizes[:-1]
    if fill_to is not None and panels:
        extra = max(0, fill_to - (sum(sizes) + 2 * len(panels)))
        last = panels[-1]
        if extra and last.grow and sizes[-1] == len(last.lines):
            panels[-1] = Panel(**{**last.__dict__, "lines": last.lines + last.grow(extra), "grow": None})
        sizes[-1] += extra
    out = []
    for p, n in zip(panels, sizes):
        out += draw_panel(p, width, n)
    return out


# ---------------------------------------------------------------- panels


def cpu_panel(d: dict, iw: int) -> Panel:
    cpu, mem = d.get("cpu") or {}, d.get("memory") or {}
    temps = dict(d.get("temps") or [])
    cores = cpu.get("cores") or []
    hist = (d.get("history") or {}).get("cpu", [])
    stats_w = 34
    area = max(8, iw - stats_w - 2)
    n = len(cores)
    vals = cores
    for bw, gap in ((3, 1), (2, 1), (1, 1), (1, 0)):
        if n * (bw + gap) - gap <= area:
            break
    else:  # more cores than columns: average neighbouring cores
        group = math.ceil(n / area)
        vals = [sum(cores[i:i + group]) / len(cores[i:i + group]) for i in range(0, n, group)]
    height = 6
    rows = []
    for r in range(height):
        b = height - 1 - r
        frac = (b + 1) / height
        role = "crit" if frac > 0.9 else "warn" if frac > 0.6 else "ok"  # equaliser-style gradient
        line = ""
        for v in vals:
            f = max(0, min(8, round(v / 100 * height * 8) - b * 8))
            line += S(role, "surface") + VBLOCK[f] * bw + RESET + " " * gap
        rows.append(line)
    if bw >= 2:
        rows.append("".join(S(level_role(v, 70, 90), bold=True) + f"{v:>{bw}.0f}"[-bw:] + RESET + " " * gap for v in vals))
        rows.append(S("dim") + "".join(f"{('c' + str(i)) if bw >= 3 else str(i % 10):^{bw}}" + " " * gap
                                        for i in range(len(vals))) + RESET)
    else:
        rows.append(S("dim") + f"{n} threads" + RESET)
    total = cpu.get("total", 0)
    load = cpu.get("load", [0, 0, 0])
    count = cpu.get("count", 1)
    cpu_temp = temps.get("CPU")
    lbl = lambda s: S("dim") + f"{s:<6}" + RESET  # noqa: E731
    stats = [
        lbl("Usage") + hbar(total, 10, level_role(total, 70, 90)) + S("text", bold=True) + f" {total:>3.0f}%" + RESET,
        lbl("Load") + S(level_role(load[0] / count * 100, 100, 150), bold=True) + f"{load[0]:.2f}" + RESET
        + S("dim") + f"  {load[1]:.2f}  {load[2]:.2f}  /{count}" + RESET,
        lbl("Clock") + S("text", bold=True) + (f"{cpu['ghz']:.2f} GHz" if cpu.get("ghz") else "n/a") + RESET,
        lbl("Mem") + hbar(mem.get("pct", 0), 10, level_role(mem.get("pct", 0), 85, 92))
        + S("text", bold=True) + f" {mem.get('pct', 0):>3}%" + RESET
        + S("dim") + f" {mem.get('used_gb', 0)}/{mem.get('total_gb', 0)}G" + RESET,
        lbl("Swap") + hbar(mem.get("swap_pct", 0), 10, level_role(mem.get("swap_pct", 0), 60, 80))
        + S("text", bold=True) + f" {mem.get('swap_pct', 0):>3}%" + RESET,
        lbl("Temp") + (T.icon("thermo") + " " + S(level_role(cpu_temp, 80, 90), bold=True) + f"{cpu_temp:.0f}°C" + RESET
                       if cpu_temp else S("dim") + "n/a" + RESET),
        lbl("60s") + spark(hist, 26, "accent", top=100),
    ]
    lines = []
    for i in range(max(len(rows), len(stats))):
        left = rows[i] if i < len(rows) else ""
        right = stats[i] if i < len(stats) else ""
        lines.append(fit(left, area) + "  " + right)
    return Panel("cpu", "CPU & Memory", lines, note=f"{n} cores · 1s" if n else "1s", min_lines=len(lines))


def gpu_panel(d: dict, iw: int) -> Panel | None:
    gpus = d.get("gpus")
    if not gpus:
        return None
    lines = []
    hist = d.get("history") or {}
    for g in gpus:
        name = S("text", bold=True) + g["name"] + RESET
        if g["state"] == "sleeping":
            lines.append(name + S("dim") + "  · asleep (power-saving, not woken up to poll)" + RESET)
            continue
        facts = []
        if g.get("clock"):
            facts.append(f"{g['clock']:.0f} MHz")
        if g.get("power") is not None:
            facts.append(f"{g['power']:.1f} W")
        if g.get("pstate"):
            facts.append(g["pstate"])
        temp = ""
        if g.get("temp") is not None:
            temp = "  " + T.icon("thermo") + S(level_role(g["temp"], 80, 90), bold=True) + f" {g['temp']:.0f}°C" + RESET
        lines.append(name + temp + S("dim") + ("  · " + " · ".join(facts) if facts else "") + RESET)
        util = g.get("util")
        row = S("dim") + "Util " + RESET
        if util is not None:
            row += hbar(util, 12, level_role(util, 70, 90)) + S("text", bold=True) + f" {util:>3.0f}%" + RESET
        else:
            row += S("dim") + "n/a" + " " * 14 + RESET
        if g.get("mem_total"):
            vram = g["mem_used"] / g["mem_total"] * 100
            row += S("dim") + "  VRAM " + RESET + hbar(vram, 8, level_role(vram, 85, 95)) \
                + S("dim") + f" {g['mem_used'] / 1024:.1f}/{g['mem_total'] / 1024:.0f}G" + RESET
        used = vlen(row)
        if iw - used > 8:
            row += "  " + spark(hist.get(f"gpu:{g['id']}", []), iw - used - 2, "mauve", top=100)
        lines.append(row)
    return Panel("gpu", "GPU", lines, note=f"{len(gpus)} found · 3s", role="mauve", min_lines=min(len(lines), 2))


def storage_panel(d: dict, iw: int) -> Panel | None:
    disks = d.get("disks")
    if not disks:
        return None
    temps = dict(d.get("temps") or [])
    lines = []
    bw = max(10, iw - 30)
    for disk in disks:
        label = disk["mount"] if len(disk["mount"]) <= 10 else "…" + disk["mount"][-9:]
        lines.append(S("text") + f"{label:<10}" + RESET + hbar(disk["pct"], bw, level_role(disk["pct"], 80, 90))
                     + S("text", bold=True) + f" {disk['pct']:>3}%" + RESET
                     + S("dim") + f" {size_label(disk['avail_gb'])} free" + RESET)
    io, hist = d.get("diskio"), d.get("history") or {}
    if io:
        lines.append(S("dim") + "I/O       " + RESET + S("peach") + "read " + S("text", bold=True) + human_rate(io["read"]) + RESET
                     + S("dim") + "  ·  " + RESET + S("pink") + "write " + S("text", bold=True) + human_rate(io["write"]) + RESET)
    note = f"NVMe {temps['NVMe']:.0f}°C · 1s/60s" if "NVMe" in temps else "1s/60s"

    def grow(extra: int) -> list[str]:
        if not io:
            return []
        return dual_chart(hist.get("dr", []), hist.get("dw", []), ("read", "peach"), ("write", "pink"), iw, extra)

    return Panel("disk", "Storage", lines, note=note, role="peach", grow=grow, min_lines=len(lines))


def services_panel(d: dict, iw: int) -> Panel:
    svc = d.get("services")
    if not svc:
        return Panel("gear", "Services", [S("dim") + "checking…" + RESET], note="10s", role="blue")
    entries = [("ssh", "active" if svc["ssh"] else "failed", "listening :22" if svc["ssh"] else "NOT listening")]
    entries += [(name, state, "" if state == "active" else state) for name, _, state in svc["watched"] if state != "disabled"]
    disabled = [name for name, _, state in svc["watched"] if state == "disabled"]
    cols = 2 if len(entries) > 4 and iw >= 44 else 1
    colw = iw // cols
    cells = []
    for name, state, note in entries:
        note_role = "dim" if state in ("active", "disabled") else state_role(state)
        cells.append(dot(state_role(state)) + " " + S("dim" if state == "disabled" else "text") + name + RESET
                     + (" " + S(note_role) + note + RESET if note else ""))
    per_col = math.ceil(len(cells) / cols)
    lines = ["".join(fit(cells[c * per_col + r], colw - 1) + " " for c in range(cols) if c * per_col + r < len(cells))
             for r in range(per_col)]
    dk = d.get("docker")
    if dk and dk.get("available"):
        bad = len(dk["unhealthy"]) + len(dk["restarting"])
        lines.append(T.icon("docker") + " " + S("text") + "Docker " + S("ok" if not bad else "warn", bold=True)
                     + f"{dk['running']}/{dk['total']}" + RESET + S("dim") + " containers running" + RESET
                     + (S("warn") + f" · {bad} unhealthy" + RESET if bad else ""))
    if disabled:
        lines.append(S("dim") + f"• {len(disabled)} disabled: " + ", ".join(disabled) + RESET)
    watched_units = {f"{n}.service" for n, _, _ in svc["watched"]}
    others = [u for _, u, _ in svc["failed"] if u not in watched_units]
    if others:
        lines.append(S("crit") + f"● {len(others)} other failed unit(s): " + S("dim")
                     + ", ".join(u.removesuffix(".service") for u in others) + RESET)
    up = sum(1 for _, s, _ in entries if s == "active")
    return Panel("gear", "Services", lines, note=f"{up}/{len(entries)} up · 10s", role="blue", min_lines=min(len(lines), 3))


def runners_panel(d: dict, iw: int) -> Panel | None:
    runners = d.get("runners")
    if not runners:
        return None
    count = collections.Counter(r["state"] for r in runners)
    frames = "▁▃▅▇▅▃"
    tick = int(time.time())
    lines = []
    for r in runners:
        who = S("text", bold=True) + r["name"] + RESET + S("dim") + "  " + r["repo"] + RESET
        if r["state"] == "busy":
            elapsed = human_duration(time.time() - r["since"]) if r["since"] else ""
            state = S("ok") + frames[tick % len(frames)] + " " + S("ok", bold=True) + f"{'JOB ' + elapsed:<10}"
        elif r["state"] == "idle":
            state = dot("teal") + " " + S("teal") + f"{'idle':<10}"
        elif r["state"] == "down":
            state = dot("crit") + " " + S("crit", bold=True) + f"{'down':<10}"
            if r["reason"] and r["reason"] != "exit-code":
                who += S("crit") + f"  {r['reason']}" + RESET
        else:
            continue
        lines.append(state + RESET + " " + who)
    off = [r for r in runners if r["state"] == "off"]
    if off:
        lines.append(S("dim") + f"● {len(off)} stopped: " + ", ".join(r["name"] for r in off) + RESET)
    note = " · ".join(f"{count[k]} {k}" for k in ("busy", "idle", "down", "off") if count[k]) + " · 5s"
    return Panel("github", "GitHub Runners", lines, note=note, role="ok" if count["busy"] else "accent",
                 min_lines=min(len(lines), 2))


def lan_panel(d: dict, iw: int) -> Panel:
    lan = d.get("lan")
    if not lan:
        return Panel("lan", "LAN", [S("dim") + "checking…" + RESET], note="10s", role="teal")
    lines = []
    for nic in lan["nics"]:
        wifi = nic["kind"] == "wifi"
        head = T.icon("wifi" if wifi else "lan") + " " + S("text", bold=True) + ("Wi-Fi" if wifi else "Ethernet") + RESET \
            + S("dim") + f" {nic['name']}" + RESET + "  "
        if not nic["up"]:
            state = S("dim") + ("● not connected" if wifi else "● no cable") + RESET
        elif wifi:
            state = dot("ok") + " " + S("text") + (nic["ssid"] or "connected") + RESET + "  " + signal_bars(nic["signal"])
            if nic["signal"] is not None:
                state += S("dim") + f" {nic['signal']}%" + (f" {nic['dbm']} dBm" if nic["dbm"] else "") + RESET
        else:
            speed = (f"{nic['speed'] // 1000} Gb/s" if nic["speed"] >= 1000 else f"{nic['speed']} Mb/s") if nic["speed"] else ""
            state = dot("ok") + " " + S("text") + "link up" + RESET + S("dim") + f" {speed}" + RESET
        if nic["default"]:
            state += S("accent") + "  ♦ default" + RESET
        lines.append(head + state)
        if nic["up"] and nic["ip"]:
            lines.append("   " + S("dim") + "IP " + RESET + S("text", bold=True) + nic["ip"] + RESET + S("dim") + f"/{nic['prefix']}" + RESET)
        elif nic["up"]:
            lines.append("   " + S("warn") + "link is up but no IPv4 address (DHCP?)" + RESET)
    if lan["gateway"]:
        gw = (dot("ok") + S("ok") + f" {lan['gw_ms']:.0f} ms" + RESET) if lan["gw_ms"] is not None \
            else (dot("crit") + S("crit", bold=True) + " not answering" + RESET)
        lines.append(S("dim") + "Gateway " + RESET + S("text") + lan["gateway"] + RESET + "  " + gw)
    else:
        lines.append(dot("crit") + S("crit", bold=True) + " No default gateway" + RESET)
    lines.append(S("dim") + "Devices " + RESET + S("text", bold=True) + str(lan["devices"]) + RESET + S("dim")
                 + f" seen on the LAN · {lan['active']} active now" + RESET)
    return Panel("lan", "LAN", lines, note="10s", role="teal", min_lines=min(len(lines), 4))


def internet_panel(d: dict, iw: int) -> Panel:
    net, traffic = d.get("internet"), d.get("traffic")
    hist = d.get("history") or {}
    lines = []
    if net:
        role = {"online": "ok", "degraded": "warn"}.get(net["state"], "crit")
        lines.append(S("dim") + "Status  " + RESET + dot(role) + " " + S(role, bold=True) + net["state"].upper() + RESET
                     + S("dim") + (f" · {net['latency']} ms" if net["latency"] is not None else "")
                     + f" · probes {net['probes_ok']}/{net['probes']}" + RESET)
    else:
        lines.append(S("dim") + "checking…" + RESET)
    if traffic:
        sw = max(6, iw - 16)
        lines.append(S("teal") + "↓ " + RESET + S("text", bold=True) + f"{human_rate(traffic['rx']):>10}" + RESET + "  "
                     + spark(hist.get("rx", []), sw, "teal"))
        lines.append(S("mauve") + "↑ " + RESET + S("text", bold=True) + f"{human_rate(traffic['tx']):>10}" + RESET + "  "
                     + spark(hist.get("tx", []), sw, "mauve"))
    if net and net["ts_state"] is not None:
        role = "ok" if net["ts_state"] == "Running" else "crit"
        lines.append(S("dim") + "Tailscale " + RESET + dot(role) + " " + S(role) + net["ts_state"] + RESET
                     + "  " + S("text") + (net["ts_ip"] or "") + RESET)
    def grow(extra: int) -> list[str]:
        if not traffic:
            return []
        return dual_chart(hist.get("rx", []), hist.get("tx", []), ("↓ in", "teal"), ("↑ out", "mauve"), iw, extra)

    return Panel("globe", "Internet", lines, note="1s/30s", role="blue", grow=grow)


# ---------------------------------------------------------------- page


def header(d: dict, width: int) -> str:
    cpu = d.get("cpu") or {}
    left = T.icon("host") + " " + S("bright", bold=True) + (cpu.get("host") or socket.gethostname()) + RESET
    if cpu.get("uptime"):
        left += S("dim") + "  up " + human_duration(cpu["uptime"]) + RESET
    power = d.get("power") or {}
    bat, ac = power.get("battery"), power.get("ac")
    right = ""
    if bat:
        cap = bat["capacity"] or 0
        role = "crit" if cap < 15 or ac is False else "warn" if cap < 30 else "ok"
        right += T.icon("battery") + " " + hbar(cap, 6, role) + S(role, bold=True) + f" {cap}%" + RESET
        if bat["status"] == "Charging":
            right += " " + T.icon("bolt") + S("dim") + (f" {human_duration(bat['eta'])} to full" if bat["eta"] else " charging") + RESET
        elif ac is False or bat["status"] == "Discharging":
            right += S("crit", bold=True) + " ON BATTERY" + RESET + (S("dim") + f" {human_duration(bat['eta'])} left" + RESET if bat["eta"] else "")
        elif ac:
            right += S("dim") + " plugged in" + RESET
        if bat["health"] is not None:
            right += S("dim") + f" · health {bat['health']}%" + RESET
        right += "    "
    right += S("bright", bold=True) + datetime.now().strftime("%a %d %b  %H:%M:%S") + RESET
    return fit(" " + left, width - vlen(right) - 1) + right + " "


def banner(issues: list[Issue], width: int, tall: bool) -> list[str]:
    crit = [i for i in issues if i.level == "crit"]
    warns = len(issues) - len(crit)
    if crit:
        kind = "crit"
        text = f"{len(issues)} ISSUE{'S' if len(issues) > 1 else ''}"
        sub = f"{len(crit)} critical" + (f" · {warns} warning{'s' if warns > 1 else ''}" if warns else "") + " - details below"
    elif issues:
        kind = "warn"
        text = f"{len(issues)} WARNING{'S' if len(issues) > 1 else ''}"
        sub = "nothing critical - worth a look"
    else:
        kind, text, sub = "ok", "ALL SYSTEMS OK", "every check passed - nothing needs your attention"
    bg = S("base", kind)
    blank = bg + " " * width + RESET
    if tall and T.glyphs != "basic":
        rows = [a + "    " + b for a, b in zip(halfblock(BIG_ICONS[kind]), big_text(text))]
        if vlen(rows[0]) <= width - 4:
            return [blank] + [bg + r.center(width) + RESET for r in rows] + [bg + sub.center(width) + RESET]
    line = bg + f"{text}  -  {sub}".center(width) + RESET
    return [blank, line, blank] if tall else [line]


def issue_lines(issues: list[Issue], width: int, max_rows: int) -> list[str]:
    if not issues:
        return []
    cols = 2 if width >= 100 and len(issues) > max_rows else 1
    colw = width // cols
    cells = [" " + T.icon(i.level) + " " + S("crit" if i.level == "crit" else "warn", bold=i.level == "crit") + i.text + RESET
             for i in issues]
    capacity = max_rows * cols
    if len(cells) > capacity:
        cells = cells[:capacity - 1] + [S("dim") + f"    + {len(issues) - capacity + 1} more" + RESET]
    rows = math.ceil(len(cells) / cols)
    return ["".join(fit(cells[c * rows + r], colw) for c in range(cols) if c * rows + r < len(cells)) for r in range(rows)]


def render_lines(snap: Snapshot, width: int, height: int | None = None, vt: int | None = None) -> list[str]:
    """Lay out the page. height=None renders at natural height (for --once)."""
    d = snap.data
    tall = height is None or height >= 34
    lines = [header(d, width)]
    lines += banner(snap.issues, width, tall)
    lines += issue_lines(snap.issues, width, 3 if tall else 2)
    avail = None if height is None else height - len(lines) - 1
    if width >= 110:
        lw = int(width * 0.54)
        rw = width - lw - 1
        # Left: fast-moving numbers you look at most. Right: state that changes occasionally.
        left = [p for p in (cpu_panel(d, lw - 4), gpu_panel(d, lw - 4), internet_panel(d, lw - 4)) if p]
        right = [p for p in (services_panel(d, rw - 4), runners_panel(d, rw - 4),
                             lan_panel(d, rw - 4), storage_panel(d, rw - 4)) if p]
        tallest = avail if avail is not None else max(len(stack(left, lw, None)), len(stack(right, rw, None)))
        lcol, rcol = stack(left, lw, avail, tallest), stack(right, rw, avail, tallest)
        lcol += [" " * lw] * (len(rcol) - len(lcol))
        rcol += [" " * rw] * (len(lcol) - len(rcol))
        lines += [a + " " + b for a, b in zip(lcol, rcol)]
    else:
        panels = [p for p in (cpu_panel(d, width - 4), services_panel(d, width - 4), runners_panel(d, width - 4),
                              gpu_panel(d, width - 4), lan_panel(d, width - 4), internet_panel(d, width - 4),
                              storage_panel(d, width - 4)) if p]
        lines += stack(panels, width, avail, avail)
    if height is not None:
        lines = lines[:height - 1]
        lines += [""] * (height - 1 - len(lines))
    footer = S("dim") + f" Updated {datetime.now().strftime('%H:%M:%S')} · refresh tuned per section:" \
        " cores 1s · GPU 3s · runners 5s · services 10s · LAN 10s · internet 30s · disks 60s"
    if vt:
        footer += f"  ·  Ctrl+Alt+F1 shell · Alt+F{vt} back"
    lines.append(footer + RESET)
    out = [fit(line, width) for line in lines]
    if T.glyphs == "basic":
        out = [line.translate(BASIC_MAP) for line in out]
    return out


def render(snap: Snapshot, width: int, height: int | None = None, vt: int | None = None) -> str:
    return "\x1b[H" + "\n".join(render_lines(snap, width, height, vt)) + "\x1b[J"


# ---------------------------------------------------------------- demo


def demo_snapshot(t: float | None = None) -> Snapshot:
    """Fictional data for screenshots and for trying the UI on any machine."""
    t = time.time() if t is None else t
    wave = lambda i, s=1.0: (math.sin(t / 3 * s + i * 1.7) + 1) / 2  # noqa: E731
    cores = [min(100.0, 8 + 85 * wave(i) ** 2) for i in range(8)]
    data = {
        "power": {"battery": {"capacity": 64, "status": "Charging", "eta": 2820, "health": 91, "watts": 18.4}, "ac": True},
        "cpu": {"host": "homelab", "uptime": 3_888_000, "load": [2.42, 2.1, 1.9], "count": 8, "cores": cores,
                "total": sum(cores) / len(cores), "ghz": 3.41},
        "memory": {"pct": 47, "used_gb": 7.4, "total_gb": 15.6, "swap_pct": 6},
        "disks": [{"mount": "/", "pct": 83, "avail_gb": 79.2}, {"mount": "/srv", "pct": 41, "avail_gb": 1120.5}],
        "temps": [("CPU", 61.0), ("NVMe", 41.0), ("WiFi", 39.0)],
        "gpus": [{"id": "0000:01:00.0", "vendor": "0x10de", "name": "NVIDIA GeForce RTX 3060", "state": "active",
                  "util": 38 + 30 * wave(9, 0.5), "mem_used": 4300, "mem_total": 12288, "temp": 63, "power": 71.5,
                  "clock": 1785, "max_clock": 2100, "pstate": "P2"},
                 {"id": "0000:00:02.0", "vendor": "0x8086", "name": "Intel UHD Graphics 770", "state": "active",
                  "util": 4.0, "mem_used": None, "mem_total": None, "temp": None, "power": None,
                  "clock": 350, "max_clock": 1450, "pstate": None}],
        "services": {"watched": [("nginx", "system", "active"), ("home-assistant", "user", "active"),
                                 ("backup", "system", "failed"), ("jellyfin", "system", "active"),
                                 ("pihole", "system", "active")],
                     "failed": [], "ssh": True},
        "runners": [{"unit": "a", "repo": "website", "name": "homelab-1", "state": "busy", "since": time.time() - 754, "reason": None},
                    {"unit": "b", "repo": "api", "name": "homelab-2", "state": "idle", "since": None, "reason": None},
                    {"unit": "c", "repo": "api", "name": "homelab-3", "state": "off", "since": None, "reason": None}],
        "lan": {"nics": [{"name": "eth0", "kind": "ethernet", "up": True, "default": True, "ip": "192.168.0.42",
                          "prefix": 24, "speed": 1000, "ssid": None, "signal": None, "dbm": None},
                         {"name": "wlan0", "kind": "wifi", "up": False, "default": False, "ip": None, "prefix": None,
                          "speed": None, "ssid": None, "signal": None, "dbm": None}],
                "iface": "eth0", "gateway": "192.168.0.1", "gw_ms": 0.6, "devices": 23, "active": 9},
        "internet": {"state": "online", "latency": 14, "probes_ok": 2, "probes": 2, "ts_state": "Running", "ts_ip": "100.64.0.7"},
        "traffic": {"iface": "eth0", "rx": 2_450_000 * (0.6 + wave(3)), "tx": 310_000 * (0.5 + wave(5))},
        "docker": {"available": True, "running": 11, "total": 12, "unhealthy": [], "restarting": []},
        "kernel": [],
        "diskio": {"read": 12_400_000 * wave(7), "write": 3_100_000 * wave(8)},
        "history": {"dr": [abs(9e6 * math.sin((t + i) / 7)) ** 1.0 * (1 if (i // 9) % 3 else 0.2) for i in range(120)],
                    "dw": [abs(3e6 * math.cos((t + i) / 4)) * (0.3 if (i // 13) % 2 else 1) for i in range(120)],"cpu": [35 + 30 * math.sin((t + i) / 6) + 10 * math.sin((t + i) / 2.3) for i in range(120)],
                    "rx": [abs(2e6 * math.sin((t + i) / 5)) + 2e5 for i in range(120)],
                    "tx": [abs(4e5 * math.sin((t + i) / 3.3)) + 5e4 for i in range(120)],
                    "gpu:0000:01:00.0": [40 + 35 * math.sin((t + i) / 4) for i in range(120)],
                    "gpu:0000:00:02.0": [3 + 2 * math.sin(t + i) for i in range(120)]},
    }
    return Snapshot(evaluate(data), data)


# ---------------------------------------------------------------- console font


def build_font(src: str, dst: str) -> int:
    """Copy a PSF console font, swapping rarely used accented glyphs for bar glyphs and icons."""
    with open(src, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:4] == b"\x72\xb5\x4a\x86":
        _, _, hs, flags, n, cs, h, w = struct.unpack("<8I", raw[:32])
        if not flags & 1:
            raise ValueError("font has no unicode table")
        glyphs = [raw[hs + i * cs: hs + (i + 1) * cs] for i in range(n)]
        entries = raw[hs + n * cs:].split(b"\xff")[:n]
    elif raw[:2] == b"\x36\x04":
        mode, cs = raw[2], raw[3]
        n, h, w = (512 if mode & 1 else 256), cs, 8
        if not mode & 6:
            raise ValueError("font has no unicode table")
        glyphs = [raw[4 + i * cs: 4 + (i + 1) * cs] for i in range(n)]
        table, pos, entries = raw[4 + n * cs:], 0, []
        for _ in range(n):
            chars = []
            while pos + 1 < len(table):
                (u,) = struct.unpack("<H", table[pos:pos + 2])
                pos += 2
                if u == 0xFFFF:
                    break
                if u != 0xFFFE:
                    chars.append(chr(u))
            entries.append("".join(chars).encode())
    else:
        raise ValueError("not a PSF font")
    maps = [set(e.split(b"\xfe")[0].decode("utf-8", "ignore")) for e in entries]
    have = set().union(*maps)
    sacrifice = (set(range(0xA1, 0x100)) - {0xAB, 0xBB, 0xB0, 0xB7}) | set(range(0x100, 0x250)) | set(range(0x2B0, 0x370))
    rank = lambda o: 0 if o >= 0x100 else 1 if o < 0xC0 else 2 if o < 0xE0 else 3  # noqa: E731
    spare = sorted((i for i, m in enumerate(maps) if m and all(ord(c) in sacrifice for c in m)),
                   key=lambda i: (max(rank(ord(c)) for c in maps[i]), i))
    need = [c for c in CUSTOM_CHARS if c not in have]
    if len(need) > len(spare):
        need = need[:len(spare)]
        if (len(need) - len([c for c in need if c in BLOCK_CHARS])) % 2:
            need = need[:-1]  # keep icon pairs whole
    bpr = (w + 7) // 8
    glyphs = [bytearray(g) for g in glyphs]
    for ch, gi in zip(need, spare):
        data = bytearray()
        for row in custom_glyph(ch, w, h):
            bits = sum(1 << (bpr * 8 - 1 - x) for x, on in enumerate(row) if on)
            data += bits.to_bytes(bpr, "big")
        glyphs[gi] = data
        entries[gi] = ch.encode()
    header = struct.pack("<8I", 0x864AB572, 0, 32, 1, n, bpr * h, h, w)
    with open(dst, "wb") as f:
        f.write(header + b"".join(bytes(g) for g in glyphs) + b"".join(e + b"\xff" for e in entries))
    return len(need)


# ---------------------------------------------------------------- app


class DisplayWatcher:
    """Detects lid-open and monitor-connect events, and whether any screen can be seen at all."""

    def __init__(self) -> None:
        self.last = self._state()

    @staticmethod
    def _state() -> tuple[bool, frozenset[str]]:
        lid_open = all("open" in (read(p) or "open") for p in glob.glob("/proc/acpi/button/lid/*/state"))
        connected = frozenset(os.path.basename(p) for p in glob.glob("/sys/class/drm/card*-*")
                              if read(f"{p}/status") == "connected")
        return lid_open, connected

    def screen_on(self) -> bool:
        """False only when the lid is closed and no external monitor is connected."""
        lid_open, connected = self.last
        return lid_open or any(not any(p in c for p in INTERNAL_PANELS) for c in connected)

    def triggered(self) -> bool:
        now = self._state()
        lid_opened = now[0] and not self.last[0]
        new_screen = bool(now[1] - self.last[1])
        self.last = now
        return lid_opened or new_screen


def tty_number() -> int | None:
    try:
        m = re.fullmatch(r"/dev/tty(\d+)", os.ttyname(sys.stdout.fileno()))
        return int(m.group(1)) if m else None
    except OSError:
        return None


def active_vt() -> int | None:
    try:
        data = fcntl.ioctl(sys.stdout.fileno(), VT_GETSTATE, b"\0" * 6)
        return struct.unpack("HHH", data)[0]
    except OSError:
        return None


def activate_vt(vt: int) -> None:
    try:
        fcntl.ioctl(sys.stdout.fileno(), VT_ACTIVATE, vt)
    except OSError as exc:
        print(f"glance: cannot switch to tty{vt}: {exc}", file=sys.stderr)


def terminal_size() -> tuple[int, int]:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        if rows and cols:
            return cols, rows
    except OSError:
        pass
    size = shutil.get_terminal_size((120, 40))
    return size.columns, size.lines


def configure_term(full_screen: bool) -> None:
    global T
    term = os.environ.get("TERM", "")
    glyphs = os.environ.get("GLANCE_GLYPHS") or ("basic" if term == "linux" else "unicode")
    colors = os.environ.get("GLANCE_COLORS")
    if not colors:
        if term == "linux":
            colors = "palette" if full_screen else "ansi"
        elif os.environ.get("COLORTERM") in ("truecolor", "24bit") or "256color" in term or "kitty" in term:
            colors = "truecolor"
        else:
            colors = "ansi"
    T = Term(glyphs, colors)


class Screen:
    """Full-screen output that only rewrites the lines that changed."""

    def __init__(self) -> None:
        self.prev: list[str] = []

    def draw(self, lines: list[str]) -> None:
        out = []
        if len(lines) != len(self.prev):
            out.append("\x1b[2J")
            self.prev = []
        for i, line in enumerate(lines):
            if i >= len(self.prev) or self.prev[i] != line:
                out.append(f"\x1b[{i + 1};1H{line}")
        self.prev = lines
        sys.stdout.write("".join(out))
        sys.stdout.flush()


USAGE = """usage: glance.py [--once | --json | --demo] [--no-switch]

  (no flags)          full-screen live dashboard on the current terminal
  --once              print a single frame and exit (exit code 1 on critical issues)
  --json              print a machine-readable snapshot and exit
  --demo              live dashboard with fictional sample data (add --once for one frame)
  --no-switch         don't bring this VT to the front at start-up
  --build-font S D    write console font D: font S plus Server Glance's icons and bar glyphs
  --version           print the version
"""


def sampled_snapshot() -> Snapshot:
    collector = Collector(load_config())
    collector.collect_all()
    time.sleep(1)  # usage and throughput need two samples
    for name in ("cpu", "traffic", "gpus"):
        collector.run_task(name)
    return collector.snapshot()


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(USAGE, end="")
        return 0
    if "--version" in args:
        print(__version__)
        return 0
    if "--build-font" in args:
        i = args.index("--build-font")
        added = build_font(args[i + 1], args[i + 2])
        print(f"glance: wrote {args[i + 2]} ({added} glyphs added)")
        return 0

    demo = "--demo" in args
    if "--json" in args:
        configure_term(full_screen=False)
        snap = demo_snapshot() if demo else sampled_snapshot()
        print(json.dumps({"issues": [i.__dict__ for i in snap.issues], **snap.data}, indent=2, default=str))
        return 1 if not demo and any(i.level == "crit" for i in snap.issues) else 0
    if "--once" in args:
        configure_term(full_screen=False)
        snap = demo_snapshot() if demo else sampled_snapshot()
        width, _ = terminal_size()
        print("\n".join(render_lines(snap, min(width, 150))))
        return 1 if not demo and any(i.level == "crit" for i in snap.issues) else 0

    configure_term(full_screen=True)
    collector = None
    if not demo:
        collector = Collector(load_config())
        collector.start()

    vt = tty_number()
    old_attrs = termios.tcgetattr(sys.stdin) if sys.stdin.isatty() else None
    if old_attrs:
        attrs = termios.tcgetattr(sys.stdin)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)  # swallow keystrokes
        termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)

    def restore(*_: object) -> None:
        sys.stdout.write(("\x1b]R" if T.colors == "palette" else "") + "\x1b[?25h\x1b[0m\x1b[2J\x1b[H")
        sys.stdout.flush()
        if old_attrs:
            termios.tcsetattr(sys.stdin, termios.TCSANOW, old_attrs)
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)
    if T.colors == "palette":
        sys.stdout.write(T.palette_sequence())
    sys.stdout.write("\x1b[0m\x1b[?25l\x1b[2J")

    watcher = DisplayWatcher()
    if vt and not demo and "--no-switch" not in args:
        activate_vt(vt)  # show ourselves at boot

    screen = Screen()
    last_render = 0.0
    time.sleep(0.3)  # let the first samples land
    while True:
        if vt and not demo and watcher.triggered():
            activate_vt(vt)
            last_render = 0.0
        # Nobody can see us (lid shut and no monitor, or another VT in front): don't draw.
        visible = demo or not vt or (watcher.screen_on() and active_vt() in (None, vt))
        if collector:
            collector.set_visible(visible)
        if not visible:
            screen.prev = []  # full redraw when we're seen again
            time.sleep(EVENT_POLL_SECONDS)
            continue
        if time.monotonic() - last_render >= RENDER_SECONDS:
            width, height = terminal_size()
            try:
                snap = demo_snapshot() if demo else collector.snapshot()
                screen.draw(render_lines(snap, width, height, vt))
            except Exception as exc:  # never die on a rendering bug; systemd would loop
                sys.stdout.write(f"\x1b[H\x1b[2Jglance: render error: {exc}")
                sys.stdout.flush()
                screen.prev = []
            last_render = time.monotonic()
        time.sleep(EVENT_POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
