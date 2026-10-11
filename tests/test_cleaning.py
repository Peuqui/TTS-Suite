"""Text cleaning: what is removed, what becomes a hint, and the state of one reading."""
from __future__ import annotations

import pytest

from tts_suite.cleaning import TextCleaner
from tts_suite.constants import TTS_LIST_MAX_ITEMS
from tts_suite.i18n import UnsupportedLanguage, translate


def cleaner(language: str = "de", **pronunciations: str) -> TextCleaner:
    return TextCleaner(language, pronunciations)


def hint(key: str, language: str = "de") -> str:
    return translate(key, language)


def clean_all(units: list[str], language: str = "de") -> list[str]:
    text_cleaner = cleaner(language)
    return [text_cleaner.clean(unit) for unit in units]


def test_markdown_html_urls_and_links_are_cleaned() -> None:
    text_cleaner = cleaner()
    assert text_cleaner.clean("Das ist **fett** und *kursiv*<br/> mit <b>Tag</b>.") == "Das ist fett und kursiv mit Tag."
    assert text_cleaner.clean("## Überschrift hier") == "Überschrift hier"
    assert text_cleaner.clean("Siehe [diese Seite](https://example.org/x) und https://example.org/y sowie www.example.org.") == (
        "Siehe diese Seite und sowie"
    )
    assert text_cleaner.clean("<!--USED_SOURCES: {} -->Nur der Text.") == "Nur der Text."
    assert text_cleaner.clean("> Zitierte Zeile hier.") == "Zitierte Zeile hier."
    assert text_cleaner.clean("Ein Bild ![alt](http://x.org/a.png) hier im Text.") == "Ein Bild hier im Text."


def test_dashes_become_commas_and_unspeakable_symbols_vanish() -> None:
    assert cleaner().clean("Text – mehr Text → und ± das.") == "Text , mehr Text und das."


def test_emojis_go_but_laughter_stays_for_the_container() -> None:
    text_cleaner = cleaner()
    assert text_cleaner.clean("Gut gemacht 👍 und Sonne ☀️ hier.") == "Gut gemacht und Sonne hier."
    assert text_cleaner.clean("Das war lustig 😂 wirklich.") == "Das war lustig 😂 wirklich."
    assert text_cleaner.clean("Traurig 😢 ist es.") == "Traurig ist es."


def test_code_is_spoken_as_a_hint_once_per_block() -> None:
    assert clean_all(["Nutze `print(x)` hier.", "Und `foo` dort.", "Normaler Text danach.", "Wieder `bar` hier."]) == [
        hint("tts_hint_code"), "", "Normaler Text danach.", hint("tts_hint_code"),
    ]


def test_a_code_block_inside_one_unit_becomes_the_hint() -> None:
    assert cleaner().clean("Beispiel:\n```python\nx = 1\n```\nDanach geht es weiter.") == (
        f"Beispiel:\n\n{hint('tts_hint_code')}\n\nDanach geht es weiter."
    )


def test_a_code_block_over_several_units_is_one_hint_and_the_text_after_it_is_spoken() -> None:
    units = ["Hier ein Beispiel für dich.", "```python.", "x = 1.", "print(x).", "```.", "Danach geht es wirklich weiter."]
    assert clean_all(units) == [
        "Hier ein Beispiel für dich.", hint("tts_hint_code"), "", "", "", "Danach geht es wirklich weiter.",
    ]


def test_text_in_the_unit_that_closes_a_code_block_is_kept() -> None:
    text_cleaner = cleaner()
    assert text_cleaner.clean("Vorher ```x = 1") == f"Vorher \n{hint('tts_hint_code')}"
    assert text_cleaner.clean("y = 2``` und dann geht es hier weiter.") == "und dann geht es hier weiter."


def test_tables_are_one_hint_per_block() -> None:
    assert clean_all(["| Name | Alter |", "| --- | --- |", "| Anna | 3 |", "Dazu gibt es einen Text.", "| a | b |"]) == [
        hint("tts_hint_table"), "", "", "Dazu gibt es einen Text.", hint("tts_hint_table"),
    ]
    assert cleaner().clean("Vor der Tabelle.\n\n| a | b |\n| 1 | 2 |\n\nNach der Tabelle.") == (
        f"Vor der Tabelle.\n\n{hint('tts_hint_table')}\n\nNach der Tabelle."
    )


def test_formulas_are_a_hint() -> None:
    assert clean_all(["Es gilt $E = mc^2$ immer.", "Ebenso $a+b$ hier."]) == [hint("tts_hint_formula"), ""]
    assert cleaner().clean("Block:\n$$x^2 + y^2$$\nEnde des Textes.") == f"Block:\n {hint('tts_hint_formula')} \nEnde des Textes."


def test_a_long_list_is_cut_off_after_the_limit_in_a_run_of_units() -> None:
    items = [f"- Punkt Nummer {number}" for number in range(1, TTS_LIST_MAX_ITEMS + 3)]
    result = clean_all(items + ["Das war die Liste, danke."])
    assert result[:TTS_LIST_MAX_ITEMS] == [f"Punkt Nummer {number}" for number in range(1, TTS_LIST_MAX_ITEMS + 1)]
    assert result[TTS_LIST_MAX_ITEMS:] == [hint("tts_hint_more_items"), "", "Das war die Liste, danke."]


def test_a_long_list_in_one_text_is_one_hint_with_the_count() -> None:
    count = TTS_LIST_MAX_ITEMS + 1
    text = "Das gibt es:\n" + "\n".join(f"- Punkt {number}" for number in range(count)) + "\nEnde."
    assert cleaner().clean(text) == (
        f"Das gibt es:\n\n{translate('tts_hint_list', 'de', items=str(count))}\nEnde."
    )
    short = "Das gibt es:\n- eins\n- zwei\nEnde."
    assert cleaner().clean(short) == "Das gibt es:\neins\nzwei\nEnde."


def test_the_hints_follow_the_language_of_the_request() -> None:
    assert clean_all(["Nutze `x` hier."], "en") == ["There is code here."]
    with pytest.raises(UnsupportedLanguage):
        cleaner("fr").clean("Nutze `x` hier.")


def test_two_readings_do_not_influence_each_other() -> None:
    first, second = cleaner(), cleaner()
    assert first.clean("Nutze `x` hier.") == hint("tts_hint_code")
    assert first.clean("Auch `y` hier.") == ""
    assert second.clean("Nutze `x` hier.") == hint("tts_hint_code")
    first.clean("```python")  # opens a block in one reading only
    assert second.clean("Ein ganz normaler Satz.") == "Ein ganz normaler Satz."


def test_pronunciations_replace_whole_words_with_the_case_as_given() -> None:
    text_cleaner = cleaner(AIfred="Alfred", **{"z. B.": "zum Beispiel"})
    assert text_cleaner.clean("AIfred sagt: AIfredo bleibt, aifred auch, z. B. so.") == (
        "Alfred sagt: AIfredo bleibt, aifred auch, zum Beispiel so."
    )


def test_a_unit_with_nothing_to_speak_is_empty() -> None:
    assert clean_all(["...", "   ", "<!-- nur Kommentar -->", "😢", "---"]) == [""] * 5
