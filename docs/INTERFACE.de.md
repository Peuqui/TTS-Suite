# Die Schnittstelle des TTS-Suite-Dienstes

[English](INTERFACE.md)

**Entwurf 2 zur Durchsicht** durch die Programme, die ihn nutzen werden (AIfred Intelligence, Agent-Orc).
Entwurf 2 nimmt das Review von AIfred Intelligence zu Entwurf 1 auf. Für die Engine-Container soll die
bestehende Konvention von AIfred Intelligence (`docs/de/architecture/tts-container-conventions.md`)
unverändert hierher ziehen.

## Zwei Schichten

1. **Engine-Container**, einer je Engine, jeder mit einem eigenen kleinen HTTP-Server. Eine Anfrage trägt den
   Text, den Stimmennamen und die Sprache, nie Audiodaten; die Antwort ist die Audiodatei (`audio/wav`). Die
   Stimmen sind Dateien in einem Ordner, der in jeden Container schreibgeschützt eingebunden ist.
2. **Der Dienst** davor: die eine Schnittstelle, mit der ein Programm spricht. Er wählt die Engine aus der
   Eskalationsliste, startet und stoppt Container (auf diesem Rechner oder über SSH auf einem anderen),
   bearbeitet das Audio nach (Tempo und Tonhöhe mit ffmpeg für Engines ohne eigene Unterstützung) und gibt es
   zurück. Ein Client spricht nie direkt mit einem Container, auch nicht mit einem auf einem anderen Rechner.

Beim Client bleibt: den Text in Sätze schneiden, ihn fürs Sprechen bereinigen (Markdown, Zahlen, Emoji),
Dateinamen und Ablage des Audios und alles, was seine Agenten kennt (welcher Agent welche Stimme hat). Der
Dienst bekommt fertigen Text und antwortet mit Bytes.

## Endpunkte des Dienstes

| Methode und Pfad | Zweck |
|---|---|
| `GET /health` | `200` mit `{"status": "ok"}`, solange der Dienst läuft. |
| `GET /engines` | Die Eskalationsliste in Sprechreihenfolge; Felder unten. |
| `GET /voices?engine=<key>` | Die Stimmen einer Engine: je `name` und `label`. Ohne `engine`: die Ordner des gemeinsamen Stimmenverzeichnisses. |
| `POST /voices/sync?engine=<key>` | Lässt eine Cloud-Engine, die Stimmen klont (DashScope), die Stimmen des gemeinsamen Verzeichnisses hochladen (Enrollment). Antwortet mit den nun bekannten Stimmen. |
| `POST /speak` | Lässt einen Text sprechen. JSON-Körper unten; antwortet `200` mit `audio/wav`. |
| `POST /engines/{engine}/start`, `POST /engines/{engine}/stop` | Startet oder stoppt den Container eines Eintrags (Abfrage `host` für einen anderen Rechner). |
| `POST /engines/reload` | Liest die Engine-Ordner neu ein (eine Engine wird abgeschaltet, indem man ihren Ordner nach `disabled/` verschiebt; sie bleibt als `deactivated` in der Liste). |
| `GET /config`, `POST /config` | Die Eskalationsliste (`engine`, `host`, `enabled`), die Rechner (`name`, `address`, `ssh_port`, …) und Standards je Engine (`unit`, Stimme, Tempo, Tonhöhe). Änderungen überstehen einen Neustart (wie bei STT-Suite). |

### `GET /engines`

Je Eintrag: `engine`, `host` (null: dieser Rechner), `label` und `label_short` (Namen für eine Oberfläche, auf
Deutsch und Englisch), `enabled`, `deactivated`, `status` (`running`, `stopped`, `starting`, `unreachable`),
`unit` (Wahl des Nutzers) und `unit_default` (`sentence`, `paragraph` oder `whole`: wie viel Text die Engine
auf einmal nimmt), `needs_gpu`, `cloud`, `browser`.

`browser: true` kennzeichnet den Eintrag, der für die eigene Stimme des Clients steht (der Browser spricht,
der Dienst kann es nicht). Er steht in der Liste, weil die Reihenfolge Sache der Liste ist; der Dienst
überspringt ihn immer, und der Client entscheidet, ob und wie er spricht (Text, Sprache, Stimme, Tempo und
Tonhöhe hat er selbst).

### `POST /speak`

```json
{"text": "Hallo Welt.", "language": "de", "voice": "Alice", "speed": 1.0, "pitch": 1.0,
 "engine": null, "voices": {"edge": {"voice": "de-DE-KatjaNeural", "speed": 1.1}}, "run": "r-42",
 "wait": true}
```

- `text` und `language` sind Pflicht; `voice`, `speed` und `pitch` haben die eingestellten Standards.
- **Eine Stimme gehört zu einer Engine.** `voice` ist die Standardstimme (ein Ordner des gemeinsamen
  Verzeichnisses, gültig für die Container-Engines). Andere Engines haben eigene Namen (Edge, Piper,
  Cloud-Stimmen); darum kann `voices` Stimme, Tempo und Tonhöhe je Engine-Schlüssel überschreiben. So liefert
  der Client die Stimmen seiner Agenten mit; der Dienst muss die Agenten nie kennen, und der Client muss nie
  wissen, welche Engine sprechen wird.
- `engine` (optional) nennt einen Eintrag und überspringt die Eskalation: Diese Engine spricht oder die
  Anfrage scheitert.
