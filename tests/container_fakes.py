"""Fakes for the container tests: engines that run in a container, cards, SSH and VRAM readings
that need no Docker, ``nvidia-smi``, network or GPU."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from speech_fakes import FakeEngine, FakeLauncher

from tts_suite.api import EngineDefaults, EntryStatus, Host, SuiteConfig
from tts_suite.engines import registry
from tts_suite.engines.base import Reachability
from tts_suite.escalation import EscalationEntry, Skip
from tts_suite.host_ctl import HostControlError
from tts_suite.placement import GpuInfo

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
        self.installed = installed
        self.ready_delay_s = ready_delay_s
        self.ready_ok = ready_ok
        self.placements: list[tuple[str, int | None]] = []
        self.stops = 0
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

    def ensure_ready(self, timeout: int | None = None) -> tuple[bool, str, str]:
        self.ready_calls += 1
        self.started_at = time.monotonic()
        time.sleep(self.ready_delay_s)
        self.up = self.ready_ok
        self.finished_at = time.monotonic()
        return (True, "ready", "cuda:0") if self.ready_ok else (False, "model did not load", "")

    def stop(self) -> tuple[bool, str]:
        self.stops += 1
        self.up = False
        return True, "stopped"


class CardList:
    """What ``nvidia-smi`` lists. ``queries`` counts the calls, ``take`` makes a card fuller."""

    def __init__(self, *free_mib: int, total_mib: int = 24000) -> None:
        self.cards = [GpuInfo(index, f"GPU-{index}", free, total_mib, total_mib - free) for index, free in enumerate(free_mib)]
        self.queries = 0
        self._lock = threading.Lock()

    def __call__(self) -> list[GpuInfo]:
        with self._lock:
            self.queries += 1
            return list(self.cards)


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
    return SuiteConfig(voices_dir=tmp_path, engines=engines or {}, **kwargs)


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

    async def stop(self, entry: EscalationEntry) -> None:
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped.append(entry.label)

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
