"""The Suite: configuration, the list of engines, voices, start/stop, reload, speaking."""
from __future__ import annotations

from pathlib import Path

import pytest

from container_fakes import ARAGON, ContainerEngine, FakeManagedLauncher, install, make_config
from speech_fakes import FakeEngine, run_async

from tts_suite import escalation, suite as suite_module
from tts_suite.api import (
    ConfigInvalid,
    EngineControlFailed,
    EngineDefaults,
    EngineInUse,
    EntryConfig,
    SpeakRequest,
    SuiteConfig,
    TTSSuite,
    UnknownEngine,
    VoiceInfo,
)
from tts_suite.escalation import Skip
from tts_suite.suite import Suite, create_suite


def _suite(config: SuiteConfig, launcher: FakeManagedLauncher | None = None) -> tuple[Suite, FakeManagedLauncher]:
    launcher = launcher or FakeManagedLauncher()
    suite: TTSSuite = create_suite(config, launcher=launcher)  # also type-checks that Suite implements the Protocol
    assert isinstance(suite, Suite)
    return suite, launcher


# ── Configuration ───────────────────────────────────────────────────


def test_the_configuration_is_replaced_for_the_speaker_and_the_launcher(tmp_path: Path) -> None:
    suite, launcher = _suite(make_config(tmp_path, ["xtts"]))
    new = make_config(tmp_path, ["edge"], headroom_mib=100)

    async def scenario() -> SuiteConfig:
        await suite.set_config(new)
        return await suite.config()

    assert run_async(scenario()) is new and launcher.configs == [new]
    assert [entry.engine.key for entry in suite._speaker.entries] == ["edge"]


def test_an_invalid_configuration_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path, ["xtts"])
    suite, launcher = _suite(config)

    async def scenario() -> None:
        with pytest.raises(ConfigInvalid, match="unknown TTS engine"):
            await suite.set_config(make_config(tmp_path, ["nope"]))
        with pytest.raises(ConfigInvalid, match="voices_dir"):
            await suite.set_config(make_config(tmp_path / "missing", ["xtts"]))
        assert await suite.config() is config

    run_async(scenario())
    assert launcher.configs == []


def test_a_broken_configuration_cannot_create_a_suite(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid):
        create_suite(make_config(tmp_path, ["nope"]), launcher=FakeManagedLauncher())


# ── The list of engines ─────────────────────────────────────────────


def test_engines_lists_the_entries_in_order_with_everything_a_surface_needs(tmp_path: Path) -> None:
    config = SuiteConfig(
        voices_dir=tmp_path,
        entries=(EntryConfig("xtts"), EntryConfig("edge", enabled=False), EntryConfig("browser")),
        engines={"edge": EngineDefaults(unit="whole")},
    )
    suite, _ = _suite(config, FakeManagedLauncher(statuses={"xtts": "stopped", "edge": "running", "browser": "running"}))
    xtts, edge, browser = run_async(suite.engines())
    assert [info.engine for info in (xtts, edge, browser)] == ["xtts", "edge", "browser"]
    assert (xtts.host, xtts.status, xtts.enabled, xtts.needs_gpu, xtts.cloud, xtts.browser, xtts.deactivated) == (
        None, "stopped", True, True, False, False, False,
    )
    assert set(xtts.label) == set(xtts.label_short) == {"de", "en"} and all(xtts.label.values())
    assert xtts.label_short["de"] == "XTTS" and xtts.unit == xtts.unit_default
    assert (edge.enabled, edge.cloud, edge.status, edge.unit, edge.unit_default) == (False, True, "running", "whole", "sentence")
    assert browser.browser and not browser.needs_gpu


def test_a_remote_entry_names_its_host_and_a_deactivated_one_is_listed_as_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(escalation, "is_deactivated", lambda key: key == "ghost")
    config = SuiteConfig(
        voices_dir=tmp_path, hosts=(ARAGON,),
        entries=(EntryConfig("qwen3local", host="Aragon"), EntryConfig("ghost")),
    )
    suite, _ = _suite(config, FakeManagedLauncher(statuses={"qwen3local@Aragon": "unreachable"}))
    remote, ghost = run_async(suite.engines())
    assert (remote.host, remote.status) == ("Aragon", "unreachable")
    assert (ghost.deactivated, ghost.enabled, ghost.status, ghost.label["de"]) == (True, False, "stopped", "ghost")


# ── Voices ──────────────────────────────────────────────────────────


def test_voices_without_an_engine_are_the_folders_of_the_shared_directory(tmp_path: Path) -> None:
    for name in ("Bob", "Alice"):
        (tmp_path / name).mkdir()
        (tmp_path / name / f"{name}.wav").write_bytes(b"x")
    (tmp_path / "Stray").mkdir()  # a folder without its <name>.wav is no voice
    suite, _ = _suite(make_config(tmp_path, []))
    assert run_async(suite.voices()) == [VoiceInfo("Alice", "Alice"), VoiceInfo("Bob", "Bob")]