- Ohne sie spricht der erste Eintrag der Liste, der sprechen kann. Scheitert er, übernimmt der nächste.
- **`run`** (optional, eine ID, die der Client für ein Vorlesen ausdenkt, etwa eine Antwort): der Dienst merkt
  sich je Lauf etwa zehn Minuten, welche Einträge ausfielen und ob schon gesprochen wurde. Ausgefallene
  Einträge bleiben für den Rest des Laufs draußen. Fällt ein Eintrag nach dem ersten Ton aus, spricht der
  Nachfolger zuerst eine Ansage (zum Beispiel „Stimmwechsel zu X wegen Y“, deutsch oder englisch nach
  `language`; die Texte liegen in der Suite) und dann den unfertigen Satz. Ohne `run` hält der Dienst keinen
  Zustand.
- **`wait`** (Standard `true`): die Anfrage darf auf den Start eines Containers warten (Kaltstart: üblich 20 s
  und mehr; der Client stellt seine Zeitgrenze entsprechend ein). Mit `false` kommt sofort `503`
  `{"error": "starting"}`, und der Start läuft im Hintergrund weiter.
- Die Antwort trägt das Audio und diese Kopfzeilen:
  - `X-TTS-Engine`: wer gesprochen hat, als `engine@host`.
  - `X-TTS-Trace`: JSON-Liste `{entry, outcome, reason}` mit `outcome` aus `skipped`, `failed`, `spoke` — der
    ganze Verlauf der Eskalation (ersetzt eine Kopfzeile nur der Ausfälle; der Client baut daraus seine
    Hinweise, etwa „XTTS · Aragon → DashScope (Serverausfall)“).
  - `X-TTS-Switch`: nur, wenn eine Ansage des Stimmwechsels vorangestellt wurde; trägt ihren Grund.
- Die Gründe sind überall dieselben Codes: `disabled`, `deactivated`, `stopped`, `no_vram`, `timeout`,
  `software`, `server`, `cloud_quota`.
- Kein Eintrag kann sprechen: `503` mit `{"error": "no_engine", "detail": ..., "trace": [...]}`. Falsche
  Eingabe: `400`. Zu viele Anfragen für eine Engine: `429` (siehe unten).

Ein Client, der Satz für Satz braucht, schneidet den Text selbst nach der `unit`, die `GET /engines` meldet.

## Was der Dienst selbst tut

- **Parallele Anfragen.** Je Engine und Rechner begrenzt der Dienst die gleichzeitig laufenden Anfragen
  (`max_parallel` in der Konfiguration der Engine) mit einem Semaphor; Clients dürfen die Sätze parallel
  schicken. Über einer Warteschlangengrenze antwortet er mit `429`.
- **Platzierung von GPU-Engines.** Auf welcher Grafikkarte ein Container startet, entscheidet eine
  *Platzierungs-Richtlinie*, eine austauschbare Funktion. Der Standard fragt `nvidia-smi` nach dem freien VRAM
  und nimmt die Karte mit dem meisten. Ein Programm, das mehr weiß (AIfred Intelligence weiß, welche Modelle
  llama-swap und vLLM halten), liefert eine eigene Richtlinie; sie wird in der Konfiguration benannt und als
  Python-Modul geladen. Ohne eine Richtlinie, die zum Rechner passt, startete die Suite Container blind neben
  anderen Lasten.
- **Protokoll.** Der Dienst schreibt mit Pythons `logging`, eine Zeile je übersprungenem oder gescheitertem
  Eintrag, dasselbe wie in `X-TTS-Trace`.

## Regeln gemeinsam mit STT-Suite

- Fehler sind JSON: `{"error": "<code>", "detail": "<text>"}`.
- **Zugang:** Solange der Dienst nur auf `127.0.0.1` lauscht, verlangt er nichts. Lauscht er auf einer anderen
  Adresse, verlangt er ein Token (`Authorization: Bearer <token>`) und startet ohne eines nicht. Der Dienst
  hält die Schlüssel der Cloud-Engines und die GPU: Ein offener Dienst im Netz lässt jeden auf Ihre Rechnung
  sprechen.
- **Gesundheit:** `GET /health` bei jedem Dienst.
- **Ein Client funktioniert ohne den Dienst.** Er hat seinen eigenen Ersatz (Agent-Orc: die Browser-Stimme)
  und nennt den Dienst durch eine optionale Adresse in seiner Konfiguration.
- Schlüssel von Cloud-Engines liegen in der Umgebung oder in Dateien, die nur der Nutzer lesen kann, nie im
  Repository.
- Jedes Repository hat eine README auf Deutsch und auf Englisch.

## Herauslösen aus AIfred Intelligence

Zieht mit in die Suite: die Engine-Ordner und ihre Container, Eskalation, Engine-Verwaltung und Rechner
(samt `scripts/tts-host-ctl.sh` und dem Compose-Aufruf, der `TTS_GPU_UUID` setzt), die Nachbearbeitung, die
Ansagetexte und Engine-Namen (deutsch und englisch), die Konfiguration (ersetzt die Teile von
`aifred.lib.config`, die sie nutzte) und die Tests von Eskalation, Reload, Vollständigkeit und der
DashScope-Engine. Bei AIfred bleibt: `resolve_voice` und die Stimmen der Agenten, Satzschnitt und
Textbereinigung, Dateinamen und Ablage des Audios, seine Platzierungs-Richtlinie (der Suite geliefert) und ein
Client-Test.

## Offene Punkte

- Streaming (Teile, während die Engine noch spricht) gehört nicht zur ersten Fassung; `POST /speak` liefert
  das ganze Audio je Satz. Teile für `whole`-Engines können folgen.
- Das Format ist WAV; `ogg` könnte folgen, wo eine Engine es anbietet.
