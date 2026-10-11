"""``docker/tts/_vram_limit/sitecustomize.py`` — run the way Python runs it: imported at interpreter start-up
from ``PYTHONPATH``, in a subprocess, against a stub ``torch`` package (never the real one)."""
from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tts_suite.constants import DOCKER_TTS_DIR

SCRIPT = DOCKER_TTS_DIR / "_vram_limit" / "sitecustomize.py"
BUDGET_ENV = "TTS_VRAM_BUDGET_MIB"
CARD_TOTAL_MIB = 16000

# The stub records into the file named by STUB_LOG: that it was imported, that the callback was handed
# to ``_lazy_call``, and what the callback then asked of the card.
STUB_TORCH = f"""
import json, os

def _log(event):
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps(event) + "\\n")

def _lazy_call(callback):
    _log(["lazy_call"])
    callback()

def get_device_properties(device):
    _log(["get_device_properties", device])
    class Properties:
        total_memory = {CARD_TOTAL_MIB} * 1024 * 1024
    return Properties()

def set_per_process_memory_fraction(fraction, device):
    _log(["set_fraction", fraction, device])

class cuda:
    _lazy_call = staticmethod(_lazy_call)
    get_device_properties = staticmethod(get_device_properties)
    set_per_process_memory_fraction = staticmethod(set_per_process_memory_fraction)

_log(["imported"])
"""


def _start_interpreter(tmp_path: Path, budget: str | None) -> tuple[subprocess.CompletedProcess[str], list[list[object]]]:
    startup = tmp_path / "startup"
    (startup / "torch").mkdir(parents=True)
    shutil.copy(SCRIPT, startup / "sitecustomize.py")
    (startup / "torch" / "__init__.py").write_text(STUB_TORCH, encoding="utf-8")
    log = tmp_path / "stub.log"
    environment = {key: value for key, value in os.environ.items() if key != BUDGET_ENV}
    environment |= {"PYTHONPATH": str(startup), "STUB_LOG": str(log)}
    if budget is not None:
        environment[BUDGET_ENV] = budget
    result = subprocess.run(
        [sys.executable, "-c", "print('program ran')"], env=environment, capture_output=True, text=True, timeout=60,
    )
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, events


def test_the_script_compiles(tmp_path: Path) -> None:
    py_compile.compile(str(SCRIPT), cfile=str(tmp_path / "sitecustomize.pyc"), doraise=True)


@pytest.mark.parametrize("budget", [None, ""])
def test_without_a_budget_nothing_happens(budget: str | None, tmp_path: Path) -> None:
    result, events = _start_interpreter(tmp_path, budget)
    assert result.returncode == 0 and "program ran" in result.stdout
    assert events == []  # torch was not even imported


def test_the_cap_is_registered_for_the_first_use_of_cuda_as_a_share_of_the_card(tmp_path: Path) -> None:
    result, events = _start_interpreter(tmp_path, "4000")
    assert result.returncode == 0 and "program ran" in result.stdout
    assert events == [
        ["imported"], ["lazy_call"], ["get_device_properties", 0], ["set_fraction", 4000 / CARD_TOTAL_MIB, 0],
    ]
    assert "[sitecustomize] VRAM cap 4000 MiB" in result.stdout


@pytest.mark.parametrize("budget", ["lots", "0", "-5", "1.5", " "])
def test_an_invalid_budget_stops_the_interpreter_instead_of_running_without_the_cap(
    budget: str, tmp_path: Path,
) -> None:
    result, events = _start_interpreter(tmp_path, budget)
    assert result.returncode != 0
    assert "program ran" not in result.stdout
    assert BUDGET_ENV in result.stderr and "VRAM cap" in result.stderr
    assert events == []
