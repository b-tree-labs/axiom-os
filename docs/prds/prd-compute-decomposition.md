# PRD — Compute Decomposition

**Product / Feature:** Compute Decomposition — federation-routed, trait-aware, LLM-proposed deterministic-verified compute orchestration inside the Axiom harness.

**Owner:** Ben Booth (B-Tree Labs)   •   **Status:** Draft (design)   •   **Last updated:** 2026-05-01

**Related ADRs:** ADR-027 (federated memory), ADR-029 (federation composition), ADR-030 (federated inference), ADR-031 (extension self-containment), ADR-035 (LLM-tier policy), ADR-036 (extension runtime surfaces), ADR-037 (federation state propagation), ADR-039 (scientific displays), ADR-040 (this capability — *see `docs/adrs/adr-040-compute-decomposition.md`*).

**Related specs:** `spec-aeos-0.1.md`, `spec-llm-tier-policy.md`, `spec-event-bus.md`, `spec-classification-boundary.md`, `spec-federation.md`, `spec-compute-decomposition.md`.

---

## 1) Elevator Pitch

A scientist on a laptop says "run this study," and the harness automatically decomposes the work into natural sub-parts, distributes them across trusted federation peers' idle compute, aggregates the results deterministically, renders the figures via Scientific Displays, and drafts a citation-grounded candidate paper — turning a 4-hour HPC-queue job into a 38-minute run across nine friends' laptops at lunch, without leaving the chat.

## 2) Problem / Opportunity

Today every serious computational study has the same shape: write the input deck, push it to an HPC scheduler, wait in queue, wait for the run, pull results back, plot them in matplotlib, screenshot the plots, paste them into a draft paper, manually attribute every number, mail the draft to collaborators. State is lost at every hand-off, attribution is reconstructed by hand, and the colleagues' workstations sit idle the whole time.

The federation already routes signed memory fragments (ADR-027), inference requests (ADR-030), and scientific artifacts (ADR-039). It does not route **work**. The result: a lone scientist with a laptop and ten federated peers does the same job as a lone scientist with a laptop and zero peers. The federation is a one-way valley for inference and storage but a wasteland for compute.

The opportunity is asymmetric. Three primitives are now in place that no competitor stack composes:

- **Federation directory (ADR-037)** — typed, signed, gossipped state with classification-aware visibility and per-record TTLs. Compute offers, claims, and results slot into this directly.
- **"LLM proposes, deterministic kernel verifies" pattern (ADR-039)** — proven for math; the same shape generalizes to decomposition planning, adapter-code generation, and paper drafting.
- **Cohort-scoped trust + classification gates (ADR-022 through 025, `spec-classification-boundary.md`)** — work routes only to peers cleared for the chunk's content; federation never silently ships export-controlled work to an unauthorized leaf.

Composing these three into a first-class compute primitive turns "another agentic chat tool" into "the harness that turns my colleagues' idle laptops into my personal cluster, while we all retain provenance, attribution, and reproducibility."

The named consequence: this is the fourth asymmetric edge after Federation, Memory Composition, and Scientific Displays. It is the difference between "we route data" and "we route work."

## 3) Goals & Success Metrics

**Primary goal:** Take a deep-science compute problem fitting one of the registered decomposition patterns, run it across cohort federation peers in less wall-clock time than the user's nearest HPC alternative (queue + run), and produce a single signed aggregated artifact + figure set + candidate paper draft from the run trace.

**Success metrics:**

| ID | Metric | Target |
|---|---|---|
| **M1** | A registered pattern's decomposer + recomposer round-trip is bit-identical (deterministic) or statistically valid (stochastic) on identical inputs. | 100% in CI; release-gating. |
| **M2** | A leaf failure mid-chunk (process crash / network partition / heartbeat-miss) results in successful chunk reassignment + re-execution within 60s. | p95 ≤ 60s in chaos tests. |
| **M3** | Stochastic chunks re-run with identical seeds produce identical bytes; deterministic chunks served from content-addressed cache match the original execution. | 100% in CI; release-gating. |
| **M4** | Asymmetric-advantage demo: a real domain workload (per the first consumer extension) completes in less wall-clock than the user's nearest HPC queue-plus-run alternative on a 5-10 peer cohort. | Demonstrated in Phase B; documented as a reproducibility receipt. |
| **M5** | Every numeric claim in a candidate paper draft links back (via trace ID) to a signed `COMPUTE_RESULT` directory record. | 100% in CI; release-gating; mirror of ADR-039 M3 for math results. |
| **M6** | The reproducibility appendix in a candidate paper deterministically regenerates from the same trace; the appendix hash is stable. | 100% in CI. |
| **M7** | Chunks tagged at a classification level above any visible peer's ceiling never get dispatched; the user is offered the documented gap-resolution choices. | 100% in CI; release-gating. |
| **M8** | The user-facing `axi compute run` command produces three artifacts (aggregated MemoryFragment, figure set, candidate paper draft) from a single invocation, no manual intermediate steps. | Yes/no acceptance gate at Phase B exit. |
| **M9** | Pre-emptive resource estimator's `local | local-with-tasks | federation` classification matches actual run cost class on a held-out workload set. | ≥ 80% accuracy at Phase B; ≥ 90% at Phase C. |
| **M10** | A second domain extension (after the first consumer) plugs into the primitive without core changes. | Demonstrated at Phase C. |

