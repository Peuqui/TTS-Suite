# Die Schnittstelle des TTS-Suite-Dienstes

[English](INTERFACE.md)

**Entwurf 3 zur Durchsicht** durch die Programme, die ihn nutzen werden (AIfred Intelligence, Agent-Orc).
Entwurf 3 folgt den Entscheidungen: ein Systemdienst auf dem Rechner, der Dienst zerlegt den Text, das Audio
kommt immer als Strom von Teilstücken zurück. Die Python-API dahinter ist `src/tts_suite/api.py`; dieses
Dokument ist ihre Form auf dem Draht, eins zu eins. Für die Engine-Container zieht die Konvention von AIfred
Intelligence (`docs/de/architecture/tts-container-conventions.md`) unverändert hierher.

## Zwei Schichten

1. **Engine-Container**, einer je Engine, jeder mit einem eigenen kleinen HTTP-Server. Eine Anfrage trägt den
   Text, den Stimmennamen und die Sprache, nie Audiodaten; die Antwort ist die Audiodatei (`audio/wav`). Die
   Stimmen sind Dateien in einem Ordner, der in jeden Container schreibgeschützt eingebunden ist.
2. **Der Dienst** davor, als Systemdienst mit geringen Rechten auf dem Rechner. Er ist der einzige Besitzer
   von Start und Stopp der Container und der Wahl der Grafikkarte, weil mehrere Programme (AIfred, Agent-Orc,
   weitere) dieselben Karten teilen. Er wählt die Engine aus der Eskalationsliste, **zerlegt den Text nach
   der Einheit der Engine, die spricht**, bearbeitet das Audio nach (Tempo und Tonhöhe mit ffmpeg für Engines
   ohne eigene Unterstützung) und schickt es als Strom zurück. Ein Client spricht nie direkt mit einem
   Container. Weitere Rechner (SSH) sind eine optionale Liste; der Normalfall ist dieser Rechner.

Der Dienst bereinigt den Text fürs Sprechen je Einheit (Markdown, Zahlen, Emoji, Adressen; ein Codeblock wird
durch einen kurzen gesprochenen Hinweis ersetzt, einmal je Block; `pronunciations` der Konfiguration tauschen
Wörter gegen die gesprochene Form). Beim Client bleibt: Dateinamen und Ablage des Audios und alles, was seine
Agenten kennt (welcher Agent welche Stimme hat). Der Dienst bekommt Text und antwortet mit Audio.

## Endpunkte

| Methode und Pfad | Zweck |
|---|---|
| `GET /health` | `200` mit `{"status": "ok"}`, solange der Dienst läuft. |
| `GET /engines` | Die Eskalationsliste in Sprechreihenfolge; Felder unten. |
| `GET /voices?engine=<key>` | Die Stimmen einer Engine: je `name` und `label`. Ohne `engine`: die Ordner des gemeinsamen Stimmenverzeichnisses. |
| `POST /voices/sync?engine=<key>` | Lässt eine Cloud-Engine, die Stimmen klont (DashScope), die Stimmen des gemeinsamen Verzeichnisses hochladen. Antwortet mit den nun bekannten Stimmen. |
| `POST /speak` | Tür 1: der ganze Text; die Antwort ist der Strom von Frames (unten). |
| `WS /speak/stream` | Tür 2: der Text kommt in Stücken (ein Sprachmodell schreibt noch); die Frames laufen zurück, sobald sie existieren. |
| `POST /engines/{engine}/start`, `POST /engines/{engine}/stop` | Startet oder stoppt den Container eines Eintrags (Abfrage `host` für einen anderen Rechner). |
| `POST /engines/reload` | Liest die Engine-Ordner neu ein (eine Engine wird abgeschaltet, indem man ihren Ordner nach `disabled/` verschiebt; sie bleibt als `deactivated` in der Liste). Antwortet `{"removed": [...], "added": [...]}`. |
| `GET /config`, `POST /config` | Die Konfiguration unten. Änderungen überstehen einen Neustart (wie bei STT-Suite). |

### `GET /engines`

