# PLINTH — initial agent skills + capabilities

**Status:** Design (2026-05-28) · **Agent:** PLINTH (data-platform orchestrator)

PLINTH conducts the data platform; it does **not** re-implement ingestion,
storage, or scheduling. Per [ADR-049](../../../../docs/adrs/adr-049-data-platform-orchestration-boundary.md) as
amended by [ADR-138](../../../../docs/adrs/adr-138-the-data-platform-has-no-heavy-orchestrator.md),
Scheduled runs own materialization — PLINTH **triggers and monitors** them and
supplies the agent judgment + safety around them; it is not itself the
scheduler. (The [layering model](../../../../docs/specs/spec-data-architecture.md):
medallion = substrate of record, RAG = served view; Sense is the agent-signal
subsystem, orthogonal to the data platform.) Every external mutation routes
through `guarded_act` (RACI graduation-safety).

## Initial skill set

| Skill | What it does | Reuses |
|---|---|---|
| **`register-connector`** | Register a portable `IngestSource` connector + wire it to a scheduled runner | `IngestSource` contract; the scheduled runner; `publishing` Box auth |
| **`run-ingest`** (gated) | Trigger + monitor an ingest run; enforce the **provenance gate** + `guarded_act` on the external write; land **bronze** → dbt silver/gold → RAG **served view** | scheduled runs, dbt; `rag.ingest_router` (gate); `RAGStore` |
| **`pack-flow`** | Generate + distribute corpus packs: delta / scoped / quantized; maintain the hot-chunk cache | `corpus_generation`, the pack-at-scale model (spec "Pack distribution") |
| **`corpus-health`** | Watch corpus freshness, lakehouse-table health, embedding/retrieval drift; surface, don't auto-fix | store metrics; RIVET/heartbeat patterns |
| **`site-push`** | Push curated content from an **acquisition** site outward to **processing/served** sites (generic site→site) | federation transport; the deployment-topology model |

## Safety — `guarded_act`

The `guarded_act` seam (marked in `agent.py`) wraps every external mutation —
bronze writes, pack publishes, cross-site pushes — in the RACI v2 D6 model
([ADR-045]): **reversibility gate** (refuse irreversible autonomous ops),
**volume/rate circuit-breaker** (over-limit batch downgrades to a prompt),
**novelty confirmation**. PLINTH acts autonomously within those bounds and
notifies; it escalates outside them.

## Phasing

- **Exercise #1 (now):** `register-connector` (Box) + `run-ingest` + `guarded_act`
  — the minimum to run a real Box→RAG pipeline through the gate into
  the full medallion + served RAG.
- **Next:** `pack-flow` (the pack-at-scale model) + `corpus-health`.
- **Multi-site:** `site-push` once a second site (acquisition → processing)
  exists.

## Open

- `IngestSource` ↔ Sense is resolved by ADR-049 (portable connector consumed by
  a scheduled runner; Sense stays in the agent-signal lane).
- PLINTH's AEOS skill manifest entries (one `kind = "skill"` per skill above).
