# Pull request lander

Status: Draft. Warn mode only.

## Why

An open backlog spends a full suite on every pull request, including
duplicates and batches one session already opened. RIVET should show
what can be folded, what can be closed, what a person can approve, and
what a person has to decide, before anyone spends another run.

## What it does

- Group open pull requests and name the ones that look superseded.
- List pull requests that are ready for a person to approve, and the
  ones that need a person because of the paths they touch, a conflict,
  a draft, an unknown check, or a repair cap.
- Estimate the suite minutes those choices would save.
- From the existing heartbeat, record a proposal. The proposal does not
  merge and does not enqueue.

## CI cost and waste

A budget that runs out mid-day refuses every job, release included, and
nobody sees it coming. RIVET reads month-to-date spend and its projection
every hour, names where the minutes go and which of them are waste, and
says so while there is still budget to act on.

- Detect: spend, projection, percent of budget, minutes by repo, workflow,
  event and job, and waste signals (update-branch re-runs, superseded runs,
  selection fallbacks, flakes, chronic red branches, duplicate main runs).
- Fix what is safe: cancel superseded runs; update only the pull request
  that lands next.
- Hold to the budget: at warn, land in batches and refuse blind retries;
  at stop, land only releases and alert.
- Prevent: `ci lint` warns on workflow shapes that waste minutes, before
  each land and on demand. A weekly digest names the top waste.
- Every alert names the one thing RIVET did and the one decision left.

Thresholds are data in `[ci_health]`. The surface is `axi release
ci-health`, the MCP, and chat. See the spec's CI health section.

## What it does not do

- Merge, enqueue, force-push, or post a required check.
- Close pull requests. Superseded rows are advice.
- Talk to a forge other than to read, and only when a repo is named.
- Apply a repair that has not been approved for this head and this diff.
  A repo on the watch list has one lander. Outside actors deliver a fix
  on a side branch; they do not merge.

## Safety

Approval is bound to a head SHA. A new push resets it. The lander and
the auto-closer do not act on the same pull request. Behavior, pricing,
security, data, and test-setup paths route to a person.
