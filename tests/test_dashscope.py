"""DashScope Qwen-Audio 3: voice catalog, model choice, error mapping, voice enrollment."""
from __future__ import annotations

import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from tts_suite.constants import DASHSCOPE_API_KEY_ENV, DASHSCOPE_MAPPING_FILE_NAME, DATA_DIR_ENV
from tts_suite.engines import TTS_ENGINES, TTSFailure, get_engine
from tts_suite.engines.dashscope_audio3 import engine as dashscope_audio3
from tts_suite.engines.dashscope_audio3.enroll import enroll_progress, load_mapping


def _wav_bytes(sample_rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x01\x00" * sample_rate)
    return buffer.getvalue()


class FakeCloud:
    def __init__(self, post_reply: SimpleNamespace | Exception) -> None:
        self.post_reply = post_reply
        self.posts: list[dict[str, Any]] = []
        self.downloads: list[str] = []

    def post(self, url: str, **kwargs: Any) -> SimpleNamespace:
        self.posts.append({"url": url, **kwargs})
        if isinstance(self.post_reply, Exception):
            raise self.post_reply
        return self.post_reply

    def get(self, url: str, **kwargs: Any) -> SimpleNamespace:
        self.downloads.append(url)
        return SimpleNamespace(content=_wav_bytes(), raise_for_status=lambda: None)


def _ok_reply() -> SimpleNamespace:
    return SimpleNamespace(
        ok=True, status_code=200, text="",
        json=lambda: {"output": {"audio": {"url": "http://result.example/audio.wav"}}},
    )


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    fake = FakeCloud(_ok_reply())
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setenv(DASHSCOPE_API_KEY_ENV, "key")
    return fake


@pytest.fixture(autouse=True)
def data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The data folder of the suite (holds the voice mapping) — a folder that does not exist yet."""
    folder = tmp_path / "data"
    monkeypatch.setenv(DATA_DIR_ENV, str(folder))
    return folder


@pytest.fixture
def engine(tmp_path: Path) -> dashscope_audio3.DashScopeAudio3Engine:
    registered = get_engine("dashscope_audio3")
    assert isinstance(registered, dashscope_audio3.DashScopeAudio3Engine)
    return registered.with_voices_dir(tmp_path)


def test_registered_as_the_dashscope_engine() -> None:
    assert "dashscope_audio3" in TTS_ENGINES
    assert "dashscope" not in TTS_ENGINES


def test_flash_voice_uses_the_flash_model_and_https_download(cloud: FakeCloud, engine: Any) -> None:
    audio = engine.generate_speech("Hallo", "Mary", "de")
    body = cloud.posts[0]["json"]
    assert (body["model"], body["input"]["voice"], body["input"]["language_type"]) == (
        "qwen-audio-3.0-tts-flash", "loongmary", "German")
    assert cloud.posts[0]["headers"] == {"Authorization": "Bearer key"}
    assert cloud.downloads == ["https://result.example/audio.wav"]
    with wave.open(io.BytesIO(audio)) as wav:  # bytes of a valid WAV
        assert wav.getframerate() == 24000


def test_flagship_voice_needs_the_plus_model(cloud: FakeCloud, engine: Any) -> None:
    engine.generate_speech("Hallo", "Ling Xin", "de")
    assert cloud.posts[0]["json"]["model"] == "qwen-audio-3.0-tts-plus"


def test_unknown_voice_is_an_engine_failure(cloud: FakeCloud, engine: Any) -> None:
    with pytest.raises(TTSFailure) as failure:
        engine.generate_speech("Hallo", "Bella", "de")
    assert failure.value.reason == "engine"
    assert cloud.posts == []


def test_missing_api_key_is_an_engine_failure(cloud: FakeCloud, engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DASHSCOPE_API_KEY_ENV)
    assert not engine.is_running()
    with pytest.raises(TTSFailure) as failure:
        engine.generate_speech("Hallo", "Mary", "de")
    assert failure.value.reason == "engine" and DASHSCOPE_API_KEY_ENV in failure.value.detail


def test_http_error_is_an_engine_failure(cloud: FakeCloud, engine: Any) -> None:
    cloud.post_reply = SimpleNamespace(ok=False, status_code=400, text="InvalidParameter")
    with pytest.raises(TTSFailure) as failure:
        engine.generate_speech("Hallo", "Mary", "de")
    assert failure.value.reason == "engine"
    assert "400" in failure.value.detail


def test_too_many_requests_is_cloud_quota(cloud: FakeCloud, engine: Any) -> None:
    cloud.post_reply = SimpleNamespace(ok=False, status_code=429, text="Throttling.RateQuota")
    with pytest.raises(TTSFailure) as failure:
        engine.generate_speech("Hallo", "Mary", "de")
    assert failure.value.reason == "cloud_quota" and "429" in failure.value.detail


def test_a_cloud_that_takes_too_long_is_a_timeout_and_a_connect_timeout_is_unreachable(cloud: FakeCloud, engine: Any) -> None:
    for error, reason in ((requests.ReadTimeout("slow"), "timeout"), (requests.ConnectTimeout("no route"), "unreachable")):
        cloud.post_reply = error
        with pytest.raises(TTSFailure) as failure:
            engine.generate_speech("Hallo", "Mary", "de")
        assert failure.value.reason == reason


def test_a_download_cut_off_midway_is_an_engine_failure(cloud: FakeCloud, engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(url: str, **kwargs: Any) -> SimpleNamespace:
        raise requests.exceptions.ChunkedEncodingError("cut off")

    monkeypatch.setattr(requests, "get", broken)
    with pytest.raises(TTSFailure) as failure:
        engine.generate_speech("Hallo", "Mary", "de")
    assert failure.value.reason == "engine"


def test_network_outage_is_unreachable(cloud: FakeCloud, engine: Any) -> None:
    cloud.post_reply = requests.ConnectionError("no route")
    with pytest.raises(TTSFailure) as failure:
        engine.generate_speech("Hallo", "Mary", "de")
    assert failure.value.reason == "unreachable"


def test_default_voice_is_in_the_catalog(engine: Any) -> None:
    assert engine.default_voice in engine.get_voices()


def test_cloned_voices_come_first_and_use_the_flash_model(cloud: FakeCloud, engine: Any, data_folder: Path) -> None:
    data_folder.mkdir()
    (data_folder / DASHSCOPE_MAPPING_FILE_NAME).write_text(
        json.dumps({"Codine": {"voice_id": "qwen-audio-3.0-tts-flash-codine-1"}}), encoding="utf-8",
    )
    assert next(iter(engine.get_voices())) == "★ Codine"
    # With and without the ★ prefix (it may be stripped before the engine sees it).
    for voice in ("★ Codine", "Codine"):
        engine.generate_speech("Hallo", voice, "de")
        body = cloud.posts[-1]["json"]
        assert (body["model"], body["input"]["voice"]) == ("qwen-audio-3.0-tts-flash", "qwen-audio-3.0-tts-flash-codine-1")


# ── Enrollment ─────────────────────────────────────────────────────

@pytest.fixture
def voices(tmp_path: Path) -> Path:
    voices_dir = tmp_path / "voices"
    voice_dir = voices_dir / "Codine"
    voice_dir.mkdir(parents=True)
    (voice_dir / "Codine.wav").write_bytes(_wav_bytes(16000))
    return voices_dir


def _capture_post(monkeypatch: pytest.MonkeyPatch, output: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> SimpleNamespace:
        sent.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=200, text="", json=lambda: {"output": output})

    monkeypatch.setattr(requests, "post", fake_post)
    return sent


def test_request_follows_the_cosyvoice_variant(voices: Path, data_folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture_post(monkeypatch, {"voice_id": "qwen-audio-3.0-tts-flash-codine-1"})
    lines = list(enroll_progress("key", voices))
    body = sent[0]["json"]
    assert body["model"] == "voice-enrollment"
    assert body["input"]["action"] == "create_voice"
    assert body["input"]["target_model"] == "qwen-audio-3.0-tts-flash"
    assert body["input"]["prefix"] == "codine"
    assert body["input"]["url"].startswith("data:audio/wav;base64,")
    assert load_mapping()["Codine"]["voice_id"] == "qwen-audio-3.0-tts-flash-codine-1"
    assert (data_folder / DASHSCOPE_MAPPING_FILE_NAME).exists()  # the folder was created on the first save
    assert not (voices / DASHSCOPE_MAPPING_FILE_NAME).exists()  # nothing is written into the voices tree
    assert "DashScope enroll done: 1 new" in lines[-1]


def test_an_enrolled_unchanged_voice_is_not_uploaded_again(voices: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture_post(monkeypatch, {"voice_id": "v1"})
    list(enroll_progress("key", voices))
    list(enroll_progress("key", voices))
    assert len(sent) == 1


def test_a_response_without_voice_id_is_a_failure(voices: Path, data_folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_post(monkeypatch, {})
    lines = list(enroll_progress("key", voices))
    assert any("failed" in line for line in lines)
    assert not (data_folder / DASHSCOPE_MAPPING_FILE_NAME).exists()


def test_prepare_voices_needs_the_key(voices: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture_post(monkeypatch, {"voice_id": "v1"})
    registered = get_engine("dashscope_audio3")
    assert registered is not None
    bound = registered.with_voices_dir(voices)
    monkeypatch.delenv(DASHSCOPE_API_KEY_ENV, raising=False)
    assert list(bound.prepare_voices()) == []
    monkeypatch.setenv(DASHSCOPE_API_KEY_ENV, "key")
    assert "DashScope enroll done: 1 new" in list(bound.prepare_voices())[-1]
    assert len(sent) == 1
