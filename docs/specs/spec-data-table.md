# Spec: Data Table

**Owner:** Platform (scidisplay + appkit) • **Status:** Draft • **Last updated:** 2026-10-28
**Key ADRs:** [ADR-128](../adrs/adr-128-medallion-tier-boundaries.md) (consumers read gold) •
[ADR-157](../adrs/adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md) (bounded calls) •
ADR-214 (one chart spec, in review) • ADR-213 (upload pipeline and previewers, in review)

## Overview

A table is how a researcher first meets data, and usually how they last check it. This spec makes
the platform's one table good enough for that job at any size. It must open on a billion rows as
quickly as on ten, show values with their units, uncertainty and quality, and hold objects you can
open, not only text. It should tell you the shape of each column before you read a row, and hand
you a chart, an export or the code to reproduce the view.

There is exactly one table, as there is one chart:

| Layer | Owns |
|---|---|
| `scidisplay/table_spec.py` (Axiom) | **TableSpec**: the document describing a view (columns, sort, filter, window, grouping). Inert, versioned. |
| The data door (Axiom) | **Row sources on the server**: answering a TableSpec over gold with sort, filter and grouping pushed into the database. |
| `table/DataTable.tsx` (appkit) | **The renderer**: a virtualized grid over a `RowSource`, plus a cell-type registry. |
| Terminal, chat, MCP | Render the same spec: text pages, chat artifacts, structured results. |

Four surfaces, one document. A table in a chat answer, on the web, in the terminal and behind MCP
cannot disagree about what "sorted by value" means.

### Where this starts from

- **TableSpec 1.0** exists: `columns` (`id`, `label`, `sortable`, `align`, `uncertainty_from`),
  `sort`, `page` (numbered, default 25, **capped at 1,000**), `filter` (`search`, `equals`),
  `render_text`, `tabulate`, and a chart offer per table via `chart_choice.offer_for`.
- **DataTable** exists in appkit and is deliberately the only table. Its props are
  `to_datatable_props()` output. It is used by the Library browser and chat artifacts, and now
  chat markdown tables.
- **The ceiling:** `rows_needed()` sorts **in memory over a 10,000-row window**. A sort on a large
  result is therefore a sort of its first ten thousand rows. That is honest (the window is named)
  but it doesn't scale. This spec replaces it with sort pushed into the database.
- **In flight (appkit):** column resizing, virtualization over a `RowSource` interface, and a
  cell-renderer registry, delivered with the Library port. This spec is their contract.

## Principles

1. **The browser holds a window, never the result.** Memory and DOM are bounded by what is on
   screen plus overscan, whatever the row count.
2. **Work goes to the data.** Sort, filter, grouping, aggregation and column profiles are computed
   where the rows live (gold, through the data door), never by fetching and slicing.
3. **Counts are honest.** A total is exact, estimated (and labelled `~`), or unknown. Never a
   guess shown as fact.
4. **Values are never quietly changed.** A value marked bad by an expectation shows as flagged with
   its reason, not as blank and never as zero. An absent value is visibly absent, not a "-" that
   reads as a minus sign.
5. **One renderer per thing.** Cell sparklines use the chart renderer; cell previews use the
   preview registry. The table never grows a second drawing path.
6. **Reproducible.** Every view is a document: it can be saved, shared, cited, exported with
   provenance, and turned into the code that produces it.
7. **Contributed code never runs in the page.** Extensions contribute cell *types* as data shapes
   that platform renderers draw, as previewers do (ADR-213).

## Contracts

### TableSpec 2.0 (additive over 1.0)

`schema_version: "2.0"`. Every 1.0 document parses as 2.0 with defaults. All new fields are
optional.

