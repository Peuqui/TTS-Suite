# TTS-Suite

[English](README.md)

Sprachausgabe (Text zu Sprache) als kleiner HTTP-Dienst, gebaut aus Engines, die als Module laufen:
Jede Engine ist ein Docker-Container (oder ein Cloud-Dienst), und eine Schnittstelle davor lässt jedes
Programm Text sprechen, ohne selbst Sprachausgabe zu bauen. Das Schwesterprojekt ist
[STT-Suite](https://github.com/Peuqui/STT-Suite) (Spracherkennung).

> **Stand: Entwurfsphase.** Das Repository beginnt mit der Schnittstelle ([docs/INTERFACE.de.md](docs/INTERFACE.de.md)),
> die zuerst die Programme prüfen, die sie nutzen werden. Die Engines werden aus
> [AIfred Intelligence](https://github.com/Peuqui/AIfred-Intelligence) übernommen, wo sie heute laufen.

## Idee

- **Engines sind Module.** Lokale Engines (zum Beispiel XTTS, Qwen3-TTS) laufen als Docker-Container, die
  man mit `docker compose` auswählt; Cloud-Engines (zum Beispiel Edge, DashScope) werden über das Internet
  gerufen. Man betreibt nur die, die man will.
- **Eine Liste entscheidet, wer spricht.** Eine geordnete Eskalationsliste nennt Engine und Ort (dieser
  Rechner, ein anderer Rechner, Cloud). Der erste Eintrag, der sprechen kann, spricht; fällt er aus, übernimmt
  der nächste.
- **Kein Programm hängt von einem anderen ab.** Ein Client kennt nur die Adresse des Dienstes. Ohne den
  Dienst hat er seinen eigenen Ersatz (zum Beispiel die Stimme des Browsers).
- **Stimmen sind Dateien** in einem gemeinsamen Ordner, für jede Engine nur lesbar.

## Voraussetzungen

- Docker mit Compose; für lokale GPU-Engines eine NVIDIA-Grafikkarte mit dem NVIDIA Container Toolkit.

## Lizenz

Kommt mit der ersten Veröffentlichung.
