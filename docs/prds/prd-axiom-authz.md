# PRD: `axiom.authz` — Unified Authorization Site (GUARD)

**Status:** Draft (2026-05-30)
**Owner:** Benjamin Booth
**Companion ADR:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md)
**Companion Spec:** [spec-governance-fabric.md](../specs/spec-governance-fabric.md)
**Primitive class:** AEOS built-in extension (`axiom.extensions.builtins.authz`)
**Agent:** GUARD (Reviewer + Governor)

---

## 1. Elevator Pitch

GUARD is the single decision point every other primitive consults for *"may actor A do action X on resource R under classification Z?"* Today that question is asked at hundreds of call sites, answered by ad-hoc `if user.is_admin:` checks, and the audit trail is log archaeology. After GUARD, the question is asked one way, answered through one engine that consults ownership + trust + classification + RACI uniformly, and produces a verdict that's itself a queryable audit fragment.

## 2. Problem / Opportunity

### What's broken today

- **Authorization is duplicated.** The classroom extension's `instructor_only` check, the federation's `peer_admitted` check, the RAG layer's `tier_allowed` check, the policy engine's `policy_passes` check — all four answer the same question (may this actor do this thing?) with different code paths, different signatures, different failure modes, and no shared audit format.
- **The policy engine exists but is consulted unevenly.** `src/axiom/policy/` has the four-scope engine, the directive store, the action guard. But there's no contract that says *"if you're writing a function that takes user-controlled inputs and produces side effects, you must consult policy."* So most call sites don't.
- **Verdicts aren't auditable.** When a federation admission succeeds, the success is logged; when an `instructor_only` check passes, nothing is recorded. We can't answer "who authorized this action?" without forensic log work.
- **No graduation path.** ADR-045 RACI distinguishes proposal / pre-approved / autonomous, but only the agents that bothered to wire it up. A novel action class has no default disposition; it either runs or it doesn't.
- **No federation-aware authorization.** A peer cohort node forwards an action; the local node has no canonical way to consult its own cohort policy + the peer's trust score + the resource's classification together and return a typed verdict. Each federation call site rolls its own.

### Why now

- ADR-055 commits to the action envelope (§1 of the spec); we need a single primitive that *interprets* envelopes.
- ADR-052 just shipped — `axiom.authz` will be the second extension to land on the DatabaseProvider primitive. The schema-per-extension contract is the right home for verdict storage.
- ADR-045 D6 (graduation safety) just shipped — RACI graduation can be made the default disposition uniformly only if there's one site to wire it through.
- A representative domain extension (e.g. a workflow consumer) lands as the first major domain extension during 2026-06; every transition will consult `authz.decide`. Building it without GUARD means each transition site rolls its own check.

## 3. Goals & Success Metrics

**Primary goal:** Every action that crosses an authorization boundary on the platform consults `axiom.authz.decide(envelope)` exactly once, and the verdict becomes a queryable audit fragment.

**Success metrics (post-implementation):**

| Metric | Target |
|---|---|
| Number of distinct ad-hoc authorization sites in the codebase | 0 (lint enforces) |
| `authz.decide` p99 latency, cached | < 5 ms |
| `authz.decide` p99 latency, cold | < 50 ms |
| Audit-trail completeness — "for every action that produced an effect fragment, find the authz verdict that permitted it" | 100% in the audit drill |
| RACI graduation pathway exercised | Every primitive's autonomy ladder uses `authz`'s graduation machinery, not its own |
| Federation-inbound actions denied at the boundary when peer trust score is below per-resource threshold | 100% in the fuzz suite |
| Time-to-decision for a novel action class (no prior policy) | Returns `propose_to_human` within p99 < 10 ms; never permits silently |
| Lint failures on a primitive's PR that elides `decide` for an action site | 0 (lint blocks merge) |

## 4. Key Users / Personas

