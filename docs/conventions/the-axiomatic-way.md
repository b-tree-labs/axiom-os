# The Axiomatic Way

**Status:** Living doctrine  •  **Audience:** Extension developers, contributors, integrators  •  **Last updated:** 2026-04-24

The principles that shape Axiom, and the conventions we follow for consistency. Read this before you design an extension, propose a new subsystem, or extend the platform. When a design conflicts with a principle, the principle wins — or the doctrine changes deliberately, not by drift.

This document distills what already lives in Axiom's specs and ADRs. Each entry cites its primary source so you can go deeper. The distinction between **principles** (architectural commitments — changing them changes what Axiom *is*) and **conventions** (consistency choices we follow to stay legible) is deliberate.

---

## Principles

### 1. One substrate. No parallel graphs.

Every distributed concern — memory, retrieval, inference, trust, evaluation — uses the same four primitives: `axiom://` addressing, the trust graph, the four-scope policy coordinate, and cohort propagation. A proposal that would introduce a parallel addressing scheme, parallel trust graph, or parallel propagation protocol is rejected by the rejection test in ADR-029. Fit the substrate, or amend it deliberately.

*Why:* Combinatorial power is the promise of composition. It only materializes if combinations are cheap to express. One substrate keeps them cheap.

*Source:* [ADR-029](../adrs/adr-029-federation-composition.md).

---

### 2. Build on existing standards. Extend only what they can't express.

Use real DNS for service discovery. Real HTTP for content negotiation, caching, and conditional requests. Real TLS for mutual authentication. Real MCP for tool invocation. Real A2A for agent-to-agent communication. Real SKILL.md for skills. Real Sigstore for signatures. Axiom extensions live where those standards leave gaps — federation-native resolution, cross-node trust, classification-aware data flow — not where they already suffice.

*Why:* Parallel systems for already-solved problems create integration debt and burn developer trust. Reach of standard-compliant extensions multiplies across ecosystems.

*Sources:* [design-resolution-protocol §3.1](../working/design-resolution-protocol.md), [ADR-032](../adrs/adr-032-standards-positioning-dual-track.md), AEOS §3.

---

### 3. Self-sufficient by default. Federation is additive.

An Axiom node with no federation works fully by itself — local memory, local retrieval, local agents. Joining a federation adds delegation, remote authorities, and shared resources; it never creates a dependency the node can't live without. If the federation link breaks, the node degrades gracefully with a freshness notice, it doesn't fail.

*Why:* Offline-first is the only honest default for research and teaching environments. It is also the only honest default for classified contexts where the wire may be down by policy.

*Source:* [design-resolution-protocol §3.3](../working/design-resolution-protocol.md).

---

### 4. Deterministic code authorizes. Models advise.

Classification, policy, signature verification, approval routing, and cohort membership are deterministic code paths. Model output shapes behavior *within already-granted capability*; it never grants capability. A tampered or hallucinated prompt produces misbehavior, never privilege escalation.

*Why:* Security properties that depend on model output are not security properties. The trust boundary must be expressible in code a reviewer can read.

*Sources:* [spec-security §2](../specs/spec-security.md), AEOS §3.4.

---

### 5. Every result carries provenance and staleness.

