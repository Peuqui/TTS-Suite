"""Docker access of the engines: start and stop a container engine through its compose file, ask for its state."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

from .api import SuiteError
from .constants import (
    DOCKER_COMPOSE_TIMEOUT_S,
    DOCKER_INSPECT_TIMEOUT_S,
    DOCKER_LOG_TAIL_LINES,
    DOCKER_LOG_TAIL_MAX_CHARS,
)

logger = logging.getLogger(__name__)

ComposeAction = Literal["up", "down"]
#: What a container is: Docker's own states, or ``absent`` when there is no such container.
ContainerState = Literal["running", "created", "restarting", "exited", "absent"]
#: The Docker states the suite tells apart (anything else — paused, dead, removing — is unexpected).
DOCKER_CONTAINER_STATES: dict[str, ContainerState] = {
    "running": "running", "created": "created", "restarting": "restarting", "exited": "exited",
}
#: What Docker says (stderr) for a container that does not exist.
NO_SUCH_CONTAINER = "No such container"

#: Environment variable the compose files read to pin the container to a card.
GPU_UUID_ENV = "TTS_GPU_UUID"
#: Environment variable the compose files pass on to ``sitecustomize.py`` (``docker/tts/_vram_limit``),
#: which caps the PyTorch memory of the container at this many MiB.
VRAM_BUDGET_ENV = "TTS_VRAM_BUDGET_MIB"
#: Environment variable the compose files read for the shared voices tree (user data, one place by setting).
VOICES_DIR_ENV = "TTS_VOICES_DIR"


def docker_compose_action(
    compose_file: Path,
    action: ComposeAction,
    service_label: str,
    *,
    voices_dir: Path,
    gpu_uuid: str = "",
    vram_budget_mib: int | None = None,
) -> tuple[bool, str]:
    """Run ``docker compose up -d`` / ``down`` on a compose file.

    ``voices_dir``: the shared voices tree the compose files mount (needed by ``down`` as well, which
    reads the file). ``gpu_uuid``: card a container starts on (``CUDA_VISIBLE_DEVICES=GPU-…`` in the compose
    files uses the UUID form, which survives PCI re-enumeration). Empty = the value from
    the ``.env`` next to the compose file stays valid. ``vram_budget_mib``: the limit the container
    gets for its PyTorch memory; ``None`` = no limit. Returns ``(success, message)``.
    """
    if not compose_file.exists():
        return False, f"docker-compose.yml not found: {compose_file}"

    command = ["docker", "compose", "-f", str(compose_file)]
    command += ["up", "-d"] if action == "up" else ["down"]

    # An empty variable would override the .env value, so it is only set for a known card.
    environment = os.environ.copy()
    environment[VOICES_DIR_ENV] = str(voices_dir)
    if action == "up" and gpu_uuid:
        environment[GPU_UUID_ENV] = gpu_uuid
    if action == "up" and vram_budget_mib is not None:
        environment[VRAM_BUDGET_ENV] = str(vram_budget_mib)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, cwd=str(compose_file.parent), env=environment,
            timeout=DOCKER_COMPOSE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"docker compose {action} timed out"
    if result.returncode != 0:
        return False, f"docker compose {action} failed: {result.stderr}"
    verb = "started" if action == "up" else "stopped"
    logger.info("%s container %s", service_label, verb)
    return True, f"{service_label} container {verb}"


class ContainerStateUnreadable(SuiteError):
    """Docker did not answer, or answered something the suite cannot read. Not the same as "absent":
    the caller decides what a blind answer means."""


def container_state(container_name: str) -> ContainerState:
    """The state of a container (``docker inspect``); ``absent`` when there is no such container.
    :class:`ContainerStateUnreadable` when Docker does not answer otherwise."""
    command = ["docker", "inspect", "--type", "container", "-f", "{{.State.Status}}", container_name]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=DOCKER_INSPECT_TIMEOUT_S, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise ContainerStateUnreadable(f"docker unavailable: {exc}") from exc
    if result.returncode != 0:
        if NO_SUCH_CONTAINER in result.stderr:
            return "absent"
        raise ContainerStateUnreadable(f"docker inspect {container_name} failed: {result.stderr.strip()[:200]}")
    status = result.stdout.strip()
    state = DOCKER_CONTAINER_STATES.get(status)
    if state is None:
        raise ContainerStateUnreadable(f"container {container_name} is in an unexpected state: {status!r}")
    return state


def container_log_tail(container_name: str) -> str:
    """The last lines a container wrote (stdout and stderr, one line), to say why it ended. Only
    explains an error that is reported anyway, so a failing ``docker logs`` gives ``""``."""
    command = ["docker", "logs", "--tail", str(DOCKER_LOG_TAIL_LINES), container_name]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=DOCKER_INSPECT_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if result.returncode != 0:
        return ""
    lines = (result.stdout + result.stderr).strip().splitlines()
    return " | ".join(line.strip() for line in lines)[-DOCKER_LOG_TAIL_MAX_CHARS:]
