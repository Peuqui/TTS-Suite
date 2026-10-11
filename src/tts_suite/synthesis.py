"""One synthesis: engine call plus post-processing. The one place that knows both."""

from __future__ import annotations

import logging
from concurrent.futures import Executor

from .constants import CLONED_VOICE_PREFIX
from .engines.base import TTSEngine
from .postprocess import adjust_audio, needs_postprocess

logger = logging.getLogger(__name__)


async def synthesize(
    engine: TTSEngine,
    text: str,
    voice: str,
    language: str,
    speed: float = 1.0,
    pitch: float = 1.0,
    *,
    executor: Executor,
) -> bytes:
    """Generate speech with one engine instance and return audio of ``engine.audio_media_type``.

    The ★ prefix that voice lists use for cloned voices is stripped here, so engines see
    the clean name. Speed (for engines without native support) and pitch (for all) are
    applied afterwards with ffmpeg. Raises :class:`~tts_suite.engines.base.TTSFailure`
    when the engine delivers no audio. Blocking engines run in ``executor``.
    """
    voice = voice.removeprefix(CLONED_VOICE_PREFIX)
    logger.info(
        "TTS: engine=%s@%s, voice=%s, lang=%s, speed=%s, pitch=%s",
        engine.key, engine.address, voice, language, speed, pitch,
    )
    audio = await engine.generate_speech_async(text, voice, language, speed, pitch, executor=executor)
    if needs_postprocess(engine, speed, pitch):
        # Speed only for engines that cannot do it themselves; native ones already applied it.
        applied_speed = speed if engine.needs_speed_postprocess else 1.0
        audio = await adjust_audio(audio, engine.audio_media_type, applied_speed, pitch)
    return audio
