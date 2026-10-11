# Die Schnittstelle des TTS-Suite-Dienstes

[English](INTERFACE.md)

**Entwurf zur Prüfung** durch die Programme, die ihn nutzen werden (AIfred Intelligence, Agent-Orc). Wo es
um Engine-Container geht, soll die bestehende Konvention von AIfred Intelligence
(`docs/de/architecture/tts-container-conventions.md`) unverändert hierher wandern.

## Zwei Schichten

1. **Engine-Container**, einer je Engine, jeder mit einem eigenen kleinen HTTP-Server. Eine Anfrage trägt
   den Text, den Stimmennamen und die Sprache, nie Audiodaten; die Antwort ist die Audiodatei
   (`audio/wav`). Die Stimmen sind Dateien in einem Ordner, der in jeden Container nur lesbar eingehängt ist.
2. **Der Dienst** davor: die eine Schnittstelle, mit der ein Programm spricht. Er wählt die Engine aus der
   Eskalationsliste, startet und stoppt Container (auf diesem Rechner oder per SSH auf einem anderen) und
   liefert das Audio.

## Endpunkte des Dienstes

| Methode und Pfad | Zweck |
|---|---|
| `GET /health` | `200` mit `{"status": "ok"}`, solange der Dienst läuft. |
| `GET /engines` | Die Eskalationsliste in Sprechreihenfolge: je Eintrag `engine`, `host` (null: dieser Rechner), `label`, `enabled`, `status`, `unit` (`sentence`, `paragraph` oder `whole`: wie viel Text die Engine auf einmal nimmt), `cloud`. |
| `GET /voices` | Die Namen der Stimmen (die Ordner des gemeinsamen Stimmenverzeichnisses). |
| `POST /speak` | Lässt einen Text sprechen. JSON-Körper unten; antwortet mit `200` und `audio/wav`. |
| `POST /engines/{engine}/start`, `POST /engines/{engine}/stop` | Startet oder stoppt den Container eines Eintrags (Abfrageparameter `host` für einen anderen Rechner). |
| `GET /config`, `POST /config` | Die Eskalationsliste und die Rechner; Änderungen überleben einen Neustart (wie bei STT-Suite). |

### `POST /speak`

```json
{"text": "Hallo Welt.", "language": "de", "voice": "Alice", "engine": null, "speed": 1.0}
```

- `text` und `language` sind Pflicht; `voice` ist sonst die eingestellte Stimme.
- `engine` (optional) nennt einen Eintrag und überspringt die Eskalation: Diese Engine spricht oder die
  Anfrage scheitert.
- Ohne sie spricht der erste Eintrag der Liste, der sprechen kann. Fällt er aus, übernimmt der nächste.
- Die Antwort trägt das Audio und zwei Kopfzeilen: `X-TTS-Engine` (wer gesprochen hat, als `engine@host`)
  und `X-TTS-Failed` (die Einträge, die vorher ausfielen, mit Grund; fehlt, wenn keiner ausfiel).
- Kein Eintrag kann sprechen: `503` mit `{"error": "no_engine", "tried": [...]}`. Falsche Eingabe: `400`.

Ein Client, der Satz für Satz braucht, schneidet den Text selbst nach der `unit`, die `GET /engines` meldet.

## Regeln, die mit STT-Suite gelten

- Fehler sind JSON: `{"error": "<code>", "detail": "<Text>"}`.
- **Zugang:** Solange der Dienst nur auf `127.0.0.1` lauscht, verlangt er nichts. Lauscht er auf einer anderen
  Adresse, verlangt er einen Token (`Authorization: Bearer <Token>`) und startet ohne ihn nicht. Der Dienst
  hält die Schlüssel der Cloud-Engines und die Grafikkarte: Ein offener Dienst im Netz lässt jeden auf deine
  Rechnung sprechen.
- **Gesundheit:** `GET /health` bei jedem Dienst.
- **Ein Client funktioniert ohne den Dienst.** Er hat seinen eigenen Ersatz (Agent-Orc: die Stimme des
  Browsers) und kennt den Dienst nur über eine optionale Adresse in seiner Konfiguration.
- Schlüssel von Cloud-Engines stehen in der Umgebung oder in Dateien, die nur der Benutzer lesen darf, nie im
  Repository.
- Jedes Repository hat eine README auf Deutsch und auf Englisch.

## Offene Punkte

- Streaming (Teile, während die Engine noch spricht) gehört nicht zu diesem Entwurf; `POST /speak` liefert
  das ganze Audio.
- Das Format ist WAV; `ogg` könnte folgen, wo eine Engine es anbietet.
- Der Stimmen-Upload für Cloud-Engines, die Stimmen klonen (DashScope), braucht einen eigenen Endpunkt.
