# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The ``/rag`` serving mount — retrieval-grounded completion (RATIONALIZE-2).

The thin, single-call ``retrieve -> inject -> generate`` path that replaces the
hand-written ``rag_shim``. It does NOT run an agent tool-loop. All retrieval
intelligence is delegated to ``src/axiom/rag/`` — crucially ``retriever.retrieve``
(RRF + ``AccessContext`` filtering + probe-tuned recall), not a hand-written
single-probe ``ORDER BY``. OpenAI-compatible, exposed as model ``rag-model`` so
existing IDE/client configs keep working.

``build_rag_router`` is pure: ``retrieve_fn`` / ``embed_fn`` / ``llm_call`` are
injected so it is testable without Postgres, an embeddings provider, or an LLM.
``rag_mount_spec`` wires the real store + retriever + gateway and returns the
:class:`MountSpec` the composed app discovers.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from axiom.rag.context_block import build_rag_context_block
from axiom.rag.retriever import RetrievedChunk

_LOG = logging.getLogger("axiom.rag.serving")

MODEL_NAME = "rag-model"

_GROUNDING = (
    "You are the organization's RAG assistant. Answer using ONLY the context "
    "below when it is relevant; cite source titles inline. If the context does "
    "not contain the answer, say so plainly and answer from general knowledge "
    "with an explicit 'not grounded in the corpus — verify before citing' notice."
)

# Type aliases for the injected seams.
RetrieveFn = Callable[[str, list[float] | None], Sequence[RetrievedChunk]]
EmbedFn = Callable[[list[str]], list[list[float]] | None]
LLMCall = Callable[[list[dict], bool], Any]


def _system_prompt(chunks: Sequence[RetrievedChunk]) -> str:
    if not chunks:
        return _GROUNDING + "\n\n(No corpus context retrieved.)"
    return _GROUNDING + "\n\n=== CONTEXT ===\n" + build_rag_context_block(chunks)


def build_rag_router(
    *,
    retrieve_fn: RetrieveFn,
    embed_fn: EmbedFn,
    llm_call: LLMCall,
    model_name: str = MODEL_NAME,
) -> APIRouter:
    """Build the OpenAI-compatible ``/rag`` router from injected seams."""
    router = APIRouter()

    @router.get("/v1/models")
    def models() -> dict:
        return {"object": "list",
                "data": [{"id": model_name, "object": "model", "owned_by": "axiom"}]}

    @router.post("/v1/chat/completions")
    def chat(body: dict) -> Any:
        messages = body.get("messages", [])
        user_q = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        if not isinstance(user_q, str):
            import json
            user_q = json.dumps(user_q)

        chunks: list[RetrievedChunk] = []
        retrieval_error: str | None = None
        if user_q.strip():
            embedding = None
            try:
                vecs = embed_fn([user_q])
                if vecs:
                    embedding = vecs[0]
            except Exception as exc:  # noqa: BLE001 — embedding best-effort; falls back to text-only
                # A failed embedder still lets text retrieval try, but the
                # degradation must be VISIBLE, not silent (live incident: a
                # wedged dependency looked identical to a genuine no-match).
                retrieval_error = f"embedding failed: {type(exc).__name__}: {exc}"
                _LOG.warning("rag embedding failed: %s", exc)
            try:
                chunks = list(retrieve_fn(user_q, embedding))
            except Exception as exc:  # noqa: BLE001 — degrade to ungrounded, never 500
                # Surface the failure so a wedged DB/pool is distinguishable
                # from "the corpus has no answer". Both still degrade to an
                # ungrounded answer (no 500), but this one is flagged.
                retrieval_error = f"retrieval failed: {type(exc).__name__}: {exc}"
                _LOG.error("rag retrieval failed — serving UNGROUNDED: %s", exc)
                chunks = []

        out_messages = [{"role": "system", "content": _system_prompt(chunks)}]
        out_messages += [m for m in messages if m.get("role") != "system"]

        provenance: dict[str, Any] = {"retrieved": len(chunks),
                      "sources": [c.source_title for c in chunks]}
        if retrieval_error:
            # Visible on the wire (x_rag.error) so clients/dashboards/evals can
            # tell "retrieval broke" from "no corpus match" — the exact signal
            # missing when the live shim silently served ungrounded answers.
            provenance["error"] = retrieval_error

        if body.get("stream"):
            def gen():
                try:
                    for line in llm_call(out_messages, True):
                        yield line
                except Exception as exc:  # noqa: BLE001
                    import json
                    yield f"data: {json.dumps({'error': {'message': str(exc)[:300]}})}\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        try:
            resp = llm_call(out_messages, False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f"upstream LLM error: {exc}") from exc
        if not isinstance(resp, dict):
            raise HTTPException(status_code=502, detail="upstream returned no JSON")
        resp.setdefault("model", model_name)
        resp["x_rag"] = provenance
        return resp

    return router


