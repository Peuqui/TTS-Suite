"""The speaking part of the suite: configuration -> runs and streams.

Owns what readings share — the escalation list, the engine slots, the table of runs — and
opens the streams. ``TTSSuite`` (api) wraps it together with the engine management.
"""

from __future__ import annotations

import time
from typing import Callable

from .api import NoEngineAvailable, SpeakRequest, SuiteConfig, UnknownEngine
from .constants import KEEPALIVE_SEC, RUN_TTL_S
from .engines.registry import get_engine
from .escalation import EngineLauncher, EngineSlotTable, EscalationEntry, RunTable, SpeechRun, bind_entries
from .stream import Stream


class Speaker:
    def __init__(
        self,
        config: SuiteConfig,
        launcher: EngineLauncher,
        *,
        keepalive_s: float = KEEPALIVE_SEC,
        run_ttl_s: float = RUN_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._launcher = launcher
        self._keepalive_s = keepalive_s
        self._runs = RunTable(run_ttl_s, clock)
        self.set_config(config)

    def set_config(self, config: SuiteConfig) -> None:
        """Replace the configuration; :class:`ConfigInvalid` leaves the old one in place. Runs
        and readings under way keep the list and the limits they started with."""
        self._entries = bind_entries(config)
        self._config = config
        self._slots = EngineSlotTable(config.engines)

    def open_stream(self, request: SpeakRequest) -> Stream:
        entries = self._entries_for(request)
        run = self._run_for(request, entries)
        return Stream(request, run, keepalive_s=self._keepalive_s)

    def speak(self, text: str, request: SpeakRequest) -> Stream:
        """The whole text at once: the stream of :meth:`open_stream` with all text handed over."""
        stream = self.open_stream(request)
        stream.add_text(text)
        stream.end_text()
        return stream

    async def aclose(self) -> None:
        self._runs.clear()

    def _entries_for(self, request: SpeakRequest) -> tuple[EscalationEntry, ...]:
        """The list, or — for a request that names an engine — only that engine's entries."""
        if request.engine is None:
            return self._entries
        if get_engine(request.engine) is None:
            raise UnknownEngine(f"unknown TTS engine {request.engine!r}")
        pinned = tuple(entry for entry in self._entries if entry.engine.key == request.engine)
        if not pinned:
            raise NoEngineAvailable(f"engine {request.engine!r} is not in the escalation list", ())
        return pinned

    def _run_for(self, request: SpeakRequest, entries: tuple[EscalationEntry, ...]) -> SpeechRun:
        def create() -> SpeechRun:
            return SpeechRun(entries, self._launcher, self._slots, self._config.engines)

        if request.run is None:
            return create()
        return self._runs.get_or_create((request.run, request.engine), create)
