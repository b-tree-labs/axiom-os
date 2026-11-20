# Spec — Data platform service levels

- **Status:** Draft — a first stab, deliberately aspirational
- **Owner:** data platform   •   **Last updated:** 2026-09-30
- **Companion to:** ADR-128 (tier boundaries), ADR-148 (reliance)

## Why this exists

We have never said what the platform promises. Partners ask "did my data land?"
and we answer from a report; nobody has ever said how *late* it may be before
that is a failure rather than a Tuesday.

Ben, 2026-09-30: *"our data platform probably needs SLAs … it's aspirational at
this point. We don't have infrastructure, we don't have the proper deployment to
formally support SLAs, but I think we should start thinking about them … even
with a temporary prototype deployment, we could probably
put some SLAs in place and set some expectations."*

## The rule that makes this tractable

**Offer a service level only on something already measured.**

An SLO whose signal does not exist is a wish, and a wish published to a partner
is worse than silence, because it will be believed. So the first question for
every number below is not "what would be good" but "what already tells us".

Three of them already do, which is more than expected.

| Promise | Existing signal | What it does |
|---|---|---|
| **Freshness** | `gold.ingest_freshness` / `gold.ingest_stale` | Carries `typical_gap`, each feed's OWN observed cadence, self-calibrated from its arrivals; flags lag beyond 4× it. A 1/min feed trips in ~4 min, a daily one in ~4 days, with **no per-feed threshold to maintain** |
| **Availability** | the canary | Exercises the install path on a cadence and reports |
| **Shape** | declaration drift (ADR-122) | A standing check that what is declared still matches what is there |

The freshness one is the strong card. It is the rarest property in a monitoring
system: a threshold that needs no human to set it, and therefore does not rot.
It also already caught the failure that motivated it — a site four months stale
under a report working as designed, because nobody had declared a cadence.

## Scope — what a level is offered ON

Per ADR-148: **service levels are offered on gold and on nothing else.**

Bronze and silver are readable, and every response says `contract: none`. That is
the whole commitment: none. It is not a disclaimer bolted on, it is what lets
them be open at all.

## A first stab

Deliberately few, and each traceable to a signal that exists.

### S1 — Freshness

> **A gold feed is fresh when its lag is within 4× its own observed cadence.**
> We aim for **95% of declared feeds fresh, measured hourly**, and for a feed
> that goes stale to be **visible in the freshness report within one cadence**.

*Why this shape:* it self-calibrates, so it stays true when a feed's rate
changes. *What is missing:* nothing technical. The 95% is the aspiration; we have
never measured what the current number is, and **that measurement is the first
piece of work**, not the target.

### S2 — Availability of the read path

> **The gold read verbs answer or refuse within 30 seconds.**

*Why 30 s:* it is already enforced, today, in the figure client — a draw that
has not answered in 30 s is treated as a source that is gone rather than slow.
Stating it as a platform level makes one number true in two places instead of a
client-side timeout nobody else knows about.

*What is missing:* the same deadline on the CLI and MCP paths, which do not have
one.

### S3 — Correctness of shape

> **A gold column's declared unit, type and role match what it holds, or the
> drift is reported within a day.**

*Why:* ADR-122 already runs this as a standing check. This promises the
*reporting*, not the absence of drift — we cannot promise nobody breaks a
declaration; we can promise it does not go unnoticed.

*Deliberately not promised:* that values are correct. We can speak for shape,
provenance and freshness. "The number is right" is the producer's claim, and the
fault taxonomy (ADR-132) exists precisely because it sometimes is not.

### S4 — Durability

> **Gold survives a node rebuild**, because backups are policy-driven and
> validated rather than assumed.

*What is missing:* a restore has to be rehearsed before this is a promise. An
unrehearsed backup is a belief.

## Guardrails

1. **No level without a signal.** If it cannot be measured today, it is a goal in
   this document, never a number in a partner's hands.
2. **A level is per environment.** A prototype node and a production node cannot
   carry the same promise, and pretending otherwise is how a prototype becomes
   load-bearing by accident.
3. **Measure before promising.** Every number above is aspirational until we have
   run it for a month. Publishing first and measuring second is how an SLA
   becomes a thing nobody looks at.
4. **A missed level is a finding, not an alarm.** It goes where drift and
   staleness already go, on the existing surface.
5. **Never promise correctness of values.** See S3.

## A prototype node, specifically

The first node is a prototype on a university network, not an export-control
authorised enclave and not a
production deployment. It has already shown what a prototype does: disk pressure
took Postgres down through GC'd local images; the canary's timer sat disabled
from the day it was installed; site extensions ran from hand-copied packages the
deploy never updated.

So the honest level for such a node is **best-effort, stated as such**:

> **A prototype node offers no availability guarantee.** Readings that have landed are
> served on a best-effort basis, with the same freshness *reporting* as any
> other node: you will always be able to see how old the data is, even when it
> is old. Loss of the node is a loss of service, not a loss of data — gold is
> backed up under policy — and restoration is measured in days, not minutes.

That is worth writing down precisely because it is unflattering. A partner who
knows the node is best-effort can plan; one who assumes it is production finds out
during something that matters.

**What such a node could credibly promise now**, being the only one anybody actually
measure:

- Freshness *reporting* — the signal exists and runs.
- That gold is backed up, once a restore has been rehearsed.
- That a stale feed is visible, which is already true.

## Open

1. **What are the current numbers?** Everything here is a target with no
   baseline. The first task is a month of measurement, not a publication.
2. **Who is the promise to?** A partner site, an internal researcher and a
   regulator want different documents from the same signals.
3. **Does a service level belong in the node profile** (ADR-019), so an
   environment declares its own rather than inheriting a fleet-wide one?
4. **What is the remedy when a level is missed?** An SLA with no consequence is
   an SLO with a formal tone. For an academic platform the honest answer may be
   "we tell you and we say why", and that should be stated rather than implied.
