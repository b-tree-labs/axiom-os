# ADR-129 — Gold to the app store is a contract, not a join

**Status:** Proposed (2026-09-24)
**Relates to:** ADR-131 (there is always a medallion) · ADR-128 (medallion tier boundaries) · ADR-127 (data orchestration at our tier) · ADR-055 (PULSE)

## Context

The web app serves from its own OLTP store. That store is a **projection of
gold**, refreshed on a schedule — the app answers from a small, fast,
always-current table rather than querying the lakehouse per request. The shape
is right and it already exists.

What is not right is that the implementation assumes the two databases are the
same database. They are, on this node, and only because this node is a
prototype.

**Production separates them.** The medallion tier scales independently, on
different nodes, in a different environment — plausibly still Postgres, but a
*different Postgres*, reached over a network, with its own credentials,
failure modes and latency. The co-location on one node is an artifact of a proof
of concept and must not become the architecture.

### What the current implementation actually does

A consumer's gold-sync module says so in its own docstring — "Postgres-only (the
app store and gold share the node's Postgres)" — and the code is one statement
spanning two schemas:

```sql
INSERT INTO webapp.site_catalog_channel (...)
SELECT site, stream, channel, COALESCE(min(unit),''), count(*), min(ts), max(ts), now()
FROM gold.signals
GROUP BY site, stream, channel
ON CONFLICT (site, stream, channel) DO UPDATE SET ...
```

Three properties of that statement matter:

1. **It cannot exist across two databases.** A cross-schema `INSERT … SELECT`
   is not a cross-database operation. Separation does not degrade this path; it
   deletes it.
2. **It is a full aggregate of the fact table, quarter-hourly.** It scans
   `gold.signals` — 70 million rows — to maintain **224** catalog rows, every
   15 minutes, with no watermark and no incrementality. Over a network this is
   not slow, it is untenable.
3. **The portable path already exists and is unused in production.**
   `store.upsert_channels(session, rows)` is row-level, works against SQLite,
   and — per the same docstring — "is what tests exercise". The
   production-shaped path is the one nothing runs.

### The observable consequence today

The refresh unit has **745 failures against 600 successes since 2026-09-11** — a
55% failure rate on a 15-minute timer. So the projection is current about half
the time and nothing distinguishes the halves. The proximate cause is a
coupling: `sync.py` runs

```python
migrate("head")          # schema migration
gold_sync.push_catalog(s)   # data refresh, gated behind it
```

so an Alembic migration executes 96 times a day as a **precondition for moving
data**, and a stale `alembic_version` (`Can't locate revision identified by
'0001'`) stops the data refreshing at all.

## Decision

**1. The boundary between gold and the app store is a contract, not a join.**
It is defined as a transfer between two systems that do not share a
transaction, a process, or an instance — and it is implemented that way *now*,
on the prototype, where it is cheap to get right.

**2. A serving surface reads exactly one route to gold, declared at install,
and never falls back to the other.** There are two legitimate routes, because
there are two shapes of install:

- **the projection** — the app store, refreshed by the transfer. This is the
  route when the web app is installed, and on that route gold is reachable
  *only* by the transfer.
- **gold verbs** — a declared read API over gold, which is how a node runs
  headless: no web app, no app store, no projection to refresh. Serving still
  reads gold, and it still reads it through one named contract rather than a
  query the caller composed.

The rule is not "the app reads its own store." It is that **the route is chosen
once, at install, and a surface never silently uses the other one.** Falling
back from a stale projection to a direct gold read is the specific failure this
forbids: it converts a visible freshness problem into an invisible latency and
coupling problem, and it means two installs of the same surface answer from
different places.

Both routes are the same contract. Gold publishes a declared surface; what
differs is whether a projection sits between it and the reader. That is why
gold verbs are not an exception to this ADR — they are the second consumer of
the thing this ADR says gold must expose.

Because `webapp` is presently a schema beside `gold` in one database, **only
discipline separates them on the projection route, so that discipline gets a
static guard** in the manner of ADR-128.

**3. The transfer is incremental and watermarked, not a full re-aggregate.**
It moves what changed since a recorded cursor. A projection that rescans the
fact table to refresh a dimension does not survive either scale or separation.

**4. Migration is a deploy concern; the refresh is a runtime one.** They are
separated. A schema behind head must not stop the projection updating, and a
projection refresh must not be a migration trigger.

**5. The projection carries its own freshness, and the app surfaces it.** A
`synced_at` the app can read, and show. An app that implies "now" while serving
a projection last refreshed 40 minutes ago is making a claim it cannot support
— and with a 55% failure rate it is making it wrongly half the time.

**6. Direction is set by network policy, not preference.** Site edges push
out; nothing reaches in. A design in which the app store *pulls* from gold is
unavailable across those boundaries regardless of how clean it looks, so the
contract is expressed as **gold publishes, the app store accepts**.

## When a projection-route app may read gold directly

This section is about the projection route only. On the gold-verbs route
reading gold *is* the declared path, so there is nothing to except.

Not never, but named, and never on a serving path.

A fact too large to project — an operator exporting a year of signals, an
analyst's ad-hoc aggregate — is a **reporting** read. It is declared as such,
labelled in the surface as coming from the analytical store, and may be slow,
rate-limited or unavailable without the app being down. This is the same shape
as ADR-128's E5 diagnostic exception: the escape is legitimate, and it must
*name itself*.

What it is not: a convenience because the join was easier, or a fallback when
the projection is stale. **A stale projection is reported as stale, never
silently backfilled from gold** — that converts a visible freshness problem
into an invisible latency and coupling problem.

## Consequences

- `gold_sync.push_catalog`'s in-database statement is prototype-only and is
  replaced by the row-level path that already exists and is already tested.
  The test path becomes the production path, which is the right direction for
  that asymmetry.
- The transfer needs what any network transfer needs and currently has none of:
  batching, a cursor, resumability, and idempotency at the row level rather
  than at the statement level.
- Cross-database means credentials and a reachable endpoint. That is a
  deployment surface with its own failure modes, and it is better discovered on
  a prototype than during a production separation.
- The 15-minute cadence becomes a policy rather than a timer, and belongs on
  PULSE with the rest of the platform's recurring work (ADR-127), where it is
  gated and receipted rather than firing from a unit file nobody reads.

## Open questions

1. Whether the transfer is a push job on the medallion side or an accept
   endpoint on the app side. §6 constrains the direction of *data*, not
   necessarily of *initiation*.
2. Whether `synced_at` is per-row or per-projection. Per-row survives partial
   transfers; per-projection is what a UI usually wants to display.
3. What the app does when the projection is older than a declared budget —
   serve with a notice, or refuse. This is the app-facing half of the freshness
   question and deserves its own answer rather than a default.