Je Eintrag: `engine`, `host` (null: dieser Rechner), `label` und `label_short` (Namen für eine Oberfläche,
Deutsch und Englisch), `enabled`, `deactivated`, `status` (`running`, `stopped`, `starting`, `unreachable`),
`unit` (Wahl des Nutzers) und `unit_default` (`sentence`, `paragraph` oder `whole`: wie viel Text die Engine
auf einmal nimmt), `needs_gpu`, `cloud`, `browser`.

`browser: true` kennzeichnet den Eintrag, der für die eigene Stimme des Clients steht (der Browser spricht,
der Dienst kann es nicht). Er steht in der Liste, weil die Reihenfolge Sache der Liste ist; der Dienst
überspringt ihn immer, und der Client entscheidet, ob und wie er spricht.

### `GET /config`, `POST /config`

Ein Objekt: `voices_dir`; `entries` (`engine`, `host`, `enabled`, in Sprechreihenfolge); `hosts` (optional:
`name`, `address`, `ssh_port`, `ssh_user`); `engines` (je Engine-Schlüssel: `unit`, `voice` mit `voice`,
`speed`, `pitch`, `vram_budget_mib`, `max_parallel`, `max_queue`); `gpus` und `headroom_mib` (siehe unten);
`pronunciations` (ein Wort zur Form, die stattdessen gesprochen wird). `POST` prüft einmal, im Engine-Paket;
was nicht funktionieren kann, ist `400` mit dem Grund. Cloud-Schlüssel gehören nicht dazu (unten).

**Die Platzierung auf der GPU ist ein Regelsatz**, kein Rückruf an ein Programm: Eine Engine startet auf der
ersten Karte aus `gpus` (`gpu` = Index wie `nvidia-smi` ihn nennt, oder UUID; `reserve_mib` = VRAM, der für
andere Programme frei bleibt), deren freier VRAM abzüglich der Reserve das `vram_budget_mib` der Engine plus `headroom_mib` deckt (ein
Aufschlag, damit keine Karte bis auf das letzte MiB gefüllt wird). Der Container bekommt das Budget als Kappe;
die Suite beobachtet den Verbrauch nicht und stoppt keine Engine deswegen (`GET /engines` meldet, was eine
Engine belegt, als `vram_mib`, nur zur Anzeige). Leeres `gpus` heißt alle Karten, die mit dem meisten freien
VRAM zuerst. Passt keine Karte, wird der Eintrag mit `no_vram` übersprungen (nicht gemerkt: die nächste
Anfrage wählt neu).

### Die Anfrage: der Kopf eines Vorlesens

```json
{"language": "de", "voice": "Alice", "speed": 1.0, "pitch": 1.0, "engine": null,
 "voices": {"edge": {"voice": "de-DE-KatjaNeural", "speed": 1.1}}, "run": "r-42", "wait": true}
```

- `language` ist Pflicht; `voice`, `speed` und `pitch` haben die eingestellten Standards. `POST /speak`
  trägt Kopf und Text als `{"text": "...", ...Kopf}`; der WebSocket beginnt mit dem Kopf allein.
- **Eine Stimme gehört zu einer Engine.** `voice` ist die Standardstimme (ein Ordner des gemeinsamen
  Verzeichnisses, gültig für die Container-Engines). Andere Engines haben eigene Namen (Edge, Piper,
  Cloud-Stimmen); darum kann `voices` Stimme, Tempo und Tonhöhe je Engine-Schlüssel überschreiben. So liefert
  der Client die Stimmen seiner Agenten mit; der Dienst muss die Agenten nie kennen, und der Client muss nie
  wissen, welche Engine sprechen wird.
- `engine` (optional) nennt einen Eintrag und überspringt die Eskalation: Diese Engine spricht oder das
  Vorlesen scheitert.
