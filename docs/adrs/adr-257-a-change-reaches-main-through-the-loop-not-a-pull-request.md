# ADR-257: A change reaches main through the loop, not a pull request

**Status:** Accepted (2026-11-12). The goal and every decision in it are the owner's, made 2026-11-12.
**Related:** ADR-045 (earned graduation), ADR-109 (build provenance), ADR-142 (attestation), ADR-169 (synthetic journeys), ADR-170 (release slots), ADR-182 (never down), ADR-197 (the switch contract), ADR-210 (feature flags), ADR-236 (one permission model, proposed), ADR-250 (grants, proposed), ADR-253 (asks), ADR-254 (one switchboard, proposed).
**PRD:** [The loop](../prds/prd-the-loop.md).

## Context

A pull request does five jobs: it proves a change, limits exposure to it, gives a
person a place to judge it, records the approval, and keeps agents off main. In
November 2026 the cost of doing all five in one place became plain. Main needed a
merge freeze to produce one green commit for a release. Changes that passed their own
checks reddened main because they were tested against an older main. Rebase runs
exhausted the hosted CI budget in two days. Finished work waited on one reviewer.

None of the five jobs needs a pull request. Each has a better mechanism, most of them
already designed or built here: test-first development and the pre-push check prove a
change; feature flags limit exposure; release slots and the switch contract remove a
bad release; attestation records a person's judgement; grants bound what an agent may
do. What is missing is the decision to rely on them, the order in which to do so, and
the few pieces that do not exist yet (cohorts, automatic revert, a feedback path,
attestation of a commit).

## Decision

**A change reaches main by a direct push that has passed the loop's gates, and is off
until a flag turns it on. Pull requests are retired tier by tier, each tier only after
its gates exist and have held.**

- **D1. Five mechanisms replace the five jobs.**

  | Job | Mechanism |
  |---|---|
  | Prove the change | Test first; the pre-push check against current main; main checked on every commit |
  | Limit exposure | A flag, default off; rollout by cohort and percentage; a kill switch |
  | Remove a bad change | Automatic revert on main; smoke journeys in the idle slot; slot rollback |
  | A person's judgement | A signature on the commit for what a flag cannot contain |
  | Keep agents bounded | A grant that names repository, tier and expiry |

- **D2. Three tiers, by what a flag can contain.** Tier 0 is documents, tests and
  design assets. Tier 1 is extension and feature code whose effect a flag contains.
  Tier 2 is everything a flag cannot contain: schema migrations, authorization,
  secrets, packaging and pins, CI workflows and hooks, the guards, the flag system,
  and the protected paths in `CODEOWNERS`. A change is the highest tier of any path it
  touches. The tier of a path is declared in one file and computed, never judged per
  change.

- **D3. Gates per tier.** A tier leaves pull requests only when every gate in its row
  is built and has held.

  | Tier | Gates before direct push |
  |---|---|
  | 0 | Content guards before push and on the server; automatic revert on main |
  | 1 | Tier 0's gates; cohorts and kill switch; smoke journeys in the idle slot; the feedback path |
  | 2 | Tier 1's gates; a person's attestation on the commit, checked by the server (before the push for irreversible or outward-reaching paths, within a window for the rest) |

- **D4. New behaviour ships off.** A tier 1 change that adds or alters behaviour
  declares a flag whose registry default is off, with an owner and a review date. A
  change that cannot be put behind a flag is tier 2.

- **D5. Exposure is decided in the record, after main.** Cohort rules, rollouts and
  kill switches live in the database and are evaluated on the server. The node file
  holds only what a node needs before its database is up; where the two disagree the
  platform reports it and raises one alert, and never changes either on its own.

- **D6. Main heals itself.** A commit that breaks main is reverted by the platform
  after one re-run of a known flake. The revert is an ordinary commit that names what
  it reverts and why, and opens an ask for the author. A revert of a revert needs a
  person.

