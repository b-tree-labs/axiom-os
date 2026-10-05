<div class="formal-title-block">

# AEOS: A Foundational Standard for Agent-Bearing Platforms

## A White Paper Companion to AEOS 1.0 — The Agent Ecosystem Open Standard

**Benjamin Booth**

*Contributing Organizations*

- B-Tree Labs
- The University of Texas at Austin

*Drafted: 2026-05-02*

*Companion specification:* [`spec-aeos-1.0.md`](../specs/spec-aeos-1.0.md)

*Audience:* Harness evaluators, extension developers, the agent-curious, adopters of Axiom or any library built upon it. And colleagues asking "what is this thing, why does it exist, and how does it fit?"

</div>

---

## Abstract

The agent-platform standards landscape covers a great deal of useful ground: MCP specifies tool exposure, A2A specifies agent-to-agent messaging, OpenAPI specifies REST surfaces, MCPB specifies tool bundles, SKILL.md specifies model-mediated skills, OASF schematizes capability metadata, AGENTS.md guides coding agents, Sigstore signs releases. Each addresses a real concern. Together they leave a foundational layer unaddressed: how the *platform itself* should be organized, governed, and federated so the agents living within it have predictable boundaries, visible provenance, and durable user accountability.

The Agent Ecosystem Open Standard (AEOS) fills that layer. AEOS 0.1 was a packaging and declaration standard — manifest format, capability kinds, signed-release expectations. AEOS 1.0 carries the 0.1 surface forward and adds the foundational content the work between 0.1 and 1.0 surfaced as necessary: enumerated agent classes, layer authority, checks and balances, conflict-resolution authority, user rights, due process, equal protection, succession. AEOS does not replace the existing standards; it composes them and adds the substrate they sit on top of. This paper explains what AEOS is, what it is not, where it fits in the standards landscape, and what we deliberately deferred to a future revision.

---

## 1. The Problem

A modern agent-bearing platform — one that hosts multiple agents, may federate with peer platforms, and serves users whose data and decisions matter — confronts five operational concerns that no single existing standard addresses end-to-end:

1. **Packaging and declaration.** How is an agent or extension shaped? How does the runtime discover it? How are its capabilities described? *(MCPB, MCP, SKILL.md cover slices.)*
2. **Identity and signing.** How does an extension prove its authenticity at install? How does the platform know what it's running? *(Sigstore covers slices.)*
3. **Class enumeration and behavior contracts.** What kinds of agents exist? What does each kind do, and what is it not allowed to do? Without an enumerated set of classes, every agent system reinvents the categorization, and federation between two systems requires a class-mapping table for every pair.
4. **Governance and conflict resolution.** When two agents have overlapping authority, who wins? When a human disagrees with an automated decision, what's their recourse? Without a foundational answer, every platform improvises — and the improvisations don't compose across federation boundaries.
5. **User rights as a first-class architectural concern.** Sovereignty, override, knowability, portability, redress, leave. These are foundational protections for the humans on whose behalf the platform acts. They are routinely treated as policy concerns; they belong in the architecture.

The first two are well-served by composing existing standards. The next three are not. AEOS exists to address them.

The positioning is **industrial and commercial** — labs, classrooms, factories, plants, military deployments, company organizations, devops teams, content pipelines, and similar settings where strong security and federation across cohorts are non-negotiable. Off-the-shelf agentic platforms typically force a choice between hosted convenience (cede sovereignty) and on-premise isolation (lose collaboration). AEOS refuses that tradeoff: federation is a first-class concern, sovereignty is the default posture, and accountability is encoded as architecture.

---

## 2. The Approach

AEOS 1.0 organizes its content into nineteen normative sections plus four appendices. The substantive shape rests on five interlocking commitments.

### 2.1 Twelve principles, derived not invented

The principles in §2 of the specification carry forward seven from AEOS 0.1 (self-containment, purpose-driven naming, compound-by-default layout, deterministic trust boundary, capability declaration via entry points, signed releases, federation-native participation) and add five earned in the work between 0.1 and 1.0:

