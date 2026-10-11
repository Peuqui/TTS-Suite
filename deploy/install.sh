#!/bin/bash
# Install the TTS-Suite service for the current user: its own venv with the code of this clone
# (editable: the compose files and scripts of the engines live in the clone and are found from the
# code), the command `tts-suite` in ~/.local/bin, and a systemd user service that runs it.
# The service starts on its first run only with a configuration: name the shared voices folder once
# with `tts-suite serve --voices-dir <folder>` (it is kept in ~/.config/tts-suite/config.json).
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
echo "Installed $REPO to $VENV."
echo "Next: tts-suite token; tts-suite serve --voices-dir <folder> once to write the configuration; then"
echo "systemctl --user enable --now tts-suite"
