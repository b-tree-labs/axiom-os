# ADR-088: memory-recall projection corpus (amends ADR-069 projection scope)

**Status:** Proposed · **Date:** 2026-07-10
**Owner:** @ben
**Amends:** ADR-069 D2.ii (projection scope). ADR-069 otherwise stands in full.
**Related:** ADR-087 (cross-mem), RPE spec, spec-rag-architecture

## Context

ADR-069 D2.ii scopes the memory→RAG projection to `semantic` (plus
maturity-promoted facts): *"`vault` is never projected; raw `episodic` and
`core` are never projected."* That rule was written for the **document/knowledge
corpora** (community/org/internal), where it is correct: raw episodes and
persona fragments would pollute topical document retrieval.

ADR-087 introduces `CompositionService.recall()` — semantic search over a
user's *own memory* ("what do I know / what happened / how did I do this").
Under ADR-069's scope, recall cannot reach episodic, procedural, resource, or
core fragments by query at all: memory is semantically blind outside `semantic`.
Widening `_PROJECTABLE` in place would violate ADR-069's rationale for the
document corpora. The resolution is corpus separation, not a scope fight.

## Decision

1. **A dedicated `rag-memory` corpus**, per-principal, joins the corpus model
   beside community/org/internal. It is the retrieval index for
   `CompositionService.recall()` and is never blended into document-corpus
   queries unless an RPE plan explicitly requests fusion.

2. **Per-type projection policy for `rag-memory`:** `core`, `semantic`,
   `episodic`, `procedural`, and `resource` fragments are projectable by
   default. **`vault` is categorically excluded — no configuration can include
   it.** Resource fragments project as metadata + pointer, not blob content
   (this answers ADR-069 open question 2 for this corpus).

3. **The document corpora are unchanged.** ADR-069 D2.ii continues to govern
   community/org/internal exactly as written: semantic-only, vault never, raw
   episodic/core never.

4. **ADR-069 D5 is unchanged.** Procedural memory still graduates into Skills
   for *execution*. Its projection into `rag-memory` serves recall-as-context
   ("how did I handle X before"), not skill dispatch.

5. **Chunk contract.** `rag-memory` chunks carry `cognitive_type`,
   `fragment_ref`, and the fragment's `visibility`/`classification` so the
   ADR-087 serving gate can pre-filter at retrieval time. The projection is a
   rebuildable index with no write path: delete-and-rebuild is always safe.

6. **Eviction.** When a fragment is superseded or forgotten, its projected
   chunk is invalidated via `fragment_ref` (answering ADR-069 open question 3
   for this corpus; the same mechanism is available to the document projection).

## Consequences

**Wins**
- Episodic/procedural/resource/core recall becomes queryable without touching
  the document corpora's correctness or ADR-069's privacy floor.
- RPE intents can now honestly target memory recall vs document retrieval vs
  explicit fusion of both.
- One projection mechanism (fragment → chunk with `fragment_ref`) serves both
  corpora families; only the type policy differs.

**Costs**
- A second projection to keep fresh (same indexer, different policy).
- Per-principal corpus lifecycle (creation, rebuild, eviction) becomes part of
  corpus-health maintenance.

**Non-goals**
- No change to maturation (episodic → semantic distillation still feeds the
  document projection per ADR-069 D3).
- No relaxation of vault exclusion anywhere, in any corpus, under any
  configuration.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
