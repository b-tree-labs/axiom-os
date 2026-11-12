# ADR-046 — RIVET / TIDY boundary: build-forward vs reclaim-backward

**Status:** Proposed (2026-05-26)
**Deciders:** Ben Booth
**Related:** ADR-045 (RACI evolution — TIDY's prune autonomy runs at the D6 `act-then-notify` tier), spec-agent-coverage-manifest.md, spec-cloud-routine-prompt-pattern.md

## Context

RIVET (`release` extension) and TIDY (`hygiene` extension) both sit near the
PR / branch / release lifecycle, and ownership of one capability was
ambiguous: **who deletes a merged branch and prunes its remote ref?**

Observed symptoms of the ambiguity:

- **Neither agent prunes merged remote branches.** After a PR merges on the
  GitHub origin, its head branch persists indefinitely (21 such on the axiom
  repo, 9 on a consumer repo as of 2026-05-26).
- **TIDY detects merged local branches but never executes** the deletion
  (`git_signals.check_stale_branches` marks `auto_fixable=True`; nothing acts
  on it). This is the detection-only anti-pattern.
- The `feat/rivet-close-stale-*` branch names suggested RIVET handles stale
  branches. It does not — RIVET's `close-stale` closes its own **🔴 CI-failure
  tracker issues**, keyed on PR/tag merge state. It has never touched a branch
  ref. The conflation was nominal.

The agent names already encode the resolution: RIVET (the welder) builds and
ships; TIDY (the cleaner) reclaims residue.

## Decision

**RIVET makes the green; TIDY removes the brown. They meet only at the event bus.**

**RIVET owns the forward lifecycle.** Build, test, ship, release, and the
CI-failure-issue lifecycle. RIVET is the authoritative source of "is this
PR/tag merged and shipped?" It emits lifecycle events — `rivet.pr_merged`,
`rivet.tag_released`, `rivet.ci_recovered` — and performs **no destructive git
operations** on branches, refs, worktrees, or stashes.

**TIDY owns backward reclamation.** All destructive git working-state cleanup —
merged local branches, merged remote refs, stale worktrees, orphaned stashes,
stale local tags — in addition to its existing infra hygiene. TIDY **executes**
(not merely detects), under its guard stack (protected-branch list, active-
worktree skip, dirty-floor, dry-run) plus **merge confirmation** (`git branch
-r --merged` + `gh pr` state, or a RIVET lifecycle event) and **reversibility**
(archive the ref before a remote delete; `git stash branch` before a worktree
remove).

**The contract is the event bus, bidirectional:**

- **TIDY → RIVET:** Coverage Manifest state / `local_sweep.healthy`. RIVET
  refuses to cut a tag while a `block`-severity entry is unresolved. (Already in
  place.)
- **RIVET → TIDY:** lifecycle events. TIDY reclaims residue on `pr_merged` /
  `tag_released` instead of polling `gh` itself. (New.)

## Consequences

- **+** Single, unambiguous owner for branch/ref deletion (TIDY); no overlap as
  both agents grow more capable.
- **+** Closes the merged-remote-branch gap and TIDY's detect-only gap.
- **+** RIVET stays inside its "build & ship" persona; it never grows a
  `git push --delete`.
- **+** Every future capability sorts cleanly by one question: *creating/shipping
  (RIVET) or reclaiming (TIDY)?*
- **−** Requires a new lifecycle-event surface on RIVET and a subscription on
  TIDY (the RIVET→TIDY direction did not previously exist).
- **−** TIDY's guard + reversibility surface grows (archive-ref-before-delete,
  remote-ref handling).

## Migration

1. RIVET: emit `rivet.pr_merged` / `rivet.tag_released` / `rivet.ci_recovered`
   from the heartbeat / `pr_check_responder`. No behavioural change to closing.
2. TIDY: add a `branch-hygiene` skill; add merged-local-branch and merged-
   remote-ref prune (guarded, reversible); expose `axi hygiene list branches --prune`
   (dry-run default, destructive form under the `purge` tier); subscribe to
   RIVET's lifecycle events.
3. Coverage Manifest: add a **"Merged branch / remote ref reclaimable"** entry
   (owner: TIDY; detection: `git_signals` + RIVET event; response: prune under
   guards), replacing the worktrees-only framing.
4. Persona deltas to both agents to record the boundary.
