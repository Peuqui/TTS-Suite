"""The HTTP service against a suite that does what the test says."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from service_fakes import FakeStream, FakeSuite, audio, default_config
from starlette.testclient import TestClient, WebSocketDisconnect

from tts_suite.api import (
    AudioSegment,
    ConfigInvalid,
    EngineControlFailed,
    EngineInUse,
    EngineRef,
    EngineStarting,
    EngineTarget,
    GpuReleaseOutcome,
    GpuReleaseResult,
    InvalidRequest,
    NoEngineAvailable,
    Overloaded,
    ReadingAborted,
    SpeakSummary,
    TraceStep,
    UnknownEngine,
    UnknownHost,
)
from tts_suite.i18n import UnsupportedLanguage
from tts_suite.service.app import create_app
from tts_suite.service.auth import Access, access_for, create_token, read_token
from tts_suite.service.config_store import ConfigStore
from tts_suite.service.frames import FRAMES_MEDIA_TYPE, decode_frames

TOKEN = "secret-token"
TOKEN_HEADERS = {"X-TTS-Suite-Token": TOKEN}


MAX_TEXT_CHARS = 100
ENGINES = [{"engine": "xtts", "unit": "sentence", "host": None}]  # the list every request brings


def build(tmp_path: Path, token_for_everything: bool = False) -> tuple[TestClient, FakeSuite, ConfigStore]:
    suite = FakeSuite(default_config(tmp_path))
    store = ConfigStore(tmp_path / "state" / "config.json")
    app = create_app(suite, Access(token=TOKEN, token_for_everything=token_for_everything), store, MAX_TEXT_CHARS)
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
        ("POST", "/voices/push"),
        ("POST", "/config"),
    ]
    for method, path in commands:
        refused = client.request(method, path)
        assert refused.status_code == 401 and refused.json()["error"] == "unauthorized", path
        wrong = client.request(method, path, headers={"X-TTS-Suite-Token": "nope"})
        assert wrong.status_code == 401, path
    assert suite.calls == []


def test_the_commands_work_with_the_token(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    assert client.post("/engines/xtts/start", params={"host": "aragon"}, headers=TOKEN_HEADERS).status_code == 204
    assert client.post("/engines/xtts/start", params={"gpu": "4"}, headers=TOKEN_HEADERS).status_code == 204
    assert client.post("/engines/xtts/stop", headers=TOKEN_HEADERS).status_code == 204
    assert client.post("/engines/reload", headers=TOKEN_HEADERS).json() == {"removed": ["old"], "added": ["new"]}
    assert client.post("/voices/sync", params={"engine": "dashscope"}, headers=TOKEN_HEADERS).status_code == 200
    assert client.post("/voices/push", headers=TOKEN_HEADERS).json() == {"hosts": ["Aragon"]}
    assert suite.calls == [
        ("start", ("xtts", "aragon", None)),
        ("start", ("xtts", None, "4")),
        ("stop", ("xtts", None)),
        ("reload", ()),
        ("sync_voices", ("dashscope",)),
        ("push_voices", ()),
    ]


def test_without_a_token_configured_the_commands_stay_locked(tmp_path: Path) -> None:
    suite = FakeSuite(default_config(tmp_path))
    app = create_app(
        suite, Access(token=None, token_for_everything=False), ConfigStore(tmp_path / "c.json"), MAX_TEXT_CHARS
    )
    refused = TestClient(app).post("/engines/reload")
    assert refused.status_code == 401 and "none is configured" in refused.json()["detail"]


def test_on_a_network_everything_needs_the_token(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path, token_for_everything=True)
    assert client.get("/health").status_code == 200
    assert client.get("/engines").status_code == 401
    assert client.get("/engines", headers=TOKEN_HEADERS).status_code == 200
    assert client.post("/speak", json={"text": "Hi", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0}).status_code == 401


SPEAK_TEST = {
    "text": "Hallo Welt.", "language": "de", "pause_ms": 700, "order": ["edge@", "xtts@Aragon"],
    "use": {"edge@": True, "xtts@Aragon": False}, "units": {"edge@": "sentence", "xtts@Aragon": "whole"},
    "voices": {"edge": "Deutsch (Katja)"},
}


def test_the_test_reading_of_the_page_is_kept_on_the_server_for_every_device(tmp_path: Path) -> None:
    client, _, store = build(tmp_path)
    assert client.get("/speak-test").json() == {}                                       # nothing saved yet
    assert client.put("/speak-test", json=SPEAK_TEST).status_code == 401                # a change needs the token
    assert client.put("/speak-test", json=SPEAK_TEST, headers=TOKEN_HEADERS).json() == SPEAK_TEST
    assert client.get("/speak-test").json() == SPEAK_TEST                                # another device reads it
    again, _, _ = build(tmp_path)                                                       # and it survives a restart
    assert again.get("/speak-test").json() == SPEAK_TEST
    assert (store.path.parent / "speak-test.json").is_file()


def test_a_test_reading_that_is_no_valid_state_is_refused(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path)
    for wrong in ({**SPEAK_TEST, "pause_ms": "much"}, {**SPEAK_TEST, "units": {"edge@": "line"}}, {"text": "x"}):
        answer = client.put("/speak-test", json=wrong, headers=TOKEN_HEADERS)
        assert answer.status_code == 400 and answer.json()["error"] == "bad_request"
    assert client.get("/speak-test").json() == {}


def test_a_new_configuration_is_set_and_kept(tmp_path: Path) -> None:
    client, suite, store = build(tmp_path)
    wanted = {
        "voices_dir": str(tmp_path / "other"),
        "gpu_mode": "list",
        "gpus": [{"gpu": "0", "reserve_mib": 2048}],
        "pronunciations": {"AIfred": "Alfred"},
    }
    answer = client.post("/config", json=wanted, headers=TOKEN_HEADERS)
    assert answer.status_code == 200
    assert answer.json()["gpus"] == [{"gpu": "0", "reserve_mib": 2048}]
    saved = store.load()
    assert saved is not None and saved.voices_dir == tmp_path / "other"
    assert saved.pronunciations == {"AIfred": "Alfred"}
    assert suite._config == saved


def test_a_configuration_the_suite_refuses_is_not_kept(tmp_path: Path) -> None:
    client, suite, store = build(tmp_path)
    suite.rejects_config = ConfigInvalid("no engine 'nope'")
    answer = client.post("/config", json={"voices_dir": str(tmp_path)}, headers=TOKEN_HEADERS)
    assert answer.status_code == 400
    assert answer.json() == {"error": "config_invalid", "detail": "no engine 'nope'"}
    assert store.load() is None


def test_a_configuration_that_makes_no_sense_is_a_bad_request(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path)
    assert client.post("/config", json={"hosts": "aragon"}, headers=TOKEN_HEADERS).json()["error"] == "bad_request"
    assert client.post("/config", content=b"{", headers=TOKEN_HEADERS).json()["error"] == "bad_request"
    assert client.post("/config", json=[1], headers=TOKEN_HEADERS).status_code == 400


def test_errors_of_the_suite_have_their_status_and_code(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    cases = [
        (UnknownEngine("nope"), 404, "unknown_engine"),
        (UnknownHost("nope"), 404, "unknown_host"),
        (InvalidRequest("empty list"), 422, "invalid_request"),
        (Overloaded("queue full"), 429, "overloaded"),
        (EngineInUse("in use"), 409, "engine_in_use"),
        (EngineControlFailed("docker failed"), 502, "engine_control_failed"),
        (UnsupportedLanguage("xx"), 422, "unsupported_language"),
    ]
    for error, status, code in cases:
        suite.refuses = error
        answer = client.post("/speak", json={"text": "Hi", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0})
        assert (answer.status_code, answer.json()["error"]) == (status, code)


def test_a_start_that_goes_on_says_when_to_ask_again(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([EngineStarting("xtts is starting")])
    answer = client.post("/speak", json={"text": "Hi", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0, "wait": False})
    assert answer.status_code == 503 and answer.json()["error"] == "starting"
    assert answer.headers["retry-after"] == "5"
    assert suite.stream.closed


def test_no_engine_to_speak_gives_the_course_of_the_escalation(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    trace = (TraceStep(entry="xtts", outcome="skipped", reason="no_vram"),)
    suite.stream = FakeStream([NoEngineAvailable("nobody can speak", trace)])
    answer = client.post("/speak", json={"text": "Hi", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0})
    assert answer.status_code == 503
    assert answer.json()["trace"][0] == {"entry": "xtts", "outcome": "skipped", "reason": "no_vram", "detail": ""}


def test_a_reading_comes_back_as_frames_in_order_with_the_summary(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), audio(engine="edge@aragon", data=b"two"), audio("", b"", True)])
    answer = client.post(
        "/speak", json={"text": "Hello world.", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0, "voice": "Alice"}
    )
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


def test_a_pause_marker_is_an_audio_frame_without_body_that_carries_the_pause(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), AudioSegment(audio=b"", media_type="", engine="", pause_ms=700), audio(data=b"two")])
    frames = decode_frames(client.post("/speak", json={"text": "Hi", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 700}).content)
    assert [(header["kind"], header["pause_ms"], header["keepalive"], header["bytes"]) for header, _ in frames[:3]] == [
        ("audio", 0, False, 3), ("audio", 700, False, 0), ("audio", 0, False, 3),
    ]
    assert (frames[1][0]["media_type"], frames[1][0]["engine"]) == ("", "")


def test_the_head_may_carry_the_voices_of_the_engines_and_the_run(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    head = {
        "text": "Hi",
        "language": "de",
        "run": "r-42",
        "wait": False,
        "engines": [{"engine": "edge", "unit": "paragraph", "host": "aragon"}],
        "paragraph_pause_ms": 700,
        "voices": {"edge": {"voice": "de-DE-KatjaNeural", "speed": 1.1}},
    }
    assert client.post("/speak", json=head).status_code == 200
    _, request = suite.requests[0]
    assert (request.run, request.wait, request.engines) == ("r-42", False, (EngineRef("edge", "paragraph", "aragon"),))
    assert request.voices["edge"].voice == "de-DE-KatjaNeural" and request.voices["edge"].speed == 1.1
    assert request.paragraph_pause_ms == 700


def test_a_failure_after_the_first_frame_ends_the_stream_with_an_error_frame(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), NoEngineAvailable("all failed", ())])
    answer = client.post("/speak", json={"text": "Hi", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0})
    assert answer.status_code == 200
    frames = decode_frames(answer.content)
    assert [header["kind"] for header, _ in frames] == ["audio", "error"]
    assert json.loads(frames[1][1])["error"] == "no_engine"
    assert suite.stream.closed


def test_a_reading_without_audio_still_ends_with_its_summary(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([], SpeakSummary(switch="engine"))
    frames = decode_frames(client.post("/speak", json={"text": "", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0}).content)
    assert [header["kind"] for header, _ in frames] == ["summary"]
    assert json.loads(frames[0][1])["switch"] == "engine"


def test_a_request_without_text_or_language_is_a_bad_request(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    for body in (
        {"language": "en", "engines": ENGINES, "paragraph_pause_ms": 0},
        {"text": "Hi", "engines": ENGINES, "paragraph_pause_ms": 0},
        {"text": 5, "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0},
        {"text": "Hi", "language": "en", "paragraph_pause_ms": 0},
        {"text": "Hi", "language": "en", "engines": ENGINES},  # the pause is not optional
        {"text": "Hi", "language": "en", "engines": [{"engine": "xtts", "unit": "sentence"}], "paragraph_pause_ms": 0},
    ):
        answer = client.post("/speak", json=body)
        assert (answer.status_code, answer.json()["error"]) == (400, "bad_request")
    assert suite.requests == []


def test_a_stream_over_the_websocket_gets_its_text_in_pieces(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), audio(data=b"two")])
    with client.websocket_connect("/speak/stream") as socket:
        socket.send_text(json.dumps({"type": "start", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0, "run": "r-1"}))
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
        socket.send_text(
            json.dumps(
                {"type": "start", "language": "en", "engines": [{"engine": "nope", "unit": "sentence", "host": None}], "paragraph_pause_ms": 0}
            )
        )
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
        socket.send_text(json.dumps({"type": "start", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0}))
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
    with client.websocket_connect("/speak/stream", headers=TOKEN_HEADERS) as socket:
        socket.send_text(json.dumps({"type": "start", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0}))
        socket.send_text(json.dumps({"type": "finish"}))
        assert decode_frames(socket.receive_bytes())[0][0]["kind"] == "audio"


def test_the_suite_is_closed_when_the_service_ends(tmp_path: Path) -> None:
    suite = FakeSuite(default_config(tmp_path))
    app = create_app(
        suite, Access(token=TOKEN, token_for_everything=False), ConfigStore(tmp_path / "c.json"), MAX_TEXT_CHARS
    )
    with TestClient(app):
        assert not suite.closed
    assert suite.closed


def test_the_configuration_survives_a_restart(tmp_path: Path) -> None:
    client, _, store = build(tmp_path)
    client.post("/config", json={"voices_dir": str(tmp_path / "v"), "headroom_mib": 256}, headers=TOKEN_HEADERS)
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


def test_a_text_beyond_the_limit_is_refused_before_anything_is_made(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    answer = client.post("/speak", json={"text": "x" * (MAX_TEXT_CHARS + 1), "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0})
    assert (answer.status_code, answer.json()["error"]) == (413, "text_too_long")
    assert suite.requests == []
    assert (
        client.post("/speak", json={"text": "x" * MAX_TEXT_CHARS, "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0}).status_code
        == 200
    )


def test_the_text_of_a_stream_counts_over_all_its_pieces(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio()])
    with client.websocket_connect("/speak/stream") as socket:
        socket.send_text(json.dumps({"type": "start", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0}))
        for _ in range(3):
            socket.send_text(json.dumps({"type": "text", "text": "y" * 40}))
        kinds = []
        while True:
            try:
                header, body = decode_frames(socket.receive_bytes())[0]
            except WebSocketDisconnect:
                break
            kinds.append((header["kind"], json.loads(body)["error"] if header["kind"] == "error" else None))
    assert ("error", "text_too_long") in kinds
    assert suite.stream.closed


def test_the_page_of_the_service_is_served_without_a_token_with_a_strict_policy(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path, token_for_everything=True)
    page = client.get("/ui/")
    assert page.status_code == 200 and "TTS-Suite" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-cache"
    script = client.get("/ui/app.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert client.get("/ui/frames.js").status_code == 200
    # What the page asks for is not served without the token on a network.
    assert client.get("/engines").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui/"


# ── Release on request ──────────────────────────────────────────────


def test_release_takes_both_forms_of_the_targets_without_a_token_on_this_machine(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    everything = client.post("/release", json={"targets": "all"})
    assert everything.status_code == 200
    assert everything.json() == [{"engine": "xtts", "host": None, "outcome": "stopped", "detail": "xtts stopped"}]
    assert client.post("/release", json={"targets": [{"engine": "xtts", "host": None}]}).status_code == 200
    assert suite.calls == [("release", ("all",)), ("release", ((EngineTarget("xtts", None),),))]


def test_release_asks_for_the_token_where_everything_does(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path, token_for_everything=True)
    assert client.post("/release", json={"targets": "all"}).status_code == 401
    assert client.post("/release", json={"targets": "all"}, headers=TOKEN_HEADERS).status_code == 200
    assert suite.calls == [("release", ("all",))]


@pytest.mark.parametrize(
    "body",
    [{}, {"targets": "some"}, {"targets": [{"engine": "xtts"}]}],
)
def test_release_refuses_a_malformed_body(tmp_path: Path, body: dict[str, object]) -> None:
    client, suite, _ = build(tmp_path)
    assert client.post("/release", json=body).status_code == 400
    assert suite.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [(InvalidRequest("empty"), 422, "invalid_request"), (UnknownEngine("x"), 404, "unknown_engine"), (UnknownHost("x"), 404, "unknown_host")],
)
def test_release_answers_the_errors_of_the_suite_like_speak(tmp_path: Path, error: Exception, status: int, code: str) -> None:
    client, suite, _ = build(tmp_path)
    suite.refuses = error  # type: ignore[assignment]
    refused = client.post("/release", json={"targets": []})
    assert refused.status_code == status and refused.json()["error"] == code


# ── The common GPU release API ──────────────────────────────────────


def test_gpu_holders_without_a_token_on_this_machine(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path)
    answer = client.get("/gpu/holders")
    assert answer.status_code == 200
    assert answer.json() == {"holders": [{"item": "xtts", "gpu_uuids": ["GPU-a"], "memory_mib": {"GPU-a": 4000}, "busy": False}]}


def test_gpu_needs_without_a_token_on_this_machine(tmp_path: Path) -> None:
    client, _, _ = build(tmp_path)
    answer = client.get("/gpu/needs")
    assert answer.status_code == 200
    assert answer.json() == {"needs": [{"item": "xtts", "need_mib": 3680, "source": "estimate"}]}


@pytest.mark.parametrize(
    ("outcomes", "status"),
    [(("released", "not_running"), 200), (("released", "busy"), 409), (("busy", "failed"), 500)],
)
def test_gpu_release_answers_with_the_status_of_its_results(
    tmp_path: Path, outcomes: tuple[GpuReleaseOutcome, ...], status: int
) -> None:
    client, suite, _ = build(tmp_path)
    suite.gpu_results = tuple(GpuReleaseResult(f"e{i}", outcome, "") for i, outcome in enumerate(outcomes))
    answer = client.post("/gpu/release", json={"items": ["e0", "e1"], "force": True, "reason": "GPU-Orc: loading X"})
    assert answer.status_code == status
    assert [r["outcome"] for r in answer.json()["results"]] == list(outcomes)
    assert suite.calls == [("gpu_release", (("e0", "e1"), True, "GPU-Orc: loading X"))]


@pytest.mark.parametrize(
    "body",
    [{"items": "all"}, {"items": "some", "reason": "r"}, {"items": "all", "reason": "r", "force": "yes"},
     {"items": "all", "reason": "r", "extra": 1}],
)
def test_gpu_release_refuses_a_malformed_body(tmp_path: Path, body: dict[str, object]) -> None:
    client, suite, _ = build(tmp_path)
    assert client.post("/gpu/release", json=body).status_code == 400
    assert suite.calls == []


def test_a_reading_aborted_for_the_gpu_ends_with_its_own_error_code(tmp_path: Path) -> None:
    client, suite, _ = build(tmp_path)
    suite.stream = FakeStream([audio(data=b"one"), ReadingAborted("loading X")])
    answer = client.post("/speak", json={"text": "Hi", "language": "en", "engines": ENGINES, "paragraph_pause_ms": 0})
    frames = decode_frames(answer.content)
    assert [header["kind"] for header, _ in frames] == ["audio", "error"]
    assert json.loads(frames[1][1]) == {"error": "reading_aborted", "detail": "reading aborted, the GPU is needed: loading X"}