- **Engineered gravity, not enforced parity.** The platform shapes surfaces so the right path is the path of least resistance. It does not pretend to enforcement it does not have. Honest about limits in heterogeneous environments where some participants will be observed-only.
- **Pattern reuse over invention.** Affordances pick existing structural templates; novelty is an explicit decision.
- **Tier governs presence, not power.** Surfacing rules gate automatic visibility; they never gate available invocation.
- **Provenance is universal.** Every action carries a `via=` field identifying its source; the audit trail is the architecture's evidence handle.
- **Sovereignty defaults down.** Powers not delegated upward are reserved to the lower layer; user sovereignty is the default.

These are not aspirations. They are constraints the rest of the specification respects and, where it does not, calls itself out for.

### 2.2 Ten agent classes

§3 enumerates ten canonical agent classes: **Orchestrator** (coordinate, route, dispatch), **Generator** (produce, synthesize, transform), **Steward** (maintain, sweep, observe resources), **Sensor** (observe, detect, extract), **Reviewer** (inspect, validate, gate), **Governor** (set policy, propose accords), **Federator** (peer, bridge cohorts), **Attester** (verify, sign, vouch), **Shepherd** (build, tag, ship, watch), **Combatant** (contest, defend, oppose).

Each class describes a *capability shape* — the verb the agent principally embodies — not exclusive ownership. Agents may span multiple classes when their behavior matches multiple verb shapes; classes may have multiple canonical agents. The reference implementation contains several deliberately multi-class agents (a federation-and-governance agent that is both Federator and Governor; a hygiene agent that is both Steward and Sensor in its different operating modes). The class system describes what an agent does, not who owns the verb.

### 2.3 Four layers of authority

§5 specifies four authority layers — platform / cohort / extension / user — with explicit "owns / cannot override" boundaries. Powers not explicitly delegated upward are reserved to the lower layer. The default authority for any unspecified situation is user sovereignty. This is the architectural analogue of subsidiarity: keep authority as close to the affected party as the work allows.

### 2.4 Conflict-resolution authority

§7 codifies five clauses governing how agents, accords, and layer authorities resolve conflicts:

1. **Authority hierarchy** — accountable human, designated steward, service agent (within pre-approved policy), cohort steward. Three of four are humans; the single agent step acts only within human-signed delegation.
2. **Default behavior in unspecified situations** — yield, record the gap, escalate.
3. **Human-override universality, scoped** — within the human's accountability, the override is unblockable; outside, it does not extend.
4. **Provenance contract** — what the platform records on every action.
5. **Amendment process** — proposed by Governor, public-noticed, quorum-approved, ratified.

The model is honest about the controversial case (§7.1.1: agent authority over a human in narrow, scoped scenarios such as attestation lockout, duress safe-state, regulatory automated halt) and surrounds it with mandatory redress provisions.

### 2.5 Six user rights

§9 enumerates six rights as architectural guarantees, each with a definition, the mechanisms that uphold it, and the signals that indicate violation: **sovereignty** (control over data, decisions, and delegations), **override** (within scope, unblockable), **know** (provenance access), **portability** (export in a portable format), **redress** (appeal mechanism), **leave** (no lock-in).

These are foundational because the platform's posture toward its users is a load-bearing decision. Treating them as policy concerns lets implementations vary inconsistently; treating them as architecture forces every conformant implementation to provide them.

The §13.3 amendment-base-case states one of AEOS 1.0's two absolutes: the right of override and the right of leave **cannot be removed by amendment, regardless of quorum**. Without this guarantee, the amendment process could remove the safety net that makes the rest of the architecture tolerable.

---

## 3. Related Work

AEOS does not replace any existing standard. It composes them and adds the foundational layer they do not address.

### 3.1 Tool exposure and bundling

**MCP (Model Context Protocol).** Specifies how an LLM-bearing client discovers and invokes tools published by a server, plus prompt templates and resources. AEOS extensions whose only declared capability is tool exposure are valid MCP servers; AEOS adds the rest of the extension structure (manifest, signing, governance, rights conformance) around the MCP-typed tools.

