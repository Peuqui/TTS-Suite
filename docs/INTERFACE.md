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
   (AIfred, Agent-Orc, others) share the same cards. It tries the engines **the request names**, in the order it names them, **cuts the
   text by the unit of the entry that speaks**, post-processes the audio (speed and pitch with ffmpeg for
   engines without native support) and streams it back. A client never talks to a container directly. Other
   machines (SSH) are an optional list of hosts; the normal case is this machine. **There is no list of
   engines in the service that anyone shares:** it offers all its engines (the catalog), and every request
   brings its own list.

The service cleans the text for speech by unit (Markdown, numbers, emoji, URLs; a block of code is replaced by
a short spoken hint, once per block; `pronunciations` of the configuration swap words for the form that is
spoken). What stays with the client: file names and storage of the audio, and everything that knows its
agents (which agent has which voice). The service gets text and answers with audio.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /health` | `200` with `{"status": "ok"}` while the service runs. |
| `GET /ui/` | The page of the service (plain HTML, CSS and JavaScript, light or dark, German or English): the catalog with start and stop, the configuration, the voices with sync, and trying a reading with a list of its own and its course. It holds no data and needs no token; what it asks for follows the rules below (the token is entered once and kept in `localStorage` of the browser). |
| `GET /engines` | The catalog: every engine on every machine it can run on; fields below. |
| `GET /speak-test`, `PUT /speak-test` | What the page keeps of the test reading (text, language, pause, order and choices per engine), on the server, so that every device sees the same (`speak-test.json` next to the configuration). `PUT` needs the token. |
| `GET /voices?engine=<key>` | The voices of one engine: for each `name` and `label`. Without `engine`: the folders of the shared voices directory. |
| `POST /voices/sync?engine=<key>` | Has a cloud engine that clones voices (DashScope) upload the voices of the shared directory. Answers with the voices now known. |
| `POST /voices/push` | Hands the voices directory to every configured host (`receive-voices` of `scripts/tts-host-ctl.sh`; the host adds and overwrites, nothing is deleted). Needs the token. Answers `{"hosts": [names]}`; a host that does not take it is an error naming it. |
| `POST /speak` | Door 1: the whole text; the answer is the stream of frames (below). |
| `WS /speak/stream` | Door 2: the text arrives in pieces (a language model is still writing); the frames run back as they exist. |
| `POST /engines/{engine}/start`, `POST /engines/{engine}/stop` | Starts or stops the container of a row of the catalog (query `host`: the machine's name; none: this one). On start, the query `gpu` (index or UUID) puts a GPU engine of this machine on exactly that card, past the rules of `gpu_mode` (for a measurement on a given card): `422` for an engine on a host or without GPU, `409` while it runs or starts, `502` with the reason when the card is missing or has too little room. |
| `POST /release` | A client asks for room on the cards of OTHER hosts. Body `{"targets": "all"}` (every container row on the hosts) or `{"targets": [{"engine", "host"}]}`. Shuts the containers down (not only unloads) and answers per target `{engine, host, outcome, detail}`: `stopped`, `not_running`, `in_use` (a reading holds it, left alone), `unreachable` (host does not answer), `failed`. Engines on this machine: `422` `invalid_request`, they go through `/gpu/release` (one way). A request like `/speak`: no token on this machine. |
| `GET /gpu/holders` | The common GPU release API of the suites (the STT-Suite has the same; [GPU-Orc](https://github.com/Peuqui/GPU-Orc) uses it): `{"holders": [{item, gpu_uuids, memory_mib, busy, need_mib}]}`, every GPU engine of this machine whose container runs. `gpu_uuids` comes from the container's configuration (`DeviceRequests` or `NVIDIA_VISIBLE_DEVICES`, as UUIDs), so it is always known; `memory_mib` per card is `null` when it cannot be measured here; `busy` = a reading holds it or it is starting; `need_mib` = what it needs on a card, as in `/gpu/needs` (`null` = unknown). |
| `GET /gpu/needs` | What each local GPU engine needs on a card, running or not, for GPU-Orc's plan: `{"needs": [{item, need_mib, source}]}`. `need_mib` is the configured `vram_budget_mib` (`source: configured`), else the engine's built-in value (`estimate`), else `null` (`unknown`). No token on this machine. |
| `POST /gpu/release` | Body `{"items": ["<engine>", ...] \| "all", "force": false, "reason": "..."}` (`reason` required, it is logged). One result per item `{item, outcome, detail}`: `released` (the container is proven down: Docker reports it gone or exited, after `docker kill` if the stop did not do it), `not_running`, `busy` (a reading holds it or it is starting, and `force` is false), `failed` (the end could not be proven). `200`, `409` when one is busy, `500` when one failed. With `force` a start under way is withdrawn (the start gives up before `docker compose up` or at the next poll while the model loads), the readings that hold the engine or wait for its start end with `reading_aborted` naming the reason, then the container is stopped. No token on this machine. |
| `POST /engines/reload` | Reads the engine folders again (an engine is switched off by moving its folder to `disabled/`: it is not in the catalog, and a request that names it gets a skip `deactivated`). Answers `{"removed": [...], "added": [...]}`. |
| `GET /config`, `POST /config` | The configuration below. Changes survive a restart (as in STT-Suite). |

### `GET /engines`

One row per engine and machine: this machine (`host` null) gets a row for each engine of the suite, and each
configured host a row for each engine that has a REST API there (no cloud or browser engines on a host).
A row has `engine`, `host`, `label` and `label_short` (names for a user interface, German and English),
`status` (`running`, `stopped`, `starting`, `unreachable`: the host does not answer),
`unit_default` (`sentence`, `paragraph` or `whole`: how much text the engine recommends it is given at once),
`needs_gpu`, `cloud`, `browser`, `installed` (it can be started: it needs no container (cloud, CPU) or runs on
another host, otherwise its Docker image is built), `start_hint` (why the last start of this entry failed,
until the next start, a new configuration or a reload; otherwise null), `vram_mib` and `idle_remaining_s`
(seconds until a running container ends itself for being idle, any request resets it; null = not running, no
container or auto-shutdown off). Rows of other hosts
are asked in parallel: a dead host does not delay the others.

`browser: true` marks the row that stands for the client's own voice (the browser speaks, the service cannot);
a request may not name it, the client decides whether and how it speaks.

### `GET /config`, `POST /config`

One object: `voices_dir`; `hosts` (optional, the machines requests refer to by name: `name`, `address`,
`ssh_port`, `ssh_user`); `engines` (defaults per engine key: `voice` with `voice`, `speed`, `pitch`; `voice_tuning`, speed and pitch per
voice name, which apply whenever that voice speaks, after the request's own values and before the engine's defaults;
`vram_budget_mib`, `max_parallel`, `max_queue`); `gpus` and `headroom_mib` (see below); `placement_lock_file` (a file every program placing models on the cards locks with `flock` while it chooses a card and loads onto it, the STT suite among them; `null` = the suite is alone on the cards); `gpu_orc_url` (the GPU-Orc of this machine, `http://127.0.0.1:5070`: its `available_mib` per card counts as the free VRAM where it is less than nvidia-smi's, because a vLLM model may fill its share of a card while it computes; a GPU-Orc that does not answer or cannot say refuses the start with `no_vram`; `null` = no GPU-Orc, nvidia-smi alone);
`pronunciations` (a word to the form spoken instead). `POST` validates once, in the engine package; a changed `voices_dir` releases the running containers (one a reading holds is logged and keeps the old folder), the next request starts them with the new one;
what cannot work is `400` with the reason. There is no list of entries: who speaks, in which order, is the
request's business. Cloud keys are not part of it (below).

