# Tech Spec: RAG Retrieval Policy Engine (RPE)

**Status:** Accepted (as-implemented MVP)
**Date:** 2026-04-17
**Authors:** Benjamin Booth, Claude
**Related:** spec-rag.md, spec-classification-boundary.md, ADR-026/027/028, `project_memory_architecture_unified.md`.
**Implementation:** `src/axiom/rag/rpe.py` (19 tests passing).

---

## 1. Purpose

Move beyond a single monolithic RAG index toward **N smaller,
context-matched retrievals selected dynamically per query**.
Retrieval is shaped by `(principal, intent, constraints)`, not
just embedding similarity.

This spec captures the Retrieval Policy Engine (RPE) — a
deterministic layer that maps:

```
(principal, intent, constraints) → RetrievalPlan
                                     ↳ sources, strategy, parameters
```

The plan then drives the existing retrieval stack (vector / graph
/ hybrid, local / federated fan-out, top-k / reranking / filtering).

## 2. Architectural position

```
query
  │
  ▼
[intent classifier]                   ← model-mediated (LLM; batch later)
  │   returns: intent_id
  ▼
[RPE: build_plan(principal, intent, constraints)]   ← DETERMINISTIC
  │   returns: RetrievalPlan { sources, strategy, params }
  ▼
[retriever executes plan]             ← existing PolicyAwareRetriever
  │                                     + federation fan-out
  ▼
[retrospective access check]          ← axiom.memory.access (#34)
  ▼
[classification gate]                 ← axiom.rag.gating (#31)
  ▼
[LLM generation with retrieved context]
  │
  ▼
[post-filter breach detection]        ← axiom.memory.post_filter (#40)
  ▼
response
```

RPE composes with the existing stack — it does not replace anything.
The memory primitives are the *enforcement* layer; RPE is the
*selection* layer.

## 3. Core intents (platform-shipped)

Axiom ships **8 core intents**. Intent classification is model-
mediated; plan derivation is deterministic.

| Intent | Purpose | Strategy | Top-K | Maturity floor |
|---|---|---|---|---|
| `lookup` | Factual retrieval; quick answer | vector | 3 | — |
| `diagnosis` | Causal chain / troubleshoot | graph | 5 | Facts |
| `synthesis` | Multi-source comprehension | hybrid | 10 | Facts |
| `teaching` | Pedagogy-ready explanation | vector | 5 | **Frameworks** |
| `operations` | Facility-specific, fresh data | vector | 5 | Patterns |
| `research` | Breadth; federated fan-out | fan_out | 20 | — |
| `generative` | Seed material for creation | vector | 10 | — |
| `metacognitive` | "How am I doing?" — user's own history | trace | 50 | — |

Two additions beyond the original 6: **Generative** (compose an
artifact) and **Metacognitive** (meta-questions; pulls user trace
history rather than topic knowledge).

## 4. Intent taxonomy — extension-registrable

**Core intents live in the platform.** Domain extensions register
custom intents via `IntentRegistry`:

```python
from axiom.rag.rpe import Intent, IntentRegistry

reg = IntentRegistry.with_defaults()
reg = reg.register(Intent(
    id="symptom-triage",
    description="Clinical symptom-to-diagnosis retrieval.",
    default_strategy="graph",
))
```

Extension-registered intents inherit a conservative generic plan
(tight `top_k=3`, single org-tier source). Domains that want
custom plans implement their own plan builder and route it in
(next-version enhancement).

## 5. Constraints — the third input

Constraints shape the plan without overriding intent:

| Constraint | Effect |
|---|---|
| `federated_peers: [str]` | Enables fan-out to listed peers (research/synthesis) |
| `classification_ceiling: str` | Filters peers without attestation at the ceiling |
| `peer_attestations: {peer: att}` | Cross-node EC attestations (ADR-028) |
| `maturity_floor: str` | Overrides default floor from intent |
| `recency_bias: float` | Overrides default recency weighting |

Constraints come from:
- **Principal's context**: is this a student, operator, researcher?
- **Policy coordinate** (`axiom.memory.policy`): resolved at the
  current time + user + agent.
- **Classroom context**: EC-gated classroom injects
  `classification_ceiling="EC"` automatically.

## 6. Determinism boundary (spec-classification-boundary §2 alignment)

**Model-mediated**:
- Intent classification (LLM reads query + returns `intent_id`)
- Future: CURIO Karpathy loop proposes decision-table tweaks

**Deterministic**:
- `build_plan(principal, intent_id, constraints) → RetrievalPlan`
  — always reproducible. Same inputs, same plan. Guaranteed by
  `TestDeterminism` in `test_rpe.py`.
- Plan execution (retriever runs the plan) — deterministic given
  the retrieved corpus state.

