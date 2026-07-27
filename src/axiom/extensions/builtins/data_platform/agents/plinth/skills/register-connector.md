# SKILL: register-connector

**Owner:** PLINTH (`axi plinth`)
**Kind:** skill (deterministic — no LLM judgment)
**Status:** active
**Last updated:** 2026-05-29

## Purpose

Persist an :class:`IngestSource` configuration so PLINTH can re-run it
deterministically on demand or via Dagster. Connectors are the deploy-
side declaration of "what does this site ingest from"; the registry
lives at `$AXIOM_STATE/plinth/connectors/<name>.toml`.

## When this skill fires

The operator (or the consumer's deploy runbook) invokes
`axi plinth register-connector` with the source-binding parameters.
PLINTH writes the TOML. Subsequent `run-ingest` calls load it; the
Dagster code location enumerates the registry at load time and builds
one `corpus__<slug>` asset + `corpus__<slug>_sensor` per connector.

## Action

```
axi plinth register-connector \
    --name <name> \
    --kind box \
    --folder-id <Box folder id> \
    --bronze-root <path on host> \
    --rag-dsn-env DP1_RAG_DSN \
    [--provenance-rules-file <path>] \
    [--default-disposition allow|quarantine|exclude] \
    [--default-tier <tier>] \
    [--box-session-dir <path>] \
    [--force]
```

Idempotent on identical inputs. Re-registering with different fields
without `--force` raises — silent overwrites would mask typos against
an existing connector.

## Safety

Deterministic; nothing autonomous. The connector file IS the durable
record of intent; bronze + RAG writes only happen on subsequent
`run-ingest` calls, which apply `guarded_act` per ADR-045 D6.
