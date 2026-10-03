#!/usr/bin/env python3
"""Server Glance - one-glance server status on the local console.

Runs full-screen on a Linux virtual terminal (default tty8). Whenever the lid
is opened or an external screen is plugged in, it switches the console to its
own VT so the status is the first thing you see.

Stdlib only. Read-only: it never executes anything derived from input.
https://github.com/saherkh4/server-glance
"""
from __future__ import annotations

import fcntl
import glob
import json
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
from dataclasses import dataclass, field
from datetime import datetime

__version__ = "1.0.0"

REFRESH_SECONDS = 5
SLOW_REFRESH_SECONDS = 30
EVENT_POLL_SECONDS = 1

DEFAULT_CONFIG = {
    # Units shown in the Services panel, e.g. ["nginx", "docker", "my-app"].
    # Each is looked up as a user unit and a system unit; the healthier one wins.
    "services": [],
    # Failed units matching these regexes are not reported as issues.
    "ignore_failed": [],
    "internet_probes": ["1.1.1.1:443", "8.8.8.8:53"],
}

VT_ACTIVATE = 0x5606
VT_WAITACTIVE = 0x5607
VT_GETSTATE = 0x5603

# ---------------------------------------------------------------- helpers


def run(args: list[str], timeout: float = 3.0) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
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


@dataclass
class Issue:
    level: str  # "crit" | "warn"
    text: str


@dataclass
class Snapshot:
    issues: list[Issue] = field(default_factory=list)
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------- collectors


def collect_battery() -> dict | None:
    for base in sorted(glob.glob("/sys/class/power_supply/*")):
        if read(f"{base}/type") != "Battery" or read(f"{base}/scope") == "Device":
            continue
        cap = read_int(f"{base}/capacity")
        status = read(f"{base}/status") or "Unknown"
        # Batteries report either energy (uWh, rate in uW) or charge (uAh, rate in uA).
        unit, rate_file = ("energy", "power_now") if os.path.exists(f"{base}/energy_now") else ("charge", "current_now")
        energy_now = read_int(f"{base}/{unit}_now")
        energy_full = read_int(f"{base}/{unit}_full")
        energy_design = read_int(f"{base}/{unit}_full_design")
        power = read_int(f"{base}/{rate_file}") or 0
        eta = None
        if power and energy_now is not None and energy_full:
            if status == "Discharging":
                eta = energy_now / power * 3600
            elif status == "Charging":
                eta = (energy_full - energy_now) / power * 3600
        health = round(energy_full / energy_design * 100) if energy_full and energy_design else None
        watts = read_int(f"{base}/power_now")
        return {
            "name": os.path.basename(base),
            "capacity": cap,
            "status": status,
            "eta": eta,
            "health": health,
            "watts": round(watts / 1e6, 1) if watts else None,
            "cycles": read_int(f"{base}/cycle_count"),
        }
    return None


def collect_ac() -> bool | None:
    for base in glob.glob("/sys/class/power_supply/*"):
        if read(f"{base}/type") == "Mains":
            return read(f"{base}/online") == "1"
    return None


