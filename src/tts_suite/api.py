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

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

# ── Vocabulary ──────────────────────────────────────────────────────

Unit = Literal["sentence", "paragraph", "whole"]
"""How much text an engine takes at once. The client cuts the text by it."""

GpuMode = Literal["auto", "list"]
EntryStatus = Literal["running", "stopped", "starting", "unreachable"]
"""``running`` = speaks; ``starting`` = a start is under way; ``stopped`` = nothing runs, the host (if any)
is up, so it can be started; ``unreachable`` = the host does not answer, or a cloud engine has no access."""

SkipReason = Literal["disabled", "deactivated", "stopped", "no_vram"]
"""Why an entry is passed over BEFORE an attempt."""

FailureReason = Literal["engine", "software", "unreachable", "timeout", "cloud_quota"]
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
class VoiceTuning:
    """Speed and pitch of ONE voice of an engine (some voices sound right only a little faster or
    deeper); ``None`` = the next default."""

    speed: float | None = None
    pitch: float | None = None


@dataclass(frozen=True)
class EngineDefaults:
    """Per-engine defaults the request's list does not carry (the unit is the request's, per entry)."""

    voice: VoiceSettings = field(default_factory=VoiceSettings)
    # Speed and pitch per voice name (as the voice list shows it): they apply whenever that voice
    # speaks, after what the request itself says and before the defaults of the engine above.
    voice_tuning: Mapping[str, VoiceTuning] = field(default_factory=dict)
    vram_budget_mib: int | None = None  # GPU engines: VRAM the engine may use (card choice + cap in the container)
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
    programs that talk to it. The suite offers every engine it knows (the catalog,
    :meth:`TTSSuite.engines`); it keeps NO escalation list: each request brings its own, so no
    program owns a list the others share. ``gpu_mode`` ``auto`` puts an engine on a card no program uses
    (``memory.used`` 0) that fits, else on the card with the most free VRAM that fits (the default); ``list`` only on the cards of ``gpus``, the first
    one whose free VRAM (``nvidia-smi``) minus ``reserve_mib`` covers the engine's ``vram_budget_mib``
    plus ``headroom_mib``. The container gets the budget itself as its cap; the headroom
    only keeps a card from being filled to the last MiB. The cap counts PyTorch memory only,
    so it is an outlier guard, not an exact limit; the suite does not watch the use or stop an
    engine for it (``EngineInfo.vram_mib`` only reports it). In ``auto`` mode ``gpus`` is not
    used (and kept for the next switch to ``list``). No card fits: the entry is skipped with
    ``no_vram`` (not remembered: the next request chooses again).

    ``placement_lock_file`` names a file that every program placing models on the cards locks (``flock``)
    while it chooses a card and loads onto it (the STT suite does); programs that start together then do
    not choose the same card. ``None`` = the suite is alone on the cards.

    ``pronunciations`` maps a word to the form that is spoken instead (whole words, case as
    written; for example "AIfred" -> "Alfred"). The text cleaning of every unit applies it; it is
    the one program-specific part of the cleaning that is configuration.
    """

    voices_dir: Path
    hosts: tuple[Host, ...] = ()  # optional other machines (SSH keys are the service's); empty = this machine only
    engines: Mapping[str, EngineDefaults] = field(default_factory=dict)
    gpu_mode: GpuMode = "auto"
    gpus: tuple[GpuRule, ...] = ()
    headroom_mib: int = HEADROOM_MIB_DEFAULT
    placement_lock_file: Path | None = None  # shared with other programs that place models on the cards
    pronunciations: Mapping[str, str] = field(default_factory=dict)


# ── What the suite reports ──────────────────────────────────────────


@dataclass(frozen=True)
class EngineInfo:
    """One row of the catalog, ``GET /engines``: an engine on this machine (``host`` None) or, for an
    engine with a REST API, on one of the configured hosts. Not a list the suite speaks by: the
    request says which of these rows it uses, in which order and by which unit."""

    engine: str
    host: str | None
    label: Mapping[str, str]  # language → name for a user interface
    label_short: Mapping[str, str]
    status: EntryStatus
    unit_default: Unit  # what the engine recommends as the unit of an entry
    needs_gpu: bool
    cloud: bool
    browser: bool  # stands for the client's own voice; the suite always skips it
    installed: bool  # can be started: no container needed (cloud, CPU) or runs on another host; else the Docker image is built
    start_hint: str | None  # why the last start of this entry failed (until the next start, a new configuration or a reload); else None
    vram_mib: Mapping[str, int] | None  # {gpu_uuid: MiB} per card it holds (display only); {} = none/not local; None = measurement failed
    idle_remaining_s: float | None  # seconds until a running container ends itself for being idle (a request resets it); None = not running, no container, or auto-shutdown off
    # What the engine does when ``EngineDefaults`` leaves a limit empty (for a page that shows them).
    default_vram_mib: int | None  # GPU engines: the budget; None = the engine takes no budget
    default_max_parallel: int
    default_max_queue: int


@dataclass(frozen=True)
class EngineTarget:
    """One catalog row a client asks the suite to release. Nothing is optional: ``host=None`` is the
    explicit value "this machine"."""

    engine: str
    host: str | None


ReleaseOutcome = Literal["stopped", "not_running", "in_use", "unreachable", "failed"]
"""``stopped`` = the container was taken down, its VRAM is free; ``not_running`` = nothing to release (no
container, or it does not run); ``in_use`` = a reading holds it, so it was left alone; ``unreachable`` = the
host does not answer; ``failed`` = the stop did not work (``detail`` says why)."""


@dataclass(frozen=True)
class ReleaseResult:
    """What :meth:`TTSSuite.release` did for one target."""

    engine: str
    host: str | None
    outcome: ReleaseOutcome
    detail: str


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
class EngineRef:
    """One entry of the list a request brings. Nothing is optional: ``host=None`` is the explicit
    value "this machine"."""

    engine: str
    unit: Unit  # how much text this entry takes at once; the text is cut by the unit of the entry that speaks
    host: str | None  # name of a configured Host; None = this machine


@dataclass(frozen=True)
class SpeakRequest:
    """Header of one reading; the text itself is given whole to :meth:`TTSSuite.speak`
    or in pieces to the stream from :meth:`TTSSuite.open_stream`."""

    language: str  # must be one the suite has texts in, else ``UnsupportedLanguage`` at ``speak`` / ``open_stream``
    # The list of THIS request, in escalation order: the first entry that can speak speaks. The suite
    # keeps no list of its own and no program shares one with another (list per request, no shared
    # list). Empty, a repeated engine@host, an unknown engine or host, or an engine without REST API on
    # a host is refused at once (``InvalidRequest``, ``UnknownEngine``, ``UnknownHost``). Not to be
    # confused with ``SuiteConfig.engines`` (defaults per engine).
    engines: tuple[EngineRef, ...]
    # Silence between two paragraphs of the input (a blank line), in ms; 0 = none, negative = ``InvalidRequest``.
    # The suite does not make the silence: it delivers a pause marker (``AudioSegment.pause_ms``) at each
    # paragraph boundary for the units sentence and paragraph; an engine with the unit ``whole`` sets the
    # pause at the blank line itself. Nothing is optional: the client says "no pause" with 0.
    paragraph_pause_ms: int
    voice: str | None = None  # default voice (valid for the container engines)
    speed: float | None = None
    pitch: float | None = None
    voices: Mapping[str, VoiceSettings] = field(default_factory=dict)  # "engine@host", "engine@" (this machine) or "engine" (all rows)
    run: str | None = None  # client-made ID of one reading; state is kept ~5 min
    wait: bool = True  # False: ``EngineStarting`` instead of waiting for a cold start


@dataclass(frozen=True)
class AudioSegment:
    """One piece of audio, delivered in speaking order as soon as it exists."""

    audio: bytes
    media_type: str  # "audio/wav", "audio/ogg", "audio/mpeg"; empty for a keep-alive or pause marker
    engine: str  # who spoke it, ``engine@host``; empty for a keep-alive or pause marker
    keepalive: bool = False
    """Marker without audio (``audio`` empty); the client decides what to do with it, for example a
    device channel plays its own silence. It comes only after the first delivered segment and only
    while a segment takes longer than ``KEEPALIVE_SEC``, in every audio format."""
    pause_ms: int = 0
    """> 0: a pause marker (``audio`` empty, no keep-alive): the client makes this much silence for its output
    (a device channel plays PCM silence, a browser waits between its files). It comes at a paragraph
    boundary of the input, in speaking order, between the last segment of a paragraph and the first of the
    next one (``SpeakRequest.paragraph_pause_ms``)."""


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
        that names an engine unknown to the suite or missing from the list, or a language the suite
        has no texts in, fails at once in ``speak`` / ``open_stream`` (:class:`UnknownEngine`,
        :class:`NoEngineAvailable`, :class:`UnsupportedLanguage`).
        A voice change after a failure is announced in a segment of its own, in front of the
        first segment of the successor."""
        ...

    async def aclose(self) -> None:
        """Abort: stops the generation of this stream's remaining segments and releases the engine
        slots and the lock of the run. A request already sent to a container is not cancelled
        there, the container finishes it (its thread and HTTP call run to the end, up to the read
        timeout). The caller calls it when its client is gone; calling it after a normal end does
        nothing."""
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
    """An entry, a ``start`` or a ``stop`` names an engine the suite does not know."""


