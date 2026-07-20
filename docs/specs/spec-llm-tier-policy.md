# Spec — LLM-tier policy

**Status:** Drafting (2026-05-01) — companion to ADR-035.
**Owner:** Ben Booth.

## 1. Purpose

Provide a declarative, evolvable mapping from semantic LLM tiers
(`simple` / `standard` / `smart` / `smartest`) to specific model
incumbents — and the operational metadata needed to resolve, route,
fall back, and re-evaluate them over time.

## 2. Tier definitions

| Tier | Semantic capability | Where it runs | Latency target | Footprint |
|---|---|---|---|---|
| `simple` | Always-available, smallest viable, lightweight | Leaf node (laptop) | p50 ≤ 1s, p95 ≤ 3s | < 2 GB |
| `standard` | Sophisticated-prompt-capable, runs on modern laptop | Leaf node (laptop) | p50 ≤ 3s, p95 ≤ 8s | < 6 GB |
| `smart` | Strong capability + RAG grounded | Federated (Server tier; e.g., a self-hosted node) | p50 ≤ 8s, p95 ≤ 20s | Server-side |
| `smartest` | Maximum capability + full RAG + reranking | Federated (Server / Platform tier) | (variable; instructor-side analysis only) | Server-side |

Tier semantics are **stable**; specific model assignments are
**evolvable** (re-evaluated quarterly or on significant model
releases).

## 3. Policy file format

Policy lives at `axiom/runtime/policy/llm-tiers.toml` (per
installation; user-level overrides at
`~/.config/axiom/llm-tiers.toml` take precedence).

### 3.1 Per-tier declaration

```toml
[tier.<name>]
model = "<provider-specific model identifier>"
provider = "ollama" | "llama-server" | "openai-compatible" | "anthropic" | ...
endpoint = "<URL>"
api_key_env = "<env var name>"  # optional
requires_vpn = false
verify_ssl = true
last_validated = "<YYYY-MM-DD>"
footprint_gb = <float>
target_latency_p50_s = <float>
target_latency_p95_s = <float>
fallback = ["<other-tier-name>", ...]  # ordered fallback chain
default_request_params = { temperature = 0.3, max_tokens = 4000, ... }
notes = "<free text — why this incumbent, what alternatives were considered>"
```

### 3.2 Composite tiers

```toml
[tier.smartest]
composite_of = "smart"
augmentations = ["rag_full", "reranker", "graph_informed_chunker"]
last_validated = "<YYYY-MM-DD>"
```

Composite tiers reference a base tier and add augmentations
(retrieval pipeline, reranking, graph-informed chunking, etc.).

### 3.3 Evaluation candidates

```toml
[evaluation_candidates]
"tier.simple" = ["<candidate model 1>", "<candidate model 2>"]
"tier.standard" = ["..."]
```

Tracked replacements being evaluated. Updated by the re-evaluation
cycle (§5).

### 3.4 Federation recommendations (optional)

```toml
[federation_recommendations]
# Other institutions' policy decisions, opt-in source for our
# evaluation_candidates list
sources = ["axiom://federation/policy-feed/osu-ne", ...]
```

## 4. Resolution API

### 4.1 Synchronous resolution

```python
from axiom.policy.llm_tiers import resolve_tier

provider = resolve_tier("standard")
# Returns LLMProvider with current incumbent model + endpoint + auth
# + fallback chain pre-loaded
```

### 4.2 Resolution failure semantics

If the primary incumbent for a tier is unreachable (e.g., local
Ollama not running for `standard`), the resolver:

1. Tries the next tier in the `fallback` chain
2. Returns the first reachable provider
3. Logs the fallback (LangFuse trace + structured log) so operators
   can see when fallbacks are firing

Hard failure (no fallback reachable) raises
`UnresolvableTierError` — caller decides whether to refuse
(student-facing tutor) or degrade silently (background
housekeeping).

