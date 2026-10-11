"""Where the configuration of the suite is kept between starts: one JSON file, the object of
``GET /config``. Changes are written at once and survive a restart."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..api import SuiteConfig
from .wire import config_from_json, config_to_json


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SuiteConfig | None:
        """The saved configuration, None while nothing was saved."""
        if not self.path.exists():
            return None
        return config_from_json(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, config: SuiteConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(config_to_json(config), indent=2, ensure_ascii=False) + "\n"
        # A file of its own per writer, renamed over the old one: nobody reads half a file.
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=f"{self.path.name}.", suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
