# Memory Maturity & Bounding — Unified Pipeline Across All Memory

**Status:** Proposed (2026-04-28)
**Owner:** Ben Booth
**Layer:** Axiom core
**Supersedes scope:** Generalizes `spec-rag-knowledge-maturity.md` (which remains the chunk-specific reference) to all memory fragments per ADR-033.
**Related:** `adr-033-layered-memory-architecture.md`, `spec-memory.md`, `spec-rag-knowledge-maturity.md`, `spec-federation-policy.md`, `working/plan-agent-modes-analysis.md` §7.8, `working/memory-persistence-plan.md`

---

## 1. Why this spec exists

`spec-rag-knowledge-maturity.md` introduced a 6-layer maturity pipeline (Layer 0 Data → Layer 5 Wisdom) for RAG corpus content: chunks promote as evidence accumulates, validated facts crystallize from interaction patterns, and regression failures materialize as eval cases. That pipeline assumed chunks were the unit of memory.

ADR-033 changed the unit of memory. Every cognitive type (`core`, `episodic`, `semantic`, `procedural`, `resource`, `vault`) lands as a `MemoryFragment` in a single layered substrate (event log + concept graph + projections + federation policy). Plan + agent modes per ADR-034 will multiply fragment write volume by 50× or more — every plan derivation, every plan-step result, every tool call, every interrupt, every agent thought.

That aggression is only safe if memory is **bounded, rolled, and matured** as a discipline. Two failure modes this spec rules out:

- **Unbounded growth.** Plan + agent modes that write fragments forever, with no compaction, no archival, no retention policy → disks fill, projections slow, the maturity discipline drifts to RAG-only and the rest is unmanaged.
- **Maturity by extension.** Each extension implements its own promotion / forgetting / archival rules → policy drift, federation incompatibility, no cross-cohort durability guarantees.

This spec generalizes the §1 `spec-rag-knowledge-maturity` pipeline to all memory and adds the bounding mechanisms (tiered retention, TIDY sweep, cohort archival, classification expiry) that scale-grade memory requires.

## 2. Maturity layers — generalized

The same six layers, generalized from chunks to fragments. `spec-rag-knowledge-maturity.md` §2 remains authoritative for chunk-specific behavior; this section specifies the broader contract.

| Layer | Name | Generalized definition | Producer | Promotion trigger |
|-------|------|-----------------------|----------|-------------------|
| **L0** | Data | Raw indexed fragments. Includes raw chunks, agent-run events, plan derivations, tool outputs, conversational fragments, signal events. | Ingest pipeline + every CompositionService write. | Automatic on write. |
| **L1** | Patterns | Retrieval / activity patterns mined from L0 events. Includes RAG retrieval frequency, plan-step recurrence, tool-call frequency, interaction clusters. | TIDY sweep. | Frequency / recency thresholds (configurable per cognitive type). |
| **L2** | Facts | Validated knowledge derived from L1 patterns + L0 evidence. **Plan-step proofs (per ADR-034) are first-class L2 candidates** — a verified step's output, with its proof artifact, naturally promotes to a fact. | SCAN crystallization + human review **+ proof verification**. | Promotion policy + human approval **OR** machine-verified proof spec satisfied. |
| **L3** | Frameworks | Cross-domain synthesised mental models. Includes CURIO cross-domain synthesis + concept-graph cluster generalizations. | CURIO cross-domain synthesis + human curation. | CURIO monthly synthesis + approval. |
| **L4** | Application | Validated applied procedures + plan templates. **Reusable plans (per ADR-034) live here when their replay envelope reproduces consistently.** | Human authorship + CURIO research assist + plan-template promotion. | Human approval required. |
| **L5** | Wisdom | Accumulated heuristics from long-term federated patterns. | CURIO federated research patterns. | v2+. |

**Two new sources of L2 facts post-ADR-034:**

1. **Plan-step proofs.** A `PlanStep` whose `proof` was satisfied (test passed, typecheck passed, structural match, retrieval cite-set, peer attestation, deterministic replay) is a *machine-verified fact* — its output + the proof artifact promote to L2 without human review for proof types that are inherently rigorous (test/typecheck/replay). Human-attestation-style proofs still require review.
2. **Concept-graph cluster facts.** Concepts in the L2 graph (per ADR-033 Layer 2) that have N+ supporting fragments across M+ scopes within a recency window promote to L2 facts in the maturity sense. Bridges the graph-layer concept primitive and the maturity-layer fact primitive.

## 3. Tiered retention — what stays where, for how long

Bounding mechanism #1: not all L0 fragments live forever in hot storage.