```jsonc
{
  "schema_version": "2.0",
  "kind": "rows",                       // open registry, unchanged
  "title": "…",
  "id": "tbl_…",                        // stable view identity: saved views, persisted widths
  "source": {                           // NEW: where rows come from
    "type": "verb",                     // "verb" | "inline" | "ref"
    "verb": "data.table",               // a data-door verb answering a TableSpec
    "args": { "dataset": "…", "window": { "from": "…", "to": "…" } }
  },
  "columns": [{
    "id": "value", "label": "Value", "sortable": true, "align": "right",
    "uncertainty_from": "value_sigma",
    "type": "quantity",                 // NEW: cell type from the registry (see below)
    "unit": "kW",                       // NEW: declared unit; UNDECLARED is shown as such
    "format": { "digits": "auto" },     // NEW: type-specific formatting options
    "width": { "initial": 120, "min": 60, "max": 480 },   // NEW
    "resizable": true, "pinned": "left",                  // NEW: "left" | "right" | null
    "colorscale": { "palette": "cividis", "domain": "column" },  // NEW: optional
    "profile": true,                    // NEW: show the column profile header
    "filterable": true                  // NEW
  }],
  "sort": [{ "column": "time", "direction": "desc" }],   // CHANGED: a list (multi-key); a single object still parses
  "filter": {
    "search": "…",
    "equals": [["site", "a"]],
    "where": [                           // NEW: structured predicates
      { "column": "value", "op": "between", "value": [0, 100] },
      { "column": "quality", "op": "in", "value": ["good", "suspect"] },
      { "column": "note", "op": "is_null" }
    ]
  },
  "group": {                             // NEW (phase 4): grouping and aggregation
    "by": ["site"],
    "aggregates": [{ "column": "value", "fn": "mean" }, { "column": "*", "fn": "count" }]
  },
  "page": { "number": 1, "size": 25 },   // KEPT for text surfaces (terminal, chat summary)
  "window": { "offset": 0, "limit": 200, "cursor": null },   // NEW: what a scrolling surface fetches
  "links": { "charts": ["fig_…"] }       // NEW: linked charts (brushing and selection)
}
```

**Rules:**
- `page` and `window` coexist. Text surfaces use pages. Scrolling surfaces use windows. Both
  are bounded by `PAGE_SIZE_MAX` (1,000) per request. A window is never a request for
  everything.
- `where` operators are a closed set: `eq ne lt le gt ge between in not_in is_null not_null
  contains starts_with`. Values are typed by the column. Unknown columns or operators are
  `SpecFormatError`.
- `sort` on a column the source can't sort in the database is refused with a reason, unless the
  source offers an explicit bounded fallback. The response then states the bound (`"sorted over the
  first 10,000 rows"`), as today.

### RowSource (client contract, appkit)

```ts
interface RowSource<Row> {
  key(row: Row): string;                                  // stable identity; selection survives refetch
  count(q: Query): Promise<Count>;                        // {kind:"exact"|"estimate"|"unknown", n?}
  fetch(q: Query, range: {offset: number; limit: number} | {cursor: string; limit: number},
        signal: AbortSignal): Promise<{rows: Row[]; next?: string; stats?: FetchStats}>;
  profile?(q: Query, column: string, signal: AbortSignal): Promise<ColumnProfile>;
  subscribe?(q: Query, onChange: (c: Change) => void): () => void;   // live tables
}
type Query = { sort; filter; group?; columns: string[] };   // the spec's view, minus paging
```

Implementations:
- `arrayRowSource(rows)` sorts and filters locally. It's for small inline tables and today's callers.
- `doorRowSource(spec)` POSTs the spec with a window to the data door and passes the abort
  signal through, so scrolling past a window cancels its query.
- `liveRowSource` (later) is a door source plus `subscribe` for appended rows (new readings).

**Renderer obligations:**
- Request only visible rows plus overscan.
- Keep an LRU of fetched windows, capped (default 20 windows).
- Show placeholder rows while loading.
- Cancel requests that are no longer visible.
- Never hold more than the cap.

### Server verb `data.table` (Axiom, data door)

- **Input:** a TableSpec with `source.type = "verb"` and a window.
- **Output:**

  ```jsonc
  { "rows": [...], "keys": [...], "next": "cursor|null",
    "count": { "kind": "exact|estimate|unknown", "n": 0 },
    "applied": { "sort": [...], "filter": {...} },  // what was ACTUALLY applied, for honesty
    "provenance": { "reads": [...], "as_of": "…" },  // the reads made (same shape as chat citations)
    "uncertainty": {...} }                            // per the serving envelope
  ```

**Rules:**
- **Gold only** (ADR-128). The verb resolves datasets through the gold catalog, under the
  caller's tenancy and RLS.
- **Keyset (cursor) pagination for sorted reads,** never `OFFSET` deep into a large table. The
  cursor encodes the sort key and row key of the last row. Offset windows are allowed only
  for small results or estimated jumps (the scrollbar dragged to 73%), where the verb answers
  with the nearest keyset position and says so.
- **Pushdown:** `where` and `sort` compile to parameterized SQL on the gold relation. A
  sortable column must be indexed or declared cheap. A column declared sortable without an
  index fails a conformance test, not a user.
