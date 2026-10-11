"""Container lifecycle of the engines: one shared scaffold in ``TTSEngine``."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tts_suite import docker_ctl
from tts_suite.engines import TTS_ENGINES, get_engine
from tts_suite.engines.qwen3local.engine import Qwen3LocalEngine
from tts_suite.engines.xtts.engine import XTTSEngine

CONTAINER_ENGINES = [e for e in TTS_ENGINES.values() if e.runs_in_container]


@pytest.mark.parametrize("engine", CONTAINER_ENGINES, ids=lambda e: e.key)
def test_container_engine_declares_what_the_scaffold_needs(engine: Any) -> None:
    assert engine.default_port
    assert engine.startup_timeout_s > 0
    assert engine.docker_compose_path.exists()


def test_model_ready_signatures() -> None:
    assert Qwen3LocalEngine()._model_ready({"model_loaded": True})
    assert not Qwen3LocalEngine()._model_ready({"model_loaded": False})
    assert XTTSEngine()._model_ready({"model_loaded": True, "custom_voices": []})
    assert not XTTSEngine()._model_ready({"model_loaded": True})


def test_ensure_ready_returns_without_starting_when_model_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Qwen3LocalEngine()
    monkeypatch.setattr(engine, "_health", lambda timeout=None: {"model_loaded": True, "device": "cuda:0"})
    monkeypatch.setattr(engine, "_start_local", lambda: pytest.fail("must not start"))
    assert engine.ensure_ready() == (True, "Qwen3-TTS already ready (cuda:0)", "cuda:0")


def test_ensure_ready_starts_and_waits_for_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = XTTSEngine()
    answers = iter([None, None, {"model_loaded": True, "custom_voices": [], "device": "cuda"}])
    started: list[bool] = []

    def start() -> tuple[bool, str]:
        started.append(True)
        return True, "up"

    monkeypatch.setattr(engine, "_health", lambda timeout=None: next(answers))
    monkeypatch.setattr(engine, "_start_local", start)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert engine.ensure_ready() == (True, "XTTS ready (cuda)", "cuda")
    assert started == [True]


def test_ensure_ready_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = XTTSEngine()
    monkeypatch.setattr(engine, "_health", lambda timeout=None: None)
    monkeypatch.setattr(engine, "_start_local", lambda: (True, "up"))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    ok, message, device = engine.ensure_ready(timeout=3)
    assert (ok, device) == (False, "")
    assert "Timeout after 3s" in message


def test_ensure_ready_reports_a_failed_start(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = XTTSEngine()
    monkeypatch.setattr(engine, "_health", lambda timeout=None: None)
    monkeypatch.setattr(engine, "_start_local", lambda: (False, "no docker"))
    assert engine.ensure_ready() == (False, "no docker", "")


def test_remote_engine_is_never_started_or_stopped() -> None:
    remote = XTTSEngine().at_host("192.0.2.2")
    with pytest.raises(RuntimeError, match="remote host"):
        remote.start()
    with pytest.raises(RuntimeError, match="remote host"):
        remote.stop()


def test_remote_ensure_ready_only_checks_health(monkeypatch: pytest.MonkeyPatch) -> None:
    remote = XTTSEngine().at_host("192.0.2.2")
    monkeypatch.setattr(type(remote), "is_running", lambda self: False)
    ok, message, _device = remote.ensure_ready()
    assert not ok
    assert "192.0.2.2" in message


def test_an_engine_without_rest_api_cannot_go_to_another_host() -> None:
    engine = get_engine("piper")
    assert engine is not None
    with pytest.raises(ValueError, match="no REST API"):
        engine.at_host("192.0.2.2")


def test_lightweight_engines_have_nothing_to_start() -> None:
    engine = get_engine("edge")
    assert engine is not None
    assert engine.start() == (True, "no-op")
    assert engine.stop() == (True, "no-op")
    assert engine.ensure_ready() == (True, "ready", "")


# ── docker_ctl (docker itself is replaced) ─────────────────────────

class _Run:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode, self.stderr = returncode, stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


@pytest.fixture
def compose_file(tmp_path: Path) -> Path:
    path = tmp_path / "docker-compose.yml"
    path.write_text("services: {}\n")
    return path


def test_compose_up_pins_the_card(compose_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    assert docker_ctl.docker_compose_action(compose_file, "up", "XTTS", "GPU-1") == (True, "XTTS container started")
    call = run.calls[0]
    assert call["command"] == ["docker", "compose", "-f", str(compose_file), "up", "-d"]
    assert call["env"]["TTS_GPU_UUID"] == "GPU-1"
    assert call["cwd"] == str(compose_file.parent)


def test_compose_up_without_a_card_leaves_the_env_file_value(compose_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("TTS_GPU_UUID", raising=False)
    docker_ctl.docker_compose_action(compose_file, "up", "XTTS")
    assert "TTS_GPU_UUID" not in run.calls[0]["env"]


def test_compose_down_and_failures(compose_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    assert docker_ctl.docker_compose_action(compose_file, "down", "XTTS", "GPU-1") == (True, "XTTS container stopped")
    assert run.calls[0]["command"][-1] == "down" and "TTS_GPU_UUID" not in run.calls[0]["env"]

    run.returncode, run.stderr = 1, "boom"
    ok, message = docker_ctl.docker_compose_action(compose_file, "up", "XTTS")
    assert not ok and "boom" in message

    ok, message = docker_ctl.docker_compose_action(compose_file.with_name("missing.yml"), "up", "XTTS")
    assert not ok and "not found" in message


def test_is_installed_asks_docker_for_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    assert XTTSEngine().is_installed()
    assert run.calls[0]["command"] == ["docker", "image", "inspect", "xtts-rtx8000"]
    run.returncode = 1
    assert not XTTSEngine().is_installed()


def test_is_installed_without_docker_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_docker(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", no_docker)
    assert not XTTSEngine().is_installed()
