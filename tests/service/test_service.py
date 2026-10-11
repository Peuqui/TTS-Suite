"""The HTTP service against a suite that does what the test says."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from service_fakes import FakeStream, FakeSuite, audio, default_config
from starlette.testclient import TestClient, WebSocketDisconnect

from tts_suite.api import (
    ConfigInvalid,
    EngineControlFailed,
    EngineInUse,
    EngineStarting,
    NoEngineAvailable,
    Overloaded,
    SpeakSummary,
    TraceStep,
    UnknownEngine,
)
from tts_suite.i18n import UnsupportedLanguage
from tts_suite.service.app import create_app
from tts_suite.service.auth import Access, access_for, create_token, read_token
from tts_suite.service.config_store import ConfigStore
from tts_suite.service.frames import FRAMES_MEDIA_TYPE, decode_frames

TOKEN = "secret-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def build(tmp_path: Path, token_for_everything: bool = False) -> tuple[TestClient, FakeSuite, ConfigStore]:
    suite = FakeSuite(default_config(tmp_path))
    store = ConfigStore(tmp_path / "state" / "config.json")
    app = create_app(suite, Access(token=TOKEN, token_for_everything=token_for_everything), store)
    return TestClient(app), suite, store


def test_health_and_the_lists_ask_for_nothing_on_this_machine(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    engines = client.get("/engines").json()
    assert engines[0]["engine"] == "xtts" and engines[0]["vram_mib"] == {"GPU-1": 4000}
    assert client.get("/voices").json() == [{"name": "Alice", "label": "Alice"}]
    assert client.get("/config").json()["voices_dir"] == str(tmp_path / "voices")


def test_the_engine_of_the_voices_goes_to_the_suite(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    client.get("/voices", params={"engine": "edge"})
    assert suite.calls == [("voices", ("edge",))]


def test_a_command_that_changes_something_needs_the_token_also_here(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    commands = [
        ("POST", "/engines/xtts/start"),
        ("POST", "/engines/xtts/stop"),
        ("POST", "/engines/reload"),
        ("POST", "/voices/sync?engine=dashscope"),
        ("POST", "/config"),
    ]
    for method, path in commands:
        refused = client.request(method, path)
        assert refused.status_code == 401 and refused.json()["error"] == "unauthorized", path
        wrong = client.request(method, path, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401, path
    assert suite.calls == []


def test_the_commands_work_with_the_token(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    assert client.post("/engines/xtts/start", params={"host": "aragon"}, headers=BEARER).status_code == 204
    assert client.post("/engines/xtts/stop", headers=BEARER).status_code == 204
    assert client.post("/engines/reload", headers=BEARER).json() == {"removed": ["old"], "added": ["new"]}
    assert client.post("/voices/sync", params={"engine": "dashscope"}, headers=BEARER).status_code == 200
    assert suite.calls == [
        ("start", ("xtts", "aragon")),
        ("stop", ("xtts", None)),
        ("reload", ()),
        ("sync_voices", ("dashscope",)),
    ]


def test_without_a_token_configured_the_commands_stay_locked(tmp_path: Path) -> None:
    suite = FakeSuite(default_config(tmp_path))
    app = create_app(suite, Access(token=None, token_for_everything=False), ConfigStore(tmp_path / "c.json"))
    refused = TestClient(app).post("/engines/reload")
    assert refused.status_code == 401 and "none is configured" in refused.json()["detail"]


def test_on_a_network_everything_needs_the_token(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path, token_for_everything=True)
    assert client.get("/health").status_code == 200
    assert client.get("/engines").status_code == 401
    assert client.get("/engines", headers=BEARER).status_code == 200
    assert client.post("/speak", json={"text": "Hi", "language": "en"}).status_code == 401


def test_a_new_configuration_is_set_and_kept(tmp_path: Path) -> None:
    client, suite, store = build(tmp_path)
    wanted = {
        "voices_dir": str(tmp_path / "other"),
        "entries": [{"engine": "xtts", "host": None, "enabled": True}],
        "gpus": [{"gpu": "0", "reserve_mib": 2048}],
        "pronunciations": {"AIfred": "Alfred"},
    }
    answer = client.post("/config", json=wanted, headers=BEARER)
    assert answer.status_code == 200
    assert answer.json()["gpus"] == [{"gpu": "0", "reserve_mib": 2048}]
    saved = store.load()
    assert saved is not None and saved.voices_dir == tmp_path / "other"
    assert saved.pronunciations == {"AIfred": "Alfred"}
    assert suite._config == saved


def test_a_configuration_the_suite_refuses_is_not_kept(tmp_path: Path) -> None:
    client, suite, store = build(tmp_path)
    suite.rejects_config = ConfigInvalid("no engine 'nope'")
    answer = client.post("/config", json={"voices_dir": str(tmp_path)}, headers=BEARER)
    assert answer.status_code == 400
    assert answer.json() == {"error": "config_invalid", "detail": "no engine 'nope'"}
    assert store.load() is None


def test_a_configuration_that_makes_no_sense_is_a_bad_request(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path)
    assert client.post("/config", json={"entries": "xtts"}, headers=BEARER).json()["error"] == "bad_request"
    assert client.post("/config", content=b"{", headers=BEARER).json()["error"] == "bad_request"
    assert client.post("/config", json=[1], headers=BEARER).status_code == 400


def test_errors_of_the_suite_have_their_status_and_code(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    cases = [
        (UnknownEngine("nope"), 404, "unknown_engine"),
        (Overloaded("queue full"), 429, "overloaded"),
        (EngineInUse("in use"), 409, "engine_in_use"),
        (EngineControlFailed("docker failed"), 502, "engine_control_failed"),
        (UnsupportedLanguage("xx"), 422, "unsupported_language"),
    ]
    for error, status, code in cases:
        suite.refuses = error
        answer = client.post("/speak", json={"text": "Hi", "language": "en"})
        assert (answer.status_code, answer.json()["error"]) == (status, code)


def test_a_start_that_goes_on_says_when_to_ask_again(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([EngineStarting("xtts is starting")])
    answer = client.post("/speak", json={"text": "Hi", "language": "en", "wait": False})
    assert answer.status_code == 503 and answer.json()["error"] == "starting"
    assert answer.headers["retry-after"] == "5"
    assert suite.stream.closed


def test_no_engine_to_speak_gives_the_course_of_the_escalation(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    trace = (TraceStep(entry="xtts", outcome="skipped", reason="no_vram"),)
    suite.stream = FakeStream([NoEngineAvailable("nobody can speak", trace)])
    answer = client.post("/speak", json={"text": "Hi", "language": "en"})
    assert answer.status_code == 503
    assert answer.json()["trace"][0] == {"entry": "xtts", "outcome": "skipped", "reason": "no_vram", "detail": ""}


def test_a_reading_comes_back_as_frames_in_order_with_the_summary(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), audio(engine="edge@aragon", data=b"two"), audio("", b"", True)])
    answer = client.post("/speak", json={"text": "Hello world.", "language": "en", "voice": "Alice"})
    assert answer.status_code == 200
    assert answer.headers["content-type"] == FRAMES_MEDIA_TYPE
    frames = decode_frames(answer.content)
    assert [(header["kind"], body) for header, body in frames[:3]] == [
        ("audio", b"one"),
        ("audio", b"two"),
        ("audio", b""),
    ]
    assert frames[1][0]["engine"] == "edge@aragon" and frames[2][0]["keepalive"] is True
    assert frames[0][0]["media_type"] == "audio/wav" and frames[0][0]["bytes"] == 3
    summary_header, summary_body = frames[3]
    assert summary_header["kind"] == "summary"
    assert json.loads(summary_body) == {
        "trace": [{"entry": "xtts", "outcome": "spoke", "reason": None, "detail": ""}],
        "switch": None,
    }
    text, request = suite.requests[0]
    assert text == "Hello world." and request.language == "en" and request.voice == "Alice"
    assert suite.stream.closed


def test_the_head_may_carry_the_voices_of_the_engines_and_the_run(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    head = {
        "text": "Hi",
        "language": "de",
        "run": "r-42",
        "wait": False,
        "engine": "edge",
        "voices": {"edge": {"voice": "de-DE-KatjaNeural", "speed": 1.1}},
    }
    assert client.post("/speak", json=head).status_code == 200
    _, request = suite.requests[0]
    assert (request.run, request.wait, request.engine) == ("r-42", False, "edge")
    assert request.voices["edge"].voice == "de-DE-KatjaNeural" and request.voices["edge"].speed == 1.1


def test_a_failure_after_the_first_frame_ends_the_stream_with_an_error_frame(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), NoEngineAvailable("all failed", ())])
    answer = client.post("/speak", json={"text": "Hi", "language": "en"})
    assert answer.status_code == 200
    frames = decode_frames(answer.content)
    assert [header["kind"] for header, _ in frames] == ["audio", "error"]
    assert json.loads(frames[1][1])["error"] == "no_engine"
    assert suite.stream.closed


def test_a_reading_without_audio_still_ends_with_its_summary(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([], SpeakSummary(switch="engine"))
    frames = decode_frames(client.post("/speak", json={"text": "", "language": "en"}).content)
    assert [header["kind"] for header, _ in frames] == ["summary"]
    assert json.loads(frames[0][1])["switch"] == "engine"


def test_a_request_without_text_or_language_is_a_bad_request(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    for body in ({"language": "en"}, {"text": "Hi"}, {"text": 5, "language": "en"}):
        answer = client.post("/speak", json=body)
        assert (answer.status_code, answer.json()["error"]) == (400, "bad_request")
    assert suite.requests == []


def test_a_stream_over_the_websocket_gets_its_text_in_pieces(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), audio(data=b"two")])
    with client.websocket_connect("/speak/stream") as socket:
        socket.send_text(json.dumps({"type": "start", "language": "en", "run": "r-1"}))
        socket.send_text(json.dumps({"type": "text", "text": "First "}))
        socket.send_text(json.dumps({"type": "text", "text": "sentence."}))
        socket.send_text(json.dumps({"type": "finish"}))
        frames = decode_frames(b"".join(socket.receive_bytes() for _ in range(3)))
    assert [header["kind"] for header, _ in frames] == ["audio", "audio", "summary"]
    assert suite.stream.fed == ["First ", "sentence."] and suite.stream.finished
    assert suite.requests[0][1].run == "r-1"
    assert suite.stream.closed


def test_a_stream_that_cannot_start_ends_with_an_error_frame(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.refuses = UnknownEngine("nope")
    with client.websocket_connect("/speak/stream") as socket:
        socket.send_text(json.dumps({"type": "start", "language": "en", "engine": "nope"}))
        header, body = decode_frames(socket.receive_bytes())[0]
    assert header["kind"] == "error" and json.loads(body)["error"] == "unknown_engine"


def test_the_first_message_of_a_stream_must_be_its_start(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    for first in ('{"type": "text", "text": "Hi"}', "no json", '{"type": "start"}'):
        with client.websocket_connect("/speak/stream") as socket:
            socket.send_text(first)
            header, body = decode_frames(socket.receive_bytes())[0]
        assert header["kind"] == "error" and json.loads(body)["error"] == "bad_request", first
    assert suite.requests == []


def test_a_stream_refuses_an_unknown_message_and_releases_the_suite(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio()])
    with client.websocket_connect("/speak/stream") as socket:
        socket.send_text(json.dumps({"type": "start", "language": "en"}))
        socket.send_text(json.dumps({"type": "bogus"}))
        kinds = []
        while True:
            try:
                kinds.append(decode_frames(socket.receive_bytes())[0][0]["kind"])
            except WebSocketDisconnect:
                break
    assert "error" in kinds
    assert suite.stream.closed


def test_a_stream_on_a_network_needs_the_token(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path, token_for_everything=True)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/speak/stream"):
        pass
    with client.websocket_connect("/speak/stream", headers=BEARER) as socket:
        socket.send_text(json.dumps({"type": "start", "language": "en"}))
        socket.send_text(json.dumps({"type": "finish"}))
        assert decode_frames(socket.receive_bytes())[0][0]["kind"] == "audio"


def test_the_suite_is_closed_when_the_service_ends(tmp_path: Path) -> None:
    suite = FakeSuite(default_config(tmp_path))
    app = create_app(suite, Access(token=TOKEN, token_for_everything=False), ConfigStore(tmp_path / "c.json"))
    with TestClient(app):
        assert not suite.closed
    assert suite.closed


def test_the_configuration_survives_a_restart(tmp_path: Path) -> None:
    client, _, store = build(tmp_path)
    client.post("/config", json={"voices_dir": str(tmp_path / "v"), "headroom_mib": 256}, headers=BEARER)
    again = ConfigStore(store.path).load()
    assert again is not None and again.headroom_mib == 256
    assert list(store.path.parent.iterdir()) == [store.path]


def test_an_open_service_on_a_network_does_not_start_without_a_token() -> None:
    with pytest.raises(ValueError, match="needs a token"):
        access_for("0.0.0.0", None)
    assert access_for("127.0.0.1", None).token_for_everything is False
    assert access_for("::1", None).token_for_everything is False
    assert access_for("10.0.0.2", "t").token_for_everything is True


def test_the_token_is_made_once_for_the_user_alone(tmp_path: Path) -> None:
    path = tmp_path / "tts" / "token"
    assert read_token(path) is None
    token = create_token(path)
    assert len(token) >= 40 and read_token(path) == token
    assert path.stat().st_mode & 0o777 == 0o600
    assert create_token(path) == token
