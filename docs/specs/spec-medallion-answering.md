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

**Namespace:** `data.*`, not `gold.*`. An extension owns exactly one skill namespace — its
CLI noun (`authz`→`audit`, `publishing`→`press`, `data_platform`→`data`) — so these live under
`data`, and the MCP tool names are `axiom_data__aggregate` and friends. (Corrected from the
first draft of this spec, which proposed a second namespace.) The verbs were `data.gold_*` until
the tier became an argument: `aggregate` and `series` take `tier`, and `catalog` / `describe`
answer for every tier, so a caller does not have to know which medallion they are on before
they can phrase the question.

| Capability | Inputs | Returns (envelope §3) |
|---|---|---|
| `data.catalog` | `tier?` | the tier's tables/views (`axi data tables` is the gold spelling) |
| `data.describe` | `object, tier?` | columns: name, type |
| `data.aggregate` | `table, column, fn, window?, filter?, group_by?, allow_mixed?, allow_mixed_units?, include_synthetic?, tier?` | one scalar; `fn ∈ {sum, mean, min, max, std, count}` — or one scalar per group |
| `data.series` | `table, column, bucket, time_column, fn?, window?, filter?, group_by?, allow_mixed?, allow_mixed_units?, include_synthetic?, tier?` | bucketed series `[{t, value}]` — or one named line per group, the analytics tool's input shape |
| `data.roles` | `include_synthetic?` | which quantities the fleet can answer, and which sites answer each |
| `data.compare` | `role, bucket, sites?, fn?, window?, include_synthetic?, allow_mixed_units?` | one quantity across sites on a shared time axis |

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

### 2.1 The population guard

A gold table may carry columns that say what a value **is** rather than what it measures:
`unit`, `source_class`, `model_ref`, `role`, `derivation`, `quality`. Where it does, two rows
can look identical and mean different things — one came off an instrument, the other out of a
model — and an aggregate that blends them is indistinguishable from a real one once it leaves
this module.

`aggregate` and `series` therefore compute the **composition** of the matched rows
over those columns before answering, and split them in two:

- **Mixing-hostile — `unit`, `source_class`.** More than one distinct value and the verb
  **refuses**, raising `MixedPopulation` instead of returning a number. A mean over degC and K
  is not a temperature; a mean over a measurement and a prediction is not a measurement of
  anything, and that is the exact failure the source manifest's mandatory `model_ref` exists to
  prevent. NULL counts as a distinct value: "unit unstated" is a different population from
  "unit degC", and collapsing them is how a unitless value gets served as though it carried one.
- **Mixing-notable — `model_ref`, `derivation`, `role`, `quality`.** Reported as a provenance
  note, not refused. Two models averaged together is one number attributable to neither; a
  `derived` channel summed beside the `raw` channels it was computed from counts that physics
  twice. Both change what the number means without making it nonsense.

Three ways through a refusal, and the error message names all three because a caller given only
"no" reaches for raw SQL, which has no guard at all:

1. **`group_by`** — split the answer so each value summarises one population. `group_by=
   ["source_class"]` on `aggregate` *is* the measured-versus-predicted comparison; on
   `series` it is the overlay, one named line per population over the same buckets.
2. **`filter`** — narrow to one population up front.
3. **`allow_mixed`** — state that the blend is intended. The answer then carries a note saying
   so, so a reader downstream can still tell.

`allow_mixed_units` is the narrow form of the third: it waives the unit check alone, for a
caller who knows the rows share a scale despite their labels. It does not excuse a blend of
source classes, because a shared scale says nothing about whether a number was measured.

A series is judged over the **whole window**, not per bucket. A line whose unit changes
halfway is the worst version of a blend: every bucket is internally consistent and the line is
a lie.

An answer over one unit says which, in the envelope's `unit` field, read from the same
composition the guard judged so the unit and the refusal describe the same rows. Grouping by
`unit` leaves no single unit to report, so the field is absent and each group carries its own.
All-undeclared rows agree with each other and answer as `unit not declared`, never as
dimensionless.

There is one guard. Two were built in parallel (a whole-population check here and an in-pass
unit check beside it) and merged into this one, because two guards with two waivers answer the
same question differently: the in-pass check refused a series already split by unit, and it
refused `count`.

`count` is exempt from refusal: counting rows across units or source classes is a real count of
rows, not a quantity the mixing corrupts. Its composition is still reported.

A table with none of these columns is unaffected — no extra query, no composition block.

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
    "rows":   1234,
    "attribution": { "unit": "degC", "source_class": "predicted",
                     "model_ref": "corral:forecaster-v2" },   // §2.1, single-valued columns only
    "composition": { "quality": { "good": 1200, "suspect": 34 } },  // §2.1, what it was made of
    "note":   "quality spans 2 values: good (1,200), suspect (34)"
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
- **`attribution` says what the answer is; `composition` says what it was made of.** A
  provenance column that resolved to one value across every matched row becomes a claim the
  answer carries — this number is degC, it is predicted, it came out of that model. A column
  that did not is deliberately absent from `attribution` and appears in `composition` instead,
  because summarising it away is the thing §2.1 refuses to do.

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