```
HOT (default)            WARM                    COLD                    ARCHIVE
    |                      |                       |                       |
    | <30d (configurable)  | 30d–1yr               | 1yr–<retention_max>   | retention_max+
    |                      |                       |                       |
    | full content + index | full content,         | content tombstoned,   | content + provenance
    | full retrieval       | reduced index         | provenance preserved  | only; no retrieval
```

| Tier | Storage | Retrieval cost | What lives here |
|---|---|---|---|
| **HOT** | SQLite primary, full FTS | µs–ms | Recent fragments (< 30d default); all L1+ derivatives indefinitely. |
| **WARM** | SQLite secondary or compressed page | ms | Older L0 fragments still actively cited by L1+. Index reduced (FTS dropped; metadata kept). |
| **COLD** | Tombstoned in-place (content blob removed; provenance + headers preserved) | Tombstone return only | L0 fragments older than warm window with no L1+ derivative referring. Compaction candidate. |
| **ARCHIVE** | Cohort-archive bundle (per §6); read-only signed snapshot | Ceremony-grade only | Beyond `retention_max`; preserved for audit; not part of live projections. |

**Defaults per node profile** (per ADR-019):

| Profile | Hot window | Warm window | Cold window | Archive trigger |
|---|---|---|---|---|
| Edge | 7d | 30d | 90d | 90d |
| Workstation | 30d | 180d | 1yr | 1yr |
| Server | 90d | 1yr | 5yr | 5yr |
| Platform | 1yr | 5yr | indefinite | cohort end-of-life |

These are starting points, *not* commitments — Prague's first cohort will surface the right values empirically. Override via `[memory.retention.<profile>]` in `models.toml` or scope-specific config.

## 4. TIDY sweep — periodic compaction

Bounding mechanism #2: TIDY (per `prd-agents.md`) periodically:

1. **Promotes** L0 events meeting L1 thresholds (interaction frequency, retrieval count) to L1 records.
2. **Identifies cold candidates** — L0 fragments outside the warm window with no L1+ derivative.
3. **Tombstones cold fragments** — replaces content blob with `tombstone:reason=cold_compaction`; preserves provenance + IDs.
4. **Crystallizes L1 patterns** matching L2 promotion thresholds via SCAN pipeline.
5. **Audits classification expiry** (§7) — fragments past their declass date have classification stamp updated.

The sweep is **idempotent + replayable**: re-running it produces identical state. Sweep events are themselves L0 fragments (audit trail of every compaction action).

**Schedule:**
- Edge / Workstation: nightly during idle.
- Server: hourly during low-traffic windows.
- Platform: cohort-coordinator-scheduled.

CLI surface: `axi memory sweep [--scope <id>] [--dry-run]`.

## 5. Promotion gates — what's required to advance a layer

| Transition | Gate | Notes |
|---|---|---|
| L0 → L1 | Frequency threshold (configurable) | Worked example: a query pattern retrieved 5+ times in 30d promotes. |
| L1 → L2 (RAG facts) | SCAN crystallization + human review | Per existing `spec-rag-knowledge-maturity.md` §7. |
| L1 → L2 (plan-step proofs) | Proof spec satisfied | Test/typecheck/replay → automatic. Attestation/null → human review. |
| L1 → L2 (concept facts) | Cross-scope evidence threshold | N supporters across M scopes within recency window. |
| L2 → L3 | CURIO synthesis + curator approval | Cross-domain framework formation. |
| L3 → L4 | Human authorship review | Plan templates and procedures. |
| L4 → L5 | v2+ | Federated wisdom mining (post-Prague). |

Gates are recorded as L0 events: `PromotionEvaluated`, `PromotionApproved`, `PromotionRejected` with full rationale. Replayable per ADR-033.

## 6. Cohort archival — when a cohort's lifetime ends

Bounding mechanism #3: cohorts have lifetimes.

Prague Summer 2026's cohort ends when the class concludes. Its memory does *not* disappear — it transitions to a **read-only signed archive bundle**:

1. Coordinator initiates `axi cohort archive <cohort_id>` (ceremony-grade; signed by coordinator + co-signers).
2. TIDY sweep runs to its terminal idempotent state.
3. All L0–L5 content is exported to `cohort-<id>-archive.tar.zst`, signed, and emitted with a manifest of fragment counts + classification statistics + maturity-layer distribution.
4. The cohort scope is set to read-only. New writes rejected; queries continue to work; federation projections continue to honor visibility horizons.
5. The archive bundle is registered in the federation registry per ADR-027 with its retention policy + access permissions.
6. Future cohorts may *cite* the archive (read-only); the archive cannot be modified.

**The cohort persistence guarantee from `working/memory-persistence-plan.md`:** the archive's schema_version is decoded by every Axiom release through the cohort's declared end-of-life date. Test fixtures pin this guarantee.

