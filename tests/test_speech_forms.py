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
        ("1 h Sonne, 2 h Regen", "eine Stunde Sonne, 2 Stunden Regen"),
        ("1 t und 3 t", "eine Tonne und 3 Tonnen"),
        ("100 m² und 5 m2", "100 Quadratmeter und 5 Quadratmeter"),
        ("1 kWh, 2,5 kWh", "eine Kilowattstunde, 2 Komma 5 Kilowattstunden"),
        ("48 GiB bei 85 tok/s", "48 Gibibyte bei 85 Token pro Sekunde"),
        ("5 mph sind 1 mi", "5 Meilen pro Stunde sind eine Meile"),
        ("72 °F und 20° C", "72 Grad Fahrenheit und 20 Grad Celsius"),
        ("6,5 l/100 km", "6 Komma 5 Liter auf hundert Kilometer"),
        ("100km und 5kWh", "100 Kilometer und 5 Kilowattstunden"),
        ("25 km/h", "25 Kilometer pro Stunde"),
    ],
)
def test_units_after_a_number_are_spoken_with_their_number(written: str, spoken: str) -> None:
    assert DE.expand(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Es hat 1 h Sonne", "Es hat eine Stunde Sonne"),  # the gender from the table
        ("Er wiegt 1 kg", "Er wiegt ein Kilogramm"),
        ("Das sind 1 km", "Das sind ein Kilometer"),
        ("Mit 1 kg Mehl", "Mit einem Kilogramm Mehl"),  # the case from the preposition before it
        ("in 1 min", "in einer Minute"),
        ("nach 1 km", "nach einem Kilometer"),
        ("für 1 € mehr", "für einen Euro mehr"),
        ("um 1 °C wärmer", "um ein Grad Celsius wärmer"),
        ("mit 1,50 €", "mit einem Euro 50"),
        ("für 0,01 £", "für einen Penny"),
        ("Er läuft 1 km", "Er läuft ein Kilometer"),  # the accusative after a verb needs the sentence understood
    ],
)
def test_one_before_a_unit_is_the_article_of_its_gender_and_case(written: str, spoken: str) -> None:
    assert DE.expand(written) == spoken


def test_english_keeps_the_figure_one() -> None:
    assert EN.expand("It takes 1 h and 1 mi for $1") == "It takes 1 hour and 1 mile for 1 dollar"


@pytest.mark.parametrize(
    "written",
    [
        "5mal versucht",  # a unit is a word of its own, not the start of one
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
        ("1,00 €", "ein Euro"),
        ("0,35 €", "35 Cent"),
        ("$0.35", "35 Cent"),
        ("0,01 £", "ein Penny"),
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
        genders = "one" in table  # [singular, plural, gender]; currencies also the gender of the minor unit
        assert all(len(forms) == (3 if genders else 2) for forms in table["units"].values()), language
        assert all(len(forms) == (6 if genders else 4) for forms in table["currencies"].values()), language
        if genders:
            used = {forms[2] for forms in table["units"].values()} | {
                gender for forms in table["currencies"].values() for gender in forms[4:]
            }
            assert used <= set(table["one"]) - {"dative_after", "accusative_after"}, language
        assert all(key.endswith(".") for key in table["abbreviations"]), language


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Foto 3840 x 2160 Pixel", "Foto 3840 mal 2160 Pixel"),
        ("Auflösung 3840×2160", "Auflösung 3840 mal 2160"),
        ("Format 1920x1080x3", "Format 1920 mal 1080 mal 3"),
        ("Zimmer 3x4 m", "Zimmer 3 mal 4 Meter"),
        ("Platte 2,5 X 4,5", "Platte 2 Komma 5 mal 4 Komma 5"),
    ],
)
def test_dimensions_are_said_as_a_product(written: str, spoken: str) -> None:
    assert DE.expand(written) == spoken
    assert EN.expand("Photo 3840 x 2160") == "Photo 3840 by 2160"


