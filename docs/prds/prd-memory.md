# Axiom Memory — Product Requirements

**Product:** Axiom Memory (the unified memory primitive every extension consumes)
**Owner:** Ben Booth
**Status:** Active draft
**Last updated:** 2026-04-26
**Related:**
- ADR-033 (layered memory architecture)
- spec-memory.md (normative tech spec — paired with this PRD)
- spec-federation-policy.md (visibility + classification + trust + gateway)
- spec-classification-boundary.md (regulatory regimes)
- working/memory-benchmarks.md (how we measure)
- working/memory-architecture-stress-tests.md (cross-extension validation)
- working/cognee-vs-build-study.md (build vs adopt)

---

## 1) Elevator pitch

Axiom Memory is the single primitive every Axiom extension uses for memorable data — provenanced, classified, federable, retractable, replayable. One write path, one read path, one projection layer, one federation membrane. No extension reinvents memory; no operator audits memory in N places.

## 2) Problem / opportunity

Memory has been everyone's concern and no one's responsibility in Axiom. Each extension has built its own bespoke store, with bespoke retention, bespoke projections, bespoke (or absent) federation. Three concrete failures fall out of this:

- **Continuity** is broken. A user interacting with an agent on Monday has no episodic context on Wednesday because each extension stores interactions differently and projects them differently.
- **Federation** is unsafe. Without a per-fragment visibility horizon and classification stamp, sharing across scope boundaries means audit-by-prayer.
- **Audit** is impossible. There's no single place to stand to answer "why did the agent decide X?" — the trail runs through bespoke stores, none of which carry cryptographic provenance.

The opportunity is bigger than fixing these. Mem0, Letta, MemoryOS, MemGPT, Cognee, Anthropic's Claude memory, ChatGPT memory — every agent platform is converging on the same core idea (long-term memory matters; episodic + semantic + working separation matters; reflection matters). None of them have:

- cryptographic `(T, U, A, R)` provenance,
- federation-native primitives,
- classification-aware extraction (CUI / EAR / ITAR / Part 810 + nationality),
- DPM-aligned replay + projections,
- sovereign LLM extraction (no required cloud calls),
- Apache 2.0 licensing.

Axiom can be the choice for agent platform memory because we own the differentiators, not because we beat their feature lists.

**Beyond intra-Axiom extensions, the same substrate serves external LLM tools the user touches every day** — Claude Code, Codex, JetBrains AI, VS Code Copilot, Gemini, ChatGPT Desktop. The integration surface (per-tool adapters, ingest daemon, resume-on-start hooks, provider-memory bridge) lives in `prd-cross-tool-memory.md`; this PRD scopes the substrate that surface consumes.

## 3) Goals & success metrics

**Primary goal (locked 2026-05-11):** Be the best memory platform for **agent-platform builders**, measured head-to-head against **Mem0** and **Letta**.

**What "best" wins on — three axes, all required:**

1. **Cross-tool reach.** Axiom's substrate spans every LLM tool the builder integrates with (Claude Code, Codex, JetBrains AI, VS Code Copilot, Gemini, ChatGPT Desktop) via MCP + the AEOS adapter pattern. Mem0/Letta require per-tool integration code; Axiom requires a manifest. See `prd-cross-tool-memory.md`.
2. **Developer ergonomics.** `axi ext init` to a working extension in <5 minutes; lint catches drift at PR time; one write path, one read path. Mem0 needs per-store boilerplate; Letta needs server setup.
3. **Public benchmark parity-or-surplus.** LongMemEval / LoCoMo / MemBench at numbers comparable to or better than Mem0 v0.1 and Letta head-to-head, published on a rolling cadence per `working/memory-benchmarks.md`.

The §6 differentiator stack (cryptographic provenance, federation-native, classification-aware, MIRIX taxonomy, sovereign extraction, Apache 2.0, shadow-memory discipline, pluggable backends) is the **moat** — capabilities Mem0/Letta would have to redesign to match. Differentiators are *necessary* for the goal but not *sufficient*; the three axes above are how the goal is judged.

**Explicitly de-prioritized:** "Best for regulated deployment" (compliance-first / CUI / EAR / ITAR-led) is **not** the headline driver. Compliance posture stays in the differentiator stack but does not gate "best." Adoption by regulated users is a downstream consequence, not the success metric. See `project_axiom_best_goal_locked.md` for the lock context.

**Operational measurement methodology** — the three axes resolve to numbers, defended quarterly via `working/memory-benchmarks.md`:

