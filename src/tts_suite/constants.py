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

#: ``docker compose up -d`` / ``down``: a hanging Docker daemon must not block the start lock for good.
DOCKER_COMPOSE_TIMEOUT_S = 60
#: A reader that waits for a start gives up this long after the engine's own startup time (the
#: compose call and the queue for the one-start-at-a-time lock come on top of it).
START_WAIT_RESERVE_S = 90

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

# ── Speaking (escalation, text cutting, stream) ─────────────────────

#: A segment that takes longer than this many seconds is followed by a keep-alive piece of silence
#: until it is there (the client's connection may time out on a quiet stream). Source: ``FREEECHO2_KEEPALIVE_SEC``.
KEEPALIVE_SEC = 10.0
#: Length of one keep-alive piece. Source: ``KEEPALIVE_SILENCE_MS``.
KEEPALIVE_SILENCE_MS = 20

#: How long the state of a run (failed entries, voice-change announcement) is kept after its last use.
RUN_TTL_S = 600

#: Requests that may wait for a free slot of an engine, beyond ``max_parallel_requests``, before ``Overloaded``.
ENGINE_QUEUE_DEFAULT = 8

#: Speed and pitch when neither the request nor the configuration names one (1.0 = unchanged).
SPEED_DEFAULT = 1.0
PITCH_DEFAULT = 1.0

#: Sentences with fewer words are joined with the next one (the engines hallucinate on very short sentences).
#: Source: ``TTS_MIN_SENTENCE_WORDS``.
TTS_MIN_SENTENCE_WORDS = 3

#: How many sentences of a reading (unit "sentence") are released as soon as they are complete; every
#: later sentence waits until the NEXT one is complete or the text ends (safer sentence ends, and a
#: very short closing sentence can still be joined to its predecessor). The first sentence decides the
#: time to the first sound, so it goes at once. Fallback steps if that proves too weak (too many wrongly
#: cut sentences, too short pieces sounding bad), see docs/DESIGN-DECISIONS.de.md: 2 = the second sentence
#: goes at once as well (less look-ahead); 0 = even the first waits for the second (safest detection, costs
#: one sentence's generation time before the first sound: ~2.5-3.5 s at 10 tokens/s, ~0.5 s at 50).
SENTENCES_RELEASED_IMMEDIATELY = 1

#: A sentence end is only final when this many characters follow it: the end detection looks ahead
#: (URL context 10 characters, closing quotes), so a cutter that sees a text in pieces must wait for them.
SENTENCE_LOOKAHEAD_CHARS = 10

#: Characters that close a quotation or parenthesis right after a sentence end and belong to that sentence.
SENTENCE_CLOSERS = "\"')»"

#: Words that end with a period but not a sentence (lowercase). Source: ``ABBREVIATIONS_DE`` / ``_EN``.
ABBREVIATIONS_DE = frozenset({
    "z.b.", "z. b.", "d.h.", "d. h.", "u.a.", "u. a.", "o.ä.", "o. ä.",
    "bzw.", "ca.", "etc.", "evtl.", "ggf.", "inkl.", "max.", "min.",
    "nr.", "s.", "str.", "tel.", "usw.", "vgl.", "vs.", "z.t.",
    "dr.", "prof.", "hr.", "fr.", "ing.", "dipl.",  # titles
    "jan.", "feb.", "mär.", "apr.", "jun.", "jul.", "aug.", "sep.", "okt.", "nov.", "dez.",  # months
    "mo.", "di.", "mi.", "do.", "sa.", "so.",  # days ("fr." is among the titles)
})
ABBREVIATIONS_EN = frozenset({
    "e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "ms.", "dr.", "prof.",
    "inc.", "ltd.", "corp.", "co.", "jr.", "sr.",
    "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "oct.", "nov.", "dec.",
    "mon.", "tue.", "wed.", "thu.", "fri.", "sat.", "sun.",
    "no.", "vol.", "ch.", "pg.", "pp.", "fig.", "approx.", "dept.",
})
ABBREVIATIONS = ABBREVIATIONS_DE | ABBREVIATIONS_EN

#: Texts the suite speaks itself (voice-change announcements), German and English, next to the modules.
I18N_FILE_NAME = "i18n.json"

# ── Text cleaning ───────────────────────────────────────────────────

#: A single unit longer than this (or with a line break) counts as a whole text, not as one item of a
#: run of sentences. Source: AIfred's ``clean_text_for_tts``.
MULTILINE_TEXT_MIN_CHARS = 500

#: Lists with more items than this are replaced by a spoken hint. Source: AIfred's ``tts_list.full_max_items``.
TTS_LIST_MAX_ITEMS = 5

#: Emojis the cleaning keeps: the engine normalization in the container turns them into laughter.
LAUGHTER_EMOJIS = "😂🤣😆😄😅😁🙂😊"

# ── Containers: control from the suite (SSH, cards, VRAM) ───────────

#: Languages the suite names its engines in (the keys of ``EngineInfo.label``); every engine's
#: ``i18n.json`` carries them.
LABEL_LANGUAGES = ("de", "en")

#: SSH key the suite uses to start and stop containers on another host. The key is pinned on that
#: host to ``scripts/tts-host-ctl.sh`` (``command=`` in ``authorized_keys``), so nothing else can run.
#: The environment variable names another path; without it the default applies.
#: Source: AIfred ``TTS_HOST_SSH_KEY`` (there ``~/.ssh/aifred_tts_host``).
TTS_HOST_SSH_KEY_ENV = "TTS_SUITE_SSH_KEY"
TTS_HOST_SSH_KEY_DEFAULT = Path.home() / ".ssh" / "tts_suite_host"
#: Connecting to the host, and one control command (``start`` returns once ``docker compose up -d`` is done).
#: Source: ``TTS_HOST_SSH_CONNECT_TIMEOUT_S`` / ``TTS_HOST_SSH_COMMAND_TIMEOUT_S``.
TTS_HOST_SSH_CONNECT_TIMEOUT_S = 3
TTS_HOST_SSH_COMMAND_TIMEOUT_S = 60

#: ``nvidia-smi`` queries: the card list, the processes using a card, and ``docker compose ps`` /
#: ``docker top`` (the host PIDs of a container).
NVIDIA_SMI_QUERY_TIMEOUT_S = 5
NVIDIA_SMI_APPS_TIMEOUT_S = 10
DOCKER_PIDS_TIMEOUT_S = 10
