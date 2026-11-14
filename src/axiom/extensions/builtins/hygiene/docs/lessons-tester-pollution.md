# Lessons — test-fixture identity pollution

**Incident date:** 2026-05-04
**Repo affected:** `axiom` (one branch — `feat/twin-build-phase-0`)
**Scope:** 18 commits authored as `tester <t@t.test>`, 8 of which were destructive scaffold-churn commits, all baked into a feature branch already on origin.

This file is the canonical post-mortem + prevention pattern for TIDY product knowledge. Treat it as part of TIDY's design context, not a one-off project artifact.

## Failure mechanism

A test fixture in `tests/cli/ext/test_publish.py:_init_git_repo` ran:

```python
subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
```

Intent: write `user.name=tester` to the *local config of the temp repo at `path`*.

Reality: when `path` resolves to a location inside a real git worktree (rather than a tmp dir), `git -C <path> config` walks up to find the nearest `.git` and writes to **that** repo's local config. In a multi-worktree layout (axiom uses worktrees for every feature/design branch), the "local" config for any worktree is the **shared common config** at `<bare>/.git/config`. So **all worktrees of the same repo inherit the polluted identity**.

Once `user.name=tester` was written to the shared config, every subsequent commit made *anywhere in any axiom worktree* — including the user's legitimate Phase 2a..6a work — was authored as `tester`. 18 commits accumulated before anyone noticed.

Separately, the same fixture also ran:

```python
subprocess.run(["git", "init", "-q", str(path)], check=True)
subprocess.run(["git", "-C", str(path), "add", "."], check=True)
subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
```

Under the same path-resolution failure mode, this committed *the test scaffold's content* (with `axi ext init` template files like `AGENTS.md`, `pyproject.toml`, `tagged_ext/`) **into the user's real branch**, with the user's last legitimate commit as parent. These commits also *deleted* whatever wasn't in the scaffold's tree — `.github/workflows/`, `.axi/agents/`, `.gitignore`, `.pre-commit-config.yaml`, etc. Pure destruction recorded as new history.

## Why it went undetected for hours

- The polluted commits had legitimate-looking subjects (Phase 2a, Phase 5, etc.) — only the *author* field was wrong.
- `git status` shows working-tree state, not authorship of past commits — never surfaced.
- Pre-push hooks ran the test suite (which itself triggered the pollution) and sometimes blocked, but on test failures, not on author anomalies.
- The user's local config still showed `Benjamin Booth` because the global ~/.gitconfig wasn't touched — only the *repo-local* config (which won) was polluted.

## Prevention pattern (now landed in TIDY)

Three layers, all required:

### Layer 1 — Defensive: fixture isolation

The canonical primitives live in `src/axiom/extensions/builtins/hygiene/_git_isolation.py`:

```python
from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)

def _init_git_repo(path: Path, ...) -> None:
    assert_test_tmp_path(path)
    env = git_isolated_env()
    subprocess.run([..., "git", ...], env=env, cwd=path, check=True)
```

`git_isolated_env()` sets `GIT_CONFIG_GLOBAL=/dev/null` + `GIT_CONFIG_SYSTEM=/dev/null` so any `git config` writes inside the test cannot bleed up to the user's `~/.gitconfig`, the system config, OR — most dangerously — into a parent git repo's local config when the test path resolves inside a real worktree. `assert_test_tmp_path()` is belt + suspenders: even if a caller's path resolution is wrong, it aborts before any git op.

**Every test fixture that runs `git init`, `git config`, `git commit`, `git tag`, or `git push` MUST import these helpers** — there is no legitimate reason for a test to write to the user's git config. Don't re-roll the helpers locally; import from the shared module so future fixtures can't drift from the contract.

#### R2 widening (2026-05-04 evening)

The initial R2 patch covered only `tests/cli/ext/test_publish.py`. A subsequent test sweep revealed that pre-existing helpers in four other test files used the same unsafe pattern (no `env=`, no path guard) — and one of them (`test_worktrees.py`, with `user.name=Test` / `email=test@example.com`) was the actual contamination vector that re-corrupted `feat/tidy-drift-dashboard` during pre-push validation. R2-widening landed the shared `_git_isolation.py` module and routed every contamination vector through it:

| Vector | Helper | Status after R2-wide |
| --- | --- | --- |
| `tests/cli/ext/test_publish.py` | `_init_git_repo` | uses shared module |
| `src/.../hygiene/tests/test_worktrees.py` | `_git` | uses shared module |
| `src/.../hygiene/tests/test_drift.py` | `_run` | uses shared module |
| `src/.../hygiene/tests/test_tidy_discover.py` | `_git_init` | uses shared module |
| `src/.../hygiene/tests/test_tidy_raci_cli.py` | `_git_init` | uses shared module |

### Layer 2 — Detective: TIDY surfaces suspicious authors

`hygiene/drift.py` now scans each branch's commits (between merge-base with `origin/main` and HEAD) for author/committer identities matching:

- name in `{"tester", "test", "pytest*", "fixture", "anonymous", "unknown"}`
- email matching `t@t.test`, `test@test.*`, `pytest@*`, `fixture@*`, `*@example.{com|org|net}`

Branches with any suspicious commit get `suggested_action="quarantine-suspicious-authors"` regardless of drift severity, and the decision packet shows offending SHAs + authors + subjects. The dashboard's `SUSP` column makes the count visible at a glance:

```
BRANCH                          SEV     A/B     DIRTY   AGE   PR   SUSP   ACTION
feat/twin-build-phase-0         stale   24/57   286     0d    none 18     quarantine-suspicious-authors
```

### Layer 2.5 — Self-healing: auto-undo stray-fixture commits (landed 2026-06-11)

Detection alone wasn't enough: the session-end guard in `tests/conftest.py`
*detected* HEAD movement but only warned, so a fixture that committed into the
real worktree (the `Test <test@example.com>` "seed" pattern) left the branch
corrupted — and it recurred on every full-suite / pre-push run because nothing
undid it.

The guard now **auto-heals**: at session end, if HEAD moved AND every commit in
the moved range was authored by a known pollution identity
(`tests._pollution_guard.all_commits_are_pollution`) AND the worktree has no
uncommitted *tracked* changes (`git status --porcelain -uno` empty), it
`git reset --hard` back to the session-start snapshot. Otherwise it surfaces
loudly with the manual recovery command.

**Load-bearing safety rule (learned the hard way):** `git reset --hard` undoes
the pollution commit but ALSO destroys uncommitted edits to tracked files — during
development of this very fix, an unguarded `reset --hard` wiped the guard's own
uncommitted edits. So auto-heal is gated on a clean *tracked* worktree.
Untracked files are deliberately ignored (`-uno`) because `reset --hard` leaves
them untouched, so they carry no data-loss risk and must not veto a heal. **Never
`reset --hard` a worktree with uncommitted tracked changes to "clean up" — stash
or refuse.**

### Layer 3 — Process: don't push polluted history

When TIDY says quarantine, **do not push, PR, or merge** until the history is rewritten. Recovery playbook:

1. Identify the last clean commit (`git log --author='!tester' --format='%h %an' -20`).
2. Branch off that commit: `git checkout -b feat/<name>-clean <last-clean>`.
3. Cherry-pick legitimate commits with corrected authorship:
   ```bash
   git cherry-pick <sha> --author="Benjamin Booth <ben@b-treeventures.com>"
   ```
   (Or `git rebase -i` with `exec git commit --amend --author=... --no-edit` per commit.)
4. Force-push **only when** the branch is unique to one author and not yet PR'd. If a PR exists, coordinate with reviewers first.
5. Re-run `axi hygiene stat drift --branch <name>` to confirm zero suspicious commits remain.

## Audit query

Any time a contamination is suspected, run:

```bash
# Per-repo scan
git log --all --author='tester' --format='%h %ai %an %d %s'

# Per-branch scan (used by TIDY's automated check)
git log --author='tester' --format='%h' <branch> | wc -l
```

Cross-branch sweep across a workspace:

```bash
axi hygiene stat drift --workspace ~/Projects/<workspace>
# Look for nonzero SUSP column.
```

## Open items

- ~~Self-healing: auto-undo stray-fixture commits~~ **DONE 2026-06-11** (Layer 2.5 above).
- Pre-commit hook variant: refuse to commit when `git config user.name` matches a suspicious pattern (catches it at commit time, not just at branch-audit time).
- Pre-push hook: refuse to push branches with any suspicious commits without an explicit `--allow-suspicious-authors` flag.
- Periodic M-O routine: emit a daily drift digest including SUSP column so this category of pollution surfaces within hours, not after a multi-commit accumulation.
- **Lint guard for R2 contract:** add a ruff/grep-based check that fails CI when `subprocess.run([..., "git", ...])` appears in a test file without an `env=` keyword — the simplest mechanism for keeping new fixtures on the shared isolation primitives.