- Ohne sie spricht der erste Eintrag der Liste, der sprechen kann. Scheitert er, übernimmt der nächste.
- **`run`** (optional, eine ID, die der Client für ein Vorlesen ausdenkt): der Dienst merkt sich je Lauf etwa
  zehn Minuten, welche Einträge ausfielen und ob schon gesprochen wurde. Ausgefallene Einträge bleiben für
  den Rest des Laufs draußen. Fällt ein Eintrag nach dem ersten Ton aus, spricht der Nachfolger zuerst eine
  Ansage (zum Beispiel „Stimmwechsel zu X wegen Y“, deutsch oder englisch nach `language`; die Texte liegen
  in der Suite), **ein eigenes Segment vor dem ersten des Nachfolgers**, und dann das unfertige Stück. Auswahl
  und Zustand eines Laufs liegen hinter einer Sperre, sodass nie zwei Stücke beide den Nachfolger starten und
  die Ansage einmal erfolgt. Startet der Dienst mitten im Lauf neu, ist der Zustand verloren, und die Eskalation
  beginnt wieder oben in der Liste (der Client sieht es an `engine` der Frames).
- **`wait`** (Standard `true`): das Vorlesen darf auf den Start eines Containers warten (Kaltstart: üblich
  20 s und mehr). Mit `false` endet es sofort mit `503` `{"error": "starting"}`, und der Start läuft im
  Hintergrund weiter.

### Die Antwort: Frames

Audio kommt immer als Strom von **Frames in Sprechreihenfolge, jeder sobald er existiert**, auch bei einem
ganzen Text, damit das erste Stück so früh wie möglich beginnt. Beide Türen nutzen dieselben Frames:

- Ein Frame ist `uint32` (Big Endian) Länge des Kopfes, der Kopf als UTF-8-JSON, dann der Körper. Der Kopf
  nennt `kind` und die `bytes` des Körpers. Auf dem WebSocket ist ein Frame eine Binärnachricht; im
  HTTP-Körper (`application/x-tts-frames`, gestückelt) folgen sie aufeinander.
- `kind: "audio"`: Körper = Audio (`media_type`, z. B. `audio/wav`); der Kopf nennt auch `engine` (wer es
  gesprochen hat, als `engine@host`) und `keepalive` (`true`: eine Markierung mit leerem Körper (`bytes: 0`), die den Strom nur offen hält, solange
  ein Stück lange braucht, für jedes Format; verwerfen).
- `kind: "summary"`: der letzte Frame, Körper = JSON `{"trace": [...], "switch": null | "<Grund>"}`. `trace`
  ist der ganze Verlauf der Eskalation, eine Liste von `{entry, outcome, reason, detail}` mit `outcome` aus
  `skipped`, `failed`, `spoke`; der Client baut daraus seine Hinweise (etwa „XTTS · Aragon → DashScope
  (Serverausfall)“). `switch` ist gesetzt, wenn eine Ansage des Stimmwechsels vorangestellt wurde.
- `kind: "error"`: beendet den Strom, wenn nach dem ersten Frame etwas scheitert; Körper = JSON wie in einer
  Fehlerantwort unten.

Vor dem ersten Frame hat der Dienst sich auf keinen Status festgelegt: Ein Vorlesen, das nicht beginnen kann,
endet mit einem passenden HTTP-Status (der WebSocket schickt einen `error`-Frame und schließt). Fehler sind
JSON `{"error": "<code>", "detail": "<text>", "trace": [...]}`: `no_engine` (`503`: kein Eintrag kann
sprechen), `starting` (`503`), `overloaded` (`429`: mehr Anfragen für eine Engine, als ihre Warteschlange
nimmt), `unknown_engine` (`404`), `engine_in_use` (`409`: ein Stopp für einen Container, den gerade ein Vorlesen
nutzt), `engine_control_failed` (`502`: Docker oder der Rechner hat Start oder Stopp nicht geschafft),
`bad_request` und `config_invalid` (`400`), `unsupported_language` (`422`: kein Text der Suite in dieser
Sprache), `unauthorized` (`401`). `starting` trägt `Retry-After`. Am WebSocket ist ein abgelehnter
Handshake (kein Token) eine geschlossene Verbindung.

