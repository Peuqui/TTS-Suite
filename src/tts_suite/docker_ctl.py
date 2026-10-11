"""Docker access of the engines: start and stop a container engine through its compose file."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ComposeAction = Literal["up", "down"]

#: Environment variable the compose files read to pin the container to a card.
GPU_UUID_ENV = "TTS_GPU_UUID"


def docker_compose_action(
    compose_file: Path,
    action: ComposeAction,
    service_label: str,
    gpu_uuid: str = "",
) -> tuple[bool, str]:
    """Run ``docker compose up -d`` / ``down`` on a compose file.

    ``gpu_uuid``: card a container starts on (``CUDA_VISIBLE_DEVICES=GPU-…`` in the compose
    files uses the UUID form, which survives PCI re-enumeration). Empty = the value from
    the ``.env`` next to the compose file stays valid. Returns ``(success, message)``.
    """
    if not compose_file.exists():
        return False, f"docker-compose.yml not found: {compose_file}"

    command = ["docker", "compose", "-f", str(compose_file)]
    command += ["up", "-d"] if action == "up" else ["down"]

    # An empty variable would override the .env value, so it is only set for a known card.
    environment = os.environ.copy()
    if action == "up" and gpu_uuid:
        environment[GPU_UUID_ENV] = gpu_uuid
    result = subprocess.run(
        command, capture_output=True, text=True, cwd=str(compose_file.parent), env=environment,
    )
    if result.returncode != 0:
        return False, f"docker compose {action} failed: {result.stderr}"
    verb = "started" if action == "up" else "stopped"
    logger.info("%s container %s", service_label, verb)
    return True, f"{service_label} container {verb}"
