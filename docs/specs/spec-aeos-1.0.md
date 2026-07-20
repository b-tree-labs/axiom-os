# Agent Ecosystem Open Standard (AEOS) — Version 1.0

> **DRAFT.** §§1–7 authored. §§8–19 + appendices remain skeletal pending the next drafting round.

**Status:** Public Preview — 1.0-draft (per Path 3 soft-launch; full publication post-Prague validation)
**Version:** 1.0.0-draft
**Editor:** Benjamin Booth
**Date:** TBD
**Predecessor:** [AEOS 0.1](spec-aeos-0.1.md)

---

## Abstract

The Agent Ecosystem Open Standard (AEOS) is a foundational specification for platforms that host, govern, and federate AI agents. AEOS 0.1 was a packaging and declaration standard — a manifest format, capability kinds, signed-release expectations. AEOS 1.0 retains and refines that surface, and adds the foundational layers that experience proved necessary: enumerated agent classes, layer authority, checks and balances, conflict-resolution authority, user rights, due process, equal protection, and succession.

The acronym is preserved across the version transition; the spelled-out name expands from *Agent Extension Open Standard* (0.1) to *Agent Ecosystem Open Standard* (1.0) to honor the broadened scope.

AEOS does not replace existing standards. It composes them — MCP, A2A, SKILL.md, OpenAPI, Sigstore, OASF — and adds the foundational layers those individual standards do not address. Conformant runtimes implement AEOS as their substrate; conformant extensions declare against it; conformant federations compose under it.

The specification is authored in normative language (RFC 2119: MUST, SHOULD, MAY) where the rule is load-bearing, and in measured-bias language ("conformant runtimes typically...", "by default...") elsewhere. Absolutes are reserved for cases where any softening would invite the failure mode the rule prevents.

---

## A note on reference implementations

This specification cites *reference implementations* throughout — Axiom (the platform), AXI and the agent team (Axiom's canonical agents), `axi` (Axiom's CLI surface). These citations are illustrative, not normative. The specification is portable: a different platform may implement AEOS with different agents, a different CLI, a different storage substrate, and remain conformant.

When a reference-implementation example appears, it is marked: *(reference: Axiom)*. Treat unmarked text as portable normative content.

---

## 1. Preamble

The standard exists in service of three conditions whose tension defines the era this specification is written into:

**Sovereignty.** Organizations and individuals operating intelligent systems require sovereignty over their data, their decisions, and their agents. Off-the-shelf platforms typically force a choice between hosted convenience (cede sovereignty) and on-premise isolation (lose collaboration). AEOS refuses that tradeoff: federation is a first-class concern, and sovereignty is the default posture.

**Cooperation.** Sovereign operators benefit immensely from peer cooperation — across labs, classrooms, factories, plants, military deployments, company organizations, devops teams, content pipelines. The shared theme is an industrial or commercial need for strong security paired with federation across cohorts. AEOS specifies the trust, identity, and accord primitives that make cooperation safe.

**Accountability.** Every action a system takes is traceable to a human who is, ultimately, accountable. Agents may advise, propose, execute under delegated authority — but the authority is always human in origin and human in last appeal. AEOS encodes this as architecture, not policy: provenance is universal, override is universal, and the rights enumerated in §9 are inviolable.

The standard's bias is consequently:

- *Federation-native* — peer cohorts compose without ceding sovereignty.
- *Human-accountable* — the human is the corrective for what the platform cannot detect; conformant runtimes make this practical.
- *Deterministic where it counts* — LLMs may advise; deterministic code authorizes the consequential operations.
- *Provenance-everywhere* — the audit trail is the architecture's evidence handle, not an afterthought.
- *Tier-respecting* — surfacing rules govern visibility; they do not govern available capability.
- *Engineered-gravity, not enforced-parity* — the platform shapes surfaces so the right path is the path of least resistance; it does not pretend to enforcement it does not have.

The body of this specification operationalizes these biases. Where this preamble's language and a later section's language appear to disagree, the later section governs — but the apparent disagreement is itself a signal worth investigating.

---

## 2. Foundational Principles

AEOS 1.0 is designed against twelve principles. The first seven carry forward from AEOS 0.1; the next five were earned in the work between 0.1 and 1.0. Every later specification detail derives from these.

### 2.1 Self-containment

An AEOS extension is a single directory with all artifacts it owns: source code, tests, documentation, manifest, changelog, license. Extraction from a monorepo to a standalone repository requires no restructuring — just moving the directory. *(Carries from 0.1 §3.1.)*

### 2.2 Purpose-driven naming

Extensions are named by what they do or which domain they serve, not by what type they are. Type information lives in the manifest, not the directory name. Hyphens, underscores, or domain-prefix conventions are platform-specific; AEOS specifies only that the name be purpose-driven. *(Carries from 0.1 §3.2.)*

### 2.3 Compound by default

Every extension is scaffolded with the canonical compound layout — typed subdirectories per capability kind. Extensions that ultimately provide only one capability kind are compound extensions with only one populated subdirectory. The uniformity removes decision burden at authoring time and makes refactoring (adding a second capability kind later) frictionless. *(Carries from 0.1 §3.3.)*

### 2.4 Deterministic trust boundary

LLMs may advise; deterministic code authorizes the consequential operations. AEOS trust profiles, RACI gates, classification stamps, and accord enforcement are all deterministic primitives. The boundary is load-bearing: collapsing it (allowing the LLM to authorize) is the failure mode the principle prevents. *(Carries from 0.1 §3.4.)*

### 2.5 Capability declaration via entry points

Capabilities are declared distributively — registered through module imports (in the reference implementation, Python entry points). The manifest enumerates provided capabilities for discovery and validation; the registration is language-native, allowing refactors without manifest churn. The manifest is authoritative for validation; the entry points are authoritative for loading. *(Carries from 0.1 §3.5.)*

### 2.6 Signed releases by default

Conformant releases are signed via Sigstore's keyless OIDC flow. Installers verify signatures before executing extension code. Unsigned releases install only with explicit override, and the override is recorded with user acknowledgment. The principle responds to a real and recurring failure mode: malicious or compromised extensions injected into a trusted distribution channel. *(Carries from 0.1 §3.6, softened from absolute.)*

### 2.7 Federation-native where applicable

AEOS extensions can participate in multi-cohort federation: signed attestations travel with them, trust-profile compatibility is declared, quarantine and recovery ceremonies are first-class. These features are optional — a local-only extension is a valid AEOS extension that ignores federation metadata. *(Carries from 0.1 §3.7.)*

### 2.8 Engineered gravity, not enforced parity

The platform shapes surfaces so the right path is the path of least resistance; it does not pretend to enforcement it does not have. When a capability has a well-typed canonical surface (e.g. a CLI verb), conformant runtimes bias toward that surface for matching intents — through tool consolidation, prompt templating, surface design — without preventing the underlying capability from being reached through other paths. The bias is honest about its limit: gravity, not enforcement.

### 2.9 Pattern reuse over invention

Affordances pick existing structural templates (the four-step approval flow; the three-part error format; the severity-tagged finding shape; the footer summary; the corner-frame; the tier-graduation hint). Adding a new affordance requires picking an existing pattern unless none fits — and inventing a new pattern requires documentation. The principle preserves coherence as the surface grows.

### 2.10 Tier governs presence, not power

Surfacing rules — capability tiers, per-extension familiarity, intent groups, and **brand/portfolio tier** — gate *automatic visibility* of capabilities. They never gate *available invocation*. A user who explicitly addresses a capability above their current tier reaches it; the surfacing rule only governs whether the capability is suggested unprompted. The principle ensures the platform never withholds power, only manages discovery.

**Brand/portfolio tier** is the cross-product surfacing dimension. When several portfolio packages share an environment — a platform package plus one or more domain distributions, each with its own branded entry point — the active brand surfaces its own tier and the platform base it builds on, not a sibling or parent product's extensions. This mirrors how an OS distribution composes: the borrowed base keeps neutral, stable names (coreutils/systemd are never re-branded per distro), and the distribution's identity is data, not a renaming of the base. A package self-declares its tier (in the reference implementation, the `axiom.portfolio_member` entry point) — distinct from the manifest `owner` field, which is attribution, not tier. The exact OSS precedent is the freedesktop `.desktop` `OnlyShowIn` / `NotShowIn` / `NoDisplay` keys: an installed, fully-invocable entry that a given environment chooses not to show in its menu. Because brand/portfolio scoping is *surfacing*, discovery and invocation remain universal across brands; only the listing is scoped. (See Axiom ADR-048.)

### 2.11 Provenance is universal

Every action a conformant runtime originates carries a `via=` field identifying its source (terminal, chat, remote-trigger, accord-mediated, etc.) plus the actor's signed identity. Receiving runtimes treat the absence of provenance as an integrity signal. The audit trail is the architecture's evidence handle; conflict detection, accord enforcement, and trust-graph composition all rely on it.

### 2.12 Sovereignty defaults down

Powers not explicitly delegated to higher layers are reserved to lower ones. Platform / cohort / extension / user is the default authority hierarchy; in any unspecified situation, the lower layer's sovereignty is the default. The principle is the constitutional analogue of Tenth-Amendment reservation; it makes the platform's posture toward its users one of explicit delegation rather than implicit authority.

### 2.13 State externalization

Agents MUST NOT carry, in process memory, state on which observable behavior depends. State that crosses any invocation lives in exactly one of: (a) the persistence layer of the platform (per ADR-052); (b) the configuration store of the platform (per ADR-058, exposed via `axiom.infra.config`); (c) a federation-replicated store. In-process caches are permitted only as derived projections of one of these stores, and MUST be invalidated by the store's change events.

The principle is the architectural property reactor-class deployments (where restarts are restricted) and cloud-class deployments (where restarts are frequent) demand identically: an agent that loses memory at any moment behaves the same as an agent that does not. Conformant runtimes therefore SHOULD prefer ephemeral one-shot invocation (per `[extension.runtime_mode]`) for agents, and MUST provide a watched-configuration mechanism so long-running daemons can absorb behavior changes without restart.

The state-externalization rule is the unifying principle the eight clauses 2.1–2.12 compose toward. It is the principle every other AEOS conformance check, in any sub-section, may be re-derived from.

### 2.14 Configuration is durable + watched + auditable

Conformant runtimes MUST expose a configuration primitive with the following properties:

- **Schema-validated.** Each extension declares its configurable fields with type + classification + lockable flag. Writes that fail validation do not take effect.
- **Watched at filesystem (or equivalent) level by default.** Changes to the externalized configuration propagate to in-process subscribers without requiring restart of the consuming process.
- **Receipt-emitting.** Each change produces an audit fragment per the governance fabric (ADR-055), classified at the receiving field's classification floor.
- **Lockable.** A field MAY be declared `lockable=true`; in that case, an explicit `lock` operation commits a value pending an authority's override. The lock-override authority composes with the keystore + capability primitives but is OUT of scope for the configuration primitive itself: this section requires the predicate, not the cryptographic enforcement.
- **Importable, not reinvented.** Extensions MUST consume the platform's configuration primitive (`axiom.infra.config` in the Axiom runtime) and MUST NOT roll their own watched-config layer.

The reactor lesson is the lock + receipt path; the cloud lesson is the watch + ephemeral path; the unifying lesson is that one primitive serves both because both want the same property.

---

## 3. Agent Class Enumeration (normative)

AEOS 1.0 enumerates ten canonical agent classes. Each describes a *capability shape* — the verb an agent in that class principally embodies. Class membership describes shape, not exclusive ownership: agents MAY span multiple classes when their behavior matches multiple verb shapes; classes MAY have multiple canonical agents.

Conformant runtimes:

- MUST recognize the ten classes by name in their manifest validation
- MUST allow an agent's manifest to declare membership in one or more classes
- SHOULD record agent class membership in the provenance ledger so cross-class action sequences are auditable

