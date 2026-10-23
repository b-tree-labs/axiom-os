# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An answer's cited sources are remembered per answer, so /compact can keep them (ADR-168, C5).

Retrieval keys (C1, C2...) are only meaningful within one retrieve() call and the session stores messages,
not chunks. Without a record made at answer time, compacting a grounded conversation would leave bare
markers that resolve to nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.extensions.builtins.chat import answer_sources
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra.orchestrator.session import Session
from axiom.rag.retriever import RetrievedChunk


def _chunk(key, path, title):
    return RetrievedChunk(
        citation_key=key, rank=1, source_path=path, source_title=title, chunk_text="b",
        chunk_index=0, corpus="rag-org", similarity=0.9, rrf_score=0.5,
    )


def test_only_the_sources_the_answer_actually_cited_are_recorded():
    s = Session()
    s.add_message("user", "q")
    msg = s.add_message("assistant", "It is so [C2].")
    answer_sources.record(s, [_chunk("C1", "/a.md", "A"), _chunk("C2", "/b.md", "B")])
    got = answer_sources.for_message(s, msg)
    assert [(g["key"], g["path"], g["title"]) for g in got] == [("C2", "/b.md", "B")]


def test_an_uncited_or_ungrounded_answer_records_nothing():
    s = Session()
    s.add_message("assistant", "No markers here.")
    answer_sources.record(s, [_chunk("C1", "/a.md", "A")])
    answer_sources.record(s, [])
    assert "answer_sources" not in s.context


def test_the_agent_records_when_it_emits_citations():
    s = Session()
    s.add_message("assistant", "Per [C1].")
    scope = SimpleNamespace(session=s, last_retrieved=[_chunk("C1", "/a.md", "A")])
    ChatAgent._emit_citations(SimpleNamespace(_render=None), scope=scope)
    assert answer_sources.for_message(s, s.messages[0])[0]["path"] == "/a.md"
