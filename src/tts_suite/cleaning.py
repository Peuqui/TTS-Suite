"""Text cleaning: what sounds bad when read aloud is removed or replaced by a spoken hint.

Ported from AIfred Intelligence (``clean_text_for_tts``): HTML comments and tags, code
(hint), Markdown tables (hint), LaTeX (hint), long lists (hint), emojis, Markdown
formatting, URLs, links (the text stays) and blockquotes. Engine-specific normalization
(characters that crash a tokenizer, punctuation against hallucinations) stays in the container.

Not ported, because it belongs to the calling program: the removal of collapsible blocks
(``<think>``, ``<details>``, ...), AIfred's own markers (round labels, the performance footer,
timing metadata in parentheses). The pronunciation list (``SuiteConfig.pronunciations``) replaces
the hard-wired "AIfred" -> "Alfred".

AIfred kept the state of a reading (which hints were spoken already, the list run) in module
globals. Here it is the state of one :class:`TextCleaner`, and a reading has exactly one: the units
of a reading go through it in speaking order, so a hint is spoken once per block, not once per unit.
"""

from __future__ import annotations

import re
import string
from typing import Mapping

from .constants import (
    BRACKET_MAX_CHARS,
    LAUGHTER_EMOJIS,
    LINK_TEXT_MAX_CHARS,
    LINK_URL_MAX_CHARS,
    MULTILINE_TEXT_MIN_CHARS,
    TTS_LIST_MAX_ITEMS,
)
from .i18n import translate
from .splitting import SENTENCE_FINAL_CHARS

HINT_CODE = "tts_hint_code"
HINT_TABLE = "tts_hint_table"
HINT_FORMULA = "tts_hint_formula"
HINT_MORE_ITEMS = "tts_hint_more_items"
HINT_LIST = "tts_hint_list"

CODE_FENCE = "```"
READABLE_WORD = re.compile(r"[^\W\d_]{2,}")  # two letters in a row: there is something to read
LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])\s+")

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_CODE_BLOCK = re.compile(re.escape(CODE_FENCE) + r".*?" + re.escape(CODE_FENCE), re.DOTALL)
_TABLE_BLOCK = re.compile(r"(\|[^\n]+\|\n?)+", re.MULTILINE)
_LIST_BLOCK = re.compile(r"(?:^[ \t]*(?:[-*+]|\d+[.)])\s+[^\n]*\n?){2,}", re.MULTILINE)
_LIST_ITEM_MARK = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_BLOCK_FORMULA = re.compile(r"\$\$[^$]+\$\$", re.DOTALL)
_INLINE_FORMULA = re.compile(r"\$[^$]+\$")
_INLINE_CODE = re.compile(r"`[^`]+`")
_LINK = re.compile(rf"\[([^\]]{{1,{LINK_TEXT_MAX_CHARS}}})\]\([^)]{{1,{LINK_URL_MAX_CHARS}}}\)")
_IMAGE = re.compile(rf"!\[[^\]]{{0,{LINK_TEXT_MAX_CHARS}}}\]\([^)]{{1,{LINK_URL_MAX_CHARS}}}\)")
_BLOCKQUOTE = re.compile(r"^>\s*", re.MULTILINE)
_BRACKETS = re.compile(rf"\[[^\]\n]{{0,{BRACKET_MAX_CHARS}}}\]")
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # Symbols & Pictographs (incl. clock faces)
    "\U0001F680-\U0001F6FF"  # Transport & Maps
    "\U0001F700-\U0001F77F"  # Alchemy Symbols
    "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
    "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols & Pictographs
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols & Pictographs Extended-A
    "\U0001F1E0-\U0001F1FF"  # Flags
    "\U00002600-\U000027BF"  # Misc Symbols
    "\U0000FE00-\U0000FE0F"  # Variation Selectors
    "\U0001F018-\U0001F270"  # Additional symbols
    "\U0000238C-\U00002454"  # Misc Technical
    "‍"                 # Zero Width Joiner
    "〰"                 # Wavy Dash
    "]+"
)
_EMOTICON = re.compile("[\U0001F600-\U0001F64F]")
_HORIZONTAL_LINES = re.compile("[─━═┄┅┈┉╌╍]+")  # cause crackling in some engines: a pause instead
_VERTICAL_LINES = re.compile("[│┃║┆┇┊┋╎╏]+")
_ASCII_RULE = re.compile(r"-{3,}|={3,}|_{3,}")
_HEADING_MARK = re.compile(r"#+\s")
_BULLET = re.compile(r"^[-*+]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\d+\.\s+", re.MULTILINE)
_URL = re.compile(r"https?://\S+|www\.\S+")
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
#: Dashes mark a pause: a comma keeps it ("Text – mehr Text" must not run together).
_DASHES = str.maketrans({"–": ",", "—": ",", "‒": ",", "―": ",", " ": " "})
#: Everything else (arrows, math symbols, ...) makes "quirks" in the engines. Kept: letters, digits,
#: basic punctuation, ° (degree), − (minus sign), / (km/h), % and the laughter emojis (the container
#: turns them into laughter).
_UNSPEAKABLE = re.compile(rf"[^\w\s.,!?;:\-'\"()\[\]°−/%{re.escape(''.join(LAUGHTER_EMOJIS))}]")
_SPACES = re.compile(r"  +")
_BLANK_LINES = re.compile(r"\n{3,}")