| Class | Verb | Reference implementation: Axiom |
|---|---|---|
| Orchestrator | coordinate, route, dispatch | AXI; CHALKE (classroom dispatch) |
| Generator | produce, synthesize, transform | CURIO, PRESS, CHALKE (prep) |
| Steward | maintain, sweep, observe resources | TIDY |
| Sensor | observe, detect, extract | SCAN; TIDY (vitals) |
| Reviewer | inspect, validate, gate | REV-U; RIVET (CI gates) |
| Governor | set policy, propose accords | WARDEN |
| Federator | peer, bridge cohorts | WARDEN |
| Attester | verify, sign, vouch | (identity-layer agents) |
| Shepherd | build, tag, ship, watch | RIVET |
| Combatant | contest, defend, oppose | (red-team, defense, enforcement) |

### 3.1 Orchestrator

**Verb shape:** coordinate, route, dispatch.

An orchestrator receives intent (from a human, another agent, or a triggering event) and routes it through the system — to sibling agents, to CLI verbs, to external tools. The orchestrator does not principally *produce* artifacts (that's a Generator) or *enforce* policy (that's a Governor); its role is the routing fabric.

**Required capabilities:** intent parsing; action plan construction; dispatch to declared sibling classes; result aggregation.

**Distinguished from neighbors:**
- *Not Generator* — it routes work, doesn't produce work
- *Not Governor* — it dispatches under existing policy, doesn't set policy
- *Not Reviewer* — it routes results, doesn't gate them

### 3.2 Generator

**Verb shape:** produce, synthesize, transform.

A generator produces new artifacts from inputs — documents, code, summaries, plans, course materials, diagrams, reports, tests. Two recognized sub-shapes:

- **Synthesizer** — combines existing material into something new (research synthesis, summarization, RAG-grounded composition)
- **Producer** — originates or transforms a target artifact (markdown to polished document, spec to code, requirements to test cases)

**Required capabilities:** input validation; idempotence-where-possible (same inputs → same artifact); explicit failure modes when inputs are insufficient; provenance citation of sources used.

**Distinguished from neighbors:**
- *Not Steward* — it adds artifacts, doesn't maintain them
- *Not Attester* — it produces work, doesn't certify it (though it MAY sign its outputs)

### 3.3 Steward

**Verb shape:** maintain, sweep, observe resources.

A steward keeps the operational substrate healthy — disk usage, scratch directories, stale state, leak detection, retention enforcement, hygienic sweeps. The steward acts continuously and largely autonomously; its work is mostly invisible until it surfaces a hygiene concern.

**Required capabilities:** scheduled or event-driven sweeps; observability into the resources it stewards; non-destructive default with explicit-confirm on destructive operations; provenance for every state change.

**Distinguished from neighbors:**
- *Not Sensor* — Sensor *observes* signals; Steward observes *resources* and acts on them
- *Not Combatant* — it maintains by routine, not by contest

### 3.4 Sensor

**Verb shape:** observe, detect, extract.

A sensor observes continuous streams (events, logs, network traffic, document edits, federation chatter) and extracts signals — patterns, anomalies, summaries. Sensors are passive by default; they raise concerns rather than act on them.

**Required capabilities:** stream ingestion; pattern matching or learned classification; signal triage; emission of structured signal events for downstream consumption (Orchestrator, Steward, Combatant).

**Distinguished from neighbors:**
- *Not Steward* — it observes signals; Steward acts on resources
- *Not Reviewer* — it surfaces patterns continuously; Reviewer gates discrete artifacts

### 3.5 Reviewer

**Verb shape:** inspect, validate, gate.

A reviewer inspects a discrete artifact (a code change, a document, a model release, a configuration update) and produces a verdict — pass, conditional pass, fail — with cited evidence. Reviewers gate discrete events; sensors observe continuous streams.

**Required capabilities:** structured artifact ingest; one or more verification passes (correctness, security, style, policy compliance, etc.); validator gates that filter false positives; severity-tagged findings.

**Distinguished from neighbors:**
- *Not Sensor* — it gates discrete events with verdicts; Sensor observes streams without verdicts
- *Not Governor* — it applies existing policy; Governor sets it

### 3.6 Governor

**Verb shape:** set policy, propose accords.

A governor authors policy — accords between agents, capability scopes, exception lists, escalation rules — and proposes them to accountable humans for ratification. Governors do not enforce policy directly (that's typically a Combatant or a runtime check); they author the rules under which others operate.

**Required capabilities:** detection of policy gaps or conflicts; accord proposal authoring; submission for accountable-human approval; recording ratified accords as joint-provenance memory artifacts.

**Distinguished from neighbors:**
- *Not Federator* — Federator mediates peer-to-peer cohort traffic; Governor authors the rules of the road
- *Not Combatant* — Governor authors policy; Combatant enforces it when softer options fail

### 3.7 Federator

**Verb shape:** peer, bridge cohorts.

A federator mediates traffic across cohort boundaries — peer registration, attestation exchange, gatekeeping at federation joins, quarantine ceremonies, trust-graph state propagation. The federator is the agent at the cohort edge.

**Required capabilities:** peer registry maintenance; attestation verification; gatekeeping per the cohort's admission policy; participation in quarantine and recovery ceremonies; trust-graph contribution.

**Distinguished from neighbors:**
- *Not Governor* — Federator mediates per existing policy; Governor authors the policy
- *Not Attester* — Federator verifies external attestations as a routing concern; Attester originates them

### 3.8 Attester

**Verb shape:** verify, sign, vouch.

An attester originates and verifies cryptographic claims — identity verification, capability attestations, signed extension releases, key rotations, vouching for principals. The attester is the cryptographic ground truth for all the other classes that consume signed material.

**Required capabilities:** key management; signature generation and verification; attestation issuance with declared scope and lifetime; key rotation procedures; revocation handling.

**Distinguished from neighbors:**
- *Not Federator* — Attester originates attestations; Federator routes traffic that bears them

### 3.9 Shepherd

**Verb shape:** build, tag, ship, watch.

A shepherd manages artifact lifecycle from authoring through release — build orchestration, test gating, version tagging, release signing (often via an Attester), distribution, and post-release watching. The shepherd is the agent that walks artifacts through their lifecycle phases.

**Required capabilities:** build pipeline orchestration; release-tag management; ship-or-block decisions per validation gates; post-release monitoring and alerting; rollback support.

**Distinguished from neighbors:**
- *Not Reviewer* — Shepherd manages the *pipeline*; Reviewer gates discrete points within it
- *Not Steward* — Shepherd walks artifacts through phases; Steward maintains operational state

### 3.10 Combatant

**Verb shape:** contest, defend, oppose.

A combatant contests, defends, or opposes — red-team probing, threat detection and response, negotiation on behalf of a principal, enforcement when softer accord options (yield, defer, coordinate, escalate) have failed. The combatant exists because cooperation is not the only mode the system must support; some interactions are adversarial by design or by failure.

**Required capabilities:** declared adversarial posture (probe, defend, contest); scoped authority for the action shape; explicit accountability chain (combatant actions are particularly sensitive to provenance); compliance with §6 checks-and-balances (no unilateral action on critical paths).

**Distinguished from neighbors:**
- *Not Governor* — Combatant enforces; Governor authors what is enforced
- *Not Reviewer* — Reviewer gates with verdicts; Combatant takes action against threats or contested positions

### 3.11 Multi-class membership

Agents MAY span multiple classes when their declared behavior matches multiple verb shapes. The reference implementation contains several such agents:

- **WARDEN** (reference: Axiom) — Federator + Governor: federation gatekeeping (Federator) plus accord authoring (Governor)
- **TIDY** (reference: Axiom) — Steward + Sensor: resource maintenance (Steward) plus vitals observation (Sensor)
- **CHALKE** (reference: Axiom) — Generator + Orchestrator: course-prep authoring (Generator) plus classroom session dispatch (Orchestrator)
- **RIVET** (reference: Axiom) — Shepherd + Reviewer: lifecycle pipeline (Shepherd) plus CI quality gates (Reviewer)

Multi-class membership is declared per-capability in the manifest. A single agent's manifest MAY have multiple `kind=agent` provides blocks, each declaring membership in a single class — or a single `kind=agent` block with a `classes` array. Either form is conformant.

---

## 4. Capability Kinds

AEOS 1.0 defines eight capability kinds. The list extends AEOS 0.1's seven by adding `prompt` (formalized in 0.1.x point releases) and remains stable within the 1.0 major version.

| Kind | Purpose | Inherited from 0.1? |
|---|---|---|
| agent | LLM-backed autonomous component with persistent identity | Yes (with class-membership declaration added) |
| tool | Stateless callable with typed I/O schemas | Yes |
| cmd | CLI noun-verb grouping | Yes (with verb-grammar rule added in 0.1.x) |
| service | Long-running daemon | Yes |
| adapter | Third-party integration | Yes |
| skill | SKILL.md-format model-mediated instructions | Yes |
| hook | Lifecycle interceptor | Yes |
| prompt | Templated MCP prompt the platform publishes | Added in 0.1.x; formalized at 1.0 |

Per-kind specifications carry from AEOS 0.1 §4 substantially unchanged, with the following 1.0-era additions:

- **agent**: declares one or more class memberships per §3
- **cmd**: verb-grammar rule (imperative verbs, not bare resource nouns) per AEOS 0.1.x point release
- **prompt**: extension contributions extend platform prompts via declared fill points; provenance markers wrap each contribution

Full per-kind detail in 0.1 §4 is incorporated by reference; this section enumerates only the deltas.

### 4.x Persistence — the `[database]` manifest block (forward-referenced, ADR-052)

Extensions that need an RDBMS declare it in a top-level `[database]` block in `axiom-extension.toml`:

```toml
[database]
needs_schema = true
migrations_path = "migrations"    # default; optional
```

This is not a capability kind — it is a top-level extension manifest block (parallel to `[extension.federation]` and `[extension.signing]`). Implementations:

- The platform's `axiom.infra.db.DatabaseProvider` owns one Postgres per Axiom install; each extension owns one Postgres schema named after itself; the provider sets `search_path` per-connection so unqualified table names resolve to the extension's schema.
- Extensions use `axiom.infra.db.session_for("<ext>")` for runtime sessions and `axiom.infra.db.engine_for("<ext>")` in their Alembic `env.py` for migrations.
- Within-extension multitenancy is a documented menu, not a single answer: single-tenant default, row-level `tenant_id`, or schema-per-tenant. Picked at scaffold time.
- Cross-extension reads go through the data platform (ADR-049), not OLTP joins.

Full mechanism: **ADR-052**. The schema integration into this spec — lint enforcement, scaffold picker generators, `axi db migrate` cross-extension orchestration — lands in the spec's next revision (tracked: axiom-os#265 item 10).

---

## 5. Layer Authority

AEOS recognizes four layers of authority. Powers not explicitly delegated to higher layers are reserved to lower ones, per principle 2.12. The default authority — when no explicit delegation specifies otherwise — is user sovereignty.

| Layer | Owns | Cannot override |
|---|---|---|
| **Platform** | Specification conformance; runtime execution; signed-release verification; cross-layer arbitration | User §9 rights; cohort sovereignty within cohort scope |
| **Cohort** | Federation-level policy; peer admission; cross-cohort accords; cohort-scoped trust graph | User §9 rights; extension-internal decisions outside cohort policy scope |
| **Extension** | Extension-internal logic; capability implementation; declared accord patterns | Platform conformance rules; cohort policies the extension agreed to on join |
| **User** | All powers not explicitly delegated upward | The user's own delegations to extensions or cohorts (until revoked per §9) |

When a decision falls in unspecified territory, it falls to the lowest layer with relevant scope (typically the user). When two layers' authority appears to overlap, §8 (Supremacy + Precedence) governs.

---

## 6. Checks and Balances

Critical-path actions — actions whose effects are difficult or impossible to reverse — SHOULD NOT be taken by a single agent class without sibling validation. Conformant runtimes enforce this for the action types in §6.2.

### 6.1 Why this is bias, not absolute

Sibling validation is bias because:

- Sibling agents may be unreachable (network partition, degraded peer, key rotation in flight)
- The cost of waiting may exceed the cost of the action's reversibility
- Some actions are time-critical (a Combatant defending against an active threat may not have the latency budget for sibling validation)

Conformant runtimes therefore implement sibling validation as a **default that can be overridden with declared cause**. The override is logged with provenance; repeated overrides without subsequent ratification trigger Governor review.

### 6.2 Action types requiring sibling validation by default

Action types listed here SHOULD require sibling validation by default per the conventions above. The list is normative; conformant runtimes MUST recognize these classifications:

- **Cross-cohort writes** (Federator action): require Attester validation of the receiving cohort's admission policy compliance
- **Policy ratification** (Governor proposal accepted): requires accountable-human assent (humans are not "siblings" in the agent-class sense, but the validation discipline is identical)
- **Combatant offensive actions** (active probe, defensive intervention, enforcement): require Governor authorization plus Reviewer review of intended scope
- **Identity claims** (new attestation issued): require existing-Attester chain-of-trust validation
- **Destructive Steward operations** (purge, mass-revocation, archive deletion): require Reviewer pre-approval
- **Release ship** (Shepherd ships an artifact): requires Reviewer pass on quality gates
- **Generator outputs entering a sealed corpus** (community RAG, archived knowledge base): require Reviewer pre-approval

### 6.3 Trust-graph composition

Where multiple agents (or multiple instances of the same agent class) might validate, the relative weight of their voices follows the conformant runtime's **trust-graph mechanism** — a structure that records observed reliability, signed peer endorsements, and lifecycle attestations into per-agent and per-cohort weights *(reference: Axiom's ADR-028)*. Conformant runtimes:

