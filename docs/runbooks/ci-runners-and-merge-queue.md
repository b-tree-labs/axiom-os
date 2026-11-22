# CI runners and the merge queue

How this repository's CI spends minutes, where the heavy jobs run, and the
settings changes behind it. The runner system itself (GCP project, Cloud Run
control service, VM image, guards) lives in `b-tree-labs/ci-runners`; its
README is the operations runbook for the GCP side.

## Where each job runs

| Workflow | Job | Runner |
|---|---|---|
| CI | Smoke, Install-Path Tests | `[self-hosted, btl-gcp, gcp-4]` when `CI_RUNNER=gcp`, else `ubuntu-24.04` |
| CI | Unit Tests (3.13), Integration Tests | `[self-hosted, btl-gcp]` (gcp-8) when `CI_RUNNER=gcp`, else `ubuntu-24.04` |
| CI | Lint, Changed Paths, Migration, Container Smoke, Build, publish | `ubuntu-24.04` |
| Node chart | chain, zero-gap, archive, zero-gap-serving, zero-gap-overlap | `gcp-4` when `CI_RUNNER=gcp`, else `ubuntu-24.04` |
| Node chart | lint, generator, migrations, changes, publish-runtime | `ubuntu-24.04` |
| Semgrep, pip-audit, RIVET lander, mirror, ... | all | `ubuntu-24.04` |

A pull request from a fork never routes to the GCP runners: the repository
does not run fork pull-request workflows, forks get no variables, the
`runs-on` expression requires the head repository to be this repository, and
the control service refuses any run whose head repository differs.

### The switch

```bash
gh variable set CI_RUNNER -R b-tree-labs/axiom-os-private --body gcp   # heavy jobs to GCP
gh variable delete CI_RUNNER -R b-tree-labs/axiom-os-private          # back to GitHub-hosted
```

The switch takes effect on the next run and costs no CI run of its own. If
the GCP side is down (jobs sit queued), delete the variable and re-run.

`tests/test_ci_cost_controls.py` locks the routing: every heavy job carries
the variable and the head-repository guard, and falls back to
`ubuntu-24.04`.

## When the suite runs

- **Pull request:** Lint + Smoke, then Unit Tests narrowed by testmon to the
  blast radius of the diff (full suite when a conftest, pyproject, lock file
  or fixture changed, or past a third of the suite). Docs-only and draft pull
  requests short-circuit every test job at the step level, so the required
  checks still report success. A newer push cancels the older run.
- **Merge queue:** the full suite once per merge (Unit, Integration,
  Migration, Install-Path, Build).
- **Push to main:** skipped when a merge-queue run already passed the same
  tree (`tag-already-green`).
- **Node chart:** on a pull request, the heavy jobs run only when the chart,
  images, smoke scripts, deploy code, the compose file or the workflow
  changed; a migrations-only change gets lint, the generator and the
  migration guard. A draft skips the heavy jobs until it is marked ready.
  Every heavy job runs on main pushes to those paths, and nightly at 07:17
  UTC while `CI_RUNNER=gcp` (without the GCP runners the nightly runs the
  light jobs only, since the heavy set is about 80 hosted minutes).

## The merge-queue ruleset

**Not available on the current plan.** GitHub offers merge queues on private
repositories only under Enterprise Cloud; on Team the API rejects the rule.
Until then the brakes are: `strict` off (no update-branch re-runs), testmon
narrowing on pull requests, and the main-push run on the GCP runners.

Definition: `.github/rulesets/main-merge-queue.json` (squash, ALLGREEN, up to
5 entries per group, 90-minute check timeout; required: Lint, Unit Tests
(3.13), Integration Tests, Install-Path Tests; repository admins may bypass
through a pull request only). Integration and Install-Path are skipped on
pull requests, and a skipped job reports success, so requiring them blocks
nothing before the queue and gates for real in it.

Apply (repository admin), only when no release pull request is mid-flight:

```bash
gh api -X POST repos/b-tree-labs/axiom-os-private/rulesets \
  --input .github/rulesets/main-merge-queue.json --jq '{id, name, enforcement}'
```

Update an existing one: `gh api -X PUT repos/b-tree-labs/axiom-os-private/rulesets/<id> --input ...`.
Remove: `gh api -X DELETE repos/b-tree-labs/axiom-os-private/rulesets/<id>`.

With the queue on, land with `gh pr merge <n> --squash --auto` (enqueues once
checks pass). A direct `gh pr merge` without `--auto` is refused for anyone
without bypass. RIVET's lander must enqueue the same way.

## Change log

| Date | Setting | Change | By |
|---|---|---|---|
| 2026-10-26 | Branch protection `main` | `strict` up-to-date requirement off | owner-approved |
| 2026-10-30 | Repository variable `CI_RUNNER` | set to `gcp` (only workflows carrying the routing read it) | owner-approved |
| 2026-10-30 | Ruleset `main merge queue` | NOT created: the API refuses the `merge_queue` rule (422). Merge queues on private repositories need GitHub Enterprise Cloud; b-tree-labs is on Team. The definition stays for an upgrade. | |

## Measured on the GCP runners (2026-10-30, PR #1518)

| Job | GitHub-hosted (2 vCPU) | GCP | Ratio |
|---|---|---|---|
| Full unit suite | 48 min job, 45:42 pytest, 4 workers | 16 min job, 11:54 pytest, 16 workers on gcp-8 | 0.33x |
| Node chart heavy jobs (14) | 3.1 to 8.4 min each | 3.3 to 9.2 min each on gcp-4 | 0.79x to 1.21x |
| Smoke | 1.3 min (warm venv cache) | 3.0 min on the first run (cold cache) | warms after one run |
| Queue to start | seconds | about 45 s (VM boot) | |

Cost per run: a pull-request CI run $0.04 (was about $0.16), a Node chart run
$0.11 (was about $0.47), a full unit suite on gcp-8 about $0.025 (was about
$0.27 hosted). gcp-4 is the smallest c3d shape and every chart job stays
within 1.3x of hosted on it.

A spot preemption mid-job fails that job ("the self-hosted runner lost
communication"); re-run it with `gh run rerun <id> --failed`. Seen once in
the first 40 jobs.

The VM runs the runner user with a systemd user manager and Yama ptrace
scope 1, like the hosted image; tests that read the host (systemd-run
--user, /proc) depend on both.

## Cost reference (2026-10-30)

Measured since 2026-10-25 on GitHub-hosted runners: a CI run bills about 28
minutes (Unit 17, Integration 3, Smoke 2, Install-Path 1, the rest small);
a Node chart run about 83. October: $277.79 for about 49,000 Linux minutes
(about $0.0057 per minute). GCP spot `c3d-standard-8` is about $0.10 per hour
all-in (VM, disk, IP), `c3d-standard-4` about half; billed per second.
