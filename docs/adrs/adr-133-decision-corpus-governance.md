# ADR-133 — The decision corpus is governed before it is aggregated

**Status:** Proposed
**Date:** 2026-09-25
**Supersedes:** none
**Related:** spec-case-construct §7, ADR-126 (typed decision receipts),
ADR-130 (federated site identity), ADR-027 (federated memory)

> Numbering note: `scripts/lint_adr_numbers.py --next` returned 132, but
> it reads only the working tree. 131 is claimed by an open PR and 132 by
> a sibling session's in-flight sensing-fault taxonomy, so this takes 133.

---

## Context

The oversight record emits, with no additional work, a tuple per decided
case: the state, the actions that were available, the one taken, what it
ran, and an outcome observed later rather than asserted. It also keeps
the actions **not** taken and the refusals. That is an unusually complete
decision corpus — most operational logs carry neither counterfactuals nor
independently observed results — and its value per example is
correspondingly high.

Two consequences follow immediately, and both are hard to reverse.

The first is that the corpus becomes worth aggregating across sites long
before it becomes large in absolute terms. A single site produces
hundreds of decisions a year; the interesting statistics ("this remedy
resolves this condition N% of the time") need many sites.

The second is that **collection cannot be undone**. A record that
crosses a site boundary has crossed it. A decision to gather first and
work out the terms afterwards is not a decision that can be revisited,
which is precisely the class of choice an ADR exists for.

There is also a standing conflict of interest in the platform's
ownership: the same platform serves sites that pay nothing and sites
that pay, and aggregating one's operational record to improve or sell a
service to another is a conflict that cannot be resolved by a default
setting.

## Decision

**1. Nothing raw crosses a site boundary.** Cross-site aggregation
carries derived statistics only — counts, rates, distributions over a
declared population. Individual claims, evidence strings, entity
identifiers, principals and free-text notes stay at the site that
produced them. This rides the existing federation shape (declared peers,
classification, compatible accounts) rather than a new path.

**2. Consent precedes collection, per site, in writing.** A site's
record is aggregated only under an explicit agreement naming what
statistics leave, at what granularity, to whom, and for what. Absence of
a refusal is not consent. This is checked before a boundary crossing,
not audited after one.

**3. The corpus is not licensed for third-party model training.** Its
value rests on the outcomes being independently observed and not
publicly available; selling them destroys the property that made them
worth selling. Derived artifacts — a benchmark, calibration statistics,
an assurance report — are the sellable form.

**4. Outcomes stay independently observed.** No model, agent or operator
may write an outcome, at any scale. The reflexivity failure — a model
trained on decisions this system recorded, whose decisions this system
then records, laundering its own priors into evidence — has exactly one
structural defence, and this is it. It is already invariant §4.2 of the
construct; this ADR extends it explicitly to aggregated use.

**5. Censoring is marked, never dropped.** A case decided but not yet
resolved, a held case, an acknowledged one — each has no outcome, and
that absence is a labelled state in any emitted dataset. Dropping them
teaches the reader that everything a person touched resolved.

**6. A benchmark keeps a permanent hold-out and never publishes its
items.** Continuous regeneration from live outcomes is the defence
against contamination; not publishing is the defence against Goodhart.
A benchmark whose items are known is a training set.

## Consequences

**We can say what we do not do.** The prohibitions above are sellable in
a regulated setting, where "your operational record is not our training
data" is a question that gets asked and usually answered evasively.

**Aggregation costs more to build.** Derived-statistics-only means the
statistics must be specified up front and computed at the edge; we
cannot gather raw records now and decide the questions later. That is
the intended trade.

**Some questions become unanswerable.** Any analysis needing raw
cross-site joins is out of scope by construction. If one is genuinely
required it needs its own ADR and its own consent, not an exception
here.

**The near-term work is unaffected.** Calibration measurement, the
regression golden set, precedent retrieval and a single-site benchmark
all operate inside one site's boundary and need none of this machinery.
That ordering is deliberate: prove the thesis where no governance is
required, and design the boundary before anything crosses it.

**This ADR must be revisited before the first cross-site collection**,
not after. If the statistics turn out to need a granularity this
forbids, that is a new decision and supersedes this one in writing.
