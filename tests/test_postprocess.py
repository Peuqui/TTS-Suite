"""Post-processing with ffmpeg (bytes in, bytes out) and the synthesis dispatch."""
from __future__ import annotations

import asyncio
import io
import shutil
import wave
from typing import Any

import pytest

from tts_suite import postprocess
from tts_suite.engines import TTSEngine, TTSFailure, get_engine
from tts_suite.postprocess import adjust_audio, build_filter_chain, needs_postprocess
from tts_suite.synthesis import synthesize

FFMPEG_MISSING = shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None
needs_ffmpeg = pytest.mark.skipif(FFMPEG_MISSING, reason="ffmpeg/ffprobe not installed — real post-processing cannot run")

SAMPLE_RATE = 24000


def _wav(seconds: float = 1.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"\x10\x00" * int(SAMPLE_RATE * seconds))
    return buffer.getvalue()


def _duration(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio)) as wav:
        return wav.getnframes() / wav.getframerate()


def _engine(key: str) -> TTSEngine:
    engine = get_engine(key)
    assert engine is not None
    return engine


# ── Pure logic (no ffmpeg needed) ──────────────────────────────────

def test_filter_chain_for_speed_and_pitch() -> None:
    assert build_filter_chain(24000, speed=1.25, pitch=1.0) == "atempo=1.25"
    assert build_filter_chain(24000, speed=1.0, pitch=1.2) == "asetrate=28800,aresample=24000"
    assert build_filter_chain(24000, speed=1.5, pitch=0.9) == "asetrate=21600,aresample=24000,atempo=1.5"


def test_extreme_speeds_chain_several_atempo_filters() -> None:
    assert build_filter_chain(24000, speed=4.0, pitch=1.0) == "atempo=2.0,atempo=2.0"
    assert build_filter_chain(24000, speed=0.3, pitch=1.0) == "atempo=0.5,atempo=0.6"


def test_unchanged_factors_need_no_filter() -> None:
    assert build_filter_chain(24000, speed=1.005, pitch=0.995) == ""


def test_speed_counts_only_for_engines_without_native_speed() -> None:
    assert needs_postprocess(_engine("xtts"), speed=1.25, pitch=1.0)
    assert not needs_postprocess(_engine("piper"), speed=1.25, pitch=1.0)
    assert not needs_postprocess(_engine("xtts"), speed=1.0, pitch=1.0)


def test_pitch_is_a_post_step_for_every_engine() -> None:
    assert needs_postprocess(_engine("piper"), speed=1.0, pitch=1.2)


def test_unchanged_audio_is_passed_through_untouched() -> None:
    audio = _wav()
    assert asyncio.run(adjust_audio(audio, "audio/wav", speed=1.0, pitch=1.0)) is audio


def test_an_ffprobe_that_cannot_tell_the_sample_rate_is_a_software_failure_not_a_guessed_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(command: list[str], timeout: float | None = None) -> tuple[int, bytes, bytes]:
        assert command[0] == "ffprobe"  # ffmpeg must not even be called with a guessed rate
        return 1, b"", b"Invalid data found"

    monkeypatch.setattr(postprocess, "_run", failing)
    with pytest.raises(TTSFailure) as failure:
        asyncio.run(adjust_audio(_wav(), "audio/wav", speed=1.25))
    assert failure.value.reason == "software" and "sample rate" in failure.value.detail


def test_missing_ffmpeg_is_a_software_failure_not_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(TTSFailure) as failure:
        asyncio.run(adjust_audio(_wav(), "audio/wav", speed=1.25))
    assert failure.value.reason == "software"
    assert "not installed" in failure.value.detail


# ── Real ffmpeg ────────────────────────────────────────────────────

@needs_ffmpeg
def test_speed_up_shortens_the_audio() -> None:
    faster = asyncio.run(adjust_audio(_wav(2.0), "audio/wav", speed=2.0))
    assert _duration(faster) == pytest.approx(1.0, abs=0.1)


@needs_ffmpeg
def test_slow_down_lengthens_the_audio() -> None:
    slower = asyncio.run(adjust_audio(_wav(1.0), "audio/wav", speed=0.5))
    assert _duration(slower) == pytest.approx(2.0, abs=0.1)


@needs_ffmpeg
def test_pitch_keeps_the_format_and_shortens_by_the_factor() -> None:
    # asetrate + aresample (the original chain) raises the pitch AND the tempo by the factor.
    changed = asyncio.run(adjust_audio(_wav(1.0), "audio/wav", pitch=1.2))
    assert changed[:4] == b"RIFF"
    assert _duration(changed) == pytest.approx(1.0 / 1.2, abs=0.05)


# ── synthesize() ───────────────────────────────────────────────────

class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def generate(self, text: str, voice: str, language: str, speed: float, pitch: float) -> bytes:
        self.calls.append((text, voice, language, speed, pitch))
        return _wav(1.0)


def test_synthesize_strips_the_star_and_skips_ffmpeg_for_unchanged_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine("xtts")
    recorder = _Recorder()
    monkeypatch.setattr(engine, "generate_speech_async", recorder.generate)
    audio = asyncio.run(synthesize(engine, "Hallo", "★ Codine", "de"))
    assert recorder.calls == [("Hallo", "Codine", "de", 1.0, 1.0)]
    assert _duration(audio) == pytest.approx(1.0)


@needs_ffmpeg
def test_synthesize_post_processes_speed_for_engines_without_native_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine("xtts")
    monkeypatch.setattr(engine, "generate_speech_async", _Recorder().generate)
    monkeypatch.setattr(type(engine), "audio_media_type", "audio/wav")
    audio = asyncio.run(synthesize(engine, "Hallo", "Codine", "de", speed=2.0))
    assert _duration(audio) == pytest.approx(0.5, abs=0.1)


@needs_ffmpeg
def test_synthesize_leaves_native_speed_to_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine("piper")
    recorder = _Recorder()
    monkeypatch.setattr(engine, "generate_speech_async", recorder.generate)
    audio = asyncio.run(synthesize(engine, "Hallo", "Deutsch (Thorsten)", "de", speed=2.0))
    assert recorder.calls[0][3] == 2.0          # handed to the engine
    assert _duration(audio) == pytest.approx(1.0)  # and not applied a second time
