"""``scripts/tts-host-ctl.sh`` — run as the pinned SSH key runs it: the command in SSH_ORIGINAL_COMMAND, in a
copy of the repository layout with a stub ``docker`` that records what compose is given."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tts_suite.constants import SUITE_ROOT

SCRIPT = SUITE_ROOT / "scripts" / "tts-host-ctl.sh"
STUB_DOCKER = '#!/usr/bin/env bash\necho "$TTS_VOICES_DIR $*" >> "$STUB_LOG"\n'


@pytest.fixture
def suite_copy(tmp_path: Path) -> Path:
    root = tmp_path / "suite"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    (root / "docker" / "tts" / "xtts").mkdir(parents=True)
    (root / "docker" / "tts" / "xtts" / "docker-compose.yml").write_text("services: {}\n")
    (root / "voices").mkdir()
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    return root


def _run(root: Path, command: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    environment = {
        **os.environ,
        "SSH_ORIGINAL_COMMAND": command,
        "PATH": f"{root.parent / 'bin'}:{os.environ['PATH']}",
        "STUB_LOG": str(root.parent / "docker.log"),
        "TTS_VOICES_DIR": "",
    }
    return subprocess.run(
        [str(root / "scripts" / SCRIPT.name)], input=stdin, capture_output=True, env=environment, check=False,
    )


def _tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize("command", ["start xtts", "stop xtts", "stop", "status"])
def test_every_compose_call_sees_the_voices_folder_next_to_the_script(suite_copy: Path, command: str) -> None:
    assert _run(suite_copy, command).returncode == 0
    calls = (suite_copy.parent / "docker.log").read_text().splitlines()
    assert calls and all(call.startswith(f"{suite_copy / 'voices'} compose") for call in calls)


def test_received_voices_are_added_and_nothing_is_deleted(suite_copy: Path) -> None:
    (suite_copy / "voices" / "Old").mkdir()
    (suite_copy / "voices" / "Old" / "Old.wav").write_bytes(b"old")
    answer = _run(suite_copy, "receive-voices", _tar({"./Alice/Alice.wav": b"new"}))
    assert answer.returncode == 0
    assert (suite_copy / "voices" / "Alice" / "Alice.wav").read_bytes() == b"new"
    assert (suite_copy / "voices" / "Old" / "Old.wav").read_bytes() == b"old"


def test_a_member_that_leaves_the_voices_folder_is_refused(suite_copy: Path) -> None:
    answer = _run(suite_copy, "receive-voices", _tar({"../evil.txt": b"x"}))
    assert answer.returncode != 0 and not (suite_copy / "evil.txt").exists()
