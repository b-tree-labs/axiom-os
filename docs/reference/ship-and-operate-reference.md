# Part 4 agent reference: ship and operate

The lookup page behind [Part 4: Ship and operate](../guides/ship-and-operate.md). It is written
for the coding agent a contributor drives. `AGENTS.md` routes agents here. Facts come from
`.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `.github/workflows/node-chart.yml`,
`.axiom/release-lander.toml`, `src/axiom/infra/deploy/` and `infra/smoke/`; if this page and those
files disagree, the files win, and this page needs a fix.

Use this page to act. Explain to the person in plain words rather than pasting it.

## Contents

1. [Commands](#commands)
2. [Releases](#releases)
3. [Keeping main green](#keeping-main-green)
4. [Deploy and rollback](#deploy-and-rollback)
5. [Operating](#operating)
6. [What needs a person](#what-needs-a-person)
7. [When something goes wrong](#when-something-goes-wrong)

## Commands

| Task | Command |
|---|---|
| The CI state of the head of `main` | `gh run list --branch main --workflow ci.yml --limit 5`, then compare `headSha` with `git rev-parse origin/main` |
| CI health, spend and waste | `axi release ci-health` |
| Release state | `axi release status` |
| Cut a release (maintainers only) | `axi release cut patch` (`--dry-run` first) |
| Does a tag exist | `gh api repos/<owner>/<repo>/git/refs/tags/v<version>` (404 means no) |
| Published versions | `pip index versions axiom-os-lm` |
| Pick up a release in a product clone | `pip install -U axiom-os-lm` in the clone's `.venv` |
| Switch a blue-green role | `python -m axiom.infra.deploy.bluegreen compose --role <role> --to <slot>` (or `k8s`) |
| The zero-gap upgrade proof | `infra/smoke/zero_gap_upgrade.py compose` (CI: `node-chart.yml`, job `zero-gap`) |
| Node health | `axi node doctor`, `axi node vitals`, `axi node drift` |
| Secrets and expiry | `axi vault list`, `axi vault audit --expiry` |
| Who may do what, and what was decided | `axi audit list`, `axi audit graduation` |
| Agents | `axi agents roster`, `axi agents ask <agent> "<request>"` |

## Releases

- **Who:** a maintainer. An agent never tags, releases or publishes for a contributor.
- **How:** `axi release cut <part>` bumps `pyproject.toml`, tags and pushes. The tag must be
  annotated with a body: `publish.yml` uses the body as the release notes and mirrors only written
  notes to the public repository. A lightweight tag publishes with generated notes that stay
  private.
- **What the tag runs:** `publish.yml` builds the wheel, verifies it in a clean environment and
  publishes to PyPI with attestations (`attestations: true`, ADR-109).
- **How a change reaches a product:** only through a published release. A product's `.venv`
  installs `axiom-os-lm` from the index; `pip install -U axiom-os-lm` moves it forward.
- **Release trains:** being designed (aligned majors on a calendar). No ADR yet; do not cite one.

## Keeping main green

- **Required checks:** `Lint` and `Unit Tests (3.13)` on the head commit of the pull request.
- **Head-run truth.** Read the run for the head SHA of `main`, not "the last completed run". The
  `main` concurrency group keeps one running and one pending run, so a burst of merges leaves
  intermediate heads untested. An older red run may not describe `main` now; an older green run
  never does.
- **Explicit success only.** Merge when every required check on the head SHA reports `success`.
  An empty or pending check list is not success.
- **Red main.** The pre-push hook refuses pushes while `main` is red unless the commit carries a
  `Bypass-Reason:` trailer. The one standing case is a push that only fixes the failure; its five
  conditions are in `AGENTS.md`.
- **Merge freeze.** Before a release, maintainers may freeze merges until the tag exists. Open the
  pull request; arm `gh pr merge <n> --squash --auto` only after the tag exists.
- **Generated files.** A test that fails because a committed generated file is stale names the
  script that regenerates it (for example `python scripts/skill_chaining_debt.py`). Regenerate on
  top of the current `main`, not on a stale base.
- **Coming (#1782):** RIVET judges heads by their own runs, merges on explicit success only,
  serializes landings, re-runs a known flake once, and holds a release lock from bump to tag.
- **CI cost.** Never press "Update branch" while checks run, batch related changes into one pull
  request, and keep at most three open.

## Deploy and rollback

- **The rule:** nothing we do takes a service down (ADR-182). A deploy is blue-green where the
  role has slots (ADR-170): start the idle slot, wait for health, send traffic to both, then to
  the new one; keep the old one for rollback.
- **The switch:** `axiom.infra.deploy.bluegreen` prints one JSON line per step and exits non-zero,
  leaving traffic where it was, if the idle slot never becomes healthy.
- **Migrations:** expand only during a switch. A drop, rename or NOT NULL ships in a later
  release, marked `# contract: <what> unused since <version>`; `axiom.infra.deploy.migration_guard`
  refuses anything else.
