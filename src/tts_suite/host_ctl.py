"""Control of containers on another machine over SSH.

Ported from AIfred Intelligence (``host_control``). The key is pinned on the host to
``scripts/tts-host-ctl.sh``, which knows ``start <service>``, ``receive-voices`` (a tar on stdin), ``stop [<service>]`` and ``status``;
nothing else can run through it.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

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


def run_host_command(host: Host, *command: str, stdin: bytes | None = None) -> str:
    """Run the host's control script over SSH (``stdin`` goes to its standard input) and return its
    output. :class:`HostControlError` with the host's message when it fails."""
    ssh = [
        "ssh", "-i", str(ssh_key_path()), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", f"ConnectTimeout={TTS_HOST_SSH_CONNECT_TIMEOUT_S}", "-p", str(host.ssh_port),
        "--", ssh_target(host), *command,
    ]
    spoken = " ".join(command)
    try:
        result = subprocess.run(
            ssh, capture_output=True, input=stdin, timeout=TTS_HOST_SSH_COMMAND_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HostControlError(f"{host.name}: '{spoken}' timed out") from exc
    except FileNotFoundError as exc:
        raise HostControlError(f"{host.name}: '{spoken}' cannot run, ssh is not installed") from exc
    if result.returncode != 0:
        raise HostControlError(f"{host.name}: '{spoken}' failed: {result.stderr.decode(errors='replace').strip()[-300:]}")
    return result.stdout.decode(errors="replace")


def pack_voices(voices_dir: Path) -> bytes:
    """The voices folder as a tar (paths relative to it), the form ``receive-voices`` unpacks."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(voices_dir, arcname=".")
    return buffer.getvalue()
