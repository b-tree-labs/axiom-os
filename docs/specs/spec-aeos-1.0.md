# Agent Ecosystem Open Standard (AEOS) — Version 1.0

> **DRAFT.** §§1–7 authored. §§8–19 + appendices remain skeletal pending the next drafting round. §20 (Graduated Autonomy), principle §2.15 and the §2.16 glossary drafted 2026-10-10.
>
> **Governing spec:** AEOS 0.1 ([spec-aeos-0.1.md](spec-aeos-0.1.md)) is the canonical standard and governs extension conformance; this 1.0 successor is in draft and is not yet the governing spec.

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

- *Federation-native* — peer cohorts compose while each keeps its sovereignty.
- *Human-accountable* — the human is the corrective for what the platform cannot detect; conformant runtimes make this practical.
- *Deterministic where it counts* — LLMs may advise; deterministic code authorizes the consequential operations.
- *Provenance-everywhere* — the audit trail is the architecture's evidence handle, designed in from the start.
- *Tier-respecting* — surfacing rules govern visibility; available capability is the same at every tier.
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

LLMs may advise; deterministic code authorizes the consequential operations. AEOS trust profiles, RACI gates (the §20 evaluator), classification stamps, and accord enforcement are all deterministic primitives. The boundary is load-bearing: collapsing it (allowing the LLM to authorize) is the failure mode the principle prevents. *(Carries from 0.1 §3.4.)*

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

The state-externalization rule is the unifying principle the twelve clauses 2.1–2.12 compose toward. It is the principle every other AEOS conformance check, in any sub-section, may be re-derived from.

### 2.14 Configuration is durable + watched + auditable

Conformant runtimes MUST expose a configuration primitive with the following properties:

- **Schema-validated.** Each extension declares its configurable fields with type + classification + lockable flag. Only writes that pass validation take effect.
- **Watched at filesystem (or equivalent) level by default.** Changes to the externalized configuration propagate to in-process subscribers while the process keeps running of the consuming process.
- **Receipt-emitting.** Each change produces an audit fragment per the governance fabric (ADR-055), classified at the receiving field's classification floor.
- **Lockable.** A field MAY be declared `lockable=true`; in that case, an explicit `lock` operation commits a value pending an authority's override. The lock-override authority composes with the keystore + capability primitives but is OUT of scope for the configuration primitive itself: this section requires the predicate, not the cryptographic enforcement.
- **Importable, not reinvented.** Extensions MUST consume the platform's configuration primitive (`axiom.infra.config` in the Axiom runtime) and MUST NOT roll their own watched-config layer.

The reactor lesson is the lock + receipt path; the cloud lesson is the watch + ephemeral path; the unifying lesson is that one primitive serves both because both want the same property.

### 2.15 Autonomy is certified per seat, under a human

Autonomy belongs to **seats**: entries in a runtime registry. Each seat
holds a certified rung per capability for each operating identity (an
agent's version plus anything that could move the trust placed in it,
§20.2.2) and has its own seat resolver, who is
a person or a named group with a named leader. At the moment an agent acts,
it binds to whichever seat covers that action, so binding is per action:
the action decides the seat. **Extensions are packaging, not seats**
(§20.2.1); seats come from the registry.

A seat's rung for a capability moves up one rung at a time, Observe →
Suggest → Draft → Act with approval → Act, only on evidence a person
certifies. It moves down at once. A declared ceiling caps it, and the
Accountable party is always a named human. One deterministic evaluator gives
the answer, and that answer is the only one. Real-world actuation follows
the same rule: it is governed by the seat that covers it, under a low
ceiling, and always passes through the interlocks. §20 is normative, and its terms are
defined in §2.16.

### 2.16 Glossary: graduated autonomy

These are the terms §2.15 and §20 use. Each has exactly one meaning in
this specification. Terms defined elsewhere in AEOS (capability kinds §4,
agent classes §3, rights §9) keep those meanings and are cross-referenced
here. ADR citations point to the reference implementation (Appendix C.7).

**Seats, binding and packaging**

- **Seat.** The atomic unit of accountability for agent action. A seat is
  an entry in the registry that covers a set of actions. It holds a
  certified rung per capability, a ceiling and a resolver (§20.2).
  *Reference: Axiom's ADR-126 "seat", "a recurring decision position".*
  **Conflict noted:** Axiom's ADR-166 uses "tracker seat" for an account on
  an external tracker, as in "membership is a principal, not a tracker
  seat". That sense means an account on an external system; autonomy comes only
  from AEOS seats.
- **Registry (seat registry).** The runtime's collection of seats, and the
  only source of binding (§20.2). It is separate from the extension registry,
  model registries, and §12.7.4's registry of stewardless scopes.
- **Binding.** An agent attaching to the one seat that covers the action
  it is about to take, at the moment it acts. Binding is per action. The
  binding records the operating identity,
  the seat, the capability and the action (§20.2). A human actor acting
  directly acts on their own authority (§7.3); seats and rungs govern
  agents.
- **Extension.** Packaging only. It bundles agents, capabilities and skills
  for install, signing and loading (§14–§16). Seats, rungs and resolvers come
  from the registry; an extension is never a seat (§20.2.1).
- **Agent.** An LLM-backed autonomous component with persistent identity
  (capability kind `agent`, §4). An agent binds to a seat and acts, and it
  is always trusted as a particular operating identity. Human-facing copy
  says "bot".
- **Operating identity.** An agent's version plus anything that could
  increase or decrease the trust that someone depending on the agent would
  want to give it. The list is open: memory, retrieval access, tools,
  peripherals and runtime are examples only. Trust and certification are
  earned per operating identity, not per version, and any change that could
  materially move that trust triggers re-certification as a new identity
  (§20.2.2).
- **Actor.** Whatever acts on a given action: an agent or a human (§7.4).
  When the actor is an agent, its identity is its operating identity. The
  principal is who the actor is or acts for; the actor is who acts this
  time.
- **Capability.** A declared function an action exercises, such as
  `grade_push` in an adapter's `capabilities` list (§15.1). Seats hold
  rungs per capability. **Distinct from** *capability kind* (§4: agent,
  tool, cmd and so on) and from a *provided component* (one
  `[[extension.provides]]` block). §20 always says "capability kind" or
  "provided component" when it means those.
- **Capability family.** A named group of related capabilities: memory,
  own-service repair, scheduled jobs, channels, multi-step operation.
  *Reference: the levels of Axiom's ADR-081 dial, reinterpreted in §20.8.*
  Rungs apply within it.
- **Skill.** SKILL.md-format, model-mediated instructions (capability kind
  `skill`, §4). A skill is packaged in an extension. When an agent acts on
  a skill, the action binds to a seat like any other.
- **Actuation (real-world control).** An action that changes physical
  state through the actuation chain (§20.7). It is a capability like any
  other, typically under a low ceiling.
- **Intent / Command.** An agent produces an `Intent`, a request. Only the
  deterministic chain turns it into a `Command`, an instruction to an
  actuator. *Reference: Axiom's spec-embodied-surfaces.*
- **Envelope check / interlock.** Deterministic safety checks in the
  actuation chain that run on every command, at every rung, and a trip is
  a refusal (§20.7). *Envelope* in §20 means
  only this physical operating envelope. A seat's certified range is its
  *certified scope*, and §7.1's "policy envelope" is a seat certified at
  rung 4.
- **Trust profile.** An extension manifest field (§15.1). It is a
  load-time requirement, separate from rungs (§20.2.1).

**People and accountability**

