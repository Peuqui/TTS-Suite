"""The launcher: brings the containers of the entries up and down.

Ported from AIfred Intelligence (``tts_escalation``): a local GPU engine starts on the card the
GPU-Orc of this machine names, an engine on another host starts over SSH, an engine
without a container (cloud, Piper, eSpeak, Edge) is only checked. Starts run as one task per entry,
shared by everyone who waits for it and by a ``wait=False`` request that only begins it. A forced
GPU release withdraws a start of a local entry: the readings waiting for it end with
:class:`~tts_suite.api.ReadingAborted`.

Every system call (``nvidia-smi``, GPU-Orc, Docker, SSH, HTTP) sits behind a function handed to the
constructor, so tests replace it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .api import (
    EngineControlFailed,
    EngineInUse,
    EngineStarting,
    EntryStatus,
    Host,
    ReadingAborted,
    SuiteConfig,
)
from .constants import START_WAIT_RESERVE_S, TTS_READY_POLL_INTERVAL_S
from .docker_ctl import CONTAINER_STILL_RUNNING, ContainerStateUnreadable
from .engines.base import TTSEngine
from .escalation import EngineLauncher, EscalationEntry, Skip
from .holds import EngineHolds
from .host_ctl import HostCommand, HostControlError, pack_voices, run_host_command
from .placement import GpuInfo, PlacementRefused, PlaceRequest, place_with_gpu_orc, query_gpus, vram_budget_mib

logger = logging.getLogger(__name__)

#: Why a container must not be taken down now: a start is under way, or a reading holds it.
Busy = Literal["starting", "in_use"]


class ManagedLauncher(EngineLauncher, Protocol):
    """What :class:`~tts_suite.suite.Suite` needs of a launcher beyond the escalation's questions."""

    def set_config(self, config: SuiteConfig) -> None:
        """Take a new configuration; the remembered failures of all entries are forgotten."""
        ...

    def clear_failures(self, entry: EscalationEntry | None) -> None:
        """Forget why the entry (``None`` = every entry) did not start: the next request tries again."""
        ...

    async def start_on_gpu(self, entry: EscalationEntry, gpu: str) -> Skip | None:
        """Start a local GPU entry on the card ``gpu`` (index or UUID) and wait until it serves; ``None`` = it
        serves now, else why not. :class:`EngineInUse` while it runs or starts (its card is chosen already)."""
        ...

    async def stop(self, entry: EscalationEntry, *, force: bool = False) -> None:
        """Take the entry's container down. :class:`EngineInUse` while it starts, and while a reading holds
        it unless ``force``; :class:`EngineControlFailed` when it cannot be stopped. A local container counts
        as stopped only when Docker reports it down (after ``docker kill`` if the stop did not do it)."""
        ...

    async def withdraw_start(self, entry: EscalationEntry, reason: str) -> None:
        """End the start of a local entry that is under way: wait until it gave up, then ``docker kill`` its
        container (a model that is still loading has nothing to wind down). Whoever waits for the start gets
        :class:`ReadingAborted` naming ``reason``. ``stop`` afterwards proves the container down. Nothing to do
        when no start is under way."""
        ...

    def busy(self, entry: EscalationEntry) -> Busy | None:
        """Why the entry's container must not be taken down now; ``None`` = it may."""
        ...

    async def gpu_cards(self, entry: EscalationEntry) -> tuple[str, ...] | None:
        """The cards (UUIDs) a local entry's running container may use, from its configuration; ``None`` when
        its container does not run. :class:`EngineControlFailed` when Docker or ``nvidia-smi`` cannot tell."""
        ...

    async def status(self, entry: EscalationEntry) -> EntryStatus: ...

    async def push_voices(self) -> list[str]:
        """Hand the voices folder to every configured host (they add and overwrite, nothing is deleted).
        The names of the hosts that took it. :class:`EngineControlFailed` at the first host that does not."""
        ...

    def start_failure(self, entry: EscalationEntry) -> Skip | None:
        """Why the last start of the entry did not work (until the next start, a new configuration or a reload)."""
        ...

    async def aclose(self) -> None: ...


