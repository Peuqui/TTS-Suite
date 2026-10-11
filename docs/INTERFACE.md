# The interface of the TTS-Suite service

[Deutsch](INTERFACE.de.md)

**Draft 2 for review** by the programs that will use it (AIfred Intelligence, Agent-Orc). Draft 2 takes in
AIfred Intelligence's review of draft 1. Where an engine container is concerned, the existing convention of
AIfred Intelligence (`docs/en/architecture/tts-container-conventions.md`) is meant to move here unchanged.

## Two layers

1. **Engine containers**, one per engine, each with its own small HTTP server. A request carries the
   text, the voice name and the language, never audio bytes; the answer is the audio file
   (`audio/wav`). The voices are files in one folder mounted read-only into every container.
2. **The service** in front of them: the one interface a program talks to. It picks the engine from the
   escalation list, starts and stops containers (on this machine, or on another one over SSH), post-processes
   the audio (speed and pitch with ffmpeg for engines without native support), and hands it back. A client
   never talks to a container directly, not even one on another machine.

What stays with the client: cutting the text into sentences, cleaning it for speech (Markdown, numbers,
emoji), file names and storage of the audio, and everything that knows its agents (which agent has which
voice). The service gets finished text and answers with bytes.

## Endpoints of the service

| Method and path | Purpose |
|---|---|
| `GET /health` | `200` with `{"status": "ok"}` while the service runs. |
| `GET /engines` | The escalation list in speaking order; fields below. |
| `GET /voices?engine=<key>` | The voices of one engine: for each `name` and `label`. Without `engine`: the folders of the shared voices directory. |
| `POST /voices/sync?engine=<key>` | Has a cloud engine that clones voices (DashScope) upload the voices of the shared directory (enrollment). Answers with the voices now known. |
| `POST /speak` | Has a text spoken. JSON body below; answers `200` with `audio/wav`. |
| `POST /engines/{engine}/start`, `POST /engines/{engine}/stop` | Starts or stops an entry's container (query `host` for another machine). |
| `POST /engines/reload` | Reads the engine folders again (an engine is switched off by moving its folder to `disabled/`; it stays in the list as `deactivated`). |
| `GET /config`, `POST /config` | The escalation list (`engine`, `host`, `enabled`), the hosts (`name`, `address`, `ssh_port`, …) and per-engine defaults (`unit`, voice, speed, pitch). Changes survive a restart (as in STT-Suite). |

### `GET /engines`

For each entry: `engine`, `host` (null: this machine), `label` and `label_short` (names for a user
interface, in German and English), `enabled`, `deactivated`, `status` (`running`, `stopped`, `starting`,
`unreachable`), `unit` (the user's choice) and `unit_default` (`sentence`, `paragraph` or `whole`: how much
text the engine takes at once), `needs_gpu`, `cloud`, `browser`.

`browser: true` marks the entry that stands for the client's own voice (the browser speaks, the service
cannot). It is in the list because the order is the list's business; the service always skips it, and the
client decides whether and how it speaks (text, language, voice, speed and pitch it has itself).

### `POST /speak`

```json
{"text": "Hello world.", "language": "en", "voice": "Alice", "speed": 1.0, "pitch": 1.0,
 "engine": null, "voices": {"edge": {"voice": "en-GB-SoniaNeural", "speed": 1.1}}, "run": "r-42",
 "wait": true}
```

- `text` and `language` are required; `voice`, `speed` and `pitch` default to the configured ones.
- **A voice belongs to an engine.** `voice` is the default voice (a folder of the shared directory, valid for
  the container engines). Other engines have names of their own (Edge, Piper, cloud voices), so `voices` may
  override voice, speed and pitch per engine key. The client delivers its agents' voices this way; the
  service never has to know the agents, and the client never has to know which engine will speak.
- `engine` (optional) names one entry and skips the escalation: that engine speaks or the request fails.
- Without it the first entry of the list that can speak does. If it fails, the next one takes over.
- **`run`** (optional, an ID the client makes up for one reading, for example one answer): the service keeps
  per run which entries failed and whether something was spoken already, for about ten minutes. Sentences of
  one run may arrive in parallel: the choice and the state of a run sit behind a lock, so two sentences never
  both start the successor and never both speak the announcement; **only the first sentence the successor
  serves gets it**. If the service restarts in the middle of a run, the state is lost and the escalation
  starts at the top of the list again (the client sees it in `X-TTS-Engine`). Entries that
  failed stay out for the rest of the run. If an entry fails after the first sound, the successor first
  speaks an announcement (for example "Voice change to X because of Y", German or English by `language`;
  the texts live in the suite) and then the unfinished sentence. Without `run` the service holds no state.