## 4) Key Users / Personas

| Persona | Primary tasks | Technical level |
|---|---|---|
| **Computational scientist (study leader)** | Defines a problem, picks the LLM tier for decomposition, watches the federation distribute work, reviews the candidate paper draft, signs and shares the run artifact. | Expert: comfortable with input decks, command line, occasional HPC submission. |
| **Cohort peer (compute donor)** | Runs the harness on a workstation or laptop; advertises available compute via `COMPUTE_OFFER`; opts in to (or out of) participating in cohort runs; reviews per-leaf attestations after the fact. | Intermediate: comfortable with `axi` CLI and with cohort membership ceremony. |
| **Federation reviewer** | Receives a candidate paper draft + run trace + reproducibility appendix; verifies the run from the appendix; comments back through `/share`. | Expert: cares about provenance, attribution, reproducibility. |
| **Domain extension author** | Registers decomposers, recomposers, leaf adapters, and paper templates against the primitive's pattern slots; ships an extension that turns "the primitive can decompose" into "the primitive decomposes *my* workload." | Expert in the domain; intermediate in Axiom internals. |
| **Operator (cohort root / IT)** | Approves compute-related federation policy; reviews `COMPUTE_OFFER` advertisements per node; revokes leaf participation on policy violation. | Expert in operations; intermediate in scientific computing. |

## 5) Vision

When a scientist opens `axi chat` from a laptop and asks "run this study," the harness inspects the problem, asks the cohort federation what's available, proposes a decomposition (validated against the deterministic registry), shows the user the plan, dispatches chunks to colleagues' idle workstations, streams progress back through the chat, aggregates the result deterministically, renders the figures, and drafts a citation-grounded candidate paper — all before the user finishes their lunch break.

Five years out, "I queued this on the cluster and waited overnight" reads the way "I drove across town to use the library's computer" reads now: a workflow we used to tolerate before tools were federated.

## 6) Scope — Key Capabilities (MVP)

The MVP is **Phase B exit** per ADR-040 D13 (the asymmetric-advantage demo). Phase A (this design + scaffolding) delivers the substrate; Phase B delivers the demo.

1. **Problem manifest + decomposition vocabulary** — a `Problem` declares pattern, classification, resource estimate, and pattern-specific parameters. The registry holds named patterns with deterministic decomposer/recomposer pairs. *Acceptance:* `axi compute plan <problem.yaml>` produces a `DecompositionPlan` validated against the registry.
2. **`embarrassingly_parallel` pattern with full trait routing** — deterministic and stochastic variants both supported, with content-addressed caching for the deterministic case and seed coordination for the stochastic case. *Acceptance:* round-trip M1 + M3 pass in CI on a non-domain stub.
3. **Federation directory wiring** — `COMPUTE_OFFER`, `COMPUTE_CLAIM`, `COMPUTE_RESULT` record types implemented; gossipped per ADR-037; cohort-scoped by default; classification-gated per `spec-classification-boundary.md`. *Acceptance:* `axi federation peers --offering compute` shows live offers; `axi compute trace <plan_id>` shows the live claim/result graph.
4. **Per-leaf runner contract** — subprocess sandbox (Sci Displays D3 model); stdout / stderr / progress streamed via `axi tasks`; heartbeat every 5s; reassignment on miss > 30s. *Acceptance:* M2 passes in chaos tests.
5. **Aggregation + signed MemoryFragment** — recomposer runs on originating node; produces single MemoryFragment via CompositionService with full `(T, U, A, R)` provenance and `references` listing every `COMPUTE_RESULT`. *Acceptance:* aggregated fragment hash is stable across re-runs from cache.
6. **One stub domain extension** — registered against `embarrassingly_parallel`; exercises the full primitive end-to-end with a non-domain placeholder workload (e.g. parameter sweep over a simple analytical function). *Acceptance:* M4 demonstrated end-to-end in CI.
7. **Pre-emptive resource estimator + user-prompt routing** — cheap deterministic heuristic classifies into `local | local-with-tasks | federation`; user is asked before any federation routing. *Acceptance:* M9 ≥ 80% on the held-out set.
8. **Candidate paper drafting** — one universal template (placeholder domain template); LLM (`smartest` per ADR-035) fills slots from the run trace; deterministic reproducibility appendix. *Acceptance:* M5 + M6 pass in CI.
9. **`axi compute run` one-command UX** — single CLI invocation runs estimate → propose → verify → distribute → execute → aggregate → render → draft; returns three artifacts. *Acceptance:* M8 yes/no gate at Phase B exit.

