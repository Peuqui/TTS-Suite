"""Speech forms: what an engine would spell out or misread becomes the words a person says.

Units after a number ("25 km/h" -> "25 Kilometer pro Stunde", the singular for exactly 1), amounts
of money ("13,50 €" -> "13 Euro 50", "0,35 €" -> "35 Cent", "$5" -> "5 dollars"; a currency sign
without a number becomes its name), a minus before a number, a dash between
two numbers ("5–10" -> "5 bis 10"; the cleaner would make a pause of it), symbols (§), common
abbreviations ("z. B." -> "zum Beispiel") and, where the table names a decimal word, the digits after
the decimal separator one by one ("3,14" -> "3 Komma 1 4", as German reads them).

Where the table gives the units a gender (German), a 1 before a unit or amount becomes the article
form: "1 h" -> "eine Stunde", "1 kg" -> "ein Kilogramm". The case comes from the word right before the
number, a preposition that always takes it ("mit 1 Stunde" -> "mit einer Stunde", "für 1 €" -> "für
einen Euro"); otherwise the nominative stays ("er läuft ein Kilometer": the accusative after a verb
would need the sentence understood). One table per language in ``speech_forms.json``; a
language without a table is left as it is. SSOT for every engine: no engine container expands
anything itself.

A unit counts only right after a number, so "m", "h" or the English "in" stay words elsewhere. A unit
of a single letter needs a space after the number ("5 m", not "5m"): "90s" is a decade and "$5m"
five million. A space inside a key ("z. B.", "sq ft") also matches a no-break or narrow space.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from .constants import SPEECH_FORMS_FILE_NAME

_NUMBER = r"\d+(?:[.,]\d+)*"  # as written: "1.000", "12,5", "1,234.50"
_NUMBER_START = r"(?<![\w.,:])"  # not the tail of a word, of another number or of a time ("14:30 h")
_SPACE = "[   ]"  # space, no-break space, narrow no-break space
_UNIT_END = r"(?![\w²³/])"  # "km" is not the start of "km/h", "m" not of "m²"
_WORD_BEFORE = re.compile(rf"(\w+){_SPACE}+$")


@cache
def _tables() -> dict[str, dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = json.loads(
        (Path(__file__).parent / SPEECH_FORMS_FILE_NAME).read_text(encoding="utf-8")
    )
    return tables


def abbreviations_of_all_languages() -> frozenset[str]:
    """The abbreviations the tables expand, lowercased: the splitter must not end a sentence in them."""
    return frozenset(abbreviation.lower() for table in _tables().values() for abbreviation in table["abbreviations"])


def _alternation(keys: Any) -> str:
    """One regex alternative per key, the longest first (so "km/h" wins over "km")."""
    def pattern(key: str) -> str:
        return "".join(_SPACE if char == " " else re.escape(char) for char in key)

    return "|".join(pattern(key) for key in sorted(keys, key=len, reverse=True))


def _key_of(written: str, keys: Any) -> str:
    """The table key of a match whose spaces may be other spaces than in the key."""
    normalized = re.sub(_SPACE, " ", written)
    return next(key for key in keys if key == normalized)


@dataclass(frozen=True)
class _Compiled:
    table: dict[str, Any]
    range_dash: re.Pattern[str]
    minus: re.Pattern[str]
    currency_first: re.Pattern[str]
    currency: re.Pattern[str]
    currency_alone: re.Pattern[str]
    cents: re.Pattern[str]
    unit: re.Pattern[str]
    symbol: re.Pattern[str]
    abbreviation: re.Pattern[str]
    decimals: re.Pattern[str]


@cache
def _compiled(language: str) -> _Compiled | None:
    table = _tables().get(language)
    if table is None:
        return None
    currencies = _alternation(table["currencies"])
    signs = _alternation(key for key in table["currencies"] if not key.isalpha())
    return _Compiled(
        table=table,
        range_dash=re.compile(rf"(?<=\d){_SPACE}?–{_SPACE}?(?=\d)"),
        minus=re.compile(r"(?<![\w.,)\]])[-−](?=\d)"),
        currency_first=re.compile(rf"(?<!\w)(?P<currency>{currencies}){_SPACE}?(?P<number>{_NUMBER})"),
        currency=re.compile(rf"{_NUMBER_START}(?P<number>{_NUMBER}){_SPACE}?(?P<currency>{currencies})(?!\w)"),
        currency_alone=re.compile(rf"{_SPACE}?(?P<sign>{signs})"),
        # Two digits after the last separator are cents in either writing: a thousands group has three.
        cents=re.compile(r"(?P<whole>\d+(?:[.,]\d{3})*)[.,](?P<cents>\d{2})"),
        unit=re.compile(
            rf"{_NUMBER_START}(?P<number>{_NUMBER})(?P<space>{_SPACE}?)(?P<unit>{_alternation(table['units'])}){_UNIT_END}"
        ),
        symbol=re.compile(rf"{_SPACE}?(?P<symbol>{_alternation(table['symbols'])}){_SPACE}?"),
        abbreviation=re.compile(rf"(?<!\w)(?:{_alternation(table['abbreviations'])})(?!\w)"),
        decimals=re.compile(
            rf"{_NUMBER_START}(?P<whole>\d+(?:{re.escape(_thousands(table))}\d{{3}})*)"
            rf"{re.escape(table['decimal_separator'])}(?P<decimals>\d+)(?![.,]?\d)"
        ),
    )


def _thousands(table: dict[str, Any]) -> str:
    return "," if table["decimal_separator"] == "." else "."


def _counted(number: str, forms: list[str], before: str, table: dict[str, Any]) -> str:
    """The number with its word: plural except for exactly one ("1,5 Stunden", "0 Grad"). ``forms`` is
    ``[singular, plural]`` or, in a table with genders, ``[singular, plural, gender]``: the 1 then
    becomes the article in the case the word before it asks for (``before`` is the text up to the number)."""
    if number != "1":
        return f"{number} {forms[1]}"
    if len(forms) < 3:  # no genders (English): the engine says "one"
        return f"{number} {forms[0]}"
    one = table["one"]
    word = _WORD_BEFORE.search(before)
    preceding = word.group(1).lower() if word else ""
    case = "dat" if preceding in one["dative_after"] else "acc" if preceding in one["accusative_after"] else "nom"
    return f"{one[forms[2]][case]} {forms[0]}"


class SpeechForms:
    """The speech forms of one language (see the module docstring)."""

    def __init__(self, language: str) -> None:
        self._compiled = _compiled(language)

    def expand(self, text: str) -> str:
        compiled = self._compiled
        if compiled is None:
            return text
        table = compiled.table
        text = compiled.range_dash.sub(f" {table['range']} ", text)
        text = compiled.minus.sub(f"{table['minus']} ", text)
        text = compiled.currency_first.sub(r"\g<number> \g<currency>", text)
        text = compiled.currency.sub(lambda match: self._amount(match, compiled), text)
        text = compiled.unit.sub(lambda match: self._unit(match, table), text)
        text = compiled.symbol.sub(lambda match: f" {table['symbols'][match.group('symbol')]} ", text)
        text = compiled.abbreviation.sub(lambda match: self._abbreviation(match, table), text)
        if "decimal_word" in table:  # after the amounts and units: they read the number as written
            text = compiled.decimals.sub(
                lambda match: f"{match.group('whole')} {table['decimal_word']} {' '.join(match.group('decimals'))}", text,
            )
        # A sign the amounts left ("5 Mio. €", "Preis in €") would be dropped as unspeakable.
        return compiled.currency_alone.sub(lambda match: f" {table['currencies'][match.group('sign')][1]}", text)

    @staticmethod
    def _amount(match: re.Match[str], compiled: _Compiled) -> str:
        """"13,50 €" -> "13 Euro 50", "13,05 €" -> "13 Euro 5", "0,35 €" -> "35 Cent": as said aloud.
        The forms are ``[unit, units, minor unit, minor units]``, a table with genders adds the gender
        of each."""
        table = compiled.table
        forms = table["currencies"][_key_of(match.group("currency"), table["currencies"])]
        major, minor = forms[0:2] + forms[4:5], forms[2:4] + forms[5:6]
        before = match.string[:match.start()]
        number = match.group("number")
        split = compiled.cents.fullmatch(number)
        if split is None:
            return _counted(number, major, before, table)
        whole, cents = split.group("whole"), split.group("cents").lstrip("0")
        if whole == "0":
            return _counted(cents or "0", minor, before, table)
        spoken = _counted(whole, major, before, table)
        return f"{spoken} {cents}" if cents else spoken

    @staticmethod
    def _unit(match: re.Match[str], table: dict[str, Any]) -> str:
        unit = _key_of(match.group("unit"), table["units"])
        if len(unit) == 1 and unit.isalpha() and not match.group("space"):
            return match.group(0)  # "90s", "5m": a single letter right after the number is no unit
        return _counted(match.group("number"), table["units"][unit], match.string[:match.start()], table)

    @staticmethod
    def _abbreviation(match: re.Match[str], table: dict[str, Any]) -> str:
        spoken: str = table["abbreviations"][_key_of(match.group(0), table["abbreviations"])]
        # At the end of the unit the period was also the end of the sentence: it stays.
        return f"{spoken}." if not match.string[match.end():].strip() else spoken
