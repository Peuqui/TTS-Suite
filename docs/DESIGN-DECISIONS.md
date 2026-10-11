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
engine (sentence, paragraph, whole) happens in the service, because only it knows which
engine speaks when the escalation list moves on. Audio always comes back as a stream of
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
