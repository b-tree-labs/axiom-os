# ADR-173: Estimation is deterministic and self-calibrating; the model proposes a size, never a date

**Status:** Accepted (2026-10-06)
**Related:** [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md) (the change log throughput is measured from), [ADR-171](adr-171-adaptivity-every-external-system-is-an-optional-additive-connector.md) (the read path touches only local data; the model is an additive proposer), [ADR-172](adr-172-products-are-the-composable-spine-up-a-ladder-to-a-north-star.md) (dependency edges are the gates; products are what gets forecast), [ADR-136](adr-136-uncertainty-is-a-platform-primitive.md) (`axiom.uncertainty`), `docs/specs/spec-uncertainty.md`, `docs/prds/prd-program.md` (R19, R20, R22), `docs/specs/spec-program.md` (§Forward design)

## Context

The most common program-management question is "when will X be ready," asked in
many permutations from many perspectives. A language model answers it badly: it
ignores resourcing, head-count, dependency gates, and measured pace, and it emits
a confident single date with no basis. The model is good at exactly one narrow
judgement in this space — "this new thing is about the size of those past things."
The decision is where to draw the line between the deterministic machine and the
model, so the forecast is trustworthy and still low-friction.

A second failure to avoid: a forecast that demands a form be filled in. A form
nobody fills in is a tracker nobody trusts — so the resourcing and sizing inputs
have to come from data the system already holds, with the human asked almost
nothing.

## Decision

**The date math is deterministic; the language model never invents a date. It
only proposes a t-shirt size, and rarely asks one short question.**

- **The forecast is computed:** measured throughput × resourcing, over sized
  remaining scope, along the dependency gates (ADR-172's `depends_on`).
  Throughput is read from the change log (ADR-165) per person / lane / size and
  kept as a **distribution**, not a single velocity, so the answer carries spread;
  it is anchored to delivered history and sharpens as work closes
  (**self-calibrating**).
- **The model's only inputs are a proposed size and, rarely, one question.** It
  proposes a t-shirt size (XS–XL) from similar past items — one-tap confirm,
  optional; an unsized item takes the proposal. It never computes a duration.
- **Resourcing is auto-pulled** from the data (owner, assignees, lane); the tool
  asks at most one or two questions, and only where the data is silent (the
  99%-automated target).
- **The answer is a range with its stated assumptions**, carrying its uncertainty
  via `axiom.uncertainty` (never a scalar error bar where the sources are known),
  never a false-precise single day. The range is the default; the how-computed is
  drill-down.
- **Calibrated sizes flow back** into the tracker's estimate / weight fields,
  consent-gated.

This is the ADR-171 posture on the read path: the forecast reads only local data
(the data file plus the change log), makes no external call, and works with
nothing else present; the model is an *additive* proposer whose absence degrades
to "an unsized item takes a default," never a broken forecast.

## Options considered

- **Let the model estimate the date.** Lowest effort, and the failure the whole
  ADR exists to refuse: no basis, ignores resourcing / gates / pace, false
  precision. Rejected.
- **Pure deterministic, no model at all** (require a human size on every item).
  Deterministic and trustworthy, but it is the form nobody fills in. Rejected: the
  model's size proposal is what keeps it frictionless.
- **A single velocity, not a distribution.** Simpler math, but it throws away the
  spread and produces a false-precise point. Rejected against the uncertainty
  discipline.
- **Deterministic math + model-proposed size + measured distribution (chosen).**
  The machine owns the arithmetic and the calibration; the model owns the one
  judgement it is good at; the human confirms a size and almost nothing else.

## Consequences

- Every permutation — "product ships," "lane clears," "how soon with one more
  person" — is the same deterministic forecast, run on the baseline or on a
  scenario fork (the spec's §Forward design; scenarios are ADR-171's
  no-new-machinery forks of the data).
- The forecast's quality rises automatically as the change log grows: the system
  gets better at answering by measuring the result of its own past answers (the
  R22 recursive-calibration loop).
- Consent-gated write-back means the tracker's own estimate fields improve without
  manual entry, but never silently — the calibrated size is proposed, not forced.
- What this commits us to: throughput must stay measurable from the change log, so
  the change-kind vocabulary's `status` / `item` history (ADR-165) is load-bearing
  for the forecast, not only for "what changed."
- The model is kept on a short leash deliberately: the one place it touches the
  forecast — a bounded, optional, auditable size proposal — is what lets the
  forecast be called deterministic.
