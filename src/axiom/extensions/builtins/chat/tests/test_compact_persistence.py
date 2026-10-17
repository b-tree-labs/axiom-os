# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A compaction survives a reload, and never leaves a half-compacted session (ADR-168, C5).

The hosted store appends messages by position and never removes one, so a shorter message list used to
save as "nothing new" and reload as the uncompacted conversation. ``replace_messages`` is the explicit
rewrite; these tests pin that both stores round-trip a compaction and its undo.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands_ext import compact as c
from axiom.extensions.builtins.chat.commands_ext import load_all
from axiom.extensions.builtins.chat.db_models import Base
from axiom.infra.orchestrator.session import SessionStore
from axiom.infra.orchestrator.session_db import DatabaseSessionStore

from .compact_helpers import FakeLLM, agent_with, conversation, provider


@pytest.fixture
def llm():
    s = FakeLLM()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _audit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path / "runtime"))


@pytest.fixture
def db_store():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    try:
        yield DatabaseSessionStore(engine=engine)
    finally:
        engine.dispose()


def _run(agent, store, line):
    load_all()
    return REGISTRY.dispatch(line, CommandContext(agent=agent, store=store, line=line))


def test_the_hosted_store_reloads_the_compacted_session_and_its_undo(monkeypatch, llm, db_store):
    s = db_store.create(principal_id="@tester")
    for m in conversation(6, with_tools_in={1}).messages:
        s.add_message(m.role, m.content, tool_calls=m.tool_calls)
    db_store.save(s)
    original = [(m.role, m.content, m.tool_calls) for m in s.messages]
    agent = agent_with([provider("local", llm.endpoint)], monkeypatch, s)

    _run(agent, db_store, "/compact keep 2")
    reloaded = db_store.load(s.session_id, "@tester")
    assert [m.content for m in reloaded.messages] == [m.content for m in s.messages]
    assert reloaded.messages[0].content.startswith(c.SUMMARY_MARK)
    assert reloaded.context[c.COMPACTIONS_KEY], "the originals must survive the reload for undo"

    # a later turn still appends, rather than colliding with the old positions
    s.add_message("user", "after")
    db_store.save(s)
    assert db_store.load(s.session_id, "@tester").messages[-1].content == "after"

    _run(agent, db_store, "/compact undo")
    back = db_store.load(s.session_id, "@tester")
    assert [(m.role, m.content, m.tool_calls) for m in back.messages[:-1]] == original


def test_the_file_store_round_trips_a_compaction(monkeypatch, llm, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    s = store.create()
    for m in conversation(5).messages:
        s.add_message(m.role, m.content, tool_calls=m.tool_calls)
    agent = agent_with([provider("local", llm.endpoint)], monkeypatch, s)
    _run(agent, store, "/compact keep 1")
    again = store.load(s.session_id)
    assert again.messages[0].content.startswith(c.SUMMARY_MARK)
    assert len(again.messages) == len(s.messages)


def test_if_the_store_cannot_persist_the_session_is_rolled_back(monkeypatch, llm):
    class Broken:
        def save(self, session):
            raise OSError("disk full")

    s = conversation(5)
    snapshot = [m.to_dict() for m in s.messages]
    agent = agent_with([provider("local", llm.endpoint)], monkeypatch, s)
    out = _run(agent, Broken(), "/compact keep 1")
    assert [m.to_dict() for m in s.messages] == snapshot
    assert c.COMPACTIONS_KEY not in s.context
    assert "disk full" in out and "unchanged" in out.lower()
