# SKILL: worktree-hygiene

**Owner:** TIDY (`axi tidy`)
**Kind:** skill (model-mediated)
**Status:** active
**Last updated:** 2026-05-02

## Purpose

Notice and propose removal of stale `git worktree` checkouts that are
cluttering a developer's workspace. TIDY's hygiene mandate covers
filesystem-level cruft; orphan worktrees are exactly that.

## When this skill fires

TIDY surfaces worktree clutter when **any** of the following are true
on a worktree, and **none** of the safety floors below apply:

- **S1** — the worktree's directory does not exist on disk, or git
  itself marks the entry "prunable" in
  `git worktree list --porcelain`.
- **S2** — the worktree's branch has been deleted on origin
  (`git ls-remote --heads origin <branch>` returns no rows).
- **S3** — the worktree's HEAD is already an ancestor of
  `origin/main` (`git merge-base --is-ancestor` exits 0).
- **S4** — a PR for the branch is `MERGED` or `CLOSED` according to
  `gh pr list --head <branch> --state all`.

A verdict cites which signals fired. TIDY **does not** propose
removal on "this looks old" or "no commits in N days" alone — every
proposal has a concrete, falsifiable citation.

## Safety floors

TIDY **does not** propose force-prune (and refuses `--force`-less
removal) when:

- The worktree has uncommitted or untracked content that is **not** a
  known nit (`__pycache__`, `.DS_Store`, `.pytest_cache`,
  `.mypy_cache`, `.ruff_cache`).
- The worktree is administratively locked
  (`git worktree lock`) and no S1 signal fired.
- The worktree path resolves to the repository's main checkout.

In those cases the worktree appears in the report (so the developer
can decide), but the verdict is advisory rather than actionable.

## Action

The deterministic action is `axi hygiene list worktrees [--prune] [--dry-run]
[--force] [--repo PATH]`:

- **No flag** — list each worktree with marker `STALE` / `ok` and
  cite reasons.
- **`--dry-run`** — print the exact `git worktree remove …` command
  TIDY *would* run, with no side effects.
- **`--prune`** — execute `git worktree remove`. Adds `--force`
  automatically when git requires it, except when a non-nit dirty
  state is present (then skip with a hint).
- **`--force`** — override the dirty-content floor. Required when the
  developer has reviewed the dirt and confirmed it's safe to drop.

## RACI posture

Per `feedback_raci_automation_escalation`, TIDY proposes; the
developer approves. The default posture is:

- **R** (Responsible): TIDY — runs the assessment.
- **A** (Accountable): the developer — must approve a `--prune`.
- **C** (Consulted): nothing else.
- **I** (Informed): RIVET — reads the verdict via heartbeat to
  surface clutter alongside CI status.

After three consecutive "no" responses on the same worktree, TIDY
stops re-proposing it for the session.

## Related

- `feedback_raci_automation_escalation` — escalation cadence
- `project_burn_e_phase1_landed.md` — RIVET vs TIDY division of labor
- RIVET cloud-routine watcher — companion lifecycle eye for cloud
  routines (lifecycle ≠ hygiene; do not conflate)
