# SKILL: data.conform_try

**Owner:** `axi data conform-try` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active
**Last updated:** 2026-09-08

## What this skill does

Dry-runs a registered normalizer over one bronze record and returns the
canonical `silver.signals` rows it yields, together with the mistakes that
silver would otherwise absorb without complaint.

Writes nothing. No database, no bronze root, no upsert.

See `conform_try.py` for the function body. The CLI verb is a thin wrapper that
translates flags into a params dict and calls the registered skill function.
The same skill is reachable from any agent persona through
`ctx.registry.invoke("data.conform_try", params, ctx)`, and — because it is
registered with a spec per ADR-072 — it projects to the MCP tool surface as
well. One definition, every pathway, no second implementation to drift.

## Why it exists

`silver.signals` is keyed `(row_hash, channel)` and the upsert is
`ON CONFLICT DO NOTHING`. Every row a normalizer yields from one bronze record
inherits the same `row_hash`, so **two yielded rows with the same channel name
collide and the second is discarded with no error and no warning** — while the
conform funnel still counts both in `rows_out`.

That is invisible in production and obvious here. The same applies to a naive
timestamp landing in a `timestamptz` column, and to a normalizer that sets
`site` or `row_hash` itself and disagrees with what `conform_rows` would have
filled in.

## Inputs / Outputs

| Input | Type | Notes |
|---|---|---|
| `schema_ref` | str | Which normalizer. **Omit to list what is registered** — the fastest way to discover your entry point did not load. |
| `record` | object or JSON string | The bronze record. Payload under `row`, matching what the edge writes. |

Returns a uniform `SkillResult` (`{ok, value, errors, actions_taken}`).
`ok` is false when any warning fires, so a CI caller can gate on it.

## Safety

Read-only and side-effect free. It calls a registered normalizer, which is a
pure function by contract; a normalizer that performs I/O is already outside its
contract and this skill will not stop it.

Discovery honours the portfolio boundary: a normalizer from a distribution that
does not declare `axiom.portfolio_member` is never imported, so this skill
cannot be used to execute arbitrary installed code.