Every answer the platform returns — a retrieval hit, a federation response, an agent assertion, a promoted finding — travels with: authority (who produced it), freshness (when it was produced, how long it's valid), trust path (the chain that led to this authority), and classification (access tier + stamp). Downstream consumers make decisions with full context.

*Why:* Without provenance, every answer is an oracle. With it, every answer is evidence.

*Sources:* [ADR-026](../adrs/adr-026-ownership-model.md), [design-resolution-protocol §3.4](../working/design-resolution-protocol.md).

---

### 6. The agent architecture is a Read-Eval-Print-Loop.

Signals come in (**Read** — SCAN), the system evaluates truth and researches gaps (**Eval** — CURIO), results leave the system as artifacts (**Print** — PRESS), and continuity is maintained across cycles (**Loop** — AXI). Service agents (TIDY for hygiene, TRIAGE for diagnostics, RIVET for releases) support the cycle without participating in it.

*Why:* A cognitive cycle with distinct ownership per phase is easier to reason about, test, and extend than a pipeline-with-special-cases. Each agent's correctness has a single definition.

*Source:* [axiom-repl-agent-framework](../working/axiom-repl-agent-framework.md).

---

### 7. Agent identity is canonical. The face is rebrandable.

Agent identities (SCAN, AXI, CHALKE) are durable. A deployment layer may present a different name to its users — "Neut" in one context, something else in another — but the underlying agent, its skills, and its lifecycle are invariant. Rebranding is a presentation choice; it is not a fork.

*Why:* It lets a single codebase serve multiple brands and audiences without fragmenting engineering. It is also how skill inheritance and cross-deployment knowledge sharing remain coherent.

*Source:* [axiom-repl-agent-framework §2](../working/axiom-repl-agent-framework.md).

---

### 8. Skills are learned, not wired.

A skill is a sequence of steps that produces value — a procedural recipe with YAML frontmatter at `<ext>/skills/<name>/SKILL.md` per the agentskills.io specification. Skills are standalone and composable; any agent with access may invoke them, and skills may invoke other skills.

An agent *acquires* a skill by declaring it in its manifest. At runtime the declaration causes the skill to be woven into the agent's context (today: into the system prompt's identity layer via `weave_agent_skills()`; over time: tool exposure, retrieval, fine-tuning). Learning is persistent — declaration stays, the weaving keeps happening, unlearning is explicit. A compromised SKILL.md shapes behavior, never authorization.

*Why:* Skills that are "registered but inert" are bureaucracy. Skills that affect runtime behavior are the point. The declaration is how the weaving is triggered; the permanence is how the agent grows over time.

*Sources:* AEOS §4.6, `src/axiom/agents/skills_runtime.py`, [spec-security §2.3](../specs/spec-security.md).

---

### 9. Shared platform primitives, not per-extension reinventions.

Cross-cutting platform capabilities — HTTP serving, storage, observability, auth, logging, identity — live as canonical built-ins. Extensions register with them. They do not reimplement them. This is principle 1 applied inside a single node.

*Why:* Each extension shipping its own HTTP server is the parallel-graphs problem at the platform-capability level. Shared primitives mean one place to add middleware, one place to swap the implementation, one place to audit.

*Source:* AEOS §3.5; applied throughout the extension catalog.

---

### 10. Extensions are portable units.

An extension is a self-contained unit. Its docs, tests, README, CHANGELOG, and manifest live inside its directory. Extracting an extension into its own repository is one `git filter-repo --path <ext>/` command, not an archaeology exercise. Core repo-level docs hold only platform-scope content.

*Why:* Portability is not a future concern to solve later — it is the constraint that keeps coupling visible now.

*Source:* [ADR-031](../adrs/adr-031-extension-self-containment.md).

---

### 11. Two operating modes. The boundary is the API.

An Axiom node runs in one of two modes. **Standalone** — the platform is the runtime, with end-to-end enforcement of classification, policy, and approval. **Paired with another agent harness** — external harnesses (MCP-capable, A2A-capable) consume Axiom capabilities over the network; Axiom's tool handlers are the server-side enforcement point. In both modes, authorization is server-side and uncircumventable by the client.

*Why:* Enterprise consumers already think about cloud services this way (AWS enforces IAM on API calls; it does not control what happens with downloaded data on a laptop). Drawing the trust boundary at the API is the honest version of that contract for agents.

---

### 12. Platform hooks shape behavior. They never grant capability.

Cross-cutting behavior — audit, cost metering, classification gating, approval routing, prompt injection scrubbing — is wired through the platform's hook surface, not by forking the runtime. Extensions and operators declare hooks in the manifest (`[[extension.provides]] kind = "hook"`) or drop a Python file at `$AXIOM_HOME/hooks/<event>.py`; the platform fires a fixed taxonomy of lifecycle events (`tool.pre_invoke`, `prompt.pre_submit`, `cli.command_started`, `federation.pre_accept`, etc.) and the registered hooks see them in priority order.

