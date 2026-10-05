# RAG Knowledge Maturity Pipeline

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-20
**Layer:** Axiom core
**Related:** `spec-rag-architecture.md`, `prd-rag.md`, `spec-agent-architecture.md`

---

## Terms Used

| Term | Definition | Reference |
|------|-----------|-----------|
| `tier` | Content sensitivity axis: `public · restricted · classified` (Axiom archetypes); Axiom configures as `public · restricted · export_controlled` | `axiom glossary tier` |
| `scope` | Content visibility axis: `community · facility · personal` | `axiom glossary scope` |
| `knowledge_maturity` | Integer 0–5 representing how thoroughly raw data has been validated and synthesised into durable knowledge | `axiom glossary knowledge_maturity` |
| `knowledge_fact` | A discrete, validated proposition extracted from interaction history | `axiom glossary knowledge_fact` |
| `retrieval_log` | Per-chunk view of retrieval events, derived from `interaction_log` | `axiom glossary retrieval_log` |
| `promotion_policy` | Configurable thresholds governing when content advances to a higher maturity layer | `axiom glossary promotion_policy` |
| `domain_pack` | A versioned bundle of community-scope content (chunks + facts) for a specific domain, distributed as an installable artifact | `axiom glossary domain_pack` |

All terms resolve via `axiom glossary <term>` or `docs/glossary-axiom.toml`.

---

## 1. Overview

The raw corpus (Layer 0) is just indexed documents. A freshly ingested document is immediately retrievable, but its epistemic value is untested — it has never been queried, its claims have never been confirmed, and no validated knowledge has been derived from it. Value compounds as operators interact with the system: retrieval patterns reveal which content is genuinely useful, explicit feedback signals identify what the system got wrong, and accumulated interactions allow SCAN to crystallize discrete validated facts.

The knowledge maturity pipeline is the mechanism by which noisy interactions become durable knowledge. It has two jobs:

1. **Promotion** — advance content and derived facts through Layers 0-5 as evidence accumulates, subject to configurable policy thresholds and human review gates.
2. **Quality closure** — capture negative signals (thumbs down, explicit corrections) and materialise them as regression test cases, so failures permanently improve system behaviour.

This spec covers the interaction log schema, the knowledge fact schema, the promotion policy protocol, the SCAN crystallization pipeline (Layers 1→2), the TIDY crystallization sweep, and the regression evaluation loop. The two-dimensional content model (`tier × scope`), three-store architecture, embedding provenance, personal RAG, and EC compliance are covered in `spec-rag-architecture.md`.

---

## 2. Layer Definitions

| Layer | Name | Description | Who produces | Promotion trigger |
|-------|------|-------------|--------------|-------------------|
| 0 | Data | Raw indexed documents and chunks | Ingest pipeline | Automatic on ingest |
| 1 | Patterns | Retrieval patterns mined from interaction log | TIDY sweep | Retrieval frequency threshold (configurable) |
| 2 | Facts | Validated knowledge facts extracted from interaction clusters | SCAN crystallization + human review | Promotion policy evaluation + human approval |
| 3 | Frameworks | Cross-domain synthesised mental models | CURIO cross-domain synthesis + human curation | CURIO monthly synthesis + human approval |
| 4 | Application | Validated applied procedures | Human authorship + CURIO research assist | Human approval required |
| 5 | Wisdom | Accumulated operational heuristics derived from long-term pattern mining | CURIO federated research patterns + long-term mining | v2+ |

**v1 ships Layers 0–2.** Layers 3–5 are defined here for schema forward-compatibility. **CURIO** ([spec-auto-research.md](spec-auto-research.md)) is the supply-side engine for Layers 3–5 — its cross-domain synthesis, standing orders, and platform self-research cycles produce the framework candidates, validated procedures, and heuristics that populate these layers.

---

## 3. Interaction Log Schema

The `interaction_log` table is the foundational record for the entire maturity pipeline. Every RAG-assisted LLM completion must write one row. Nothing in the maturity pipeline — promotion, crystallization, regression materialisation — can function without a complete interaction log.

