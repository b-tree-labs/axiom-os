# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/rag shared``: a terminal conversation searches the shared documents only, and is not added to search.

A terminal searched every corpus, including this machine's own, where each conversation is indexed after every turn.
Asking a question twice grounded the second answer in the first: the earlier answer came back as a cited source.
``/rag shared`` gives the terminal what a surface answering other people already has, the shared corpora on the
read side and nothing indexed on the write side.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import agent as agent_module
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands_ext.renew import _restore_settings
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.orchestrator.session import Session

ensure_loaded()


def _run(agent, line):
    out = REGISTRY.dispatch(line, CommandContext(agent=agent, store=None, surface="headless", line=line))
    return re.sub(r"\x1b\[[0-9;]*m", "", str(out))


def _agent():
    a = ChatAgent.__new__(ChatAgent)
    a.session = Session(principal_id="@u:test")
    a._get_rag_store = lambda: SimpleNamespace(does_own_hybrid=True)  # takes the query as text: nothing to embed
    return a


@pytest.fixture
def searched(monkeypatch):
    """The corpora each retrieval asked for."""
    import axiom.rag.retriever as retriever

    calls: list = []
    monkeypatch.setattr(retriever, "retrieve", lambda **kw: (calls.append(kw["corpora"]), [])[1])
    return calls


def test_shared_searches_the_shared_corpora_only(searched):
    a = _agent()
    _run(a, "/rag shared")
    a._rag_context("what is the thermal shield for?")
    assert searched == [["rag-community", "rag-org"]]


def test_on_searches_every_corpus_again(searched):
    a = _agent()
    _run(a, "/rag shared")
    _run(a, "/rag on")
    a._rag_context("what is the thermal shield for?")
    assert searched == [None]


def test_shared_never_widens_a_scope_that_already_names_its_corpora(searched):
    a = _agent()
    scope = ChatScope(session=Session(), retrieval_corpora=["rag-community", "rag-tenant:lab"])
    scope.session.context["rag"] = "shared"
    a._rag_context("what is the thermal shield for?", scope=scope)
    assert searched == [["rag-community", "rag-tenant:lab"]]


def _indexing_started(monkeypatch, rag):
    started: list = []

    class _Thread:
        def __init__(self, target, daemon):
            started.append(target)

        def start(self):
            pass

    monkeypatch.setattr(agent_module.threading, "Thread", _Thread)
    scope = ChatScope(session=Session())
    scope.session.session_id = "31a68def99c7"
    if rag:
        scope.session.context["rag"] = rag
    ChatAgent.__new__(ChatAgent)._schedule_session_index(scope=scope)
    return bool(started)


def test_shared_does_not_index_the_conversation(monkeypatch):
    assert _indexing_started(monkeypatch, None), "a terminal conversation is indexed by default"
    assert not _indexing_started(monkeypatch, "shared")


def test_status_and_resume_say_so():
    a = _agent()
    assert "shared documents only" in _run(a, "/rag shared")
    assert a.session.context["rag"] == "shared"
    assert "on, from the shared documents only" in _run(a, "/rag status")
    assert "retrieval from the shared documents only" in _restore_settings(a, a.session)


def test_the_startup_flag_takes_shared():
    from axiom.extensions.builtins.chat import cli_flags
    from axiom.extensions.builtins.chat.cli import get_parser

    a = _agent()
    cli_flags.apply_startup(get_parser().parse_args(["--rag", "shared"]), a, None)
    assert a.session.context["rag"] == "shared"