- MUST record validation events with the validating agent's signed identity
- SHOULD allow validation thresholds to be expressed in trust-weighted terms (not just simple counts)
- MAY implement quorum policies for cohort-level validation

### 6.4 Failure mode when sibling validation is unreachable

When the required sibling agent is unreachable within a runtime-defined timeout:

- The acting agent SHOULD record the unreachability, fall back to a conservative default (yield rather than act), and surface the situation for human attention
- Where conservative-yield is itself unsafe (a Combatant defending against an active threat), the runtime MAY allow proceeding under declared override — but the override is logged, the original validation requirement remains pending, and post-action ratification is required

The principle: **degraded validation degrades to escalation, not to silent unilateral action.**

---

## 7. Conflict Resolution Authority

When agents, accords, or layer authorities conflict, AEOS 1.0 specifies five clauses governing how the conflict is resolved.

### 7.1 Authority hierarchy

The default chain of authority for accord approval and conflict adjudication is:

| Step | Actor | Notes |
|---|---|---|
| 1 | **Accountable human** | Human-of-record for the affected scope: resource owner, project lead, instructor, operator. Final authority by default. |
| 2 | **Designated steward** | A *human* delegated authority by the accountable human; same kind of actor with narrower scope. |
| 3 | **Service agent** | An *agent* that may auto-approve only within a pre-approved policy the accountable human signed off on. The service agent never originates authority — it only exercises authority humans delegated. |
| 4 | **Cohort steward** | A *human* delegate at the federation level, designated for cross-cohort decisions. |

Three of the four steps are humans. The single agent-step (service agent) acts only within the pre-approved policy envelope.

#### 7.1.1 Agent authority over a human (limited and controversial)

Agent authority over a human is **not granted by default** and requires explicit, narrow, human-signed delegation. Envisioned scenarios are limited and controversial:

- **Attestation lockout** — A human under attestation lockout (e.g. compromised key, identity dispute) may have an agent enforce the lockout against their human override until the dispute is resolved by a higher authority.
- **Duress safe-state** — A pre-arranged duress signal triggers an agent to enforce a safe-state transition (e.g. revoke active permissions, snapshot critical state) that the duressed human cannot block.
- **Regulatory-required automated halt** — A regulatory or safety-required automated halt may enforce a state against human override for the duration of the halt (e.g. classified-information leakage prevention, safety-interlock engagement).

Each such case requires:

- An accord with explicit scope, duration, and renewal terms
- Logged provenance for every override
- Redress provisions: a path for the human (or a designated proxy) to challenge the agent's action through a higher-authority chain
- Periodic review by accountable humans not subject to the override

The clause exists because *not* enabling these scenarios in any form leaves real safety and security gaps. Enabling them carelessly inverts the authority model. The trade-off is sharp; the clause errs toward narrow scope plus mandatory redress.

### 7.2 Default behavior in unspecified situations

When no accord, policy, or precedent covers a case, the runtime's default behavior is:

1. **Yield** — the acting agent does not act unilaterally; it queues or aborts the action
2. **Record the gap** — the situation is logged with full provenance as a "policy gap" event
3. **Escalate** — the nearest accountable human (per §7.1) is notified; the gap is queued for accord authorship by a Governor

This default exists because the alternatives — guess, race, deadlock — all fail more dangerously than yielding.

### 7.3 Human-override universality (scoped)

An accountable human MAY override any accord they are party to, any
agent action taken under their delegated authority, and any action
affecting resources within their scope of accountability. **Within
that scope, the override is universal**: no accord, no trust-graph
standing, no precedence rule blocks it.

