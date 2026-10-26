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
