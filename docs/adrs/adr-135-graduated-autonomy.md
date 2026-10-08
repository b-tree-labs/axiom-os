# ADR-135 — Graduated autonomy: fitness for the mission, maturity, and a ceiling

**Status:** Proposed
**Date:** 2026-09-26
**Related:** spec-case-construct §2 (Solver / Decider), §3 (`calibrate`),
receipts.handling (`never_worked`, `reserved`), ADR-134 (consequence)

---

## Context

The construct already has one autonomy rule, and it is a good one:
`never_worked` holds a fix back until it has cleared this case once, and
lets it run unwatched thereafter. Autonomy earned by track record rather
than granted by somebody deciding it is safe — the gray area measured
instead of adjudicated.

Three things have since shown that one rule is not enough.

**A track record is about the past, and some work is about tonight.** A
tractor that worked six fields correctly last week tells you little about
whether it should work a field tonight in fog on a degraded position fix.
Drone light shows — the closest mature analog to a fleet of autonomous
endpoints — do not use track records at all. Every unit proves fitness
**immediately before use**: battery, GPS lock, compass, motor test. Units
that fail are swapped on the ground. They fly thousands of endpoints with
one operator on that basis.

**A track record on units is not evidence about the fleet.** "Push new
weights to all forty" has no track record, because it has never been the
same decision twice. A hundred good per-unit calls say nothing about the
simultaneous act.

**Some authority is not earnable at all.** Where accountability is
personal and non-delegable — a licensed operator, a signing engineer — no
number of correct calls confers it. `authority_reserved` already ships for
this and was added ahead of this ADR; this document is where it is
justified rather than merely present.

## Decision

**Autonomy is granted by three independent gates, all of which must pass,
and each of which answers a different question.**

| gate | question | evidence | who declares it |
|---|---|---|---|
| **Fitness** | is it fit for THIS mission, now? | a check run immediately before acting | the capability, per mission kind |
| **Maturity** | has this pairing earned it? | the record — `calibrate` over (solver, condition) | measured, never declared |
| **Ceiling** | may it ever be automatic? | `Condition.authority_reserved` | declared, per condition |

Failing any gate means a person decides. The three are not a score and
must never be combined into one: they fail for different reasons and a
reader needs to know which.

### Fitness is per mission, not per endpoint

"Is rbt-12 healthy" is the question a fleet tool answers. The question
here is "is rbt-12 fit to pick in the cold aisle for the next four
hours" — a pairing of endpoint and intended function. Fitness is checked
at the moment of use and its result is a claim like any other, so a stale
fitness check reads as `stale`: *you can no longer tell whether it is fit*.

This is the drone-show import, and it is strictly stronger than a track
record for anything whose conditions change between uses.

### Maturity is measured per (solver, condition), never per solver

The unit of trust is not the model and not the endpoint: it is the pairing
of who is judging with what they are judging (spec §3, `calibrate`). rbt-12
may be mature at picking and immature at labelling on the same weights.
A maturity level that averaged over conditions would license the thing it
is worst at.

Maturity is a reading of the record, not a setting. Nobody promotes an
endpoint; the record does, and can demote it when the reading changes.

### The ceiling outranks both

Declared per condition, checked first, and never retired by evidence. A
ceiling that any amount of good history could lift is a threshold wearing
a different word.

### Consequence sets the bar, not the rule

ADR-134's consequence class determines *how much* maturity is required and
*how recent* a fitness check must be — a `harm`-class action needs a
fitness check minutes old and a long record; a `noise`-class one may need
neither. The gates are the same everywhere; the bar is a function of what
happens if it is wrong.

## Consequences

**Autonomy becomes explainable in one sentence per gate.** "It has the
record but not a current fitness check" is actionable. A single score
saying 0.72 is not.

**The maturity matrix is a reading, so it can go down.** An endpoint that
drifts loses autonomy without anybody revoking it, which is the property
that makes unattended operation defensible at all.

**Fitness checks are work nobody is doing today.** This ADR creates an
obligation — every capability that wants unattended operation must declare
what proving fitness means for it — and the honest expectation is that
most will not, in which case they stay attended. That is the correct
failure direction and it will be unpopular.

**It does not make a model safe.** These gates decide whether a *decision*
runs unattended. They say nothing about whether the underlying model is
good, and a fleet that passes all three can still be wrong in a way
nothing here detects. Anyone reading this as a safety case has read it
wrong.

**Nothing here is built yet except the ceiling.** `authority_reserved`
ships; fitness and maturity do not. Stating them now is what stops the
next feature inventing a fourth mechanism.

## Alternatives considered

**Track record alone** — what ships today. Fails on missions whose
conditions change between uses, and on acts that have never happened
before.

**A trust score per endpoint.** Tunable, unarguable, averages over exactly
the conditions that matter, and cannot say which gate failed. It is the
shape everybody builds and the reason nobody trusts the output.

**Human approval for everything.** Honest, and it makes the economics
impossible — the entire premise of a fleet is that one accountable person
answers for many units. Demanding per-act approval concedes that.
