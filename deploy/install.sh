#!/bin/bash
# Install the TTS-Suite service for the current user: its own venv with the code of this clone
# (editable: the compose files and scripts of the engines live in the clone and are found from the
# code), the command `tts-suite` in ~/.local/bin, and a systemd user service that runs it.
# Also makes the token (readable for you alone) and, if there is none yet, a first configuration
# (~/.config/tts-suite/config.json: the voices folder of this clone, `voices/`, and the pronunciation of
# AIfred as Alfred); the hosts and the defaults per engine are set later on the page of the service.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
VENV=${XDG_DATA_HOME:-$HOME/.local/share}/tts-suite/venv
BIN=$HOME/.local/bin
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    echo "Python 3.12 or newer is needed; python3 here is $(python3 --version 2>&1)." >&2
    exit 1
fi
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet -e "$REPO"
mkdir -p "$BIN" "$UNITS"
ln -sfn "$VENV/bin/tts-suite" "$BIN/tts-suite"
cp "$REPO/deploy/tts-suite.service" "$UNITS/tts-suite.service"
systemctl --user daemon-reload
"$BIN/tts-suite" token
CONFIG_FILE=${XDG_CONFIG_HOME:-$HOME/.config}/tts-suite/config.json
if [ ! -e "$CONFIG_FILE" ]; then
    python3 - "$REPO" "$CONFIG_FILE" <<'PY'
import json, os, sys
repo, target = sys.argv[1:]
os.makedirs(os.path.dirname(target), exist_ok=True)
config = json.load(open(f"{repo}/deploy/config.example.json", encoding="utf-8"))
config["voices_dir"] = f"{repo}/voices"
config["hosts"] = []
json.dump(config, open(target, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
    chmod 600 "$CONFIG_FILE"
fi
bash "$REPO/deploy/install-engines.sh"
echo "Installed $REPO to $VENV."
echo "Next: systemctl --user enable --now tts-suite   (or, for AIfred: sudo $REPO/deploy/switch-to-system-unit.sh)"