def collect_system() -> dict:
    uptime = float((read("/proc/uptime") or "0").split()[0])
    load = [float(x) for x in (read("/proc/loadavg") or "0 0 0").split()[:3]]
    mem = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        mem[key] = int(rest.split()[0]) * 1024 if rest.split() else 0
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    swap_total, swap_free = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    disks = []
    for line in run(["df", "-P", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs",
                     "-x", "overlay", "-x", "efivarfs"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[0].startswith("/dev/"):
            used_pct = int(parts[4].rstrip("%"))
            disks.append({"mount": parts[5], "pct": used_pct,
                          "avail_gb": round(int(parts[3]) / 1024 / 1024, 1)})
    return {
        "host": socket.gethostname(),
        "uptime": uptime,
        "load": load,
        "cores": os.cpu_count() or 1,
        "mem_pct": round((total - avail) / total * 100) if total else 0,
        "mem_used_gb": round((total - avail) / 1024**3, 1),
        "mem_total_gb": round(total / 1024**3, 1),
        "swap_pct": round((swap_total - swap_free) / swap_total * 100) if swap_total else 0,
        "disks": disks,
    }


def collect_temps() -> list[tuple[str, float]]:
    temps: dict[str, float] = {}
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(f"{hw}/name") or "hwmon"
        if name in ("BAT0", "ADP0") or name.startswith("hidpp"):
            continue
        for inp in glob.glob(f"{hw}/temp*_input"):
            value = read_int(inp)
            if value is None or value <= 0:
                continue
            # Keep only the hottest reading per device to stay compact.
            key = {"coretemp": "CPU", "k10temp": "CPU", "zenpower": "CPU", "nvme": "NVMe",
                   "ath10k_hwmon": "WiFi", "iwlwifi_1": "WiFi"}.get(name, name.split("_")[0])
            temps[key] = max(temps.get(key, 0), value / 1000)
    return sorted(temps.items(), key=lambda kv: -kv[1])


def unit_state(name: str) -> tuple[str, str]:
    """Return (scope, state). Checks user and system scope; an active unit in either wins."""
    unit = name if "." in name else f"{name}.service"
    found: list[tuple[str, str]] = []
    for scope in (["--user"], []):
        out = run(["systemctl", *scope, "show", unit, "-p", "LoadState,ActiveState,SubState,UnitFileState"]).strip()
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if props.get("LoadState") == "loaded":
            state = props.get("ActiveState", "?")
            if state == "activating" and props.get("SubState") == "auto-restart":
                state = "restarting"
            elif state == "inactive" and props.get("UnitFileState") != "enabled":
                state = "disabled"
            found.append(("user" if scope else "system", state))
    rank = {"active": 0, "reloading": 1, "activating": 2, "disabled": 3}
    return min(found, key=lambda f: rank.get(f[1], 9)) if found else ("-", "not-found")


def problem_units(scope: list[str]) -> list[tuple[str, str]]:
    out = run(["systemctl", *scope, "list-units", "--all", "--no-legend", "--plain",
               "--state=failed,auto-restart"])
    units = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            units.append((parts[0], parts[3]))
    return units


def collect_services(cfg: dict) -> dict:
    watched = [(name, *unit_state(name)) for name in cfg["services"]]
    watched = [w for w in watched if w[2] != "not-found"]
    ignore = [re.compile(p) for p in cfg.get("ignore_failed", [])]
    failed = []
    for scope, label in ((["--user"], "user"), ([], "system")):
        for unit, sub in problem_units(scope):
            if not any(p.search(unit) for p in ignore):
                failed.append((label, unit, sub))
    ssh_listening = ":22 " in run(["ss", "-tlnH"]) or " *:22" in run(["ss", "-tlnH"])
    return {"watched": watched, "failed": failed, "ssh": ssh_listening}


def collect_network(cfg: dict) -> dict:
    route = run(["ip", "-4", "route", "show", "default"]).splitlines()
    iface = gateway = ip = ssid = None
    signal_pct = None
    if route:
        m = re.search(r"via (\S+) dev (\S+)", route[0])
        if m:
            gateway, iface = m.group(1), m.group(2)
    if iface and re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface):
        m = re.search(r"inet (\S+)/", run(["ip", "-4", "-o", "addr", "show", "dev", iface]))
        ip = m.group(1) if m else None
        if os.path.isdir(f"/sys/class/net/{iface}/wireless"):
            m = re.search(r"Wi-Fi access point: (.+?) \(", run(["networkctl", "status", "--no-pager", iface]))
            ssid = m.group(1) if m else None
            if not ssid and shutil.which("iwgetid"):
                ssid = run(["iwgetid", "-r", iface]).strip() or None
            if not ssid and shutil.which("nmcli"):
                for line in run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
                    if line.startswith("yes:"):
                        ssid = line[4:]
            for line in (read("/proc/net/wireless") or "").splitlines():
                if line.strip().startswith(f"{iface}:"):
                    quality = float(line.split()[2].rstrip("."))
                    signal_pct = min(100, round(quality / 70 * 100))
    ok = 0
    latency = None
    for probe in cfg["internet_probes"]:
        host, _, port = probe.rpartition(":")
        start = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                ok += 1
                ms = round((time.monotonic() - start) * 1000)
                latency = ms if latency is None else min(latency, ms)
        except OSError:
            pass
    total = len(cfg["internet_probes"])
    internet = "online" if ok == total else ("degraded" if ok else "offline")
    ts_state, ts_ip = None, None
    if shutil.which("tailscale"):
        try:
            data = json.loads(run(["tailscale", "status", "--json"]) or "{}")
            ts_state = data.get("BackendState")
            ts_ip = next((a for a in data.get("TailscaleIPs") or [] if "." in a), None)
        except json.JSONDecodeError:
            ts_state = "error"
    return {"iface": iface, "ip": ip, "gateway": gateway, "ssid": ssid, "signal": signal_pct,
            "wifi": ssid is not None or (iface is not None and os.path.isdir(f"/sys/class/net/{iface}/wireless")),
            "internet": internet, "latency": latency, "ts_state": ts_state, "ts_ip": ts_ip}


def collect_docker() -> dict | None:
    if not shutil.which("docker"):
        return None
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"], timeout=5)
    if not out and run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=5) == "":
        return {"available": False}
    rows = [line.split("|", 2) for line in out.splitlines() if line.count("|") == 2]
    return {
        "available": True,
        "running": sum(1 for r in rows if r[1] == "running"),
        "total": len(rows),
        "unhealthy": [r[0] for r in rows if "unhealthy" in r[2]],
        "restarting": [r[0] for r in rows if r[1] == "restarting"],
    }


