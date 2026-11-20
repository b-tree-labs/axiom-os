# ADR-163: Steer and Log are two projections of one journal

**Status:** Accepted (2026-10-05)
**Related:** [ADR-060](adr-060-cross-agent-event-routing.md) (event routing), ADR-119 (fleet push), ADR-123 (the Steer surface), ADR-150 (attestation logbooks), ADR-156 (oversight surfaces, in flight), [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md), [ADR-162](adr-162-the-watcher-primitive.md), `docs/specs/spec-embodied-surfaces.md` (CommandJournal)

## Context

Three surfaces grew toward the same data without a shared substrate. The
CommandJournal (spec-embodied-surfaces) is designed and journal-authoritative,
but scoped to actuation commands. Steer, the oversight worklist, carries a
proto activity feed in its Feed tab. A default Log surface was wanted for
every node: a reverse-chronological timeline over every emitter (agents,
changes, attestations, memory, releases, program sync). Building any of the
three independently would have minted two or three stores of record for one
stream of facts, and the build was held for this decision.

A survey of external agent harnesses sharpened the stakes: the field default
is a merged chat record; a split appears only when agents run in the
background, and then it is a session list with "needs you" as a status —
nobody ships a cross-emitter, append-only, queryable ledger as a first-class
destination. That ledger is a named differentiator, and it only works if
there is exactly one of it.

A third axis surfaced during design: Steer had accumulated agent AUTHORING
(creation, tuning, teaching, granting) beside its operate duties, mixing
design-time and runtime concerns on one surface.

## Decision

1. **One journal.** A single append-only substrate, fed by the event bus and
   the durable act stores, generalizing the CommandJournal: commands become
   one lane beside agents, tool audit, attestations, memory, releases, and
   program activity. The journal is authoritative on conflict. There is no
   second store.
2. **Log and Steer are projections of it.** Log is the complete ledger
   projection: reverse-chronological, lane-filtered, permanent; live versus
   historical is a filter, not a separate home. Steer is the actionable
   worklist projection: rights-filtered, short half-life, derived, never
   storing anything of its own. The two deep-link both ways. Approvals live
   at two altitudes, neither inside the ledger: inline in the conversation
   channel for runtime gates, and an administrative queue for deployment
   gates.
3. **Authoring leaves Steer.** Agent creation, tuning, teaching, and granting
   move to an Agents surface: a roster whose empty state invites creation,
   with create as an action on the roster, never a navigation-rail verb.
   Steer narrows to pure operate. Author, operate, and record are three
   surfaces over the one journal, and authoring acts emit journal events like
   any others.
4. **Design weighting.** The create-use-manage loop is designed to consumer
   grade (conversational creation, live preview, summonable specialists);
   the governance depth (rights-filtered approvals, fleet, audit) arrives as
   progressive disclosure, not as the front door.

## Options considered

- **A: Log as a tab of Steer.** Follows the field's merged-record default,
  and dilutes both: a worklist that scrolls forever is not actionable, and a
  ledger trimmed for actionability is not complete.
- **C: Log IS the substrate, everything a view.** Collapses the surface
  distinctions the audiences need; composability belongs in the substrate,
  not in merging the surfaces.
- **B (chosen): one substrate, two projections, authoring separated.** One
  source of truth, each surface shaped by its question: what happened; what
  needs me now; what exists and how it is configured.

## Consequences

- The build unblocks: the base Log surface, the domain console override seam
  that the attestation logbooks ride (ADR-150 records become one lane), and
  the program extension's journal evidence lane (ADR-161).
- Steer can never be a second store; anything Steer shows must be derivable
  from the journal plus rights.
- Amendments owed as the build lands, each in its owning document:
  prd-oversight-surfaces and prd-steer (Steer narrows; Agents surface),
  prd-logging (the timeline becomes the default log view),
  spec-embodied-surfaces (CommandJournal generalizes; commands one lane),
  with ADR-123 amended by reference and ADR-156 carrying the oversight
  surface composition in flight.
- The journal inherits the bus subject grammar (ADR-060, ADR-162), so new
  emitters appear in the Log without bespoke integration.

## Amendment (2026-11-09): the journal references signed logbook entries

**The journal references signed logbook entries and never holds the official
copy.** A signed entry is kept in its logbook (ADR-150), signed into that
logbook's chain (ADR-143). The journal's row for it carries a reference (the
logbook, its sequence number, the attestation id and the digest) and a display
line, never the statement body, its fields or observed values. Reading the
entry goes through the logbook, under the logbook's own read rules.

**"The journal is authoritative on conflict" (decision 1) applies to the event
stream**: what happened, in what order, from which emitter. It does not apply
to a signed record. If the journal and a logbook disagree about a signed entry,
the logbook wins, and the disagreement is reported, not repaired by writing to
either.

**The Log page hosts logbooks, and the Timeline is its cross-source view.**
Decision 2 called Log "the complete ledger projection". As built, Log is the
page that hosts the logbooks extensions declare (`kind = "logbook"`), and the
projection of the journal is the Timeline: one component, filtered by the
selection, by lane and by what the viewer's role may see, at the bottom of any
screen and as the full view inside Log. Steer is retired in favour of Control
(ADR-211); the rest of decision 2 and decisions 3 and 4 are unchanged.

Why: an accountable act has one official record. A second copy in the journal
would be a second store of record for the same fact, which this ADR exists to
prevent, and would let a projection outrank a signed chain.
