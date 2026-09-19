# SKILL: run-ingest

**Owner:** PLINTH (`axi plinth`)
**Kind:** skill (gated — `guarded_act`)
**Status:** active
**Last updated:** 2026-05-29

## Purpose

Drive one source → bronze → RAG pass for a registered connector.
Bronze gets the substrate of record (provenance-gated,
content-addressed); the RAG served view gets the embeddings PLINTH's
downstream consumers retrieve. Per ADR-049, this is the lakehouse
path — Dagster owns the schedule; PLINTH owns the safety wrap.

## When this skill fires

- A per-source Dagster sensor `corpus__<slug>_sensor` polls
  `IngestSource.list_changed(since)` every minute; when new items
  appear it materializes that source's `corpus__<slug>` asset, which
  calls into the same code. (One sensor + asset per connector of any
  registered kind; the source is constructed through the kind's
  `SourceKindProvider`.)
- Operators invoke `axi plinth run-ingest --connector <name>`
  ad-hoc — backfill, smoke test, after a rule-set change.
- PLINTH's heartbeat may call this directly when Dagster is unavailable
  (lean-tier fallback).

## Action

```
axi plinth run-ingest --connector <name> \
    [--since <ISO-8601>] \
    [--volume-mode off|refuse|confirm] \
    [--json]
```

The run does, per item:

1. `IngestSource.fetch(item_id)` — pull bytes + metadata
2. `BronzeWriter.write(item)` — provenance-gate then write content +
   sidecar (`ALLOW` lands; `QUARANTINE` lands aside; `EXCLUDE` records
   the decision and skips)
3. `embed_bronze_record(record, item, store)` — chunk + embed + upsert
   into RAG (`ALLOW` only)

## Safety — `guarded_act`

Every per-item write routes through `guarded_act` per ADR-045 D6:

- **Reversibility gate** — `reversible=True` (re-running a connector
  re-derives all bronze + RAG state from the origin source; nothing
  here is destructive).
- **Volume bound** — default 10 items per tick
  (`AGENT_ACTION_DEFAULT_MAX_PER_TICK`). Override per-deploy with
  `PLINTH_DATA_PLATFORM_INGEST_MAX_PER_TICK`. Default mode is
  `confirm`: an over-limit batch returns `would_proceed` so an operator
  re-runs with `--volume-mode off` for explicit consent (backfill).
- **Hard disable** — `PLINTH_DATA_PLATFORM_INGEST_DISABLE=1` halts the
  run before any external write.
- **Sentinel-file pause** — drop a file at
  `$AXIOM_STATE/agents/plinth/pause.data_platform.ingest.json` to pause
  all DP ingest without touching env vars.

## Provenance gate (non-negotiable)

The bronze write IS the provenance gate. EXCLUDE items never reach the
embedder — the decision is recorded in `_excluded/` and the bytes never
land in the lakehouse. The gate runs *before* anything that touches the
substrate of record; there is no upstream of it in this skill.