@pytest.mark.parametrize(
    "written",
    [
        "Nummer 0x1F4A",  # a hexadecimal number
        "Faktor 2x schneller",  # no number after the x
        "Version x86 und A4x",  # no number before it
        "Das Modell X3 kommt",
    ],
)
def test_what_only_looks_like_a_dimension_stays(written: str) -> None:
    assert DE.expand(written) == written


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("5 + 3 = 8", "5 plus 3 gleich 8"),
        ("Tom & Jerry", "Tom und Jerry"),
        ("Wert ≥ 5 und ≤ 10", "Wert größer gleich 5 und kleiner gleich 10"),
        ("x != 3 und a >= b", "x ungleich 3 und a größer gleich b"),
        ("5 ± 2 und 3 ÷ 4 und ≈ 7", "5 plus minus 2 und 3 geteilt durch 4 und ungefähr 7"),
        ("a < b und c > d", "a kleiner als b und c größer als d"),
        ("Ruf +49 151 an", "Ruf plus 49 151 an"),
        ("2 × 3", "2 mal 3"),
    ],
)
def test_the_signs_of_a_calculation_are_spoken(written: str, spoken: str) -> None:
    assert DE.expand(written).split() == spoken.split()  # the words, whatever the spaces around them


def test_english_signs() -> None:
    assert EN.expand("5 + 3 = 8 and a <= b").split() == ["5", "plus", "3", "equals", "8", "and", "a", "less", "than", "or", "equal", "to", "b"]


@pytest.mark.parametrize("written", ["C++ ist gut", "Tabelle &amp; Stuhl", "a->b und c=>d", "a==b"])
def test_signs_inside_something_else_stay_unspoken_words(written: str) -> None:
    spoken = DE.expand(written)
    assert "plus" not in spoken and "gleich" not in spoken and "kleiner" not in spoken and "größer" not in spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("um 14:30 Uhr", "um 14 Uhr 30"),
        ("um 9:05 und 18:00", "um 9 Uhr 5 und 18 Uhr"),
        ("Es ist 14:30 h", "Es ist 14 Uhr 30"),
        ("am 12.11.2026", "am 12. November 2026"),
        ("am 1.3.2026", "am 1. März 2026"),
        ("Stand 2026-11-12", "Stand 12. November 2026"),
        ("1/2 Liter und 3/4 Stunde", "ein halb Liter und drei Viertel Stunde"),
    ],
)
def test_times_dates_and_fractions_are_spoken(written: str, spoken: str) -> None:
    assert DE.expand(written) == spoken


@pytest.mark.parametrize(
    "written",
    [
        "Verhältnis 3:1",  # a ratio, minutes have two digits
        "Uhrzeit 14:30:15",  # hours, minutes and seconds
        "Datum 1/2/2026",  # not a fraction
        "Version 1.2.3 und 12.11.26",
        "Seite 5/8",
        "Zeit 24:00 und 12:75",
    ],
)
def test_what_only_looks_like_a_time_a_date_or_a_fraction_stays(written: str) -> None:
    assert DE.expand(written) == written


def test_english_dates_and_the_hour_stay_as_the_engine_reads_them() -> None:
    assert EN.expand("On 2026-11-12 at 14:30, 1/2 cup") == "On November 12, 2026 at 14:30, one half cup"


def test_the_cleaner_speaks_them_for_every_engine() -> None:
    assert TextCleaner("de", {}).clean("Das Foto hat 3840×2160 Pixel, 5 + 3 = 8.") == "Das Foto hat 3840 mal 2160 Pixel, 5 plus 3 gleich 8."
    assert TextCleaner("de", {}).clean("Ist a < b und c > d?") == "Ist a kleiner als b und c größer als d?"
    assert TextCleaner("de", {}).clean("Zeile <b>fett</b> bleibt Text") == "Zeile fett bleibt Text"  # a real tag still goes