| Persona | Primary tasks | Pain today |
|---|---|---|
| **Extension author** | Add an action to my extension that needs authorization; understand the contract for what makes my action permitted. | Hand-rolls `if user.has_role`; no shared audit format; novel action classes have no graduation story. |
| **Platform engineer** | Refactor a call site that has implicit-trust into one that consults authz. | No canonical decision API; must choose between four ad-hoc check paths. |
| **Operator (graduation site)** | Approve a graduation proposal so subsequent same-class actions are autonomous. | Each primitive has its own proposal UX (or doesn't). One uniform RACI proposal pattern would unify. |
| **Compliance auditor** | "Find every action involving CUI data this quarter and the principal who authorized it." | Cross-system log forensics. |
| **Federation operator** | "Admit cohort X's WARDEN to forward actions classified `internal` only when their trust score ≥ 0.7." | One-off code per cohort policy; no canonical site. |
| **Security reviewer** | Verify that no action of class C ever bypasses authorization. | Static analysis is the only assurance; no central audit trail. |

## 5. Scope — Key Capabilities

### 5.1 The decide API

```python
# axiom.extensions.builtins.authz.public_api

def decide(envelope: ActionEnvelope) -> Verdict:
    """The single decision point. Returns a typed verdict and writes a receipt."""

@dataclass(frozen=True)
class Verdict:
    decision: Literal["permit", "deny", "propose_to_human", "rate_limit", "expired_capability"]
    reason:   str
    receipt_fragment_id: FragmentRef
    graduation_state:    GraduationState
    next_action_for_caller: NextAction  # 'proceed' | 'abort' | 'enqueue_proposal' | 'await_human'
```

The contract: callers branch on `verdict.next_action_for_caller`. No legitimate caller inspects the `decision` directly; the typed next-action field is the API.

**Acceptance:** every primitive's call sites consult `decide` and branch correctly; tests verify the no-bypass property under fuzz.

### 5.2 Policy rules + matchers

A `Rule` declaratively matches `ActionEnvelope` fields and returns a partial verdict:

```python
@dataclass(frozen=True)
class Rule:
    name:               str
    intent_match:       IntentPattern         # e.g. "notification.send.*"
    actor_match:        PrincipalPattern      # e.g. "@instructor:*"
    resource_match:     ResourcePattern       # e.g. "channel://slack/team-rsc/#alerts"
    classification:    list[Classification]   # which classifications this rule applies to
    federation_origin: Optional[PeerPattern]  # for inbound peer actions
    disposition:       Literal["permit", "deny", "propose", "require_capability"]
    priority:          int                    # higher wins on conflict
    ttl:               Optional[datetime]     # rules can expire
```

Rules live in `authz.policies`; the engine evaluates all matching rules and combines per a documented precedence (deny wins ties; explicit propose beats implicit permit; higher priority wins per-disposition).

**Acceptance:** rule engine + precedence tests covering every documented case; benchmark `decide` against a 10k-rule policy database remains under p99 5 ms.

### 5.3 RACI graduation as the default disposition

Per ADR-045 + ADR-055 D7: when no explicit rule matches and the action's intent class has no prior graduation state for this actor, the default verdict is `propose_to_human`. After N successful approvals (configurable per intent class; default 5), the verdict graduates to `permit`. If the human ever denies a proposal of that class, the counter resets and a `regression_to_proposal` signal fires.

The graduation state is stored as `authz.graduation` keyed by `(actor, intent_class, resource_pattern)`.

**Acceptance:** novel action class returns `propose_to_human`; after configurable N approvals the same action returns `permit`; a denial resets the counter; receipts capture every graduation transition.

### 5.4 The `axi audit` CLI

```bash
axi audit list --since 7d --primitive notification --actor @jim:example-org
axi audit show <receipt-fragment-id>
axi audit chain <receipt-fragment-id>             # walk provenance backwards
axi audit causes <fragment-id>                    # find receipts that produced this fragment
axi audit graduation --actor @bbooth:example-org  # show all graduation states
axi audit explain <receipt-fragment-id>           # human-readable rationale for the verdict
```

The `explain` subcommand is load-bearing: it reads the receipt and reconstructs the rules that matched, the graduation state at the moment, and the federation context — outputs the *why*, not just the *what*.

**Acceptance:** every subcommand has structured output (`--json`) and human-readable terminal output; `explain` covers every Verdict.decision; integration tests against a synthetic 30-day receipt corpus.

### 5.5 Migration of existing authorization sites

A migration order, ordered by leverage:

1. **Federation peer admission** — the highest-trust boundary. Move from ad-hoc cohort code to canonical `decide` consultation. Per ADR-027.
2. **RAG retrieval (RPE)** — the eight RPE intents map to `intent` ontology entries; rules consume.
3. **Classroom course-fork + course-promote** — instructor authorization sites.
4. **LLM provider calls** — gateway-level invocation; classification routing on the prompt + retrieved memory.
5. **Memory composition delete + share** — ADR-026's right-of-delete + cross-cohort sharing.
6. **Extension `axi ext install / publish`** — install-time authorization of the publisher signature.
7. **(Future)** Every primitive added after this PRD lands.

**Acceptance:** each site converts in a separate PR with TDD; the lint check from §5.6 prevents regressions.

### 5.6 The `no_action_without_authz` lint

Per spec §9.1: a static-analysis check that every public function in a primitive that takes an `ActionEnvelope` calls `decide` before performing work.

**Acceptance:** the lint runs in CI; PRs that elide `decide` fail; the lint has a documented allowlist for boot-time / synthetic-action call sites (§1.4 of the spec).

### 5.7 Federation-side WARDEN integration

GUARD and WARDEN cooperate at federation boundaries:

- **Outbound** — when a local action forwards to a peer, GUARD's verdict is `permit_with_federation_hop`; WARDEN re-signs the envelope for cross-cohort transit per ADR-022.
- **Inbound** — when a peer forwards an action to us, WARDEN's signature-verification gates GUARD's classification + cohort-policy evaluation.

**Acceptance:** federation fuzz tests verify that no peer-forwarded action skips GUARD; trust-score thresholds enforce per-resource (§7.3 of the spec).

## 6. Non-Functional / Constraints

- **Performance** — p99 < 5 ms cached; < 50 ms cold (§11 of spec).
- **Storage** — `authz.*` tables grow ~1 receipt per action; ADR-049 silver/gold compaction handles long-tail.
- **Availability** — `authz.decide` MUST succeed-or-deny-fast; never silently permit on infra failure. Inability to reach the policy engine returns `deny` with reason `engine_unavailable`.
- **Federation neutrality** — verdicts on local-origin actions never depend on peer reachability.
- **No bypass** — there is no in-band escape from `authz.decide`. Operators reach the disable path via tier-0 OS-level credentials (`/etc/axiom/break_glass`), recorded as a tamper-evident receipt.
- **Backward compatibility** — existing ad-hoc authorization sites continue to work in parallel during migration; both decisions are recorded (with the `legacy_authorization` provenance stamp) until the site is cut over.

## 7. Timeline (high level)

| Phase | Scope | Target |
|---|---|---|
| Phase 0 | This PRD + spec sections cross-referenced | 2026-06 |
| Phase 1 | Decide API + Rule engine + Postgres schema; tests; no migration yet | 2026-06 |
| Phase 2 | RACI graduation + `axi audit` CLI + the `no_action_without_authz` lint | 2026-07 |
| Phase 3 | First three migration cutovers (federation admission, RPE, classroom) | 2026-07 → 2026-08 |
| Phase 4 | WARDEN federation handshake + cohort policy import | 2026-08 |
| Phase 5 | Remaining migration cutovers; the lint becomes hard-blocking | 2026-09 |

Each phase ships value: Phase 1 lets Expman consume; Phase 2 makes audits trustable; Phase 3 closes the first three classes of duplicated auth; Phase 4 unlocks federation-native; Phase 5 closes out the migration debt.

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Decide latency becomes a bottleneck | Cache verdicts per `(actor, intent, resource, classification)` with TTL bounded by graduation state; benchmark every phase |
| Rule precedence ambiguity (two rules both match, opposite disposition) | Documented precedence; `decide` exposes the precedence path in receipts; integration tests cover the tie-breakers |
| Graduation thresholds wrong by default | Default `n=5` is conservative; per-class override; operator can graduate manually |
| RACI proposal UX overwhelms a single human | Proposals batch in HERALD inbox; categorize by intent class; recipient preferences per class |
| Existing call sites don't get migrated; lint stays advisory | Phase 5 hard-blocks; pre-Phase-5 a quarterly migration push tracked in `docs/working/governance-fabric-march.md` |

**Open questions:**

- (Phase 2) Should the graduation counter live per-`(actor, intent_class)` (broad) or per-`(actor, intent_class, resource_class)` (narrow)? Trade between learning speed and security granularity. Decision deferred to Phase 2 design review.
- (Phase 4) How does WARDEN's federation-side authz interact with the GUARD's local one when the trust scores disagree? Conservative side wins (the lower trust score governs), but the receipt's blame attribution is open.
- (Phase 5) Break-glass mechanism — `/etc/axiom/break_glass`-style tier-0 bypass. UX, operator training, tamper-evidence storage. Separate sub-PRD?

## 9. Acceptance & Rollout

**Sign-off:**
- Engineering: Ben Booth
- Product: Ben Booth (B-Tree Labs)
- Security review: TBD external (when Phase 4 federation work begins)

**Rollout plan:**
1. Phase 0–1 land on `feat/governance-fabric-authz` branch.
2. Phase 1 cuts axiom 0.25.x with `axiom.authz` available but no migrations done.
3. Phases 2–3 each cut a minor: 0.26 (audit + lint), 0.27 (first migrations).
4. Phase 4 cuts 0.28.
5. Phase 5 cuts 0.29 and toggles the lint to hard-block.

**Rollback criteria:**
- `decide` latency degrades > 2× target → throttle the rule-evaluation engine; surface alert via HERALD.
- A correct-by-construction proof of the no-bypass property fails its assertion → halt Phase 5 cutover.
- Federation fuzz tests reveal an inbound peer can skip GUARD → emergency revert + post-mortem.

## 10. Contacts & Links

- Product lead: Benjamin Booth — no-reply@axiom-os.ai
- Eng lead: Benjamin Booth
- ADR: [`adr-055-unified-governance-fabric.md`](../adrs/adr-055-unified-governance-fabric.md)
- Spec: [`spec-governance-fabric.md`](../specs/spec-governance-fabric.md)
- Sibling PRDs: [vault](prd-axiom-vault.md), [notifications](prd-axiom-notifications.md), [schedule](prd-axiom-schedule.md)
- Related — ADR-026 ownership, ADR-027 federated memory, ADR-028 trust graph, ADR-035 binding, ADR-045 RACI, ADR-049 data platform, ADR-052 DatabaseProvider

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
