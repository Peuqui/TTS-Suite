"""The measurement of a burn-in: the peak over every reading, and a failure ends it."""
from __future__ import annotations

import asyncio

import pytest
from speech_fakes import run_async

from tts_suite.burn_in import BURN_IN_ROUNDS, measure_peak_mib


def test_every_round_is_read_and_the_peak_counts_also_after_the_last_reading() -> None:
    read: list[str] = []
    samples = iter([1000, 3500])  # the second one is the sample after the last reading
    told: list[tuple[int, int]] = []

    async def reading(language: str, text: str) -> None:
        read.append(language)
        await asyncio.sleep(0)

    async def sample() -> int:
        return next(samples, 0)

    peak = run_async(measure_peak_mib(reading, sample, lambda current, mib: told.append((current, mib))))
    assert read == [language for language, _ in BURN_IN_ROUNDS]
    assert peak == 3500


def test_the_texts_are_long_enough_for_the_decoder_to_reach_its_full_allocation() -> None:
    assert {language for language, _ in BURN_IN_ROUNDS} == {"de", "en"}
    assert all(len(text) > 2000 for _, text in BURN_IN_ROUNDS)


def test_a_failed_reading_ends_the_measurement() -> None:
    async def reading(language: str, text: str) -> None:
        raise RuntimeError("the engine broke")

    async def sample() -> int:
        return 1000

    with pytest.raises(RuntimeError, match="the engine broke"):
        run_async(measure_peak_mib(reading, sample, lambda current, mib: None))
