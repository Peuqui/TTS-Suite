# TTS-Suite

[Deutsch](README.de.md)

Text-to-speech as a small HTTP service, built from engines that run as modules: each engine is a
Docker container (or a cloud service), and one interface in front of them lets any program have
text spoken without implementing speech output itself. The sister project is
[STT-Suite](https://github.com/Peuqui/STT-Suite) (speech recognition).

> **Status: design stage.** The repository starts with the interface ([docs/INTERFACE.md](docs/INTERFACE.md)),
> which the programs that will use it review first. The engines are taken over from
> [AIfred Intelligence](https://github.com/Peuqui/AIfred-Intelligence), where they run today.

## Idea

- **Engines are modules.** Local engines (for example XTTS, Qwen3-TTS) run as Docker containers
  you choose with `docker compose`; cloud engines (for example Edge, DashScope) are called over
  the internet. You run only the ones you want.
- **One list decides who speaks.** An ordered escalation list names engine and place (this machine,
  another machine, cloud). The first entry that can speak does; if it fails, the next takes over.
- **No program depends on another.** A client only knows the address of the service. Without the
  service it has its own fallback (the browser's voice, for example).
- **Voices are files** in one shared folder, read-only for every engine.

## Requirements

- Docker with Compose; for local GPU engines an NVIDIA GPU with the NVIDIA Container Toolkit.

## License

To be added with the first release.
