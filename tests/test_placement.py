"""Card choice: order, reserve, headroom, most free VRAM, unknown budget, nothing fits."""
from __future__ import annotations

from pathlib import Path

import pytest

from container_fakes import ContainerEngine

from tts_suite.api import EngineDefaults, GpuRule, SuiteConfig
from tts_suite.placement import GpuInfo, PlacementRefused, choose_gpu, parse_gpus, vram_budget_mib

CARDS = [GpuInfo(0, "GPU-a", 5000, 24000), GpuInfo(1, "GPU-b", 9000, 24000), GpuInfo(2, "GPU-c", 7000, 24000)]


def _config(tmp_path: Path, *rules: GpuRule, headroom_mib: int = 500, **engines: EngineDefaults) -> SuiteConfig:
    return SuiteConfig(voices_dir=tmp_path, gpus=rules, headroom_mib=headroom_mib, engines=engines)


def test_the_nvidia_smi_lines_are_read() -> None:
    assert parse_gpus("0, GPU-a, 5000, 24000\n1, GPU-b, 9000, 24000\n") == CARDS[:2]


def test_the_first_fitting_card_in_the_order_of_the_list_wins(tmp_path: Path) -> None:
    config = _config(tmp_path, GpuRule("2"), GpuRule("GPU-b"), GpuRule("0"))
    placement = choose_gpu(CARDS, ContainerEngine("x"), config)  # budget 4000 + 500 headroom
    assert (placement.gpu.uuid, placement.budget_mib) == ("GPU-c", 4000)


def test_a_card_is_skipped_when_free_vram_minus_reserve_does_not_cover_budget_and_headroom(tmp_path: Path) -> None:
    config = _config(tmp_path, GpuRule("2", reserve_mib=2600), GpuRule("1", reserve_mib=4400))
    assert choose_gpu(CARDS, ContainerEngine("x"), config).gpu.index == 1  # card 2: 7000-2600=4400 < 4500; card 1: 9000-4400=4600
    exact = _config(tmp_path, GpuRule("2", reserve_mib=2500))
    assert choose_gpu(CARDS, ContainerEngine("x"), exact).gpu.index == 2  # 7000-2500 = 4500 = budget + headroom: fits


def test_the_headroom_is_added_to_the_budget(tmp_path: Path) -> None:
    tight = [GpuInfo(0, "GPU-a", 4400, 24000)]
    with pytest.raises(PlacementRefused):
        choose_gpu(tight, ContainerEngine("x"), _config(tmp_path, headroom_mib=512))
    assert choose_gpu(tight, ContainerEngine("x"), _config(tmp_path, headroom_mib=400)).gpu.index == 0


def test_an_empty_list_means_every_card_the_one_with_the_most_free_vram_first(tmp_path: Path) -> None:
    assert choose_gpu(CARDS, ContainerEngine("x"), _config(tmp_path)).gpu.uuid == "GPU-b"


def test_a_listed_card_that_is_not_there_is_not_an_option(tmp_path: Path) -> None:
    assert choose_gpu(CARDS, ContainerEngine("x"), _config(tmp_path, GpuRule("4"), GpuRule("1"))).gpu.index == 1
    with pytest.raises(PlacementRefused, match="none of the configured GPUs is present"):
        choose_gpu(CARDS, ContainerEngine("x"), _config(tmp_path, GpuRule("4")))


def test_the_configured_budget_beats_the_engines_own(tmp_path: Path) -> None:
    engine = ContainerEngine("x")
    assert vram_budget_mib(engine, _config(tmp_path)) == 4000
    assert vram_budget_mib(engine, _config(tmp_path, x=EngineDefaults(vram_budget_mib=8000))) == 8000
    config = _config(tmp_path, x=EngineDefaults(vram_budget_mib=8000))
    assert choose_gpu(CARDS, engine, config).budget_mib == 8000 and choose_gpu(CARDS, engine, config).gpu.index == 1


def test_an_unknown_budget_refuses_the_placement(tmp_path: Path) -> None:
    engine = ContainerEngine("x")
    engine.default_vram_budget_mib = None  # type: ignore[assignment]
    with pytest.raises(PlacementRefused, match="no VRAM budget for x"):
        choose_gpu(CARDS, engine, _config(tmp_path))


def test_when_nothing_fits_the_text_says_what_was_needed_and_what_was_most_free(tmp_path: Path) -> None:
    config = _config(tmp_path, x=EngineDefaults(vram_budget_mib=12000))
    with pytest.raises(PlacementRefused) as refused:
        choose_gpu(CARDS, ContainerEngine("x"), config)
    assert str(refused.value) == "needs 12500 MiB, most free 9000 MiB on GPU 1"
    reserved = _config(tmp_path, GpuRule("0"), GpuRule("2", reserve_mib=3000), x=EngineDefaults(vram_budget_mib=6000))
    with pytest.raises(PlacementRefused) as refused:
        choose_gpu(CARDS, ContainerEngine("x"), reserved)
    assert str(refused.value) == "needs 6500 MiB, most free 5000 MiB on GPU 0"
