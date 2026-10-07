# ADR-148 — Reliance is what the serving rule forbids, and reading is not reliance

- **Status:** Proposed — 2026-09-30
- **Context owner:** data platform
- **Amends ADR-128.** ADR-128 stands in full; this narrows one term in it —
  what "a serving path must not read the working tiers" forbids — and makes the
  narrowing checkable. It supersedes nothing.

## Context

ADR-128 established the medallion boundary and, crucially, made it enforceable:
`SERVING_TIER = "gold"`, `WORKING_TIERS = ("bronze", "silver")`, and a guard that
greps for a serving path touching either. It was written because two serving
paths *had* read `silver.signals` directly — the partner-facing ingest summary
and the freshness report — and one of them re-derived, worse, an answer gold
already gave. SENNA sat four months stale underneath that. The rule earned its
place.

It defines serving as:

> anything whose output reaches a person, an agent, an API, a figure or a
> report — a count for a status line is serving, and so is a chart's provenance
> string.

**That wording forbids something we now need.** A person writing a bronze→silver
conform extension cannot do the work without reading bronze and silver: they
need to see what arrived, what shape it is, and whether their transformation
produced what they expected. Doing that through our own MCP tools, `axi data`,
or `neut chat` puts the output in front of "a person, an agent, an API" — so by
the letter of ADR-128 the only supported way to develop a conform pass is to
bypass the platform and open a database client. That is worse in every respect,
including the one ADR-128 cares about: it is unlogged, ungated and unlabelled.

Ben, 2026-09-30: *"people should be able to introspect, they should be able to
see what's there, validate it, but not have any kind of important dependency,
however we want to formally define it."*

Defining it formally is the whole of this decision.

## Decision

**The serving rule forbids RELIANCE on the working tiers, not reading of them.**

### Reliance, formally

A read **relies** on the data it returns if **any** of the following holds.

- **R1 — It outlives the request.** The result, or anything derived from it, is
  written to a table, a file, a cache, an index, a memory, a document, or a
  message that persists after the response.
- **R2 — It is asserted about the world.** The result is presented as a fact
  about what happened — a temperature, a power, a count of events, a status —
  rather than as a fact about the store.
- **R3 — It moves something.** The result determines an automated action: an
  alert, a gate, a dispatch, a promotion, a control output, a scheduling
  decision.
- **R4 — It is inherited.** The result is an input to a computation that itself
  relies, by R1, R2 or R3. Reliance is transitive and closes upward.

A read that satisfies none of R1–R4 **introspects**. It is permitted on every
tier.

### The distinction in one line

**Reliance is a claim about the world; introspection is a claim about the
store.**

`silver.signals holds 4,000 rows for this channel, in degC, none since Tuesday`
is a claim about the store. `The fuel temperature was 21.4 degC` is a claim about
the world. A developer who looks at the value 21.4 while checking a conform pass
is making the first kind of claim about the second kind of number, and that is
exactly the case this permits: they are asking *what does the store say*, not
*what was true*.

### What introspection must carry

Every working-tier read returns, with the result and not beside it:

- `tier` — which tier answered.
- `contract: none` — bronze is what arrived and silver is mid-derivation, so the
  shape may change whenever a conform pass changes, without a version.
- The same provenance a gold read carries: source, method, row count, and the
  access filter that was applied.

### How it is enforced

ADR-128's guard is retargeted, not removed. It no longer asks *did anything read
a working tier*; it asks **did anything that serves consume a working-tier
result**:

1. A working-tier read is reachable only through the declared introspection
   capability, and that capability tags its result.
2. A serving path must refuse a tagged result. The tag travels, so R4 is
   enforced structurally rather than remembered — a serving path that received a
   working-tier answer through three hops still sees the tag.
3. The static guard now names the *serving* modules and forbids them importing
   or invoking the introspection capability, which is a narrower and more honest
   check than grepping for the word `silver`.

**The tag is the enforcement.** If a label were only a convention, this ADR would
be an erosion of ADR-128 rather than an amendment to it — that concern is why
the guard is specified here and not left to implementation.

### What reliance entitles you to

Ben, in the same conversation: the platform needs SLAs, and reliance is what
ties to them. It does, and the tie is the cleanest justification this amendment
has:

**An SLA is offered exactly where reliance is permitted.**

- Gold is the tier you may rely on, so gold is the tier we owe something about —
  freshness, availability, shape.
- The working tiers carry `contract: none`, which is not a hedge. It is the
  precise statement that **no service level is offered**, which is *why* they can
  be readable without becoming a commitment.

This resolves what would otherwise be the strongest objection to opening bronze:
that a readable thing becomes a depended-on thing. It becomes one only if we
promise something about it, and here we say in the response itself that we do
not.

The levels themselves are not this ADR's to set — see
`docs/specs/spec-data-platform-service-levels.md`.

### What does not change

- `SERVING_TIER = "gold"` stands. Anything that answers still answers from gold.
- The transformation rules, the allowances and the tier-choice rule in ADR-128
  are untouched.
- Access control is unaffected and is a separate axis: the tier is a *maturity*
  boundary, and who may read *which* rows remains `access_tier`, filtered per
  query and recorded in the provenance.

## Consequences

**Good.** Conform development becomes a supported path rather than a reason to
bypass the platform, and it becomes a *logged* one — a read through the
capability carries its principal, where a psql session carries nothing. The
guard gets more precise: it now forbids the thing ADR-128 was actually worried
about, instead of a proxy for it.

**Costs.** There is now a rule that a reviewer must apply rather than a word a
grep can find; R2 in particular is a judgement. The tag must be threaded through
every path that could carry a result toward serving, and a path that drops it is
a silent hole — so the tag's propagation needs its own test, not a convention.

**The risk worth naming.** "Validate it" and "answer from it" are adjacent. A
developer checking a conform pass in `neut chat` gets a plausible answer about
the world drawn from silver, and nothing stops them pasting it into a report.
The tag makes that visible; it cannot make it impossible. That is accepted: the
alternative is an unlogged psql session, where it is neither visible nor
recorded.

## Open

- **Is R2 checkable at all, or only reviewable?** The other three are mechanical.
- **Does an agent's context count as persistence under R1?** A tool result sits
  in a transcript that outlives the request, which reads as reliance by the
  letter and not by the intent.
- **What does a chat surface do with a tagged result** — refuse it, or show it
  with the label attached? This decides whether `neut chat` can answer "what is
  in bronze for this source" at all.