```sql
CREATE TABLE interaction_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Query
    query_text      TEXT NOT NULL,
    query_hash      TEXT NOT NULL,   -- SHA-256 of normalised query_text; used for deduplication
    query_embedding_model TEXT,      -- model used to embed the query for retrieval

    -- Retrieval
    chunks_retrieved JSONB NOT NULL, -- [{chunk_id, score, corpus_id, access_tier, scope}]
    retrieval_latency_ms INT,

    -- Generation
    prompt_template_id      TEXT,
    prompt_template_version TEXT,
    prompt_content_hash     TEXT,    -- SHA-256 of the full rendered prompt
    llm_provider            TEXT NOT NULL,
    llm_model               TEXT NOT NULL,
    response_text           TEXT,    -- NULL when response is classified-tier
    response_hash           TEXT NOT NULL,  -- SHA-256 of response_text
    input_tokens            INT,
    output_tokens           INT,
    generation_latency_ms   INT,

    -- Classification
    access_tier     TEXT NOT NULL,   -- tier of the most sensitive chunk retrieved
    scope           TEXT NOT NULL,   -- scope of the session context

    -- Signals
    confidence_signal   FLOAT,       -- 0.0–1.0; NULL = not computed
    feedback_signal     SMALLINT,    -- +1 thumbs up | -1 thumbs down | 0 explicit neutral | NULL = no signal
    correction_text     TEXT,        -- explicit correction provided by user

    -- Maturity
    maturity_layer      SMALLINT NOT NULL DEFAULT 0,
    crystallized        BOOLEAN NOT NULL DEFAULT FALSE,
    crystallized_at     TIMESTAMPTZ,
    knowledge_fact_ids  UUID[]       -- facts extracted from this interaction
);

-- Query performance
CREATE INDEX ON interaction_log (session_id, created_at);
CREATE INDEX ON interaction_log (query_hash);

-- TIDY sweep: find un-crystallized rows without scanning crystallized=TRUE rows
CREATE INDEX ON interaction_log (crystallized, created_at) WHERE NOT crystallized;

-- Regression sweep: find negative signals
CREATE INDEX ON interaction_log (feedback_signal) WHERE feedback_signal IS NOT NULL;
```

### 3.1 Write Contract

Every completion path in the RAG pipeline must write to `interaction_log` before returning a response to the caller. This is not optional. Completions that skip the log cannot participate in promotion, crystallization, or regression evaluation.

Fields with `NOT NULL DEFAULT` constraints may be omitted on insert; all others are required at write time. `response_text` is explicitly nullable — classified-tier responses must not be stored in plaintext; store `response_hash` only.

### 3.2 Feedback Signal Capture

`feedback_signal` is populated by the CLI thumbs up/down affordance (`axiom rag feedback --up / --down`) and by any UI surface that supports inline feedback. The interaction `id` is included in every response envelope so the caller can submit feedback after the fact. `correction_text` is populated when the user provides a freetext correction via `axiom rag feedback --correct "..."`.

---

## 4. Retrieval Log

The `retrieval_log` is a per-chunk view of retrieval events derived from `interaction_log.chunks_retrieved`. It exists for efficient chunk-level queries required by the promotion policy engine. `interaction_log` is the source of truth; `retrieval_log` must never be written independently.

```sql
CREATE MATERIALIZED VIEW retrieval_log AS
SELECT
    gen_random_uuid()                          AS id,
    il.id                                      AS interaction_id,
    il.created_at,
    (chunk->>'chunk_id')::UUID                 AS chunk_id,
    (chunk->>'corpus_id')::TEXT                AS corpus_id,
    (chunk->>'score')::FLOAT                   AS score,
    (chunk->>'access_tier')::TEXT              AS access_tier,
    (chunk->>'scope')::TEXT                    AS scope,
    il.feedback_signal,
    il.crystallized
FROM interaction_log il,
     LATERAL jsonb_array_elements(il.chunks_retrieved) AS chunk;

CREATE INDEX ON retrieval_log (chunk_id, created_at);
CREATE INDEX ON retrieval_log (chunk_id, feedback_signal) WHERE feedback_signal IS NOT NULL;
```

The promotion policy reads from this view to compute `RetrievalStats` per chunk. TIDY refreshes the view as part of each crystallization sweep (`REFRESH MATERIALIZED VIEW CONCURRENTLY retrieval_log`).

If query volume makes a materialized view impractical, the view may be replaced with a physical `retrieval_log` table populated by an insert trigger on `interaction_log`. The external interface (column names and indexes) does not change.

---