def test_the_voices_of_an_engine_are_its_live_ones_else_its_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live, silent = FakeEngine("live"), FakeEngine("silent")
    monkeypatch.setattr(live, "get_voices", lambda: {"Katja": "k1", "Conrad": "c1"})
    monkeypatch.setattr(type(silent), "voices_fallback", {"Static": "s"})
    install(monkeypatch, live, silent)
    suite, _ = _suite(make_config(tmp_path, []))
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
    suite, _ = _suite(make_config(tmp_path, []))
    assert run_async(suite.sync_voices("cloudfake")) == [VoiceInfo("Alice", "Alice")] and prepared == ["done"]


# ── Starting, stopping, reloading ───────────────────────────────────


def test_start_does_nothing_for_a_serving_entry_and_starts_a_stopped_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    serving, stopped = FakeEngine("servingfake"), FakeEngine("stoppedfake")
    install(monkeypatch, serving, stopped)
    launcher = FakeManagedLauncher(down={"stoppedfake": None})
    suite, _ = _suite(make_config(tmp_path, ["servingfake", "stoppedfake"]), launcher)

    async def scenario() -> None:
        await suite.start("servingfake")
        assert launcher.starts == []
        await suite.start("stoppedfake")

    run_async(scenario())
    assert launcher.starts == ["stoppedfake"]


def test_a_start_that_does_not_work_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("novram"))
    launcher = FakeManagedLauncher(down={"novram": Skip("no_vram", "needs 4512 MiB, most free 900 MiB on GPU 1")})
    suite, _ = _suite(make_config(tmp_path, ["novram"]), launcher)
    with pytest.raises(EngineControlFailed, match=r"novram not started \(no_vram\): needs 4512 MiB, most free 900 MiB on GPU 1"):
        run_async(suite.start("novram"))


def test_start_and_stop_address_an_entry_of_the_list_by_engine_and_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local, remote = ContainerEngine("lo"), ContainerEngine("re", remote=True)
    install(monkeypatch, local, remote)
    config = SuiteConfig(
        voices_dir=tmp_path, hosts=(ARAGON,), entries=(EntryConfig("lo"), EntryConfig("re", host="Aragon")),
    )
    monkeypatch.setattr(ContainerEngine, "at_host", lambda self, address, port=None: self)
    suite, launcher = _suite(config)

    async def scenario() -> None:
        await suite.stop("lo")
        await suite.stop("re", "Aragon")
        for engine, host in (("nope", None), ("lo", "Aragon"), ("xtts", None)):
            with pytest.raises(UnknownEngine):
                await suite.stop(engine, host)
            with pytest.raises(UnknownEngine):
                await suite.start(engine, host)

    run_async(scenario())
    assert launcher.stopped == ["lo", "re@Aragon"]


def test_a_container_in_use_cannot_be_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, ContainerEngine("busy"))
    launcher = FakeManagedLauncher()
    launcher.stop_error = EngineInUse("busy is in use by a reading")
    suite, _ = _suite(make_config(tmp_path, ["busy"]), launcher)
    with pytest.raises(EngineInUse):
        run_async(suite.stop("busy"))


def test_reload_rereads_the_engine_folders_and_binds_the_list_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def reload_engines() -> tuple[list[str], list[str]]:
        calls.append("reload")
        return ["gone"], ["new"]

    monkeypatch.setattr(suite_module, "reload_engines", reload_engines)
    suite, _ = _suite(make_config(tmp_path, ["xtts"]))
    bound_before = suite._speaker.entries
    assert run_async(suite.reload()) == (["gone"], ["new"]) and calls == ["reload"]
    assert suite._speaker.entries is not bound_before and [e.engine.key for e in suite._speaker.entries] == ["xtts"]


# ── Speaking and closing ────────────────────────────────────────────


def test_speak_and_open_stream_deliver_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("fake"))
    suite, _ = _suite(make_config(tmp_path, ["fake"]))
    request = SpeakRequest(language="de")

    async def scenario() -> tuple[int, int]:
        whole = [segment async for segment in suite.speak("Hallo Welt hier.", request).segments()]
        stream = suite.open_stream(request)
        await stream.feed("Und noch ein Satz hier.")
        await stream.finish()
        piecewise = [segment async for segment in stream.segments()]
        return len(whole), len(piecewise)

    assert run_async(scenario()) == (1, 1)


def test_closing_the_suite_closes_the_launcher(tmp_path: Path) -> None:
    suite, launcher = _suite(make_config(tmp_path, []))
    run_async(suite.aclose())
    assert launcher.closed