For Prague specifically: the cohort's declared end-of-life is the longer of (a) 7 years (FERPA-aligned student-record retention) or (b) cohort-coordinator-declared. The archive bundle is required reading for a hypothetical 2031 reviewer — it must decode + project under whatever Axiom ships in 2031.

## 7. Classification expiry — when classified fragments declassify

Some classification stamps have explicit expiry dates: CUI 25-year rule, some EAR controls, FERPA student records, time-bound proprietary embargoes.

The fragment's `classification.expires_at` field (added per amendment to `spec-classification-boundary.md` — TODO cross-link) is honored by the TIDY sweep:

- On expiry: classification stamp updated to the next-lower regime (CUI → unclassified; export-controlled → public; etc.).
- Audit event written: `ClassificationExpired` with old + new stamp + expiry rule.
- Federation gateway re-evaluates: previously-blocked projections may now flow.
- Visibility horizon is *not* automatically widened — classification expiry relaxes the floor; visibility intent stays as the writer set it.

**Subtlety:** classification can expire to a *less restrictive* state. The reverse (classification escalation) is never automatic — it is an explicit re-stamp event by an authorized principal.

## 8. Forgetting — anchored to accountable human (per ADR-035)

Bounding mechanism #4 — selective, not blanket.

Per spec-memory §3.6 + ADR-035: a human can request derivation-stoppage on fragments where they are the `accountable_human_id`. Mechanism:

- Human invokes `axi me forget --scope <id> [--cognitive-type <t>]`.
- A `ForgetRequest` event is written, signed by the human.
- TIDY sweep on next cycle:
  - Marks matching fragments as `derivation_stopped`.
  - L1+ derivatives of those fragments are removed from active projections.
  - Original L0 events preserved (audit-grade), but no new L1+ promotion possible.
- Federation gateway: if any pending projection includes derivation-stopped fragments, the projection is regenerated.

