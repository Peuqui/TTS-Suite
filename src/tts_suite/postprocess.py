"""Post-processing of synthesized audio: speed and pitch with ffmpeg.

Engines without native speed support (``needs_speed_postprocess``) and every engine for
pitch go through here. Bytes in, bytes out; ffmpeg runs as a subprocess without blocking
the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from .constants import (
    ATEMPO_MAX,
    ATEMPO_MIN,
    AUDIO_EXTENSIONS,
    DEFAULT_SAMPLE_RATE_HZ,
    FFPROBE_TIMEOUT_S,
    POSTPROCESS_TOLERANCE,
)
from .engines.base import TTSEngine, TTSFailure

logger = logging.getLogger(__name__)


def _differs_from_one(factor: float) -> bool:
    return abs(factor - 1.0) >= POSTPROCESS_TOLERANCE


def needs_postprocess(engine: TTSEngine, speed: float, pitch: float) -> bool:
    """Speed only counts for engines that cannot do it themselves; pitch is a post-step for all."""
    return (engine.needs_speed_postprocess and _differs_from_one(speed)) or _differs_from_one(pitch)


def build_filter_chain(sample_rate: int, speed: float, pitch: float) -> str:
    """The ffmpeg ``-af`` chain for the given factors (1.0 = unchanged).

    Pitch: ``asetrate`` + ``aresample`` (raises the pitch; the tempo rises with it by the same factor).
    Speed: ``atempo`` (changes tempo without pitch), chained for factors beyond its range.
    """
    filters: list[str] = []
    if _differs_from_one(pitch):
        filters.append(f"asetrate={int(sample_rate * pitch)}")
        filters.append(f"aresample={sample_rate}")
    if _differs_from_one(speed):
        remaining = speed
        while remaining > ATEMPO_MAX:
            filters.append(f"atempo={ATEMPO_MAX}")
            remaining /= ATEMPO_MAX
        while remaining < ATEMPO_MIN:
            filters.append(f"atempo={ATEMPO_MIN}")
            remaining /= ATEMPO_MIN
        if _differs_from_one(remaining):
            filters.append(f"atempo={remaining}")
    return ",".join(filters)


async def _run(command: list[str], timeout: float | None = None) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TTSFailure("software", f"{command[0]} not installed (needed for speed/pitch)") from exc
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    return process.returncode or 0, stdout, stderr


async def _sample_rate(audio_file: Path) -> int:
    """Sample rate of the file; the XTTS/Qwen3 rate when ``ffprobe`` cannot tell."""
    code, stdout, _ = await _run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_file)],
        timeout=FFPROBE_TIMEOUT_S,
    )
    text = stdout.decode().strip()
    return int(text) if code == 0 and text.isdigit() else DEFAULT_SAMPLE_RATE_HZ


async def adjust_audio(audio: bytes, media_type: str, speed: float = 1.0, pitch: float = 1.0) -> bytes:
    """Apply speed and pitch to ``audio`` and return the result in the same format.

    Raises :class:`TTSFailure` (``software``) if ffmpeg is missing or fails — audio with
    the wrong speed is not passed on silently.
    """
    if not (_differs_from_one(speed) or _differs_from_one(pitch)):
        return audio
    extension = AUDIO_EXTENSIONS[media_type]
    with tempfile.TemporaryDirectory(prefix="tts_suite_") as work:
        source = Path(work) / f"in.{extension}"
        target = Path(work) / f"out.{extension}"
        source.write_bytes(audio)
        sample_rate = await _sample_rate(source)
        chain = build_filter_chain(sample_rate, speed, pitch)
        logger.info("ffmpeg: speed=%sx pitch=%sx (%s)", speed, pitch, chain)
        code, _, stderr = await _run(["ffmpeg", "-y", "-i", str(source), "-af", chain, str(target)])
        if code != 0 or not target.exists():
            raise TTSFailure("software", f"ffmpeg failed (exit {code}): {stderr.decode()[:200]}")
        return target.read_bytes()
