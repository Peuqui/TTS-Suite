"""Cutting a text into units: sentences, paragraphs, whole, and the cutter for a text in pieces."""
from __future__ import annotations

import random

import pytest

from tts_suite.api import Unit
from tts_suite.splitting import UnitCutter, split_text

UNITS: tuple[Unit, ...] = ("sentence", "paragraph", "whole")

TEXTS = [
    "Dr. Müller kam um 3.5 Uhr an. Er sagte z.B. nichts! Ok.\n\nEine Überschrift\n\nDas ist ein weiterer Satz mit Inhalt: "
    "Hier die Liste.\n\n| a | b. |\nSiehe http://x.de/a.b für mehr. Ende dieses Textes hier.",
    "Hallo Welt. Wie geht es dir heute? Mir geht es gut, danke der Nachfrage! Und dir?",
    "Er sagte: \"Das ist wirklich ein guter Plan.\" Dann ging er (ohne ein Wort zu sagen) nach Hause. Fertig und aus.",
    "Wait... Was war das? Nichts weiter. Ja.",
    "Hier nun die gewünschte Tabelle:\n\n| a | b |\n| 1 | 2 |\n\nDanach folgt ein Absatz mit mehreren Wörtern drin.",
    "Der Preis liegt bei 3.5 Euro und das ist gut. Danke schön für alles.\r\n\r\nNeuer Absatz mit Worten.",
]


def test_sentences_respect_abbreviations_decimals_and_urls() -> None:
    text = "Dr. Müller kam um 3.5 Uhr an. Er sagte z.B. nichts! Siehe http://x.de/a.b für mehr Infos."
    assert split_text(text, "sentence") == [
        "Dr. Müller kam um 3.5 Uhr an.", "Er sagte z.B. nichts!", "Siehe http://x.de/a.b für mehr Infos.",
    ]


def test_a_word_that_merely_ends_like_an_abbreviation_ends_its_sentence() -> None:
    # "Haus." ends with "s." and "noch." with "ch."; AIfred took them for abbreviations
    assert split_text("Das ist das Haus. Und es ist wirklich groß. Ich weiß es noch. Dann kam er auch.", "sentence") == [
        "Das ist das Haus.", "Und es ist wirklich groß.", "Ich weiß es noch.", "Dann kam er auch.",
    ]


def test_a_heading_without_punctuation_is_a_sentence_of_its_own() -> None:
    assert split_text("Eine kurze Überschrift hier\n\nDer Text danach hat mehrere Wörter.", "sentence") == [
        "Eine kurze Überschrift hier.", "Der Text danach hat mehrere Wörter.",
    ]


def test_a_sentence_with_too_few_words_goes_to_the_next() -> None:
    assert split_text("Ja. Das ist ein längerer Satz hier.", "sentence") == ["Ja. Das ist ein längerer Satz hier."]


def test_paragraphs_are_cut_at_blank_lines() -> None:
    assert split_text("Eins hier.\r\n \r\nZwei hier.\n\n\nDrei hier", "paragraph") == ["Eins hier.", "Zwei hier.", "Drei hier"]


def test_whole_is_one_unit() -> None:
    assert split_text("  Eins hier.\n\nZwei hier.  ", "whole") == ["Eins hier.\n\nZwei hier."]


@pytest.mark.parametrize("unit", UNITS)
def test_an_empty_text_has_no_units(unit: Unit) -> None:
    assert split_text(" \n ", unit) == []


@pytest.mark.parametrize("unit", UNITS)
@pytest.mark.parametrize("text", TEXTS)
def test_pieces_anywhere_give_the_units_of_the_whole_text(text: str, unit: Unit) -> None:
    expected = split_text(text, unit)
    for size in range(1, 14):
        cutter = UnitCutter(unit)
        units = [u for start in range(0, len(text), size) for u in cutter.feed(text[start:start + size])]
        assert units + cutter.flush() == expected, f"piece size {size}"
    for seed in range(40):
        picker = random.Random(seed)
        cutter, units, position = UnitCutter(unit), [], 0
        while position < len(text):
            step = picker.randint(1, 12)
            units += cutter.feed(text[position:position + step])
            position += step
        assert units + cutter.flush() == expected, f"seed {seed}"


def test_a_sentence_is_released_when_its_end_is_certain() -> None:
    cutter = UnitCutter("sentence")
    assert cutter.feed("Das ist ein Satz.") == []  # a decimal number could follow
    assert cutter.feed(" Und der nächste Satz") == ["Das ist ein Satz."]
    assert cutter.flush() == ["Und der nächste Satz."]


def test_a_paragraph_is_released_at_its_blank_line_and_whole_at_the_end() -> None:
    paragraphs = UnitCutter("paragraph")
    assert paragraphs.feed("Absatz eins hier.\n") == []
    assert paragraphs.feed("\nAbsatz zwei") == ["Absatz eins hier."]
    assert paragraphs.flush() == ["Absatz zwei"]
    whole = UnitCutter("whole")
    assert whole.feed("Absatz eins.\n\nAbsatz zwei.") == []
    assert whole.flush() == ["Absatz eins.\n\nAbsatz zwei."]


def test_the_unit_may_be_set_after_the_first_text() -> None:
    cutter = UnitCutter()
    assert cutter.feed("Erster Satz hier mit Worten. Zweiter Satz folgt danach, noch ein Stück") == []
    assert cutter.set_unit("sentence") == ["Erster Satz hier mit Worten."]
    assert cutter.feed(" und weiter.") == []
    assert cutter.flush() == ["Zweiter Satz folgt danach, noch ein Stück und weiter."]


def test_the_unit_is_set_once_and_must_be_valid() -> None:
    cutter = UnitCutter("whole")
    with pytest.raises(RuntimeError, match="already set"):
        cutter.set_unit("sentence")
    with pytest.raises(ValueError, match="speech unit"):
        UnitCutter("word")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="not set"):
        UnitCutter().flush()
