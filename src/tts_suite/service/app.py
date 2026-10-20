"""The HTTP service: `create_app` wraps a :class:`tts_suite.api.TTSSuite` in the endpoints of
``docs/INTERFACE.md``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..api import SpeakRequest, SuiteError, TTSSuite
from .auth import Access
from .config_store import ConfigStore, SpeakTestStore
from .errors import ServiceError, bad_request, text_too_long
from .frames import FRAMES_MEDIA_TYPE, audio_frame, error_frame, summary_frame
from .wire import (
    config_from_json,
    config_to_json,
    engines_to_json,
    error_answer,
    releases_to_json,
    request_from_json,
    targets_from_json,
    voices_to_json,
)

log = logging.getLogger("tts_suite.service")

# The page of the service (plain HTML, CSS and JavaScript, no build step). It holds no data, so it needs no
# token; what it asks for does. It may only load its own files and talk to this service.
UI_DIR = Path(__file__).parent / "ui"
UI_PREFIX = "/ui"
UI_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    # Without it a browser keeps an old script for hours and a fixed page does not arrive; with it
    # the ETag decides, and an unchanged file costs one short answer.
    "Cache-Control": "no-cache",
}

# A client that is told to wait for a container to start asks again after this long.
RETRY_AFTER_SECONDS = 5
# The WebSocket closes with this code when the request is refused (a policy violation).
REFUSED_CLOSE_CODE = 1008


def _answer(error: SuiteError) -> JSONResponse:
    status, body = error_answer(error)
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS)} if body["error"] == "starting" else None
    return JSONResponse(body, status_code=status, headers=headers)


def _head(data: Any) -> SpeakRequest:
    """The head of a reading from what the client sent."""
    try:
        return request_from_json(data)
    except ValidationError as error:
        raise bad_request(str(error)) from error


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as error:
        raise bad_request("the body is no JSON") from error
    if not isinstance(body, dict):
        raise bad_request("the body is no JSON object")
    return body


def create_app(suite: TTSSuite, access: Access, store: ConfigStore, max_text_chars: int) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await suite.aclose()

    app = FastAPI(title="TTS-Suite", lifespan=lifespan)
    speak_test = SpeakTestStore(store.path.with_name("speak-test.json"))

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, error: ServiceError) -> JSONResponse:
        return JSONResponse(error.body(), status_code=error.status)

    @app.exception_handler(SuiteError)
    async def suite_error(_: Request, error: SuiteError) -> JSONResponse:
        return _answer(error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(bad_request(str(error)).body(), status_code=400)

    def check(request: Request, changes_something: bool) -> None:
        access.check(request.headers, changes_something)

    @app.middleware("http")
    async def page_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        if request.url.path.startswith(UI_PREFIX):
            response.headers.update(UI_HEADERS)
        return response

    @app.get("/", include_in_schema=False)
    async def to_the_page() -> RedirectResponse:
        return RedirectResponse(f"{UI_PREFIX}/")

    app.mount(UI_PREFIX, StaticFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/engines")
    async def engines(request: Request) -> Any:
        check(request, False)
        return engines_to_json(await suite.engines())

    @app.get("/voices")
    async def voices(request: Request, engine: str | None = None) -> Any:
        check(request, False)
        return voices_to_json(await suite.voices(engine))

    @app.post("/voices/sync")
    async def sync_voices(request: Request, engine: str) -> Any:
        check(request, True)
        return voices_to_json(await suite.sync_voices(engine))

    @app.post("/voices/push")
    async def push_voices(request: Request) -> dict[str, list[str]]:
        check(request, True)
        return {"hosts": await suite.push_voices()}

    @app.get("/config")
    async def get_config(request: Request) -> Any:
        check(request, False)
        return config_to_json(await suite.config())

    @app.post("/config")
    async def set_config(request: Request) -> Any:
        check(request, True)
        try:
            config = config_from_json(await _json_body(request))
        except ValidationError as error:
            raise bad_request(str(error)) from error
        await suite.set_config(config)
        store.save(config)
        return config_to_json(config)

    @app.get("/speak-test")
    async def get_speak_test(request: Request) -> Any:
        check(request, False)
        return speak_test.load()

    @app.put("/speak-test")
    async def put_speak_test(request: Request) -> Any:
        check(request, True)
        try:
            return speak_test.save(await _json_body(request))
        except ValidationError as error:
            raise bad_request(str(error)) from error

    @app.post("/engines/reload")
    async def reload_engines(request: Request) -> dict[str, list[str]]:
        check(request, True)
        removed, added = await suite.reload()
        return {"removed": removed, "added": added}

    @app.post("/engines/{engine}/start", status_code=204)
    async def start(request: Request, engine: str, host: str | None = None, gpu: str | None = None) -> None:
        check(request, True)
        await suite.start(engine, host, gpu)

    @app.post("/engines/{engine}/stop", status_code=204)
    async def stop(request: Request, engine: str, host: str | None = None) -> None:
        check(request, True)
        await suite.stop(engine, host)

    @app.post("/release")
    async def release(request: Request) -> Any:
        # A client asks, like for a reading, so no token on this machine; the suite decides what happens.
        check(request, False)
        body = await _json_body(request)
        try:
            targets = targets_from_json(body.get("targets"))
        except ValidationError as error:
            raise bad_request(str(error)) from error
        return releases_to_json(await suite.release(targets))

    @app.post("/speak")
    async def speak(request: Request) -> StreamingResponse:
        check(request, False)
        body = await _json_body(request)
        text = body.pop("text", None)
        if not isinstance(text, str):
            raise bad_request("`text` is missing or no text")
        if len(text) > max_text_chars:
            raise text_too_long(max_text_chars)
        stream = suite.speak(text, _head(body))
        segments = stream.segments()
        # Nothing is promised before the first piece exists: a reading that cannot start ends
        # with a status of its own, one that fails later with an error frame.
        try:
            first = await anext(segments, None)
        except BaseException:
            await stream.aclose()
            raise

        async def frames() -> AsyncIterator[bytes]:
            try:
                if first is not None:
                    yield audio_frame(first)
                    async for segment in segments:
                        yield audio_frame(segment)
                yield summary_frame(stream.summary)
            except SuiteError as error:
                yield error_frame(error_answer(error)[1])
            finally:
                await stream.aclose()

        return StreamingResponse(frames(), media_type=FRAMES_MEDIA_TYPE)

    @app.websocket("/speak/stream")
    async def speak_stream(socket: WebSocket) -> None:
        try:
            access.check(socket.headers, False)
        except ServiceError:
            await socket.close(code=REFUSED_CLOSE_CODE)
            return
        await socket.accept()
        try:
            await _serve_stream(socket, suite, max_text_chars)
        except WebSocketDisconnect:
            log.info("the client of a stream left")

    return app


async def _refuse(socket: WebSocket, body: dict[str, Any]) -> None:
    await socket.send_bytes(error_frame(body))
    await socket.close()


async def _serve_stream(socket: WebSocket, suite: TTSSuite, max_text_chars: int) -> None:
    """One reading over the WebSocket: the client sends its head (`{"type": "start", ...}`), then
    pieces of text and the end; the frames go back as they exist."""
    try:
        start = json.loads(await socket.receive_text())
        if not isinstance(start, dict) or start.pop("type", None) != "start":
            raise bad_request('the first message must be {"type": "start", ...}')
        stream = suite.open_stream(_head(start))
    except ServiceError as error:
        await _refuse(socket, error.body())
        return
    except ValueError:
        await _refuse(socket, bad_request("the first message is no JSON").body())
        return
    except SuiteError as error:
        await _refuse(socket, error_answer(error)[1])
        return

    async def send_frames() -> None:
        try:
            async for segment in stream.segments():
                await socket.send_bytes(audio_frame(segment))
            await socket.send_bytes(summary_frame(stream.summary))
        except SuiteError as error:
            await socket.send_bytes(error_frame(error_answer(error)[1]))

    async def take_text() -> None:
        taken = 0
        while True:
            message = json.loads(await socket.receive_text())
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "text" and isinstance(message.get("text"), str):
                taken += len(message["text"])
                if taken > max_text_chars:
                    raise text_too_long(max_text_chars)
                await stream.feed(message["text"])
            elif kind == "finish":
                await stream.finish()
                return
            else:
                raise bad_request('expected {"type": "text", "text": ...} or {"type": "finish"}')

    sender = asyncio.create_task(send_frames())
    receiver = asyncio.create_task(take_text())
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        # The text is complete: the frames go on until the end. The sender ending first (an error
        # or the summary) makes the rest of the text of no use.
        if receiver.done() and not sender.done():
            await receiver
            await sender
        else:
            receiver.cancel()
        _raise_failures(sender, receiver)
        await socket.close()
    except ServiceError as error:
        await socket.send_bytes(error_frame(error.body()))
        await socket.close()
    except SuiteError as error:
        await socket.send_bytes(error_frame(error_answer(error)[1]))
        await socket.close()
    finally:
        for task in (sender, receiver):
            task.cancel()
        await stream.aclose()


def _raise_failures(sender: asyncio.Task[None], receiver: asyncio.Task[None]) -> None:
    """Raise what a finished task failed with (a cancelled one failed with nothing)."""
    for task in (sender, receiver):
        if task.done() and not task.cancelled() and task.exception() is not None:
            raise task.exception()  # type: ignore[misc]
