"""DashScope Qwen-Audio 3.0 TTS — cloud TTS of Alibaba, no local GPU.

HTTP endpoint whose result is a URL to a WAV file, a catalog of system voices
and cloned voices enrolled for the flash model (``enroll.py``). Replaced
the older Qwen3-TTS cloud engine (10.10.2026), whose cloned voices Alibaba
switched off.

The API key comes from the environment (``DASHSCOPE_API_KEY_ENV``), never from the configuration.
"""
from __future__ import annotations

import io
import logging
import os
import socket
import struct
import wave
from http import HTTPStatus
from typing import Iterator

import requests

from ...api import FailureReason
from ...constants import CLONED_VOICE_PREFIX, DASHSCOPE_API_KEY_ENV
from ..base import TTSEngine, TTSFailure, request_failure, validate_audio

logger = logging.getLogger(__name__)

# Display name → Alibaba voice id (docs: "Qwen-Audio TTS voice list", 10.10.2026).
# All of them speak German understandably despite the docs listing only
# Chinese and English; the voice carries the accent.
_VOICES: dict[str, str] = {
    "Mary":       "loongmary",          # English, warm British accent
    "Eva":        "loongeva_v3.6",      # English, American
    "John":       "loongjohn",          # English, American, male
    "Ling Xin":   "longanlingxin",      # companion, warm (plus model)
    "Lu Feng":    "longanlufeng",       # companion, bright, male (plus model)
    "Feng Yue":   "longanfengyue",
    "Yuan Fei":   "longanyuanfei",
    "Ling Xi":    "longanlingxi",
    "Xiao Xin":   "longanxiaoxin",
    "Huan":       "longanhuan_v3.6",
    "Li Dou":     "longjielidou_v3.6",  # child, male
    "Pao Pao":    "longpaopao_v3.6",    # child
    "Huo Huo":    "longhuohuo_v3.6",    # character, male
    "Chuan Shu":  "longchuanshu_v3.6",  # character, male, Sichuan accent
}
# The two flagship voices only work with the plus model.
_PLUS_VOICE_IDS = {"longanlingxin", "longanlufeng"}

#: Network errors that are no ``requests`` exceptions (``requests`` wraps most socket failures in its own).
_SOCKET_ERROR_TYPES = (ConnectionError, TimeoutError, socket.gaierror)


def _api_key() -> str:
    return os.environ.get(DASHSCOPE_API_KEY_ENV, "")


def _apply_pcm_gain(pcm_data: bytes, gain: float) -> bytes:
    """Apply volume gain to 16-bit PCM data. Returns unchanged data if gain == 1.0."""
    if gain == 1.0:
        return pcm_data
    samples = struct.unpack(f"<{len(pcm_data) // 2}h", pcm_data)
    gained = [max(-32768, min(32767, int(s * gain))) for s in samples]
    return struct.pack(f"<{len(gained)}h", *gained)


