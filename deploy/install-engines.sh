#!/bin/bash
# Provision the engines that need more than the Python package: the Piper voice, eSpeak, and the
# Docker images of the GPU engines (Qwen3-TTS, XTTS). Edge (cloud) and the browser voice need nothing.
# Called by install.sh; run it again later to add an engine. Every question can be answered in advance
# with its environment variable (unattended install); without a terminal and without the variable the
# default applies.
#
#   TTS_SUITE_INSTALL_PIPER   y/n  download the German Piper voices Thorsten and Ramona (about 114 + 63 MB)  default y
#   TTS_SUITE_INSTALL_ESPEAK  y/n  install eSpeak (package espeak-ng, needs sudo)           default y
#   TTS_SUITE_INSTALL_QWEN3   y/n  build the Qwen3-TTS image (NVIDIA GPU, large; the model downloads on first use)  default n
#   TTS_SUITE_INSTALL_XTTS    y/n  build the XTTS image (NVIDIA GPU, large; the model downloads on first use)       default n
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
DATA=${TTS_SUITE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/tts-suite}
PIPER_VOICES_URL=https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE
# <path below PIPER_VOICES_URL>/<model file>, one line per voice (the names the engine knows are in engines/piper/engine.py)
PIPER_VOICES=(thorsten/high/de_DE-thorsten-high.onnx ramona/low/de_DE-ramona-low.onnx)
CONFIG_FILE=${XDG_CONFIG_HOME:-$HOME/.config}/tts-suite/config.json
VOICES_DIR=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["voices_dir"])' "$CONFIG_FILE")

# ask <ENV_VAR> <prompt> <default y|n>: the answer ends up in REPLY (y or n).
ask() {
    local var="$1" prompt="$2" default="$3"
    if [ -n "${!var+x}" ]; then
        REPLY="${!var}"
        echo "$prompt$REPLY   (from $var)"
    elif [ ! -t 0 ]; then
        REPLY="$default"
        echo "$prompt  (default $default: no terminal, $var not set)"
    else
        read -r -p "$prompt" -n 1 REPLY
        echo ""
        [ -n "$REPLY" ] || REPLY="$default"
    fi
    [[ $REPLY =~ ^[JjYy]$ ]] && REPLY=y || REPLY=n
}

install_piper_voices() {
    local target="$DATA/piper_models" voice file
    mkdir -p "$target"
    for voice in "${PIPER_VOICES[@]}"; do
        file=$(basename "$voice")
        if [ -f "$target/$file" ]; then
            echo "Piper voice already there: $target/$file"
            continue
        fi
        for part in "$file" "$file.json"; do
            curl -fL --retry 3 -o "$target/$part.part" "$PIPER_VOICES_URL/$(dirname "$voice")/$part"
            mv "$target/$part.part" "$target/$part"
        done
        echo "Piper voice installed: $target/$file"
    done
}

install_espeak() {
    if command -v espeak-ng >/dev/null; then
        echo "eSpeak already installed."
    elif command -v apt-get >/dev/null; then
        sudo apt-get install -y espeak-ng
    elif command -v dnf >/dev/null; then
        sudo dnf install -y espeak-ng
    elif command -v pacman >/dev/null; then
        sudo pacman -S --noconfirm espeak-ng
    else
        echo "No known package manager: install the package espeak-ng by hand." >&2
        return 1
    fi
}

# build_image <engine directory under docker/tts>
build_image() {
    (cd "$REPO/docker/tts/$1" && TTS_VOICES_DIR="$VOICES_DIR" docker compose build)
}

ask TTS_SUITE_INSTALL_PIPER "Download the Piper voices Thorsten and Ramona (German, local, no GPU, about 180 MB)? (Y/n): " y
[ "$REPLY" = y ] && install_piper_voices

ask TTS_SUITE_INSTALL_ESPEAK "Install eSpeak (robotic offline voice, package espeak-ng, needs sudo)? (Y/n): " y
[ "$REPLY" = y ] && install_espeak

if ! command -v nvidia-smi >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "No NVIDIA GPU or no Docker Compose here: Qwen3-TTS and XTTS are skipped (Edge and the browser voice still work)."
    exit 0
fi
ask TTS_SUITE_INSTALL_QWEN3 "Build the Qwen3-TTS image (voice cloning, ~5-7 GB VRAM, large build, the model downloads on first use)? (y/N): " n
[ "$REPLY" = y ] && build_image qwen3-tts
ask TTS_SUITE_INSTALL_XTTS "Build the XTTS image (voice cloning, ~2 GB VRAM, large build, the model downloads on first use)? (y/N): " n
[ "$REPLY" = y ] && build_image xtts
exit 0
