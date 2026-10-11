"""The speech stream: a text that arrives in pieces becomes audio segments in speaking order.

Behaviour as in AIfred Intelligence (``speech_synthesis.py``): the first unit is generated at
once and delivered, the others are generated meanwhile (in parallel, as far as the engine's
slots allow) and delivered in speaking order. While a segment takes longer than the keep-alive
interval, a keep-alive marker (a segment without audio) is delivered so that the client sees the stream
is alive; what it does with the marker is its decision (a device channel plays its own silence).

Which unit the text is cut by depends on the engine that speaks. That engine is chosen when
the first text arrives (the choice may start a container), so the stream collects text until
the choice is made and cuts afterwards (:class:`~tts_suite.splitting.UnitCutter`). Each cut unit is
cleaned (:class:`~tts_suite.cleaning.TextCleaner`, one per RUN, so a code block that opens in one call
and closes in the next is still one block) before it goes to the engine; a unit with nothing left to
speak is dropped. Streams that share a run must not run at the same time: they would clean into one state.

Production is coupled to delivery: a unit is started only while fewer than ``max_parallel`` units of
the engine are running or finished-but-not-delivered, so a slow consumer does not have the whole text
generated ahead of it.

A voice change is announced here, in speaking order: when the next segment to deliver comes from
another entry than the last one delivered, and that one dropped out of the run, the announcement
is delivered first, as a segment of its own.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping

from .api import AudioSegment, SpeakRequest, SpeakSummary
from .constants import KEEPALIVE_SEC
from .escalation import EscalationEntry, SpeechRun, Spoken
from .splitting import UnitCutter

#: Sent while a segment takes long: no audio, no format, no engine — the client decides what to do with it.
KEEPALIVE_MARKER = AudioSegment(audio=b"", media_type="", engine="", keepalive=True)


class Stream:
    """One reading, implementing :class:`~tts_suite.api.SpeechStream`."""

    def __init__(
        self, request: SpeakRequest, run: SpeechRun, *, keepalive_s: float = KEEPALIVE_SEC,
        pronunciations: Mapping[str, str] = MappingProxyType({}),
    ) -> None:
        self._request = request
        self._run = run
        self._keepalive_s = keepalive_s
        self._cutter = UnitCutter()
        self._cleaner = run.cleaner(request.language, pronunciations)  # the reading's, not this stream's
        self._units: asyncio.Queue[str | None] = asyncio.Queue()  # cut units; None = text ended
        self._results: asyncio.Queue[asyncio.Task[Spoken] | None] = asyncio.Queue()  # in speaking order
        self._tasks: list[asyncio.Task[Spoken]] = []
        self._pipeline: asyncio.Task[None] | None = None
        self._has_text = False
        self._finished = False
        self._sealed = False  # the end of the results is queued
        self._closed = False
        self._error: Exception | None = None
        self._window: asyncio.Semaphore = asyncio.Semaphore(0)  # replaced when the entry is chosen
        self._last_entry: EscalationEntry | None = None  # who spoke the segment delivered last
        self._delivered_any = False  # the keep-alive starts after the first segment

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
            entry = await self._run.select(self._request)
            # One reading never fills the queue; a place is given back when its segment is delivered
            self._window = asyncio.Semaphore(self._run.max_parallel(entry))
            self._release(self._cutter.set_unit(self._run.unit(entry)))
            if self._finished:
                self._end_input()
            while (raw_unit := await self._units.get()) is not None:
                unit = self._cleaner.clean(raw_unit)  # here, in speaking order: the cleaner keeps the reading's state
                if not unit:
                    continue
                await self._window.acquire()
                task = asyncio.get_running_loop().create_task(self._run.speak(unit, self._request))
                self._tasks.append(task)
                self._results.put_nowait(task)
        except Exception as error:  # noqa: BLE001 — reported to the consumer at the end of the results
            self._error = error
        finally:
            self._results.put_nowait(None)

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
                spoken = task.result()
                async for announcement in self._announce_voice_change(spoken.entry):
                    yield announcement
                if self._closed:
                    return
                self._last_entry = spoken.entry
                self._window.release()  # delivered: the next unit may start
                yield self._segment(spoken)
            if self._error is not None:
                raise self._error
        finally:
            await self.aclose()

    async def _keep_alive_until_done(self, future: asyncio.Future[Any]) -> AsyncIterator[AudioSegment]:
        """Wait for ``future``; every keep-alive interval without result yields a keep-alive marker (not
        before the first delivered segment: the client's connection is only at risk once audio has started)."""
        while not future.done() and not self._closed:
            await asyncio.wait({future}, timeout=self._keepalive_s)
            if not future.done() and self._delivered_any:
                yield KEEPALIVE_MARKER

    async def _announce_voice_change(self, successor: EscalationEntry) -> AsyncIterator[AudioSegment]:
        """Nothing when ``successor`` speaks on after the entry of the last delivered segment (or no
        segment was delivered yet); else the keep-alives while the announcement is made, and the
        announcement, spoken by the run's current entry."""
        if self._last_entry is None or self._last_entry.label == successor.label:
            return
        reason = self._run.failure_of(self._last_entry.label)
        if reason is None:
            return
        text = self._run.announcement(successor, reason, self._request.language)
        task = asyncio.ensure_future(self._run.speak(text, self._request))
        self._tasks.append(task)
        async for keepalive in self._keep_alive_until_done(task):
            yield keepalive
        if self._closed:
            return
        self._run.note_switch(reason)
        yield self._segment(task.result())

    def _segment(self, spoken: Spoken) -> AudioSegment:
        self._delivered_any = True
        return AudioSegment(spoken.audio, spoken.entry.engine.audio_media_type, spoken.entry.label)

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
