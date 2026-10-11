"""Escalation: which entry of the list speaks, and what happens when one fails.

Ported from AIfred Intelligence (``tts_escalation.py``). The list is the request's and ordered; the first
entry that can speak speaks.

* Before the first sound an entry that fails drops out silently, the next one takes over.
* An entry that fails AFTER it has spoken is replaced the same way. The announcement of the voice
  change (with the reason) is the stream's business: it decides in speaking order, when the
  successor's first segment is next in line (see :mod:`tts_suite.stream`).
* An entry that failed stays out for the rest of the run, so the voice does not jump back and forth.
* The selection is serialised per run, so sentences generated in parallel do not start a
  container twice.

What this module does not do: starting containers (card choice, SSH) is the job of an
:class:`EngineLauncher`; a client program's own voice (the browser entry) never speaks here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from concurrent.futures import Executor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping, Protocol

from .api import (
    ConfigInvalid,
    EngineDefaults,
    EngineRef,
    FailureReason,
    Host,
    InvalidRequest,
    NoEngineAvailable,
    Overloaded,
    SkipReason,
    SpeakRequest,
    SuiteConfig,
    SuiteError,
    TraceStep,
    Unit,
    UnknownEngine,
    VoiceSettings,
)
from .cleaning import TextCleaner
from .constants import HOST_ADDRESS_PATTERN, HOST_SSH_USER_PATTERN, PITCH_DEFAULT, RUN_TTL_S, SPEED_DEFAULT
from .engines.base import DeactivatedEngine, TTSEngine, TTSFailure
from .engines.registry import get_engine, is_deactivated
from .i18n import translate
from .synthesis import synthesize

logger = logging.getLogger(__name__)


# ── Entries ─────────────────────────────────────────────────────────


def entry_label(engine_key: str, host: Host | None) -> str:
    """``engine@host``, or ``engine`` on this machine — the name in traces and segments."""
    return engine_key if host is None else f"{engine_key}@{host.name}"


@dataclass(frozen=True)
class EscalationEntry:
    """One entry of a request's list: an engine, bound to a host (``None`` = this machine), with the
    unit the request asked for."""

    engine: TTSEngine
    host: Host | None
    unit: Unit

    @property
    def label(self) -> str:
        return entry_label(self.engine.key, self.host)


@dataclass(frozen=True)
class Skip:
    """Why an entry is passed over before an attempt: the reason, and a text that says more
    (``TraceStep.detail``)."""

    reason: SkipReason
    detail: str = ""


class EngineLauncher(Protocol):
    """Brings the container of an entry up (card choice for local GPU engines, SSH for engines on
    another host) and watches it. The escalation only asks; it never starts anything itself."""

    async def is_serving(self, entry: EscalationEntry) -> bool:
        """Whether the entry can take a request right now (a cheap check, never starts anything)."""
        ...

    async def ensure_serving(self, entry: EscalationEntry, *, wait: bool) -> Skip | None:
        """Start the entry and wait until it serves; ``None`` = it serves now. A :class:`Skip` when it
        cannot (``stopped``: no way to start it or the start failed, ``no_vram``: no card fits).
        With ``wait=False`` a start that is needed is begun and ``EngineStarting`` raised."""
        ...

    def hold(self, entry: EscalationEntry) -> None:
        """A reading uses the entry: its container is not stopped and is kept alive until :meth:`release`."""
        ...

    def release(self, entry: EscalationEntry) -> None:
        ...


def _index_hosts(config: SuiteConfig) -> dict[str, Host]:
    hosts: dict[str, Host] = {}
    for host in config.hosts:
        if host.name in hosts:
            raise ConfigInvalid(f"hosts: duplicate host name {host.name!r}")
        if not re.fullmatch(HOST_ADDRESS_PATTERN, host.address):
            raise ConfigInvalid(f"hosts[{host.name}]: address {host.address!r} is not a host name or address")
        if host.ssh_user and not re.fullmatch(HOST_SSH_USER_PATTERN, host.ssh_user):
            raise ConfigInvalid(f"hosts[{host.name}]: ssh_user {host.ssh_user!r} is not a user name")
        hosts[host.name] = host
    return hosts


def _known(engine_key: str) -> bool:
    return get_engine(engine_key) is not None or is_deactivated(engine_key)


def _check_defaults(config: SuiteConfig) -> None:
    for key, defaults in config.engines.items():
        if not _known(key):
            raise ConfigInvalid(f"engines: defaults for unknown TTS engine {key!r}")
        if defaults.max_parallel is not None and defaults.max_parallel < 1:
            raise ConfigInvalid(f"engines[{key}]: max_parallel must be at least 1")
        if defaults.max_queue is not None and defaults.max_queue < 0:
            raise ConfigInvalid(f"engines[{key}]: max_queue must not be negative")


def _check_pronunciations(config: SuiteConfig) -> None:
    for word in config.pronunciations:
        if not word.strip():
            raise ConfigInvalid("pronunciations: a word must not be empty")


def check_config(config: SuiteConfig) -> dict[str, Host]:
    """THE check of a configuration: whatever cannot work (a missing ``voices_dir``, a bad host,
    defaults for an unknown engine or with bad limits) is :class:`ConfigInvalid`. Returns the hosts
    by name — what a request's entries are bound to."""
    if not config.voices_dir.is_dir():
        raise ConfigInvalid(f"voices_dir {config.voices_dir} is not a directory")
    hosts = _index_hosts(config)
    _check_defaults(config)
    _check_pronunciations(config)
    return hosts


