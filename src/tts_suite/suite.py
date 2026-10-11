"""The suite: :class:`~tts_suite.api.TTSSuite` implemented with the speaker, the engine registry and a launcher.

Nothing new is decided here — speaking is the :class:`~tts_suite.speaker.Speaker`, engines come from
the registry, containers are the launcher's. This class only joins them to the one entry point.
"""

from __future__ import annotations

import asyncio

from .api import (
    EngineControlFailed,
    EngineInfo,
    EntryStatus,
    SpeakRequest,
    SuiteConfig,
    UnknownEngine,
    VoiceInfo,
)
from .constants import LABEL_LANGUAGES
from .engines.base import shared_voice_names
from .engines.registry import engine_unit, reload_engines, require_engine, voice_names
from .escalation import EscalationEntry
from .launcher import ManagedLauncher, SuiteLauncher
from .speaker import Speaker
from .stream import Stream


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
        config = self._speaker.config
        statuses = await asyncio.gather(*(self._status(entry) for entry in self._speaker.entries))
        return [
            EngineInfo(
                engine=entry.engine.key,
                host=entry.host.name if entry.host is not None else None,
                label={language: self._label(entry, language) for language in LABEL_LANGUAGES},
                label_short={language: entry.engine.label_short for language in LABEL_LANGUAGES},
                enabled=entry.enabled,
                deactivated=entry.engine.deactivated,
                status=status,
                unit=engine_unit(entry.engine, config.engines),
                unit_default=entry.engine.default_speech_unit,
                needs_gpu=entry.engine.needs_gpu,
                cloud=entry.engine.cloud,
                browser=entry.engine.renders_in_browser,
            )
            for entry, status in zip(self._speaker.entries, statuses)
        ]

    async def _status(self, entry: EscalationEntry) -> EntryStatus:
        if entry.engine.deactivated:
            return "stopped"
        return await self._launcher.status(entry)

    @staticmethod
    def _label(entry: EscalationEntry, language: str) -> str:
        if entry.engine.deactivated:
            return entry.engine.key  # its folder lies in disabled/ — it has no i18n.json to read here
        return entry.engine.label(language)

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
        entry = self._entry(engine, host)
        if await self._launcher.is_serving(entry):
            return
        skip = await self._launcher.ensure_serving(entry, wait=True)
        if skip is not None:
            raise EngineControlFailed(f"{entry.label} not started ({skip.reason}): {skip.detail}")

    async def stop(self, engine: str, host: str | None = None) -> None:
        await self._launcher.stop(self._entry(engine, host))

    async def reload(self) -> tuple[list[str], list[str]]:
        removed, added = await asyncio.to_thread(reload_engines)
        self._speaker.set_config(self._speaker.config)  # bind the list to the engines as they are now
        return removed, added

    def _entry(self, engine: str, host: str | None) -> EscalationEntry:
        """The entry of the list for an engine on a host (``None`` = this machine)."""
        for entry in self._speaker.entries:
            if entry.engine.key == engine and (entry.host.name if entry.host else None) == host:
                return entry
        raise UnknownEngine(f"{engine}@{host or 'this machine'} is not in the escalation list")


def create_suite(config: SuiteConfig, *, launcher: ManagedLauncher | None = None) -> Suite:
    """The suite for a configuration; ``launcher`` replaces the real one (tests)."""
    return Suite(config, launcher or SuiteLauncher(config))
