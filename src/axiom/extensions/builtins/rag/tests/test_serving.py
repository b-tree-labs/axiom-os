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

from axiom.rag.grounding import GroundingThreshold
from axiom.rag.provenance import ProvenanceGateConfig
from axiom.rag.retriever import RetrievedChunk


def _chunk(key, title, text, rank):
    return RetrievedChunk(
        citation_key=key, rank=rank, source_path=f"docs/{key}.md",
        source_title=title, chunk_text=text, chunk_index=0, corpus="rag-org",
        similarity=0.9, rrf_score=0.5,
    )


def _client(retrieve_fn, llm_call, embed_fn=None, *,
            grounding_threshold=None, provenance_config=None):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app
    from axiom.extensions.builtins.rag.serving import build_rag_router

    embed_fn = embed_fn or (lambda texts: [[0.1] * 768 for _ in texts])
    app = create_app(title="t", version="0", description="")
    app.include_router(
        build_rag_router(
            retrieve_fn=retrieve_fn, embed_fn=embed_fn, llm_call=llm_call,
            grounding_threshold=grounding_threshold,
            provenance_config=provenance_config,
        )
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


def test_no_retrieval_refuses_deterministically_without_calling_llm():
    """Empty retrieval must REFUSE — the model is never handed an empty
    context to free-generate from. This is the exact fails-open weakness the
    live shim had (it served the model-prior answer with only a soft prompt)."""
    called = {"llm": False}

    def llm_call(messages, stream):
        called["llm"] = True
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "From general knowledge."}, "finish_reason": "stop"}]}

    client = _client(lambda q, e: [], llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model",
        "messages": [{"role": "user", "content": "unrelated question"}],
    })
    assert resp.status_code == 200
    body = resp.json()
    # The LLM was NOT called — the refusal is deterministic, not model-mediated.
    assert called["llm"] is False
    # The answer is the uncertainty notice, not a fabricated one.
    answer = body["choices"][0]["message"]["content"]
    assert "verify before citing" in answer.lower()
    assert "From general knowledge." not in answer
    # Provenance is visible on the wire.
    assert body["x_rag"]["retrieved"] == 0
    assert body["x_rag"]["grounded"] is False


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


# ---------------------------------------------------------------------------
# G4 — deterministic anti-fabrication gates (grounding + provenance)
# ---------------------------------------------------------------------------


def test_weak_grounding_prepends_notice_deterministically():
    """A below-threshold (but non-empty) retrieval still runs the LLM, but the
    uncertainty notice is stamped on the answer deterministically — not left to
    the model to self-flag via the soft prompt."""
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "A tentative answer."}, "finish_reason": "stop"}]}

    # One chunk at similarity 0.9, but the threshold demands 0.95 -> weak.
    strict = GroundingThreshold(min_citations=1, min_top_score=0.95,
                                min_distinct_sources=1)
    client = _client(lambda q, e: [_chunk("C1", "T", "some context", 1)],
                     llm_call, grounding_threshold=strict)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "q"}]})
    body = resp.json()
    answer = body["choices"][0]["message"]["content"]
    assert "verify before citing" in answer.lower()   # notice present
    assert "A tentative answer." in answer             # LLM answer preserved
    assert answer.lower().index("verify") < answer.index("A tentative")  # prepended
    assert body["x_rag"]["grounded"] is False


def test_grounded_answer_passes_through_clean():
    """Strong retrieval + a benign answer: no notice, no withholding, and the
    grounded flag is surfaced True."""
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "Scram occurs on high power [C1]."},
                "finish_reason": "stop"}]}

    client = _client(
        lambda q, e: [_chunk("C1", "Safety", "Scram on high power.", 1),
                      _chunk("C2", "Appendix", "Limits apply.", 2)],
        llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "q"}]})
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "Scram occurs on high power [C1]."
    assert body["x_rag"]["grounded"] is True
    assert "provenance" not in body["x_rag"]  # nothing withheld


def test_fabricated_currency_withheld_by_provenance():
    """A grounded answer that states a currency value absent from the retrieved
    text is withheld (fails closed) and replaced with the abstention."""
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "The total cost is $4,500."}, "finish_reason": "stop"}]}

    # Chunk text has NO such figure -> the $4,500 is unsupported.
    client = _client(
        lambda q, e: [_chunk("C1", "Doc", "Cost figures are in Appendix B.", 1)],
        llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "cost?"}]})
    body = resp.json()
    answer = body["choices"][0]["message"]["content"]
    assert "$4,500" not in answer                       # the fabrication is gone
    assert "tool-verified value" in answer.lower()      # replaced by abstention
    assert body["x_rag"]["provenance"].startswith("withheld:")


def test_supported_currency_passes_provenance():
    """The same currency claim passes when it appears in the retrieved text."""
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "The total cost is $4,500."}, "finish_reason": "stop"}]}

    client = _client(
        lambda q, e: [_chunk("C1", "Doc", "The total cost is $4,500 per unit.", 1)],
        llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "cost?"}]})
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "The total cost is $4,500."
    assert "provenance" not in body["x_rag"]


def test_streaming_empty_retrieval_refuses_without_calling_llm():
    """The refusal holds on the streaming path too: the notice is streamed and
    the LLM is never called."""
    called = {"llm": False}

    def llm_call(messages, stream):
        called["llm"] = True
        return iter(["data: should-not-happen\n\n"])

    client = _client(lambda q, e: [], llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "stream": True,
        "messages": [{"role": "user", "content": "q"}]})
    assert resp.status_code == 200
    assert called["llm"] is False
    assert "verify before citing" in resp.text.lower()
    assert "[DONE]" in resp.text


def test_injected_provenance_config_gates_domain_units():
    """The ``provenance_config`` seam lets a deployment gate domain quantities
    (the default baseline is currency/date only). A config with a power-unit
    pattern withholds a fabricated 'MW' value absent from the retrieved text."""
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "The rated power is 250 MW."}, "finish_reason": "stop"}]}

    cfg = ProvenanceGateConfig().with_patterns(
        quantity_patterns=[r"\b\d+(?:\.\d+)?\s?MW\b"]
    )
    client = _client(
        lambda q, e: [_chunk("C1", "Spec", "The rated power is documented in T1.", 1)],
        llm_call, provenance_config=cfg)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "power?"}]})
    body = resp.json()
    answer = body["choices"][0]["message"]["content"]
    assert "250 MW" not in answer                       # domain fabrication gated
    assert body["x_rag"]["provenance"].startswith("withheld:")


def test_empty_user_message_bypasses_gate():
    """A system-only / empty turn is not a question to ground — it passes
    straight through to the LLM (no spurious refusal)."""
    def llm_call(messages, stream):
        return {"choices": [{"index": 0, "message": {"role": "assistant",
                "content": "Hello!"}, "finish_reason": "stop"}]}

    client = _client(lambda q, e: [], llm_call)
    resp = client.post("/v1/chat/completions", json={
        "model": "rag-model", "messages": [{"role": "user", "content": "   "}]})
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "Hello!"
    assert "grounded" not in body["x_rag"]  # gate did not run
