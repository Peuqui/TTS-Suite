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

**Liste je Anfrage, Katalog, keine gemeinsame Liste (11.10.2026).** Der Dienst führt
keine Eskalationsliste, die alle Programme mitbenutzen. Er bietet alle Engines an
(Katalog, `GET /engines`: eine Zeile je Engine auf diesem Rechner und je konfiguriertem
Rechner für jede Engine mit REST-Schnittstelle; Cloud- und Browser-Engines laufen nur
lokal). Jede Anfrage bringt ihre eigene Liste mit: Auswahl, Reihenfolge, Einheit und
Rechner je Eintrag. Kein Programm besitzt eine Liste, die ein anderes mitbenutzt; AIfred
ist nicht privilegiert, weitere Programme sind denkbar. Nichts in der Anfrage ist
optional: eine leere Liste, ein doppelter Eintrag (dieselbe Engine auf demselben Rechner)
und ein unbekannter Rechner werden sofort abgelehnt, bevor etwas gestartet wird. Geteilt
bleibt, was die Karten betrifft: die Engine-Instanzen und die Plätze je Engine und Adresse
(`max_parallel` gilt über alle Anfragen und Programme), denn die Kapazität gehört dem Dienst.
Start und Stopp arbeiten auf jeder Zeile des Katalogs, ohne Liste.


**Status einer Katalogzeile (2026-10-11).** `running` = sie spricht; `starting` = ein Start läuft;
`stopped` = der Host ist erreichbar (oder es ist dieser Rechner) und der Container läuft nicht, sie
lässt sich also starten; `unreachable` = der Host antwortet nicht (bei einer Cloud-Engine: kein
Zugang). Die Unterscheidung kommt aus der Health-Anfrage der Engine selbst (HTTP an Host:Port), ohne
Ping: eine verweigerte Verbindung heißt, der Host läuft und nichts lauscht; eine
Verbindungs-Zeitüberschreitung, fehlende Route oder nicht auflösbarer Name heißt, der Host ist aus;
jede HTTP-Antwort heißt, er ist da. Jede Zeile fragt für sich, ein toter Host verzögert
`GET /engines` also um eine Health-Zeitgrenze, nicht um eine je Zeile. Ein Start auf einem
unerreichbaren Host scheitert sofort (`stopped`, "host ... unreachable"), ohne auf den SSH-Verbindungsaufbau zu warten.

## Freigabe auf Anfrage

Ein Client, der Platz auf einer Karte braucht (etwa AIfred für ein Sprachmodell), bittet den Dienst
per `POST /release`, Container von den Karten zu nehmen: `{"targets": "all"}` oder eine Liste von
`{"engine", "host"}`. Der Container wird heruntergefahren (VRAM danach frei), nicht nur das Modell
entladen. Der Dienst entscheidet je Ziel: `stopped`, `not_running` (läuft nicht oder hat keinen
Container), `in_use` (eine Lesung hält ihn, die Lesung geht vor, er wird NICHT gestoppt),
`unreachable` (Host antwortet nicht, ohne SSH-Versuch) oder `failed` (Stopp misslungen, mit Meldung).
Die Ziele laufen parallel, ein toter Host verzögert die anderen nicht. Die ganze Anfrage wird vor dem
ersten Stopp geprüft (leer, doppelt, unbekannte Engine oder unbekannter Host: nichts wird gestoppt).
Es ist derselbe Launcher-Stopp wie bei `stop`, kein zweiter Weg. Der Endpunkt ist eine Anfrage wie
`/speak`: auf Loopback ohne Token, sonst mit; die ändernden Verwaltungsbefehle (Start, Stopp,
Konfiguration) bleiben immer tokenpflichtig.

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
Einheit des sprechenden Eintrags (Satz, Absatz, ganz) geschieht im Dienst, weil nur er weiß,
welcher Eintrag der Liste gerade spricht, wenn die Eskalation weiterspringt. Die Einheit
steht im Eintrag der Anfrage, nicht in der Konfiguration; die Engine empfiehlt nur einen
Standard (`unit_default`). Das Audio kommt
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

## Datenordner

Was die Suite selbst erzeugt oder braucht und nicht Teil des Quellbaums ist, liegt in
einem einzigen Datenordner: `~/.local/share/tts-suite`, mit der Umgebungsvariable
`TTS_SUITE_DATA_DIR` änderbar (eine Funktion, `constants.data_dir()`, liest sie bei jedem
Aufruf). Darin: `piper_models/` (die `.onnx`-Modelle von Piper) und
`dashscope_audio3_voices.json` (die Zuordnung der bei DashScope angemeldeten Stimmen).
Beides lag vorher im Quellbaum bzw. im Stimmenordner; der Stimmenordner bleibt dem Benutzer
und enthält nur noch die Referenz-WAVs. Es gibt keinen Rückgriff auf die alten Orte und kein
Kopieren durch den Code: ein bestehender Bestand wird einmal von Hand in den Datenordner
verschoben. Der Ordner wird beim ersten Schreiben der Zuordnung angelegt; die Piper-Modelle
legt der Betreiber hinein. Das Piper-Programm selbst liegt weiter neben dem Python der Suite.
