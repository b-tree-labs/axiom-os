# Cloud Routine Prompt Pattern — Specification

**Status:** Active — Reference Implementation
**Version:** 0.1.0
**Editor:** Benjamin Booth
**Date:** 2026-05-03
**Reference implementation:** Axiom platform (`b-tree-labs/axiom-os`)

---

## Abstract

This specification defines the **state-machine prompt pattern** that all cloud-spawned agent routines (those launched via `RemoteTrigger`, `axi cloud routine`, or equivalent) MUST follow. The pattern exists because routine prompts written as task-list checklists for a junior engineer have a well-documented failure mode: the agent stops at a plausible exit point well short of the true acceptance criteria. State-machine structure with mechanically verified exit conditions makes that failure mode structurally impossible.

This specification is a companion to `spec-agent-coverage-manifest.md`. The Coverage Manifest names *what to watch*; this spec governs *how a routine resolves an escalated condition*.

---

## 1. The originating failure mode

On 2026-05-03 a cloud routine was tasked with driving the Axiom test sweep to zero failures. The prompt was written as a thorough engineering brief: context, goal, approach (ordered), constraints, out-of-scope, acceptance, push-and-PR, tactical hints. The routine made five surgical commits, pushed the branch, and **stopped before opening the PR**. Five Whys traced the failure to four contributing factors:

1. The "Push & PR" section was last in a long prompt.
2. Acceptance was a checklist for a human reviewer, not a self-check the agent had to mechanically pass.
3. The user-visible artifact (the PR) was the prompt's last action, not its first.
4. Nothing forced the agent to verify exit conditions before stopping.

The state-machine pattern below addresses each.

---

## 2. Conformance

A cloud routine prompt conforms to this specification when:

1. It is structured as numbered states (§3).
2. The first state (§4) verifies preconditions before any modification.
3. The user-visible artifact (PR, issue, comment, file) is created in an EARLY state, not as the final action (§5).
4. Each state has at least one mechanically verifiable post-condition check (§3.2).
5. The final state (§7) is exit verification with a mechanical check list. Failure to satisfy any check means the routine reports honestly and does NOT exit clean.
6. The routine is required to produce a final numbered status report (§8).

The keywords MUST, SHOULD, and MAY are interpreted per RFC 2119.

---

## 3. State-machine structure

### 3.1 Required sections

Every conformant prompt MUST contain three section types:

- **STATE 0 — Preconditions**: verify on entry (§4)
- **STATE 1..N — Transitions**: ordered actions, each with post-condition checks (§5, §6)
- **STATE EXIT — Verification**: mechanical exit checks; the section the agent must reach to exit clean (§7)

### 3.2 Post-condition checks

A post-condition is a shell command (or equivalent tool call) whose output is mechanically classifiable as pass/fail. English-prose descriptions of "what should be true" are NOT post-conditions; they are documentation.

Example:
- ✅ `gh pr view --json state -q '.state == "OPEN"'` returns `true`
- ❌ "the PR is open and looks good"

---

## 4. STATE 0 — Preconditions

The first state MUST verify entry assumptions before any modification. This is a defensive measure against environment drift between routine schedule time and routine fire time. Any false precondition causes the routine to STOP and report; modification proceeds only if all preconditions hold.

Typical entry checks:
- Branch / tag exists at expected SHA.
- Required files exist; required tools (`gh`, `pytest`, `ruff`) are available.
- The work the routine is about to do has not already been done by a parallel agent.
- Prior runs of this routine left artifacts that the current run must build on.

---

## 5. Artifact creation MUST be early

The single most important structural constraint: **whatever artifact a human will eventually review (PR, issue, document, comment) MUST be created in an early state, not as the final action.**

Why: an agent that runs out of token budget, hits an unexpected error, or judges itself prematurely-done at any point in a long routine will leave behind whatever it has done up to that point. If the artifact is created early — even in placeholder form — partial progress remains visible and resumable. If the artifact is created last, partial progress vanishes.

In the originating incident, the routine performed the entire fix loop and pushed the branch, then stopped before creating the PR. The work was preserved (commits on a remote branch) but was operationally invisible: no PR meant no review queue entry, no status badge, no notification. Closing this gap is the spec's primary structural requirement.