class UnknownHost(SuiteError):
    """An entry, a ``start`` or a ``stop`` names a host that is not in ``SuiteConfig.hosts``."""


class InvalidRequest(SuiteError):
    """The list of a request cannot work: empty, the same engine@host twice, an engine without REST
    API on a host, a unit that does not exist, a negative ``paragraph_pause_ms``. Raised at once, before anything is started."""


class UnsupportedLanguage(SuiteError):
    """The suite has no texts of its own (announcements, hints) in the language of the request.
    Raised at once by ``speak`` / ``open_stream``, before anything is started (the service answers
    HTTP 400); the languages it has are those of ``i18n.json`` (``de`` and ``en`` at present).
    Whether an engine can speak a supported language is not checked here: the escalation decides."""


class EngineInUse(SuiteError):
    """``stop`` for a container that a reading is using right now (HTTP 409)."""


class EngineControlFailed(SuiteError):
    """``start`` or ``stop`` of a container did not work; the message says why (a skip reason and
    its detail, the host's answer, Docker's)."""


class ConfigInvalid(SuiteError):
    """``set_config`` was given something that cannot work: defaults for an unknown engine, a
    bad host, a missing ``voices_dir`` (HTTP 400). The check lives here,
    once; the service does not repeat it."""


# ── The entry point ─────────────────────────────────────────────────


