"""The Suite: configuration, the catalog of engines, voices, start/stop, reload, speaking."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from container_fakes import (
    ARAGON,
    CardList,
    ContainerEngine,
    FakeManagedLauncher,
    FakeSsh,
    install,
    make_config,
)
from speech_fakes import FakeEngine, request, run_async

from tts_suite import suite as suite_module
from tts_suite.api import (
    ConfigInvalid,
    EngineControlFailed,
    EngineDefaults,
    EngineInfo,
    EngineInUse,
    EngineTarget,
    GpuHolder,
    GpuReleaseResult,
    Host,
    InvalidRequest,
    ReadingAborted,
    SuiteConfig,
    TTSSuite,
    UnknownEngine,
    UnknownHost,
    VoiceInfo,
)
from tts_suite.engines import registry
from tts_suite.engines.base import Reachability, TTSEngine
from tts_suite.escalation import EscalationEntry, Skip
from tts_suite.launcher import SuiteLauncher
from tts_suite.suite import Suite, create_suite


@pytest.fixture(autouse=True)
def no_docker_no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real engines in these tests would ask Docker whether their image is built and ``nvidia-smi``
    what they hold; the fakes have no ``i18n.json`` for their labels."""
    monkeypatch.setattr(TTSEngine, "is_installed", lambda self: True)
    monkeypatch.setattr(suite_module, "engine_vram_mib", lambda engine: {})
    monkeypatch.setattr(TTSEngine, "label", lambda self, language: self.key)


def _suite(config: SuiteConfig, launcher: FakeManagedLauncher | None = None) -> tuple[Suite, FakeManagedLauncher]:
    launcher = launcher or FakeManagedLauncher()
    suite: TTSSuite = create_suite(config, launcher=launcher)  # also type-checks that Suite implements the Protocol
    assert isinstance(suite, Suite)
    return suite, launcher


# ── Configuration ───────────────────────────────────────────────────


def test_the_configuration_is_replaced_for_the_speaker_and_the_launcher(tmp_path: Path) -> None:
    suite, launcher = _suite(make_config(tmp_path))
    new = make_config(tmp_path, headroom_mib=100)

    async def scenario() -> SuiteConfig:
        await suite.set_config(new)
        return await suite.config()

    assert run_async(scenario()) is new and launcher.configs == [new]


def test_an_invalid_configuration_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    suite, launcher = _suite(config)

    async def scenario() -> None:
        with pytest.raises(ConfigInvalid, match="unknown TTS engine"):
            await suite.set_config(make_config(tmp_path, {"nope": EngineDefaults()}))
        with pytest.raises(ConfigInvalid, match="voices_dir"):
            await suite.set_config(make_config(tmp_path / "missing"))
        assert await suite.config() is config

    run_async(scenario())
    assert launcher.configs == []


def test_a_broken_configuration_cannot_create_a_suite(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid):
        create_suite(make_config(tmp_path, {"nope": EngineDefaults()}), launcher=FakeManagedLauncher())


# ── The catalog ─────────────────────────────────────────────────────


def _rows(infos: list[EngineInfo]) -> dict[tuple[str, str | None], EngineInfo]:
    return {(info.engine, info.host): info for info in infos}


def test_the_catalog_has_a_row_per_registered_engine_on_this_machine(tmp_path: Path) -> None:
    suite, _ = _suite(make_config(tmp_path), FakeManagedLauncher(statuses={"xtts": "stopped", "edge": "running"}))
    rows = _rows(run_async(suite.engines()))
    assert [key for key in rows] == [(key, None) for key in registry.TTS_ENGINES]  # registry order, no list involved
    xtts, edge, browser = rows[("xtts", None)], rows[("edge", None)], rows[("browser", None)]
    assert (xtts.host, xtts.status, xtts.needs_gpu, xtts.cloud, xtts.browser) == (None, "stopped", True, False, False)
    assert set(xtts.label) == set(xtts.label_short) == {"de", "en"} and all(xtts.label.values())
    assert xtts.label_short["de"] == "XTTS" and xtts.unit_default == "sentence"
    assert (edge.cloud, edge.status, edge.unit_default) == (True, "running", "sentence")
    assert browser.browser and not browser.needs_gpu


