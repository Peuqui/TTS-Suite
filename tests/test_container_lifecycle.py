"""Container lifecycle of the engines: one shared scaffold in ``TTSEngine``."""
from __future__ import annotations

import errno
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import requests

from tts_suite import docker_ctl
from tts_suite.constants import TTS_READY_STATE_CHECK_INTERVAL_S
from tts_suite.engines import TTS_ENGINES, get_engine
from tts_suite.engines import base as engines_base
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


def _waiting_engine(
    monkeypatch: pytest.MonkeyPatch, states: list[str] | Exception, log_tail: str = "",
) -> tuple[XTTSEngine, list[float], list[str]]:
    """An engine whose container starts but whose health never answers. Returns the engine, the
    sleeps it made and the container names Docker was asked about; ``states``: what Docker
    answers one question after the other (the last one stays), or the error it raises."""
    engine = XTTSEngine()
    sleeps: list[float] = []
    asked: list[str] = []
    answers = iter(states if isinstance(states, list) else [])

    def state(name: str) -> str:
        asked.append(name)
        if isinstance(states, Exception):
            raise states
        return next(answers)

    monkeypatch.setattr(engine, "_health", lambda timeout=None: None)
    monkeypatch.setattr(engine, "_start_local", lambda: (True, "up"))
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr(engines_base, "container_state", state)
    monkeypatch.setattr(engines_base, "container_log_tail", lambda name: log_tail)
    return engine, sleeps, asked


def test_ensure_ready_stops_waiting_when_the_container_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, sleeps, asked = _waiting_engine(monkeypatch, ["exited"], log_tail="OutOfMemoryError")
    ok, message, device = engine.ensure_ready(timeout=1000)
    assert (ok, device) == (False, "")
    assert "container exited while the model loaded" in message and message.endswith(": OutOfMemoryError")
    assert asked == ["xtts"]  # the container name, asked once
    assert len(sleeps) == TTS_READY_STATE_CHECK_INTERVAL_S  # not the 1000 s of the limit


def test_ensure_ready_stops_waiting_when_the_container_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _sleeps, _asked = _waiting_engine(monkeypatch, ["absent"], log_tail="never asked for")
    ok, message, _device = engine.ensure_ready(timeout=1000)
    assert not ok
    assert message == "XTTS: container exited while the model loaded (state: absent)"


@pytest.mark.parametrize("alive_state", ["created", "running", "restarting"])
def test_ensure_ready_keeps_polling_while_the_container_is_there(
    alive_state: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 3 * TTS_READY_STATE_CHECK_INTERVAL_S
    engine, sleeps, asked = _waiting_engine(monkeypatch, [alive_state] * 10)
    ok, message, _device = engine.ensure_ready(timeout=limit)
    assert not ok and f"Timeout after {limit}s" in message
    assert len(sleeps) == limit
    assert len(asked) == 2  # at 5 and 10 s; the poll at 15 s does not exist (the range ends at 14)


def test_ensure_ready_stops_when_docker_cannot_tell(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, sleeps, _asked = _waiting_engine(monkeypatch, docker_ctl.ContainerStateUnreadable("docker unavailable: x"))
    ok, message, _device = engine.ensure_ready(timeout=1000)
    assert not ok and "cannot tell whether the container is alive: docker unavailable: x" in message
    assert len(sleeps) == TTS_READY_STATE_CHECK_INTERVAL_S


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
    assert docker_ctl.docker_compose_action(compose_file, "up", "XTTS", voices_dir=compose_file.parent, gpu_uuid="GPU-1") == (True, "XTTS container started")
    call = run.calls[0]
    assert call["command"] == ["docker", "compose", "-f", str(compose_file), "up", "-d"]
    assert call["env"]["TTS_GPU_UUID"] == "GPU-1"
    assert call["env"]["TTS_VOICES_DIR"] == str(compose_file.parent)
    assert call["cwd"] == str(compose_file.parent)


def test_compose_up_without_a_card_leaves_the_env_file_value(compose_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("TTS_GPU_UUID", raising=False)
    docker_ctl.docker_compose_action(compose_file, "up", "XTTS", voices_dir=compose_file.parent)
    assert "TTS_GPU_UUID" not in run.calls[0]["env"]


def test_compose_down_and_failures(compose_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    assert docker_ctl.docker_compose_action(compose_file, "down", "XTTS", voices_dir=compose_file.parent) == (True, "XTTS container stopped")
    assert run.calls[0]["command"][-1] == "down" and "TTS_GPU_UUID" not in run.calls[0]["env"]

    run.returncode, run.stderr = 1, "boom"
    ok, message = docker_ctl.docker_compose_action(compose_file, "up", "XTTS", voices_dir=compose_file.parent)
    assert not ok and "boom" in message

    ok, message = docker_ctl.docker_compose_action(compose_file.with_name("missing.yml"), "up", "XTTS", voices_dir=compose_file.parent)
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


# ── Reachability of the engine's host (from the health request alone) ──


def _free_port() -> int:
    """A loopback port nothing listens on: bound to learn a free number, then released."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _failing_get(error: Exception) -> Any:
    def get(*args: Any, **kwargs: Any) -> Any:
        raise error
    return get


def _caused_by(error: Exception, cause: BaseException) -> Exception:
    error.__cause__ = cause
    return error


def test_a_closed_port_on_a_reachable_host_is_refused() -> None:
    engine = XTTSEngine().at_host("127.0.0.1", _free_port())
    assert engine.reachability() == "refused"
    assert not engine.is_running()


@pytest.mark.parametrize(("error", "expected"), [
    (requests.ConnectTimeout("timed out"), "unreachable"),
    (_caused_by(requests.ConnectionError("no route"), OSError(errno.EHOSTUNREACH, "No route to host")), "unreachable"),
    (_caused_by(requests.ConnectionError("no net"), OSError(errno.ENETUNREACH, "Network is unreachable")), "unreachable"),
    (_caused_by(requests.ConnectionError("no name"), socket.gaierror(-2, "Name or service not known")), "unreachable"),
    (_caused_by(requests.ConnectionError("refused"), ConnectionRefusedError(errno.ECONNREFUSED, "refused")), "refused"),
    (_caused_by(requests.ConnectionError("cut off"), ConnectionResetError(errno.ECONNRESET, "reset")), "answering"),
    (requests.ReadTimeout("no answer"), "answering"),
])
def test_the_kind_of_connection_error_tells_whether_the_host_is_there(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str,
) -> None:
    monkeypatch.setattr(requests, "get", _failing_get(error))
    engine = XTTSEngine().at_host("192.0.2.2")
    assert engine.reachability() == expected
    assert not engine.is_running()


def test_an_http_answer_that_is_not_ready_means_the_host_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    class Answer:
        ok = False
        headers: dict[str, str] = {}

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Answer())
    engine = XTTSEngine().at_host("192.0.2.2")
    assert engine.reachability() == "answering" and not engine.is_running()
