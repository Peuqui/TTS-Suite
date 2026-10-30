"""Card choice for a local GPU engine: the rules of ``SuiteConfig`` applied to what ``nvidia-smi`` says.

Ported from AIfred Intelligence (``place_local_gpu_engine``), with the rules in the configuration
instead of a callback: ``gpu_mode`` (``auto`` = a card nobody uses first, then the one with the most free VRAM; ``list`` = only the cards of
``gpus``: order = preference, ``reserve_mib`` stays free for other programs),
``headroom_mib`` (margin on top of the budget) and the budget of the engine.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from dataclasses import dataclass

from .api import SuiteConfig
from .constants import GPU_ORC_LOADING_TIMEOUT_S, NVIDIA_SMI_QUERY_TIMEOUT_S
from .engines.base import TTSEngine

NVIDIA_SMI_GPU_FIELDS = "index,uuid,memory.free,memory.total,memory.used"


class PlacementRefused(Exception):
    """No card can take the engine right now; the message says why."""


@dataclass(frozen=True)
class GpuInfo:
    index: int
    uuid: str
    free_mib: int
    total_mib: int
    used_mib: int

    @property
    def is_unused(self) -> bool:
        """No program holds memory on the card (not even a CUDA context)."""
        return self.used_mib == 0


@dataclass(frozen=True)
class Placement:
    """Where a GPU engine goes: the card, and the budget it is started with (``None``: no limit, a burn-in)."""

    gpu: GpuInfo
    budget_mib: int | None


def parse_gpus(output: str) -> list[GpuInfo]:
    """The ``nvidia-smi --query-gpu=index,uuid,memory.free,memory.total,memory.used --format=csv,noheader,nounits`` lines."""
    cards = []
    for line in output.strip().splitlines():
        index, uuid, free, total, used = (value.strip() for value in line.split(","))
        cards.append(GpuInfo(int(index), uuid, int(free), int(total), int(used)))
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


def vram_needed_mib(engine: TTSEngine, config: SuiteConfig) -> int | None:
    """The free VRAM a start needs on a card: the budget plus ``headroom_mib``. The one place the
    margin is added; the placement and the catalog's ``needed_mib`` both read it. ``None`` = no budget."""
    budget = vram_budget_mib(engine, config)
    return None if budget is None else budget + config.headroom_mib


def refuse_while_a_model_loads(url: str | None) -> None:
    """GPU-Orc's traffic light before a start. :class:`PlacementRefused` while a big model loads, and when
    GPU-Orc gives no usable answer: nobody can then say that the cards really are as empty as they look."""
    if url is None:
        return
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/loading", timeout=GPU_ORC_LOADING_TIMEOUT_S) as response:
            loading = json.load(response)["loading"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PlacementRefused(f"GPU-Orc at {url} gave no usable answer: {exc!r}") from exc
    if loading:
        raise PlacementRefused(f"{', '.join(loading)} is loading")


def _candidates(cards: list[GpuInfo], config: SuiteConfig) -> list[tuple[GpuInfo, int]]:
    """The cards in the order they are tried, each with the VRAM left after its reserve."""
    if config.gpu_mode == "auto":
        # A card nobody uses first (the engines of this suite and the STT suite then spread over the
        # free cards instead of piling onto the one with the most room), then the most free VRAM.
        ordered = sorted(cards, key=lambda card: (not card.is_unused, -card.free_mib, card.index))
        return [(card, card.free_mib) for card in ordered]
    candidates = []
    for rule in config.gpus:
        card = next((card for card in cards if rule.gpu in (str(card.index), card.uuid)), None)
        if card is not None:  # a listed card that is not there (not plugged in) is simply not an option
            candidates.append((card, card.free_mib - rule.reserve_mib))
    return candidates


def _named_card(cards: list[GpuInfo], gpu: str) -> list[tuple[GpuInfo, int]]:
    """The one card a start asks for by index or UUID, with all its free VRAM."""
    card = next((card for card in cards if gpu in (str(card.index), card.uuid)), None)
    if card is None:
        raise PlacementRefused(f"GPU {gpu} is not present")
    return [(card, card.free_mib)]


def choose_gpu(cards: list[GpuInfo], engine: TTSEngine, config: SuiteConfig, gpu: str | None = None) -> Placement:
    """The first card (in the order of ``config.gpus`` in list mode, else a card nobody uses first, then the most free VRAM)
    whose free VRAM minus reserve covers the budget plus headroom; with ``gpu`` (index or UUID) only that card,
    outside the rules of ``gpu_mode``. :class:`PlacementRefused` when none does or the budget is unknown."""
    budget = vram_budget_mib(engine, config)
    needed = vram_needed_mib(engine, config)
    if budget is None or needed is None:
        raise PlacementRefused(f"no VRAM budget for {engine.key} — set engines[{engine.key}].vram_budget_mib")
    candidates = _candidates(cards, config) if gpu is None else _named_card(cards, gpu)
    if not candidates:
        raise PlacementRefused("none of the configured GPUs is present")
    for card, available in candidates:
        if available >= needed:
            return Placement(card, budget)
    card, available = max(candidates, key=lambda candidate: candidate[1])
    raise PlacementRefused(f"needs {needed} MiB, most free {available} MiB on GPU {card.index}")


def choose_gpu_for_burn_in(cards: list[GpuInfo], config: SuiteConfig) -> Placement:
    """The card of a burn-in: the first one of the usual order that nobody uses, without a limit. The
    limit would cap what is measured, and without it the engine may grow past its budget, so it shares
    the card with no one. :class:`PlacementRefused` when every card is in use."""
    unused = [card for card, _ in _candidates(cards, config) if card.is_unused]
    if not unused:
        raise PlacementRefused("a burn-in needs a GPU nobody uses, every GPU is in use")
    return Placement(unused[0], None)
