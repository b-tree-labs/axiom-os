# ADR-079: Data-modality routing, tiered gold storage, and data-as-tool

Status: Proposed (2026-06-24)

## Context

The data platform today ships **bronze + a pgvector RAG store**; the medallion
silver/gold layers are designed but unbuilt (`spec-data-architecture.md`,
`adr-003`). Because there was no modality-aware silver routing, **all** ingested
bytes flowed bronze → RAG. A real consumer workload exposed three concrete
problems:

1. **Non-document data polluted RAG.** High-volume structured/time-series data
   (a year of streaming measurements) was chunked + embedded for semantic
   search. That is the wrong tool: cosine similarity over row fragments cannot
   answer temporal/structured queries, and ~50k low-signal chunks add retrieval
   noise. (Measured in the RAG scorecard: document grounding healthy, structured
   "grounding" ~0 — a routing mismatch, not a quality failure.)
2. **No serving tier for structured data.** `adr-003` / `spec-data-architecture
   §2` specify Iceberg/DuckDB (columnar, analytical) only, while
   `prd-data-platform §7.3` already assumes a *"served TimescaleDB/gold tier."*
   The two docs contradict; neither defines a hot/cold split.
3. **No first-class way to reason over structured data conversationally.** The
   value is comparing **observed** vs **modeled** state (a consumer's measured
   signals vs a predictive model's output) in chat — not dashboards or raw RAG.

This is largely a **build + reconciliation** gap, not a greenfield design: the
silver contract (`ACU_VCUFlowLoop/spec-ingest-contract.md §1`), egress/push
ingest (`prd-data-platform`, RDQ-*), DT tools (`prd-digital-twin-hosting`), and
tenancy vocabulary (`adr-050`) already exist. This ADR makes the platform-level
decisions that tie them together and resolves the contradictions.

Per `adr-050`, all decisions here are **domain-agnostic**: the platform speaks
`site`/`tenant`, `dataset`, `modality`, `observed`/`modeled` — never a specific
facility or domain. The motivating consumer workload is domain-specific and
lives in the consumer's own repo, not baked into the platform.

## Decision

**1. Modality classifier at bronze → silver (extensible registry).**
Bronze → silver routing is governed by an ordered registry of **detectors**
(content-type, extension, schema/content sniff) → **modality** → **routing
target**, following the existing provider-registry pattern
(`SourceKindProvider`/`DatabaseKindProvider`). Reference modalities + routes:

| Modality | Route |
|---|---|
| prose / document | silver-clean → **gold (curate)** → RAG-as-served-view |
| time-series / telemetry | silver-conform → time-series gold (serving) |
| tabular / relational (non-temporal) | silver-conform → relational gold |
| scientific / array (HDF5, NetCDF, sim output, spectra, mesh) | object store + catalog |
| media / image | OCR/STT/vision → derived text → (prose path) |
| code / config / input-decks | text + link to outputs |
| email / comms | (prose path) + thread metadata |
| geospatial / CAD | asset store + metadata |
| **unknown** | **quarantine** (fail-safe; mirrors the provenance gate) |

New modality = register a detector + a route; **no platform-code change**.

**RAG is a served *view of gold*, never a silver bypass.** Every modality
flows bronze → silver → **gold** → a served view; RAG is the *vector
projection of curated gold*, the data-as-tool layer (decision 3) is the
*structured projection* — same curated substrate, two views (consistent with
`spec-data-architecture` "RAG = served view fed from gold" and `adr-070`
"medallion gold ↔ shared corpus tier"). "Gold" is modality-specific: for
time-series it's aggregation/serving; **for prose it is curation** — dedup
near-duplicates, select the canonical version, apply access-tier/
classification, promote to the federation corpus tiers. Indexing *silver*
(cleaned but uncurated) inherits duplicate/superseded/mis-tiered noise;
indexing *gold* (curated, deduped, tiered) is cleaner and correctly
access-controlled. RAG indexes the **catalog card / data dictionary** for
non-prose modalities (discovery), never their raw values.

> Note: today's corpus runs the **prototype shortcut** (bronze → chunk →
> embed → RAG, no silver/gold) — which is *why* it carries duplicate scanned
> docs, mis-chunked OCR, and mixed-in telemetry. Building silver + the gold
> curation layer is the fix; RAG then re-projects from gold.

**2. Tiered gold storage (resolves the adr-003 ↔ prd-data-platform conflict).**
Gold has two co-resident tiers, not one:
- **Hot serving tier — TimescaleDB** (already chosen in `prd-data-platform §7.3`):
  interactive point-in-time + short-range queries for agents/tools/dashboards.
- **Cold analytical tier — Iceberg/DuckDB** (`adr-003`): cheap columnar archive
  at scale + batch analytics + time-travel.

`adr-003` is **amended, not reversed** — it correctly chose the analytical
engine but omitted the serving tier. Each gold dataset declares its tier(s);
high-volume series land in both (hot recent window + cold full history).