def collect_kernel_warnings() -> list[str]:
    out = run(["journalctl", "-k", "-p", "warning", "--since", "-1h", "-o", "cat", "--no-pager", "-q"])
    pattern = re.compile(r"thermal|temperature|overheat|thrott|critical|I/O error|nvme|oom|out of memory", re.I)
    return [line for line in out.splitlines() if pattern.search(line)][-3:]


# ---------------------------------------------------------------- evaluation


def evaluate(d: dict) -> list[Issue]:
    issues: list[Issue] = []
    add = lambda level, text: issues.append(Issue(level, text))  # noqa: E731

    bat, ac = d.get("battery"), d.get("ac")
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

    s = d["system"]
    for disk in s["disks"]:
        if disk["pct"] >= 90:
            add("crit", f"Disk {disk['mount']} {disk['pct']}% full ({disk['avail_gb']} GB left)")
        elif disk["pct"] >= 80:
            add("warn", f"Disk {disk['mount']} {disk['pct']}% full")
    if s["mem_pct"] >= 92:
        add("crit", f"Memory {s['mem_pct']}% used")
    elif s["mem_pct"] >= 85:
        add("warn", f"Memory {s['mem_pct']}% used")
    if s["swap_pct"] >= 80:
        add("warn", f"Swap {s['swap_pct']}% used")
    if s["load"][1] > s["cores"] * 1.5:
        add("warn", f"High load: {s['load'][1]:.1f} on {s['cores']} cores")

    for label, temp in d.get("temps", []):
        if temp >= 90:
            add("crit", f"{label} temperature {temp:.0f}C")
        elif temp >= 80:
            add("warn", f"{label} temperature {temp:.0f}C")

    svc = d.get("services")
    if svc:
        for name, scope, state in svc["watched"]:
            if state not in ("active", "disabled"):
                add("crit", f"Service {name} is {state}")
        watched_units = {f"{n}.service" for n, _, _ in svc["watched"]}
        for scope, unit, sub in svc["failed"]:
            if unit not in watched_units:
                add("warn", f"{scope} unit {unit.removesuffix('.service')} {sub}")
        if not svc["ssh"]:
            add("crit", "SSH is not listening on port 22")

    net = d.get("network")
    if net:
        if net["internet"] == "offline":
            add("crit", "No internet connectivity")
        elif net["internet"] == "degraded":
            add("warn", "Internet connectivity degraded")
        if not net["iface"]:
            add("crit", "No default network route")
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

    for line in d.get("kernel", []):
        add("warn", f"Kernel: {line[:90]}")

    issues.sort(key=lambda i: 0 if i.level == "crit" else 1)
    return issues


# ---------------------------------------------------------------- rendering

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
RED, GREEN, YELLOW, CYAN, WHITE = "\x1b[31m", "\x1b[32m", "\x1b[33m", "\x1b[36m", "\x1b[37m"
BG_RED, BG_GREEN, BG_YELLOW = "\x1b[41m", "\x1b[42m", "\x1b[43m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def vlen(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def pad(s: str, width: int) -> str:
    visible = vlen(s)
    if visible > width:
        # Truncate visible text, keeping escape codes intact.
        out, count = [], 0
        for token in re.split(r"(\x1b\[[0-9;]*m)", s):
            if ANSI_RE.fullmatch(token or "x"):
                out.append(token)
            else:
                out.append(token[: max(0, width - count)])
                count += len(token)
        return "".join(out) + RESET
    return s + " " * (width - visible)


def size_label(gb: float) -> str:
    return f"{gb / 1024:.1f}T" if gb >= 1000 else f"{gb:.0f}G" if gb >= 100 else f"{gb}G"


