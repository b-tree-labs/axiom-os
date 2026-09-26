# Spec: Medallion Answering — generic gold-tier query capabilities

**Status:** Draft (2026-09-18) · **Decision:** ADR-115 · **Product:** [prd-data-platform.md → Generic answering surface](../prds/prd-data-platform.md) · **Related:** ADR-049 (cross-extension reads ride the data platform), ADR-052 (schema-per-extension), ADR-056 (skills), ADR-072/073 (capability projection / registry-driven surfaces), ADR-113 (deterministic quantitative answering), ADR-114 (identity + effect gates), [spec-analytics-tool.md](spec-analytics-tool.md), [spec-output-provenance-gate.md](spec-output-provenance-gate.md).

## 1. Purpose

Give a **base Axiom install** the ability to answer quantitative questions about whatever data
the operator has loaded into the gold tier — deterministically, provenance-stamped, access-gated
— with **zero domain code**. Domain packs specialize this surface by *declaration*, not by
hand-writing per-quantity SQL.

## 2. The four capabilities

All are ADR-056 skills in the `data_platform` extension, declared `side_effects=False` and
projected via `surfaces=("cli","mcp","agent_tool")`.

**Namespace:** `data.gold_*`, not `gold.*`. An extension owns exactly one skill namespace — its
CLI noun (`authz`→`audit`, `publishing`→`press`, `data_platform`→`data`) — so these live under
`data`, and the MCP tool names are `axiom_data__gold_aggregate` and friends. (Corrected from the
first draft of this spec, which proposed a second namespace.)

| Capability | Inputs | Returns (envelope §3) |
|---|---|---|
| `data.gold_tables` | — | the gold tier's tables/views |
| `data.gold_describe` | `table` | columns: name, type |
| `data.gold_aggregate` | `table, column, fn, window?, filter?` | one scalar; `fn ∈ {sum, mean, min, max, std, count}` |
| `data.gold_series` | `table, column, bucket, time_column, fn?, window?, filter?` | bucketed series `[{t, value}]` — the analytics tool's input shape |

Grammar decision (ADR-115): **one verb with a `table` argument**, not per-table projected verbs.
Rationale: tool-count stays constant as data grows; discoverability comes from `gold.tables` +
`gold.describe` (the model lists, then asks). A domain pack MAY additionally project named verbs
(§5) where curation matters.

`filter` is a **restricted predicate grammar** (column ⟨op⟩ literal, AND-joined; ops
`= != < > <= >= in`), compiled to parameterized SQL — never string-spliced, never raw SQL from
the caller. Identifiers are validated against the introspected schema (unknown table/column →
typed error, not SQL error); literals must be quoted strings or numbers, so a bare identifier
cannot smuggle a column reference past the grammar.

`between` is deliberately **absent**: `>=` plus `<=` expresses the same range without an
AND-ambiguity in the parser, and a smaller grammar is a smaller attack surface. `bucket` is
likewise validated against a closed interval grammar (`<n> <unit>`) before it is bound.

## 3. Result envelope

Every capability returns the same JSON envelope the provenance gate and analytics tool already
consume (as-built in the domain exemplars):

```json
{
  "data":       { ... },                    // null when the window has no rows
  "unit":       { "<field>": "<unit>" },    // from column metadata when declared (§5), else omitted
  "provenance": {
    "source": "gold.<table>",
    "method": "mean(<column>), window 2026-09-01..2026-09-18, filter <normalized>",
    "rows":   1234
  }
}
```

- **Empty ≠ error:** no rows → `data: null` + a provenance note; the assistant abstains rather
  than inventing (`spec-output-provenance-gate.md`).
- **"No rows" and "no values" are different facts, and the note MUST say which.** A window that
  matched nothing and a window whose rows are all null in the aggregated column both yield
  `data: null`, but they call for opposite responses — widen the window, versus stop asking this
  column. The statement therefore selects `count(*)` alongside `count(<column>)` so the note can
  distinguish them. (`count` of nothing remains a real `0`, not null.)
- **Numbers are emitted in the stored unit.** Unit *conversion* is the answering layer's job
  (gate `equivalence_scales`); the envelope's `unit` block is what makes conversion checkable.

## 4. Access control (fail-closed)

- Verb-level: `allowed_principals` + ADR-114 site rules apply as to any capability.
- Row/column-level: gold tables MAY carry an `access_tier` column and tables an access-tier
  registration; the verbs filter to the caller's tier and **fail closed** when tier metadata is
  missing for a restricted-marked table (mirrors the RAG retrieve access filter). A generic verb
  must never widen access relative to the curated verbs it generalizes.
- **An empty grant set is a refusal, not an empty filter.** A caller holding no tiers against a
  tiered table raises `AccessTierUnavailable`. Rendering it as `IN ()` would turn the deny into a
  database syntax error, and "you hold no grants" is a different answer from "no rows matched" —
  the caller has to be able to tell them apart. The WHERE renderer refuses to build an empty
  `IN` at all, as defense in depth.
- **An applied tier filter is part of the provenance.** The filter changes which rows the number
  came from, so it is recorded in `method`. Without it, two callers with different grants receive
  different numbers carrying identical provenance, and the discrepancy is unexplainable after
  the fact.

## 5. The declaration layer (domain packs)

A pack registers **named quantities** as data, not code:

```toml
[[quantity]]
name        = "peak_output_power"          # projects as <ns>.peak_output_power
table       = "signals"                    # gold table
column      = "output_power_w"
fn          = "max"
unit        = "W"                          # + preferred display unit, e.g. "MW"
filter      = "quality != 'corrupt'"       # curation: exclusion rules
provenance_note = "corrupt console rows excluded"
```

The platform compiles the declaration onto the §2 machinery: one executor, N declarations. A
pack MAY still ship hand-written SQL verbs for shapes the grammar can't express (multi-table,
window functions) — those remain ordinary skills, and the coverage map records which quantities
are declaration-backed vs bespoke.

## 6. Coverage integration

`gold.tables`/`describe` output feeds the coverage reconcile (ADR-113): every *gated* quantity
must have a producer among {generic verbs × declared quantities × bespoke verbs} in the
**installed** capability set. No hardcoded verb tables anywhere in a serving layer.

## 7. Non-goals

- Free-form SQL execution for callers (the filter grammar is deliberately small).
- Cross-extension OLTP joins (ADR-049: reads ride the data platform).
- Tier-4 dynamical models (domain model tier, per ADR-113).
- Write paths of any kind (`side_effects=False` is load-bearing).

## 8. Acceptance

- A fresh install + one ingested table can answer "mean of `<column>` last week" via chat, CLI,
  and MCP with identical values and a provenance block. Uninstalling the data platform returns
  the same question to a clean abstention.
- A declared quantity and the equivalent `gold.aggregate` call return byte-identical `data`.
- A restricted-tier row never appears in any generic-verb result for an unprivileged caller
  (test proves the filter can fail closed).
