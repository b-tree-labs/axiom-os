# Axiom Memory Reflection — Technical Specification

**Status:** Draft (normative for Axiom 0.17+; **stage 3 of the maturation lifecycle** — see `spec-memory-maturation.md`)
**Owner:** Ben Booth
**Created:** 2026-05-12 (replaces an earlier draft that conflated the lifecycle frame with this stage)
**Authority:** Normative contract for *consolidation* — the derivative operation that produces semantic fragments from episodes. Extensions consume the `ReflectionExtractor` protocol; the platform owns triggering (via the dream cycle), cost capping, federation enforcement, and provenance chains.
**Position in the lifecycle:** Stage 3 (consolidate). Sits between stage 2 (importance scoring) and stage 4 (compaction). Does not own retention or tombstone propagation — those live in `spec-memory-compaction.md`.
**PRD:** `docs/prds/prd-memory.md` (parent — substrate); ADR-033 stage 3.
**Related:**
- `spec-memory-maturation.md` — the umbrella; lifecycle frame this spec slots into
- `spec-memory-compaction.md` — stage 4+, owns tombstone propagation triggered by retraction
- `spec-memory.md` — substrate (write path, MIRIX taxonomy, provenance, replay)
- `prd-cross-tool-memory.md` — MIRIX cognitive-type tagging at write path (cross-4) supplies the producer-side type
- `prd-prompt-registry.md` — synthesis templates live here, versioned + eval-able
- `prd-agents.md` — dream-cycle service runs reflection; SCAN monitors cadence

---

## Quick Start — what 95% of extension authors need

If you are writing a new Axiom extension and the default maturation profile is fine, you need nothing — the platform runs default reflection over every scope.

When you *do* want custom consolidation behavior, register an extractor in your extension manifest:

```toml
[[provides]]
kind = "reflection_extractor"
ref = "my_extension.reflection:default_extractor"
scope_pattern = "my-extension-scope:*"
synthesis_template = "reflection_default_v1"
cadence = "daily"   # daily | weekly | monthly | custom
```

Implement the extractor — return semantic-fragment proposals from a batch of episodes:

```python
from axiom.memory.reflection import ReflectionExtractor, EpisodeBatch, SemanticProposal

class DefaultExtractor(ReflectionExtractor):
    def synthesize(self, batch: EpisodeBatch) -> list[SemanticProposal]:
        result = batch.gateway.complete(
            template_id="reflection_default_v1",
            variables={"episodes": batch.episode_summaries},
        )
        return [
            SemanticProposal(
                summary=insight.text,
                derived_from=[ep.fragment_id for ep in insight.citations],
                confidence=insight.confidence,
            )
            for insight in result.insights
        ]
```

The dream cycle (`spec-memory-maturation.md §6`) calls your extractor at the right cadence; the policy gate (§5 below) decides which proposals become fragments; the platform writes them through `CompositionService.write` with `cognitive_type="semantic"` and `derived_from` chains.

That's the full critical path.

---

## Choose Your Path

| You are building... | Read |
|---|---|
| **An extension that uses default reflection** | Quick Start above. You're done. |
| **An extension that overrides only triggers** | + §3 (Triggers via the dream cycle) |
| **A deterministic (non-LLM) extractor** | + §4.2 |
| **An identity-consolidation extractor** (semantic → `core`) | + §6 (Cadence variants) |
| **Reflection over regulated content** | + §7 (Federation + classification) + `spec-classification-boundary.md` |
| **A consumer reading reflection output** (projection, RAG) | + `spec-memory-maturation.md §7.4` (Replay determinism) |

---

## 1) What this stage owns

Reflection consolidates a batch of episodes into derived semantic facts. Specifically:

- It is **invoked by** the dream-cycle orchestrator (`spec-memory-maturation.md §6`), not by writers directly. Reflection never runs synchronously on the write hot path.
- It **produces** new fragments with `cognitive_type="semantic"` (or `core` for identity cadence) via `CompositionService.write`. Provenance + signing + classification + visibility go through the standard write path.
- It **does not** retract, summarize, or tombstone any source fragment. Those operations live in `spec-memory-compaction.md`.
- It **does not** decide retention windows. Those live in `spec-memory-compaction.md §5`.

