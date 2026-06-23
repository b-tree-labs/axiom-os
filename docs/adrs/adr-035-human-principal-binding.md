# ADR-035: Human-Principal Binding — Every AI Action Accountable to a Named Human

**Status:** Proposed (2026-04-27)
**Supersedes:** none (extends ADR-026 ownership; complements ADR-033 layered memory + ADR-034 plan/agent pipelines)
**Related:** ADR-026 (ownership model), ADR-027 (federated memory), ADR-028 (trust graph), ADR-033 (layered memory), ADR-034 (plan/agent pipelines), `spec-memory.md` (provenance contract), `spec-federation-policy.md` (cross-cohort propagation), `spec-aeos-0.1.md` (extension manifests), `working/plan-agent-modes-analysis.md` §7.7, `working/memory-persistence-plan.md` (schema_version implications)

## Context

Agentic systems are converging on a future where AI work is unattributable mush. "ChatGPT did it." "The agent decided." "The model said so." Across the agent-harness market — Codex, Claude Code, Cursor, Devin — the load-bearing accountability surface ends at *the user account that initiated the session*: a single principal, locally scoped, often anonymized, frequently shared across teams. There is no architectural commitment that *every action carries a named human who stands behind it*.

For most consumer SaaS this is fine. For the cohorts Axiom serves — regulated research programs, classified analysis, education with academic-integrity expectations, government-aligned operators — it is not fine. These contexts *require* a chain of authority: a graduate student under a PI; an analyst under a program manager; an instructor under a department chair; an operator under a license. When AI work happens inside these contexts, the question "who is accountable for what this agent just did?" must have an unambiguous, audit-grade answer. Not in a session log. Not in a billing record. *In the architecture.*

Axiom today is partway there. `MemoryFragment.provenance.principal_id` records *who acted* — but if an agent acted, the principal *is* the agent. That was sufficient when agents were simple tool wrappers acting under an obvious local user. As Axiom adds plan + agent modes (ADR-034), federation handoff (ADR-027/Stage 5a), classification-aware tool runtime (ADR-032 / spec-aeos), and pedagogical agents (Classroom CHALKE), the chain from human → delegation → agent → action → cross-cohort effect grows long enough that "the principal that acted" stops answering "who is accountable."

The end-to-end design study `working/plan-agent-modes-analysis.md` §7.7 elevated this from an open question to a load-bearing differentiator. The decision recorded here makes that elevation architectural.

## Decision

Every memorable action in Axiom is bound to **two principals, both mandatory, both audit-visible, both propagated across federation:**

1. **The actor (`principal_id`)** — who did the work. May be a human; may be an agent (AXI, SCAN, CURIO, CHALKE, an extension agent, a peer-cohort agent). Already exists in `MemoryFragment.provenance`. No semantic change.
2. **The accountable human (`accountable_human_id`)** — the human whose authority the actor invokes. **New, mandatory, never null.** When a human acts directly, `accountable_human_id == principal_id`. When an agent acts, this field names the human who initiated the chain, or to whom delegation was explicitly recorded.

```python
@dataclass(frozen=True)
class Provenance:
    timestamp: datetime
    principal_id: str                        # actor (existing)
    agents: Sequence[str]                    # existing
    resources: Sequence[str]                 # existing
    accountable_human_id: str                # NEW — mandatory; never empty
    delegation_chain: Sequence[str] = ()     # NEW — principals between human → actor
```

### D1 — The accountable human is mandatory

`CompositionService.write` rejects any fragment whose `accountable_human_id` is unset, empty, or fails to resolve to a known human principal. The check fires at write time, not on read. A fragment without an accountable human is a corrupt fragment.

For interactive sessions: the accountable human is the authenticated session user. For agent-initiated chains: the accountable human is inherited from the originating fragment (the user message that started the conversation, or the explicit delegation that authorized the agent to act).

### D2 — The delegation chain records the path

When agent A acts under human H, and agent A spawns agent B (a sub-agent or a peer-cohort agent via A2A), the resulting fragment carries:

- `principal_id = B`
- `accountable_human_id = H`
- `delegation_chain = [H, A, B]`

The chain is informational + auditable. Any principal in the chain can be queried for "everything that happened under my delegation." Federation handoffs append to the chain at gateway projection time; the receiving cohort's local agent is the new actor, but `H` and the prior chain are preserved.

### D3 — Cross-cohort propagation: federation never strips the accountable human

When a fragment (or projection) crosses a federation boundary per ADR-027 / Stage 5a:

