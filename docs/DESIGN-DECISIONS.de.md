# Entwurfsentscheidungen

[English](DESIGN-DECISIONS.md)

Entscheidungen, die den Aufbau der TTS-Suite prägen, mit Begründung und mit dem, was
wir tun, falls sich eine Annahme als falsch erweist. Stand: 11.10.2026.

## Ein Dienst als einziger Besitzer der Karten

Die Suite läuft als Systemdienst mit niedrigen Rechten auf dem Rechner mit den GPUs. Sie
startet und stoppt die Engine-Container und wählt die Karte, für alle Programme, die sie
nutzen. Mehrere Programme teilen dieselben Karten; eine Bibliothek in jedem Programm
hätte mehrere Besitzer, die einander nicht kennen. Weitere Rechner (SSH) sind eine
optionale Liste in der Konfiguration, der Normalfall ist der eigene Rechner.

## Kartenwahl und Speichergrenze

Die Wahl der Karte folgt Regeln der Konfiguration (`gpus`: Reihenfolge und Reserve je
Karte, `headroom_mib`), nicht einem Rückruf an ein Programm. Jede Engine hat ein Budget
(`vram_budget_mib`, Startwert aus dem Burn-in der Engine, die Konfiguration überschreibt
es). Eine Karte kommt in Frage, wenn ihr freier Speicher abzüglich Reserve das Budget plus
Aufschlag deckt (Standard 512 MiB, derselbe wie bei AIfred bisher).

Das Budget dient der Kartenwahl und ist zugleich die Kappe im Container
(`TTS_VRAM_BUDGET_MIB`, beim `up` durchgereicht). Der Dienst beobachtet den Verbrauch nicht
und stoppt keine Engine; `EngineInfo.vram_mib` meldet ihn nur zur Anzeige. Eine Karte ohne
Platz ergibt `no_vram`, das nicht gemerkt wird: der nächste Lauf wählt neu.
Gemessen am 11.10.2026 auf einer V100 mit einer Einhängedatei (`sitecustomize.py`, setzt
beim ersten CUDA-Zugriff `set_per_process_memory_fraction`): Qwen3-TTS und XTTS laufen
unter einer Grenze knapp über ihrem Bedarf unverändert, bei einer Grenze unter dem
Modellbedarf scheitert das Laden mit `OutOfMemoryError`. Die Grenze zählt nur
PyTorch-Speicher, nicht den CUDA-Kontext; sie ist ein Ausreißerschutz, keine exakte Grenze. Der Mechanismus nutzt `torch.cuda._lazy_call`,
eine private PyTorch-Schnittstelle, die bei einem Update brechen kann.

## Der Dienst zerlegt den Text

Der Aufrufer schickt Text so, wie er ihn hat: ganz (`POST /speak`) oder in Stücken, während
ein Sprachmodell noch schreibt (WebSocket `/speak/stream`). Die Zerlegung nach der
Einheit der sprechenden Engine (Satz, Absatz, ganz) geschieht im Dienst, weil nur er weiß,
welche Engine gerade spricht, wenn die Eskalationsliste weiterspringt. Das Audio kommt
immer als Strom von Teilstücken in Sprechreihenfolge zurück, sobald sie fertig sind.

## Textbereinigung im Dienst

Die allgemeine Bereinigung (Markdown, Code und Tabellen als gesprochener Hinweis, LaTeX,
URLs, Links, HTML, Emojis) läuft im Dienst vor der Zerlegung, mit dem Zustand des Laufs
(ein Codeblock über mehrere Stücke, Hinweise einmal je Antwort). Aussprache-Ersetzungen
(zum Beispiel „AIfred“ → „Alfred“) sind eine Konfigurationsliste. Programmspezifische
Teile bleiben beim Programm: AIfred filtert seine Denk- und Detail-Blöcke selbst, weil ein
anderer Aufrufer sie sprechen lassen möchte. Die Engine-spezifische Normalisierung
(Sonderzeichen, die den Tokenizer abstürzen lassen, Satzzeichen gegen Halluzinationen)
bleibt im Container.

## Vorausschau bei der Satz-Einheit

Ein Satz gilt als beendet, wenn mehr als 10 Zeichen darauf folgen oder der Text zu Ende
ist (schützt vor Abkürzungen, Dezimalzahlen, URLs und Anführungszeichen).

**Entscheidung: Der erste Satz geht sofort, ab dem zweiten gibt die Suite einen Satz erst
frei, wenn der nächste da ist.** Beim ersten Satz zählt die Zeit bis zum ersten Ton. Danach
spricht die Engine ohnehin langsamer, als ein Sprachmodell schreibt (gesprochen etwa 4
Tokens pro Sekunde), der nächste Satz ist also meistens schon da; die Vorausschau kostet
dort nichts. Gewinn: sicherere Satzgrenzen, und ein sehr kurzer Schluss (unter drei
Wörtern, „Und dir?“) lässt sich an den Vorgänger hängen, solange er nicht freigegeben ist.

**Wenn sich das als zu schwach erweist** (zu viele falsch getrennte Sätze, zu kurze Stücke
klingen schlecht), gibt es zwei Stufen, in dieser Reihenfolge:

1. Auch den **zweiten** Satz zurückhalten, bis der dritte da ist.
2. Auch den **ersten** Satz zurückhalten, bis der zweite da ist. Das kostet bis zum ersten
   Ton die Erzeugungszeit eines Satzes (grob 2,5 bis 3,5 Sekunden bei 10 Tokens pro
   Sekunde, etwa eine halbe Sekunde bei 50), dafür ist die Erkennung am sichersten.

Die Anzahl der Sätze, die zurückgehalten werden, soll deshalb eine Konstante bleiben, kein
fest verdrahteter Sonderfall im Code. Zwei- und Einwort-Sätze sind bei beiden Stufen der
Grenzfall: Ob die Engines sehr kurze Stücke gut sprechen, zeigt der Betrieb.