class DashScopeAudio3Engine(TTSEngine):
    key = "dashscope_audio3"
    label_short = "DashScope Audio 3"
    runs_in_container = False
    needs_gpu = False
    needs_speed_postprocess = True
    supports_language = True
    display_order = 50
    in_default_escalation = True
    cloud = True
    default_voice = "Mary"

    base_url: str = "https://dashscope-intl.aliyuncs.com/api/v1"
    # Cloned voices are bound to the model they were enrolled for (enroll.py
    # reads it from here).
    model_flash: str = "qwen-audio-3.0-tts-flash"
    model_plus: str = "qwen-audio-3.0-tts-plus"
    # The output is already as loud as the local engines (about -21 LUFS).
    output_gain: float = 1.0
    #: Seconds to wait for the synthesis and for the download of its WAV file.
    request_timeout_s: int = 60
    sample_rate_hz: int = 24000

    # ISO short code → DashScope language_type.
    language_map_dashscope: dict[str, str] = {
        "de": "German",   "en": "English", "fr": "French",   "es": "Spanish",
        "it": "Italian",  "pt": "Portuguese", "ru": "Russian",
        "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    }

    def is_running(self) -> bool:
        """Cloud engine: usable as soon as the API key is configured."""
        return bool(_api_key())

    @property
    def voices_fallback(self) -> dict[str, str]:
        # Cloned voices (★ …) on top of the system voices.
        return {**self._cloned_voices(), **_VOICES}

    def get_voices(self) -> dict[str, str]:
        # Fixed catalog; no live discovery endpoint.
        return self.voices_fallback

    def _cloned_voices(self) -> dict[str, str]:
        """Enrolled cloned voices from the mapping (data folder): '★ Name' → voice id. Read live
        (cheap JSON read) so a freshly enrolled WAV shows up without a restart."""
        from .enroll import load_mapping
        return {
            f"{CLONED_VOICE_PREFIX}{name}": entry["voice_id"]
            for name, entry in load_mapping().items()
            if entry.get("voice_id")
        }

    def prepare_voices(self) -> Iterator[str]:
        """Enroll new or changed reference voices (idempotent via WAV hash —
        instant when nothing changed) so the cloned voices are ready."""
        from .enroll import enroll_progress
        api_key = _api_key()
        if api_key:
            yield from enroll_progress(api_key, self.shared_voices_dir)

    def generate_speech(
        self,
        text: str,
        voice: str,
        language: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> bytes:
        """Synthesise ``text`` and fetch the WAV file. Needs the API key in the
        environment. Speed/pitch are post-processed via ffmpeg."""
        try:
            api_key = _api_key()
            if not api_key:
                raise TTSFailure("engine", f"DashScope API key not configured ({DASHSCOPE_API_KEY_ENV})")
            # The ★ prefix of cloned voices may or may not be stripped before we get here.
            voices = self.voices_fallback
            voice_id = voices.get(voice) or voices.get(f"{CLONED_VOICE_PREFIX}{voice}")
            if voice_id is None:
                raise TTSFailure("engine", f"{self.label_short}: unknown voice {voice!r}")
            model = self.model_plus if voice_id in _PLUS_VOICE_IDS else self.model_flash
            language_type = self.language_map_dashscope.get(language, "Auto")
            logger.info(
                "%s TTS: voice=%s, id=%s, model=%s, lang=%s, text_length=%d",
                self.label_short, voice, voice_id, model, language_type, len(text),
            )

            reply = requests.post(
                f"{self.base_url}/services/audio/tts/SpeechSynthesizer",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "input": {"text": text, "voice": voice_id, "language_type": language_type},
                    "parameters": {"format": "wav", "sample_rate": self.sample_rate_hz},
                },
                timeout=self.request_timeout_s,
            )
            if not reply.ok:
                reason: FailureReason = "cloud_quota" if reply.status_code == HTTPStatus.TOO_MANY_REQUESTS else "engine"
                raise TTSFailure(reason, f"{self.label_short}: HTTP {reply.status_code}: {reply.text[:200]}")
            audio_url = reply.json()["output"]["audio"]["url"]
            # The result URL is signed and handed out as http://; the same file is served over https.
            audio = requests.get(audio_url.replace("http://", "https://", 1), timeout=self.request_timeout_s)
            audio.raise_for_status()
            return self._with_gain(audio.content)
        except Exception as e:  # noqa: BLE001 — _failure sorts it into a TTSFailure
            raise self._failure(e) from e

    def _with_gain(self, wav_bytes: bytes) -> bytes:
        """One WAV file from DashScope → the same WAV with the gain applied."""
        with wave.open(io.BytesIO(wav_bytes)) as wav_stream:
            sample_rate = wav_stream.getframerate()
            pcm_data = wav_stream.readframes(wav_stream.getnframes())
        pcm_data = _apply_pcm_gain(pcm_data, self.output_gain)

        output = io.BytesIO()
        with wave.open(output, "wb") as wav_out:
            wav_out.setnchannels(1)
            wav_out.setsampwidth(2)
            wav_out.setframerate(sample_rate)
            wav_out.writeframes(pcm_data)
        audio = output.getvalue()
        if not validate_audio(audio):
            raise TTSFailure("engine", f"{self.label_short} returned no usable audio")
        logger.info(
            "%s TTS: audio received (%d bytes, %.1f s)", self.label_short, len(audio),
            len(pcm_data) / (sample_rate * 2),
        )
        return audio

    def _failure(self, error: Exception) -> TTSFailure:
        """A TTSFailure from whatever went wrong: a network/internet outage is "unreachable", a
        request that takes too long is a "timeout"; DashScope answering with an error (auth, bad
        voice) is an engine failure."""
        if isinstance(error, TTSFailure):
            return error
        if isinstance(error, requests.RequestException):
            return request_failure(self.label_short, error)
        if isinstance(error, TimeoutError):
            return TTSFailure("timeout", f"{self.label_short}: {type(error).__name__}: {error}")
        if isinstance(error, _SOCKET_ERROR_TYPES):
            return TTSFailure("unreachable", f"{self.label_short}: {type(error).__name__}: {error}")
        return TTSFailure("engine", f"{self.label_short}: {type(error).__name__}: {error}")
