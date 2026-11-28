# ADR-136 — Uncertainty is a platform primitive, carried as an affine form

**Status:** Accepted
**Date:** 2026-09-28
**Supersedes:** none
**Related:** ADR-128 (medallion tier boundaries), ADR-131 (there is always a
medallion), ADR-132 (fault taxonomy), ADR-134 (severity carries consequence),
ADR-135 (graduated autonomy)

## Context

Every surface this platform ships reports numbers that came from
measurement, and none of them could compose the uncertainty of those
numbers. `silver.signals` had carried an `uncertainty` column since it was
designed — in the value's own unit, with NULL meaning unreported, on the
explicit reasoning that *zero is a claim of perfect precision*. That column
was good and it was not enough, for two reasons that only appear when
something tries to use it.

**A scalar cannot be composed.** Two readings from one instrument share its
calibration error. Averaging them as independent claims a precision the
instrument cannot deliver, and the arithmetic has no way to know: the two
scalars are indistinguishable from two independent ones. Every aggregation,
every derived channel, every difference between two sensors is exposed to
this, and the error is always in the same direction — overconfidence.

**A number without its measurand is undefined.** GUM is unambiguous that an
uncertainty is a statement about a specific quantity being measured. A bare
`0.5` does not say 0.5 of what, evaluated how, traceable to what, valid over
what range. Those omissions are what make an uncertainty unauditable, and an
unauditable uncertainty is worse than none because it invites reliance.

The immediate driver was a serving-boundary defect: conform writes
`uncertainty: None` by default and the gold aggregation maps `mean → avg`
without touching uncertainty at all. Whatever an ingest declared was dropped
before anybody could read it.

The generalising driver is that extensions will contribute uncertainty in
ways nobody can enumerate in advance, so the platform has to state a
contract rather than a schema.

## Decision

### D1 — Uncertainty is platform-level, not an extension

It lives in `src/axiom/uncertainty/`. Not because it is core to any one
subsystem but because *every* subsystem contributes to it, and a primitive
that several extensions must agree on cannot be owned by one of them.

### D2 — The carrier is an affine form over globally named sources

A quantity is `x₀ + Σ aᵢεᵢ` where each `εᵢ` is an independent unit-variance
source with a globally unique name. Standard uncertainty is `√(Σaᵢ²)`;
correlation between two quantities is `Σaᵢbᵢ / (u_a·u_b)`.

This is affine arithmetic (Comba & Stolfi 1993; de Figueiredo & Stolfi
2004), adopted for exactly the property the founder asked for: it is
**closed** under addition, subtraction and scaling, so composition to
arbitrary depth needs no new machinery and no declarations. Correlation is
*computed from shared symbols* rather than stored, which means it cannot go
stale, cannot be forgotten, and cannot be declared wrong.

The consequence worth stating plainly: a difference between two readings
from one instrument correctly shows the calibration cancelling, and a mean
of them correctly refuses to average it away. Neither behaviour is
special-cased. Both fall out of the symbols.

### D3 — Symbols are namespaced per extension

`<extension>:<resource>:<aspect>`, validated by regex. `signals:tc-14:repeatability`,
`data_platform:resample:step_hold`, `model_corral:surrogate-v3:truncation`.

Two extensions cannot collide, and an extension can mint sources for
resources the platform has never heard of. Sharing a symbol means sharing a
physical source — which is a real modelling decision, and the one most
likely to be got wrong, so it is explicit.

### D4 — Composition never consults the registry

Budgets (measurand, evaluation type, traceability, validity range) live in a
registry, and the arithmetic does not read it. A map composes correctly when
the declaring extension is absent, uninstalled, or newer than this node.
Metadata enriches a report; it is never load-bearing for a number.

### D5 — Absence has three kinds, and they are not the same

- **Structured** — symbols known. Correlation computable. Exact composition.
- **Magnitude only** — `u` known, correlation unknown. Yields **bounds**.
- **Unquantified** — nothing reported. **Counted**, and left outside the
  bound, because a bound that silently excluded a contribution would be a
  false bound.

This is ADR-132's fault taxonomy at one level down, and the same reasoning
as *a value without its unit is not a fact*: absence is written, never blank.

### D6 — A bound must state its premise

For magnitudes `uᵢ` under *any* correlation, the achievable standard
deviation lies in `[max(0, 2·max(uᵢ) − Σuᵢ), Σuᵢ]`. Root-sum-square is the
floor **only under non-negative correlation**. Since that premise is usually
true and never guaranteed, a combination carries both the RSS-based `low`
and the unconstrained `low_unconstrained`, plus the `premise` string naming
the assumption. Both endpoints are proved attainable.