## 5. Knowledge Fact Schema

A `knowledge_fact` is a discrete, validated proposition extracted from one or more interaction log records by the SCAN crystallization pipeline. Facts are the primary output of Layer 2.

```sql
CREATE TABLE knowledge_facts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Content
    proposition     TEXT NOT NULL,              -- the fact statement; must be a single verifiable claim
    context         TEXT,                        -- supporting context; quoted passage or SCAN summary
    source_interaction_ids UUID[] NOT NULL,     -- interaction_log rows this was derived from
    source_chunk_ids       UUID[],              -- specific chunks cited in those interactions

    -- Classification
    access_tier     TEXT NOT NULL,              -- inherited from sources; never downgraded (see §7.3.4)
    scope           TEXT NOT NULL,
    domain_tags     TEXT[],                     -- e.g., ["system_physics", "procedures"] — domain-specific; set by SCAN

    -- Maturity
    maturity_layer  SMALLINT NOT NULL DEFAULT 2,
    validation_state TEXT NOT NULL DEFAULT 'pending_review',
        -- pending_review | approved | rejected | superseded
    reviewed_by     TEXT,                       -- user id of reviewer
    reviewed_at     TIMESTAMPTZ,
    superseded_by   UUID REFERENCES knowledge_facts(id),

    -- Embedding
    embedding       VECTOR(768),
    embedding_model TEXT,
    embedding_dims  INT,
    needs_reembed   BOOLEAN NOT NULL DEFAULT FALSE,

    -- Trust gradient (see §6.3)
    trust_path      TEXT NOT NULL DEFAULT 'yellow',
        -- green | yellow | red
    resolution_notes TEXT
        -- SCAN's reasoning for yellow/red paths; NULL on green
);

CREATE INDEX ON knowledge_facts (validation_state, created_at);
CREATE INDEX ON knowledge_facts (access_tier, scope);
CREATE INDEX ON knowledge_facts USING ivfflat (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;
```

### 5.1 Validation State Machine

```
pending_review → approved     (human reviewer accepts, OR green/yellow path auto-promote)
pending_review → rejected     (human reviewer rejects)
approved       → superseded   (newer fact replaces this one; superseded_by is set)
```

Rejected facts are retained for audit purposes and to prevent the same erroneous proposition from being re-submitted. The optimizer step (§7.3.3) checks rejected facts before proposing a new candidate.

Only `approved` facts are retrievable by the RAG pipeline. `pending_review`, `rejected`, and `superseded` facts are excluded from embedding search at query time.

### 5.2 Schema Migration

For existing deployments, add the trust gradient columns:

```sql
ALTER TABLE knowledge_facts
    ADD COLUMN IF NOT EXISTS trust_path TEXT NOT NULL DEFAULT 'yellow',
    ADD COLUMN IF NOT EXISTS resolution_notes TEXT;
```

The `DEFAULT 'yellow'` ensures existing facts — which predate trust path tracking — are treated as requiring SCAN review rather than being silently auto-approved.

---

## 6. Promotion Policy

The `PromotionPolicy` protocol defines the interface that governs layer advancement. The default implementation reads from `rag.toml`. Custom implementations may be registered via the extension system.

### 6.1 Protocol Definition

```python
from typing import Protocol, Literal
from dataclasses import dataclass
from uuid import UUID
from datetime import datetime

TrustPath = Literal["green", "yellow", "red"]


@dataclass
class RetrievalStats:
    chunk_id: UUID
    retrieval_count: int
    positive_signal_count: int    # feedback_signal = +1
    negative_signal_count: int    # feedback_signal = -1
    no_signal_count: int          # feedback_signal IS NULL
    positive_ratio: float         # positive_signal_count / (positive + negative); NaN if no signals
    first_retrieved: datetime
    last_retrieved: datetime


@dataclass
class PromotionDecision:
    trust_path: TrustPath
    eligible: bool
    rationale: str          # logged for observability
    suggested_action: str   # what SCAN should do next


class PromotionPolicy(Protocol):
    def eligible_for_layer1(
        self,
        chunk_id: UUID,
        retrieval_stats: RetrievalStats,
    ) -> bool:
        """Return True if chunk qualifies for Layer 1 (Patterns) promotion."""
        ...

    def eligible_for_layer2(
        self,
        interaction_ids: list[UUID],
        retrieval_stats: RetrievalStats,
    ) -> bool:
        """Return True if an interaction cluster qualifies for crystallization."""
        ...

    def evaluate(
        self,
        fact: KnowledgeFact,
        retrieval_stats: RetrievalStats,
        existing_community_facts: list[KnowledgeFact],  # for contradiction check
        facility_count: int,  # how many facilities independently validated this fact
    ) -> PromotionDecision:
        """Return a PromotionDecision with trust_path for Layer 2 community promotion."""
        ...
```