class TTSSuite(Protocol):
    """The engine layer. The service wraps it; programs may embed it."""

    async def aclose(self) -> None:
        """Release semaphores, HTTP sessions, the table of runs and the synthesis threads (the
        service calls it in its lifespan): syntheses not yet started are dropped, running ones
        end on their own. Takes down every container, on this machine and on the hosts, also one a
        reading was using; the next request starts what it needs again."""
        ...

    async def config(self) -> SuiteConfig: ...

    async def set_config(self, config: SuiteConfig) -> None:
        """Replace the configuration (persisting it is the caller's job). Raises
        :class:`ConfigInvalid` for a configuration that cannot work."""
        ...

    async def engines(self) -> list[EngineInfo]:
        """The catalog: a row per registered engine on this machine and, for each configured host, a
        row per engine with a REST API. A host that does not answer shows ``unreachable``/``stopped``
        rows, not an error; the hosts are asked in parallel."""
        ...

    async def voices(self, engine: str | None = None) -> list[VoiceInfo]: ...

    def speak(self, text: str, request: SpeakRequest) -> SpeechStream:
        """The whole text at once. Same behaviour as :meth:`open_stream` with one ``feed``
        and a ``finish``: the first segment is out as soon as it is generated. Raises
        :class:`UnsupportedLanguage` (language without suite texts), :class:`InvalidRequest`,
        :class:`UnknownEngine` and :class:`UnknownHost` (the list) at once, before anything is started."""
        ...

    def open_stream(self, request: SpeakRequest) -> SpeechStream:
        """Same early errors as :meth:`speak`."""
        ...

    async def start(self, engine: str, host: str | None = None, gpu: str | None = None) -> None:
        """Start the container of any catalog row and wait until it serves. Forgets why an earlier start
        of it failed. ``gpu`` (index or UUID) puts a local GPU engine on that card, outside the rules of
        ``gpu_mode`` (for a measurement on a given card): :class:`InvalidRequest` for an engine on a host or
        without GPU, :class:`EngineInUse` while it runs or starts, :class:`EngineControlFailed` when the card
        is not there or has too little room."""
        ...

    async def stop(self, engine: str, host: str | None = None) -> None: ...

    async def release(self, targets: tuple[EngineTarget, ...] | Literal["all"]) -> tuple[ReleaseResult, ...]:
        """A client asks the suite to take containers off the cards (for example to make room for a language
        model): the container is stopped, not only unloaded. The suite decides per target (see
        :data:`ReleaseOutcome`); a container a reading holds is never stopped. ``"all"`` = every catalog row
        with a container, on this machine and on the hosts. The targets are handled in parallel; the results
        come in the order of the targets. The whole request is checked before the first stop:
        :class:`InvalidRequest` (empty or a repeated target), :class:`UnknownEngine`, :class:`UnknownHost`."""
        ...

    async def reload(self) -> tuple[list[str], list[str]]:
        """Read the engine folders again; returns (removed, added) engine keys. Forgets the
        remembered start failures of all entries. Raises :class:`ConfigInvalid` when an engine
        folder was deleted (not moved to ``disabled/``) that the defaults still name: the engine is
        gone from the registry, the configuration stays as it was until a corrected one is set."""
        ...

    async def sync_voices(self, engine: str) -> list[VoiceInfo]:
        """Have a cloud engine that clones voices upload the shared voices."""
        ...

    async def push_voices(self) -> list[str]:
        """Hand the voices folder to every configured host, which adds and overwrites and deletes nothing.
        Returns the names of the hosts that took it; :class:`EngineControlFailed` at the first that did not."""
        ...
