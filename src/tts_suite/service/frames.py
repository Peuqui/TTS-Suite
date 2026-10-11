"""The frames audio comes back in, the same on both doors (``POST /speak`` and the WebSocket).

A frame is the length of its header (``uint32``, big endian), the header as UTF-8 JSON and the
body. The header names the ``kind`` and the ``bytes`` of the body. On the WebSocket one frame is
one binary message; in the HTTP body they follow each other.
"""

from __future__ import annotations

import json
import struct
from typing import Any

from ..api import AudioSegment, SpeakSummary
from .wire import summary_to_json

FRAMES_MEDIA_TYPE = "application/x-tts-frames"
HEADER_LENGTH = struct.Struct(">I")


def encode_frame(header: dict[str, Any], body: bytes = b"") -> bytes:
    head = json.dumps({**header, "bytes": len(body)}, ensure_ascii=False).encode("utf-8")
    return HEADER_LENGTH.pack(len(head)) + head + body


def audio_frame(segment: AudioSegment) -> bytes:
    header = {
        "kind": "audio",
        "media_type": segment.media_type,
        "engine": segment.engine,
        "keepalive": segment.keepalive,
    }
    return encode_frame(header, segment.audio)


def summary_frame(summary: SpeakSummary) -> bytes:
    return encode_frame({"kind": "summary"}, json.dumps(summary_to_json(summary)).encode("utf-8"))


def error_frame(error: dict[str, Any]) -> bytes:
    """Ends a stream that fails after its first frame; `error` is the body of an error answer."""
    return encode_frame({"kind": "error"}, json.dumps(error).encode("utf-8"))


def decode_frames(data: bytes) -> list[tuple[dict[str, Any], bytes]]:
    """The frames in a byte string (for clients and tests)."""
    frames = []
    offset = 0
    while offset < len(data):
        (head_length,) = HEADER_LENGTH.unpack_from(data, offset)
        offset += HEADER_LENGTH.size
        header = json.loads(data[offset : offset + head_length])
        offset += head_length
        frames.append((header, data[offset : offset + header["bytes"]]))
        offset += header["bytes"]
    return frames