Tombstone propagation is *consumed* by reflection (when a source episode is retracted, downstream semantic fragments need re-evaluation — see §10) but *implemented* in the compaction spec.

---

## 2) Core types

```python
# axiom.memory.reflection
@dataclass(frozen=True)
class EpisodeBatch:
    """Input to a ReflectionExtractor. Built by the dream cycle."""
    scope: str
    cadence: Literal["daily", "weekly", "monthly", "custom"]
    episodes: tuple[MemoryFragment, ...]    # ordered oldest → newest
    episode_summaries: tuple[str, ...]      # content.summary projection
    importance: tuple[float, ...]            # 0–10 if scoring enabled (else 0.0)
    accumulated_importance: float
    window_start: str                        # ISO 8601
    window_end: str                          # ISO 8601
    gateway: LLMGateway                      # classification-aware
    template_registry: PromptRegistry
    policy: PolicyCoord
    prior_semantic: tuple[MemoryFragment, ...]  # semantic fragments from prior cycles
                                                 # in this scope (for weekly/monthly cadences)

@dataclass(frozen=True)
class SemanticProposal:
    summary: str
    derived_from: tuple[str, ...]            # source fragment_ids; must be subset of batch
    confidence: float                        # 0–1
    cognitive_type_target: Literal["semantic", "core"] = "semantic"  # "core" only for identity cadence
    classification_override: ClassificationStamp | None = None  # default: max(source classifications)
    visibility_override: VisibilityHorizon | None = None
    extra: dict[str, Any] | None = None
```

The platform owns:
- `CompositionService` invocation (extractors never write directly)
- Provenance chain (`(T, U, A, R)`; agents set includes extractor class + gateway model id)
- Cost accounting (LLM calls, tokens, walltime) per dream-cycle budget
- Federation + classification enforcement (§7)
- Cycle-metric fragments that record what ran

---

## 3) Triggers via the dream cycle

Reflection extractors don't define their own clocks. They declare a **cadence** in their manifest; the dream cycle (`spec-memory-maturation.md §6`) decides when their cadence fires.

| Cadence | Default trigger combination | What it produces |
|---|---|---|
| `daily` | `any_of:[time:24h, importance_threshold:150]` after `cooldown:60s` | Episode → semantic |
| `weekly` | `any_of:[time:7d, count_of_new_semantic:30]` | Semantic → semantic-of-semantic (themes) |
| `monthly` | `time:30d` plus per-scope quality check | Semantic → core (identity), per `spec-memory-maturation.md §5` |
| `custom` | Manifest specifies its own trigger expression (per `spec-memory-maturation.md §6.1`) | Per extractor |

Defaults are tunable per scope via the maturation policy profile (`default | aggressive | conservative | regulated`).

Importance scoring (stage 2 of maturation) is independent: it runs at write time (if enabled) or in a pre-reflection sweep within the dream cycle. The score is consumed by importance-threshold triggers; without it, those triggers can't fire and the platform falls back to time-based.

---

## 4) Synthesis

### 4.1 LLM-driven (default)

The extractor calls `batch.gateway.complete(template_id=..., variables=...)`. The gateway:

- Resolves the template via the prompt registry (versioned, cache-aware per `prd-prompt-registry.md`)
- Enforces classification routing: CUI batches route only to sovereign providers
- Enforces cost cap: per-cycle budget from `spec-memory-maturation.md §6.4`; exceeding it raises `CostExceededError` and the extractor returns an empty list
- Records `(template_id, template_version, model_id, model_temperature, model_seed, prompt_content_hash, response_content_hash)` on every call (audit trail)

Default templates ship in the prompt registry:
- `reflection_default_v1` — Park et al. style: "Given these recent episodes, what 3 insights can you infer? Cite sources by episode id."
- `reflection_weekly_themes_v1` — "Given these recent semantic facts, what 1–3 themes emerge?"
- `identity_consolidation_v1` — "Given these N semantic facts about <subject>, write a single core-identity statement."
- `episode_importance_v1` — for stage 2 importance scoring

Extensions can register their own templates (versioned independently) and bind via the `synthesis_template` manifest field.

### 4.2 Deterministic synthesis variant

`DeterministicReflectionExtractor` is a peer kind. Same contract; `synthesize` is a pure function of `(episodes, importance, template_text)`. Use cases:

- Test fixtures (reflection compliance suite)
- Regulated scopes where an LLM call is undesirable
- Aggregation-style insights ("scope produced N messages in 24h") that don't need generative reasoning

The platform records `extractor_kind = "llm" | "deterministic"` in the audit log. Deterministic replays are byte-identical; LLM replays are not (per `spec-memory-maturation.md §7.4`).

### 4.3 Citation requirement

Every `SemanticProposal.derived_from` MUST be a non-empty subset of `batch.episodes` (or `batch.prior_semantic` for higher cadences). Proposals without citations are rejected at the policy gate (§5).

---

## 5) Policy gate

Before any semantic fragment is written, the platform passes each `SemanticProposal` through `PolicyCoord.evaluate_reflection_proposal`. Rules:

| Rule | Action on violation |
|---|---|
| `derived_from` non-empty and ⊆ batch.episodes ∪ batch.prior_semantic | reject |
| `confidence ≥ scope.min_confidence` (default 0.5) | reject |
| `summary` ≤ 2000 chars | reject |
| Synthesized classification ≤ max(source classifications) | enforced (set to max) |
| Synthesized visibility ≤ max(source visibility) | enforced (set to max) |
| Per-cycle quota: ≤ `reflection.max_semantic_per_window` (default 20) | drop excess by `confidence` desc |
| `cognitive_type_target="core"` requires cadence="monthly" + per-scope identity-consolidation policy | reject |

Rejected proposals get `policy_rejection_reason` in the cycle-metric audit fragment.

---

## 6) Cadence variants

### 6.1 Daily (canonical Park et al.)

- Input: `batch.episodes` from the last 24h (or since the last daily cycle)
- Output: `cognitive_type="semantic"` fragments
- Template: `reflection_default_v1` (or extension override)

### 6.2 Weekly (theme consolidation)

- Input: `batch.prior_semantic` from the last 7d (and possibly `batch.episodes` for context)
- Output: `cognitive_type="semantic"` fragments derived from semantic sources (the reflection-on-reflection case from `spec-memory-maturation.md §13 open question`)
- Depth cap: weekly fragments cannot derive from other weekly fragments (depth-2 max; explicit opt-in for depth-3)

### 6.3 Monthly (identity consolidation)

- Input: `batch.prior_semantic` from the last 30d filtered to subject-stable semantics
- Output: `cognitive_type="core"` fragments — durable identity / preference statements
- Promotion gate: requires ≥ N source semantics across ≥ M days (per-scope policy; default N=3, M=14)
- Supersession: new `core` fragments supersede older ones for the same subject via `content.supersedes = old_fragment_id`

### 6.4 Custom

Extensions can register custom cadences with their own trigger expressions and template bindings. Subject to the same policy gate.

---

## 7) Federation + classification

Derived classification = max(source classifications). Derived visibility = max(source visibility). Both monotonic non-decreasing per `spec-memory-maturation.md §7.3`.

Gateway routing follows `spec-classification-boundary.md`:

| Source classification mix | Allowed providers |
|---|---|
| All `public` | any provider with `provider.public = true` |
| Any `cui` | only providers with `data_flow_capabilities.cui = true` (sovereign only) |
| Any `ear` / `itar` / `part_810` | only sovereign + explicit per-regime capability |

Federation outflow is governed by `spec-federation-policy.md` — derived fragments inherit their sources' federation rules.

---

## 8) Output shape

```python
composition.write(
    content={
        "summary": proposal.summary,
        "derived_from": list(proposal.derived_from),
        "reflection_session_id": batch.session_id,
        "cadence": batch.cadence,
        "extractor": extractor.__qualname__,
        "extractor_kind": "llm" | "deterministic",
        "template_id": template_id,
        "template_version": template_version,
        "model_id": gateway.model_id if extractor_kind == "llm" else None,
        "confidence": proposal.confidence,
        "extra": proposal.extra or {},
    },
    cognitive_type=proposal.cognitive_type_target,  # "semantic" or "core"
    principal_id=batch.scope_principal,
    agents={extractor.__qualname__} | ({gateway.model_id} if extractor_kind == "llm" else set()),
    resources={f"axiom://memory/{uid}" for uid in proposal.derived_from},
)
```

