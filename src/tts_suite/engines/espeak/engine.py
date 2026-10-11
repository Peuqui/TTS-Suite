"""eSpeak — minimal robotic offline TTS."""
from __future__ import annotations

import logging
import subprocess

from ...constants import ESPEAK_BASE_WPM, ESPEAK_VOICE_LIST_TIMEOUT_S
from ..base import TTSEngine, TTSFailure, validate_audio

logger = logging.getLogger(__name__)

#: Voice id used when the requested voice is unknown.
ESPEAK_STANDARD_VOICE = "de"

# All catalogued eSpeak voices. Format: display_name → (espeak_voice_id, language).
# Standard voices (de, en, ...) are always available. mbrola voices need
# the corresponding ``mbrola`` + ``mbrola-deX`` apt packages and get
# filtered out at runtime if not installed (see :func:`_detect_available_voices`).
_ESPEAK_VOICES_ALL: dict[str, tuple[str, str]] = {
    # Deutsch — Standard eSpeak (robotic, always available)
    "Deutsch Standard":         ("de",      "de"),
    "Deutsch Männlich 1":       ("de+m1",   "de"),
    "Deutsch Männlich 2":       ("de+m2",   "de"),
    "Deutsch Weiblich 1":       ("de+f1",   "de"),
    "Deutsch Weiblich 2":       ("de+f2",   "de"),
    # Deutsch — mbrola (more natural, requires apt packages)
    "Deutsch mbrola-2 (M)":     ("mb/mb-de2", "de"),
    "Deutsch mbrola-3 (F)":     ("mb/mb-de3", "de"),
    "Deutsch mbrola-4 (M)":     ("mb/mb-de4", "de"),
    "Deutsch mbrola-5 (F)":     ("mb/mb-de5", "de"),
    "Deutsch mbrola-6 (M)":     ("mb/mb-de6", "de"),
    "Deutsch mbrola-7 (F)":     ("mb/mb-de7", "de"),
    # Englisch — Standard eSpeak (always available)
    "Englisch Standard":        ("en",      "en"),
    "Englisch US":              ("en-us",   "en"),
    "Englisch UK":              ("en-gb",   "en"),
    # Englisch — mbrola
    "Englisch mbrola UK (M)":   ("mb/mb-en1", "en"),
    "Englisch mbrola US-1 (F)": ("mb/mb-us1", "en"),
    "Englisch mbrola US-2 (M)": ("mb/mb-us2", "en"),
    "Englisch mbrola US-3 (M)": ("mb/mb-us3", "en"),
}


def _detect_available_voices() -> dict[str, tuple[str, str]]:
    """Filter ``_ESPEAK_VOICES_ALL`` down to the voices actually
    installed on this host. mbrola voices are kept only when
    ``espeak-ng --voices=mb`` lists them."""
    mbrola_available: set[str] = set()
    try:
        result = subprocess.run(
            ["espeak-ng", "--voices=mb"], capture_output=True, text=True, timeout=ESPEAK_VOICE_LIST_TIMEOUT_S,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n")[1:]:
                parts = line.split()
                # File column (index 4) holds the voice id like "mb/mb-de2".
                if len(parts) >= 5 and parts[4].startswith("mb/"):
                    mbrola_available.add(parts[4])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    available: dict[str, tuple[str, str]] = {}
    for name, (voice_id, lang) in _ESPEAK_VOICES_ALL.items():
        if voice_id.startswith("mb/"):
            if voice_id in mbrola_available:
                available[name] = (voice_id, lang)
        else:
            available[name] = (voice_id, lang)
    return available


# Cached at module load — re-detection on every dropdown render would
# fork espeak-ng each time.
_AVAILABLE_VOICES: dict[str, tuple[str, str]] = _detect_available_voices()


class EspeakEngine(TTSEngine):
    key = "espeak"
    default_speech_unit = "whole"
    label_short = "eSpeak"
    runs_in_container = False
    needs_gpu = False
    # eSpeak's -s flag controls speed natively.
    needs_speed_postprocess = False
    display_order = 70

    @property
    def voices_fallback(self) -> dict[str, str]:
        # Voice lists only need display names; the (voice_id, lang) tuple
        # is used internally during synthesis.
        return {name: name for name in _AVAILABLE_VOICES}

    def get_voices(self) -> dict[str, str]:
        return self.voices_fallback

    def generate_speech(
        self,
        text: str,
        voice: str,
        language: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> bytes:
        """eSpeak subprocess synth (WAV on stdout). Speed is applied natively via the
        words-per-minute ``-s`` flag."""
        voice_config = _AVAILABLE_VOICES.get(voice)
        if voice_config:
            voice_id, _ = voice_config
        else:
            # Unknown voice (e.g. renamed): the engine's standard voice speaks
            # instead of nothing — visible in the log.
            voice_id = ESPEAK_STANDARD_VOICE
            logger.warning("eSpeak: voice %r unknown — speaking with the standard voice %r", voice, voice_id)

        # eSpeak speed = words per minute: 1.0 → 175 wpm, 1.25 → 220 wpm, 2.0 → 350 wpm.
        wpm = int(ESPEAK_BASE_WPM * speed)
        logger.info("eSpeak TTS: voice=%s, speed=%s, wpm=%d", voice_id, speed, wpm)

        try:
            result = subprocess.run(
                ["espeak-ng", "-v", voice_id, "-s", str(wpm), "--stdout", text],
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise TTSFailure("engine", "espeak-ng not installed (sudo apt install espeak-ng)") from exc
        if result.returncode != 0 or not validate_audio(result.stdout):
            error = result.stderr.decode() if result.stderr else "no audio"
            raise TTSFailure("engine", f"eSpeak exit {result.returncode}: {error[:200]}")
        logger.info("eSpeak TTS: audio received (%d bytes)", len(result.stdout))
        return result.stdout
