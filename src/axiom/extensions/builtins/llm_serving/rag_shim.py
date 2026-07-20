#!/usr/bin/env python3
"""RPE-RAG completion shim (#45) — the org RAG endpoint on :8766.

Pivot architecture: this is the thin, single-call retrieve->inject->generate path
that REPLACES the homegrown chat_server agent.turn tool-loop (the fragility the
2026-06-29 incident exposed). It does NOT run an agent loop.

Flow:  query -> embed (ollama nomic-embed-text, 768) -> pgvector top-k (chunks)
       -> inject grounded context -> call LiteLLM (:41883, qwen, no-think) -> return.

OpenAI-compatible (`/v1/chat/completions`, `/v1/models`), exposed as model
"rag-model" so existing IDE/client configs keep working. Concurrency comes from
vLLM continuous batching upstream; this shim is fully async (httpx + psycopg pool).

All hosts/keys come from env (domain-agnostic; seed for AEOS serve-extension #48).
"""
import os
import json
import contextlib
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg_pool import AsyncConnectionPool

DB_URL       = os.environ["RAG_DB_URL"]                       # postgresql://...
OLLAMA       = os.environ.get("RAG_OLLAMA", "http://localhost:11434")
EMBED_MODEL  = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")
LITELLM_URL  = os.environ.get("RAG_LITELLM_URL", "https://localhost:41883")
LITELLM_KEY  = os.environ.get("RAG_LITELLM_KEY", "")  # no baked default: deployments MUST set this
GEN_MODEL    = os.environ.get("RAG_GEN_MODEL", "qwen")
TOP_K        = int(os.environ.get("RAG_TOP_K", "6"))
CHUNK_CAP    = int(os.environ.get("RAG_CHUNK_CHARS", "1200"))  # per-chunk char cap (#16)
MIN_SCORE    = float(os.environ.get("RAG_MIN_SCORE", "0.30"))  # cosine-sim floor

app = FastAPI(title="rag-shim")
pool = AsyncConnectionPool(DB_URL, min_size=2, max_size=12, open=False)
client = httpx.AsyncClient(timeout=180.0, verify=False)

GROUNDING = (
    "You are the organization's RAG assistant. Answer using ONLY the context "
    "below when it is relevant; cite source titles inline. If the context does "
    "not contain the answer, say so plainly and answer from general knowledge "
    "with an explicit 'not grounded in the corpus — verify before citing' notice."
)

@app.on_event("startup")
async def _startup():
    await pool.open()

@app.get("/health")
@app.get("/health/liveliness")
async def health():
    return {"status": "ok", "model": "rag-model"}

@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "rag-model", "object": "model", "owned_by": "axiom"}]}

async def embed(text: str):
    r = await client.post(f"{OLLAMA}/api/embeddings", json={"model": EMBED_MODEL, "prompt": text})
    r.raise_for_status()
    return r.json()["embedding"]

async def retrieve(qvec):
    vec = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
    sql = (
        "SELECT chunk_text, source_title, corpus, 1-(embedding <=> %s::vector) AS score "
        "FROM chunks WHERE embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector LIMIT %s"
    )
    async with pool.connection() as conn:
        cur = await conn.execute(sql, (vec, vec, TOP_K))
        rows = await cur.fetchall()
    hits = [{"text": t, "title": title, "corpus": c, "score": float(s)}
            for (t, title, c, s) in rows if s is not None and float(s) >= MIN_SCORE]
    return hits

def build_context(hits):
    blocks = []
    for h in hits:
        blocks.append(f"[{h['title'] or h['corpus'] or 'source'}] {h['text'][:CHUNK_CAP]}")
    return "\n\n".join(blocks)

@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages", [])
    user_q = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
    if not isinstance(user_q, str):
        user_q = json.dumps(user_q)

    hits, ctx = [], ""
    with contextlib.suppress(Exception):
        if user_q.strip():
            hits = await retrieve(await embed(user_q))
            ctx = build_context(hits)

    sys_content = GROUNDING + ("\n\n=== CONTEXT ===\n" + ctx if ctx else "\n\n(No corpus context retrieved.)")
    out_msgs = [{"role": "system", "content": sys_content}] + [m for m in msgs if m.get("role") != "system"]

    payload = {
        "model": GEN_MODEL,
        "messages": out_msgs,
        "max_tokens": body.get("max_tokens", 1024),
        "temperature": body.get("temperature", 0.2),
        # no-think default (#40/#47): fast interactive path
        "chat_template_kwargs": {"enable_thinking": False},
    }

    # Streaming passthrough (IDEs default to stream:true) — forward LiteLLM SSE.
    if body.get("stream"):
        payload["stream"] = True
        async def gen():
            async with client.stream(
                "POST", f"{LITELLM_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"}, json=payload,
            ) as up:
                async for line in up.aiter_lines():
                    if line:
                        yield line + "\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    r = await client.post(
        f"{LITELLM_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {LITELLM_KEY}"},
        json=payload,
    )
    if r.status_code != 200:
        return JSONResponse(status_code=502, content={"error": {"message": f"upstream {r.status_code}: {r.text[:300]}"}})
    resp = r.json()
    resp.setdefault("model", "rag-model")
    # surface grounding provenance without breaking OpenAI shape
    resp["x_rag"] = {"retrieved": len(hits), "sources": [h["title"] for h in hits][:TOP_K]}
    return resp
