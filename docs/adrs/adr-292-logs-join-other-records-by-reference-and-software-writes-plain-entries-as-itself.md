# ADR-292: Logs join other records by reference, and software writes plain entries as itself

**Status:** Accepted (2026-11-26)
**Related:** [ADR-251](adr-251-indexes-and-context-are-labelled-projections-the-record-is-sealed-never-compacted-one-check-at-every-door.md)
(one record, labelled projections),
[ADR-266](adr-266-readings-from-several-sources-share-one-timeline.md) (one timeline),
[ADR-290](adr-290-a-log-is-plain-unless-it-declares-it-is-signed.md) (plain logs; D8, the accountable signer),
[prd-attestation.md §6g](../prds/prd-attestation.md)

## Context

A log entry is read beside other things: what the assistant remembers about it, what the platform and
the machines were doing at the time, and what other extensions wrote about the same work. The platform
holds most of those already. Platform events carry `trace_id` and `session_id`, the audit chain carries
`chain_id`, the journal indexes Axiom's own records as references, and memory can hold a pointer to a
log record. Nothing joins them to an entry, nothing reaches the logs of the host, the cluster or a
sibling application, and no extension can write an entry from its own code.

Two ways to join them were available: ingest the other systems' logs into the platform and search them
there, or link by an id or a time window and fetch from where they live. Ingesting is the familiar
answer and costs storage, a pipeline and a second copy of data whose labels the platform does not know.

## Decision

**D1. Logs join other records by reference, never by copying them.** An entry is linked to other records
by the ids of the operation that made it and by a time window. Axiom's own records are indexed by the
journal as references, as today. An outside source (the host's journal, a cluster's pod logs, a sibling
application) is queried on demand through a declared adapter for a bounded window or an id, shown, and
dropped; it is never indexed or stored. The only copy is an excerpt a person pins to an entry as
evidence, which becomes a hashed attachment naming the source, the query and the time it was fetched.

**D2. An entry records the ids of the operation that made it, in the record.** `trace_id`, `chain_id`
and `session_id`, where the operation has them, are part of the chained record, so the link verifies
and survives export and archive. They are context, not secrets. The audit chain gains `trace_id`, so
the three sources share one id.

**D3. An outside source is seen only by the roles the site names for it.** Its rows carry no Axiom
labels, so the one check cannot judge them row by row. A source declares its roles (host and cluster
logs default to the site's operators); an adapter that labels its rows lets the check apply to each.

**D4. Software writes plain entries as itself.** An extension's code, and an agent over MCP, may write
a plain entry in one step through the public call, as its own identity, signed for by its accountable
human under ADR-290 D8, with the operation's ids. A signed log is never written this way: its entries
need a person at the prompt. This supersedes the rule that software may only propose an entry.

**D5. Memory points at an entry when a person asks or an agent acts on it.** A person's "remember this",
and an agent that acts on an entry, write a reference fragment (a pointer, the labels and a one-line
summary, never the content). A chat turn that only mentions an entry writes nothing.

**D6. An extension consumes new entries through a platform-run subscription.** It declares the logs and
a skill; the platform calls the skill for each new entry the extension may read, in order, at least
once, from a cursor the platform keeps.

## Consequences

- No pipeline, store or retention policy is added for outside logs. A source that is down shows as
  unavailable, and its rotated-out history is gone from the views, which is the price of D1.
- Every record shape that gains ids (log records, audit records) gains them additively; old records
  have none and verify as before.
- An agent's plain entries carry its accountable human's name. A site that wants no agent writing to a
  log declares it signed.
- Clocks: a row from another machine names its clock and is marked unaligned until the timeline of
  ADR-266 supplies an offset.
