"""The suite: :class:`~tts_suite.api.TTSSuite` implemented with the speaker, the engine registry and a launcher.

Nothing new is decided here — speaking is the :class:`~tts_suite.speaker.Speaker`, engines come from
the registry, containers are the launcher's. This class only joins them to the one entry point.
"""

from __future__ import annotations

import asyncio
import logging

from .api import (
    EngineControlFailed,
    EngineInfo,
    EntryStatus,
    SpeakRequest,
    SuiteConfig,
    VoiceInfo,
)
from .constants import LABEL_LANGUAGES
from .engines.base import shared_voice_names
from .engines.registry import reload_engines, require_engine, voice_names
from .escalation import EscalationEntry
from .launcher import ManagedLauncher, SuiteLauncher
from .speaker import Speaker
from .stream import Stream
from .vram import VramMeasurementFailed, engine_vram_mib

logger = logging.getLogger(__name__)


class Suite:
    """Implements :class:`~tts_suite.api.TTSSuite`. ``launcher`` must have been built from the same
    configuration (:func:`create_suite` does that)."""

    def __init__(self, config: SuiteConfig, launcher: ManagedLauncher) -> None:
        self._launcher = launcher
        self._speaker = Speaker(config, launcher)

    async def aclose(self) -> None:
        await self._speaker.aclose()
        await self._launcher.aclose()

    # ── Configuration ───────────────────────────────────────────────

    async def config(self) -> SuiteConfig:
        return self._speaker.config

    async def set_config(self, config: SuiteConfig) -> None:
        self._speaker.set_config(config)  # the one check; ConfigInvalid leaves everything as it was
        self._launcher.set_config(config)

    async def engines(self) -> list[EngineInfo]:
        # One task per row, so a host that does not answer delays only its own rows by the health timeout
        return list(await asyncio.gather(*(self._info(entry) for entry in self._speaker.catalog_entries())))

    async def _info(self, entry: EscalationEntry) -> EngineInfo:
        status = await self._launcher.status(entry)
        vram_mib, installed = await asyncio.gather(self._vram_mib(entry, status), self._installed(entry))
        return EngineInfo(
            engine=entry.engine.key,
            host=entry.host.name if entry.host is not None else None,
            label={language: entry.engine.label(language) for language in LABEL_LANGUAGES},
            label_short={language: entry.engine.label_short for language in LABEL_LANGUAGES},
            status=status,
            unit_default=entry.engine.default_speech_unit,
            needs_gpu=entry.engine.needs_gpu,
            cloud=entry.engine.cloud,
            browser=entry.engine.renders_in_browser,
            installed=installed,
            start_hint=self._start_hint(entry),
            vram_mib=vram_mib,
        )

    @staticmethod
    async def _installed(entry: EscalationEntry) -> bool:
        """Whether the entry can be started. An engine on another host is not the suite's to judge
        (it cannot see that machine's Docker), so it counts as installed."""
        if entry.host is not None:
            return True
        return await asyncio.to_thread(entry.engine.is_installed)

    def _start_hint(self, entry: EscalationEntry) -> str | None:
        failure = self._launcher.start_failure(entry)
        return failure.detail if failure is not None else None

    @staticmethod
    async def _vram_mib(entry: EscalationEntry, status: EntryStatus) -> dict[str, int] | None:
        """What a running local GPU engine holds per card (display only); empty for all others,
        ``None`` (logged) when the measurement failed."""
        engine = entry.engine
        if entry.host is not None or not engine.needs_gpu or status != "running":
            return {}
        try:
            return await asyncio.to_thread(engine_vram_mib, engine)
        except VramMeasurementFailed as failed:
            logger.error("TTS: VRAM of %s could not be measured: %s", entry.label, failed)
            return None

    # ── Voices ──────────────────────────────────────────────────────

    async def voices(self, engine: str | None = None) -> list[VoiceInfo]:
        voices_dir = self._speaker.config.voices_dir
        if engine is None:
            return [VoiceInfo(name, name) for name in shared_voice_names(voices_dir)]
        bound = require_engine(engine).with_voices_dir(voices_dir)
        names = await asyncio.to_thread(voice_names, bound)  # a container engine answers live, else its catalog
        return [VoiceInfo(name, name) for name in names]

    async def sync_voices(self, engine: str) -> list[VoiceInfo]:
        bound = require_engine(engine).with_voices_dir(self._speaker.config.voices_dir)
        await asyncio.to_thread(lambda: list(bound.prepare_voices()))
        return await self.voices(engine)

    # ── Speaking ────────────────────────────────────────────────────

    def speak(self, text: str, request: SpeakRequest) -> Stream:
        return self._speaker.speak(text, request)

    def open_stream(self, request: SpeakRequest) -> Stream:
        return self._speaker.open_stream(request)

    # ── Containers ──────────────────────────────────────────────────

    async def start(self, engine: str, host: str | None = None) -> None:
        entry = self._catalog_entry(engine, host)
        self._launcher.clear_failures(entry)  # an explicit start tries again, whatever happened before
        if await self._launcher.is_serving(entry):
            return
        skip = await self._launcher.ensure_serving(entry, wait=True)
        if skip is not None:
            raise EngineControlFailed(f"{entry.label} not started ({skip.reason}): {skip.detail}")

    async def stop(self, engine: str, host: str | None = None) -> None:
        await self._launcher.stop(self._catalog_entry(engine, host))

    async def reload(self) -> tuple[list[str], list[str]]:
        removed, added = await asyncio.to_thread(reload_engines)
        self._launcher.clear_failures(None)
        self._speaker.set_config(self._speaker.config)  # bind to the engines as they are now
        return removed, added

    def _catalog_entry(self, engine: str, host: str | None) -> EscalationEntry:
        """The handle of a catalog row; the same errors as an entry of a request."""
        return self._speaker.entry_for(engine, host, require_engine(engine).default_speech_unit)


def create_suite(config: SuiteConfig, *, launcher: ManagedLauncher | None = None) -> Suite:
    """The suite for a configuration; ``launcher`` replaces the real one (tests)."""
    return Suite(config, launcher or SuiteLauncher(config))
