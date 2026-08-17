# ADR-055: Unified Governance Fabric — Vault, Schedule, Notifications, Authz as Siblings Over the Existing Governance Bones

**Status:** Proposed (2026-05-30)
**Supersedes:** none
**Refines:** ADR-026 (ownership), ADR-028 (trust graph), ADR-035 (human-principal binding), ADR-045 (RACI graduation), spec-classification-boundary
**Builds on:** ADR-027 (federated memory), ADR-052 (database tenancy), ADR-012 (provider identity), ADR-038 (built-in MCP server)
**Related primitives (sibling tracking issues):** axiom-os#277 (axiom.schedule), axiom-os#278 (axiom.notifications), axiom-os#274 (host-unit lifecycle / `axi schedule install`), MCP server #268
**Specs:** `spec-governance-fabric.md` (the build-ready shared substrate this ADR commits to)
**Consumer PRDs:** `prd-axiom-authz.md`, `prd-axiom-vault.md`, `prd-axiom-notifications.md`, `prd-axiom-schedule.md`

---

## Context

Axiom has, over the last twelve months, accumulated a remarkable set of *governance bones*:

- **Ownership** (ADR-026) — single master + peer delegations + four rights (read / write / delete / delegate)
- **Trust graph** (ADR-028) — EigenTrust-shaped, optimistic defaults
- **Human-principal binding** (ADR-035) — `accountable_human_id` + `delegation_chain` mandatory on every fragment
- **RACI graduated autonomy** (ADR-043, ADR-045) — proposal → pre-approval → autonomous, per action class
- **Classification boundary** (`spec-classification-boundary.md`) — community / regulated / EC tiers; flows respect boundaries
- **Federation identity + visibility** (ADR-020/022/024/027) — multi-authority signatures, cohort registry, classification-gated outflow
- **Provenance** — `(T, U, A, R)` tuple immutable at write time; every fragment names its origin

What we *don't* have are the **everyday primitives every consumer extension needs to do useful work safely**:

| Primitive | What's missing | What ships today | Symptom |
|---|---|---|---|
| **Secret / key storage** | A managed vault with capability-token issuance, rotation, revocation | OS-keychain ad-hoc; raw env vars; per-extension reinventions | Every connector ships its own secret-handling, badly; agents see plaintext credentials they shouldn't |
| **Notifications** | Multi-channel outbound (inbox + push + Slack + Teams + email) with delivery receipts | SMTP module exists; nothing else | TIDY hygiene findings land in logs nobody reads; Expman has nowhere to ping Jim when a sample needs custody handoff; competitive-parity-gaps.md has been flagging this since 2026-04 |
| **App-level scheduler** | Time-based and event-based triggers, RACI-gated, memory-context-bound | RemoteTrigger gives cloud routines; manifest-declared `[[agent.cadence]]` doesn't exist | SLA timers don't fire; reminders aren't recurring; every consumer reinvents cron |
| **Unified authz site** | A single decision point every other primitive consults: *"may actor A do action X on resource R under classification Z?"* | Policy engine exists; every call site rolls its own checks | Authorization logic duplicated across the codebase; no audit trail of why an action was permitted |

The temptation is to build these four independently — four PRDs, four extensions, four release cadences. Every peer harness on the market has done that, and the result is the same in every case: secret-handling becomes the worst-shaped surface in the system; notifications get bolted to whichever channel the loudest user asked for first; scheduling lives in a corner of one tool, unaware of the rest; authz is a thousand ad-hoc `if user.is_admin` checks scattered across the source.

We don't have to make that mistake. The governance bones already exist. **Every action this platform takes — schedule a job, send a notification, fetch a secret, fire a tool, write a memory fragment, route a query across federation — is the same shape:**

```
ACTOR  acts under  TRUST  on  CONTENT-of-CLASSIFICATION
              ↓
       produces a provenance-stamped receipt
              ↓
       respects federation visibility
              ↓
       graduates autonomy per RACI
```

If the four primitives are built **as siblings consuming one shared envelope** — instead of as silos that reinvent the envelope four different ways — the platform gets a property no peer harness has and few are positioned to build: **a unified governance fabric where one rule about trust, classification, ownership, or RACI applies everywhere, automatically.**

