# ADR-040: Compute Decomposition — Federation-Routed, Trait-Aware, LLM-Proposed-Deterministic-Verified

**Status:** Proposed (2026-05-01)
**Supersedes:** none
**Related:** ADR-022/023/024/025 (federation identity, topology, root availability, threat model), ADR-026 (ownership), ADR-027 (federated memory), ADR-028 (trust graph), ADR-029 (federation composition meta-ADR), ADR-030 (federated inference), ADR-031 (extension self-containment), ADR-035 (LLM-tier policy), ADR-036 (extension runtime surfaces), ADR-037 (federation state propagation), ADR-039 (scientific displays — the "LLM proposes, deterministic verifies" precedent).
**Specs:** `spec-aeos-0.1.md`, `spec-llm-tier-policy.md`, `spec-event-bus.md`, `spec-classification-boundary.md`, `spec-federation.md`, `spec-compute-decomposition.md` (this capability).
**PRD:** `prd-compute-decomposition.md`.

---

## Context

Axiom federation already routes signed memory fragments, federated inference requests, and signed scientific artifacts. It does not route **work**. A scientist with a heavy computation has exactly two options today: run it on their local node and wait, or queue it on an institutional HPC cluster and wait differently. The federation peers — colleagues' workstations, lab machines, classmates' laptops — sit unused while a single node bottlenecks.

The capability gap has three independent forces converging:

1. **Compute is the largest unsolved primitive in the federation stack.** Memory propagates (ADR-027), inference routes (ADR-030), state replicates (ADR-037), but a multi-hour deterministic-or-stochastic compute problem still binds to one node.
2. **The "LLM proposes, deterministic kernel verifies" pattern from ADR-039 generalizes.** Scientific Displays proved the pattern works for math: the LLM proposes an equation, SymPy authorizes the result, provenance composes through the existing CompositionService. The same pattern shape — *propose decomposition, deterministically verify, deterministically aggregate* — applies to compute decomposition for a much larger class of problems.
3. **Federation directory (ADR-037) plus background tasks plus per-language attestation (ADR-039 D3 sandbox model) plus LLM-tier routing (ADR-035) are now in place.** Every primitive a federation-routed compute capability needs already exists in some form. The decision is whether to compose them into a first-class **`ComputeDecomposition`** primitive or let every consumer reinvent the orchestration.

The third force is the timing pressure: without a unified primitive, the first three consumer extensions to want federation-routed compute will each invent their own decomposition vocabulary, their own seed-coordination scheme, their own aggregation protocol, their own paper-drafting prompt — and the federation directory will accumulate three incompatible record-type families. The cost of *not* unifying compounds quickly.

This ADR is intentionally **domain-agnostic** per `feedback_axiom_domain_agnostic` and the project CLAUDE.md: no nuclear / reactor / facility / course references. The capability serves any deep-science domain whose workload decomposes naturally — particle methods, spatial PDE solves, parameter sweeps, ensemble runs, map-reduce-shaped data processing, molecular-dynamics replicas. Domain extensions parameterize the primitive with their solver-specific decomposers, recomposers, adapter scripts, and paper templates.

---

## Decision

Axiom-core ships a new primitive — **`ComputeDecomposition`** — that orchestrates the decomposition, federation routing, per-leaf execution, deterministic aggregation, and candidate-paper drafting of large compute problems. The primitive is a thin orchestrator over existing infrastructure: it owns the decomposer/recomposer registry, the trait-routing logic, the chunk-claim protocol, and the paper-template mechanism. It does not own its own federation, its own sandbox, its own task queue, or its own LLM router.

### D1 — One primitive, one vocabulary; domain extensions parameterize

`ComputeDecomposition` defines a fixed vocabulary: `Problem`, `DecompositionPlan`, `Chunk`, `ChunkResult`, `Decomposer`, `Recomposer`, `LeafAdapter`, `PaperTemplate`. Every domain extension that wants federation-routed compute *parameterizes* this vocabulary by registering decomposers, recomposers, adapters, and paper templates against named pattern slots in the registry.

Rationale:

- A single vocabulary is the difference between "every consumer reinvents this" and "extensions plug into a primitive." ADR-029 federation composition is the existing precedent: meta-pattern in core, specifics in extensions.
- A fixed vocabulary lets the federation directory (ADR-037) carry typed records (`COMPUTE_OFFER`, `COMPUTE_CLAIM`, `COMPUTE_RESULT`) that any consumer reads consistently. If decomposition vocabulary differed per consumer, directory records would have to be opaque blobs.
- Pattern parameterization (rather than pattern subclassing) keeps the registry flat and the schema stable. A particle-method pattern is the embarrassingly-parallel pattern with extra invariants attached, not a subclass.

Rejected alternatives:

- Per-domain orchestrator extensions (no shared core) — the directory-record-type fragmentation cost is unbounded; the second consumer pays it forever.
- Generic pipeline framework (no decomposition vocabulary at all, just "stages") — too generic to enforce the trait-routing and aggregation invariants this primitive's value depends on.

### D2 — Problem-shape registry: a closed vocabulary of named decomposition patterns

The registry ships with a small number of well-known decomposition patterns. Each pattern names a deterministic invariant family the decomposer must preserve and the recomposer must restore.

| Pattern | Decompose | Recompose | Trait | Examples (any domain) |
|---|---|---|---|---|
| `embarrassingly_parallel` | Slice independent units (samples / particles / replicas / parameter rows) | Pool results; scalar/vector reduction by declared accumulator | stochastic *or* deterministic | Monte Carlo batches, parameter sweeps, ensemble runs |
| `spatial_domain` | Partition mesh / lattice / grid into subdomains with halo-overlap | Halo-exchange merge; convergence iterate if iterative | deterministic | Finite-element / finite-volume PDE solves |
| `temporal_stepping` | Sequence time steps; stop-and-restart at coupling boundaries | Concatenate trajectory; coupling-residual check | deterministic | Time-marched simulations, multi-physics coupling outer loop |
| `matrix_block` | Partition matrix into row / column / 2D blocks | Block-recompose with declared block algebra (e.g. block-Cholesky) | deterministic | Linear algebra, spectral methods |
| `map_reduce` | Map over input shards | Reduce by declared associative operator | deterministic | Data processing, feature extraction |
| `composite` | Outer pattern + inner pattern per outer chunk | Outer recompose of per-outer-chunk inner-recomposed results | mixed (per-layer trait) | Multi-physics outer-loop with inner stochastic / spatial inner |

The registry is *open for extension, closed for invention*: domain extensions register **parameterizations** (e.g., a stochastic-transport extension registers `stochastic_transport_batches` as a parameterized `embarrassingly_parallel` with seed-discipline + tally-accumulator rules), but they do not invent novel pattern shapes. Patterns added to the core registry follow the standard ADR cycle.

Rationale:

- A closed vocabulary is what makes the recomposer correctness argument tractable. If "any function can be a recomposer" then aggregation correctness is per-extension, untestable in core.
- The pattern set covers the asymmetric-advantage demo class explicitly: embarrassingly parallel + spatial domain + temporal stepping + map-reduce + composite of those four. Anything outside that class is honestly out of scope (D11).
- Extensions parameterize, not invent: this preserves the invariant family per pattern. A solver-specific decomposer slots into `embarrassingly_parallel` and inherits its aggregation rules, rather than declaring its own.

Rejected alternatives:

- Free-form decomposer/recomposer pairs — invariant correctness becomes per-extension; no central testability.
- Single universal "split + merge" abstraction — too thin; the trait-routing and aggregation rules differ enough between patterns that a universal abstraction reduces to "pass a function pointer," which is not a primitive.

### D3 — Deterministic-vs-stochastic trait routing is a first-class chunk attribute

Every chunk emitted by a decomposer carries a `trait` field: `deterministic`, `stochastic`, or `hybrid` (deterministic outer with stochastic inner). The trait determines retry policy, caching policy, aggregation rule, and reproducibility receipt.

| Trait | Retry on leaf failure | Caching | Aggregation | Reproducibility receipt |
|---|---|---|---|---|
| `deterministic` | Reassign + retry; identical bytes expected | Content-addressed cache hit returns instantly | Deterministic reduction (declared by pattern) | Input hash + kernel attestation |
| `stochastic` | Reassign + re-seed; original chunk's contribution voided | No cache (re-execution gives different bytes by design) | Statistical aggregator (declared by pattern: weighted mean, tally sum, etc.) | Input hash + seed + kernel attestation |
| `hybrid` | Reassign + recompute deterministic prefix; re-seed stochastic suffix | Cache the deterministic prefix; never the stochastic suffix | Per-pattern composite | Input hash + seed + kernel attestation |

