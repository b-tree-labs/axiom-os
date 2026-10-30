# PRD: Capability Budgets (program feature 2)

**Status:** Living. **Spec:** `spec-capability-budgets.md`.

## Problem

"It launched 102 agents and blew my $120 quota in 6 minutes." Runaway
spend is a top-three user pain (limit-exhaustion bugs with 1,400+
comments; vendors removing cost visibility), and the enterprise ask is
verbatim: "pre-funded spend keys with hard limits." Capability tokens
already scope WHAT an agent may do and UNTIL WHEN; nothing anywhere
scopes HOW MUCH it may consume doing it.

## Users

- Anyone who spawns subagents (the runaway case).
- A team lead allocating spend per project/agent with receipts.
- The Receipts surface (burn-down rendering) and the desktop glyph.

## Requirements

### R1 — Budget rides the capability
`CapabilityToken` gains a budget envelope: metered units, cap, drawn.
Enforcement at the `decide()` floor — the same floor whose TTL
enforcement is production-proven. Exhaustion is a floor denial with a
receipt naming the token, the draw history, and the spawn tree.

### R2 — Spawns draw from their parent
A subagent's capability is minted with a slice of the parent's
remaining budget (delegation-shaped, like delegation_depth). The
102-agent storm becomes structurally impossible: the tree can never
exceed the root grant.

### R3 — Metered in native units, honest about dollars
The meter counts provider-native units (tokens, calls). Dollar
rendering uses a visible, editable price table and is always labeled
an estimate. We never claim billing-exact dollars — a mismatch with a
provider invoice would spend all trust the receipts earned.

### R4 — "No new authority," not a kill switch
Exhaustion denies NEW draws (new spawns, next tool call) at task
boundaries; it never terminates in-flight work mid-action. A
half-finished migration killed by a budget is worse than the overrun.

### R5 — Warn before the wall
At a configurable threshold (default 80%) a HERALD notification fires
once, with the burn-down attached. Silent-then-dead is the failure
mode of every quota system users hate; the warning is a requirement,
not a courtesy.

### R6 — Defaults target the runaway case only
Budgets default ON for subagent spawns (inherit-slice), OFF for
top-level sessions (opt-in). The product must never feel like an
allowance to the person who owns the machine.

### R7 — Renewal never refills
Interacts with capability renewal (the 0.58.1 incident fix): a
re-minted token carries the REMAINING budget forward. A long-lived
host that renews hourly must not mint a fresh budget hourly — renewal
continues the grant; it does not enlarge it.

## Non-requirements (v1)

- No provider billing-API reconciliation.
- No org-level budget hierarchies (team→project→agent) — single-grant
  trees only.
- No pricing-table auto-updates (manual, visible, versioned).

## Red-team

**Steelman.** Verbatim demand on both buyer sides; the primitive and
its floor are proven in production this week; nobody ships
spend-scoped capability tokens; the burn-down visual is the program's
most legible money story; and it monetizes naturally (the governance
meter counts draws).

**Strawman.** (1) Cost attribution is a swamp: token counts ≠ invoice
dollars, prices drift, users will call the numbers wrong. (2) Budget
fatigue: users hit the wall twice and set ∞ — the F2 version of
approval fatigue, and the kill test dies. (3) Mid-task denial can cost
more than it saves. (4) Provider-side limits already exist; "my Max
plan is the budget."

**Design responses.** (1)→R3 native units + estimate labeling.
(2)→R5 warn-first + R6 targeting only spawns (the case users already
WANT capped) + one-tap timed raise (+50% for this task, receipted) so
the escape hatch is granular, not ∞. (3)→R4 task-boundary semantics.
(4)→provider limits cap the ACCOUNT, not the tree — the runaway
story exhausts a plan across everything else the user is doing; R2 is
per-tree isolation no provider offers, and the receipt shows which
spawn spent what, which no provider shows either.

## Success criteria / kill test

Success: our own orchestrator + session spawns run budgeted for 30
days; ≥1 real runaway is capped with a receipt; the burn-down renders
in F0. Kill (from the map, refined): if dogfood shows raises/overrides
on >50% of denials, the semantics are wrong — revisit R4/R6 before
shipping outward.
