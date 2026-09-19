# ADR-035 — LLM-tier policy as a first-class Axiom primitive

**Status:** Proposed (drafted 2026-05-01)
**Drivers:** Benjamin Booth
**Supersedes:** Ad-hoc model references in connect presets and per-extension `axiom-extension.toml` files

## Context

The Axiom platform routes LLM calls across multiple capability tiers
(operational housekeeping, classroom student tutor, deep-dive
research, instructor-side analysis). Today, individual extensions and
connect presets hard-code specific model names (e.g.,
`unsloth/Qwen3.5-122B-A10B-GGUF:Q4_K_M`) in their config. This
creates two problems:

1. **Model landscape evolves rapidly.** New SLMs/LLMs land monthly
   (gemma2 → gemma3, qwen2.5 → qwen3, etc.). Updating specific model
   references across N extensions + presets + tests is friction that
   delays adoption of better models.
2. **Tier semantics are scattered.** Today the meaning of "small
   leaf-node model" is implicit in whichever extension references
   Bonsai-1.7B; "classroom-tier model" is implicit in whichever
   classroom code path uses Qwen 122B. There's no central place to
   ask "what model does the `standard` tier resolve to right now?"

A real example surfaced 2026-05-01 — the bifurcated-leaf-node
proposal:
- Replace **Bonsai 1.7B** (current "smallest footprint" leaf) with
  **gemma2:2b** as default operational/housekeeping model
- Add **qwen2.5:7b** as classroom-tier leaf model (sophisticated
  prompts, e.g., domain student questions, latency target ~3-5s on
  modern laptop)
- Keep **Qwen 122B** on a self-hosted node as federated-cohort tier
- Plus future "smartest" tier: full RAG + reranking pipeline

Without a policy primitive, this single architectural call requires
N file edits across the codebase. With a policy primitive, it's one
declarative file update + a propagated re-resolution.

## Decision

**Introduce `LLMTierPolicy` as a first-class Axiom primitive.**

### Tier vocabulary

The platform exposes named **tiers** with semantic capability
meaning, not specific model names:

| Tier | Semantic | Example use cases |
|---|---|---|
| `simple` | Smallest viable; always-available; minimal footprint | Install ops, TIDY / TRIAGE housekeeping, last-resort fallback, lightweight CLI background tasks |
| `standard` | Mid-capability; sophisticated-prompt-capable; runs locally on modern laptop | Student tutor, classroom Q&A, instructor co-pilot drafting, default classroom mode |
| `smart` | Strong capability + RAG grounded; federated | Deep-dive research mode, classroom escalation when local insufficient, instructor-side analysis |
| `smartest` | Maximum capability + full RAG + reranking | Research loops, cross-cohort analytics, paper-evidence runs |

### Policy declares per-tier resolution

A single declarative policy file (e.g., `axiom/runtime/policy/llm-tiers.toml`)
declares the *current* model resolution for each tier:

```toml
[tier.simple]
model = "gemma2:2b"
provider = "ollama"
endpoint = "http://localhost:11434"
last_validated = "2026-05-01"
footprint_gb = 1.5
target_latency_p50_s = 1.0
target_latency_p95_s = 3.0
fallback = []  # always-available; no fallback needed

[tier.standard]
model = "qwen2.5:7b"
provider = "ollama"
endpoint = "http://localhost:11434"
last_validated = "2026-05-01"
footprint_gb = 4.5
target_latency_p50_s = 3.0
target_latency_p95_s = 8.0
fallback = ["smart", "dumb"]  # if local 7B unavailable, escalate

[tier.smart]
model = "unsloth/Qwen3.5-122B-A10B-GGUF:Q4_K_M"
provider = "llama-server"
endpoint = "https://llm.internal.example:8443/v1"
requires_vpn = true
verify_ssl = false
last_validated = "2026-05-01"
target_latency_p50_s = 8.0
target_latency_p95_s = 20.0
fallback = ["standard"]

[tier.smartest]
# Composite tier: smart + RAG + reranking pipeline
composite_of = "smart"
augmentations = ["rag_full", "reranker", "graph_informed_chunker"]
last_validated = "2026-05-01"

[evaluation_candidates]
# Tracked replacements being evaluated
"tier.simple" = ["gemma3:2b", "qwen2.5:1.5b", "phi-4-mini"]
"tier.standard" = ["qwen3:7b", "gemma3:9b", "llama3.3-8b"]
"tier.smart" = ["qwen3-coder-235b", "gemma3-27b"]
```

### Continuous re-validation

Per `project_validated_classification_pattern` (LM re-validates
static declarations over time), the LLM-tier policy is **a living
decision**:

- `last_validated` field tracks when each tier's model was last reviewed
- `evaluation_candidates` tracks replacements being assessed
- A periodic re-evaluation cadence (proposed: quarterly) compares
  current incumbents vs candidates against a standardized eval
  harness; updates to the policy land via PR with the eval data
  attached
- LM-mediated re-evaluation is a future capability — an Axiom agent
  can scan model-release feeds, generate candidate proposals, and
  surface them to the operator for review

