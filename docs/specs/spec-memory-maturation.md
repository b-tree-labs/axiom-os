# Axiom Memory Maturation — Technical Specification

**Status:** Draft (normative for Axiom 0.17+; introduces the lifecycle frame within which `spec-memory-reflection.md` and `spec-memory-compaction.md` operate as stage-specific instances)
**Owner:** Ben Booth
**Created:** 2026-05-12
**Authority:** Normative contract for how memory fragments *change state over time*. Extensions consume the lifecycle stages + dream-cycle orchestrator; the platform owns transitions, provenance preservation through compression, storage tiering, federation enforcement.
**PRD:** `docs/prds/prd-memory.md` (parent — substrate)
**Related:**
- `spec-memory.md` — substrate contract (write path, MIRIX taxonomy, provenance, replay)
- `spec-memory-reflection.md` — daily consolidation stage (the "dreaming" pass)
- `spec-memory-compaction.md` — compression stage (lossy reduction + retention)
- `prd-cross-tool-memory.md` — MIRIX cognitive-type tagging at write path (cross-4) — supplies the maturation backbone
- `prd-prompt-registry.md` — stage-specific synthesis templates live here
- `prd-agents.md` — TIDY runs compaction; CURIO/agents trigger reflection; SCAN monitors stage lag
- ADR-026 (ownership), ADR-027 (federated memory), ADR-033 (layered memory architecture stages)

---

## Quick Start — what 95% of extension authors need

If you are writing a new Axiom extension and "just want memory to age gracefully," you usually need nothing. The platform runs maturation for every scope by default.

When you *do* need to customize, the three knobs:

**1. Pick a maturation policy in your extension manifest:**

```toml
[[provides]]
kind = "maturation_policy"
scope_pattern = "my-extension-scope:*"
profile = "default"   # default | aggressive | conservative | custom
```

The `default` profile runs: importance scoring at write, daily dreaming pass (episode → semantic), weekly compaction (summarize episodes older than 7d), monthly archival (move cold), retention windows per classification.

**2. Override at the stage level (only the stages that matter to you):**

```toml
[maturation."my-extension-scope:*".consolidation]
trigger = "any_of:[time:1h,importance_threshold:80]"  # more frequent than default

[maturation."my-extension-scope:*".compaction]
disabled = true   # never compact this scope (audit-critical)

[maturation."my-extension-scope:*".retention]
episodic = "90d"
semantic = "5y"
core     = "indefinite"
```

**3. The dream cycle takes it from there.** A scheduled service (or low-activity trigger) walks every scope, runs whichever stages are due, respects budgets, writes through `CompositionService`, preserves provenance, propagates tombstones.

That's the full critical path.

If you want more (custom consolidation extractors, multi-tier storage backends, federation-aware compaction, retention proofs for audit), use the navigator below.

---

## Choose Your Path — which sections do I need to read

| You are building... | Read |
|---|---|
| **An extension that uses memory normally** | Quick Start above. You're done. |
| **An extension with custom consolidation logic** | + `spec-memory-reflection.md` |
| **An extension that compacts/retires fragments specially** | + `spec-memory-compaction.md` |
| **A regulated extension** (CUI / EAR / ITAR / Part 810) | + §10 (federation + retention enforcement) + `spec-classification-boundary.md` |
| **A consumer of MIRIX-typed semantic facts** | + §5 (cognitive-type promotion) + §6 (the dream cycle) |
| **Multi-host replication or external-archive storage** | + §9 (storage tiering) |

---

## 1) Why memory must mature

Three forcing constraints make consolidation non-optional:

1. **Cost grows linearly with episode count.** Raw episodic memory at the substrate level scales with time. Retrieval over a year of episodes is ~50× the work of retrieval over a week. Without consolidation, every query re-runs the same pattern-discovery from raw data.
2. **Signal-to-noise degrades.** A year of episodic memory holds roughly the same number of *useful facts* as a week, buried in 50× the noise. Without abstraction, the system can't tell what's load-bearing.
3. **Retention is real.** Storage isn't infinite; some content has retention windows (CUI/EAR/ITAR expiry; user-deletion requests); some has legal forget-after-N dates. Choosing what to keep requires having already *known what was important* — which requires having consolidated it first.

The differentiation point: **most agent-memory systems treat consolidation as a single cron job; the Axiom substrate models it as a coherent multi-stage lifecycle that operates across timescales from seconds to years.**

---

## 2) Two operations, often confused

The maturation lifecycle is built from two distinct operations:

| Operation | What it does | Output | Lossiness |
|---|---|---|---|
| **Consolidation** (derivative) | Produces *new* fragments at higher abstraction. Episodic → semantic; semantic → semantic-of-semantic; many semantic → core (identity). | Adds fragments with `content.derived_from = [source_uuid, ...]` provenance chain | Lossless w.r.t. provenance (sources still exist); abstractive w.r.t. content (the derived fragment summarizes) |
| **Compaction** (compressive) | *Reduces* existing fragments. Full-detail episode → summary episode → tombstone. Replaces or removes. | Mutates `content` to a shorter form, then eventually tombstones | Lossy by design — once compacted, the original detail is gone (or retrievable only from cold archive) |

These compose:

```
       consolidation                compaction
episodic ───────────► semantic + episodic ───────────► semantic + summarized episodic
                                                                          │
                                                                          │ compaction (further)
                                                                          ▼
                                                                  semantic + tombstoned
                                                                  (semantic survives; episode gone)
```

**Invariant**: compaction never tombstones an episode while semantic fragments still cite it, *unless* the operator explicitly accepts breaking the audit chain. The default policy refuses to break chains.

`spec-memory-reflection.md` owns consolidation. `spec-memory-compaction.md` owns compaction.

---

## 3) The six maturation stages

Memory passes through six stages over its lifetime:

| Stage | Timescale | What happens | Storage tier |
|---|---|---|---|
| **1. Capture** | μs–ms | Fragment written via `CompositionService.write`. Provenance + signature + classification + visibility set. `cognitive_type` typically `episodic`. | Hot (working SQLite) |
| **2. Score** | seconds–minutes | Optional importance scoring (0–10) at write time or shortly after. Light LLM or deterministic. Stored in `content.importance`. | Hot |
| **3. Consolidate** | hours–days | Reflection: episode → semantic synthesis via the dream cycle. Park et al. style for the daily pass; lighter heuristics for sub-daily; theme-level for weekly. Produces `cognitive_type="semantic"` fragments with `derived_from` chains. | Hot |
| **4. Compact** | days–weeks | Episodes summarized once their consolidation-derived semantic is in the ledger. Lossy reduction of original content; original kept in cold archive if retention requires it. | Hot → warm |
| **5. Archive** | weeks–months | Cold fragments move to slower-tier storage (object store; gzipped batches). Still addressable via `axiom://` URI; query path goes through projection cache + on-demand fetch. | Warm → cold |
| **6. Forget** | months–years (or per retention policy) | Tombstone records (audit-preserved) under retention rules. Cryptographic erasure for keyed encryption-at-rest. Provenance chain marks fragments as "tombstoned" so projections recompute. | Cold → none |

Each stage has its own triggers (§6), policies (§7), and ownership (§11). Stages 1–2 happen synchronously at write; stages 3–6 are asynchronous, batch-oriented, and run inside the dream cycle.

---

## 4) Multi-scale timing

Different stages fire at different cadences. The platform coordinates so faster-scale work doesn't fight slower-scale work.

| Cadence | What fires | Why this scale |
|---|---|---|
| **Sub-second** | Capture, score (if synchronous) | Per write event |
| **Hourly** | Importance scoring catch-up; near-duplicate collapse in working buffer; thumbnail summaries | Light cleanup, doesn't block reads |
| **Daily** ("dreaming") | Reflection (episode → semantic); first-pass compaction (summarize episodes > 24h old that have semantic derivations); SCAN reports stage lag | The canonical heavy pass. Aligns with biological consolidation analogue. |
| **Weekly** | Semantic theme consolidation (semantic → semantic-of-semantic); deeper compaction; classification re-evaluation | Aggregation across days surfaces themes individual days miss |
| **Monthly** | Identity-level consolidation (many semantic → `core`); archival to cold storage; retention policy enforcement window-1 | Identity/preferences emerge from sustained patterns |
| **Quarterly / yearly** | Long-archival rotation; retention enforcement window-N; classification expiry; legal-forget execution | Compliance, storage cost, and long-term forgetting |

Stages can be **partial within a cadence** — the orchestrator decides based on backlog. If the daily dream pass falls behind, the weekly one runs first on whatever's caught up.

---

## 5) MIRIX cognitive-type promotion semantics

The MIRIX 6-type taxonomy (`core / episodic / semantic / procedural / resource / vault`) is the maturation backbone. Consolidation moves fragments *along* the type axis; compaction moves them *along* the detail axis.

Allowed promotions (each requires explicit `derived_from`):

