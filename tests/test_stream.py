"""The speech stream: order, parallel production, keep-alive, errors, abort, failover."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from speech_fakes import FakeEngine, FakeLauncher, entry, frames_for, make_run, run_async, wav_info

from tts_suite.api import (
    AudioSegment,
    EngineDefaults,
    EngineStarting,
    EntryConfig,
    NoEngineAvailable,
    Overloaded,
    SpeakRequest,
    SuiteConfig,
    UnknownEngine,
)
from tts_suite.constants import KEEPALIVE_SILENCE_MS, MEDIA_TYPE_WAV
from tts_suite.speaker import Speaker
from tts_suite.stream import Stream

REQUEST = SpeakRequest(language="de")
SENTENCES = ["Der erste Satz kommt hier.", "Danach folgt der zweite Satz noch.", "Zuletzt kommt noch ein dritter Satz hier."]
TEXT = " ".join(SENTENCES)


async def collect(stream: Stream) -> list[AudioSegment]:
    return [segment async for segment in stream.segments()]


def lengths(segments: list[AudioSegment]) -> list[int]:
    return [wav_info(segment.audio)[0] for segment in segments]


def test_a_text_is_spoken_sentence_by_sentence_in_order() -> None:
    engine = FakeEngine("a")
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        return await collect(stream)

    segments = run_async(scenario())
    assert lengths(segments) == [frames_for(s) for s in SENTENCES]
    assert {s.engine for s in segments} == {"a"} and all(s.media_type == MEDIA_TYPE_WAV for s in segments)
    assert stream.summary.trace[-1].outcome == "spoke" and stream.summary.switch is None


def test_segments_come_in_speaking_order_when_the_sentences_finish_the_other_way_round() -> None:
    engine = FakeEngine("a")
    engine.max_parallel_requests = 3
    engine.gates = {"erste": asyncio.Event(), "zweite": asyncio.Event(), "dritte": asyncio.Event()}
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        reader = asyncio.create_task(collect(stream))
        for gate in ("dritte", "zweite", "erste"):  # the last sentence is done first
            await asyncio.sleep(0.01)
            engine.gates[gate].set()
        return await reader

    assert lengths(run_async(scenario())) == [frames_for(s) for s in SENTENCES]
    assert engine.peak == 3  # all three were generated at the same time


def test_one_reading_never_runs_more_syntheses_than_the_engine_allows() -> None:
    engine = FakeEngine("a")  # two slots
    engine.gates = {"Satz": asyncio.Event()}
    stream = Stream(REQUEST, make_run([entry(engine)], defaults={"a": EngineDefaults(max_queue=0)}))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        reader = asyncio.create_task(collect(stream))
        await asyncio.sleep(0.05)
        engine.gates["Satz"].set()
        return await reader  # no Overloaded although the queue is 0: the stream waits itself

    assert len(run_async(scenario())) == 3 and engine.peak == 2


def test_the_first_segment_is_out_while_the_text_is_still_arriving() -> None:
    engine = FakeEngine("a")
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> None:
        segments = stream.segments()
        await stream.feed("Der erste Satz kommt hier. Der zweite Satz beginnt gerade ")
        first = await anext(segments)
        assert wav_info(first.audio)[0] == frames_for("Der erste Satz kommt hier.")
        await stream.feed("und endet jetzt.")
        await stream.finish()
        rest = [segment async for segment in segments]
        assert lengths(rest) == [frames_for("Der zweite Satz beginnt gerade und endet jetzt.")]

    run_async(scenario())


def test_the_unit_is_that_of_the_speaking_engine() -> None:
    engine = FakeEngine("a", unit="paragraph")
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> None:
        await stream.feed("Erster Absatz mit Sätzen. Noch einer.\n\nZweiter Absatz")
        await stream.finish()
        assert lengths(await collect(stream)) == [
            frames_for("Erster Absatz mit Sätzen. Noch einer."), frames_for("Zweiter Absatz"),
        ]

    run_async(scenario())


def test_a_whole_text_engine_speaks_only_after_the_text_ended() -> None:
    engine = FakeEngine("a", unit="whole")
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> None:
        reader = asyncio.create_task(collect(stream))
        await stream.feed("Erster Satz hier. Zweiter Satz hier.")
        await asyncio.sleep(0.05)
        assert not reader.done() and engine.texts == []
        await stream.finish()
        assert lengths(await reader) == [frames_for("Erster Satz hier. Zweiter Satz hier.")]

    run_async(scenario())


def test_an_empty_reading_ends_without_choosing_an_engine() -> None:
    launcher = FakeLauncher()
    stream = Stream(REQUEST, make_run([entry(FakeEngine("a"))], launcher))

    async def scenario() -> None:
        await stream.feed("  ")
        await stream.finish()
        assert await collect(stream) == []

    run_async(scenario())
    assert launcher.asked == [] and stream.summary.trace == ()


def test_no_text_after_finish() -> None:
    stream = Stream(REQUEST, make_run([entry(FakeEngine("a"))]))

    async def scenario() -> None:
        await stream.finish()
        with pytest.raises(RuntimeError, match="no more text"):
            await stream.feed("Zu spät.")

    run_async(scenario())


# ── Keep-alive ──────────────────────────────────────────────────────


def test_a_slow_segment_is_bridged_by_silence_in_the_format_of_the_stream() -> None:
    engine = FakeEngine("a")
    engine.gates = {"zweite": asyncio.Event()}
    stream = Stream(REQUEST, make_run([entry(engine)]), keepalive_s=0.02)

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        segments = stream.segments()
        got = [await anext(segments)]
        got.append(await anext(segments))  # the second sentence hangs: silence instead
        engine.gates["zweite"].set()
        got += [segment async for segment in segments]
        return got

    got = run_async(scenario())
    assert got[0].keepalive is False
    keepalive = got[1]
    assert keepalive.keepalive and keepalive.engine == "" and keepalive.media_type == MEDIA_TYPE_WAV
    assert wav_info(keepalive.audio) == (24000 * KEEPALIVE_SILENCE_MS // 1000, 24000)
    assert [s.keepalive for s in got if s.engine] == [False, False, False]


def test_there_is_no_silence_before_the_first_segment() -> None:
    engine = FakeEngine("a")
    engine.gates = {"erste": asyncio.Event()}
    stream = Stream(REQUEST, make_run([entry(engine)]), keepalive_s=0.01)
    seen: list[AudioSegment] = []

    async def read() -> None:
        async for segment in stream.segments():
            seen.append(segment)

    async def scenario() -> None:
        await stream.feed(SENTENCES[0])
        await stream.finish()
        reader = asyncio.create_task(read())
        await asyncio.sleep(0.1)
        assert seen == []
        engine.gates["erste"].set()
        await reader
        assert len(seen) == 1

    run_async(scenario())


# ── Errors ──────────────────────────────────────────────────────────


def test_an_error_before_the_first_segment_comes_from_the_first_step() -> None:
    cases = [
        (make_run([entry(FakeEngine("a"), enabled=False)]), SpeakRequest(language="de"), NoEngineAvailable),
        (make_run([entry(FakeEngine("a"))], FakeLauncher({"a": None})), SpeakRequest(language="de", wait=False), EngineStarting),
        (make_run([entry(FakeEngine("a"))], defaults={"a": EngineDefaults(max_parallel=1, max_queue=0)}), REQUEST, Overloaded),
    ]
    for run, request, expected in cases:
        async def scenario(run=run, request=request, expected=expected) -> None:
            if expected is Overloaded:
                run_slots = run._slots.for_engine(run._entries[0].engine)  # type: ignore[attr-defined]
                async with run_slots.slot():
                    await _expect_first_step_error(run, request, expected)
            else:
                await _expect_first_step_error(run, request, expected)

        run_async(scenario())


async def _expect_first_step_error(run: object, request: SpeakRequest, expected: type[Exception]) -> None:
    stream = Stream(request, run)  # type: ignore[arg-type]
    await stream.feed(TEXT)
    await stream.finish()
    segments = stream.segments()
    with pytest.raises(expected):
        await anext(segments)


def test_an_error_after_the_first_segment_ends_the_stream_with_that_error() -> None:
    engine = FakeEngine("a", fail="engine", fail_after=1)
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> None:
        await stream.feed(TEXT)
        await stream.finish()
        segments = stream.segments()
        first = await anext(segments)
        assert wav_info(first.audio)[0] == frames_for(SENTENCES[0])
        with pytest.raises(NoEngineAvailable) as error:
            await anext(segments)
        assert [s.reason for s in error.value.trace if s.outcome == "failed"] == ["engine"]

    run_async(scenario())


def test_a_failure_in_mid_text_is_taken_over_with_an_announcement() -> None:
    first, second = FakeEngine("a", fail="unreachable", fail_after=1), FakeEngine("b")
    stream = Stream(REQUEST, make_run([entry(first), entry(second)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        return await collect(stream)

    segments = run_async(scenario())
    assert [s.engine for s in segments] == ["a", "b", "b"]
    announced = "Stimmwechsel wegen Serverausfall, weiter mit B. " + SENTENCES[1]
    assert lengths(segments) == [frames_for(SENTENCES[0]), frames_for(announced), frames_for(SENTENCES[2])]
    assert stream.summary.switch == "unreachable"
    assert [(s.entry, s.outcome) for s in stream.summary.trace] == [("a", "spoke"), ("a", "failed"), ("b", "spoke")]


# ── Abort ───────────────────────────────────────────────────────────


def test_aclose_stops_the_generation_and_frees_slots_and_lock() -> None:
    engine = FakeEngine("a")
    engine.gates = {"zweite": asyncio.Event(), "dritte": asyncio.Event()}  # never released
    run = make_run([entry(engine)])
    stream = Stream(REQUEST, run)

    async def scenario() -> None:
        await stream.feed(TEXT)
        await stream.finish()
        segments = stream.segments()
        await anext(segments)
        await asyncio.sleep(0.02)
        assert engine.running == 2
        await stream.aclose()
        assert engine.cancelled == 2 and engine.running == 0
        assert [s async for s in segments] == []           # the reader is not left hanging
        assert not run._lock.locked()                       # type: ignore[attr-defined]
        slots = run._slots.for_engine(engine)               # type: ignore[attr-defined]
        async with slots.slot():                            # a free slot again
            pass

    run_async(scenario())


def test_aclose_wakes_a_reader_that_is_waiting() -> None:
    engine = FakeEngine("a")
    engine.gates = {"Satz": asyncio.Event()}
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> None:
        await stream.feed(TEXT)
        await stream.finish()
        reader = asyncio.create_task(collect(stream))
        await asyncio.sleep(0.02)
        await stream.aclose()
        assert await reader == []

    run_async(scenario())


def test_aclose_after_a_normal_end_does_nothing() -> None:
    stream = Stream(REQUEST, make_run([entry(FakeEngine("a"))]))

    async def scenario() -> None:
        await stream.feed(SENTENCES[0])
        await stream.finish()
        assert len(await collect(stream)) == 1
        await stream.aclose()
        await stream.aclose()
        assert len(stream.summary.trace) == 1

    run_async(scenario())


# ── The speaker (run table, named engine, speak) ────────────────────


def test_speak_hands_over_the_whole_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine("fake")
    _install(monkeypatch, engine)
    speaker = Speaker(_config(tmp_path, ["fake"]), FakeLauncher())

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(TEXT, REQUEST))

    assert lengths(run_async(scenario())) == [frames_for(s) for s in SENTENCES]


def test_the_state_of_a_run_is_kept_for_the_next_call_with_the_same_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken, healthy = FakeEngine("brokenfake", fail="engine"), FakeEngine("healthyfake")
    _install(monkeypatch, broken, healthy)
    now = [0.0]
    speaker = Speaker(_config(tmp_path, ["brokenfake", "healthyfake"]), FakeLauncher(), clock=lambda: now[0])
    named = SpeakRequest(language="de", run="reading-1")

    async def scenario() -> None:
        await collect(speaker.speak(SENTENCES[0], named))
        broken.fail = None                              # healthy again, but the run remembers
        await collect(speaker.speak(SENTENCES[1], named))
        await collect(speaker.speak(SENTENCES[2], SpeakRequest(language="de", run="reading-2")))
        now[0] = 601                                    # reading-1 is forgotten
        await collect(speaker.speak(SENTENCES[0], named))

    run_async(scenario())
    assert len(broken.texts) == 3 and len(healthy.texts) == 2  # tries: first call, reading-2, after the expiry


def test_a_named_engine_speaks_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = FakeEngine("onefake", fail="engine"), FakeEngine("twofake")
    _install(monkeypatch, first, second)
    speaker = Speaker(_config(tmp_path, ["onefake", "twofake"]), FakeLauncher())

    async def scenario() -> None:
        pinned = speaker.speak(SENTENCES[0], SpeakRequest(language="de", engine="onefake"))
        with pytest.raises(NoEngineAvailable):
            await collect(pinned)
        assert second.texts == []                       # the escalation is skipped
        assert lengths(await collect(speaker.speak(SENTENCES[0], SpeakRequest(language="de", engine="twofake")))) == [
            frames_for(SENTENCES[0]),
        ]
        with pytest.raises(UnknownEngine):
            speaker.speak("x", SpeakRequest(language="de", engine="nope"))

    run_async(scenario())


def test_a_named_engine_outside_the_list_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeEngine("onefake"), FakeEngine("twofake"))
    speaker = Speaker(_config(tmp_path, ["onefake"]), FakeLauncher())
    with pytest.raises(NoEngineAvailable, match="not in the escalation list"):
        speaker.open_stream(SpeakRequest(language="de", engine="twofake"))


def test_a_broken_configuration_leaves_the_old_one_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeEngine("onefake"))
    speaker = Speaker(_config(tmp_path, ["onefake"]), FakeLauncher())
    from tts_suite.api import ConfigInvalid

    with pytest.raises(ConfigInvalid):
        speaker.set_config(_config(tmp_path, ["nope"]))
    assert len(run_async(_collect_text(speaker))) == 3


async def _collect_text(speaker: Speaker) -> list[AudioSegment]:
    return await collect(speaker.speak(TEXT, REQUEST))


def _config(tmp_path: Path, keys: list[str]) -> SuiteConfig:
    return SuiteConfig(voices_dir=tmp_path, entries=tuple(EntryConfig(key) for key in keys))


def _install(monkeypatch: pytest.MonkeyPatch, *engines: FakeEngine) -> None:
    """Put fake engines into the registry for the test (the binding looks engines up there)."""
    from tts_suite.engines import registry

    for engine in engines:
        monkeypatch.setitem(registry.TTS_ENGINES, engine.key, engine)
