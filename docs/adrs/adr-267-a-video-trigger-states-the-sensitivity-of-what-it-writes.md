# ADR-267: A video trigger states the sensitivity of what it writes, and `restricted` is the class a site controls by its own rules

**Status:** Draft
**Related:** ADR-240 D3 (a trigger is a registered contract that emits events), ADR-264 (a trigger can take the events of the triggers before it), [ADR-158](adr-158-export-control-health-privacy-and-personal-data-are-regimes-of-one-mechanism.md) (regimes; a label can come from declaration), [spec-video](../specs/spec-video.md) §`StreamTrigger` and §Events, [spec-signal-ingest-and-producer](../specs/spec-signal-ingest-and-producer.md) (OverflowPolicy is sensitivity-gated), [prd-video](../prds/prd-video.md) Phase 6

## Context

Every video event is journaled with sensitivity `internal`: the ledger stamps one
constant on every record. That was right while triggers said things like "something
moved". It is wrong as soon as a trigger reads something a site must control. A
consumer's trigger chained after the visible regions of a fixed view can say what
each region holds, and for some sites what a region holds is controlled
information: not because an export regulation names it, but because the site's
own rules do.

The journal's sensitivity classes are `open`, `internal`, `ec-controlled` and
`itar`. Two are export regimes. None says "controlled by the site's own rules", so
such a trigger would have to be written down as `internal`, which says nothing is
controlled, or as `ec-controlled`, which says something false about why. What
sensitivity does today is small and load-bearing: a controlled class may not use
the `drop_oldest` overflow policy, so a journal never silently loses such a record,
and a site's `SensitivityFilter` reads it before anything is journaled.

ADR-158 describes the general answer, regimes with labels from detection,
declaration or inheritance, but no code carries a regime label on a DAQ record
yet. The consumer needs its events marked now, before they are ever sent anywhere.

## Decision

**D1. A trigger states the sensitivity of what it writes.** `StreamTrigger` gains an
optional attribute `sensitivity`, one of the journal's classes, `internal` when a
trigger does not say. It is code, not configuration: whether what a trigger reads is
controlled is a property of what it reads, and a configuration file that forgets a
line must not make controlled data look ordinary. `BaseTrigger` stamps it on each
event it builds.

**D2. The event carries it, and the journal records it.** `TriggerEvent` gains a
field `sensitivity` (default `internal`), refused at construction outside the
classes. The runner writes each event with its trigger's class, whatever the event
says, so a trigger that builds its events by hand cannot write controlled data as
ordinary. The ledger writes it as the record's sensitivity on the trigger's own feed,
and reads it back into the event's row. An episode closed for a trigger on shutdown
keeps the sensitivity of its start.

**D3. A trigger with a class that is not one is refused at attach**, with its fix,
like any attachment that cannot work (ADR-220 §3: a reload that does not build
changes nothing).

**D4. `restricted` is a sensitivity class**: data a site controls by its own rules
rather than an export regulation. It is controlled, beside `ec-controlled` and
`itar`: a journal may not drop it on overflow. What else a site does with it (who
may read it, where it may go) is the site's `SensitivityFilter` and, later, a
regime's handling rules.

## Options considered

**An attachment setting, `sensitivity = "…"`.** Lets a site raise a trigger's class
without a code change, and lets a missing line lower it. Rejected as the only
mechanism; it can be added later as a way to raise, never to lower, a trigger's own
class.

**Write such events as `ec-controlled`.** Gets the controlled handling, but records a
false reason, and an export-control audit would count them. Rejected.

**Wait for regimes (ADR-158).** The right long-term home for what a class means. A
regime label can map onto `restricted` when it lands; nothing here has to be undone.

## Consequences

Expand only: a new optional attribute, a new event field with a default, a new
class. Events journaled before keep `internal`. A consumer reading the class list
must accept `restricted`. `instrument video list --triggers --json` and
`events --json` show the sensitivity, so a person can see what a trigger declares
and what each event was written as.