def bind_engine(engine_key: str, host: Host | None, voices_dir: Path) -> TTSEngine:
    """The registry's engine bound to the shared voices tree and to ``host`` (``None`` = this machine).
    :class:`UnknownEngine` for an engine the suite does not have; :class:`InvalidRequest` for an engine
    without a REST API on a host. An engine whose folder lies in ``engines/disabled/`` becomes a
    placeholder that is passed over."""
    engine = get_engine(engine_key)
    if engine is None:
        if is_deactivated(engine_key):
            return DeactivatedEngine(engine_key)
        raise UnknownEngine(f"unknown TTS engine {engine_key!r}")
    engine = engine.with_voices_dir(voices_dir)
    if host is None:
        return engine
    try:
        return engine.at_host(host.address)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


# ── Engine slots ────────────────────────────────────────────────────


class EngineSlots:
    """The syntheses an engine (per host) may run at once, and how many requests may wait for
    a slot; one more is refused with :class:`Overloaded`."""

    def __init__(self, max_parallel: int, max_queue: int) -> None:
        self.max_parallel = max_parallel
        self._max_queue = max_queue
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._waiting = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._semaphore.locked() and self._waiting >= self._max_queue:
            raise Overloaded(f"{self.max_parallel} running and {self._max_queue} waiting requests")
        self._waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        try:
            yield
        finally:
            self._semaphore.release()


class EngineSlotTable:
    """The slots of every (engine, host) — shared across every reading of the suite."""

    def __init__(self, defaults: Mapping[str, EngineDefaults]) -> None:
        self._defaults = defaults
        self._slots: dict[tuple[str, str], EngineSlots] = {}

    def reconfigure(self, defaults: Mapping[str, EngineDefaults]) -> None:
        """Take new defaults. The slots of an engine whose limits changed are replaced (syntheses
        under way keep the old ones); all others stay the same objects, so running and new readings
        keep sharing one capacity — a new table per configuration would double it."""
        def limits(configured: Mapping[str, EngineDefaults], engine_key: str) -> tuple[int | None, int | None]:
            entry = configured.get(engine_key, EngineDefaults())
            return entry.max_parallel, entry.max_queue

        for key in [key for key in self._slots if limits(self._defaults, key[0]) != limits(defaults, key[0])]:
            del self._slots[key]
        self._defaults = defaults

    def for_engine(self, engine: TTSEngine) -> EngineSlots:
        key = (engine.key, engine.address)
        if key not in self._slots:
            configured = self._defaults.get(engine.key, EngineDefaults())
            max_parallel = configured.max_parallel if configured.max_parallel is not None else engine.max_parallel_requests
            max_queue = configured.max_queue if configured.max_queue is not None else engine.max_queued_requests
            self._slots[key] = EngineSlots(max_parallel, max_queue)
        return self._slots[key]


# ── Voice ───────────────────────────────────────────────────────────


def resolve_voice(
    engine: TTSEngine, request: SpeakRequest, defaults: Mapping[str, EngineDefaults],
) -> tuple[str, float, float]:
    """Voice, speed and pitch of one entry. Each value comes from the first of: the request's
    setting for this engine (``request.voices``), the request's own fields, the configured
    defaults of the engine, the engine's own default (speed and pitch: neutral). The request's
    ``voice`` is a voice of the container engines and is not offered to the others.
    No voice at all: :class:`TTSFailure` ``software`` — this entry fails, the list goes on."""
    own = request.voices.get(engine.key, VoiceSettings())
    configured = defaults[engine.key].voice if engine.key in defaults else VoiceSettings()
    voice = next(
        (name for name in (
            own.voice, request.voice if engine.runs_in_container else None, configured.voice, engine.default_voice,
        ) if name),
        None,
    )
    speed = next((v for v in (own.speed, request.speed, configured.speed, SPEED_DEFAULT) if v is not None))
    pitch = next((v for v in (own.pitch, request.pitch, configured.pitch, PITCH_DEFAULT) if v is not None))
    if voice is None:
        raise TTSFailure("software", f"no voice configured for {engine.key}")
    if speed <= 0 or pitch <= 0:
        raise TTSFailure("software", f"invalid speed/pitch for {engine.key}: {speed}/{pitch}")
    return voice, speed, pitch


