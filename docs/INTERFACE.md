# The interface of the TTS-Suite service

[Deutsch](INTERFACE.de.md)

**Draft 3 for review** by the programs that will use it (AIfred Intelligence, Agent-Orc). Draft 3 follows
the decisions: one system service on the machine, the service cuts the text, audio always comes back as a
stream of pieces. The Python API behind it is `src/tts_suite/api.py`; this document is its wire form, one to
one. Where an engine container is concerned, the convention of AIfred Intelligence
(`docs/en/architecture/tts-container-conventions.md`) moves here unchanged.

## Two layers

1. **Engine containers**, one per engine, each with its own small HTTP server. A request carries the
   text, the voice name and the language, never audio bytes; the answer is the audio file
   (`audio/wav`). The voices are files in one folder mounted read-only into every container.
2. **The service** in front of them, running as a system service with low rights on the machine. It is the
   only owner of starting and stopping containers and of the choice of the GPU, because several programs
   (AIfred, Agent-Orc, others) share the same cards. It picks the engine from the escalation list, **cuts the
   text by the unit of the engine that speaks**, post-processes the audio (speed and pitch with ffmpeg for
   engines without native support) and streams it back. A client never talks to a container directly. Other
   machines (SSH) are an optional list; the normal case is this machine.

The service cleans the text for speech by unit (Markdown, numbers, emoji, URLs; a block of code is replaced by
a short spoken hint, once per block; `pronunciations` of the configuration swap words for the form that is
spoken). What stays with the client: file names and storage of the audio, and everything that knows its
agents (which agent has which voice). The service gets text and answers with audio.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /health` | `200` with `{"status": "ok"}` while the service runs. |
| `GET /engines` | The escalation list in speaking order; fields below. |
| `GET /voices?engine=<key>` | The voices of one engine: for each `name` and `label`. Without `engine`: the folders of the shared voices directory. |
| `POST /voices/sync?engine=<key>` | Has a cloud engine that clones voices (DashScope) upload the voices of the shared directory. Answers with the voices now known. |
| `POST /speak` | Door 1: the whole text; the answer is the stream of frames (below). |
| `WS /speak/stream` | Door 2: the text arrives in pieces (a language model is still writing); the frames run back as they exist. |
| `POST /engines/{engine}/start`, `POST /engines/{engine}/stop` | Starts or stops an entry's container (query `host` for another machine). |
| `POST /engines/reload` | Reads the engine folders again (an engine is switched off by moving its folder to `disabled/`; it stays in the list as `deactivated`). Answers `{"removed": [...], "added": [...]}`. |
| `GET /config`, `POST /config` | The configuration below. Changes survive a restart (as in STT-Suite). |

### `GET /engines`

For each entry: `engine`, `host` (null: this machine), `label` and `label_short` (names for a user
interface, German and English), `enabled`, `deactivated`, `status` (`running`, `stopped`, `starting`,
`unreachable`), `unit` (the user's choice) and `unit_default` (`sentence`, `paragraph` or `whole`: how much
text the engine takes at once), `needs_gpu`, `cloud`, `browser`.

`browser: true` marks the entry that stands for the client's own voice (the browser speaks, the service
cannot). It is in the list because the order is the list's business; the service always skips it, and the
client decides whether and how it speaks.

### `GET /config`, `POST /config`

One object: `voices_dir`; `entries` (`engine`, `host`, `enabled`, in speaking order); `hosts` (optional:
`name`, `address`, `ssh_port`, `ssh_user`); `engines` (per engine key: `unit`, `voice` with `voice`, `speed`,
`pitch`, `vram_budget_mib`, `max_parallel`, `max_queue`); `gpus` and `headroom_mib` (see below);
`pronunciations` (a word to the form spoken instead). `POST` validates once, in the engine package;
what cannot work is `400` with the reason. Cloud keys are not part of it (below).

**GPU placement is a rule set**, no callback to a program: an engine starts on the first card of `gpus`
(`gpu` = index as `nvidia-smi` lists it, or UUID; `reserve_mib` = VRAM that stays free for other programs)
whose free VRAM minus the reserve covers the engine's `vram_budget_mib` plus `headroom_mib` (a margin so a
card is not filled to the last MiB). The container gets the budget as its cap; the suite does not watch the
use or stop an engine for it (`GET /engines` reports what an engine holds as `vram_mib`, for display). An
empty `gpus` means every card, the one with the most free VRAM first. No card fits: the entry is skipped
with `no_vram` (not remembered: the next request chooses again).

### The request: the head of a reading

```json
{"language": "en", "voice": "Alice", "speed": 1.0, "pitch": 1.0, "engine": null,
 "voices": {"edge": {"voice": "en-GB-SoniaNeural", "speed": 1.1}}, "run": "r-42", "wait": true}