**MCPB (MCP Bundle).** A packaging format for MCP-only extensions. An AEOS extension whose only `kind=...` provides block is `kind=tool` is a valid MCPB archive with additional AEOS metadata. MCPB-only consumers ignore the extra fields.

**OpenAPI 3.1.** REST/HTTP surface description. AEOS adapter capabilities (`kind=adapter`) wrapping REST integrations declare their surface via embedded OpenAPI specs. AEOS does not redescribe what OpenAPI already specifies.

### 3.2 Agent-to-agent communication

**A2A.** Cross-agent messaging protocol. AEOS agents expose A2A Agent Cards at `/.well-known/agent-card.json` when running in network-reachable mode. Cross-cohort traffic carrying the negotiation in agent-coordination protocols (a future operational spec) rides A2A.

### 3.3 Skills and coding-agent guidance

**SKILL.md / agentskills.io.** Format for model-mediated instructions that an LLM can follow. AEOS skill capabilities (`kind=skill`) use SKILL.md format verbatim. An extension's `skills/<name>/SKILL.md` files are valid standalone skills.

**AGENTS.md.** Guidance documents for coding agents operating on a repository. AEOS extensions at the repository level provide AGENTS.md files. AEOS adds nothing to AGENTS.md's content shape; it just specifies that an extension SHOULD have one.

### 3.4 Metadata schemas

**OASF (Open Agentic Schema Framework).** Schematizes capability and metadata for agentic systems. AEOS manifest fields are compatible with OASF where they overlap. Where AEOS adds non-overlapping fields (rights conformance, agent class memberships), the AEOS schema documents them; future contributions to OASF may upstream these where appropriate.

### 3.5 Signing, identity, attestation

**Sigstore / PEP 740.** Keyless OIDC signing for releases. AEOS extensions sign with Sigstore. AEOS 1.0 expands the signed material beyond the artifact to include rights conformance and agent class membership declarations, so the signed release carries the foundational claims those declarations make.

### 3.6 What AEOS adds

What AEOS adds, and where the existing standards do not reach:

- **Enumerated agent classes with verb-shape definitions** — neither MCP, A2A, nor OASF specifies what kinds of agents exist or what each kind does
- **Layer authority and conflict-resolution authority** — none of the existing standards address governance
- **User rights as architectural guarantees** — typically a policy concern in adjacent standards; AEOS makes them required
- **Provenance discipline as universal contract** — bits and pieces appear in OpenAPI's audit conventions and Sigstore's transparency logs, but no unified contract
- **Succession and continuity for stewardless scopes** — no parallel in existing standards; the closest analogues are organizational governance frameworks adapted from human institutions

The composition is deliberate. AEOS would not be improved by reinventing what MCP/A2A/Sigstore/etc. already specify well; it would be incomplete without adding what they do not specify at all.

---

## 4. Lineage and Naming

The 0.1 → 1.0 pattern is not unique to AEOS. Foundational standards commonly start as packaging or declaration formats and grow into full architectural specifications as the surrounding ecosystem matures. POSIX evolved from a system-call interface into a foundational operating-system specification covering processes, signals, file systems, and concurrency. W3C's HTML grew from a markup language into a web-platform specification covering documents, scripts, security, accessibility, and storage. Kubernetes consolidated container orchestration into a foundational platform specification covering scheduling, networking, security, and storage. The pattern is consistent: a focused 0.1 establishes credibility; a 1.0 absorbs the surrounding concerns the focused version was insufficient to address alone.

The acronym **AEOS** is preserved across the version transition. The expanded name changes:

- **0.1** — *Agent **Extension** Open Standard*. Apt when the spec was a packaging and declaration format.
- **1.0** — *Agent **Ecosystem** Open Standard*. Apt when the spec covers governance, rights, agent classes, federation participation, and the foundational substrate the extensions live within.

The acronym continuity is intentional: every existing reference to "AEOS" remains valid; only the spelled-out meaning expands to fit the broadened scope. This is a design lesson AEOS itself encodes — preserve compatibility where the cost is low, even when the substantive change is significant.

---

## 5. What's Not in 1.0