**GPU placement is a rule set**, no callback to a program. `gpu_mode` `auto` (the default) puts an engine on a card no program
uses that fits, else on the card with the most free VRAM that fits; `list` only on the first card of `gpus` that fits
(`gpu` = index as `nvidia-smi` lists it, or UUID; `reserve_mib` = VRAM that stays free for other programs)
whose free VRAM minus the reserve covers the engine's `vram_budget_mib` plus `headroom_mib` (a margin so a
card is not filled to the last MiB). The container gets the budget as its cap; the suite does not watch the
use or stop an engine for it (`GET /engines` reports what an engine holds as `vram_mib`, for display). An
`gpus` is not used in `auto` mode (it is kept for the next switch to `list`; `list` needs at least one card). No card fits: the entry is skipped
with `no_vram` (not remembered: the next request chooses again).

### The request: the head of a reading

```json
{"language": "en", "paragraph_pause_ms": 700,
 "engines": [{"engine": "qwen3local", "unit": "sentence", "host": null},
             {"engine": "xtts", "unit": "paragraph", "host": "Aragon"},
             {"engine": "edge", "unit": "sentence", "host": null}],
 "voices": {"qwen3local": {"voice": "Alice", "speed": 1.0, "pitch": 1.0}, "edge": {"voice": "en-GB-SoniaNeural"}},
 "speed": 1.0, "pitch": 1.0, "run": "r-42", "wait": true}
```

- `language` and `paragraph_pause_ms` are required. `paragraph_pause_ms` (integer, 0: no pause; negative: `invalid_request`; missing: `bad_request`) is the silence between paragraphs, given as pause markers. `POST /speak` carries the head and the text as `{"text": "...", ...head}`; the
  WebSocket opens with the head alone.
