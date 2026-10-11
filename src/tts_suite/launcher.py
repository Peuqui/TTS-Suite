"""The launcher: brings the containers of the escalation list up and down.

Ported from AIfred Intelligence (``tts_escalation``): a local GPU engine starts on the card the
rules choose (one local GPU start at a time), an engine on another host starts over SSH, an engine
without a container (cloud, Piper, eSpeak, Edge) is only checked. Starts run as one task per entry,
shared by everyone who waits for it and by a ``wait=False`` request that only begins it.

Every system call (``nvidia-smi``, Docker, SSH, HTTP) sits behind a function handed to the
constructor, so tests replace it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Protocol

from .api import (
    EngineControlFailed,
    EngineInUse,
    EngineStarting,
    EntryStatus,
    Host,
    SuiteConfig,
)
from .constants import START_WAIT_RESERVE_S, TTS_READY_POLL_INTERVAL_S
from .engines.base import TTSEngine, TTSFailure
from .escalation import EngineLauncher, EscalationEntry, Skip
from .holds import EngineHolds
from .host_ctl import HostCommand, HostControlError, run_host_command
from .placement import GpuInfo, PlacementRefused, choose_gpu, query_gpus, vram_budget_mib
from .vram import VramGuard, VramMeasurementFailed

logger = logging.getLogger(__name__)


class ManagedLauncher(EngineLauncher, Protocol):
    """What :class:`~tts_suite.suite.Suite` needs of a launcher beyond the escalation's questions."""

    def set_config(self, config: SuiteConfig) -> None:
        """Take a new configuration; the remembered failures of all entries are forgotten."""
        ...

    def clear_failures(self, entry: EscalationEntry | None) -> None:
        """Forget why the entry (``None`` = every entry) did not start or was stopped for its VRAM use:
        the next request tries again."""
        ...

    async def stop(self, entry: EscalationEntry) -> None:
        """Take the entry's container down. :class:`EngineInUse` while a reading holds it,
        :class:`EngineControlFailed` when it cannot be stopped."""
        ...

    async def status(self, entry: EscalationEntry) -> EntryStatus: ...

    async def aclose(self) -> None: ...


