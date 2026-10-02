# ADR-091 — Shareable-URL provenance: ingest preserves each document's origin link

**Status:** Proposed · **Date:** 2026-07-13
**Owner:** @ben
**Builds on:** ADR-074 (Registry Fabric — `SourceKindProvider` is the per-kind ingest seam), ADR-070 (knowledge architecture — data platform owns bronze→silver→gold), ADR-049 (portable connector contract — `FetchedItem`/bronze sidecar), ADR-052 (dependency direction — scale providers behind a seam), ADR-056 (skills as invocable functions — maintenance verbs are skill fns)
**Related:** #531 (multi-Box source fan-out — landed the source-side of this ADR), #538 (corpus onboarding), the RAG ingest/provenance pipeline (`rag/tests/test_ingest_provenance_integration.py`)

## Context

A document's **shareable link** in its origin system (Box, Google Drive, SharePoint,
Confluence, …) is the strongest provenance we can hand a human: click, and you're at the
authoritative source. Historically that link was not persisted, so chat citations,
extraction reports, and audits could name a flattened `source_path` but couldn't send
anyone to the file.

The framework itself is sound — the Box provider is a proper `SourceKindProvider`
citizen — so the fix **rides the existing contract, it does not duplicate it.** What the
audit found:

- **The origin id already flows.** `FetchedItem.item_id` is the Box file id, and
  `bronze/sinks.py` already writes it (plus `etag`, `source_path`, `extra`) into the
  sidecar manifest. There is **no need for a new `source_ref_id` on the walk contract** —
  it already exists as `item_id`.
- **The link is the one true gap.** `FetchedItem` had no `source_url`, and the Box source
  built a human `source_path` from `path_collection` but never constructed the web URL
  from the `item_id` it already holds.
- **The id died at one boundary.** `rag_embed.embed_bronze_record` threaded only
  `source_path` into `store.upsert_chunks`; `item_id` was dropped there, and the
  `documents` table had no column to receive an id or URL. Bronze kept it; the RAG store
  threw it away.
- **A corpus can bypass the connector entirely.** A corpus ingested as `data_source =
  local` (files indexed off disk) never runs the source connector, so it has no origin id
  at all — which is why recovering links there needs a re-catalog, not just a column.

## Decision

Preserve the origin link as first-class provenance by **extending the existing contract at
its one gap and closing the one drop point** — source-agnostic, at the seam.

1. **Reuse `FetchedItem.item_id` as the origin stable id.** No new walk-record field.
2. **Add one field: `FetchedItem.source_url: str | None`,** populated by the provider — the
   Box source builds `https://app.box.com/file/{item_id}`. URL construction stays *in the
   provider* (only it knows its link shape); the platform never hardcodes a source's URL.
3. **`SourceKindProvider.url_for(config, ref_id) -> str | None`** (default `None`) so the
   platform can construct/refresh a URL from a stored id **without a re-walk**. URL-less
   kinds (local FS) return `None` — exempt **by declaration**, never silently.
4. **Close the drop point.** Thread `item_id` (as `source_ref_id`) and `source_url` through
   `embed_bronze_record` → `upsert_chunks` → the `documents` INSERT. Add `source_url` +
   `source_ref_id` to `documents` in both `rag/store.py` (Postgres) and `rag/sqlite_store.py`
   via idempotent `ADD COLUMN IF NOT EXISTS`. Re-index uses `COALESCE`-on-conflict so a
   URL-less re-embed can't wipe a link a prior ingest/backfill captured. Retrieval/citation
   surfaces render `source_url` when present, falling back to `source_path`.
5. **`axi data backfill-urls <connector>`** (skill fn per ADR-056) hydrates a corpus indexed
   before URL capture: re-catalog the source metadata-only, build each URL via `url_for`,
   match catalog paths to indexed documents (boundary-aware suffix match — refuses to guess
   on ambiguity), and `UPDATE` the two columns. `--dry-run` reports the match rate first.

**Landed in two phases:** #531 shipped the source-side (steps 1–3 + the embedder threading);
this change completes the store side (step 4) and the backfill verb (step 5). Note #531
merged the embedder's `source_url=`/`source_ref_id=` kwargs *before* the store accepted them
— the store change here also fixes that latent `TypeError` on the real ingest path.

## Consequences

- Hosted-source ingests become **navigable back to the authoritative document**; citations
  turn clickable — using plumbing that already carried the id, plus one field and two columns.
- **Backfill has two honest modes**, via `axi data backfill-urls`:
  - *Connector-ingested corpora* — the id is in the bronze sidecar, so backfill is a pure
    `url_for(item_id)` metadata pass (no re-embed).
  - *Local-ingested corpora* — no id exists, so backfill re-catalogs through the source to
    recover `path → id → url`. The deeper fix for such a corpus is to re-ingest it *through*
    the connector so it gains id, URL, etag-incremental, and health like any other source.
    Running the backfill requires the connector's own auth (e.g. the Box CCG secret) — it
    runs where the connector runs, not from an unprivileged shell.
- **New obligation per provider:** set `source_url` (or implement `url_for`), or opt out by
  returning `None`. An AEOS conformance check can assert the declaration exists.
- **Warn-not-fail** keeps ingest resilient while making the gap measurable.

## Alternatives considered

- **Add a new `source_ref_id` to the walk contract.** Rejected — `FetchedItem.item_id`
  already is it; a parallel field would duplicate the seam.
- **Store only the id, build URLs at render time.** Rejected as sole approach — render sites
  multiply; persist the resolved `source_url` *and* keep `item_id` for refresh via `url_for`.
- **A Box-specific `box_url` column.** Rejected — the value is that the seam is
  source-agnostic; `contracts.py` already enumerates the kinds that follow Box.
- **Fail-closed on a missing URL.** Rejected — a missing link is a navigability regression,
  not a correctness one; blocking a load on it would push operators to `--no-verify` the gate.
