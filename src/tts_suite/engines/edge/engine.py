"""Edge TTS — Microsoft cloud, always-on, used as fallback."""
from __future__ import annotations

import logging

from ...constants import MEDIA_TYPE_MP3
from ..base import TTSEngine, TTSFailure, validate_audio

logger = logging.getLogger(__name__)

#: Voice id used when the requested display name is unknown.
EDGE_STANDARD_VOICE = "de-DE-KatjaNeural"


class EdgeEngine(TTSEngine):
    key = "edge"
    label_short = "Edge"
    runs_in_container = False
    needs_gpu = False
    # Edge respects the rate parameter natively — no ffmpeg post.
    needs_speed_postprocess = False
    audio_media_type = MEDIA_TYPE_MP3
    display_order = 80
    in_default_escalation = True
    cloud = True

    # display name → Microsoft Neural Voice id. Static catalogue —
    # Edge has no live discovery endpoint we use.
    _VOICES: dict[str, str] = {
        # Deutschland (de-DE)
        "Deutsch (Katja)":       "de-DE-KatjaNeural",
        "Deutsch (Amala)":       "de-DE-AmalaNeural",
        "Deutsch (Seraphina)":   "de-DE-SeraphinaMultilingualNeural",
        "Deutsch (Conrad)":      "de-DE-ConradNeural",
        "Deutsch (Killian)":     "de-DE-KillianNeural",
        "Deutsch (Florian)":     "de-DE-FlorianMultilingualNeural",
        # Österreich (de-AT)
        "Österreich (Ingrid)":   "de-AT-IngridNeural",
        "Österreich (Jonas)":    "de-AT-JonasNeural",
        # Schweiz (de-CH)
        "Schweiz (Leni)":        "de-CH-LeniNeural",
        "Schweiz (Jan)":         "de-CH-JanNeural",
        # Englisch
        "Englisch (Jenny)":      "en-US-JennyNeural",
        "Englisch (Guy)":        "en-US-GuyNeural",
        # Weitere Sprachen
        "Französisch (Denise)":  "fr-FR-DeniseNeural",
        "Spanisch (Elvira)":     "es-ES-ElviraNeural",
    }

    @property
    def voices_fallback(self) -> dict[str, str]:
        return dict(self._VOICES)

    def get_voices(self) -> dict[str, str]:
        return self.voices_fallback

    async def generate_speech_async(
        self,
        text: str,
        voice: str,
        language: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> bytes:
        """Edge TTS — native async via edge-tts library. ``voice`` here
        is the display name; we map it through ``voices_fallback`` to
        the Microsoft voice id (``de-DE-KatjaNeural`` etc.). Speed is
        applied natively via the rate string (``+25%`` = 25% faster);
        pitch is not supported by Edge and gets ignored at this layer."""
        import edge_tts
        from edge_tts.exceptions import EdgeTTSException

        voice_id = self.voices_fallback.get(voice, EDGE_STANDARD_VOICE)

        # Edge TTS rate format: "+X%" / "-X%". 1.0 → "+0%", 1.25 → "+25%".
        rate = f"{round((speed - 1.0) * 100):+d}%"

        if not text.strip():
            raise TTSFailure("software", "Edge TTS called with empty text")

        logger.info("Edge TTS: voice=%s, rate=%s, text_length=%d", voice_id, rate, len(text))
        audio = bytearray()
        try:
            async for chunk in edge_tts.Communicate(text, voice_id, rate=rate).stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
        except EdgeTTSException as exc:
            raise TTSFailure("engine", f"Edge TTS: {type(exc).__name__}: {exc}") from exc
        except OSError as exc:  # aiohttp connection errors are OSError subclasses
            raise TTSFailure("unreachable", f"Edge TTS: {type(exc).__name__}: {exc}") from exc
        if not validate_audio(bytes(audio)):
            raise TTSFailure("engine", "Edge TTS returned no usable audio")
        logger.info("Edge TTS: audio received (%d bytes)", len(audio))
        return bytes(audio)
