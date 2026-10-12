#!/usr/bin/env bash
# Control of the TTS containers on a host, called by the TTS-Suite of ANOTHER machine over SSH.
#
# The suite uses its own key, pinned to exactly this script in ~/.ssh/authorized_keys of the host:
#
#   command="/path/to/TTS-Suite/scripts/tts-host-ctl.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... tts-suite-host
#
# The command then arrives in SSH_ORIGINAL_COMMAND (or directly in "$@"):
#
#   start <service> <voices_dir>  start the container of docker/tts/<service> (up -d); <voices_dir> is the
#                     shared voices folder on THIS host (the suite's voices_dir), mounted into the container
#   stop [<service>]  stop this one / all TTS containers of this repository (VRAM free)
#   status            list the running TTS containers
#
# <service> is a folder below docker/tts/ with a docker-compose.yml (for example xtts, qwen3-tts).
# The card and the idle time are set in the .env next to the compose file (TTS_GPU_UUID, *_KEEP_ALIVE).
set -euo pipefail

TTS_DIR="$(cd "$(dirname "$(readlink -f "$0")")/../docker/tts" && pwd)"

if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
    read -r -a args <<< "$SSH_ORIGINAL_COMMAND"
else
    args=("$@")
fi

action="${args[0]:-}"
service="${args[1]:-}"
voices_dir="${args[2]:-}"

compose_files() {
    find "$TTS_DIR" -mindepth 2 -maxdepth 2 -name docker-compose.yml | sort
}

require_service() {
    if [[ ! "$service" =~ ^[a-z0-9-]+$ || ! -f "$TTS_DIR/$service/docker-compose.yml" ]]; then
        echo "unknown TTS service: '$service'" >&2
        exit 2
    fi
}

case "$action" in
    start)
        require_service
        if [[ ! "$voices_dir" =~ ^/[A-Za-z0-9._/-]+$ || ! -d "$voices_dir" ]]; then
            echo "voices folder missing on this host: '$voices_dir'" >&2
            exit 2
        fi
        export TTS_VOICES_DIR="$voices_dir"
        docker compose -f "$TTS_DIR/$service/docker-compose.yml" up -d
        ;;
    stop)
        if [[ -n "$service" ]]; then
            require_service
            docker compose -f "$TTS_DIR/$service/docker-compose.yml" stop
        else
            while read -r compose; do
                docker compose -f "$compose" stop
            done < <(compose_files)
        fi
        ;;
    status)
        while read -r compose; do
            docker compose -f "$compose" ps --status running --format '{{.Service}}'
        done < <(compose_files)
        ;;
    *)
        echo "usage: tts-host-ctl.sh start <service> <voices_dir> | stop [<service>] | status" >&2
        exit 2
        ;;
esac
