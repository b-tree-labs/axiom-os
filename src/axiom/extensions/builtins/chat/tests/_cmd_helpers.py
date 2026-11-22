# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared drivers for the /diff, /undo and /fork command tests: real registry, real files, real ledger."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import checkpoints
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.tools import execute_tool
from axiom.infra.orchestrator.session import Session


def agent_turn(session_id, writes):
    """One agent turn: ``writes`` is ``[(path, content), ...]`` made through the real write_file tool."""
    with checkpoints.turn_binding(session_id):
        for path, content in writes:
            out = execute_tool("write_file", {"file_path": str(path), "content": content})
            assert "error" not in out, out


def make_agent(store=None, **session_kw):
    return SimpleNamespace(session=Session(**session_kw)), store


def run(line, agent, store=None, surface="repl"):
    ensure_loaded()
    return REGISTRY.dispatch(line, CommandContext(agent=agent, store=store, surface=surface))


@pytest.fixture(autouse=True)
def _jsonl_ledger(monkeypatch):
    """Keep the action ledger in the per-test state dir; without this it writes to a developer's real DB."""
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