- **Counts:**
  - exact when cheap (under a threshold, or a maintained count);
  - otherwise an estimate from planner statistics or the time-series store's approximate row
    count, labelled `estimate`;
  - exact on request, as a separate bounded call.
- **Bounds** (ADR-157): per-call row cap (1,000), statement timeout, cost budget per principal. A
  refused or truncated call says why in the envelope.

### Column profile `data.profile`

- **Output:**

  ```jsonc
  { "column": "value", "type": "quantity", "unit": "kW",
    "n": 1234567, "nulls": 0.012, "distinct": { "kind": "estimate", "n": 4021 },
    "min": 0, "max": 1102, "mean": 312.4, "quantiles": [p5, p25, p50, p75, p95],
    "histogram": { "edges": [...], "counts": [...] },     // numeric and time
    "top": [{ "value": "a", "n": 9123 }, ...],              // categorical, top 10
    "flags": { "bad": 31, "suspect": 4 },                   // from expectations
    "basis": { "kind": "exact|sample", "rows": 1000000 } }  // say when it's a sample
  ```

- **Computed** on the *filtered* query, server-side. A sample is used above a threshold and
  labelled. The result is cached by (query hash, gold watermark).

### Cell-type registry

A cell type maps a column type to a renderer and a text form. The text form is what terminal,
export and copy use, so every surface agrees. Platform types:

| Type | Shows | Text form |
|---|---|---|
| `text` | Truncated with full text on hover or focus | as is |
| `number` | Decimal-aligned, digits governed by `uncertainty_from` when set | formatted number |
| `quantity` | Value, unit and ± uncertainty; unit undeclared is shown as such | `335 ± 12 kW` |
| `time` | Absolute, with a relative hint; timezone explicit | ISO 8601 |
| `bool`, `enum`, `state` | Chip using the palette registry | label |
| `quality` | A flag with its reason (`sentinel:0`, `consistent:…`) | `bad (sentinel:0)` |
| `ref` | **A clickable object**: `{kind, id, label?}`; opens via the host's resolver | `kind:id` |
| `spark` | A sparkline or mini-histogram, drawn by the chart renderer's micro kinds | min–max summary |
| `bullet` | Value against a limit or target | `value / limit` |
| `nested` | Expands inline to a JSON tree, list or sub-table | compact JSON |
| `link`, `code`, `image` | URL, code span, thumbnail through the preview registry | URL / code / filename |

**Rules:**
- **Object refs (`ref`):**
  - The host app registers resolvers per `kind` (a file → the Library preview, a saved chart → Charts,
    a channel → its series, a principal → their profile).
  - Labels are resolved in **batches** per window, never per cell.
  - Authorization is checked at resolution. An object the viewer can't see renders as
    "not visible to you", never as its id leaking a name.
- **Extensions contribute types** in their manifest (`[[cell_types]]`) as a *data shape* plus a
  mapping onto a platform renderer (for example a domain's "unit cell" maps to `ref`, or a
  "spectrum" maps to `spark`). No contributed component code runs in the page.

### Chat and dual drive

- **Tables in chat:**
  - structured table artifacts render DataTable over an inline or door source;
  - markdown tables in answers are parsed to DataTable in a compact density;
  - large results arrive as a door source reference, not inline rows.
- **Actions** (dual drive, with undo receipts): `set_sort`, `set_filter`, `group_by`,
  `pin_column`, `resize_column`, `focus_row`, `chart_this` (columns or selection → chart
  suggestions), `save_view`.
- **`view_context`** includes the focused table's spec and selection keys, bounded and
  truncated within the existing 16 KiB cap. Never row data beyond the visible summary.

### Saved views, export and code

- **Saved views:** a TableSpec with `id` and revision, stored in the same per-account catalog as
  charts, saved by the same single **Save** action as chat charts. Shareable by link.
- **Export:**
  - CSV or Parquet of the *whole* filtered and sorted view, streamed server-side;
  - a provenance sidecar (spec, reads, gold watermark, principal, time);
  - above a size cap it becomes an async export job that lands in the Library.
- **Copy as code:** a snippet that reproduces the exact view (Python via the platform client,
  and SQL against the gold view). Same filters, sort and columns, with the gold watermark in a
  comment.

## Design

### Rendering

- **Virtualization in both axes.**
  - Rows have a fixed height by default; expanded rows (nested, sub-table) are measured and
    cached.
  - Columns are virtualized beyond the viewport, except pinned columns.
  - Sticky header and pinned columns.
  - Target 60 fps scrolling with 100 columns × 10⁷ rows (fake source).