| From | To | Mechanism | Required |
|---|---|---|---|
| `episodic` | `semantic` | Reflection (daily dreaming) | ≥ 1 episode source |
| `semantic` | `semantic` | Reflection-on-reflection (weekly themes) | ≥ 2 semantic sources |
| `semantic` | `core` | Identity consolidation (monthly) | ≥ N semantic sources over ≥ M days, per scope policy |
| any | `vault` | Lateral move when post-hoc classification escalates | Classification stamp change |
| any | (tombstoned) | Compaction / forget | Retention policy or operator request |

**Forbidden promotions**:
- `core` → anything else (identity is terminal; if you're wrong about identity, write a new `core` fragment that supersedes and tombstone the old)
- `procedural` is hand-curated typically; auto-promotion needs explicit per-scope opt-in
- `resource` is for blob/file references; not subject to auto-promotion

This matches `spec-memory.md §3.2` MIRIX semantics and `prd-cross-tool-memory.md` cross-4 (cognitive-type tagging at write path).

---

## 6) The dream cycle

Most consolidation + compaction happens during low-activity windows — the "dreaming" pattern. The dream cycle is the orchestrator that runs the right stages at the right cadence.

### 6.1 Triggers

- **Low-activity inactivity**: no writes in a scope for ≥ `dream.inactivity_threshold` (default 5 min) and dream-eligible work in backlog
- **Scheduled**: hourly / daily / weekly / monthly cron entries (configurable per host)
- **Manual**: `axi memory dream --scope <s>` (forces a cycle subject to budget)
- **Catch-up**: if a scheduled cycle was missed (host offline), the next opportunity runs catch-up

### 6.2 Cycle structure

```
dream_cycle(scope):
    if budget.exceeded(scope):
        log("scope %s over daily budget; skipping", scope); return
    if scope.has_pending(importance_scoring):
        run importance_scoring (per spec-memory-reflection §4.2)
    if scope.has_pending(consolidation, cadence="daily"):
        run reflection.daily(scope)
    if scope.has_pending(consolidation, cadence="weekly"):
        run reflection.weekly(scope)
    if scope.has_pending(consolidation, cadence="monthly"):
        run reflection.identity(scope)
    if scope.has_pending(compaction, cadence="any"):
        run compaction(scope)
    if scope.has_pending(archival):
        run archival(scope)
    if scope.has_pending(retention):
        run retention_sweep(scope)
    record(cycle_metrics, scope)
```

The orchestrator skips stages whose triggers haven't fired, respects per-scope budgets, and writes structured cycle-metric fragments (themselves `cognitive_type="episodic"`, source `dream-cycle`) so SCAN can monitor stage lag.

### 6.3 Per-host scheduling

By default the dream cycle runs as a `service` capability owned by the memory extension. Hosts can override:
- Disable entirely (`memory.dream.enabled = false`) — useful for ephemeral CI agents
- Inactivity-only (no scheduled fires) — useful for laptops where idle is the natural trigger
- Aggressive (every 15 min) — useful for high-throughput environments

### 6.4 Cycle budgets

Per-scope per-day:
- `dream.budget_tokens` — LLM cost cap
- `dream.budget_calls` — LLM call count cap
- `dream.budget_walltime_seconds` — total work per cycle

When budget is hit, the cycle stops cleanly at the next stage boundary. State is preserved so the next eligible cycle resumes.

---

## 7) Cross-stage invariants

### 7.1 Provenance preservation through compaction

When compaction summarizes an episode, the summary fragment carries:
- `content.compacted_from = original_fragment_id`
- `content.compaction_version` (the rule set used)
- `content.original_length` (size of what was reduced)

When the summary is itself eventually tombstoned, the tombstone references both the summary and any chain back to the original. The audit chain survives even after the content is gone.

### 7.2 Tombstone propagation through `derived_from`

`spec-memory-reflection.md §11` and `spec-memory-compaction.md §6`:

- Tombstoning a source episode triggers re-evaluation of every semantic fragment with `derived_from` referencing it
- Hard policy (default): tombstone all derived semantics
- Soft policy: mark them with reduced confidence; tombstone only when all sources gone

Compaction respects derivation: an episode is not compacted-to-tombstone while any active semantic fragment cites it, unless the operator accepts that the audit chain will lose its source.

### 7.3 Federation + classification stability through maturation

Classification is **monotonic non-decreasing**: maturation cannot *decrease* a fragment's classification.

- A `cui` episode that consolidates into a semantic fragment produces a `cui` semantic
- A `cui` episode that gets compacted-to-summary produces a `cui` summary
- A `cui` fragment that gets archived to cold storage stays `cui`; cold storage must satisfy `cui` requirements

Visibility moves the same way: maturation never widens visibility without an explicit operator override.

### 7.4 Replay determinism after maturation

Per `spec-memory.md §6.3`, projections are pure functions of `(events, graph, task)`. Maturation changes the event stream over time (compacts episodes, adds semantics, tombstones). Replay determinism is then:

- **Within a fragment-set state**: `project(state, task)` byte-identical
- **Across maturation states**: `project(state_t1, task) ≠ project(state_t2, task)` is expected and correct; what matters is that *each individual state* replays identically
- The audit log records every maturation transition so any state in history is reproducible from the log

---

## 8) Per-extension maturation policies

Manifest declaration (Quick Start). Discovery via `axi ext` registry at extension load. Profiles:

| Profile | Importance scoring | Consolidation | Compaction | Archival | Retention |
|---|---|---|---|---|---|
| `default` | opt-in (off) | daily + weekly + monthly | summarize > 7d; tombstone > 30d (consolidated) | move > 30d to warm; > 365d to cold | indefinite for `core`; classification-driven else |
| `aggressive` | on | daily + weekly + monthly; lower thresholds | summarize > 1d; tombstone > 7d | move > 7d warm; > 30d cold | shorter windows |
| `conservative` | off | weekly only | summarize > 90d; never tombstone | move > 365d cold only | indefinite default; explicit forget only |
| `regulated` | on (deterministic) | weekly + monthly; sovereign LLM only | summarize > 365d if retention allows; per-regime windows | encrypted cold storage | classification + per-regime expiry enforced |
| `custom` | manifest-driven | manifest-driven | manifest-driven | manifest-driven | manifest-driven |

A scope can use one profile globally or override per stage. The `custom` profile requires every stage to be configured explicitly.

---

## 9) Storage tiering

Three tiers, each addressable via `axiom://` URI:

| Tier | Backend | Latency target | What lives here |
|---|---|---|---|
| **Hot** | SQLite `artifacts.db` + in-memory cache | < 10 ms p95 | Recently written, recently read, semantic + core, active episodics |
| **Warm** | Rotated SQLite shards or single-file projection cache | < 100 ms p95 | Episodics 1–4 weeks old; compacted summaries; superseded semantics |
| **Cold** | Object store (SeaweedFS / S3-compatible / encrypted blob); gzipped batches | < 5 s p95 | Episodics > 4 weeks; archival semantics; retention-pending fragments |

Tier transitions are part of the maturation lifecycle (stage 5 + 6). A fragment's tier is metadata in the ledger; clients use `axiom://` URIs that resolve transparently. Cold-fetch is allowed but throttled to avoid latency spikes.

For air-gapped deployments, all three tiers are local (cold = encrypted local archive). For Edge profile, warm and cold may both collapse onto a larger SQLite shard.

---

## 10) Federation + retention enforcement

For regulated deployments:

- **CUI / EAR / ITAR / Part 810** fragments have per-regime retention windows. The retention sweep at stage 6 enforces them. Violations are logged as compliance defects.
- **Federation outflow** during archival respects the federation gateway. Cold storage may be a peer cohort node — federation rules apply.
- **Sovereign LLM** is required for any consolidation that touches a regulated fragment (per `spec-classification-boundary.md` `data_flow_capabilities`).
- **Cryptographic erasure**: where keyed encryption-at-rest is used, "forget" can be achieved by destroying the key for that scope-window, even if the ciphertext remains. Per ADR-026 ownership.

WARDEN (Vega federation agent) verifies retention enforcement on cohort-level audits.

---

## 11) Ownership: who runs what

Maturation is multi-agent by design. From `prd-agents.md`:

| Stage | Owning agent | Responsibility |
|---|---|---|
| Capture, Score | The producing extension + L0 write path | Synchronous |
| Consolidate (reflection) | The extension's `ReflectionExtractor` (or platform default), orchestrated by the dream-cycle service | Async, scheduled |
| Compact | TIDY (hygiene agent) | Async, hygiene cadence |
| Archive | TIDY + storage backend driver | Async |
| Forget (retention) | TIDY + WARDEN (compliance check) | Async, audit-logged |
| Monitor | SCAN | Continuous (heartbeat freshness, stage lag) |
| Escalate | TRIAGE | When stages fall behind or hit budget caps |

Operators don't invoke these directly except via `axi memory <verb>` (manual override surface).

---

## 12) `axi memory` maturation verbs

```bash
axi memory dream --scope <s>                 # force a cycle for one scope
axi memory dream --all                       # force cycles across all scopes (subject to budgets)
axi memory dream --dry-run --scope <s>       # show what would run, write nothing

axi memory status --scope <s>                # per-stage lag + budget consumed
axi memory status --all --json               # machine-readable

axi memory compact --scope <s> --age 7d      # one-shot compaction sweep
axi memory archive --scope <s> --age 30d     # one-shot archival
axi memory retention --scope <s> --apply     # one-shot retention sweep (default dry-run)

axi memory promote <fragment> --to semantic  # operator-driven promotion (audited)
axi memory tombstone <fragment>              # explicit retraction
```

All maturation verbs respect the policy gate and are audit-logged.

---

## 13) Decided + open questions

**Decided (2026-05-12):**

- **Two-operation model.** Consolidation (derivative, lossless w.r.t. provenance) and compaction (compressive, lossy by design) are distinct subsystems with their own specs. They compose strictly: consolidation runs before compaction can remove the consolidated-from sources.
- **MIRIX as the maturation backbone.** Promotion follows the MIRIX axis (episodic → semantic → core), with `derived_from` chains preserved at every promotion.
- **Dream cycle as the unified orchestrator.** Stages don't run independently; the orchestrator coordinates them per scope per cadence per budget. This avoids fast-scale work fighting slow-scale work.
- **Storage tiering is part of maturation.** Hot/warm/cold transitions are stages 5–6, not a separate concern. `axiom://` URIs resolve transparently across tiers.
- **Classification monotonicity.** Maturation cannot decrease classification or widen visibility. Retention reduces them only via the tombstone (forget) path.
- **Per-scope policy profiles.** `default | aggressive | conservative | regulated | custom`. Each maps to a defined set of stage triggers + retention windows.

**Open (decide as the stage-specific specs commit):**

- **Reflection-on-reflection depth cap.** §reflection. Default position: depth-2 (weekly themes), depth-3 only with explicit opt-in.
- **Identity consolidation rules.** §reflection — how many semantic sources over how many days qualifies a `semantic → core` promotion? Default 3 sources / 14 days; tunable.
- **Compaction reversibility from cold.** §compaction — should compacted-then-archived episodes be re-inflatable from cold storage for audit? Cost-vs-fidelity tradeoff; per-regime policy.
- **Cycle preemption rules.** When a high-activity write storm interrupts a running dream cycle, does it abort cleanly or finish the current stage first? Default position: finish current stage, suspend rest.
- **Cold-tier replication topology.** Single cold backend vs replicated cohort cold? Default: single backend; replication a per-scope opt-in via federation.

---

## 14) Acceptance + tests

Phase-1 acceptance (this spec + reflection + compaction Phase-1):
- Dream cycle runs end-to-end on a fixture scope: importance scoring → reflection → compaction → archival → retention check (each stage produces expected counts within budget)
- MIRIX promotion provenance: `derived_from` chain back to original episodes survives a full daily → weekly → monthly cycle
- Classification monotonicity: regression suite asserts maturation never reduces a fragment's classification
- Tombstone propagation: retracting a source episode triggers semantic re-evaluation within one cycle
- Storage tier transitions visible via `axi memory status` + reflected in `axiom://` URI resolution

Phase-2 acceptance (post-MVP):
- Identity consolidation produces a sensible `core` fragment from a fixture of semantic accumulations over 14 simulated days
- Cycle preemption + resume works across host restart
- Cold-tier round-trip: write → archive → cold-fetch → bytes match
- Federation: a cohort-peer dream cycle on a CUI scope only routes to sovereign LLMs

---

## 15) Relationship to adjacent specs

- **`spec-memory.md`** — substrate. This spec strictly extends it: adds stage semantics + cognitive-type promotion rules + tier metadata. No breaking changes to fragment shape.
- **`spec-memory-reflection.md`** — stage 3 (consolidate). The daily dreaming pass. Owns LLM-driven and deterministic synthesis variants.
- **`spec-memory-compaction.md`** — stage 4 + 5 + 6 (compact, archive, forget). The compressive operations + retention enforcement.
- **`prd-prompt-registry.md`** — synthesis templates for consolidation stages live here. Reflection templates, importance-scoring templates, identity-consolidation templates.
- **`prd-agents.md`** — agent ownership of stages (see §11). TIDY for compaction/archive/forget; reflection-extractor + dream-cycle service for consolidation; SCAN for monitoring; TRIAGE for escalation.
- **ADR-033 Stage 3+** — projections consume fragments at every maturation stage equally. The dream cycle is what produces the rich fragments projections rely on at scale.
- **ADR-027 (federated memory)** — federation respects maturation: cohort peers can disagree on stage progress (different cycle schedules) but the substrate (provenance, classification, addressing) is shared.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