def rag_mount_spec():
    """Wire the real store + retriever + gateway and return the ``/rag`` MountSpec.

    Discovered by the composed app (AEOS ``service`` block). Config via env:
    ``AXIOM_RAG_DSN`` (store), ``AXIOM_RAG_GATEWAY_URL``/``AXIOM_RAG_GATEWAY_KEY``
    (LiteLLM gateway — replaced by infra.gateway routing in RATIONALIZE-4),
    ``AXIOM_RAG_GEN_MODEL``, ``AXIOM_RAG_TOP_K``, ``AXIOM_RAG_CORPORA``.
    """
    from axiom.extensions.builtins.http.registry import MountSpec
    from axiom.rag.embeddings import embed_texts
    from axiom.rag.retriever import AccessContext, retrieve
    from axiom.rag.store_factory import create_store

    dsn = os.environ.get("AXIOM_RAG_DSN") or os.environ.get("DATABASE_URL", "")
    store = create_store(dsn)
    top_k = int(os.environ.get("AXIOM_RAG_TOP_K", "8"))
    corpora_env = os.environ.get("AXIOM_RAG_CORPORA", "")
    corpora = [c.strip() for c in corpora_env.split(",") if c.strip()] or None
    access = AccessContext()  # T0-1 default; full policy engine wraps this later

    def retrieve_fn(query_text, query_embedding):
        return retrieve(store, query_text, query_embedding,
                        corpora=corpora, limit=top_k, access_context=access)

    def llm_call(messages, stream):
        return _gateway_call(messages, stream)

    return MountSpec(
        prefix="/rag",
        router=build_rag_router(retrieve_fn=retrieve_fn, embed_fn=embed_texts,
                                llm_call=llm_call),
        extension="rag",
        bind="127.0.0.1",
        trust_zone="loopback",
    )


def _gateway_call(messages: list[dict], stream: bool) -> Any:
    """Call the LiteLLM gateway (no-think). Replaced by infra.gateway in #57."""
    import httpx

    url = os.environ.get("AXIOM_RAG_GATEWAY_URL", "https://localhost:41883")
    key = os.environ.get("AXIOM_RAG_GATEWAY_KEY", "")
    model = os.environ.get("AXIOM_RAG_GEN_MODEL", "qwen")
    payload = {
        "model": model, "messages": messages,
        "max_tokens": int(os.environ.get("AXIOM_RAG_MAX_TOKENS", "1024")),
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if stream:
        payload["stream"] = True

        def _iter():
            with httpx.Client(timeout=180.0, verify=False) as client:  # noqa: S501
                with client.stream("POST", f"{url}/v1/chat/completions",
                                   headers=headers, json=payload) as up:
                    for line in up.iter_lines():
                        if line:
                            yield line + "\n"
        return _iter()

    with httpx.Client(timeout=180.0, verify=False) as client:  # noqa: S501
        r = client.post(f"{url}/v1/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


__all__ = ["MODEL_NAME", "build_rag_router", "rag_mount_spec"]
