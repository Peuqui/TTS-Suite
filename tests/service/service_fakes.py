"""A suite that does what a test tells it, for the tests of the service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from tts_suite.api import (
    AudioSegment,
    EngineInfo,
    SpeakRequest,
    SpeakSummary,
    SuiteConfig,
    SuiteError,
    TraceStep,
    VoiceInfo,
)

ENGINE_INFO = EngineInfo(
    engine="xtts",
    host=None,
    label={"de": "XTTS", "en": "XTTS"},
    label_short={"de": "XTTS", "en": "XTTS"},
    enabled=True,
    deactivated=False,
    status="running",
    unit="sentence",
    unit_default="sentence",
    needs_gpu=True,
    cloud=False,
    browser=False,
    vram_mib={"GPU-1": 4000},
)


def audio(engine: str = "xtts", data: bytes = b"RIFF", keepalive: bool = False) -> AudioSegment:
    return AudioSegment(audio=data, media_type="audio/wav", engine=engine, keepalive=keepalive)


class FakeStream:
    """Gives out its items one by one: a segment, or an error that is raised at that point."""

    def __init__(self, items: list[AudioSegment | SuiteError], summary: SpeakSummary | None = None) -> None:
        self.items = items
        self.summary = summary or SpeakSummary(trace=(TraceStep(entry="xtts", outcome="spoke"),))
        self.fed: list[str] = []
        self.finished = False
        self.closed = False
        # A stream whose text comes in pieces ends its segments only when the text is complete.
        self._text_complete = asyncio.Event()

    async def feed(self, text: str) -> None:
        self.fed.append(text)

    async def finish(self) -> None:
        self.finished = True
        self._text_complete.set()

    def whole_text_given(self) -> None:
        self._text_complete.set()

    async def segments(self) -> AsyncIterator[AudioSegment]:
        for item in self.items:
            if isinstance(item, SuiteError):
                raise item
            yield item
        await self._text_complete.wait()

    async def aclose(self) -> None:
        self.closed = True


class FakeSuite:
    def __init__(self, config: SuiteConfig) -> None:
        self._config = config
        self.stream = FakeStream([audio()])
        self.refuses: SuiteError | None = None
        self.requests: list[tuple[str | None, SpeakRequest]] = []
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False
        self.rejects_config: SuiteError | None = None

    async def aclose(self) -> None:
        self.closed = True

    async def config(self) -> SuiteConfig:
        return self._config

    async def set_config(self, config: SuiteConfig) -> None:
        if self.rejects_config is not None:
            raise self.rejects_config
        self._config = config

    async def engines(self) -> list[EngineInfo]:
        return [ENGINE_INFO]

    async def voices(self, engine: str | None = None) -> list[VoiceInfo]:
        self.calls.append(("voices", (engine,)))
        return [VoiceInfo(name="Alice", label="Alice")]

    def speak(self, text: str, request: SpeakRequest) -> FakeStream:
        if self.refuses is not None:
            raise self.refuses
        self.requests.append((text, request))
        self.stream.whole_text_given()
        return self.stream

    def open_stream(self, request: SpeakRequest) -> FakeStream:
        if self.refuses is not None:
            raise self.refuses
        self.requests.append((None, request))
        return self.stream

    async def start(self, engine: str, host: str | None = None) -> None:
        self.calls.append(("start", (engine, host)))

    async def stop(self, engine: str, host: str | None = None) -> None:
        self.calls.append(("stop", (engine, host)))

    async def reload(self) -> tuple[list[str], list[str]]:
        self.calls.append(("reload", ()))
        return ["old"], ["new"]

    async def sync_voices(self, engine: str) -> list[VoiceInfo]:
        self.calls.append(("sync_voices", (engine,)))
        return [VoiceInfo(name="Alice", label="Alice")]


def default_config(tmp_path: Path) -> SuiteConfig:
    return SuiteConfig(voices_dir=tmp_path / "voices")
