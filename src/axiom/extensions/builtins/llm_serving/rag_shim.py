#!/usr/bin/env python3
"""Org RAG completion endpoint — now a thin adapter over the composed ``/rag``.

Converges onto the hardened path (SERVE-CONVERGE): this used to be a hand-written
``embed -> single-probe pgvector ORDER BY -> inject -> LiteLLM`` path that bypassed
the retriever entirely, so it got none of the platform's retrieval intelligence.
It now serves :func:`axiom.extensions.builtins.rag.serving.build_rag_router` — the
SAME single-call ``retrieve -> inject -> generate`` shape (no agent loop, so the
latency win is kept), but through ``retriever.retrieve``: RRF fusion, the heuristic
reranker, per-chunk access enforcement, the budgeted context block, citation keys,
and the deterministic grounding/provenance answer-gate.

Deployment is unchanged: ``uvicorn rag_shim:app`` on the org port. OpenAI-compatible
(``/v1/chat/completions``, ``/v1/models``, model ``rag-model``), plus ``/health``.

Concurrency: the router's handlers are sync, so FastAPI runs them in its threadpool.
Each thread gets its OWN ``RAGStore`` (one psycopg2 connection per thread, reused) —
concurrency-safe without the old async pool, and no per-request connect churn.

Config (env, unchanged contract):
  RAG_DB_URL           postgresql://...  (the org corpus)
  RAG_OLLAMA           embedding host (default http://localhost:11434)
  RAG_EMBED_MODEL      embedding model (default nomic-embed-text — MUST match the index)
  RAG_LITELLM_URL/KEY  generation gateway (no baked key: deployments MUST set it)
  RAG_GEN_MODEL        generation model (default qwen)
  RAG_TOP_K            retrieved chunks (default 6)
  RAG_MAX_TOKENS       generation cap (default 1024)
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from typing import Any

import httpx

from axiom.extensions.builtins.rag.serving import MODEL_NAME, build_rag_router

# One store (one connection) per FastAPI threadpool thread. psycopg2 connections
# are not safe to share across concurrent threads; thread-local keeps each request
# on its own connection while reusing it across requests that land on that thread.
_tls = threading.local()


def _store() -> Any:
    store = getattr(_tls, "store", None)
    if store is None:
        from axiom.rag.store import RAGStore

        # ensure_schema=False: a serving endpoint must not run DDL, and lazy
        # connect means the first request on each thread opens its connection.
        store = RAGStore(os.environ["RAG_DB_URL"], ensure_schema=False)
        _tls.store = store
    return store


def _embed_fn(texts: list[str]) -> list[list[float]] | None:
    """Embed via the same ollama endpoint/model the index was built with, so the
    query vector is comparable to the stored ones. Best-effort: build_rag_router
    degrades to text-only retrieval if this raises."""
    ollama = os.environ.get("RAG_OLLAMA", "http://localhost:11434")
    model = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")
    out: list[list[float]] = []
    with httpx.Client(timeout=60.0) as client:
        for text in texts:
            resp = client.post(
                f"{ollama}/api/embeddings", json={"model": model, "prompt": text}
            )
            resp.raise_for_status()
            out.append(resp.json()["embedding"])
    return out


def _retrieve_fn(query_text: str, query_embedding: list[float] | None) -> Any:
    """The hardened retrieval path — RRF + rerank + access filter + citation keys —
    where before this was a single-probe ``ORDER BY``. A fail-closed access context
    is passed (loopback endpoint → no principal → the safe baseline)."""
    from axiom.rag.retriever import access_context_for_principal, retrieve

    top_k = int(os.environ.get("RAG_TOP_K", "6"))
    return retrieve(
        _store(),
        query_text=query_text,
        query_embedding=query_embedding,
        limit=top_k,
        access_context=access_context_for_principal(None),
    )


def _llm_call(messages: list[dict], stream: bool) -> Any:
    """Call the LiteLLM gateway (no-think). Non-stream returns the OpenAI dict
    (build_rag_router raises 502 on an upstream error); stream returns an iterator
    of SSE lines that the router forwards."""
    url = os.environ.get("RAG_LITELLM_URL", "https://localhost:41883")
    key = os.environ.get("RAG_LITELLM_KEY", "")
    model = os.environ.get("RAG_GEN_MODEL", "qwen")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(os.environ.get("RAG_MAX_TOKENS", "1024")),
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if stream:
        payload["stream"] = True

        def gen() -> Iterator[str]:
            with httpx.Client(timeout=180.0, verify=False) as client:  # noqa: S501
                with client.stream(
                    "POST", f"{url}/v1/chat/completions", headers=headers, json=payload
                ) as up:
                    for line in up.iter_lines():
                        if line:
                            yield line + "\n"

        return gen()

    with httpx.Client(timeout=180.0, verify=False) as client:  # noqa: S501
        resp = client.post(
            f"{url}/v1/chat/completions", headers=headers, json=payload
        )
    resp.raise_for_status()
    return resp.json()


def build_shim_app(*, retrieve_fn: Any, embed_fn: Any, llm_call: Any) -> Any:
    """Assemble the org endpoint: the composed ``/rag`` router plus ``/health``.

    Pure in its seams so it is testable with fakes (no Postgres / ollama / LLM)."""
    from fastapi import FastAPI

    app = FastAPI(title="rag-shim")

    @app.get("/health")
    @app.get("/health/liveliness")
    def health() -> dict:
        return {"status": "ok", "model": MODEL_NAME}

    app.include_router(
        build_rag_router(retrieve_fn=retrieve_fn, embed_fn=embed_fn, llm_call=llm_call)
    )
    return app


# The deployed ASGI app (``uvicorn rag_shim:app``). Seams are module functions that
# read env at call time, so importing this module needs no environment.
app = build_shim_app(retrieve_fn=_retrieve_fn, embed_fn=_embed_fn, llm_call=_llm_call)