1. **Information-recall parity or surplus** vs. Mem0 and Letta on public benchmarks (LongMemEval, LoCoMo, MemBench). Target: parity at every Stage 1 → Stage 6 milestone; surplus once the concept graph (Stage 2) lands. *(Implements axis 3 — benchmark parity.)*
2. **Differentiation defensibility** — every claim in §6 maps to a passing test in `working/memory-benchmarks.md §10`. A claim without a test is removed from the PRD. *(Defends the moat.)*
3. **Trend, not a snapshot.** Performance, compliance, and benchmark numbers are recorded continuously via the `memory-benchmarks` CI workflow (`.github/workflows/memory-benchmarks.yml`). Regressions ≥ 20% require named justification on the merge that introduces them. Trend lives in `working/memory-benchmarks-trend.md`. *(Audit hygiene across all axes.)*

**Success metrics — measurable per `working/memory-benchmarks.md`:**

| Goal | Metric | Target |
|---|---|---|
| **Axis 1 — cross-tool reach** | # LLM tools with at least one active integration path (MCP-native or transcript-ingest) | claude-code shipped; 3+ by Tier 2 end (per `memory-roadmap.md`); 6 by Tier 3 end |
| **Axis 1 — cross-tool restart safety** | Resume-eval corpus hydration rate (bench-4 per `memory-roadmap.md`) | ≥ 90% on canonical corpus |
| **Axis 2 — developer ergonomics** | Time-to-first-fragment from `axi ext init` on cold workstation | < 5 min |
| Episodic continuity (axis 3) | LongMemEval / LoCoMo task accuracy | ≥ parity with Mem0 v0.1 / Letta baseline |
| Replay determinism | `project(events≤t) == project(events≤t)` over N=1000 runs | 100% identical projections |
| Federation isolation | CUI fragment never appears in any peer projection | 100% (1k fuzz tests) |
| Forget propagation | Tombstoned fragment absent from every projection ≤ 1 cycle later | 100% |
| Aggregate-view accuracy | A scope-operator's aggregate view (brief / summary / status / report — extension-specific) matches an independently-derived ground-truth log | ≥ 90% event-coverage; 0 fabrications |
| Provenance integrity | Every projection cites fragment_ids it composed | 100%; no orphan citations |
| Shadow memory | New extensions accumulate ≤ 1 undeclared store before lint catch | 0 in production deployments |

Targets ratchet stage by stage per ADR-033 migration.

## 4) Key users / personas

This PRD names **role archetypes** — abstract personas every Axiom extension specializes in its own vocabulary. Per `feedback_axiom_domain_agnostic` and ADR-031, Axiom core docs never name domain-specific roles (student, researcher, operator-of-X). Extensions document their concrete persona mapping in their own PRD.

| Archetype | Primary tasks | Tech level |
|---|---|---|
| **Memory subject** — the principal whose actions are memorized | See what's been logged about them. Retract specific entries. Edit preferences. Opt out. Trust that their data won't leak across scope boundaries. | Non-technical. CLI or chat. |
| **Scope operator** — the principal who governs a memory scope | Get aggregate views over memory in their scope. Audit retractions without seeing the content. Promote curated material to peer scopes. Run on a mix of regulated + unrestricted nodes. | Mixed. CLI + chat. |
| **Extension developer** | Write memorable data through one API. Get provenance, classification, retraction, federation for free. Discover how to do the right thing via lint warnings, not handbook reading. | Technical. Code. |
| **Platform operator** | Audit any extension's storage footprint. Configure trust profile per scope. Run benchmarks on every release. Prove compliance posture for regulators. | Technical. CLI + dashboards. |
| **AI safety / compliance** | Trace any agent decision back to the fragments and projections that produced it. Verify retraction actually erases. Prove classification handling matches policy. | Technical. Audit logs + tooling. |

**Where extensions specialize the archetypes** — each extension's PRD names its concrete personas and maps them to this table:

| Extension | Memory subject (concrete) | Scope operator (concrete) |
|---|---|---|
| classroom (`prd-classroom.md`) | student | instructor |
| chat / agent (`prd-agents.md`) | conversation owner | shared-agent governor |
| research-loop / CURIO | investigator | project lead |
| domain consumer (B-Tree-Labs-built or third-party) | per consumer's PRD | per consumer's PRD |

Treat any concrete role name or concrete CLI command in this PRD as a bug. The archetypes above are the correct vocabulary for the core. Concrete personas, concrete CLI surfaces, and concrete user journeys live in each extension's own PRD/spec; this PRD only defines the abstract contract those extensions consume.

