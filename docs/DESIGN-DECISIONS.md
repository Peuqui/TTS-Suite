# Design decisions

[Deutsch](DESIGN-DECISIONS.de.md)

Decisions that shape the TTS-Suite, with the reasoning and with what we do if an assumption
turns out wrong. As of 2026-10-11.

## One service as the only owner of the cards

The suite runs as a low-privilege system service on the machine with the GPUs. It starts
and stops the engine containers and chooses the card, for every program that uses it.
Several programs share the same cards; a library in each program would give several owners
that do not know each other. Other machines (over SSH) are an optional list in the
configuration; the normal case is the machine itself.

**A list per request, a catalog, no shared list (2026-10-11).** The service keeps no
escalation list that all programs share. It offers every engine (the catalog, `GET /engines`:
a row per engine on this machine and, for each configured host, per engine with a REST API;
cloud and browser engines run locally only). Each request brings its own list: selection,
order, unit and host per entry. No program owns a list that another one uses; AIfred is not
privileged, other programs are conceivable. Nothing in the request is optional: an empty
list, a repeated entry (the same engine on the same host) and an unknown host are refused at
once, before anything is started. What concerns the cards stays shared: the engine
instances and the slots per engine and address (`max_parallel` holds across all requests and
programs), because the capacity belongs to the service. Start and stop work on any row of
the catalog, without a list.

**Status of a catalog row (2026-10-11).** `running` = it speaks; `starting` = a start is under way;
`stopped` = the host is up (or it is this machine) and the container is not running, so it can be
started; `unreachable` = the host does not answer (for a cloud engine: no access). The difference
comes from the engine's own health request (HTTP to host:port), no ping: a refused connection means the
host is up and nothing listens, a connect timeout, a missing route or an unresolvable name means the
host is off, any HTTP answer means it is there. Every row asks on its own, so a dead host delays
`GET /engines` by one health timeout, not by one per row. A start on an unreachable host fails at
once (`stopped`, "host ... unreachable") without waiting for an SSH connect.

## Release on request

A client that needs room on a card (for example AIfred for a language model) asks the service with
`POST /release` to take containers off the cards: `{"targets": "all"}` or a list of
`{"engine", "host"}`. The container is shut down (VRAM free afterwards), not only unloaded. The
service decides per target: `stopped`, `not_running` (not running or no container), `in_use` (a
reading holds it; the reading comes first, it is NOT stopped), `unreachable` (the host does not
answer, no SSH attempt) or `failed` (the stop did not work, with the message). The targets run in
parallel, a dead host does not delay the others. The whole request is checked before the first stop
(empty, repeated, unknown engine or host: nothing is stopped). It is the same launcher stop as
`stop`, not a second path. The endpoint is a request like `/speak`: no token on loopback, a token
otherwise; the commands that change the service (start, stop, configuration) always need the token.

## Card choice and memory limit

The card is chosen by rules in the configuration (`gpus`: order and reserve per card,
`headroom_mib`), not by a callback to a program. Every engine has a budget
(`vram_budget_mib`, a starting value from the engine's burn-in; the configuration
overrides it). A card qualifies when its free memory minus the reserve covers the budget
plus the headroom (default 512 MiB, the same AIfred has used so far).

The budget serves the card choice and is also the cap in the container
(`TTS_VRAM_BUDGET_MIB`, passed on at `up`). The service does not watch the use and stops no
engine; `EngineInfo.vram_mib` reports it for display only. A card without room gives
`no_vram`, which is not remembered: the next run chooses again. Measured on 2026-10-11 on a V100 with a mounted
file (`sitecustomize.py`, which calls `set_per_process_memory_fraction` on the first CUDA
access): Qwen3-TTS and XTTS run unchanged under a limit slightly above their need, and loading
fails with `OutOfMemoryError` under a limit below the model's need. The limit counts only
PyTorch memory, not the CUDA context; it is an outlier guard, not an exact limit. The mechanism uses `torch.cuda._lazy_call`, a private
PyTorch interface that may break with an update.

## The service cuts the text