- **Principal.** An identity that can act or be acted for: a human, or an
  agent principal. It is distinct from *actor* (who acts this time) and
  from *operating identity* (what an agent is trusted as). *Reference: `@name:context`, Axiom's ADR-020; ADR-166.*
- **Accountable (accountable human).** The named human who answers for an
  action. Always a human, and never an agent, service, extension or named
  group (§20.1). *Reference: Axiom's ADR-035, `accountable_human_id`.*
- **Responsible.** The agent taking the action, bound to a seat for it
  (§20.1).
- **Resolver (seat resolver).** The person, or the named group, who answers
  a seat's approvals and consultations and certifies its promotions. It
  is always people; the software step is the *evaluator*.
- **Named group.** A resolver made of several people. It has a recorded
  name, recorded members and a group leader. A group decision counts only
  when the leader, or a member the leader designates, signs it. A named
  group survives membership changes (§20.10). Accountable is always a
  human, such as its group leader.
- **Group leader.** The human named as answerable for a named group's
  decisions as resolver.
- **Steward (disambiguation).** *Steward* is an agent class (§3.3).
  *Designated steward* and *cohort steward* are humans (§7.1, §12). Either
  holds a §20 role only by assignment: for example, a designated steward
  may serve as a seat's resolver.
- **Delegated accountability.** The authority a resolver exercises for a
  seat on the Accountable human's behalf. It is recorded in the delegation
  chain, and that human stays on the record. *Reference:
  Axiom's ADR-035 `delegation_chain`; authority only narrows, ADR-086.*

**Rungs and how they are decided**

- **Rung (autonomy level).** How far a seat may act for one capability.
  The five rungs are Observe, Suggest, Draft, Act with approval and Act
  (§20.3). "Autonomy level" means rung. The ADR-081 dial and the trust
  levels map onto rungs in §20.8.
- **Disposition.** The evaluator's output for one action:
  - `approve`: the resolver signs first;
  - `consulted`: a party the seat names is asked first;
  - act-then-notify (`N`): the action runs, then appears in a batched
    digest with an undo window;
  - `informed`: the action runs and is reported. *Reference: Axiom's
  ADR-045, D6.1 and D6.6.* Consulted and Informed are dispositions.
- **Evaluator.** The one deterministic function that computes
  min(ceiling, certified rung, dial) and renders the disposition (§20.4).
  *Reference: Axiom's `check_raci()`.*
- **Ceiling.** The highest rung a seat may ever reach for a capability. It
  is declared, never earned. *Reference: Axiom's ADR-135 Ceiling,
  `Condition.authority_reserved`; ADR-045 D5 floor and D6.2
  irreversibility.*
- **Consequence class.** The declared cost if an action goes wrong:
  `harm`, `loss`, `waste`, `noise`, or `unknown` when undeclared. It sets
  the evidence bar for promotion, and a deployment MAY use it to express
  ceilings. *Reference: Axiom's ADR-134.*
- **Dial.** The setting in force for a seat and capability: an override,
  otherwise the node default (§20.4 step 4). It is always at or below
  the certified rung. *Reference: `raci.<action>`, `raci.trust`.*
- **Goal.** The long horizon a person states: what done means, which
  decisions only a person may make, and what ends the work. Opening or
  replacing a goal (`/goal`) is an action. A goal does not raise a rung
  and does not authorize the steps that follow (§20.12).
- **Loop.** A wake that re-reads the current goal and takes at most the
  next increment that goal covers (`/loop`). Each wake is a new turn.
  A loop holds no authority of its own. A loop with no goal is refused.
  When the goal is done, a stop condition hits, or the next step is a
  decision the goal does not cover, the loop ends and asks (§20.12).
- **Mission.** A goal together with the loop driving it, if a loop is
  armed. Steps inside a mission still bind per action. The mission's
  ceiling is the lowest certified rung of the seats those steps bind to,
  and it cannot exceed the ceiling recorded when the goal was opened.
- **Node.** One installation of a conformant runtime. A node default is a
  setting that applies across that installation.
- **Starting rung.** A new seat's first certified rung. It is proposed by
  the node default, fixed and signed when the seat is ratified, and at
  rung 3 or below (§20.2).
- **Brake.** A control that can only lower the outcome after evaluation
  (§20.4):
  - the master off switch, which stops anything from acting unattended;
  - `halt`;
  - a volume or rate circuit-breaker;
  - a novelty check on a target outside the seat's certified scope,
    meaning the repos, paths or sites it was certified on.
- **Halt.** A fleet-wide stop. It is resolved before any routing and
  outranks every rung (§20.7, §7.1.1).
- **Override.** An in-scope human's universal power to stop or revert an
  action (§7.3, §9.2). It can only lower the outcome; raising a seat is
  certification (§20.4).
- **Sibling validation.** Another agent class's check, which §6.2 requires
  for listed actions. It applies at every rung, separately from human
  approval (§20.4).
- **Service agent.** §7.1's step-3 agent. In §20 terms, it is an agent bound
  to a seat certified at rung 4 for the action's capability.

**Certification and records**

- **Certification.** A person's signed act that promotes a seat one rung
  for one capability, on Fitness, Maturity and outcome evidence (§20.5).
  *Reference: the `autonomy` logbook in Axiom's spec-attestation.*
- **Fitness.** A check run immediately before acting that shows the seat
  is fit for this mission now. A *mission* is the specific work about to
  be done, with its conditions. *Reference: Axiom's ADR-135.*
- **Maturity.** The record of decisions taken in a seat for a capability.
  It is read, never declared. *Reference: Axiom's ADR-135, `calibrate`.*
- **Demotion / revocation.** Lowering a seat's rung for a capability. It
  takes effect immediately. Automatic demotion is recorded as a receipt;
  a human revocation is signed (§20.5).
- **Receipt (decision receipt).** The record of a decision taken by a rule,
  a model or a person, including its later `outcome`. For an agent it names
  the operating identity. It records a decision, which is distinct from an
  attestation. *Reference: Axiom's ADR-126.*
- **Attestation (two senses, kept apart).**
  - *Cryptographic attestation*: a signed machine claim, such as a signed
    release, a key or a behavioural attestation (§3.8, §17). Attester
    agents issue these.
  - *Human attestation*: a human principal's signed act. Certification is
    one. Software never makes one *(reference: Axiom's ADR-142 rule 1,
    "Software never attests")*.

  §20 means the human sense unless it says otherwise.
- **Ack window.** How long a seat waits for its resolver to answer before
  the request escalates. Silence is never approval (§20.10).
- **Escalation.** Routing a decision to the nearest person with authority
  over it: first the seat's resolver, then the §7.1 chain. A yield under
  §7.2 escalates, and so does a vacant resolver (§20.10).
- **Policy gap.** An action outside every seat's coverage, or a case outside
  every policy. It
  yields and escalates (§7.2).
- **Handoff.** Work passing from one agent to another. Responsible may
  change; Accountable stays the same (§20.6). A shift turnover between people is
  a resolver change (§20.10).

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

A steward keeps the operational substrate healthy — disk usage, scratch directories, stale state, leak detection, retention enforcement, hygienic sweeps. The steward acts continuously and largely autonomously; its work is mostly invisible until it surfaces a hygiene concern. Its autonomy for each action comes from the seat that covers that action (§20.2); class membership confers none. (The *Steward* class is not the human *designated steward* of §7.1; see §2.16.)

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

