"""The thin layers over nvidia-smi, docker and ssh — with the system call replaced."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from container_fakes import ARAGON

from tts_suite import docker_ctl, host_ctl, placement, vram
from tts_suite.api import Host
from tts_suite.constants import DOCKER_COMPOSE_TIMEOUT_S, TTS_HOST_SSH_KEY_DEFAULT
from tts_suite.engines import base as engines_base
from tts_suite.engines import get_engine
from tts_suite.vram import ComputeApp, engine_vram_mib, parse_compute_apps


class _Run:
    """Replaces ``subprocess.run``: answers with ``stdout``/``returncode`` and records the calls."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "", raises: Exception | None = None) -> None:
        self.stdout, self.returncode, self.stderr, self.raises = stdout, returncode, stderr, raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


# ── nvidia-smi: cards ───────────────────────────────────────────────


def test_query_gpus_reads_the_card_list(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run("0, GPU-aaa, 21000, 24576\n1, GPU-bbb, 900, 16384\n")
    monkeypatch.setattr(subprocess, "run", run)
    cards = placement.query_gpus()
    assert [(c.index, c.uuid, c.free_mib, c.total_mib) for c in cards] == [(0, "GPU-aaa", 21000, 24576), (1, "GPU-bbb", 900, 16384)]
    assert run.calls[0]["command"] == [
        "nvidia-smi", "--query-gpu=index,uuid,memory.free,memory.total", "--format=csv,noheader,nounits",
    ]


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (_Run(raises=FileNotFoundError("nvidia-smi")), "nvidia-smi unavailable"),
        (_Run(returncode=9, stderr="driver gone"), "nvidia-smi failed: driver gone"),
        (_Run(stdout="\n"), "nvidia-smi lists no GPUs"),
    ],
)
def test_query_gpus_refuses_with_the_reason(run: _Run, message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(placement.PlacementRefused, match=message):
        placement.query_gpus()


# ── nvidia-smi / docker: the use of one container ───────────────────


def test_the_compute_app_lines_are_read() -> None:
    assert parse_compute_apps("100, 3000, GPU-a\n200, 9000, GPU-b\n") == [ComputeApp("100", 3000, "GPU-a"), ComputeApp("200", 9000, "GPU-b")]


def test_only_the_processes_of_the_engines_container_count(monkeypatch: pytest.MonkeyPatch) -> None:
    apps = [ComputeApp("100", 3000, "GPU-a"), ComputeApp("101", 500, "GPU-a"), ComputeApp("200", 9000, "GPU-b")]
    monkeypatch.setattr(vram, "compute_apps", lambda: apps)
    monkeypatch.setattr(vram, "container_host_pids", lambda container: {"100", "101", "102"} if container == "xtts" else set())
    assert engine_vram_mib(get_engine("xtts")) == {"GPU-a": 3500}  # type: ignore[arg-type]
    assert engine_vram_mib(get_engine("qwen3local")) == {}  # type: ignore[arg-type]


def test_nothing_is_measured_for_an_engine_without_a_container_or_when_no_process_uses_a_card(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object) -> None:
        raise AssertionError("must not be called")

    monkeypatch.setattr(vram, "compute_apps", fail)
    assert engine_vram_mib(get_engine("edge")) == {}  # type: ignore[arg-type]
    monkeypatch.setattr(vram, "compute_apps", lambda: [])
    monkeypatch.setattr(vram, "container_host_pids", fail)
    assert engine_vram_mib(get_engine("xtts")) == {}  # type: ignore[arg-type]


def test_the_host_pids_of_a_container_come_from_docker_top(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "PID\n400\n401\nnot-a-pid\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert vram.container_host_pids("xtts") == {"400", "401"}
    assert calls == [["docker", "top", "xtts", "-eo", "pid"]]  # no compose: it needs the compose variables


def test_a_container_that_is_not_there_has_no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _Run(returncode=1, stderr="Error response from daemon: No such container: xtts"))
    assert vram.container_host_pids("xtts") == set()
    monkeypatch.setattr(subprocess, "run", _Run(returncode=1, stderr="Error response from daemon: Container abc is not running"))
    assert vram.container_host_pids("xtts") == set()


@pytest.mark.parametrize(
    "run",
    [
        _Run(raises=FileNotFoundError("nvidia-smi")),
        _Run(raises=subprocess.TimeoutExpired("nvidia-smi", 10)),
        _Run(returncode=9, stderr="driver not loaded"),
        _Run(stdout="100, 3000\n"),
        _Run(stdout="100, lots, GPU-a\n"),
    ],
)
def test_a_failing_nvidia_smi_is_a_failed_measurement_not_an_empty_list(run: _Run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(vram.VramMeasurementFailed):
        vram.compute_apps()


def test_memory_the_driver_does_not_report_is_none_and_a_failed_measurement_when_it_is_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    apps = parse_compute_apps("100, [N/A], GPU-a\n200, [Not Supported], GPU-a\n300, 700, GPU-a\n")
    assert [app.used_mib for app in apps] == [None, None, 700]
    monkeypatch.setattr(vram, "compute_apps", lambda: apps)
    monkeypatch.setattr(vram, "container_host_pids", lambda container: {"300"})
    assert engine_vram_mib(get_engine("xtts")) == {"GPU-a": 700}  # type: ignore[arg-type]
    monkeypatch.setattr(vram, "container_host_pids", lambda container: {"100", "300"})
    with pytest.raises(vram.VramMeasurementFailed, match="process 100"):
        engine_vram_mib(get_engine("xtts"))  # type: ignore[arg-type]


def test_docker_not_answering_is_a_failed_measurement_not_no_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _Run(returncode=1, stderr="Cannot connect to the Docker daemon"))
    with pytest.raises(vram.VramMeasurementFailed, match="Docker daemon"):
        vram.container_host_pids("xtts")
    monkeypatch.setattr(subprocess, "run", _Run(raises=FileNotFoundError("docker")))
    with pytest.raises(vram.VramMeasurementFailed, match="docker unavailable"):
        vram.container_host_pids("xtts")


def test_the_ssh_command_pins_key_port_and_target(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(stdout="up\n")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("TTS_SUITE_SSH_KEY", "/keys/suite")
    host = Host("Aragon", "192.0.2.2", ssh_port=2222, ssh_user="tts")
    assert host_ctl.run_host_command(host, "start", "xtts") == "up\n"
    command = run.calls[0]["command"]
    assert command[:3] == ["ssh", "-i", "/keys/suite"]
    assert command[command.index("-p") + 1] == "2222" and command[-4:] == ["--", "tts@192.0.2.2", "start", "xtts"]
    assert "BatchMode=yes" in command and "IdentitiesOnly=yes" in command


def test_without_a_user_the_target_is_the_address_and_without_the_variable_the_default_key_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("TTS_SUITE_SSH_KEY", raising=False)
    host_ctl.run_host_command(ARAGON, "status")
    command = run.calls[0]["command"]
    assert command[2] == str(TTS_HOST_SSH_KEY_DEFAULT) and command[-3:] == ["--", "192.0.2.2", "status"]


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (_Run(returncode=255, stderr="x" * 400 + "Permission denied"), "Aragon: 'start xtts' failed: .*Permission denied"),
        (_Run(raises=subprocess.TimeoutExpired("ssh", 60)), "Aragon: 'start xtts' timed out"),
        (_Run(raises=FileNotFoundError("ssh")), "ssh is not installed"),
    ],
)
def test_a_failing_ssh_command_is_an_error_with_the_reason(run: _Run, message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(host_ctl.HostControlError, match=message) as error:
        host_ctl.run_host_command(ARAGON, "start", "xtts")
    assert len(str(error.value)) < 400  # only the end of a long stderr


# ── Docker: the budget reaches the container ────────────────────────


def test_the_budget_is_handed_to_compose_up_and_only_to_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("TTS_VRAM_BUDGET_MIB", raising=False)
    docker_ctl.docker_compose_action(compose_file, "up", "XTTS", voices_dir=tmp_path, gpu_uuid="GPU-1", vram_budget_mib=3680)
    docker_ctl.docker_compose_action(compose_file, "up", "XTTS", voices_dir=tmp_path, gpu_uuid="GPU-1")
    docker_ctl.docker_compose_action(compose_file, "down", "XTTS", voices_dir=tmp_path, vram_budget_mib=3680)
    assert run.calls[0]["env"]["TTS_VRAM_BUDGET_MIB"] == "3680"
    assert "TTS_VRAM_BUDGET_MIB" not in run.calls[1]["env"] and "TTS_VRAM_BUDGET_MIB" not in run.calls[2]["env"]


def test_a_hanging_docker_compose_times_out_with_a_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    run = _Run(raises=subprocess.TimeoutExpired("docker", 60))
    monkeypatch.setattr(subprocess, "run", run)
    assert docker_ctl.docker_compose_action(compose_file, "up", "XTTS", voices_dir=tmp_path) == (False, "docker compose up timed out")
    assert docker_ctl.docker_compose_action(compose_file, "down", "XTTS", voices_dir=tmp_path) == (False, "docker compose down timed out")
    assert run.calls[0]["timeout"] == DOCKER_COMPOSE_TIMEOUT_S


def test_an_engine_placed_on_a_card_starts_with_card_budget_and_voices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(engines_base, "docker_compose_action", lambda *args, **kwargs: calls.append((args, kwargs)) or (True, "up"))
    placed = get_engine("xtts").with_voices_dir(tmp_path).on_gpu("GPU-9", 3680)  # type: ignore[union-attr]
    assert (placed.gpu_uuid, placed.vram_budget_mib) == ("GPU-9", 3680)
    assert placed.start() == (True, "up")
    assert calls[0][0][2:] == ("XTTS",) and calls[0][1]["gpu_uuid"] == "GPU-9" and calls[0][1]["vram_budget_mib"] == 3680
    assert calls[0][1]["voices_dir"] == tmp_path
    assert get_engine("xtts").vram_budget_mib is None  # type: ignore[union-attr]  # the registry engine is unchanged