`eligible_for_layer1` and `eligible_for_layer2` gate the crystallization pipeline (§8). `evaluate` is called inside the crystallization pipeline (§7.3, Step 5) to determine the trust path for each candidate fact.

### 6.2 Trust Path Criteria

The trust gradient replaces the binary `requires_human_review` gate. Agentic curation (GREEN and YELLOW) is the primary path; human review (RED) is reserved for exceptional cases.

**GREEN — auto-promote, no SCAN call needed:**
- `confidence >= 0.85`
- AND `access_tier == "public"`
- AND `validation_state` candidate is `"approved"` (no blocking signal)
- AND no contradiction detected
- AND (`facility_count >= 2` OR single-facility with `confidence >= 0.95`)

GREEN facts are written with `validation_state = "approved"` and `trust_path = "green"` immediately. They require no SCAN resolver call and no human action.

**YELLOW — SCAN agentic resolution (see §7.3, Step 5b):**
- `0.60 <= confidence < 0.85`
- OR potential contradiction detected (cosine similarity 0.75–0.90 with an existing fact of different valence)
- OR novel single-facility fact (no similar existing community fact, `confidence` 0.70–0.94)
- OR `domain_tag` in `sensitive_tags` list (configurable per deployment)

YELLOW facts are passed to the SCAN resolver. If SCAN resolves without escalation and scope is `community`, the fact is promoted with `trust_path = "yellow"` and `resolution_notes` populated.

**RED — human review required (should be rare):**
- `access_tier == "classified"` (always RED — invariant, see §12)
- OR `confidence < 0.60`
- OR SCAN flagged an irresolvable contradiction
- OR facility policy: `require_human_review = true` for specific `domain_tags`
- OR `facility_count >= 2` but facilities reached different validation conclusions

RED facts are written with `validation_state = "pending_review"` and `trust_path = "red"`. They enter the human review queue (§7.4).

### 6.3 Default Policy Configuration (`rag.toml`)

```toml
[promotion.layer_0_to_1]
min_retrievals       = 5
min_positive_ratio   = 0.6
require_human_review = false
min_age_days         = 7       # prevents promoting content that spiked briefly then went cold

[promotion.layer_1_to_2]
min_retrievals       = 20
min_positive_ratio   = 0.75

[promotion.trust_gradient]
green_confidence_threshold    = 0.85
green_multi_facility_required = true  # green requires ≥2 facilities unless confidence ≥ 0.95
yellow_min_confidence         = 0.60
sensitive_tags                = []    # domain_tags that always trigger yellow minimum
red_on_classified             = true  # classified facts always RED (required — do not disable)
human_review_sla_days         = 5

[promotion.targets]
green_path_target   = 0.90   # aspirational: 90% of promotions via green or yellow
red_path_ceiling    = 0.05   # alert if red_path_ratio > 5% (review queue health)
```

### 6.4 Policy Evaluation Logic

The Layer 0→1 and Layer 1→2 eligibility logic is unchanged from the original threshold model:

```
eligible_for_layer1:
  retrieval_count >= min_retrievals
  AND positive_ratio >= min_positive_ratio
  AND (now() - first_retrieved) >= min_age_days

eligible_for_layer2:
  retrieval_count >= layer_1_to_2.min_retrievals
  AND positive_ratio >= layer_1_to_2.min_positive_ratio
```

Negative signals (`feedback_signal = -1`) lower `positive_ratio` and can prevent or reverse promotion eligibility. A chunk that accumulates significant negative signal after Layer 1 promotion is flagged for review by the next TIDY sweep.

The `evaluate()` method applies the GREEN / YELLOW / RED criteria in §6.2, in that order (GREEN first, RED last). The first matching criteria set wins. `PromotionDecision.rationale` is always populated and written to the sweep report for observability.

---

