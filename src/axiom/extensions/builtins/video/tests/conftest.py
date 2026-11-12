# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the video extension: a state directory, stand-in tools, a login, a source."""

from __future__ import annotations

import json
import logging
import stat
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _no_receipt_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Receipts off, as the repo's own suite runs (tests/conftest.py).

    Every capability call passes the authority gate, which otherwise probes the
    platform database for its receipt table. The root conftest points that at an
    address meant to refuse; on a host that drops instead of refusing, the probe
    waits out a TCP timeout on every call. Subprocesses inherit this too.
    """
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(path))
    return path


@dataclass
class FakeTools:
    """Wrapper scripts standing in for ``ffmpeg`` and ``ffprobe`` (see fake_ffmpeg.py)."""

    ffmpeg: Path
    ffprobe: Path
    argv_log: Path
    input_log: Path

    def env(self) -> dict[str, str]:
        return {
            "AXIOM_VIDEO_FFMPEG": str(self.ffmpeg),
            "AXIOM_VIDEO_FFPROBE": str(self.ffprobe),
            "FAKE_VIDEO_ARGV_LOG": str(self.argv_log),
            "FAKE_VIDEO_INPUT_LOG": str(self.input_log),
        }

    def inputs(self) -> list[dict]:
        """What each child read from its inherited pipe (the URL, login included)."""
        if not self.input_log.exists():
            return []
        return [json.loads(line) for line in self.input_log.read_text().splitlines() if line]

    def argv(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text().splitlines() if line]


def _wrapper(path: Path, tool: str) -> Path:
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_ffmpeg.py"}" {tool} "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    if sys.platform == "win32":
        pytest.skip("the stand-in tools are POSIX shell wrappers")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tools = FakeTools(
        ffmpeg=_wrapper(bindir / "ffmpeg", "ffmpeg"),
        ffprobe=_wrapper(bindir / "ffprobe", "ffprobe"),
        argv_log=tmp_path / "argv.jsonl",
        input_log=tmp_path / "inputs.jsonl",
    )
    for key, value in tools.env().items():
        monkeypatch.setenv(key, value)
    return tools


@pytest.fixture
def no_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine without ffmpeg: the override names a path where nothing is."""
    monkeypatch.setenv("AXIOM_VIDEO_FFMPEG", str(tmp_path / "nowhere" / "ffmpeg"))
    monkeypatch.setenv("AXIOM_VIDEO_FFPROBE", str(tmp_path / "nowhere" / "ffprobe"))


@dataclass
class Login:
    """A camera login held where the vault would hold it, behind an ``env://`` ref."""

    ref: str
    username: str
    password: str
    env: dict[str, str]


@pytest.fixture
def login(monkeypatch: pytest.MonkeyPatch) -> Login:
    var = f"VIDEO_TEST_LOGIN_{uuid.uuid4().hex[:8].upper()}"
    username, password = "viewer", f"pw-{uuid.uuid4().hex}"
    monkeypatch.setenv(var, f"{username}:{password}")
    return Login(
        ref=f"env://{var}",
        username=username,
        password=password,
        env={var: f"{username}:{password}"},
    )


@pytest.fixture
def file_vault(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The vault's dev backend: ``vault set`` values in a 0600 file under ``<state>/secrets``."""
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.delenv("AXIOM_FILE_SECRETS_PATH", raising=False)
    return state_dir / "secrets"


@pytest.fixture
def fake_source() -> Iterator:
    from .fakes import FakeFrameSource, registered_kind

    source = FakeFrameSource()
    with registered_kind(source):
        yield source


@pytest.fixture
def ctx(state_dir: Path):
    from axiom.extensions.builtins.video import skills
    from axiom.infra.skills import SkillContext, SkillRegistry

    registry = SkillRegistry()
    skills.bind(registry)
    return SkillContext(
        registry=registry, state_dir=state_dir, logger=logging.getLogger("test.video")
    )


@pytest.fixture
def call(ctx):
    """Invoke a video capability the way every surface does: through the gateway."""
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    def _call(capability: str, **params):
        return invoke_capability(ctx.registry, capability, params, ctx, surface=CLI_SURFACE)

    return _call


@pytest.fixture
def no_leftovers(state_dir: Path) -> Iterator[Path]:
    """Whatever a test starts in ``state_dir`` is stopped when it ends, pass or fail."""
    yield state_dir
    from .harness import stop_everything

    stop_everything(state_dir)
