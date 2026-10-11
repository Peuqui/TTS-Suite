"""Texts the suite speaks itself (voice-change announcements, hints), German and English.

The texts live in ``i18n.json`` (``{key: {language: text}}``), not in the code.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from .api import UnsupportedLanguage
from .constants import I18N_FILE_NAME


@cache
def _texts() -> dict[str, dict[str, str]]:
    texts: dict[str, dict[str, str]] = json.loads(
        (Path(__file__).parent / I18N_FILE_NAME).read_text(encoding="utf-8")
    )
    return texts


def supported_languages() -> frozenset[str]:
    """The languages the suite has ALL its own texts in (announcements, hints): the one list of
    languages a reading may ask for, read from ``i18n.json``."""
    return frozenset.intersection(*(frozenset(by_language) for by_language in _texts().values()))


def translate(key: str, language: str, **values: str) -> str:
    """The text ``key`` in ``language`` with ``{placeholders}`` filled in. A missing key is a
    programming error (``KeyError``); a language without a text is :class:`UnsupportedLanguage`."""
    by_language = _texts()[key]
    if language not in by_language:
        raise UnsupportedLanguage(f"no text {key!r} in language {language!r} (have {sorted(by_language)})")
    return by_language[language].format(**values)
