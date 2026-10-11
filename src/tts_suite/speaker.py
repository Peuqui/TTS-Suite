"""The speaking part of the suite: configuration -> runs and streams.

Owns what readings share — the bound engines, the engine slots, the table of runs — and
opens the streams. It keeps no escalation list: every request brings its own, bound here.
``TTSSuite`` (api) wraps it together with the engine management.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable

from .api import (
    Host,
    InvalidRequest,
    SpeakRequest,
    SuiteConfig,
    Unit,
    UnknownHost,
    UnsupportedLanguage,
)
from .constants import KEEPALIVE_SEC, RUN_TTL_S, SYNTHESIS_THREADS
from .engines.base import SPEECH_UNITS, TTSEngine
from .engines.registry import TTS_ENGINES
from .escalation import (
    EngineLauncher,
    EngineSlotTable,
    EscalationEntry,
    RunTable,
    SpeechRun,
    bind_engine,
    check_config,
    entry_label,
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
        self._engines: dict[str, TTSEngine] = {}  # engine@host -> the bound engine, shared by all requests
        self._hosts: dict[str, Host] = {}
        # Blocking syntheses only; health checks and starts keep the default executor to themselves.
        self._executor = ThreadPoolExecutor(SYNTHESIS_THREADS, thread_name_prefix="tts-synthesis")
        self.set_config(config)

    def set_config(self, config: SuiteConfig) -> None:
        """Replace the configuration; :class:`ConfigInvalid` leaves the old one in place. Runs
        under way keep the entries they started with; the engine slots are shared by all readings."""
        hosts = check_config(config)
        self._hosts = hosts
        self._config = config
        self._engines.clear()  # bound to the old hosts, voices tree or registry
        self._slots.reconfigure(config.engines)

    @property
    def config(self) -> SuiteConfig:
        return self._config

    def entry_for(self, engine_key: str, host_name: str | None, unit: Unit) -> EscalationEntry:
        """The entry for an engine on a host (``None`` = this machine). The bound engine is the one
        every request and every catalog row of the same engine@host uses, so they share its slots.
        :class:`UnknownEngine`, :class:`UnknownHost`, :class:`InvalidRequest` (unit, engine without
        REST API on a host)."""
        if unit not in SPEECH_UNITS:
            raise InvalidRequest(f"unit must be one of {SPEECH_UNITS}, got {unit!r}")
        host = self._host(host_name)
        label = entry_label(engine_key, host)
        if label not in self._engines:
            self._engines[label] = bind_engine(engine_key, host, self._config.voices_dir)
        return EscalationEntry(self._engines[label], host, unit)

    def catalog_entries(self) -> list[EscalationEntry]:
        """What the suite offers: every registered engine on this machine, and on each configured host
        every engine with a REST API (cloud and browser engines cannot run there). The unit of such an
        entry is the engine's recommendation; it only serves as a handle for status, start and stop."""
        engines = list(TTS_ENGINES.values())
        placements: list[tuple[TTSEngine, str | None]] = [(engine, None) for engine in engines]
        for host in self._config.hosts:
            placements.extend((engine, host.name) for engine in engines if engine.default_port is not None)
        return [self.entry_for(engine.key, host_name, engine.default_speech_unit) for engine, host_name in placements]

    def _host(self, host_name: str | None) -> Host | None:
        if host_name is None:
            return None
        if host_name not in self._hosts:
            raise UnknownHost(f"unknown host {host_name!r}")
        return self._hosts[host_name]

    def open_stream(self, request: SpeakRequest) -> Stream:
        self._require_supported_language(request)
        if request.paragraph_pause_ms < 0:
            raise InvalidRequest(f"paragraph_pause_ms must not be negative, got {request.paragraph_pause_ms}")
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
        """The request's list, bound, in its order. Nothing is optional: an empty list is refused."""
        if not request.engines:
            raise InvalidRequest("the request's list of engines is empty")
        entries = tuple(self.entry_for(ref.engine, ref.host, ref.unit) for ref in request.engines)
        labels = [entry.label for entry in entries]
        for label in labels:
            if labels.count(label) > 1:
                raise InvalidRequest(f"{label!r} is in the list twice")
        return entries

    def _run_ended(self, request: SpeakRequest) -> None:
        if request.run is not None:
            self._runs.touch((request.run, request.engines))

    def _run_for(self, request: SpeakRequest, entries: tuple[EscalationEntry, ...]) -> SpeechRun:
        def create() -> SpeechRun:
            return SpeechRun(entries, self._launcher, self._slots, self._config.engines, self._executor)

        if request.run is None:
            return create()
        return self._runs.get_or_create((request.run, request.engines), create)
