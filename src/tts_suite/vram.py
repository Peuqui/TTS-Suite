"""Reporting the VRAM use of a running GPU engine (display only — nothing is stopped for it).

Ported from AIfred Intelligence (``local_tts_gpu_footprint``): ``nvidia-smi`` reports the host PIDs of
the processes holding card memory, ``docker top`` lists the host PIDs of a container; their
intersection is exactly the engine's container, not the LLM or another worker on the same card.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from .constants import DOCKER_PIDS_TIMEOUT_S, NVIDIA_SMI_APPS_TIMEOUT_S
from .docker_ctl import NO_SUCH_CONTAINER
from .engines.base import TTSEngine

logger = logging.getLogger(__name__)

NVIDIA_SMI_APP_FIELDS = "pid,used_memory,gpu_uuid"
#: What ``docker top`` says (stderr) for a container that does not run (a missing one: ``NO_SUCH_CONTAINER``).
NOT_RUNNING = "is not running"


class VramMeasurementFailed(Exception):
    """The VRAM use could not be measured (``nvidia-smi`` or Docker did not answer, or answered
    something unreadable). Not the same as "0 MiB": the caller decides what a blind report means."""


@dataclass(frozen=True)
class ComputeApp:
    """One process holding memory on a card (``pid`` is the host PID). ``used_mib`` is ``None`` when
    the driver does not report the process's memory (``[N/A]``, ``[Not Supported]``)."""

    pid: str
    used_mib: int | None
    gpu_uuid: str


def _is_not_reported(field: str) -> bool:
    """``[N/A]``, ``[Not Supported]``: how ``nvidia-smi`` says a value is not available."""
    return field.startswith("[") and field.endswith("]")


def parse_compute_apps(output: str) -> list[ComputeApp]:
    """The lines of ``nvidia-smi --query-compute-apps=pid,used_memory,gpu_uuid --format=csv,noheader,nounits``.
    A memory field the driver does not report becomes ``None``; any other unreadable line raises
    :class:`VramMeasurementFailed`."""
    apps = []
    for line in output.strip().splitlines():
        fields = [value.strip() for value in line.split(",")]
        if len(fields) != 3:
            raise VramMeasurementFailed(f"unreadable nvidia-smi line: {line!r}")
        pid, used, gpu_uuid = fields
        if _is_not_reported(used):
            apps.append(ComputeApp(pid, None, gpu_uuid))
        elif used.isdigit():
            apps.append(ComputeApp(pid, int(used), gpu_uuid))
        else:
            raise VramMeasurementFailed(f"unreadable memory in nvidia-smi line: {line!r}")
    return apps


def _run_measuring(command: list[str], timeout_s: float) -> str:
    """The output of a command the measurement needs; :class:`VramMeasurementFailed` when it does not run or fails."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise VramMeasurementFailed(f"{command[0]} unavailable: {exc}") from exc
    if result.returncode != 0:
        raise VramMeasurementFailed(f"{' '.join(command[:3])} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def compute_apps() -> list[ComputeApp]:
    """The processes holding card memory (empty when nothing runs); :class:`VramMeasurementFailed`
    when ``nvidia-smi`` is unavailable or fails."""
    command = ["nvidia-smi", f"--query-compute-apps={NVIDIA_SMI_APP_FIELDS}", "--format=csv,noheader,nounits"]
    return parse_compute_apps(_run_measuring(command, NVIDIA_SMI_APPS_TIMEOUT_S))


def container_host_pids(container_name: str) -> set[str]:
    """Host PIDs of the processes inside a container (``docker top``). Compose is not involved: it would
    need the variables of the compose files (card, voices). A container that is not there has no
    processes; :class:`VramMeasurementFailed` when Docker does not answer otherwise."""
    command = ["docker", "top", container_name, "-eo", "pid"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=DOCKER_PIDS_TIMEOUT_S, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise VramMeasurementFailed(f"docker unavailable: {exc}") from exc
    if result.returncode != 0:
        if NO_SUCH_CONTAINER in result.stderr or NOT_RUNNING in result.stderr:
            return set()
        raise VramMeasurementFailed(f"docker top {container_name} failed: {result.stderr.strip()[:200]}")
    return {pid for pid in result.stdout.split()[1:] if pid.isdigit()}


def engine_vram_mib(engine: TTSEngine) -> dict[str, int]:
    """VRAM (MiB) of a local engine's container per card: ``{gpu_uuid: MiB}``; empty when the
    container holds no card memory (or is not there). :class:`VramMeasurementFailed` when it cannot
    be told, also when the driver does not report the memory of the container's own process."""
    if engine.docker_compose_path is None:
        return {}
    apps = compute_apps()
    if not apps:
        return {}
    pids = container_host_pids(engine.service_dir)
    per_card: dict[str, int] = {}
    for app in apps:
        if app.pid in pids:
            if app.used_mib is None:
                raise VramMeasurementFailed(f"nvidia-smi reports no memory for process {app.pid} of {engine.key}")
            per_card[app.gpu_uuid] = per_card.get(app.gpu_uuid, 0) + app.used_mib
    return per_card
