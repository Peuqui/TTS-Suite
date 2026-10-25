#!/bin/bash
# Switch the TTS-Suite from the systemd USER unit to the SYSTEM unit tts-suite@<user>.service, so that
# the service control page (www-data) and AIfred can start and stop it through polkit.
# Run as root: sudo deploy/switch-to-system-unit.sh [user]
# The unit and the polkit rule come from deploy/system/ of this repository. Same port as before (5090),
# so the two units must never run together: the user unit is stopped and disabled first.
# Optional, only on a machine that has such a setup: MINIPC=<folder with system-config/> also installs
# the nginx snippet and names the service control page (steps 4 and 5).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Needs root: sudo $0 [user]" >&2
    exit 1
fi
SERVICE_USER=${1:-mp}
USER_ID=$(id -u "$SERVICE_USER")
HOME_DIR=$(getent passwd "$SERVICE_USER" | cut -d: -f6)
REPO=$(cd "$(dirname "$0")/.." && pwd)
SYSTEM_FILES=$REPO/deploy/system
MINIPC=${MINIPC:-}
UNIT=tts-suite@$SERVICE_USER.service

for file in tts-suite@.service 60-tts-suite.rules.in; do
    [[ -f $SYSTEM_FILES/$file ]] || { echo "Missing: $SYSTEM_FILES/$file" >&2; exit 1; }
done
[[ -x $HOME_DIR/.local/share/tts-suite/venv/bin/tts-suite ]] || { echo "No tts-suite in $HOME_DIR/.local/share/tts-suite/venv: run deploy/install.sh first." >&2; exit 1; }
# Checked before anything changes: failing later would leave the user unit stopped and no system unit.
# Without polkit neither the user nor AIfred could start or restart the system unit without sudo.
POLKIT_RULES=/etc/polkit-1/rules.d
[[ -d $POLKIT_RULES ]] || { echo "No $POLKIT_RULES: polkit is missing. Install it first (Debian/Ubuntu: sudo apt install polkitd; Fedora: sudo dnf install polkit; Arch: sudo pacman -S polkit), then run this again." >&2; exit 1; }

echo "== 1/3 Stopping and disabling the user unit of $SERVICE_USER (port 5090 must be free)"
sudo -u "$SERVICE_USER" XDG_RUNTIME_DIR="/run/user/$USER_ID" systemctl --user disable --now tts-suite || echo "(no user unit was running)"

echo "== 2/3 Installing the system unit and the polkit rule"
install -v -m 644 "$SYSTEM_FILES/tts-suite@.service" /etc/systemd/system/tts-suite@.service
sed "s/@SERVICE_USER@/$SERVICE_USER/g" "$SYSTEM_FILES/60-tts-suite.rules.in" > "$POLKIT_RULES/60-tts-suite.rules"
chmod 644 "$POLKIT_RULES/60-tts-suite.rules"
systemctl daemon-reload

echo "== 3/3 Starting $UNIT"
systemctl enable --now "$UNIT"
sleep 3
systemctl is-active "$UNIT"
curl -fsS -o /dev/null -w "GET /engines: HTTP %{http_code}\n" http://127.0.0.1:5090/engines

if [[ -n $MINIPC ]]; then
    CONFIG=$MINIPC/system-config
    echo "== 4/5 nginx: installing the snippet; the site file is NOT overwritten"
    install -v -m 644 "$CONFIG/nginx/snippets/tts-suite.conf" /etc/nginx/snippets/tts-suite.conf
    if diff -u /etc/nginx/sites-available/narnia "$CONFIG/nginx/sites-available/narnia"; then
        echo "The site file equals the repository's."
    else
        echo "^ The site file differs from the repository. It needs the lines"
        echo "    include snippets/tts-suite.conf;                       (next to agent-orc.conf)"
        echo "    location = /tts-suite-icon.svg { root /var/www/html; }  (next to agent-orc-icon.svg)"
        echo "  Add them (or take the repository's file if the other differences are old), then:"
    fi
    echo "  nginx -t && systemctl reload nginx"
    echo "== 5/5 Landing page and service control page"
    echo "  sudo $CONFIG/ai-service-control/install.sh"
    echo "  (it also copies the icon tts-suite-icon.svg; /tts-suite/ui/ is then reachable behind the login)"
fi
echo "Done. Check: systemctl status $UNIT; http://127.0.0.1:5090/ui/"