AEOS 1.0 deliberately defers content that deserves its own design treatment rather than piecemeal embedding. Three substantial omissions are noted explicitly in the specification:

### 5.1 Operational protocols for agent conflict resolution

The five conflict-resolution clauses in §7 specify the *authority structure*. The wire shape of the negotiation between agents — message types, timeout semantics, accord-artifact schema, the four resolution patterns (yield, defer, coordinate, escalate), the six accord patterns (specialization split, pipeline, consultation, peer review, active-passive, weighted vote) — belongs in a sibling specification (`spec-agent-accord-protocol.md` in the reference implementation). AEOS 1.0 establishes the *that*; the operational spec establishes the *how*.

### 5.2 Elections, voting, and collective decision procedures

Quorum and threshold concepts appear scattered across §6.3, §7.5, §8.4, §13. These implicit voting patterns deserve unification into a formal Elections-and-Voting mechanism: who votes, what thresholds (simple majority, two-thirds, supermajority, unanimity), how votes are weighted (equal vs trust-graph-weighted vs reputation-bounded), procedural questions (time-bounded windows, eligibility verification, dispute resolution). Appendix D of the specification places this as a future-exploration item with the open structural question of whether it becomes a §6.X subsection, a top-level §20, or its own separate spec.

### 5.3 The harness-to-harness coordination problem

When agents in peer harnesses (Claude Code, Cursor, Codex, OpenCode, axi chat) operate on the same scope, their actions can collide — duplicate work, conflicting opinions, race conditions. The detection and resolution of these collisions composes with the conflict-resolution work above but adds harness-specific complications: external harnesses are not natively accord-aware, and the platform can only observe their actions (via cross-harness session mirroring) rather than enforce against them. The realistic posture is **engineered gravity, not enforced parity**: shape the MCP surface so the cooperating path is most attractive, accept that determined non-cooperation cannot be prevented, and rely on provenance discipline to make divergence detectable and human-correctable.

### 5.4 Three open user-rights questions

§9.7 explicitly acknowledges three rights not specified in 1.0:

- **Right to be forgotten** — tension with provenance integrity; likely composes with regulatory frameworks
- **Right to delegate** — explicit framing of delegation-as-a-right rather than default permission
- **Right to inherit** — what happens to a user's delegations and state upon disability or death

Each deserves design treatment beyond what 1.0 had bandwidth for.

### 5.5 Reference implementation work

Several reference-implementation concerns are out of scope for AEOS itself but inform its design: a WASM-backed extension loader (post-Prague-launch direction in the reference implementation), the CLI affordance catalog (eight UI patterns for surfacing capability), the cross-harness slash-command generator. These exist as adjacent specifications cited in Appendix B; AEOS does not depend on them.

---

## 6. Discussion

### 6.1 What we may have gotten wrong

The two absolutes in §7.3 (within-scope override unblockable) and §13.3 (override and leave cannot be amended away) are the most consequential decisions in the specification. If they are wrong — if some plausible scenario requires constraining them — the architecture's posture toward its users inverts. We chose to make them absolute because any softening invites the failure mode the rule prevents. We are aware this constrains future revisions; we judge the constraint worth the safety guarantee.

The ten-class enumeration is the second most consequential decision. A different decomposition — eight classes, twelve, or a fundamentally different organizing principle — would produce a substantially different specification. We chose ten because the verb shapes of agents in the reference implementation cluster cleanly into ten distinct categories, with multi-class membership handling the cases where an agent spans two. Other implementations may find different cluster structures more natural. The §3.11 multi-class clause is the safety valve that keeps the enumeration from being over-constrained.

### 6.2 What we deliberately punted

We did not specify Elections and Voting because the content deserves its own design conversation. We did not specify the operational accord protocol because it belongs in a sibling spec, not in AEOS. We did not specify the WASM extension loader because it is a reference-implementation concern. We did not address the three open user rights (forgotten, delegate, inherit) because each is a substantive design problem in its own right. These deferrals are listed in §5 of this paper and §§9.7, 13, and Appendix D of the specification so that a future reader can see exactly what was deliberately left unaddressed.