This split is intentional. Models suggest; deterministic code
enforces. Auditability of *what was retrieved* does not depend
on reproducing the LLM.

## 7. Federation as compositional retrieval

The differentiator. Most RAG systems pick one index. RPE picks a
plan *across peers*:

```python
RetrievalPlan(
    sources=[
        SourceSpec(tier="org",       cognitive_types=("semantic",)),
        SourceSpec(tier="community", cognitive_types=("semantic",)),
        SourceSpec(tier="community", node="osu.axiom.edu", remote=True),
        SourceSpec(tier="community", node="inl.axiom.gov", remote=True),
    ],
    strategy="fan_out",
    params={"top_k": 20, "breadth_weight": 0.8},
)
```

Trust-weighted aggregation (ADR-028 derived-score) boosts results
from higher-trust peers during rerank. Classification gates
(ADR-028 peer attestations) drop peers lacking valid attestation
under an EC ceiling.

## 8. Maturity-as-retrieval-signal

Knowledge maturity ladder (Data → Patterns → Facts → Frameworks →
Application → Wisdom) is a **queryable dimension**, not just a
promotion target:

- Teaching: `maturity_floor=Frameworks` — students get explanation-
  ready material, not raw data.
- Operations: `maturity_floor=Patterns` — operators get concrete
  fact-grounded procedures.
- Research: no floor — researchers want breadth including raw.

This is a small-but-visceral differentiator. No competitor exposes
maturity as a retrieval axis.

## 9. Tuning — the CURIO loop (future)

The plan decision table in `_CORE_PLAN_TABLE` encodes current
defaults. CURIO runs a Karpathy-style autoresearch loop:

1. Observe retrieval outcomes (answer quality, user follow-ups,
   explicit feedback).
2. Propose decision-table tweaks (e.g., "bump synthesis `top_k`
   from 10 to 15").
3. Run statistical significance tests (p < 0.05 per existing
   `CURIO quality gates`).
4. Surface as proposed changes for human review (never auto-mutate
   — same principle as ADR-028 adaptation loop).

Today, the decision table is hand-authored. Tomorrow, CURIO
suggests adjustments that humans accept.

## 10. Integration with existing retriever

RPE composes with `axiom.rag.policy.PolicyAwareRetriever` (already
built). The flow:

```python
intent_id = classify_intent(query, principal_context)  # model-mediated
plan = build_plan(principal, intent_id, constraints)   # deterministic
retriever = build_retriever_from_plan(plan)            # (next task)
results = retriever.retrieve(query)
```

The `build_retriever_from_plan` step (not in MVP) assembles:
- Local corpora per the plan's `tier` + `cognitive_types` filters
- Remote fetches for each remote source (federation A2A via #17
  pack distribution primitives)
- Graph backend selection (Apache AGE) when `strategy=graph`
- Rerank weights per `params`

## 11. Privacy

- **Trust scores used during fan-out are private** to the querying
  principal (ADR-028 enforces). The plan lists peers; the retriever
  uses local trust scores to rerank without exposing them.
- **User-trace retrieval (metacognitive intent) stays local** by
  default. If the user wants to compare against cohort patterns,
  that's a separate opt-in flow.

## 12. Open items

- **Intent classifier implementation.** Today `build_plan` accepts
  an `intent_id` but the classifier isn't in this repo. Wire a
  cheap deterministic classifier (keyword + shape heuristics; like
  our `axiom.memory.auto_classifier`) as a fallback; LLM classifier
  as the richer option.
- **Extension-custom plan builders.** Extensions currently inherit
  a generic plan for their intents. Spec extension: register plan
  functions alongside intents.
- **`build_retriever_from_plan`.** The glue between RPE and the
  existing retriever stack. Not in the MVP; next iteration.
- **Online vs pre-computed intent classification.** Online LLM
  classification adds latency. Batch classification for recurring
  query patterns is a future optimization.

## 13. Test coverage

`tests/rag/test_rpe.py`: 19 tests covering:
- 8 core intents registered by default
- Intent registry: with_defaults, register, duplicate detection
- RetrievalPlan + SourceSpec shape
- Per-intent plan checks:
  - lookup: vector, small top_k
  - teaching: `maturity_floor = Frameworks`
  - operations: recency bias, facility tier
  - research: fan_out when peers given, `remote=True` sources
  - synthesis: multi-hop, hybrid strategy
  - diagnosis: graph strategy
  - generative: breadth (`top_k >= 5`)
  - metacognitive: `user_traces` source
- Constraint enforcement:
  - No peers → local only
  - EC ceiling + unattested peers → no remote sources
- Extension intent → generic conservative plan
- Determinism: same inputs always produce same plan
- Unknown intent → raises ValueError
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
