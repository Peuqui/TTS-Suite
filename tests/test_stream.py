"""The speech stream: order, parallel production, keep-alive, errors, abort, failover."""
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
    run_async,
    wav_info,
)

from tts_suite.api import (
    AudioSegment,
    EngineDefaults,
    EngineStarting,
    EntryConfig,
    Host,
    NoEngineAvailable,
    Overloaded,
    SpeakRequest,
    SuiteConfig,
    UnknownEngine,
    UnsupportedLanguage,
)
from tts_suite.constants import MEDIA_TYPE_MP3, MEDIA_TYPE_OGG, MEDIA_TYPE_WAV
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


def test_a_slow_consumer_does_not_have_the_whole_text_generated_ahead_of_it() -> None:
    engine = FakeEngine("a")  # two slots
    stream = Stream(REQUEST, make_run([entry(engine)]))
    sentences = [f"Dies ist der Satz mit der Nummer {n} im Text." for n in range(60)]

    async def scenario() -> None:
        await stream.feed(" ".join(sentences))
        await stream.finish()
        segments = stream.segments()
        await anext(segments)               # one segment taken ...
        await asyncio.sleep(0.2)            # ... and the consumer sleeps
        assert len(engine.texts) <= 2 * engine.max_parallel_requests
        await stream.aclose()

    run_async(scenario())


def test_the_segments_of_a_long_text_still_all_come_when_the_consumer_reads_on() -> None:
    engine = FakeEngine("a")
    stream = Stream(REQUEST, make_run([entry(engine)]))
    sentences = [f"Dies ist der Satz mit der Nummer {n} im Text." for n in range(30)]

    async def scenario() -> list[AudioSegment]:
        await stream.feed(" ".join(sentences))
        await stream.finish()
        return await collect(stream)

    assert lengths(run_async(scenario())) == [frames_for(s) for s in sentences]


# ── Keep-alive ──────────────────────────────────────────────────────


@pytest.mark.parametrize("media_type", [MEDIA_TYPE_WAV, MEDIA_TYPE_OGG, MEDIA_TYPE_MP3])
def test_a_slow_segment_is_bridged_by_a_marker_without_audio_in_every_format(media_type: str) -> None:
    engine = FakeEngine("a")
    engine.audio_media_type = media_type
    engine.gates = {"zweite": asyncio.Event()}
    stream = Stream(REQUEST, make_run([entry(engine)]), keepalive_s=0.02)

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        segments = stream.segments()
        got = [await anext(segments)]
        got.append(await anext(segments))  # the second sentence hangs: a marker instead
        await asyncio.sleep(0.1)  # more intervals pass: more markers are due
        got.append(await anext(segments))
        engine.gates["zweite"].set()
        got += [segment async for segment in segments]
        return got

    got = run_async(scenario())
    assert got[0].keepalive is False and got[0].media_type == media_type
    for marker in got[1:3]:
        assert marker == AudioSegment(audio=b"", media_type="", engine="", keepalive=True)
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


ANNOUNCEMENT = "Stimmwechsel wegen Serverausfall, weiter mit B."
FIVE = ["Satz eins kommt hier.", "Satz zwei kommt hier.", "Satz drei kommt hier.", "Satz vier kommt hier.", "Satz fünf kommt hier."]


