# ADR-189: A time-series table is declared once, and its storage follows

**Status:** Proposed
**Date:** 2026-10-15
**Related:** ADR-128 (tier boundaries), ADR-131 (there is always a medallion), ADR-174 (Postgres is the runtime database), ADR-180 (a producing site keeps a local medallion), ADR-182 (never down from our own causes), spec-signal-ingest-and-producer §6.2, prd-data-platform

## Context

The data platform was measured for the first time on 2026-10-05, ahead of
onboarding three partner nodes. The dominant object in a 560 GB database was
a **466 GB plain table**: 860 million rows of seven repeated low-cardinality
text columns, a timestamp and a double, never partitioned, never compressed,
on a volume with 198 GB free and growing 3.4 GB a day. TimescaleDB 2.28 was
installed on that server and idle. A probe on 396,000 real rows measured
**113.9x** compression with `compress_segmentby = <identity columns>` and
`compress_orderby = ts DESC`.

Nothing was wrong with any one component. The pieces existed and were not
connected:

- `silver.signals` could become a compressed, retained hypertable (#1197,
  #1224), but only `silver.signals`, by name, through one flag.
- A native range-partition lifecycle existed for declared tables
  (`[[data_platform.timeseries]]`), but it managed partitions, not
  compression, and could not adopt a populated table (its own guard refuses
  a DEFAULT partition with rows).
- Retention under disk pressure existed (#1250) with the right rule, that
  nothing is deleted that has not been delivered upstream, but it knew about
  two tables by name.
- The store-health checks (`axiom.infra.store_health`) could *say* a large
  table was not a hypertable, and nothing could act on what they said.
- Converting the live table in place (`create_hypertable(migrate_data)`)
  locks it for the length of a full copy, which on 466 GB is an outage, and
  ADR-182 forbids outages from our own causes.

The result was that the biggest reclaim available on the node was one
nobody could apply in a planned window with any confidence.

## Decision

**A table is declared as time series once, in configuration or by the
extension that owns it, and the platform derives and applies its storage
shape from that declaration: hypertable, compression, policies, health.
Existing plain tables are adopted into that shape in place, lock-light and
resumable, by a verb that verifies every window before it trusts it.**

### D1 — The contract

A `[[data_platform.timeseries]]` entry with `engine = "hypertable"` is a
`TimeseriesContract`: `schema`, `table`, `time_column` (default `ts`),
`identity` (the columns that identify a series), `rows_per_day` (the declared
rate), and optionally `chunk_interval`, `compress_after` (default 7 days),
`retain` (default: never) and `forward_guard`. From it the platform derives:

| derived | rule |
|---|---|
| chunk interval | the widest rung of `1h, 2h, 4h, 6h, 12h, 1d, 7d, 30d` holding at most ten million rows at the declared rate; one day when no rate is declared |
| compression | `segmentby = identity`, `orderby = <time_column> DESC` (the shape the 113.9x was measured on) |
| compression policy | chunks whose newest row is older than `compress_after` |
| retention policy | only when `retain` is declared; deleting history is never implicit |

The same config key already declares the native-partition engine; the two are
told apart by `engine` and never read each other's entries. An extension that
owns a time-series table declares it the same way. Nothing is discovered: a
table absent from the declarations is never touched, which is the safe
default for a mechanism that can drop data.

### D2 — Applied at the schema layer, idempotently

`axi data ensure-schema` applies every declared contract after the tier DDL,
at deploy time, so bronze, silver and any extension table get the shape by
one rule. The apply is idempotent and **reports the difference, not the
outcome**: it reads the catalogue, changes only what differs from the
declaration, and returns the steps it actually took (`compress chunks older
than 2 days (was 7 days)`), so a deploy log is checkable. A changed
declaration converges, including a retention withdrawn from the declaration
being withdrawn from the database, and said. A declared contract that
cannot be applied fails the deploy, for the same reason the tier DDL does.

`silver.signals` (#1197) now rides this contract for its compression and
policies; only its key rewrite and companion table remain its own.

### D3 — What apply refuses

- A unique index that omits the time column (TimescaleDB requires it in
  every unique index): refused with the index named and the fix stated.
- A populated plain table above one million rows or one gigabyte: refused,
  naming `axi data adopt-timeseries`. Converting in place would hold a lock
  for the length of a full copy; a deploy must never stall a live writer.

### D4 — Retention never drops what has not left (the #1250 rule)

Retention is a TimescaleDB policy. For a table that is the copy this node
forwards upstream (`forward_guard = true`), the policy is installed
**paused** and a pass that knows the forwarder's cursor releases it: the
cursor advances only on a confirmed delivery, so while any batch awaits
delivery the job stays paused and nothing is dropped. The guard reports a
change of state (paused, resumed), not the state. A host node that forwards
nothing declares no guard and its policy runs from the start.

### D5 — Adoption of an existing plain table, in place

`axi data adopt-timeseries <schema.table>` converts a populated plain table
without a long lock:

1. **Estimate (the default, nothing written).** Rows and bytes from the
   catalogue, the time range, dependent views, triggers, unique indexes that
   omit the time column, and the expected ratio measured on a sample window
   copied into a scratch hypertable and compressed. From these: the windows,
   the bytes needed, and an ETA from the sample's measured rate. The free
   space on the volume is checked against the need before anything starts.
2. **Copy by time window** into a new hypertable created from the contract
   (`<table>__adopt`), oldest first, one window per transaction, readers and
   the writer running throughout. Each window is **verified before it is
   marked done**: the row count and an order-independent hash of the rows
   (the sum of a 64-bit prefix of each row's digest) must match between
   source and copy. The verified chunks are compressed as the copy
   advances, so the copy needs compressed space, not a second 466 GB.
3. **Resume.** Progress lives in a ledger in the database, in the same
   transaction as each window, so an interrupted run resumes at the first
   window not marked done and a window is never half-trusted.
4. **Swap** in one short transaction: a final count per window, a copy of
   the tail that arrived since the last pass under the only exclusive lock
   taken, the two renames, dependent views recreated so they bind to the new
   table (a view follows the old table's identity through a rename),
   privileges carried over. The old table is kept, renamed `<table>__old`.
5. **Drop the old table** only on `--drop-old`, after a final verification
   of every window against the old table.
6. **Rollback** at any point before the drop: before the swap, the copy and
   the ledger are removed; after it, the names are swapped back and the
   views rebound.

Progress and an ETA are printed as it runs and written to the node's
status, so a window can be planned and watched.

**Unattended.** `--schedule` runs the copy as a job on the platform's own
scheduler, not as a service of its own. The data orchestrator is the managed
service (it restarts on failure and after a reboot); the job is a PULSE cadence
it fires, and each fire is one bounded batch of windows (`--batch-seconds`) that
returns. The ledger is the state, so an interrupted fire (a `kill -9`, a
dropped connection, a reboot) loses only the window in flight and the next fire
resumes. A fire first waits, bounded, for the database to accept connections
and fails loudly when it does not. It connects to `$AXIOM_ADOPT_DSN`, which
should name the database's own address and not a load balancer: the k3d one at
`localhost:5432` closes a connection idle for 600 s (`proxy_timeout`), and a
window's verification is one long statement with no traffic. Copying a table is
single-flight across processes by an advisory lock held on the copy's
connection, so a scheduled batch and an operator's `--apply` never overlap and
a dead process releases it. When every window is copied and verified the job
marks itself done and pauses its cadence; the swap, the verification of the old
table and the drop (`--swap`, `--verify`, `--drop-old`) are never scheduled and never automatic. A change record (ADR-182) is written
before the copy starts and when it ends. `--unschedule` removes the job and
leaves the copy and its ledger alone.

### D6 — Storage health is measured, and feeds the alarm that exists

`axi data storage` reports, per declared table and per large relation: bytes
(table, index, total), compressed against uncompressed and the ratio, the
oldest chunk, bytes per day, and the days of runway on the volume. For a
hypertable the daily rate comes from the chunks themselves (the bytes of the
chunks covering the last days, divided by those days), which answers
immediately; a plain table falls back to the sampled database growth the
store-health audit already keeps. The same facts feed the existing
`days_to_full` check in `axiom.infra.store_health`, so the disk alarm an
operator already reads gains a rate it did not have.

## Options considered

- **Convert in place with `migrate_data => true`.** One statement, and a
  lock held for hours on the table that matters most. Refused by D3; the
  reason D5 exists.
- **Native range partitions for the big table.** The lifecycle exists, but a
  populated DEFAULT partition cannot be partitioned online, there is no
  compression, and retention would still be a mass DELETE. The native
  engine stays for tables that want it; the contract is the default for
  time series.
- **A heuristic that finds time-series tables by size and time index.** The
  store-health check already does this to *warn*. Acting on a guess would
  eventually convert, or retain, a table nobody declared. Declared, never
  discovered.
- **Retention as an Axiom-run drop pass instead of a TimescaleDB job.** Two
  mechanisms for one policy. Pausing the native job from the forwarder's
  state reuses both the policy and the #1250 boundary.
- **A whole-table hash at the end instead of per-window verification.** One
  mismatch in 860 million rows would say only "wrong", after hours. Per
  window, a mismatch names the window and the run resumes from it.

## Consequences

- Every time-series table on every node is declared in one place and has
  the same shape, and a deploy log says what changed about it.
- The node this was written for can take the adoption in a planned window:
  dry-run first, the copy with the writer running, a short swap, the old
  table kept until verified. The 466 GB table becomes a few gigabytes.
- Retention stays explicit and never deletes a copy that has not reached
  anywhere else.
- Follow-up: the contract covers Postgres with TimescaleDB only (ADR-174);
  a companion table that must share a policy (silver's uncertainty terms)
  is still handled by name, and a declared `companions` list is the obvious
  next step. The retention guard is released by the archive-reclaim pass on
  a contributor node; a node without that pass keeps a guarded policy
  paused, by design.