@dataclass
class _Withdrawal:
    """How a forced GPU release ends a start under way: the start's thread looks at ``event`` while the
    model loads, ``reason`` goes to the readings that waited for the start."""

    event: threading.Event = field(default_factory=threading.Event)
    reason: str = ""


class SuiteLauncher:
    """The real :class:`ManagedLauncher`."""

    def __init__(
        self,
        config: SuiteConfig,
        *,
        gpus: Callable[[], list[GpuInfo]] = query_gpus,
        place: Callable[[str, PlaceRequest], GpuInfo] = place_with_gpu_orc,
        host_command: HostCommand = run_host_command,
        holds: EngineHolds | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._gpus = gpus
        self._place = place
        self._host_command = host_command
        self._holds = holds or EngineHolds()
        self._clock = clock
        self._starts: dict[str, asyncio.Task[Skip | None]] = {}
        self._withdrawals: dict[str, _Withdrawal] = {}  # label -> how its start under way can be ended
        # Why an entry is out: its last start did not work. Kept until an explicit start, a new
        # configuration or a reload. Not remembered: ``no_vram`` of the card choice, which passes
        # as soon as a card has room again.
        self._failures: dict[str, Skip] = {}

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
            if (unreachable := await self._unreachable_host(entry)) is not None:
                return unreachable
            if (failure := self._failures.get(entry.label)) is not None:
                return failure  # no new wait for a start that did not work; ``start`` tries again
            unstartable = await self._unstartable(entry)
            if unstartable is not None:
                return unstartable
        task = self._in_flight(entry.label) or self._begin_start(entry)
        if not wait:
            raise EngineStarting(f"{entry.label} is starting")
        return await self._wait_for_start(entry, task)

    async def start_on_gpu(self, entry: EscalationEntry, gpu: str) -> Skip | None:
        if self._in_flight(entry.label) or await self.is_serving(entry):
            raise EngineInUse(f"{entry.label} is already running or starting — stop it to choose its card")
        if (unstartable := await self._unstartable(entry)) is not None:
            return unstartable
        return await self._wait_for_start(entry, self._begin_start(entry, gpu))

    async def _wait_for_start(self, entry: EscalationEntry, task: asyncio.Task[Skip | None]) -> Skip | None:
        limit_s = entry.engine.startup_timeout_s + START_WAIT_RESERVE_S
        try:
            # shield: a waiter that goes away or runs out of patience does not cancel the start
            return await asyncio.wait_for(asyncio.shield(task), limit_s)
        except TimeoutError:
            return Skip("stopped", f"start did not finish within {limit_s} s")

    def hold(self, entry: EscalationEntry) -> None:
        if entry.engine.runs_in_container:
            self._holds.acquire(entry.label, entry.engine)

    def release(self, entry: EscalationEntry) -> None:
        self._holds.release(entry.label)

    # ── Starting ────────────────────────────────────────────────────

    def _in_flight(self, label: str) -> asyncio.Task[Skip | None] | None:
        task = self._starts.get(label)
        return task if task is not None and not task.done() else None

    def _begin_start(self, entry: EscalationEntry, gpu: str | None = None) -> asyncio.Task[Skip | None]:
        withdrawal = self._withdrawals[entry.label] = _Withdrawal()
        task = asyncio.get_running_loop().create_task(self._start(entry, gpu, withdrawal))
        self._starts[entry.label] = task
        task.add_done_callback(lambda done: self._start_finished(entry.label, done))
        return task

    def _start_finished(self, label: str, task: asyncio.Task[Skip | None]) -> None:
        """Remember how the start ended, so that status and a later ``wait=False`` request can tell."""
        if self._starts.get(label) is task:
            del self._starts[label]
            del self._withdrawals[label]
        if task.cancelled():
            return
        error = task.exception()  # also marks it as retrieved: a background start has no one awaiting it
        if isinstance(error, ReadingAborted):
            return  # ended on purpose, nothing is wrong with the entry: the next request starts it again
        if error is not None:
            logger.error("TTS start of %s failed: %r", label, error)
            self._failures[label] = Skip("stopped", f"start failed: {type(error).__name__}: {error}")
            return
        skip = task.result()
        if skip is None:
            self._failures.pop(label, None)
        elif skip.reason == "no_vram":
            self._failures.pop(label, None)  # the card choice is tried again at the next request
        else:
            self._failures[label] = skip

    async def _unreachable_host(self, entry: EscalationEntry) -> Skip | None:
        """``stopped`` when the entry's remote host cannot be reached at all (a refusal means the host is up).
        Asked before any SSH attempt, so a host that is off costs one health timeout, not the SSH connect wait."""
        if entry.host is None or not entry.engine.is_remote:  # a deactivated placeholder has no address to ask
            return None
        if await asyncio.to_thread(entry.engine.reachability) != "unreachable":
            return None
        return Skip("stopped", f"host {entry.host.name} unreachable")

    async def _unstartable(self, entry: EscalationEntry) -> Skip | None:
        """Why the suite has no way to start this entry; ``None`` = it has."""
        engine = entry.engine
        if not engine.runs_in_container:
            return Skip("stopped", f"{engine.key} has no container the suite can start")
        if entry.host is None and not await asyncio.to_thread(engine.is_installed):
            return Skip("stopped", "docker image not built")
        return None

    async def _start(self, entry: EscalationEntry, gpu: str | None, withdrawal: _Withdrawal) -> Skip | None:
        if entry.host is not None:
            return await self._start_remote(entry.engine, entry.host)
        return await self._start_local(entry.engine, gpu, withdrawal)

    async def _start_local(self, engine: TTSEngine, gpu: str | None, withdrawal: _Withdrawal) -> Skip | None:
        """Start the container on this machine — on the card GPU-Orc names when it needs a GPU (with ``gpu``
        only that card) — and wait until its model is loaded. :class:`ReadingAborted` when the start is withdrawn."""
        if engine.needs_gpu:
            url = self._config.gpu_orc_url
            if url is None:
                return Skip("stopped", "no GPU-Orc configured (gpu_orc_url): it names the card of a GPU engine")
            budget = vram_budget_mib(engine, self._config)
            if budget is None:
                return Skip("no_vram", f"no VRAM budget for {engine.key} — set engines[{engine.key}].vram_budget_mib")
            request = PlaceRequest(self._config.gpu_orc_consumer, engine.key, budget, "engine start", gpu)
            try:
                card = await asyncio.to_thread(self._place, url, request)
            except PlacementRefused as refused:
                return Skip("no_vram", str(refused))  # transient: ``_start_finished`` does not remember it
            logger.info("TTS: starting %s on GPU %d (budget %d MiB)", engine.key, card.index, budget)
            engine = engine.on_gpu(card.uuid, budget)
        ready, message, _device = await asyncio.to_thread(engine.ensure_ready, withdrawal.event)
        if withdrawal.event.is_set():
            raise ReadingAborted(withdrawal.reason)
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

    async def withdraw_start(self, entry: EscalationEntry, reason: str) -> None:
        task = self._in_flight(entry.label)
        if task is None:
            return
        withdrawal = self._withdrawals[entry.label]
        withdrawal.reason = reason
        withdrawal.event.set()
        # Wait for the thread to give up (at the next poll, a few seconds): a `docker compose up` still running
        # would bring the container back after the kill.
        await asyncio.wait({task})
        killed, message = await asyncio.to_thread(entry.engine.kill_container)
        if not killed:
            logger.warning("TTS: docker kill of the withdrawn start of %s: %s", entry.label, message)

    async def stop(self, entry: EscalationEntry, *, force: bool = False) -> None:
        engine = entry.engine
        if not engine.runs_in_container:
            raise EngineControlFailed(f"{entry.label} has no container the suite controls")
        if self._in_flight(entry.label):
            raise EngineInUse(f"{entry.label} is starting")
        if self._holds.in_use(entry.label) and not force:
            raise EngineInUse(f"{entry.label} is in use by a reading")
        if entry.host is not None:
            try:
                await asyncio.to_thread(self._host_command, entry.host, "stop", engine.service_dir)
            except HostControlError as failed:
                raise EngineControlFailed(str(failed)) from failed
            return
        stopped, message = await asyncio.to_thread(engine.stop)
        await self._prove_down(entry, "" if stopped else message)

    async def _prove_down(self, entry: EscalationEntry, stop_problem: str) -> None:
        """Stopped means Docker reports the container down (gone or exited). One that still runs gets
        ``docker kill`` and is asked again; a stop that cannot be proven raises :class:`EngineControlFailed`."""
        engine = entry.engine
        try:
            state = await asyncio.to_thread(engine.container_state)
            if state in CONTAINER_STILL_RUNNING:
                logger.warning("TTS: %s still %s after the stop (%s), docker kill", entry.label, state, stop_problem or "ok")
                _killed, kill_message = await asyncio.to_thread(engine.kill_container)
                state = await asyncio.to_thread(engine.container_state)
                if state in CONTAINER_STILL_RUNNING:
                    raise EngineControlFailed(f"{entry.label}: container still {state} after docker kill ({kill_message})")
        except ContainerStateUnreadable as unreadable:
            raise EngineControlFailed(f"{entry.label}: stop not proven, {unreadable}") from unreadable
        if stop_problem:
            logger.warning("TTS: %s is down although the stop reported: %s", entry.label, stop_problem)

    def busy(self, entry: EscalationEntry) -> Busy | None:
        if self._in_flight(entry.label):
            return "starting"
        if self._holds.in_use(entry.label):
            return "in_use"
        return None

    async def gpu_cards(self, entry: EscalationEntry) -> tuple[str, ...] | None:
        engine = entry.engine
        try:
            if await asyncio.to_thread(engine.container_state) not in CONTAINER_STILL_RUNNING:
                return None
            configured = await asyncio.to_thread(engine.container_gpus)
            if configured is None:
                return None
            cards = await asyncio.to_thread(self._gpus)
        except (ContainerStateUnreadable, PlacementRefused) as unreadable:
            raise EngineControlFailed(f"{entry.label}: cards unknown, {unreadable}") from unreadable
        return resolve_cards(configured, cards, entry.label)

    async def status(self, entry: EscalationEntry) -> EntryStatus:
        """What the entry would do right now, by checks only — nothing is started."""
        if self._in_flight(entry.label):
            return "starting"
        if await self._unreachable_host(entry) is not None:
            return "unreachable"
        if await asyncio.to_thread(entry.engine.is_running):
            return "running"
        if not entry.engine.runs_in_container:
            return "unreachable"  # a cloud engine without access: there is nothing the suite could start
        return "stopped"

    async def push_voices(self) -> list[str]:
        voices = await asyncio.to_thread(pack_voices, self._config.voices_dir)
        for host in self._config.hosts:
            try:
                await asyncio.to_thread(self._host_command, host, "receive-voices", stdin=voices)
            except HostControlError as failed:
                raise EngineControlFailed(str(failed)) from failed
        return [host.name for host in self._config.hosts]

    def start_failure(self, entry: EscalationEntry) -> Skip | None:
        """Why the last start of this entry did not work (until the next one)."""
        return self._failures.get(entry.label)


def resolve_cards(configured: tuple[str, ...] | Literal["all"], cards: list[GpuInfo], label: str) -> tuple[str, ...]:
    """The UUIDs of what a container's configuration names: ``"all"``, UUIDs (also a unique prefix) or the
    indices of ``nvidia-smi``. :class:`EngineControlFailed` for a name that is no card of this machine."""
    if configured == "all":
        return tuple(card.uuid for card in cards)
    uuids = []
    for name in configured:
        if name.isdigit():
            matches = [card.uuid for card in cards if card.index == int(name)]
        else:
            matches = [card.uuid for card in cards if card.uuid.startswith(name)]
        if len(matches) != 1:
            raise EngineControlFailed(f"{label}: its container names card {name!r}, which is no card of this machine")
        uuids.append(matches[0])
    return tuple(uuids)