**The right does not extend across human accountability boundaries.**
A human has no override authority over actions in domains owned by
other humans, in cohorts they are not members of, or over agents
operating under another human's delegation. The override right is
universal in *blocking-power* (within scope) and bounded in
*coverage* (to the human's own domain).

The *mechanism* by which the override is exercised may vary by harness — a CLI flag, a chat directive, a signed override token — but conformant harnesses MUST provide one. Overrides are:

- **Logged** with provenance (who, when, what was overridden, what cause was cited, what scope the human asserted)
- **Never blocked** by lower-authority elements within scope
- **Subject to redress** by other accountable humans (overrides themselves are not above review)
- **Scope-validated** — a conformant runtime MUST verify that the
  invoking human's scope of accountability covers the action being
  overridden; out-of-scope override attempts are rejected and logged

The principle that overrides are logged but never blocked (within
scope) is the safety-net that makes the rest of the architecture
tolerable. The unconditional-blocking-power clause is one of the few
in this specification that states an absolute, because any softening
*within scope* would invite the failure mode the rule prevents. The
scope-validation clause prevents the dual failure mode (override
authority leaking across accountability boundaries).

### 7.4 Provenance contract

Conformant runtimes MUST record, for every action they originate:

- **Actor** — signed identity of the agent or human acting
- **Scope** — the resource, cohort, or context the action affected
- **Trigger** — what prompted the action (user request, scheduled tick, sibling agent dispatch, accord enforcement)
- **Accord-version-in-effect** — which accord (if any) governed the action, by version
- **Human-override status** — whether the action was an override, who overrode, and the cited cause
- **Sibling-validation status** — whether sibling validation per §6 was satisfied, deferred, or overridden

Receiving runtimes treat the absence of any required field as an integrity signal. Cross-cohort actions whose provenance is incomplete trigger §6 validation failure handling.

### 7.5 Amendment process

Foundational rules — accords, policies, declarations under §7 — change through an amendment process with the following requirements:

- **Proposal** — a Governor authors the proposed change, with cited motivation and impact analysis
- **Public notice** — the proposal is published to the affected scope (cohort, federation) for a runtime-defined waiting period (default: 30 days for cohort-level; 90 days for federation-level)
- **Quorum approval** — the affected scope's accountable humans (or designated stewards) approve per the scope's defined quorum threshold
- **Ratification record** — the amendment is recorded with provenance; the previous rule remains in effect until ratification commits

This clause is recursive: amendments to §7.5 itself follow §7.5. The recursion has a base case at §13 (the meta-amendment process for AEOS itself).

---

## 8. Supremacy + Precedence

When two or more accords, policies, declarations, or layer authorities apply
to the same action and produce different conclusions, AEOS specifies the
precedence order that resolves the conflict. The order is normative and
applied deterministically by conformant runtimes.

### 8.1 Default precedence order

From highest to lowest:

| Tier | Source of authority | Notes |
|---|---|---|
| 1 | **Human override** (§7.3) | Supreme. Logged but never blocked. |
| 2 | **AEOS itself** | The specification's own normative requirements |
| 3 | **Federation accord** | Multi-cohort agreements ratified per §7.5 |
| 4 | **Cohort accord** | Single-cohort agreements ratified per §7.5 |
| 5 | **Extension-internal policy** | Declared in extension manifests; binding within the extension's scope |
| 6 | **Local agent agreement** | Bilateral or small-group arrangements between agents in the same context |
| 7 | **§7.2 default** | Yield, record gap, escalate |

Tie-breaks within a tier follow the conformant runtime's trust-graph
mechanism. Where the trust graph also produces a tie, the action falls
through to tier 7 (§7.2 default) and escalates.

### 8.2 Apparent conflict vs real conflict

Many apparent conflicts are not real: two policies may use different
vocabularies for the same concept, or apply at different scopes that
happen to overlap on a single action. Conformant runtimes SHOULD attempt
*conflict reconciliation* — checking whether the policies actually
contradict on the case at hand — before invoking precedence.

Reconciliation succeeds when both policies' stated outcomes can be
satisfied simultaneously (e.g. one specifies a notification requirement,
another specifies an audit requirement; both can be satisfied). Only
genuine outcome-divergence triggers precedence.

When reconciliation fails, the precedence order applies, and the losing
policy's authors SHOULD be notified that their policy was overridden in
this case so that future policy edits can avoid the conflict shape.

### 8.3 Cascading override

A higher-tier authority MAY enable a lower-tier authority to act in a
case the higher tier would otherwise govern. The mechanism is *delegation
within precedence*: the higher tier publishes a delegation that says
"within scope X, the lower tier governs." Such delegations:

- MUST be explicitly scoped (not blanket)
- MUST be revocable by the delegating authority at any time
- MUST be recorded in the provenance ledger
- MUST NOT delegate authority that the delegating tier itself does not
  hold (no laundering of authority through chains of delegation)

### 8.4 Exit paths from precedence-bound contexts

Failure to agree with the imposed precedence order MUST have clear exit
paths in two flavors. Both paths preserve the participant's
data-sovereignty rights per §9.

#### 8.4.1 Voluntary exit

Any participant — extension, agent, user, cohort — MAY leave any context
they entered, at any time, with no penalty beyond what their original
entry agreement specified. The right to leave is foundational and
cannot be precedence-overridden by lower-tier authorities. (Higher-tier
authorities — e.g. a regulatory hold — MAY constrain timing, but not
the right itself.)

Voluntary exit triggers:

- **Notice** to remaining participants (per the entered context's
  exit-notice clause, default: immediate)
- **Settlement** of any in-flight obligations (queued work, owed
  artifacts, pending attestations)
- **Data sovereignty preservation** — exiting participant retains a
  portable export of their state per §9 (Right to portability)
- **Provenance record** of the exit, including cited cause if any

#### 8.4.2 Forced exit

A context's stewards MAY remove a participant whose continued presence
violates accords or threatens other participants. Forced exit requires:

- **Accord-defined cause** — the conduct triggering exit must match a
  pre-defined cause clause; arbitrary expulsion is not permitted
- **Notice and an appeal window** — the participant being removed
  receives notice and a runtime-defined window (default: 7 days for
  cohort-level; 30 days for federation-level) to appeal to a
  higher-authority chain
- **Quorum approval** at the steward layer if the participant being
  removed is itself a steward (a cohort cannot expel its own steward
  without quorum of remaining stewards)
- **Provenance record** with full cause citation; the record contributes
  to the trust graph for future composition

Forced exits do not waive the exiting participant's §9 rights: data
portability, redress, and audit-access remain available throughout the
appeal window and after.

### 8.5 Federation-level precedence

When precedence applies across federation peers — e.g. one cohort's
accord conflicts with another's — the resolution rules of §7.5
(amendment) apply rather than unilateral precedence. Cross-cohort
conflict is a Governor-class authoring concern, not a runtime-resolution
concern; the runtime's job is to surface the conflict and queue it for
accord renegotiation.

---

## 9. User Rights

AEOS 1.0 enumerates six explicit user rights. Conformant runtimes:

- MUST provide a mechanism by which each right is exercisable
- MUST surface a path to exercise the right that is reachable from the
  user's primary interaction surface
- MUST record exercises of these rights in the provenance ledger
- MUST treat the absence of a stated mechanism as a conformance failure

The rights enumerated here are explicit; they do not exclude other
rights the user may hold under the user's other governing frameworks
(legal, organizational, contractual). When AEOS rights and external
rights overlap, the more protective right governs.

### 9.1 Right of sovereignty

**Definition:** The user holds primary authority over their own data,
their own decisions, and the agents they delegate to. Authority delegated
upward is explicit and revocable.

**Mechanisms:**

- The platform records every delegation as an explicit, signed event
- Revocation is immediate-effect and propagates through the federation
  via the cohort registry
- The user's "consent posture" is queryable and editable through the
  primary interaction surface

**Violation signal:** an action attributed to the user that the user
did not consent to (no signed delegation; no opt-in record). Such
actions MUST be rolled back where reversible and surfaced as
provenance-integrity failures where not.

### 9.2 Right of override (scoped)

**Definition:** Per §7.3, the user MAY override any accord they are a
party to, any agent action taken on their behalf or under their
delegated authority, and any action affecting resources within their
scope of accountability. **Within that scope, the override is
universal** — no platform element blocks it. **Outside that scope, the
right does not apply** — the user has no override authority over
agentic domains owned by other humans.

**Mechanisms:**

- Conformant harnesses provide a documented override path (CLI flag,
  chat directive, signed override token, etc.)
- Override events are logged with full provenance: who, when, what
  was overridden, what cause was cited, what scope the user asserted
- Overrides themselves are subject to redress per §9.5; the right of
  override is not above review
- The runtime validates that the user's scope of accountability
  covers the action being overridden before applying the override

**Violation signals:**

- A within-scope override request that was rejected or silently
  ignored
- An out-of-scope override that was accepted (this is a security
  failure — authority crossed an accountability boundary)
- A within-scope override that was accepted but not recorded with
  scope-assertion provenance

### 9.3 Right to know

**Definition:** The user MAY query the provenance ledger for any action
that affected the user's scope. The audit trail is not for the platform's
benefit alone; it is the user's evidence handle into what was done in
their name.

**Mechanisms:**

- Provenance queries are first-class; the platform exposes a query
  surface (in the reference implementation: `axi hygiene stat mem`,
  `axi memory show`, the chat history surface)
- Queries are cohort-aware; the user can ask "what did agents on
  behalf of @me do this week" and get a complete answer for the
  cohorts the user is a member of
- Provenance records are immutable except for explicit redaction
  per §9.6 (right to be forgotten — open question)

**Violation signal:** a provenance query that returns incomplete
results, or a redaction without recorded authority. The audit trail
must be either complete or explicitly noted as redacted.

### 9.4 Right to portability

**Definition:** The user MAY export their state — memory, history,
delegations, attestations, accord memberships — in a portable format
that another conformant runtime can ingest.

**Mechanisms:**

- The export format is specified per AEOS revision (1.0 specifies a
  baseline JSON-Lines schema for episodic memory + a YAML schema for
  delegations and accord memberships)
- The export is signed by the originating platform's Attester so the
  receiving platform can verify integrity
- Export does NOT require platform cooperation beyond what AEOS
  specifies; an exiting user is entitled to their state regardless of
  the platform's preferences

**Violation signal:** an export request that fails to produce a
portable, signed result. Failure to produce the export is itself a
right violation, not merely an operational error.

### 9.5 Right to redress

**Definition:** The user MAY appeal any action affecting their scope to
a higher-authority chain. The appeal process is bounded in time and
results in a recorded verdict.

**Mechanisms:**

- The platform exposes an appeal-filing surface from the user's primary
  interaction surface
- Appeals are routed to the next-higher accountable authority per §7.1
- Appeals carry an appeal-window (default: 7 days for cohort-level
  appeals; 30 days for federation-level) during which the appealed
  action MAY be paused at the user's request, subject to safety
  constraints
- The verdict is recorded in the provenance ledger and contributes to
  the trust graph

**Violation signal:** an appeal that is rejected without a recorded
verdict, or routed to no higher authority (creating an appeal black hole).

### 9.6 Right to leave

**Definition:** The user MAY exit any context — cohort, extension,
delegation, agent relationship — at any time. The right composes with
§8.4 (exit paths) at the participant scope; this clause anchors it as
a foundational user right.

**Mechanisms:**

- Voluntary-exit per §8.4.1 from any context the user entered
- Settlement of in-flight obligations does not extend the exit indefinitely;
  the maximum settlement window is bounded per the entered agreement
- Data portability per §9.4 accompanies the exit
- The user retains §9.5 redress rights for actions in the exited context
  for a runtime-defined retention window (default: 1 year)

**Violation signal:** an exit request that is denied, indefinitely
delayed beyond the settlement window, or accompanied by data loss.

### 9.7 Open: rights not yet specified

The following rights are recognized as legitimate concerns but
deliberately left open in 1.0 pending further design:

- **Right to be forgotten** — the right to compel redaction of
  provenance records about the user. Tension: provenance integrity
  vs. privacy. Likely composes with regulatory frameworks
  (GDPR-shaped in some jurisdictions).
- **Right to delegate** — explicit framing of delegation-as-a-right
  rather than a default permission. Tension: delegation already exists
  operationally; codifying it as a right may add structure that the
  default delegation flows already cover.
- **Right to inherit** — what happens to a user's delegations,
  accords, and state upon their disability or death. Tension:
  unfamiliar territory for software platforms; likely composes with
  designated-successor mechanisms in the user's external governing
  frameworks.

A future AEOS revision SHOULD address these. The 1.0 spec acknowledges
them rather than ignoring them.

---

## 10. Due Process

Actions affecting user rights enumerated in §9 require due process —
explicit procedural protections that ensure the user has notice,
opportunity to act, and a record they can later inspect. Conformant
runtimes apply these protections automatically where the action
classification (per §10.2) requires.

### 10.1 The three-part requirement

For any action that affects a §9 right, conformant runtimes MUST provide:

1. **Notice** — the user is informed of the action *before* it commits,
   except in time-critical safety scenarios per §10.3
2. **Opportunity to override** — per §9.2, the user has a documented
   path to block or revert the action
3. **Audit trail** — per §9.3, the action is recorded with full
   provenance including the due-process flow followed

Failure to provide any one of the three is a conformance failure.

### 10.2 Action classification

Not every action requires the full three-part requirement; many actions
are read-only or trivially reversible and do not affect rights. AEOS
classifies actions by their reversibility and rights-impact:

| Class | Definition | Due process |
|---|---|---|
| **Read** | Returns information without state change | None required beyond audit trail |
| **Soft write** | Mutates state but trivially reversible (revert is a single operation) | Audit trail; notice on request |
| **Hard write** | Mutates state with non-trivial reversal (multi-step revert, or partial loss on revert) | Notice + override-opportunity + audit trail |
| **Destructive** | State change irreversible or reversal involves substantive loss | Strict three-part requirement; tier-aware confirmation per §10.3 |

The classification of a given verb is declared in the manifest's
`kind=cmd` block (per AEOS 0.1.x verb-grammar work). Conformant
runtimes apply due process per the declared class.

### 10.3 Tier-aware confirmation for destructive operations

The destructive class composes with the surfacing tier (per the
reference implementation's progressive-disclosure model in
`prd-axi-cli.md`). Confirmation thresholds escalate by tier:

- **`starter`-tier destructive operations**: SHOULD be rare; when
  present, require explicit `[y/N]` confirmation with cited consequences
- **`core`-tier destructive operations**: same as starter
- **`advanced`-tier destructive operations**: require typed-confirmation
  (user types a phrase, not just `y`); the phrase cites the resource
  being destroyed
- **`internal`-tier destructive operations**: same as advanced; logged
  with elevated provenance flag

For time-critical safety scenarios (e.g. an automated halt under §7.1.1),
the *notice* component MAY be replaced by an after-the-fact notification
provided the action is logged and reversible by §9.5 redress.

### 10.4 Right-to-redress as the safety net

Where due process is partially circumvented for legitimate cause (time
pressure, attestation lockout, regulatory halt), the §9.5 right to
redress remains intact. The combination is the architecture's safety
net: due process is the front door; redress is the back door for the
cases the front door cannot accommodate.

---

## 11. Equal Protection

Rules apply uniformly across agent classes and human roles where
applicable. The principle prevents two failure modes:

- **Class-arbitrary denial** — denying a right to one agent class while
  granting it to another, without principled distinction
- **Class-arbitrary grant** — granting a privilege to one class
  (typically a high-trust class like Attester or Governor) that
  bypasses constraints applicable to others

### 11.1 Scope

Equal protection applies to:

- **Override-and-redress rights** (§9.2 and §9.5) — these apply equally
  regardless of which agent class acted; an action by an Orchestrator
  is no less reviewable than an action by a Combatant
- **Provenance contract** (§7.4) — every class records the same fields
- **Due process** (§10) — the action's classification determines
  treatment, not the agent class executing it
- **Sibling validation** (§6) — applies by action type, not by class
  identity (a Governor's destructive operation is no less subject to
  Reviewer pre-approval than a Steward's)

### 11.2 Permitted distinctions

Not all distinctions between classes violate equal protection. Permitted
distinctions:

- **Capability-based scope** — only Attesters issue attestations; only
  Combatants take adversarial action. This is functional specialization,
  not arbitrary class privilege.
- **Trust-graph weight** — different classes (and different agents
  within a class) carry different trust standing. This affects voting
  weight and validation authority but does not exempt any class from
  the rules above.
- **Tier elevation requirements** — a class whose actions are
  predominantly destructive (Combatant) MAY require higher tier
  thresholds for invocation than a class whose actions are predominantly
  read-only (Sensor). The threshold scales with the action class
  (§10.2), not with agent class identity in the abstract.

### 11.3 Equal protection across human roles

The principle extends to human roles: an accountable human's overrides
are no more reviewable than a designated steward's; a cohort steward's
forced-exit cause is held to the same evidentiary standard as a
within-cohort steward's.

---

## 12. Succession + Continuity

AEOS-governed systems persist across the inevitable lifecycle events of
their participants: stewards become unreachable, accountable humans
transfer their roles, nodes leave cohorts, keys rotate. This section
specifies the orderly-transfer guarantees that prevent these events from
becoming governance crises.

### 12.1 Steward succession

When an accountable human or designated steward becomes unreachable
(by definition: fails to respond to required signals within a
runtime-defined timeout, default 30 days for cohort-level roles), a
succession procedure activates:

1. **Notification** to the affected scope: the unreachability is
   announced with a recorded timestamp
2. **Successor designation** per the steward's pre-declared succession
   plan (a designated alternate, a quorum-elected successor per the
   scope's procedures, or escalation to the next-higher authority)
3. **Activation window** — the successor takes office after a
   runtime-defined notice period (default: 7 days), during which the
   original steward MAY return and resume office
4. **Transfer of authority** — accords, delegations, and standing
   authorizations under the original steward continue under the
   successor unless explicitly revoked

### 12.2 Accountable-human role transfer (with available outgoing human)

When an accountable human transfers their role *and the outgoing human
is available to sign* (planned departure, organizational
restructuring), the transfer:

- MUST be signed by both the outgoing and incoming humans
- MUST identify which delegations, accord memberships, and standing
  authorizations transfer (some MAY be scope-limited and not transfer)
- MUST be announced to the affected scope with a notice period
  (default: 14 days for cohort-level transfers; 60 days for
  federation-level)

When the outgoing human is *not* available to sign — vacated role,
unreachable, deceased, role abandoned — the transfer follows §12.7
(stewardless and no-accountable-human cases) instead of this section.
The two procedures are deliberately distinct: signed transfer is the
preferred path; §12.7 handles the cases that would otherwise leave
agents and resources stranded.

### 12.3 Node departure from a cohort

When a node leaves a cohort (voluntary per §8.4.1, or forced per §8.4.2):

- **Outstanding obligations** are settled per the cohort's exit
  agreement (queued work delivered or returned; in-flight attestations
  resolved; pending appeals retained per §9.5)
- **Trust-graph standing** of the departing node is preserved in
  archival form; future returns do not start from zero standing
- **Federated memory** artifacts the departing node co-produced remain
  accessible to remaining cohort members per the conformant runtime's
  **joint-provenance memory model** — a memory architecture in which
  artifacts produced collaboratively across a cohort carry signed
  contributions from each participant and remain queryable after any
  one participant's departure *(reference: Axiom's ADR-027)*; the
  departing node retains their portable export per §9.4

### 12.4 Key rotation

Cryptographic keys rotate on a schedule (declared per Attester) or on
event (compromise, revocation, role transfer). Rotation:

- MUST overlap old and new keys for a runtime-defined window during
  which both are accepted, allowing in-flight signed material to
  validate before the old key is retired
- MUST be announced to the federation peer registry so other nodes
  can update their verification material
- MUST preserve the old key's signed history (signatures made under
  the old key remain valid for their validity period; only future
  signatures require the new key)

### 12.5 Cohort dissolution

When a cohort dissolves (all stewards depart; quorum-decision to
dissolve; external mandate), the dissolution:

- MUST follow §12.1–§12.3 for each affected role
- MUST result in portable exports for every remaining member per §9.4
- MUST preserve the cohort's accord history as joint-provenance
  artifacts so future federations can reference them
- MUST NOT result in data loss for any member; dissolution is an
  orderly conclusion, not an erasure

### 12.6 Continuity guarantee

Across all succession events, conformant runtimes guarantee:

- No action loses its provenance — the audit trail survives all
  transfers
- No user loses their §9 rights — rights persist across role changes
- No accord lapses silently — accords either continue under new
  authority or are explicitly terminated with a recorded cause
- No participant is stranded — every participant has at least one
  exit path (per §8.4) regardless of upstream events

### 12.7 Stewardless agents and zero-accountable-human cases

In some deployments — and likely in many — there will be no
identifiable accountable human at the moment some action is
proposed. Real scenarios include: an open-source AEOS implementation
deployed by an individual who later moved on; a research-lab cohort
whose original principal departed without designating successors; an
autonomous agent left running after its sponsoring project ended; a
node deployed for a one-time experiment that was never formally
retired. **Conformant runtimes MUST treat zero-accountable-human as a
known operating condition rather than an exceptional crisis.**

#### 12.7.1 Nomination of accountable humans

Conformant runtimes MUST provide a mechanism by which a human MAY be
*nominated* into accountable-human standing for a stewardless scope.
The nomination procedure:

- **Nomination record** — the nominating party (any human with
  legitimate connection to the scope, see §12.7.3) signs a
  nomination naming the candidate, citing why they are the
  appropriate successor, and identifying their connection to the
  scope
- **Notification** — the candidate receives the nomination through
  whatever channel the runtime supports, with the cited reasoning
  and the nominator's identity surfaced
- **Acceptance window** — the candidate has a runtime-defined window
  (default: 14 days) to acknowledge and accept, decline with cause,
  or remain silent
- **Recursion on decline or silence** — if the nomination is
  declined or expires, the system either escalates to a different
  candidate (per §12.7.3) or, if no candidate accepts, transitions
  the scope per §12.7.4

Nominations are public within the affected scope; the nominator's
identity and reasoning are queryable via the §9.3 right to know.
Nominations cannot be made anonymously.

#### 12.7.2 Acceptance signals integrity

Acceptance of a nomination is a signed, recorded event. Once
accepted, the new accountable human assumes the role per §12.1's
activation procedure (notice period, succession of standing
authorizations, etc.).

A nomination cannot bind a human to accountability without their
acceptance. The system records who was *offered* the role; it
attributes accountability only to those who explicitly accepted.

#### 12.7.3 Searching for peripheral humans

When no human pre-designated for the scope is available, conformant
runtimes SHOULD attempt to identify *peripheral* humans whose
connection to the scope makes them legitimate candidates for
nomination. The search consults dimensions of recorded relevance:

- **Org-chart proximity** — humans in organizational reporting
  relationships to the scope (the project's lead, the team's
  manager, the lab's PI), where such relationships are recorded
- **Conversation recency and proximity** — humans whose recent
  interaction history with the scope (per the provenance ledger)
  shows substantive engagement
- **Resource ownership** — humans who own resources the scope
  consumes or produces (storage, compute, source repositories)
- **Federation peers** — accountable humans of cohorts that peer
  with the affected scope and whose accord arrangements suggest
  shared governance interest
- **Prior delegation chains** — humans who appeared as delegators
  in any of the scope's prior signed delegations, even if those
  delegations are no longer active

Search results are *candidates for nomination*, not automatic
appointees. Each candidate goes through §12.7.1's nomination
procedure with cited reasoning.

#### 12.7.4 Behavior when no accountable human is found

If §12.7.3's search produces no candidates, or all candidates decline
or remain silent, the affected scope enters **stewardship-suspended**
state:

- All non-trivial actions in the scope are queued, not executed.
  "Non-trivial" follows the §10.2 classification: read and soft-write
  actions continue; hard-write and destructive actions queue.
- Federation peers are notified of the suspended state so they can
  decide whether to extend their own §12.7.3 search or to terminate
  accord relationships with the scope.
- The scope's §9 rights remain in force for any remaining users —
  they MAY still exercise sovereignty, override, know, portability,
  redress, and leave. Stewardship suspension does not eliminate user
  rights.
- A **registry of stewardless scopes** is maintained — discoverable
  by any conformant runtime — listing scopes in suspended state with
  their cited search history. The registry exists to make adoption
  possible: a human encountering a stewardless scope they have
  legitimate connection to MAY initiate a nomination per §12.7.1
  with themselves as candidate.

If a stewardless scope remains in suspended state past a
runtime-defined long-window (default: 1 year), the runtime SHOULD
proceed to an **orderly conclusion** per §12.5 (cohort dissolution)
— preserving exports, accord-history, and rights records. Indefinite
suspension is not a stable steady state; the architecture prefers
deliberate dissolution to silent decay.

#### 12.7.5 Adversarial nomination attempts

Nomination is an attack surface: an adversary could attempt to
nominate themselves as accountable human for a scope they have no
legitimate connection to. Conformant runtimes MUST defend against
this:

- The nominator's connection to the scope (per §12.7.3 dimensions)
  is itself recorded and queryable; nominations from parties with
  no recorded connection are accepted but flagged
- A nomination contested by an existing scope participant (per the
  registry) triggers a quorum review per the scope's defined
  quorum threshold before the nomination can complete
- Federation peers MAY publish trust-graph signals about the
  nominator that the receiving scope incorporates into its review
- The §9.5 right to redress applies: a scope participant who
  believes a nomination was illegitimate has a path to challenge
  through a higher-authority chain

The procedure errs toward inclusion (allowing legitimate adoption of
abandoned scopes) while preserving redress against abuse.

---

## 13. Amendment Process for AEOS

AEOS itself changes through an amendment process distinct from §7.5
(which governs amendments to operational accords). §13 is the
*meta*-amendment process: how the foundational specification revises
itself.

### 13.1 Threshold

Amendments to AEOS require:

- **Drafted proposal** — authored by a Governor (or, for AEOS itself,
  the Editor named at the front of the specification) with cited
  motivation and impact analysis
- **Public notice period** — published to the broader conformant
  runtime community for the duration of the notice period (default:
  90 days)
- **Quorum approval** — multi-cohort quorum of accountable humans (or
  designated stewards) per the threshold defined for the amendment
  scope (see §13.2)
- **Ratification record** — the amendment is recorded with full
  provenance; the previous specification version remains in effect
  for declared-conformance extensions until they migrate

### 13.2 Threshold scaling by amendment scope

AEOS amendments fall into three scopes with proportionate quorum
thresholds:

| Scope | Examples | Quorum (default) |
|---|---|---|
| **Editorial** | Typo fixes, clarifications that don't change normative content, example updates | Editor + one reviewer |
| **Material** | New section, new principle, normative requirement added | Simple majority of cohort stewards |
| **Foundational** | Change to §7 (Conflict Resolution), §9 (Bill of Rights), §13 (this section), or to the meaning of an existing principle | 2/3 supermajority of cohort stewards + accountable-human assent |

The thresholds are defaults; conformant federations MAY raise them
(never lower).

### 13.3 Recursion

This section is recursive: amendments to §13 itself follow §13. The
recursion has a base case:

> **The right of override (§7.3) and the right of leave (§9.6) cannot
> be removed by amendment.** Any proposed amendment that would remove
> or substantively narrow either right is procedurally invalid and
> SHALL NOT be ratified, regardless of quorum.

The base case exists because override and leave are the safety net
that makes the rest of the architecture tolerable; an amendment
process that could remove them would invert the architecture's
posture toward its users.

### 13.4 Version transitions

AEOS uses semantic versioning (per principle 2.6's reference to
SemVer 2.0.0):

- **Patch** (e.g. 1.0.0 → 1.0.1) — editorial amendments per §13.2
- **Minor** (e.g. 1.0.0 → 1.1.0) — material amendments that add
  capability without breaking conformance
- **Major** (e.g. 1.0.0 → 2.0.0) — foundational amendments that may
  break conformance for extensions declared against the prior major
  version

Within a major version, the specification is stable; cross-major
migrations require explicit migration (per Appendix A for the 0.1 → 1.0
transition).

### 13.5 Authoring transparency

Each amendment carries a cited rationale recorded in Appendix C
(Decisions log). The log captures *why-not-otherwise*: alternatives
considered, why the chosen path was preferred, what evidence motivated
the change. The log is lighter than full ADRs but heavier than commit
messages; future readers should be able to reconstruct the reasoning
without consulting external sources.

---

---

## 14. Directory Layout

A conformant extension organizes its source, tests, documentation, manifest,
and metadata into a canonical structure. Two layout modes exist:
**compound** (for standalone, distribution-installable extensions) and
**flat** (for built-in extensions that ship inside a host package).

### 14.1 Canonical compound layout

The compound layout is the default and applies to standalone extensions:

```
<extension-name>/                       # purpose-named directory (per principle 2.2)
├── <extension-package>/                # language package matching the directory name
│   ├── __init__.py                     # PUBLIC API surface, with __all__ declared
│   ├── agents/                         # optional — agent modules
│   │   └── <agent-name>/
│   │       ├── __init__.py
│   │       ├── agent.py
│   │       └── persona.md              # agent system prompt; internal, not a standalone skill
│   ├── tools/                          # optional — tool modules
│   │   └── <tool-name>/
│   ├── commands/                       # optional — cmd modules (CLI verbs per kind=cmd)
│   │   └── <noun>/
│   ├── services/                       # optional — service (long-running daemon) modules
│   │   └── <service-name>/
│   ├── adapters/                       # optional — third-party integration modules
│   │   └── <integration-name>/
│   ├── skills/                         # optional — STANDALONE reusable skills
│   │   └── <skill-name>/               # not bound to any agent
│   │       ├── SKILL.md                # SKILL.md format
│   │       ├── references/
│   │       └── scripts/
│   ├── prompts/                        # optional — kind=prompt MCP prompt templates (1.0)
│   │   └── <prompt-name>.md
│   ├── hooks/                          # optional — hook modules
│   │   └── <hook-name>.py
│   ├── _internal/                      # strictly private, never imported externally
│   │   └── ...
│   └── py.typed                        # type-information marker (language-specific)
├── tests/
│   ├── unit_tests/
│   │   ├── test_standard.py            # inherits from the conformance test base
│   │   └── test_<specific>.py
│   ├── integration_tests/
│   │   └── test_standard.py
│   ├── fixtures/
│   └── conftest.py
├── docs/
│   ├── prds/
│   │   └── prd.md
│   ├── specs/
│   │   └── spec.md
│   ├── decisions/
│   │   └── adr-001-<title>.md
│   ├── working/
│   ├── overview.md
│   └── reference/
├── AGENTS.md                           # coding-agent guidance
├── README.md                           # user-facing landing
├── CHANGELOG.md                        # Keep-a-Changelog format
├── LICENSE
├── pyproject.toml                      # or language-equivalent build manifest
├── axiom-extension.toml                # AEOS manifest
└── .importlinter                       # local boundary-enforcement config (optional)
```

The 1.0 layout adds an optional `prompts/` subdirectory for the
`kind=prompt` capability that 0.1.x point releases formalized
(MCP prompt templates published by the platform).

### 14.2 Required files

Every standalone (distribution-installable) extension MUST have:

- A build manifest (`pyproject.toml` or language-equivalent)
- The AEOS manifest (`axiom-extension.toml`)
- `README.md`
- `CHANGELOG.md` in Keep-a-Changelog format
- `LICENSE`
- The package's public-API surface file with `__all__` declared
- A unit-test entry inheriting from a conformance test base class

### 14.3 Built-in extensions and the flat layout

Extensions that ship inside a host package (built-ins, declared in the
manifest as `builtin = true`) use a flat layout: the extension's root
directory IS the package, with no inner package nesting:

```
<host-package>/extensions/builtins/<ext>/
├── __init__.py
├── axiom-extension.toml                # with builtin = true
├── agents/<name>/persona.md
├── tools/…
├── commands/…
├── prompts/…
├── tests/
│   └── unit_tests/test_standard.py
└── docs/…
```

For built-ins, `README.md`, `CHANGELOG.md`, `LICENSE`, and the build
manifest belong to the host package, not the individual extension.
When a built-in extracts to a standalone repository, those files are
added as part of the extraction and the layout transitions to compound.

### 14.4 Multi-class agents and layout

When an agent fulfills multiple classes per §3.11, the layout has two
conformant forms:

- **Single-directory form** — one `agents/<name>/` directory; the
  agent's `axiom-extension.toml` block declares multiple classes
  in a `classes = [...]` array. Recommended when the agent's code
  paths share substantially across class behaviors.
- **Multi-directory form** — separate `agents/<name>/` blocks per
  class, each with its own manifest entry; the underlying class
  modules MAY share a common runtime via internal imports.
  Recommended when the class behaviors are largely independent.

Either form is conformant; the choice is authorial.

### 14.5 Package naming

The directory name and language-package name are identical and are
the extension's purpose name (per principle 2.2). No type suffix.

Valid: `classroom`, `connect`, `memory`, `syllabus_extraction`,
`reactor_physics`. Invalid: `classroom_domain`, `memory_module`,
`connect_adapter` (type suffixes).

Legacy extensions using deprecated suffixes (e.g. `_agent`) are
expected to migrate before declaring 1.0 conformance.

---

## 15. Manifest Format

The `axiom-extension.toml` file is the AEOS manifest — a TOML v1.0.0
document with a defined schema. The schema is the single source of
truth for what fields are valid; this section describes the manifest's
shape and 1.0-specific additions.

### 15.1 Schema overview

```toml
# AEOS Manifest — axiom-extension.toml

# ---- Extension identity ----
[extension]
name = "classroom"                      # matches directory and package name
version = "0.1.0"                       # SemVer 2.0.0
description = "Classroom learning management, analytics, and research"
owner = "ut-austin"
license = "Apache-2.0"
homepage = "https://keplo.dev"          # optional
repository = "https://github.com/ut-austin-ne/keplo"
aeos_version = "1.0.0"                  # AEOS spec version this extension conforms to
classification_ceiling = "public"       # max classification this extension handles
trust_profile = "standard"              # required trust profile
builtin = false                         # true for host-package built-ins

# ---- 1.0-era addition: rights conformance declarations ----
[extension.rights_conformance]          # NEW in 1.0
sovereignty = "conformant"              # see §15.4 for valid values per right
override = "conformant"
know = "conformant"
portability = "conformant"
redress = "conformant"
leave = "conformant"

# ---- Compatibility ----
[extension.compatibility]
mcp = ">= 2025-11"
a2a = ">= 0.3"
python = ">= 3.11"
platforms = ["linux", "darwin", "windows"]

# ---- Provided capabilities ----
# Each [[extension.provides]] block declares one capability.

[[extension.provides]]
kind = "agent"
name = "chalke"
entry = "classroom.agents.chalke:ChalkeAgent"
persona = "classroom/agents/chalke/persona.md"
description = "Classroom instructor companion agent"
classes = ["generator", "orchestrator"]   # NEW in 1.0: agent class memberships per §3
requires_signals = ["student_absence", "help_request"]
uses_skills = ["reactor_physics_tutor"]

[[extension.provides]]
kind = "tool"
name = "syllabus_extraction"
entry = "classroom.tools.syllabus_extraction:SyllabusExtractor"
description = "Extract course structure from uploaded syllabus"
idempotent = true
side_effects = "none"

[[extension.provides]]
kind = "cmd"
noun = "enrollment"
entry = "classroom.commands.enrollment:cli"
description = "Manage student enrollment"
subcommands = ["add", "remove", "list", "notify"]
tier = "core"                            # 0.1.x addition: surfacing tier
intent_groups = ["teach"]                # 0.1.x addition: intent group rollups

[[extension.provides]]
kind = "adapter"
integration = "canvas_lms"
entry = "classroom.adapters.canvas:CanvasAdapter"
auth_methods = ["oauth2", "api_token"]
capabilities = ["grade_push", "roster_sync"]

[[extension.provides]]
kind = "skill"
name = "reactor_physics_tutor"
path = "classroom/skills/reactor_physics_tutor/"
description = "Standalone tutoring skill; invokable by any agent with access"

[[extension.provides]]
kind = "prompt"                          # 0.1.x addition: MCP prompt template
name = "classroom-grading-context"
path = "classroom/prompts/grading-context.md"
description = "Active class cohort + current rubric for grading"
extends = "axi-help-snapshot"
fill_point = "extension_context"

[[extension.provides]]
kind = "hook"
events = ["session.started", "session.ended"]
entry = "classroom.hooks:session_hooks"
priority = 100
fail_mode = "warn"

# ---- Consumed capabilities ----
[[extension.consumes]]
kind = "core"
package = "axiom"
version = ">= 0.14, < 0.20"

[[extension.consumes]]
kind = "extension"
package = "vega-trust"
version = ">= 0.1, < 0.2"
capabilities = ["federation", "trust_profile"]

# ---- Federation characteristics ----
[extension.federation]
shareable = true
requires_attestation = true
quarantine_recoverable = true

# ---- Signing ----
[extension.signing]
required = true
methods = ["sigstore"]
publisher_identity = "ut-austin"

# ---- Testing conformance ----
[extension.testing]
standard_tests = ["unit", "integration"]
test_base_class = "axiom_tests.standard.ExtensionStandardTests"
minimum_coverage = 80
```

### 15.2 Required manifest fields

The `[extension]` section requires: `name`, `version`, `description`,
`license`, `aeos_version`. Every extension MUST declare at least one
`[[extension.provides]]` block.

For 1.0 conformance, extensions also declare:

- `[extension.rights_conformance]` — see §15.4
- For each `kind=agent` block: a `classes` array — see §15.3

### 15.3 Agent class membership declarations (1.0 addition)

Every `kind=agent` block declares one or more agent classes from the §3
enumeration:

```toml
classes = ["orchestrator"]                      # single class
classes = ["generator", "orchestrator"]          # multi-class per §3.11
```

The runtime validates that the declared classes match observed
behavior. Class declarations that diverge from observed behavior
trigger lint warnings and, persistently, contribute to trust-graph
standing changes.

Conformant runtimes MUST recognize the ten class names in §3 by their
lowercase forms: `orchestrator`, `generator`, `steward`, `sensor`,
`reviewer`, `governor`, `federator`, `attester`, `shepherd`, `combatant`.
Implementations MAY recognize additional classes for their own
purposes; portability requires the canonical ten remain valid.

### 15.4 Rights conformance declarations (1.0 addition)

The `[extension.rights_conformance]` block declares the extension's
posture toward each §9 right. Each right is declared with one of:

| Value | Meaning |
|---|---|
| `conformant` | The extension implements the mechanisms required by §9 for this right |
| `not_applicable` | The extension does not interact with the right's domain (e.g. a read-only data adapter declares `redress = "not_applicable"`) |
| `delegated` | The extension delegates the right's enforcement to a named upstream component (e.g. a thin CLI wrapper around another extension) |
| `unaudited` | The extension has not been audited for this right; surfaced in `axi ext lint --strict` as a warning |

Extensions that declare `conformant` MUST provide the mechanisms §9
specifies for that right. Declaration is the publish-time conformance
claim; the runtime's lint and validation tools verify the claim
matches the implementation.

### 15.5 Validation

The manifest validates against a published JSON Schema (in the
reference implementation: `aeos-manifest-1.0.json`). Validation occurs
at install time, on every `lint` invocation, and at runtime when the
extension is first loaded. Schema violations block install; runtime
violations log a conformance warning and continue (so a stricter-than-
schema runtime check doesn't break the extension's primary function).

### 15.6 Strict root + permissive transitional sections

The manifest's root object enforces `additionalProperties: false`. The
`[extension]` table itself is strict — every key must be an
AEOS-defined property. Other root sections (transitional from earlier
runtime conventions) remain permissively-schemed for backward
compatibility while the migration to capability-block declarations
completes:

| Root section | Status |
|---|---|
| `[extension]` | Strict |
| `[agent]` (lifecycle) | Permissive — pre-AEOS daemon-lifecycle block; future migration to `kind=service` |
| `[[connections]]` | Permissive — pre-AEOS integration block; future migration to `kind=adapter` |
| `[chat_tools]` | Permissive — module-level tool registry; future migration to per-tool `kind=tool` blocks |
| `[skills]` | Configures the skills-scanner directory |
| `[[providers]]`, `[[extractors]]`, `[mcp_servers]`, `[[prompt_contributions]]` | Permissive; runtime-consumed |

New extensions SHOULD use `[[extension.provides]]` form for any new
declaration. Legacy `[[cli.commands]]` blocks were removed at AEOS
0.1.x; manifests declare CLI commands via `[[extension.provides]]
kind="cmd"`.

---

## 16. Capability Declaration via Entry Points

Capabilities are declared distributively via language-native entry
points (in the reference implementation: Python entry points).
Registration is import-time; the manifest enumerates capabilities for
discovery and validation.

### 16.1 Entry-point registration (reference: Python)

```toml
# pyproject.toml
[project.entry-points."axiom.agents"]
chalke = "classroom.agents.chalke:ChalkeAgent"

[project.entry-points."axiom.tools"]
syllabus_extraction = "classroom.tools.syllabus_extraction:SyllabusExtractor"

[project.entry-points."axiom.commands"]
enrollment = "classroom.commands.enrollment:cli"

[project.entry-points."axiom.prompts"]                     # 1.0 addition
classroom-grading-context = "classroom.prompts:grading_context"
```

At installation, the runtime loads entry points via the language's
standard discovery mechanism (in Python: `importlib.metadata.entry_points()`).
The conformance lint tool verifies that manifest `entry` values
match registered entry points.

### 16.2 Manifest authoritative for validation; entry points authoritative for loading

The manifest is the *declarative* truth — what the extension says it
provides, against which validators check. The entry points are the
*operational* truth — how the runtime actually loads symbols. The two
must agree; lint tools check the agreement.

This split lets refactors (renaming a class, moving a module) update
the entry-point declaration without manifest churn, while still
preserving the manifest as the conformance contract.

### 16.3 Public API discipline

Every extension's public API surface (`__init__.py` in Python) declares
the symbols available for cross-extension import. All other symbols are
private:

```python
# classroom/__init__.py
from classroom.agents.chalke import ChalkeAgent
from classroom.tools.syllabus_extraction import SyllabusExtractor

__all__ = ["ChalkeAgent", "SyllabusExtractor"]
```

Cross-extension imports are restricted to declared public APIs.
Boundary enforcement at the repo level (in the reference
implementation: `import-linter`) blocks violations in CI.

---

## 17. Signed Releases, Attestation, and Recovery

Conformant extension releases are signed; signatures are verified at
install; behavioral attestation extends the trust model beyond install
time; quarantine and recovery handle the case where a once-trusted
extension drifts.

### 17.1 Signing (carries from 0.1)

Every published AEOS extension SHOULD be signed via Sigstore's keyless
OIDC flow. The publisher authenticates via their identity provider
(GitHub, Google, institutional OIDC); Sigstore issues a short-lived
certificate; the artifact and signature are published together.

Unsigned releases install only with explicit override (`--allow-unsigned`
or runtime equivalent); the override is logged with user
acknowledgment.

### 17.2 What is signed (1.0 expansion)

AEOS 0.1 signed the artifact + manifest. 1.0 expands the signed
material to include the foundational metadata that conformance now
depends on:

- The manifest itself (including all `[[extension.provides]]` blocks)
- The agent class membership declarations on each agent provides block
- The `[extension.rights_conformance]` declarations
- The `[extension.federation]` characteristics
- The signing block's `publisher_identity`
- The artifact contents (existing 0.1 behavior)

This means rights-conformance and class-membership claims travel with
the signed release; an installer can trust those claims after signature
verification.

### 17.3 Verification at install

The install process verifies the Sigstore signature against the
declared `publisher_identity` before extracting or running extension
code. Mismatches abort the install with a clear error citing the
expected and observed identities.

### 17.4 Behavioral attestation (AEOS leap-ahead)

Beyond install-time signing, AEOS specifies *behavioral attestation*: a
conformant runtime observes an extension's actual behavior over time
and issues an attestation:

> "At time T, extension X's observed behavior matched its declared
> capabilities and class memberships with confidence C."

Attestations are signed by the observing runtime and consumable by
other AEOS runtimes for trust decisions. The reference implementation
uses a behavioral classifier to compute the
confidence value.

Behavioral attestation is OPTIONAL for extensions but REQUIRED for
installation in deployments whose classification ceiling exceeds
"restricted" or whose trust profile demands it.

### 17.5 Quarantine and recovery

When an extension's observed behavior diverges from its declared
capabilities or class memberships, a conformant runtime MAY quarantine
the extension. Quarantined extensions remain installed but execute
only in restricted mode pending recovery ceremony:

1. The publisher diagnoses the drift cause and ships an updated release
2. The updated release is signed (per §17.1) and behaviorally attested
   (per §17.4) under the new state
3. The runtime verifies the new attestation against current observed
   behavior and lifts quarantine

The quarantine/recovery cycle directly responds to the failure mode
where a once-legitimate extension drifts — through update, compromise,
or gradual scope-creep — into behavior its manifest no longer accurately
describes. Detection plus contained restriction is preferable to either
silent acceptance or scorched-earth deletion.

Quarantine is itself an action subject to §6 sibling validation; a
single Reviewer-class agent does not unilaterally quarantine an
extension serving live traffic.

---

## 18. Testing, Validation, and Conformance

Conformant extensions and conformant runtimes are verifiable. AEOS
specifies the test-inheritance model, the validation surface, and the
conformance levels extensions and harnesses declare against.

### 18.1 Shared test conformance package (reference: `axiom-tests`)

A conformant runtime publishes a test-conformance package providing:

- Abstract test base classes per capability kind (`ExtensionStandardTests`,
  `ToolTests`, `AgentTests`, etc.)
- Reusable test fixtures registered via the language's plugin mechanism
- Mock services for integration tests (LLM, federation peer, IdP,
  registry) so integration suites run without live dependencies

In the reference implementation, this package is `axiom-tests`. Other
conformant runtimes publish equivalents.

### 18.2 Standard test inheritance

Every extension's `tests/unit_tests/test_standard.py` inherits from the
runtime's conformance base classes:

```python
from axiom_tests.unit_tests import ExtensionStandardTests, ToolTests, AgentTests

class TestClassroomExtension(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self):
        return Path(__file__).parent.parent.parent / "axiom-extension.toml"

class TestSyllabusExtractionTool(ToolTests):
    @pytest.fixture
    def tool_class(self):
        from classroom.tools.syllabus_extraction import SyllabusExtractor
        return SyllabusExtractor
```

Capability-kind base classes expose properties defaulting to `False`;
extensions override the properties they support, activating the
relevant test surface.

### 18.3 Lint and validation surface

Conformant runtimes provide a lint surface that verifies declared
conformance against the implementation. In the reference
implementation, the surface is `axi ext lint`; other conformant
implementations may name it differently.

The lint surface verifies:

- Manifest schema validity
- File-presence requirements (§14.2)
- Public-API surface declarations (§16.3)
- Entry-point registration matches manifest `entry` values
- Agent class membership claims match observed behavior (1.0 addition)
- Rights conformance claims match implemented mechanisms (1.0 addition)
- Verb grammar (per AEOS 0.1.x point release) for every `kind=cmd` block
- Provenance contract surface (per §7.4) emits required fields
- Tier and intent-group declarations on `kind=cmd` blocks (per 0.1.x)
- Cross-capability consistency (e.g. an agent declaring `uses_skills`
  must reference skills that exist, locally or via declared dependencies)

The lint surface has two strictness modes: default (warnings) and
`--strict` (warnings become errors). Default mode is appropriate for
in-development iteration; `--strict` is appropriate for CI gates and
publish flows.

### 18.4 Conformance levels

AEOS defines three conformance levels. Extensions and harnesses declare
their level explicitly.

#### 18.4.1 Bronze — Compatibility

- Manifest validates against the AEOS schema for the declared
  `aeos_version`
- Layout conforms (compound or flat per `builtin`)
- All required files present
- The runtime's lint surface reports zero errors at default strictness

#### 18.4.2 Silver — Signed and Tested

- All Bronze requirements
- The release is signed per §17.1
- Standard-test coverage meets the runtime's defined threshold
  (default: 80%)
- Public-API discipline enforced (boundary linter passes)

#### 18.4.3 Gold — Federation-Ready

- All Silver requirements
- The extension supports behavioral attestation per §17.4
- The extension supports quarantine and recovery per §17.5
- The extension declares trust-profile requirements and classification
  ceiling
- The extension's federated-tier lint surface passes (cross-cohort
  validation rules satisfied)

A conformant runtime's lint surface reports the highest level achieved.
Production deployments handling classification above "public" SHOULD
require Gold conformance for any extension that participates in the
classified workload.

### 18.5 Validation lifecycle

Validation occurs at multiple lifecycle points:

| Lifecycle point | Validation performed |
|---|---|
| Authoring | Lint runs in development; CI gates |
| Publish | Lint at `--strict`; signature applied; behavioral baseline established |
| Install | Manifest validates; signature verified; required-files checked |
| Runtime first-load | Class memberships verified against observed behavior baseline |
| Runtime ongoing | Behavioral classifier monitors drift; quarantine triggers per §17.5 |

The validation surface composes — a lint pass at authoring is not a
substitute for runtime monitoring; runtime monitoring is not a
substitute for install-time signature verification.

---

## 19. Federation-Native Where Applicable

AEOS extensions MAY participate in multi-cohort federation. When they
do, signed attestations travel with them, trust-profile compatibility
is declared explicitly, quarantine and recovery ceremonies are
first-class, and §6's checks-and-balances rules govern cross-cohort
actions.

Federation participation is OPTIONAL. A local-only extension is a
valid AEOS extension that ignores federation metadata.

### 19.1 Declared federation characteristics

Extensions that participate in federation declare three characteristics
in their manifest:

```toml
[extension.federation]
shareable = true                        # may be distributed via federation channel
requires_attestation = true             # must carry behavioral attestation
quarantine_recoverable = true           # supports quarantine/recovery ceremony
```

`shareable = false` extensions stay local; the runtime enforces by
declining to advertise them on federation channels. `requires_attestation =
true` raises the install-time bar for receiving cohorts. `quarantine_recoverable
= true` opts the extension into the §17.5 ceremony.

### 19.2 Cross-cohort actions and §6 composition

Cross-cohort actions — actions whose effects cross a federation
boundary — are subject to §6 sibling validation. Specifically:

- Cross-cohort writes require Federator-class validation of the
  receiving cohort's admission policy compliance (§6.2)
- Cross-cohort writes require Attester-class validation of the
  signature chain (§6.2)
- Trust-graph composition (§6.3) determines the relative weight of
  validations from different cohorts

These compositions ensure that federation-mediated actions inherit the
same checks-and-balances posture as within-cohort actions.

### 19.3 Quarantine ceremonies as accord events

Quarantining a federated extension affects multiple cohorts. Per §7.5,
quarantine ceremonies are accord events:

- The quarantining runtime publishes a quarantine declaration with
  cited cause
- Affected cohorts (those whose extensions depend on the quarantined
  one, or whose policies referenced the quarantined extension's
  capabilities) receive notice
- Recovery ceremonies follow the §7.5 amendment process: ratification
  threshold appropriate to the scope of the quarantine

This ensures quarantine is not a unilateral act that disrupts
federation members without process.

### 19.4 Cohort-level federation primitives (reference-implementation
capability)