The caller sends text as it has it: whole (`POST /speak`) or in pieces while a language
model is still writing (WebSocket `/speak/stream`). Cutting by the unit of the speaking
entry (sentence, paragraph, whole) happens in the service, because only it knows which
entry of the list speaks when the escalation moves on. The unit is part of the request's
entry, not of the configuration; the engine only recommends a default (`unit_default`). Audio always comes back as a stream of
pieces in speaking order, as soon as each is ready.

## Text cleaning in the service

The general cleaning (Markdown, code and tables as a spoken hint, LaTeX, URLs, links, HTML,
emojis) runs in the service before the cutting, with the state of the run (a code block
across several pieces, hints once per reply). Pronunciation replacements (for example
"AIfred" → "Alfred") are a configuration list. Program-specific parts stay with the
program: AIfred filters its own thinking and detail blocks, because another caller may want
them spoken. The engine-specific normalization (special characters that crash the
tokenizer, punctuation against hallucinations) stays in the container.

## Look-ahead for the sentence unit

A sentence counts as finished when more than 10 characters follow it or the text has ended
(protects against abbreviations, decimal numbers, URLs and quotation marks).

**Decision: the first sentence goes at once; from the second on, the suite releases a
sentence only when the next one has arrived.** For the first sentence the time to the first
sound counts. After that the engine speaks more slowly than a language model writes
(spoken about 4 tokens per second), so the next sentence is usually there already; the
look-ahead costs nothing there. Gain: safer sentence boundaries, and a very short ending
(under three words, "And you?") can be attached to the previous sentence while that one is
not yet released.

**If this proves too weak** (too many wrongly split sentences, too-short pieces sound
bad), there are two steps, in this order:

1. Hold back the **second** sentence too, until the third has arrived.
2. Hold back the **first** sentence too, until the second has arrived. This costs the
   generation time of one sentence before the first sound (roughly 2.5 to 3.5 seconds at 10
   tokens per second, about half a second at 50), but detection is safest.

The number of sentences held back should therefore stay a constant, not a hard-wired
special case in the code. Two- and one-word sentences are the borderline case in both
steps: whether the engines speak very short pieces well will show in operation.

## Pauses between paragraphs

A pause between two paragraphs is a parameter of the request (`SpeakRequest.paragraph_pause_ms`, not
optional, 0 = none, negative = `InvalidRequest`), not a setting of an engine or of the client. The suite does
not make silence: it delivers a **pause marker**, a segment without audio (`audio=b""`, empty `media_type` and
`engine`, `keepalive=False`) with `AudioSegment.pause_ms > 0`. The client makes silence of that length for its
output (a device channel plays PCM silence, a browser waits between its files). On the wire it is an audio frame
with an empty body and `pause_ms` in its header, like the keep-alive marker.

The marker stands at every paragraph boundary of the input (a blank line between two paragraphs that have
something to speak), in speaking order: after the last piece of one paragraph and in front of the first of the
next, never before the first or after the last. It is queued in the same order as the syntheses, so it is
delivered at its place however the sentences finish, and it neither holds back the generation of the next ones
nor counts as keep-alive. The cutter marks the boundary (`CutUnit.paragraph_start`: a blank line lies in front of
the unit); whole text and stream give the same units with the same marks. A paragraph that has nothing left to
speak after the cleaning adds no pause of its own, and there are never two markers in a row. For the unit
`whole` there are no markers: the engine sets the pause at the blank line itself. If the speaking entry changes
in mid text, the markers stay; the announcement of the voice change gets none.

## Data folder

Whatever the suite creates or needs and is no part of the source tree lives in one data
folder: `~/.local/share/tts-suite`, changeable with the environment variable
`TTS_SUITE_DATA_DIR` (one function, `constants.data_dir()`, reads it at every call). Inside:
`piper_models/` (Piper's `.onnx` models) and `dashscope_audio3_voices.json` (the mapping of
the voices enrolled with DashScope). Both used to live in the source tree and in the voices
folder respectively; the voices folder now belongs to the user and holds only the reference
WAVs. There is no fallback to the old places and no copying by the code: an existing
collection is moved into the data folder by hand, once. The folder is created when the
mapping is first written; the operator puts the Piper models into it. The Piper executable
still sits next to the suite's Python.
