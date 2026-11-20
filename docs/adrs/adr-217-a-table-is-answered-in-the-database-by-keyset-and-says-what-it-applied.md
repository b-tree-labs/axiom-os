# ADR-217: A table is answered in the database by keyset, and says what it applied

**Status:** Draft (2026-10-28), for review
**Related:** [spec-data-table](../specs/spec-data-table.md) (phase 2), [ADR-128](adr-128-medallion-tier-boundaries.md) (consumers read gold), [ADR-157](adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md) (every call is bounded), ADR-115 (generic answering over the served tiers), ADR-214 draft (one chart spec), ADR-213 draft (previewers are data shapes)

## Context

The platform has one table, described by a document (`TableSpec`) and drawn by
one renderer on every surface. TableSpec 1.0 pages a result the caller already
holds, and sorts it in memory over a 10,000-row window (`rows_needed`). That is
honest, because the window is named, but it cannot scale: a sort of a
hundred-million-row gold relation is a sort of its first ten thousand rows.
The web renderer can now virtualize over a `RowSource` that pushes sort and
filter to wherever rows live, so the server half is the gap.

Four decisions here are expensive to change once saved views, chat answers and
other nodes depend on them:

1. the shape of the document that names a view over gold (TableSpec 2.0);
2. how a window over a sorted, filtered relation is addressed (offset or keyset);
3. what the server promises about counts, sortability and bounds;
4. what the answer carries so every surface can say what was done.

## Decision

**TableSpec 2.0 is additive over 1.0.** Every 1.0 document parses and writes
back byte for byte. A document that uses a 2.0 field must say
`schema_version: "2.0"`, so an older reader refuses it by version instead of
dropping what it does not know. 2.0 adds: typed columns (`type` from the cell
registry, `unit`, `format`, a width object, `colorscale`, `profile`,
`filterable`); a multi-key `sort` (a single object still parses); `filter.where`
with a closed operator set (`eq ne lt le gt ge between in not_in is_null
not_null contains starts_with`) whose values are typed by the column; `window`
(offset or cursor, bounded like a page); `id`; `source` (`verb`, `inline` or
`ref`); and `links` and `group`, carried but not yet served. Each cell type has
one text form, used by the terminal, export and copy, and held equal to the web
registry by a shared vector file.

**`data.table` answers a door spec in the database.** It is a data-door verb
(CLI, MCP, agent tool, and forwarded by a bound install like every gold read):

- **Gold only.** The relation is resolved in the `gold` schema; there is no
  tier argument. The caller's site scope, access tiers and the self-test
  hold-out are the same predicates every gold verb ANDs on, and row-level
  security on the serving role applies underneath.
- **Pushdown.** `where`, `equals`, `search` and the sort compile to one
  parameterized statement. Identifiers are checked against the catalogue and
  quoted; values are bound and cast to the column's catalogue type. No caller
  string becomes SQL text.
- **Keyset, never deep OFFSET.** Every order is made total by the relation's
  unique row key (a unique index over NOT NULL columns, or a declared key);
  ties follow the last sort key's direction, so a uniform order is one row
  comparison and an index condition. A window returns `next`, an opaque cursor
  holding the last row's order values and a fingerprint of the query; a cursor
  from another sort or filter is refused. `OFFSET` is used only up to 10,000
  rows. A larger offset is an **estimated jump**: the planner's histogram gives
  the leading key's value at that fraction, the window seeks to it, and
  `position` says the row number is an estimate while the rows are exact.
- **Nulls last in both directions.** A nullable leading key is read in two
  segments, its values and then its nulls, each an index scan in either
  direction, so a descending sort needs no second index to keep nulls last.
- **Sortable means indexed or declared cheap.** The leading sort key must be
  ordered by a btree index (after any columns the query holds equal, in a
  direction the index can scan), or the relation must be small, or a
  declaration must list the column as cheap. Later keys must follow in the same
  index or the leading key's ties must be small. Otherwise the sort is refused
  with the reason and the index that would serve it. `check_declarations` is
  the conformance test of the same rule over declared table sources, so a
  missing index fails a test, not a user.
- **Counts are exact, estimated or unknown.** A bounded probe (at most 100,000
  rows and 250 ms, run only when the estimate is under 200,000) makes a small
  count exact. Otherwise the count is the planner's estimate for the filtered
  statement, or the time-series store's approximate
  row count for an unfiltered hypertable, labelled `estimate`. `count: "exact"`
  is its own bounded call; one that does not finish stays an estimate and says
  why. With no statistics the count is `unknown`.
- **Bounded (ADR-157).** A row cap of 1,000 per window and the caller's budget,
  a statement timeout, a planner-cost budget per caller class (checked by
  EXPLAIN before the window runs), and the byte budget. A bounded answer lists
  why in `bounded`; a refused one says why and what would succeed.

**The answer carries what was done:** `rows`, `keys`, `next`, `count`,
`position`, `applied` (the sort, the tie-break, the filter, the platform scope
and the window), `bounded`, `provenance` (`reads` in the shape chat citations
use, `as_of` and the relation's gold watermark) and `uncertainty` (per-row
coverage of the columns that declare one, every field present). It also carries
`spec` and `text`, so chat recognises it as a table and the terminal prints
the same window.

**`data.profile` is computed on the filtered query,** sampled above a million
rows with a seeded block sample that is labelled as one, and cached by (query
fingerprint, gold watermark). The watermark is the relation's write counters,
chunks included; a relation without them (a view) is not cached, and says so.

## Options considered

- **OFFSET/LIMIT for every window.** Simple, and a scrollbar maps onto it
  directly, but the cost of row N is N: the last page of a hundred million rows
  reads them all. Kept for offsets up to 10,000 only.
- **Server-side cursors held open between windows.** Exact positions, but a
  connection per reader for as long as they scroll, which ADR-157's per-caller
  concurrency cannot afford, and nothing survives a reconnect or a second node.
- **A materialized row number per sort.** Exact jumps, but a write per sort
  order per change to gold, and gold is append-heavy.
- **Sort anything, with a fallback window.** What 1.0 did. It answers a
  different question than the one asked; an explicit bounded fallback stays
  possible per source, stated in `applied`.
- **Count everything exactly.** `count(*)` over a large filtered relation is a
  full scan on every query change. An estimate that says it is one is the
  honest default.

## Consequences

- Every gold relation served as a table needs a unique row key and an index
  per sortable leading column. The conformance check is how a deployment finds
  out before a reader does.
- A cursor is valid only for its query; changing the sort or filter starts from
  the top. The web table already clears its cache on a query change.
- An estimated jump lands near, not at, the requested row. The table shows the
  rows as exact and the position as approximate.
- `group`, pivot and `liveRowSource` are phase 4; `ref`, `spark` and `bullet`
  cells, export and copy-as-code are phase 3. The document already carries
  their fields where the spec names them.
- The profile's sample is block-level, which is fast but can over-represent
  correlated blocks; it is labelled as a sample for that reason.
