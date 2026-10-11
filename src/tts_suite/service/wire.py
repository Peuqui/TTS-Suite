"""The JSON form of the engine layer's types. They come from the dataclasses of ``tts_suite.api``
through pydantic, so there is no second description of any of them."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from ..api import (
    ConfigInvalid,
    EngineControlFailed,
    EngineInfo,
    EngineInUse,
    EngineStarting,
    NoEngineAvailable,
    Overloaded,
    SpeakRequest,
    SpeakSummary,
    SuiteConfig,
    SuiteError,
    UnknownEngine,
    VoiceInfo,
)
from ..i18n import UnsupportedLanguage

_CONFIG = TypeAdapter(SuiteConfig)
_ENGINES = TypeAdapter(list[EngineInfo])
_VOICES = TypeAdapter(list[VoiceInfo])
_REQUEST = TypeAdapter(SpeakRequest)
_SUMMARY = TypeAdapter(SpeakSummary)

# Every error the suite raises on purpose: its HTTP status and its code in `{"error": ...}`.
ERROR_ANSWERS: dict[type[SuiteError], tuple[int, str]] = {
    NoEngineAvailable: (503, "no_engine"),
    EngineStarting: (503, "starting"),
    Overloaded: (429, "overloaded"),
    UnknownEngine: (404, "unknown_engine"),
    EngineInUse: (409, "engine_in_use"),
    EngineControlFailed: (502, "engine_control_failed"),
    ConfigInvalid: (400, "config_invalid"),
    UnsupportedLanguage: (422, "unsupported_language"),
}


def config_to_json(config: SuiteConfig) -> Any:
    return _CONFIG.dump_python(config, mode="json")


def config_from_json(data: Any) -> SuiteConfig:
    return _CONFIG.validate_python(data)


def engines_to_json(engines: list[EngineInfo]) -> Any:
    return _ENGINES.dump_python(engines, mode="json")


def voices_to_json(voices: list[VoiceInfo]) -> Any:
    return _VOICES.dump_python(voices, mode="json")


def request_from_json(head: Any) -> SpeakRequest:
    return _REQUEST.validate_python(head)


def summary_to_json(summary: SpeakSummary) -> Any:
    return _SUMMARY.dump_python(summary, mode="json")


def error_answer(error: SuiteError) -> tuple[int, dict[str, Any]]:
    """The status and the body (`{"error", "detail"}`, with the `trace` of a failed escalation)
    of an error the suite raised."""
    status, code = ERROR_ANSWERS[type(error)]
    body: dict[str, Any] = {"error": code, "detail": str(error)}
    if isinstance(error, NoEngineAvailable):
        body["trace"] = [step.__dict__ for step in error.trace]
    return status, body