**3. Data-as-tool (structured-dataset tool primitive) over gold.**
Structured gold datasets are reachable by agents as **tools**, not retrieval,
because structured/temporal queries are deterministic lookups (consistent with
`adr-069` "temporal → direct-inject, not RAG"). The platform provides a generic
`query_dataset(dataset, filters, range, agg)` primitive; consumers register
purpose-named tools over it. Two reference tools per observed/modeled dataset:
`observed_state` and `modeled_state`, plus a **deterministic `compare()`** tool
(returns structured deltas: error, bias, max-divergence) with agent narration on
top — deterministic compute, LLM only for explanation. All exposed through the
**axi MCP**, so any chat host (in-platform or external) reaches them identically.

**4. `site` + `dataset` dimensions are first-class across all tiers.**
Per `adr-050`, `site`/`tenant` is the tenancy axis. It (and a consumer-defined
`source_class` discriminator distinguishing observed vs modeled vs simulated
records) must ride the data through bronze provenance → silver row → gold key →
RAG chunk filter → tool scoping. Default queries are **site-scoped + isolated**;
cross-site is explicit + rides the existing classification/federation machinery.
Connector config declares `--site`.

**5. Generator-producers + a versioned training-dataset class.**
Not all platform data is *ingested* from an external source — some is
**generated** internally by asynchronous batch jobs (e.g. high-fidelity
simulation that synthesizes training data for a downstream model). The platform
treats a **GeneratorJob** as a first-class producer peer to `IngestSource`:
orchestrated by Dagster (per `adr-049`), HPC/batch-scheduler-capable (its
physics/compute body is a consumer plug-in), landing its output through the same
bronze → silver → gold path with `source_class = synthetic-generated`.

Its output is a distinct **training-dataset class**, treated differently from
serving telemetry:
- **immutable + versioned** (snapshot id + content hash),
- **lineage-tracked** — pinned to the generator config + generator-code version
  + the input-data snapshot it consumed,
- stored in the **cold/analytical tier** with time-travel for reproducible
  retraining (it is *not* hot-served and *not* RAG-indexed beyond a catalog
  card),
- referenced by the trained model's metadata (model → exact training-data
  version).

The domain-specific generator body (simulation templates, parsers, consumer-
specific tracking, asset type) is a **consumer plug-in behind the GeneratorJob
seam**;
the platform owns orchestration, landing, versioning, and lineage. This lets an
existing consumer synthetic-data generator port onto the platform as a
Dagster-orchestrated asset, and lets a parallel generator for a *different*
consumer asset type be stood up by swapping only the plug-in body — no
platform-code change. (`source_class` enum gains `synthetic-generated`.)

## Consequences

**Positive.** Document RAG stops being polluted by structured data; structured
data becomes queryable by the right engine; observed-vs-modeled becomes a
first-class conversational capability via MCP; multi-site tenancy is designed in
before backfill; the adr-003 contradiction is resolved.

**Costs / follow-ups.** Adds TimescaleDB as a serving engine (a Postgres
extension — low ops, co-resident). Requires building the silver classifier + the
gold tiers (currently bronze-only). Existing RAG needs remediation: **re-chunk**
mis-chunked documents (keep) and **route the structured subset out** (small,
identifiable) — not a bulk purge.

**Per-spec amendments this ADR drives** (apply in the owning repos):
- `spec-data-architecture.md`: new §3.2.1 modality classifier; §2/§6 dual-tier
  gold (Timescale hot + Iceberg cold); §8.4.1 IngestSink endpoint + edge buffer;
  §8.5.1 data-as-tool/MCP.
- `spec-ingest-contract.md` (consumer repo): add `site`, a consumer asset-type
  field, and `source_class` to the silver row; note the channel-map applies to
  the time-series modality only.
- `prd-data-platform.md`: RDQ-014/015 (central IngestSink endpoint + edge
  buffer/relay); modality-classifier section; Timescale serving-tier row.
- The IngestSink endpoint is **transport-decoupled**: its app/router is mounted
  on the core `serve` extension (see `spec-serve.md`), not a bespoke server —
  the push (`POST /ingest`) path rides the shared HTTP engine + middleware.
- `prd-digital-twin-hosting.md`: generalize `twin_*` tools to
  `observed_state`/`modeled_state` + `compare()` over the gold serving tier.
- `prd-data-platform.md` / `spec-digital-twin-architecture.md`: define the
  **GeneratorJob** producer seam + the versioned **training-dataset** class
  (extends the existing `rom_training_datasets` / RTP-* + Shadow/shadowcaster
  lineage); the consumer's existing physics-sim generator ports as a
  Dagster-orchestrated asset; a parallel generator for a different consumer
  asset type plugs into the same seam.

**References.** adr-003 (lakehouse), adr-007 (streaming-first), adr-049
(orchestration boundary), adr-050 (site/tenant vocabulary), adr-052 (schema-per-
extension), adr-069 (memory/RAG boundary), adr-070 (knowledge architecture);
prd-data-platform, spec-data-architecture, spec-ingest-contract,
prd-digital-twin-hosting; FlowLoopDT `StateSeries`.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