def test_each_host_gets_a_row_for_every_engine_with_a_rest_api_and_for_no_other(tmp_path: Path) -> None:
    suite, _ = _suite(make_config(tmp_path, hosts=(ARAGON, Host("Basti", "192.0.2.3"))))
    rows = _rows(run_async(suite.engines()))
    for host in ("Aragon", "Basti"):
        assert {key for key, name in rows if name == host} == {
            key for key, engine in registry.TTS_ENGINES.items() if engine.default_port is not None
        }
        assert ("xtts", host) in rows and ("qwen3local", host) in rows
        for no_rest_api in ("piper", "edge", "dashscope_audio3", "browser"):  # cloud, CLI and browser engines stay local
            assert (no_rest_api, host) not in rows
    assert rows[("xtts", "Aragon")].host == "Aragon"


def test_a_host_that_does_not_answer_is_a_row_not_an_error(tmp_path: Path) -> None:
    suite, _ = _suite(make_config(tmp_path, hosts=(ARAGON,)), FakeManagedLauncher(statuses={"xtts@Aragon": "unreachable"}))
    assert _rows(run_async(suite.engines()))[("xtts", "Aragon")].status == "unreachable"


class HostAwareEngine(ContainerEngine):
    """An engine whose binding to a host yields its own object, reachable or not by address."""

    default_port = 5000
    reach_by_address: dict[str, Reachability]

    def _bound(self, **changes: Any) -> Any:
        bound = HostAwareEngine(self.key)
        bound.reach_by_address = self.reach_by_address
        bound.address = changes.get("address", self.address)
        return bound

    def reachability(self) -> Reachability:
        return self.reach_by_address[self.address]


def test_the_catalog_tells_a_host_that_is_off_from_a_host_whose_container_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = HostAwareEngine("hostfake")
    engine.reach_by_address = {"localhost": "refused", "192.0.2.2": "unreachable", "192.0.2.3": "refused"}
    install(monkeypatch, engine)
    config = make_config(tmp_path, hosts=(ARAGON, Host("Basti", "192.0.2.3")))
    suite, _ = _suite(config, SuiteLauncher(config, gpus=list, host_command=FakeSsh()))  # type: ignore[arg-type]
    rows = _rows(run_async(suite.engines()))
    assert rows[("hostfake", "Aragon")].status == "unreachable"
    assert rows[("hostfake", "Basti")].status == "stopped"  # up, the container is off
    assert rows[("hostfake", None)].status == "stopped"


def test_the_rows_are_asked_in_parallel_so_a_silent_host_delays_only_by_one_wait(tmp_path: Path) -> None:
    launcher = FakeManagedLauncher(status_delay_s=0.05)
    suite, _ = _suite(make_config(tmp_path, hosts=(ARAGON, Host("Basti", "192.0.2.3"))), launcher)
    infos = run_async(suite.engines())
    assert len(infos) > 10 and launcher.status_peak == len(infos)  # all asked at the same time, not one after the other


def test_installed_tells_whether_a_row_can_be_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built, missing = ContainerEngine("builtfake"), ContainerEngine("missingfake", installed=False)
    cloud = FakeEngine("cloudfake", cloud=True)
    built.default_port = missing.default_port = 5000
    install(monkeypatch, built, missing, cloud)
    suite, _ = _suite(make_config(tmp_path, hosts=(ARAGON,)))
    rows = _rows(run_async(suite.engines()))
    assert rows[("builtfake", None)].installed and not rows[("missingfake", None)].installed and rows[("cloudfake", None)].installed
    assert rows[("missingfake", "Aragon")].installed  # the suite cannot see that host's Docker


def test_start_hint_is_the_reason_of_the_last_failed_start_until_it_is_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, ContainerEngine("brokenfake"), ContainerEngine("fine"))
    suite, launcher = _suite(make_config(tmp_path))

    def hints() -> tuple[str | None, str | None]:
        rows = _rows(run_async(suite.engines()))
        return rows[("brokenfake", None)].start_hint, rows[("fine", None)].start_hint

    assert hints() == (None, None)
    launcher.failures["brokenfake"] = Skip("stopped", "start failed: model did not load")
    assert hints() == ("start failed: model did not load", None)
    run_async(suite.set_config(make_config(tmp_path)))  # the real launcher forgets here
    launcher.clear_failures(None)
    assert hints() == (None, None)


