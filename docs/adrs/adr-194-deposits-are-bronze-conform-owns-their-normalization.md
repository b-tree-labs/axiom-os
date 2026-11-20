# ADR-194: Deposits are bronze, and conform owns their normalization

**Status:** Proposed (2026-10-15)

## Context

The conform spine (ADR-023) turns bronze `_rows` — transport-normalized
records the ingest edge annotated with `schema_ref` and `row_hash` — into
canonical `silver.signals`, through a schema_ref-keyed normalizer registry.
That covers every producer that POSTs rows.

It does not cover **mirror connectors**. A connector that pulls a shared
folder or an object store lands whole files: one JSON record per file under
`<connector>/_records/<day>/`, bytes content-addressed under `_content/`.
Those deposits never pass the ingest edge, so nothing annotates them into
`_rows`, and the conform walk never sees them. Measured on a live node: two
such connectors had been landing instrument runs nightly for weeks with **zero
rows in silver**, invisibly — the funnel had no category for them at all.

Three ways to make deposits reach silver were considered:

1. **Annotate at the mirror** — the pull itself parses each file into `_rows`.
2. **Annotate in a separate pre-step** — a file-to-rows pass writes `_rows`
   beside `_records`, then the existing conform runs.
3. **Conform reads deposits directly** — a second, deposit-shaped normalizer
   contract on the same spine.

The tenant data kit (prd-tenant-data-kit) sharpens the question: the people
who know these file formats are the sites' own engineers, and their
contribution surface is the kit's `conform/` folder. Whatever parses a deposit
must be registerable the way a row normalizer is, testable on a laptop, and
runnable under the same tenant rules.

## Decision

**A deposit is bronze as it stands.** The mirror stays a *receiver* (ADR-131):
it records what arrived and stores the bytes, and never parses a partner's
file format. No second bronze form is synthesized from the first — generating
`_rows` from deposits would double the bytes (bronze already retains the file)
and create two raw records for one arrival, each claiming to be the source.

**Conform grows a deposit-shaped half, on the same spine**
(`data_platform/conformance/deposits.py`):

- A **deposit normalizer** is a pure function of one `Deposit` (the mirror's
  record plus a path to the bytes) yielding canonical signal rows. It is
  registered per `schema_ref` in a `DepositRegistry`, mirroring the row
  registry; a deployment declares `{connector: schema_ref}` exactly as it
  declares `{connector: site}`.
- **Declining is normal; failing is loud.** A share holds run notes and photos
  beside instrument files, so a normalizer that returns no rows is counted
  (`deposits_skipped`), never erred. But a file it claims and cannot parse
  FAILS a strict run (`deposit_verdict`), unlike one malformed line in a row
  stream: a bad line costs one reading, a bad deposit costs a whole file whose
  producer believes it was delivered.
- **Row identity is derived, not invented:** `row_hash =
  sha256(content_sha256 | ts)`, with `channel` already in silver's key. The
  same bytes re-conformed produce the same hashes, so the nightly re-pull of
  an unchanged share is a no-op all the way to `ON CONFLICT DO NOTHING` — the
  idempotency the row path gets from the edge's annotation, derived here from
  the content address.
- **Deposits arrive in sets**, so each `Deposit` can see its source-folder
  siblings through `companion()`: a samples export finds the channel catalogue
  deposited beside it, a trace finds its settings file. A normalizer never
  touches the content store's layout.
- **The kit carries it as part of the conform contribution kind,** not as a
  sixth kind: `kit.toml` marks a source `kind = "deposit"`, and a `conform/`
  module registers `register_deposits(registry)` beside (or instead of)
  `register_all`. `kit-try` conforms sample deposits into the local medallion;
  `kit-check` runs every sample deposit through the normalizer and lints the
  rows frame-by-frame.

The tenancy rule is unchanged (ADR-023-A1 §A1.3): on a shared host a deposit
normalizer is platform-reviewed code or a per-tenant worker's, never a site
package loaded into the shared process. The kit is where the site author
writes and proves it; promotion is the same ladder as a row normalizer.

## Consequences

- A mirror connector is now *conformable*, and — more important — its silence
  is now *visible*: deposits under a connector with no declared ref, a ref
  with no registered normalizer, bytes missing from the content store, and a
  file that failed to parse are four named funnel fields, each failing a
  strict run.
- Backfill and re-derive reach deposits for free: bronze retains the files,
  and identity is a function of their content.
- A reading's identity differs by path: edge-annotated rows hash the producer's
  record; deposit rows hash `(file, ts)`. A series continued across both paths
  (a backfilled deposit meeting a live push) must dedup on `(site, feed,
  channel, ts)` semantics, not on `row_hash` — the adoption question the site
  config already handles by matching cadence, recorded here so nobody expects
  row_hash to collide across the two.
- One deposit can be millions of readings, so the deposit walk checkpoints per
  file and the kit uses the batched upsert. A conform pass still re-reads
  every deposit (cheap: skipped by `DO NOTHING`), and the watermark treatment
  rows get (ADR-193) is future work for deposits — the natural key is the
  content hash, which `latest_records` already dedupes on.