- **Scrollbar honesty.**
  - With an `estimate` count, the scrollbar is proportional to the estimate and the header says
    `~1.2 billion rows`.
  - With `unknown`, the table scrolls infinitely with a "more…" sentinel and no scrollbar
    pretence.
- **Column profile headers:** a collapsible band under the header showing type, unit, null %,
  and a mini histogram or top-k bars. It's fetched lazily per visible column, and refreshes when
  the filter changes.
- **Interaction:**
  - Columns resize by pointer and keyboard, and auto-fit on double-click (fit to the visible
    window, not the whole result).
  - Widths persist per user and table id (local preference only).
  - Multi-key sort (shift-click).
  - Filters show as removable chips.
  - Facets in a side panel use `top`/`histogram` from profiles.
- **Accessibility:**
  - follows the WAI-ARIA grid pattern: arrow-key cell navigation, Home/End, PageUp/PageDown;
  - `aria-rowcount` reflects the count, or -1 when unknown;
  - screen readers announce sort and filter changes;
  - colour is never the only carrier: flags have icons and text.

### Linked analysis

- **Table ↔ chart:**
  - a brush on a linked chart becomes a `where` predicate;
  - row selection highlights points;
  - `chart_this` sends the view and columns to the chart-suggestion engine (`chart_choice`),
    which proposes only kinds valid for the data's profile.
- **Compare:**
  - two sources (two windows, two runs) align on a key, with differences highlighted;
  - numeric deltas are shown with uncertainty where both sides declare it.
- **Group and pivot (phase 4):** grouping and aggregation are pushed down; groups expand to
  their rows through a child source; a pivot is a group with a column dimension, bounded in
  column count.

### Failure modes

| Situation | Behaviour |
|---|---|
| A slow window | Placeholders, then a quiet "still loading" after 2 s; never blocks scrolling. |
| A refused or truncated query | Envelope reason shown in the table header (budget, timeout, sort not supported), with the action that would succeed. |
| The source changes underneath | Live sources append. Otherwise a "data updated, refresh" chip appears, and the view never jumps under the reader. |
| A profile computed on a sample | Labelled "sample of N". |
| A ref that won't resolve | Shows "not found" or "not visible to you". |

## Phasing

| Phase | Scope | Exit criteria |
|---|---|---|
| **1. Foundation** (in flight) | `RowSource` and `arrayRowSource`; row and column virtualization; resizing; cell registry with today's types; Library, previews, chat and markdown on DataTable | 10⁷-row fake source: bounded DOM and memory; existing tests green |
| **2. Gold-backed** | TableSpec 2.0 parse and emit; `data.table` with keyset pagination and pushdown; counts; `doorRowSource` with abort; column profiles; `quality` and `quantity` cells; terminal and MCP parity | 10⁹-row gold table opens under 500 ms p95 to first window; sort and filter pushed down (query-plan test); no in-memory sort window on door sources |
| **3. Rich and linked** | `ref` cells with batched resolvers; `spark` and `bullet` cells via the chart renderer; facets; colour scales; chart linking and `chart_this`; Save; export with provenance; copy-as-code; chat dual-drive actions | Each feature is reachable from chat and web; saved view round-trips; exported file reproduces the view |
| **4. Analysis** | Grouping and aggregation pushdown, pivot, compare/diff, live tables | Group over 10⁹ rows within ADR-157 budgets; compare shows uncertainty-aware deltas |

## Decisions

- **ADR-128:** row sources read gold only; the door enforces it.
- **ADR-157:** every table call is bounded (rows, time, cost) and says when it was bounded.
- **ADR-214:** sparkline and bullet cells are chart kinds drawn by the one chart renderer.
- **ADR-213:** contributed cell types follow the previewer rule (data shapes, platform renderers).
- A new ADR is needed for TableSpec 2.0 and the `data.table` keyset contract once phase 2
  starts.

## Open questions

- **Keyset over composite and nullable sort keys:** null ordering, and ties broken by row key
  everywhere? (owner: data door)
- **Thresholds for exact vs estimated counts and for profile sampling,** per store. Defaults are
  needed per gold relation type. (owner: data door)
- **Whether `group` and pivot belong in TableSpec or a separate analysis document** once
  formulas (computed columns) are wanted. (owner: platform)
- **Live tables:** subscribe through the existing live chart transport, or a table-specific
  stream? (owner: platform)
- **Export job size cap and retention** in the Library. (owner: Library)
