"""Burn-in: what a local GPU engine really takes on its card.

The engine reads long worst-case texts while its VRAM is sampled; the peak becomes its budget. Texts of
about 2 kB, because engines grow with the length of what they read (Qwen3-TTS: about 5 GB idle, about
7 GB on a long reply) and a short text ends before the decoder reaches its full allocation. One round in
each language, and every round has to deliver audio: a peak without a finished reading would only be the
idle footprint, and a budget that small lets the engine run out of memory on its first long reading.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .api import BurnInProgress
from .constants import BURN_IN_SAMPLE_INTERVAL_S

_TEXT_DE = (
    "Die schnelle braune Katze springt über den schlafenden Hund am "
    "Ufer des großen Flusses, während im Hintergrund die Glocken der "
    "alten Kirche läuten und der Wind die welken Blätter durch die "
    "engen Gassen der Altstadt treibt. Niemand bemerkt den Reisenden "
    "mit dem schweren Mantel, der gerade aus dem letzten Zug gestiegen "
    "ist und nun zielstrebig in Richtung des Marktplatzes geht, wo "
    "sich um diese späte Stunde nur noch wenige Verkäufer aufhalten. "
    "Am alten Brunnen vor dem Rathaus bleibt er stehen, holt einen "
    "vergilbten Brief aus der Innentasche seines Mantels und liest ihn "
    "noch einmal aufmerksam durch, bevor er entschlossen weitergeht. "
    "Ein paar Straßen weiter, in einer schmalen Sackgasse, öffnet sich "
    "die Tür eines kleinen Antiquariats, und der Reisende tritt ohne "
    "ein Wort der Begrüßung ein. Der Inhaber, ein hagerer alter Mann "
    "mit randloser Brille und einem grauen Tuch um die Schultern, "
    "blickt von seinem Buch auf, nickt knapp und greift nach einem "
    "verschlossenen Holzkästchen, das hinter dem Tresen aufbewahrt war. "
    "Draußen beginnt es leise zu regnen, und in den Pfützen auf dem "
    "Kopfsteinpflaster spiegeln sich die flackernden Laternen, die "
    "noch aus einer früheren Zeit stammen, in der die Straßen abends "
    "von einem Wärter mit langer Stange entzündet wurden. Der "
    "Reisende öffnet das Kästchen vorsichtig, als wäre der Inhalt aus "
    "feinstem Glas gefertigt, und betrachtet lange Zeit die ineinander "
    "verschlungenen Buchstaben einer alten Inschrift, deren Bedeutung "
    "ihm bis heute verborgen geblieben war. Der Antiquar tritt näher, "
    "räuspert sich vernehmlich und erklärt mit ruhiger, sorgsam "
    "abgewogener Stimme, dass diese Zeichen aus einer Sprache stammen, "
    "die schon vor mehr als dreihundert Jahren verstummt sein soll, "
    "als die letzten Bewohner einer abgelegenen Inselgemeinschaft im "
    "Sturm vor der Küste verschwanden und keinerlei Zeugnisse hinter "
    "sich ließen außer einigen wenigen Tafeln aus dunklem Schiefer. "
    "Während sie noch sprechen, hört man von weit her das langsame "
    "Quietschen eines Wagens, der über das nasse Pflaster rollt, und "
    "kurz darauf das gedämpfte Hufschlagen zweier müder Pferde, die "
    "ihre Last an einem unsichtbaren Ziel hin bewegen. In der "
    "schmalen Werkstatt hinter dem Antiquariat, durch eine halb "
    "geöffnete Tür sichtbar, brennt eine einzelne Kerze auf einem "
    "Tisch voller Notizen, Werkzeuge und halb zerlegter mechanischer "
    "Uhrwerke, deren feine Zahnräder im Licht der Flamme golden "
    "schimmern und an die Geduld eines vergangenen Handwerks erinnern."
)

_TEXT_EN = (
    "The quick brown fox jumps over the lazy dog by the bank of the "
    "great river, while in the background the bells of the old church "
    "are ringing and the wind drives the withered leaves through the "
    "narrow alleys of the old town. Nobody notices the traveler with "
    "the heavy coat who has just stepped off the last train and is "
    "now walking purposefully towards the market square, where only a "
    "few vendors remain at this late hour. At the old fountain in "
    "front of the town hall he stops, pulls a yellowed letter from "
    "the inner pocket of his coat, and reads it through once again "
    "carefully before continuing on with determined steps. A few "
    "streets further, in a narrow dead end, the door of a small "
    "antiquarian bookshop swings open, and the traveler steps inside "
    "without a word of greeting. The owner, a gaunt old man with "
    "rimless glasses and a grey shawl around his shoulders, looks up "
    "from his book, nods curtly, and reaches for a sealed wooden box "
    "that had been kept behind the counter. Outside it begins to rain "
    "softly, and in the puddles on the cobblestones the flickering "
    "street lamps are reflected, lamps that date from an earlier era "
    "when the streets in the evening were lit by a warden with a long "
    "pole bearing a small flame at its tip. The traveler opens the "
    "box carefully, as though its contents were fashioned from the "
    "finest glass, and studies for a long time the interlocking "
    "characters of an old inscription whose meaning has remained "
    "hidden from him to this very day. The bookseller steps closer, "
    "clears his throat audibly, and explains in a calm, carefully "
    "measured voice that these signs come from a language that is "
    "said to have fallen silent more than three hundred years ago, "
    "when the last inhabitants of a remote island community vanished "
    "in a storm off the coast and left behind no records save for a "
    "few tablets of dark slate. While they are still talking, one "
    "hears from far away the slow creaking of a cart rolling over "
    "the wet pavement, and shortly afterwards the muffled hoofbeats "
    "of two weary horses pulling their burden toward some invisible "
    "destination. In the narrow workshop behind the bookshop, visible "
    "through a half-open door, a single candle burns on a table "
    "covered with notes, tools and half-disassembled clockwork "
    "mechanisms whose delicate gears glint golden in the light of "
    "the flame, reminding one of the patience of a craft long past."
)

#: The readings of a burn-in, in order: (language, text).
BURN_IN_ROUNDS = (("de", _TEXT_DE), ("en", _TEXT_EN))


async def measure_peak_mib(
    read: Callable[[str, str], Awaitable[None]],
    sample: Callable[[], Awaitable[int]],
    progress: BurnInProgress,
) -> int:
    """The peak of ``sample`` (MiB) while ``read`` goes through :data:`BURN_IN_ROUNDS`. An error of a
    reading or of a sample ends the measurement: there is no peak to trust then."""
    peak = 0
    current_round = 0
    done = asyncio.Event()

    async def watch() -> None:
        nonlocal peak
        while not done.is_set():
            peak = max(peak, await sample())
            progress(current_round, peak)
            try:
                await asyncio.wait_for(done.wait(), BURN_IN_SAMPLE_INTERVAL_S)
            except TimeoutError:
                pass

    watcher = asyncio.create_task(watch())
    try:
        for current_round, (language, text) in enumerate(BURN_IN_ROUNDS, 1):
            await read(language, text)
    finally:
        done.set()
        await watcher
    return max(peak, await sample())  # the last reading may have ended between two samples
