# ADR-049 — Data-platform orchestration boundary (Dagster vs Sense vs the source contract)

**Status:** Accepted (2026-05-28) · Supersedes the "Source-abstraction reconciliation" block in spec-data-architecture.md ("Architecture update")

## Context

The data platform's heavy tier is **Dagster + dbt + Iceberg/DuckDB**. Axiom's
`signals`/**Sense** subsystem independently provides source polling, scheduling,
a work queue, and change-detection (watermarks) for the **agent fleet**. On the
surface both "poll a source → detect change → enqueue → run on a schedule" — which
reads as **architectural duplication (a bad smell)**.

An earlier reconciliation (PR #256) tried to make **Sense the data platform's
canonical source layer**. That over-centralized on Sense, obscured that **Dagster
is the lakehouse orchestrator**, and left genuine ambiguity about which path a new
pipeline should take. This ADR removes that ambiguity.

## Decision

**There is one orchestrator per pipeline, chosen by a single binary question:
*does this data become a lakehouse asset, or is it an ephemeral agent signal?***

1. **Dagster owns the data platform.** It is the only thing that materializes
   lakehouse assets (Iceberg tables, dbt models, lineage, backfills). Data
   destined for the **medallion → Dagster** (sensors, schedules, assets).
2. **Sense is the agent-fleet signal subsystem, not a data-pipeline orchestrator.**
   Ephemeral signals consumed by agents (briefings, triggers) **→ Sense**. Sense
   is orthogonal to the data platform; we do **not** extend it to drive lakehouse
   pipelines.
3. **These never overlap for the same data** — it either has a durable table home
   (Dagster) or it does not (Sense). The destination is the decision; it is binary.
4. **`IngestSource` is a portable *connector* contract, not an orchestrator.** A
   connector (Box, …) is written once against `list_changed` / `fetch` and
   **consumed by whichever orchestrator the deployment tier provides** — a Dagster
   sensor/asset in the heavy tier; a minimal direct runner on a lean/offline node
   with no lakehouse. **No deployment runs both for the same data.** The portable
   connector is what *removes* duplication (the connector logic is written once);
   the orchestrators are tier-specific *runtimes*, not duplicated logic.
5. **Native-first inside the heavy tools.** Use Dagster sensors/schedules + dbt
   sources/freshness rather than re-expressing them; `IngestSource` is a seam only
   for sources lacking a native connector.
6. **PLINTH operates the platform; it does not replace Dagster.** PLINTH (the
   data-platform agent) registers connectors, **triggers/monitors Dagster runs**,
   applies the provenance gate + `guarded_act` (RACI graduation-safety) to external
   mutations, and surfaces corpus/lakehouse health. Scheduling + materialization
   authority is Dagster's; agent judgment + safety is PLINTH's.

## Consequences

- **No two-scheduler ambiguity.** "Which path?" is answered by destination, and
  it's binary — never "both."
- **Lean/offline nodes** (no Dagster — nuclear facilities are offline-first) still
  ingest via the portable connector + a minimal runner; the lakehouse is not
  required to use a source.
- **Exercise #1 (full medallion) is a Dagster pipeline:** Box sensor → bronze
  asset (provenance gate) → dbt silver/gold → RAG-embed asset (served view). Sense
  is not involved. (It would be, only on a lean RAG-only node — which was rejected
  for #1.)
- Sense stays in its lane; PLINTH does not reinvent Dagster's scheduler.

## Why not unify into one orchestrator?

Neither subsumes the other, so a forced merge would be worse than the boundary:
- **Dagster can't replace Sense** in the lean base — it needs a running daemon + a
  database + isn't offline-first; Sense's zero-dep file model runs everywhere.
- **Sense can't replace Dagster** — it has no assets, lineage, materialization,
  backfill, or dbt integration, all of which the lakehouse requires.

The boundary (destination) keeps them non-overlapping, which is what makes "two
engines" *not* duplication.