```

- `language` is required; `voice`, `speed` and `pitch` default to the configured ones. `POST /speak`
  carries the head and the text as `{"text": "...", ...head}`; the WebSocket opens with the head alone.
- **A voice belongs to an engine.** `voice` is the default voice (a folder of the shared directory, valid for
  the container engines). Other engines have names of their own (Edge, Piper, cloud voices), so `voices` may
  override voice, speed and pitch per engine key. The client delivers its agents' voices this way; the
  service never has to know the agents, and the client never has to know which engine will speak.
- `engine` (optional) names one entry and skips the escalation: that engine speaks or the reading fails.
- Without it the first entry of the list that can speak does. If it fails, the next one takes over.
- **`run`** (optional, an ID the client makes up for one reading): the service keeps per run which entries
  failed and whether something was spoken already, for about ten minutes. Entries that failed stay out for
  the rest of the run. If an entry fails after the first sound, the successor first speaks an announcement
  (for example "Voice change to X because of Y", German or English by `language`; the texts live in the
  suite), **a segment of its own in front of the successor's first one**, and then the unfinished piece. The
  choice and the state of a run sit behind a lock, so two pieces never both start the successor and the
  announcement is made once.
  If the service restarts in the middle of a run, the state is lost and the escalation starts at the top of the
  list again (the client sees it in `engine` of the frames).
- **`wait`** (default `true`): the reading may wait for a container to start (cold start: typically 20 s and
  more). With `false` it ends at once with `503` `{"error": "starting"}`, and the start goes on in the
  background.

### The answer: frames

Audio always comes back as a stream of **frames in speaking order, each as soon as it exists**, also for a
whole text, so the first piece starts as early as possible. Both doors use the same frames:

- A frame is `uint32` (big endian) header length, the header as UTF-8 JSON, then the body. The header names
  `kind` and the body's `bytes`. On the WebSocket one frame is one binary message; in the HTTP body
  (`application/x-tts-frames`, chunked) they follow each other.
- `kind: "audio"`: body = audio (`media_type`, e.g. `audio/wav`); header also `engine` (who spoke it, as
  `engine@host`) and `keepalive` (`true`: a marker with an empty body (`bytes: 0`) that only holds the stream open while a piece
  takes long, for every format; drop it).
- `kind: "summary"`: the last frame, body = JSON `{"trace": [...], "switch": null | "<reason>"}`. `trace` is
  the whole course of the escalation, a list of `{entry, outcome, reason, detail}` with `outcome` one of
  `skipped`, `failed`, `spoke`; the client builds its notes from it (for example "XTTS · Aragon → DashScope
  (outage)"). `switch` is set if an announcement of a voice change was put in front.
- `kind: "error"`: ends the stream when something fails after the first frame; body = JSON as in an error
  answer below.

Before the first frame the service has not committed to a status: a reading that cannot start ends with a
proper HTTP status (the WebSocket sends an `error` frame and closes). Errors are JSON
`{"error": "<code>", "detail": "<text>", "trace": [...]}`: `no_engine` (`503`: no entry can speak),
`starting` (`503`), `overloaded` (`429`: more requests for an engine than its queue takes), `unknown_engine`
(`404`), `engine_in_use` (`409`: a stop for a container a reading is using), `engine_control_failed` (`502`:
Docker or the host did not do a start or stop), `bad_request` and `config_invalid` (`400`),
`unsupported_language` (`422`: no text of the suite in that language), `unauthorized` (`401`). `starting`
carries `Retry-After`. On the WebSocket a refused handshake (no token) is a closed connection.

Reasons are the same codes everywhere. Failures (after the attempt): `engine`, `software`, `unreachable`,
`timeout`, `cloud_quota`. Skips (before the attempt): `disabled`, `deactivated`, `stopped`, `no_vram`.

### The WebSocket `/speak/stream`

The client sends text messages (JSON): first `{"type": "start", ...head}`, then any number of
`{"type": "text", "text": "..."}`, finally `{"type": "finish"}`. The service cuts the text by the unit of the
engine that speaks and starts on the first complete unit; the rest is spoken after `finish`. Frames come back
as binary messages; after the `summary` frame the service closes.

## What the service does by itself

- **Parallel requests.** Per engine and host the service limits the requests running at once
  (`max_parallel`) with a semaphore and the waiting ones to `max_queue`; beyond that `overloaded`.
- **Logging.** The service logs with Python's `logging`, one line per skipped or failed entry, the same as in
  the `trace`.

## Rules shared with STT-Suite

- **Access:** while the service listens only on `127.0.0.1`, speaking and the `GET` endpoints ask for
  nothing. Commands that change something (`POST /config`, `/engines/*/start|stop|reload`, `/voices/sync`:
  hosts and SSH targets, GPU containers, cloud keys) always need the token, also locally, since every program
  on the machine reaches localhost. Listening on any other address, everything requires a token
  (`Authorization: Bearer <token>`; the WebSocket takes it in that header too) and the service refuses to
  start without one.
- **Health:** `GET /health` on every service.
- **A client works without the service.** It has its own fallback (Agent-Orc: the browser's voice) and
  names the service by one optional address in its configuration.
- Keys of cloud engines live in the environment or in files kept readable for the user alone, never in the
  configuration or the repository.
- Every repository has a README in German and in English.

## Moving out of AIfred Intelligence

Goes with the suite: the engine folders and their containers, the escalation, engine manager and hosts
(including `scripts/tts-host-ctl.sh` and the compose call that sets `TTS_GPU_UUID`), the cleaning and cutting of the text by unit,
the post-processing, the announcement texts and engine labels (German and English), the configuration, and
the tests of escalation, reload, completeness and the DashScope engine. The engines are the ones that exist in the suite (no MOSS, no Fish-Speech). The container and project names
(and the volume `xtts_voices`) are pinned in the compose files (`name:`), so Docker does not recreate running
containers. The voices folder holds the user's own data: it is not in the public repository, its path is a
setting, and both the suite and AIfred (agent bundles) use that one place. The list and hosts, today in
AIfred's `settings.json`, move to `/config` as the one place; AIfred reads and writes them over HTTP through a
thin client, with a one-time migration and no parallel operation. Stays with AIfred: `resolve_voice` and the
agents' voices, file names and storage of the audio, and a client test.

## Open points

- The format of the audio is WAV; `ogg` could follow where an engine offers it.