### 4.3 Per-student override resolution

```python
provider = resolve_tier_for_student(
    "standard",
    student_handle="@alice:prague",
    classroom_id="...",
)
# Looks up per-student override → falls back to cohort default →
# falls back to platform default (the bare `tier.standard` declaration)
```

The per-student override (tasks #12 + #14) operates at the *tier
level*; resolution happens centrally.

## 5. Re-evaluation cadence

### 5.1 Schedule

- **Quarterly** baseline review (Q1 / Q2 / Q3 / Q4)
- **Ad-hoc** when a significant model release happens (e.g., qwen3,
  gemma3, claude-5)
- **On-demand** when an institutional sponsor requests evaluation

### 5.2 Protocol

Each re-evaluation cycle:

1. Run the standardized eval harness (see §6) against current
   incumbents AND each `evaluation_candidates` entry per tier
2. Compare on: factuality, citation accuracy, refusal quality,
   latency, footprint, cost
3. If a candidate dominates the incumbent on N-of-M dimensions
   (instructor-controlled threshold), draft a policy-update PR
4. PR includes: eval data, regression analysis, footprint delta,
   reviewer assignment
5. Merge → `last_validated` field updates

### 5.3 LM-mediated proposal generation (future)

A future Axiom agent (CURIO or a new agent) scans model-release
feeds (Hugging Face, Ollama registry, vendor announcements),
generates candidate proposals, and surfaces them to the operator —
adopting the `project_validated_classification_pattern` for living
declarations.

## 6. Standardized eval harness

The Day 1 RAG harness (`axiom/docs/working/visual-journeys/day1-rag-harness/`)
serves as the seed eval harness. Re-evaluations run:

- Should-RAG-win battery (rw-*) — corpus-specific factual recall
- Should-be-wash battery (wash-*) — general-knowledge baseline
- Out-of-corpus battery (oc-*) — refusal quality + adversarial
- Latency p50/p95 measurements per tier
- Footprint validation
- Cost-per-question (where applicable)

Output is a per-tier scorecard the policy-update reviewer consults.

## 7. Validation tests

Policy file is validated at install/load time:

- Every referenced tier name resolves
- Every `fallback` chain is acyclic and bottoms out at a tier with
  no fallback
- Every endpoint URL is well-formed
- Every `last_validated` is < 6 months old (warn if older; error if
  > 12 months)
- Every model name resolves at the provider (e.g., `ollama list`
  contains it; if Ollama not installed, warn for leaf tiers)

## 8. Migration from current state

Pre-ADR-035 state:
- `rag_grounding/axiom-extension.toml` connect presets hard-code
  Qwen-122B model name
- `axi infra` install path doesn't bundle any leaf-node LLM
- Various per-extension `axiom-extension.toml` reference Bonsai

Migration plan (per ADR-035 §Migration path):
1. Land policy file (this spec) + resolver helper
2. Migrate `rag_grounding` presets to tier-name references
3. Update `axi infra` install to bundle gemma2:2b via Ollama
4. Update `axi classroom join` to pull qwen2.5:7b on classroom join
5. Migrate other extensions over Phase 0.x

## 9. Open spec questions

- **Policy file precedence**: per-installation vs per-user vs both?
- **Cross-platform model identifiers**: Ollama uses `gemma2:2b`;
  llama-server uses `unsloth/...:Q4_K_M`; commercial APIs use
  vendor-specific names. Spec should normalize where possible.
- **Provider authentication**: spec assumes API keys via env vars;
  should there be a centralized credential manager reference?
- **Schema versioning**: how do we version this policy file format
  itself for forward-compat?

## 10. Cross-references

- ADR-035 (decision)
- ADR-019 node profiles (which tier each profile defaults to)
- `feedback_llm_tier_is_general_knowledge_dial` memory
- `project_validated_classification_pattern` memory
- Day 1 RAG harness (the standardized eval seed)
