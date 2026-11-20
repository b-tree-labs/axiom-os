# ADR-242: CLERK writes progress from cited evidence, and never writes dates

**Status:** Draft
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md), [ADR-162](adr-162-the-watcher-primitive.md), [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md), [ADR-166](adr-166-program-membership-is-a-principal-not-a-tracker-seat.md), [ADR-173](adr-173-estimation-is-deterministic-the-model-proposes-a-size-never-a-date.md), [spec-program](../specs/spec-program.md) §Tracking from GitHub across many repositories

## Context

Until now the program feeders only detected: they overlaid tracker facts into
a namespaced block and reported findings, and every human-committed field
(`owner`, dates, `status`, `pct`) was written only by a person. A program
tracked from GitHub across many repositories produces a steady stream of
evidence (linked issues closing, pull requests merging), and asking an owner
to restate a percent the evidence already shows is the busywork that makes a
tracker go stale. At the same time, a date is a commitment a person makes to
other people, and a percent that moved with no stated reason cannot be
trusted. Once CLERK writes a field, consumers (the status page, the forecast,
follow-ups) start relying on it, so which fields an agent may write is hard to
take back.

## Decision

CLERK may write an item's `pct` and a new `delivery` state (`not_started` |
`in_progress` | `done`) from evidence, and only from evidence it cites: every
such write records `progress_basis[field]` (the basis sentence and the
evidence URLs) and the change-log entry carries the same citation. Writes
only move forward (a percent rises, delivery steps ahead); anything else is a
`progress_proposal`. A deployment opts in with `program.evidence_updates:
"apply"`; the default is `"propose"`. CLERK **never** writes a date or the
`status` commitment: a date the evidence disagrees with is a `date_proposal`
finding for the item's owner.

Evidence rolls up to an item only through an explicit link written in the
work (tracker reference, URL, or a `Program:` line). Unlinked work is a
finding, never inferred.

## Options considered

- **Detect only (status quo).** Safe, but the percent goes stale and owners
  repeat what the evidence already says.
- **Write everything the evidence suggests, including dates.** Fastest to a
  "live" chart, but it moves commitments other people planned against without
  their owner, which is the failure ADR-173 already ruled out for estimates.
- **Infer links from text similarity or authorship.** More coverage, but a
  wrong guess silently inflates or deflates progress; an explicit link is
  cheap to write and auditable.

## Consequences

- The status page and change log show progress that follows the work, each
  change with a reason a reader can check.
- `delivery` joins the schema and `delivery_changed` joins the closed change
  vocabulary (ADR-165).
- Owners receive date proposals through the existing drift and `changes`
  surfaces; accepting one is an ordinary owner edit.
- Follow-up: let an owner accept a proposal in one step (`axi program item
  accept-proposal`), and decide whether a closed program issue alone should
  mark `done` when no work evidence exists.