Die Gründe sind überall dieselben Codes. Ausfälle (nach dem Anlauf): `engine`, `software`, `unreachable`,
`timeout`, `cloud_quota`. Überspringen (vor dem Anlauf): `disabled`, `deactivated`, `stopped`, `no_vram`.

### Der WebSocket `/speak/stream`

Der Client schickt Textnachrichten (JSON): zuerst `{"type": "start", ...Kopf}`, dann beliebig viele
`{"type": "text", "text": "..."}`, zuletzt `{"type": "finish"}`. Der Dienst zerlegt den Text nach der Einheit
der Engine, die spricht, und beginnt mit der ersten vollständigen Einheit; der Rest wird nach `finish`
gesprochen. Die Frames kommen als Binärnachrichten zurück; nach dem `summary`-Frame schließt der Dienst.

## Was der Dienst selbst tut

- **Parallele Anfragen.** Je Engine und Rechner begrenzt der Dienst die gleichzeitig laufenden Anfragen
  (`max_parallel`) mit einem Semaphor und die wartenden auf `max_queue`; darüber `overloaded`.
- **Protokoll.** Der Dienst schreibt mit Pythons `logging`, eine Zeile je übersprungenem oder gescheitertem
  Eintrag, dasselbe wie im `trace`.

## Regeln gemeinsam mit STT-Suite

- **Zugang:** Solange der Dienst nur auf `127.0.0.1` lauscht, verlangen Sprechen und die `GET`-Endpunkte
  nichts. Befehle, die etwas ändern (`POST /config`, `/engines/*/start|stop|reload`, `/voices/sync`: Rechner
  und SSH-Ziele, GPU-Container, Cloud-Schlüssel), brauchen immer das Token, auch lokal, da jedes Programm auf
  dem Rechner localhost erreicht. Lauscht der Dienst auf einer anderen Adresse, verlangt alles ein Token
  (`Authorization: Bearer <token>`; der WebSocket nimmt es ebenfalls in dieser Kopfzeile), und er startet ohne
  eines nicht.
- **Gesundheit:** `GET /health` bei jedem Dienst.
- **Ein Client funktioniert ohne den Dienst.** Er hat seinen eigenen Ersatz (Agent-Orc: die Browser-Stimme)
  und nennt den Dienst durch eine optionale Adresse in seiner Konfiguration.
- Schlüssel von Cloud-Engines liegen in der Umgebung oder in Dateien, die nur der Nutzer lesen kann, nie in
  der Konfiguration oder im Repository.
- Jedes Repository hat eine README auf Deutsch und auf Englisch.

## Herauslösen aus AIfred Intelligence

Zieht mit in die Suite: die Engine-Ordner und ihre Container, Eskalation, Engine-Verwaltung und Rechner
(samt `scripts/tts-host-ctl.sh` und dem Compose-Aufruf, der `TTS_GPU_UUID` setzt), die Bereinigung und Zerlegung des Textes
nach Einheit, die Nachbearbeitung, die Ansagetexte und Engine-Namen (deutsch und englisch), die
Konfiguration und die Tests von Eskalation, Reload, Vollständigkeit und der DashScope-Engine. Die Engines sind die, die es in der Suite gibt (kein
MOSS, kein Fish-Speech). Container- und Projektnamen (und das Volume `xtts_voices`) werden in den Compose-Dateien gepinnt (`name:`), damit Docker
laufende Container nicht neu anlegt. Der Stimmenordner enthält Nutzerdaten: Er liegt nicht im öffentlichen
Repository, sein Pfad ist eine Einstellung, und Suite wie AIfred (Agent-Bündel) nutzen diesen einen Ort. Die
Liste und die Rechner, heute in AIfreds `settings.json`, ziehen nach `/config` als den einen Ort; AIfred
liest und schreibt sie über HTTP mit einem dünnen Client, mit einmaliger Migration und ohne Parallelbetrieb.
Bei AIfred bleibt: `resolve_voice` und die Stimmen der Agenten, Dateinamen und Ablage des Audios und ein
Client-Test.

## Offene Punkte

- Das Audioformat ist WAV; `ogg` könnte folgen, wo eine Engine es anbietet.