An attester originates and verifies cryptographic claims — identity verification, capability attestations, signed extension releases, key rotations, vouching for principals. The attester is the cryptographic ground truth for all the other classes that consume signed material. Its attestations are cryptographic attestations (machine claims), never human attestations or §20 certifications (§2.16).

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
- SHOULD allow validation thresholds to be expressed in trust-weighted terms as well as simple counts
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

The full three-part requirement applies to rights-affecting actions; many
actions are read-only or trivially reversible and leave rights untouched. AEOS
classifies actions by their reversibility and rights-impact:

| Class | Definition | Due process |
|---|---|---|
| **Read** | Returns information; state is unchanged | None required beyond audit trail |
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
  (user types a phrase rather than `y`); the phrase cites the resource
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
  archival form; future returns resume from that archived standing
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
  orderly conclusion that preserves every member's data

### 12.6 Continuity guarantee

Across all succession events, conformant runtimes guarantee:

- Every action keeps its provenance — the audit trail survives all
  transfers
- Every user keeps their §9 rights — rights persist across role changes
- Every accord ends only on the record — it either continues under new
  authority or is explicitly terminated with a recorded cause
- Every participant keeps an exit — at least one
  exit path (per §8.4) regardless of upstream events
- Every seat stays answerable — seats, their certified rungs and their records
  outlive any resolver, group leader or bound agent, and a vacant
  resolver narrows the seat (rung-4 Soft writes only) until a successor
  is named (§20.10; example §20.11.3)

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
Every nomination names its nominator.

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

- All non-trivial actions in the scope are queued.
  "Non-trivial" follows the §10.2 classification: read and soft-write
  actions continue; hard-write and destructive actions queue.
- Federation peers are notified of the suspended state so they can
  decide whether to extend their own §12.7.3 search or to terminate
  accord relationships with the scope.
- The scope's §9 rights remain in force for any remaining users —
  they MAY still exercise sovereignty, override, know, portability,
  redress, and leave. User rights survive stewardship
  suspension.
- A **registry of stewardless scopes** is maintained — discoverable
  by any conformant runtime — listing scopes in suspended state with
  their cited search history. The registry exists to make adoption
  possible: a human encountering a stewardless scope they have
  legitimate connection to MAY initiate a nomination per §12.7.1
  with themselves as candidate.

If a stewardless scope remains in suspended state past a
runtime-defined long-window (default: 1 year), the runtime SHOULD
proceed to an **orderly conclusion** per §12.5 (cohort dissolution)
— preserving exports, accord-history, and rights records. Suspension
is meant to end; the architecture prefers
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

For agents, the observation SHOULD name the operating identity it was made
under. It is evidence, never certification (§20.2.2).

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

## 20. Graduated Autonomy (normative)

> **Drafted 2026-10-10** (Editor: Benjamin Booth). A **Material**
> amendment under §13.2: a new section and a new principle (§2.15). It
> changes no text in §7 or §9.

Graduated autonomy answers one question: **may this action, taken now by
this agent in the seat that covers it, proceed before a person approves
it?** A long job is many such actions. The long horizon is a **goal**,
and a **loop** re-reads that goal on each wake (§20.12). Neither one
raises a rung. AEOS treats this as *one system*. Ownership vocabulary (RACI),
trust levels, autonomy dials and autonomy gates are all inputs to one
evaluation, and one
deterministic **evaluator** renders the outcome (§2.4). A runtime with two
answers to "can this agent act?" is non-conformant, whichever answer happens
to be right.

Every term this section uses is defined once, in §2.16 *Glossary:
graduated autonomy*. Four of them are easy to confuse, and they stay
strictly apart:

- *extension*: packaging;
- *seat*: a registry entry, and the atomic unit of accountability;
- *resolver*: a person or a named group;
- *evaluator*: deterministic software.

### 20.1 Two roles; everything else is a disposition

- **Accountable** is always a named human (§1 *Accountability*, §7.1).
  Every action and every handoff resolves to one, and a runtime MUST NOT
  record an agent, a service, an extension, a named group or "the system"
  as Accountable. A named group can be a resolver, never Accountable *(reference: Axiom's ADR-035, `accountable_human_id`,
  mandatory and never null)*. When none can be found, §12.7 applies and the
  action yields.
- **Responsible** is the agent taking the action. For that action it is
  bound to the seat that covers it (§20.2), and it acts at that seat's rung
  for the action's capability, at most.
- **Consulted** and **Informed** are **dispositions**
  the evaluator produces (§20.3):
  - `consulted`: a party the seat names is asked before the action, and
    the action waits for the answer;
  - `informed` or `N`: the resolver is told after the action.

  Only `approve` requires the resolver's signature.

A seat resolver answers for the seat on the Accountable human's behalf, and
keeps that human in the record. In §7.1 terms, the resolver is
the person the Accountable human delegated to (step 2, designated steward),
unless the resolver is the Accountable human. When the resolver is a named
group, its group leader is a human who answers for the group's decisions. A
group decision counts only when the leader, or a member the leader
designates, signs it.

The RACI name MAY be kept as shorthand for this pair of roles. In human-facing
copy, implementations SHOULD use the words rather than the letters. In the
reference implementation the letter `A` in the disposition matrix means
*approve*, not *Accountable*, so a bare letter is ambiguous.

### 20.2 Seats are a registry; binding is per action

Autonomy is held by **seats**: agents bind to them, and extensions package
the agents. A conformant runtime keeps a **seat registry**. Each seat records:

- the actions it covers;
- a certified rung **per capability**, recorded for each operating
  identity that has worked the seat (§20.2.2, §20.3, §20.5);
- its ceiling (§20.4 step 2);
- its seat resolver: a named person, or a named group with a named leader.
- the parties it consults before an action, if any. These are named for
  the `consulted` disposition, and may be people or, under §6, sibling
  agents.

*(Reference: Axiom's ADR-126 "seat", "a recurring decision position", where
reliability attaches to decider-in-seat-at-site; ADR-135 measures maturity
per (solver, condition), "never per solver".)*

A seat works like a station on a factory floor. The station has its own
sign-off for each machine and its own supervisor. Whoever works there works
under that station's sign-off, and a worker who walks to another station
works under that station's.

**Seats are ratified policy.** Creating, changing or retiring a seat is
policy ratification under §6.2. A Governor (§3.6) or a person may propose
it, and the Accountable human for the actions it covers ratifies it.

**Binding is per action.** At the moment an agent acts, the runtime looks up
the seat that covers that action, and the agent is bound to that seat for
that action only. The same agent can be bound to a different seat for its
next action.

- An action outside every seat's coverage is a policy gap (§7.2). The action itself
  yields. The agent may still observe, suggest or draft about it
  (rungs 0–2).
- The registry MUST resolve each action to exactly one seat. Overlapping
  coverage is a registry conflict, and while it stands the lowest
  effective rung of the overlapping seats applies.
- A seat MAY restrict which agents may bind to it. If an operating identity
  (§20.2.2) is new to the seat, it starts at the seat's starting
  rung. If it is the successor of an identity the seat certified, it starts
  at the lower of its predecessor's rung and rung 3 *(reference: Axiom's
  ADR-045 D6.4, novelty confirmation)*.
- A node-wide or user-wide setting MAY **lower** any seat at once (a
  brake). It MAY propose a new seat's **starting rung**, which works as
  follows:
  - it is fixed when the seat is ratified;
  - it is signed as the seat's first certification (§20.5);
  - it MUST NOT exceed rung 3.

  Rung 4 is reached only by certification on evidence. Settings can only
  hold a seat at or below its certified rung.
