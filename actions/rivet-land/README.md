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
| `issue_comment`, `pull_request_review` | `/rivet merge`, `/rivet option <X>`, `/rivet hold <reason>`, `/rivet rerun`, `/rivet close`, `/rivet exempt <reason>`. Only the owner's commands are honoured. |

## Idle exit: nothing to do costs almost nothing

The first step, `rivet_idle.sh`, runs before Python, the install, the cache and
the App token. On `schedule`, `workflow_dispatch` and `workflow_run` it counts the
repository's open pull requests (drafts included, since RIVET reports on drafts)
and open `RIVET: needs` issues, both with the `github-token` input. It sets the
output `idle=true`, skips every later step, and writes one line to the job summary
(`RIVET lander: no open pull requests; nothing to evaluate.`) only when both
reads succeed and both counts are exactly zero.

- Any doubt runs the full path exactly as before: the call fails, GitHub rate
  limits, the answer is not a plain count, `gh` is missing, or the step itself
  crashes (`continue-on-error`).
- An open `RIVET: needs` issue is never idle, because the full run is what
  closes it once nothing is waiting. (The search matches the default title
  prefix; a custom `[reminders] issue_title` that does not start with
  `RIVET: needs` is not seen by this check, and its issue is closed on the next
  run that finds a pull request.)
- `pull_request`, `pull_request_review` and `issue_comment` events never go
  idle and never make the extra calls: they are about one pull request.
- What a zero-pull-request run did before, and now defers to the next full run:
  create the two RIVET labels if missing, remove a leaked `rivet:claimed` label
  from closed pull requests, and validate the config (`verify_config.py`). None
  of these affects a pull request: a pull request event runs the config check
  and the labels before anything is evaluated.
- The kill switch and release lock inputs are untouched. They reach the lander
  step whenever it runs.

When the full path runs, pip's wheel cache is restored and saved under an exact
key that names the pin and the ISO week (see "When the caches are busted").

## Skip when nothing changed

After the idle check, one GraphQL read (`rivet_state.sh`) takes the open pull requests
(head, draft, review decision, mergeability, merge state, labels, check rollup, auto-merge,
`updatedAt`) and the open `RIVET: needs` issue. They are hashed together with the kill switch,
release lock, `act`, owner, pinned `axiom-version`, the SHA-256 of the repo's config file, the
SHA-256 of this action's own files plus `github.action_ref`, and a time bucket
`floor(now / unchanged-max-age-hours)`. `actions/cache/restore` with `lookup-only: true` asks
whether `rivet-seen-<hash>` exists, and downloads nothing. A hit skips every later step and
the job summary says nothing changed. Only `schedule` and `workflow_run` look up;
`workflow_dispatch` always evaluates in full; `pull_request`, review and comment events are
never skipped and never save.

