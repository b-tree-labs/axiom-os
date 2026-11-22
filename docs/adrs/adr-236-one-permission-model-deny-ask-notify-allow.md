# ADR-236: One permission model: GUARD answers deny, ask, notify or allow

**Status:** Proposed (2026-11-05)
**Supersedes on acceptance:** [ADR-043](adr-043-raci-evolution.md), [ADR-045](adr-045-raci-evolution.md)
**Amends:** ADR-055 D7, ADR-081 §1, ADR-114 §2 and §3, ADR-135, `prd-axiom-authz` §5.1 to §5.3, `spec-governance-fabric` §5.1
**Related:** ADR-035, ADR-126, ADR-134, ADR-142, ADR-201, PR #1277, PR #1503, PR #1504
**Migration:** [unified-approvals-migration-plan-2026-11-05](../working/unified-approvals-migration-plan-2026-11-05.md)

## The model

*May this actor do this action on this resource?* GUARD's `decide()` answers, always with a receipt. Nothing else decides.

| Answer | What happens |
|---|---|
| `deny` | Refused. |
| `ask` | Waits for a person (`axi approve`). |
| `notify` | Runs now, shows in a digest, can be undone for 24 hours. |
| `allow` | Runs. The receipt is the only trace. |

Rules name an action and an answer, and optionally a resource, an actor or a category. **The strictest matching rule wins**, in the order above. With no match the answer is `ask`, except that a declared read is `allow`. Five presets name common rule sets. After ten clean approvals across three days the system suggests a rule, ending at `notify`, and a person accepts it once. A denial, a revert or an undo withdraws it. Floors cap everything: irreversible, classified, safety-critical and control actions never go past `ask`.

```toml
# permissions.toml, shown by `axi permissions`
preset = "cautious"
accountable = "@owner"
approvers = ["@maintainer"]

[[rule]]
action = "pr.rerun"
actor = "@rivet"
resource = "pr://acme/app/*"
answer = "notify"

[[rule]]
action = "pr.*"
category = "sensitive"     # a fact the caller computed, here from the paths
answer = "ask"
```

