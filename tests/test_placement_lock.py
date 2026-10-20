"""The card-choice lock shared with other programs: exclusive, optional, and released when it is let go."""
from __future__ import annotations

import asyncio
import fcntl
from pathlib import Path

from container_fakes import CardList, ContainerEngine, local_entry
from speech_fakes import run_async

from tts_suite.api import SuiteConfig
from tts_suite.launcher import SuiteLauncher
from tts_suite.placement_lock import program_lock


def test_no_lock_file_means_nothing_to_wait_for() -> None:
    async def scenario() -> None:
        async with program_lock(None):
            pass

    run_async(scenario())


def test_the_second_holder_waits_until_the_first_lets_go(tmp_path: Path) -> None:
    lock = tmp_path / "placement.lock"
    order: list[str] = []

    async def holder(name: str, seconds: float) -> None:
        async with program_lock(lock):
            order.append(f"{name} in")
            await asyncio.sleep(seconds)
            order.append(f"{name} out")

    async def scenario() -> None:
        first = asyncio.create_task(holder("a", 0.3))
        await asyncio.sleep(0.05)
        await holder("b", 0)
        await first

    run_async(scenario())
    assert order == ["a in", "a out", "b in", "b out"]


def test_a_start_waits_for_a_program_that_holds_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "placement.lock"
    engine = ContainerEngine("a")
    launcher = SuiteLauncher(
        SuiteConfig(voices_dir=tmp_path, placement_lock_file=lock), gpus=CardList(20000),
    )

    async def scenario() -> None:
        with lock.open("a") as other_program:  # the STT suite, say, choosing its card
            fcntl.flock(other_program, fcntl.LOCK_EX)
            start = asyncio.create_task(launcher.ensure_serving(local_entry(engine), wait=True))
            await asyncio.sleep(0.5)
            assert not start.done() and engine.ready_calls == 0
        assert await start is None
        assert engine.ready_calls == 1
        await launcher.aclose()

    run_async(scenario())
