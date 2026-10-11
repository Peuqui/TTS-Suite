"""TTS engine plugin registry — SSOT for everything a caller needs to know about an
individual TTS backend.

Each engine lives in its own folder (``<key>/engine.py``, ``i18n.json``), subclasses
:class:`TTSEngine` and is found by the registry. Callers route through the registry
instead of branching on the engine key — adding an engine is a new folder, nothing else.

    from tts_suite.engines import get_engine

    engine = get_engine("qwen3local")
    if engine.is_running():
        audio = await engine.generate_speech_async("Hallo", "Alice", "de")
"""

from .base import SPEECH_UNITS, DeactivatedEngine, TTSEngine, TTSFailure, shared_voice_names, validate_audio
from .registry import (
    TTS_ENGINES,
    default_escalation,
    engine_unit,
    get_engine,
    gpu_engine_keys,
    gpu_engines,
    installed_gpu_engines,
    is_deactivated,
    parse_speed_factor,
    reload_engines,
    require_engine,
    speech_unit,
    voice_names,
)

__all__ = [
    "SPEECH_UNITS",
    "DeactivatedEngine",
    "TTSEngine",
    "TTSFailure",
    "TTS_ENGINES",
    "default_escalation",
    "engine_unit",
    "get_engine",
    "gpu_engine_keys",
    "gpu_engines",
    "installed_gpu_engines",
    "is_deactivated",
    "parse_speed_factor",
    "reload_engines",
    "require_engine",
    "shared_voice_names",
    "speech_unit",
    "validate_audio",
    "voice_names",
]
