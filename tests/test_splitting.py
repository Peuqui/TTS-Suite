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


# ── Look-ahead of the unit "sentence" ───────────────────────────────

FIRST = "Das ist der erste Satz."
SECOND = "Hier kommt der zweite Satz."
THIRD = "Und dies ist der dritte Satz."
# A sentence end is certain when more than 10 characters follow it (and a period needs a capital after it)
TAIL = " Nächster Anfang"


def split_text_with(text: str, immediate: int) -> list[str]:
    cutter = UnitCutter("sentence", immediate_sentences=immediate)
    return cutter.feed(text) + cutter.flush()


def test_the_first_sentence_goes_at_once_the_second_waits_for_the_third() -> None:
    cutter = UnitCutter("sentence")
    assert cutter.feed(FIRST) == []  # nothing follows it yet
    assert cutter.feed(" Hier kommt") == [FIRST]  # complete: it goes, although the next one is not even there
    assert cutter.feed(" der zweite Satz.") == []
    assert cutter.feed(f" {THIRD}") == []  # the second is complete now, but it waits for its successor
    assert cutter.feed(" Vierter Satz hier") == [SECOND]  # the third is complete: the second goes
    assert cutter.flush() == [THIRD, "Vierter Satz hier."]


def test_the_second_sentence_goes_at_flush_if_no_third_comes() -> None:
    cutter = UnitCutter("sentence")
    assert cutter.feed(f"{FIRST} {SECOND}{TAIL}") == [FIRST]
    assert cutter.flush() == [f"{SECOND}{TAIL}."]


def test_a_short_ending_is_joined_to_its_unreleased_predecessor() -> None:
    cutter = UnitCutter("sentence")
    assert cutter.feed(f"{FIRST} {SECOND} Und du?") == [FIRST]
    assert cutter.flush() == [f"{SECOND} Und du?"]
    assert split_text(f"{FIRST} Ja?", "sentence") == [f"{FIRST} Ja?"]  # the first is not out yet either


def test_a_short_ending_stays_alone_if_its_predecessor_is_out() -> None:
    cutter = UnitCutter("sentence")
    assert cutter.feed(f"{FIRST}{TAIL}") == [FIRST]
    assert cutter.feed(" Ja?") == []
    assert cutter.flush() == [f"{TAIL.strip()} Ja?"]  # joined with what came after; a lone "Ja?" is tested below
    alone = UnitCutter("sentence")
    assert alone.feed(f"{FIRST} Hier kommt der Rest davon.") == [FIRST]
    assert alone.flush() == ["Hier kommt der Rest davon."]
    assert split_text("Und du?", "sentence") == ["Und du?"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{FIRST}{TAIL}", {0: [], 1: [FIRST], 2: [FIRST]}),
        (f"{FIRST} {SECOND}{TAIL}", {0: [FIRST], 1: [FIRST], 2: [FIRST, SECOND]}),
    ],
)
@pytest.mark.parametrize("immediate", [0, 1, 2])
def test_the_number_of_immediately_released_sentences_is_a_parameter(
    text: str, expected: dict[int, list[str]], immediate: int,
) -> None:
    cutter = UnitCutter("sentence", immediate_sentences=immediate)
    released = cutter.feed(text)
    assert released == expected[immediate]
    assert released + cutter.flush() == split_text_with(text, immediate)


def test_with_no_immediate_sentence_the_first_waits_for_the_second() -> None:
    cutter = UnitCutter("sentence", immediate_sentences=0)
    assert cutter.feed(f"{FIRST}{TAIL}") == []
    assert cutter.feed(" bringt noch einen vollen Satz. Dann Ende hier") == [FIRST]


def test_the_default_is_the_named_constant() -> None:
    from tts_suite import splitting

    assert splitting.SENTENCES_RELEASED_IMMEDIATELY == 1
    cutter = UnitCutter("sentence")
    assert cutter.feed(f"{FIRST}{TAIL}") == [FIRST]  # one sentence at once


def test_look_ahead_leaves_paragraph_and_whole_alone() -> None:
    paragraphs = UnitCutter("paragraph", immediate_sentences=0)
    assert paragraphs.feed("Absatz eins hier.\n\nAbsatz zwei") == ["Absatz eins hier."]


@pytest.mark.parametrize("immediate", [0, 1, 2])
@pytest.mark.parametrize("text", TEXTS + [f"{FIRST} {SECOND} Und du?", f"{FIRST} {SECOND} {THIRD} Ja."])
def test_pieces_anywhere_give_the_same_units_with_any_look_ahead(text: str, immediate: int) -> None:
    expected = split_text_with(text, immediate)
    for size in range(1, 14):
        cutter = UnitCutter("sentence", immediate_sentences=immediate)
        units = [u for start in range(0, len(text), size) for u in cutter.feed(text[start:start + size])]
        assert units + cutter.flush() == expected, f"piece size {size}"