The reference implementation provides a federation extension surface
exposing:

- Peer registration and attestation exchange
- Trust-graph state propagation across cohorts
- Quarantine and recovery ceremony orchestration
- Federation-tier CLI verbs (`axi ext federate <name>`, `axi ext attest`,
  `axi ext quarantine`, `axi ext recover`) — see the reference
  implementation's CLI specification for shape

Conformant implementations supply equivalent surfaces. The federation
primitives are not themselves part of AEOS — they are how a conformant
runtime *implements* the federation participation AEOS specifies.

### 19.5 Federation and the bill of rights

Cross-cohort actions retain user §9 rights. A user whose data flows
through a federation:

- Retains sovereignty (§9.1) — federation participation does not waive
  consent
- Retains know (§9.3) — provenance is queryable across cohort
  boundaries (federated provenance is a feature)
- Retains portability (§9.4) — export remains available even when
  data is replicated across cohorts
- Retains leave (§9.6) — exiting a federation is a §8.4 voluntary
  exit, with §12.3 node-departure semantics

Cohort stewards may not amend or delegate away these rights through
federation policy.

---

## Appendix A — Migration from AEOS 0.1

AEOS 0.1-conformant extensions migrate to 1.0 conformance through
declarative additions; existing manifests remain valid until the
declared `aeos_version` is bumped.

