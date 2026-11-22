# ADR-171: Adaptivity — the program is self-sufficient; every external system is an optional, additive, gracefully-degrading connector

**Status:** Accepted (2026-10-06)
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (the composed CLERK), [ADR-162](adr-162-the-watcher-primitive.md) (the watcher primitive — the connector seam), [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md) (the self-update core + the read-only source seam), [ADR-166](adr-166-program-membership-is-a-principal-not-a-tracker-seat.md) (membership is a principal, not a tracker seat), [ADR-167](adr-167-the-program-is-editable-through-its-own-tool.md) (editable through its own tool, no hard dependency), [ADR-172](adr-172-products-are-the-composable-spine-up-a-ladder-to-a-north-star.md), [ADR-173](adr-173-estimation-is-deterministic-the-model-proposes-a-size-never-a-date.md), `docs/prds/prd-program.md` (R9, R10, R14, R15), `docs/specs/spec-program.md` (§Forward design)

## Context

The program tool has to serve a wide range of people and deployments from the
same code: a solo operator on a laptop, an air-gapped node, a ten-person
program, a contributor who lives in a wiki and never opens the tracker, a partner
with no account on any system the program uses.

Across the phases a single pattern was already decided piecemeal. The data file
is the authoritative source of truth and self-updates (ADR-165). Membership is a
principal, not a tracker seat (ADR-166). The program is editable through its own
tool with zero hard dependency on any tracker or harness (ADR-167). A source is
used only after a readiness ladder and is skipped loudly otherwise (prd R9). The
extension names no vendor and no domain (prd R10). What was missing was the one
principle those are all instances of — stated once, as the system's defining
posture, so the next feeder, product type, or surface inherits it instead of
re-deciding it.

Two failures force the statement now. First, the **operational** failure: a tool
that breaks, or silently reports stale-as-fresh, when an external system is
absent, unauthenticated, or down. Second, the **adoption** failure: forcing a
contributor onto a new tool instead of ingesting the surface they already work in.

## Decision

**The program tool is self-sufficient. The program data file is the authoritative
source of truth, and every verb works with no external system present.** Every
external system — a tracker (GitLab/GitHub), a wiki, any harness (including
Claude Code), the directory/IdP, the vault — is an **optional, additive,
gracefully-degrading connector**, used only where it is (1) configured, (2)
available, and (3) additive. Absent or unauthenticated, the tool reports the
affected facts **`unverified`** and **skips loudly** — it never rounds unverified
up to synced, and it never breaks.

Two corollaries make it concrete and testable:

1. **Meet people where they work.** A contributor's existing surface — a wiki, a
   repository, issue activity — is ingested as an evidence source (an ADR-162
   watcher instance behind the ADR-165 read-only source seam), rather than a new
   tool they must adopt. The work they already do is the report.
2. **New sources and new product types are data and config, not code.** A new
   feeder is a new source instance; a new product type or lifecycle is a
   deployment declaration (ADR-172). The extension's code names no vendor and no
   domain (R10), so adding a connector or a type never edits the substrate.

## Options considered

- **Require a tracker or a harness as the backbone.** Simplest to build against
  one integration — and the failure this ADR generalizes from ADR-166/167: it
  makes the absent-system case the broken case, excludes the no-account
  contributor, and couples the substrate to a vendor. Rejected.
- **Best-effort integration with silent fallback.** Degrade quietly when a system
  is missing. Rejected: silent degradation reports partial or stale state as if
  whole — the exact unverified-is-not-synced hazard R11 and the platform's
  uncertainty discipline forbid. Degradation must be *loud*.
- **Self-sufficient core plus optional, additive connectors (chosen).** The data
  file is authoritative and complete on its own; connectors only add *verified*
  facts on top, and announce when they cannot.

## Consequences

- Every current and future verb carries a with-nothing-present contract. The
  ADR-167 lifecycle test (a full add → invite → redeem → edit run with no
  GitLab/GitHub/harness and no directory provider) is the template, and each new
  surface ships its own.
- A connector contributes only *verified* facts; an unverified connector
  contributes a loud `unverified` / `skipped` signal, never a silent gap and
  never a false "synced" (R9, R11, the crosslink-health posture).
- The posture is a design test for every future addition: if it cannot degrade to
  absent-and-loud, it is the wrong shape. It governs the product spine (evidence
  is a crosslink that can be unverified, ADR-172), the instrument (scenarios,
  forecast, and shaping are renders over the local data file and change log — no
  external call on the read path, ADR-173), and attachments / deep-links (served
  by the node's own endpoint).
- It is also the **internal-machinery** ethos restated. Just as connectors are
  additive rather than load-bearing, the instrument surfaces are the existing data
  forked and re-rendered rather than a new engine (the "anti-monster" rule) —
  near-zero external dependency and near-zero new machinery are one posture.
- Cost: a connector author must implement the readiness ladder and the loud-skip
  path, not just the happy path. That is deliberate — it is what keeps
  "unverified" honest.
