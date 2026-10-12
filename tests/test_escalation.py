"""Escalation: binding, checking the configuration, selection and skips, failover, slots, voices, the table of runs."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from speech_fakes import (
    FakeEngine,
    FakeLauncher,
    GatedFailEngine,
    entry,
    frames_for,
    make_run,
    request,
    run_async,
    wav_info,
)

from tts_suite import escalation
from tts_suite.api import (
    ConfigInvalid,
    EngineDefaults,
    EngineRef,
    EngineStarting,
    GpuRule,
    Host,
    InvalidRequest,
    NoEngineAvailable,
    Overloaded,
    SpeakRequest,
    SuiteConfig,
    TraceStep,
    UnknownEngine,
    VoiceSettings,
)
from tts_suite.engines import get_engine
from tts_suite.escalation import (
    EngineSlots,
    RunTable,
    Skip,
    bind_engine,
    check_config,
    entry_label,
    resolve_voice,
)

ARAGON = Host("Aragon", "192.0.2.2")
REQUEST = request("a")


def _config(tmp_path: Path, **kwargs: object) -> SuiteConfig:
    return SuiteConfig(voices_dir=tmp_path, **kwargs)  # type: ignore[arg-type]


# ── Binding ─────────────────────────────────────────────────────────


def test_a_local_engine_is_the_registry_engine_bound_to_the_voices(tmp_path: Path) -> None:
    bound = bind_engine("xtts", None, tmp_path)
    assert bound.key == "xtts" and not bound.is_remote and bound.shared_voices_dir == tmp_path


def test_a_remote_engine_is_bound_to_its_host_and_keeps_the_registry_engine_local(tmp_path: Path) -> None:
    bound = bind_engine("qwen3local", ARAGON, tmp_path)
    assert bound.service_url == "http://192.0.2.2:5052"
    assert get_engine("qwen3local").service_url == "http://localhost:5052"  # type: ignore[union-attr]


def test_an_engine_of_a_deactivated_folder_is_a_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(escalation, "is_deactivated", lambda key: key == "ghost")
    assert bind_engine("ghost", None, tmp_path).deactivated


def test_an_unknown_engine_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UnknownEngine, match="unknown TTS engine"):
        bind_engine("nope", None, tmp_path)


def test_an_engine_without_rest_api_cannot_be_bound_to_a_host(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="no REST API"):
        bind_engine("piper", ARAGON, tmp_path)


def test_the_entry_label_names_the_host() -> None:
    assert entry_label("xtts", None) == "xtts" and entry_label("xtts", ARAGON) == "xtts@Aragon"


# ── Checking the configuration ─────────────────────────────────────


def test_the_hosts_of_a_valid_configuration_are_indexed_by_name(tmp_path: Path) -> None:
    assert check_config(_config(tmp_path, hosts=(ARAGON,))) == {"Aragon": ARAGON}


@pytest.mark.parametrize(
    ("hosts", "message"),
    [
        ((ARAGON, Host("Aragon", "10.0.0.3")), "duplicate host"),
        ((Host("Evil", "-oProxyCommand=x"),), "not a host name"),
        ((Host("Evil", "192.0.2.9 -x"),), "not a host name"),
        ((Host("Evil", "192.0.2.9", ssh_user="-oX"),), "not a user name"),
    ],
)
def test_a_bad_host_is_refused(tmp_path: Path, hosts: tuple[Host, ...], message: str) -> None:
    with pytest.raises(ConfigInvalid, match=message):
        check_config(_config(tmp_path, hosts=hosts))


def test_an_empty_pronunciation_word_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid, match="pronunciations"):
        check_config(_config(tmp_path, pronunciations={" ": "x"}))


def test_the_list_mode_needs_a_card(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid, match="gpu_mode list"):
        check_config(_config(tmp_path, gpu_mode="list"))
    check_config(_config(tmp_path, gpu_mode="list", gpus=(GpuRule("0"),)))


def test_a_pronunciation_word_twice_in_different_case_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid, match="twice"):
        check_config(_config(tmp_path, pronunciations={"AIfred": "Alfred", "aifred": "Alfred"}))


def test_a_missing_voices_dir_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid, match="voices_dir"):
        check_config(SuiteConfig(voices_dir=tmp_path / "missing"))


@pytest.mark.parametrize(
    ("engines", "message"),
    [
        ({"nope": EngineDefaults()}, "unknown TTS engine"),
        ({"xtts": EngineDefaults(max_parallel=0)}, "max_parallel"),
        ({"xtts": EngineDefaults(max_queue=-1)}, "max_queue"),
    ],
)
def test_bad_engine_defaults_are_refused(tmp_path: Path, engines: dict[str, EngineDefaults], message: str) -> None:
    with pytest.raises(ConfigInvalid, match=message):
        check_config(_config(tmp_path, engines=engines))


# ── Voice ───────────────────────────────────────────────────────────


def _voice(engine: FakeEngine, head: SpeakRequest, defaults: dict[str, EngineDefaults]) -> tuple[str, float, float]:
    return resolve_voice(engine, head, defaults)


def test_the_voice_comes_from_the_engine_setting_then_the_request_then_the_defaults_then_the_engine() -> None:
    engine = FakeEngine("x")
    engine.runs_in_container = True
    defaults = {"x": EngineDefaults(voice=VoiceSettings("Config", 1.1, 1.2))}
    head = request("x", voice="Request", speed=1.5, voices={"x": VoiceSettings(voice="Own")})
    assert _voice(engine, head, defaults) == ("Own", 1.5, 1.2)       # voice: own; speed: request; pitch: config
    assert _voice(engine, request("x", voice="Request"), defaults) == ("Request", 1.1, 1.2)
    assert _voice(engine, REQUEST, defaults) == ("Config", 1.1, 1.2)
    assert _voice(engine, REQUEST, {}) == ("V", 1.0, 1.0)               # the engine's own voice, neutral speed/pitch


def test_the_request_voice_is_not_offered_to_engines_that_are_not_containers() -> None:
    assert _voice(FakeEngine("edge"), request("edge", voice="Container"), {})[0] == "V"


def test_no_voice_fails_the_entry_not_silently() -> None:
    engine = FakeEngine("x")
    engine.default_voice = None
    run = make_run([entry(engine), entry(FakeEngine("y"))])
    spoken = run_async(run.speak("Hallo Welt hier.", REQUEST))
    assert spoken.entry.engine.key == "y"
    assert run.trace[0] == TraceStep("x", "failed", "software", "no voice configured for x")


# ── Selection and skips ─────────────────────────────────────────────


def test_the_first_entry_that_can_speak_speaks_and_the_skips_are_traced() -> None:
    engines = [FakeEngine(k) for k in ("a", "b", "c", "d", "e")]
    browser = FakeEngine("browser")
    browser.renders_in_browser = True
    deactivated = FakeEngine("gone")
    deactivated.deactivated = True
    launcher = FakeLauncher({"c": Skip("stopped"), "d": Skip("no_vram", "needs 7000 MiB, most free 900 MiB on GPU 1")})
    run = make_run([
        entry(deactivated), entry(browser),
        entry(engines[2]), entry(engines[3]), entry(engines[4]),
    ], launcher)
    spoken = run_async(run.speak("Hallo Welt hier.", REQUEST))
    assert spoken.entry.engine.key == "e" and spoken.audio
    assert run.trace == (
        TraceStep("gone", "skipped", "deactivated"),
        TraceStep("browser", "skipped", "disabled", "speaks only in the client's own browser"),
        TraceStep("c", "skipped", "stopped"),
        TraceStep("d", "skipped", "no_vram", "needs 7000 MiB, most free 900 MiB on GPU 1"),
        TraceStep("e", "spoke"),
    )
    assert launcher.starts == ["c", "d"]


def test_a_stopped_entry_is_started_by_the_launcher_and_then_speaks() -> None:
    launcher = FakeLauncher({"a": None})
    run = make_run([entry(FakeEngine("a")), entry(FakeEngine("b"))], launcher)
    assert run_async(run.speak("Hallo Welt hier.", REQUEST)).entry.engine.key == "a"
    assert launcher.starts == ["a"]


def test_without_waiting_a_cold_start_is_reported() -> None:
    run = make_run([entry(FakeEngine("a"))], FakeLauncher({"a": None}))
    with pytest.raises(EngineStarting):
        run_async(run.speak("Hallo Welt hier.", request("a", wait=False)))


def test_when_nobody_can_speak_the_trace_says_why() -> None:
    browser = FakeEngine("b")
    browser.renders_in_browser = True
    run = make_run([entry(FakeEngine("a")), entry(browser)], FakeLauncher({"a": Skip("stopped")}))
    with pytest.raises(NoEngineAvailable) as error:
        run_async(run.speak("Hallo Welt hier.", REQUEST))
    assert error.value.trace == (
        TraceStep("a", "skipped", "stopped"),
        TraceStep("b", "skipped", "disabled", "speaks only in the client's own browser"),
    )


def test_parallel_sentences_start_a_container_once() -> None:
    launcher = FakeLauncher({"a": None})
    run = make_run([entry(FakeEngine("a"))], launcher)

    async def scenario() -> None:
        await asyncio.gather(*(run.speak(f"Satz Nummer {n} hier.", REQUEST) for n in range(6)))

    run_async(scenario())
    assert launcher.starts == ["a"]


# ── Failover ────────────────────────────────────────────────────────


def test_a_failure_before_the_first_sound_is_silent() -> None:
    broken, healthy = FakeEngine("a", fail="unreachable"), FakeEngine("b")
    run = make_run([entry(broken), entry(healthy)])
    spoken = run_async(run.speak("Erster Satz hier.", REQUEST))
    assert spoken.entry.engine.key == "b"
    assert healthy.texts == ["Erster Satz hier."] and run.switch is None
    assert run.trace == (TraceStep("a", "failed", "unreachable", "a broke"), TraceStep("b", "spoke"))


def test_a_failed_entry_stays_out_for_the_rest_of_the_run() -> None:
    flaky, healthy = FakeEngine("a", fail="engine", fail_after=1), FakeEngine("b")
    run = make_run([entry(flaky), entry(healthy)])

    async def scenario() -> None:
        await run.speak("Eins hier.", REQUEST)
        await run.speak("Zwei hier.", REQUEST)
        flaky.fail = None  # healthy again — but it does not come back in this run
        await run.speak("Drei hier.", REQUEST)

    run_async(scenario())
    assert len(flaky.texts) == 2 and len(healthy.texts) == 2


def test_parallel_sentences_failing_one_after_the_other_report_one_failure() -> None:
    first, second = GatedFailEngine("a"), FakeEngine("b")
    first.max_parallel_requests = 4
    run = make_run([entry(first), entry(second)])

    async def scenario() -> None:
        await run.speak("Erster Satz hier.", REQUEST)
        first.fail_gates = {f"Nummer {n}": asyncio.Event() for n in range(4)}
        tasks = [asyncio.create_task(run.speak(f"Satz Nummer {n} hier.", REQUEST)) for n in range(4)]
        await asyncio.sleep(0.01)
        for gate in first.fail_gates.values():  # staggered, as timeouts are
            gate.set()
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)

    run_async(scenario())
    assert [step.outcome for step in run.trace].count("failed") == 1
    assert len(second.texts) == 4


def test_a_failed_entry_keeps_its_hold_until_the_sentences_still_running_on_it_are_over() -> None:
    first, second = GatedFailEngine("a"), FakeEngine("b")
    first.max_parallel_requests = 3
    launcher = FakeLauncher()
    run = make_run([entry(first), entry(second)], launcher)

    async def scenario() -> None:
        await run.speak("Erster Satz hier.", REQUEST)
        first.gates = {"langsam": asyncio.Event()}
        first.fail_gates = {"kaputt": asyncio.Event()}
        slow = asyncio.create_task(run.speak("Ein langsamer Satz hier.", REQUEST))
        broken = asyncio.create_task(run.speak("Ein kaputter Satz hier.", REQUEST))
        await asyncio.sleep(0.01)
        first.fail_gates["kaputt"].set()
        await broken
        assert launcher.held == ["a", "b"]      # a sentence still runs on a: its container stays held
        first.gates["langsam"].set()
        await slow
        assert launcher.held == ["b"]

    run_async(scenario())


def test_an_unexpected_error_of_an_engine_is_a_software_failure_of_that_entry() -> None:
    class Broken(FakeEngine):
        async def generate_speech_async(self, *args: object, **kwargs: object) -> bytes:
            raise KeyError("boom")

    run = make_run([entry(Broken("a")), entry(FakeEngine("b"))])
    assert run_async(run.speak("Hallo Welt hier.", REQUEST)).entry.engine.key == "b"
    assert run.trace[0].reason == "software" and "KeyError" in run.trace[0].detail


# ── Slots ───────────────────────────────────────────────────────────


def test_requests_beyond_the_slots_and_the_queue_are_refused() -> None:
    slots = EngineSlots(max_parallel=1, max_queue=1)
    order: list[str] = []

    async def hold(name: str, release: asyncio.Event) -> None:
        async with slots.slot():
            order.append(name)
            await release.wait()

    async def scenario() -> None:
        release = asyncio.Event()
        first = asyncio.create_task(hold("first", release))
        await asyncio.sleep(0)
        second = asyncio.create_task(hold("second", release))
        await asyncio.sleep(0)
        with pytest.raises(Overloaded):
            async with slots.slot():
                pytest.fail("a third request must not get in")
        release.set()
        await asyncio.gather(first, second)
        async with slots.slot():  # free again
            order.append("again")

    run_async(scenario())
    assert order == ["first", "second", "again"]


def test_a_slot_is_released_when_the_work_fails() -> None:
    slots = EngineSlots(max_parallel=1, max_queue=0)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async with slots.slot():
                raise RuntimeError("boom")
        async with slots.slot():
            pass

    run_async(scenario())


def test_overload_comes_from_the_speaking_call_not_as_an_entry_failure() -> None:
    engine = FakeEngine("a")
    defaults = {"a": EngineDefaults(max_parallel=1, max_queue=0)}
    run = make_run([entry(engine), entry(FakeEngine("b"))], defaults=defaults)

    async def scenario() -> None:
        engine.gates["halten"] = asyncio.Event()
        busy = asyncio.create_task(run.speak("Bitte halten hier.", REQUEST))
        await asyncio.sleep(0.01)
        with pytest.raises(Overloaded):
            await run.speak("Noch ein Satz hier.", REQUEST)
        engine.gates["halten"].set()
        await busy

    run_async(scenario())
    assert not any(step.outcome == "failed" for step in run.trace)


def test_the_slot_limits_come_from_the_configuration_else_from_the_engine() -> None:
    table = escalation.EngineSlotTable({"a": EngineDefaults(max_parallel=5)})
    assert table.for_engine(FakeEngine("a")).max_parallel == 5
    assert table.for_engine(FakeEngine("b")).max_parallel == FakeEngine.max_parallel_requests
    assert table.for_engine(FakeEngine("a")) is table.for_engine(FakeEngine("a"))
    assert table.for_engine(FakeEngine("a", remote=True)) is not table.for_engine(FakeEngine("a"))


# ── The table of runs ───────────────────────────────────────────────


def test_a_run_lives_for_ttl_seconds_after_its_last_use() -> None:
    now = [0.0]
    table = RunTable(ttl_s=600, clock=lambda: now[0])
    create = lambda: make_run([entry(FakeEngine("a"))])
    run = table.get_or_create(("r1", ()), create)
    now[0] = 500
    assert table.get_or_create(("r1", ()), create) is run     # used again: the time starts anew
    now[0] = 1000
    assert table.get_or_create(("r1", ()), create) is run
    now[0] = 1601
    assert table.get_or_create(("r1", ()), create) is not run  # expired
    assert len(table) == 1


def test_a_run_that_speaks_longer_than_the_ttl_stays_in_the_table() -> None:
    now = [0.0]
    table = RunTable(ttl_s=600, clock=lambda: now[0])
    create = lambda: make_run([entry(FakeEngine("a"))])
    run = table.get_or_create(("r1", ()), create)
    run.attach()                                                 # a stream is speaking it
    now[0] = 1500
    assert table.get_or_create(("r1", ()), create) is run     # a second call joins the same run
    run.detach()
    table.touch(("r1", ()))                                    # the last stream ended: the time starts now
    now[0] = 2000
    assert table.get_or_create(("r1", ()), create) is run
    now[0] = 2601
    assert table.get_or_create(("r1", ()), create) is not run


def test_the_slot_table_keeps_the_slots_of_engines_whose_limits_did_not_change() -> None:
    table = escalation.EngineSlotTable({"a": EngineDefaults(max_parallel=1), "b": EngineDefaults(max_parallel=2)})
    slots_a, slots_b = table.for_engine(FakeEngine("a")), table.for_engine(FakeEngine("b"))
    table.reconfigure({"a": EngineDefaults(max_parallel=1), "b": EngineDefaults(max_parallel=3)})
    assert table.for_engine(FakeEngine("a")) is slots_a                    # same capacity for running and new readings
    assert table.for_engine(FakeEngine("b")) is not slots_b and table.for_engine(FakeEngine("b")).max_parallel == 3


def test_runs_are_told_apart_by_their_id_and_the_named_engine() -> None:
    table = RunTable()
    create = lambda: make_run([entry(FakeEngine("a"))])
    assert table.get_or_create(("r1", ()), create) is not table.get_or_create(("r2", ()), create)
    assert table.get_or_create(("r1", ()), create) is not table.get_or_create(("r1", (EngineRef("a", "sentence", None),)), create)
    table.clear()
    assert len(table) == 0


def test_the_audio_of_an_entry_is_what_the_engine_made() -> None:
    run = make_run([entry(FakeEngine("a"))])
    spoken = run_async(run.speak("Hallo Welt hier.", REQUEST))
    assert wav_info(spoken.audio)[0] == frames_for("Hallo Welt hier.")


def test_an_entry_without_a_voice_fails_before_anything_is_started_for_it() -> None:
    mute = FakeEngine("mute")
    mute.default_voice = None  # type: ignore[assignment]
    launcher = FakeLauncher(down={"mute": None})
    run = make_run([entry(mute)], launcher)
    with pytest.raises(NoEngineAvailable):
        run_async(run.speak("Ein Satz ohne Stimme hier.", REQUEST))
    assert launcher.starts == [] and launcher.asked == []  # no container started, not even asked
    assert [(s.entry, s.outcome, s.reason) for s in run.trace] == [("mute", "failed", "software")]
