"""The speech stream: order, parallel production, keep-alive, errors, abort, failover."""
from __future__ import annotations

import asyncio

import pytest
from speech_fakes import (
    SENTENCES,
    TEXT,
    FakeEngine,
    FakeLauncher,
    GatedFailEngine,
    collect,
    entry,
    frames_for,
    lengths,
    make_run,
    request,
    run_async,
    wav_info,
)

from tts_suite.api import (
    AudioSegment,
    EngineDefaults,
    EngineStarting,
    Host,
    NoEngineAvailable,
    Overloaded,
    SpeakRequest,
)
from tts_suite.constants import MEDIA_TYPE_MP3, MEDIA_TYPE_OGG, MEDIA_TYPE_WAV
from tts_suite.stream import Stream, pause_marker

REQUEST = request("a")


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
    browser = FakeEngine("a")
    browser.renders_in_browser = True
    cases = [
        (make_run([entry(browser)]), REQUEST, NoEngineAvailable),
        (make_run([entry(FakeEngine("a"))], FakeLauncher({"a": None})), request("a", wait=False), EngineStarting),
        (make_run([entry(FakeEngine("a"))], defaults={"a": EngineDefaults(max_parallel=1, max_queue=0)}), REQUEST, Overloaded),
    ]
    for run, head, expected in cases:
        async def scenario(run=run, head=head, expected=expected) -> None:
            if expected is Overloaded:
                run_slots = run._slots.for_engine(run._entries[0].engine)  # type: ignore[attr-defined]
                async with run_slots.slot():
                    await _expect_first_step_error(run, head, expected)
            else:
                await _expect_first_step_error(run, head, expected)

        run_async(scenario())


async def _expect_first_step_error(run: object, head: SpeakRequest, expected: type[Exception]) -> None:
    stream = Stream(head, run)  # type: ignore[arg-type]
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
    stream = Stream(request("a", "b", language="en"), make_run([entry(first), entry(second, Host("Aragon", "192.0.2.2"))]))

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


