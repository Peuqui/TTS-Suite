"""Where a local GPU engine starts: the GPU-Orc of this machine decides (``POST /place``), the suite starts the
engine on the card it names.

GPU-Orc counts what every program may take on a card (a vLLM model its share, an engine its budget), keeps a
margin on top and promises the room for a while, so two programs that start together never get the same room.
The suite only knows the cards by index and UUID (``nvidia-smi``), to read which ones a container uses.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .api import GpuNeedSource, SuiteConfig
from .constants import GPU_ORC_HEALTH_TIMEOUT_S, NVIDIA_SMI_QUERY_TIMEOUT_S
from .engines.base import TTSEngine

NVIDIA_SMI_GPU_FIELDS = "index,uuid"


class PlacementRefused(Exception):
    """No card can take the engine right now; the message says why."""


@dataclass(frozen=True)
class GpuInfo:
    index: int
    uuid: str


@dataclass(frozen=True)
class PlaceRequest:
    """What the suite asks GPU-Orc before it starts an engine on a card."""

    consumer: str  # the suite as GPU-Orc's configuration names it
    item: str  # the engine as ``/gpu/holders`` names it
    need_mib: int  # its budget, without margin (GPU-Orc adds its own)
    reason: str
    card: str | None = None  # only this card (index or UUID)


def parse_gpus(output: str) -> list[GpuInfo]:
    """The ``nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits`` lines."""
    cards = []
    for line in output.strip().splitlines():
        index, uuid = (value.strip() for value in line.split(","))
        cards.append(GpuInfo(int(index), uuid))
    return cards


def query_gpus() -> list[GpuInfo]:
    """The cards of this machine. :class:`PlacementRefused` when ``nvidia-smi`` does not answer or lists no card."""
    command = ["nvidia-smi", f"--query-gpu={NVIDIA_SMI_GPU_FIELDS}", "--format=csv,noheader,nounits"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=NVIDIA_SMI_QUERY_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise PlacementRefused(f"nvidia-smi unavailable: {exc}") from exc
    if result.returncode != 0:
        raise PlacementRefused(f"nvidia-smi failed: {result.stderr.strip()[:200]}")
    cards = parse_gpus(result.stdout)
    if not cards:
        raise PlacementRefused("nvidia-smi lists no GPUs")
    return cards


def place_with_gpu_orc(url: str, request: PlaceRequest) -> GpuInfo:
    """The card GPU-Orc gives the engine; its need is promised there until the engine shows in ``/gpu/holders``.
    Waits as long as GPU-Orc says a caller should (``caller_timeout_s``: making room may take that long).
    :class:`PlacementRefused` with GPU-Orc's reason when no card has room, or when GPU-Orc gives no usable answer."""
    base = url.rstrip("/")
    body: dict[str, Any] = {
        "consumer": request.consumer, "item": request.item, "need_mib": request.need_mib, "reason": request.reason,
    }
    if request.card is not None:
        body["card"] = int(request.card) if request.card.isdigit() else request.card
    try:
        health = _call(urllib.request.Request(f"{base}/health"), GPU_ORC_HEALTH_TIMEOUT_S)
        post = urllib.request.Request(
            f"{base}/place", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST",
        )
        answer = _call(post, float(health["caller_timeout_s"]))
        card = answer["card"]
        placed = None if card is None else GpuInfo(int(card["index"]), str(card["uuid"]))
        why = answer["why"]
    except urllib.error.HTTPError as error:
        raise PlacementRefused(f"GPU-Orc at {url} refused: {error.code} {_error_text(error)}") from error
    except OSError as error:
        raise PlacementRefused(f"GPU-Orc at {url} did not answer: {error}") from error
    except (ValueError, KeyError, TypeError) as error:
        raise PlacementRefused(f"GPU-Orc at {url} answered in an unknown form: {error!r}") from error
    if placed is None:
        raise PlacementRefused(f"GPU-Orc: {why}")
    return placed


def _call(request: urllib.request.Request, timeout_s: float) -> Any:
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)


def _error_text(error: urllib.error.HTTPError) -> str:
    """GPU-Orc's ``{"error": ...}``, else the raw body."""
    raw = error.read().decode(errors="replace")
    try:
        return str(json.loads(raw)["error"])
    except (ValueError, KeyError, TypeError):
        return raw[:200]


def vram_budget(engine: TTSEngine, config: SuiteConfig) -> tuple[int | None, GpuNeedSource]:
    """The VRAM an engine may use and where the number comes from: the configured budget, else the engine's
    own (burn-in) value; ``None`` when neither exists."""
    configured = config.engines.get(engine.key)
    if configured is not None and configured.vram_budget_mib is not None:
        return configured.vram_budget_mib, "configured"
    if engine.default_vram_budget_mib is not None:
        return engine.default_vram_budget_mib, "estimate"
    return None, "unknown"


def vram_budget_mib(engine: TTSEngine, config: SuiteConfig) -> int | None:
    """The VRAM an engine may use (see :func:`vram_budget`)."""
    return vram_budget(engine, config)[0]
