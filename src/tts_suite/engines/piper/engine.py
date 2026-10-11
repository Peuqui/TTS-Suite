"""Piper TTS — offline neural voices, runs as a Python subprocess."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ...constants import PIPER_MODELS_DIR
from ..base import TTSEngine, TTSFailure, validate_audio

logger = logging.getLogger(__name__)

# Display name → (ONNX model filename, language code). Models live in
# ``PIPER_MODELS_DIR`` (``<suite root>/piper_models/<filename>``). Adding a new voice:
# drop the .onnx file in that directory and add a line here.
PIPER_VOICES: dict[str, tuple[str, str]] = {
    # Deutsch — männliche Stimmen
    "Deutsch (Thorsten)":  ("de_DE-thorsten-high.onnx", "de"),
    "Deutsch (Karlsson)":  ("de_DE-karlsson-low.onnx",  "de"),
    # Deutsch — weibliche Stimmen
    "Deutsch (Ramona)":    ("de_DE-ramona-low.onnx",    "de"),
    "Deutsch (Kerstin)":   ("de_DE-kerstin-low.onnx",   "de"),
    "Deutsch (Eva K)":     ("de_DE-eva_k-x_low.onnx",   "de"),
    "Deutsch (MLS)":       ("de_DE-mls-medium.onnx",    "de"),  # multi-speaker
}

#: Standard voice when the requested one is unknown — a key of PIPER_VOICES.
PIPER_STANDARD_VOICE = "Deutsch (Thorsten)"


def _piper_binary() -> Path:
    """Path to the piper executable next to the Python interpreter that runs the suite
    (its virtual environment)."""
    name = "piper.exe" if os.name == "nt" else "piper"
    return Path(sys.executable).parent / name


class PiperEngine(TTSEngine):
    key = "piper"
    default_speech_unit = "whole"
    label_short = "Piper"
    runs_in_container = False
    needs_gpu = False
    # Piper applies its --length-scale internally — no ffmpeg needed.
    needs_speed_postprocess = False
    display_order = 60
    in_default_escalation = True

    @property
    def voices_fallback(self) -> dict[str, str]:
        # Voice lists only need the display names; the (model, lang) tuple
        # is used internally during synthesis.
        return {name: name for name in PIPER_VOICES}

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
        """Piper subprocess synth. Speed is applied natively via
        ``--length_scale`` (no ffmpeg post-processing needed)."""
        if voice in PIPER_VOICES:
            model_filename, _lang = PIPER_VOICES[voice]
        else:
            # Unknown voice (e.g. renamed): the engine's standard voice speaks
            # instead of nothing — visible in the log.
            model_filename, _lang = PIPER_VOICES[PIPER_STANDARD_VOICE]
            logger.warning("Piper: voice %r unknown — speaking with the standard voice %r", voice, PIPER_STANDARD_VOICE)
        model_path = PIPER_MODELS_DIR / model_filename

        # length_scale inverts speed: higher = slower (1.0 = normal,
        # 0.8 ≈ 1.25× faster, 0.5 = 2× faster).
        length_scale = 1.0 / speed
        logger.info("Piper TTS: voice=%s, speed=%s, length_scale=%s", voice, speed, length_scale)

        with tempfile.TemporaryDirectory(prefix="tts_suite_piper_") as work:
            output_file = Path(work) / "out.wav"
            result = subprocess.run(
                [str(_piper_binary()), "--model", str(model_path), "--output_file", str(output_file),
                 "--length_scale", str(length_scale)],
                input=text.encode("utf-8"),
                capture_output=True,
            )
            if result.returncode != 0 or not output_file.exists():
                raise TTSFailure("engine", f"Piper exit {result.returncode}: {result.stderr.decode()[:200]}")
            audio = output_file.read_bytes()
        if not validate_audio(audio):
            raise TTSFailure("engine", "Piper returned no usable audio")
        logger.info("Piper TTS: audio received (%d bytes)", len(audio))
        return audio
