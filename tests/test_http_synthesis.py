"""``_synthesize_via_http``: the shared call of all container engines (HTTP is replaced)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import requests

from tts_suite.constants import TTS_CONNECT_TIMEOUT_S, TTS_READ_TIMEOUT_S
from tts_suite.engines import TTSFailure
from tts_suite.engines.qwen3local.engine import Qwen3LocalEngine
from tts_suite.engines.xtts.engine import XTTSEngine

AUDIO = b"OggS" + bytes(200)


class FakeContainer:
    def __init__(self, reply: SimpleNamespace | Exception) -> None:
        self.reply = reply
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> SimpleNamespace:
        self.posts.append({"url": url, **kwargs})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _answer(status: int = 200, content: bytes = AUDIO, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status, content=content, text=text)


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> FakeContainer:
    fake = FakeContainer(_answer())
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def test_success_returns_the_audio_bytes(container: FakeContainer) -> None:
    audio = XTTSEngine()._synthesize_via_http("/tts", {"text": "Hallo", "speaker": "Alice", "language": "de"})
    assert isinstance(audio, bytes) and audio == AUDIO
    post = container.posts[0]
    assert post["url"] == "http://localhost:5051/tts"
    assert post["json"] == {"text": "Hallo", "speaker": "Alice", "language": "de"}
    assert post["timeout"] == (TTS_CONNECT_TIMEOUT_S, TTS_READ_TIMEOUT_S)


def test_a_remote_engine_posts_to_its_host(container: FakeContainer) -> None:
    XTTSEngine().at_host("192.0.2.2", 6051)._synthesize_via_http("/tts", {"text": "Hallo"})
    assert container.posts[0]["url"] == "http://192.0.2.2:6051/tts"


def test_connection_refused_is_unreachable(container: FakeContainer) -> None:
    container.reply = requests.ConnectionError("refused")
    with pytest.raises(TTSFailure) as failure:
        XTTSEngine()._synthesize_via_http("/tts", {"text": "Hallo"})
    assert failure.value.reason == "unreachable"
    assert "http://localhost:5051" in failure.value.detail


def test_connect_timeout_is_unreachable(container: FakeContainer) -> None:
    container.reply = requests.ConnectTimeout("no answer")
    with pytest.raises(TTSFailure) as failure:
        XTTSEngine()._synthesize_via_http("/tts", {"text": "Hallo"})
    assert failure.value.reason == "unreachable"


def test_read_timeout_is_a_timeout(container: FakeContainer) -> None:
    container.reply = requests.ReadTimeout("too slow")
    with pytest.raises(TTSFailure) as failure:
        XTTSEngine()._synthesize_via_http("/tts", {"text": "Hallo"})
    assert failure.value.reason == "timeout"


@pytest.mark.parametrize("error", [requests.exceptions.ChunkedEncodingError("cut off"), requests.exceptions.ContentDecodingError("garbled")])
def test_an_answer_cut_off_by_a_dying_container_is_an_engine_failure_not_software(
    container: FakeContainer, error: Exception,
) -> None:
    container.reply = error
    with pytest.raises(TTSFailure) as failure:
        XTTSEngine()._synthesize_via_http("/tts", {"text": "Hallo"})
    assert failure.value.reason == "engine"


def test_http_error_is_an_engine_failure(container: FakeContainer) -> None:
    container.reply = _answer(status=500, text="CUDA out of memory")
    with pytest.raises(TTSFailure) as failure:
        XTTSEngine()._synthesize_via_http("/tts", {"text": "Hallo"})
    assert failure.value.reason == "engine"
    assert "HTTP 500" in failure.value.detail and "CUDA out of memory" in failure.value.detail


def test_too_small_audio_is_an_engine_failure(container: FakeContainer) -> None:
    container.reply = _answer(content=b"\x00" * 10)
    with pytest.raises(TTSFailure) as failure:
        XTTSEngine()._synthesize_via_http("/tts", {"text": "Hallo"})
    assert failure.value.reason == "engine"
    assert "no usable audio" in failure.value.detail


def test_failure_is_a_suite_error() -> None:
    from tts_suite.api import SuiteError
    assert issubclass(TTSFailure, SuiteError)


# ── What each container engine sends and delivers ──────────────────

def test_qwen3_sends_the_full_language_name(container: FakeContainer) -> None:
    assert Qwen3LocalEngine().generate_speech("Hallo", "Alice", "de") == AUDIO
    assert container.posts[0]["json"] == {"text": "Hallo", "speaker": "Alice", "language": "German"}
    assert Qwen3LocalEngine().audio_media_type == "audio/wav"


def test_xtts_delivers_ogg(container: FakeContainer) -> None:
    engine = XTTSEngine()
    assert engine.generate_speech("Hallo", "Alice", "de") == AUDIO
    assert engine.audio_media_type == "audio/ogg" and engine.audio_extension == "ogg"
    assert container.posts[-1]["json"] == {"text": "Hallo", "speaker": "Alice", "language": "de"}


def test_async_generation_runs_the_sync_call_off_the_event_loop(container: FakeContainer) -> None:
    import asyncio
    assert asyncio.run(XTTSEngine().generate_speech_async("Hallo", "Alice", "de")) == AUDIO
