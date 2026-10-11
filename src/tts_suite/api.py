"""The Python API of the TTS-Suite engine layer — the contract between the engine
package and the HTTP service (and any program that embeds it).

Draft 3 (types and protocols only, no behaviour). The service layer
(``INTERFACE.md``) wraps :class:`TTSSuite` one to one; nothing here knows about
AIfred Intelligence, Agent-Orc, or any particular engine.

Rules: everything asynchronous (sentences of one reading are spoken in
parallel), one configuration object, no global state outside :class:`TTSSuite`,
no imports from a client program.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Literal, Mapping, Protocol, Sequence

# ── Vocabulary ──────────────────────────────────────────────────────

Unit = Literal["sentence", "paragraph", "whole"]
"""How much text an engine takes at once. The client cuts the text by it."""

EntryStatus = Literal["running", "stopped", "starting", "unreachable"]

SkipReason = Literal["disabled", "deactivated", "stopped", "no_vram"]
"""Why an entry is passed over BEFORE an attempt."""

FailureReason = Literal["engine", "software", "unreachable", "timeout", "cloud_quota", "vram_exceeded"]
"""Why an entry failed AFTER an attempt (a failure after the first sound
triggers the announcement of a voice change)."""


# ── Configuration (one object; the service persists it) ─────────────


@dataclass(frozen=True)
class Host:
    """Another machine the suite starts and stops containers on over SSH."""

    name: str
    address: str
    ssh_port: int = 22
    ssh_user: str = ""


@dataclass(frozen=True)
class VoiceSettings:
    """Voice, speed and pitch; ``None`` = use the next default."""

    voice: str | None = None
    speed: float | None = None
    pitch: float | None = None


@dataclass(frozen=True)
class EntryConfig:
    """One entry of the escalation list (order = speaking order)."""

    engine: str
    host: str | None = None  # name of a Host; None = this machine
    enabled: bool = True


@dataclass(frozen=True)
class EngineDefaults:
    """Per-engine defaults the list does not carry."""

    unit: Unit | None = None  # None = the engine's own default
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    vram_budget_mib: int | None = None  # GPU engines: VRAM the engine may use, set by whoever ran the burn-in
    max_parallel: int | None = None  # requests running at once; None = the engine's own default
    max_queue: int | None = None  # requests waiting beyond that, then ``Overloaded``; None = engine default


@dataclass(frozen=True)
class GpuRule:
    """One card the suite may put GPU engines on (order = preference)."""

    gpu: str  # index as ``nvidia-smi`` lists it, or the card's UUID
    reserve_mib: int = 0  # VRAM that stays free for other programs on this card


HEADROOM_MIB_DEFAULT = 512
"""Safety margin on top of an engine's budget when a card is chosen; the value AIfred
Intelligence has used for its burn-in peaks so far."""


@dataclass(frozen=True)
class SuiteConfig:
    """Everything the suite needs; loaded and saved by the service.

    Secrets: the keys of cloud engines are NOT part of this object (they live in
    the environment or in files only the user can read).

    GPU placement is a rule set, not a callback: the suite owns the cards' use for all
    programs that talk to it. An engine starts on the first card of ``gpus`` whose free
    VRAM (``nvidia-smi``) minus ``reserve_mib`` covers the engine's ``vram_budget_mib``
    plus ``headroom_mib``. The container gets the budget itself as its limit; the headroom
    only keeps a card from being filled to the last MiB.
    The budget is also a ceiling: an engine that uses more than its budget fails with
    ``vram_exceeded``, its container is stopped and the escalation goes on. An empty
    ``gpus`` means every card, the one with the most free VRAM first. No card fits: the
    entry is skipped with ``no_vram``.

    ``pronunciations`` maps a word to the form that is spoken instead (whole words, case as
    written; for example "AIfred" -> "Alfred"). The text cleaning of every unit applies it; it is
    the one program-specific part of the cleaning that is configuration.
    """

    voices_dir: Path
    entries: tuple[EntryConfig, ...] = ()
    hosts: tuple[Host, ...] = ()  # optional other machines; empty = this machine only
    engines: Mapping[str, EngineDefaults] = field(default_factory=dict)
    gpus: tuple[GpuRule, ...] = ()
    headroom_mib: int = HEADROOM_MIB_DEFAULT
    pronunciations: Mapping[str, str] = field(default_factory=dict)


# ── What the suite reports ──────────────────────────────────────────


@dataclass(frozen=True)
class EngineInfo:
    """One entry of ``GET /engines``."""

    engine: str
    host: str | None
    label: Mapping[str, str]  # language → name for a user interface
    label_short: Mapping[str, str]
    enabled: bool
    deactivated: bool  # its folder sits in ``disabled/``
    status: EntryStatus
    unit: Unit  # the user's choice
    unit_default: Unit  # what the engine says
    needs_gpu: bool
    cloud: bool
    browser: bool  # stands for the client's own voice; the suite always skips it


@dataclass(frozen=True)
class VoiceInfo:
    name: str
    label: str


@dataclass(frozen=True)
class TraceStep:
    """One station of an escalation: who was passed over, who failed, who spoke."""

    entry: str  # ``engine@host`` (or ``engine`` on this machine)
    outcome: Literal["skipped", "failed", "spoke"]
    reason: SkipReason | FailureReason | None = None
    detail: str = ""


# ── Speaking ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpeakRequest:
    """Header of one reading; the text itself is given whole to :meth:`TTSSuite.speak`
    or in pieces to the stream from :meth:`TTSSuite.open_stream`."""

    language: str
    voice: str | None = None  # default voice (valid for the container engines)
    speed: float | None = None
    pitch: float | None = None
    engine: str | None = None  # names one entry and skips the escalation
    voices: Mapping[str, VoiceSettings] = field(default_factory=dict)  # per engine key
    run: str | None = None  # client-made ID of one reading; state is kept ~10 min
    wait: bool = True  # False: ``EngineStarting`` instead of waiting for a cold start


@dataclass(frozen=True)
class AudioSegment:
    """One piece of audio, delivered in speaking order as soon as it exists."""

    audio: bytes
    media_type: str  # "audio/wav"
    engine: str  # who spoke it, ``engine@host``; empty for a keep-alive
    keepalive: bool = False  # silence that only holds the stream open while a segment takes long


@dataclass(frozen=True)
class SpeakSummary:
    """What happened in one reading; complete when the segment stream has ended."""

    trace: tuple[TraceStep, ...] = ()
    switch: FailureReason | None = None  # set if an announcement of a voice change was delivered


class SpeechStream(Protocol):
    """One reading whose text arrives in pieces (a language model is still writing).
    The suite cuts the text by the unit of the engine that speaks and starts on the
    first complete unit; the client never cuts."""

    async def feed(self, text: str) -> None: ...

    async def finish(self) -> None:
        """No more text: the rest is cut and spoken."""
        ...

    def segments(self) -> AsyncIterator[AudioSegment]:
        """Audio in speaking order. A failure after the first segment ends the iteration
        with the :class:`SuiteError` that caused it (before the first segment the same
        error comes from the first step of the iteration), never silently. Exception: a request
        that names an engine unknown to the suite or missing from the list fails at once in
        ``speak`` / ``open_stream`` (:class:`UnknownEngine`, :class:`NoEngineAvailable`).
        A voice change after a failure is announced in a segment of its own, in front of the
        first segment of the successor."""
        ...

    async def aclose(self) -> None:
        """Abort: stops the segments still being generated, releases the engine slots
        and the lock of the run. The caller calls it when its client is gone; calling it
        after a normal end does nothing."""
        ...

    @property
    def summary(self) -> SpeakSummary:
        """Valid after ``segments()`` ended."""
        ...


class SuiteError(Exception):
    """Base of every error the suite raises on purpose."""


class NoEngineAvailable(SuiteError):
    """No entry can speak. ``trace`` tells why for each."""

    def __init__(self, detail: str, trace: Sequence[TraceStep]) -> None:
        super().__init__(detail)
        self.trace = tuple(trace)


class EngineStarting(SuiteError):
    """``wait=False`` and a container is still starting."""


class Overloaded(SuiteError):
    """More requests for an engine than its queue takes (HTTP 429)."""


class UnknownEngine(SuiteError):
    pass


class EngineInUse(SuiteError):
    """``stop`` for a container that a reading is using right now (HTTP 409)."""


class EngineControlFailed(SuiteError):
    """``start`` or ``stop`` of a container did not work; the message says why (a skip reason and
    its detail, the host's answer, Docker's)."""


class ConfigInvalid(SuiteError):
    """``set_config`` was given something that cannot work: an unknown engine, a
    host not in ``hosts``, a missing ``voices_dir`` (HTTP 400). The check lives here,
    once; the service does not repeat it."""


# ── The entry point ─────────────────────────────────────────────────


class TTSSuite(Protocol):
    """The engine layer. The service wraps it; programs may embed it."""

    async def aclose(self) -> None:
        """Release semaphores, HTTP sessions and the table of runs (the service calls
        it in its lifespan)."""
        ...

    async def config(self) -> SuiteConfig: ...

    async def set_config(self, config: SuiteConfig) -> None:
        """Replace the configuration (persisting it is the caller's job). Raises
        :class:`ConfigInvalid` for a configuration that cannot work."""
        ...

    async def engines(self) -> list[EngineInfo]: ...

    async def voices(self, engine: str | None = None) -> list[VoiceInfo]: ...

    def speak(self, text: str, request: SpeakRequest) -> SpeechStream:
        """The whole text at once. Same behaviour as :meth:`open_stream` with one ``feed``
        and a ``finish``: the first segment is out as soon as it is generated."""
        ...

    def open_stream(self, request: SpeakRequest) -> SpeechStream: ...

    async def start(self, engine: str, host: str | None = None) -> None:
        """Start the container and wait until it serves. Forgets why an earlier start of this entry
        (or the VRAM ceiling) took it out of the list."""
        ...

    async def stop(self, engine: str, host: str | None = None) -> None: ...

    async def reload(self) -> tuple[list[str], list[str]]:
        """Read the engine folders again; returns (removed, added) engine keys. Forgets the
        remembered start failures of all entries. Raises :class:`ConfigInvalid` when an engine
        folder was deleted (not moved to ``disabled/``) that the list still names: the engine is
        gone from the registry, the list stays as it was until a corrected configuration is set."""
        ...

    async def sync_voices(self, engine: str) -> list[VoiceInfo]:
        """Have a cloud engine that clones voices upload the shared voices."""
        ...
