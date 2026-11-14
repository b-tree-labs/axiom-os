# Pull request lander

Status: Draft. Warn mode only.

RIVET's acting work, retries, storm calming and its contract with KEEL are planned in
[prd-agent-backed-capabilities-and-subagents](../../../../../../../docs/prds/prd-agent-backed-capabilities-and-subagents.md)
under ADR-237. RIVET is one agent on the shared harness; that table is the sequence.

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
- Tell blind from green: no repos, no pipelines, an empty target or unreadable
  data is reported as blind or as an error, never as healthy.
- Say when main is red: an urgent alert at once with the incident and the
  culprit, repeating until green, once per spell and interval.
- Watch from outside: a job on the forge reads main and keeps one incident, so
  a dead local watch does not mean silence. It states what it cannot see.

Thresholds are data in `[ci_health]`. The surface is `axi release
ci-health`, the MCP, and chat. See the spec's CI health section.

## What it does not do

- Force-push, bypass a required check, or post one. The report path does not
  merge or enqueue; landing is the workflow's separate act step, from main.
- Close pull requests. Superseded rows are advice.
- Talk to a forge other than to read, and only when a repo is named.
- Apply a repair that has not been approved for this head and this diff.
  A repo on the watch list has one lander. Outside actors deliver a fix
  on a side branch; they do not merge.

## Safety

Approval is bound to a head SHA. A new push resets it. The lander and
the auto-closer do not act on the same pull request. Behavior, pricing,
security, data, and test-setup paths route to a person.