That's what this ADR commits to.

---

## Decision

### D1 — The four primitives are **siblings**, not silos

`axiom.authz`, `axiom.vault`, `axiom.notifications`, `axiom.schedule` are four AEOS-conformant built-in extensions that share one substrate (the action envelope, see D2) and consume the same governance bones. They are sized, designed, and released as a coherent family. Each PRD references the shared Tech Spec; each ADR for an in-flight refinement of a sibling primitive references this one.

This is the same shape as the federation primitives (Vega): the parts read independently, but the architectural property is the coherence of the whole.

### D2 — One **action envelope** for every governed action

Every operation on the platform that crosses a trust, classification, or ownership boundary — and that's almost every operation — carries an envelope:

```python
@dataclass(frozen=True)
class ActionEnvelope:
    actor: Principal               # who's acting (Matrix-style @name:context)
    capability: CapabilityToken     # what they're authorized to do, scoped + time-limited
    classification: Classification  # the data-tier label of the resource being acted on
    context: CompositionContext     # the memory context this action runs under
    provenance_parent: ProvenanceRef  # which prior fragment caused this action
    federation_origin: PeerId | None  # if this action was forwarded by a federated peer
```

The envelope is the universal currency. `axiom.authz` decides whether the envelope is permitted. `axiom.vault` issues a `CapabilityToken` that fits the envelope's actor/classification. `axiom.notifications` and `axiom.schedule` both refuse to act without one.

This is not new layering on top of memory composition — it's making the *already-implicit* contract of CompositionService **explicit and uniform** so the four sibling primitives, and every downstream extension, consume it the same way.

### D3 — Capability tokens, not raw credentials

Every outbound action requiring authentication — a Slack send, a GitHub API call, an LLM provider request, a federated peer hop — goes through a **scoped, time-limited, revocable capability token** issued by `axiom.vault`. Agents do not see raw API keys. The token names exactly what action it permits, on what resource, until when, and is revocable independently of the underlying credential.

This is the property no peer harness has. Claude Code, Cursor, Aider, OpenAI Agents, LangGraph — every one of them gives the running agent direct access to your raw provider credentials. An agent that misbehaves can do anything that credential can do. Axiom's vault model means **misbehavior is contained to the capability that was vended**, and revocation is a one-line change that propagates across the trust graph.

### D4 — The connector shape is a first-class registered contract

Per the long-running observation in `docs/working/competitive-parity-gaps.md`:

> A proper connector shape = OAuth + token vault + rate-limit/retry + consistent MCP surface + provenance stamp. Shipping one-off integrations without this shape is what competitors do badly.

This ADR commits to making the connector shape an **AEOS-registered capability kind**: an extension declaring `[[extension.provides]] kind = "connector"` MUST conform to a manifest schema that names its OAuth flow, the vault binding for its tokens, the rate-limit policy, the MCP surface it exposes, and the provenance stamp it applies. The lint refuses to publish a connector that elides any of these.

The Tech Spec (`spec-governance-fabric.md`) defines the contract. Every Slack, GitHub, Google Drive, Canvas, Box, Notion, Anthropic, OpenAI adapter conforms. One shape, many integrations.

### D5 — Classification routing is enforced at every primitive boundary

A notification touching ITAR data refuses to fan-out to a non-cleared channel. A scheduled job touching EAR-restricted memory cannot be scheduled on a host outside the right tier. A capability token vended for an EC-tier action cannot be presented to a community-tier peer. A federated query carrying CUI fragments cannot route through a peer cohort whose `cohort_policy` lacks CUI handling.

This isn't four different checks — it's *one rule*, defined in `spec-classification-boundary.md`, consulted by `axiom.authz` and enforced at every action-envelope boundary. The boundary check happens *once*, returns a typed verdict, and the verdict itself becomes a provenance-stamped receipt fragment.

### D6 — Federation is native to all four primitives — not an extension

A schedule can be defined on cohort A's node, fire on cohort B's hardware under capability-bound authority granted by cohort B's WARDEN, produce a notification routed to cohort C's HERALD inbox, all with the trust-graph credibility and classification gates intact end-to-end. This is the property `prd-cross-surface-memory.md` and the federation ADRs gestured toward without ever getting to the verbs.

