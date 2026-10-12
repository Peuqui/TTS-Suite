"""Which containers a reading is using right now — and keeping them alive meanwhile.

Ported from AIfred Intelligence (``acquire_tts`` / ``release_tts`` / ``tts_keepalive_loop``). A
container that a reading holds is not stopped (a stop would tear it down under the reading), and
while it is held, a heartbeat resets its idle timer (the containers end themselves after
``*_KEEP_ALIVE`` minutes without a request). The count lives here, once, in the service; readings
hold through their runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import requests

from .constants import TTS_KEEPALIVE_HTTP_TIMEOUT, TTS_KEEPALIVE_INTERVAL_SECONDS
from .engines.base import TTSEngine

logger = logging.getLogger(__name__)

KEEP_ALIVE_PATH = "/keep_alive"


def ping_keep_alive(engine: TTSEngine) -> None:
    """``GET /keep_alive`` of the engine's container (the endpoint only resets the idle timer).
    Blocking; raises what ``requests`` raises."""
    requests.get(f"{engine.service_url}{KEEP_ALIVE_PATH}", timeout=TTS_KEEPALIVE_HTTP_TIMEOUT)


class EngineHolds:
    """A count per held entry (``engine@host``). The first hold starts the heartbeat task of the
    entry, the last release ends it. Needs the running event loop when the first hold is taken."""

    def __init__(
        self,
        ping: Callable[[TTSEngine], None] = ping_keep_alive,
        interval_s: float = TTS_KEEPALIVE_INTERVAL_SECONDS,
    ) -> None:
        self._ping = ping
        self._interval_s = interval_s
        self._counts: dict[str, int] = {}
        self._heartbeats: dict[str, asyncio.Task[None]] = {}

    def acquire(self, label: str, engine: TTSEngine) -> None:
        self._counts[label] = self._counts.get(label, 0) + 1
        if label not in self._heartbeats:
            self._heartbeats[label] = asyncio.get_running_loop().create_task(self._heartbeat(label, engine))

    def release(self, label: str) -> None:
        """One hold less; a release without a hold does nothing."""
        count = self._counts.get(label, 0)
        if count <= 1:
            self._counts.pop(label, None)
            heartbeat = self._heartbeats.pop(label, None)
            if heartbeat is not None:
                heartbeat.cancel()
        else:
            self._counts[label] = count - 1

    def in_use(self, label: str) -> bool:
        return self._counts.get(label, 0) > 0

    async def aclose(self) -> None:
        heartbeats = list(self._heartbeats.values())
        self._counts.clear()
        self._heartbeats.clear()
        for heartbeat in heartbeats:
            heartbeat.cancel()
        await asyncio.gather(*heartbeats, return_exceptions=True)

    async def _heartbeat(self, label: str, engine: TTSEngine) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await asyncio.to_thread(self._ping, engine)
            except requests.ConnectionError:
                # Nothing listening: the container is down on purpose (a stop, a restart) — expected, not a fault.
                logger.info("TTS keep-alive: %s is not listening, ping skipped", label)
            except Exception as exc:  # noqa: BLE001 — a failed ping must not end the heartbeat
                logger.warning("TTS keep-alive ping for %s failed: %s", label, exc)