def _compile_pronunciations(pronunciations: Mapping[str, str]) -> re.Pattern[str] | None:
    """One pattern for all words (whole words, case as given; the longest first)."""
    if not pronunciations:
        return None
    words = sorted(pronunciations, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in words) + r")(?!\w)")


class TextCleaner:
    """Cleans the units of ONE reading, in speaking order. Holds the state of that reading:
    which hints were spoken already, the run of list items, and whether a code block is open
    (its fences lie in different units)."""

    def __init__(self, language: str, pronunciations: Mapping[str, str]) -> None:
        self._language = language
        self._pronunciations = pronunciations
        self._pronunciation_pattern = _compile_pronunciations(pronunciations)
        self._announced: set[str] = set()
        self._list_items = 0  # consecutive list items seen (a unit is one item)
        self._inside_code_block = False

    def clean(self, text: str) -> str:
        """The speakable text of ``text``; an empty string if nothing is left to speak."""
        if "\n" in text or len(text) > MULTILINE_TEXT_MIN_CHARS:
            # A whole text (paragraph, whole): no run of single units, so no hint is "already spoken"
            self._reset_hints()
        cleaned = _HTML_COMMENT.sub("", text).strip()
        cleaned = self._pronounce(cleaned)
        cleaned = _HTML_TAG.sub("", cleaned)
        cleaned = self._replace_code_blocks(cleaned)
        if "\n" not in cleaned:
            hint = self._hint_for_single_unit(cleaned)
            if hint is not None:
                return hint
        cleaned = self._replace_tables(cleaned)
        cleaned = self._replace_long_lists(cleaned)
        cleaned = self._replace_formulas(cleaned)
        cleaned = _IMAGE.sub("", cleaned)  # before the links: "![alt](url)" contains a link
        cleaned = _LINK.sub(r"\1", cleaned)
        cleaned = _BLOCKQUOTE.sub("", cleaned)
        cleaned = _BRACKETS.sub("", cleaned)  # structural markers like [Quelle]; after the links, which keep their text
        cleaned = self._remove_emojis(cleaned)
        cleaned = _HORIZONTAL_LINES.sub("\n", cleaned)
        cleaned = _VERTICAL_LINES.sub("", cleaned)
        cleaned = _ASCII_RULE.sub("\n", cleaned)
        cleaned = cleaned.replace("**", "").replace("*", "")
        cleaned = _INLINE_CODE.sub(lambda _: self._hint_once(HINT_CODE, " {} ", " "), cleaned)
        cleaned = cleaned.replace("`", "")
        cleaned = _HEADING_MARK.sub("", cleaned)
        cleaned = _BULLET.sub("", cleaned)
        cleaned = _NUMBERED.sub("", cleaned)
        cleaned = _URL.sub("", cleaned)
        cleaned = _ZERO_WIDTH.sub("", cleaned).translate(_DASHES)
        cleaned = _UNSPEAKABLE.sub(" ", cleaned)
        cleaned = _SPACES.sub(" ", cleaned)
        cleaned = _BLANK_LINES.sub("\n\n", cleaned).strip()
        return cleaned if any(char.isalnum() for char in cleaned) else ""  # "." alone is nothing to speak

    # ── State ───────────────────────────────────────────────────────

    def _reset_hints(self) -> None:
        self._announced.clear()
        self._list_items = 0

    def _hint_once(self, key: str, template: str = "{}", repeated: str = "") -> str:
        """The hint ``key`` in ``template`` the first time since the last regular text, ``repeated`` after."""
        if key in self._announced:
            return repeated
        self._announced.add(key)
        return template.format(translate(key, self._language))

    # ── Steps ───────────────────────────────────────────────────────

    def _pronounce(self, text: str) -> str:
        if self._pronunciation_pattern is None:
            return text
        return self._pronunciation_pattern.sub(lambda match: self._pronunciations[match.group(0)], text)

    def _replace_code_blocks(self, text: str) -> str:
        """Code becomes one spoken hint. A fence may open in one unit and close in a later one: the
        units in between are code too and vanish."""
        if self._inside_code_block:
            closing = text.find(CODE_FENCE)
            if closing < 0:
                return ""
            self._inside_code_block = False
            # The cutter ends a fence line with a period of its own ("```."); it is no text
            text = text[closing + len(CODE_FENCE):].lstrip(SENTENCE_FINAL_CHARS + string.whitespace)
        text = _CODE_BLOCK.sub(lambda _: self._hint_once(HINT_CODE, "\n{}\n", "\n"), text)
        opening = text.find(CODE_FENCE)
        if opening >= 0:
            self._inside_code_block = True
            text = text[:opening] + self._hint_once(HINT_CODE, "\n{}\n", "\n")
        return text.strip()

    def _hint_for_single_unit(self, text: str) -> str | None:
        """A single line (a sentence): a table row, a formula or inline code is replaced as a whole by
        its hint (the first time; "" after), a long list run is cut off after its limit. ``None`` =
        the unit is regular text. Regular text ends the block: the hints may be spoken again."""
        if text.startswith("|") or text.count("|") >= 2:
            return self._hint_once(HINT_TABLE)
        if _INLINE_FORMULA.search(text):
            return self._hint_once(HINT_FORMULA)
        if _INLINE_CODE.search(text):
            return self._hint_once(HINT_CODE)
        is_item = LIST_ITEM.match(text) is not None
        if is_item:
            self._list_items += 1
            if self._list_items > TTS_LIST_MAX_ITEMS:
                return self._hint_once(HINT_MORE_ITEMS)
        elif self._list_items > 0 and READABLE_WORD.search(text):
            self._list_items = 0  # a readable line ends the run
            self._announced.discard(HINT_MORE_ITEMS)
        if not is_item and READABLE_WORD.search(text):
            self._reset_hints()
        return None

    def _replace_tables(self, text: str) -> str:
        return _TABLE_BLOCK.sub(lambda _: self._hint_once(HINT_TABLE, "\n{}\n", "\n"), text)

    def _replace_long_lists(self, text: str) -> str:
        def replace(block: re.Match[str]) -> str:
            items = len(_LIST_ITEM_MARK.findall(block.group(0)))
            if items > TTS_LIST_MAX_ITEMS:
                return f"\n{translate(HINT_LIST, self._language, items=str(items))}\n"
            return block.group(0)

        return _BLANK_LINES.sub("\n\n", _LIST_BLOCK.sub(replace, text)).strip()

    def _replace_formulas(self, text: str) -> str:
        def replace(_: re.Match[str]) -> str:
            return self._hint_once(HINT_FORMULA, " {} ", " ")

        return _INLINE_FORMULA.sub(replace, _BLOCK_FORMULA.sub(replace, text))

    @staticmethod
    def _remove_emojis(text: str) -> str:
        """All emojis go except the laughter ones, which the container turns into laughter."""
        text = _EMOJI.sub("", text)
        return _EMOTICON.sub(lambda match: match.group(0) if match.group(0) in LAUGHTER_EMOJIS else "", text).strip()