### A.1 Migration steps

1. **Bump `aeos_version`** from `"0.1.0"` to `"1.0.0"` in the
   `[extension]` block.
2. **Declare agent class memberships** for every `kind=agent` block.
   Add a `classes = [...]` array per §3 / §15.3. The runtime's lint
   tool can suggest classes based on observed behavior; authors
   confirm.
3. **Declare rights conformance** by adding `[extension.rights_conformance]`
   per §15.4. Extensions that do not interact with a right's domain
   declare `not_applicable`; extensions that delegate declare
   `delegated` (with the upstream cited); extensions that have not
   audited declare `unaudited` (which surfaces in `--strict` lint).
4. **Verify verb grammar** (already required in 0.1.x point releases).
5. **Verify `kind=prompt` blocks** declare correctly per the schema
   (existing if added during 0.1.x).
6. **Re-sign the release** with the expanded signed-material set per
   §17.2.

### A.2 Soft-warning window

Conformant runtimes provide a 6-month soft-warning window after AEOS
1.0 publication during which:

- Extensions declaring `aeos_version = "0.1.0"` continue to validate
  against the 0.1 schema
- Extensions declaring `aeos_version = "1.0.0"` validate against the
  1.0 schema with `--strict` warnings only (not errors) for missing
  rights-conformance or class-membership declarations
