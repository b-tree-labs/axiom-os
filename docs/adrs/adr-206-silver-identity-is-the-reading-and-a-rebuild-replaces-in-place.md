# ADR-206: Silver's identity is the reading, and a rebuild replaces it in place

**Status:** Proposed  •  **Date:** 2026-10-27  •  **Deciders:** platform owner

**Related:** ADR-023 (conformance), ADR-182 (expand then contract), ADR-189
(time-series storage), ADR-193 (incremental conform), ADR-194 (deposits are
bronze), spec-signal-ingest-and-producer §6.2.1.

## Context

`silver.signals` was keyed on `(row_hash, channel)`, and `row_hash` hashes a
transport artifact. On the row path it hashes the bronze row. On the deposit
path it hashes the deposit file's content hash plus `ts`. So one reading that
arrived twice had two hashes and landed twice. That happens when the same
readings come in a second file, from a second connector, or once as a row and
once as a deposit. On a live node, one site's partner deposits normalized to
the same feed as an earlier backfill. Thirteen days had identical counts, and
loading the deposits would have doubled those days.

The same key blocked reprocessing. The owner's requirement: someone who wants
to reprocess data already in gold with new conform logic, because something in
gold is wrong, must be able to. `conform-run --rebuild` forgot watermarks and
re-derived on `row_hash`. That rewrote only rows whose bytes it re-read under
the same hash. A corrected re-delivery got a new hash and was added next to the
wrong row. The rebuild also could not be scoped, so it rewrote every site's
history at once.

The owner's design rule (2026-10-08): every hop is idempotent on reading
identity `(site, feed, channel, ts, source_class)` plus a content hash.

## Decision

1. **The identity of a silver row is the reading**,
   `(site, feed, channel, ts, source_class)`, enforced by the unique index
   `signals_reading_identity`. `ts` is in it, as TimescaleDB requires.
   `row_hash` stays as an attribute: it names the first arrival and is never
   rewritten.
2. **Every conform write is an upsert on the identity.** It does nothing when
   the content is identical and updates in place when the content differs.
   Content is the served value and its metadata plus the uncertainty terms.
   Provenance (`schema_ref`, `conform_version`) does not by itself count as a
   change. Each row records `content_hash`, `conform_version` and
   `conformed_at`.
3. **A rebuild is a scoped replace.** `conform-run --rebuild` takes
   `--connector`, `--site`, `--start` and `--end`. It re-reads bronze for that
   scope, upserts, and reports inserted, replaced and unchanged counts. It
   writes nothing outside the window, then recomputes the gold catalogue.
   `--dry-run` gives exact counts and rolls back. A rebuild refuses when the
   identity key is absent, because without it a rebuild can duplicate.
4. **Expand, then contract.** This release adds the nullable stamp columns.
   It creates the index only on an empty table. The writer uses the identity
   upsert once a valid index exists and the legacy key until then. A populated
   node moves through an operator path: a read-only preflight, a collapse that
   keeps the newest conform and copies every other row to
   `silver.signals_identity_audit`, and an index build whose lock profile was
   measured. A later release drops the transport primary key and repoints the
   uncertainty companion.

## Consequences

- Two paths that carry the same reading produce one row. An identical
  re-conform writes no tuple. A corrected one replaces in place.
- Two rows at one instant with different values cannot both stay under one
  identity. A live install had 370,889 such instants on one channel, and every
  one of them was divergent, none a re-send. Under this key, the newest conform
  wins. The collapse refuses divergent identities unless the operator passes
  `--accept-divergent`, and it keeps every removed row in the audit table. If a
  producer's timestamps cannot separate two observations, that is fixed at the
  producer or by declaring a different feed or source_class.
- When a reading is replaced through a different path, `row_hash` still names
  the first arrival. `schema_ref`, `conform_version` and `conformed_at` name the
  latest.
- A rebuild leaves behind a row whose identity it no longer produces, for
  example under a renamed channel. It removes an old row only when the same
  bronze row (same `row_hash` and channel) now maps to a different identity.
- TimescaleDB 2.17.2 cannot add a unique index to a hypertable once
  compression is enabled. Such a node upgrades the extension before migrating.
  2.30.2 was verified.
