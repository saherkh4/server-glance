<div align="center">

# 🖥️ Server Glance

**Open the lid. Know if your server is OK.**

A status screen for the physical console of your home server, old laptop or headless box.
It starts at boot, shows up by itself when you open the lid or plug in a monitor, and puts one
big green or red banner on top so you can tell from across the room.

[![CI](https://github.com/saherkh4/server-glance/actions/workflows/ci.yml/badge.svg)](https://github.com/saherkh4/server-glance/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![Platform](https://img.shields.io/badge/platform-Linux%20%2F%20systemd-orange)
[![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

<img src="docs/healthy.svg" alt="Server Glance showing a green ALL SYSTEMS OK banner with power, system, network and service panels" width="860">

</div>

---

## Why

Lots of us run a home server on an **old laptop** or a small box under the desk. When something
feels off, you open the lid and get… a login prompt. Then you log in, run `htop`, `df -h`,
`systemctl --failed`, `upower`, `docker ps`, `tailscale status`…

**Server Glance replaces all of that with one screen that's already waiting for you.**

<div align="center">
<img src="docs/issues.svg" alt="Server Glance showing a red banner: on battery power, a failed service, disk 83% full, CPU temperature 84C" width="860">
<br><sub>Charger unplugged? A service crashed? Disk filling up? You'll see it before you sit down.</sub>
</div>

## ✨ Features

| | |
| --- | --- |
| 🚦 **One-glance verdict** | A full-width banner: green **ALL SYSTEMS OK**, yellow warnings, or red **ATTENTION NEEDED**, with the issues listed under it, most serious first. |
| 🔋 **Battery & power** | Charge %, charging or discharging, time to full or empty, battery health vs. design capacity, power draw, and an alert when the **charger is unplugged**. Made with laptop servers in mind. |
| 📈 **System** | CPU load per core, memory, swap, every real disk, and temperatures (CPU, NVMe, Wi-Fi, chipset). |
| 🌐 **Network** | Internet reachability and latency, LAN or Wi-Fi IP, SSID and signal, and Tailscale state. |
| ⚙️ **Services** | SSH, your own list of systemd units (user or system), anything **failed or restart-looping**, and Docker containers that are unhealthy or restarting. |
| 🧯 **Kernel warnings** | Thermal, throttling, I/O, NVMe and out-of-memory messages from the last hour. |
| 👀 **Shows itself** | Opening the **lid** or plugging in an **HDMI/DP monitor** switches the console to the dashboard, even if you left it somewhere else. |
| 🪶 **Tiny & safe** | One Python file and the standard library. No browser, no X/Wayland, no web server, no open ports. Runs as your user, reads only, and never runs anything based on input. |

## 🚀 Quick start

```bash
git clone https://github.com/saherkh4/server-glance.git
cd server-glance

./glance.py --demo     # look at it right now, with sample data
./glance.py --once     # one real snapshot of this machine

sudo ./install.sh      # install it: starts on every boot, on tty8
```

That's it. Press **Ctrl+Alt+F8** to see it, or just close and reopen the lid.

> **Requirements:** Linux with systemd and Python 3.10+. Ubuntu Server 22.04+ and Debian 12+
> work out of the box. Optional tools improve what it can show: `docker`, `tailscale`,
> `networkctl`/`nmcli`/`iwgetid` for Wi-Fi names.

## 🧭 How it works

```
 boot ──► systemd ──► server-glance.service ──► full-screen dashboard on tty8 ──► console switches to tty8
                                                   │
          lid opened / monitor plugged in ◄────────┘ (checked every second) ──► switch back to tty8
```

- It runs on its **own virtual terminal** (tty8 by default), so your normal login on tty1
  stays where it is: **Ctrl+Alt+F1** for a shell, **Alt+F8** to come back.
- It uses a **larger console font** on its own terminal only, so it's readable from a distance.
- It refreshes every 5 seconds. Slower checks (network, Docker, journal) run in the
  background every 30 seconds, so the screen never freezes.
- It never crashes the screen. If a check fails, that panel shows "unavailable" or is
  hidden, and nothing gets made up.

## 🔧 Configuration

Optional. Put this in `~/.config/server-glance/config.json` for the user the service runs as,
or point `GLANCE_CONFIG` at a file.

```json
{
  "services": ["nginx", "docker", "home-assistant", "jellyfin"],
  "ignore_failed": ["^actions\\.runner\\."],
  "internet_probes": ["1.1.1.1:443", "8.8.8.8:53"]
}
```

| Key | What it does |
| --- | --- |
| `services` | Units shown in the Services panel. Each one is checked as a user unit and as a system unit, and the healthy one is shown. Units that are disabled on purpose are dimmed and don't raise an alarm. |
| `ignore_failed` | Regexes for failed units you don't want flagged. |
| `internet_probes` | `host:port` targets for plain TCP connects. No HTTP requests and no third-party "what's my IP" lookups. |

### Install options

```bash
sudo VT=9 ./install.sh                         # use tty9 instead
sudo FONT=Lat15-Terminus32x16 ./install.sh     # bigger or smaller font (see /usr/share/consolefonts)
sudo GLANCE_USER=alice ./install.sh            # run as a different user
sudo ./install.sh --uninstall                  # remove everything
```

## 🚦 What counts as a problem

| Check | ⚠️ Warning | ❌ Critical |
| --- | --- | --- |
| Power | running on battery, battery < 30%, health < 60% | battery < 15%, or on battery under 30% |
| Disk (any) | ≥ 80% | ≥ 90% |
| Memory | ≥ 85% | ≥ 92% |
| Swap | ≥ 80% | |
| Temperature | ≥ 80 °C | ≥ 90 °C |
| Load (5 min) | > 1.5 × CPU cores | |
| Watched services | | enabled but not running |
| Other systemd units | failed or restart-looping | |
| SSH | | not listening on :22 |
| Internet | some probes fail | all probes fail / no route |
| Tailscale | not `Running` | |
| Docker | unhealthy or restarting containers, daemon down | |

## 🔒 Security & privacy

- **Nothing is exposed to the network.** There's no web server and no listening port. The
  information only appears on the physical screen.
- Runs as an **unprivileged user** with `NoNewPrivileges`. The only extra capability is
  `CAP_SYS_TTY_CONFIG`, which it needs to switch the console to its own terminal.
- Read-only: it reads `/proc` and `/sys` and runs a fixed set of commands (`systemctl`,
  `df`, `ip`, `ss`, `docker`, `tailscale`, `journalctl`) with timeouts. It never builds a command
  from untrusted input.
- The only outbound traffic is the TCP connects you configure in `internet_probes`.
- Keep in mind that anyone who can see the screen can see what's on it (IP addresses, service
  names). That's the point, but think about where the screen is.

## 🛠️ Troubleshooting

```bash
systemctl status server-glance        # is it running?
journalctl -u server-glance -e        # logs
sudo chvt 8                           # jump to the dashboard from SSH
./glance.py --json                    # see exactly what it collected
```

- **Lid opens but the screen stays black.** Make sure the system doesn't suspend on lid close.
  Set `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`, which is usual for laptop servers.
- **Screen goes blank after a while.** Add `consoleblank=0` to the kernel command line.
- **Odd characters instead of bars.** Your console font is missing block glyphs. Use one of the
  `Lat15-Terminus*` fonts with `FONT=`.

## 🧪 Development

```bash
python3 -m unittest discover -s tests -v   # tests
./glance.py --demo                         # sample data, works on any machine
python3 tools/screenshot.py                # regenerate docs/*.svg
```

PRs are welcome. Please keep it to **one file with zero dependencies**.
Ideas: SMART disk health, UPS (NUT) status, ZFS/RAID state, a QR code that links to your
dashboard, and more languages.

## 📄 License

[MIT](LICENSE). Use it, fork it, and put it on your server.