### Consumers reference tier names, not models

All callers (extensions, connect presets, classroom, signal
extractors, TIDY agents, etc.) reference **tier names**:

```toml
# In an extension's axiom-extension.toml:
[[connect.preset.providers]]
kind = "llm"
provider_name = "leaf-classroom"
tier = "standard"  # ← tier name, not model name
routing_tier = "any"
```

```python
# In code:
provider = llm_provider_for_tier("standard")  # resolves via policy
```

The resolution layer maps tier → current incumbent model → endpoint
+ auth + request-shape config.

### Per-student tier overrides remain orthogonal

The per-student tier override (tasks #12 + #14, the unified `axi
classroom policy --student <handle> --llm <tier>` command) operates
at the *tier level*, not the model level. The instructor sets
"Alice's tutor mode = `standard` tier" — she gets whatever the
policy currently resolves `standard` to, automatically.

### Federation-aware

LLM-tier policy is **per-institution by default**, but federation
can share recommendations:

- Each institution's policy is sovereign
- Federation peers can publish their `evaluation_candidates` lists
  to a shared registry — "OSU NE evaluated qwen3:7b for `standard`
  tier, found +12% on rw-* battery"
- Institutions can opt-in to receive federation recommendations as
  candidate sources for their own re-evaluation

## Consequences

### Positive

- **One file changes, the platform follows.** Updating `gemma2:2b`
  → `gemma3:2b` is a single-line policy edit; every consumer
  picks up the change at next resolution.
- **Tier semantics are documented.** Anyone reading the policy file
  knows what each tier means + what model it currently resolves to
  + when it was last validated.
- **Continuous improvement is structural, not heroic.** New model
  evaluation has a home; doesn't depend on someone remembering to
  audit every extension config.
- **Per-student / per-classroom overrides work cleanly.** Tier
  vocabulary is the override surface; resolution happens centrally.
- **Federation knowledge-sharing without lock-in.** Institutions
  share recommendations, not models — sovereign by default.

### Negative

- **One more abstraction layer to maintain.** Resolution machinery
  + policy file + re-evaluation cadence are new responsibilities.
- **Migration cost.** Existing extensions hard-coding model names
  need to migrate to tier references. Phase-0.x work item.
- **Policy file is a single point of failure.** If misconfigured,
  every consumer breaks. Mitigated by validation tests + the
  fallback chain.

### Risks

- **Tier semantics drift.** What "standard" meant in 2026 may not
  match 2027. Mitigation: explicit semantic-capability description
  per tier (above table); re-evaluation cadence enforces alignment.
- **Provider monoculture.** If the policy resolves all tiers to one
  provider (e.g., Ollama), provider downtime cascades. Mitigation:
  explicit fallback chain per tier; federation tier (`smart`) uses
  different infra than leaf tiers (`simple`/`standard`).

## Migration path

1. **2026-05** — Write spec (`spec-llm-tier-policy.md`) +
   skeleton policy file (`runtime/policy/llm-tiers.toml`).
2. **2026-05** — Update `feedback_llm_tier_is_general_knowledge_dial`
   memory to reference the policy. Update
   `project_axiom_node_profiles_adr019` memory with policy-driven
   Edge tier (gemma2:2b default + qwen2.5:7b on classroom join).
3. **2026-05/06** — Implement resolution helper
   (`axiom.policy.llm_tiers.resolve_tier(name)`).
4. **2026-05/06** — Migrate `rag_grounding` extension's connect
   presets to use tier names. Other extensions follow over Phase 0.x.
5. **2026-05** — Bundle gemma2:2b into install path; pull qwen2.5:7b
   on `axi classroom join`. Inference engine: Ollama (Qwen 3 + gemma 2
   well-supported, simple HTTP API).
6. **Pre-Prague (2026-05-15)** — All classroom-relevant code paths
   resolve through the policy.
7. **2026-Q3** — First quarterly re-evaluation cycle; eval harness
   + candidate comparison + policy update PR.

## Open questions

- **Policy file location:** `runtime/policy/llm-tiers.toml`
  (per-installation) vs `~/.config/axiom/llm-tiers.toml` (per-user)
  vs both with override precedence?
- **Re-evaluation harness:** which battery? Day 1 RAG harness +
  CHALKE quiz set + adversarial probes? Standardize before first
  cycle.
- **LM-mediated candidate proposals:** which agent (CURIO? a new
  agent?) owns the periodic scan + proposal-generation work?

## Related

- `axiom/docs/adrs/adr-019-node-profiles.md` — node profiles (this
  ADR refines the per-profile model assumption)
- `axiom/docs/specs/spec-llm-tier-policy.md` (this ADR's spec
  companion)
- `feedback_llm_tier_is_general_knowledge_dial` memory (the original
  pedagogical-lever framing this ADR codifies)
- `project_validated_classification_pattern` memory (the
  re-validation pattern this ADR adopts)
- Tasks #14, #15, #27 (downstream consumers of this policy)