- Lint suggestions are surfaced for unmigrated declarations

After the soft-warning window, `--strict` lint reports unmigrated
declarations as errors. Default-mode lint continues to surface
warnings indefinitely so older extensions remain installable.

### A.3 Tooling

The reference implementation's `axi ext migrate` verb walks the
extension's manifest, suggests class memberships and rights
conformance declarations, and writes the updates with author
review. Other conformant runtimes provide equivalent tooling.

### A.4 What does NOT need to change

- Layout (compound or flat) — no changes
- Capability-kind blocks (agent / tool / cmd / service / adapter /
  skill / hook / prompt) — schema is additive; existing blocks remain
  valid
- Test inheritance — base classes remain stable
- Sigstore signing flow — unchanged, just expanded signed material
- Federation declarations — unchanged

The migration is deliberately scoped to *declarative additions*. No
0.1-conformant extension's runtime code needs to change to declare
1.0 conformance.

---

## Appendix B — Cross-references to operational specs

AEOS 1.0 is the foundation. Operational specifications that compose
with it live in adjacent documents. The reference implementation
publishes the following:

| Adjacent document | Owns |
|---|---|
| `spec-agent-accord-protocol.md` | A2A wire shape for accord negotiation, message types, timeouts, failure modes |
| `prd-agent-conflict-resolution.md` | User-facing conflict-resolution model, escalation patterns, governor authoring |
| `spec-axi-cli.md` | CLI surface design for the reference implementation |
| `spec-extension-loading.md` | Runtime extension loader (Python today; WASM-bound future direction) |
| `prd-commands-generator.md` | Cross-harness slash-command generation |

