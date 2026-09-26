# Spec: Chat Prompt Pipeline — the trustworthy-answer turn contract

**Status:** Draft (2026-09-18) · **Decision:** ADR-113 (deterministic answering), ADR-115 (capability-scoped data answering) · **Related:** [prd-deterministic-quantitative-answering.md](../prds/prd-deterministic-quantitative-answering.md), [spec-output-provenance-gate.md](spec-output-provenance-gate.md), [spec-analytics-tool.md](spec-analytics-tool.md), [spec-medallion-answering.md](spec-medallion-answering.md), [spec-prompt-registry.md](spec-prompt-registry.md) (templates), ADR-054 (model tiers), ADR-072/073 (capability projection), ADR-114 (authority gates).

## 1. Purpose & scope

Defines the **turn pipeline** every Axiom chat surface runs — the ordered stages between a user
message and a delivered answer, with each stage's contract and failure posture. This is the
productized form of what today exists as a consumer-layer serving implementation; the ONE-chat
extraction implements this spec, and any surface (CLI, HTTP, MCP-fronted client) MUST traverse
the same pipeline so one identity, one policy, and one answer quality hold everywhere.

Out of scope: transport details (spec-serve), template file format (spec-prompt-registry),
retrieval internals (spec-rag-architecture).

## 2. The stages (ordered; each names its failure posture)

| # | Stage | Contract | On failure |
|---|---|---|---|
| 1 | **Command expansion** | A bare `/command` (or unfilled palette template) expands to real intent BEFORE retrieval and the cache key; the rewritten turn is what the model sees | pass through unexpanded |
| 2 | **Persona resolution** | Client system message wins; else persona model-suffix; else the caller's role default. Resolved BEFORE the cache key so personas never share cached answers | default persona |
| 3 | **Identity + access context** | Resolve principal (`@name:context`); derive access tiers; authz decision logged (enforcement per deployment flag; ADR-114 gates tool effects regardless) | unidentified → public tier only |
| 4 | **Cache key** | `hash(data-epoch + persona + principal + non-system history)`; degraded/suppressed answers are NEVER cached | cache miss |
| 5 | **Query rewrite** | Standalone search query via the fast tier (ADR-054 SMALL_MODEL); bounded tokens | raw message; retrieval never breaks |
| 6 | **Retrieval** | Hybrid (dense + BM25 → RRF → rerank) with the stage-3 access tiers enforced fail-closed | empty hit set |
| 7 | **Tool loop** | Tools = the **discovered** capability set (ADR-072/073 projection: generic medallion verbs, declared quantities, bespoke pack verbs, the analytics tool). Multi-round; every dispatch rides `invoke_capability` (ADR-114 identity + effect gates). **Hardcoded dispatch tables are non-conformant** | tool error → typed error result to the model; never fabricated data |
| 8 | **Determinism directive** | The system prompt routes aggregate/fit/rate computation to deterministic tools (analytics / medallion verbs); the model narrates, tools compute | — |
| 9 | **Output-provenance gate** | Fail-closed: a specific value not grounded in this turn's evidence (retrieved docs, tool results, prior turns, published constants — unit-scale-aware) is suppressed and replaced by the configured abstention; suppression is traced with the pre-gate answer | suppression IS the failure handling |
| 10 | **Stamp + persist + trace** | Data-epoch stamp on the answer; memory append; cache write (only clean turns); append-only turn trace incl. gate decisions | best-effort; never blocks the answer |

## 3. Capability-scoped behavior (the three install modes)

The pipeline is identical in all modes; only stage 7's discovered set differs
(prd-deterministic-quantitative-answering §7): chat-core → no data verbs → quantitative
questions about operator data abstain cleanly at stage 9; +data platform → generic
`gold.*` verbs answer them; +domain packs → curated declared verbs sharpen them. Coverage
reconcile runs against the discovered set.

## 4. Conformance

- One pipeline implementation; surfaces differ only in transport adapters.
- Stage order is normative: expansion before cache key; persona before cache key; gate before
  stamp/persist/cache; suppressed or errored turns never cached.
- Every stage's failure posture is the one listed — a stage that fails open where this table
  says fail-closed (or vice versa) is a conformance bug.
- The turn trace must be sufficient to answer "why did the gate block this" without reproducing
  the turn (query, verbs+args, gate decision, unsupported claims, pre-gate answer).
