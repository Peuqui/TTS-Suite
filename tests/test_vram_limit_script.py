"""``docker/tts/_vram_limit/sitecustomize.py`` — run against a fake ``torch``, never the real one."""
from __future__ import annotations

import py_compile
import runpy
import sys
import types
from pathlib import Path
from typing import Any, Callable

import pytest

from tts_suite.constants import DOCKER_TTS_DIR

SCRIPT = DOCKER_TTS_DIR / "_vram_limit" / "sitecustomize.py"
MIB = 1024 * 1024


class FakeTorch(types.ModuleType):
    """Records what the script does: ``_lazy_call`` queues, ``set_per_process_memory_fraction`` is the cap."""

    def __init__(self, total_mib: int) -> None:
        super().__init__("torch")
        self.queued: list[Callable[[], None]] = []
        self.caps: list[tuple[float, int]] = []
        self.device_queries: list[int] = []
        torch = self

        def get_device_properties(device: int) -> Any:
            torch.device_queries.append(device)
            return types.SimpleNamespace(total_memory=total_mib * MIB)

        self.cuda = types.SimpleNamespace(
            _lazy_call=self.queued.append,
            get_device_properties=get_device_properties,
            set_per_process_memory_fraction=lambda fraction, device: torch.caps.append((fraction, device)),
        )


def _run(monkeypatch: pytest.MonkeyPatch, torch: FakeTorch, budget: str | None) -> None:
    monkeypatch.setitem(sys.modules, "torch", torch)
    if budget is None:
        monkeypatch.delenv("TTS_VRAM_BUDGET_MIB", raising=False)
    else:
        monkeypatch.setenv("TTS_VRAM_BUDGET_MIB", budget)
    runpy.run_path(str(SCRIPT))


def test_the_script_compiles(tmp_path: Path) -> None:
    py_compile.compile(str(SCRIPT), cfile=str(tmp_path / "sitecustomize.pyc"), doraise=True)


@pytest.mark.parametrize("budget", [None, ""])
def test_without_a_budget_nothing_happens(budget: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    torch = FakeTorch(total_mib=16384)
    _run(monkeypatch, torch, budget)
    assert torch.queued == [] and torch.caps == [] and torch.device_queries == []


def test_the_cap_is_applied_only_when_cuda_is_first_used_and_as_a_share_of_the_card(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    torch = FakeTorch(total_mib=16000)
    _run(monkeypatch, torch, "4000")
    # Registered through _lazy_call, not applied: the server master must not initialise CUDA before the fork.
    assert len(torch.queued) == 1 and torch.caps == [] and torch.device_queries == []
    torch.queued[0]()  # CUDA initialises
    assert torch.caps == [(0.25, 0)] and torch.device_queries == [0]
    assert "[sitecustomize] VRAM cap 4000 MiB" in capsys.readouterr().out


def test_a_budget_that_is_not_a_number_is_an_error_not_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        _run(monkeypatch, FakeTorch(total_mib=16000), "lots")