## 7. Conversation Crystallization (Evaluator-Optimizer Pipeline)

Crystallization is the Layer 1→2 promotion mechanism. SCAN owns this pipeline; TIDY owns scheduling and batch management (§8). The pipeline extracts a candidate `knowledge_fact` from a cluster of related interaction log records, validates it against existing knowledge, and routes it through the trust gradient for promotion or review.

### 7.1 What It Does

Takes a set of related `interaction_log` records — either same-topic cluster identified by TIDY, or explicitly linked via `axiom rag crystallize --session <id>` — and runs a multi-step LLM pipeline:

1. **Evaluator** (SCAN): extract a candidate proposition from the interactions
2. **Optimizer** (SCAN): deduplicate and check for contradiction against existing facts
3. **Trust path determination**: call `promotion_policy.evaluate()` to route the candidate through GREEN / YELLOW / RED

GREEN and YELLOW candidates are auto-promoted. RED candidates are written with `validation_state = pending_review` and enter the human review queue. Human review is the exception, not the default path.

### 7.2 Trigger Conditions

| Trigger | Description |
|---------|-------------|
| TIDY crystallization sweep | Scheduled sweep (§8) identifies clusters meeting promotion_policy thresholds |
| Manual CLI | `axiom rag crystallize --session <id>` — crystallize a specific session immediately |
| Manual CLI (batch) | `axiom rag crystallize --since <date>` — crystallize all eligible interactions since a date |

### 7.3 Pipeline Steps

```
Step 1: Cluster selection
  TIDY queries interaction_log for rows where:
    - crystallized = FALSE
    - promotion_policy.eligible_for_layer2() returns TRUE for the cluster's retrieval stats
    - Groups by semantic similarity: cosine similarity > 0.88 on query embeddings
      (query embeddings stored in a separate lookup table; see §7.5)
  Output: list of interaction_id clusters

Step 2: Evaluator (SCAN)
  LLM call per cluster.
  System prompt:
    "You are a knowledge extraction assistant. Given a set of interaction records
     (user queries and system responses), extract a single concise, verifiable
     proposition representing a validated piece of knowledge. Return JSON only."
  Input:  query_text + response_text for each interaction in the cluster.
          Do NOT pass raw chunk text — chunk content may be classified-tier
          and must not cross the boundary. Use synthesised responses only.
  Output: {
    "proposition": str,       -- one sentence; a falsifiable claim
    "context":     str,       -- supporting context or evidence
    "confidence":  float,     -- 0.0–1.0
    "domain_tags": list[str]  -- optional; domain-specific tags
  }

Step 3: Optimizer (SCAN)
  a. Embed the candidate proposition using the standard embedding pipeline.
  b. Search knowledge_facts (approved + pending_review) with cosine similarity > 0.92.
  c. Decision:
     - Near-duplicate found (similarity > 0.92, same proposition class):
         Skip candidate. Mark source interactions as crystallized.
         Link source interactions to existing fact (append to knowledge_fact_ids).
     - Contradiction found (high similarity but opposing polarity, detected by LLM judge):
         Flag both candidate and existing fact for human review.
         Write candidate with validation_state = pending_review and a contradiction_flag.
         Do NOT auto-promote.
     - Novel (no match above threshold):
         Write knowledge_fact with validation_state = pending_review.

Step 4: Tier inheritance
  access_tier = max sensitivity across all source interaction tiers.
  If any source interaction has access_tier = "classified", the fact is classified-tier.
  Tier is never downgraded. A classified-tier fact cannot be reclassified to restricted
  or public by any automated process — only by explicit operator action with audit trail.

Step 5: Trust path determination
  Call promotion_policy.evaluate() with:
    - the candidate fact (proposition, confidence, domain_tags, access_tier)
    - retrieval_stats for the cluster
    - existing_community_facts (top-10 by embedding similarity from knowledge_facts)
    - facility_count (distinct facility origins among source interactions)

  Route based on PromotionDecision.trust_path:

  GREEN → write fact with:
            validation_state = "approved"
            trust_path       = "green"
            resolution_notes = NULL
          If scope = "community": promoted_to_community = True immediately.
          No SCAN resolver call. No human action required.

  YELLOW → proceed to Step 5b (SCAN resolver).

  RED → write fact with:
          validation_state = "pending_review"
          trust_path       = "red"
          resolution_notes = PromotionDecision.rationale
        Add to human review queue. Do NOT auto-promote.

Step 5b: SCAN resolver (YELLOW path only)
  A second LLM call with expanded context:

  System prompt:
    "You are a knowledge curator. Given a candidate fact and potentially similar
     or conflicting existing facts, determine: (1) Is this a duplicate? (2) Is this
     a contradiction? (3) Should this be promoted? Provide a resolution note."

  Input:  candidate fact
          + top-3 similar existing facts (by embedding cosine similarity)
          + their source context (interaction query_text + response_text)

  Output (JSON):
    {
      "action": "promote | merge | defer | escalate",
      "resolution_notes": str,
      "merged_proposition": str   // only present when action = "merge"
    }

  Route based on SCAN action:
    promote   → write fact with validation_state = "approved", trust_path = "yellow",
                resolution_notes = SCAN output. If scope = "community": promote.
    merge     → write merged fact (merged_proposition replaces proposition),
                validation_state = "approved", trust_path = "yellow",
                resolution_notes = SCAN output. Mark original overlapping fact
                as superseded_by = new fact id.
    defer     → write fact with validation_state = "pending_review",
                trust_path = "yellow", resolution_notes = SCAN output.
                Enters review queue but with lower priority than RED.
    escalate  → treat as RED: write validation_state = "pending_review",
                trust_path = "red", resolution_notes = SCAN output.

Step 6: Attribution
  source_interaction_ids = all interaction_log.id values in the cluster.
  source_chunk_ids = union of chunk_id values from chunks_retrieved across those interactions.

Step 7: Mark interactions
  UPDATE interaction_log
     SET crystallized = TRUE, crystallized_at = now()
   WHERE id = ANY(:cluster_interaction_ids);
```