## 5) Scope — key capabilities (MVP)

The MVP is the Stage 1 + Stage 3-first-projection state already shipped, plus the docs that make it *the choice*. Stages 2/4/5/6 of ADR-033 are post-MVP per the migration plan.

1. **Single write path** — `CompositionService.write` accepts every memorable payload from every extension. Provenance + signing + classification + visibility set at write. Acceptance: zero direct DB calls in any new extension's write path; lint catches violations.
2. **Single read path** — `EventStore.list(scope=...)` is the only listing primitive; cross-scope reads require explicit `FederationGateway`. Acceptance: smoke test confirms no extension reads across scopes.
3. **MIRIX-typed fragments** — every fragment carries `cognitive_type ∈ {core, episodic, semantic, procedural, resource, vault}`. Acceptance: per-type validators enforce the type's content shape; type misuse caught at write rather than at read.
4. **VisibilityHorizon + ClassificationStamp on every fragment** — default-deny on both. Acceptance: shipping today; 60+ tests pass on the composition rule.
5. **Tombstone-based retraction** — append-only, audit-preserved, propagates through every projection on next read. Acceptance: any extension's retraction surface ends-to-end works; the canary-extension proof-of-life is tracked in the consuming extension's tests.
6. **First Layer 3 projection** — `RecentActivityProjection` for episodic continuity. Acceptance: any extension consuming the projection folds prior interactions into LLM context with no extension-side custom code; the canary-extension wire-up is tracked in its own spec.
7. **Storage discipline** — `MemoryStore` vs `EphemeralStore` distinction documented; `axi ext lint` warns on raw DB use. Acceptance: lint catches shadow stores in any extension's tree; migration helper bridges them into L1.
8. **Memory transparency to users** — generic primitive (per spec-memory.md): `EventStore.list(scope=...)` filtered by principal yields the memory-subject's view; tombstone via `EventStore.tombstone(id)`. Acceptance: one user-facing command per affordance, per extension; the concrete CLI surface (e.g., `<ext> me --memory`, `--forget`) lives in each extension's own PRD/spec.
9. **Continuous benchmarks** — public + custom benchmarks runnable per release. Acceptance: baseline established this week; trend tracked thereafter.
10. **Session-scoped memory with intelligent cross-session recall** — every fragment carries a `session_id` recording which CLI / chat / process invocation produced it. Sessions are first-class objects: immutable UUIDs, user-renameable, auto-named at process start from `<cwd-basename>-<YYYY-MM-DD-HHMM>`. Default read scope follows the MIRIX taxonomy — episodic fragments stay session-bound (events happen *in a session*); core, procedural, and resource fragments are cross-session always (stable knowledge); semantic fragments are cross-session by relevance. The principle: **things you did are scoped to where you did them; things you know are global.** Explicit `--all` or `session=*` opens episodic across sessions when a user genuinely wants the cross-session view. Acceptance: a user working in two repos in parallel sees clean per-session episodic context but full procedural/semantic carry-over; one user-facing affordance per session (`axi session list / current / use / rename`) per the same one-affordance-per-extension rule from item 8.

Out of MVP, in the migration roadmap (ADR-033 stages 2/4/5/6):

- L2 concept graph + extraction pipeline (Cognee-inspired but Axiom-owned)
- L4 federation gateway end-to-end
- Reflection / consolidation passes (Park et al. style)
- Working → recall → archival hierarchy operationalized (MemGPT/Letta-style)
- Cross-cohort concept federation
- Privacy-preserving memory aggregation

## 6) Distinctive bets — what makes us *the choice*

Each bet is something a competitor would have to *change* their architecture to match, not just *add* a feature for.