The four primitives make those verbs *concrete*. Peer-to-peer scheduling, cross-cohort notifications, federation-delegated capability tokens, multi-authority authz verdicts — all expressible in the action envelope, all auditable through provenance.

### D7 — RACI graduation governs autonomy at every primitive boundary

Per ADR-045: every action carries a graduated-autonomy classification — proposal, pre-approved, autonomous. The fabric makes RACI the **default disposition** for every primitive action:

- A scheduled job initially fires as a *proposal* the operator approves once; after N successful firings the operator graduates it to autonomous.
- A notification to a high-classification recipient initially proposes; after the recipient confirms reliability, it auto-sends.
- A vault grant initially requires the human's tap-to-approve; after the agent demonstrates discipline, the grant becomes autonomous for that action class.
- An authz verdict on a novel action initially returns *propose-to-human*; after the human's decision pattern stabilizes, the verdict goes autonomous.

Same one mechanism, applied uniformly. The user's experience: "I taught Axiom what I'm okay with; it now does that on its own and asks for the rest."

### D8 — Provenance-stamped receipts for **every** action

Every action through any of the four primitives produces a `receipt` memory fragment in the appropriate tier:

```
fact_kind: "action_receipt"
content:
  envelope: <the ActionEnvelope as JSON>
  verdict: "permitted" | "denied" | "deferred-to-human"
  outcome: "succeeded" | "failed" | "pending"
  effect_fragments: [<refs to fragments this action produced>]
```

The receipt is queryable, federable (per its classification), and the load-bearing artifact for the entire audit story. "Did Jim see the alert?" — query `notification_receipt`s for `recipient = @jim`. "Why did this scheduled job not fire?" — query `schedule_receipt`s for `dedupe_key = …`. "Who authorized this token?" — query `vault_receipt`s for `capability_id = …`. "Who approved this autonomous transition?" — query `authz_receipt`s. The audit trail isn't bolted on; it's the natural by-product of every action.

### D9 — The four primitives consume the data platform (ADR-049) for cross-extension reads

Per ADR-049, OLTP joins across extension schemas are forbidden; cross-extension reads ride the Bronze → Silver → Gold layers. The receipts from D8 are themselves a Bronze source: a `governance_fabric_silver` view aggregates receipts across primitives and surfaces them to dashboards, compliance reports, and the eventual `axi audit` CLI.

This means the audit-trail story is *one query path*, not four. Compliance gets a single pane of glass; we get cohesion.

### D10 — Five agents own operational responsibility

Per AEOS §3 agent class enumeration, the new operational footprint of this fabric:

| Agent | Class membership | Primitive |
|---|---|---|
| **GUARD** | Reviewer + Governor | `axiom.authz` — the unified decision point |
| **KEEP** | Steward + Governor | `axiom.vault` — token issuance + rotation + revocation |
| **HERALD** | Generator + Sensor | `axiom.notifications` — outbound delivery + inbound listening |
| **PULSE** | Orchestrator | `axiom.schedule` — cadence loop + RACI proposal flow |
| **WARDEN** (pre-existing — Vega) | Governor | Federation-side counterpart for all four — cross-cohort grants, visibility, admission |

GUARD, KEEP, HERALD, PULSE are net-new. WARDEN's scope expands as these primitives land.

---

## Consequences

**Positive**

- One mental model for the user, the extension developer, and the auditor. Trust, classification, ownership, RACI graduation, federation visibility are all expressed the same way regardless of whether the action is a notification, a scheduled job, a secret retrieval, or a tool invocation.
- Capability tokens contain misbehavior in a way every peer harness fails to. Compromised agent → revoke that one capability; underlying credentials untouched.
- Classification-aware everything makes Axiom credible for regulated, healthcare, and other high-tier domains without ad-hoc checks. One rule, enforced everywhere.
- The connector shape (D4) gives extension authors a templated form: scaffolding new integrations becomes mechanical, all integrations get rate-limits + retries + provenance + MCP exposure for free.
- Receipts (D8) make the audit story queryable rather than log-archaeological.
- Federation-native primitives (D6) are the first place "axiom://" routing surfaces in user-visible verbs: cross-cohort scheduling, cross-cohort notifications, cross-cohort capability grants. Concrete federation value lands.