# ── One run ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Spoken:
    """The result of one unit: its audio and who spoke it."""

    audio: bytes
    entry: EscalationEntry


class SpeechRun:
    """The state of one reading (or of several calls that share a run ID)."""

    def __init__(
        self,
        entries: tuple[EscalationEntry, ...],
        launcher: EngineLauncher,
        slots: EngineSlotTable,
        defaults: Mapping[str, EngineDefaults],
        executor: Executor,
    ) -> None:
        self._entries = entries
        self._executor = executor
        self._launcher = launcher
        self._slots = slots
        self._defaults = defaults
        self._lock = asyncio.Lock()
        self._current: EscalationEntry | None = None
        self._failed: dict[str, FailureReason] = {}  # entry label -> why it dropped out of this run
        self._spoken: set[str] = set()
        self._trace: list[TraceStep] = []
        self._switch: FailureReason | None = None
        # Syntheses running on an entry: a failed entry keeps its hold until the last of them is over.
        self._busy: dict[str, int] = {}
        self._cleaner: TextCleaner | None = None
        # The entries this run holds (their containers are in use) while streams are attached to it.
        self._attached = 0
        self._held: dict[str, EscalationEntry] = {}

    @property
    def attached(self) -> bool:
        """Whether a stream uses this run right now."""
        return self._attached > 0

    def attach(self) -> None:
        """A stream starts to use this run (several streams may share one under a run ID)."""
        self._attached += 1

    def detach(self) -> None:
        """A stream stopped using this run; the last one lets go of the containers the run holds."""
        self._attached -= 1
        if self._attached <= 0:
            for entry in self._held.values():
                self._launcher.release(entry)
            self._held.clear()

    def _hold(self, entry: EscalationEntry) -> None:
        if entry.label not in self._held:
            self._held[entry.label] = entry
            self._launcher.hold(entry)

    def _unhold_if_idle(self, entry: EscalationEntry) -> None:
        """A failed entry lets go of its container once no sentence of this run runs on it any more
        (a stop in the middle of their requests would tear them down)."""
        if entry.label in self._failed and not self._busy.get(entry.label):
            if self._held.pop(entry.label, None) is not None:
                self._launcher.release(entry)

    @property
    def trace(self) -> tuple[TraceStep, ...]:
        return tuple(self._trace)

    @property
    def switch(self) -> FailureReason | None:
        """The reason of the first voice change announced in this run."""
        return self._switch

    def cleaner(self, language: str, pronunciations: Mapping[str, str]) -> TextCleaner:
        """The text cleaner of this reading, made by its first stream: its state (an open code block,
        the hints already spoken) outlives a single call."""
        if self._cleaner is None:
            self._cleaner = TextCleaner(language, pronunciations)
        return self._cleaner

    def failure_of(self, label: str) -> FailureReason | None:
        """Why the entry dropped out of this run; ``None`` = it did not."""
        return self._failed.get(label)

    def note_switch(self, reason: FailureReason) -> None:
        """The stream announced a voice change."""
        self._switch = self._switch or reason

    def max_parallel(self, entry: EscalationEntry) -> int:
        return self._slots.for_engine(entry.engine).max_parallel

    async def select(self, request: SpeakRequest) -> EscalationEntry:
        """The entry that speaks now — chosen at the first call. An entry without a voice for this
        request fails BEFORE anything is started for it. :class:`NoEngineAvailable` when none can."""
        async with self._lock:
            if self._current is not None and self._current.label not in self._failed:
                self._hold(self._current)  # a later stream of this run holds it again (idempotent)
                return self._current
            for entry in self._entries:
                if entry.label in self._failed:
                    continue
                try:
                    resolve_voice(entry.engine, request, self._defaults)
                except TTSFailure as failure:
                    self._fail(entry, failure.reason, failure.detail)
                    continue
                skip = await self._skip(entry, request.wait)
                if skip is None:
                    logger.info("TTS escalation: %s speaks", entry.label)
                    self._current = entry
                    self._hold(entry)
                    return entry
                self._note_skip(entry, skip)
            raise NoEngineAvailable("no TTS entry can speak", self._trace)

    async def _skip(self, entry: EscalationEntry, wait: bool) -> Skip | None:
        """Why this entry cannot speak right now; ``None`` = it can. A stopped container is
        started by the launcher if it can be."""
        if entry.engine.deactivated:
            return Skip("deactivated")
        if entry.engine.renders_in_browser:
            return Skip("disabled", "speaks only in the client's own browser")
        if await self._launcher.is_serving(entry):
            return None
        return await self._launcher.ensure_serving(entry, wait=wait)

    def _note_skip(self, entry: EscalationEntry, skip: Skip) -> None:
        logger.info("TTS escalation: skip %s (%s) %s", entry.label, skip.reason, skip.detail)
        if not any(step.entry == entry.label and step.outcome == "skipped" for step in self._trace):
            self._trace.append(TraceStep(entry.label, "skipped", skip.reason, skip.detail))

    async def speak(self, text: str, request: SpeakRequest) -> Spoken:
        """Have ``text`` spoken; the entry that manages it delivers the audio. A failing entry
        drops out and the next one tries (:class:`NoEngineAvailable` when none is left).
        :class:`Overloaded` and the other errors of the suite are not an entry's failure and go on up."""
        while True:
            entry = await self.select(request)
            try:
                audio = await self._synthesize(entry, text, request)
            except TTSFailure as failure:
                self._fail(entry, failure.reason, failure.detail)
                continue
            except SuiteError:
                raise
            except Exception as exc:  # noqa: BLE001 — any other error is a software error of this entry: next entry
                self._fail(entry, "software", f"{type(exc).__name__}: {exc}")
                continue
            self._record_spoken(entry)
            return Spoken(audio, entry)

    async def _synthesize(self, entry: EscalationEntry, text: str, request: SpeakRequest) -> bytes:
        self._busy[entry.label] = self._busy.get(entry.label, 0) + 1
        try:
            voice, speed, pitch = resolve_voice(entry.engine, request, self._defaults)
            async with self._slots.for_engine(entry.engine).slot():
                audio = await synthesize(entry.engine, text, voice, request.language, speed, pitch, executor=self._executor)
            return audio
        finally:
            self._busy[entry.label] -= 1
            self._unhold_if_idle(entry)

    def _fail(self, entry: EscalationEntry, reason: FailureReason, detail: str) -> None:
        if entry.label in self._failed:
            return  # parallel sentences report one outage: one trace step, one reason
        logger.warning("TTS escalation: %s failed (%s): %s", entry.label, reason, detail)
        self._failed[entry.label] = reason
        self._unhold_if_idle(entry)
        self._trace.append(TraceStep(entry.label, "failed", reason, detail))

    def _record_spoken(self, entry: EscalationEntry) -> None:
        if entry.label not in self._spoken:
            self._spoken.add(entry.label)
            self._trace.append(TraceStep(entry.label, "spoke"))

    @staticmethod
    def announcement(successor: EscalationEntry, reason: FailureReason, language: str) -> str:
        """The sentence that announces a voice change to ``successor`` and names the reason."""
        target = successor.engine.label_short
        if successor.host is not None:
            target = translate("tts_switch_target_remote", language, engine=target, host=successor.host.name)
        return translate(f"tts_switch_{reason}", language, target=target)


# ── The table of runs ───────────────────────────────────────────────


RunKey = tuple[str, tuple[EngineRef, ...]]
"""Run ID and the list of the request: the same ID with another list is another run."""


class RunTable:
    """The runs that clients named with a run ID (a run is the reading of one run ID with one list). A run is forgotten ``ttl_s`` seconds after its
    last use — a run a stream is attached to is in use, however long it speaks; expired runs are
    dropped when the table is used (no background task)."""

    def __init__(self, ttl_s: float = RUN_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._runs: dict[RunKey, tuple[SpeechRun, float]] = {}

    def get_or_create(self, key: RunKey, create: Callable[[], SpeechRun]) -> SpeechRun:
        now = self._clock()
        self._runs = {k: (run, expires) for k, (run, expires) in self._runs.items() if run.attached or expires > now}
        run = self._runs[key][0] if key in self._runs else create()
        self._runs[key] = (run, now + self._ttl_s)
        return run

    def touch(self, key: RunKey) -> None:
        """The last stream of a run ended: its time to live starts now."""
        if key in self._runs:
            self._runs[key] = (self._runs[key][0], self._clock() + self._ttl_s)

    def clear(self) -> None:
        self._runs.clear()

    def __len__(self) -> int:
        return len(self._runs)
