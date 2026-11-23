# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/fork copies the live conversation, switches to the copy, and keeps the original resumable."""

from __future__ import annotations

from axiom.extensions.builtins.chat.command_registry import REGISTRY
from axiom.extensions.builtins.chat.tests._cmd_helpers import (
    _jsonl_ledger,  # noqa: F401
    make_agent,
    run,
)
from axiom.infra.orchestrator.session import SessionStore


def _chat(tmp_path, principal="@alice:s"):
    store = SessionStore(sessions_dir=tmp_path / "sessions")
    agent, _ = make_agent(principal_id=principal, title="plan")
    agent.session.add_message("user", "hello")
    agent.session.add_message("assistant", "hi")
    return agent, store


def test_fork_switches_the_chat_and_leaves_the_original_resumable(tmp_path):
    agent, store = _chat(tmp_path)
    original = agent.session.session_id
    out = run("/fork", agent, store)
    assert agent.session.session_id != original
    assert agent.session.title == "plan (fork)" and "fork" in out
    resumed = store.load(original)
    assert [m.content for m in resumed.messages] == ["hello", "hi"] and resumed.title == "plan"
    assert original in out, "the output names the original so it can be resumed"


def test_fork_with_a_title_and_forked_from_context(tmp_path):
    agent, store = _chat(tmp_path)
    original = agent.session.session_id
    run("/fork try the other approach", agent, store)
    assert agent.session.title == "try the other approach (fork)"
    assert agent.session.context["forked_from"] == {
        **agent.session.context["forked_from"],
        "session_id": original,
        "message_index": 2,
    }
    assert store.load(agent.session.session_id).context["forked_from"]["session_id"] == original


def test_unsaved_messages_are_in_the_fork(tmp_path):
    agent, store = _chat(tmp_path)  # never saved yet
    run("/fork", agent, store)
    assert [m.content for m in agent.session.messages] == ["hello", "hi"]


def test_the_fork_keeps_the_owner(tmp_path):
    agent, store = _chat(tmp_path, "@alice:s")
    run("/fork", agent, store)
    assert agent.session.principal_id == "@alice:s"


def test_an_empty_conversation_has_nothing_to_fork(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "s")
    agent, _ = make_agent(principal_id="@alice:s")
    before = agent.session.session_id
    out = run("/fork", agent, store)
    assert "nothing to fork" in out.lower() and agent.session.session_id == before


def test_fork_is_available_to_the_repl_and_the_tui_through_the_registry():
    from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded

    ensure_loaded()
    for name in ("/fork", "/diff", "/undo"):
        cmd = REGISTRY.get(name)
        assert cmd is not None and {"repl", "tui"} <= set(cmd.surfaces)
    assert REGISTRY.get("/undo").mutates and REGISTRY.get("/fork").mutates and not REGISTRY.get("/diff").mutates
