"""Fakes for the speaking tests: an engine and a launcher that need no Docker, network or GPU."""
from __future__ import annotations

import asyncio
import io
import wave
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any, Coroutine, TypeVar

from tts_suite.api import AudioSegment, EngineDefaults, EngineRef, EngineStarting, SpeakRequest, Unit
from tts_suite.engines.base import TTSEngine, TTSFailure
from tts_suite.escalation import EngineSlotTable, EscalationEntry, Skip, SpeechRun
from tts_suite.stream import Stream

SAMPLE_RATE = 24000
FRAMES_PER_CHARACTER = 10
T = TypeVar("T")

#: The pool the fakes' blocking syntheses would run in (a real one; the speaker owns the production pool).
TEST_EXECUTOR = ThreadPoolExecutor(4)


def run_async(scenario: Coroutine[Any, Any, T], timeout: float = 10) -> T:
    """Run a test scenario in its own event loop; a hang becomes a failure."""
    async def guarded() -> T:
        return await asyncio.wait_for(scenario, timeout)
    return asyncio.run(guarded())


def wav_bytes(frames: int, rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(2 * frames))
    return buffer.getvalue()


def wav_info(audio: bytes) -> tuple[int, int]:
    """(frames, sample rate) of a WAV file — the test reads the length as the identity of a unit."""
    with wave.open(io.BytesIO(audio), "rb") as source:
        return source.getnframes(), source.getframerate()


def frames_for(text: str) -> int:
    return len(text) * FRAMES_PER_CHARACTER


SENTENCES = ["Der erste Satz kommt hier.", "Danach folgt der zweite Satz noch.", "Zuletzt kommt noch ein dritter Satz hier."]
TEXT = " ".join(SENTENCES)


async def collect(stream: Stream) -> list[AudioSegment]:
    return [segment async for segment in stream.segments()]


def lengths(segments: list[AudioSegment]) -> list[int]:
    """The frame counts of the segments — the identity of what was spoken (``frames_for``)."""
    return [wav_info(segment.audio)[0] for segment in segments]


class FakeEngine(TTSEngine):
    """Speaks by returning a WAV whose length encodes the text (``frames_for``).

    ``fail``: every synthesis fails with this reason, ``fail_after``: only from the n-th call on.
    ``gates``: a text containing a key waits until that event is set."""

    default_voice = "V"
    max_parallel_requests = 2

    def __init__(
        self, key: str, *, remote: bool = False, fail: Any = None, fail_after: int | None = None,
        unit: Unit = "sentence", cloud: bool = False,
    ) -> None:
        super().__init__(address="192.0.2.2" if remote else "localhost")
        self.key = key
        self.label_short = key.upper()
        self.default_speech_unit = unit
        self.cloud = cloud
        self.fail = fail
        self.fail_after = fail_after
        self.texts: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.running = 0
        self.peak = 0
        self.cancelled = 0

    def _bound(self, **changes: Any) -> Any:
        return self  # the test keeps one object to look at; binding changes nothing for the fake

    def get_voices(self) -> dict[str, str]:
        return {}

    async def generate_speech_async(
        self, text: str, voice: str, language: str, speed: float = 1.0, pitch: float = 1.0, *, executor: Executor,
    ) -> bytes:
        self.texts.append(text)
        calls = len(self.texts)
        if self.fail and (self.fail_after is None or calls > self.fail_after):
            raise TTSFailure(self.fail, f"{self.key} broke")
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            for key, gate in self.gates.items():
                if key in text:
                    await gate.wait()
            return wav_bytes(frames_for(text))
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.running -= 1


class GatedFailEngine(FakeEngine):
    """Like ``FakeEngine``, but a text containing a key of ``fail_gates`` WAITS for that event and
    only then fails (``unreachable``) — a failure that arrives later, while other sentences run, as
    timeouts and broken connections do. ``FakeEngine.fail`` raises before the first ``await``, so
    no sentence is ever in flight at the same time there."""

    def __init__(self, key: str, **kwargs: Any) -> None:
        super().__init__(key, **kwargs)
        self.fail_gates: dict[str, asyncio.Event] = {}

    async def generate_speech_async(
        self, text: str, voice: str, language: str, speed: float = 1.0, pitch: float = 1.0, *, executor: Executor,
    ) -> bytes:
        for marker, gate in self.fail_gates.items():
            if marker in text:
                self.texts.append(text)
                await gate.wait()
                raise TTSFailure("unreachable", f"{self.key} broke on {marker}")
        return await super().generate_speech_async(text, voice, language, speed, pitch, executor=executor)


class FakeLauncher:
    """``down``: label -> what ``ensure_serving`` answers while the entry is not serving
    (``None`` = it starts and serves from then on). Everything else serves."""

    def __init__(self, down: dict[str, Skip | None] | None = None) -> None:
        self.down = dict(down or {})
        self.starts: list[str] = []
        self.asked: list[str] = []
        self.held: list[str] = []  # labels held right now, one item per hold

    async def is_serving(self, entry: EscalationEntry) -> bool:
        self.asked.append(entry.label)
        return entry.label not in self.down

    async def ensure_serving(self, entry: EscalationEntry, *, wait: bool) -> Skip | None:
        if not wait:
            raise EngineStarting(f"{entry.label} is starting")
        await asyncio.sleep(0)  # a start takes time: parallel sentences must not start it twice
        self.starts.append(entry.label)
        outcome = self.down[entry.label]
        if outcome is None:
            del self.down[entry.label]
        return outcome

    def hold(self, entry: EscalationEntry) -> None:
        self.held.append(entry.label)

    def release(self, entry: EscalationEntry) -> None:
        self.held.remove(entry.label)


def entry(engine: TTSEngine, host: Any = None, unit: Unit | None = None) -> EscalationEntry:
    """An entry of a request's list; the unit is the engine's own unless the test says otherwise."""
    return EscalationEntry(engine, host, unit or engine.default_speech_unit)


def refs(*keys: str, unit: Unit = "sentence", host: str | None = None) -> tuple[EngineRef, ...]:
    """The list of a request: these engines, in this order, all with the same unit and host."""
    return tuple(EngineRef(key, unit, host) for key in keys)


def request(*keys: str, language: str = "de", paragraph_pause_ms: int = 0, **kwargs: Any) -> SpeakRequest:
    """A request whose list names these engines (all by sentence, on this machine)."""
    return SpeakRequest(language=language, engines=refs(*keys), paragraph_pause_ms=paragraph_pause_ms, **kwargs)


def make_run(
    entries: list[EscalationEntry], launcher: FakeLauncher | None = None,
    defaults: dict[str, EngineDefaults] | None = None,
) -> SpeechRun:
    defaults = defaults or {}
    return SpeechRun(tuple(entries), launcher or FakeLauncher(), EngineSlotTable(defaults), defaults, TEST_EXECUTOR)

