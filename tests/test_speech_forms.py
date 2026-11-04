"""Speech forms: units after a number, amounts, minus, ranges, symbols, abbreviations and German
decimals become the words a person says, for every engine; what only looks like them stays."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tts_suite.cleaning import TextCleaner
from tts_suite.constants import SPEECH_FORMS_FILE_NAME
from tts_suite.speech_forms import SpeechForms, abbreviations_of_all_languages
from tts_suite.splitting import PROTECTED_ABBREVIATIONS, split_text

DE = SpeechForms("de")
EN = SpeechForms("en")


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Wind mit 25 km/h", "Wind mit 25 Kilometer pro Stunde"),
        ("1 h Sonne, 2 h Regen", "1 Stunde Sonne, 2 Stunden Regen"),
        ("1 t und 3 t", "1 Tonne und 3 Tonnen"),
        ("100 m² und 5 m2", "100 Quadratmeter und 5 Quadratmeter"),
        ("1 kWh, 2,5 kWh", "1 Kilowattstunde, 2 Komma 5 Kilowattstunden"),
        ("48 GiB bei 85 tok/s", "48 Gibibyte bei 85 Token pro Sekunde"),
        ("5 mph sind 1 mi", "5 Meilen pro Stunde sind 1 Meile"),
        ("72 °F und 20° C", "72 Grad Fahrenheit und 20 Grad Celsius"),
        ("6,5 l/100 km", "6 Komma 5 Liter auf hundert Kilometer"),
        ("100km und 5kWh", "100 Kilometer und 5 Kilowattstunden"),
        ("25 km/h", "25 Kilometer pro Stunde"),
    ],
)
def test_units_after_a_number_are_spoken_with_their_number(written: str, spoken: str) -> None:
    assert DE.expand(written) == spoken


@pytest.mark.parametrize(
    "written",
    [
        "5mal versucht",  # a unit is a word of its own, not the start of one
        "Es ist 14:30 h",  # a time, not thirty hours
        "das m und das h",  # no number before it
        "5m Abstand",  # a single letter needs a space after the number
        "Er kam 5 Minuten später",  # already a word
    ],
)
def test_what_only_looks_like_a_unit_stays(written: str) -> None:
    assert DE.expand(written) == written


def test_english_decades_and_millions_stay_and_units_take_the_english_plural() -> None:
    assert EN.expand("In the 90s we had 5m users") == "In the 90s we had 5m users"
    assert EN.expand("6 ft 2 in. tall, 1 lb and 3 lbs") == "6 feet 2 inches tall, 1 pound and 3 pounds"
    assert EN.expand("1 h at 60 mph") == "1 hour at 60 miles per hour"


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("13,50 €", "13 Euro 50"),
        ("13,05 €", "13 Euro 5"),
        ("1,00 €", "1 Euro"),
        ("0,35 €", "35 Cent"),
        ("$0.35", "35 Cent"),
        ("0,01 £", "1 Penny"),
        ("1.234,56 €", "1.234 Euro 56"),
        ("1.000 €", "1.000 Euro"),
        ("CHF 20", "20 Franken"),
        ("5 Mio. €", "5 Millionen Euro"),
        ("Preis in €", "Preis in Euro"),
    ],
)
def test_amounts_are_said_as_people_say_them(written: str, spoken: str) -> None:
    assert DE.expand(written) == spoken


def test_english_amounts() -> None:
    assert EN.expand("It costs $19.99, €0.50 or £1") == "It costs 19 dollars 99, 50 cents or 1 pound"


def test_minus_range_and_symbols() -> None:
    assert DE.expand("Es hat -3 °C") == "Es hat minus 3 Grad Celsius"
    assert DE.expand("COVID-19 und Seite 3-5") == "COVID-19 und Seite 3-5"  # no minus inside a word or between numbers
    assert DE.expand("5–10 m/s") == "5 bis 10 Meter pro Sekunde"
    assert EN.expand("5 – 10 km") == "5 to 10 kilometers"
    assert DE.expand("laut § 5 Abs. 2 und §§ 3") == "laut Paragraph 5 Absatz 2 und Paragraphen 3"


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Wir fliegen z. B. heute", "Wir fliegen zum Beispiel heute"),
        ("Wir fliegen z.B. heute", "Wir fliegen zum Beispiel heute"),
        ("Wir fliegen z. B. heute", "Wir fliegen zum Beispiel heute"),
        ("Thermik, d. h. gute Bedingungen", "Thermik, das heißt gute Bedingungen"),
        ("Wolken, Wind usw.", "Wolken, Wind und so weiter."),  # at the end the period stays the sentence end
        ("Das kostet ca. 5 € inkl. MwSt. heute", "Das kostet circa 5 Euro inklusive Mehrwertsteuer heute"),
        ("Im 3. Jh. v. Chr. war das", "Im 3. Jahrhundert vor Christus war das"),
    ],
)
def test_abbreviations_are_spoken_out(written: str, spoken: str) -> None:
    assert DE.expand(written) == spoken


def test_english_abbreviations() -> None:
    assert EN.expand("Gliders, e.g. the ASK 21, i.e. trainers") == "Gliders, for example the ASK 21, that is trainers"


def test_german_decimals_are_read_digit_by_digit_english_ones_left_to_the_engine() -> None:
    assert DE.expand("Pi ist 3,14") == "Pi ist 3 Komma 1 4"
    assert DE.expand("1,2,3 Punkte") == "1,2,3 Punkte"  # a list of numbers, no decimal
    assert EN.expand("Pi is 3.14") == "Pi is 3.14"


def test_a_language_without_a_table_is_left_as_it_is() -> None:
    assert SpeechForms("fr").expand("25 km/h, z. B. 13,50 €") == "25 km/h, z. B. 13,50 €"


# ---- in the cleaner ------------------------------------------------------------------------


def test_the_cleaner_expands_before_it_drops_unspeakable_signs_and_after_the_urls() -> None:
    cleaner = TextCleaner("de", {})
    assert cleaner.clean("Die Wohnung hat 80 m² für 950 € im Monat.") == (
        "Die Wohnung hat 80 Quadratmeter für 950 Euro im Monat."
    )
    assert cleaner.clean("Siehe https://example.org/5km/h und 5–10 km.") == "Siehe und 5 bis 10 Kilometer."


# ---- in the splitter ------------------------------------------------------------------------


def test_an_abbreviation_of_several_parts_reaches_the_cleaner_whole() -> None:
    units = [unit.text for unit in split_text("Wir fliegen z. B. am Samstag. Der Wind kommt aus West.", "sentence")]
    assert units == ["Wir fliegen z. B. am Samstag.", "Der Wind kommt aus West."]


def test_every_abbreviation_the_tables_expand_is_protected_by_the_splitter() -> None:
    assert abbreviations_of_all_languages() <= PROTECTED_ABBREVIATIONS


# ---- the table ---------------------------------------------------------------------------------


def test_the_table_has_no_duplicate_keys_and_complete_forms() -> None:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [key for key, _ in pairs]
        assert len(keys) == len(set(keys)), [key for key in keys if keys.count(key) > 1]
        return dict(pairs)

    path = Path(__file__).parents[1] / "src" / "tts_suite" / SPEECH_FORMS_FILE_NAME
    tables = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    for language, table in tables.items():
        assert all(len(forms) == 2 for forms in table["units"].values()), language
        assert all(len(forms) == 4 for forms in table["currencies"].values()), language
        assert all(key.endswith(".") for key in table["abbreviations"]), language