- **Every switch runs on one runner** (ADR-197): prepare, probe, commit, rollback.
- **Proof:** `infra/smoke/zero_gap_upgrade.py` deploys blue to green and back under live load on
  Compose and K3D, and passes only with 0 refused requests, 0 missing rows and 0 duplicates. It
  needs Docker, Helm and an `AXI` binary; CI installs them in `node-chart.yml`. On a shared
  computer, read the latest run instead of starting it: `gh run list --workflow node-chart.yml --limit 3`.
- **Without slots:** a product's deploy command installs a release beside the previous ones and
  moves `current` only after the install succeeds. Rollback is deploying the previous version
  again. The product's Part 4 names the command.
- **Spec:** [spec-release-slots.md](../specs/spec-release-slots.md) (the Helm and Terraform half is
  designed, not built) and [spec-cicd-and-deployment.md](../specs/spec-cicd-and-deployment.md).

## Operating

| Agent | Owns | Commands |
|---|---|---|
| KEEL | The node: inventory, vitals, doctor, drift, clean, worktrees, maintenance windows (ADR-230; absorbs TIDY and TRIAGE) | `axi node <verb>` |
| RIVET | CI health, releases, landing | `axi release <verb>` |
| KEEP | Secrets, expiry, rotation | `axi vault <verb>` |
| GUARD | Permissions (deny, ask, notify, allow) and the audit trail | `axi audit <verb>` |
| HERALD | Notifications and alerts (ADR-249, ADR-252) | through the other agents |
| PULSE | Schedules | through the other agents |

**The console gap.** No single operations console exists yet ([prd-fleet-console.md](../prds/prd-fleet-console.md)).
Use the commands above, and say so when a person asks for "the dashboard".

## What needs a person

- Tagging, releasing, publishing or deploying to a node people use.
- Bumping a pin in a site repository (a pull request the site's owner reviews).
- Moving a pin backwards. Never; fix the node.
- Arming auto-merge during a merge freeze.
- Rotating, revealing or deleting a secret.
- Stopping a service, or removing a container, network, volume or database you did not create.

## When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| A product clone does not have a change that merged in Axiom | No release carries it yet | Wait for the release, then `pip install -U axiom-os-lm` |
| The hook says `main` is red, but the failure looks unrelated | It read an older run | Check the head SHA's own run before treating `main` as red |
| A pull request merged before its tests ran | Auto-merge read an empty check list as passing | Never arm on an empty list; check the head SHA's checks are `success` |
| A generated-file test fails after an unrelated merge | Another pull request changed what it is generated from | Regenerate on current `main` and push the result |
| A deploy failed | The install or the health check failed | Traffic stays where it was. Read the failing step's output; do not retry blind |
| A rollback is needed | The new release misbehaves | Deploy or switch to the previous version, prove it with `--version`, then report |

See also: [Part 3 agent reference](developer-workflow.md), [FAQ](faq.md).