| Bet | What it gives us |
|---|---|
| **Cryptographic provenance** `(T, U, A, R)` per fragment | Every memory event is signable and auditable to the principal + agents + resources that produced it. Mem0/Letta/MemoryOS treat provenance as descriptive metadata. |
| **Federation-native** `axiom://` + cohort-sharded registry + hop-bounded gateway | Memory crosses orgs *as a primitive*, not as a sync hack. 10k–100k node target. |
| **Classification-aware** | CUI / EAR / ITAR / Part 810 + foreign-national filtering wired through the gateway. No competitor ships gov/regulated-deployment-ready memory. |
| **DPM-aligned** | Append-only L1 + task-conditioned projection at L3 = deterministic replay, auditable rationale, multi-tenant isolation, statelessness. Architectural, not bolted on. |
| **MIRIX cognitive taxonomy** | Six types as first-class data, not as tags. Hierarchies and consolidation ride this naturally. |
| **Sovereign LLM extraction** | Per-extractor data-flow capability declaration. CUI never sees an external provider. |
| **Apache 2.0 + open governance** | vs Mem0 source-available, Letta AGPL. Production deployable in regulated contexts without escape clauses. |
| **Shadow-memory discipline** | Lint + audit + sanctioned APIs. Third-party extensions *can't* accumulate hidden memory undetected. |
| **Pluggable backends** | SQLite for Edge, Postgres for Server, SeaweedFS / encrypted blob for Platform / classified. Same protocol surface. |
| **Cross-tool first** | Same substrate consumed by Axiom extensions *and* external LLM tools (Claude Code, Codex, JetBrains AI, VS Code Copilot, Gemini, ChatGPT Desktop) via MCP + AEOS adapter pattern + A2A peering. Per-tool adapter parsers and a unified ingest daemon back-stop the model-discipline gap; provider memory enrolls as a federated peer with delegated rights. Mem0/Letta require per-tool integration code; Axiom requires a manifest. See `prd-cross-tool-memory.md`. |
| **Session-aware composition** | Fragments carry `session_id` in their `(T, U, A, R, S)` provenance tuple, and read scope follows MIRIX type semantics rather than a global flag. Competing memory systems are either flat (everything shared, parallel-repo work cross-pollutes) or hard-walled (everything siloed, procedural knowledge doesn't carry). Axiom's typed-default rule means a user in two repos in parallel gets clean episodic separation *and* full procedural / semantic carry-over without configuration. |

## 7) Non-functional / constraints

- **Performance:** L1 write < 10 ms p95 single-fragment; L3 projection over 10k-fragment scope < 100 ms p95; benchmark targets ratchet per stage.
- **Security:** Fragment immutability invariant; signing keypair per scope; tombstones append-only; classification stamp evaluated before extractor invocation.
- **Privacy:** Default-deny on every federation outflow surface; per-fragment visibility horizon set at write; nationality filtering per-export-control-regime.
- **Backward compatibility:** Existing on-disk fragments without `visibility` / `classification` decode with safe defaults; no migration required for read paths.
- **Determinism:** Layer 3 projections are pure functions of `(events, graph, task)`; replay must produce byte-identical outputs.
- **Profiles:** Edge (laptop, ≤ 100k fragments per scope), Workstation (≤ 1M), Server (≤ 100M, Postgres backend), Platform (≥ 100M, distributed backend).
- **Air-gap:** L1 + L2 + L3 work without network. L4 gateway is a separately-instantiated primitive.

## 8) Timeline

> **Authoritative sequencing lives in `docs/working/memory-roadmap.md`.** That doc replaces this §8 when conflicts arise. Treat the table below as a scoped preview.

Aligned with ADR-033 migration stages.

| Phase | Window | Deliverable |
|---|---|---|
| Phase 0 (now) | shipped | ADR-033 + spec-federation-policy + Stage 1 dual-write + RecentActivityProjection + classification stamp + visibility horizon + episodic-in-ask wiring |
| Phase 1 | 1 wk | This PRD + spec-memory + memory-benchmarks (baseline) |
| Phase 2 | 2 wk | Stage 2 — L2 concept graph (SQLite-backed, Axiom-owned per build study) + first deterministic concept extractor (consumed by the canary extension first; per-extension extractors register alongside) |
| Phase 3 | 2 wk | Stage 3 — additional projections (`BriefProjection` refactor, `StudyPlanProjection`) + spec-memory-reflection |
| Phase 4 | post-Prague | Stage 4 — blob store extraction + manifest as projection |
| Phase 5 | post-Prague | Stage 5 — federation gateway + per-fragment classification enforcement at the wire |
| Phase 6 | quarter+ | Stage 6 — second-extension proof (research-loop or chat-agent consumes the same primitives) |

## 9) Risks & open questions

| Risk | Mitigation |
|---|---|
| Three-spec tension (knowledge-graph / session-store / agent-state-management) confuses extension authors during transition | spec-memory.md explicitly resolves authority + supersession; the contending specs get amend-PRs after spec-memory lands. |
| Cognee ecosystem moves faster than our build-our-own | Protocol boundary keeps Cognee swappable per `cognee-vs-build-study.md`. Adopt selectively post-measurement. |
| Performance tail of "every read is a projection" hurts chat working memory | Stage 3 adds a `(scope, task, as_of, log_head)` cache. Benchmark gates re-evaluation. |
| Classification + visibility composition produces surprising default-deny in extensions that didn't think about it | Documented + tested + lint surfaces the choice. PRD frames as a feature, not a bug. |
| Reflection / consolidation pass has high LLM cost at scale | Frequency knob; cohort-level cadence; classification-aware extractor selection caps cost at the regime boundary. |
| External agents (built against Mem0 / Letta) can't easily plug in | Adapter shapes now scoped in `prd-cross-tool-memory.md`. Per-tool AEOS extension pattern; Mem0/Letta interop as future adapters under the same pattern. |

**Decided (per spec-memory §14, all 2026-04-26):**

- **Concept identity across extensions: consolidate by canonical name.** Federation works better when nodes naturally agree on identity by hashing the same canonical string. Implemented via `canonical_concept_id`.
- **Time-travel cost: deferred to a future scaled-data-infra extension.** Memory's `snapshot_at()` + `as_of` are the cooperation seams; the production-grade medallion + snapshot tier lives in its own extension (the domain consumer is a planned consumer). Memory does not duplicate that work.
- **Lint vs enforcement gradient: trigger-gated, not calendar-gated.** Four staged triggers (T0–T3) per spec-memory §14.1; memory team commits only to T0 (today) until each forcing context lands.

**Still open (decide by phase 2 start):**

- Blob ref lifecycle under federation (strip vs unresolved-target).
- Inline vs background extractor execution defaults.
- Reflection / consolidation contract (Park et al.) — follow-up spec.
- Memory hierarchy operationalization (working → recall → archival) — follow-up spec.
- **Cross-session semantic-fragment relevance threshold.** §5 item 10 says semantic fragments are cross-session "by relevance" — phase 1 ships keyword-overlap + recency as a stand-in; phase 2 should evaluate vector-similarity and tune the threshold against real cross-session recall failures. The MIRIX-aligned default for `core`/`procedural`/`resource` (always cross-session) and `episodic` (session-bound) is locked; only the `semantic` filter is up for tuning.

## 10) Acceptance & rollout

**Sign-off:** Ben Booth (product). Engineering gate: 1042+ existing tests pass + new memory-benchmarks baseline established + spec-memory.md cross-references resolve.

**Rollout:** Already canary-deploying via the first consumer extension. Phase 1 lands the docs that make memory legible. Phase 2 lands the concept graph behind the same protocols — extension migration is opt-in per stage. Rollback: ADR-033's dual-write contract means every stage can revert to the bespoke store as the read source.

## 11) Reconciliation with adjacent specs

The three tensions identified in the audit are resolved here, codified in spec-memory.md:

- **spec-knowledge-graph (Apache AGE entity extraction over RAG chunks)** — becomes one storage backend behind L2 `ConceptGraph` protocol. The extraction pipeline ingests both `MemoryFragment` and RAG chunks. AGE is the recommended Server-tier backend; SQLite remains the Edge-tier default.
- **spec-session-store (PG-backed sessions as primary table, interaction log as a view)** — sessions become **projections** of L1 conversation_turn fragments. The existing PG table stays as a **read cache** that can always be rebuilt from L1. Existing query API unchanged.
- **spec-agent-state-management (flat-file/PG state with locks + retention)** — scope clarified to *operational* state (cursor positions, session presence, autosave drafts). *Cognitive* state (anything memorable) routes through `MemoryStore`. State retention policy continues to govern its scope; sanctioned `EphemeralStore` (per ADR-033 storage discipline) is the new home for transient state going forward.

Each amend-PR to those specs lands after spec-memory is accepted; until then, spec-memory is authoritative on overlaps.

## 12) Contacts & links

- Product lead: Benjamin Booth (no-reply@axiom-os.ai)
- Eng lead: same
- Spec: `docs/specs/spec-memory.md`
- Architecture: `docs/adrs/adr-033-layered-memory-architecture.md`
- Federation policy: `docs/specs/spec-federation-policy.md`
- Benchmarks: `docs/working/memory-benchmarks.md`
- Build vs adopt: `docs/working/cognee-vs-build-study.md`
- Stress tests: `docs/working/memory-architecture-stress-tests.md`
- Cross-tool integration PRD: `docs/prds/prd-cross-tool-memory.md`
- Cross-tool design sketch: `docs/working/cross-tool-memory-guarantees-sketch.md`

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
