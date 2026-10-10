# ADR-064 — RIVET gains a narrow fix capability

**Status:** Proposed — 2026-06-01
**Owner:** @ben
**Related:** ADR-045 (RACI evolution), ADR-056 (skill-as-function), ADR-060 (cross-agent event routing), `reference_rivet_ci_owner` memory, `feedback_post_push_ci_watch` memory

## Context

The 2026-06-01 session exposed a structural gap. RIVET detects red CI, classifies the failure, and writes `~/.axi/agents/rivet/reports/pr-NNN-*.md` reports. It does not author fixes, does not loop on stale findings, and does not retry.

Concrete evidence from this one day:

- **PR #337** was red for 17 hours. RIVET filed one report at 06:21Z, classified it `code`, prescribed next steps, then moved on. The PR landed only because a human went looking. RIVET wrote no follow-up report on it the entire day.
- **Five parallel PRs** (#418, #421, #423, #424, #425) red on the same two universal failures (`test_non_repo_aborts_cleanly`, `test_manifest_validates_against_schema`). RIVET wrote a per-PR report for each but never connected them to one root cause. Five reports, one fix.
- **The pre-push hook hardening** (#429) and the **manifest-path fix** (#428) both happened because a human (me) dispatched agents with hand-written prompts. There is no autonomous detect → fix → push loop in the platform.

The current roster splits responsibilities cleanly but leaves the fix half-orphaned:

| Agent | On a red PR |
|---|---|
| RIVET | Detects, classifies, reports, then stops |
| TIDY | Hygiene only (stale branches/worktrees/artifacts); won't author code |
| TRIAGE | Diagnoses CLI errors; no commit authority |
| Everyone else | Domain-bound; doesn't touch CI/PR repair |

Nothing currently owns "see-red → author-fix → push." That role gets done by Claude Code in the harness on demand, which means it doesn't happen when nobody is watching.

## Decision

RIVET gains a narrow **`fix_trivial`** skill that authors fixes for a strict whitelist of recurring patterns. Anything outside the whitelist escalates to a human via HERALD (per ADR-060 routing). The skill never invents code; it applies known transforms on patterns RIVET already classifies.

### Whitelist (initial cut)

1. **Lint autofix** — ruff `F401` unused import / `F841` unused variable / `E702` semicolon-separated statements. Run `ruff check --fix` over the diff scope, commit `chore(lint): rivet autofix on <PR#>`, push to the same branch.
2. **Missing `Bypass-Reason:` trailer** on amend-needed commits when main is red. Amend the trailer on HEAD using the same mechanism the pre-push hook uses (#429).
3. **Stale-rebase failures** — when a PR's failing tests pass on a newer main, run `gh pr update-branch <PR#>` and let CI re-run. Detection: failing tests on the branch's HEAD pass on `origin/main` at HEAD.
4. **Unused-import F401 in test fixtures** — same as #1 but scoped to test files specifically (sometimes the F401 is intentional; the test-files scope is conservative).

The whitelist grows by ADR amendment, not by RIVET self-extension. Every new pattern requires: (a) a documented detector, (b) a documented transform, (c) test coverage of the transform, (d) a kill-switch env-var to disable that pattern.

### Confidence policy

Each whitelist pattern carries a `confidence_floor` field (0..1). RIVET runs the fix only when its classifier's confidence on the failure type meets the floor. Default floor is `0.9` for code-mutating fixes; `0.7` for non-mutating actions (rebase, retry-flake). Below floor, RIVET writes the report as today and stops.

### Audit + reversibility

Every fix commit RIVET authors carries a trailer:

```
Rivet-Fix: <pattern-id>
Rivet-Confidence: <float>
Rivet-Source-Run: <github_action_run_url>
```

`axi rivet fix-log` lists every fix RIVET has authored, queryable by PR / branch / pattern. Operator can `axi rivet fix-revert <commit>` to back out a single fix; the revert is itself trailered and logged.

### Escalation path (per ADR-060)

When RIVET decides a failure is **not** in the whitelist, or confidence is below floor, it:
1. Writes the failure report as today.
2. Publishes `rivet.fix_escalated` on the agent bus with the report path and classification.
3. The agent-bus → HERALD bridge routes the escalation to the operator's `(classification: internal, priority: high)` channel per recipient preferences.

This is the same bridge ADR-060 + agent_bridge.default_routing already ship. No new wiring; just a new subject.

## Consequences

**Wins**
- PR #337's 17-hour silence becomes impossible: a stale-rebase pattern picks it up on the next heartbeat and rebases automatically.
- Lint-only failures (PR #423's `import logging`) repair themselves; the human PR queue shrinks.
- The audit trail makes RIVET's fixes reviewable + reversible without trust-falling into autonomous code mutation.
- Builds on RIVET's existing classifier + skill registration; no new agent personality, no new daemon.

**Costs**
- The whitelist needs disciplined growth. The temptation to broaden it past patterns with mechanical transforms is real. ADR-amendment requirement is the brake.
- A bad classifier confidence + a whitelisted pattern could push a wrong fix. The confidence floor + revert log are the mitigations; the kill-switch env var is the failsafe.
- Operators may grow complacent ("RIVET will handle it") on patterns it doesn't actually cover. The escalation path is the partial answer; the bigger answer is keeping the whitelist documented and short.

**Non-goals (this ADR)**
- A full autonomous "FORGE" PR-repair agent that handles arbitrary failures via LLM judgment. That is a separate proposal; this ADR is the bounded mechanical step that proves the pattern + builds the audit infrastructure FORGE would need.
- Cross-PR coordination (e.g., closing related duplicate PRs).
- Authoring NEW tests; RIVET only repairs failing existing ones via the whitelist.

## Rollout

| PR | Scope |
|---|---|
| PR-1 | Skill scaffold (`rivet.fix_trivial`) + audit log + revert command + first pattern (lint autofix) + TDD; integrate with existing RIVET heartbeat loop |
| PR-2 | Pattern: missing Bypass-Reason trailer amend |
| PR-3 | Pattern: stale-rebase auto-update |
| PR-4 | ADR-060 escalation subject + recipient-prefs default routing |
| PR-5 | Consumer-side wiring of the same skill for its CI watcher |

Each pattern PR includes: detector test, transform test, confidence floor calibration on the day's real failures, kill-switch env var.

## Open questions

1. **Where does the fix-log persist?** Memory fragment (CompositionService) so it queries through the same surface as receipts? Or a separate JSONL? Memory is more consistent with the platform; JSONL is faster to grep ad-hoc. Lean memory.
2. **Should fix commits go through GUARD's authz?** Probably yes, with a cap-token bound to the PR branch scope. Defer to PR-1 implementation.
3. **Cadence of fix attempts on the same PR?** Avoid loops. Propose: at most one auto-fix per PR per heartbeat tick; if the fix doesn't go green within 2 ticks, escalate and stop.
