"""Where the suite keeps what must survive a restart: the configuration (one JSON file, the object of
``GET /config``) and what the page of the service remembers of the test reading (``GET /speak-test``).
Changes are written at once."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from ..api import SuiteConfig, Unit
from .wire import config_from_json, config_to_json


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A file of its own per writer, renamed over the old one: nobody reads half a file.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SuiteConfig | None:
        """The saved configuration, None while nothing was saved."""
        if not self.path.exists():
            return None
        return config_from_json(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, config: SuiteConfig) -> None:
        _write_atomically(self.path, json.dumps(config_to_json(config), indent=2, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class SpeakTestState:
    """What the page remembers of the test reading, the same on every device: the text, the language,
    the pause, the order of the engines and what was chosen for each (keys are ``engine@host``)."""

    text: str
    language: str
    pause_ms: int
    order: list[str] = field(default_factory=list)
    use: dict[str, bool] = field(default_factory=dict)
    units: dict[str, Unit] = field(default_factory=dict)
    voices: dict[str, str] = field(default_factory=dict)  # per engine


_SPEAK_TEST = TypeAdapter(SpeakTestState)


class SpeakTestStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Any:
        """The saved state as JSON; an empty object while nothing was saved."""
        if not self.path.exists():
            return {}
        return _SPEAK_TEST.dump_python(_SPEAK_TEST.validate_python(json.loads(self.path.read_text(encoding="utf-8"))))

    def save(self, state: Any) -> Any:
        """Validates the JSON object, keeps it, and returns it as it is kept."""
        kept = _SPEAK_TEST.dump_python(_SPEAK_TEST.validate_python(state))
        _write_atomically(self.path, json.dumps(kept, indent=2, ensure_ascii=False) + "\n")
        return kept
