# ADR-269: A change lands by its risk tier: Ship, Show or Ask

**Status:** Accepted (2026-11-20)
**Related:** [ADR-064](adr-064-rivet-fix-trivial.md) (RIVET trivial fixes), #2147 (tiered RIVET), #2152 (roadmap), #2102 (commit attestations), RIVET spec `src/axiom/extensions/builtins/release/docs/specs/spec.md`

## Context

A pull request does four jobs at once:

- **It gates.** Tests pass before main.
- **It reviews.** A person reads the change.
- **It bundles a unit to roll back and audit.**
- **It coordinates** people working on the same code.

We open a pull request for every change, so every change pays for all four jobs, including the many where three of them add nothing. The cost showed on 2026-11-19. A same-author series of four SoilMetrix pull requests deadlocked on a batching gate. Green pull requests waited a day for a person to press merge. A fix that was ready sat behind review it did not need.

RIVET now lands green pull requests in every repository we own (#2147). On 2026-11-20 it landed three on its own. It judges a pull request by the checks its branch requires. It never counts its own checks. It reverts a red main. A cheap routed model fixes lint, config and test failures through a fixed tool set, and anything harder goes to a person. That makes it possible to stop treating every change the same.

## Decision

**A change lands by the risk tier RIVET gives it.**

| Tier | What lands this way | How |
|---|---|---|
| **Ship** (the default) | Fixes and flagged features with an attested local run | RIVET verifies the attestation, runs only what the local run could not, posts the required statuses on the commit and fast-forwards main. There is no pull request. |
| **Show** | Behaviour changes behind a default-off flag; large diffs | It lands at once and appears in a daily digest the owner can revert from. |
| **Ask** | The protected paths: money, credentials and security, data migrations, deploys, and export-controlled code | A pull request and a person's approval first, as today. |

Each job a pull request does is replaced, not dropped:

- **Gate → attestation.** The pre-push hook signs the tree hash, the selected tests and their results, and the environment's lock hash (#2102). CI re-runs a random sample of attested runs to keep local runs honest, and it batches queued commits and bisects a failing batch.
- **Review → review by risk.** Ask before landing. Show after landing. Ship never, unless the digest or a revert calls for it.
- **Rollback → flags plus a watch after landing.** RIVET watches main's own run, deploy health and per-function probes after each landing. It reverts on a regression and files the evidence. Release rings (ring advance decided by the Postrule statistical gate) separate deploying code from turning it on.
- **Coordination → trunk.** Small commits, no long-lived branches, and one landing at a time with main green between them.

Autonomy is earned and metered. A repository starts at Ask everywhere and moves to Show and then Ship on measured evidence:

- the risk tier agrees with the owner's own calls on at least 95% of decisions in shadow;
- the revert rate does not rise.

It falls back on its own when an error budget burns. A repository's own rule can hold it at Ask indefinitely, for example the NQA-1 review the reactor code needs.

## Options considered

- **Keep a pull request for every change, and let RIVET only land them.** This is where we are now. It removes the waiting but keeps the cost of opening, reading and closing a pull request for changes nobody needs to read. It also leaves the batching and stacking problems in place.
- **Merge queue only.** GitHub's queue gates and batches, but it still needs a pull request per change and has no notion of risk. It cannot tell a typo fix from a pricing change.
- **Land everything straight to main and review after landing.** This is fast, but it drops the human gate that the protected paths and regulated code genuinely need, and it trusts every change equally.
- **Ship, Show, Ask, decided by RIVET.** Chosen. Review goes where it matters, everything else lands, and every step is measured, so autonomy can widen or shrink on evidence.

## Consequences

- **Easier:** most changes land minutes after they are ready. Review time goes to the changes that need it. There are no batching deadlocks, because the trunk replaces the stack.
- **Harder:** we depend on attestations being honest, on the watch after landing catching what review used to, and on flags being cleaned up. Each needs its own guard: sampled re-runs, synthetic probes, and flag-removal PRs opened by RIVET.
- **Committed to:** the RIVET roadmap in #2152, in order:
  1. R1: RIVET measures itself.
  2. R2: it lands as the `axiom-rivet` App, so landings start main CI (done 2026-11-20); R2b: one landing at a time, with main green between them.
  3. R3: the watch after landing, with automatic revert.
  4. R4: flaky-test quarantine.
  5. R5: the risk tier goes live, with Show and its digest.
  6. R6: attested landing without a pull request (Ship), piloted on postrule.
  7. R7: local Firecracker runners for the heavy suites and export-controlled repositories.
  8. R8: release rings.
  9. R9: the safety nets that move work from Ask to Show.
- **Before any tier change ships:** RIVET's live path runs against a sandbox repository before an action tag or release moves. The 2026-11-20 rollout found five bugs that only a live run showed (an empty comment marker, title keywords, skipped checks, its own neutral check, and an unreadable protection set).
- **Regulated code stays at Ask** until its own quality rules say otherwise. That covers NQA-1 and export control, and every UT reactor repository's fixer stays off.
