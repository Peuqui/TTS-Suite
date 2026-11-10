"""Who the service thinks is calling: the peer of the connection, not a header a proxy sets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tts_suite.service import cli


def test_serve_ignores_forwarded_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Behind nginx the peer is this machine. uvicorn's default would take the browser's address from
    X-Forwarded-For; a service that listens beyond loopback (token for everything) then asked every page behind the
    proxy for the token, although the proxy has its own login."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "create_suite", lambda config: object())
    monkeypatch.setattr(cli, "create_app", lambda *args: "the app")
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: captured.update(options))
    arguments = cli.parser().parse_args(
        ["serve", "--host", "0.0.0.0", "--config", str(tmp_path / "config.json"), "--token-file", str(tmp_path / "token")]
    )
    (tmp_path / "token").write_text("a-token", encoding="utf-8")

    cli.serve(arguments)

    assert captured["proxy_headers"] is False
