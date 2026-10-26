"""Fakes for the container tests: engines that run in a container, cards, SSH and VRAM readings
that need no Docker, ``nvidia-smi``, network or GPU."""
from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from speech_fakes import FakeEngine, FakeLauncher

from tts_suite import placement
from tts_suite.api import EngineDefaults, EntryStatus, Host, SuiteConfig
from tts_suite.docker_ctl import ContainerState
from tts_suite.engines import registry
from tts_suite.engines.base import Reachability
from tts_suite.escalation import EscalationEntry, Skip
from tts_suite.host_ctl import HostControlError
from tts_suite.launcher import Busy
from tts_suite.placement import GpuInfo, PlacementRefused, PlaceRequest

ARAGON = Host("Aragon", "192.0.2.2")


class ContainerEngine(FakeEngine):
    """A GPU engine in a container. ``up`` is whether its container answers; the calls the
    launcher makes are recorded."""

    runs_in_container = True
    needs_gpu = True
    startup_timeout_s = 5
    default_vram_budget_mib = 4000

    def __init__(
        self, key: str, *, remote: bool = False, installed: bool = True, ready_delay_s: float = 0.0,
        ready_ok: bool = True, **kwargs: Any,
    ) -> None:
        super().__init__(key, remote=remote, **kwargs)
        self.up = False
        self.loading = False  # its container is there, the model is not loaded yet (``ensure_ready`` waits)
        self.installed = installed
        self.ready_delay_s = ready_delay_s
        self.ready_ok = ready_ok
        self.placements: list[tuple[str, int | None]] = []
        self.stops = 0
        self.kills = 0
        self.survives_stop = False  # the stop leaves the container running (a stuck container)
        self.survives_kill = False  # not even docker kill takes it down
        self.gpus: tuple[str, ...] | str = ("GPU-0",)  # what its container's configuration names
        self.ready_calls = 0
        self.reach: Reachability | None = None  # what the host answers; None = follows ``up``
        self.started_at = self.finished_at = 0.0  # when ``ensure_ready`` ran (monotonic seconds)

    def is_running(self) -> bool:
        return self.up

    def reachability(self) -> Reachability:
        return self.reach or ("answering" if self.up else "refused")

    def is_installed(self) -> bool:
        return self.installed

    def on_gpu(self, gpu_uuid: str, vram_budget_mib: int | None = None) -> Any:
        self.placements.append((gpu_uuid, vram_budget_mib))
        return self

    def ensure_ready(self, withdrawn: threading.Event, timeout: int | None = None) -> tuple[bool, str, str]:
        self.ready_calls += 1
        self.started_at = time.monotonic()
        if withdrawn.is_set():
            return False, "start withdrawn", ""
        self.loading = True
        # Like the real wait: the model loads for ``ready_delay_s``, a withdrawal ends the wait at the next poll
        # and leaves the container there.
        if withdrawn.wait(self.ready_delay_s):
            self.finished_at = time.monotonic()
            return False, "start withdrawn", ""
        self.loading = False
        self.up = self.ready_ok
        self.finished_at = time.monotonic()
        return (True, "ready", "cuda:0") if self.ready_ok else (False, "model did not load", "")

    def stop(self) -> tuple[bool, str]:
        self.stops += 1
        if not self.survives_stop:
            self.up = self.loading = False
        return True, "stopped"

    def container_state(self) -> ContainerState:
        return "running" if self.up or self.loading else "absent"

    def kill_container(self) -> tuple[bool, str]:
        self.kills += 1
        if not self.survives_kill:
            self.up = self.loading = False
        return True, "killed"

    def container_gpus(self) -> Any:
        return self.gpus if self.up or self.loading else None


GPU_ORC = "http://gpu-orc.invalid"  # a reserved name: a test that forgets its fake reaches nothing real


class CardList:
    """What ``nvidia-smi`` lists: ``count`` cards GPU-0, GPU-1, … ``queries`` counts the calls."""

    def __init__(self, count: int = 1) -> None:
        self.cards = [GpuInfo(index, f"GPU-{index}") for index in range(count)]
        self.queries = 0
        self._lock = threading.Lock()

    def __call__(self) -> list[GpuInfo]:
        with self._lock:
            self.queries += 1
            return list(self.cards)


class FakePlace:
    """Replaces GPU-Orc's ``POST /place``: it names ``card`` (or the card the request names), or refuses with
    ``refusal``; ``requests`` records what was asked, with the address."""

    def __init__(self, card: int = 0, refusal: str | None = None) -> None:
        self.card = card
        self.refusal = refusal
        self.requests: list[tuple[str, PlaceRequest]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, request: PlaceRequest) -> GpuInfo:
        with self._lock:
            self.requests.append((url, request))
        if self.refusal is not None:
            raise PlacementRefused(self.refusal)
        index = self.card if request.card is None else int(request.card)
        return GpuInfo(index, f"GPU-{index}")


