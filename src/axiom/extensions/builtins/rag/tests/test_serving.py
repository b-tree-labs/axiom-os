# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /rag serving router (RATIONALIZE-2).

The router is the thin, single-call retrieve->inject->generate path that
replaces the hand-written rag_shim. It composes src/axiom/rag/ primitives
(retrieve = RRF + access filter, build_rag_context_block = budgeted) and is
pure: retrieve_fn / embed_fn / llm_call are injected, so these tests need no
Postgres, embeddings provider, or LLM.
"""

from __future__ import annotations

import pytest

from axiom.rag.retriever import RetrievedChunk


def _chunk(key, title, text, rank):
    return RetrievedChunk(
        citation_key=key, rank=rank, source_path=f"docs/{key}.md",
        source_title=title, chunk_text=text, chunk_index=0, corpus="rag-org",
        similarity=0.9, rrf_score=0.5,
    )


def _client(retrieve_fn, llm_call, embed_fn=None):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app
    from axiom.extensions.builtins.rag.serving import build_rag_router

    embed_fn = embed_fn or (lambda texts: [[0.1] * 768 for _ in texts])
    app = create_app(title="t", version="0", description="")
    app.include_router(
        build_rag_router(retrieve_fn=retrieve_fn, embed_fn=embed_fn, llm_call=llm_call)
    )
    return TestClient(app)


def test_models_lists_rag_model():
    client = _client(lambda q, e: [], lambda messages, stream: {})
    body = client.get("/v1/models").json()
    assert [m["id"] for m in body["data"]] == ["rag-model"]


def test_grounded_completion_injects_context_and_returns_openai_shape():
    seen = {}

    def retrieve_fn(query_text, query_embedding):
        seen["query"] = query_text
        seen["embedding_len"] = len(query_embedding) if query_embedding else 0
        return [_chunk("C1", "TRIGA Safety", "Scram on high power.", 1),
                _chunk("C2", "Appendix A", "LCO limits.", 2)]

    def llm_call(messages, stream):
        seen["system"] = messages[0]["content"]
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "Grounded answer [C1]."}, "finish_reason": "stop"}]}

    client = _client(retrieve_fn, llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model",
        "messages": [{"role": "user", "content": "TRIGA safety requirements?"}],
    })
    assert resp.status_code == 200
    body = resp.json()
    # OpenAI shape
    assert body["choices"][0]["message"]["content"] == "Grounded answer [C1]."
    # retrieval ran with the user query + a 768-dim embedding
    assert seen["query"] == "TRIGA safety requirements?"
    assert seen["embedding_len"] == 768
    # context was injected into the system message with the retrieved text
    assert "Scram on high power." in seen["system"]
    # provenance surfaced without breaking the OpenAI shape
    assert body["x_rag"]["retrieved"] == 2
    assert "TRIGA Safety" in body["x_rag"]["sources"]


def test_no_retrieval_flags_ungrounded():
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "From general knowledge."}, "finish_reason": "stop"}]}

    client = _client(lambda q, e: [], llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model",
        "messages": [{"role": "user", "content": "unrelated question"}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["x_rag"]["retrieved"] == 0


def test_retrieval_failure_surfaces_error_not_silent_ungrounded():
    """A retrieval EXCEPTION must be distinguishable from a genuine no-match.

    The live incident: wedged DB pools made retrieval throw, the shim silently
    degraded to "(No corpus context retrieved.)", and the model answered
    ungrounded with no signal. The composed mount must instead surface the
    failure in x_rag.error (still 200, still degrades — but visibly)."""
    def retrieve_boom(q, e):
        raise RuntimeError("connection pool exhausted")

    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "answer"}, "finish_reason": "stop"}]}

    client = _client(retrieve_boom, llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "q"}]})
    assert resp.status_code == 200  # still degrades gracefully, no 500
    x = resp.json()["x_rag"]
    assert x["retrieved"] == 0
    assert x.get("error")  # the failure is VISIBLE, not silent
    assert "pool" in x["error"].lower()


def test_genuine_no_match_has_no_error():
    """A real empty result (no exception) must NOT carry an error — only true
    failures do, so the two are distinguishable on the wire + in dashboards."""
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "from general knowledge"}, "finish_reason": "stop"}]}

    client = _client(lambda q, e: [], llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "q"}]})
    x = resp.json()["x_rag"]
    assert x["retrieved"] == 0
    assert not x.get("error")


def test_upstream_error_surfaces_502():
    def llm_call(messages, stream):
        raise RuntimeError("vllm down")

    client = _client(lambda q, e: [_chunk("C1", "t", "x", 1)], llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "q"}]})
    assert resp.status_code == 502