Rationale:

- Without trait routing, a re-seeded stochastic chunk silently corrupts the aggregate (because the original chunk's contribution is still in the pool). Voiding-then-aggregating is the only correct reduction.
- Content-addressed caching of deterministic chunks turns re-runs from "1 hour" to "instant," which is the difference between "demo works once" and "demo is reproducible from a paper appendix." This is the same insight as Bazel remote caching for build artifacts.
- Hybrid traits are the realistic case for multi-physics: outer time-stepping is deterministic, inner Monte Carlo is stochastic. Without hybrid, every multi-physics chunk is forced into one trait and we lose either caching or aggregation correctness.

Rejected alternatives:

- Single trait (everything stochastic) — wastes deterministic caching opportunity.
- Single trait (everything deterministic) — silently miscomputes stochastic aggregates.
- Per-extension trait declaration without core enforcement — extensions can declare wrong; core has no recourse.

### D4 — LLM as orchestrator and interpreter only; never as truth source

The LLM has exactly three roles in the primitive:

1. **Decomposition proposal.** Given a `Problem` description, the LLM proposes a `DecompositionPlan` selecting a registered pattern, parameterizing it, and naming the registered decomposer/recomposer pair. The plan is *validated* by a deterministic verifier against the registry's invariants for that pattern. Invalid plans are rejected with structured feedback; the LLM may propose again.
2. **Per-leaf adapter code generation.** Given a chunk and a target language/runtime, the LLM may generate the per-leaf adapter (the bash / Python / shell / Slurm fragment that hands the chunk to the domain solver). Generated adapter code passes through the same per-language attestation pipeline as Sci Displays D3 (subprocess sandbox + signature + reproducibility receipt) before any leaf will run it.
3. **Candidate-paper drafting.** From a completed run trace (DecompositionPlan + ChunkResult set + aggregated MemoryFragment + figures), the LLM drafts a candidate paper from a domain-extension-supplied template. The draft is structurally constrained by the template, attribution-grounded in the trace, and tone-controlled by the prompt.

The LLM **never** runs the calculation, **never** aggregates results, **never** signs artifacts, **never** decides retries, **never** authorizes a numeric value. Every numeric claim in the candidate paper links back to a signed `COMPUTE_RESULT` directory record.

Rationale:

- This is the load-bearing trust property. "The LLM cannot lie about the math" is the same asymmetric-edge claim as ADR-039 D2; here it scales from "one equation" to "an entire computational study with N peers."
- Decomposition proposal is the right LLM job (it requires understanding problem shape and pattern semantics — a language-model competency); aggregation is the wrong LLM job (it requires bit-exact arithmetic — a deterministic-kernel competency).
- Adapter code generation is gated by attestation (the kernel verifies the code does what it says before any leaf runs it); paper drafting is gated by the template + trace grounding (the model fills slots, never invents claims).

Consequences:

- Some problems will be ambiguous — the registry's invariant verifier rejects the LLM's plan and offers no automatic decomposition. The user resolves these manually (pick a pattern explicitly, or modify the problem statement). The harness never improvises a decomposition.
- The LLM-tier policy (ADR-035) governs which tier handles which step. Recommended defaults: decomposition proposal = `smart`; adapter code generation = `smartest`; paper drafting = `smartest`. Per-workspace overrides via the existing tier policy file.

### D5 — Federation routing reuses ADR-037 directory; new typed record types only

Compute work is gossipped through the existing federation directory (ADR-037 D2: discovery and state propagation are the same primitive). No new gossip transport, no new signing scheme, no new revocation channel. Three new record types extend the directory:

| Record type | Authority | Carries | TTL |
|---|---|---|---|
| `COMPUTE_OFFER` | self (signed by node's key) | Available CPU / GPU / RAM / disk / network classes; current load; classification ceiling; advertised pattern support | minutes |
| `COMPUTE_CLAIM` | self | `(plan_id, chunk_id, claimant_node_id, claimed_at, expected_completion_at)`; renewable | seconds (heartbeat-renewed) |
| `COMPUTE_RESULT` | self | `(plan_id, chunk_id, output_artifact_uri, kernel_attestation, elapsed_ms, signature)` | days |

Cohort-scoped by default per ADR-037 D6 (visibility is policy-bounded). Cross-cohort compute requires explicit `--public` per the same pattern as ADR-039 D6 federation share.

Rationale:

- Reusing the directory subsumes claim coordination, peer discovery, liveness observation, and revocation propagation in one mechanism. Inventing a parallel "compute scheduler" duplicates four already-built primitives.
- Typed records keep the primitive observable: `axi federation peers --offering compute` and `axi compute trace <plan_id>` are queries against the existing directory query API.
- Heartbeat-renewed `COMPUTE_CLAIM` records compose with the existing buddy-detection / liveness primitive (ADR-037 D5): a stale claim is a stale liveness query against the claimant; reassignment is a query, not a new mechanism.

Rejected alternatives:

- Custom RPC compute scheduler — duplicates ADR-037; consumers would have to integrate two federation surfaces.
- Object-storage-as-queue (drop chunks in S3, leaves poll) — invisible to the federation directory; no liveness observation; no classification routing.
- Centralized scheduler — single point of failure; violates the local-first survives-partition property of ADR-037 D9.

### D6 — Per-leaf runner contract: subprocess sandbox + background-tasks streaming + heartbeat

Every leaf runs every chunk in a subprocess with the **same sandbox model as ADR-039 D3**: `RLIMIT_AS` + `RLIMIT_CPU` + minimal `RLIMIT_NOFILE`, no inherited file descriptors, no inherited environment beyond an allow-list, no network unless the chunk's adapter explicitly declares network needs and the leaf's sandbox profile permits it (Seatbelt on macOS, seccomp+unshare on Linux). Containers are an opt-in `--container` flag for users who already have Docker; not the default.

Stdout / stderr / progress events stream back to the originating node via the existing `axi tasks` background-tasks primitive (`infra/tasks/`, landed on `feat/chat-surface-improvements`). Heartbeat every 5s. Chunk reassigned on heartbeat miss > 30s. The leaf may renew its `COMPUTE_CLAIM` to extend deadline on long chunks; the originating node may revoke a claim on policy violation.

Rationale:

- One sandbox model for all in-process LLM computation (Sci Displays) and all federation-routed compute (this primitive) reduces the operator's mental model from "two sandbox surfaces" to "one." Every per-language attestation rule generalizes.
- The background-tasks primitive already handles streaming, persistence across reconnects, and the chat-surface display of long-running work. Reusing it gives compute leaves the same UX as local long jobs from day one.
- Heartbeat-on-claim composes with ADR-037 D5 buddy detection: claim staleness is liveness staleness is reassignment trigger.

Rejected alternatives:

- Container-by-default — too heavy for laptop case; defers adoption; same reasoning as ADR-039 D3.
- In-process (no subprocess) — one bad chunk hangs the leaf agent; unacceptable.
- Custom streaming protocol — duplicates `axi tasks` for no consumer benefit.

### D7 — Aggregation is deterministic, runs on the originating node, produces a single signed MemoryFragment

The recomposer is **deterministic** by definition: given the same `ChunkResult` set, it produces bit-identical output. It runs on the originating node (the node that emitted the `Problem`), reads `ChunkResult` records from the federation directory, dereferences output artifacts (cached locally for the deterministic ones, fetched on-demand for stochastic ones), and produces a single output **MemoryFragment** via the existing CompositionService — populating `(T, U, A, R)` provenance with the originating user (T), the orchestrator agent (U), the LLM-tier policy that selected the decomposition LLM (A), and a `references` list naming every contributing `COMPUTE_RESULT` record (R).

Re-runs of the recomposer over the same `ChunkResult` set produce the same MemoryFragment hash. The aggregated fragment is the single citeable artifact for the run.

Rationale:

- The originating node is the right authority for aggregation: it owns the problem, it has the trust relationships with the contributing peers, and it is the natural attribution sink. Distributing aggregation across leaves invites Byzantine inconsistency.
- A single MemoryFragment as the run artifact slots into every existing memory consumer: the chat surface, the federation share mechanism, the candidate-paper drafter, the citation graph. No new artifact type.
- Bit-identical output from identical inputs is what makes content-addressed caching of *the aggregate itself* possible — re-runs of an identical study are free.

Rejected alternatives:

- Aggregation on a "scheduler" node — no such node exists in the federation; centralizing it reintroduces the failure mode ADR-037 D1 specifically rejected.
- Per-chunk-fragment with no aggregate — leaves the user with N fragments to cite; useless for paper drafting.
- LLM-mediated aggregation — direct violation of D4.

### D8 — Pre-emptive routing decision (estimate-before-decompose), not post-fail fallback

Before decomposing, the primitive runs a cheap deterministic **resource estimator** keyed on the registered pattern: matrix dimension, particle count × batch count, mesh node count, parameter-row count. The estimator classifies the problem's expected total work into `local (<5min) | local-with-tasks (5min–1h) | federation (>1h or estimated load > local capacity)`. Federation routing is offered to the user *before* decomposition runs.

Rationale:

- Same reasoning as ADR-039 D4: post-fail fallback wastes the user's battery and patience.
- The decomposition step itself is a non-trivial compute (especially for spatial-domain problems); deciding to decompose at all should be a first-class user choice, not an automatic consequence of running the primitive.
- The estimator is a heuristic; when wrong-low (we said "local," it took hours), the background-tasks primitive catches it; when wrong-high (we said "federation," it would have been local in 4 minutes), the user waved off federation and ran locally — no harm done.

Rejected alternatives:

- LLM-classified routing — same cost-routing-stakes objection as ADR-039 D4.
- Always-route-to-strongest-peer — burns trust budget; cohort federation will cease to be cooperative.
- Auto-route (no user prompt) — federation routing has cost-and-trust externalities the user must consent to per request.

### D9 — Candidate-paper drafting is template-driven, trace-grounded, attribution-aware

The primitive ships a **paper-template mechanism**: each domain extension registers one or more `PaperTemplate` records with named slots (`title`, `abstract`, `methods/decomposition`, `methods/per-leaf-kernel`, `methods/aggregation`, `results`, `figures`, `reproducibility-appendix`, `references`). Drafting is a single LLM call (`smartest` tier per ADR-035) that fills slots from the run trace under three hard constraints:

1. **Structural constraint** — every slot must be present; the LLM may not invent new sections.
2. **Grounding constraint** — every numeric claim, every named method, every figure caption must cite a specific trace artifact (DecompositionPlan, ChunkResult, MemoryFragment, ChartRender). The drafting prompt provides a `trace_index` mapping; the LLM must reference IDs from this index.
3. **Tone constraint** — academic register; declarative; no superlatives; no marketing language. Negative examples in the prompt.

The reproducibility appendix is **deterministically generated** (not LLM-drafted): it is a serialization of the DecompositionPlan, the seed table, the per-leaf kernel attestations, the federation directory snapshot at run-time, and the content hashes of every input deck.

Rationale:

- Template-driven ensures every paper has the same skeleton; trace-grounded ensures the LLM can't fabricate; attribution-aware ensures every claim is reviewable.
- The reproducibility appendix is the most important section and the easiest to mishandle if the LLM touches it; emitting it deterministically is the safe move.
- Per-domain templates let physics papers, chemistry papers, materials papers, biology papers each follow their field's conventions while sharing the core trace-grounding mechanism.

Rejected alternatives:

- Free-form LLM paper draft — fabrication risk; non-reproducible; not peer-reviewable.
- Single universal template (no per-domain variation) — fails to match field conventions; reviewers will reject.
- LLM-generated reproducibility appendix — the one section where any fabrication invalidates the entire paper; never let the LLM near it.

### D10 — Cohort-scoped by default; classification gates apply per chunk

Compute work flows within a single cohort by default (per ADR-037 D6 and the spec-classification-boundary rules). Each chunk inherits the `Problem`'s classification ceiling; eligible leaves are those whose advertised classification ceiling (`COMPUTE_OFFER.classification_ceiling`) is ≥ the chunk's classification level *and* whose nationality / EAR / ITAR posture satisfies the chunk's export-control stamp.

If no eligible leaf exists in the cohort for some subset of chunks, the primitive surfaces the gap to the user and offers three options: (a) reduce problem scope to the eligible peers' capacity; (b) run those chunks locally; (c) wave off the run.

Rationale:

- The classification boundary is the same gate as memory federation (ADR-027) and federated inference (ADR-030); compute inherits it with no special cases.
- Surface the gap to the user — never silently route an export-controlled chunk to an ineligible peer (the most expensive failure mode).
- Cross-cohort compute is the explicit `--public` opt-in (D5); never the default.

Rejected alternatives:

- Implicit cross-cohort routing (route to anyone willing) — directly violates classification spec invariants S1-S5.
- Cohort-bound with no surfaced gap — silent failures; bad UX.

### D11 — Honest scope boundary: this primitive does not solve all parallel computing

The primitive serves problems whose decomposition fits one of the registered patterns (D2). It does **not** serve:

- **Tightly coupled MPI codes requiring sub-100µs inter-rank latency.** Federation transport latency is orders of magnitude too high. These workloads stay on HPC interconnects.
- **Codes that cannot checkpoint or restart.** The reassign-on-failure protocol (D6) requires chunks to be restartable from input. Codes without this property need a wrapper or a code-change before they can decompose; the primitive will not silently retry-from-zero arbitrary work.
- **Codes that require shared memory across ranks.** Federation peers do not share memory; period.
- **Codes whose chunks are not independently sandbox-executable.** If a chunk needs root, raw network access, or a privileged kernel module, the per-leaf sandbox will refuse it. The primitive surfaces a structured refusal; it does not relax the sandbox.

The class the primitive serves: **embarrassingly parallel + loosely-coupled domain-decomposition + map-reduce + composite of those**, where each chunk is a self-contained restartable subprocess invocation. This is a large class (Monte Carlo, parameter sweeps, ensemble runs, many spatial PDE solves with Schwarz-style coupling, most map-reduce workflows). It is also the class with the highest gap between "what I can do on my laptop" and "what my colleagues' laptops could do collectively." That gap is the asymmetric-advantage demo's target.

Rationale:

- Honest scope is what makes the primitive trustworthy. Pretending to handle tightly-coupled MPI invites the demo failure ("ran on 9 laptops, gave nonsense") that destroys credibility.
- The covered class is large enough to dominate the day-to-day computational scientist's workload — most working scientists' "wait for the cluster" jobs are embarrassingly parallel or loosely coupled.

Rejected alternatives:

- Claim universal coverage — invites the destructive demo failure.
- Refuse to ship until tightly-coupled MPI works — refuses the 80% case for the 20% the federation cannot serve.

### D12 — Demo-day ergonomics: one chat command from problem to paper

The user-facing surface is a single chat command (or CLI: `axi compute run`) that takes a problem manifest, runs the full sequence (estimate → propose → verify → distribute → execute → aggregate → render → draft paper), and returns three artifacts: the aggregated MemoryFragment, the Sci Displays figure set, and the candidate paper draft. Every intermediate step is surfaced as background-tasks progress events; the user can cancel, inspect chunk-level results, or re-route mid-run.

Rationale:

- The asymmetric-advantage demo line — *"this 4-hour HPC job ran in 38 minutes across 9 of my friends' laptops while they were at lunch, and here's the paper draft"* — only lands if the user experience is "one command, three artifacts." Anything more complex breaks the headline.
- Background-tasks integration means the user can close the laptop lid and reopen to a finished run with a notification; the primitive does not require an attended terminal session.

Rejected alternatives:

- Multi-step user-driven workflow — kills the demo headline.
- Web-only UI — Edge users (laptops at conferences, planes) need it to work without a browser; CLI + chat are both required Phase 1.

### D13 — Phasing: behind a feature flag through Phase A; defaults on at Phase C

| Phase | Scope | Status gate |
|---|---|---|
| **A** (design + scaffolding) | This ADR + PRD + spec; primitive package skeleton; one `embarrassingly_parallel` pattern; one stub domain extension; flag-gated, off by default. | ADR + PRD + spec approved. |
| **B** (single-pattern e2e) | `embarrassingly_parallel` pattern + stochastic + deterministic trait routing; full federation directory wiring; first domain extension producing a real run; candidate paper drafted. | Working asymmetric-advantage demo on a small problem with 3-5 peers. |
| **C** (production patterns + GA) | `spatial_domain` + `temporal_stepping` + `composite` patterns; policy hardening; reproducibility-appendix invariant tests; defaults-on for cohort-scoped compute. | Demo on real problem with 8-10 peers; signed paper draft survives independent reproducibility check. |
| **D** (cross-cohort + advanced sandbox) | Cross-cohort compute opt-in; gVisor / Firecracker option for untrusted-peer compute; Platform-tier integration (HPC-cluster submit-as-leaf). | Federated study published with peers across institutions. |

Explicit non-target: **Prague June 2026 is not a target.** This primitive is post-Prague. Phase A may overlap with the Prague hardening window; Phase B begins after Prague. See `prd-compute-decomposition.md §7`.

Rationale:

- Prague delivery is the immediate priority per `project_prague_runway_plan_2026_04_29`; this primitive does not block it.
- Trying to deliver Phase B before Prague would compete for the same engineering attention; the right call is to land the design now (so it's ready to start Phase B post-Prague) and defer implementation.
- Domain extensions (a consumer's Fleet Compute) can also progress through Phase A (their own ADR + PRD + spec) in parallel without blocking core work.

---

## Consequences

**Positive:**

- A single primitive answers "how do I federate compute?" for every domain extension that wants it. The directory record types and chunk vocabulary are stable across consumers.
- The "LLM proposes, deterministic verifies" pattern proven in ADR-039 generalizes from math to entire computational studies — the same trust property scales to the largest unit of work the harness handles.
- Existing primitives (ADR-037 directory, ADR-035 LLM-tier policy, ADR-039 sandbox + Sci Displays figures, `axi tasks` background-tasks, CompositionService) all compose without modification; the primitive is genuinely thin.
- The asymmetric-advantage demo line — *"4-hour HPC job in 38 minutes on 9 friends' laptops, plus a paper draft"* — becomes a real demo, not a slideware claim.
- Cohort-scoped + classification-gated by default; cross-cohort opt-in only — never accidentally leaks export-controlled work to an ineligible peer.
- Honest scope (D11) makes the primitive trustworthy; users learn what it serves vs what it doesn't and the demo never embarrasses them.

**Negative / costs:**

- Three new federation directory record types (`COMPUTE_OFFER`, `COMPUTE_CLAIM`, `COMPUTE_RESULT`) join the existing eight; record-type registry needs governance review (per ADR-037 §Consequences).
- The decomposer/recomposer registry adds a new core schema concern; pattern additions require an ADR each (D2 by design — but it is real overhead).
- Candidate-paper drafting prompts are non-trivial to keep grounded; drift in the smartest-tier model behavior may require prompt re-engineering on each tier-policy update (per ADR-035 quarterly re-evaluation cycle).
- Resource estimator (D8) is heuristic; it will sometimes mis-classify. Mitigated by the user-prompt-before-routing UX, but there will be confused users in Phase B.
- Phasing (D13) means consumers waiting for `spatial_domain` or `temporal_stepping` patterns will be on hold through Phase B. Acceptable cost given the Prague constraint.

**What this ADR does NOT do:**

- Does not specify the on-the-wire format of `COMPUTE_*` records — that belongs to the spec (`spec-compute-decomposition.md §5`).
- Does not enumerate the full set of patterns to ship; only the closed shape of the registry. Pattern additions follow the standard ADR cycle.
- Does not replace HPC clusters for tightly-coupled MPI workloads. D11 is explicit.
- Does not specify the cross-cohort visibility policy in detail beyond "opt-in `--public`"; that interacts with ADR-027 federated memory propagation rules and may need its own follow-up ADR if cross-cohort compute becomes a hot path in Phase D.
- Does not specify Platform-tier (HPC cluster) submit-as-leaf integration; that is Phase D and may require a separate ADR for the submit/poll/retrieve protocol against an HPC scheduler.

---

## References

- `prd-compute-decomposition.md` — what this enables for users
- `spec-compute-decomposition.md` — module layout, schemas, runner contract, trait routing, aggregation invariants
- ADR-037 — federation directory (the substrate the new record types extend)
- ADR-039 — Sci Displays sandbox model + "LLM proposes / deterministic verifies" pattern (the precedent)
- ADR-035 + `spec-llm-tier-policy.md` — tier routing for decomposition / adapter / paper
- `spec-classification-boundary.md` — classification + EC gates that apply per chunk
- `feedback_axiom_domain_agnostic.md` — why this ADR names no domains

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
