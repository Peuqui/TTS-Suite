"""Abstract base class for TTS engines."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from concurrent.futures import Executor
from functools import partial
from pathlib import Path
from typing import Any, Iterator, Optional, Self, get_args

from ..api import FailureReason, SuiteError, Unit
from ..constants import (
    AUDIO_EXTENSIONS,
    AUDIO_MIN_BYTES,
    COMPOSE_FILE_NAME,
    DOCKER_INSPECT_TIMEOUT_S,
    DOCKER_TTS_DIR,
    ENGINE_QUEUE_DEFAULT,
    MEDIA_TYPE_WAV,
    TTS_CONNECT_TIMEOUT_S,
    TTS_HEALTH_TIMEOUT_S,
    TTS_READ_TIMEOUT_S,
    TTS_READY_LOG_INTERVAL_S,
    TTS_READY_POLL_INTERVAL_S,
    TTS_READY_POLL_TIMEOUT_S,
    TTS_READY_STATE_CHECK_INTERVAL_S,
    TTS_VOICES_TIMEOUT_S,
)
from ..docker_ctl import ContainerStateUnreadable, container_log_tail, container_state, docker_compose_action

logger = logging.getLogger(__name__)

#: Units of speech: how much text goes to the engine at once. ``sentence`` = streaming sentence by
#: sentence (earliest first speech), ``paragraph`` = paragraph by paragraph (often better stress),
#: ``whole`` = everything at once (best quality, the first speech comes after the whole generation).
SPEECH_UNITS: tuple[str, ...] = get_args(Unit)

LOCALHOST = "localhost"


class TTSFailure(SuiteError):
    """An engine delivered no speech. Every engine raises this instead of returning
    ``None`` — the request's list decides who goes on."""

    def __init__(self, reason: FailureReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: FailureReason = reason
        self.detail = detail


def shared_voice_names(voices_dir: Path) -> list[str]:
    """The cloned voices every container engine reads from the shared tree
    (``<Name>/<Name>.wav`` below ``voices_dir``) — the catalog while no
    container answers, so a new voice shows up without one running."""
    return sorted(wav.stem for wav in voices_dir.glob("*/*.wav") if wav.stem == wav.parent.name)


def validate_audio(audio: bytes) -> bool:
    """Whether audio bytes are usable: not suspiciously small."""
    return len(audio) >= AUDIO_MIN_BYTES


def request_failure(source: str, error: Exception) -> TTSFailure:
    """The :class:`TTSFailure` for an error of a ``requests`` call to ``source``: no connection
    (also a connect timeout) is ``unreachable``, a read timeout is ``timeout``, anything else of
    ``requests`` (the answer cut off or undecodable, a container that died while answering) is
    an ``engine`` failure. These are no software errors of the suite."""
    import requests

    if isinstance(error, requests.ConnectionError):  # ConnectTimeout is one as well
        return TTSFailure("unreachable", f"{source}: {error}")
    if isinstance(error, requests.Timeout):
        return TTSFailure("timeout", f"{source} did not answer in time: {error}")
    return TTSFailure("engine", f"{source}: {type(error).__name__}: {error}")


class TTSEngine(ABC):
    """One TTS backend, all in one place.

    Subclasses declare identity + capabilities as class-level attributes,
    and override the methods that actually differ per backend (mostly
    voice discovery + speech generation). Defaults are tuned for the
    lightweight non-container case (Edge / Piper / eSpeak) so that
    container-based engines only need to override what's actually
    different.

    NEVER instantiate directly — only via :data:`registry.TTS_ENGINES`.
    """

    # ── Identity ───────────────────────────────────────────────────
    #: Stable engine key, used in the configuration and in engine lists
    #: (e.g. "qwen3local", "xtts", "edge"). Equals the folder name.
    key: str

    #: Short label for compact UI surfaces (channel dropdowns, status messages).
    #: Long, descriptive labels live in the engine's ``i18n.json``.
    label_short: str

    # ── Capabilities (defaults = lightweight engine) ───────────────
    #: True for engines that run as a Docker container we own
    #: (qwen3local / xtts). False for cloud/CLI engines.
    runs_in_container: bool = False

    #: True if the engine occupies GPU VRAM. Lightweight engines
    #: (Edge / Piper / eSpeak / DashScope) → False.
    needs_gpu: bool = False

    #: VRAM budget in MiB a GPU engine starts with — the peak of its stress burn-in.
    #: ``None`` = no secured value; whoever ran the burn-in sets it in the configuration
    #: (``EngineDefaults.vram_budget_mib``).
    default_vram_budget_mib: Optional[int] = None

    #: Whether this engine ignores the speed parameter on its own and
    #: requires ffmpeg post-processing to rate-shift the audio.
    needs_speed_postprocess: bool = False

    #: True if the engine actually honours the ``language`` argument of
    #: :meth:`generate_speech`. Engines that auto-detect the language
    #: from the text or encode it in the voice id itself
    #: (Edge / Piper / eSpeak) leave this False — a user interface can then
    #: grey out the language choice. Speed and pitch always apply (ffmpeg
    #: post-processing), so no equivalent flag exists for those.
    supports_language: bool = False

    #: Media type of the audio :meth:`generate_speech` returns (a key of ``AUDIO_EXTENSIONS``).
    audio_media_type: str = MEDIA_TYPE_WAV

    #: Default unit of this engine (see ``SPEECH_UNITS``); the request's entry
    #: carries the unit actually used. Fast engines stream sentence by sentence, slow local
    #: ones render better as a whole.
    default_speech_unit: Unit = "sentence"

    #: Sort key for engine lists — lower numbers come first.
    #: Convention: 10 = most-recommended GPU container, 90 = browser. New engines just pick a free integer.
    display_order: int = 100

    #: Local Docker image name (without tag). None for lightweight
    #: engines. Used by :meth:`is_installed` to check provisioning —
    #: the image is the source of truth, the compose file is the recipe.
    image_name: Optional[str] = None

    #: Subdirectory under ``docker/tts/`` containing the build recipe.
    #: Default = engine key; override only when the dir name differs
    #: (Qwen3 key="qwen3local" but dir="qwen3-tts").
    compose_subdir: Optional[str] = None

    #: How many syntheses this engine (per host) may run at once. Measured on
    #: a 3090 Ti: XTTS serves two in parallel without loss, Qwen3-TTS slows
    #: down about five-fold (2× ~14 s instead of 2× ~2.7 s) — it gets 1.
    max_parallel_requests: int = 2

    #: How many requests may wait for a free slot beyond ``max_parallel_requests``
    #: (per host); more are refused with ``Overloaded``. ``EngineDefaults.max_queue`` overrides.
    max_queued_requests: int = ENGINE_QUEUE_DEFAULT

    #: Seconds a container engine gets to come up and load its model —
    #: locally and on a TTS host alike. 0 for engines without a container.
    startup_timeout_s: int = 0

    #: True only for :class:`DeactivatedEngine`, the placeholder of an entry whose
    #: engine folder was moved to ``engines/disabled/``.
    deactivated: bool = False

    #: True for engines that run in someone else's cloud (DashScope, Edge) —
    #: the text leaves the house. Shown as a label in the catalog.
    cloud: bool = False

    #: Port of the engine's REST API — set by engines that run as an HTTP
    #: service (the containers). None = no REST API (cloud/CLI engines);
    #: such engines can only run on this machine or in the cloud.
    default_port: Optional[int] = None

    #: True for the entry that stands for the client's own voice (Web Speech API in a
    #: browser): the suite lists it but never speaks with it.
    renders_in_browser: bool = False

    #: Voice used when neither the request nor the configuration names one
    #: (None = such a request cannot be spoken on this engine).
    default_voice: Optional[str] = None

    def __init__(
        self,
        address: str = LOCALHOST,
        port: Optional[int] = None,
        gpu_uuid: str = "",
        voices_dir: Optional[Path] = None,
        vram_budget_mib: Optional[int] = None,
    ) -> None:
        """``address``/``port`` of the REST API. The registry instances run on
        this machine (``localhost``); :meth:`at_host` binds the same engine to
        a remote TTS host (escalation entries). ``gpu_uuid``: card a local
        container starts on ("" = the card named in the compose file's ``.env``).
        ``voices_dir``: the shared voices tree; :meth:`with_voices_dir` binds it.
        ``vram_budget_mib``: the limit a local container is started with (:meth:`on_gpu`)."""
        self.address = address
        self.port = port or self.default_port
        self.gpu_uuid = gpu_uuid
        self.voices_dir = voices_dir
        self.vram_budget_mib = vram_budget_mib

    def _bound(self, **changes: Any) -> Self:
        """The same engine with some of its bindings replaced."""
        bindings: dict[str, Any] = {
            "address": self.address, "port": self.port, "gpu_uuid": self.gpu_uuid, "voices_dir": self.voices_dir,
            "vram_budget_mib": self.vram_budget_mib,
        }
        return type(self)(**(bindings | changes))

    def on_gpu(self, gpu_uuid: str, vram_budget_mib: Optional[int] = None) -> Self:
        """The same local engine, started on another card with a VRAM limit (``None`` = none)."""
        self._require_local("place")
        return self._bound(gpu_uuid=gpu_uuid, vram_budget_mib=vram_budget_mib)

    def at_host(self, address: str, port: Optional[int] = None) -> Self:
        """The same engine, bound to a remote host. Raises ``ValueError`` for
        engines without a REST API — they cannot run on another machine."""
        if self.default_port is None:
            raise ValueError(f"TTS engine {self.key!r} has no REST API and cannot run on a remote host")
        return self._bound(address=address, port=port)

    def with_voices_dir(self, voices_dir: Path) -> Self:
        """The same engine reading the shared voices from ``voices_dir``."""
        return self._bound(voices_dir=voices_dir)

    @property
    def shared_voices_dir(self) -> Path:
        """The shared voices tree — the engine must have been bound to it."""
        if self.voices_dir is None:
            raise RuntimeError(f"TTS engine {self.key!r} is not bound to a voices directory (with_voices_dir)")
        return self.voices_dir

    @property
    def is_remote(self) -> bool:
        """True for an instance bound to another machine. Remote engines are
        never started or stopped from here — their host runs them."""
        return self.address != LOCALHOST

    # ── Own folder + label ─────────────────────────────────────────
    @property
    def package_dir(self) -> Path:
        """Folder of the engine (``engines/<key>/``): its code, ``i18n.json``
        and whatever else belongs to it."""
        return Path(sys.modules[type(self).__module__].__file__ or "").parent

    def label(self, lang: str) -> str:
        """Long, translated name for engine lists — from the engine's own
        ``i18n.json`` (``{"de": ..., "en": ...}``)."""
        labels: dict[str, str] = json.loads((self.package_dir / "i18n.json").read_text(encoding="utf-8"))
        return labels[lang]

    # ── Locations ──────────────────────────────────────────────────
    @property
    def service_url(self) -> Optional[str]:
        """HTTP base URL of the engine's REST API. None for engines that
        don't run as an HTTP service."""
        if self.port is None:
            return None
        return f"http://{self.address}:{self.port}"

    @property
    def docker_compose_path(self) -> Optional[Path]:
        """docker-compose.yml path. The compose file is a *build recipe*
        — present in the repo, used to (re)build the image. NOT used to
        decide whether the engine is installed (the image is).

        Default: ``docker/tts/<compose_subdir or key>/docker-compose.yml``
        — engines only have to override ``compose_subdir`` if the directory
        name differs from the engine key.
        """
        if not self.runs_in_container:
            return None
        return DOCKER_TTS_DIR / self.service_dir / COMPOSE_FILE_NAME

    @property
    def service_dir(self) -> str:
        """Directory under ``docker/tts/`` — also the service name a TTS host's
        control script takes and the ``container_name`` in that directory's
        docker-compose.yml."""
        return self.compose_subdir or self.key

    @property
    def voices_fallback(self) -> dict[str, str]:
        """Static voice list used when the engine is unreachable. Same
        shape as :meth:`get_voices`. Override per engine."""
        return {}

    # ── Voices ─────────────────────────────────────────────────────
    @abstractmethod
    def get_voices(self) -> dict[str, str]:
        """Return ``{voice_name: voice_id}`` for the engine's *currently
        live* voice set.

        For container engines: hit the container's /voices endpoint.
        For static engines: return the bundled voice map.
        Return ``{}`` if the engine isn't reachable — the caller falls
        back to :attr:`voices_fallback` in that case.
        """

    def _fetch_voices_json(self) -> Optional[dict[str, Any]]:
        """GET ``<service_url>/voices`` of a container engine. A stopped
        container (connection refused) is the normal case and stays
        silent; only a container that answers wrongly is worth a warning.
        Returns ``None`` on any failure."""
        import requests
        try:
            response = requests.get(f"{self.service_url}/voices", timeout=TTS_VOICES_TIMEOUT_S)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data
        except requests.ConnectionError:
            return None
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Failed to fetch %s voices: %s", self.label_short, exc)
            return None

    # ── Language mapping (default: ISO codes pass through) ─────────
    @property
    def language_map(self) -> dict[str, str]:
        """Mapping from the short language code (``"de"``, ``"en"``,
        ``"zh"``, …) to the engine-specific language tag. Empty dict
        means "engine takes the ISO code as-is"."""
        return {}

    # ── Lifecycle ──────────────────────────────────────────────────
    def is_installed(self) -> bool:
        """True if the engine is *provisioned* on this host — i.e. ready
        to be started.

        For container engines (``image_name`` set): asks Docker whether
        the image is locally available. The image is the artefact that
        can actually run; the compose file is just a recipe to rebuild
        it. So deleting the image makes the engine "not installed"
        even if the compose file is still in the repo.

        For lightweight engines (Edge / Piper / eSpeak / cloud — no
        ``image_name``): always True, they ship with the suite.
        """
        if not self.image_name:
            return True
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self.image_name],
                capture_output=True,
                timeout=DOCKER_INSPECT_TIMEOUT_S,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def is_running(self) -> bool:
        """True if the engine can accept requests *right now*. Default
        ``True`` for lightweight engines (always-on); container engines
        answer through their health endpoint."""
        if not self.runs_in_container:
            return True
        health = self._health()
        return health is not None and self._model_ready(health)

    def _health(self, timeout: float | None = None) -> dict[str, Any] | None:
        """JSON of the engine's health endpoint; ``{}`` when it answers 2xx
        without JSON; ``None`` when it is not reachable or answers an error."""
        import requests
        try:
            response = requests.get(
                f"{self.service_url}/health", timeout=timeout or TTS_HEALTH_TIMEOUT_S,
            )
            if not response.ok:
                return None
            if not response.headers.get("content-type", "").startswith("application/json"):
                return {}
            data: dict[str, Any] = response.json()
            return data
        except (OSError, ValueError):
            return None

    def _model_ready(self, health: dict[str, Any]) -> bool:
        """Whether a health answer means "model loaded, can synthesise"."""
        return bool(health.get("model_loaded"))

    def _device(self, health: dict[str, Any]) -> str:
        """Compute target a health answer reports ("cuda:0", "cpu")."""
        return str(health.get("device", "unknown"))

    def start(self) -> tuple[bool, str]:
        """Bring the engine up. Returns ``(success, message)``. Only for
        engines on this machine — a remote host runs its engines itself."""
        self._require_local("start")
        return self._start_local()

    def stop(self) -> tuple[bool, str]:
        """Take the engine down. Returns ``(success, message)``."""
        self._require_local("stop")
        return self._stop_local()

    def ensure_ready(self, timeout: int | None = None) -> tuple[bool, str, str]:
        """Ensure the engine is up and serving. Returns
        ``(success, status_message, device)``. ``device`` is the engine's
        compute target ("cuda:0", "cpu", "") — empty for engines that
        don't expose one. A remote engine is not started, only checked."""
        if self.is_remote:
            if self.is_running():
                return True, f"{self.label_short} ready on {self.address}", ""
            return False, f"{self.label_short} not serving on {self.address}", ""
        return self._ensure_ready_local(timeout)

    def _require_local(self, action: str) -> None:
        if self.is_remote:
            raise RuntimeError(
                f"cannot {action} {self.key!r} on remote host {self.address} — the host runs it"
            )

    # Lifecycle for engines on this machine. Container engines run their compose
    # file; the other engines have nothing to start.
    def _start_local(self) -> tuple[bool, str]:
        if self.docker_compose_path is None:
            return True, "no-op"
        return docker_compose_action(
            self.docker_compose_path, "up", self.label_short,
            voices_dir=self.shared_voices_dir, gpu_uuid=self.gpu_uuid, vram_budget_mib=self.vram_budget_mib,
        )

    def _stop_local(self) -> tuple[bool, str]:
        if self.docker_compose_path is None:
            return True, "no-op"
        return docker_compose_action(
            self.docker_compose_path, "down", self.label_short, voices_dir=self.shared_voices_dir,
        )

    def _ensure_ready_local(self, timeout: int | None) -> tuple[bool, str, str]:
        """Start the container if needed and wait until its model is loaded."""
        if not self.runs_in_container:
            return True, "ready", ""

        health = self._health(timeout=TTS_READY_POLL_TIMEOUT_S)
        if health is not None and self._model_ready(health):
            device = self._device(health)
            return True, f"{self.label_short} already ready ({device})", device

        success, message = self._start_local()
        if not success:
            return False, message, ""

        limit = timeout or self.startup_timeout_s
        logger.info("%s: waiting for model to load...", self.label_short)
        for waited in range(limit):
            health = self._health(timeout=TTS_READY_POLL_TIMEOUT_S)
            if health is not None and self._model_ready(health):
                device = self._device(health)
                logger.info("%s: model loaded on %s", self.label_short, device)
                return True, f"{self.label_short} ready ({device})", device
            if waited > 0 and waited % TTS_READY_STATE_CHECK_INTERVAL_S == 0:
                ended = self._container_ended_message()
                if ended is not None:
                    return False, ended, ""
            if waited > 0 and waited % TTS_READY_LOG_INTERVAL_S == 0:
                logger.info("%s: still waiting (%ds / %ds)...", self.label_short, waited, limit)
            time.sleep(TTS_READY_POLL_INTERVAL_S)
        return False, f"{self.label_short}: Timeout after {limit}s waiting for model", ""

    def _container_ended_message(self) -> str | None:
        """Why waiting for the model is pointless: the container ended (a model that fails to load ends
        it) or Docker cannot tell. ``None`` while the container is there (``created``, ``running``,
        ``restarting``)."""
        try:
            state = container_state(self.service_dir)
        except ContainerStateUnreadable as exc:
            return f"{self.label_short}: cannot tell whether the container is alive: {exc}"
        if state not in ("exited", "absent"):
            return None
        message = f"{self.label_short}: container exited while the model loaded (state: {state})"
        log_tail = container_log_tail(self.service_dir) if state == "exited" else ""
        return f"{message}: {log_tail}" if log_tail else message

    # ── Speech generation ──────────────────────────────────────────
    def generate_speech(
        self,
        text: str,
        voice: str,
        language: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> bytes:
        """Synthesise ``text`` with ``voice`` into audio bytes of :attr:`audio_media_type`.
        Raises :class:`TTSFailure` when no audio comes back.

        ``language`` is the ISO short code (``"de"`` / ``"en"`` / …).
        The engine maps it through :attr:`language_map` if needed.

        ``speed`` / ``pitch`` semantics: engines with
        ``needs_speed_postprocess=False`` (Piper / eSpeak / Edge) apply
        the values natively here. Engines with ``True`` ignore them and
        rely on the post-processor (:mod:`tts_suite.postprocess`).

        Default: ``NotImplementedError`` — engines must override.
        """
        raise NotImplementedError(
            f"{self.key} engine has no sync generate_speech; engines with "
            f"native async IO should override generate_speech_async instead."
        )

    async def generate_speech_async(
        self,
        text: str,
        voice: str,
        language: str,
        speed: float = 1.0,
        pitch: float = 1.0,
        *,
        executor: Executor,
    ) -> bytes:
        """Async wrapper around :meth:`generate_speech`. The default
        offloads the sync method to a thread of ``executor`` (the speaker's own pool for syntheses)
        so blocking HTTP / subprocess calls don't stall the event loop and don't take threads
        from the default executor. Engines with native async IO (Edge) override this, skip the
        sync method and ignore ``executor``. Cancelling the coroutine does not stop a running thread.
        """
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(
            executor, partial(self.generate_speech, text, voice, language, speed, pitch),
        )

    def _synthesize_via_http(self, path: str, payload: dict[str, Any]) -> bytes:
        """POST ``payload`` to ``<service_url><path>`` and return the audio of the answer.
        Shared by all container engines (local or on a remote host).
        Raises :class:`TTSFailure` otherwise."""
        import requests

        logger.info(
            "%s @ %s: speaker=%s, language=%s, text_length=%d",
            self.label_short, self.address, payload.get("speaker") or payload.get("reference_id"),
            payload.get("language", "-"), len(str(payload.get("text", ""))),
        )
        try:
            response = requests.post(
                f"{self.service_url}{path}", json=payload,
                timeout=(TTS_CONNECT_TIMEOUT_S, TTS_READ_TIMEOUT_S),
            )
        except requests.RequestException as exc:
            raise request_failure(f"{self.label_short} at {self.service_url}", exc) from exc
        if response.status_code != 200:
            raise TTSFailure("engine", f"{self.label_short} HTTP {response.status_code}: {response.text[:200]}")
        if not validate_audio(response.content):
            raise TTSFailure("engine", f"{self.label_short} returned no usable audio")
        logger.info("%s: audio received (%d bytes)", self.label_short, len(response.content))
        return response.content

    @property
    def audio_extension(self) -> str:
        """File extension of :attr:`audio_media_type`."""
        return AUDIO_EXTENSIONS[self.audio_media_type]

    def prepare_voices(self) -> Iterator[str]:
        """Make the engine's voices ready before it speaks (idempotent), yielding
        one progress line per step. Default: nothing to prepare — the DashScope
        engine registers its cloned voices with the cloud here."""
        return iter(())

    # ── Misc ───────────────────────────────────────────────────────
    def __repr__(self) -> str:  # pragma: no cover — debugging convenience
        return f"<TTSEngine {self.key!r}>"


class DeactivatedEngine(TTSEngine):
    """Placeholder for a request's entry whose engine folder lies in
    ``engines/disabled/``: the entry is skipped with ``deactivated``. Never registered."""

    deactivated = True

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        self.label_short = key

    def get_voices(self) -> dict[str, str]:
        return {}

    def is_running(self) -> bool:
        return False

    def is_installed(self) -> bool:
        return False
