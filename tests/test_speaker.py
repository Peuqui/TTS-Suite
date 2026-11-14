"""The speaker: the list of a request, its checks, the state shared by all requests, runs, speak."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from container_fakes import ARAGON, install
from speech_fakes import (
    SENTENCES,
    TEXT,
    FakeEngine,
    FakeLauncher,
    collect,
    frames_for,
    lengths,
    request,
    run_async,
)

from tts_suite.api import (
    AUTO,
    AudioSegment,
    AutoEntry,
    ConfigInvalid,
    EngineDefaults,
    EngineRef,
    InvalidRequest,
    NoEngineAvailable,
    SpeakRequest,
    SuiteConfig,
    UnknownEngine,
    UnknownHost,
    UnsupportedLanguage,
)
from tts_suite.escalation import Skip
from tts_suite.speaker import Speaker


def _speaker(tmp_path: Path, launcher: FakeLauncher | None = None, **kwargs: object) -> Speaker:
    return Speaker(SuiteConfig(voices_dir=tmp_path, **kwargs), launcher or FakeLauncher())  # type: ignore[arg-type]


def _remote_capable(engine: FakeEngine) -> FakeEngine:
    engine.default_port = 5000  # an engine with a REST API can be bound to a host
    return engine


# ── Speaking ────────────────────────────────────────────────────────


def test_speak_hands_over_the_whole_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("fake"))
    speaker = _speaker(tmp_path)

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(TEXT, request("fake")))

    assert lengths(run_async(scenario())) == [frames_for(s) for s in SENTENCES]


def test_the_speaker_hands_its_pronunciations_to_the_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine("fake")
    install(monkeypatch, engine)
    speaker = _speaker(tmp_path, pronunciations={"AIfred": "Alfred"})

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak("AIfred spricht hier einen vollständigen Satz.", request("fake")))

    run_async(scenario())
    assert engine.texts == ["Alfred spricht hier einen vollständigen Satz."]


@pytest.mark.parametrize("language", ["nl", "", "DE"])
def test_a_language_without_suite_texts_is_refused_at_once_and_nothing_is_started(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine("fake")
    install(monkeypatch, engine)
    launcher = FakeLauncher({"fake": None})  # down: a reading would start it
    speaker = _speaker(tmp_path, launcher)
    head = request("fake", language=language)
    with pytest.raises(UnsupportedLanguage):
        speaker.open_stream(head)
    with pytest.raises(UnsupportedLanguage):
        speaker.speak("Ein Satz kommt hier.", head)
    assert launcher.asked == [] and launcher.starts == [] and engine.texts == []


@pytest.mark.parametrize("language", ["de", "en"])
def test_the_languages_of_the_suite_texts_are_spoken(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeEngine("fake"))
    speaker = _speaker(tmp_path)

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak("Ein Satz kommt hier.", request("fake", language=language)))

    assert len(run_async(scenario())) == 1


# ── The list of a request ───────────────────────────────────────────


def test_the_list_of_a_request_sets_the_order_and_leaves_other_engines_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, two, three = FakeEngine("onefake"), FakeEngine("twofake"), FakeEngine("threefake")
    install(monkeypatch, one, two, three)
    launcher = FakeLauncher()
    speaker = _speaker(tmp_path, launcher)

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(SENTENCES[0], request("threefake", "twofake")))

    segments = run_async(scenario())
    assert [segment.engine for segment in segments] == ["threefake"]  # the first of the list speaks
    assert launcher.asked == ["threefake"]                             # the second and the unnamed: not even checked
    assert one.texts == [] and two.texts == []


def test_a_one_entry_list_speaks_alone_and_fails_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = FakeEngine("onefake", fail="engine"), FakeEngine("twofake")
    install(monkeypatch, first, second)
    speaker = _speaker(tmp_path)

    async def scenario() -> None:
        with pytest.raises(NoEngineAvailable):
            await collect(speaker.speak(SENTENCES[0], request("onefake")))
        assert second.texts == []                       # not in the list: never tried
        assert lengths(await collect(speaker.speak(SENTENCES[0], request("twofake")))) == [frames_for(SENTENCES[0])]

    run_async(scenario())


def test_a_request_goes_on_to_the_next_entry_of_its_list_but_never_to_an_unlisted_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, other, spare = FakeEngine("brokenfake", fail="engine"), FakeEngine("otherfake"), FakeEngine("sparefake")
    install(monkeypatch, broken, other, spare)
    speaker = _speaker(tmp_path)

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(SENTENCES[0], request("brokenfake", "otherfake")))

    assert {segment.engine for segment in run_async(scenario()) if segment.audio} >= {"otherfake"}
    assert spare.texts == []


def test_an_entry_on_a_host_is_labelled_with_the_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one, two = _remote_capable(FakeEngine("onefake")), FakeEngine("twofake")
    install(monkeypatch, one, two)
    speaker = _speaker(tmp_path, hosts=(ARAGON,))
    head = SpeakRequest(language="de", paragraph_pause_ms=0, engines=(
        EngineRef("twofake", "sentence", None), EngineRef("onefake", "sentence", ARAGON.name), EngineRef("onefake", "sentence", None),
    ))
    assert [entry.label for entry in speaker._bind(speaker._refs_for(head))] == ["twofake", f"onefake@{ARAGON.name}", "onefake"]


# ── Units per entry ─────────────────────────────────────────────────

TWO_PARAGRAPHS = "Satz eins steht hier. Satz zwei steht hier.\n\nSatz drei steht hier. Satz vier steht hier."


def test_each_entry_has_its_own_unit_and_the_speaking_entry_cuts_the_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_paragraph, by_sentence = FakeEngine("paragraphfake"), FakeEngine("sentencefake")
    install(monkeypatch, by_paragraph, by_sentence)
    speaker = _speaker(tmp_path)
    mixed = (EngineRef("paragraphfake", "paragraph", None), EngineRef("sentencefake", "sentence", None))

    async def scenario() -> None:
        first = await collect(speaker.speak(TWO_PARAGRAPHS, SpeakRequest(language="de", paragraph_pause_ms=0, engines=mixed)))
        assert [segment.engine for segment in first] == ["paragraphfake"] * 2
        reversed_ = await collect(speaker.speak(TWO_PARAGRAPHS, SpeakRequest(language="de", paragraph_pause_ms=0, engines=mixed[::-1])))
        assert [segment.engine for segment in reversed_] == ["sentencefake"] * 4

    run_async(scenario())


def test_when_the_first_entry_is_passed_over_the_text_is_cut_by_the_unit_of_the_one_that_speaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_paragraph, by_sentence = FakeEngine("paragraphfake"), FakeEngine("sentencefake")
    install(monkeypatch, by_paragraph, by_sentence)
    speaker = _speaker(tmp_path, FakeLauncher({"paragraphfake": Skip("stopped")}))
    mixed = (EngineRef("paragraphfake", "paragraph", None), EngineRef("sentencefake", "sentence", None))

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(TWO_PARAGRAPHS, SpeakRequest(language="de", paragraph_pause_ms=0, engines=mixed)))

    assert [segment.engine for segment in run_async(scenario())] == ["sentencefake"] * 4


# ── Requests that cannot work ───────────────────────────────────────


def test_an_empty_list_is_refused_before_anything_is_checked_or_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("onefake"))
    launcher = FakeLauncher({"onefake": None})
    speaker = _speaker(tmp_path, launcher)
    with pytest.raises(InvalidRequest, match="empty"):
        speaker.open_stream(SpeakRequest(language="de", paragraph_pause_ms=0, engines=()))
    with pytest.raises(InvalidRequest, match="empty"):
        speaker.speak("Ein Satz kommt hier.", SpeakRequest(language="de", paragraph_pause_ms=0, engines=()))
    assert launcher.asked == [] and launcher.starts == []


def test_a_negative_paragraph_pause_is_refused_before_anything_is_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("onefake"))
    launcher = FakeLauncher({"onefake": None})
    speaker = _speaker(tmp_path, launcher)
    negative = request("onefake", paragraph_pause_ms=-1)
    with pytest.raises(InvalidRequest, match="paragraph_pause_ms"):
        speaker.open_stream(negative)
    with pytest.raises(InvalidRequest, match="paragraph_pause_ms"):
        speaker.speak("Ein Satz kommt hier.", negative)
    assert launcher.asked == [] and launcher.starts == []


def test_the_same_engine_on_the_same_host_twice_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("onefake"))
    launcher = FakeLauncher()
    speaker = _speaker(tmp_path, launcher)
    with pytest.raises(InvalidRequest, match="twice"):
        speaker.open_stream(SpeakRequest(language="de", paragraph_pause_ms=0, engines=(
            EngineRef("onefake", "sentence", None), EngineRef("onefake", "whole", None),  # another unit is no other entry
        )))
    assert launcher.asked == []


def test_one_engine_on_this_machine_and_on_a_host_are_two_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, _remote_capable(FakeEngine("onefake")))
    speaker = _speaker(tmp_path, hosts=(ARAGON,))
    assert len(speaker._bind(speaker._refs_for(SpeakRequest(language="de", paragraph_pause_ms=0, engines=(
        EngineRef("onefake", "sentence", None), EngineRef("onefake", "sentence", ARAGON.name),
    ))))) == 2


def test_an_unknown_engine_or_host_is_refused_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("onefake"))
    launcher = FakeLauncher({"onefake": None})
    speaker = _speaker(tmp_path, launcher)
    with pytest.raises(UnknownEngine):
        speaker.open_stream(request("onefake", "nope"))
    with pytest.raises(UnknownHost, match="Ghost"):
        speaker.open_stream(SpeakRequest(language="de", paragraph_pause_ms=0, engines=(EngineRef("onefake", "sentence", "Ghost"),)))
    assert launcher.asked == [] and launcher.starts == []


def test_an_engine_without_rest_api_on_a_host_and_an_unknown_unit_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeEngine("onefake"))  # no default_port: no REST API
    speaker = _speaker(tmp_path, hosts=(ARAGON,))
    with pytest.raises(InvalidRequest, match="no REST API"):
        speaker.open_stream(SpeakRequest(language="de", paragraph_pause_ms=0, engines=(EngineRef("onefake", "sentence", ARAGON.name),)))
    with pytest.raises(InvalidRequest, match="unit"):
        speaker.open_stream(SpeakRequest(language="de", paragraph_pause_ms=0, engines=(EngineRef("onefake", "word", None),)))  # type: ignore[arg-type]


# ── What requests share ─────────────────────────────────────────────


def test_two_requests_with_different_lists_share_the_capacity_of_an_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slow, other = FakeEngine("slowfake"), FakeEngine("otherfake")
    slow.max_parallel_requests = 1
    slow.gates = {"Satz": asyncio.Event()}
    install(monkeypatch, slow, other)
    speaker = _speaker(tmp_path)

    async def scenario() -> None:
        first = asyncio.create_task(collect(speaker.speak("Der erste Satz hier.", request("slowfake"))))
        second = asyncio.create_task(collect(speaker.speak("Der zweite Satz hier.", request("slowfake", "otherfake"))))
        await asyncio.sleep(0.05)
        slow.gates["Satz"].set()
        await asyncio.gather(first, second)

    run_async(scenario())
    assert slow.peak == 1  # max_parallel 1 holds across both clients


def test_a_new_configuration_does_not_double_the_capacity_of_an_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine("slowfake")
    engine.max_parallel_requests = 1
    engine.gates = {"Satz": asyncio.Event()}
    install(monkeypatch, engine)
    config = SuiteConfig(voices_dir=tmp_path)
    speaker = Speaker(config, FakeLauncher())

    async def scenario() -> None:
        first = asyncio.create_task(collect(speaker.speak("Der erste Satz hier.", request("slowfake"))))
        await asyncio.sleep(0.02)
        speaker.set_config(config)                       # a reload, a changed voice ...
        second = asyncio.create_task(collect(speaker.speak("Der zweite Satz hier.", request("slowfake"))))
        await asyncio.sleep(0.02)
        engine.gates["Satz"].set()
        await asyncio.gather(first, second)

    run_async(scenario())
    assert engine.peak == 1


def test_the_bound_engine_of_an_entry_is_kept_until_the_configuration_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("onefake"))
    speaker = _speaker(tmp_path)
    before = speaker.entry_for("onefake", None, "sentence")
    assert speaker.entry_for("onefake", None, "whole").engine is before.engine  # the unit is the request's, the engine is shared
    speaker.set_config(speaker.config)
    assert speaker._engines == {}  # rebuilt on the next request


# ── Runs ────────────────────────────────────────────────────────────


def test_the_state_of_a_run_is_kept_for_the_next_call_with_the_same_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken, healthy = FakeEngine("brokenfake", fail="engine"), FakeEngine("healthyfake")
    install(monkeypatch, broken, healthy)
    now = [0.0]
    speaker = Speaker(SuiteConfig(voices_dir=tmp_path), FakeLauncher(), clock=lambda: now[0])
    named = request("brokenfake", "healthyfake", run="reading-1")

    async def scenario() -> None:
        await collect(speaker.speak(SENTENCES[0], named))
        broken.fail = None                              # healthy again, but the run remembers
        await collect(speaker.speak(SENTENCES[1], named))
        await collect(speaker.speak(SENTENCES[2], request("brokenfake", "healthyfake", run="reading-2")))
        now[0] = 601                                    # reading-1 is forgotten
        await collect(speaker.speak(SENTENCES[0], named))

    run_async(scenario())
    assert len(broken.texts) == 3 and len(healthy.texts) == 2  # tries: first call, reading-2, after the expiry


def test_a_run_is_identified_by_its_id_and_its_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken, healthy = FakeEngine("brokenfake", fail="engine"), FakeEngine("healthyfake")
    install(monkeypatch, broken, healthy)
    speaker = _speaker(tmp_path)

    async def scenario() -> None:
        both = request("brokenfake", "healthyfake", run="r-1")
        await collect(speaker.speak(SENTENCES[0], both))          # brokenfake fails inside this run
        broken.fail = None
        await collect(speaker.speak(SENTENCES[1], both))          # same run: still remembers the failure
        assert len(broken.texts) == 1
        await collect(speaker.speak(SENTENCES[2], request("brokenfake", run="r-1")))
        assert len(broken.texts) == 2                             # another list: a run of its own, tries again
        paragraph = SpeakRequest(language="de", paragraph_pause_ms=0, run="r-1", engines=(
            EngineRef("brokenfake", "paragraph", None), EngineRef("healthyfake", "sentence", None),
        ))
        await collect(speaker.speak(SENTENCES[2], paragraph))     # another unit is another list too
        assert len(broken.texts) == 3

    run_async(scenario())


def test_a_code_block_that_opens_in_one_call_and_closes_in_the_next_of_the_same_run_does_not_swallow_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine("fake")
    install(monkeypatch, engine)
    speaker = _speaker(tmp_path)
    named = request("fake", run="reading-1")

    async def scenario() -> None:
        await collect(speaker.speak("Hier kommt ein Beispiel für dich:\n\n```python\nprint(x)\n", named))
        await collect(speaker.speak("```\n\nDanach geht es wirklich weiter. Und noch ein ganz normaler Satz.", named))

    run_async(scenario())
    # (the second call starts with a lone fence, so its cutter sees an unclosed block and keeps the rest as one unit)
    assert engine.texts[-1] == "Danach geht es wirklich weiter. Und noch ein ganz normaler Satz."


# ── Configuration ───────────────────────────────────────────────────


def test_a_broken_configuration_leaves_the_old_one_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEngine("onefake"))
    speaker = _speaker(tmp_path)
    with pytest.raises(ConfigInvalid):
        speaker.set_config(SuiteConfig(voices_dir=tmp_path, engines={"nope": EngineDefaults()}))

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(TEXT, request("onefake")))

    assert len(run_async(scenario())) == 3



# ── The suite's own list ("auto") ───────────────────────────────────


def _auto(*entries: tuple[str, bool]) -> tuple[AutoEntry, ...]:
    return tuple(AutoEntry(key, "sentence", None, enabled) for key, enabled in entries)


def _auto_request(**kwargs: object) -> SpeakRequest:
    return SpeakRequest(language="de", paragraph_pause_ms=0, engines=AUTO, **kwargs)  # type: ignore[arg-type]


def test_auto_speaks_by_the_switched_on_entries_of_the_configured_list_in_its_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    off, broken, healthy, spare = FakeEngine("offfake"), FakeEngine("brokenfake", fail="engine"), FakeEngine("healthyfake"), FakeEngine("sparefake")
    install(monkeypatch, off, broken, healthy, spare)
    launcher = FakeLauncher()
    speaker = _speaker(tmp_path, launcher, auto_engines=_auto(("offfake", False), ("brokenfake", True), ("healthyfake", True)))

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(SENTENCES[0], _auto_request()))

    assert [segment.engine for segment in run_async(scenario()) if segment.audio] == ["healthyfake"]
    assert len(broken.texts) == 1                      # tried first, failed before the first sound
    assert off.texts == [] and spare.texts == []       # switched off, and not in the list
    assert "offfake" not in launcher.asked and "sparefake" not in launcher.asked


@pytest.mark.parametrize("auto_engines", [(), _auto(("onefake", False))])
def test_auto_without_a_switched_on_entry_has_nothing_to_speak_with_and_starts_nothing(
    auto_engines: tuple[AutoEntry, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeEngine("onefake"))
    launcher = FakeLauncher({"onefake": None})
    speaker = _speaker(tmp_path, launcher, auto_engines=auto_engines)
    with pytest.raises(NoEngineAvailable, match="auto list"):
        speaker.open_stream(_auto_request())
    assert launcher.asked == [] and launcher.starts == []


def test_a_request_with_its_own_list_does_not_use_the_auto_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listed, own = FakeEngine("listedfake"), FakeEngine("ownfake")
    install(monkeypatch, listed, own)
    speaker = _speaker(tmp_path, auto_engines=_auto(("listedfake", True)))

    async def scenario() -> list[AudioSegment]:
        return await collect(speaker.speak(SENTENCES[0], request("ownfake")))

    assert [segment.engine for segment in run_async(scenario())] == ["ownfake"]
    assert listed.texts == []


def test_a_changed_auto_list_is_a_new_run_for_the_same_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken, healthy = FakeEngine("brokenfake", fail="engine"), FakeEngine("healthyfake")
    install(monkeypatch, broken, healthy)
    speaker = _speaker(tmp_path, auto_engines=_auto(("brokenfake", True), ("healthyfake", True)))
    named = _auto_request(run="r-1")

    async def scenario() -> None:
        await collect(speaker.speak(SENTENCES[0], named))     # brokenfake fails inside this run
        broken.fail = None
        await collect(speaker.speak(SENTENCES[1], named))     # same list: the run remembers the failure
        assert len(broken.texts) == 1
        speaker.set_config(SuiteConfig(voices_dir=tmp_path, auto_engines=_auto(("brokenfake", True))))
        await collect(speaker.speak(SENTENCES[2], named))     # another list behind "auto": a run of its own
        assert len(broken.texts) == 2

    run_async(scenario())


def _browser(key: str) -> FakeEngine:
    engine = FakeEngine(key)
    engine.renders_in_browser = True
    return engine


@pytest.mark.parametrize(("auto_engines", "message"), [
    ((AutoEntry("onefake", "sentence", "Ghost", True),), "unknown host"),
    ((AutoEntry("onefake", "sentence", None, True), AutoEntry("onefake", "whole", None, False)), "twice"),
    ((AutoEntry("nope", "sentence", None, True),), "unknown TTS engine"),
    ((AutoEntry("onefake", "sentence", ARAGON.name, True),), "no REST API"),
    ((AutoEntry("browserfake", "sentence", None, True),), "browser"),
    ((AutoEntry("onefake", "word", None, True),), "unit"),  # type: ignore[arg-type]
])
def test_an_auto_list_no_request_could_name_is_refused_and_the_old_configuration_stays(
    auto_engines: tuple[AutoEntry, ...], message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeEngine("onefake"), _browser("browserfake"))  # onefake has no REST API
    speaker = _speaker(tmp_path, hosts=(ARAGON,), auto_engines=_auto(("onefake", True)))
    with pytest.raises(ConfigInvalid, match=message):
        speaker.set_config(SuiteConfig(voices_dir=tmp_path, hosts=(ARAGON,), auto_engines=auto_engines))
    assert speaker.config.auto_engines == _auto(("onefake", True))


def test_one_engine_on_this_machine_and_on_a_host_are_two_entries_of_the_auto_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, _remote_capable(FakeEngine("onefake")))
    both = (AutoEntry("onefake", "sentence", ARAGON.name, True), AutoEntry("onefake", "whole", None, True))
    speaker = _speaker(tmp_path, hosts=(ARAGON,), auto_engines=both)
    assert [entry.label for entry in speaker._bind(speaker._refs_for(_auto_request()))] == [f"onefake@{ARAGON.name}", "onefake"]
