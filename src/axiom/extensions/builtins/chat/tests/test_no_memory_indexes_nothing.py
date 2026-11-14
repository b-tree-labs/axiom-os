# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``--no-memory`` means nothing from the session is recalled, recorded or indexed (#2009).

The flag used to skip only ``attach_memory``. The transcript was still indexed into the node's
internal corpus after every turn, so a session started without memory came back in a later one.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat import memory_wiring
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway
from axiom.infra.orchestrator.session import Session, SessionStore

SECRET = "PLOVERWICK5521"


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A repository root with a real SQLite corpus and a session folder; the indexer runs inline."""
    import axiom.rag.embeddings as embeddings
    import axiom.rag.personal as personal
    from axiom.extensions.builtins.settings import store as settings_mod

    monkeypatch.setattr(embeddings, "embed_texts", lambda *a, **k: None)
    monkeypatch.setattr(personal, "embed_texts", lambda *a, **k: None)
    root = tmp_path / "machine"
    sessions = root / "runtime" / "sessions"
    sessions.mkdir(parents=True)
    corpus_url = f"sqlite:///{root}/rag.db"

    class _Settings:
        def get(self, key, default=None):
            return corpus_url if key == "rag.database_url" else default

    class _SyncThread:
        def __init__(self, target, daemon=False):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(settings_mod, "SettingsStore", _Settings)
    monkeypatch.setattr(agent_mod, "_REPO_ROOT", root)
    monkeypatch.setattr(agent_mod, "threading", SimpleNamespace(Thread=_SyncThread))
    return SimpleNamespace(corpus_url=corpus_url, sessions=SessionStore(sessions_dir=sessions), sessions_dir=sessions)


@pytest.fixture
def agent(machine, tmp_path):
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.complete_with_tools.return_value = CompletionResponse(text="ok", provider="test", success=True)
    return ChatAgent(gateway=gw, bus=EventBus(log_path=tmp_path / "events.jsonl"))


def _seed(machine) -> Session:
    """A saved conversation above the indexer's three-message floor."""
    session = Session(session_id="s-no-memory")
    session.add_message("user", f"The code word is {SECRET}.")
    session.add_message("assistant", f"Noted: {SECRET}.")
    session.add_message("user", f"Remember {SECRET}.")
    machine.sessions.save(session)
    return session


def _corpus_holds(machine, needle: str) -> bool:
    from axiom.rag.retriever import retrieve
    from axiom.rag.sqlite_store import SQLiteRAGStore

    store = SQLiteRAGStore(machine.corpus_url)
    store.connect()
    try:
        hits = retrieve(store=store, query_text=needle, query_embedding=None, limit=8)
    finally:
        store.close()
    return any(needle in hit.chunk_text for hit in hits)


def _a_turn_then_save(machine, agent) -> None:
    agent.turn("and once more", stream=False)
    machine.sessions.save(agent.session)
    agent.turn("one more turn", stream=False)  # indexes the file as last saved


@pytest.mark.acceptance("2009-A1")
def test_a_no_memory_session_writes_nothing_to_the_internal_corpus_after_a_turn(machine, agent, monkeypatch):
    attached = []
    monkeypatch.setattr(memory_wiring, "attach_memory", lambda a: attached.append(a) or True)
    agent.session = _seed(machine)

    memory_wiring.wire_memory(agent, no_memory=True)
    _a_turn_then_save(machine, agent)

    assert attached == [], "nothing is recalled or recorded"
    assert not _corpus_holds(machine, SECRET), "nothing is indexed after a turn"


@pytest.mark.acceptance("2009-A1")
def test_a_no_memory_session_file_is_not_indexed_by_a_later_bulk_index(machine, agent):
    """The saved file carries the decision, so the watcher and ``rag index`` skip it too."""
    from axiom.rag.personal import ingest_sessions
    from axiom.rag.store import CORPUS_INTERNAL
    from axiom.rag.store_factory import create_store

    agent.session = _seed(machine)
    memory_wiring.wire_memory(agent, no_memory=True)
    _a_turn_then_save(machine, agent)

    store = create_store(machine.corpus_url)
    store.connect()
    indexed, _ = ingest_sessions(machine.sessions_dir, store, corpus=CORPUS_INTERNAL)
    store.close()
    assert indexed == 0
    assert not _corpus_holds(machine, SECRET)


@pytest.mark.acceptance("2009-A2")
def test_without_the_flag_transcript_indexing_is_unchanged(machine, agent, monkeypatch):
    attached = []
    monkeypatch.setattr(memory_wiring, "attach_memory", lambda a: attached.append(a) or True)
    agent.session = _seed(machine)

    memory_wiring.wire_memory(agent, no_memory=False)
    _a_turn_then_save(machine, agent)

    assert attached == [agent]
    assert _corpus_holds(machine, SECRET)


def test_the_chat_entry_point_wires_memory_through_the_flag():
    """The CLI reads ``--no-memory`` into ``wire_memory`` rather than skipping ``attach_memory`` itself."""
    import inspect

    from axiom.extensions.builtins.chat.commands import cli

    src = inspect.getsource(cli)
    assert "wire_memory(agent, no_memory=" in src
    assert "attach_memory(agent)" not in src