- `accountable_human_id` is preserved as-is. The receiving cohort sees the originating human.
- `delegation_chain` is preserved + appended-to as the receiving cohort's local agent acts on the projection.
- The federation gateway *cannot* drop or rename the accountable human. A peer that requests projection without honoring this field is rejected at gateway with audit.

This means: when an Austin-cohort agent acts on a sub-plan delegated from Prague, the resulting work is forever attributable to the Prague student who initiated it — even though the actor is an Austin agent.

### D4 — Retraction (per spec-memory §3.6) is anchored on the accountable human

A human's retraction request applies to fragments where they are the `accountable_human_id`, regardless of who the actor was. A student can retract control over derivations made under their accountability by an agent, by a sub-agent, or by a federated peer agent. Cross-principal retraction (one human asking another's data be forgotten) is still rejected per existing semantics.

The audit trail is preserved; only forward derivation is stopped. This is the architectural grounding for what spec-memory §3.6 already commits to.

### D5 — Plan-level + run-level binding

Per ADR-034:

- `Plan.accountable_human` is mandatory. Every step inherits the plan-level binding unless explicitly delegated to a different human (with that human's signature in the delegation chain at the step).
- `AgentRun.accountable_human` is mandatory. Every event fragment in the run carries it. Cross-cohort A2A handoff requires the receiving cohort to record the originating human, even if their local agent is the actor.

Without this commitment, ADR-034's federation primitives (peer agent collaboration; Phase 4) are merely performant; with this commitment, they are accountable.

### D6 — Surfacing in UX

The accountable human is *visible everywhere the actor is visible*. Concretely:

- Every agent message in a chat surface shows: `agent: AXI (acting under: ben:example-org)`.
- Every plan view shows: `accountable: <human>` in the header; per-step delegation chains shown when they diverge.
- Every audit log entry shows the actor + accountable + delegation chain.
- Every projection that lists fragments shows accountable-human as a default column.
- `axi me accountability [--principal=<human>]` returns "everything done under this human's accountability" across actors, agents, and federated peers.

This is not optional UI — it is part of the contract. Invisible accountability is unenforceable accountability.

### D7 — Schema-version bump

Adding `accountable_human_id` + `delegation_chain` to provenance is a schema change per `working/memory-persistence-plan.md` §4 (rename / add-required field). Per the policy table this is a **bump**.

Migration handling:

- Pre-bump fragments (no `accountable_human_id`) decode under the prior decoder; the field defaults to `legacy:unattributed` for read-back compatibility, and the fragment is flagged in the audit projection.
- New writes (post-bump) require the field; CompositionService rejects writes without it.
- A migration helper `axi memory migrate <scope_id> --backfill-accountable-human` walks pre-bump fragments and assigns accountable humans where the chain can be unambiguously inferred (e.g., a single user account in the scope's history). Where ambiguous, fragments stay flagged + reviewable.

For Prague's go-live in early June 2026, this ADR ships before any cohort fragment is written without the field, so pre-bump fragments are a documentation concern rather than a Prague concern.

### D8 — Built-in agent contracts

Every built-in agent extension (AXI, SCAN, CURIO, PRESS, TIDY, TRIAGE, CHALKE, WARDEN, future) updates its write paths to populate `accountable_human_id` from the initiating context. The AEOS extension manifest (per `spec-aeos-0.1.md`) gains a required `accountability_policy` field naming how the extension resolves the accountable human (typically: "inherits from initiating fragment" — the default; explicit override only with rationale).

### D9 — Extensions outside the trust boundary cannot bypass

Third-party extensions installed via Vyzier or external sources cannot write through CompositionService without honoring this contract. The CompositionService check is the enforcement point; AEOS conformance gates extension publication on it.

## Consequences

### Positive

- **The differentiator most peer harnesses cannot replicate without rebuilding.** Codex, Claude Code, Cursor, Devin would each require a session-model redesign + a federation story they don't have to deliver this. We deliver it as a primitive.
- **Regulated, classified, educational cohorts get audit-grade accountability natively.** No ad-hoc audit middleware; no after-the-fact log scraping; the chain is in every fragment.
- **Retraction (spec-memory §3.6) becomes meaningful.** "I retract my accountability" is a coherent operation when the binding exists.
- **Federation handoff preserves human attribution.** The Prague student → Austin agent example becomes possible without losing who is accountable.
- **The user-visible accountability surface is a marketing-defensible claim.** "Every action in Axiom is accountable to a named human, by architecture." Codex cannot say this.
- **Aligns with Axiom's identity convention** (`@name:context` Matrix-style principals per project memory) and ADR-026 ownership model — the human-principal binding is the load-bearing seam those layers were already pointing toward.

### Negative / costs

- **Schema-version bump.** Documented in §D7. Migration helper required; cohort fixtures need accountable-human in the v2 fixture variant per memory-persistence-plan §6.
- **Every CompositionService write path updated.** Every place a fragment is constructed needs the accountable-human plumbed through. Mechanical sweep but real cost.
- **Every built-in agent extension updated.** AXI, SCAN, CURIO, PRESS, TIDY, TRIAGE, CHALKE. Each needs an `accountability_policy` declared in its AEOS manifest + a write-path audit. Several PRs.
- **UX surface expands.** Every agent message + plan view + audit log shows accountable-human. Surface is small per place but ubiquitous.
- **Extension-author burden.** Third-party extension authors must understand the binding. Mitigated by AEOS conformance + scaffolded `axi ext init`.

### Risks + mitigations

| Risk | Mitigation |
|---|---|
| Accountable-human resolves wrong in deeply chained agent flows (e.g., a CURIO loop spawning a sub-agent four levels deep) | Default = inherit from initiating fragment; explicit override required with rationale recorded; audit projection surfaces deep chains for review. |
| Federation peer drops or strips the field | Gateway rejects projections without it; trust profile downgraded for repeat offenders per ADR-028. |
| Pre-bump migration mis-assigns accountability | `axi memory migrate --dry-run` shows assignments before writing; ambiguous cases left flagged rather than guessed; reviewer signs off. |
| Agents acting "on behalf of" a human who didn't authorize the specific action | Delegation chain is informational, not authorization — the *write-time* check is that the accountable human is a real principal in the scope. Authorization is a separate concern (RACI / TrustProfile, per ADR-034 §D6). |
| UX clutter from accountable-human surfaces | Compact display formats (`@ben:example-org` collapses to `ben`); show full chain only on hover / expansion. |

## Compliance gates introduced

- `pytest -m accountability_compliance` (new marker):
  - Every fragment has a non-empty `accountable_human_id`.
  - Every plan + agent run has a resolvable accountable human.
  - Federation projection round-trip preserves the accountable human + appends delegation chain correctly.
  - Retraction request scoped on accountable human stops forward derivation but preserves audit history.
  - Pre-bump migration helper produces auditable, reviewable assignments.

These join `memory_compliance` (`working/memory-persistence-plan.md` §6) and `pipeline_compliance` (ADR-034) as release gates.

## Phasing

- **Now (Phase 0 of analysis doc):** This ADR + schema-version bump in MemoryFragment + CompositionService write-time check + built-in agent extensions updated + AEOS manifest amendment + `axi memory migrate --backfill-accountable-human`.
- **Phase 1 (plan mode MVP):** Plans require accountable human; CLI surfaces it.
- **Phase 2 (agent mode MVP):** Runs require it; UX surfaces it on every agent message; `axi me accountability` projection ships.
- **Phase 4 (federation handoff):** Gateway preservation + delegation-chain append at projection time.

This ADR is a Phase 0 ratification — the sooner it lands, the less migration work it implies (because Prague writes are still pre-cohort).

## Open items

- **Authorization vs. accountability.** This ADR establishes accountability (who stands behind it). Authorization (was this action permitted under their authority?) is a separate concern carried in `TrustProfile` + per-step `gate` (ADR-034 §D6). The two specs cross-reference.
- **Service / system principals.** Some writes are genuinely systemic (TIDY sweep, schema migration). The convention: `@system:axiom-platform` is the accountable human; the platform operator's named principal is in the delegation chain. Spec amendment to `spec-memory.md` to formalize.
- **External-agent provenance.** Agents from external systems (e.g., a federated peer cohort whose agent identity is opaque to us) project work into our cohort. The accountable human is preserved per §D3, but the *actor* may be an opaque peer-agent ID. Audit projection flags these for cohort-coordinator review.

## The bottom line

Codex's accountability ends at an OpenAI account. Claude Code's at a local machine user. Neither propagates the accountable human across federation, classification, or extension boundaries — because neither has those boundaries to begin with. Axiom does. This ADR makes accountability load-bearing in the architecture, not aspirational in the marketing. *No AI action exists without a named human standing behind it* — by construction, not by promise.
