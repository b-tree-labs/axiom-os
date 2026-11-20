# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the /copy, /export and /sources tests: a real agent-shaped object (real Session,
real ChatScope, real gateway) and a fake clipboard executable (the OS clipboard is the only boundary)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

from axiom.extensions.builtins.chat import where
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.orchestrator.session import Session
from tests.test_gateway import _gateway_with_providers, _make_provider

# Built from parts so this file does not itself trip a secret scanner.
FAKE_OPENAI_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"


def make_agent(monkeypatch, turns=(), retrieved=(), provenance=None):
    """``turns`` is [(user, assistant), ...]."""
    provider = _make_provider(
        name="home", endpoint="http://localhost:8080/v1", api_key_env="", priority=1
    )
    session = Session()
    for user, assistant in turns:
        session.add_message("user", user)
        session.add_message("assistant", assistant)
    scope = ChatScope(session=session)
    scope.last_retrieved = list(retrieved)
    scope.last_provenance = provenance
    agent = SimpleNamespace(gateway=_gateway_with_providers([provider], monkeypatch), session=session)
    from axiom.extensions.builtins.chat.agent import set_default_scope

    set_default_scope(agent, scope)
    monkeypatch.setattr(where, "_retrieval_where", lambda: where.RetrievalWhere("local", "", "local"))
    return agent


def run(agent, line, surface="repl"):
    from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded

    ensure_loaded()
    return REGISTRY.dispatch(line, CommandContext(agent=agent, store=None, surface=surface))


def fake_clipboard(tmp_path: Path, monkeypatch) -> Path:
    """Put pbcopy/xclip/xsel on PATH that write stdin to a file. Returns that file."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    out = tmp_path / "clip.out"
    for name in ("pbcopy", "xclip", "xsel"):
        exe = bindir / name
        exe.write_text(f'#!/bin/sh\n/bin/cat > "{out}"\n')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + "/usr/bin:/bin")
    return out


def no_clipboard(tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