def test_vram_is_reported_for_running_local_gpu_engines_only_and_a_failed_measurement_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    suite, _ = _suite(make_config(tmp_path, hosts=(ARAGON,)))

    def measure(engine: object) -> dict[str, int]:
        if engine.key == "qwen3local":  # type: ignore[attr-defined]
            raise suite_module.VramMeasurementFailed("nvidia-smi unavailable: gone")
        return {"GPU-a": 3500}

    monkeypatch.setattr(suite_module, "engine_vram_mib", measure)
    with caplog.at_level("ERROR"):
        rows = _rows(run_async(suite.engines()))
    assert rows[("xtts", None)].vram_mib == {"GPU-a": 3500} and rows[("qwen3local", None)].vram_mib is None
    assert rows[("qwen3local", "Aragon")].vram_mib == {} and rows[("edge", None)].vram_mib == {}
    assert "qwen3local could not be measured" in caplog.text and "gone" in caplog.text


def test_idle_remaining_comes_from_the_health_answer_of_a_running_container_engine_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite, _ = _suite(
        make_config(tmp_path), FakeManagedLauncher(statuses={"xtts": "running", "qwen3local": "stopped", "edge": "running"}),
    )
    monkeypatch.setattr(
        registry.TTS_ENGINES["xtts"].__class__, "_health", lambda self, timeout=None: {"idle_remaining_s": 287.5},
    )
    rows = _rows(run_async(suite.engines()))
    assert rows[("xtts", None)].idle_remaining_s == 287.5
    assert rows[("qwen3local", None)].idle_remaining_s is None  # stopped: nothing is counting down
    assert rows[("edge", None)].idle_remaining_s is None  # no container: nothing ends itself


def test_idle_remaining_is_none_when_the_container_has_auto_shutdown_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite, _ = _suite(make_config(tmp_path), FakeManagedLauncher(statuses={"xtts": "running"}))
    monkeypatch.setattr(
        registry.TTS_ENGINES["xtts"].__class__, "_health", lambda self, timeout=None: {"idle_remaining_s": None},
    )
    assert _rows(run_async(suite.engines()))[("xtts", None)].idle_remaining_s is None


# ── Voices ──────────────────────────────────────────────────────────


def test_voices_without_an_engine_are_the_folders_of_the_shared_directory(tmp_path: Path) -> None:
    for name in ("Bob", "Alice"):
        (tmp_path / name).mkdir()
        (tmp_path / name / f"{name}.wav").write_bytes(b"x")
    (tmp_path / "Stray").mkdir()  # a folder without its <name>.wav is no voice
    suite, _ = _suite(make_config(tmp_path))
    assert run_async(suite.voices()) == [VoiceInfo("Alice", "Alice"), VoiceInfo("Bob", "Bob")]


def test_the_voices_of_an_engine_are_its_live_ones_else_its_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live, silent = FakeEngine("live"), FakeEngine("silent")
    monkeypatch.setattr(live, "get_voices", lambda: {"Katja": "k1", "Conrad": "c1"})
    monkeypatch.setattr(type(silent), "voices_fallback", {"Static": "s"})
    install(monkeypatch, live, silent)
    suite, _ = _suite(make_config(tmp_path))
    assert run_async(suite.voices("live")) == [VoiceInfo("Katja", "Katja"), VoiceInfo("Conrad", "Conrad")]
    assert run_async(suite.voices("silent")) == [VoiceInfo("Static", "Static")]
    with pytest.raises(UnknownEngine):
        run_async(suite.voices("nope"))


def test_sync_voices_has_the_engine_prepare_them_and_returns_the_voices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = FakeEngine("cloudfake", cloud=True)
    prepared: list[str] = []

    def prepare() -> object:
        prepared.append("done")
        yield "enrolling"

    monkeypatch.setattr(cloud, "prepare_voices", prepare)
    monkeypatch.setattr(cloud, "get_voices", lambda: {"Alice": "a"})
    install(monkeypatch, cloud)
    suite, _ = _suite(make_config(tmp_path))
    assert run_async(suite.sync_voices("cloudfake")) == [VoiceInfo("Alice", "Alice")] and prepared == ["done"]


