# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/sources renders what grounded the last reply and the provenance verdict nothing used to show."""

from __future__ import annotations

from axiom.extensions.builtins.chat.tests._transcript_support import make_agent, run
from axiom.rag.answer_gate import check_answer_provenance
from axiom.rag.retriever import RetrievedChunk


def _chunk(key, path, corpus="docs", title="A title"):
    return RetrievedChunk(
        citation_key=key, rank=1, source_path=path, source_title=title, chunk_text="body",
        chunk_index=0, corpus=corpus, similarity=0.9, rrf_score=0.5,
    )


def test_no_retrieval_and_no_citation_says_so(monkeypatch):
    agent = make_agent(
        monkeypatch, [("hi", "hello")], provenance=check_answer_provenance("hello", [])
    )
    out = run(agent, "/sources")
    assert "no retrieval in this turn" in out.lower()


def test_grounded_answer_lists_each_source_with_corpus_and_store(monkeypatch):
    chunks = [_chunk("C1", "docs/a.md", "manuals"), _chunk("C2", "docs/b.md", "notes", "B")]
    prov = check_answer_provenance("see [C1] and [C2]", chunks)
    out = run(make_agent(monkeypatch, [("q", "see [C1] and [C2]")], chunks, prov), "/sources")
    assert "[C1]" in out and "docs/a.md" in out and "manuals" in out
    assert "[C2]" in out and "notes" in out
    assert "local" in out.lower()  # where the store lives
    assert "grounded" in out.lower() and "every citation" in out.lower()


def test_an_invented_citation_is_called_out_in_plain_words(monkeypatch):
    chunks = [_chunk("C1", "docs/a.md")]
    prov = check_answer_provenance("see [C1] and [C7]", chunks)
    out = run(make_agent(monkeypatch, [("q", "x")], chunks, prov), "/sources")
    assert "ungrounded" in out.lower() and "C7" in out
    assert "not among" in out


def test_invented_citation_with_nothing_retrieved_is_still_reported(monkeypatch):
    prov = check_answer_provenance("see [C1]", [])
    out = run(make_agent(monkeypatch, [("q", "see [C1]")], [], prov), "/sources")
    assert "ungrounded" in out.lower() and "nothing was retrieved" in out
    assert "no retrieval in this turn" in out.lower()


def test_retrieved_but_uncited_is_reported_as_uncited_not_grounded(monkeypatch):
    chunks = [_chunk("C1", "docs/a.md")]
    prov = check_answer_provenance("plain answer", chunks)
    out = run(make_agent(monkeypatch, [("q", "plain answer")], chunks, prov), "/sources")
    assert "uncited" in out.lower() and "grounded" not in out.lower().replace("ungrounded", "")


def test_an_unchecked_turn_is_unknown_not_grounded(monkeypatch):
    chunks = [_chunk("C1", "docs/a.md")]
    out = run(make_agent(monkeypatch, [("q", "a")], chunks, None), "/sources")
    assert "not checked" in out.lower()
    assert "GROUNDED" not in out


def test_remote_store_is_named_with_its_host(monkeypatch):
    from axiom.extensions.builtins.chat import where

    chunks = [_chunk("C1", "docs/a.md")]
    agent = make_agent(monkeypatch, [("q", "[C1]")], chunks, check_answer_provenance("[C1]", chunks))
    monkeypatch.setattr(
        where, "_retrieval_where", lambda: where.RetrievalWhere("remote-node", "node.example.edu", "served")
    )
    out = run(agent, "/sources")
    assert "node.example.edu" in out