`resources` carries the addressable references to source fragments via the `axiom://` URI scheme (per ADR-027). Projections and RAG consumers can follow them.

---

## 9) `axi memory reflect` — manual surface

```bash
axi memory reflect --scope <s> --cadence daily       # force a daily pass (subject to cooldown)
axi memory reflect --scope <s> --dry-run --cadence daily   # show what would be synthesized
axi memory reflect --replay <fragment>               # re-fetch LLM response for an existing semantic fragment (audit)
axi memory reflect --list-templates                  # show registered synthesis templates per scope
```

Manual fires bypass cooldown only with `--force`. Useful for development + audit.

---

## 10) Interaction with compaction (consumed contract)

`spec-memory-compaction.md` owns retention + tombstone propagation. Reflection consumes one event from it:

- **When a source episode is tombstoned**, compaction notifies via the substrate's tombstone-event channel. Reflection re-evaluates every semantic fragment with `derived_from` referencing the tombstoned episode:
  - **Hard policy** (default): tombstone the derived semantic too (via compaction, not reflection — reflection only flags)
  - **Soft policy** (opt-in per scope): leave the semantic but lower its confidence; re-fire reflection at next cycle to either re-derive (if remaining sources support it) or tombstone

The exact mechanics live in `spec-memory-compaction.md §6`. Reflection's responsibility is to be a *consumer* of that event, not the producer.

---

## 11) Decided + open

**Decided (2026-05-12):**

- Reflection is **stage 3** of the maturation lifecycle. It does not own retention, tombstoning, archival, or storage tiering.
- Three default cadences (daily / weekly / monthly) plus `custom`. Each maps to a default trigger expression and a default template.
- LLM and deterministic extractor kinds are peer. Deterministic guarantees byte-identical replay.
- Citation is mandatory. Empty `derived_from` is a policy-gate rejection.
- Monthly cadence produces `core` fragments via a constrained identity-consolidation pathway. Defaults to N=3 sources over M=14 days.

**Open (decide as impl proceeds):**

- **Weekly depth-3+ semantics.** Depth-2 (semantic → weekly-semantic) is in. Depth-3 (weekly-semantic → biweekly-or-monthly-semantic) requires explicit per-scope opt-in. Decide if depth-3 is even useful.
- **Cached LLM responses for replay strength.** Store the response content hash → response body in audit storage so LLM replays are byte-identical *while the cache is valid*. Tradeoff: storage growth vs replay strength. Decide post-Phase-1 measurement.
- **Reflection-on-stage-2 (importance-only).** Could the importance-scoring stage feed back into reflection sufficiency ("we've scored episodes; here's the importance distribution before we reflect")? Possibly a free win at low cost.

---

## 12) Acceptance + tests

Phase-1 acceptance:
- `DefaultReflectionExtractor` produces a sensible insight from the gold-standard fixture (`tests/memory/fixtures/reflection_park_et_al.json`)
- Daily cadence trigger fires byte-identically across runs at the importance-threshold case
- Deterministic extractor produces byte-identical output across runs
- Citation rejection: proposals with empty `derived_from` are dropped at the policy gate; logged
- Monthly cadence produces a `core` fragment from a fixture of 5 semantic accumulations over 20 simulated days
- Federation: a CUI-scope cycle routes only to a sovereign provider

Phase-2 (post-MVP):
- Weekly theme consolidation surfaces a theme no single daily reflection could
- Cached-response replay matches the original LLM output bit-for-bit

---

## 13) Relationship to adjacent specs

- **`spec-memory-maturation.md`** — parent. Reflection is stage 3 within it. The umbrella owns the dream cycle, budgets, MIRIX promotion semantics, tier transitions.
- **`spec-memory-compaction.md`** — peer stage 4+. Owns tombstone propagation that reflection consumes.
- **`spec-memory.md`** — substrate. `cognitive_type` and `derived_from` schema fields used here are defined there.
- **`prd-prompt-registry.md`** — synthesis templates. `reflection_default_v1`, `reflection_weekly_themes_v1`, `identity_consolidation_v1`, `episode_importance_v1` are platform defaults.
- **`prd-agents.md`** — dream-cycle service owns the orchestration; SCAN monitors cycle lag.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
