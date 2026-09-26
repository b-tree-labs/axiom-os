# ADR-126: Typed-decision receipts and the calibration receipt

**Status:** Accepted (2026-09-23 — founder acceptance same day)

## Context

The receipts substrate (fleet console, ADR-119/123; Proof-of-Done and
drift designs, PR #965) records verdicts as status + evidence. The
market has meanwhile standardized a vocabulary for fast probabilistic
judgment — "typed decisions" with "calibrated confidence" ("System One
models", now a category with multiple vendors and open clones). Three
verified gaps in that category: no decision *record*, self-reported
calibration, and screening models that don't treat screened content as
hostile. Separately, the company's graduated-autonomy method (US
provisional 2026-04-21) governs *which decider holds a seat* but
deliberately does not calibrate or type the decision distribution —
an open, adjacent surface. Two decisions below are hard to reverse
because every receipt written under them becomes durable ledger shape.

## Decision

1. **A receipt's verdict is a typed decision.** The platform decision
   record is `TypedDecisionReceipt`: `decision_type`
   (choice | score | boolean), `options`, `chosen`, optional
   `stated_confidence`, `evidence`, `decider` (rule | model(id) |
   human, plus graduation phase when the seat is governed), and the
   later-arriving `outcome`. Deterministic evaluators omit
   `stated_confidence`; the absence renders as information, never as a
   defect. The honesty taxonomy's five states are a choice-type
   instance of this schema, not a parallel system.
2. **Confidence is never evidence.** GREEN-class verdicts require a
   cited observed effect regardless of any confidence carried; no
   threshold on `stated_confidence` may substitute for the evidence
   obligation anywhere in the platform. (A calibrated guess, however
   good, is still a guess — the taxonomy's UNPROVEN.)
3. **Calibration is continuously receipted, per seat, per site.** For
   every probabilistic decider seat, the platform folds
   (`stated_confidence`, `outcome`) pairs into a reliability record —
   stated-confidence-vs-empirical-frequency over a declared window —
   emitted as a first-class receipt kind: STALE when outcomes stop
   arriving, NOISY vs DRIFTED per the drift sentinel's statistical
   bar. A "seat" is a recurring decision position (e.g. tier routing,
   alert triage); reliability attaches to decider-in-seat-at-site,
   never to a model globally.
4. **Screened content is hostile input.** Any probabilistic decider
   whose input includes content being screened runs behind the
   platform's injection posture (local/fail-closed seats for EC paths
   unchanged); its verdicts are model-filled fields and remain
   untrusted identity input.

## Consequences

- F1 verdicts, F3 probe verdicts, graduation-extension decisions, and
  approval-gate outcomes converge on one schema; the console renders
  all of them with the same components (EvidenceRow gains confidence/
  reliability qualifiers — additive, no rework).
- The graduation extension's outcome log becomes the calibration feed;
  a governed seat wears a calibration receipt from its first shadow
  decision.
- Positioning follows structure: vendors publish calibration, the
  platform receipts it against deployment-local outcomes — a surface a
  model vendor cannot occupy, because the outcomes live with the
  deployment.
- Kill criterion (inherited from the accepted design doc): if no
  seat's reliability record ever changes a routing/authority decision
  after F3 dogfood, the calibration receipt folds into drift.

## References

`docs/working/typed-decision-receipts-2026-09-23.md` (accepted design,
sources cited there); ADR-119; ADR-123; `docs/prds/prd-work-verification.md`;
`docs/prds/prd-behavior-drift.md`; Postrule method study + TypeSafe/Jev
deep-dive (session record 2026-09-23).
