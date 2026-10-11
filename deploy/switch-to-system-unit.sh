#!/bin/bash
# Switch the TTS-Suite from the systemd USER unit to the SYSTEM unit tts-suite@<user>.service, so that
# the service control page (www-data) and AIfred can start and stop it through polkit.
# Run as root: sudo deploy/switch-to-system-unit.sh [user]
# The files come from the MiniPCLinux repository (system-config/...); set MINIPC to its path if it
# is not ~<user>/MiniPCLinux. Same port as before (5090), so the two units must never run together:
# the user unit is stopped and disabled first.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Needs root: sudo $0 [user]" >&2
    exit 1
fi
SERVICE_USER=${1:-mp}
USER_ID=$(id -u "$SERVICE_USER")
HOME_DIR=$(getent passwd "$SERVICE_USER" | cut -d: -f6)
MINIPC=${MINIPC:-$HOME_DIR/MiniPCLinux}
CONFIG=$MINIPC/system-config
UNIT=tts-suite@$SERVICE_USER.service

for file in systemd/system/tts-suite@.service polkit-1/rules.d/60-tts-suite.rules nginx/snippets/tts-suite.conf; do
    [[ -f $CONFIG/$file ]] || { echo "Missing: $CONFIG/$file" >&2; exit 1; }
done
[[ -x $HOME_DIR/.local/share/tts-suite/venv/bin/tts-suite ]] || { echo "No tts-suite in $HOME_DIR/.local/share/tts-suite/venv: run deploy/install.sh first." >&2; exit 1; }

echo "== 1/5 Stopping and disabling the user unit of $SERVICE_USER (port 5090 must be free)"
sudo -u "$SERVICE_USER" XDG_RUNTIME_DIR="/run/user/$USER_ID" systemctl --user disable --now tts-suite || echo "(no user unit was running)"

echo "== 2/5 Installing the system unit and the polkit rule"
install -v -m 644 "$CONFIG/systemd/system/tts-suite@.service" /etc/systemd/system/tts-suite@.service
install -v -m 644 "$CONFIG/polkit-1/rules.d/60-tts-suite.rules" /etc/polkit-1/rules.d/60-tts-suite.rules
systemctl daemon-reload

echo "== 3/5 Starting $UNIT"
systemctl enable --now "$UNIT"
sleep 3
systemctl is-active "$UNIT"
curl -fsS -o /dev/null -w "GET /engines: HTTP %{http_code}\n" http://127.0.0.1:5090/engines

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
echo "Done. Check: systemctl status $UNIT; the page https://<host>/tts-suite/ui/"
