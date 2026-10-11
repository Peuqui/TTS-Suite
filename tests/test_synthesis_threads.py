"""Blocking syntheses run in the speaker's own thread pool; what ``aclose`` does and does not stop."""
from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import Executor
from pathlib import Path

import pytest

from container_fakes import CardList, ContainerEngine, FakeSsh, install, local_entry, make_config
from speech_fakes import FakeEngine, FakeLauncher, frames_for, request, run_async, wav_bytes

from tts_suite.api import AudioSegment, EngineDefaults, SuiteError
from tts_suite.engines.base import TTSEngine
from tts_suite.launcher import SuiteLauncher
from tts_suite.speaker import Speaker
from tts_suite.stream import Stream

REQUEST = request("blocking")


class ThreadEngine(FakeEngine):
    """Like the container engines: the synthesis is a blocking call in a thread (it stands for
    ``requests.post(..., timeout=600)``) that returns when the test lets it."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.max_parallel_requests = 100
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.finished = 0
        self.thread_names: list[str] = []

    async def generate_speech_async(
        self, text: str, voice: str, language: str, speed: float = 1.0, pitch: float = 1.0, *, executor: Executor,
    ) -> bytes:
        return await TTSEngine.generate_speech_async(self, text, voice, language, speed, pitch, executor=executor)

    def generate_speech(self, text: str, voice: str, language: str, speed: float = 1.0, pitch: float = 1.0) -> bytes:
        self.thread_names.append(threading.current_thread().name)
        self.started.release()
        self.release.wait(timeout=30)
        self.finished += 1
        return wav_bytes(frames_for(text))


async def collect(stream: Stream) -> list[AudioSegment]:
    return [segment async for segment in stream.segments()]


def _speaker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ThreadEngine) -> Speaker:
    install(monkeypatch, engine)
    config = make_config(tmp_path, {engine.key: EngineDefaults(max_parallel=100, max_queue=100)})
    return Speaker(config, FakeLauncher())


def test_a_health_check_is_not_starved_by_syntheses_that_fill_the_default_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = ThreadEngine("blocking")
    speaker = _speaker(tmp_path, monkeypatch, engine)
    default_executor_size = min(32, (os.cpu_count() or 1) + 4)
    container = ContainerEngine("container")
    container.up = True
    launcher = SuiteLauncher(make_config(tmp_path), gpus=CardList(20000), host_command=FakeSsh())

    async def scenario() -> float:
        streams = [speaker.speak(f"Satz Nummer {number} hier.", REQUEST) for number in range(default_executor_size + 2)]
        readers = [asyncio.create_task(collect(stream)) for stream in streams]
        for _ in streams:
            await asyncio.to_thread(engine.started.acquire)  # every synthesis sits in a thread now
        asked = time.monotonic()
        assert await launcher.is_serving(local_entry(container))
        waited = time.monotonic() - asked
        engine.release.set()
        await asyncio.gather(*readers)
        return waited

    try:
        waited = run_async(scenario(), timeout=30)
    finally:
        engine.release.set()
    assert waited < 0.3  # the health check has its own 0.5 s budget; it is no longer queued behind syntheses
    assert {name.split("_")[0] for name in engine.thread_names} == {"tts-synthesis"}
    run_async(speaker.aclose())


def test_aclose_ends_the_stream_at_once_but_the_thread_of_a_running_request_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = ThreadEngine("blocking")
    speaker = _speaker(tmp_path, monkeypatch, engine)

    async def scenario() -> tuple[float, int, int, int]:
        stream = speaker.speak("Der erste Satz kommt hier. Danach folgt der zweite Satz noch.", REQUEST)
        reader = asyncio.create_task(collect(stream))
        await asyncio.to_thread(engine.started.acquire)
        asked = time.monotonic()
        await stream.aclose()
        closed_after = time.monotonic() - asked
        delivered = len(await reader)
        still_running = len(engine.thread_names) - engine.finished
        engine.release.set()  # the container finishes the request it was given
        for _ in range(100):
            if engine.finished == len(engine.thread_names):
                break
            await asyncio.sleep(0.02)
        return closed_after, delivered, still_running, engine.finished

    try:
        closed_after, delivered, still_running, finished = run_async(scenario(), timeout=30)
    finally:
        engine.release.set()
    assert closed_after < 0.5 and delivered == 0  # the coroutine is cancelled, the stream is over
    assert still_running >= 1 and finished >= 1  # documented: the running request is not cancelled
    run_async(speaker.aclose())


def test_closing_the_speaker_shuts_the_synthesis_threads_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ThreadEngine("blocking")
    engine.release.set()
    speaker = _speaker(tmp_path, monkeypatch, engine)

    async def scenario() -> None:
        await speaker.aclose()
        with pytest.raises(SuiteError):  # no thread to run on: the only entry fails, nobody is left
            await collect(speaker.speak("Ein Satz kommt hier.", REQUEST))

    run_async(scenario())