# ── Starting, stopping, reloading ───────────────────────────────────


def test_start_does_nothing_for_a_serving_row_and_starts_a_stopped_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    serving, stopped = FakeEngine("servingfake"), FakeEngine("stoppedfake")
    install(monkeypatch, serving, stopped)
    launcher = FakeManagedLauncher(down={"stoppedfake": None})
    suite, _ = _suite(make_config(tmp_path), launcher)

    async def scenario() -> None:
        await suite.start("servingfake")
        assert launcher.starts == []
        await suite.start("stoppedfake")

    run_async(scenario())
    assert launcher.starts == ["stoppedfake"]
    assert launcher.cleared == ["servingfake", "stoppedfake"]  # an explicit start forgets earlier failures


def test_a_start_that_does_not_work_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("novram"))
    launcher = FakeManagedLauncher(down={"novram": Skip("no_vram", "needs 4512 MiB, most free 900 MiB on GPU 1")})
    suite, _ = _suite(make_config(tmp_path), launcher)
    with pytest.raises(EngineControlFailed, match=r"novram not started \(no_vram\): needs 4512 MiB, most free 900 MiB on GPU 1"):
        run_async(suite.start("novram"))


def test_start_and_stop_work_on_any_catalog_row_without_a_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local, remote = ContainerEngine("lo"), ContainerEngine("re")
    remote.default_port = 5000
    install(monkeypatch, local, remote)
    suite, launcher = _suite(make_config(tmp_path, hosts=(ARAGON,)), FakeManagedLauncher(down={"re@Aragon": None}))

    async def scenario() -> None:
        await suite.stop("lo")
        await suite.stop("re", "Aragon")
        await suite.start("re", "Aragon")

    run_async(scenario())
    assert launcher.stopped == ["lo", "re@Aragon"] and launcher.starts == ["re@Aragon"]


def test_a_start_on_a_named_card_goes_to_the_launcher_only_for_a_local_gpu_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gpu, cpu, remote = ContainerEngine("lo"), FakeEngine("cpufake"), ContainerEngine("re")
    remote.default_port = 5000
    install(monkeypatch, gpu, cpu, remote)
    suite, launcher = _suite(make_config(tmp_path, hosts=(ARAGON,)))

    async def scenario() -> None:
        await suite.start("lo", gpu="4")
        with pytest.raises(InvalidRequest, match="only be named for a local GPU engine"):
            await suite.start("cpufake", gpu="4")
        with pytest.raises(InvalidRequest, match="only be named for a local GPU engine"):
            await suite.start("re", "Aragon", gpu="4")

    run_async(scenario())
    assert launcher.started_on_gpu == [("lo", "4")] and launcher.starts == []


def test_start_and_stop_refuse_what_is_not_a_catalog_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, ContainerEngine("lo"))  # no REST API: it has no row on a host
    suite, launcher = _suite(make_config(tmp_path, hosts=(ARAGON,)))

    async def scenario() -> None:
        for call in (suite.stop, suite.start):
            with pytest.raises(UnknownEngine):
                await call("nope", None)
            with pytest.raises(UnknownHost):
                await call("xtts", "Ghost")
            with pytest.raises(InvalidRequest, match="no REST API"):
                await call("lo", "Aragon")

    run_async(scenario())
    assert launcher.stopped == [] and launcher.starts == []


def test_a_container_in_use_cannot_be_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, ContainerEngine("busy"))
    launcher = FakeManagedLauncher()
    launcher.stop_error = EngineInUse("busy is in use by a reading")
    suite, _ = _suite(make_config(tmp_path), launcher)
    with pytest.raises(EngineInUse):
        run_async(suite.stop("busy"))


# ── Release on request ──────────────────────────────────────────────