class FakeGpuOrc:
    """Replaces ``urlopen`` for GPU-Orc itself: ``GET /health`` says ``caller_timeout_s``, ``POST /place`` gives
    ``answer``; ``error`` is raised for the place instead. ``calls`` records (method, url, body, timeout)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any] | None = None,
                 error: OSError | None = None, caller_timeout_s: float = 210.0) -> None:
        self.answer = answer
        self.error = error
        self.caller_timeout_s = caller_timeout_s
        self.calls: list[tuple[str, str, Any, float]] = []
        monkeypatch.setattr(placement.urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        body = None if request.data is None else json.loads(request.data)  # type: ignore[arg-type]
        self.calls.append((request.get_method(), request.full_url, body, timeout))
        if request.full_url.endswith("/health"):
            return io.BytesIO(json.dumps({"status": "ok", "caller_timeout_s": self.caller_timeout_s}).encode())
        if self.error is not None:
            raise self.error
        return io.BytesIO(json.dumps(self.answer).encode())


class FakeSsh:
    """Replaces ``run_host_command``: records the commands; ``fail`` makes them fail; ``then`` is
    what happens on the remote machine when a command arrives."""

    def __init__(self, fail: str | None = None, then: Any = None) -> None:
        self.fail = fail
        self.then = then
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[bytes] = []

    def __call__(self, host: Host, *command: str, stdin: bytes | None = None) -> str:
        self.commands.append((host.name, *command))
        if stdin is not None:
            self.inputs.append(stdin)
        if self.fail:
            raise HostControlError(f"{host.name}: '{' '.join(command)}' failed: {self.fail}")
        if self.then:
            self.then(*command)
        return ""


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def local_entry(engine: FakeEngine) -> EscalationEntry:
    return EscalationEntry(engine, None, engine.default_speech_unit)


def remote_entry(engine: FakeEngine, host: Host = ARAGON) -> EscalationEntry:
    return EscalationEntry(engine, host, engine.default_speech_unit)


def install(monkeypatch: pytest.MonkeyPatch, *engines: FakeEngine) -> None:
    """Put fake engines into the registry for the test (the binding looks engines up there)."""
    for engine in engines:
        monkeypatch.setitem(registry.TTS_ENGINES, engine.key, engine)


def make_config(tmp_path: Path, engines: dict[str, EngineDefaults] | None = None, **kwargs: Any) -> SuiteConfig:
    return SuiteConfig(voices_dir=tmp_path, engines=engines or {}, **{"gpu_orc_url": GPU_ORC, **kwargs})


class FakeManagedLauncher(FakeLauncher):
    """A launcher for ``Suite`` tests: the escalation's questions from ``FakeLauncher``, plus the
    calls the suite makes itself. ``statuses``: label -> status (default ``running``)."""

    def __init__(self, down: Any = None, statuses: dict[str, EntryStatus] | None = None, status_delay_s: float = 0.0) -> None:
        super().__init__(down)
        self.statuses = statuses or {}
        self.status_delay_s = status_delay_s  # how long a status question takes (a slow or silent host)
        self.status_in_flight = 0
        self.status_peak = 0  # most status questions asked at the same time
        self.configs: list[SuiteConfig] = []
        self.cleared: list[str | None] = []  # labels whose failures were forgotten (None = all)
        self.stopped: list[str] = []
        self.started_on_gpu: list[tuple[str, str]] = []  # (label, card) of each start on a named card
        self.failures: dict[str, Skip] = {}  # label -> why its last start failed
        self.stop_error: Exception | None = None
        self.forced: list[str] = []  # labels stopped with force
        self.withdrawn: list[tuple[str, str]] = []  # (label, reason) of each start withdrawn
        self.busy_reasons: dict[str, Busy] = {}  # label -> why it must not be taken down now
        self.cards: dict[str, tuple[str, ...] | None] = {}  # label -> the cards of its running container (None = down)
        self.closed = False

    def set_config(self, config: SuiteConfig) -> None:
        self.configs.append(config)

    def clear_failures(self, entry: EscalationEntry | None) -> None:
        self.cleared.append(entry.label if entry is not None else None)
        if entry is None:
            self.failures.clear()
        else:
            self.failures.pop(entry.label, None)

    async def push_voices(self) -> list[str]:
        return []

    async def start_on_gpu(self, entry: EscalationEntry, gpu: str) -> Skip | None:
        self.started_on_gpu.append((entry.label, gpu))
        return None

    async def stop(self, entry: EscalationEntry, *, force: bool = False) -> None:
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped.append(entry.label)
        if force:
            self.forced.append(entry.label)
        self.cards[entry.label] = None

    async def withdraw_start(self, entry: EscalationEntry, reason: str) -> None:
        self.withdrawn.append((entry.label, reason))
        self.busy_reasons.pop(entry.label, None)

    def busy(self, entry: EscalationEntry) -> Busy | None:
        return self.busy_reasons.get(entry.label)

    async def gpu_cards(self, entry: EscalationEntry) -> tuple[str, ...] | None:
        return self.cards.get(entry.label)

    async def status(self, entry: EscalationEntry) -> EntryStatus:
        self.status_in_flight += 1
        self.status_peak = max(self.status_peak, self.status_in_flight)
        try:
            await asyncio.sleep(self.status_delay_s)
        finally:
            self.status_in_flight -= 1
        return self.statuses.get(entry.label, "running")

    def start_failure(self, entry: EscalationEntry) -> Skip | None:
        return self.failures.get(entry.label)

    async def aclose(self) -> None:
        self.closed = True
