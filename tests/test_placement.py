"""Card choice: order, reserve, headroom, most free VRAM, unknown budget, nothing fits."""
from __future__ import annotations

from pathlib import Path

import pytest
from container_fakes import ContainerEngine

from tts_suite.api import EngineDefaults, GpuRule, SuiteConfig
from tts_suite.placement import (
    GpuInfo,
    PlacementRefused,
    choose_gpu,
    choose_gpu_for_burn_in,
    parse_gpus,
    vram_budget_mib,
    vram_needed_mib,
)

CARDS = [GpuInfo(0, "GPU-a", 5000, 24000, 19000), GpuInfo(1, "GPU-b", 9000, 24000, 15000), GpuInfo(2, "GPU-c", 7000, 24000, 17000)]


def _config(tmp_path: Path, *rules: GpuRule, headroom_mib: int = 500, **engines: EngineDefaults) -> SuiteConfig:
    return SuiteConfig(voices_dir=tmp_path, gpu_mode="list" if rules else "auto", gpus=rules, headroom_mib=headroom_mib, engines=engines)


def test_the_nvidia_smi_lines_are_read() -> None:
    assert parse_gpus("0, GPU-a, 5000, 24000, 19000\n1, GPU-b, 9000, 24000, 15000\n") == CARDS[:2]


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
    tight = [GpuInfo(0, "GPU-a", 4400, 24000, 19600)]
    with pytest.raises(PlacementRefused):
        choose_gpu(tight, ContainerEngine("x"), _config(tmp_path, headroom_mib=512))
    assert choose_gpu(tight, ContainerEngine("x"), _config(tmp_path, headroom_mib=400)).gpu.index == 0


def test_an_empty_list_means_every_card_the_one_with_the_most_free_vram_first(tmp_path: Path) -> None:
    assert choose_gpu(CARDS, ContainerEngine("x"), _config(tmp_path)).gpu.uuid == "GPU-b"


def test_auto_mode_takes_a_card_nobody_uses_before_the_one_with_the_most_free_vram(tmp_path: Path) -> None:
    cards = [*CARDS, GpuInfo(3, "GPU-d", 6000, 6000, 0), GpuInfo(4, "GPU-e", 6000, 6000, 0)]
    assert choose_gpu(cards, ContainerEngine("x"), _config(tmp_path)).gpu.uuid == "GPU-d"  # unused, though GPU-b has more free
    taken = [*CARDS, GpuInfo(3, "GPU-d", 5000, 6000, 1000), GpuInfo(4, "GPU-e", 6000, 6000, 0)]
    assert choose_gpu(taken, ContainerEngine("x"), _config(tmp_path)).gpu.uuid == "GPU-e"  # the first one is in use now: the other free card
    too_small = [*CARDS, GpuInfo(3, "GPU-d", 3000, 3000, 0)]
    assert choose_gpu(too_small, ContainerEngine("x"), _config(tmp_path)).gpu.uuid == "GPU-b"  # unused but too small: next in line


def test_auto_mode_ignores_the_list_and_picks_the_best_fit(tmp_path: Path) -> None:
    config = SuiteConfig(voices_dir=tmp_path, gpu_mode="auto", gpus=(GpuRule("0"),), headroom_mib=500)
    assert choose_gpu(CARDS, ContainerEngine("x"), config).gpu.uuid == "GPU-b"  # card 0 is listed but card 1 has the most free


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


def test_the_needed_vram_is_budget_plus_headroom_and_the_one_figure_the_placement_uses(tmp_path: Path) -> None:
    engine = ContainerEngine("x")
    assert vram_needed_mib(engine, _config(tmp_path, headroom_mib=256)) == 4256
    assert vram_needed_mib(engine, _config(tmp_path, x=EngineDefaults(vram_budget_mib=8000), headroom_mib=512)) == 8512
    engine.default_vram_budget_mib = None  # type: ignore[assignment]
    assert vram_needed_mib(engine, _config(tmp_path)) is None


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


def test_a_named_card_is_taken_outside_the_rules_of_the_mode(tmp_path: Path) -> None:
    listed = _config(tmp_path, GpuRule("1"))
    assert choose_gpu(CARDS, ContainerEngine("x"), listed, gpu="2").gpu.uuid == "GPU-c"  # not in the list
    assert choose_gpu(CARDS, ContainerEngine("x"), _config(tmp_path), gpu="GPU-c").gpu.index == 2  # auto would take GPU-b


def test_a_named_card_that_is_missing_or_too_small_refuses_the_placement(tmp_path: Path) -> None:
    with pytest.raises(PlacementRefused, match="GPU 7 is not present"):
        choose_gpu(CARDS, ContainerEngine("x"), _config(tmp_path), gpu="7")
    with pytest.raises(PlacementRefused, match="needs 4500 MiB, most free 4000 MiB on GPU 0"):
        choose_gpu([GpuInfo(0, "GPU-a", 4000, 24000, 20000), *CARDS[1:]], ContainerEngine("x"), _config(tmp_path), gpu="0")


def test_a_burn_in_takes_a_card_nobody_uses_and_starts_without_a_limit(tmp_path: Path) -> None:
    cards = [GpuInfo(0, "GPU-a", 9000, 24000, 15000), GpuInfo(1, "GPU-b", 7000, 8000, 0), GpuInfo(2, "GPU-c", 20000, 24000, 0)]
    placement = choose_gpu_for_burn_in(cards, _config(tmp_path))
    assert placement.gpu.uuid == "GPU-c" and placement.budget_mib is None


def test_a_burn_in_keeps_to_the_cards_of_the_list(tmp_path: Path) -> None:
    cards = [GpuInfo(0, "GPU-a", 24000, 24000, 0), GpuInfo(1, "GPU-b", 8000, 8000, 0)]
    assert choose_gpu_for_burn_in(cards, _config(tmp_path, GpuRule("1"))).gpu.uuid == "GPU-b"


def test_a_burn_in_refuses_when_every_card_is_in_use(tmp_path: Path) -> None:
    with pytest.raises(PlacementRefused, match="nobody uses"):
        choose_gpu_for_burn_in(CARDS, _config(tmp_path))