class SlowStopLauncher(FakeManagedLauncher):
    """Stops take ``stop_delay_s`` per label; a failing stop raises ``stop_errors[label]``."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stop_delay_s: dict[str, float] = {}
        self.stop_errors: dict[str, Exception] = {}

    async def stop(self, entry: Any, *, force: bool = False) -> None:
        await asyncio.sleep(self.stop_delay_s.get(entry.label, 0.0))
        if entry.label in self.stop_errors:
            raise self.stop_errors[entry.label]
        await super().stop(entry, force=force)


def _release_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **launcher_args: Any) -> tuple[Suite, SlowStopLauncher]:
    """Local GPU engines ``lo`` and ``re``; on the host Aragon ``re``, ``up``, ``held``, ``broken`` and ``idle``."""
    install(monkeypatch, ContainerEngine("lo"), FakeEngine("nocontainer"))
    for key in ("re", "up", "held", "broken", "idle"):
        engine = ContainerEngine(key)
        engine.default_port = 5000
        install(monkeypatch, engine)
    return _suite(make_config(tmp_path, hosts=(ARAGON,)), SlowStopLauncher(**launcher_args))  # type: ignore[return-value]


def test_release_of_host_containers_reports_every_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch, statuses={"re@Aragon": "unreachable", "idle@Aragon": "stopped"})
    launcher.stop_errors = {
        "held@Aragon": EngineInUse("held@Aragon is in use by a reading"), "broken@Aragon": EngineControlFailed("broken: boom"),
    }
    targets = tuple(EngineTarget(key, "Aragon") for key in ("up", "re", "held", "broken", "idle"))
    results = run_async(suite.release(targets))
    assert [(r.engine, r.host, r.outcome) for r in results] == [
        ("up", "Aragon", "stopped"), ("re", "Aragon", "unreachable"), ("held", "Aragon", "in_use"),
        ("broken", "Aragon", "failed"), ("idle", "Aragon", "not_running"),
    ]
    assert results[2].detail == "held@Aragon is in use by a reading" and results[3].detail == "broken: boom"
    assert launcher.stopped == ["up@Aragon"]  # nothing else was stopped, the unreachable host was not even tried


def test_release_refuses_engines_on_this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    with pytest.raises(InvalidRequest, match="gpu_release"):
        run_async(suite.release((EngineTarget("up", "Aragon"), EngineTarget("lo", None))))
    assert launcher.stopped == []


def test_release_stops_a_host_container_over_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = ContainerEngine("re", remote=True)
    remote.default_port = 5000
    remote.up = True
    install(monkeypatch, remote)
    ssh = FakeSsh()
    config = make_config(tmp_path, hosts=(ARAGON,))
    suite, _ = _suite(config, SuiteLauncher(config, gpus=list, host_command=ssh))  # type: ignore[arg-type]
    results = run_async(suite.release((EngineTarget("re", "Aragon"),)))
    assert results[0].outcome == "stopped" and [c[:2] for c in ssh.commands] == [("Aragon", "stop")]


def test_release_all_takes_the_container_rows_of_the_hosts_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    results = run_async(suite.release("all"))
    assert {r.host for r in results} == {"Aragon"} and {r.outcome for r in results} == {"stopped"}
    assert {"re@Aragon", "up@Aragon"} <= set(launcher.stopped) and "lo" not in launcher.stopped


def test_a_bad_release_request_stops_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    up = EngineTarget("up", "Aragon")
    bad: list[tuple[type[Exception], tuple[EngineTarget, ...]]] = [
        (InvalidRequest, ()),
        (InvalidRequest, (up, up)),
        (InvalidRequest, (up, EngineTarget("lo", None))),
        (UnknownEngine, (up, EngineTarget("nope", "Aragon"))),
        (UnknownHost, (up, EngineTarget("re", "Ghost"))),
    ]
    for error, targets in bad:
        with pytest.raises(error):
            run_async(suite.release(targets))
    assert launcher.stopped == []


def test_release_handles_the_targets_in_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    launcher.stop_delay_s = {"re@Aragon": 0.4}  # a slow host
    started = time.monotonic()
    results = run_async(suite.release(tuple(EngineTarget(key, "Aragon") for key in ("re", "up", "held"))))
    assert time.monotonic() - started < 0.7  # one delay, not a sum of three
    assert [r.outcome for r in results] == ["stopped"] * 3
    assert launcher.stopped[-1] == "re@Aragon"  # the fast ones finished first


# ── The common GPU release API ──────────────────────────────────────


def test_gpu_holders_are_the_local_gpu_engines_whose_container_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(suite_module, "engine_vram_mib", lambda engine: {"GPU-1": 3000} if engine.key == "lo" else {})
    launcher.cards = {"lo": ("GPU-1",), "re": ("GPU-0", "GPU-1"), "re@Aragon": ("GPU-9",)}
    launcher.busy_reasons = {"lo": "in_use"}
    holders = run_async(suite.gpu_holders())
    assert holders == [
        GpuHolder("lo", ("GPU-1",), {"GPU-1": 3000}, True),
        GpuHolder("re", ("GPU-0", "GPU-1"), None, False),  # nothing measured: memory unknown, not empty
    ]


def test_gpu_release_reports_every_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    for key in ("held", "starting", "broken", "free"):
        install(monkeypatch, ContainerEngine(key))
    launcher.cards = {key: ("GPU-0",) for key in ("held", "starting", "broken", "free")}
    launcher.busy_reasons = {"held": "in_use", "starting": "starting"}
    launcher.stop_errors = {"broken": EngineControlFailed("broken: container still running after docker kill")}
    results = run_async(suite.gpu_release(("held", "starting", "broken", "free", "lo"), force=False, reason="loading X"))
    assert [(r.item, r.outcome) for r in results] == [
        ("held", "busy"), ("starting", "busy"), ("broken", "failed"), ("free", "released"), ("lo", "not_running"),
    ]
    assert "docker kill" in results[2].detail
    assert launcher.stopped == ["free"] and launcher.forced == []


def test_forced_gpu_release_takes_a_held_and_a_starting_engine_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    for key in ("held", "starting"):
        install(monkeypatch, ContainerEngine(key))
    launcher.cards = {"held": ("GPU-0",), "starting": ("GPU-1",)}
    launcher.busy_reasons = {"held": "in_use", "starting": "starting"}
    results = run_async(suite.gpu_release(("held", "starting"), force=True, reason="loading X"))
    assert [(r.item, r.outcome) for r in results] == [("held", "released"), ("starting", "released")]
    assert launcher.withdrawn == [("starting", "loading X")]  # the start first, then the stop
    assert launcher.forced == ["held", "starting"]


def test_a_forced_release_also_ends_a_reading_that_holds_an_engine_while_it_starts_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its container went away under the reading, a second reading starts it again: busy says "starting".
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    install(monkeypatch, ContainerEngine("starting"))
    launcher.cards = {"starting": ("GPU-0",)}
    launcher.busy_reasons = {"starting": "starting"}
    aborted: list[tuple[str, str]] = []

    def abort_readings(label: str, reason: str) -> int:
        aborted.append((label, reason))
        return 1

    monkeypatch.setattr(suite._speaker, "abort_readings", abort_readings)
    results = run_async(suite.gpu_release(("starting",), force=True, reason="loading X"))
    assert [(r.item, r.outcome) for r in results] == [("starting", "released")]
    assert launcher.withdrawn == [("starting", "loading X")] and aborted == [("starting", "loading X")]


def test_an_explicit_start_that_a_forced_release_withdraws_is_a_failed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)

    async def start_on_gpu(entry: EscalationEntry, gpu: str) -> Skip | None:
        raise ReadingAborted("loading X")

    monkeypatch.setattr(launcher, "start_on_gpu", start_on_gpu)
    with pytest.raises(EngineControlFailed, match=r"lo not started \(withdrawn\): the GPU is needed: loading X"):
        run_async(suite.start("lo", gpu="0"))


def test_gpu_release_all_takes_the_holders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    launcher.cards = {"lo": ("GPU-0",)}
    results = run_async(suite.gpu_release("all", force=False, reason="r"))
    assert [(r.item, r.outcome) for r in results] == [("lo", "released")]


@pytest.mark.parametrize("items, reason", [
    ((), "r"), (("lo", "lo"), "r"), (("nope",), "r"), (("nocontainer",), "r"), (("lo",), " "),
])
def test_a_bad_gpu_release_stops_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, items: tuple[str, ...], reason: str) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    launcher.cards = {"lo": ("GPU-0",)}
    with pytest.raises(InvalidRequest):
        run_async(suite.gpu_release(items, force=True, reason=reason))
    assert launcher.stopped == []


async def _collect_until_error(stream: Any) -> Exception | None:
    """Read a stream to its end; the error it ended with, ``None`` when it ended normally."""
    try:
        async for _segment in stream.segments():
            pass
    except Exception as error:  # noqa: BLE001 — the test looks at which error it was
        return error
    return None


def test_a_forced_gpu_release_ends_the_reading_with_its_reason_and_proves_the_container_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = ContainerEngine("a")
    engine.up = True
    for key in list(registry.TTS_ENGINES):  # only the fake: the registry's own engines would ask Docker
        monkeypatch.delitem(registry.TTS_ENGINES, key)
    install(monkeypatch, engine)
    config = make_config(tmp_path)
    launcher = SuiteLauncher(config, gpus=CardList(20000), host_command=FakeSsh())
    suite = Suite(config, launcher)

    async def scenario() -> tuple[GpuReleaseResult, ...]:
        gate = engine.gates["zwei"] = asyncio.Event()
        stream = suite.speak("Der Satz mit zwei Wörtern kommt hier.", request("a"))
        reading = asyncio.ensure_future(_collect_until_error(stream))
        await asyncio.sleep(0.05)
        assert await suite.gpu_holders() == [GpuHolder("a", ("GPU-0",), None, True)]  # the reading holds it
        results = await suite.gpu_release(("a",), force=True, reason="GPU-Orc: loading X")
        gate.set()  # the synthesis that was running comes back after the container went down
        error = await reading
        assert isinstance(error, ReadingAborted) and "GPU-Orc: loading X" in str(error)
        await launcher.aclose()
        return results

    results = run_async(scenario())
    assert [(r.item, r.outcome) for r in results] == [("a", "released")]
    assert engine.stops == 1 and engine.kills == 0 and not engine.up


def test_a_forced_gpu_release_withdraws_a_start_and_ends_the_readings_waiting_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Found live (25.10.): XTTS needs ~30 s to load; a forced release in that time stayed "busy".
    engine = ContainerEngine("a", ready_delay_s=30)
    for key in list(registry.TTS_ENGINES):  # only the fake: the registry's own engines would ask Docker
        monkeypatch.delitem(registry.TTS_ENGINES, key)
    install(monkeypatch, engine)
    config = make_config(tmp_path)
    launcher = SuiteLauncher(config, gpus=CardList(20000), host_command=FakeSsh())
    suite = Suite(config, launcher)

    async def scenario() -> tuple[tuple[GpuReleaseResult, ...], float, list[Exception | None]]:
        # Two streams of one reading (a run ID): the second waits for the selection the first one makes.
        readings = [
            asyncio.ensure_future(_collect_until_error(suite.speak(text, request("a", run="r1"))))
            for text in ("Ein Satz.", "Noch einer.")
        ]
        await asyncio.sleep(0.05)
        assert await suite.gpu_holders() == [GpuHolder("a", ("GPU-0",), None, True)]  # its start counts as busy
        started = time.monotonic()
        results = await suite.gpu_release(("a",), force=True, reason="GPU-Orc: loading X")
        took = time.monotonic() - started
        errors = [await reading for reading in readings]
        await launcher.aclose()
        return results, took, errors

    results, took, errors = run_async(scenario())
    assert [(r.item, r.outcome) for r in results] == [("a", "released")]
    assert took < 5  # not the 30 s the model would load
    for error in errors:  # the second one did not start the engine again
        assert isinstance(error, ReadingAborted) and error.reason == "GPU-Orc: loading X"
    assert engine.ready_calls == 1 and engine.kills == 1 and not engine.up and not engine.loading
    assert launcher.start_failure(EscalationEntry(engine, None, engine.default_speech_unit)) is None  # not a failure


def test_reload_rereads_the_engine_folders_and_binds_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def reload_engines() -> tuple[list[str], list[str]]:
        calls.append("reload")
        return ["gone"], ["new"]

    monkeypatch.setattr(suite_module, "reload_engines", reload_engines)
    suite, launcher = _suite(make_config(tmp_path))
    suite._speaker.entry_for("xtts", None, "sentence")
    assert suite._speaker._engines != {}
    assert run_async(suite.reload()) == (["gone"], ["new"]) and calls == ["reload"]
    assert launcher.cleared == [None] and suite._speaker._engines == {}


def test_reload_with_a_deleted_engine_folder_that_the_defaults_name_is_a_config_error_and_keeps_the_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeEngine("vanishing"))
    config = make_config(tmp_path, {"vanishing": EngineDefaults()})
    suite, _ = _suite(config)

    def reload_engines() -> tuple[list[str], list[str]]:
        del registry.TTS_ENGINES["vanishing"]
        return ["vanishing"], []

    monkeypatch.setattr(suite_module, "reload_engines", reload_engines)
    with pytest.raises(ConfigInvalid, match="unknown TTS engine 'vanishing'"):
        run_async(suite.reload())
    assert suite._speaker.config is config


# ── Speaking and closing ────────────────────────────────────────────


def test_speak_and_open_stream_deliver_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("fake"))
    suite, _ = _suite(make_config(tmp_path))
    head = request("fake")

    async def scenario() -> tuple[int, int]:
        whole = [segment async for segment in suite.speak("Hallo Welt hier.", head).segments()]
        stream = suite.open_stream(head)
        await stream.feed("Und noch ein Satz hier.")
        await stream.finish()
        piecewise = [segment async for segment in stream.segments()]
        return len(whole), len(piecewise)

    assert run_async(scenario()) == (1, 1)


def test_closing_the_suite_closes_the_launcher(tmp_path: Path) -> None:
    suite, launcher = _suite(make_config(tmp_path))
    run_async(suite.aclose())
    assert launcher.closed


def test_closing_the_suite_stops_every_container_also_a_held_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    held, idle = ContainerEngine("held"), ContainerEngine("idle")
    held.up = True
    idle.up = True
    install(monkeypatch, held, idle)
    config = make_config(tmp_path)
    launcher = SuiteLauncher(config, gpus=list)
    suite, _ = _suite(config, launcher)  # type: ignore[arg-type]

    async def scenario() -> None:
        launcher.hold(EscalationEntry(held, None, "sentence"))  # a reading is using it
        await suite.aclose()

    run_async(scenario())
    assert held.stops == 1 and idle.stops == 1


# ── The voices folder: new path releases the containers, push to the hosts ──


def test_a_new_voices_folder_releases_the_running_containers_and_the_same_one_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    run_async(suite.set_config(make_config(tmp_path, hosts=(ARAGON,), headroom_mib=100)))
    assert launcher.stopped == []  # same voices_dir: nothing is released
    run_async(suite.set_config(make_config(other, hosts=(ARAGON,))))
    assert {"lo", "re", "re@Aragon"} <= set(launcher.stopped)


def test_a_container_a_reading_holds_keeps_the_old_voices_folder_and_it_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    suite, launcher = _release_setup(tmp_path, monkeypatch)
    launcher.stop_errors["lo"] = EngineInUse("lo is in use by a reading")
    other = tmp_path / "other"
    other.mkdir()
    run_async(suite.set_config(make_config(other, hosts=(ARAGON,))))
    assert "lo keeps the old voices folder (in_use)" in caplog.text


def test_the_voices_go_to_every_host_as_a_tar_of_the_folder(tmp_path: Path) -> None:
    import io
    import tarfile

    (tmp_path / "Alice").mkdir()
    (tmp_path / "Alice" / "Alice.wav").write_bytes(b"wav")
    ssh = FakeSsh()
    config = make_config(tmp_path, hosts=(ARAGON, Host("Basti", "192.0.2.3")))
    suite, _ = _suite(config, SuiteLauncher(config, gpus=list, host_command=ssh))  # type: ignore[arg-type]
    assert run_async(suite.push_voices()) == ["Aragon", "Basti"]
    assert ssh.commands == [("Aragon", "receive-voices"), ("Basti", "receive-voices")]
    with tarfile.open(fileobj=io.BytesIO(ssh.inputs[0])) as archive:
        assert "./Alice/Alice.wav" in archive.getnames()


def test_a_host_that_refuses_the_voices_is_an_engine_control_failure(tmp_path: Path) -> None:
    config = make_config(tmp_path, hosts=(ARAGON,))
    suite, _ = _suite(config, SuiteLauncher(config, gpus=list, host_command=FakeSsh(fail="no route")))  # type: ignore[arg-type]
    with pytest.raises(EngineControlFailed, match="no route"):
        run_async(suite.push_voices())
