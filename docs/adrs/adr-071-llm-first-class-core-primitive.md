# ADR-071 — `llm` as a first-class core primitive (gateway + tiers + pluggable providers)

**Status:** Accepted · **Date:** 2026-06-09
**Owner:** @ben
**Related:** ADR-054 (LLM-tier policy — consolidated here), ADR-070 (dependency direction), spec-model-routing, spec-llm-tier-policy, epic axiom-os#506

## Context

LLM access in Axiom is **scattered and has no coherent home**, despite ADR-054 declaring `LLMTierPolicy` "a first-class Axiom primitive." Today it lives across:

- `infra/gateway.py` — provider routing / model selection / `active_provider`
- `infra/router.py`, `infra/llm_params.py` — routing + sampling params
- `setup/llamafile.py` — local model profiles + provisioning (the gemma2:2b default)
- the `axiom-llm-server` deploy image — a serving backend
- ADR-054 `LLMTierPolicy` — tier semantics (simple/standard/smart)

It is **deeply cross-cutting**: ~11 builtin extensions consume the gateway (chat, classroom, diagnostics, federation, hygiene, mcp, release, review, signals, status, http). Notably `rag` is a *consumer* of generation, not the owner — so folding LLM serving into the `rag` extension would invert the dependency graph (everything would depend on `rag` to call a model). And there is **no health/coherence seam**: a degenerate served model (`bonsai-1.7b` emitting garbage) drifted undetected on a deployment for 68 days.

Two false framings to reject: (a) a new `llm` *builtin extension* — wrong, because foundational services consumed by 11 extensions belong in core, not in a consumer-layer extension others must depend on; (b) folding into `rag` — wrong for the same dependency reason.

## Decision

**Establish `llm` as a first-class *core* primitive (`src/axiom/llm/`)** — peer to `memory/`, `identity/`, `federation/` — consolidating the scattered pieces. **Not** a builtin extension; **not** part of `rag`.

Naming follows ecosystem convention: the **namespace is `llm`** (cf. LangChain `language_models`/`llms`, LlamaIndex `llms`, DSPy `LM`); the **router component is the `gateway`** (cf. LiteLLM / Portkey / Cloudflare "LLM/AI gateway"). So "LLM gateway" names the *router*, not the top-level home.

```
src/axiom/llm/
  gateway.py     # the seam consumers call: tier + intent routing, fallback, provider dispatch
  tiers.py       # ADR-054 LLMTierPolicy (simple / standard / smart)
  providers/     # pluggable serving backends: llamafile, ollama, axiom-llm-server, cloud, federated peer
  params.py      # sampling / generation params
  provisioning.py# model profiles + defaults (e.g. gemma2:2b small default)
  health.py      # coherence / readiness gate (catch degenerate models)
```

- **`rag` and the other ~11 extensions compose `axiom.llm`** through the gateway; they never own serving.
- **Serving backends are providers** behind the gateway — swapping a model (e.g. bonsai → qwen) is a provider/provisioning concern, not a consumer change.
- **Dependency direction is one-way:** extensions → core `llm` (per ADR-070). Never core/everything → `rag`.

## Consequences

- A coherent home for the serving/health/tuning/observability work — epic #506's #491 (real model), #492 (tuning), #494 (lifecycle), #499 (coherence gate) land in `llm/`, not scattered.
- **Migration:** relocate `infra/gateway` → `llm/gateway`, `infra/router`/`llm_params` → `llm/`, `setup/llamafile` provisioning → `llm/provisioning`, with back-compat shims so the ~11 consumers keep importing during transition.
- Fulfills ADR-054's "first-class primitive" intent (and consolidates its tier policy into `llm/tiers`).
- `rag` is clarified as the retrieval + augmented-generation pipeline that *composes* the gateway — and this session's misplaced `corpus_eval` (under `data_platform`) relocates to `rag`, keeping the three boundaries clean: **`data_platform` = ingest/medallion · `rag` = retrieval/RAG-pipeline · `llm` = serving/routing/tiers.**
- Health-gate seam means a future degenerate model is caught in minutes, not 68 days.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