### 6.3 Honest framing about what AEOS can and cannot do

AEOS is a specification. It cannot itself ensure that conformant runtimes correctly implement what it specifies — that is the runtime's responsibility, and a conformance-test program (sketched in §18) is the verification path. AEOS cannot ensure that humans behave as the authority hierarchy presumes — bad actors can sign nominations, override in bad faith, refuse to ratify legitimate accords. AEOS provides redress mechanisms (§9.5) and the trust graph (§6.3) to surface bad-actor patterns over time, but it cannot prevent them in the moment.

In heterogeneous environments where some participants are non-AEOS-conformant, AEOS-conformant participants can only model their cooperation through the gravity mechanisms (Layers 1–5 of the cross-harness gravity work in `spec-axi-cli.md`). Determined non-cooperation cannot be prevented by a specification. The honest framing throughout AEOS — and throughout this paper — is that the specification engineers desirable behavior, surfaces undesirable behavior to humans, and trusts humans to do the corrective work the platform cannot do automatically.

### 6.4 Where AEOS sits relative to its publication strategy

AEOS 1.0 is being released as a **limited public preview** in parallel with the reference implementation's first production deployment. The dual-track posture remains: continue contributing to existing standards (MCP, A2A, OASF, MCPB, SKILL.md, AGENTS.md) where AEOS overlaps, while maintaining AEOS as the foundational layer that captures the federation-native, governance-native, rights-native concerns the public standards do not yet address. Full publication — broader outreach, formal call for implementations, conformance test program release — follows post-deployment validation. The preview window is intentional: surface critique from the audience this paper invites *before* the specification's first revision, so the 1.1 draft incorporates external feedback rather than only reference-implementation experience.

### 6.5 An invitation

If you are an extension developer: read §§4–6 (capability kinds, layer authority, checks and balances) and §15 (manifest format) of the specification. Most of what you need to author a conformant extension is there.

If you are a harness evaluator: read §§3 (agent classes), §17 (signing and attestation), §18 (testing and conformance) of the specification. The conformance levels (Bronze / Silver / Gold) are the practical handles for evaluating an implementation.

If you are agent-curious: §1 (preamble), §2 (twelve principles), §9 (user rights), and §11 (equal protection) of the specification carry the substantive philosophy.

If you are an Axiom adopter or a peer evaluating whether your library should build on AEOS: §§7 (conflict-resolution authority), §12 (succession), §19 (federation-native) plus this paper's §3 (related work) and §5 (what's not in 1.0) carry the substantive picture.

We welcome thoughtful disagreement, honest critique, and the kinds of edge cases the reference implementation has not yet encountered. The specification is intended to be amended (per §13). Your feedback is the natural input to that process.

---

## References

| Reference | Citation |
|---|---|
| MCP — Model Context Protocol | https://modelcontextprotocol.io |
| A2A — Agent-to-Agent Protocol | https://github.com/google/A2A |
| OASF — Open Agentic Schema Framework | https://github.com/oasf-project |
| MCPB — MCP Bundle | https://github.com/anthropics/mcpb |
| SKILL.md / agentskills.io | https://agentskills.io |
| AGENTS.md | https://agentsmd.dev |
| OpenAPI 3.1 | https://spec.openapis.org/oas/v3.1.0 |
| Sigstore | https://www.sigstore.dev |
| PEP 740 — Index support for digital attestations | https://peps.python.org/pep-0740 |
| SemVer 2.0.0 | https://semver.org |
| Keep a Changelog | https://keepachangelog.com |
| MADR — Markdown Architecture Decision Records | https://adr.github.io/madr/ |
| RFC 2119 — Key words for use in RFCs | https://www.rfc-editor.org/rfc/rfc2119 |
| Companion specification | [`docs/specs/spec-aeos-1.0.md`](../specs/spec-aeos-1.0.md) |
| Predecessor specification | [`docs/specs/spec-aeos-0.1.md`](../specs/spec-aeos-0.1.md) |

---

_This paper is a companion to AEOS 1.0. Authoritative normative content lives in the specification; this paper is positional and explanatory._
