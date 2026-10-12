"""Card choice for a local GPU engine: the rules of ``SuiteConfig`` applied to what ``nvidia-smi`` says.

Ported from AIfred Intelligence (``place_local_gpu_engine``), with the rules in the configuration
instead of a callback: ``gpu_mode`` (``auto`` = best fit over every card, ``list`` = only the cards of
``gpus``: order = preference, ``reserve_mib`` stays free for other programs),
``headroom_mib`` (margin on top of the budget) and the budget of the engine.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .api import SuiteConfig
from .constants import NVIDIA_SMI_QUERY_TIMEOUT_S
from .engines.base import TTSEngine

NVIDIA_SMI_GPU_FIELDS = "index,uuid,memory.free,memory.total"


class PlacementRefused(Exception):
    """No card can take the engine right now; the message says why."""


@dataclass(frozen=True)
class GpuInfo:
    index: int
    uuid: str
    free_mib: int
    total_mib: int


@dataclass(frozen=True)
class Placement:
    """Where a GPU engine goes: the card, and the budget it is started with."""

    gpu: GpuInfo
    budget_mib: int


def parse_gpus(output: str) -> list[GpuInfo]:
    """The ``nvidia-smi --query-gpu=index,uuid,memory.free,memory.total --format=csv,noheader,nounits`` lines."""
    cards = []
    for line in output.strip().splitlines():
        index, uuid, free, total = (value.strip() for value in line.split(","))
        cards.append(GpuInfo(int(index), uuid, int(free), int(total)))
    return cards


def query_gpus() -> list[GpuInfo]:
    """The cards of this machine with their free VRAM. :class:`PlacementRefused` when ``nvidia-smi``
    does not answer or lists no card."""
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


def vram_budget_mib(engine: TTSEngine, config: SuiteConfig) -> int | None:
    """The VRAM an engine may use: the configured budget, else the engine's own (burn-in) value;
    ``None`` when neither exists."""
    configured = config.engines.get(engine.key)
    if configured is not None and configured.vram_budget_mib is not None:
        return configured.vram_budget_mib
    return engine.default_vram_budget_mib


def _candidates(cards: list[GpuInfo], config: SuiteConfig) -> list[tuple[GpuInfo, int]]:
    """The cards in the order they are tried, each with the VRAM left after its reserve."""
    if config.gpu_mode == "auto":
        return [(card, card.free_mib) for card in sorted(cards, key=lambda card: -card.free_mib)]
    candidates = []
    for rule in config.gpus:
        card = next((card for card in cards if rule.gpu in (str(card.index), card.uuid)), None)
        if card is not None:  # a listed card that is not there (not plugged in) is simply not an option
            candidates.append((card, card.free_mib - rule.reserve_mib))
    return candidates


def choose_gpu(cards: list[GpuInfo], engine: TTSEngine, config: SuiteConfig) -> Placement:
    """The first card (in the order of ``config.gpus`` in list mode, else the one with the most free VRAM first)
    whose free VRAM minus reserve covers the budget plus headroom. :class:`PlacementRefused` when
    none does or the budget is unknown."""
    budget = vram_budget_mib(engine, config)
    if budget is None:
        raise PlacementRefused(f"no VRAM budget for {engine.key} — set engines[{engine.key}].vram_budget_mib")
    needed = budget + config.headroom_mib
    candidates = _candidates(cards, config)
    if not candidates:
        raise PlacementRefused("none of the configured GPUs is present")
    for card, available in candidates:
        if available >= needed:
            return Placement(card, budget)
    card, available = max(candidates, key=lambda candidate: candidate[1])
    raise PlacementRefused(f"needs {needed} MiB, most free {available} MiB on GPU {card.index}")
