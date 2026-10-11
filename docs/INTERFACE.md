# The interface of the TTS-Suite service

[Deutsch](INTERFACE.de.md)

**Draft for review** by the programs that will use it (AIfred Intelligence, Agent-Orc). Where an
engine container is concerned, the existing convention of AIfred Intelligence
(`docs/en/architecture/tts-container-conventions.md`) is meant to move here unchanged.

## Two layers

1. **Engine containers**, one per engine, each with its own small HTTP server. A request carries the
   text, the voice name and the language, never audio bytes; the answer is the audio file
   (`audio/wav`). The voices are files in one folder mounted read-only into every container.
2. **The service** in front of them: the one interface a program talks to. It picks the engine from the
   escalation list, starts and stops containers (on this machine, or on another one over SSH), and
   hands back the audio.

## Endpoints of the service

| Method and path | Purpose |
|---|---|
| `GET /health` | `200` with `{"status": "ok"}` while the service runs. |
| `GET /engines` | The escalation list in speaking order: for each entry `engine`, `host` (null: this machine), `label`, `enabled`, `status`, `unit` (`sentence`, `paragraph` or `whole`: how much text the engine takes at once), `cloud`. |
| `GET /voices` | The names of the voices (the folders of the shared voices directory). |
| `POST /speak` | Has a text spoken. JSON body below; answers `200` with `audio/wav`. |
| `POST /engines/{engine}/start`, `POST /engines/{engine}/stop` | Starts or stops an entry's container (query `host` for another machine). |
| `GET /config`, `POST /config` | The escalation list and the hosts; changes survive a restart (as in STT-Suite). |

### `POST /speak`

```json
{"text": "Hello world.", "language": "en", "voice": "Alice", "engine": null, "speed": 1.0}
```

- `text` and `language` are required; `voice` defaults to the configured one.
- `engine` (optional) names one entry and skips the escalation: that engine speaks or the request fails.
- Without it the first entry of the list that can speak does. If it fails, the next one takes over.
- The answer carries the audio and two headers: `X-TTS-Engine` (who spoke, as `engine@host`) and
  `X-TTS-Failed` (the entries that failed before, with the reason; absent if none did).
- No entry can speak: `503` with `{"error": "no_engine", "tried": [...]}`. Bad input: `400`.

A client that needs a sentence at a time cuts the text itself by the `unit` that `GET /engines` reports.

## Rules shared with STT-Suite

- Errors are JSON: `{"error": "<code>", "detail": "<text>"}`.
- **Access:** while the service listens only on `127.0.0.1` it asks for nothing. Listening on any other
  address, it requires a token (`Authorization: Bearer <token>`) and refuses to start without one. The
  service holds the keys of the cloud engines and the GPU: an open service on a network lets anyone speak
  on your account.
- **Health:** `GET /health` on every service.
- **A client works without the service.** It has its own fallback (Agent-Orc: the browser's voice) and
  names the service by one optional address in its configuration.
- Keys of cloud engines live in the environment or in files kept readable for the user alone, never in
  the repository.
- Every repository has a README in German and in English.

## Open points

- Streaming (chunks while the engine still speaks) is not part of this draft; `POST /speak` returns the
  whole audio.
- The format is WAV; `ogg` could follow where an engine offers it.
- The voice upload for cloud engines that clone voices (DashScope) needs its own endpoint.