Phase C scope: `spatial_domain` + `temporal_stepping` + `composite` patterns; second domain extension (M10); defaults-on for cohort-scoped compute. Phase D scope: cross-cohort `--public` opt-in; gVisor / Firecracker sandbox option; Platform-tier (HPC) submit-as-leaf integration. See ADR-040 D13.

## 7) Non-Functional / Constraints

- **Performance:** Decomposition planning < 5s for problems up to 10⁴ chunks. Estimator pre-flight < 500ms. Aggregation latency dominated by `ChunkResult` artifact fetch, not by recomposer logic.
- **Reproducibility:** Same `(Problem, DecompositionPlan, seed_seed, ChunkResult set)` quadruple → bit-identical aggregated MemoryFragment hash. Always.
- **Sandbox:** Per-leaf subprocess sandbox per ADR-039 D3. No network unless adapter explicitly declares + leaf permits.
- **Security:** Every `COMPUTE_*` record signed by node key per ADR-037. Aggregated fragment signed by originating node. Every adapter code attestation per Sci Displays attestation pipeline.
- **Classification:** Chunks never dispatched to peers below their classification ceiling. EC / ITAR / Part 810 stamps honored per `spec-classification-boundary.md`. Surface gaps; never silently route around them.
- **Platforms:** Edge (laptop) + Workstation tiers must work as leaves. Server tier strongly recommended as cohort-root rendezvous. Platform tier (HPC) is Phase D.
- **Determinism floor:** The recomposer is deterministic; the registry rejects any decomposer/recomposer pair that fails the round-trip property test.
- **Scope honesty:** Tightly-coupled MPI, shared-memory codes, and codes that cannot checkpoint are out of scope (ADR-040 D11). Refusal must be structured ("not a supported pattern; here's why"), not silent.

## 8) Timeline (high level)

- **Phase A** (design + scaffolding) — 2026 Q3, post-Prague hardening window. Deliverables: this PRD + ADR-040 + spec; primitive package skeleton; `embarrassingly_parallel` pattern with one trait variant; flag-gated, off by default; one stub domain extension at the same scaffold stage.
- **Phase B** (single-pattern e2e + asymmetric-advantage demo) — 2026 Q3 → Q4. Deliverables: full `embarrassingly_parallel` with both trait variants; federation directory wiring complete; per-leaf runner contract complete; aggregation + signed MemoryFragment; first domain extension producing a real run; first candidate paper draft; **first asymmetric-advantage demo on a small problem with 3-5 peers**.
- **Phase C** (production patterns + GA) — 2026 Q4 → 2027 Q1. Deliverables: `spatial_domain` + `temporal_stepping` + `composite` patterns; second domain extension; defaults-on for cohort-scoped compute; **demo on real problem with 8-10 peers; signed paper draft survives independent reproducibility check.**
- **Phase D** (cross-cohort + advanced sandbox + HPC leaf) — 2027. Deliverables: cross-cohort `--public` opt-in; gVisor / Firecracker option; Platform-tier submit-as-leaf integration; **federated study published with peers across institutions.**

**Explicit non-target: Prague June 2026.** This primitive is post-Prague. Phase A may overlap with the Prague hardening window; Phase B begins after Prague. Pre-Prague workstreams (Tier A / B / C classroom delivery, Sci Displays Phases A/B, AEOS CLI hardening) take strict precedence. See `project_prague_runway_plan_2026_04_29` and ADR-040 D13.

