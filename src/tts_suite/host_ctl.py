"""Control of containers on another machine over SSH.

Ported from AIfred Intelligence (``host_control``). The key is pinned on the host to
``scripts/tts-host-ctl.sh``, which knows ``start <service>``, ``stop [<service>]`` and ``status``;
nothing else can run through it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from .api import Host, SuiteError
from .constants import (
    TTS_HOST_SSH_COMMAND_TIMEOUT_S,
    TTS_HOST_SSH_CONNECT_TIMEOUT_S,
    TTS_HOST_SSH_KEY_DEFAULT,
    TTS_HOST_SSH_KEY_ENV,
)


class HostControlError(SuiteError):
    """A control command did not run on the host; the message says why."""


#: ``(host, *command) -> output`` — the seam tests replace; blocking, so callers run it in a thread.
HostCommand = Callable[..., str]


def ssh_key_path() -> Path:
    return Path(os.environ.get(TTS_HOST_SSH_KEY_ENV) or TTS_HOST_SSH_KEY_DEFAULT)


def ssh_target(host: Host) -> str:
    return f"{host.ssh_user}@{host.address}" if host.ssh_user else host.address


def run_host_command(host: Host, *command: str) -> str:
    """Run the host's control script over SSH and return its output. :class:`HostControlError`
    with the host's message when it fails."""
    ssh = [
        "ssh", "-i", str(ssh_key_path()), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", f"ConnectTimeout={TTS_HOST_SSH_CONNECT_TIMEOUT_S}", "-p", str(host.ssh_port),
        ssh_target(host), *command,
    ]
    spoken = " ".join(command)
    try:
        result = subprocess.run(
            ssh, capture_output=True, text=True, timeout=TTS_HOST_SSH_COMMAND_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HostControlError(f"{host.name}: '{spoken}' timed out") from exc
    except FileNotFoundError as exc:
        raise HostControlError(f"{host.name}: '{spoken}' cannot run, ssh is not installed") from exc
    if result.returncode != 0:
        raise HostControlError(f"{host.name}: '{spoken}' failed: {result.stderr.strip()[-300:]}")
    return result.stdout
