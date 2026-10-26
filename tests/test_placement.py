"""The card of a GPU engine: the request to GPU-Orc, its answer, its refusals; the budget an engine asks with."""
from __future__ import annotations

import io
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from container_fakes import ContainerEngine, FakeGpuOrc

from tts_suite.api import EngineDefaults, SuiteConfig
from tts_suite.placement import GpuInfo, PlacementRefused, PlaceRequest, parse_gpus, place_with_gpu_orc, vram_budget_mib

URL = "http://127.0.0.1:5070/"
REQUEST = PlaceRequest("tts-suite", "qwen3local", 6024, "engine start")
PLACED: dict[str, Any] = {"card": {"index": 2, "uuid": "GPU-c"}, "why": None, "reserved_s": 60.0, "actions": [], "duration_s": 0.4}


def test_the_nvidia_smi_lines_are_read() -> None:
    assert parse_gpus("0, GPU-a\n1, GPU-b\n") == [GpuInfo(0, "GPU-a"), GpuInfo(1, "GPU-b")]


def test_gpu_orc_names_the_card_and_is_waited_for_as_long_as_it_says(monkeypatch: pytest.MonkeyPatch) -> None:
    orc = FakeGpuOrc(monkeypatch, PLACED, caller_timeout_s=210.0)
    assert place_with_gpu_orc(URL, REQUEST) == GpuInfo(2, "GPU-c")
    health, place = orc.calls
    assert health[:2] == ("GET", "http://127.0.0.1:5070/health")
    body = {"consumer": "tts-suite", "item": "qwen3local", "need_mib": 6024, "reason": "engine start"}
    assert place == ("POST", "http://127.0.0.1:5070/place", body, 210.0)


def test_a_named_card_goes_as_an_index_or_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    orc = FakeGpuOrc(monkeypatch, PLACED)
    place_with_gpu_orc(URL, PlaceRequest("tts-suite", "x", 4000, "engine start", "2"))
    place_with_gpu_orc(URL, PlaceRequest("tts-suite", "x", 4000, "engine start", "GPU-c"))
    assert [call[2]["card"] for call in orc.calls if call[0] == "POST"] == [2, "GPU-c"]


def test_no_card_refuses_with_the_reason_of_gpu_orc(monkeypatch: pytest.MonkeyPatch) -> None:
    why = "needs 6024 MiB and 512 MiB headroom, the most room is 1650 MiB on GPU 4"
    FakeGpuOrc(monkeypatch, {"card": None, "why": why, "reserved_s": None, "actions": [], "duration_s": 0.3})
    with pytest.raises(PlacementRefused) as refused:
        place_with_gpu_orc(URL, REQUEST)
    assert str(refused.value) == f"GPU-Orc: {why}"


def test_a_refused_request_names_the_error_of_gpu_orc(monkeypatch: pytest.MonkeyPatch) -> None:
    body = io.BytesIO(b'{"error": "no side-model service \'tts\', configured: [\'stt-suite\', \'tts-suite\']"}')
    FakeGpuOrc(monkeypatch, error=urllib.error.HTTPError(URL + "place", 404, "Not Found", Message(), body))
    with pytest.raises(PlacementRefused, match="refused: 404 no side-model service 'tts'"):
        place_with_gpu_orc(URL, REQUEST)


def test_a_gpu_orc_that_does_not_answer_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGpuOrc(monkeypatch, error=urllib.error.URLError("connection refused"))
    with pytest.raises(PlacementRefused, match="did not answer"):
        place_with_gpu_orc(URL, REQUEST)


def test_an_answer_in_an_unknown_form_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGpuOrc(monkeypatch, {"card": {"index": 2}, "why": None})
    with pytest.raises(PlacementRefused, match="unknown form"):
        place_with_gpu_orc(URL, REQUEST)


def test_the_configured_budget_beats_the_engines_own(tmp_path: Path) -> None:
    engine = ContainerEngine("x")
    assert vram_budget_mib(engine, SuiteConfig(voices_dir=tmp_path)) == 4000
    configured = SuiteConfig(voices_dir=tmp_path, engines={"x": EngineDefaults(vram_budget_mib=8000)})
    assert vram_budget_mib(engine, configured) == 8000