## 9) Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Decomposer/recomposer pairs registered by extensions silently violate invariants → wrong aggregate. | Round-trip property tests in CI per registered pattern parameterization (M1); release gate; published invariant doc per pattern. |
| Stochastic seed coordination drifts between leaves running different solver versions → different bytes for nominally-identical seeds. | Pin solver version + cite it in `kernel_attestation`; recomposer rejects results whose attested kernel version differs from the plan's pinned version. |
| LLM proposes a "valid-looking" decomposition that the verifier accepts but that produces wrong physics (e.g. spatial domain partition that breaks the solver's boundary conditions). | Per-pattern invariants must include domain-shape constraints (e.g., "halo width must be ≥ stencil radius"); domain extensions ship invariants as code, not English. |
| Cohort peers withdraw mid-run (laptop closed, conference Wi-Fi died) → cascading reassignment storm. | Heartbeat-then-reassign with exponential backoff on the reassignment side; mark peers transiently unavailable for N minutes after a miss before re-offering them work. |
| User defaults `--public` thinking it means "publish results" rather than "expose work outside cohort" → accidental cross-cohort leak. | `--public` requires a confirmation prompt explaining the visibility consequence; CLI help text named "expose to peers outside this cohort," not "make public"; cohort-default is the *only* implicit option. |
| Candidate paper draft contains a "valid-looking" claim that the LLM fabricated despite trace grounding. | Drafting prompt includes explicit cite-or-omit rules; CI gate requires every numeric claim to resolve to a `COMPUTE_RESULT` ID in the run trace (M5); failures block release; manual reviewer pass required at Phase B exit. |
| Resource estimator mis-classifies and the user federates a 30-second job, burning trust budget on peers. | Estimator threshold tunable; user-prompt-before-route catches most of these; track per-cohort "wasted-trip" rate as a Phase B metric. |
| The "honest scope" disclaimer (D11) is insufficient and a user pushes a tightly-coupled code through anyway. | The pattern registry refuses unregistered patterns; tightly-coupled MPI codes have no registered pattern that fits; structured refusal with documentation link, not a fallback to "best-effort" behavior. |

**Open questions (decide before Phase B):**

| Q | Decision needed by |
|---|---|
| **Q1.** What is the first domain extension's workload? (A domain consumer's Fleet Compute extension is the proposed first consumer; pick the specific benchmark — see Fleet Compute PRD.) | Phase A exit. |
| **Q2.** Does the recomposer have access to *all* `ChunkResult` artifacts before producing the aggregate, or can it run in a streaming / online aggregation mode? (Affects memory pressure on the originating node for large problems.) | Phase B mid. |
| **Q3.** How does the primitive interact with ModelCorral when the problem is itself a Model Corral entry (pinned version, manifest hash)? Direct reference vs. snapshot at decompose-time? | Phase B exit. |
| **Q4.** Cross-cohort visibility (`--public`) interacts with ADR-027 federated memory propagation rules; is a follow-up ADR needed before Phase D? | Phase C mid. |
| **Q5.** Platform-tier submit-as-leaf (HPC integration in Phase D) — is the leaf the HPC head node (which then re-fans out internally) or is each MPI rank a leaf? Affects the scheduler-shim shape. | Phase D start. |

## 10) Acceptance & Rollout

**Phase A acceptance** (this design): ADR-040 + this PRD + `spec-compute-decomposition.md` reviewed and approved; primitive package skeleton landed in `src/axiom/compute_decomposition/` behind a flag; first domain extension's design deliverables (its own ADR + PRD + spec) landed in parallel.

**Phase B acceptance**: M1, M2, M3, M5, M6, M7, M8, M9 pass in CI; M4 demonstrated in a recorded session with 3-5 cohort peers; first candidate paper draft passes manual reviewer pass.

**Phase C acceptance**: M10 demonstrated (second domain extension); independent reproducibility receipt verified by a non-author cohort peer; defaults-on for cohort-scoped compute.

**Rollout**: Flag-gated through Phase A and Phase B (opt-in via `axi compute enable`); defaults-on at Phase C entry. Rollback criterion: any release-gating metric (M1 / M3 / M5 / M6 / M7) regresses → flag back to off, fix, re-gate.

**Sign-offs:**

- Product / lead: Ben Booth.
- Federation review: V-EGA + cohort-root operators in pilot cohorts.
- First-consumer review: the domain consumer's Fleet Compute extension lead (initial proposed first consumer).
- Reproducibility review: independent peer reviewer at Phase C exit.

## 11) Contacts & Links

- **Product lead:** Ben Booth — no-reply@axiom-os.ai
- **Spec / design:** `docs/specs/spec-compute-decomposition.md`
- **ADR:** `docs/adrs/adr-040-compute-decomposition.md`
- **First consumer (proposed):** a domain consumer's Fleet Compute extension — see the consumer repo's `docs/prds/prd-fleet-compute-demo.md` and `docs/specs/spec-fleet-compute-demo.md`
- **Substrate dependencies:** ADR-037 (federation directory), ADR-039 (Sci Displays sandbox + figures), ADR-035 (LLM-tier policy), `infra/tasks/` (background tasks primitive), CompositionService

---

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
