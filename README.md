# TTS-Suite

[Deutsch](README.de.md)

Text-to-speech as a small HTTP service, built from engines that run as modules: each engine is a
Docker container (or a cloud service), and one interface in front of them lets any program have
text spoken without implementing speech output itself. The sister project is
[STT-Suite](https://github.com/Peuqui/STT-Suite) (speech recognition).

> **Status: early.** The engine layer (taken over from
> [AIfred Intelligence](https://github.com/Peuqui/AIfred-Intelligence), where the engines run today) and
> the HTTP service are written and tested against fakes; a run with real containers is still to come. The
> interface is in [docs/INTERFACE.md](docs/INTERFACE.md).

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

## Start

```bash
pip install .                                   # Python 3.12+
tts-suite token                                 # makes ~/.config/tts-suite/token (readable for you alone)
tts-suite serve --voices-dir ~/voices           # http://127.0.0.1:5090, configuration in ~/.config/tts-suite/config.json
curl http://127.0.0.1:5090/health
```

While the service listens on `127.0.0.1`, speaking and every `GET` ask for nothing; a command that changes
something (`POST /config`, starting and stopping containers, uploading voices) always needs the token
(`Authorization: Bearer <token>`). On another address everything needs it, and the service does not start
without one (`--host`, `--token-file`). The escalation list, the hosts and the GPU rules are set with
`POST /config`; see the [interface](docs/INTERFACE.md).

## License

To be added with the first release.
