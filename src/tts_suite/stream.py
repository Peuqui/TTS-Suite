"""The speech stream: a text that arrives in pieces becomes audio segments in speaking order.

Behaviour as in AIfred Intelligence (``speech_synthesis.py``): the first unit is generated at
once and delivered, the others are generated meanwhile (in parallel, as far as the engine's
slots allow) and delivered in speaking order. While a segment takes longer than the keep-alive
interval, a piece of silence is delivered so that the connection of the client stays alive.

Which unit the text is cut by depends on the engine that speaks. That engine is chosen when
the first text arrives (the choice may start a container), so the stream collects text until
the choice is made and cuts afterwards (:class:`~tts_suite.splitting.UnitCutter`).
"""

from __future__ import annotations

import asyncio
import io
import wave
from typing import Any, AsyncIterator

from .api import AudioSegment, SpeakRequest, SpeakSummary
from .constants import KEEPALIVE_SEC, KEEPALIVE_SILENCE_MS, MEDIA_TYPE_WAV
from .escalation import SpeechRun
from .splitting import UnitCutter

#: Sample value of silence in 8-bit WAV (unsigned); all wider samples are signed, silence is 0.
SILENCE_8BIT = 0x80


def silence_like(reference_wav: bytes, duration_ms: int) -> bytes:
    """A WAV file of silence with the sample rate, channels and sample width of ``reference_wav``."""
    with wave.open(io.BytesIO(reference_wav), "rb") as reference:
        channels, width, rate = reference.getnchannels(), reference.getsampwidth(), reference.getframerate()
    frames = rate * duration_ms // 1000
    sample = bytes([SILENCE_8BIT]) if width == 1 else bytes(width)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as silence:
        silence.setnchannels(channels)
        silence.setsampwidth(width)
        silence.setframerate(rate)
        silence.writeframes(sample * (frames * channels))
    return buffer.getvalue()


class Stream:
    """One reading, implementing :class:`~tts_suite.api.SpeechStream`."""

    def __init__(self, request: SpeakRequest, run: SpeechRun, *, keepalive_s: float = KEEPALIVE_SEC) -> None:
        self._request = request
        self._run = run
        self._keepalive_s = keepalive_s
        self._cutter = UnitCutter()
        self._units: asyncio.Queue[str | None] = asyncio.Queue()  # cut units; None = text ended
        self._results: asyncio.Queue[asyncio.Task[AudioSegment] | None] = asyncio.Queue()  # in speaking order
        self._tasks: list[asyncio.Task[AudioSegment]] = []
        self._pipeline: asyncio.Task[None] | None = None
        self._has_text = False
        self._finished = False
        self._sealed = False  # the end of the results is queued
        self._closed = False
        self._error: Exception | None = None
        self._keepalive: AudioSegment | None = None

    # ── Input ───────────────────────────────────────────────────────

    async def feed(self, text: str) -> None:
        self.add_text(text)

    async def finish(self) -> None:
        self.end_text()

    def add_text(self, text: str) -> None:
        """:meth:`feed` without ``await`` (what ``TTSSuite.speak`` needs to hand over a whole text)."""
        if self._closed or self._finished:
            raise RuntimeError("the stream takes no more text")
        self._has_text = self._has_text or bool(text.strip())
        self._release(self._cutter.feed(text))
        self._ensure_started()

    def end_text(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._cutter.unit is not None:
            self._end_input()
        self._ensure_started()

    def _release(self, units: list[str]) -> None:
        for unit in units:
            self._units.put_nowait(unit)

    def _end_input(self) -> None:
        self._release(self._cutter.flush())
        self._units.put_nowait(None)

    def _ensure_started(self) -> None:
        """Start the pipeline with the first text; a reading without any text just ends."""
        if self._pipeline is None and self._has_text:
            self._pipeline = asyncio.get_running_loop().create_task(self._run_pipeline())
        elif not self._has_text and self._finished and not self._sealed:
            self._sealed = True
            self._results.put_nowait(None)

    # ── Production ──────────────────────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Choose the speaking entry, cut the text by its unit, start one synthesis per unit."""
        try:
            entry = await self._run.select(wait=self._request.wait)
            window = asyncio.Semaphore(self._run.max_parallel(entry))  # one reading never fills the queue
            self._release(self._cutter.set_unit(self._run.unit(entry)))
            if self._finished:
                self._end_input()
            while (unit := await self._units.get()) is not None:
                await window.acquire()
                task = asyncio.get_running_loop().create_task(self._synthesize(unit, window))
                self._tasks.append(task)
                self._results.put_nowait(task)
        except Exception as error:  # noqa: BLE001 — reported to the consumer at the end of the results
            self._error = error
        finally:
            self._results.put_nowait(None)

    async def _synthesize(self, unit: str, window: asyncio.Semaphore) -> AudioSegment:
        try:
            spoken = await self._run.speak(unit, self._request)
        finally:
            window.release()
        return AudioSegment(spoken.audio, spoken.entry.engine.audio_media_type, spoken.entry.label)

    # ── Output ──────────────────────────────────────────────────────

    async def segments(self) -> AsyncIterator[AudioSegment]:
        try:
            while True:
                self._ensure_started()
                getter = asyncio.ensure_future(self._results.get())
                try:
                    async for keepalive in self._keep_alive_until_done(getter):
                        yield keepalive
                finally:
                    if not getter.done():
                        getter.cancel()
                if self._closed:
                    return
                task = getter.result()
                if task is None:
                    break
                async for keepalive in self._keep_alive_until_done(task):
                    yield keepalive
                if self._closed:
                    return
                segment = task.result()
                self._remember_silence(segment)
                yield segment
            if self._error is not None:
                raise self._error
        finally:
            await self.aclose()

    async def _keep_alive_until_done(self, future: asyncio.Future[Any]) -> AsyncIterator[AudioSegment]:
        """Wait for ``future``; every keep-alive interval without result yields a piece of silence."""
        while not future.done() and not self._closed:
            await asyncio.wait({future}, timeout=self._keepalive_s)
            if not future.done() and self._keepalive is not None:
                yield self._keepalive

    def _remember_silence(self, segment: AudioSegment) -> None:
        """The keep-alive silence has the format of the stream, known from its first segment.
        Only a WAV stream can be filled with a WAV silence."""
        if self._keepalive is None and segment.media_type == MEDIA_TYPE_WAV:
            self._keepalive = AudioSegment(
                silence_like(segment.audio, KEEPALIVE_SILENCE_MS), MEDIA_TYPE_WAV, "", keepalive=True,
            )

    @property
    def summary(self) -> SpeakSummary:
        return SpeakSummary(trace=self._run.trace, switch=self._run.switch)

    async def aclose(self) -> None:
        """Stop what is still being generated; the slots and the selection lock are released by the
        cancelled tasks. After a normal end there is nothing left to stop."""
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in (self._pipeline, *self._tasks) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # also fetches errors nobody asked for
        self._results.put_nowait(None)  # wakes a consumer that is still waiting