- **`wait`** (default `true`): the request may wait for a container to start (cold start: typically 20 s and
  more; the client sets its timeout accordingly). With `false` the answer is at once `503`
  `{"error": "starting"}` and the start goes on in the background.
- The answer carries the audio and these headers:
  - `X-TTS-Engine`: who spoke, as `engine@host`.
  - `X-TTS-Trace`: JSON list `{entry, outcome, reason}` with `outcome` one of `skipped`, `failed`, `spoke` —
    the whole course of the escalation (replaces a header of failures only; the client builds its notes from
    it, for example "XTTS · Aragon → DashScope (outage)").
  - `X-TTS-Switch`: only if an announcement of a voice change was put in front; carries its reason.
- Reasons are the same codes everywhere. Failures (after the attempt): `engine`, `software`, `unreachable`,
  `timeout`, `cloud_quota`. Skips (before the attempt): `disabled`, `deactivated`, `stopped`, `no_vram`.
- `X-TTS-Trace` is compact ASCII-only JSON (no umlauts, no line breaks) and kept short, since servers cut
  headers at about 8 KB.
- No entry can speak: `503` with `{"error": "no_engine", "detail": ..., "trace": [...]}`. Bad input: `400`.
  Too many requests for an engine: `429` (see below).

A client that needs a sentence at a time cuts the text itself by the `unit` that `GET /engines` reports.

## What the service does by itself

- **Parallel requests.** Per engine and host the service limits the requests running at once (`max_parallel`
  in the configuration of the engine) with a semaphore; clients may send the sentences in parallel. Beyond a
  queue limit it answers `429`.
- **Placement of GPU engines.** Which GPU a container starts on is decided by a *placement policy*, one
  replaceable function. The default asks `nvidia-smi` for the free VRAM and takes the card with the most.
  A program that knows more (AIfred Intelligence knows which models llama-swap and vLLM hold) supplies its
  own policy; it is named in the configuration and loaded as a Python module. Without a policy that fits the
  machine, the suite would start containers blind next to other loads.
- **Logging.** The service logs with Python's `logging`, one line per skipped or failed entry, the same as
  in `X-TTS-Trace`.

## Rules shared with STT-Suite

- Errors are JSON: `{"error": "<code>", "detail": "<text>"}`.
- **Access:** while the service listens only on `127.0.0.1`, `POST /speak` and the `GET` endpoints ask for
  nothing. Commands that change something (`POST /config`, `/engines/*/start|stop|reload`, `/voices/sync`:
  hosts and SSH targets, GPU containers, cloud keys) always need the token, also locally, since every
  program on the machine reaches localhost. Listening on any other address, everything requires a token
  (`Authorization: Bearer <token>`) and the service refuses to start without one. The
  service holds the keys of the cloud engines and the GPU: an open service on a network lets anyone speak
  on your account.
- **Health:** `GET /health` on every service.
- **A client works without the service.** It has its own fallback (Agent-Orc: the browser's voice) and
  names the service by one optional address in its configuration.
- Keys of cloud engines live in the environment or in files kept readable for the user alone, never in
  the repository.
- Every repository has a README in German and in English.

## Moving out of AIfred Intelligence

Goes with the suite: the engine folders and their containers, the escalation, engine manager and hosts
(including `scripts/tts-host-ctl.sh` and the compose call that sets `TTS_GPU_UUID`), the post-processing, the
announcement texts and engine labels (German and English), the configuration (replaces the parts of
`aifred.lib.config` it used), and the tests of escalation, reload, completeness and the DashScope engine. The container and project names
(and the volume `xtts_voices`) are pinned in the compose files (`name:`), so Docker does not recreate running
containers. The voices folder holds the user's own data: it is not in the public repository, its path is a
setting, and both the suite and AIfred (agent bundles) use that one place. The list and hosts, today in
AIfred's `settings.json`, move to `/config` as the one place; AIfred reads and writes them over HTTP through
a thin client, with a one-time migration and no parallel operation.
Stays with AIfred: `resolve_voice` and the agents' voices, the sentence cutting and text cleaning, the file
names and storage of the audio, its placement policy (supplied to the suite), and a client test.

## Open points

- Streaming (chunks while the engine still speaks) is not part of the first version; `POST /speak` returns
  the whole audio per sentence. Chunks for `whole` engines may follow.
- The format is WAV; `ogg` could follow where an engine offers it.
