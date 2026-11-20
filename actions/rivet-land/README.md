# rivet-land

One composite action that runs RIVET, the pull-request lander, in any repo.
It replaces the per-repo copies of `rivet-lander.yml`. It is an action and not
a reusable workflow because `.github/workflows/` is not part of the public
mirror and `actions/` is.

## What it does, per event

| Event | Behaviour |
| --- | --- |
| `pull_request` | Reads that one pull request. Posts or updates one sticky comment, keeps the neutral `RIVET` check, runs the hold check. Fork pull requests are skipped (the token is read-only). |
| `schedule`, `workflow_dispatch`, `workflow_run` | Reads the open set, lands what the config allows, and opens or updates one issue, `RIVET: needs <owner>`. |
| `issue_comment`, `pull_request_review` | `/rivet merge`, `/rivet option <X>`, `/rivet hold <reason>`, `/rivet rerun`, `/rivet close`. Only the owner's commands are honoured. |

Tier 0, before a pull request is treated as failing: a required check whose
latest conclusion is `CANCELLED` is re-run once per head SHA
(`gh run rerun <id> --failed`). Checks named in `[reminders] rerun_check_names`
are re-run once per pull request, as before. Set `rerun_cancelled = false` in
the config to turn the cancelled path off.

A pull request lands only when it is green, not a draft, touches no protected
path, has no `do-not-merge` label, and has no pending or changes-requested
human review. A person who turned auto-merge off holds it too.

## Adopt

1. Copy `release-lander.example.toml` to `.axiom/release-lander.toml` in your
   repo. Set `self_landers`, `[raci] approvers`, `[human_paths]`, and
   `rerun_check_names`.
2. Copy `caller-example.yml` to `.github/workflows/rivet-lander.yml`. Set
   `owner` and the tag.
3. Optional: set `RELEASE_FIX_WEBHOOK_URL` and `RELEASE_FIX_WEBHOOK_KEY` as
   `env` on the calling step to enable the fixer handoff. Unset, green pull
   requests still land.

The caller must check the repo out first: the config is read from the working
tree. Grant the permissions shown in the example; each is used (`checks: write`
for the neutral check, `actions: write` for reruns).

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `owner` | required | GitHub login who gets the "needs" issue and whose `/rivet` commands count. Overrides `[reminders] assignee`. |
| `act` | `"true"` | `true` lands per the config's `[raci] auto_allowed`. `false` reports only: nothing is merged. |
| `axiom-version` | `0.70.41` | Pinned `axiom-os-lm` release from PyPI. Bump it deliberately. |
| `config` | `.axiom/release-lander.toml` | Config path. Keys: `rerun_check_names`, `rerun_cancelled`, `check_name`, `issue_title`, `sticky_marker`, `waiting_label`, the remind hours, `urgent_*`. |
| `github-token` | `github.token` | Token for `gh`. Merges made with the default token do not start other workflows; pass a PAT if you need that. |
| `python-version` | `3.11` | Python for the scripts. |
| `openrouter-api-key` | empty | Tier-1 fixer key (`secrets.OPENROUTER_API_KEY`). Empty: no fixer. |
| `fixer-model` | `z-ai/glm-5.3` | OpenRouter model id for the fixer. GLM-5.3 is open-weight and served by US hosts (DeepInfra, Together, Fireworks, CoreWeave, Parasail, Bedrock); allow only US providers in the OpenRouter account. |
| `fixer-routing-tier` | `public` | Data tier the OpenRouter provider is cleared for. A repo whose `[fixer] routing_tier` is `export_controlled` finds no provider and its fixer stays off. |

## Three tiers

1. **Deterministic, free.** Land green pull requests, re-run cancelled and
   flaky required checks, ignore RIVET's own checks.
2. **Tier-1 fixer** (`fix_provider = "llm"`, `[fixer]`). A cheap routed model
   repairs lint, config and failing-test blockers in a worktree through a fixed
   tool set: no shell, no git, no secrets, no protected path, no workflow.
   RIVET pushes the fix to `rivet-fix/<pr>-<sha>` and lands it only on green.
   Two failed attempts per pull request, then tier 2.
3. **The owner.** The pull request is listed on the "RIVET: needs <owner>"
   issue. Hand it to a person or a stronger model from there.

Set the OpenRouter account to no training and zero data retention, and allow
only providers you trust with the code.

## Kill switch

Set the repository variable `AXIOM_LANDER_KILL` to `1` (the caller passes it as the `kill-switch` input; a composite action cannot read `vars`). Every merge stops. The
report, comments and the hold check still run. Unset it to resume. Landing is
also off when `[raci] auto_allowed` lacks `merge`, or when `act` is `false`.

## Tests

`pytest actions/rivet-land/tests` (collected by the repo's `testpaths`).
