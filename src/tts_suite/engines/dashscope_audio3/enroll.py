"""DashScope voice-cloning enrollment for Qwen-Audio 3.0.

Clones the shared reference voices (``<voices_dir>/<Name>/<Name>.wav``)
into DashScope's voice-cloning model ONCE each and remembers the returned
voice id in a mapping file inside the data folder of the suite
(``data_dir() / DASHSCOPE_MAPPING_FILE_NAME``; the mapping is the source of truth for "is this
voice already enrolled").

API (verified 2026-10-10): ``model="voice-enrollment"`` + ``action="create_voice"``
at the ``/services/audio/tts/customization`` endpoint, the reference audio
inline as a base64 data URI in ``url`` (no public URL needed), the voice id in
``output.voice_id``. The voice is bound to the ``target_model`` it was created
with — synthesis MUST use the same model.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ...constants import DASHSCOPE_ENROLL_TIMEOUT_S, DASHSCOPE_MAPPING_FILE_NAME, data_dir
from .engine import DashScopeAudio3Engine

logger = logging.getLogger(__name__)

# Enrollment endpoint + model (Singapore / international region).
ENROLL_URL = "https://dashscope-intl.aliyuncs.com/api/v1/services/audio/tts/customization"
ENROLL_MODEL = "voice-enrollment"
# Synthesis model the cloned voice is bound to — synthesis MUST use the same.
TARGET_MODEL = DashScopeAudio3Engine.model_flash

Mapping = dict[str, dict[str, Any]]


def mapping_path() -> Path:
    return data_dir() / DASHSCOPE_MAPPING_FILE_NAME


def _wav_meta(wav_path: Path) -> tuple[float, str]:
    """Return (duration_seconds, sha256-of-bytes) for a reference WAV."""
    with wave.open(str(wav_path), "rb") as w:
        duration = w.getnframes() / w.getframerate()
    digest = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    return duration, digest


def load_mapping() -> Mapping:
    """Load the Name -> {voice_id, wav_sha256, target_model} mapping from the data folder."""
    path = mapping_path()
    if not path.exists():
        return {}
    try:
        data: Mapping = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError) as e:
        logger.error("DashScope enroll: cannot read %s: %s", path.name, e)
        return {}


def save_mapping(mapping: Mapping) -> None:
    path = mapping_path()
    path.parent.mkdir(parents=True, exist_ok=True)  # the data folder does not exist before the first enrollment
    path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")


def _enroll_one(name: str, wav_path: Path, api_key: str) -> dict[str, Any] | None:
    """Clone one reference voice via base64 enrollment. Returns the mapping
    entry or None on failure (no fallback — the caller surfaces it and the
    voice stays unavailable). Detailed HTTP/network errors are logged here."""
    import requests

    raw = wav_path.read_bytes()
    _, digest = _wav_meta(wav_path)
    b64 = base64.b64encode(raw).decode()
    # Prefix → readable voice-id stem: digits + lowercase only, < 10 chars.
    prefix = "".join(c for c in name.lower() if c.isalnum())[:9] or "voice"

    try:
        resp = requests.post(
            ENROLL_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": ENROLL_MODEL,
                "input": {
                    "action": "create_voice",
                    "target_model": TARGET_MODEL,
                    "prefix": prefix,
                    "url": f"data:audio/wav;base64,{b64}",
                },
            },
            timeout=DASHSCOPE_ENROLL_TIMEOUT_S,
        )
    except requests.RequestException as e:
        logger.error("DashScope enroll '%s': network/internet unreachable — %s: %s", name, type(e).__name__, e)
        return None

    if resp.status_code != 200:
        logger.error("DashScope enroll '%s': HTTP %d — %s", name, resp.status_code, resp.text[:160])
        return None
    voice_id = resp.json().get("output", {}).get("voice_id")
    if not voice_id:
        logger.error("DashScope enroll '%s': no voice id in response — %s", name, resp.text[:160])
        return None
    return {"voice_id": voice_id, "wav_sha256": digest, "target_model": TARGET_MODEL}


def enroll_progress(api_key: str, voices_dir: Path) -> Iterator[str]:
    """Enroll every reference voice that is missing OR whose WAV changed,
    yielding one human-readable progress line per step.

    A start line per voice, a result line per voice, and ALWAYS a final
    summary (even when nothing changed, so "checking…" never looks stuck).

    Idempotent: a voice already in the mapping whose WAV hash matches is
    skipped silently (no re-enroll, no cost). Detailed HTTP/network errors are
    logged inside ``_enroll_one``; the yielded lines stay high-level.
    """
    if not api_key:
        yield "DashScope enroll: no API key configured — skipping"
        return
    if not voices_dir.exists():
        yield f"DashScope enroll: voices dir not found: {voices_dir}"
        return

    mapping = load_mapping()
    enrolled = skipped = failed = 0
    for sub in sorted(voices_dir.iterdir()):
        wav = sub / f"{sub.name}.wav"
        if not sub.is_dir() or not wav.exists():
            continue
        name = sub.name
        duration, digest = _wav_meta(wav)
        existing = mapping.get(name)
        if existing and existing.get("wav_sha256") == digest:
            skipped += 1
            continue  # already enrolled, WAV unchanged
        yield f"DashScope enroll: '{name}' — full {duration:.1f}s, contacting cloud…"
        entry = _enroll_one(name, wav, api_key)
        if entry:
            mapping[name] = entry
            enrolled += 1
            yield f"DashScope enroll: '{name}' → {entry['voice_id']}"
        else:
            failed += 1
            yield f"DashScope enroll: '{name}' failed — see log for details"

    if enrolled:
        save_mapping(mapping)
    tail = f", {failed} failed" if failed else ""
    yield (
        f"DashScope enroll done: {enrolled} new, {skipped} already current"
        f"{tail} ({len(mapping)} voices total)"
    )