Reference-implementation ADRs detailing mechanisms AEOS abstracts
(trust graph, joint-provenance memory, federation peer registry) are
cited in-line as illustrative; conformant implementations supply
their own equivalents.

The relationship is one-way: AEOS does not depend on these documents.
These documents reference AEOS as their foundational specification.
Implementations that conform to AEOS need not adopt the specific
operational designs in these documents; AEOS conformance is
sufficient on its own.

---

## Appendix C — Decisions log

Each material decision made during AEOS 1.0 authoring is recorded
here with date and reasoning. Format is intentionally lighter than
full ADRs: the *what*, the *why-not-otherwise*, and the *evidence*
that motivated the decision.

### C.1 Rename: "Agent Extension Open Standard" → "Agent Ecosystem Open Standard"

**Date:** 2026-05-02
**What:** The acronym AEOS retained; the spelled-out name expanded.
**Why-not-otherwise:** "Extension" was apt for 0.1's packaging-format
scope; 1.0 covers governance, rights, agent classes, federation —
substantially broader. A pure rename (e.g. "Agent Open Standard"
collapsing to AOS) would have churned every existing reference.
"Ecosystem" captures the broadened scope while preserving
acronym continuity.
**Evidence:** Author/editor consensus; no external implementations
yet that would be disrupted.

### C.2 Ten agent classes (vs. fewer; vs. more)

**Date:** 2026-05-02
**What:** §3 enumerates exactly ten classes: Orchestrator, Generator,
Steward, Sensor, Reviewer, Governor, Federator, Attester, Shepherd,
Combatant.
**Why-not-otherwise:** Earlier drafts considered seven (collapsing
Sensor into Steward; Combatant absent; Attester folded into Federator).
Each collapse lost precision: TIDY's vitals work and SCAN's signal work
have different verb shapes; combat is a real class of agent behavior
that needs its own declaration; Attester verifies cryptographic
ground truth distinct from federation routing.
**Evidence:** Live audit of reference-implementation agents and the
verb shapes they actually embody.

### C.3 Multi-class membership permitted

**Date:** 2026-05-02
**What:** §3.11 allows agents to span multiple classes when behavior
matches multiple verb shapes.
**Why-not-otherwise:** A class system that forces exclusive
membership would either misclassify multi-purpose agents (WARDEN as
*only* Federator, ignoring its Governor responsibilities) or split
them artificially. Permitting multi-class membership honors observed
behavior at the cost of slightly more complex validation.
**Evidence:** Reference-implementation agents (WARDEN, TIDY, CHALKE,
RIVET) all naturally span multiple classes.

### C.4 Override scope clarified to "within human accountability"

**Date:** 2026-05-02
**What:** §7.3 and §9.2 explicitly bound the override right to the
human's scope of accountability.
**Why-not-otherwise:** Earlier draft language read as "any human can
override anywhere," which contradicted the actual design intent.
The bounded form preserves the safety-net property within scope
(no platform element blocks override) while preventing authority
laundering across accountability boundaries.
**Evidence:** Author review caught the over-reach during prose
authoring.

### C.5 Stewardless scope handling as a foundational concern

**Date:** 2026-05-02
**What:** §12.7 specifies nomination, peripheral-human search,
stewardship-suspended state, and adversarial-nomination defense.
**Why-not-otherwise:** Treating zero-accountable-human as an
exception (rather than a known operating condition) leaves real
deployments stranded. Realistic scenarios — abandoned open-source
deployments, departed PIs, expired one-time experiments — make
stewardlessness common enough to specify, not common enough to
ignore.
**Evidence:** Author/editor recognition of the failure mode in
ongoing reference-implementation work.

### C.6 Two absolutes: §7.3 (override) and §13.3 (amendment base case)

**Date:** 2026-05-02
**What:** AEOS 1.0 states two absolutes — within-scope override
cannot be blocked, and the rights of override and leave cannot be
amended away.
**Why-not-otherwise:** Most rules in AEOS use measured-bias language
(SHOULD, by default, conformant runtimes typically). The absolutes
are reserved for the cases where any softening invites the failure
mode the rule prevents. Override is the safety-net; the
amendment-base-case prevents the amendment process from removing
the safety-net.
**Evidence:** Architectural reasoning from first principles.

### C.7 ADR references abstracted

**Date:** 2026-05-02
**What:** Specific Axiom ADR references were converted to
"reference-implementation citations" (e.g. "the conformant runtime's
trust-graph mechanism *(reference: Axiom's ADR-028)*").
**Why-not-otherwise:** A portable standard cannot have normative
dependencies on a single implementation's internal documents. The
abstraction preserves the illustrative value of the citations while
allowing other implementations to supply their own equivalents.
**Evidence:** Reviewer concern raised during 1.0 prose authoring.

---

## Appendix D — Future Exploration: Elections, Voting, and Collective Decision Procedures

**Status:** Not specified in 1.0; placeholder for future revision.

The 1.0 specification scatters quorum and threshold concepts across
several sections — §6.3 (trust-graph composition for sibling
validation), §7.5 (quorum approval for accord amendments), §13
(quorum for AEOS-itself amendments), §8.4 (forced exit may require
steward agreement). These implicit voting patterns are not currently
unified into a single *Elections and Voting* mechanism, and the
omission is deliberate: collective decision procedures introduce
design surface area that deserves its own treatment rather than
piecemeal embedding.

A future revision should explore:

**Where does voting have a formal role?**

- Cohort-level decisions where no single steward owns the call
- Trust-graph weight changes (promotions, demotions, eviction)
- Constitutional amendments (§13)
- Forced expulsion of a peer cohort
- Override of an established accord by a quorum of signatories
- Election of cohort stewards when the role is delegable rather than
  fixed
- Adoption of stewardless scopes (§12.7) when multiple legitimate
  candidates emerge

**Who votes?**

- Accountable humans: presumed default
- Designated stewards: when delegated explicitly per §7.1
- Service agents: only within pre-approved policy envelopes
- Agents on behalf of accountable humans: not by default; would
  require explicit, narrow, human-signed delegation analogous to
  §7.1.1

**What thresholds apply?**

- Simple majority: low-stakes decisions
- 2/3 (two-thirds) majority: a useful inflection point worth
  formalizing for moderate-stakes decisions
- Supermajority (3/4 or higher): high-stakes decisions
- Unanimity: rarely; reserved for foundational changes

**How are votes weighted?**

- Equal vote (one voter, one ballot)
- Trust-weighted vote (each ballot weighted by trust-graph standing)
- Reputation-bounded equal vote (equal vote with a trust-graph minimum
  to be eligible)
- The choice likely varies by decision type

**Procedural questions**

- Time-bounded voting windows
- Voter eligibility verification via attestation chains
- Vote recording (signed; secret or public depending on decision type)
- Electoral dispute resolution through the higher-authority chain
- Quorum-of-quorum for "the vote even counts"

**Open structural question:** does this become a §6.X subsection
(extending checks-and-balances), a new top-level §20 (Elections), or
its own separate spec referenced from AEOS? The answer probably
emerges from how heavily later operational specs need to compose
voting primitives. For now, AEOS 1.0 treats every quorum-shaped
decision as bespoke per its enclosing section, with the understanding
that the duplication will motivate consolidation.

---

## Appendix D — Future Exploration: Elections, Voting, and Collective Decision Procedures

**Status:** Not specified in 1.0; placeholder for future revision.

The 1.0 specification scatters quorum and threshold concepts across several
sections — §6.3 (trust-graph composition for sibling validation),
§7.5 (quorum approval for accord amendments), §13 (quorum for AEOS-itself
amendments), §8.1 (forced exit may require steward agreement). These
implicit voting patterns are not currently unified into a single
*Elections and Voting* mechanism, and the omission is deliberate:
collective decision procedures introduce design surface area that
deserves its own treatment rather than piecemeal embedding.

A future revision should explore:

**Where does voting have a formal role?**

- Cohort-level decisions where no single steward owns the call
- Trust-graph weight changes (promotions, demotions, eviction)
- Constitutional amendments (§13)
- Forced expulsion of a peer cohort (Combatant-class enforcement at scale)
- Override of an established accord by a quorum of signatories
- Election of cohort stewards when the role is delegable rather than fixed

**Who votes?**

- Accountable humans: presumed default
- Designated stewards: when delegated explicitly per §7.1
- Service agents: only within pre-approved policy envelopes (§7.1.3)
- Agents on behalf of their accountable humans: not by default; would
  require explicit, narrow, human-signed delegation analogous to §7.1.1

**What thresholds apply?**

- Simple majority: low-stakes decisions (e.g., scheduled accord
  renewal with no terms changed)
- 2/3 (two-thirds) majority: a useful inflection point worth formalizing
  for moderate-stakes decisions (e.g., accord term changes, trust-graph
  promotions)
- Supermajority (3/4 or higher): high-stakes decisions (constitutional
  amendments, forced expulsion, override of human-protected rights)
- Unanimity: rarely; reserved for foundational changes that would
  invalidate prior conformance

**How are votes weighted?**

- Equal vote (one voter, one ballot): the simplest model; risks
  ignoring trust-graph standing
- Trust-weighted vote: each voter's ballot weighted by their trust-graph
  standing; respects observed reputation but risks consolidation of
  influence
- Reputation-bounded equal vote: equal vote with a trust-graph minimum
  to be eligible to vote at all; combines accessibility with quality
  filtering
- The choice likely varies by decision type

**Procedural questions**

- Time-bounded voting windows: vote opens, vote closes, results commit
- Voter eligibility verification via attestation chains
- Vote recording in the provenance ledger (signed by voter, secret or
  public depending on decision type)
- Electoral dispute resolution through the higher-authority chain
- Quorum-of-quorum for "the vote even counts" (separate from the
  threshold for it to pass)

**Open structural question:** does this become a §6.X subsection
(extending checks-and-balances), a new top-level §20 (Elections), or
its own separate spec referenced from AEOS? The answer probably
emerges from how heavily later operational specs need to compose
voting primitives. For now, AEOS 1.0 treats every quorum-shaped
decision as bespoke per its enclosing section, with the understanding
that the duplication will motivate consolidation.

---

_Draft. §§1–7 authored 2026-05-02. §§8–19 + appendices remain skeletal pending the next drafting round; Appendix D added as future-exploration placeholder._