Hooks are **not** an authorization layer. A hook can deny a tool call within the bounds the principal is already authorized for, or rewrite an argument the caller could have rewritten themselves; it cannot elevate trust, bypass the trust graph, or skip federation policy. Those are deterministic-code paths upstream of hook dispatch (per principle #4). A tampered hook produces *misbehavior* (incorrect denial, log noise), never *privilege escalation*.

*Why:* Every peer harness already has hooks (Claude Code lifecycle scripts, Cursor middleware, LangGraph guardrails). Without them, every cross-cutting requirement becomes a runtime fork. With them, an audit log is one manifest entry plus one Python function. Keeping hooks behavior-shaping (not capability-granting) preserves the trust boundary while opening the harness up to the same composability the rest of the substrate enjoys.

*Source:* [prd-hooks](../prds/prd-hooks.md), [spec-hooks](../specs/spec-hooks.md), AEOS §4.7.

---

## Conventions

Conventions are consistency choices. They could reasonably be otherwise, but switching them once agreed creates needless friction. Follow them.

### Naming

- **Products** use normal case: **Axiom, Vega, Keplo, Vyzier** (and any domain consumer that builds on the platform). Never all-caps, never hyphenated.
- **Agents** use ALL-CAPS-HYPHEN (the AXI convention): **SCAN, TIDY, PRESS, TRIAGE, CURIO, AXI, CHALKE, WARDEN, RIVET**.
- **Extensions** are lowercase, purpose-named, no type suffix: `classroom/`, `publishing/`, `release/`, `signals/`, `diagnostics/`. Never `signals_agent/` or `publisher_cmd/` — capability information belongs in the manifest, not the directory name.

A single glance distinguishes them: "Keplo's CHALKE sends the brief" — product, agent, extension — is unambiguous.

**Singular vs plural.** Singular for activities, states, and mass nouns: `chat/`, `research/`, `publishing/`, `release/`, `hygiene/`, `memory/`, `classroom/`, `http/`. Plural when named after a stream or collection: `signals/`, `diagnostics/`, `skills/`, `agents/`. When both feel natural, pick whichever reads right in prose — "the signals extension" reads right; "the signal extension" sounds like one specific signal.

**Principals.** `@name:context`, Matrix-style, single `@`.

*Source:* [brand-product-strategy](../working/brand-product-strategy.md) §Naming, AEOS §5.4.

### Layout

- Extensions follow the canonical compound layout (AEOS §5.1): purpose-named root, nested package directory, seven capability-kind subdirectories, co-located `docs/` + `tests/`, `README.md`, `CHANGELOG.md`, manifest.
- Tests live with code, not in a separate tree, unless the test spans extensions (cross-cutting).
- Documentation for an extension lives inside the extension. Repo-level `docs/` holds only platform-scope content.

*Source:* [ADR-031](../adrs/adr-031-extension-self-containment.md), [spec-extension-layout](../specs/spec-extension-layout.md).

### Authoring discipline

- Every memory write goes through `CompositionService`. Don't bypass it for direct fragment construction.
- Provenance `(T, U, A, R)` is fixed at write time.
- IDs are auto-generated. Callers do not invent identifiers on create.
- Tests are written before the implementation they describe, always.

*Source:* `CLAUDE.md`, `AGENTS.md`.

### Voice

Trustworthy over clever. Clear over comprehensive. Confident over hedging. Technical over marketing. Writing, error messages, commit messages, and user-facing output all follow the same voice.

*Source:* [spec-brand-identity](../specs/spec-brand-identity.md).

---

## What this doctrine is not

- **Not a specification.** Specs live in `docs/specs/`. The AEOS specification is the authoritative document for the extension format and runtime contract. This doctrine explains *why* we chose what's in the spec.
- **Not a roadmap.** OKRs live in `docs/prds/prd-okrs-2026.md`.
- **Not an onboarding tutorial.** New-contributor orientation is `AGENTS.md` / `CLAUDE.md`.
- **Not exhaustive.** If a principle is missing, write it here with a concrete example and a source. Do not invent; ground.

## How to change this doctrine

1. Open a PR against this file with the proposed change.
2. Cite the sources the new principle draws from. If you can't cite a source, the principle isn't ready.
3. Update any ADRs or specs whose text would contradict the principle in the same PR.
4. Review bar: the Axiom maintainers. Principles touching federation, governance, or agent architecture get broader review.

---

## Prior art worth studying

- [Substrate TAP review](../working/design-resolution-protocol.md#2-substrate-tap-review--what-to-adopt-what-to-skip) — what we adopted (DADP, CPAC, CBAC) and what we skipped (MASR, MAMA, BAAT, VITAL, OASIS, SubCert), with reasoning for each.
- The agentskills.io specification for skills packaging.
- MCP, A2A, SKILL.md, AGENTS.md, Sigstore — the standards Axiom builds on.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