**Negative**

- Significantly more *surface*. Four new primitives + four new agents + a Tech Spec + four PRDs. The portfolio commitment is real.
- D2's universal envelope creates a refactoring path through every existing call site that does authz / notification / scheduling / secret-fetching today. The work is mostly mechanical, but it's not zero.
- The connector-shape lint (D4) will force-update existing in-progress connectors (chat/, signals/, classroom/) to declare their shapes. Some will be retroactively non-conforming.
- D7's RACI graduation depends on RACI's own maturity — ADR-045 D6 just landed; we are exercising it heavily.
- The capability-token model (D3) is genuinely harder than "give the agent your API key." There will be UX friction in the first 30 days as operators learn the model. Phased rollout (PRD per primitive describes) mitigates this.

**Reversibility**

- The action envelope (D2) is additive — existing call sites continue to work as they are; new code consumes the envelope. Mechanical refactoring of old sites can be staged.
- Capability tokens (D3) initially co-exist with raw credentials behind a feature flag; the cutover from raw-credential mode to capability-only mode is per-extension, per-channel, with operator opt-in.
- The agent-name commitments (D10 — GUARD/KEEP/HERALD/PULSE) follow the convention; the *capabilities* they own are the load-bearing thing. Renaming is mechanical.

---

## Non-Goals

- **This ADR does not specify the connector wire formats.** That's the Tech Spec's job. The ADR commits that connectors *have* a shape; the spec defines what that shape is.
- **This ADR does not legislate per-vendor adapters.** Slack adapter / Teams adapter / Notion adapter / etc. are each their own AEOS extension, each conforming to the connector shape; their PRDs are separately scoped.
- **This ADR does not displace the existing federation ADRs.** It composes on top of them. ADR-027 still governs cross-cohort memory; ADR-028 still governs trust scoring; this ADR makes both *operational verbs*.
- **This ADR does not specify mobile.** `axiom.mobile` is named as a future track (one of the channels HERALD adapts to); a separate PRD will own it.

---

## Notes

- **Why now.** The pressure has been building since 2026-04 (competitive-parity-gaps.md), but the trigger is concrete: the Expman domain consumer (the first major domain extension) is about to hit Phase 2, and every Phase 2 capability lands inside this fabric. Notifications for transitions; schedules for SLA timers; capability tokens for the analyzer-tool's API access; authz on every researcher action. Building these *for* Expman, in isolation, would mean every subsequent extension reinvents them.

- **The "build it for one customer" anti-pattern, avoided.** Per `[[feedback_portfolio_products_separate_from_axiom]]`: a domain consumer's roadmap doesn't constrain Axiom planning. The four primitives are sized as **portfolio infrastructure** — Keplo needs them, Vyzier needs them, Postrule needs them. The domain consumer is the first user, not the only user.

- **Capability tokens, briefly: not a new science.** OAuth 2.0 token-bound credentials, AWS STS temporary credentials, Macaroons, Biscuit, Capability URLs — there's a deep literature. The Axiom version (per the Tech Spec) borrows the cryptographic primitives from this literature; the novelty is *plumbing them through provenance + trust + classification + RACI uniformly*.

- **What this enables that Mastra, LangGraph, Claude Code, Cursor, Aider cannot.** A single rule about classification, propagated to every primitive simultaneously, with cryptographic enforcement and a queryable audit trail. A peer node trusting your cohort can grant a capability token your scheduled job presents at theirs, with credibility scored by the trust graph and outflow gated by their `cohort_policy`. An agent revealed to be misbehaving gets *its capability revoked everywhere on the federation* in one operation. None of these are within reach of architectures that treat each concern as an independent silo.

- **The cost of not doing this.** Every alternative is worse. Per-primitive teams build incompatible verbs; secret-handling gets reinvented per extension; notification routing forks per channel; classification checks duplicate across the codebase; the audit trail becomes log-archaeology. Three years from now we'd have to undo it. Better to commit to the shape now while there are four primitives to build, not forty.
