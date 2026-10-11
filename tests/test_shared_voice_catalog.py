"""Cloned voices come from the shared voice tree while no container answers."""
from __future__ import annotations

from pathlib import Path

import pytest

from tts_suite.engines import get_engine, shared_voice_names, voice_names


@pytest.fixture
def voice_tree(tmp_path: Path) -> Path:
    for name in ("Salomo", "Codine"):
        (tmp_path / name).mkdir()
        (tmp_path / name / f"{name}.wav").write_bytes(b"")
    # A work file next to the reference is no voice (<Name>/<Name>.wav only).
    (tmp_path / "Codine" / "Appetit-roh.wav").write_bytes(b"")
    return tmp_path


def test_names_follow_the_tree(voice_tree: Path) -> None:
    assert shared_voice_names(voice_tree) == ["Codine", "Salomo"]


def test_a_missing_tree_has_no_voices(tmp_path: Path) -> None:
    assert shared_voice_names(tmp_path / "nowhere") == []


@pytest.mark.parametrize("engine_key", ["qwen3local", "moss", "fishspeech"])
def test_container_engines_list_the_tree_while_down(voice_tree: Path, engine_key: str) -> None:
    engine = get_engine(engine_key)
    assert engine is not None
    assert list(engine.with_voices_dir(voice_tree).voices_fallback) == ["Codine", "Salomo"]


def test_an_engine_without_a_voices_directory_says_so() -> None:
    engine = get_engine("qwen3local")
    assert engine is not None
    with pytest.raises(RuntimeError, match="not bound to a voices directory"):
        engine.voices_fallback  # noqa: B018


def test_xtts_puts_the_cloned_voices_first_with_a_star(voice_tree: Path) -> None:
    engine = get_engine("xtts")
    assert engine is not None
    bound = engine.with_voices_dir(voice_tree)
    names = list(bound.voices_fallback)
    assert names[:2] == ["★ Codine", "★ Salomo"]
    assert bound.voices_fallback["★ Codine"] == "Codine"
    assert all(not name.startswith("★") for name in names[2:])


def test_catalog_shows_a_new_voice_without_a_running_container(
    voice_tree: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = get_engine("qwen3local")
    assert engine is not None
    bound = engine.with_voices_dir(voice_tree)
    monkeypatch.setattr(type(bound), "get_voices", lambda self: {})
    assert "Codine" in voice_names(bound)


def test_bindings_survive_placing_the_engine(voice_tree: Path) -> None:
    engine = get_engine("xtts")
    assert engine is not None
    placed = engine.with_voices_dir(voice_tree).on_gpu("GPU-1")
    assert (placed.gpu_uuid, placed.voices_dir) == ("GPU-1", voice_tree)
    remote = placed.at_host("10.0.0.2", 6051)
    assert (remote.service_url, remote.voices_dir, remote.is_remote) == ("http://10.0.0.2:6051", voice_tree, True)
    assert engine.service_url == "http://localhost:5051" and not engine.is_remote  # the registry instance stays
