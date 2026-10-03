#!/usr/bin/env bash
# Installs Server Glance as a boot-time systemd service on a spare virtual terminal.
#
#   sudo ./install.sh                                   # tty8, runs as the invoking user
#   sudo VT=9 FONT=Lat15-Terminus32x16 ./install.sh     # different VT / font
#   sudo GLANCE_USER=alice ./install.sh                 # run as another user
#   sudo ./install.sh --uninstall
set -euo pipefail

UNIT=/etc/systemd/system/server-glance.service
PREFIX=/usr/local/lib/server-glance
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VT="${VT:-8}"
FONT="${FONT:-Lat15-TerminusBold28x14}"

if [[ $EUID -ne 0 ]]; then
  exec sudo VT="$VT" FONT="$FONT" GLANCE_USER="${GLANCE_USER:-$(id -un)}" "$0" "$@"
fi
RUN_USER="${GLANCE_USER:-${SUDO_USER:-root}}"

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl disable --now server-glance.service 2>/dev/null || true
  rm -f "$UNIT"
  rm -rf "$PREFIX"
  systemctl daemon-reload
  echo "Server Glance removed."
  exit 0
fi

[[ "$VT" =~ ^[0-9]+$ ]] || { echo "VT must be a number" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "Unknown user: $RUN_USER" >&2; exit 1; }

RUN_UID="$(id -u "$RUN_USER")"
RUN_GROUP="$(id -gn "$RUN_USER")"

install -d -m 755 "$PREFIX"
install -m 755 "$SRC/glance.py" "$PREFIX/glance.py"

# Build a copy of the console font with icons and smooth bar glyphs added.
FONTFILE="$FONT"
GLYPHS=basic
FONT_SRC="$(ls /usr/share/consolefonts/"$FONT".psf* 2>/dev/null | head -n1 || true)"
if [[ -n "$FONT_SRC" ]] && python3 "$PREFIX/glance.py" --build-font "$FONT_SRC" "$PREFIX/glance.psf"; then
  FONTFILE="$PREFIX/glance.psf"
  GLYPHS=console
else
  echo "Note: could not build the icon font from '$FONT'; using plain glyphs." >&2
fi

# Lets the user's systemd manager run at boot without a login, so user units can be checked.
loginctl enable-linger "$RUN_USER" 2>/dev/null || true

sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$RUN_USER|g" -e "s|@GROUP@|$RUN_GROUP|g" \
    -e "s|@UID@|$RUN_UID|g" -e "s|@VT@|$VT|g" -e "s|@FONTFILE@|$FONTFILE|g" -e "s|@GLYPHS@|$GLYPHS|g" \
    "$SRC/server-glance.service.in" > "$UNIT"
chmod 644 "$UNIT"

systemctl daemon-reload
systemctl enable server-glance.service >/dev/null
systemctl restart server-glance.service
echo "Server Glance is running on tty$VT and will start on every boot."
echo "  Switch to it:  Ctrl+Alt+F$VT   (or: sudo chvt $VT)"
echo "  Logs:          journalctl -u server-glance -e"
