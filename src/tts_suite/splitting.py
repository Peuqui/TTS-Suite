"""Cutting a text into the units an engine takes at once (sentence, paragraph, whole).

The sentence detection is the one AIfred Intelligence used for its streaming TTS
(``extract_complete_sentences`` / ``split_text_for_streaming_tts``): abbreviations,
decimal numbers, URLs, ellipses, headings without a period, a colon before a list or
table, table rows and code blocks are handled. The rules were ported unchanged; only
the scan was reshaped so that it can resume at a given position of a growing text.

Two entry points, one implementation:

* :func:`split_text` — a finished text, cut by a unit.
* :class:`UnitCutter` — a text that arrives in pieces; a unit is released as soon as it
  is complete. :func:`split_text` is ``feed`` + ``flush`` of a cutter, so both give the
  same units by construction (for any way the text is divided into pieces).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .api import Unit
from .constants import (
    ABBREVIATIONS,
    SENTENCE_CLOSERS,
    SENTENCE_LOOKAHEAD_CHARS,
    SENTENCES_RELEASED_IMMEDIATELY,
    TTS_MIN_SENTENCE_WORDS,
)
from .engines.base import SPEECH_UNITS

SENTENCE_PUNCTUATION = ".!?"
#: A sentence that ends like this needs no period added when a line break ends it.
SENTENCE_FINAL_CHARS = ".!?:;"
CODE_FENCE = "```"
PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
#: Shortest sentence text that counts (a lone punctuation mark is no sentence).
MIN_SENTENCE_CHARS = 2
#: An introduction before a list or table ("...Tabelle:\n\n") must be longer than this to count as a sentence.
MIN_COLON_SENTENCE_CHARS = 11


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class _Extraction:
    """One sentence found by the scan, with where the scan decided it and where the next one starts."""

    sentence: str
    index: int
    next_start: int


def _ends_with_abbreviation(before: str) -> bool:
    """Whether ``before`` (the text up to and including a period) ends with an abbreviation as a WORD.
    AIfred matched the end of the string only, so every word ending in ``s.`` or ``ch.`` ("Haus.",
    "noch.") counted as an abbreviation and its sentence was never cut."""
    return any(
        before.endswith(abbreviation)
        and (len(before) == len(abbreviation) or not before[-len(abbreviation) - 1].isalnum())
        for abbreviation in ABBREVIATIONS
    )


def _period_ends_sentence(text: str, i: int) -> bool:
    """Whether the period at ``text[i]`` ends a sentence (not an abbreviation, decimal number, URL part)."""
    before = text[max(0, i - 10):i + 1].lower()
    after = text[i + 1:i + 3]
    if _ends_with_abbreviation(before):
        return False
    if 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
        return False  # decimal number
    url_context = text[max(0, i - 20):i + 10].lower()
    if "http" in url_context or "www." in url_context or ".com" in url_context:
        return False
    if i >= 2 and text[i - 2:i + 1] == "...":
        # An ellipsis ends a sentence only before an uppercase letter or at the end
        return bool(after and after[0].isupper()) or not after.strip()
    if not after:
        return True  # end of text
    if after[0] in " \n\t":
        return (len(after) > 1 and after[1].isupper()) or len(after) == 1
    return after[0] in SENTENCE_CLOSERS


def _scan_sentences(text: str, start: int) -> tuple[list[_Extraction], int]:
    """Find the sentences of ``text`` from ``start`` on (``text`` has ``\\n`` line ends only).
    Returns them and the position where the unfinished rest begins. The text before ``start``
    is only looked at, never cut again (the end detection looks back up to 20 characters)."""
    if text.count(CODE_FENCE) % 2 == 1:
        return [], start  # inside a code block: nothing is a sentence yet
    found: list[_Extraction] = []
    sentence_start = start
    i = start
    while i < len(text):
        char = text[i]
        if char == "\n":
            sentence = text[sentence_start:i].strip()
            if i + 1 < len(text) and text[i + 1] == "\n":
                # Paragraph break: ends a sentence even without punctuation (a heading)
                next_content_start = i + 1
                while next_content_start < len(text) and text[next_content_start] in " \t\n":
                    next_content_start += 1
                if len(sentence) >= MIN_SENTENCE_CHARS:
                    if sentence[-1] not in SENTENCE_FINAL_CHARS:
                        sentence += "."
                    found.append(_Extraction(sentence, i, next_content_start))
                sentence_start = next_content_start
                i = next_content_start - 1
            elif len(sentence) >= MIN_SENTENCE_CHARS and sentence[-1] not in SENTENCE_FINAL_CHARS:
                # Single line break after a line without punctuation: a heading
                found.append(_Extraction(sentence + ".", i, i + 1))
                sentence_start = i + 1
        if char == ":":
            after_colon = text[i + 1:i + 3]
            if after_colon.startswith("\n"):
                # Colon before line breaks: an introduction to a list or table
                sentence = text[sentence_start:i + 1].strip()
                if len(sentence) >= MIN_COLON_SENTENCE_CHARS:
                    found.append(_Extraction(sentence, i, i + 1))
                sentence_start = i + 1
                while i + 1 < len(text) and text[i + 1] in "\n\t ":
                    i += 1
        if char in SENTENCE_PUNCTUATION:
            # Table rows arrive piece by piece ("| Marmelade (z."): a period there is no sentence end
            current_line_start = text.rfind("\n", 0, i) + 1
            if text[current_line_start:i + 1].lstrip().startswith("|"):
                i += 1
                continue
            if char in "!?" or _period_ends_sentence(text, i):
                end = i + 1
                while end < len(text) and text[end] in SENTENCE_CLOSERS:
                    end += 1  # closing quotes and parentheses belong to the sentence
                i = end - 1
                sentence = text[sentence_start:end].strip()
                if len(sentence) >= MIN_SENTENCE_CHARS:
                    found.append(_Extraction(sentence, i, end))
                sentence_start = end
        i += 1
    return found, sentence_start


class UnitCutter:
    """A text that arrives in pieces, cut into units.

    ``feed(piece)`` returns the units that became complete, ``flush()`` the rest once the
    text has ended. A unit is released only when it is complete: a sentence when its end is
    certain (:data:`SENTENCE_LOOKAHEAD_CHARS` characters follow it, or the text ended), a
    paragraph at its blank line, the whole text only at ``flush``.

    Look-ahead of the unit "sentence": the first ``immediate_sentences`` sentences are released
    as soon as they are complete, every later one only when the NEXT sentence is complete too
    (or at ``flush``). A closing sentence with fewer than ``TTS_MIN_SENTENCE_WORDS`` words is joined
    to its predecessor if that is not released yet.

    The unit is that of the engine that speaks, which is known only after the first
    selection — later than the first piece of text. So the unit may be set after the
    construction (:meth:`set_unit`): until then pieces are only collected and nothing is
    released; ``set_unit`` returns what the collected text yields. Once set, the unit stays
    for the whole text (a successor engine with another unit takes the same units).
    """

    def __init__(self, unit: Unit | None = None, *, immediate_sentences: int = SENTENCES_RELEASED_IMMEDIATELY) -> None:
        self._immediate_sentences = immediate_sentences
        self._unit: Unit | None = None
        self._text = ""
        self._start = 0  # position in the normalized text where the unreleased part begins
        self._carry = ""  # a too short sentence, waiting for the next one
        self._held: list[str] = []  # complete sentences waiting for their successor
        self._released = 0  # sentences released so far
        if unit is not None:
            self.set_unit(unit)

    @property
    def unit(self) -> Unit | None:
        return self._unit

    def set_unit(self, unit: Unit) -> list[str]:
        """Fix the unit (once) and release what the text collected so far yields."""
        if unit not in SPEECH_UNITS:
            raise ValueError(f"speech unit must be one of {SPEECH_UNITS}, got {unit!r}")
        if self._unit is not None:
            raise RuntimeError(f"the unit is already set to {self._unit!r}")
        self._unit = unit
        return self._cut(final=False)

    def feed(self, text: str) -> list[str]:
        self._text += text
        return self._cut(final=False)

    def flush(self) -> list[str]:
        """The text has ended: release everything that is left. The cutter is empty afterwards."""
        if self._unit is None:
            raise RuntimeError("the unit is not set yet (set_unit)")
        units = self._cut(final=True)
        self._text, self._start, self._carry, self._held, self._released = "", 0, "", [], 0
        return units

    def _cut(self, final: bool) -> list[str]:
        if self._unit is None:
            return []
        text = _normalize_newlines(self._text)
        if self._unit == "sentence":
            return self._cut_sentences(text, final)
        if self._unit == "paragraph":
            return self._cut_paragraphs(text, final)
        return [text.strip()] if final and text.strip() else []

    def _cut_paragraphs(self, text: str, final: bool) -> list[str]:
        units: list[str] = []
        position = self._start
        for separator in PARAGRAPH_BREAK.finditer(text, position):
            units.append(text[position:separator.start()].strip())
            position = separator.end()
        self._start = position
        if final:
            units.append(text[position:].strip())
        return [unit for unit in units if unit]

    def _cut_sentences(self, text: str, final: bool) -> list[str]:
        if final:
            # A paragraph end forces the last sentence to complete
            text = text.rstrip() + "\n\n"
        found, rest_start = _scan_sentences(text, min(self._start, len(text)))
        if final:
            sentences = [extraction.sentence for extraction in found]
            rest = text[rest_start:].strip()
            if rest:
                sentences.append(rest)
        else:
            certain = [e for e in found if len(text) - e.index > SENTENCE_LOOKAHEAD_CHARS]
            if certain:
                self._start = certain[-1].next_start
            sentences = [extraction.sentence for extraction in certain]
        self._held += self._join_short(sentences)
        if final and self._carry:
            # A short ending joins its predecessor if that one is still here; a released one stays as it is
            if self._held:
                self._held[-1] = f"{self._held[-1]} {self._carry}"
            else:
                self._held.append(self._carry)
            self._carry = ""
        return self._release_ready(final)

    def _join_short(self, sentences: list[str]) -> list[str]:
        """Sentences with fewer than ``TTS_MIN_SENTENCE_WORDS`` words go to the next one."""
        units: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if self._carry:
                sentence = f"{self._carry} {sentence}"
                self._carry = ""
            if len(sentence.split()) < TTS_MIN_SENTENCE_WORDS:
                self._carry = sentence
            else:
                units.append(sentence)
        return units

    def _release_ready(self, final: bool) -> list[str]:
        """The held sentences that may go: the immediate ones, those whose successor is complete, all at the end."""
        units: list[str] = []
        while self._held and (final or self._released < self._immediate_sentences or len(self._held) >= 2):
            units.append(self._held.pop(0))
            self._released += 1
        return units


def split_text(text: str, unit: Unit) -> list[str]:
    """A finished text cut by ``unit``."""
    cutter = UnitCutter(unit)
    return cutter.feed(text) + cutter.flush()