### 7.4 Human Review Interface

`axiom rag review` lists all `knowledge_facts` with `validation_state = pending_review`, ordered by `created_at` ascending (oldest first). For each fact:

```
axiom rag review                    # interactive review queue
axiom rag review --approve <id>     # approve a specific fact
axiom rag review --reject <id>      # reject with optional --reason "..."
axiom rag review --edit <id>        # open proposition in $EDITOR, then re-submit
```

On **approve**: `validation_state = approved`, `reviewed_by = <current user>`, `reviewed_at = now()`. Fact becomes retrievable on next embedding index refresh.

On **reject**: `validation_state = rejected`, reason recorded in `context`. Source interactions remain `crystallized = TRUE` to prevent immediate re-submission. A rejected fact may be re-crystallized after operator review if the source interactions are explicitly un-crystallized (`axiom rag uncrystallize --interaction <id>`).

On **edit**: The revised proposition is embedded and the optimizer step re-runs before the fact is approved. This handles cases where SCAN extracted a correct but imprecisely worded proposition.

### 7.5 Query Embedding Lookup Table

The cluster selection step (Step 1) requires embedding lookups by `query_hash`. These are stored separately to avoid re-embedding on each sweep:

```sql
CREATE TABLE query_embeddings (
    query_hash      TEXT PRIMARY KEY,
    embedding       VECTOR(768) NOT NULL,
    embedding_model TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON query_embeddings USING ivfflat (embedding vector_cosine_ops);
```

The ingest path writes to `query_embeddings` on first occurrence of each `query_hash`. Subsequent interactions with the same `query_hash` reuse the existing embedding.

---

## 8. TIDY Crystallization Sweep

TIDY owns the scheduled stewardship job that drives the knowledge maturity pipeline at scale. The sweep is TIDY's primary knowledge hygiene responsibility.

### 8.1 Schedule Configuration (`rag.toml`)

```toml
[promotion.sweep]
schedule        = "weekly"    # daily | weekly | manual
min_batch_size  = 10          # skip sweep if fewer than N eligible interactions; avoids LLM spend on thin data
max_batch_size  = 500         # cap per sweep run to bound LLM cost; remainder carried to next run
off_hours_only  = true        # restrict to 01:00–05:00 local time to avoid peak-hours impact
```

### 8.2 Sweep Procedure

Each sweep run executes the following steps in order:

1. **Eligibility query** — Query `interaction_log` for rows where `crystallized = FALSE`. Cross-join with `retrieval_log` aggregates to compute `RetrievalStats` per chunk. Apply `promotion_policy.eligible_for_layer2()`. Collect eligible rows up to `max_batch_size`.