def test_a_reading_of_only_unspeakable_text_ends_without_audio() -> None:
    engine = FakeEngine("a")
    stream = Stream(REQUEST, make_run([entry(engine)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed("---\n\n...")
        await stream.finish()
        return await collect(stream)

    assert run_async(scenario()) == [] and engine.texts == []


# ── Pauses between paragraphs ───────────────────────────────────────

PAUSE_MS = 700
PARAGRAPHS = f"{SENTENCES[0]} {SENTENCES[1]}\n\n{SENTENCES[2]}"
PAUSED = request("a", paragraph_pause_ms=PAUSE_MS)
UNSPEAKABLE = "[Quelle Nummer eins]"  # a paragraph of three words that the cleaning removes completely


def shape(segments: list[AudioSegment]) -> list[int | str]:
    """What was delivered: the frame count of a spoken segment, ``pause:<ms>`` for a pause marker."""
    return [f"pause:{s.pause_ms}" if s.pause_ms else wav_info(s.audio)[0] for s in segments]


def pause() -> str:
    return f"pause:{PAUSE_MS}"


async def read_text(stream: Stream, text: str) -> list[AudioSegment]:
    await stream.feed(text)
    await stream.finish()
    return await collect(stream)


def test_a_pause_marker_is_a_segment_without_audio_and_without_keepalive() -> None:
    marker = pause_marker(PAUSE_MS)
    assert marker == AudioSegment(audio=b"", media_type="", engine="", keepalive=False, pause_ms=PAUSE_MS)
    assert AudioSegment(audio=b"x", media_type="audio/wav", engine="a").pause_ms == 0


@pytest.mark.parametrize("unit", ["sentence", "paragraph"])
def test_a_marker_comes_at_each_paragraph_boundary(unit: str) -> None:
    engine = FakeEngine("a", unit=unit)  # type: ignore[arg-type]
    stream = Stream(PAUSED, make_run([entry(engine)]))
    text = f"{PARAGRAPHS}\n\nEin vierter Absatz kommt noch hinzu."
    segments = run_async(read_text(stream, text))
    spoken = engine.texts
    assert shape(segments) == [
        frames_for(spoken[0]), *([frames_for(spoken[1])] if unit == "sentence" else []), pause(),
        frames_for(spoken[-2]), pause(), frames_for(spoken[-1]),
    ]
    assert [s.pause_ms for s in segments].count(PAUSE_MS) == 2


def test_between_two_paragraphs_of_the_unit_paragraph_there_is_one_marker_each() -> None:
    engine = FakeEngine("a", unit="paragraph")
    segments = run_async(read_text(Stream(PAUSED, make_run([entry(engine)])), "Eins hier da.\n\nZwei hier da.\n\nDrei hier da."))
    assert shape(segments) == [frames_for("Eins hier da."), pause(), frames_for("Zwei hier da."), pause(), frames_for("Drei hier da.")]


def test_the_unit_whole_gets_no_marker() -> None:
    engine = FakeEngine("a", unit="whole")
    segments = run_async(read_text(Stream(PAUSED, make_run([entry(engine)])), PARAGRAPHS))
    assert shape(segments) == [frames_for(PARAGRAPHS)]


def test_no_marker_before_the_first_or_after_the_last_paragraph() -> None:
    engine = FakeEngine("a")
    segments = run_async(read_text(Stream(PAUSED, make_run([entry(engine)])), f"\n\n{SENTENCES[0]}\n\n\n"))
    assert shape(segments) == [frames_for(SENTENCES[0])]


def test_a_pause_of_zero_gives_no_marker() -> None:
    engine = FakeEngine("a")
    segments = run_async(read_text(Stream(request("a", paragraph_pause_ms=0), make_run([entry(engine)])), PARAGRAPHS))
    assert shape(segments) == [frames_for(s) for s in SENTENCES]


def test_markers_keep_their_place_when_the_sentences_finish_the_other_way_round() -> None:
    engine = FakeEngine("a")
    engine.max_parallel_requests = 3
    engine.gates = {"erste": asyncio.Event(), "zweite": asyncio.Event(), "dritte": asyncio.Event()}
    stream = Stream(PAUSED, make_run([entry(engine)]))

    async def scenario() -> list[AudioSegment]:
        await stream.feed(PARAGRAPHS)
        await stream.finish()
        reader = asyncio.create_task(collect(stream))
        for gate in ("dritte", "zweite", "erste"):
            await asyncio.sleep(0.01)
            engine.gates[gate].set()
        return await reader

    segments = run_async(scenario())
    assert shape(segments) == [frames_for(SENTENCES[0]), frames_for(SENTENCES[1]), pause(), frames_for(SENTENCES[2])]
    assert engine.peak == 3  # the marker did not hold back the generation of the sentence behind it


def test_a_marker_waits_for_the_segment_in_front_of_it() -> None:
    engine = FakeEngine("a")
    engine.gates = {"zweite": asyncio.Event()}
    stream = Stream(PAUSED, make_run([entry(engine)]), keepalive_s=10)

    async def scenario() -> None:
        await stream.feed(PARAGRAPHS)
        await stream.finish()
        segments = stream.segments()
        assert wav_info((await anext(segments)).audio)[0] == frames_for(SENTENCES[0])
        pending = asyncio.ensure_future(anext(segments))
        await asyncio.sleep(0.05)
        assert not pending.done()  # the second sentence hangs, the marker behind it must not overtake
        engine.gates["zweite"].set()
        assert wav_info((await pending).audio)[0] == frames_for(SENTENCES[1])
        assert (await anext(segments)).pause_ms == PAUSE_MS

    run_async(scenario())


@pytest.mark.parametrize("unit", ["sentence", "paragraph"])
def test_paragraphs_with_nothing_left_to_speak_add_no_pause_of_their_own(unit: str) -> None:
    engine = FakeEngine("a", unit=unit)  # type: ignore[arg-type]
    text = f"Eins hier da.\n\n{UNSPEAKABLE}\n\n\n\n{UNSPEAKABLE}\n\nZwei hier da."
    segments = run_async(read_text(Stream(PAUSED, make_run([entry(engine)])), text))
    assert shape(segments) == [frames_for("Eins hier da."), pause(), frames_for("Zwei hier da.")]


def test_empty_paragraphs_at_the_start_and_end_give_no_marker() -> None:
    engine = FakeEngine("a")
    segments = run_async(read_text(Stream(PAUSED, make_run([entry(engine)])), f"{UNSPEAKABLE}\n\nEins hier da.\n\n{UNSPEAKABLE}"))
    assert shape(segments) == [frames_for("Eins hier da.")]


def test_a_change_of_the_speaking_entry_in_mid_text_keeps_the_markers_and_the_announcement_gets_none() -> None:
    first, second = FakeEngine("a", fail="unreachable", fail_after=1), FakeEngine("b")
    stream = Stream(PAUSED, make_run([entry(first), entry(second)]))
    text = f"{SENTENCES[0]}\n\n{SENTENCES[1]}\n\n{SENTENCES[2]}"
    segments = run_async(read_text(stream, text))
    assert shape(segments) == [
        frames_for(SENTENCES[0]), pause(), frames_for(ANNOUNCEMENT), frames_for(SENTENCES[1]), pause(), frames_for(SENTENCES[2]),
    ]
    assert [s.engine for s in segments if not s.pause_ms and s.engine] == ["a", "b", "b", "b"]
    assert stream.summary.switch == "unreachable"


def test_markers_do_not_count_as_keepalive() -> None:
    engine = FakeEngine("a")
    segments = run_async(read_text(Stream(PAUSED, make_run([entry(engine)])), PARAGRAPHS))
    assert [s.keepalive for s in segments] == [False] * 4


@pytest.mark.parametrize("unit", ["sentence", "paragraph", "whole"])
def test_a_text_in_any_pieces_gives_the_segments_of_the_whole_text(unit: str) -> None:
    text = (
        f"{PARAGRAPHS}\n\n{UNSPEAKABLE}\n\nEin vierter Absatz kommt noch hinzu. Und ein weiterer Satz folgt hier.\n \n"
        "Zuletzt kommt der fünfte Absatz."
    )

    def read_in_pieces(size: int) -> list[int | str]:
        engine = FakeEngine("a", unit=unit)  # type: ignore[arg-type]
        stream = Stream(PAUSED, make_run([entry(engine)]))

        async def scenario() -> list[AudioSegment]:
            for start in range(0, len(text), size):
                await stream.feed(text[start:start + size])
            await stream.finish()
            return await collect(stream)

        return shape(run_async(scenario()))

    expected = read_in_pieces(len(text))
    assert (pause() in expected) == (unit != "whole")
    for size in (1, 2, 5, 11, 40):
        assert read_in_pieces(size) == expected, f"piece size {size}"
