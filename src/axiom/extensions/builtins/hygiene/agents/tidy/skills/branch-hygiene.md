# Skill: branch-hygiene

TIDY prunes **merged** git branches — local and remote — that are residue
of shipped work. This is destructive git cleanup, which TIDY owns per
ADR-046 (RIVET makes the green; TIDY removes the brown).

## When to use

- A PR has merged and its head branch lingers on the remote.
- Local branches accumulate after their work landed on the default branch.
- RIVET emits `rivet.pr_merged` / `rivet.tag_released` (the merge/ship
  signal); TIDY reclaims the now-merged ref.

## Contract (ADR-045 D6)

1. **Candidate only if merged.** A branch is eligible only when its tip is
   reachable from the default branch — `git branch --merged` (local) or
   `git branch -r --merged origin/main` (remote). Protected branches
   (`main`/`master`/`develop`/`trunk`), the current branch, and
   worktree-occupied branches are never touched.
2. **Archive before delete (reversible).** Every prune first points
   `refs/tidy-archive/<local|remote>/<branch>` at the branch tip, so the
   delete is undoable (`undo()` / restore from the archive ref). The action
   is `reversible=True` to the guard on that basis.
3. **Guarded at tier N.** The batch runs through
   `policy.agent_action_guard.guarded_act`. An over-limit batch downgrades
   to a confirmation prompt (`needs_confirmation`) rather than acting
   blindly; the operator confirms with `--yes`.
4. **Never force/non-ancestor deletes here.** Those are irreversible and
   stay human-gated (the guard's reversibility gate refuses them
   autonomously).

## CLI

```
axi hygiene list branches                 # list merged LOCAL branches
axi hygiene list branches --remote        # list merged REMOTE (origin) refs
axi hygiene list branches --prune         # archive + delete merged local branches
axi hygiene list branches --prune --remote        # prune merged remote refs
axi hygiene list branches --prune --remote --yes  # confirm an over-limit batch
axi hygiene list branches --prune --dry-run        # preview only
```

Reclaimed branches are archived under `refs/tidy-archive/` and recoverable.

## Excluded by design

Squash-merged branches that ancestry can't confirm, and branches whose
local copy has unique commits, are **not** auto-pruned — they surface for
human review instead.