2. **Skip guard** — If eligible row count < `min_batch_size`, write sweep summary (zero clusters, skipped) and exit. This prevents spending LLM tokens on sparse signal.

3. **Clustering** — Group eligible interactions by semantic similarity of their query embeddings (cosine similarity > 0.88, from `query_embeddings` table). Each connected component above a minimum size (default: 3 interactions) becomes a candidate cluster.

4. **Crystallization** — For each cluster, invoke the SCAN crystallization pipeline (§7.3). SCAN calls are parallelized up to a configurable concurrency limit (`sweep.max_concurrent_eve_calls`, default 4).

5. **Materialized view refresh** — `REFRESH MATERIALIZED VIEW CONCURRENTLY retrieval_log`.

6. **Report** — TIDY writes a structured sweep summary to the stewardship log:

```json
{
  "sweep_id": "<uuid>",
  "started_at": "2026-03-20T02:14:00Z",
  "completed_at": "2026-03-20T02:47:00Z",
  "eligible_interactions": 142,
  "clusters_processed": 18,
  "facts_created_green": 9,
  "facts_created_yellow": 6,
  "facts_created_red_pending_review": 2,
  "duplicates_skipped": 4,
  "contradictions_flagged": 1,
  "interactions_crystallized": 142,
  "llm_tokens_consumed": 48200,
  "red_path_ratio": 0.035
}
```

`red_path_ratio` is `facts_created_red_pending_review / clusters_processed`. TIDY alerts if this exceeds `promotion.targets.red_path_ceiling` for two consecutive sweeps (Invariant 8).

### 8.3 CLI

```bash
axiom tidy sweep --knowledge                  # manual trigger (respects off_hours_only config)
axiom tidy sweep --knowledge --force          # run immediately regardless of off_hours_only
axiom tidy sweep --knowledge --dry-run        # show what would be processed; no writes, no LLM calls
axiom tidy sweep --knowledge --session <id>  # sweep only interactions from a specific session
```

---

## 9. Regression Evaluation from Production Failures

Negative feedback signals close the quality loop: a failure in production becomes a permanent regression test case, ensuring the same failure cannot silently recur after a RAG update.

### 9.1 Failure Capture

An interaction is flagged as a failure candidate when either:
- `feedback_signal = -1` (user indicated thumbs down)
- `correction_text IS NOT NULL` (user provided explicit correction)

These interactions are prioritised in the TIDY crystallization sweep (processed before neutral interactions within the same batch). They are also queued for regression test materialisation regardless of whether they meet the crystallization threshold — a single confirmed failure is sufficient evidence for a regression test.

### 9.2 Test Materialisation

During each TIDY sweep, failure candidates are materialised as promptfoo YAML test cases:

```python
# Auto-generated by TIDY sweep
# Path: tests/promptfoo/regression/auto-YYYYMMDD-{hash[:8]}.yaml
```

```yaml
- description: "Regression: {query_hash[:8]} (failed {failed_at})"
  vars:
    query: "{query_text}"
    expected_correction: "{correction_text}"   # only present if correction_text is not NULL
  assert:
    - type: not-similar
      value: "{original_failed_response}"      # the system must NOT reproduce the same bad answer
      threshold: 0.85
    - type: llm-rubric
      value: "Answer should address: {correction_text}"
      # assertion omitted when correction_text is NULL; not-similar alone is the gate
```

Each generated file is idempotent — the filename is derived from `query_hash` and `created_at`, so the same interaction always produces the same filename. Re-running the sweep does not create duplicates.

`response_text` is used as the `original_failed_response` value. If `response_text` is NULL (classified-tier), regression test materialisation is skipped for that interaction and a warning is written to the sweep report.

### 9.3 Regression Sweep Command

```bash
axiom eval regression                   # run all auto-generated regression cases against current RAG state
axiom eval regression --since 2026-01-01  # limit to cases materialised after a date
axiom eval regression --case <filename>   # run a single case
```

Results are reported per case (pass/fail/skip). `axiom eval regression` exits non-zero if any case fails, making it suitable as a CI gate.

Integration in CI:

```yaml
# .gitlab-ci.yml (optional gate — configure as allowed_failure for initial rollout)
rag-regression:
  stage: eval
  script:
    - axiom eval regression
  allow_failure: true   # remove when regression suite is stable
```

