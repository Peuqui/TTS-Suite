"""Escalation: which entry of the list speaks, and what happens when one fails.

Ported from AIfred Intelligence (``tts_escalation.py``). The list is ordered; the first
entry that can speak speaks.

* Before the first sound an entry that fails drops out silently, the next one takes over.
* An entry that fails AFTER it has spoken is replaced the same way, but its successor first
  announces the voice change (with the reason) and then speaks the sentence that was not done.
* An entry that failed stays out for the rest of the run, so the voice does not jump back and forth.
* The selection is serialised per run, so sentences generated in parallel do not start a
  container twice.

What this module does not do: starting containers (card choice, SSH) is the job of an
:class:`EngineLauncher`; a client program's own voice (the browser entry) never speaks here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Mapping, Protocol

from .api import (
    ConfigInvalid,
    EngineDefaults,
    FailureReason,
    Host,
    NoEngineAvailable,
    Overloaded,
    SkipReason,
    SpeakRequest,
    SuiteConfig,
    SuiteError,
    TraceStep,
    Unit,
    VoiceSettings,
)
from .constants import PITCH_DEFAULT, RUN_TTL_S, SPEED_DEFAULT
from .engines.base import SPEECH_UNITS, DeactivatedEngine, TTSEngine, TTSFailure
from .engines.registry import engine_unit, get_engine, is_deactivated
from .i18n import translate
from .synthesis import synthesize

logger = logging.getLogger(__name__)


# ── Entries ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EscalationEntry:
    """One entry of the list: an engine, bound to a host (``None`` = this machine)."""

    engine: TTSEngine
    host: Host | None
    enabled: bool

    @property
    def label(self) -> str:
        """``engine@host``, or ``engine`` on this machine — the name in traces and segments."""
        return self.engine.key if self.host is None else f"{self.engine.key}@{self.host.name}"


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

    async def check_vram(self, entry: EscalationEntry) -> None:
        """Called after each synthesis: raises ``TTSFailure("vram_exceeded")`` (and stops the container)
        when the entry uses more VRAM than its budget."""
        ...


def _index_hosts(config: SuiteConfig) -> dict[str, Host]:
    hosts: dict[str, Host] = {}
    for host in config.hosts:
        if host.name in hosts:
            raise ConfigInvalid(f"hosts: duplicate host name {host.name!r}")
        hosts[host.name] = host
    return hosts


def _known(engine_key: str) -> bool:
    return get_engine(engine_key) is not None or is_deactivated(engine_key)


def _check_defaults(config: SuiteConfig) -> None:
    for key, defaults in config.engines.items():
        if not _known(key):
            raise ConfigInvalid(f"engines: defaults for unknown TTS engine {key!r}")
        if defaults.unit is not None and defaults.unit not in SPEECH_UNITS:
            raise ConfigInvalid(f"engines[{key}]: unit must be one of {SPEECH_UNITS}, got {defaults.unit!r}")
        if defaults.max_parallel is not None and defaults.max_parallel < 1:
            raise ConfigInvalid(f"engines[{key}]: max_parallel must be at least 1")
        if defaults.max_queue is not None and defaults.max_queue < 0:
            raise ConfigInvalid(f"engines[{key}]: max_queue must not be negative")


def _check_pronunciations(config: SuiteConfig) -> None:
    for word in config.pronunciations:
        if not word.strip():
            raise ConfigInvalid("pronunciations: a word must not be empty")


def _bind_entry(engine_key: str, host_name: str | None, enabled: bool, config: SuiteConfig,
                hosts: Mapping[str, Host]) -> EscalationEntry:
    engine = get_engine(engine_key)
    if engine is None and not is_deactivated(engine_key):
        raise ConfigInvalid(f"entries: unknown TTS engine {engine_key!r}")
    host = None
    if host_name is not None:
        host = hosts.get(host_name)
        if host is None:
            raise ConfigInvalid(f"entries: entry {engine_key!r} names unknown host {host_name!r}")
    if engine is None:
        # The engine folder lies in engines/disabled/: the entry stays in the list, shown as off
        return EscalationEntry(DeactivatedEngine(engine_key), host, enabled=False)
    engine = engine.with_voices_dir(config.voices_dir)
    if host is not None:
        try:
            engine = engine.at_host(host.address)
        except ValueError as exc:
            raise ConfigInvalid(f"entries: {exc}") from exc
    return EscalationEntry(engine, host, enabled)


def bind_entries(config: SuiteConfig) -> tuple[EscalationEntry, ...]:
    """The escalation list of a configuration, in its order (disabled entries included), with every
    engine bound to its host and to the shared voices tree. THE check of a configuration: whatever
    cannot work (unknown engine or host, a missing ``voices_dir``, an engine without a REST API on a
    remote host, the same entry twice, bad limits) is :class:`ConfigInvalid` — a broken list is
    never passed over silently."""
    if not config.voices_dir.is_dir():
        raise ConfigInvalid(f"voices_dir {config.voices_dir} is not a directory")
    hosts = _index_hosts(config)
    _check_defaults(config)
    _check_pronunciations(config)
    entries = tuple(
        _bind_entry(raw.engine, raw.host, raw.enabled, config, hosts) for raw in config.entries
    )
    labels = [entry.label for entry in entries]
    for label in labels:
        if labels.count(label) > 1:
            raise ConfigInvalid(f"entries: {label!r} is in the list twice")
    return entries


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
    """The result of one unit: its audio, who spoke it, and the reason of the voice change that
    was announced in front of it (``None`` = no announcement)."""

    audio: bytes
    entry: EscalationEntry
    announced: FailureReason | None


class SpeechRun:
    """The state of one reading (or of several calls that share a run ID)."""

    def __init__(
        self,
        entries: tuple[EscalationEntry, ...],
        launcher: EngineLauncher,
        slots: EngineSlotTable,
        defaults: Mapping[str, EngineDefaults],
    ) -> None:
        self._entries = entries
        self._launcher = launcher
        self._slots = slots
        self._defaults = defaults
        self._lock = asyncio.Lock()
        self._current: EscalationEntry | None = None
        self._failed: set[str] = set()
        self._spoken: set[str] = set()
        self._trace: list[TraceStep] = []
        # Reason of a failure after the first sound whose announcement is still to be spoken,
        # and whether a sentence is speaking it right now (only one sentence announces).
        self._pending_switch: FailureReason | None = None
        self._switch_claimed = False
        self._switch: FailureReason | None = None
        # The entries this run holds (their containers are in use) while streams are attached to it.
        self._attached = 0
        self._held: dict[str, EscalationEntry] = {}

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

    def _unhold(self, entry: EscalationEntry) -> None:
        if self._held.pop(entry.label, None) is not None:
            self._launcher.release(entry)

    @property
    def trace(self) -> tuple[TraceStep, ...]:
        return tuple(self._trace)

    @property
    def switch(self) -> FailureReason | None:
        """The reason of the first voice change announced in this run."""
        return self._switch

    def unit(self, entry: EscalationEntry) -> Unit:
        return engine_unit(entry.engine, self._defaults)

    def max_parallel(self, entry: EscalationEntry) -> int:
        return self._slots.for_engine(entry.engine).max_parallel

    async def select(self, *, wait: bool) -> EscalationEntry:
        """The entry that speaks now — chosen at the first call. :class:`NoEngineAvailable`
        when none can."""
        async with self._lock:
            if self._current is not None and self._current.label not in self._failed:
                return self._current
            for entry in self._entries:
                if entry.label in self._failed:
                    continue
                skip = await self._skip(entry, wait)
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
        if not entry.enabled:
            return Skip("disabled")
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
            entry = await self.select(wait=request.wait)
            announced = self._claim_switch()
            succeeded = False
            try:
                spoken_text = self._announcement(entry, announced, request.language) + text
                audio = await self._synthesize(entry, spoken_text, request)
                succeeded = True
            except TTSFailure as failure:
                self._fail(entry, failure.reason, failure.detail)
                continue
            except SuiteError:
                raise
            except Exception as exc:  # noqa: BLE001 — any other error is a software error of this entry: next entry
                self._fail(entry, "software", f"{type(exc).__name__}: {exc}")
                continue
            finally:
                self._settle_switch(announced, succeeded)
            self._record_spoken(entry)
            return Spoken(audio, entry, announced)

    async def _synthesize(self, entry: EscalationEntry, text: str, request: SpeakRequest) -> bytes:
        voice, speed, pitch = resolve_voice(entry.engine, request, self._defaults)
        async with self._slots.for_engine(entry.engine).slot():
            audio = await synthesize(entry.engine, text, voice, request.language, speed, pitch)
        await self._launcher.check_vram(entry)
        return audio

    def _fail(self, entry: EscalationEntry, reason: FailureReason, detail: str) -> None:
        logger.warning("TTS escalation: %s failed (%s): %s", entry.label, reason, detail)
        self._failed.add(entry.label)
        self._unhold(entry)
        self._trace.append(TraceStep(entry.label, "failed", reason, detail))
        if entry.label in self._spoken and self._pending_switch is None:
            self._pending_switch = reason

    def _record_spoken(self, entry: EscalationEntry) -> None:
        if entry.label not in self._spoken:
            self._spoken.add(entry.label)
            self._trace.append(TraceStep(entry.label, "spoke"))

    def _claim_switch(self) -> FailureReason | None:
        """The announcement is spoken in front of ONE sentence — the first that asks for it."""
        if self._pending_switch is None or self._switch_claimed:
            return None
        self._switch_claimed = True
        return self._pending_switch

    def _settle_switch(self, claimed: FailureReason | None, succeeded: bool) -> None:
        """The sentence that carried the announcement is done (announcement spoken) or failed
        (the next sentence takes it over)."""
        if claimed is None:
            return
        self._switch_claimed = False
        if succeeded:
            self._pending_switch = None
            self._switch = self._switch or claimed

    @staticmethod
    def _announcement(entry: EscalationEntry, reason: FailureReason | None, language: str) -> str:
        """The sentence in front of the successor's first text after a failure in mid-text."""
        if reason is None:
            return ""
        target = entry.engine.label_short
        if entry.host is not None:
            target = translate("tts_switch_target_remote", language, engine=target, host=entry.host.name)
        return translate(f"tts_switch_{reason}", language, target=target) + " "


# ── The table of runs ───────────────────────────────────────────────


class RunTable:
    """The runs that clients named with a run ID. A run is forgotten ``ttl_s`` seconds after its
    last use; expired runs are dropped when the table is used (no background task)."""

    def __init__(self, ttl_s: float = RUN_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._runs: dict[tuple[str, str | None], tuple[SpeechRun, float]] = {}

    def get_or_create(self, key: tuple[str, str | None], create: Callable[[], SpeechRun]) -> SpeechRun:
        now = self._clock()
        self._runs = {k: (run, expires) for k, (run, expires) in self._runs.items() if expires > now}
        run = self._runs[key][0] if key in self._runs else create()
        self._runs[key] = (run, now + self._ttl_s)
        return run

    def clear(self) -> None:
        self._runs.clear()

    def __len__(self) -> int:
        return len(self._runs)
