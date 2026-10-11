"""Who may do what. While the service listens only on a loopback address, speaking and every
``GET`` ask for nothing. A command that changes something (configuration, containers, voices
uploaded to a cloud) always needs the token, also locally, since every program on the machine
reaches localhost. Listening elsewhere, everything needs it and the service refuses to start
without one."""

from __future__ import annotations

import ipaddress
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ServiceError

BEARER_PREFIX = "Bearer "
TOKEN_BYTES = 32
TOKEN_FILE_MODE = 0o600


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Access:
    token: str | None
    # Every request needs the token, not only the ones that change something.
    token_for_everything: bool

    def check(self, headers: Mapping[str, str], changes_something: bool) -> None:
        if not (changes_something or self.token_for_everything):
            return
        if self.token is None:
            raise ServiceError(401, "unauthorized", "this needs a token and none is configured")
        sent = headers.get("authorization", "")
        if not sent.startswith(BEARER_PREFIX) or not secrets.compare_digest(
            sent.removeprefix(BEARER_PREFIX), self.token
        ):
            raise ServiceError(401, "unauthorized", "a valid token is needed (Authorization: Bearer)")


def access_for(host: str, token: str | None) -> Access:
    """The access rules for a service listening on `host`; refuses an open one on a network."""
    everywhere = not is_loopback(host)
    if everywhere and token is None:
        raise ValueError(f"listening on {host} needs a token (--token-file); there is none")
    return Access(token=token, token_for_everything=everywhere)


def read_token(path: Path) -> str | None:
    """The token in the file, None while there is no file."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def create_token(path: Path) -> str:
    """A new token in a file only the user can read; an existing file is left as it is."""
    existing = read_token(path)
    if existing is not None:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(descriptor, TOKEN_FILE_MODE)
        handle.write(token + "\n")
    return token