- Every binding record names the operating identity, the seat, the
  capability and the action.
- An agent's class (§3) describes what the agent does. Autonomy comes
  from seats. A Steward "acts continuously and largely autonomously" (§3.3)
  only where the seat covering each action is certified at rung 4.
- Certification stays with its seat, node and cohort; it transfers only
  when the receiving Accountable human consents *(reference: Axiom's ADR-043 D7;
  ADR-045 D3, cohort-inherited overrides apply only at a matching level)*.

#### 20.2.1 Extensions are not seats

An extension is packaging: it bundles agents, capabilities and skills.
Seats live in the registry. There is no one-to-one mapping between
extensions and seats, in either direction.

- A runtime MUST NOT derive a seat, a rung or a seat resolver from the
  extension that packages an agent.
- Conformance MUST NOT depend on an extension declaring a seat. An
  extension manifest MUST NOT bind its agents to seats.
- An extension declares **what it provides and which capabilities it
  exercises or actuates**:
  - its provided components: the `[[extension.provides]]` blocks, by
    capability kind (§4);
  - the capabilities those components exercise, such as an adapter's
    `capabilities` list (§15.1).

  That declaration lets the registry route the extension's actions to
  seats. Seats and rungs come from the registry and from certification.
- The manifest's `trust_profile` is a load-time requirement, separate
  from rungs.
- Seat binding comes from the registry at runtime (§20.2), never from the
  manifest.

*Example:* an `ops` extension packages one agent. That agent binds to the
**release** seat when it tags a build, and to the **hygiene** seat when it
prunes a merged branch. The hygiene seat also covers prune actions from a
`lanes` extension and a `storage` extension. A fuller example is in
§20.11.1.

#### 20.2.2 Operating identity

**Definition.** An agent's **operating identity** is its version (the
signed release, §17.1) **plus anything that could increase or decrease the
trust that someone depending on the agent would want to give it.** Trust is
earned per operating identity, not per version. Two instances of the same
version are distinct identities when anything that could move that trust
differs between them.

What can move trust is open-ended. Illustrative examples, including but
not limited to: memory, retrieval and corpus
access, tools and skills, peripherals and actuators, model and runtime, and
the authority the agent runs under. Terms AEOS already defines count too:
the capabilities and capability kinds (§4) an agent exercises, its
extension's manifest fields such as `trust_profile` (§15.1), and its
behavioural attestation (§17.4).

- **Recording.** A runtime MUST give each operating identity a stable
  identifier, derived from the version and from every trust-relevant fact
  the runtime knows about. It MUST record that identifier, not just the
  version, in every binding (§20.2), every decision receipt, and the actor
  field of §7.4's provenance contract.
- **Re-certification on change.** Any change that could materially move
  that trust triggers re-certification. The changed agent is a **new
  operating identity**, and it earns certification of its own. At every
  seat where its predecessor was certified, it starts at the lower of the
  predecessor's rung and rung 3, so it acts at most with approval. The
  predecessor's record is admissible lineage evidence, so one fresh
  certification can restore the next rung. As always, promotion is one rung
  at a time (§20.5). If it is unclear whether a change could move trust,
  treat it as if it could.
- **Scope.** Certification, Maturity and the first-binding rule are all kept
  per operating identity, per seat, per capability (§20.4 step 3, §20.5).

**Why a new identity, not a flag on the old one.** A receipt has to name
what actually acted. If the identity were mutable and carried a
"re-certification pending" flag, past and future decisions would be
attributed to an agent that no longer exists in that form. A new identity
also makes §20.5's "demotion is immediate" true by construction: the
changed agent runs on the capped rung from its first action. The cap at
rung 3 and the lineage evidence keep the change from resetting everything.

**Behavioural attestation (§17.4)** is a cryptographic attestation about
an extension, and it is a different thing. It MAY be cited as Maturity
evidence for an operating identity, but only when the behaviour it records
was observed under that same identity. Rungs come only from
certification.

*Agent*, *actor*, *principal* and *operating identity* stay distinct:

- the **agent** is the component (§4);
- the **actor** is whatever acts on a given action, an agent or a human
  (§7.4);
- the **principal** is the identity that can act or be acted for
  (ADR-020);
- the **operating identity** is the agent-as-trusted that bindings, receipts
  and certifications name.

When the actor is an agent, its actor identity *is* its operating identity.

*Worked example: §20.11.1.*

### 20.3 Five rungs

For each capability, a seat sits on exactly one rung. The rungs are
cumulative: a rung includes everything below it.