| Old term | New term |
|---|---|
| RACI `A`, `C` | `ask` |
| RACI `N` / `I` | `notify` / `allow` |
| `permit` / `propose_to_human` | `allow` / `ask` |
| `rate_limit`, `expired_capability` | `deny`, with that reason |
| `step_up_required` | `ask`, answered by a stronger login |
| Trust levels 1 to 5 | Presets `locked`, `cautious`, `balanced`, `autonomous`, `full` |
| `raci.<action>`, site rule, `tool_permissions.json` entry | A rule |
| `raci_default`, `PlanStepGate` | A rule, or no rule (`ask`) |
| Graduated, ADR-043 `AUTO` | An accepted rule |
| ADR-043 `OFF`, three refusals | A `deny` rule |
| ADR-043 `DEGRADED` | Rule withdrawn, count at zero |
| Rungs 0 to 2 / 3 / 4 (#1277) | No action to decide / `ask` / `notify` or `allow` |
| Ceiling, `authority_reserved`, safety floor | Floor |
| Seat (#1277) | An action, actor and resource in a rule |

## Context

Axiom answers this question in at least eight places that do not connect, though `prd-axiom-authz` says one graduation system serves every primitive. Thresholds disagree: 5 (PRD, ADR-043, #1504), 10 (ADR-045 D3), 47 of 50 plus 7 days (ADR-045 D4). The code is further apart than the documents: `C` blocks only on paper and in shadow, because both live `check_raci` callers proceed on it; `raci_default` is stored and never read; `pr.*` matrix rows never reach a live decision. The migration plan has the inventory.

## Decision

1. **One decision.** `decide()` is the only decider. The capability check and the OpenFGA substrate stay in front of the rules. `check_raci`, `posture()`, `ToolPermissions` and the refusal paths of `guarded_act` become callers. A person acting at their own terminal is the answer to their own `ask`, as `UNGATED_SURFACES` already holds. An approved plan hash is how a destructive `ask` is answered.
2. **Notify is new and fails closed.** `notify` gets its own `NextAction`, so a caller not yet migrated stops. It requires declared `risk = "reversible-write"` and an undo handle. No undo means `ask`.
3. **Strictest wins, not most specific.** This is the engine's existing order and it cannot loosen anything. Specificity would need a tie-break for incomparable rules (an actor rule against a resource rule), which would be strictest anyway, and would let a narrow `allow` beat a broad `ask`, which ADR-114 §3 forbids. Since the default is `ask`, nobody needs a broad `ask` with narrow exceptions. `priority` is removed (it never changed an outcome). `require_capability` becomes `deny`.
4. **Reads and the open posture.** `risk = "read"` is `allow`. The shipped open rules (`tool_open_default`, `open_site_read`) stay as named rules until each surface can ask, then reversible writes become `notify` and undeclared risk becomes `ask`.
5. **One file, one noun.** `permissions.toml` replaces `site_rules.toml`, keeping the deny, ask and allow words chat already uses. A repo may carry `.axiom/permissions.toml`, because an actor running in CI has no home directory; it is read from the default branch. The noun is `axi permissions`, matching chat's `/permissions`. `axi approve` remains the person's answer to a waiting request. `axi approvals` would sit two letters from it. `axi flags` and `axi features` are a different question: what exists.
6. **RACI is retired as a name.** Two roles survive as plain fields: `accountable`, always a named person (ADR-035), and `informed`, who receives the digest. `approvers` lists who may answer. Responsible is the actor already on the envelope. Consulted is gone. That is not a RACI chart, and `A` in the code meant approve, so keeping the letters would mislead. An approver may not be the actor.
7. **Earning trust.** The count is per actor, action and scope, where scope is the resource with its instance removed (`pr://acme/app/*`). Ten clean approvals in a row across three days produce a suggestion. Ten with no failure bounds the failure rate near 26% at 95% confidence; five bounds it near 45% and fits in one sitting; a 5% bound needs 59, more than a year for a weekly action, and approval fatigue would win first. A low number is safe because the count grants nothing: a person accepts, the answer stops at `notify`, and floors and the breaker still apply. Distinct days replace the 7-day dwell, since waiting adds no evidence and separate days do. 47 of 50 is dropped because it tolerates denials. Three declines stop suggestions. A denial, revert or undo withdraws the rule at once and zeroes the count; the machine may do this alone, because it only tightens.
8. **Floors.** Compiled in, cannot be lowered: `risk = "destructive"` (and undeclared risk, once declarations are complete), classification `regulated` or above, publish, tag, federation broadcast, direct or forced push to a protected branch, secrets in a change, external-egress writes over MCP, and changes to permissions or floors themselves. A deployment adds floors in read-only config loaded at start, including its safety-critical and control actions. No rule, preset or acceptance passes a floor.
9. **Bounded runs for control actions.** An `ask` per command cannot work inside a control loop. A person approves one run: action, envelope and an end time. Commands inside it go through the deterministic chain (authority, envelope, interlocks) as today. A run is never earned, never suggested and never standing.
10. **Runtime changes.** Changing rules needs the named permission `permissions.change`, writes a receipt, and is open to people only, on the CLI, as `approval.approve` is. Tightening applies at once, including to anything approved and not yet run. Loosening is never retroactive: what is waiting keeps waiting. A hand edit counts when `axi permissions apply` records it.
11. **Brakes only lower.** `autonomy.enabled` stays the master switch. Pause is a `deny` rule with a reason and an end time. The rate breaker sends one burst back to `ask` without touching the rule.
12. **Agents use the same gate.** RIVET's merges, re-runs and branch updates are `pr.*` actions through `decide()`, with no private `auto_allowed` list. A merge through required checks is `reversible-write`, not on the floor; a direct push is.

## Disagreements resolved

| Source | Said | Resolved |
|---|---|---|
| PRD §5.3, ADR-043 D1, #1504 | After N approvals the verdict becomes `permit` | The count suggests. Only an accepted rule changes an answer. PRD amended. |
| ADR-045 D4 | 47 of 50, 7 days, level by level | Decision 7. No levels. |
| ADR-043 D3, ADR-045 D6.4 | Novelty score, scope envelope | The rule's resource is the envelope. Outside it no rule matches, so it asks. |
| ADR-135 | "Nobody promotes"; three gates | Fitness stays a precondition. Maturity is the count, and it suggests. The ceiling is a floor. A person accepts. |
| ADR-081 | `raci.trust` as a second knob | Presets replace the knob. The master switch stays. Levels are not built and stay out. |
| ADR-045 D5.1, `pr.merge` row | Protected push always `A`; merge `N` at trust 4 | Decision 12. |
| ADR-201 | Seats graduate | Seats rank options after the verdict and never answer this question. Their standing keeps that word. |
| #1277 §20 | Seats, rungs, certification | Kept: Accountable is a person, irreversible caps, immediate demotion, a person signs, one answer. Dropped: seat registry, rungs, operating identity, none built. §20 is redrafted on this ADR before it merges. |

## ADR-045 and ADR-043, item by item

| Item | Call | Why |
|---|---|---|
| D5 floor, D5.2 refusals | Keep | The safety invariant. Refusals are `deny` floors. |
| D6.1 `N`, 24h undo | Keep | Earned trust needs a middle answer. |
| D6.2 reversibility gate | Keep | Reads `risk`, which capabilities already declare. |
| D6.3 rate breaker | Keep | Volume is the failure a per-action rule cannot see. Fixed at 3× median or 10 per tick; no learned baseline. |
| D6.4 scope envelope | Keep, no state | Becomes the rule's resource. |
| D6.5 digest | Keep | Without it `notify` is a flood of pings. |
| D2.1 sensitive paths, D2.2 to D2.4 classification | Keep as inputs | A category and the envelope's classification. |
| D3 "always" choice | Keep | It is a rule. Chat has it now (`A`, `D`). |
| ADR-043 D2 pre-approval, D4 three refusals, D7 per node | Keep | A rule, a `deny` rule, and true by construction. |
| D1 action by blast radius | Drop | Never built. Resource, risk and category cover it. |
| D2 one-step multiplier | Drop | Shadow only. A category asks. |
| D3 weights, cohort inheritance | Drop | Never built; no user. |
| D4 retraction rate, severity doubling, cohort vouching | Drop | Never built. One revert already withdraws the rule. |
| ADR-043 `SCHEDULED`, novelty score, sticky `K` | Drop | No consumer. The same threshold applies after a withdrawal. |
| Learned blast thresholds, approver counts, per-category rosters | Drop | Shadow only, or every configured value is identical. |

## Options considered

- **Rewire RACI v2 (ADR-045 as written).** Keeps a 2-D matrix, a multiplier, overrides and levels. Most of it was never built, and it stays a second system beside `decide()`.
- **Seat registry (#1277).** Sound for a standard, but it needs a registry, certification records and identities before the first merge is governed.
- **A mapping layer over the old ladders.** That is one more layer. The plan deletes them instead.

## Consequences

- Ten decision vocabularies become four answers, and the plan lists every deletion.
- Two tightenings are deliberate: `C` becomes a blocking `ask`, and a category asks where shadow moved one step.
- `ActionEnvelope` gains `risk`, `categories` and `scope`, and undeclared risk is a visible debt.
- `decide()` needs receipts that work without a database (#1503) before RIVET can use it in CI.
- `notify` for merges waits for a real undo, a revert pull request. Until then a merge stays `allow`, as it is today.
- A person with shell access can still edit files. This model governs actors that go through `decide()`.