def color_for(pct: float, warn: float, crit: float) -> str:
    return RED if pct >= crit else YELLOW if pct >= warn else GREEN


def bar(pct: float, width: int, color: str) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def state_color(state: str) -> str:
    if state == "disabled":
        return DIM
    return GREEN if state == "active" else YELLOW if state in ("activating", "reloading") else RED


def render(snap: Snapshot, width: int, height: int, vt: int | None = None) -> str:
    d = snap.data
    s = d["system"]
    lines: list[str] = []
    now = datetime.now().strftime("%a %d %b  %H:%M:%S")

    header = f"{BOLD} {s['host']}{RESET}{DIM}  up {human_duration(s['uptime'])}{RESET}"
    lines.append(pad(header, width - len(now) - 1) + f"{BOLD}{now}{RESET}")

    crit = [i for i in snap.issues if i.level == "crit"]
    if crit:
        bg, msg = BG_RED, f"!! {len(snap.issues)} ISSUE{'S' if len(snap.issues) > 1 else ''} - ATTENTION NEEDED"
    elif snap.issues:
        bg, msg = BG_YELLOW, f"!  {len(snap.issues)} WARNING{'S' if len(snap.issues) > 1 else ''}"
    else:
        bg, msg = BG_GREEN, "ALL SYSTEMS OK"
    blank = f"{bg}{' ' * width}{RESET}"
    lines += [blank, f"{bg}{BOLD}\x1b[30m{msg.center(width)}{RESET}", blank]

    max_issues = max(3, height // 4)
    for issue in snap.issues[:max_issues]:
        c = RED if issue.level == "crit" else YELLOW
        lines.append(f" {c}{BOLD}{'X' if issue.level == 'crit' else '!'}{RESET} {issue.text}")
    if len(snap.issues) > max_issues:
        lines.append(f"   {DIM}+{len(snap.issues) - max_issues} more{RESET}")
    lines.append("")

    col = (width - 3) // 2
    left: list[str] = []
    right: list[str] = []
    bw = max(10, col - 26)

    # Battery
    bat, ac = d.get("battery"), d.get("ac")
    left.append(f"{CYAN}{BOLD}POWER{RESET}")
    if bat:
        cap = bat["capacity"] or 0
        cc = RED if cap < 15 else YELLOW if cap < 30 else GREEN
        left.append(f" Battery {bar(cap, bw, cc)} {BOLD}{cap:>3}%{RESET}")
        ac_txt = f"{GREEN}AC plugged in{RESET}" if ac else f"{RED}{BOLD}ON BATTERY{RESET}" if ac is False else "AC ?"
        status = bat["status"]
        extra = ""
        if bat["eta"]:
            extra = f" - {human_duration(bat['eta'])} {'to full' if status == 'Charging' else 'left'}"
        left.append(f" {ac_txt}{DIM} | {status}{extra}{RESET}")
        details = []
        if bat["health"] is not None:
            details.append(f"health {bat['health']}%")
        if bat["watts"]:
            details.append(f"{bat['watts']} W")
        if details:
            left.append(f" {DIM}{' | '.join(details)}{RESET}")
    else:
        left.append(f" {DIM}No battery{RESET}  AC: {'yes' if ac else 'unknown'}")
    left.append("")

    # System
    left.append(f"{CYAN}{BOLD}SYSTEM{RESET}")
    load_pct = s["load"][0] / s["cores"] * 100
    left.append(f" CPU     {bar(load_pct, bw, color_for(load_pct, 100, 150))} "
                f"{s['load'][0]:.2f}/{s['cores']}")
    left.append(f" Memory  {bar(s['mem_pct'], bw, color_for(s['mem_pct'], 85, 92))} "
                f"{s['mem_pct']:>3}% {DIM}{s['mem_used_gb']}/{s['mem_total_gb']}G{RESET}")
    left.append(f" Swap    {bar(s['swap_pct'], bw, color_for(s['swap_pct'], 60, 80))} {s['swap_pct']:>3}%")
    for disk in s["disks"][:3]:
        label = disk["mount"] if len(disk["mount"]) <= 7 else "…" + disk["mount"][-6:]
        left.append(f" {label:<7} {bar(disk['pct'], bw, color_for(disk['pct'], 80, 90))} "
                    f"{disk['pct']:>3}% {DIM}{size_label(disk['avail_gb'])} free{RESET}")
    temps = d.get("temps", [])
    if temps:
        parts = [f"{color_for(t, 80, 90)}{label} {t:.0f}C{RESET}" for label, t in temps[:4]]
        left.append(" " + "  ".join(parts))

    # Network
    net = d.get("network")
    right.append(f"{CYAN}{BOLD}NETWORK{RESET}")
    if net:
        ic = {"online": GREEN, "degraded": YELLOW}.get(net["internet"], RED)
        lat = f" {DIM}{net['latency']} ms{RESET}" if net["latency"] is not None else ""
        right.append(f" Internet  {ic}{BOLD}{net['internet'].upper()}{RESET}{lat}")
        if net["iface"]:
            kind = "Wi-Fi" if net["wifi"] else "LAN"
            right.append(f" {kind:<9} {net['ip'] or '?'} {DIM}({net['iface']}){RESET}")
            if net["wifi"]:
                sig = f" {net['signal']}%" if net["signal"] is not None else ""
                right.append(f"           {net['ssid'] or '?'}{DIM}{sig}{RESET}")
        else:
            right.append(f" {RED}No default route{RESET}")
        if net["ts_state"] is not None:
            tc = GREEN if net["ts_state"] == "Running" else RED
            right.append(f" Tailscale {tc}{net['ts_state']}{RESET} {net['ts_ip'] or ''}")
    else:
        right.append(f" {DIM}checking...{RESET}")
    right.append("")

    # Services
    svc = d.get("services")
    right.append(f"{CYAN}{BOLD}SERVICES{RESET}")
    if svc:
        ssh_c = GREEN if svc["ssh"] else RED
        right.append(f" {ssh_c}●{RESET} ssh {DIM}{'listening :22' if svc['ssh'] else 'NOT listening'}{RESET}")
        for name, scope, state in svc["watched"]:
            suffix = "" if state == "active" else f" {state_color(state)}{state}{RESET}"
            right.append(f" {state_color(state)}●{RESET} {name}{suffix}")
        if svc["failed"]:
            right.append(f" {RED}{len(svc['failed'])} failed unit(s){RESET}")
    dk = d.get("docker")
    if dk and dk["available"]:
        bad = len(dk["unhealthy"]) + len(dk["restarting"])
        dc = YELLOW if bad else GREEN
        right.append(f" {dc}●{RESET} docker {dk['running']}/{dk['total']} running"
                     + (f" {YELLOW}{bad} unhealthy{RESET}" if bad else ""))

    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        lines.append(pad(" " + l, col + 1) + " " + pad(r, col + 1))

    while len(lines) < height - 1:
        lines.append("")
    footer = f"{DIM} Updated {datetime.now().strftime('%H:%M:%S')} - refreshes every {REFRESH_SECONDS}s"
    if vt:
        footer += f" - Ctrl+Alt+F1 for a login shell, Alt+F{vt} to come back"
    lines = lines[: height - 1] + [footer + RESET]
    return "\x1b[H" + "\n".join(pad(line, width) for line in lines) + "\x1b[J"


# ---------------------------------------------------------------- demo

def demo_snapshot() -> Snapshot:
    """Fictional data for screenshots and trying the UI on any machine."""
    data = {
        "battery": {"name": "BAT0", "capacity": 64, "status": "Charging", "eta": 2820,
                    "health": 91, "watts": 18.4, "cycles": 212},
        "ac": True,
        "system": {"host": "homelab", "uptime": 3_888_000, "load": [1.42, 1.1, 0.9], "cores": 8,
                   "mem_pct": 47, "mem_used_gb": 7.4, "mem_total_gb": 15.6, "swap_pct": 6,
                   "disks": [{"mount": "/", "pct": 83, "avail_gb": 79.2},
                             {"mount": "/srv", "pct": 41, "avail_gb": 1120.5}]},
        "temps": [("CPU", 58.0), ("NVMe", 41.0), ("WiFi", 39.0)],
        "network": {"iface": "wlan0", "ip": "192.168.0.42", "gateway": "192.168.0.1", "ssid": "HomeNet",
                    "signal": 78, "wifi": True, "internet": "online", "latency": 14,
                    "ts_state": "Running", "ts_ip": "100.64.0.7"},
        "services": {"watched": [("nginx", "system", "active"), ("docker", "system", "active"),
                                 ("home-assistant", "user", "active"), ("backup", "system", "failed"),
                                 ("jellyfin", "system", "active")],
                     "failed": [("system", "backup.service", "failed")], "ssh": True},
        "docker": {"available": True, "running": 11, "total": 12, "unhealthy": [], "restarting": []},
        "kernel": [],
    }
    return Snapshot(evaluate(data), data)


# ---------------------------------------------------------------- app


class Collector:
    """Fast data is collected inline; slow data (network, docker, journal, services) in a thread."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.slow: dict = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._slow_loop, daemon=True).start()

    def _slow_loop(self) -> None:
        while True:
            data = {
                "services": collect_services(self.cfg),
                "network": collect_network(self.cfg),
                "docker": collect_docker(),
                "kernel": collect_kernel_warnings(),
            }
            with self.lock:
                self.slow = data
            self.wake.wait(SLOW_REFRESH_SECONDS)
            self.wake.clear()

    def snapshot(self) -> Snapshot:
        data = {"battery": collect_battery(), "ac": collect_ac(), "system": collect_system(),
                "temps": collect_temps()}
        with self.lock:
            data.update(self.slow)
        return Snapshot(evaluate(data), data)


class DisplayWatcher:
    """Detects lid-open and monitor-connect events."""

    def __init__(self) -> None:
        self.last = self._state()

    @staticmethod
    def _state() -> tuple[bool, frozenset[str]]:
        lid_open = all("open" in (read(p) or "open")
                       for p in glob.glob("/proc/acpi/button/lid/*/state"))
        connected = frozenset(os.path.basename(p) for p in glob.glob("/sys/class/drm/card*-*")
                              if read(f"{p}/status") == "connected")
        return lid_open, connected

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
    size = shutil.get_terminal_size((100, 30))
    return size.columns, size.lines


USAGE = """usage: glance.py [--once | --json | --demo] [--no-switch]

  (no flags)   full-screen live dashboard on the current terminal
  --once       print a single frame and exit (exit code 1 on critical issues)
  --json       print a machine-readable snapshot and exit
  --demo       render fictional sample data (add --once for a single frame)
  --no-switch  don't bring this VT to the front at start-up
  --version    print the version
"""


def main() -> int:
    if "-h" in sys.argv or "--help" in sys.argv:
        print(USAGE, end="")
        return 0
    if "--version" in sys.argv:
        print(__version__)
        return 0
    if "--demo" in sys.argv:
        width, height = terminal_size()
        frame = render(demo_snapshot(), min(width, 120), 34)
        if "--once" in sys.argv:
            print(frame.replace("\x1b[H", "").replace("\x1b[J", ""))
        else:
            sys.stdout.write("\x1b[2J" + frame)
            sys.stdout.flush()
            try:
                signal.pause()
            except KeyboardInterrupt:
                print()
        return 0

    cfg = load_config()
    collector = Collector(cfg)

    if "--once" in sys.argv or "--json" in sys.argv:
        while not collector.slow:
            time.sleep(0.1)
        snap = collector.snapshot()
        if "--json" in sys.argv:
            print(json.dumps({"issues": [i.__dict__ for i in snap.issues], **snap.data}, indent=2, default=str))
        else:
            width, height = terminal_size()
            print(render(snap, min(width, 120), 40).replace("\x1b[H", "").replace("\x1b[J", ""))
        return 1 if any(i.level == "crit" for i in snap.issues) else 0

    vt = tty_number()
    is_tty = sys.stdin.isatty()
    old_attrs = termios.tcgetattr(sys.stdin) if is_tty else None
    if is_tty:
        attrs = termios.tcgetattr(sys.stdin)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)  # swallow keystrokes
        termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)

    def restore(*_: object) -> None:
        sys.stdout.write("\x1b[?25h\x1b[0m\x1b[2J\x1b[H")
        sys.stdout.flush()
        if old_attrs:
            termios.tcsetattr(sys.stdin, termios.TCSANOW, old_attrs)
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)
    sys.stdout.write("\x1b[?25l\x1b[2J")  # hide cursor, clear

    watcher = DisplayWatcher()
    if vt and "--no-switch" not in sys.argv:
        activate_vt(vt)  # show ourselves at boot

    last_render = 0.0
    while True:
        if vt and watcher.triggered():
            activate_vt(vt)
            last_render = 0.0
        if time.monotonic() - last_render >= REFRESH_SECONDS:
            width, height = terminal_size()
            try:
                frame = render(collector.snapshot(), width, height, vt)
            except Exception as exc:  # never die on a rendering bug; systemd would loop
                frame = f"\x1b[H\x1b[2Jglance: render error: {exc}"
            sys.stdout.write(frame)
            sys.stdout.flush()
            last_render = time.monotonic()
        time.sleep(EVENT_POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