- **D7. Irreversible steps are guarded before they happen.** Anything that cannot be
  recalled once it leaves the machine or the repository (forbidden content, secrets, a
  published package) is checked before the push and again by the server, and a
  release is cut only from a main commit whose smoke journeys passed, under a lock
  that every merging workflow obeys.

- **D8. A person signs; software does not.** Tier 2 acceptance is an attestation
  (ADR-142) with the commit as its subject and the meaning `approved`. This extends
  attestation subjects to a commit; it does not let an agent or a workflow approve.
  The signature comes **before** the push for what a revert cannot undo or what
  reaches outside (migrations, authorization, secrets, release and publishing, CI
  workflows and hooks, the guards), and the server refuses those unsigned. For the
  rest of tier 2 it may come **after**, within a window (a setting; default 24 hours),
  and an unsigned change is reverted when the window closes.

- **D9. An agent pushes only under a grant.** Until grants exist (ADR-250), an agent's
  change arrives as a branch and a person or the lander takes it to main.

- **D10. Graduation is earned and reversible** (ADR-045). A tier moves to direct push
  after a stated number of changes pass its gates with no revert, and returns to pull
  requests after a stated number of reverts in a window. The numbers are settings,
  per repository and per tier, with defaults the owner set on 2026-11-12: 10 clean
  changes to graduate; 2 reverts within 14 days to return.

### Technical design

| Piece | Where | State |
|---|---|---|
| Switch registry, extension-off rule, audit | `features/` (ADR-254) | Written, awaiting landing |
| Cohorts, rollouts, kill switch, explain | `features/` resolver; two tables in schema `flags` (`cohorts`, `rollouts`) beside `flag_overrides` | To build |
| Attribute catalogue | Platform attributes in `features/`; an extension adds its own in its manifest | To build |
| Tier map | One declared file, read by the pre-push check, the server check and the lander | To build |
| Pre-push check | `scripts/hooks/pre-push` | Built; add the tier and the server-side twin |
| Automatic revert | The release extension's main watcher, which reports only today | To build |
| Release lock, head-run truth, flake re-run | The release extension | Written, awaiting landing |
| Smoke journeys in the idle slot | `infra/deploy/bluegreen.py`, the synthetic extension, `spec-release-slots.md` | Slots built; the journey gate designed, not built |
| Feedback reports | A new built-in on the audit and ask paths | To build |
| Commit attestation | The attestation extension, new subject kind | To build |
| Agent grants | ADR-250 | Proposed |

Percentage assignment hashes the flag key with the subject, so assignment is stable
and independent between flags. Resolution order, most decisive first: extension off,
kill switch, user override, account override, rollout rules, node, variant,
environment, default. Every resolved value carries the step that decided it.

## Options considered

- **Keep pull requests and make them faster** (merge queue, tiered reviewers, the
  lander). This is the present course and stays in force for each tier until it
  graduates. It lost as the end state because it shortens the queue without giving
  what a pull request never had: control of exposure after merge, and a path from use
  back to the change.
- **Remove pull requests everywhere at once.** It lost because three gates do not
  exist, and an agent with push rights is stopped today only by branch protection.
- **Trunk with short-lived branches and automatic merge, no flags.** It lost because
  without flags unfinished work must stay off main, which keeps branches long.
- **A third-party flag service.** It lost because evaluation must work on a node with
  no outbound network, and because the rule engine already exists in a product built
  on this platform.

## Consequences

- Finished work stops waiting. The scarce human step is spent only on tier 2.
- Main carries code that is off. Flags become a liability to manage: each has an
  owner and a review date, and stale flags are reported as work.
- The pre-push check and the server check become the real gate, so they must be fast,
  identical, and trusted. A slow or flaky local check is now a defect of the first
  order.
- Tier 2 keeps a human in the path by design. Its volume is the measure of how much of
  the platform a flag cannot contain, and reducing it is ongoing work.
- Review tiers, the lander and the merge queue remain the rule for every tier that has
  not graduated.
- Follow-up work: the phases in the PRD; an amendment to `spec-flags.md` with the
  cohort build; an amendment to ADR-142's subject kinds with the commit attestation.
