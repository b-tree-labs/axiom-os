# ADR-241: A record lives with its writer, gold serves what is derived, and a page reads gold only through the gold door

**Status:** Accepted (2026-11-09). Refactoring the charts to read through the gold door is follow-up work, not part of this decision.
**Related:** [ADR-128](adr-128-medallion-tier-boundaries.md) (tier boundaries),
[ADR-049](adr-049-data-platform-orchestration-boundary.md) and [ADR-052](adr-052-database-tenancy-schema-per-extension.md) (cross-extension reads ride the data platform; schema per extension),
[ADR-174](adr-174-postgres-is-the-runtime-database-and-sqlite-is-for-tests.md) (Postgres at runtime),
[ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md), ADR-143, ADR-144 and [ADR-150](adr-150-an-attestation-record-is-kept-in-a-logbook.md) (signed records in logbooks),
[ADR-163](adr-163-steer-and-log-are-two-projections-of-one-journal.md) (one journal; Log and the worklist are projections),
`docs/prds/prd-logging.md`, `docs/specs/spec-attestation.md` ("Exports, storage and analytics"),
the app-database addendum to spec-site-view and design rule DC-12 (PR #1516, open),
the publication registry (PR #1548, open).

## Context

The owner's framing: the web app's database serves the web app and its surfaces;
information core to the system belongs in gold; the web app still reads its own tables to
populate its views. Two questions were open. When should a page read gold directly, and
why allow it? And for logs: gold, the app database, or both?

ADR-128 settled which tier serves: gold, never silver. It did not settle where a record
that a person or agent *writes through the app* lives, or how it reaches gold. DC-12 and
the app-database addendum describe one direction only, gold published into the app
database. Read literally, "domain data reaches people from gold" would send the Log page
through gold and lose read-your-writes and chain verification. A consumer product's
logbook unit E5 already reads it the other way: the Log page reads the chain; everything
else reads a gold projection of it.

### Starting point: a production deployment of this pattern

A production deployment of this platform's pattern, built on an object-store lakehouse
and a separate application Postgres, has run the separation for months. What it does,
read from its code:

- **Two stores, one door between them.** Computed facts and authored reference data live
  in gold. Every surface (web, mobile, chat agent, MCP, CLI) reads the application
  database. Gold reaches it through one job, the single writer of a declared set of
  serving tables. The application never reads the lakehouse; heavy reads are kept off the
  request path.
- **Two kinds of write.** A person's declared value is written instantly into an
  application-owned table that is its system of record, then emitted asynchronously into
  bronze. A fact with rival sources (several uploads and a manual edit of the same shape)
  is written as a bronze event, and silver decides which source is authoritative; the
  application's column is only a display cache.
- **Read-your-writes by fallback.** Reads are "gold first, then the most recent declared
  entry, never the reverse". A person sees their own entry at once; once gold has caught
  up, the unified gold value wins.
- **Drift and freshness are measured, not assumed.** The writer records a hash of the
  serving rows as persisted; a check recomputes it from what is served; no hash means
  "unverified", neither drift nor clean. Freshness is three timestamps (landed, gold,
  served). A separate stamp, a hash of the value-deriving code, catches values that are
  time-fresh but computed with outdated logic.
- **Contract and audit.** The column lists the writer uses are pinned in one module and
  tested from both sides. A push that changes served data writes one audit row; an
  unchanged push writes none. Corrections need a dry-run token that fingerprints the
  current rows, back up the affected rows in the same transaction, write one audit row
  with a reason, and roll back exactly.
- **Enforcement with a ratchet.** A CI guard fails any new write to a serving or ingest
  table outside the single writer; known violations are listed with a ticket and a sunset
  date and may only shrink.
- **Scale, as built.** Data partitioned by tenant and ingest date with stable keys locked
  early; a single-node engine until a measured trigger; pushes incremental by content
  hash, sharded by tenant, per-entity atomic, with a poison entity dead-lettered. No dense
  time series reaches serving. The single writer grew into one very large module.

This ADR starts from that model and changes it only where Axiom differs: gold here is
colocated in the runtime Postgres (ADR-174) and serves dense time series, and some records
are signed acts that no tier may arbitrate.

### What external practice says

A research memo with full sources, dates and quotations is held with the owner. In brief, accessed
2026-11-09:

- **CQRS** separates the write model from read models, and Fowler warns that "for most
  systems CQRS adds risky complexity"; for demanding queries he suggests a reporting
  database instead ([bliki, 2011](https://www.martinfowler.com/bliki/CQRS.html)). Separate
  stores mean stale reads, a transactional outbox and idempotent consumers
  ([Azure Architecture Center, 2025](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs)).
  Gold is already the platform's reporting database; a read model per page is not needed.
- **Dual writes fail** under races and partial failure; derive downstream stores from one
  ordered log ([Kleppmann, 2015](https://martin.kleppmann.com/2015/05/27/logs-for-data-infrastructure.html);
  [transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html);
  [Debezium outbox router](https://debezium.io/documentation/reference/2.4/transformations/outbox-event-router.html)).
  An append-only, sequenced record is its own outbox: its sequence number is the cursor.
- **Lakehouse serving to apps** copies gold into an operational Postgres as read-only
  synced tables, in snapshot, triggered or continuous modes; writes to the copy are
  "overwritten on the next refresh", and access is governed at the source
  ([Databricks synced tables](https://docs.databricks.com/aws/en/oltp/projects/sync-tables);
  [medallion](https://docs.databricks.com/aws/en/lakehouse/medallion)). This is the
  publication registry.
- **HTAP and hybrid tables** put both workloads on one engine but keep separate table
  types with separate limits ([survey, 2024](https://arxiv.org/abs/2404.15670);
  [Snowflake hybrid tables](https://docs.snowflake.com/en/user-guide/tables-hybrid)).
  Colocation does not remove the contract.
- **Reverse ETL** treats the warehouse as source of truth for modeled attributes. Right for
  facts gold computes; wrong for records people write.
- **Data mesh**: source-aligned data products are "immutable timed facts" and "must be
  separated from the internal source systems' datasets"
  ([Dehghani, 2019](https://martinfowler.com/articles/data-monolith-to-mesh.html)). A gold
  projection of a logbook is such a product.
- **Foundry writeback** lets users edit through governed Actions, but stores edits in a
  separate writeback dataset and never overwrites the source
  ([Palantir docs](https://www.palantir.com/docs/foundry/object-link-types/allow-editing/)).
  Same principle, different topology.
- **Regulated records**: data should be "attributable, legible, contemporaneously
  recorded, original or a true copy, and accurate", and a copy is true only if it
  preserves "content and meaning ... including all metadata"
  ([FDA data integrity guidance, Dec 2018](https://www.fda.gov/media/119267/download));
  "Record changes shall not obscure previously recorded information"
  ([21 CFR 11.10(e)](https://www.law.cornell.edu/cfr/text/21/11.10)); QA records must be
  sufficient, identifiable and retrievable
  ([10 CFR 50 App. B, Criterion XVII](https://www.law.cornell.edu/cfr/text/10/appendix-B_to_part_50)).
  The official record stays where it was made; a projection is a copy.

Practice disagrees on one engine or two, and on whether users may write to the analytic
layer. The reading for Axiom: colocate physically, separate contractually; writes go to
the owner, never to gold.

### Where the code contradicts the stated rules today

1. **A mutable base table in gold.** A consumer site repository's change-capture script
   writes `gold.log_entries` by `DELETE ... WHERE source_ref` and `INSERT ... ON CONFLICT
   DO UPDATE` straight from files, skipping silver, and its verify script inserts into
   gold. Contradicts ADR-128 (gold derives) and spec-attestation (gold is a projection of
   the chain). E5 is the fix.
2. **Undeclared authored tables.** The same script writes several other gold base tables
   directly. Some are legitimate ADR-128 E1 reference data, but none is declared in
   `AXIOM_GOLD_REFERENCE_TABLES`, so the audit cannot tell an allowance from drift.
3. **The publish step accepts silver on main.** `webapp/catalog/sync.py::project_catalog`
   accepts any `schema.table` identifier. The gold-only refusal is in PR #1548, unmerged.
4. **A page reads gold without the access rules.** `webapp/catalog/series.py` reads
   `gold.signals` with its own SQL. The gold door (`data_platform/skills/gold.py`) applies
   access tiers on every read; nothing under `webapp/` outside tests mentions access tiers.
   Harmless while every charted row is default tier; a bypass the day a tiered object is
   charted.
5. **Two authorities for one fact.** ADR-163 calls the journal "authoritative on
   conflict"; ADR-142 and ADR-143 make the signed chain the record. The pending amendment
   (the journal references signed entries and never holds the official copy) resolves it.
6. **The one journal is not built.** The attest chain, the authorization decision chain,
   the action ledger (`policy.agent_actions`), the bus event log (`infra/live_feed.py`)
   and the acquisition journal (`data_platform/daq/journal.py`) each answer "what
   happened" separately.

No serving code in `src/axiom` reads silver, as ADR-128's guard requires.

## Decision (proposed)

### D1. Three homes, one record of truth per fact

| Home | Holds | Record of truth for |
|---|---|---|
| **The owner's operational schema** (ADR-052; "the app database" is every extension's schema, the web app's included) | What people and agents do through the system: signed records, commands and their outcomes, declarations, drafts, settings, preferences, layouts, pins, notifications, workflow state. | Everything written there. |
| **Gold** | The served form of domain facts: derived from silver, plus authored reference data and analysis outputs under ADR-128 E1 and E3. | Authored reference data and analysis outputs only. Never for a record a person writes through the app. |
| **Published tables in an app's schema** | Copies of gold objects a page needs fast (the publication registry). | Nothing. A publication is rebuilt from gold, and a direct write to it is a defect. |

The test for where a person's write lands, taken from the production deployment above:

- **The act is the fact** (a signature, a declaration, a setting): the writer's record is
  the truth, read instantly by its owner, and projected to gold.
- **Rival sources must be arbitrated** (several uploads and an edit of the same thing):
  each write is a bronze event and silver decides authority; the app's copy is a cache.

### D2. One direction per flow; no dual writes

```
owner's record --(sequence watermark)--> bronze --> silver --> gold --(publish)--> app tables
```

- A record reaches gold only from the committed record, by its sequence number (an
  append-only record is its own outbox) or by an outbox row written in the same
  transaction. Landing again adds nothing.
- No request writes a record and a projection. No code writes gold back into a record of
  truth. No code edits a gold object that projects a record: no `UPDATE`, `DELETE` or
  `ON CONFLICT` against it (E5's acceptance row A3, made general).
- A publication has one writer, its gold source and version recorded, a declared
  freshness, and rebuilds to identical rows without touching application state (MVP-11
  rows A4 to A6).

### D3. Which surface reads which copy

| Reader | Reads | Why |
|---|---|---|
| The owner's own page, right after a write | The record, through the owner's API | Read-your-writes; verification; the owner's access rules. |
| A page listing or summarising what it owns | Its own tables | The owner's framing; no reason to leave home. |
| A page showing domain facts it does not own | A publication, or gold through the door (D4) | ADR-049 and ADR-052 D5: never another extension's tables. |
| Chat, MCP, verbs, reports, exports, models | Gold through the door; for records, gold first, then the record for entries past gold's watermark | One answer for every harness; the fallback gives read-your-writes without making gold the record. |
| Anything that must show the official copy (a signed entry, its verification, an export for an inspector) | The record | A projection is a copy; it may show the digest that ties it to the record, never stand in for it. |

### D4. When a page may read gold directly

**All** of these must hold:

1. Read-only.
2. Through the gold door (a gold verb or the read API) with the person's access context,
   so the access tiers chat and MCP apply also apply to the page. Never page-local SQL.
3. The data is a domain fact the page does not own, and one of: large or time-series
   (copying it would double storage and add drift); historical; cross-domain or
   cross-extension; or an aggregate gold already computes.
4. The freshness is declared, and the page shows "as of" and the lag when it is exceeded.
5. The query is bounded (index-prefix predicates, point and channel caps, as
   `series.py` already does).

**Never** for: writes; reading back the person's own write; per-person or application
state; a permission-sensitive view whose rules gold does not enforce identically; the
official copy of a record; or a single SQL statement joining an app table to gold.

**Why allow it at all.** The production deployment above never lets its UI read gold,
because its gold sits on object storage off the request path and its served unit is a
small per-entity summary. Here gold is colocated in Postgres and serves dense time series.
Publishing those series would duplicate the largest data on the node, add a second
staleness, and buy nothing. The door keeps the choice reversible: if gold leaves the
request-path engine, pages switch to publications without changing what they ask for.

### D5. Ownership and measurement of every projection

- The **record's owner** owns the landing (the generic source over its sequence).
- The **domain kit** owns the conform step, the gold SQL and the verbs.
- The **app** owns its publications.
- Every projection declares its freshness and carries what makes drift detectable: a
  digest per row tying it to the record (projections of records) or a content hash of the
  rows as persisted (publications), with a check that reports drift, clean or unverified.
- Freshness is reported at three points (landed, gold, served), and served values carry a
  logic stamp so a value computed under outdated logic is visible.
- A publish that changes served rows writes one audit row; an unchanged one writes none.
- Corrections to authored or reference data use a dry-run token, a same-transaction
  backup, one audit row with a reason and an exact rollback. A correction to a signed
  record is a new signed record (ADR-142 `corrected`), never an edit.

### D6. Logs

| Kind | Record of truth | Projection | Readers | Freshness |
|---|---|---|---|---|
| **Signed logbook entries** | The attest chain in the `attest` schema. The only official copy. | Chain to bronze (a generic Axiom source over the logbook's sequence, content-addressed), silver conform, an append-only gold view with one row per record, a digest per row, supplements and imports as their own rows, no signer key. | The Log page and logbook views: the chain through `/api/v1/attest`. Chat, MCP, charts, reports, exports, models: gold first, chain fallback. | Chain: immediate. Gold: one conform cycle, declared, "as of" shown. |
| **System log** (prd-logging 1.1) | JSON-lines files on the node and their snapshots. Platform telemetry, not domain data. | None by default. If a site report needs it, aggregated health facts (counts per service per hour) land in bronze like any source. | `axi log`, the node agent, diagnosis tools. Not the Log rail item. | Live (files). |
| **Agent actions, authorization decisions, receipts** | Their own ledgers and chains. | Each may land through the same generic source when a domain needs analytics on it. | Owners and the audit tools read the record; analytics read gold. | Record: immediate. |
| **The journal and the Timeline** (ADR-163) | None. The journal is an append-only **index** of references (lane, record id, sequence, time, actor, summary) over the lane records, rebuildable from them, holding no official copy. | Fed from each lane's committed record, never by a second write. | The Log Timeline and the worklist (with rights); every row deep-links to its lane's record. History beyond the journal's window comes from gold. | Live. |
| **Ops Log projections** (runs, weekly and monthly reports, missed checks) | Derived. | Gold views over the logbook projection (consumer-aligned). Published into the app only if a page needs a fast list. | Reports, chat, charts. | One conform cycle or the report's schedule. |

**How E5 fits.** E5 is this ADR applied to one logbook and needs no change. Its digest per
row is the drift anchor, its "no mutation of gold" row is D2, its retirement of the old
writer is the ratchet in Enforcement, and its "the Log page continues to read the chain"
is D3. Two recommendations reach past E5: make the Axiom source generic over any
append-only, sequenced record, not only logbooks; and have chat use gold first with the
chain as fallback rather than gold alone.

### D7. Scale

| Dimension | Holds | Breaks first | Escape route | Changes the rule? |
|---|---|---|---|---|
| Sites and nodes per deployment (one now; partner sites and a production site planned) | Each node is the record of truth for what was written on it. | A combined view across sites, if built as live reads of other nodes. | Each node keeps its own gold; a combining site lands what crosses into its own bronze and builds its own gold; per-site publications at the edge. | **Yes:** another node's gold is never read live by a page and never treated as a record. |
| Federation | Signed chains verify anywhere. | Sending projections as if authoritative, or app tables at all. | Records cross as chain segments (verified on arrival); derived facts cross as bronze inputs; application state never crosses. | **Yes:** each row of the decision table states what crosses. |
| Tenants and organizations | ADR-052 menu; every gold row carries a site or tenant key and an access tier. | A page's own SQL missing a tenant or tier filter (contradiction 4). | Door-only reads; row-level security on gold views as defence in depth; schema per tenant where hard isolation is required. | **Yes:** D4 point 2 is mandatory. |
| Users and concurrent sessions | App-table reads are indexed lookups; chart reads are capped. | Many long-range chart sessions over raw `gold.signals` on the Postgres that also takes operational writes; likely the first real breakpoint on a single node. | Cache keyed by gold revision; gold rollups; a read replica or separate gold database behind the door. | No; the door makes the move invisible. |
| Event and journal volume | The journal holds references, not payloads. | An unpartitioned journal scanned by "latest across lanes" at sustained agent emission. | Time partitions, a retention class per lane, keyset pagination, live tail from the bus, history from gold. | No; the journal is declared an index with a retention class. |
| Telemetry volume and retention | Series stay in gold; never published. | Raw-resolution reads over years, since `gold.signals` passes silver through. | Fixed-resolution rollups in gold (min, max, avg, n), time partitions, retention tiers; the series endpoint picks the coarsest resolution that meets its cap. | No. |
| Logbook growth over decades | Records are few and small; lifetime retention. | Full-chain verification and full rebuilds grow linearly; format longevity. | Anchored checkpoints and slice verification; incremental projection by sequence; gold partitioned by year; archival export of the official copy with its chain. | No; the projection stays disposable, the chain endures. |
| Extensions and domains contributing data | One generic landing source; publications declared as data. | Each extension writing its own landing or push code; one push module growing without bound (the production deployment's single writer became one very large module). | One writer per served table, many small declared publications. | No. |
| Apps and harnesses (web, chat, MCP, CLI, API) | One door, one answer. | A harness-private gold path diverging in filters, freshness or shape. | Every harness, the web app included, reads through skills and the door. | **Yes:** strengthens D4 point 2. |

Each escape route is taken when its trigger is measured, not in advance.

## Enforcement (proposed, not built here)

1. Extend ADR-128's static guard: no `UPDATE`, `DELETE` or `ON CONFLICT` against a gold
   object that projects a record; known violations listed with a sunset date as a ratchet
   that may only shrink.
2. A guard that web routes read gold only through the gold door (no `gold.` relation in
   `webapp/` outside the door client and tests), with `series.py` as the first item on the
   ratchet.
3. The publication registry (PR #1548) refuses non-gold sources and records gold version
   and freshness; add a served-content hash and a drift check that reports drift, clean or
   unverified.
4. `AXIOM_GOLD_REFERENCE_TABLES` declared per consumer, so `axi data tier-audit` can tell
   E1 from drift.

## Options considered

- **The app database holds everything a page shows** (the production deployment's model,
  verbatim). Rejected for dense time series on colocated gold: it duplicates the largest
  data on the node and adds drift. Kept as the fallback if gold leaves the request path.
- **Every surface reads gold, including the owner's page** (DC-12 read literally). Rejected:
  loses read-your-writes and makes a projection stand in for the official copy.
- **Log entries live in gold** (today's `gold.log_entries`). Rejected: a mutable gold table
  is neither a record nor a true copy, and nothing ties it to a signature.
- **Write to both, in the request** (dual write). Rejected: races and partial failure.

## Consequences

- One sentence a reviewer can check: the record is where it was written, gold is derived
  from it, and a page reads gold only through the door with a declared freshness.
- DC-12 and the app-database addendum gain the record direction; ADR-163 takes its pending
  amendment; spec-attestation's "surfaces receive gold" is scoped to analytics.
- The web app's chart path moves behind the gold door, and access tiers apply to charts.
- E5 proceeds unchanged; its Axiom source should be generic over sequenced records.

## Open questions for the owner

1. Accept "the record lives with its writer", with gold a record of truth only for
   ADR-128 E1 and E3 data?
2. Door-only gold reads for pages, including refactoring the chart series path?
3. ADR-163: the journal as an operational index holding references only, with history
   from gold?
4. System log: files only, or aggregated health facts to gold for site reports?
5. Chat over records: gold first with the record as fallback, or gold alone with "as of"?
6. The access tier of logbook projections in gold (E5 left it open).
7. Do projections follow their record's retention class, and does a purge propagate to
   gold by rebuild?
8. Federation: records cross as verified chain segments, landed in the receiver's bronze?
9. Adopt the served-content hash, three-point freshness and logic stamp as platform
   features of the publication registry, or leave them to each product?
10. If gold leaves the request-path engine, do pages move to publications only, or keep
    door reads against a gold replica? Recommended: decide at that trigger.
