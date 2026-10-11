"""Registry: discovery, order, completeness of the engine folders, units, reload."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterator

import pytest

from tts_suite.api import EngineDefaults, EntryConfig, UnknownEngine
from tts_suite.engines import (
    SPEECH_UNITS,
    TTS_ENGINES,
    DeactivatedEngine,
    TTSEngine,
    default_escalation,
    get_engine,
    gpu_engine_keys,
    installed_gpu_engines,
    is_deactivated,
    parse_speed_factor,
    reload_engines,
    require_engine,
    speech_unit,
    voice_names,
)
from tts_suite.engines import registry

LANGUAGES = ("de", "en")
EXPECTED_ORDER = [
    "qwen3local", "xtts", "fishspeech", "moss", "dashscope_audio3", "piper", "espeak", "edge", "browser",
]
CONTAINER_ENGINES = [e for e in TTS_ENGINES.values() if e.runs_in_container]


def test_all_nine_engines_are_discovered_in_display_order() -> None:
    assert list(TTS_ENGINES) == EXPECTED_ORDER
    orders = [e.display_order for e in TTS_ENGINES.values()]
    assert orders == sorted(orders)


def test_every_engine_folder_holds_exactly_one_registered_engine() -> None:
    folders = sorted(p.parent.name for p in registry.ENGINES_DIR.glob("*/engine.py"))
    assert folders == sorted(TTS_ENGINES), "folder name must be the engine key"


@pytest.mark.parametrize("engine", TTS_ENGINES.values(), ids=lambda e: e.key)
def test_engine_has_own_folder_and_labels(engine: TTSEngine) -> None:
    assert engine.package_dir == registry.ENGINES_DIR / engine.key
    for lang in LANGUAGES:
        assert engine.label(lang).strip(), f"{engine.key}: label {lang} missing"
    assert engine.default_speech_unit in SPEECH_UNITS


@pytest.mark.parametrize("engine", CONTAINER_ENGINES, ids=lambda e: e.key)
def test_container_engine_matches_its_compose_file(engine: TTSEngine) -> None:
    compose_path = engine.docker_compose_path
    assert compose_path is not None and compose_path.exists()
    compose = compose_path.read_text(encoding="utf-8")
    assert re.search(rf"^\s*image:\s*{re.escape(engine.image_name or '')}\s*$", compose, re.M)
    assert re.search(rf'-\s*"{engine.default_port}:\d+"', compose), "host port"
    assert engine.startup_timeout_s > 0
    assert engine.health_path.startswith("/")


@pytest.mark.parametrize("engine", [e for e in TTS_ENGINES.values() if not e.runs_in_container], ids=lambda e: e.key)
def test_lightweight_engine_declares_no_container_data(engine: TTSEngine) -> None:
    assert engine.image_name is None
    assert engine.docker_compose_path is None


def test_browser_entry_is_listed_but_marked_as_the_clients_voice() -> None:
    browser = require_engine("browser")
    assert browser.renders_in_browser
    assert [e.key for e in TTS_ENGINES.values() if e.renders_in_browser] == ["browser"]


def test_default_escalation_comes_from_the_engines_and_the_browser_speaks_last() -> None:
    entries = default_escalation()
    assert [e.engine for e in entries] == [e.key for e in TTS_ENGINES.values() if e.in_default_escalation]
    assert entries[-1].engine == "browser"
    assert all(isinstance(e, EntryConfig) and e.host is None and e.enabled for e in entries)


def test_gpu_engines_are_the_container_engines() -> None:
    assert gpu_engine_keys() == {"qwen3local", "xtts", "fishspeech", "moss"}


def test_installed_gpu_engines_asks_each_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, engine in TTS_ENGINES.items():
        monkeypatch.setattr(engine, "is_installed", lambda key=key: key in {"xtts", "edge"})
    assert [e.key for e in installed_gpu_engines()] == ["xtts"]


def test_burn_in_budgets_are_the_secured_ones_only() -> None:
    budgets = {e.key: e.default_vram_budget_mib for e in TTS_ENGINES.values() if e.default_vram_budget_mib}
    assert budgets == {"qwen3local": 6024, "xtts": 3680}


def test_unknown_engine_key() -> None:
    assert get_engine("nope") is None
    with pytest.raises(UnknownEngine, match="unknown TTS engine"):
        require_engine("nope")


# ── Units ──────────────────────────────────────────────────────────

def test_slow_local_engines_default_to_the_whole_text_fast_ones_to_sentences() -> None:
    assert {key: speech_unit(key, {}) for key in ("piper", "moss", "espeak")} == {
        "piper": "whole", "moss": "whole", "espeak": "whole",
    }
    assert speech_unit("edge", {}) == "sentence" and speech_unit("dashscope_audio3", {}) == "sentence"


def test_the_configured_unit_wins_for_its_engine_only() -> None:
    defaults = {"piper": EngineDefaults(unit="paragraph"), "edge": EngineDefaults()}
    assert speech_unit("piper", defaults) == "paragraph"
    assert speech_unit("edge", defaults) == "sentence"  # unit None = the engine's own default


def test_an_invalid_unit_is_an_error() -> None:
    bad = {"edge": EngineDefaults(unit="word")}  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="speech unit"):
        speech_unit("edge", bad)


def test_unit_of_an_unknown_engine_is_an_error() -> None:
    with pytest.raises(UnknownEngine):
        speech_unit("nope", {})


# ── Small helpers ──────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [("1.25x", 1.25), ("1.25", 1.25), (1.25, 1.25), (None, None), ("", None), ("fast", None)])
def test_parse_speed_factor(raw: object, expected: float | None) -> None:
    assert parse_speed_factor(raw) == expected


class _Catalog:
    def __init__(self, live: dict[str, str] | Exception, fallback: dict[str, str]) -> None:
        self._live, self.voices_fallback = live, fallback

    def get_voices(self) -> dict[str, str]:
        if isinstance(self._live, Exception):
            raise self._live
        return self._live


def test_voice_names_live_then_fallback() -> None:
    assert voice_names(_Catalog({"A": "a"}, {"B": "b"})) == ["A"]
    assert voice_names(_Catalog({}, {"B": "b"})) == ["B"]
    assert voice_names(_Catalog(OSError("down"), {"B": "b"})) == ["B"]


# ── Deactivated engines and reload ─────────────────────────────────

def test_deactivated_engine_is_a_never_running_placeholder() -> None:
    placeholder = DeactivatedEngine("xtts")
    assert placeholder.deactivated and placeholder.key == "xtts" and placeholder.label_short == "xtts"
    assert not placeholder.is_running() and not placeholder.is_installed()
    assert placeholder.get_voices() == {}
    assert placeholder.docker_compose_path is None


def test_is_deactivated_looks_into_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "xtts").mkdir()
    (tmp_path / "xtts" / "engine.py").write_text("")
    monkeypatch.setattr(registry, "DISABLED_DIR", tmp_path)
    assert is_deactivated("xtts")
    assert not is_deactivated("xtss")


ENGINE_SOURCE = '''
from tts_suite.engines.base import TTSEngine

class {cls}(TTSEngine):
    key = "{key}"
    label_short = "{key}"
    display_order = {order}

    def get_voices(self):
        return {{}}
'''


@pytest.fixture
def fake_engines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throw-away engine package with two engines, in place of the real one: no real folder is moved."""
    package = tmp_path / "fake_engines"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for key, order in (("alpha", 10), ("beta", 20)):
        (package / key).mkdir()
        (package / key / "__init__.py").write_text("")
        (package / key / "engine.py").write_text(ENGINE_SOURCE.format(cls=key.title(), key=key, order=order))
    (package / "disabled").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(registry, "ENGINES_DIR", package)
    monkeypatch.setattr(registry, "DISABLED_DIR", package / "disabled")
    monkeypatch.setattr(registry, "_PACKAGE", "fake_engines")
    monkeypatch.setattr(registry, "TTS_ENGINES", registry._discover_engines())
    yield package
    for name in [n for n in sys.modules if n.startswith("fake_engines")]:
        del sys.modules[name]


def test_reload_changes_nothing_when_the_folders_are_the_registered_ones(fake_engines: Path) -> None:
    assert list(registry.TTS_ENGINES) == ["alpha", "beta"]
    assert reload_engines() == ([], [])


def test_moving_an_engine_folder_into_disabled_and_back_takes_effect_on_reload(fake_engines: Path) -> None:
    (fake_engines / "beta").rename(fake_engines / "disabled" / "beta")
    assert reload_engines() == (["beta"], [])
    assert list(registry.TTS_ENGINES) == ["alpha"]
    assert is_deactivated("beta")

    (fake_engines / "disabled" / "beta").rename(fake_engines / "beta")
    assert reload_engines() == ([], ["beta"])
    assert list(registry.TTS_ENGINES) == ["alpha", "beta"]
    assert not is_deactivated("beta")


def test_a_new_folder_is_picked_up_on_reload(fake_engines: Path) -> None:
    gamma = fake_engines / "gamma"
    gamma.mkdir()
    (gamma / "__init__.py").write_text("")
    (gamma / "engine.py").write_text(ENGINE_SOURCE.format(cls="Gamma", key="gamma", order=5))
    assert reload_engines() == ([], ["gamma"])
    assert "gamma" in registry.TTS_ENGINES
