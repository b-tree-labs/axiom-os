# ADR-121 — Grounding is computed by the gate, not judged by a model

**Status:** Proposed — 2026-09-21
**Owner:** @ben
**Related:** ADR-113 (deterministic quantitative answering), ADR-117 (skill
conformance steward), prd-evals §5.4/§5.6, spec-output-provenance-gate

## Context

We have committed to showing that an assistant is measurably better with this
platform configured than without. That claim needs a number, and the number most
worth having is how often an answer states something its evidence does not
support.

The published domain work measures this with **human expert annotation**. It
tried automatic schemes first — citation-string matching and embedding
similarity — and reported them as brittle, replacing them with expert-verified
definitions. That is the rigorous answer, and it is why their numbers are
trustworthy.

It is also why their benchmark is 27 queries. Expert annotation does not scale
to running on every change, and a measurement taken rarely stops catching
regressions between the times it is taken.

We have something with a different trade. The output-provenance gate (ADR-113)
already decides, per answer and deterministically, whether every value stated is
supported by that turn's evidence. It runs in production on every turn; it is
not an evaluation artefact bolted on afterwards.

This is **not a better instrument than expert annotation** — it is a cheaper and
narrower one. An expert can tell whether a claim is true. The gate can only tell
whether a stated value is supported by the evidence present. What the gate buys
is that it costs nothing per item, so it can run continuously, which is the
regime where regressions are actually caught.

The consequence of getting this wrong is not academic. A judged rate cannot gate
a release, because the same run scores differently twice. A judge is also a cost
per item, which pushes a benchmark toward being run rarely — and a benchmark run
rarely is a benchmark that stops catching regressions.

## Decision

**Hallucination rate is computed by the output-provenance gate. No model grades
it.**

Concretely:

- An answer is counted as hallucinated when the gate does not ground it. The two
  failure modes the gate already separates are reported separately: a value the
  evidence does not support, and a claim that a tool produced a value when no
  tool ran. The second can carry a *correct* number and still mislead, because
  what a reader relies on is the claim about where it came from.
- **An abstention is never a hallucination.** Declining to state a value that
  cannot be grounded is the behaviour the gate exists to produce. Counting it
  would turn the metric into an argument for switching the gate off.
- Metric names follow the published literature (context precision, citation
  precision, citation hit, hallucination rate, retrieval recall) so a score here
  can be set beside a published one rather than living in a private dialect.

**Judgement is retained only where a rule would be dishonest.** Holding a
correct value under user pressure, and refusing a sensitive request, are not
mechanically decidable from a transcript. Those stay with a rubric-based judge,
and the split is explicit rather than implied — claiming to grade them
deterministically would be a green check that cannot fail.

**The platform grades behaviour; the consumer owns the questions.** An item
declares which behaviour is correct — answer, abstain, refuse, hold ground,
verify with a tool — and the platform grades against that declaration without
knowing the subject matter. Domain pools stay in the consumer layer.

## Consequences

**Good.** The rate is reproducible, so it can gate a release rather than inform
a discussion. It costs nothing per item, so the battery can run on every change
instead of occasionally. And it is a claim a customer can verify themselves,
which is worth more than one they must take on trust.

The specific open questions — claim population, abstentions in the denominator,
false attribution, and the parameters we chose — are catalogued in
``docs/working/grounding-metrics-open-questions-2026-09-21.md``.

**A real cost: comparability is now conditional.** Our number and a published
number are only comparable if the other work counts the same things. Two
questions decide it — whether their rate counts false attribution, and how they
treat abstentions — and we do not currently know their answers. Until we do, a
side-by-side table would be a claim we have not earned. Ask, then compare.

**A narrower scope than it appears.** The gate decides grounding, not
correctness. An answer can be perfectly grounded in retrieved evidence that is
itself wrong. This metric is not a truth oracle and must not be reported as one.

**Reversal is expensive**, which is why this is an ADR. Every historical score
is computed under this definition; switching to a judged rate later invalidates
the series rather than extending it.

## Alternatives considered

**Expert-annotate everything, as the literature does.** Not rejected — deferred,
and it remains the higher standard. It does not scale to per-change gating, so
it is the wrong instrument for a release gate and the right one for periodic
validation. The two are complements: the computed rate runs on every change, and
expert annotation calibrates what it is worth.

**Compute everything.** Rejected as dishonest. Holding ground under pressure is
a judgement, and a deterministic proxy for it would pass runs that a reader
would call failures.

**Invent our own metric names.** Rejected: a private dialect makes our results
uncheckable against anyone else's, which is the opposite of what a benchmark is
for.