The pattern: create the artifact in placeholder form in an early state; iteratively update it as later states complete.

---

## 6. STATE N — Transitions

Each transition is an action with:

- A clear goal stated in one sentence.
- The shell commands or tool calls required to accomplish it.
- A post-condition check (§3.2) that runs *before* the next state begins.
- Explicit guidance on what to do if the post-condition fails (typically: STOP and report; or retry within the same state).

Transitions SHOULD be ordered so that:
- Cheap checks (preconditions, snapshots) precede expensive work (full test sweeps, multi-commit refactors).
- The user-visible artifact (§5) is created within the first three states.
- State changes are atomic from a partial-completion standpoint: if the routine dies between states, the resulting branch / PR / issue is in a coherent state.

---

## 7. STATE EXIT — Verification

The final state is a mechanical check list. All checks MUST pass for the routine to exit clean. This section closes the failure mode where an agent judges itself "done" based on intuition rather than verification.

A typical exit verification:

```bash
# Check 1: primary acceptance criterion is met
pytest tests/ -q | grep -E '0 failed' || EXIT=1

# Check 2: artifact is in expected state
gh pr view "$PR_NUM" --json state,isDraft -q '.state == "OPEN" and .isDraft == true' \
  | grep -q true || EXIT=1

# Check 3: no destructive side effects
git diff --diff-filter=D --name-only origin/main..HEAD -- 'tests/' | grep . && EXIT=1

# Check 4: code style is clean
ruff check src/ tests/ || EXIT=1
```

If `$EXIT` is set, the routine MUST:
- Comment on the artifact (PR, issue) describing exactly what's left.
- Update the artifact's status section to reflect blocked vs done.
- Exit with a clear report stating which check(s) failed and why.
- NOT mark the artifact ready-for-review.

The contract is: **honest partial completion is better than false claim of done.**

---

## 8. Final report

Every routine MUST produce a numbered final status report. The report's structure mirrors the state machine:

```
1. Did STATE 0 preconditions hold?  [yes / no, which one failed]
2. Did STATE 1 succeed?              [yes / no, error and recovery]
3. Did STATE 2 (artifact) create?    [PR/issue number + URL]
4. Did STATE 3..N succeed?           [per-state outcome]
5. Did STATE EXIT verification pass? [per-check pass/fail; if any failed, which]
6. Final artifact URL.
```

This report is the routine's audit trail. It is the only output a human is guaranteed to read; it MUST be honest about partial completion.

---

## 9. Anti-patterns

The following patterns are explicitly NON-CONFORMANT and SHOULD be removed when found:

- **Ordered checklists without post-condition checks.** "Step 5: open the PR" with no verification that the PR was opened. Replace with a state that has an explicit post-condition.
- **Acceptance criteria as the final section, with no exit verification.** Replace with STATE EXIT (§7).
- **English-prose acceptance.** "The PR should be in good shape" is not mechanically checkable. Replace with shell commands.
- **Aspirational tactical hints in lieu of structure.** "If you have time, you might want to..." invites the agent to skip work it should always do. If the action is required, make it a state; if it is optional, omit it.

---

## 10. Migration of existing routines

Routines that pre-date this specification MAY continue to operate. New routines MUST conform. When an existing routine fails in a way the state-machine pattern would have prevented, the failure is itself a `warn`-severity entry in the Coverage Manifest (§4.2 of `spec-agent-coverage-manifest.md`) and the routine SHOULD be migrated as part of the response.

---

## Appendix A — Reference example

The 2026-05-03 retry routine `trig_01UaVtR7FcjnULmmTPcR6saz` is the canonical reference example. Its prompt:

- STATE 0 verifies the branch state, ahead/behind counts, and PR absence.
- STATE 1 rebases onto current main with a post-condition that targeted tests still pass.
- STATE 2 opens the draft PR — the structural fix for the prior routine's failure mode.
- STATE 3 snapshots and categorizes failures.
- STATE 4 applies fixes iteratively, updating the PR body after each batch.
- STATE 5 (EXIT) runs four mechanical checks; failure of any one means honest partial completion.

Subsequent specifications and routines SHOULD borrow this structure.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
