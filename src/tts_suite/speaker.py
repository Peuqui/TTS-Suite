"""The speaking part of the suite: configuration -> runs and streams.

Owns what readings share — the escalation list, the engine slots, the table of runs — and
opens the streams. ``TTSSuite`` (api) wraps it together with the engine management.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable

from .api import (
    NoEngineAvailable,
    SpeakRequest,
    SuiteConfig,
    UnknownEngine,
    UnsupportedLanguage,
)
from .constants import KEEPALIVE_SEC, RUN_TTL_S, SYNTHESIS_THREADS
from .engines.registry import get_engine
from .escalation import (
    EngineLauncher,
    EngineSlotTable,
    EscalationEntry,
    RunTable,
    SpeechRun,
    bind_entries,
)
from .i18n import supported_languages
from .stream import Stream


class AttachedStream(Stream):
    """A :class:`Stream` that is attached to its run while it lives: the run holds the containers it
    speaks with (no stop, heartbeat) until the last attached stream has ended. A stream that is
    neither read to its end nor closed keeps them held."""

    def __init__(self, request: SpeakRequest, run: SpeechRun, on_detached: Callable[[], None], **options: Any) -> None:
        super().__init__(request, run, **options)
        self._attached_run = run
        self._attached_run.attach()
        self._on_detached = on_detached
        self._detached = False

    async def aclose(self) -> None:
        await super().aclose()
        if not self._detached:
            self._detached = True
            self._attached_run.detach()
            self._on_detached()


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
        self._slots = EngineSlotTable(config.engines)
        # Blocking syntheses only; health checks and starts keep the default executor to themselves.
        self._executor = ThreadPoolExecutor(SYNTHESIS_THREADS, thread_name_prefix="tts-synthesis")
        self.set_config(config)

    def set_config(self, config: SuiteConfig) -> None:
        """Replace the configuration; :class:`ConfigInvalid` leaves the old one in place. Runs
        under way keep the list they started with; the engine slots are shared by all readings."""
        self._entries = bind_entries(config)
        self._config = config
        self._slots.reconfigure(config.engines)

    @property
    def config(self) -> SuiteConfig:
        return self._config

    @property
    def entries(self) -> tuple[EscalationEntry, ...]:
        """The escalation list of the configuration, bound (disabled entries included)."""
        return self._entries

    def open_stream(self, request: SpeakRequest) -> Stream:
        self._require_supported_language(request)
        entries = self._entries_for(request)
        run = self._run_for(request, entries)
        return AttachedStream(
            request, run, partial(self._run_ended, request),
            keepalive_s=self._keepalive_s, pronunciations=self._config.pronunciations,
        )

    def speak(self, text: str, request: SpeakRequest) -> Stream:
        """The whole text at once: the stream of :meth:`open_stream` with all text handed over."""
        stream = self.open_stream(request)
        stream.add_text(text)
        stream.end_text()
        return stream

    async def aclose(self) -> None:
        self._runs.clear()
        # Not waiting: a thread inside a container request cannot be stopped and ends on its own.
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _require_supported_language(request: SpeakRequest) -> None:
        languages = supported_languages()
        if request.language not in languages:
            raise UnsupportedLanguage(
                f"the suite has no texts in language {request.language!r} (have {sorted(languages)})"
            )

    def _entries_for(self, request: SpeakRequest) -> tuple[EscalationEntry, ...]:
        """The list, or — for a request that names engines — only their entries, in the order of the
        request (several entries of one engine keep the order of the list)."""
        keys = self._requested_engines(request)
        if not keys:
            return self._entries
        for key in keys:
            if get_engine(key) is None:
                raise UnknownEngine(f"unknown TTS engine {key!r}")
        chosen: list[EscalationEntry] = []
        for key in keys:
            entries = [entry for entry in self._entries if entry.engine.key == key]
            if not entries:
                raise NoEngineAvailable(f"engine {key!r} is not in the escalation list", ())
            chosen.extend(entries)
        return tuple(chosen)

    @staticmethod
    def _requested_engines(request: SpeakRequest) -> tuple[str, ...]:
        """The engine keys of the request without repeats: naming an engine twice asks for nothing more."""
        return tuple(dict.fromkeys(request.engines))

    def _run_ended(self, request: SpeakRequest) -> None:
        if request.run is not None:
            self._runs.touch((request.run, self._requested_engines(request)))

    def _run_for(self, request: SpeakRequest, entries: tuple[EscalationEntry, ...]) -> SpeechRun:
        def create() -> SpeechRun:
            return SpeechRun(entries, self._launcher, self._slots, self._config.engines, self._executor)

        if request.run is None:
            return create()
        return self._runs.get_or_create((request.run, self._requested_engines(request)), create)
