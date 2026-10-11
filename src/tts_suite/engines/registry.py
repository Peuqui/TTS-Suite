"""Engine registry — auto-discovers all ``TTSEngine`` subclasses.

Drop a new folder ``foo/`` with ``engine.py`` (a ``TTSEngine`` subclass with
``key`` and ``label_short``) and ``i18n.json`` into this package, and it lights up
everywhere (engine lists, voice catalogs, the escalation) — no extra registration
code, no central dict to keep in sync.

The discovery order is determined by the engine's ``display_order``
class attribute (lower number = earlier in lists). This is the single source of
truth for "which TTS engines exist"; there is no parallel hardcoded list. A folder
moved to ``disabled/`` is not loaded; ``reload_engines()`` applies such a move
without a restart.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Iterator, Mapping, Protocol

from ..api import EngineDefaults, EntryConfig, UnknownEngine, Unit
from .base import SPEECH_UNITS, TTSEngine

ENGINES_DIR = Path(__file__).parent
#: Engine folders moved here are not loaded.
DISABLED_DIR = ENGINES_DIR / "disabled"
_PACKAGE = __name__.rsplit(".", 1)[0]   # "tts_suite.engines"
_SHARED_MODULES = {"base", "registry"}


def _discover_engines() -> dict[str, TTSEngine]:
    """Import ``<engine>/engine.py`` of every engine folder and instantiate the
    ``TTSEngine`` subclass(es) it defines, ordered by ``display_order``
    (ties broken by key for stability)."""
    instances: list[TTSEngine] = []
    for engine_file in sorted(ENGINES_DIR.glob("*/engine.py")):
        module = importlib.import_module(f"{_PACKAGE}.{engine_file.parent.name}.engine")
        instances.extend(
            cls()  # type: ignore[abstract]
            for cls in vars(module).values()
            if isinstance(cls, type) and issubclass(cls, TTSEngine)
            and cls.__module__ == module.__name__
        )
    instances.sort(key=lambda e: (e.display_order, e.key))
    return {e.key: e for e in instances}


TTS_ENGINES: dict[str, TTSEngine] = _discover_engines()


def reload_engines() -> tuple[list[str], list[str]]:
    """Re-read the engine folders after one was moved into or out of
    ``disabled/`` — no restart needed. Engines that vanish are stopped first if
    their container runs here. Returns ``(removed_keys, added_keys)``; nothing is
    touched when the engine folders are the registered ones (folder name = key)."""
    if {path.parent.name for path in ENGINES_DIR.glob("*/engine.py")} == set(TTS_ENGINES):
        return [], []
    engine_module_index = len(_PACKAGE.split("."))
    for name in [n for n in sys.modules if n.startswith(f"{_PACKAGE}.")]:
        if name.split(".")[engine_module_index] not in _SHARED_MODULES:
            del sys.modules[name]
    importlib.invalidate_caches()
    found = _discover_engines()
    removed = [key for key in TTS_ENGINES if key not in found]
    for key in removed:
        engine = TTS_ENGINES[key]
        if engine.runs_in_container and engine.is_running():
            engine.stop()
    added = [key for key in found if key not in TTS_ENGINES]
    # Update first, then drop the vanished keys: a reader never sees an empty
    # registry. Re-enabled engines come last until the next restart sorts them.
    TTS_ENGINES.update(found)
    for key in removed:
        del TTS_ENGINES[key]
    return removed, added


def is_deactivated(key: str) -> bool:
    """True if ``key`` names an engine whose folder lies in ``disabled/``."""
    return (DISABLED_DIR / key / "engine.py").is_file()


def get_engine(key: str) -> TTSEngine | None:
    """Return the TTSEngine for ``key``, or ``None`` if no engine
    registered under that key. Use this instead of ``TTS_ENGINES.get``
    at call sites so the call signature is easy to grep for."""
    return TTS_ENGINES.get(key)


def require_engine(key: str) -> TTSEngine:
    """Like :func:`get_engine`, but an unknown key is an error (:class:`UnknownEngine`)
    — for call sites where the key comes from a validated configuration."""
    engine = TTS_ENGINES.get(key)
    if engine is None:
        raise UnknownEngine(f"unknown TTS engine {key!r}")
    return engine


def default_escalation() -> tuple[EntryConfig, ...]:
    """Escalation list of a fresh install: the engines with
    ``in_default_escalation``, in ``display_order`` (the browser, which speaks
    last, has the highest)."""
    return tuple(EntryConfig(engine=e.key) for e in TTS_ENGINES.values() if e.in_default_escalation)


def gpu_engine_keys() -> set[str]:
    """Keys of the engines that occupy GPU VRAM (container engines on a card)."""
    return {e.key for e in gpu_engines()}


def gpu_engines() -> Iterator[TTSEngine]:
    """Iterate engines that occupy GPU VRAM."""
    return (e for e in TTS_ENGINES.values() if e.needs_gpu)


def installed_gpu_engines() -> list[TTSEngine]:
    """GPU engines whose Docker image is built on this host — i.e.
    engines that can actually be started right now."""
    return [e for e in gpu_engines() if e.is_installed()]


def engine_unit(engine: TTSEngine, defaults: Mapping[str, EngineDefaults]) -> Unit:
    """Unit of speech of an engine instance: the configured choice (``EngineDefaults.unit``) or the
    engine's default. Pure function of the configuration; applies to every client alike."""
    configured = defaults[engine.key].unit if engine.key in defaults else None
    unit = configured or engine.default_speech_unit
    if unit not in SPEECH_UNITS:
        raise ValueError(f"speech unit of {engine.key!r} must be one of {SPEECH_UNITS}, got {unit!r}")
    return unit


def speech_unit(engine_key: str, defaults: Mapping[str, EngineDefaults]) -> Unit:
    """:func:`engine_unit` for a registered engine named by its key."""
    return engine_unit(require_engine(engine_key), defaults)


class VoiceCatalog(Protocol):
    """The part of a TTS engine ``voice_names`` reads — every TTSEngine is one."""

    @property
    def voices_fallback(self) -> dict[str, str]: ...

    def get_voices(self) -> dict[str, str]: ...


def voice_names(engine: VoiceCatalog) -> list[str]:
    """Catalog names of an engine: live ``get_voices()``, on error OR an
    empty result the static ``voices_fallback``.

    Container engines return ``{}`` (no exception) while the container is
    down — the catalog pattern documented in base.py.
    """
    try:
        voices = engine.get_voices()
    except Exception:  # noqa: BLE001 — down/unreachable == empty catalog
        voices = {}
    return list((voices or engine.voices_fallback).keys())


def parse_speed_factor(raw: object) -> float | None:
    """``"1.25x"``/``"1.25"``/``1.25`` → 1.25; ``None``/``""``/garbage → None.

    Only the PARSER is the SSOT — whether the caller falls back to a default on None
    or aborts loudly is a deliberate policy of each consumer.
    """
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace("x", ""))
    except (ValueError, TypeError):
        return None
