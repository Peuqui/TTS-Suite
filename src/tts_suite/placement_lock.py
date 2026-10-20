"""The lock that makes choosing a card and loading the engine onto it one program at a time across the
machine. The STT suite takes the same file (``flock``), so a speech engine and a transcription model that
start together do not choose the same card from the same ``nvidia-smi`` reading: the second one waits until
the first has its VRAM and then sees the real state. The operating system lets go of the lock when its holder
dies, so no stale reservation can stay behind."""

from __future__ import annotations

import asyncio
import fcntl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from .constants import PLACEMENT_LOCK_POLL_INTERVAL_S


@asynccontextmanager
async def program_lock(path: Path | None) -> AsyncIterator[None]:
    """Holds the lock file ``path`` (created when missing) exclusively; ``None`` = no other program to
    coordinate with. Polls instead of blocking a thread, so a cancelled start gives up the wait."""
    if path is None:
        yield
        return
    with path.open("a") as lock_file:  # closing the file releases the lock
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(PLACEMENT_LOCK_POLL_INTERVAL_S)
        yield
