<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# `llm_serving` — agent notes

The serving stack is **vLLM engine → LiteLLM gateway → RAG shim**. `axi serving install`
renders the systemd units + `litellm.config.yaml` from `[defaults]` in
`axiom-extension.toml`. All hosts/ports/models are config — nothing baked in.

## Load-bearing invariant: the gateway must expose an EMBEDDING model, not just a generation model

The LiteLLM gateway is the one front door the RAG uses (`RAG_LITELLM_URL`). RAG does two
things through it: **embed** (documents at ingest, queries at retrieval) and **generate**.
If `litellm.config.yaml`'s `model_list` has only the generation model (e.g. `qwen`) and no
embedding model, then:

- `axi rag add` / ingest fails, and
- retrieval grounding **silently degrades** (the client often falls back to auto-discovering
  some other local server — e.g. a stray `llama-server` on `:8080` — and times out),

with **no obvious error** on the chat path. This is a real incident, not a hypothetical.
`render_litellm_config` now emits both tiers by default; keep it that way.

## GPU/CPU split — deliberate, don't "optimize" it away

- **Generation → GPU.** vLLM reserves the GPU (`vllm_gpu_mem_util`, ~0.90). It needs it.
- **Embeddings → CPU.** `nomic-embed-text` is ~137M and embeds in tens of ms on CPU ollama
  (`ollama_url`, default `:11434`). It does **not** need the GPU.

Do not dial vLLM's GPU reservation down to make room for a GPU embedder, and do not move the
embedder onto the GPU. They coexist precisely because embeddings are cheap on CPU.

## DB-role split — serving reads, ingest writes

- `RAG_DB_URL` — serving retrieval, **read-only** (least-priv) role.
- `RAG_INGEST_DB_URL` — document ingest (`axi rag add`), **write** role scoped to the RAG
  tables. Keep them separate: hardening the serving role must never break ingest
  (`permission denied for schema public` on `CREATE TABLE documents` is the symptom of a
  read-only role being used for ingest), and ingest write must never loosen serving.

## Runbook: "RAG grounding degraded / ingest fails"

Run the built-in first — it now checks the embed path end-to-end:
```
axi serving diagnose        # FAILs if the gateway is missing the embed model or ollama lacks it
```
Then confirm by hand (substitute your ports/models from `[defaults]`):
```
# 1. Does the gateway expose an EMBED model, not just generation?
curl -sk https://localhost:<gateway_port>/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | python3 -c 'import sys,json;print([m["id"] for m in json.load(sys.stdin)["data"]])'
#    -> must include the embed model (e.g. nomic-embed-text). If not, that's the bug.

# 2. Does CPU ollama actually serve the embed model?
curl -s http://localhost:11434/api/tags | python3 -c 'import sys,json;print([m["name"] for m in json.load(sys.stdin)["models"]])'
curl -s http://localhost:11434/api/embeddings -d '{"model":"nomic-embed-text","prompt":"x"}' \
  | python3 -c 'import sys,json;print("dims",len(json.load(sys.stdin)["embedding"]))'   # -> 768

# 3. Is the client auto-discovering a STRAY gen server (e.g. bonsai llama-server on :8080)?
ss -tlnp | grep :8080     # a rogue llama-server here is what the RAG CLI wrongly grabs
```
Fix = register the embed model on the gateway (add it to `model_list` → `ollama/<embed> @ ollama_url`,
reload `axiom-litellm`), **not** giving the embedder a GPU and **not** touching vLLM's reservation.
The installer bakes this in; if you're seeing it live, the node's config drifted from the template.