- **Save only after a full, normal finish.** The key read before the run and the key of the
  state the run left (RIVET's own comments change `updatedAt`) are saved only when the lander
  step succeeded. A run that errors midway records nothing, so a failed first run after a bust
  can never leave a "seen" entry behind. A hit never saves.
- **Fail open.** A query error, odd output, more than 100 open pull requests, a missing hash
  tool or a cache error leaves the key empty or misses, and the full path runs.
- **Scope.** Caches belong to the repository that runs the workflow and to the branch (ref).
  The scheduled and `workflow_run` runs use the default branch, which pull request runs can
  read but do not write here. One repository never sees another's keys.
- `skip-unchanged: "false"` turns the skip off. `unchanged-max-age-hours` (default `6`) is the
  time window: a full evaluation happens at least this often even when nothing changed, so
  clock rules (reminders, stale sweep, claim expiry) still run. Six matches the lander's own
  `window_hours` of 6, and with the cron every 3 hours (`17 */3 * * *`) as the backstop a time
  rule waits at most 6 hours. The window value and the bucket `floor(now / window)` are both in
  the key, so changing the setting busts every entry. Keep the window above the cron interval:
  at the same interval every run falls in a new bucket and no scheduled run skips.

  Why 6, from a replay of 7 days of real runs in three repositories that call this action (change events: PR open/close, reviews, human
  comments, labels, PR workflow starts and ends, main commits; skipped when none since the last
  full run and the same bucket):

  | Repo | Hourly cron, 3 h window | Hourly cron, 6 h window | Cron every 3 h, 3 h window | Cron every 3 h, 6 h window |
  | --- | --- | --- | --- | --- |
  | Repo A | 61 of 132 (46%) | 74 (56%) | 0 of 49 (0%) | 9 (18%) |
  | Repo B | 72 of 139 (52%) | 90 (65%) | 0 of 50 (0%) | 15 (30%) |
  | Repo C | 69 of 133 (52%) | 86 (65%) | 0 of 44 (0%) | 16 (36%) |

  Limits: change events are counted generously but some real changes (external statuses) are
  not observable, which would lower the skip share; one week, three repos.

### When the caches are busted

| Cache | Key | Busted by |
| --- | --- | --- |
| pip wheels | `rivet-pip-<os>-py<python>-axiom<axiom-version>-<ISO week>` | a new pin (immutable per version) or a new ISO week, so a fixed dependency is picked up within 7 days and a bad entry cannot outlive a week. Exact key only: no `restore-keys`, which would mix versions. It stores downloaded wheels only. |
| unchanged gate | `rivet-seen-<sha256>` | any change to the pull request snapshot, needs issue, kill switch, release lock, `act`, owner, pin, config content, this action's files or ref, or the time bucket. |

Manual bust, one command for everything RIVET cached:

```
gh cache list --key rivet-          # see the entries
gh cache delete --all --succeed-on-no-caches   # careful: every cache in the repo
gh cache list --key rivet- --json id --jq '.[].id' | xargs -n1 gh cache delete
```

The last line deletes only RIVET's entries. To skip both caches for one run without
deleting anything, dispatch the workflow with `force-full: true` (the caller example has the
input). GitHub evicts an entry unused for 7 days, and the oldest first when a repository passes
10 GB; these entries are a few bytes and self-expiring. Never rely on one existing: every miss
is a full run, which is always safe.

Tier 0, before a pull request is treated as failing: a required check whose
latest conclusion is `CANCELLED` is re-run once per head SHA
(`gh run rerun <id> --failed`). Checks named in `[reminders] rerun_check_names`
are re-run once per pull request, as before. Set `rerun_cancelled = false` in
the config to turn the cancelled path off.

A pull request lands only when it is green, not a draft, touches no protected
path, has no `do-not-merge` label, and has no pending or changes-requested
human review. A person who turned auto-merge off holds it too.

Two things in the example caller cost nothing and matter: the cron is
`17 */3 * * *` with `unchanged-max-age-hours: "6"` (the schedule is a backstop; `pull_request` events already
evaluate each push, and a run that finds open pull requests takes minutes), and
the job has an `if:` that skips an `issue_comment` or `pull_request_review`
event whose body has no `/rivet`, so no runner starts and no minute is billed.

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
| `axiom-version` | `0.70.51` | Pinned `axiom-os-lm` release from PyPI. Bump it deliberately. |
| `config` | `.axiom/release-lander.toml` | Config path. Keys: `rerun_check_names`, `rerun_cancelled`, `check_name`, `issue_title`, `sticky_marker`, `waiting_label`, the remind hours, `urgent_*`. |
| `github-token` | `github.token` | Token for `gh`. Merges made with the default token do not start other workflows; pass a PAT if you need that. |
| `python-version` | `3.11` | Python for the scripts. |
| `skip-unchanged`, `unchanged-max-age-hours`, `force-full` | `"true"`, `6`, `"false"` | The unchanged skip, its time window in hours, and a one-run bypass of both caches. |
| `app-id`, `app-private-key` | empty | The RIVET GitHub App (`vars.RIVET_APP_ID`, `secrets.RIVET_APP_PRIVATE_KEY`). Its merges start main's own CI; merges made with the Actions token do not. Empty keeps `github-token`. |
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

## Batching gates

A batching gate fails a pull request because older open pull requests change
the same code. Rerunning it changes nothing while they are open. When the
checks named in `[reminders] batching_check_names` are the only failing checks:

- RIVET reads the gate's failure annotation and takes the pull request numbers
  it names. It repeats the gate's verdict and does not recompute it.
- Nothing blocks the older pull requests: the comment says which ones this is
  queued behind. The owner is not asked and the pull request is not on the
  "needs" issue.
- An older pull request is a draft, conflicted, failing or held, or the
  annotation names none: the owner is asked, and the recommendation is the
  exemption.
- The gate is rerun when an older pull request closes, becomes a draft or goes
  into conflict, once per change.
- `/rivet exempt <reason>` adds `batch_exempt_label`, records the reason in a
  comment and reruns the gate. RIVET never adds the label by itself.

The gate must write its verdict as an `::error::` annotation that names the
blocking pull requests as `#<number>`.

## Kill switch

Set the repository variable `AXIOM_LANDER_KILL` to `1` (the caller passes it as the `kill-switch` input; a composite action cannot read `vars`). Every merge stops. The
report, comments and the hold check still run. Unset it to resume. Landing is
also off when `[raci] auto_allowed` lacks `merge`, or when `act` is `false`.

## Tests

`pytest actions/rivet-land/tests` (collected by the repo's `testpaths`).
