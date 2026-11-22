# ADR-134 — Severity carries consequence, and consequence is declared

**Status:** Proposed
**Date:** 2026-09-26
**Related:** spec-case-construct §3 (`rollup`), §4 (invariants), ADR-126
(typed decision receipts), ADR-135 (graduated autonomy)

---

## Context

A case's severity is the worst status among its claims. That is a join on
the status lattice, which is what makes `rollup` composable at any depth —
a case, a group of cases, a group of groups — without a new rule at each
level. The formal property is load-bearing and this ADR does not give it
up.

The problem is what status measures. The five values are **epistemic**:
`green` was checked and held, `unproven` was asserted with nothing that
could check it, `stale` has nothing recent, `failed` is contradicted,
`unknown` has never been spoken to. Every one of them describes *what we
know about a claim*. None describes *what happens if it is true*.

For a homogeneous population that never shows, because every endpoint has
the same consequence. Forty picking robots, one bad pick costs one bad
pick. The defect only appears when unlike things sit at one site under one
accountable person — which is the case a farm, a plant, or a reactor fleet
actually is.

Rendered from `receipts.simulation.farm_at_scale`, three cases, all
`failed`, ranked identically and ordered by entity id:

```
cam-04:  raising alarms that are usually wrong
zone-11: watering past its window, right now
trc-05:  left gaps in work already recorded as done
```

A camera barking at deer, a valve flooding a field *while the operator
reads the line*, and a tractor whose missed rows will not surface until
the crop shows it. The flooding one sorts second because the sort fell
through to alphabetical. No amount of better wording fixes that: the
ordering has no input that distinguishes them.

## Decision

**Severity becomes a pair — `(consequence, status)` — ordered
lexicographically, and consequence is DECLARED per `(entity_kind,
claim_kind)`, never computed.**

Three parts, and the third is the one that keeps the spec honest.

### 1. Consequence is a declared, bounded scale

A small closed set, ordered, declared alongside the condition it belongs
to and reviewable in one table:

| consequence | means |
|---|---|
| `harm` | people, or a licensed obligation |
| `loss` | material, irreversible within the operating window |
| `waste` | recoverable cost, or work that must be redone |
| `noise` | degrades attention and nothing else |

Four values, not a number. A score invites tuning, and a tuned threshold
is the thing this construct refuses everywhere else (spec §2, Handling).
A deployment that disagrees with a default declares its own, and that
declaration is reviewable rather than inferred from history.

### 2. The join is preserved, because a product of two lattices is a lattice

Severity is now the least upper bound over **both** components: the worst
consequence among a case's claims, and the worst status. A product order
of two finite total orders is itself a lattice, so `rollup` remains a join
and stays composable at any depth. The formal argument in spec §3 is
extended rather than abandoned, which is the whole reason for the pair
form rather than a weighted blend.

Lexicographic, consequence first: a `waste`-class `failed` never outranks
a `harm`-class `stale`. That ordering is a claim about what deserves
attention and it is stated here so it can be argued with.

### 3. Absent consequence is stated, never assumed

A condition with no declared consequence gets `unknown`, which sorts
**above** `noise` and **below** `waste`, and the surface says the
consequence has not been declared. It is not silently treated as harmless
and not inflated to dangerous.

This follows the construct's existing rule that absence is written rather
than left blank (spec §4.3). It also makes the gap visible at exactly the
moment somebody would be misled by it, which is how the undeclared
conditions in the farm fixture surfaced at all.

## Consequences

**The day can be ordered.** The valve sorts first because flooding a field
is `loss` and a miscalibrated camera is `noise`, not because a human
happened to write a better sentence.

**Two vocabularies must not drift.** Consequence lives on the condition,
beside the headline and the remedy, so the one table that says what a
condition MEANS also says what it COSTS. A second table would be a second
thing to get out of step.

**It is a new declaration burden, and it will be skipped.** Every
`(entity_kind, claim_kind)` now wants a consequence, and the honest
expectation is that deployments will not fill them in. That is why the
undeclared case is explicit and visible rather than defaulted — a silent
default would make this worse than not having it, because the ordering
would look considered while being arbitrary.

**It does not make severity a risk score, and must not become one.**
Consequence is what happens if the claim is true; likelihood is nowhere in
this and does not belong. The moment somebody multiplies them we are
running a tuned model of importance and have lost the property that every
ranking input is a checkable declaration.

**Existing severities change.** Anything already declared keeps its status
ordering within a consequence class, but cross-class ordering moves. The
migration is a table to fill, not a schema change.

## Alternatives considered

**Leave severity epistemic and sort by consequence in the surface.** The
surface would then rank differently from the construct, and two answers to
"what is worst" is exactly the class of defect this program keeps finding
(the brief's own projections disagreeing with each other). Rejected.

**Infer consequence from reach.** Reach counts declared dependents, which
is a proxy at best: a lighting circuit can have more dependents than an
irrigation valve and matter far less. It would also make consequence
computed rather than declared, which the construct refuses for anything
that orders a person's attention.

**A numeric priority.** Tunable, unarguable, and drifts. The closed set
can be disagreed with in a review; a number can only be nudged.
