"""The launcher: local GPU start, one start at a time, remote start over SSH, entries without a
container, background start, holds and heartbeat — with fakes for every system call."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from container_fakes import (
    CardList,
    ContainerEngine,
    FakeSsh,
    install,
    local_entry,
    make_config,
    remote_entry,
)
from speech_fakes import FakeEngine, request, run_async

from tts_suite.api import (
    EngineControlFailed,
    EngineInUse,
    EngineStarting,
    GpuRule,
    SuiteConfig,
    TraceStep,
)
from tts_suite.escalation import Skip
from tts_suite.holds import EngineHolds
from tts_suite.launcher import SuiteLauncher
from tts_suite.placement import GpuInfo
from tts_suite.speaker import Speaker


def _launcher(
    tmp_path: Path, cards: CardList | None = None, ssh: FakeSsh | None = None, config: SuiteConfig | None = None,
    **kwargs: object,
) -> SuiteLauncher:
    return SuiteLauncher(
        config or SuiteConfig(voices_dir=tmp_path), gpus=cards or CardList(20000), host_command=ssh or FakeSsh(),
        **kwargs,  # type: ignore[arg-type]
    )


# ── Local GPU start ─────────────────────────────────────────────────


def test_a_local_gpu_engine_starts_on_the_chosen_card_with_its_budget(tmp_path: Path) -> None:
    engine = ContainerEngine("a")
    cards = CardList(3000, 9000, 20000)
    launcher = _launcher(tmp_path, cards, config=SuiteConfig(voices_dir=tmp_path, gpu_mode="list", gpus=(GpuRule("1"), GpuRule("2"))))

    async def scenario() -> None:
        assert not await launcher.is_serving(local_entry(engine))
        assert await launcher.ensure_serving(local_entry(engine), wait=True) is None
        assert await launcher.is_serving(local_entry(engine))
        await launcher.aclose()

    run_async(scenario())
    assert engine.placements == [("GPU-1", 4000)] and engine.ready_calls == 1


def test_a_start_that_does_not_come_up_is_a_skip_with_the_reason(tmp_path: Path) -> None:
    engine = ContainerEngine("a", ready_ok=False)
    skip = run_async(_launcher(tmp_path).ensure_serving(local_entry(engine), wait=True))
    assert skip == Skip("stopped", "start failed: model did not load")


def test_no_card_that_fits_is_no_vram_with_the_numbers(tmp_path: Path) -> None:
    engine = ContainerEngine("a")
    skip = run_async(_launcher(tmp_path, CardList(1000, 2000)).ensure_serving(local_entry(engine), wait=True))
    assert skip == Skip("no_vram", "needs 4512 MiB, most free 2000 MiB on GPU 1") and engine.ready_calls == 0


def test_no_vram_is_not_remembered_so_the_next_request_chooses_the_card_again(tmp_path: Path) -> None:
    engine = ContainerEngine("a")
    cards = CardList(1000)
    launcher = _launcher(tmp_path, cards)

    async def scenario() -> None:
        entry = local_entry(engine)
        skip = await launcher.ensure_serving(entry, wait=True)
        assert skip is not None and skip.reason == "no_vram" and engine.ready_calls == 0
        assert launcher.start_failure(entry) is None
        cards.cards[0] = GpuInfo(0, "GPU-0", 20000, 24000, 4000)  # room again, no explicit start
        assert await launcher.ensure_serving(entry, wait=True) is None
        assert engine.ready_calls == 1

    run_async(scenario())


def test_an_engine_without_a_budget_is_no_vram(tmp_path: Path) -> None:
    engine = ContainerEngine("a")
    engine.default_vram_budget_mib = None  # type: ignore[assignment]
    skip = run_async(_launcher(tmp_path).ensure_serving(local_entry(engine), wait=True))
    assert skip is not None and skip.reason == "no_vram" and "no VRAM budget for a" in skip.detail


def test_nvidia_smi_failing_is_no_vram_with_its_message(tmp_path: Path) -> None:
    from tts_suite.placement import PlacementRefused

    def broken() -> list[object]:
        raise PlacementRefused("nvidia-smi unavailable: gone")

    launcher = SuiteLauncher(SuiteConfig(voices_dir=tmp_path), gpus=broken)  # type: ignore[arg-type]
    skip = run_async(launcher.ensure_serving(local_entry(ContainerEngine("a")), wait=True))
    assert skip == Skip("no_vram", "nvidia-smi unavailable: gone")


def test_an_image_that_is_not_built_is_a_skip_and_nothing_starts(tmp_path: Path) -> None:
    engine = ContainerEngine("a", installed=False)
    skip = run_async(_launcher(tmp_path).ensure_serving(local_entry(engine), wait=True))
    assert skip == Skip("stopped", "docker image not built") and engine.ready_calls == 0


def test_only_one_local_gpu_start_runs_at_a_time(tmp_path: Path) -> None:
    first, second = ContainerEngine("a", ready_delay_s=0.05), ContainerEngine("b", ready_delay_s=0.05)

    async def scenario() -> None:
        launcher = _launcher(tmp_path)
        await asyncio.gather(
            launcher.ensure_serving(local_entry(first), wait=True), launcher.ensure_serving(local_entry(second), wait=True),
        )

    run_async(scenario())
    assert first.ready_calls == second.ready_calls == 1
    assert first.finished_at <= second.started_at or second.finished_at <= first.started_at  # no overlap


def test_parallel_requests_for_the_same_entry_start_it_once(tmp_path: Path) -> None:
    engine = ContainerEngine("a", ready_delay_s=0.05)

    async def scenario() -> list[Skip | None]:
        launcher = _launcher(tmp_path)
        return list(await asyncio.gather(*(launcher.ensure_serving(local_entry(engine), wait=True) for _ in range(5))))

    assert run_async(scenario()) == [None] * 5 and engine.ready_calls == 1


# ── Another host (SSH) ──────────────────────────────────────────────


def test_a_remote_entry_is_started_over_ssh_and_awaited(tmp_path: Path) -> None:
    engine = ContainerEngine("q", remote=True)
    ssh = FakeSsh(then=lambda *command: setattr(engine, "up", True))
    skip = run_async(_launcher(tmp_path, ssh=ssh).ensure_serving(remote_entry(engine), wait=True))
    assert skip is None and ssh.commands == [("Aragon", "start", "q")]
    assert engine.ready_calls == 0 and engine.placements == []  # the host chooses its card, not the suite


def test_a_failing_ssh_command_is_a_skip_with_the_hosts_message(tmp_path: Path) -> None:
    ssh = FakeSsh(fail="no route to host")
    skip = run_async(_launcher(tmp_path, ssh=ssh).ensure_serving(remote_entry(ContainerEngine("q", remote=True)), wait=True))
    assert skip == Skip("stopped", "start failed: Aragon: 'start q' failed: no route to host")


def test_a_remote_container_that_never_serves_is_a_skip_after_the_startup_time(tmp_path: Path) -> None:
    engine = ContainerEngine("q", remote=True)
    engine.startup_timeout_s = 0
    skip = run_async(_launcher(tmp_path, ssh=FakeSsh()).ensure_serving(remote_entry(engine), wait=True))
    assert skip == Skip("stopped", "not ready on Aragon within 0 s")


def test_an_unreachable_host_is_a_skip_at_once_without_an_ssh_attempt(tmp_path: Path) -> None:
    engine = ContainerEngine("q", remote=True)
    engine.reach = "unreachable"
    ssh = FakeSsh()
    launcher = _launcher(tmp_path, ssh=ssh)
    skip = run_async(launcher.ensure_serving(remote_entry(engine), wait=True))
    assert skip == Skip("stopped", "host Aragon unreachable") and ssh.commands == []
    assert launcher.start_failure(remote_entry(engine)) is None  # transient: the next request asks the host again
    assert run_async(launcher.status(remote_entry(engine))) == "unreachable"


def test_a_reachable_host_with_the_container_off_is_stopped_and_started_over_ssh(tmp_path: Path) -> None:
    engine = ContainerEngine("q", remote=True)  # up = False: the host refuses the connection
    ssh = FakeSsh(then=lambda *command: setattr(engine, "up", True))
    launcher = _launcher(tmp_path, ssh=ssh)

    async def scenario() -> None:
        assert engine.reachability() == "refused" and await launcher.status(remote_entry(engine)) == "stopped"
        assert await launcher.ensure_serving(remote_entry(engine), wait=True) is None
        assert await launcher.status(remote_entry(engine)) == "running"

    run_async(scenario())
    assert ssh.commands == [("Aragon", "start", "q")]


# ── Entries without a container ─────────────────────────────────────


def test_an_engine_without_a_container_is_only_checked(tmp_path: Path) -> None:
    cloud = FakeEngine("edge", cloud=True)
    down = FakeEngine("piper")
    down.is_running = lambda: False  # type: ignore[method-assign]

    async def scenario() -> None:
        launcher = _launcher(tmp_path)
        assert await launcher.is_serving(local_entry(cloud))
        assert await launcher.status(local_entry(cloud)) == "running"
        assert await launcher.ensure_serving(local_entry(down), wait=True) == Skip(
            "stopped", "piper has no container the suite can start",
        )
        assert await launcher.status(local_entry(down)) == "unreachable"
        with pytest.raises(EngineControlFailed, match="no container"):
            await launcher.stop(local_entry(down))

    run_async(scenario())


# ── Start in the background ─────────────────────────────────────────


async def _until_settled(launcher: SuiteLauncher, engine: ContainerEngine) -> None:
    while await launcher.status(local_entry(engine)) == "starting":
        await asyncio.sleep(0.01)


def test_without_waiting_the_start_runs_in_the_background_and_the_status_follows(tmp_path: Path) -> None:
    engine = ContainerEngine("a", ready_delay_s=0.05)

    async def scenario() -> None:
        launcher = _launcher(tmp_path)
        entry = local_entry(engine)
        with pytest.raises(EngineStarting):
            await launcher.ensure_serving(entry, wait=False)
        assert await launcher.status(entry) == "starting"
        with pytest.raises(EngineStarting):  # still on its way: no second start
            await launcher.ensure_serving(entry, wait=False)
        await _until_settled(launcher, engine)
        assert await launcher.status(entry) == "running"
        assert launcher.start_failure(entry) is None

    run_async(scenario())
    assert engine.ready_calls == 1


def test_the_failure_of_a_start_is_remembered_until_an_explicit_start_or_a_new_configuration(tmp_path: Path) -> None:
    engine = ContainerEngine("a", ready_ok=False, ready_delay_s=0.02)

    async def scenario() -> None:
        launcher = _launcher(tmp_path)
        entry = local_entry(engine)
        with pytest.raises(EngineStarting):
            await launcher.ensure_serving(entry, wait=False)
        await _until_settled(launcher, engine)
        failure = Skip("stopped", "start failed: model did not load")
        assert await launcher.status(entry) == "stopped" and launcher.start_failure(entry) == failure
        assert await launcher.ensure_serving(entry, wait=False) == failure
        assert await launcher.ensure_serving(entry, wait=True) == failure  # no second wait for a start that did not work
        assert engine.ready_calls == 1
        launcher.clear_failures(entry)  # an explicit start
        with pytest.raises(EngineStarting):
            await launcher.ensure_serving(entry, wait=False)
        await _until_settled(launcher, engine)
        assert launcher.start_failure(entry) == failure and engine.ready_calls == 2
        launcher.set_config(SuiteConfig(voices_dir=tmp_path))  # a new configuration: forgotten
        assert launcher.start_failure(entry) is None

    run_async(scenario())


def test_a_reader_does_not_wait_longer_than_the_startup_time_plus_the_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tts_suite import launcher as launcher_module

    monkeypatch.setattr(launcher_module, "START_WAIT_RESERVE_S", 0.05)
    engine = ContainerEngine("a", ready_delay_s=0.4)
    engine.startup_timeout_s = 0

    async def scenario() -> None:
        launcher = _launcher(tmp_path)
        started = time.monotonic()
        skip = await launcher.ensure_serving(local_entry(engine), wait=True)
        assert skip == Skip("stopped", "start did not finish within 0.05 s")
        assert time.monotonic() - started < 0.3
        await launcher.aclose()

    run_async(scenario())


# ── Stopping ────────────────────────────────────────────────────────


def test_stop_is_refused_while_a_reading_holds_the_container(tmp_path: Path) -> None:
    engine = ContainerEngine("a")
    engine.up = True

    async def scenario() -> None:
        launcher = _launcher(tmp_path, holds=EngineHolds(ping=lambda engine: None, interval_s=60))
        entry = local_entry(engine)
        launcher.hold(entry)
        with pytest.raises(EngineInUse):
            await launcher.stop(entry)
        assert engine.stops == 0
        launcher.release(entry)
        await launcher.stop(entry)
        assert engine.stops == 1
        await launcher.aclose()

    run_async(scenario())


def test_stop_is_refused_while_the_container_is_starting(tmp_path: Path) -> None:
    engine = ContainerEngine("a", ready_delay_s=0.1)

    async def scenario() -> None:
        launcher = _launcher(tmp_path)
        entry = local_entry(engine)
        with pytest.raises(EngineStarting):
            await launcher.ensure_serving(entry, wait=False)
        with pytest.raises(EngineInUse, match="a is starting"):
            await launcher.stop(entry)
        await _until_settled(launcher, engine)
        await launcher.stop(entry)

    run_async(scenario())
    assert engine.stops == 1


def test_a_remote_container_is_stopped_over_ssh(tmp_path: Path) -> None:
    ssh = FakeSsh()
    run_async(_launcher(tmp_path, ssh=ssh).stop(remote_entry(ContainerEngine("q", remote=True))))
    assert ssh.commands == [("Aragon", "stop", "q")]
    with pytest.raises(EngineControlFailed, match="no route"):
        run_async(_launcher(tmp_path, ssh=FakeSsh(fail="no route")).stop(remote_entry(ContainerEngine("q", remote=True))))


# ── Holds and heartbeat ─────────────────────────────────────────────


def test_a_held_container_gets_a_heartbeat_until_the_last_hold_is_released() -> None:
    engine = ContainerEngine("a")
    pings: list[str] = []

    def ping(pinged: ContainerEngine) -> None:
        pings.append(pinged.key)
        if len(pings) == 1:
            raise RuntimeError("one ping lost")  # does not end the heartbeat

    async def scenario() -> None:
        holds = EngineHolds(ping=ping, interval_s=0.01)  # type: ignore[arg-type]
        holds.acquire("a", engine)
        holds.acquire("a", engine)
        await asyncio.sleep(0.1)
        assert len(pings) >= 2 and holds.in_use("a")
        holds.release("a")
        assert holds.in_use("a")
        holds.release("a")
        assert not holds.in_use("a")
        await asyncio.sleep(0.03)
        settled = len(pings)
        await asyncio.sleep(0.05)
        assert len(pings) == settled
        holds.release("a")  # one too many does nothing
        assert not holds.in_use("a")
        await holds.aclose()

    run_async(scenario())


# ── In a reading ────────────────────────────────────────────────────

REQUEST = request("a")


def test_a_reading_holds_its_container_until_the_stream_has_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ContainerEngine("a")
    engine.up = True
    install(monkeypatch, engine)
    config = make_config(tmp_path)
    launcher = SuiteLauncher(config, gpus=CardList(20000), host_command=FakeSsh())
    speaker = Speaker(config, launcher)

    async def scenario() -> None:
        gate = engine.gates["zwei"] = asyncio.Event()
        stream = speaker.speak("Der Satz mit zwei Wörtern kommt hier.", REQUEST)
        reading = asyncio.ensure_future(_collect(stream))
        await asyncio.sleep(0.05)
        with pytest.raises(EngineInUse):
            await launcher.stop(speaker.entry_for("a", None, "sentence"))
        gate.set()
        assert len(await reading) == 1
        await launcher.stop(speaker.entry_for("a", None, "sentence"))  # the stream ended: the container is free again
        await launcher.aclose()

    run_async(scenario())
    assert engine.stops == 1


def test_a_second_stream_of_the_same_run_holds_the_container_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ContainerEngine("a")
    engine.up = True
    install(monkeypatch, engine)
    config = make_config(tmp_path)
    launcher = SuiteLauncher(config, gpus=CardList(20000), host_command=FakeSsh())
    speaker = Speaker(config, launcher)
    named = request("a", run="r-1")

    async def scenario() -> None:
        await _collect(speaker.speak("Der erste Satz kommt hier.", named))  # its end lets go of the run's holds
        gate = engine.gates["zwei"] = asyncio.Event()
        reading = asyncio.ensure_future(_collect(speaker.speak("Der Satz mit zwei Wörtern kommt hier.", named)))
        await asyncio.sleep(0.05)
        with pytest.raises(EngineInUse):
            await launcher.stop(speaker.entry_for("a", None, "sentence"))
        gate.set()
        await reading
        await launcher.stop(speaker.entry_for("a", None, "sentence"))
        await launcher.aclose()

    run_async(scenario())
    assert engine.stops == 1


def test_an_aborted_stream_lets_go_of_the_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ContainerEngine("a")
    engine.up = True
    install(monkeypatch, engine)
    config = make_config(tmp_path)
    launcher = SuiteLauncher(config, gpus=CardList(20000), host_command=FakeSsh())
    speaker = Speaker(config, launcher)

    async def scenario() -> None:
        engine.gates["zwei"] = asyncio.Event()
        stream = speaker.speak("Der Satz mit zwei Wörtern kommt hier.", REQUEST)
        reading = asyncio.ensure_future(_collect(stream))
        await asyncio.sleep(0.05)
        await stream.aclose()
        await stream.aclose()  # twice does not release twice
        await asyncio.gather(reading, return_exceptions=True)
        await launcher.stop(speaker.entry_for("a", None, "sentence"))
        await launcher.aclose()

    run_async(scenario())
    assert engine.stops == 1


async def _collect(stream: object) -> list[object]:
    return [segment async for segment in stream.segments()]  # type: ignore[attr-defined]


def test_the_skip_detail_of_the_launcher_reaches_the_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tts_suite.api import NoEngineAvailable

    engine = ContainerEngine("a")
    install(monkeypatch, engine)
    config = make_config(tmp_path)
    launcher = SuiteLauncher(config, gpus=CardList(1000), host_command=FakeSsh())
    speaker = Speaker(config, launcher)

    async def scenario() -> None:
        await _collect(speaker.speak("Hallo Welt hier.", REQUEST))

    with pytest.raises(NoEngineAvailable) as error:
        run_async(scenario())
    assert error.value.trace == (TraceStep("a", "skipped", "no_vram", "needs 4512 MiB, most free 1000 MiB on GPU 0"),)