class SuiteLauncher:
    """The real :class:`ManagedLauncher`."""

    def __init__(
        self,
        config: SuiteConfig,
        *,
        gpus: Callable[[], list[GpuInfo]] = query_gpus,
        host_command: HostCommand = run_host_command,
        holds: EngineHolds | None = None,
        guard: VramGuard | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._gpus = gpus
        self._host_command = host_command
        self._holds = holds or EngineHolds()
        self._guard = guard or VramGuard()
        self._clock = clock
        self._starts: dict[str, asyncio.Task[Skip | None]] = {}
        # Why an entry is out: its last start did not work, or it was stopped for exceeding its VRAM
        # budget. Kept until an explicit start, a new configuration or a reload — a container that
        # would simply be started again would be stopped again at the next measurement.
        self._failures: dict[str, Skip] = {}
        # One local GPU start at a time: a container takes its VRAM only while the model loads, so a
        # second start chosen before that would see the first one's card as still free. The service
        # has exactly one event loop, so an asyncio lock is enough.
        self._local_gpu_start = asyncio.Lock()

    def set_config(self, config: SuiteConfig) -> None:
        self._config = config
        self.clear_failures(None)

    def clear_failures(self, entry: EscalationEntry | None) -> None:
        if entry is None:
            self._failures.clear()
        else:
            self._failures.pop(entry.label, None)

    async def aclose(self) -> None:
        starts = list(self._starts.values())
        for task in starts:
            task.cancel()
        await asyncio.gather(*starts, return_exceptions=True)
        await self._holds.aclose()

    # ── Questions of the escalation ─────────────────────────────────

    async def is_serving(self, entry: EscalationEntry) -> bool:
        serving = await asyncio.to_thread(entry.engine.is_running)
        if serving:
            self._failures.pop(entry.label, None)  # it runs (someone started it): nothing is wrong with it now
        return serving

    async def ensure_serving(self, entry: EscalationEntry, *, wait: bool) -> Skip | None:
        if not self._in_flight(entry.label):
            if (failure := self._failures.get(entry.label)) is not None:
                return failure  # no new wait for a start that did not work; ``start`` tries again
            unstartable = await self._unstartable(entry)
            if unstartable is not None:
                return unstartable
        task = self._in_flight(entry.label) or self._begin_start(entry)
        if not wait:
            raise EngineStarting(f"{entry.label} is starting")
        limit_s = entry.engine.startup_timeout_s + START_WAIT_RESERVE_S
        try:
            # shield: a waiter that goes away or runs out of patience does not cancel the start
            return await asyncio.wait_for(asyncio.shield(task), limit_s)
        except asyncio.TimeoutError:
            return Skip("stopped", f"start did not finish within {limit_s} s")

    def hold(self, entry: EscalationEntry) -> None:
        if entry.engine.runs_in_container:
            self._holds.acquire(entry.label, entry.engine)

    def release(self, entry: EscalationEntry) -> None:
        self._holds.release(entry.label)

    async def check_vram(self, entry: EscalationEntry) -> None:
        engine = entry.engine
        if entry.host is not None or not engine.needs_gpu:
            return  # a remote host watches its own cards
        budget = vram_budget_mib(engine, self._config)
        if budget is None:
            return
        try:
            used = await asyncio.to_thread(self._guard.used_mib, engine)
        except VramMeasurementFailed as failed:
            # Not measured is not "within the budget" and not "over it": the sentence is fine, the ceiling is blind
            logger.error("TTS: VRAM of %s could not be measured, the ceiling is not enforced: %s", entry.label, failed)
            return
        if used is None or used <= budget:
            return
        detail = f"{entry.label} uses {used} MiB, budget is {budget} MiB"
        stopped, message = await asyncio.to_thread(engine.stop)
        if not stopped:
            detail += f" (stopping it failed: {message})"
        logger.warning("TTS: %s", detail)
        self._failures[entry.label] = Skip("no_vram", f"exceeded budget: {detail}")
        raise TTSFailure("vram_exceeded", detail)

    # ── Starting ────────────────────────────────────────────────────

    def _in_flight(self, label: str) -> asyncio.Task[Skip | None] | None:
        task = self._starts.get(label)
        return task if task is not None and not task.done() else None

    def _begin_start(self, entry: EscalationEntry) -> asyncio.Task[Skip | None]:
        task = asyncio.get_running_loop().create_task(self._start(entry))
        self._starts[entry.label] = task
        task.add_done_callback(lambda done: self._start_finished(entry.label, done))
        return task

    def _start_finished(self, label: str, task: asyncio.Task[Skip | None]) -> None:
        """Remember how the start ended, so that status and a later ``wait=False`` request can tell."""
        if self._starts.get(label) is task:
            del self._starts[label]
        if task.cancelled():
            return
        error = task.exception()  # also marks it as retrieved: a background start has no one awaiting it
        if error is not None:
            logger.error("TTS start of %s failed: %r", label, error)
            self._failures[label] = Skip("stopped", f"start failed: {type(error).__name__}: {error}")
            return
        skip = task.result()
        if skip is None:
            self._failures.pop(label, None)
        else:
            self._failures[label] = skip

    async def _unstartable(self, entry: EscalationEntry) -> Skip | None:
        """Why the suite has no way to start this entry; ``None`` = it has."""
        engine = entry.engine
        if not engine.runs_in_container:
            return Skip("stopped", f"{engine.key} has no container the suite can start")
        if entry.host is None and not await asyncio.to_thread(engine.is_installed):
            return Skip("stopped", "docker image not built")
        return None

    async def _start(self, entry: EscalationEntry) -> Skip | None:
        if entry.host is not None:
            return await self._start_remote(entry.engine, entry.host)
        return await self._start_local(entry.engine)

    async def _start_local(self, engine: TTSEngine) -> Skip | None:
        """Start the container on this machine — on the card the rules choose when it needs a GPU —
        and wait until its model is loaded."""
        async with self._local_gpu_start:
            if await asyncio.to_thread(engine.is_running):
                return None  # started by whoever held the lock before us
            if engine.needs_gpu:
                try:
                    cards = await asyncio.to_thread(self._gpus)
                    placement = choose_gpu(cards, engine, self._config)
                except PlacementRefused as refused:
                    return Skip("no_vram", str(refused))
                logger.info(
                    "TTS: starting %s on GPU %d (budget %d MiB, %d MiB free)",
                    engine.key, placement.gpu.index, placement.budget_mib, placement.gpu.free_mib,
                )
                engine = engine.on_gpu(placement.gpu.uuid, placement.budget_mib)
            ready, message, _device = await asyncio.to_thread(engine.ensure_ready)
        return None if ready else Skip("stopped", f"start failed: {message}")

    async def _start_remote(self, engine: TTSEngine, host: Host) -> Skip | None:
        """Start the container over SSH and wait until it serves (the reply waits for it: a fast
        remote card beats a worse entry further down the list)."""
        logger.info("TTS: starting %s on %s", engine.key, host.name)
        try:
            await asyncio.to_thread(self._host_command, host, "start", engine.service_dir)
        except HostControlError as failed:
            return Skip("stopped", f"start failed: {failed}")
        deadline = self._clock() + engine.startup_timeout_s
        while not await asyncio.to_thread(engine.is_running):
            if self._clock() >= deadline:
                return Skip("stopped", f"not ready on {host.name} within {engine.startup_timeout_s} s")
            await asyncio.sleep(TTS_READY_POLL_INTERVAL_S)
        return None

    # ── Stopping and status ─────────────────────────────────────────

    async def stop(self, entry: EscalationEntry) -> None:
        engine = entry.engine
        if not engine.runs_in_container:
            raise EngineControlFailed(f"{entry.label} has no container the suite controls")
        if self._holds.in_use(entry.label):
            raise EngineInUse(f"{entry.label} is in use by a reading")
        if self._in_flight(entry.label):
            raise EngineInUse(f"{entry.label} is starting")
        if entry.host is not None:
            try:
                await asyncio.to_thread(self._host_command, entry.host, "stop", engine.service_dir)
            except HostControlError as failed:
                raise EngineControlFailed(str(failed)) from failed
            return
        stopped, message = await asyncio.to_thread(engine.stop)
        if not stopped:
            raise EngineControlFailed(f"{entry.label}: {message}")

    async def status(self, entry: EscalationEntry) -> EntryStatus:
        """What the entry would do right now, by checks only — nothing is started."""
        if self._in_flight(entry.label):
            return "starting"
        if await asyncio.to_thread(entry.engine.is_running):
            return "running"
        if not entry.engine.runs_in_container:
            return "unreachable"  # a cloud engine without access: there is nothing the suite could start
        return "stopped"

    def start_failure(self, entry: EscalationEntry) -> Skip | None:
        """Why the last start of this entry did not work (until the next one)."""
        return self._failures.get(entry.label)