### D7 — A companion table, not a widened row

Per-signal uncertainty terms go in a companion table keyed to the signal
row, not in extra columns. `GOLD_SIGNALS_BASE_COLUMNS` order is frozen and
`CREATE OR REPLACE VIEW` may only append, so widening the row for an
open-ended set of sources is not available. A companion table also matches
the cardinality: one value has many sources.

### D8 — Pipeline uncertainty is declared in named stages

`src/axiom/uncertainty/pipeline.py` names the stages of the shape that
recurs across every consumer — measured input, conditioning, model form,
numerical, surrogate, domain, configuration, validation.

Stages rather than one number because **they fail differently and are
reduced differently**: input uncertainty shrinks by measuring better,
numerical by refining, surrogate by training harder, model form only by
changing the model, and extrapolation not at all. Collapsing them loses the
one thing an engineer needs, which is where to spend the next hour.

The taxonomy is named for the *kind of ignorance* each stage introduces and
not for any domain, which is what lets one implementation serve a physics
surrogate, a process-line twin and a yield forecast without change.

### D9 — Validation is a constraint on the total, not another term

This one was got wrong first and is the least obvious decision here.

A validation comparison **measures** a model's total error. The declared
stages **account** for where that error comes from. Adding the measurement
to the account in quadrature double counts, because the measured
disagreement already contains the numerical, input and model-form error
present at the compared conditions.

So a validation stage contributes only `√(u_val² − Σu_declared²)` — the part
the account does not explain. With the account complete this reproduces the
measured figure exactly. With the account already wider than the
measurement, validation adds nothing and the account stands, because a model
may not be quoted better than its own account of itself either.

The shortfall is the most useful number in the module. It is ASME V&V 20's
real diagnostic: when the measurement exceeds the account, a genuine error
source is missing from the budget, and refining the largest declared term
cannot close it. Because an incomplete account is a different *class* of
finding from a large-but-understood term, it leads the reported advice
whatever its rank.

### D10 — Applying a measured correction imports the uncertainty of that correction

A bias correction quoted as exact is a systematic error wearing a fix. The
standard error of the correction is carried as its own term. It shrinks with
the number of comparisons; the residual scatter does not, and conflating the
two is how a model gets quoted an order of magnitude too confidently.

## Consequences

**Good.**

- Composition is closed, so an extension nobody has written yet composes
  correctly with everything shipped.
- Correlation is structural. Double-counting a derived channel is
  arithmetically impossible rather than merely discouraged.
- Overconfidence has to be declared to occur. The default is honest.
- A served value now has an audit trail to a traceable source, which is
  what metrological traceability means and what a regulator asks for.
- The same primitive serves decider calibration (ADR-135) and prediction
  intervals: both are claims about claimed confidence against observed
  reality.

**Costs, stated.**

- Every contributing extension has work to do. A symbol nobody mints is a
  source nobody can see, and the honest report of that is the
  `Unquantified` count.
- The affine form grows a term per source. Wide aggregations need
  compaction; nothing here does that yet.
- Nonlinear operations mint a residual symbol by construction, which is a
  linearisation and is labelled as one. Strongly nonlinear transforms want
  GUM-S1 Monte Carlo, not this.
- The bound under unknown correlation is wide, correctly and unhelpfully.
  The fix is declaring provenance, and the tests show declaring it
  *tightens* the answer — which is the incentive pointing the right way.

**Not decided here.**

- Coverage validation — whether stated intervals achieve their stated
  coverage — is the same operation `calibrate` performs on deciders and is
  not yet wired to uncertainty.
- Propagation through the gold aggregation is the open serving-boundary
  defect this ADR describes and does not yet fix.
- `make_uncertainty_notice` in the RAG extension is about answer grounding.
  Different concept, colliding name, to be renamed.

## The family rule

Two rules already hold in this portfolio: *a value without its unit is not a
fact*, and *a ratio without its reference is not a fact*. This ADR adds the
third and last of the family:

> **An uncertainty without its correlation structure is not composable.**

All three say the same thing at different depths. A number carries the
context that makes it mean something, or it is not a number you may compute
with.

## References

- JCGM 100:2008 (GUM) — propagation with covariance; Type A and B; the
  measurand; coverage factor; the uncertainty budget
- JCGM 101:2008 (GUM-S1) — Monte Carlo propagation
- ASME V&V 20-2009 — validation uncertainty for simulation; `E = S − D`
- Comba & Stolfi 1993; de Figueiredo & Stolfi 2004 — affine arithmetic
- Vovk, Gammerman & Shafer — conformal prediction, for the surrogate link