def test_a_failure_in_mid_text_is_announced_in_a_segment_of_its_own_in_front_of_the_successor() -> None:
    first, second = FakeEngine("a", fail="unreachable", fail_after=1), FakeEngine("b")
    stream = Stream(REQUEST, make_run([entry(first), entry(second)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        return await collect(stream)

    segments = run_async(scenario())
    assert [s.engine for s in segments] == ["a", "b", "b", "b"]
    assert lengths(segments) == [
        frames_for(SENTENCES[0]), frames_for(ANNOUNCEMENT), frames_for(SENTENCES[1]), frames_for(SENTENCES[2]),
    ]
    assert stream.summary.switch == "unreachable"
    assert [(s.entry, s.outcome) for s in stream.summary.trace] == [("a", "spoke"), ("a", "failed"), ("b", "spoke")]


def test_the_announcement_names_a_remote_successor_and_speaks_the_language_of_the_request() -> None:
    first, second = FakeEngine("a", fail="engine", fail_after=1), FakeEngine("b", remote=True)
    stream = Stream(SpeakRequest(language="en"), make_run([entry(first), entry(second, Host("Aragon", "192.0.2.2"))]))

    async def scenario() -> None:
        await stream.feed("One sentence here. Another sentence here.")
        await stream.finish()
        await collect(stream)

    run_async(scenario())
    assert second.texts == [
        "Another sentence here.", "Voice change due to an engine failure, continuing with B on Aragon.",
    ]


def test_without_a_segment_delivered_before_the_failure_nothing_is_announced() -> None:
    first, second = FakeEngine("a", fail="unreachable"), FakeEngine("b")
    stream = Stream(REQUEST, make_run([entry(first), entry(second)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(TEXT)
        await stream.finish()
        return await collect(stream)

    segments = run_async(scenario())
    assert lengths(segments) == [frames_for(s) for s in SENTENCES] and stream.summary.switch is None


def test_sentences_failing_one_after_the_other_are_announced_once() -> None:
    first, second = GatedFailEngine("a"), FakeEngine("b")
    first.max_parallel_requests = 4
    first.fail_gates = {marker: asyncio.Event() for marker in ("zwei", "drei", "vier", "fünf")}
    stream = Stream(REQUEST, make_run([entry(first), entry(second)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(" ".join(FIVE))
        await stream.finish()
        reader = asyncio.create_task(collect(stream))
        await asyncio.sleep(0.02)
        for gate in first.fail_gates.values():  # the timeouts run out one after the other
            gate.set()
            await asyncio.sleep(0.02)
        return await reader

    segments = run_async(scenario())
    assert [s.engine for s in segments] == ["a", "b", "b", "b", "b", "b"]
    assert lengths(segments) == [frames_for(FIVE[0]), frames_for(ANNOUNCEMENT)] + [frames_for(t) for t in FIVE[1:]]
    assert [step.outcome for step in stream.summary.trace] == ["spoke", "failed", "spoke"]


def test_a_failure_before_the_first_sentence_is_over_still_announces_in_front_of_the_successor() -> None:
    first, second = GatedFailEngine("a"), FakeEngine("b")
    first.max_parallel_requests = 3
    first.gates = {"eins": asyncio.Event()}          # the first sentence is slow ...
    first.fail_gates = {"zwei": asyncio.Event()}     # ... the second one breaks before it is done
    stream = Stream(REQUEST, make_run([entry(first), entry(second)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(" ".join(FIVE[:2]))
        await stream.finish()
        reader = asyncio.create_task(collect(stream))
        await asyncio.sleep(0.02)
        first.fail_gates["zwei"].set()
        await asyncio.sleep(0.02)
        first.gates["eins"].set()
        return await reader

    segments = run_async(scenario())
    assert [s.engine for s in segments] == ["a", "b", "b"]
    assert lengths(segments)[1] == frames_for(ANNOUNCEMENT) and stream.summary.switch == "unreachable"


def test_the_announcement_comes_in_front_of_the_first_segment_of_the_successor_not_one_later() -> None:
    first, second = GatedFailEngine("a"), FakeEngine("b")
    first.max_parallel_requests = 3
    first.fail_gates = {"zwei": asyncio.Event(), "drei": asyncio.Event()}
    stream = Stream(REQUEST, make_run([entry(first), entry(second)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(" ".join(FIVE[:3]))
        await stream.finish()
        reader = asyncio.create_task(collect(stream))
        await asyncio.sleep(0.02)
        first.fail_gates["drei"].set()   # sentence 3 breaks first ...
        await asyncio.sleep(0.02)
        first.fail_gates["zwei"].set()   # ... sentence 2 after it
        return await reader

    segments = run_async(scenario())
    assert lengths(segments) == [
        frames_for(FIVE[0]), frames_for(ANNOUNCEMENT), frames_for(FIVE[1]), frames_for(FIVE[2]),
    ]


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


def test_a_new_configuration_does_not_double_the_capacity_of_an_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine("slowfake")
    engine.max_parallel_requests = 1
    engine.gates = {"Satz": asyncio.Event()}
    _install(monkeypatch, engine)
    config = _config(tmp_path, ["slowfake"])
    speaker = Speaker(config, FakeLauncher())

    async def scenario() -> None:
        first = asyncio.create_task(collect(speaker.speak("Der erste Satz hier.", REQUEST)))
        await asyncio.sleep(0.02)
        speaker.set_config(config)                       # a reload, a changed voice ...
        second = asyncio.create_task(collect(speaker.speak("Der zweite Satz hier.", REQUEST)))
        await asyncio.sleep(0.02)
        engine.gates["Satz"].set()
        await asyncio.gather(first, second)

    run_async(scenario())
    assert engine.peak == 1


def test_a_code_block_that_opens_in_one_call_and_closes_in_the_next_of_the_same_run_does_not_swallow_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine("fake")
    _install(monkeypatch, engine)
    speaker = Speaker(_config(tmp_path, ["fake"]), FakeLauncher())
    named = SpeakRequest(language="de", run="reading-1")

    async def scenario() -> None:
        await collect(speaker.speak("Hier kommt ein Beispiel für dich:\n\n```python\nprint(x)\n", named))
        await collect(speaker.speak("```\n\nDanach geht es wirklich weiter. Und noch ein ganz normaler Satz.", named))

    run_async(scenario())
    # (the second call starts with a lone fence, so its cutter sees an unclosed block and keeps the rest as one unit)
    assert engine.texts[-1] == "Danach geht es wirklich weiter. Und noch ein ganz normaler Satz."


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


@pytest.mark.parametrize("language", ["fr", "", "DE"])
def test_a_language_without_suite_texts_is_refused_at_once_and_nothing_is_started(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine("fake")
    _install(monkeypatch, engine)
    launcher = FakeLauncher({"fake": None})  # down: a reading would start it
    speaker = Speaker(_config(tmp_path, ["fake"]), launcher)
    request = SpeakRequest(language=language)
    with pytest.raises(UnsupportedLanguage):
        speaker.open_stream(request)
    with pytest.raises(UnsupportedLanguage):
        speaker.speak("Ein Satz kommt hier.", request)
    assert launcher.asked == [] and launcher.starts == [] and engine.texts == []


@pytest.mark.parametrize("language", ["de", "en"])
def test_the_languages_of_the_suite_texts_are_spoken(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeEngine("fake"))
    speaker = Speaker(_config(tmp_path, ["fake"]), FakeLauncher())
    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak("Ein Satz kommt hier.", SpeakRequest(language=language)))

    assert len(run_async(scenario())) == 1


def _config(tmp_path: Path, keys: list[str]) -> SuiteConfig:
    return SuiteConfig(voices_dir=tmp_path, entries=tuple(EntryConfig(key) for key in keys))


def _install(monkeypatch: pytest.MonkeyPatch, *engines: FakeEngine) -> None:
    """Put fake engines into the registry for the test (the binding looks engines up there)."""
    from tts_suite.engines import registry

    for engine in engines:
        monkeypatch.setitem(registry.TTS_ENGINES, engine.key, engine)


# ── Text cleaning in the stream ─────────────────────────────────────


def test_units_are_cleaned_before_the_engine_and_empty_ones_drop_out_in_order() -> None:
    engine = FakeEngine("a")
    pronunciations = {"AIfred": "Alfred"}
    stream = Stream(REQUEST, make_run([entry(engine)]), pronunciations=pronunciations)
    text = (
        "AIfred sagt **hier** etwas Wichtiges.\n\n```python\nx = 1\n```\n\n"
        "Danach folgt noch ein langer Satz hier. Zum Schluss kommt noch einer."
    )

    async def scenario() -> list[AudioSegment]:
        await stream.feed(text)
        await stream.finish()
        return await collect(stream)

    segments = run_async(scenario())
    assert engine.texts == [
        "Alfred sagt hier etwas Wichtiges.", "Hier steht Code.", "Danach folgt noch ein langer Satz hier.", "Zum Schluss kommt noch einer.",
    ]
    assert lengths(segments) == [frames_for(spoken) for spoken in engine.texts]


def test_the_speaker_hands_its_pronunciations_to_the_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine("fake")
    _install(monkeypatch, engine)
    config = SuiteConfig(voices_dir=tmp_path, entries=(EntryConfig("fake"),), pronunciations={"AIfred": "Alfred"})
    speaker = Speaker(config, FakeLauncher())

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak("AIfred spricht hier einen vollständigen Satz.", REQUEST))

    run_async(scenario())
    assert engine.texts == ["Alfred spricht hier einen vollständigen Satz."]


def test_a_reading_of_only_unspeakable_text_ends_without_audio() -> None:
    engine = FakeEngine("a")
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed("---\n\n...")
        await stream.finish()
        return await collect(stream)

    assert run_async(scenario()) == [] and engine.texts == []