| Rung | The bound agent may | Disposition rendered | The seat resolver's part |
|---|---|---|---|
| **0 · Observe** | Read, retrieve and answer. Its output is information only. | None (read) | None beyond the audit trail (§10.2 *Read*) |
| **1 · Suggest** | Propose, in conversation. The proposal stays in the conversation until a person acts. | None (no action to evaluate) | Decides whether to act on the suggestion |
| **2 · Draft** | Produce an artifact that a human applies: a document, a diff, a work order, an `Intent` held for a person to submit. | None (the human's apply is the action) | Applies it, or not |
| **3 · Act with approval** | Perform the action after a person approves it. | `approve` (the seat resolver signs first), or `consulted` (a party the seat names is asked first and the action waits for the answer) | Approves, denies or answers. Blocking. |
| **4 · Act** | Perform the action, then tell. | `informed`, or act-then-notify (`N`): the action appears in a batched digest with an undo window | Reviews the digest. Can undo within the window, and can demote the seat at any time. |

At rung 4, act-then-notify is the default for reversible actions. Silent
`informed` is reserved for actions that are trivially reversible, frequent
and low-blast *(reference: Axiom's ADR-045 D6.1, D6.6)*.

These five rungs are the names for what the existing mechanisms already
decide (§20.8 maps them).

### 20.4 The composition rule

For every action, the evaluator works through these steps in this order. The
first step that stops the action decides the outcome.

1. **Who and which seat.** Resolve the Responsible agent and its operating
   identity (§20.2.2), the Accountable human (§20.1), and the seat that
   covers this action (§20.2). If there is
   no Accountable human, yield (§12.7). If no seat covers the action, yield
   (§7.2).
2. **Ceiling.** The highest rung the seat may ever reach for this
   capability. It is *declared*, never earned, and it holds against any record
   *(reference: Axiom's ADR-135 Ceiling, `Condition.authority_reserved`)*.
   These also cap the rung:
   - the safety floor: actions that always require approval *(reference:
     Axiom's ADR-045 D5)*;
   - irreversibility: an irreversible action stays at rung 3 or below
     *(reference: Axiom's ADR-045 D6.2)*;
   - consequence class: `harm`, `loss`, `waste`, `noise`, and `unknown` when
     undeclared *(reference: Axiom's ADR-134)*. Consequence sets how much
     evidence promotion needs (§20.5). A deployment MAY also express its
     ceilings per class. By default, `harm`-class work is capped at rung 3.
3. **Certified rung.** The rung the bound operating identity is certified
   for, at this seat, for this capability (§20.2.2, §20.5). This includes a current fitness check where the ceiling
   requires one.
4. **Dial.** The setting in force for this seat and capability. It is an
   override if one exists, otherwise the node default.
5. **Evaluate.** The effective rung is
   **min(ceiling, certified rung, dial)**. The evaluator renders it as the
   disposition in §20.3 *(reference: Axiom's `check_raci()` in
   `src/axiom/infra/raci.py`)*.

Rules from other sections apply in the same pass. Like the ceiling, they
can only lower the outcome:

- **Sibling validation (§6).** An action listed in §6.2 needs its sibling
  validation at every rung. Sibling validation and the resolver's approval
  at rung 3 are separate requirements, and each is needed in its own
  right.
- **Due process (§10).** A Hard write or Destructive action (§10.2) that
  affects a §9 right is capped at rung 3, so notice and the chance to
  override come before it commits (§10.1). Its approval uses §10.3's
  tier-aware confirmation, and §10.3's time-critical safety case is the
  only exception. A Soft write MAY run at rung 4 with `N`, because its undo
  window is the single-operation revert §10.2 requires.
- **Human override (§7.3, §9.2).** An in-scope override always stops or
  reverts the action, at every rung. Overrides only lower: raising is
  certification (§20.5), and ceilings are fixed by declaration.

In §7.1 terms, a seat certified at rung 4 for a capability is the
"pre-approved policy" a service agent acts within (§7.1, step 3).

Runtime brakes act **after** evaluation. They can only lower the outcome:

- the master off switch (§20.8);
- `halt`;
- a volume or rate circuit-breaker;
- a novelty check on a target outside the seat's certified scope.

A brake can send a single burst back to rung 3 without demoting the seat
*(reference: Axiom's ADR-045 D6.3, D6.4)*. Every surface that displays or
changes autonomy, including dials, onboarding and coordination views, reads
from and writes to these inputs; the evaluator decides.

### 20.5 Promotion is certification; demotion is immediate

- **Promotion is one rung at a time, per seat and capability, on
  evidence.** The evidence is:
  - **Fitness**: a check run immediately before acting, for this mission;
  - **Maturity**: the record of decisions taken in this seat for this
    capability, kept per operating identity (§20.2.2). It is read, never
    declared.
  - **Outcomes**: results stamped on decisions after the fact.

  The consequence class sets the bar: how much record, and how recent a
  fitness check *(reference: Axiom's ADR-135 Fitness and Maturity; ADR-126
  `outcome`)*.
- **The seat resolver certifies.** The runtime MAY propose a promotion
  ("approved 5/5, no reversals, promote?"). Only a person signs it: the
  resolver, or, for a named group, its group leader or a member the leader
  designates. The signed record says
  who certified which operating identity, at which seat, for which
  capability, at which rung *(reference:
  the `autonomy` logbook, `AUTONOMY_CHANGE` `delegated`, in Axiom's
  spec-attestation; ADR-142 rule 1, "Software never attests")*.
- **Demotion and revocation are immediate.** One signal or one person is
  enough, with no review, quorum or co-signature. Any of the following lowers the seat's rung for that
  capability at once:
  - a reversal or retraction;
  - a failed or stale fitness check;
  - an outcome that contradicts the decision;
  - an interlock trip;
  - the resolver, the Accountable human, or an in-scope override (§7.3)
    saying so.

  When the runtime demotes a seat on its own, it records a decision receipt.
  When a human revokes, the human signs `revoked`.
- **Re-promotion after a demotion needs fresh evidence, and more of it** than
  the first promotion did *(reference: Axiom's ADR-043 D5, `DEGRADED`; ADR-045
  D4, dwell time)*.
- **A change that could materially move trust** triggers re-certification
  by creating a new operating identity (§20.2.2). Worked example: §20.11.1.

### 20.6 Handoffs

When work passes from one agent to another, **Responsible may change.
Accountable stays.** The originating human stays recorded across the hop
*(reference: Axiom's ADR-035; ADR-034 `handoff_to_peer`)*. Authority
**only narrows** *(reference: Axiom's ADR-086, monotonic narrowing)*. Each
action the receiving agent takes binds to whichever seat covers it, and runs
at the rung the receiving agent's own operating identity holds there,
capped by what the delegator could do. Each identity brings only its own
rungs to a handoff. Worked example: §20.11.2.

### 20.7 Real-world actuation is governed like any other action

Agents may affect the physical world. Graduated autonomy is **the safe
path** to real-world control: a declared, evidenced and revocable route. The alternatives are "never",
which pushes control into unaudited side channels, and ad-hoc scripts,
which skip the evidence.

- **Actuating actions are covered by seats like any other action.** Which
  seat covers an actuation comes from the registry, independent of the
  extension that packages the driver or the agent. Actuation climbs the
  same rungs, by certification only. It is usually capped low by its
  consequence class and its ceiling, and it always has a human
  Accountable.
- **Autonomy decides whether an `Intent` may be submitted before a
  person approves it.** An agent produces an `Intent`, never a `Command`.
  - At rungs 0–1 the agent only observes or suggests.
  - At rung 2 the agent drafts an `Intent` and a person submits it.
  - At rung 3, it waits at the approval gate until the seat resolver
    approves.
  - At rung 4, it is submitted without per-act approval, and the resolver
    is notified.
- **The command chain is untouched by autonomy.** On every command, at every
  rung, the deterministic chain runs outside the model loop:
  `AuthorityRegistry.authorize` → `CommandRouter.route` →
  `EnvelopeCheck.check` + `Interlocks.evaluate` → `ActuatorWriter.write`.
  Every envelope check and interlock runs, in order, at every rung. An
  interlock trip is a refusal, and it
  counts as evidence against the seat *(reference: Axiom's
  spec-embodied-surfaces, *Actuation surface*: "intents whose RACI tier
  requires a human pause on the durable ApprovalGate … Autonomy graduates
  per tier, never per prompt")*.
- **`halt` outranks every rung.** It is resolved before any routing. A
  regulatory or safety halt can hold against human override under §7.1.1.
- **Only people make human attestations.** An autonomous actuation is
  recorded as a decision receipt *(reference: Axiom's ADR-142
  rule 1, "Software never attests")*. The human act that lets a seat act
  unattended is the signed certification (§20.5). An Attester agent's
  signatures (§3.8) are machine claims, separate from certifications.
- **Reserved authority stays reserved.** A law, a licence or a regulator
  may reserve an act to a person. The ceiling for that capability is then
  declared at the rung below unattended action, and it holds against any
  record.
  Replay, simulation and shadow operation are how a seat earns the rungs
  beneath that ceiling.
- **Surfaces carry authority only through seats.** A view that coordinates
  agents renders decisions and routes them. It reaches the world only
  through actions bound to seats under this section.

### 20.8 One vocabulary

Human-facing copy uses the rung names **Observe, Suggest, Draft, Act with
approval, Act**, and says "bot" where a person reads it. Specs, schemas and
code keep "agent". Each existing mechanism keeps its job under a single name:

| Mechanism *(reference: Axiom)* | Its job in §20 | Human-facing name |
|---|---|---|
| RACI dispositions `approve` / `consulted` / `N` / `informed` (ADR-045, `raci.py`) | The evaluator's output (§20.3) | The rung name |
| Trust levels 1–5, Locked Down … Full Trust (`raci.trust`) | The node default dial (§20.4 step 4) | "Proposed starting rung for new seats" |
| Per-action override `raci.<action>` | An override dial for the seat that covers the action (§20.4 step 4) | The seat's rung |
| ADR-081 master switch `autonomy.enabled` | A brake: when off, everything waits for a person | "Autonomy on / off" |
| ADR-081 dial levels Observe, Remember, Self-heal, Schedule, Converse, Operate | **Capability families** (rungs apply within each): memory, own-service repair, scheduled jobs, channels, multi-step operation. Seats hold a rung per capability within them. | The family name; "Observe" is the rung name only |
| ADR-043 states OFF / ASK / SCHEDULED / AUTO / DEGRADED | OFF ≈ rung 0–1; ASK and SCHEDULED = rung 3 (SCHEDULED is batched); AUTO = rung 4; DEGRADED = demoted, with the higher re-promotion bar | The rung name |
| ADR-135 Fitness, Maturity | Certification evidence (§20.5) | "Fit now", "Track record" |
| ADR-135 Ceiling, ADR-134 consequence class | The ceiling (§20.4 step 2) | "Ceiling" |
| Extension manifest `[[extension.provides]]`, `trust_profile` (§15.1) | Packaging, plus the capabilities an extension provides and actuates. **Not a seat** (§20.2.1). | Not shown as autonomy |

### 20.9 Reference implementation status (non-normative)

| Requirement | Axiom today |
|---|---|
| One evaluator | `check_raci()` exists. In `src/`, only `cli/ext/lifecycle.py` calls it. `policy/agent_action_guard.py` refuses irreversible autonomous actions on its own path. |
| Seat registry and per-action binding (§20.2) | Not built. `check_raci()` keys on an action category, with one `raci.trust` per user (global `settings.toml`, overridable per project). There is no seat record and no seat resolver. |
| Rungs 0–2 in the evaluator | Not represented. They exist only as behaviour (for example, ADR-081 level 0). |
| `N`, act-then-notify | Decided (ADR-045 D6.1). Not in `raci.py`. |
| Ceiling | `Condition.authority_reserved` ships. It is not an input to `check_raci()`, and neither is consequence class. |
| Safety floor cannot be overridden | Not enforced. A per-action override is read before the always-approve rows, so `raci.code.commit = informed` would bypass the floor. |
| Certification records | Designed (the `autonomy` logbook) but not built. Fitness and Maturity are not built. |
| Accountable is always human | Enforced at fragment write (ADR-035). |
| Seat succession (§20.10) | Not built. |
| Operating identity (§20.2.2) | Not built. Receipts record the agent and `accountable_human_id` (ADR-035), but no identity that covers the agent's environment. |
| Due-process and sibling caps (§20.4) | Not inputs to `check_raci()`. |

### 20.10 Succession: seats outlive their people

Seats are shift-proof. A seat, its certified rungs and its record belong to
the seat, and they outlast the resolver, the group leader, and any agent
bound to it.

- **Changes that keep every rung.** A shift change or a new group member
  leaves every rung where it is. A new or changed operating identity
  is handled by §20.2 and §20.2.2.
- **Changing the resolver.** This follows §12.1 when the resolver is
  unreachable, and §12.2 when the change is planned, signed by both the
  outgoing and incoming resolver. Certifications continue under the
  successor as standing authorizations (§12.1 step 4). The incoming
  resolver MAY lower any rung at once. It MUST NOT raise one without a
  fresh certification.
- **Named groups.** A named group survives changes in membership. A change
  of group leader is recorded and signed as in §12.2. A group must
  have a leader to act as resolver; otherwise it is vacant.
- **Only an answer approves.** A seat declares an ack window for its
  resolver. A request still unanswered at the end of it escalates, first to the group
  leader's designee and then along §7.1. §12.1's long timeout governs
  succession; the ack window governs individual approvals.
- **A vacant resolver** (every member of the resolver group is unreachable).
  Soft writes already certified at rung 4 continue, with `N`, and are
  reported to the Accountable human. Everything else is capped at rung 3,
  and its approvals escalate along §7.1 to the Accountable human until a
  resolver is named. If no Accountable human can be found either, §12.7
  applies (§12.7.4: reads and Soft writes continue; Hard and Destructive
  actions queue).
- **Transferring the Accountable human (§12.2).** The transfer MUST list the
  seats whose delegated accountability transfers with it.

Worked examples: §20.11.2 (turnover) and §20.11.3 (nobody answers).

### 20.11 Worked examples (non-normative)

*These examples are illustrative. Where an example and §20.1–§20.10 seem to
disagree, the normative text governs.*

**Framing: one airspace, one metaphor.** The examples use air traffic
control because handoffs, shift changes and per-position sign-off are
literal there. The airspace stands for agent work, as in a coordination
tower view:

- a **seat** is a controller position;
- a **capability** is one kind of instruction issued from that position;
- **runways** are deploy slots and lanes;
- the **watch supervisor** is the group leader of a named group;
- a **position relief briefing** is a handoff or a turnover.

Live instructions to real aircraft stay reserved to licensed people, and
§20.7 caps them.

A second metaphor was considered and declined. Hospital on-call covers
paging escalation and per-procedure privileges, and a ship's watch covers
watch turnover. ATC already has a literal form of each:

- position certification, with a trainee working under an instructor who
  can take over;
- the relief briefing;
- the supervisor change;
- "ATC zero", where a facility cannot provide service, so an adjacent
  facility takes the airspace or traffic is held.

ATC alone covers every case, with one vocabulary.

#### 20.11.1 Climbing the rungs, and binding per action (§20.2, §20.5)

**The registry entry.** The seat is ratified under §6.2: proposed by a
Governor agent and ratified by the Accountable human, `@pat:ops`.

```
seat:        approach-north
covers:      actions on runways in sector north (deploy slots and lanes for repos under north/*)
resolver:    named group "north-watch"
               group leader  @dana:ops   (designates @lee:ops to sign when she is off the position)
               members       @dana:ops, @lee:ops, @sam:ops
consults:    the release seat, on a slot conflict
ack window:  10 min
capability            ceiling                          certified rung
advise.traffic        4                                0  Observe   (starting rung)
sequence.deploy_slot  4                                0  Observe   (starting rung)
switch.prod_slot      3  (loss class; irreversible,    0  Observe   (starting rung)
                          ADR-045 D6.2)
```

The seat lives in the registry, outside every extension. The actor in this example is the agent
`sequencer` v1.4, packaged in the extension `ops-suite`. It runs as two
operating identities (§20.2.2), because the same version is deployed twice:

- **S-A** has retrieval over the runbook corpus and access to the slot-switch
  actuator;
- **S-B** has neither.

Anyone depending on these two would trust them differently, so they are
different identities. The same extension also
packages a `reporter` agent, which binds to the `briefing` seat. The seat
`approach-north` also covers sequencing actions from an agent packaged in a
different extension, `ci-bridge`. The packaging appears nowhere in the
registry entry, and the evaluation reads only the registry (§20.2.1).

**Getting skilled up.** At `approach-north`, S-A starts at Observe for each
capability.
It improves itself: it proposes revisions to its own sequencing skill and
replays each one against the journal. That improvement only adds to the
evidence. Certification still takes a person's signature (§20.5), one
rung at a time, under the ceiling.

| When | Capability | Change | Who signs, on what evidence |
|---|---|---|---|
| Week 2 | `advise.traffic` | Observe → **Suggest** | `@dana:ops`. 40 replayed sequences, 38 matching outcomes; fitness: board feed under 1 min old. |
| Week 4 | `advise.traffic` | Suggest → **Draft** | `@dana:ops`. It now writes slot orders that a person applies. |
| Week 7 | `sequence.deploy_slot` | Draft → **Act with approval** | `@lee:ops`, as `@dana:ops`'s designee. Like a trainee on position under an instructor: each sequence waits for a north-watch signature. |
| Week 12 | `sequence.deploy_slot` | Act with approval → **Act** | `@dana:ops`. Reversible, `waste` class, 200 approvals with no reversal. S-A acts, then notifies (`N`, 24 h undo window). |
| Any time | `switch.prod_slot` | Stays at or below **Act with approval** | The ceiling holds it at 3: it is declared, not earned. |

All of these certifications are recorded for S-A. Over the same weeks, S-B
earns its own record at a different seat. At `lanes-hygiene`, the resolver
there certifies S-B up to Act for `lease.release`. At `approach-north`, S-B
is new to the seat, so it would start at that seat's starting rung.
One version holds two separate records.

**Binding at the moment of action.** On one afternoon, S-A acts as follows,
and each binding and receipt records `S-A` (the identity, beyond the
version `sequencer v1.4`):

- 14:02. S-A releases a lane after a merge. The registry resolves
  `lease.release` on that lane to the seat `lanes-hygiene`, certified at
  Act. S-A binds there for this action. S-A has its own Act certification
  at `lanes-hygiene`, so the action runs with `N`.
- 14:03. S-A orders two deploys into slot B. The action is covered
  by `approach-north`, at rung 4 for `sequence.deploy_slot`, so it binds
  there and the order runs with `N`.
- 14:05. It proposes a production switch, also covered by
  `approach-north`. That capability is at rung 3, so the evaluator returns
  `approve`, and the action waits for north-watch.

That is one actor, three actions and two seats, each decided by the
registry.

**A change triggers re-certification (§20.2.2).** At week 14, a `rollback`
tool is added to S-A's environment. A tool that can undo deploys could move
the trust anyone depending on S-A would give it, so the agent is now a new
operating identity, S-A′.

- At `approach-north`, S-A′ starts at the lower of S-A's rung and rung 3,
  so `sequence.deploy_slot` drops to Act with approval at once.
- S-A's 200-approval record counts as lineage evidence.
- After 30 clean approvals using the new tool, `@dana:ops` certifies S-A′
  back to Act.

A new version (`sequencer` v1.5) works the same way: a new identity at
every seat.

**Demotion.** At week 13, a sequencing order is reversed. The rung for
`sequence.deploy_slot` drops to 3 immediately. Getting back to rung 4 needs
more evidence than the first promotion did (§20.5).

#### 20.11.2 Turnover: the group leader changes (§20.10, §12.2)

At 19:00, north-watch's group leader changes from `@dana:ops` (day) to
`@ravi:ops` (night). The position relief briefing is the §12.2 transfer.
Both sign it, and it lists open items: two pending approvals and the
morning's demotion.

- **The seat keeps resolving.** The resolver is the named group, which
  outlasts `@dana:ops`'s shift. An approval requested at 18:58 and signed at 19:02 by
  `@ravi:ops` is valid. Every open item carries over as it stands.
- **Rungs hold.** Every rung stays where it is at turnover. `@ravi:ops` MAY lower
  any rung at once. Because of weather, he holds `advise.traffic` at
  Act with approval for the night. He MUST NOT raise a rung without a fresh
  certification.
- **Agent handoff (§20.6).** At 19:10, S-A′ hands a stuck deploy to the
  agent `tidy`, which runs as operating identity T-1. Responsible changes
  from S-A′ to T-1.
  Accountable stays `@pat:ops`. T-1's cleanup action binds to the seat
  that covers it, `lanes-hygiene`, at that seat's rung, capped by what
  S-A′ could do (ADR-086). T-1 brings its own rungs.

#### 20.11.3 Nobody answers (§20.10, §7.1, §7.3, §12.7)

At 02:00, S-A′ needs approval for a production switch (rung 3).

1. **The group answers.** `@ravi:ops` does not answer within the 10-minute
   ack window, so the request goes to his designee. An unanswered request
   is never an approval. `@lee:ops` signs, because a named group's decision
   counts when its leader or a designated member signs (§20.1).
2. **Nobody in the group answers.** The resolver is vacant (§20.10). Like
   "ATC zero", the seat narrows instead of guessing:
   - Soft writes already certified at Act (lane releases, slot ordering
     with an undo window) continue. They are reported to `@pat:ops`.
   - Everything else is held at Act with approval.
   - The production switch escalates along §7.1 to the Accountable human,
     `@pat:ops`. Delegated accountability returns to the person who
     delegated it.
3. **The Accountable human doesn't answer either.** §12.7 applies. The scope
   is stewardship-suspended (§12.7.4): reads and Soft writes continue, and
   Hard and Destructive actions queue. The switch waits, and the §12.7
   nomination search starts.
4. **At every step.** Resolvers are named by people, rungs only go down,
   and any in-scope person who returns can stop or revert anything at once
   (§7.3, §9.2). When `@ravi:ops` comes back at 02:40, he works the queue.
   Everything that ran was already certified to run.

### 20.12 A goal is the long horizon; a loop re-reads it

A short horizon is one action. §20.4 decides it. A long horizon is not a
higher rung and not a second evaluator. It is a **goal** a person states,
and, when the work should continue without the person watching, a **loop**
that wakes and re-reads that goal.

The human surfaces are `/goal` and `/loop`. Specs and schemas use *goal*
and *loop*. A harness that offers the same two operations under other
spellings still conforms if the rules below hold.

- **`/goal` opens or replaces the mission.** The record names the
  Accountable human, what done means, the decisions only a person may
  make, and the stop conditions. Opening or replacing a goal is an
  action and binds to the seat that covers it. It MUST NOT raise any
  rung and MUST NOT be recorded as certification.
- **A goal without a loop is standing intent.** A person drives the next
  step. The goal is context, not permission.
- **`/loop` arms a wake.** The wake is a timer or a signal the person
  named (a check finishing, a review arriving). Each wake is a new turn.
  The runtime MUST re-read the goal at the start of the turn and MUST
  NOT carry a grant from the previous turn that the goal does not still
  cover.
- **A loop with no goal is refused.** There is nothing to re-read, so
  there is nothing the loop is allowed to continue.
- **Each step inside a wake binds on its own** (§20.2). It runs at that
  seat's rung, capped by the mission ceiling. A loop MUST NOT be how a
  seat reaches rung 4, and a rung-4 seat MUST NOT keep a loop running
  past a stop condition.
- **The loop ends, and the next step waits,** when any of these is true:
  the goal's definition of done is met; a stop condition is met; the
  next increment is a decision the goal says only a person may make; the
  goal was replaced or cleared. Ending the loop is not a demotion. The
  seat's rungs stay where certification left them.
- **State lives in the goal, not in the loop.** A wake that cannot read
  the goal stops. The loop does not remember an authority the goal
  record does not state.
- **Pacing does not widen authority.** One increment per wake. A wake
  scheduled on a fixed tick when the goal is waiting on a person, or
  when nothing it covers can proceed, MUST stop rather than invent the
  next step.

*Reference: a goal document states done and the decisions reserved to a
person; a loop statement is the resumable driver, with invariants and
stop conditions, and it does not keep authority of its own.*

---

## 20. Chains of hand-offs (normative)

*Added 2026-10-31 by [ADR-231](../adrs/adr-231-a-chain-of-hand-offs-has-executor-semantics.md).*

A **chain** is the sequence of capability invocations one request sets off as
agents hand work to each other. A conforming runtime treats a chain as an
executor treats a task: it MUST be able to end it, see it, order it under
contention, and clean up after it. The mechanism is the **baton**, a durable
per-chain record the dispatcher consults before every hand-off.

### 20.1 Dispatch never blocks

Before a hand-off the runtime MUST either run the call or refuse it at once
with a reason. It MUST NOT block a chain waiting for another chain or for a
resource. A refusal is one of two kinds: **ended** (the chain is over) or
**deferred** (try again later). This is what makes deadlock impossible: no
chain holds anything while it waits.

### 20.2 Termination without a hop limit

A runtime MUST NOT end a chain because of how many hand-offs it has made. It
MUST end a chain when, and only when, one of these holds:

1. it was **stopped** (20.5);
2. it passed its **deadline**: set by the originator, otherwise a configured default;
3. it is **orphaned** (20.6);
4. it made **no progress**: the same capability with the same arguments returned the same result twice in a row;
5. it hit the **identical-call backstop**: the same capability with the same arguments started a configured number of times (default 3) without a result to compare.

### 20.3 Priority and admission under pressure

Every chain has a priority: `critical`, `high`, `normal` or `background`,
defaulting by the surface it started from and changeable while it runs. The
runtime's resource steward (Axiom: KEEL) publishes a verdict: none, `pressure`
or `critical`. Under `pressure`, `background` chains' writes are deferred and at
most N chains keep writing, chosen by a declared strategy (`priority`,
`finish-first` or `interactive-first`). Under `critical`, only `critical`
chains write. Reads always run. Writes that are the way out of pressure
(alerts, chain stops, the steward's own upkeep) MUST NOT be deferred, and at
least one non-background chain MUST be admitted under `pressure`.

### 20.4 Starvation

A deferred chain's effective priority MUST rise with time waiting (Axiom: one
class per 30 minutes, never into `critical`), so no chain waits forever behind
others. The deadline bounds the wait in any case.

### 20.5 Cancellation

The runtime MUST offer a way for a person or an agent to stop a chain. A stop
is cooperative: the chain's next hand-off is refused, so it ends between
steps, never in the middle of one. Who stopped it and why are recorded.

### 20.6 Leases, orphans and structured concurrency

The baton is a **lease** held by the process running the chain's current step,
renewed by each hand-off and by long steps. A chain whose lease lapses is
**orphaned**. A step that starts a separate job MUST do so as a **child chain**
naming its parent; a stopped, ended or orphaned parent ends its children at
their next hand-off. The lease (minutes, renewable) and the deadline (hours,
set once) are distinct: the first says the holder is alive, the second that
the work must be finished.

### 20.7 Observability

The runtime MUST list the chains in flight with, at least: id, status,
priority, age, deadline, hand-offs so far, last capability, holder, lease
expiry and parent. Listing MUST first mark any orphans. Axiom:
`agents.chains`, `agents.chain_stop`, `agents.chain_priority`.

### 20.8 Not yet normative

Per-chain resource quotas, non-blocking joins over child chains, and per-agent
rate limits are named in ADR-231 as the next additions.

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
| `spec-embodied-surfaces.md` | The actuation chain (`Intent` → `AuthorityRegistry` → `EnvelopeCheck` + `Interlocks` → `ActuatorWriter`). §20.7 decides, per action, through the seat that covers it, whether an `Intent` may enter without a person's approval. §20.7 never modifies the chain. |

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

### C.8 One graduated-autonomy system: a seat registry, per-action binding, and actuation inside it

**Date:** 2026-10-10
**What:** §20 and §2.15 merge RACI, trust levels, the autonomy dial and the
autonomy gates into one system.
- **Roles.** Accountable is always a human; Responsible is the agent taking
  the action.
- **Seats.** Seats are kept in a registry. Each holds a certified rung per
  capability, a ceiling and a seat resolver (a person, or a named group with
  a named leader).
- **Binding.** An agent binds to the seat that covers each action, at the
  moment it acts.
- **Extensions.** Extensions are packaging only; seats live in the
  registry, and an extension is never a seat (§20.2.1).
- **Rungs and evaluation.** There are five rungs. Promotion is by
  certification and demotion is immediate. One deterministic evaluator
  computes min(ceiling, certified rung, dial).
- **Actuation.** Real-world actuation is governed like any other action,
  reached through certification under a declared ceiling.

**Why-not-otherwise:**
- If the mechanisms stay separate, they give two answers to "can this agent
  act?". A dial can be raised while the ownership map still says *approve*.
  "Responsible" can drift across a handoff, and three ladders end up sharing
  level names.
- Tying autonomy to an agent or an extension certifies it for work outside
  its record. One extension's agents do unlike things, and one kind of work
  spans several extensions. Packaging is the wrong unit of trust.
- A blanket "agents never actuate" rule leaves only unsafe paths to
  control, so control moves to unaudited channels. Bounding actuation by
  certification, a declared ceiling, and the unchanged interlock chain is
  the safer posture.

**Evidence:** The reference implementation has three overlapping
vocabularies: trust levels, an autonomy dial whose open question asks
whether trust "collapses into the dial", and an earlier state machine. Its
evaluator keys on action categories alone. Its
embodied-surfaces spec already gates `Intent` submission by RACI tier and
leaves interlocks untouched.
**Terms resolved (one meaning each; see §2.16):**
- *Resolver*: a person or named group. The deterministic function is the
  *evaluator*.
- *Capability*: a function an action exercises. Distinct from *capability
  kind* (§4) and from a *provided component*.
- *Attestation*: cryptographic or human, always qualified. §20 means human.
- *Steward*: the agent class (§3.3) versus the human designated or cohort
  steward (§7.1, §12). Disambiguated in the glossary; existing names stay.
- *Seat*: the AEOS seat versus a "tracker seat" in Axiom's ADR-166, which is
  an account.
- *Consulted*: a party the seat names is asked first. Only `approve` needs
  the resolver's signature.
- *Starting rung*: a signed first certification, at rung 3 or below.
- *Actor*: whoever acts (an agent or a human). Distinct from *principal*
  and from *operating identity*.
- *Envelope*: only the physical operating envelope. A seat's range is its
  *certified scope*.
- *Registry*: the seat registry, distinct from §12.7.4's registry of
  stewardless scopes.

**Operating identity (§20.2.2):** accountability attaches to an agent's
version plus anything that could increase or decrease the trust someone
depending on it would want to give it. That set is open-ended; any
examples are illustrative only. Trust is earned per identity, not per
version. Any change that could materially move that trust creates a new
identity, capped at rung 3 with lineage evidence, rather than flagging the
old one. That way receipts always name what actually acted, and demotion is
immediate by construction.
**Worked examples (§20.11, non-normative):** ATC only. A second metaphor
was considered and declined because ATC already has a literal form of
every case.

**Also:** seats survive changes of resolver, leader and agent (§20.10).
§6 sibling validation, §10 due process and §7.3 override are composed into
the evaluator's pass (§20.4).

---

## Appendix D — Future Exploration: Elections, Voting, and Collective Decision Procedures

**Status:** Not specified in 1.0; placeholder for future revision.

The 1.0 specification scatters quorum and threshold concepts across several
sections — §6.3 (trust-graph composition for sibling validation),
§7.5 (quorum approval for accord amendments), §13 (quorum for AEOS-itself
amendments), §8.4 (forced exit may require steward agreement). These
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
- Adoption of stewardless scopes (§12.7) when multiple legitimate
  candidates emerge

**Who votes?**

- Accountable humans: presumed default
- Designated stewards: when delegated explicitly per §7.1
- Service agents: only within pre-approved policy envelopes (§7.1, step 3;
  in §20 terms, a seat certified at rung 4)
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

_Draft. §§1–7 authored 2026-05-02. §§8–19 + appendices remain skeletal pending the next drafting round; Appendix D added as future-exploration placeholder. §2.15, §2.16 and §20 drafted 2026-10-10._