- **`engines` is required, there is no default.** Its entries are the list of this one reading, in the
  order of speaking: `engine`, `unit` (`sentence`, `paragraph` or `whole`: how much text the engine is given
  at once; the service cuts by it) and `host` (null: this machine, otherwise a name from `/config`). An engine
  may appear more than once, but not the same engine on the same host twice. Nobody owns a list that others
  use: every client keeps its own. An empty list, a double entry, an engine without a REST API on that host or
  an invalid unit is `422` (`invalid_request`); an unknown host `404` (`unknown_host`), an unknown engine `404`
  (`unknown_engine`).
- **A voice belongs to an engine.** `voices` names voice, speed and pitch per engine key (a folder of the shared
  directory for the container engines, names of their own for Edge, Piper and cloud voices); `voice`, `speed`
  and `pitch` of the head are the defaults for engines `voices` does not name, then those of `/config`.
  The key may also name one row: `engine` = every row of the engine, `engine@` = only the one on this machine,
  `engine@host` = only that host; the more specific key wins.
- The first entry that can speak does. If it fails, the next one takes over.
- **`run`** (optional, an ID the client makes up for one reading): the service keeps per run which entries
  failed and whether something was spoken already, for about five minutes after the last piece. Entries that failed stay out for
  the rest of the run. If an entry fails after the first sound, the successor first speaks an announcement
  (for example "Voice change to X because of Y", in the `language` of the request: de, en, fr, it, es, pt, ru, zh, ja, ko; the texts live in the
  suite), **a segment of its own in front of the successor's first one**, and then the unfinished piece. The
  choice and the state of a run sit behind a lock, so two pieces never both start the successor and the
  announcement is made once. If the service restarts in the middle of a run, the state is lost and the
  escalation starts at the top of the request's list again (the client sees it in `engine` of the frames).
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
  takes long, for every format; drop it). `pause_ms` (`N > 0`, empty body, `engine` empty, `keepalive` false): a pause marker, to be played as N ms of silence in the client's own output. It stands in the order of speaking at every paragraph boundary (blank line), not before the first or after the last paragraph, and only when the speaking entry's unit is `sentence` or `paragraph` (not `whole`). On a change of the speaking entry the marker comes first, then the announcement, then the successor's piece.
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
(`404`), `unknown_host` (`404`), `invalid_request` (`422`: the list or an entry is wrong), `engine_in_use` (`409`: a stop for a container a reading is using), `reading_aborted` (`503`: a forced GPU release ended the reading because its card is needed, the detail names the reason; the reading does not move on to another engine, the client may read the text again), `engine_control_failed` (`502`:
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
  (`X-TTS-Suite-Token: <token>`; the WebSocket takes it in that header too) and the service refuses to
  start without one.
- **Health:** `GET /health` on every service.
- **A client works without the service.** It has its own fallback (Agent-Orc: the browser's voice) and
  names the service by one optional address in its configuration.
- Keys of cloud engines live in the environment or in files kept readable for the user alone, never in the
  configuration or the repository.
- Every repository has a README in German and in English.

## Moving out of AIfred Intelligence

Goes with the suite: the engine folders and their containers, the escalation, engine manager and hosts
(including `scripts/tts-host-ctl.sh`, which mounts the voices folder next to it and unpacks pushed voices, and the compose call that sets `TTS_GPU_UUID`), the cleaning and cutting of the text by unit,
the post-processing, the announcement texts (ten languages) and engine labels (German and English), the configuration, and
the tests of escalation, reload, completeness and the DashScope engine. The engines are the ones that exist in the suite (no MOSS, no Fish-Speech). The container and project names
(and the volume `xtts_voices`) are pinned in the compose files (`name:`), so Docker does not recreate running
containers. The suite keeps its own data (Piper models, the DashScope voice mapping) in one data folder, `~/.local/share/tts-suite`
or the folder `TTS_SUITE_DATA_DIR` names. The voices (`voices/`) and their source recordings (`voice-sources/`) are part of the repository; the path
of the voices folder is a setting (`voices_dir`), and both the suite and AIfred (agent bundles) use that one place. The hosts, today in AIfred's
`settings.json`, move to `/config` as the one place; the lists of engines stay with the clients (AIfred keeps
his, Agent-Orc its own): each request brings its list. AIfred reads and writes the hosts over HTTP through a
thin client, with a one-time migration and no parallel operation. Stays with AIfred: `resolve_voice` and the
agents' voices, file names and storage of the audio, and a client test.

## Open points

- The format of the audio is WAV; `ogg` could follow where an engine offers it.