Cross-principal forgetting (one human asking another's data be forgotten) is rejected — accountability is non-transferable by request.

## 9. Concept-graph pruning — Layer 2 stale concept handling

The concept graph (ADR-033 Layer 2) accumulates concepts across all fragments. Bounding mechanism #5:

- A concept with no recent fragment evidence (no fragment-of-evidence within `concept_recency_window`, default 1y) and no recent edge changes drops to **deprioritized tier**.
- Deprioritized concepts: skipped from default 1-hop expansion at retrieval time; still queryable on explicit request.
- A new fragment evidence touch re-promotes the concept to active tier.
- Concepts with zero evidence (rare; usually result of fragment retraction) are eligible for hard removal during a `axi memory sweep --aggressive` cycle.

## 10. Per-cognitive-type defaults

Different cognitive types mature differently. Suggested defaults:

| Type | L0 retention (hot) | L0→L1 threshold | L1→L2 path | Notes |
|---|---|---|---|---|
| `core` | indefinite | n/a | manual | Identity-grade fragments don't decay. |
| `episodic` | 30d (default) | 5+ retrievals/30d | SCAN crystallization | High write volume; aggressive cold transition. |
| `semantic` | 1y | 3+ supporters / 6mo | concept-cluster fact | Slow-moving. |
| `procedural` | indefinite for plan templates; 30d for ad-hoc plans | proof-bound | plan-step-proof fast path | Plans are special — see §11. |
| `resource` | tied to underlying resource lifetime | n/a | n/a | Resource ref-counted. |
| `vault` | indefinite | n/a | n/a | Sensitive material; never auto-promotes. |

Override via `[memory.maturity.<cognitive_type>]` in scope config.

## 11. Plan + agent run maturity (per ADR-034)

Plans and agent runs have a distinctive maturity story:

- A **plan** at L0 (just-written) → L1 (used N+ times in cohort) → L2 (proven across runs; fact: "this plan template reliably accomplishes X") → L4 (canonical procedure for X).
- An **agent run** at L0 (event sequence) → L1 (run-pattern; "this kind of step typically takes 3 tool calls") → never promotes higher (runs are evidence, not knowledge — their *outputs* promote per the cognitive type of the output).

Plan + run retention defaults:
- Active plans: hot indefinitely.
- Completed-and-not-template-promoted plans: hot 30d → warm 6mo → cold.
- Run events: hot 30d → warm 90d → cold (Edge); longer at Server+.
- Templates (L4 plans): hot indefinitely; replicated to cohort archive.

## 12. Federation-aware archival

When a cohort's federation peers reference its fragments, archival must coordinate:

- Cohort A archives a fragment; cohort B's projection depends on it. The federation gateway notifies B's coordinator on archival.
- Archived fragments are still federation-projectable (they're signed, content-addressed, immutable) — but only via the archive bundle, not via live request.
- Peer projections built before archival continue to validate (signature stable; content-addressed).

This is handled at the federation gateway layer (Stage 5+) — cohort archival is Stage 5b territory; this spec records the contract.

## 13. Compliance gates introduced

`pytest -m maturity_compliance` (new):

- Retention round-trip: a fragment written today decodes from each tier (hot → warm → cold) without data loss in provenance.
- TIDY sweep idempotency: running the sweep twice on identical state produces identical state.
- Cohort archive replay: a pinned cohort-archive fixture decodes + projects under current Axiom.
- Classification expiry: a fragment stamped with an expired classification updates on TIDY sweep without operator intervention.
- Forgetting: a `ForgetRequest` halts L1+ promotion; original L0 preserved; audit projection visible.
- Plan-step-proof promotion: a verified step's output is L2-eligible without human review for `proof_type in {test, typecheck, replay}`.

These join `memory_compliance`, `pipeline_compliance`, `accountability_compliance`, and `model_strategy_compliance` as release gates.

## 14. Migration path

This spec is documentation today. Implementation phases:

| Phase | What lands |
|---|---|
| Phase 0 (now) | This spec; no code changes. |
| Phase 1 (with PlanPipeline MVP per ADR-034) | Tiered retention defaults; TIDY sweep promoted from RAG-only to platform; per-cognitive-type defaults loaded; plan-step-proof L2 fast path. |
| Phase 2 (with AgentPipeline MVP) | Forgetting per ADR-035; concept-graph pruning; classification expiry. |
| Phase 3 (post-Prague) | Cohort archival ceremony; signed archive bundles; federation-aware archival contracts. |
| Phase 4 (post-Stage 5b) | Cross-cohort archival coordination; federated wisdom mining (L5). |

## 15. Relationship to existing specs

| Spec | This spec's role |
|---|---|
| `spec-rag-knowledge-maturity.md` | This spec generalizes the maturity model from RAG-only to all memory; the RAG spec remains authoritative for chunk + interaction-log + retrieval-log specifics. |
| `spec-memory.md` | This spec specifies the retention/maturity/forgetting *behavior*; spec-memory specifies the contract surface. They cross-reference. |
| `adr-033-layered-memory-architecture.md` | This spec operates within ADR-033's four layers; tiered retention applies to L1; TIDY promotes L0→L1 + L1→L2; archival serializes L1+L2+L3 together. |
| `adr-034-plan-and-agent-pipeline.md` | Plan + run maturity (§11) follows from ADR-034's fragment shapes; proof-bound L2 promotion is a joint design point. |
| `adr-035-human-principal-binding.md` | Forgetting (§8) is anchored on accountable human per ADR-035. |
| `spec-classification-boundary.md` | Classification expiry (§7) requires `classification.expires_at` per a TODO amendment. |
| `spec-federation-policy.md` | Federation-aware archival (§12) extends the gateway contract. |
| `working/memory-persistence-plan.md` | Cohort archival (§6) operationalizes the cohort-lifetime guarantee. |

## 16. Open items

- **Per-cohort retention policy publication.** How does a cohort coordinator publish its retention defaults to member nodes? Federation registry entry? `axi cohort policy` CLI?
- **Concept-graph pruning aggressiveness.** §9 default 1y window is a guess. Tune empirically post-Prague.
- **Strict retention floors.** Some regulatory regimes (FERPA: 7y; ITAR: 5y from declassification) impose minimums that override profile defaults. How is the override surfaced in `[memory.retention]`?
- **Cohort-pair retention contracts.** When cohort A federates to cohort B, do their retention policies need to align? Likely yes for shared concepts; needs more thought.
- **Soft-delete vs. hard-delete for tombstoned content.** Today: blob removed, provenance preserved. Some contexts may want full hard-delete (vault cognitive type, GDPR-aligned scopes). How is the choice surfaced?

These do not block this spec; they will be addressed during implementation phases.

## 17. The bottom line

Memory aggression scales only with discipline. ADR-033 made memory layered. ADR-034 multiplies write volume. This spec is the bounding story that keeps the pipeline rigorous: tiered retention so disks don't fill, TIDY sweep so promotion happens automatically, cohort archival so cohorts persist correctly across years, classification expiry so regulated stamps relax on schedule, forgetting so accountable humans can stop derivation, concept-graph pruning so the graph doesn't drift to noise. Every mechanism is replayable, auditable, and recorded as L0 events — no hidden compaction, no silent drops.

Without this spec, plan + agent modes write themselves into a corner within a year. With it, Axiom's memory discipline scales to cohorts that span institutions and decades.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
