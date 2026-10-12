"""Edge, eSpeak, Piper: arguments and failure mapping (external programs and network are replaced)."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
from speech_fakes import TEST_EXECUTOR

from tts_suite.constants import DATA_DIR_DEFAULT, DATA_DIR_ENV, PIPER_MODELS_DIR_NAME, data_dir
from tts_suite.engines import TTSFailure, get_engine
from tts_suite.engines.edge.engine import EDGE_STANDARD_VOICE
from tts_suite.engines.espeak.engine import ESPEAK_STANDARD_VOICE
from tts_suite.engines.piper import engine as piper_module

AUDIO = b"RIFF" + bytes(300)


def _engine(key: str) -> Any:
    engine = get_engine(key)
    assert engine is not None
    return engine


# ── Edge ───────────────────────────────────────────────────────────

class FakeCommunicate:
    created: ClassVar[list[tuple[str, str, str]]] = []
    chunks: ClassVar[list[dict[str, Any]]] = []
    error: Exception | None = None

    def __init__(self, text: str, voice: str, rate: str) -> None:
        FakeCommunicate.created.append((text, voice, rate))

    async def stream(self) -> Any:
        for chunk in FakeCommunicate.chunks:
            yield chunk
        if FakeCommunicate.error:
            raise FakeCommunicate.error


@pytest.fixture
def edge(monkeypatch: pytest.MonkeyPatch) -> type[FakeCommunicate]:
    import edge_tts
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    FakeCommunicate.created = []
    FakeCommunicate.error = None
    FakeCommunicate.chunks = [
        {"type": "audio", "data": AUDIO[:150]}, {"type": "WordBoundary"}, {"type": "audio", "data": AUDIO[150:]},
    ]
    return FakeCommunicate


def test_edge_joins_the_audio_chunks_and_maps_voice_and_rate(edge: type[FakeCommunicate]) -> None:
    audio = asyncio.run(_engine("edge").generate_speech_async("Hallo", "Deutsch (Conrad)", "de", speed=1.25, executor=TEST_EXECUTOR))
    assert audio == AUDIO
    assert edge.created == [("Hallo", "de-DE-ConradNeural", "+25%")]


def test_edge_unknown_voice_speaks_with_the_standard_voice(edge: type[FakeCommunicate]) -> None:
    asyncio.run(_engine("edge").generate_speech_async("Hallo", "Nobody", "de", executor=TEST_EXECUTOR))
    assert edge.created[0][1] == EDGE_STANDARD_VOICE


def test_edge_empty_text_is_a_software_failure(edge: type[FakeCommunicate]) -> None:
    with pytest.raises(TTSFailure) as failure:
        asyncio.run(_engine("edge").generate_speech_async("  ", "Deutsch (Katja)", "de", executor=TEST_EXECUTOR))
    assert failure.value.reason == "software"


def test_edge_service_error_is_an_engine_failure(edge: type[FakeCommunicate]) -> None:
    from edge_tts.exceptions import NoAudioReceived
    edge.error = NoAudioReceived("none")
    with pytest.raises(TTSFailure) as failure:
        asyncio.run(_engine("edge").generate_speech_async("Hallo", "Deutsch (Katja)", "de", executor=TEST_EXECUTOR))
    assert failure.value.reason == "engine"


def test_edge_network_error_is_unreachable(edge: type[FakeCommunicate]) -> None:
    edge.error = ConnectionResetError("reset")
    with pytest.raises(TTSFailure) as failure:
        asyncio.run(_engine("edge").generate_speech_async("Hallo", "Deutsch (Katja)", "de", executor=TEST_EXECUTOR))
    assert failure.value.reason == "unreachable"


# ── eSpeak ─────────────────────────────────────────────────────────

class RunRecorder:
    def __init__(self, result: subprocess.CompletedProcess[bytes] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"command": command, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _completed(returncode: int = 0, stdout: bytes = AUDIO, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_espeak_speaks_to_stdout_with_native_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    run = RunRecorder(_completed())
    monkeypatch.setattr(subprocess, "run", run)
    assert _engine("espeak").generate_speech("Hallo", "Englisch UK", "en", speed=2.0) == AUDIO
    assert run.calls[0]["command"] == ["espeak-ng", "-v", "en-gb", "-s", "350", "--stdout", "Hallo"]


def test_espeak_unknown_voice_speaks_with_the_standard_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    run = RunRecorder(_completed())
    monkeypatch.setattr(subprocess, "run", run)
    _engine("espeak").generate_speech("Hallo", "Nobody", "de")
    assert run.calls[0]["command"][2] == ESPEAK_STANDARD_VOICE


def test_espeak_failures_are_engine_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", RunRecorder(_completed(returncode=1, stdout=b"", stderr=b"bad voice")))
    with pytest.raises(TTSFailure, match="eSpeak exit 1: bad voice"):
        _engine("espeak").generate_speech("Hallo", "Deutsch Standard", "de")
    monkeypatch.setattr(subprocess, "run", RunRecorder(FileNotFoundError("espeak-ng")))
    with pytest.raises(TTSFailure, match="not installed"):
        _engine("espeak").generate_speech("Hallo", "Deutsch Standard", "de")
    monkeypatch.setattr(subprocess, "run", RunRecorder(subprocess.TimeoutExpired("espeak-ng", 1)))
    with pytest.raises(TTSFailure, match="did not finish"):
        _engine("espeak").generate_speech("Hallo", "Deutsch Standard", "de")


@pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="espeak-ng not installed — real synthesis cannot run")
def test_espeak_real_synthesis_returns_a_wav() -> None:
    audio = _engine("espeak").generate_speech("Hallo Welt", "Deutsch Standard", "de")
    assert audio[:4] == b"RIFF" and len(audio) > 1000


# ── Piper ──────────────────────────────────────────────────────────

def test_the_data_folder_comes_from_the_environment_else_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    assert data_dir() == tmp_path
    monkeypatch.delenv(DATA_DIR_ENV)
    assert data_dir() == DATA_DIR_DEFAULT
    monkeypatch.setenv(DATA_DIR_ENV, "")  # an empty variable counts as unset
    assert data_dir() == DATA_DIR_DEFAULT


def _fake_piper(monkeypatch: pytest.MonkeyPatch, returncode: int = 0, write: bool = True) -> RunRecorder:
    recorder = RunRecorder(_completed(returncode=returncode, stdout=b"", stderr=b"model missing"))

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if write:
            Path(command[command.index("--output_file") + 1]).write_bytes(AUDIO)
        return recorder(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return recorder


def test_piper_passes_model_and_length_scale_and_returns_the_file_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    recorder = _fake_piper(monkeypatch)
    assert _engine("piper").generate_speech("Hallo", "Deutsch (Ramona)", "de", speed=2.0) == AUDIO
    command = recorder.calls[0]["command"]
    assert command[command.index("--model") + 1] == str(tmp_path / PIPER_MODELS_DIR_NAME / "de_DE-ramona-low.onnx")
    assert command[command.index("--length_scale") + 1] == "0.5"
    assert recorder.calls[0]["input"] == b"Hallo"


def test_piper_unknown_voice_speaks_with_the_standard_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _fake_piper(monkeypatch)
    _engine("piper").generate_speech("Hallo", "Nobody", "de")
    model_filename = piper_module.PIPER_VOICES[piper_module.PIPER_STANDARD_VOICE][0]
    assert recorder.calls[0]["command"][2].endswith(model_filename)


def test_piper_failures_are_engine_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_piper(monkeypatch, returncode=1, write=False)
    with pytest.raises(TTSFailure, match="Piper exit 1: model missing"):
        _engine("piper").generate_speech("Hallo", "Deutsch (Thorsten)", "de")


def test_piper_that_hangs_is_an_engine_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", RunRecorder(subprocess.TimeoutExpired("piper", 1)))
    with pytest.raises(TTSFailure, match="did not finish"):
        _engine("piper").generate_speech("Hallo", "Deutsch (Thorsten)", "de")


def test_piper_binary_sits_next_to_the_interpreter() -> None:
    import sys
    assert piper_module._piper_binary().parent == Path(sys.executable).parent


def test_browser_never_speaks() -> None:
    engine = _engine("browser")
    assert engine.renders_in_browser
    with pytest.raises(NotImplementedError):
        engine.generate_speech("Hallo", "Auto", "de")
