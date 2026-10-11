"""Observing the VRAM use of a running GPU engine — the second half of the budget (the first half is
the limit the container gets at its start).

Ported from AIfred Intelligence (``local_tts_gpu_footprint``): ``nvidia-smi`` reports the host PIDs of
the processes holding card memory, ``docker top`` lists the host PIDs of a container; their
intersection is exactly the engine's container, not the LLM or another worker on the same card.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .constants import DOCKER_PIDS_TIMEOUT_S, NVIDIA_SMI_APPS_TIMEOUT_S, VRAM_CHECK_MIN_INTERVAL_S
from .engines.base import TTSEngine

logger = logging.getLogger(__name__)

NVIDIA_SMI_APP_FIELDS = "pid,used_memory,gpu_uuid"


@dataclass(frozen=True)
class ComputeApp:
    """One process holding memory on a card (``pid`` is the host PID)."""

    pid: str
    used_mib: int
    gpu_uuid: str


#: ``engine -> {gpu_uuid: MiB}`` — the seam tests replace.
VramMeasure = Callable[[TTSEngine], dict[str, int]]


def parse_compute_apps(output: str) -> list[ComputeApp]:
    """The lines of ``nvidia-smi --query-compute-apps=pid,used_memory,gpu_uuid --format=csv,noheader,nounits``."""
    apps = []
    for line in output.strip().splitlines():
        pid, used, gpu_uuid = (value.strip() for value in line.split(","))
        apps.append(ComputeApp(pid, int(used), gpu_uuid))
    return apps


def compute_apps() -> list[ComputeApp]:
    """The processes holding card memory; empty when nothing runs or ``nvidia-smi`` is unavailable
    (a warning in the log says which)."""
    command = ["nvidia-smi", f"--query-compute-apps={NVIDIA_SMI_APP_FIELDS}", "--format=csv,noheader,nounits"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=NVIDIA_SMI_APPS_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("nvidia-smi unavailable: %s", exc)
        return []
    if result.returncode != 0:
        logger.warning("nvidia-smi failed: %s", result.stderr.strip()[:200])
        return []
    return parse_compute_apps(result.stdout)


def container_host_pids(compose_file: str) -> set[str]:
    """Host PIDs of the processes inside the running container(s) of a compose project."""
    ids = subprocess.run(
        ["docker", "compose", "-f", compose_file, "ps", "-q"],
        capture_output=True, text=True, timeout=DOCKER_PIDS_TIMEOUT_S, check=False,
    ).stdout.split()
    pids: set[str] = set()
    for container_id in ids:
        top = subprocess.run(
            ["docker", "top", container_id, "-eo", "pid"],
            capture_output=True, text=True, timeout=DOCKER_PIDS_TIMEOUT_S, check=False,
        ).stdout.split()
        pids.update(pid for pid in top[1:] if pid.isdigit())
    return pids


def engine_vram_mib(engine: TTSEngine) -> dict[str, int]:
    """VRAM (MiB) of a local engine's container per card: ``{gpu_uuid: MiB}``; empty when the
    container holds no card memory (or is not there)."""
    if engine.docker_compose_path is None:
        return {}
    apps = compute_apps()
    if not apps:
        return {}
    pids = container_host_pids(str(engine.docker_compose_path))
    per_card: dict[str, int] = {}
    for app in apps:
        if app.pid in pids:
            per_card[app.gpu_uuid] = per_card.get(app.gpu_uuid, 0) + app.used_mib
    return per_card


class VramGuard:
    """Measures an engine's VRAM use, but not more often than ``min_interval_s`` per engine."""

    def __init__(
        self,
        measure: VramMeasure = engine_vram_mib,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = VRAM_CHECK_MIN_INTERVAL_S,
    ) -> None:
        self._measure = measure
        self._clock = clock
        self._min_interval_s = min_interval_s
        self._last_checked: dict[str, float] = {}

    def used_mib(self, engine: TTSEngine) -> int | None:
        """The engine's use in MiB over all cards, or ``None`` when it was measured too recently.
        Blocking (``nvidia-smi``, ``docker``): run it in a thread."""
        now = self._clock()
        last = self._last_checked.get(engine.key)
        if last is not None and now - last < self._min_interval_s:
            return None
        self._last_checked[engine.key] = now
        return sum(self._measure(engine).values())
