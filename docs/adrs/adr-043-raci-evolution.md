# ADR-043: RACI Evolution — Graduated Agent Autonomy by Approval History and Class

**Status:** Proposed (2026-05-04)
**Supersedes:** none
**Related:** ADR-031 (extension self-containment), ADR-035 (LLM-tier policy), ADR-036 (extension runtime surfaces), ADR-039 (LLM-proposes-deterministic-verifies precedent), ADR-040 (compute decomposition — first multi-peer agent-driven primitive).
**Specs:** `spec-aeos-0.1.md`, `spec-agent-architecture.md`, `spec-agent-coverage-manifest.md`.
**PRD:** none (this ADR formalizes operational rules already partially expressed in feedback memories; no new product surface).
**Memory ground (load-bearing):**
- `feedback_raci_hil.md` — RACI for all agent actions
- `feedback_raci_automation_escalation.md` — propose→ask→schedule|back-off|off; 3 nos = stop asking; pre-approval skips ask (RIVET release/lifecycle first consumer; previously BURN-E, folded into RIVET 2026-06-01)
- `feedback_mo_worktree_autonomy_default.md` — M-O autonomously prunes stale worktrees; detection-only is insufficient
- `feedback_state_machine_agent_prompts.md` — agent routines must be state machines with verified exit conditions
- `feedback_agent_liveness_peer_observable.md` — peer-observed liveness as federation state
- `feedback_claude_and_tidy_close_the_loop` — detect-only is insufficient; the HITL loop must close
- `project_burn_e_phase1_landed.md` + `project_agent_persona_wiring_2026_04_28.md` — implementation precedents (BURN-E role folded into RIVET's `heartbeat_liveness_audit` skill 2026-06-01)

---

## Context

Axiom's RACI model gives every agent a clean way to ask permission. The model works for the first action of a kind. It breaks in two opposite directions over time:

1. **Approval fatigue.** A user has approved the same class of action twenty times — TIDY pruning a merged worktree, RIVET archiving a closed PR, the publishing daemon redrafting a PRD link. Every twenty-first prompt is friction without information value. The agent could just *do* the thing. Worse, the user starts approving without reading, which is the failure mode RACI was supposed to prevent.

2. **Overzealous autonomy.** An agent proceeds without asking — usually because *some* action of that class was approved once, but *this* particular instance differs in a way the agent can't see. TIDY auto-prunes a "merged" branch that actually had unique-to-local commits. The publishing daemon auto-redrafts a PRD that an external collaborator was about to edit. Approval-once is not approval-always.

Today the system tilts toward (1): the default is propose-ask, even for actions the user has approved without exception for weeks. There is no machinery that promotes a sufficiently-validated class of action to autonomy. Conversely, there is no machinery that *demotes* an action class out of autonomy when an instance fails or the user reverses an approval — every "auto" is forever until manually unwound.

The 2026-05-04 evening session surfaced both failure modes within hours: (a) M-O's drift dashboard *detected* a contamination event but didn't act or notify, and the user had to ask; (b) `git worktree remove --force` was applied in a batch without the system flagging that *this batch differs* from prior approved batches in containing live stashes. Both failures share a root: the autonomy decision is binary (ask vs. don't ask) and per-instance-stateless.

The capability gap: graduated autonomy. An agent's autonomy on an action class should be a function of (i) the user's approval history for that class, (ii) the demonstrated reliability of the class on this user's workload, and (iii) the **risk** of the specific instance (reversibility, blast radius, novelty relative to prior approvals).

## Decision

Axiom-core ships a graduated-autonomy state machine that every RACI-enabled agent threads through. The state machine has five states per (agent, action-class) pair: `OFF / ASK / SCHEDULED / AUTO / DEGRADED`. Transitions are driven by (a) explicit user input, (b) approval-history accumulation, and (c) instance-novelty checks. The state and history live in a **per-user RACI ledger** that any agent can query before proposing.

### D1 — Five-state model per (agent, action-class)

| State | Behavior | Entered when |
|---|---|---|
| `OFF` | Agent refuses to consider this action class. | User explicitly disabled, or 3 consecutive denies (per `feedback_raci_automation_escalation`). |
| `ASK` | Default. Agent proposes; user approves or denies per instance. | Initial state for new action classes. |
| `SCHEDULED` | Agent batches non-urgent proposals into a digest (daily / on-demand). | User responded "later" / "not now" without disapproving. |
| `AUTO` | Agent acts without prompting; logs to ledger; user can revoke. | User pre-approved the class, OR `N` consecutive approvals with no interleaved denies (default `N = 5`). |
| `DEGRADED` | Was `AUTO`; an instance fired then user reversed/complained. Agent reverts to `ASK` for at least `K` instances before re-promotion is even considered. | A reversal, complaint, or failure is recorded against an `AUTO` instance. |

State is keyed by `(user_id, agent_name, action_class)` not by individual action. An action class is a coarse bucket: `prune-merged-worktree`, `archive-closed-pr`, `redraft-prd-link`, etc. — coarse enough to amortize approvals, fine enough that "approved pruning a merged worktree" doesn't generalize to "approved force-pushing main."

Rationale:

- Five states (vs. 2/3/4) capture both the patience axis (`SCHEDULED`) and the reversal axis (`DEGRADED`), without exploding into a continuous-credit model that's hard to reason about.
- Per-user keying lets a multi-tenant deployment promote different classes to different states for different operators.

### D2 — Pre-approval skips the ASK state — Ben's first-consumer pattern

Per `feedback_raci_automation_escalation`, a user can pre-approve a class via an explicit gesture (settings flag, CLI command, slash command in chat). Pre-approval transitions directly `OFF | ASK | SCHEDULED → AUTO` without requiring the approval-history accumulation. The pre-approval is itself recorded to the ledger and is revocable.

RIVET's lifecycle work was the first consumer (previously BURN-E; folded into RIVET 2026-06-01): Ben pre-approved the action class "archive PR-closed branches I haven't touched in 30 days" so RIVET could run unattended. The same gesture is what every agent uses now.

### D3 — Instance novelty re-routes AUTO back to ASK for the unusual case

When an action class is in `AUTO`, the agent still computes a **novelty score** for each instance against the prior approved instances of the class. If novelty exceeds a threshold, the instance routes through `ASK` even though the class is `AUTO`. Examples of novelty:

- Action operates on a path / target the user hasn't approved before in this class
- Blast radius (files touched, peers contacted, side effects) exceeds the median of prior approvals by a large factor
- Reversibility class differs (e.g., the class was approved for "remove a worktree dir" but this instance also removes a *non-empty stash*)

The novelty check is what would have caught the 2026-05-04 worktree-removal-with-stashes incident: prior `git worktree remove` instances had been on stash-empty worktrees; this batch had stashes attached, which is novel within the class.

Each agent ships a `novelty_features()` function for its action classes. Core provides defaults (path-prefix novelty, blast-radius percentile); extensions override. Novelty thresholds are user-tunable.

### D4 — Three consecutive denies → OFF; the user gets to make agents stop asking

Per `feedback_raci_automation_escalation` ("3 nos = stop asking"). After three consecutive `ASK → deny` outcomes within a sliding window (default 30 days), the class transitions to `OFF`. The agent stops proposing entirely. The user can re-enable explicitly.

The window is a sliding window, not lifetime, so a class that was rejected during one workload phase can be reconsidered later — but the user must re-approve to re-enter `ASK`.

### D5 — DEGRADED is sticky; auto-promotion requires fresh evidence after a reversal

A class that is `AUTO` and produces a reversal does not just go back to `ASK` — it goes to `DEGRADED`. From `DEGRADED`, re-promotion to `AUTO` requires `K` consecutive successful `ASK → approve → uneventful` cycles (default `K = 10`, larger than the initial `N = 5`). This makes auto-promotion *more* conservative after evidence of failure than for an unproven class.

Rationale:

- Reversals are a strong signal that the agent's understanding of the class is incomplete; harder re-promotion forces enough new evidence to either revalidate the class or for the user to refine its scope.
- Without `DEGRADED`, every reversal would either silently re-AUTO (dangerous) or permanently `OFF` (over-correction).

### D6 — The RACI ledger is the source of truth; agents query it, don't cache state

State per `(user, agent, class)` lives in `~/.axi/raci/ledger.jsonl` (append-only) with a derived `~/.axi/raci/state.json` (current state per key). All agents read both before any proposal. The ledger records: timestamp, agent, action class, instance summary, novelty score, prior state, transition, outcome.

This is the surface M-O / drift / publishing all consult. It is also what `axi hygiene raci status` displays.

### D7 — Federation: AUTO promotions are per-user-per-node; trust does not propagate without consent

A class promoted to `AUTO` on Ben's workstation is *not* automatically `AUTO` on his self-hosted node, on a federation peer's node, or on a node Ben SSHes into via his identity. Each `(user, node, agent, class)` accumulates its own approval history, *unless* Ben explicitly opts to propagate via `axi raci promote --to <node>`.

Rationale: a class might be safe on Ben's local laptop (e.g., aggressive worktree-pruning) but unsafe on a shared classroom coordinator node where other students' work lives. The conservative default keeps each environment's promotion under explicit control.

## Consequences

**Positive:**

- Approval fatigue retires for high-volume, low-risk action classes (worktree prune, branch archive, link redraft) once each is validated 5+ times. The user sees a daily digest, not 20 individual prompts.
- Reversals get genuine learning, not just "I'll try harder next time" — the state machine re-conservatizes the class until fresh evidence accumulates.
- Pre-approval is first-class, so a user who knows what they want doesn't have to wait for the system to learn it.
- Novelty checks catch the "approved-once-in-a-similar-but-different-instance" failure mode that a binary ask/don't-ask model can't see.
- A federation deployment can have different per-node autonomy postures without code change.

**Negative:**

- More machinery — five states, novelty scoring, per-class ledger, federation-scoping rules — is more surface to keep correct.
- "Class" definitions are load-bearing: too coarse and a single approval generalizes too far; too fine and approval fatigue persists. Each agent must define them carefully, and refactoring a class definition mid-stream is a UX hazard.
- Novelty scoring is heuristic; false negatives (failed to flag a novel instance) and false positives (flagged something the user doesn't care about) are inevitable. Tuning is per-user.
- Sticky `DEGRADED` can frustrate users who feel they've already corrected the underlying issue.

## Implementation phasing

| Phase | Scope | Status |
|---|---|---|
| **P1 — Ledger + 5-state machine** | `RACILedger` with append-only writes; `RACIState` with derived current-state-per-key; CLI surface to query | Proposed |
| P2 — Novelty defaults + extension overrides | Path-prefix novelty + blast-radius percentile defaults in core; per-agent override via the AEOS manifest | Proposed |
| P3 — DEGRADED sticky promotion | Reversal detection; re-promotion gating | Proposed |
| P4 — Federation per-node scoping | Ledger lives per-`(user, node)`; explicit propagation CLI | Proposed |
| P5 — Daily digest mode | `SCHEDULED` state surfaces a once-per-day batch instead of individual prompts | Proposed |

P1 is the load-bearing prerequisite; P2–P5 layer on without breaking changes.

## Notes

- This ADR is *operational*, not architectural — it formalizes how every agent behaves rather than introducing a new primitive. The implementation lives in `axiom.agents.raci` and is consumed via the existing `BaseAgent.raci` field landed in `feat/agent-fleet-coverage-and-prompt-pattern`.
- The state-machine model intentionally mirrors `feedback_state_machine_agent_prompts` — agents at large should be state machines with verified exit conditions; their own *approval state* should be too.
- This ADR is intentionally domain-agnostic per the project CLAUDE.md.
