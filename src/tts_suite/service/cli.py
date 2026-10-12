"""`tts-suite serve` starts the service, `tts-suite token` makes the token for the commands that
change something."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from ..api import SuiteConfig
from ..constants import DEFAULT_VOICES_DIR
from ..suite import create_suite
from .app import create_app
from .auth import access_for, create_token, read_token
from .config_store import ConfigStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5090
# The most text one reading takes; the cleaning of the text grows with its length.
DEFAULT_MAX_TEXT_CHARS = 50_000
CONFIG_FILE_NAME = "config.json"
TOKEN_FILE_NAME = "token"


def config_dir() -> Path:
    """The service's own folder below the XDG config directory."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tts-suite"


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="tts-suite", description="The TTS-Suite service.")
    commands = main.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the service")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--config", type=Path, default=config_dir() / CONFIG_FILE_NAME)
    serve.add_argument("--token-file", type=Path, default=config_dir() / TOKEN_FILE_NAME)
    serve.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS)
    serve.add_argument("--voices-dir", type=Path, default=DEFAULT_VOICES_DIR, help="the shared voices folder, for the first start")
    token = commands.add_parser("token", help="make the token file if there is none")
    token.add_argument("--token-file", type=Path, default=config_dir() / TOKEN_FILE_NAME)
    return main


def serve(arguments: argparse.Namespace) -> None:
    # What the suite does is in the log (which engine speaks with which voice, speed and pitch), at INFO.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("tts_suite").setLevel(logging.INFO)
    store = ConfigStore(arguments.config)
    config = store.load() or SuiteConfig(voices_dir=arguments.voices_dir)
    try:
        access = access_for(arguments.host, read_token(arguments.token_file))
    except ValueError as error:
        sys.exit(str(error))
    store.save(config)
    uvicorn.run(
        create_app(create_suite(config), access, store, arguments.max_text_chars),
        host=arguments.host,
        port=arguments.port,
    )


def main() -> None:
    arguments = parser().parse_args()
    if arguments.command == "token":
        create_token(arguments.token_file)
        print(f"The token is in {arguments.token_file} (readable for you alone).")
    else:
        serve(arguments)