### 9.4 Test Case Lifecycle

A regression test case is retained until one of the following:

| Condition | Action |
|-----------|--------|
| The corresponding `knowledge_fact` reaches `validation_state = approved` | Archive the test case — the correction is now in the knowledge base. TIDY moves file to `tests/promptfoo/regression/archived/`. |
| Operator manual archive | `axiom eval regression --archive <filename>` — removes from active suite with an audit record. |

Archived cases are retained for 90 days (configurable: `eval.regression_archive_days` in `rag.toml`) before permanent deletion.

---

## 10. Implementation Phases

| Phase | Scope | Status |
|-------|-------|--------|
| Phase 1 | `interaction_log` schema, write to log on every completion, `feedback_signal` capture (thumbs up/down in CLI), `query_embeddings` table | Planned |
| Phase 2 | TIDY crystallization sweep, SCAN evaluator step, `knowledge_facts` table, `axiom rag review` | Planned |
| Phase 3 | Optimizer step (deduplication + contradiction), regression test materialisation, `axiom eval regression` | Planned |
| Phase 4 | Layers 3–5, cross-domain synthesis, community corpus promotion | Future |

Phase boundaries are hard: nothing in Phase 2 is built before Phase 1 is complete and the interaction log is accumulating real data. Crystallization against sparse signal produces low-quality facts.

---

## 11. Relationship to Community Corpus

Facts that reach `validation_state = approved` and `scope = community` are candidates for inclusion in a domain pack update. This is the tacit knowledge flywheel:

```
Operator interactions
  → interaction_log (Layer 0 signal)
  → TIDY sweep identifies patterns (Layer 1)
  → SCAN crystallizes facts (Layer 2, pending_review)
  → Human review approves facts
  → Approved community-scope facts → domain pack candidate
  → Domain pack published → better onboarding at next facility
```

The domain pack promotion step is not automated. An operator runs `axiom pub domain-pack --from-facts --since <date>` to review approved community-scope facts and bundle them into a new domain pack version. The domain pack publisher (covered in `spec-publisher.md`) handles versioning, signing, and distribution.

Classified-tier facts are never included in domain packs, regardless of scope. The tier inheritance rule (§7.3.4) ensures this: a fact derived from any classified-tier interaction is permanently classified-tier and cannot be published.

---

## 12. Invariants

The following invariants must hold at all times. Violations indicate a bug in the pipeline and should cause the relevant sweep or write to abort with an error rather than produce inconsistent state.

| Invariant | Description |
|-----------|-------------|
| **Log-before-respond** | `interaction_log` row is written before the completion is returned to the caller. |
| **Tier non-downgrade** | `knowledge_fact.access_tier` is always the maximum tier of its `source_interaction_ids`. No automated process may lower a fact's tier. |
| **Approved-only retrieval** | Only `knowledge_facts` with `validation_state = approved` are included in embedding search at query time. |
| **Crystallization idempotency** | Running the crystallization pipeline twice on the same set of interaction IDs produces at most one fact (duplicate detected by optimizer). |
| **Regression test idempotency** | Running the regression materialisation step twice on the same interaction produces at most one YAML file (filename is deterministic on `query_hash + created_at`). |
| **Classified response not stored** | When `access_tier = "classified"`, `response_text` must be NULL. `response_hash` is always stored. |
| **Classified never auto-promotes** | Facts with `access_tier = "classified"` are always assigned `trust_path = "red"` by the promotion policy. No automated process may promote a classified-tier fact to community corpus. This is enforced at policy evaluation time, not by convention. |
| **GREEN path target** | At least 90% of community promotions across any two consecutive sweeps must use trust_path `"green"` or `"yellow"`. If the combined green+yellow ratio falls below this threshold for two or more consecutive sweeps, TIDY raises an alert. A sustained high RED ratio indicates a misconfigured confidence threshold or systematic data quality problem, not normal operation. |
| **Review queue ceiling** | The human review queue (`validation_state = "pending_review"`, `trust_path = "red"`) must not exceed 30 items. If the queue reaches 30, TIDY re-evaluates all queued items via the SCAN resolver (Step 5b) before accepting new RED-path items. Items SCAN can now resolve are promoted or deferred; genuinely irresolvable items remain. |
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
