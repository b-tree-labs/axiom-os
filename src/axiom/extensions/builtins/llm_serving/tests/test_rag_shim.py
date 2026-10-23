# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The org RAG endpoint now serves the composed /rag router (SERVE-CONVERGE).

These prove the convergence: build_shim_app assembles build_rag_router (+ /health)
from injected seams, so the endpoint inherits the hardened path — RRF/rerank/access
via retrieve_fn and the deterministic answer-gate — instead of the old hand-written
single-probe path. Pure in its seams: no Postgres, ollama, or LLM.
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

    from axiom.extensions.builtins.llm_serving.rag_shim import build_shim_app

    embed_fn = embed_fn or (lambda texts: [[0.1] * 768 for _ in texts])
    return TestClient(
        build_shim_app(retrieve_fn=retrieve_fn, embed_fn=embed_fn, llm_call=llm_call)
    )


def test_health_reports_the_model():
    client = _client(lambda q, e: [], lambda m, s: {})
    body = client.get("/health").json()
    assert body == {"status": "ok", "model": "rag-model"}


def test_serves_openai_models_list():
    client = _client(lambda q, e: [], lambda m, s: {})
    assert "rag-model" in [m["id"] for m in client.get("/v1/models").json()["data"]]


def test_empty_retrieval_refuses_via_the_hardened_gate():
    """The convergence payoff: the endpoint inherits build_rag_router's grounding
    gate, so empty retrieval REFUSES without calling the LLM — where the old
    hand-written shim free-generated from the model prior."""
    called = {"llm": False}

    def llm_call(messages, stream):
        called["llm"] = True
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "from the model prior"}, "finish_reason": "stop"}]}

    client = _client(lambda q, e: [], llm_call)
    resp = client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": "q"}]})
    assert resp.status_code == 200
    body = resp.json()
    assert called["llm"] is False
    assert "verify before citing" in body["choices"][0]["message"]["content"].lower()
    assert body["x_rag"]["grounded"] is False


def test_grounded_completion_flows_through_the_router():
    def retrieve_fn(query_text, query_embedding):
        return [_chunk("C1", "Doc", "grounded context here", 1)]

    def llm_call(messages, stream):
        # Context block was injected into the system message.
        assert "grounded context here" in messages[0]["content"]
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "Grounded answer [C1]."}, "finish_reason": "stop"}]}

    client = _client(retrieve_fn, llm_call)
    body = client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": "q"}]}).json()
    assert body["choices"][0]["message"]["content"] == "Grounded answer [C1]."
    assert body["x_rag"]["retrieved"] == 1
    assert body["x_rag"]["grounded"] is True
