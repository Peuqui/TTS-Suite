"""Named values of the TTS-Suite — the one place for numbers, timeouts and fixed names.

Values marked "source" carry the value AIfred Intelligence has used so far
(``aifred/lib/config.py``); the suite is their home from now on.
"""

from __future__ import annotations

from pathlib import Path

# ── Locations ───────────────────────────────────────────────────────

#: Root of the repository (``src/tts_suite/constants.py`` → two levels up). The Docker recipes
#: (``docker/tts/<service>/docker-compose.yml``) and the Piper models live below it.
SUITE_ROOT = Path(__file__).resolve().parents[2]

#: Build recipes of the container engines, one folder per service.
DOCKER_TTS_DIR = SUITE_ROOT / "docker" / "tts"
COMPOSE_FILE_NAME = "docker-compose.yml"

#: Piper voice models (``*.onnx``), a folder next to the recipes.
PIPER_MODELS_DIR = SUITE_ROOT / "piper_models"

# ── Timeouts and intervals (seconds) ────────────────────────────────

#: Synthesis calls to a TTS container (local or on another machine): connecting is short, so a
#: dead server counts as a failure at once and the escalation goes on; reading is generous, because
#: whole paragraphs take minutes on slow cards. Source: ``TTS_CONNECT_TIMEOUT_S`` / ``TTS_READ_TIMEOUT_S``.
TTS_CONNECT_TIMEOUT_S = 3
TTS_READ_TIMEOUT_S = 600

#: Health query of a container; the escalation asks every entry with it before it speaks.
#: Source: ``TTS_HEALTH_TIMEOUT_S``.
TTS_HEALTH_TIMEOUT_S = 0.5

#: Heartbeat of ``/keep_alive`` against the container's idle timer, and the timeout of one such ping
#: (the endpoint only resets a timer). Source: ``TTS_KEEPALIVE_INTERVAL_SECONDS`` / ``TTS_KEEPALIVE_HTTP_TIMEOUT``.
TTS_KEEPALIVE_INTERVAL_SECONDS = 300
TTS_KEEPALIVE_HTTP_TIMEOUT = 5

#: Timeout of the ``/voices`` query of a container.
TTS_VOICES_TIMEOUT_S = 5

#: Waiting for a started container: health timeout per poll, pause between polls, and the
#: interval of the "still waiting" log line.
TTS_READY_POLL_TIMEOUT_S = 2
TTS_READY_POLL_INTERVAL_S = 1
TTS_READY_LOG_INTERVAL_S = 30

#: ``docker image inspect`` (is the engine's image built?) and the eSpeak voice listing.
DOCKER_INSPECT_TIMEOUT_S = 5
ESPEAK_VOICE_LIST_TIMEOUT_S = 5

# ── Audio ───────────────────────────────────────────────────────────

#: Media type → file extension. The media type an engine delivers is one of these keys.
AUDIO_EXTENSIONS: dict[str, str] = {
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
}
MEDIA_TYPE_WAV = "audio/wav"
MEDIA_TYPE_OGG = "audio/ogg"
MEDIA_TYPE_MP3 = "audio/mpeg"

#: Audio shorter than this many bytes is no usable audio (an empty or cut-off answer).
AUDIO_MIN_BYTES = 100

#: Post-processing (ffmpeg): speed and pitch factors closer to 1.0 than this count as unchanged.
POSTPROCESS_TOLERANCE = 0.01
#: ``atempo`` takes factors from 0.5 to 2.0 per filter; more extreme speeds chain several.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0
#: Sample rate assumed when ``ffprobe`` cannot tell (XTTS and Qwen3 deliver 24 kHz).
DEFAULT_SAMPLE_RATE_HZ = 24000
FFPROBE_TIMEOUT_S = 5

# ── Voices ──────────────────────────────────────────────────────────

#: Prefix that marks cloned voices in voice lists. Stripped before an engine is called.
CLONED_VOICE_PREFIX = "★ "

# ── Engines ─────────────────────────────────────────────────────────

#: eSpeak words per minute at speed 1.0 (range 80-500).
ESPEAK_BASE_WPM = 175

#: Environment variable that holds the DashScope API key (cloud keys are no part of the config).
DASHSCOPE_API_KEY_ENV = "DASHSCOPE_API_KEY"
#: Mapping of enrolled cloned voices, kept in the shared voices directory.
DASHSCOPE_MAPPING_FILE_NAME = "dashscope_audio3_voices.json"
#: Voice enrollment uploads a reference WAV; generous timeout.
DASHSCOPE_ENROLL_TIMEOUT_S = 90
