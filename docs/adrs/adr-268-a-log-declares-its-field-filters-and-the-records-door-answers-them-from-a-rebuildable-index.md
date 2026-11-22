# ADR-268: A log declares which of its fields are filters, and the one records door answers them from a rebuildable index

**Status:** Draft (2026-11-19)
**Related:** [prd-attestation.md §6c](../prds/prd-attestation.md#6c-filtering-by-a-logs-own-fields) (the product design: who, what the person sees, acceptance rows LF-A1 to LF-A24, phases),
[ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) and [ADR-150](adr-150-an-attestation-record-is-kept-in-a-logbook.md) (a record in a log, declared by an extension),
[ADR-144](adr-144-a-person-confirms-exactly-what-is-recorded.md) (observed fields),
[ADR-241](adr-241-a-record-lives-with-its-writer-gold-serves-what-is-derived.md) (a record lives with its writer; D2 no dual writes, D3 who reads which copy, D5 drift anchors, D6 logs),
[ADR-251](adr-251-indexes-and-context-are-labelled-projections-the-record-is-sealed-never-compacted-one-check-at-every-door.md) (D1 an index is a labelled projection, D4 structured filters run before ranking, D8 reads recorded at the door, D9 the cross-door matrix),
[ADR-128](adr-128-medallion-tier-boundaries.md) (medallion tiers),
[ADR-243](adr-243-people-see-a-log-code-keeps-logbook-and-log-is-the-cli-noun.md) (`log` is the command noun),
`docs/specs/spec-attestation.md` (unit W3, "Search and paging"; migration 0010), issues #1758 (LD-1, the cross-door matrix) and #1801 (declared conversions)

## Context

Any extension declares a log in a TOML file named by a `[[extension.provides]]` block of kind
`logbook`. External, pip-installed extensions are found the same way as built-in ones
(`attest/registry.py`, `_extension_roots()`). A log declares its entry types and each type's
typed fields (`text`, `number`, `quantity`, `choice`, `datetime`, `attest_checkbox`,
`readings`, `obligation_ref`).

The ways to narrow a log's records are fixed by the platform: entry type, meaning, signer, a time
window, one interval and text (`filters.RecordFilters`, read by `GET /{logbook}/records`,
`attest.show` and the exports), plus the two list modes `unacknowledged` and `cosign=pending`.
The Log page's filter panel (appkit `LogbookFilters`) offers the same set. A log whose entries
carry an asset id, an area or a measured pressure cannot let a reader ask for "entries about asset
A-104" or "pressure above 4 bar" except through free-text search, which cannot compare numbers,
respects no unit and matches words in every field at once.

The `logbook_inputs` seam (`field_source`, `seed`, `settings`, `presenter`, `record_label`)
supplies values for *writing* an entry. Nothing lets a log say which of its fields a *reader* may
filter on.

Three constraints shape the answer:

- **Records are signed, chained and immutable** (ADR-143). No column can be added to a signed row
  by rewriting it, and a correction is a new record that restates a field (ADR-142 `corrected`),
  so "the value of field F on entry E" is the latest signed restatement, not always the original.
- **A copy is a door** (ADR-251). Any index over record content must carry the record's labels,
  be rebuildable from the record and be read only through the one check.
- **The record's field values do not carry their types.** A record stores `content.fields` as the
  person entered them (decimal strings, `{value, unit}` objects, readings per instrument with a
  `converted` value beside an entry in another unit, #1801). Which field is a `quantity` in which
  unit is in the declaration file, which the database cannot read.

**Does the medallion rule apply?** No, not to this door. ADR-128 governs the data platform's
tiers: bronze lands, silver conforms, gold serves. The attest chain is not a tier. It is the
owner's operational record of truth (ADR-241 D1), and the Log page and the records door read the
chain by design (ADR-241 D3 and D6: "the Log page and logbook views: the chain through
`/api/v1/attest`"). The rule that does apply is ADR-251 D1: an index over the record is a labelled
projection, rebuildable, served only through the one check. Migration 0010 set the precedent: its
text-search column is "an index as a labelled projection of the record, kept beside the chain
until a gold view of the chain exists for it to move to". Nothing here reads silver or bronze.

## Decision

**A log names its filters in a `[[filter]]` table, each naming one declared field. The one
records door accepts `field.<id>` filters for exactly those fields, ANDed with every existing
filter, after the read check and never widening it. The door answers them from a field-value
index in the attest schema: derived from the records by sequence number, typed by the
declaration, carrying each record's labels and digest, and rebuildable at any time. One skill
lists a log's filters and the values a reader may pick, for every surface.**

### D1. The declaration: a `[[filter]]` table on the log, not a flag on the field

```toml
[[filter]]
field = "area"                  # a choice field: a pick list of its declared choices

[[filter]]
field = "asset_id"              # a text field
match = "equals"                # the page offers exact values seen in readable entries

[[filter]]
field = "pressure"              # a quantity in bar, with convert = [["kPa", "bar", "0.01"]]

[[filter]]
field = "note"                  # a text field; match defaults to "contains"
types = ["ROUND_CHECK"]         # optional: only these entry types' field of that id
label = "Note text"             # optional: the field's own label otherwise
```

- `field` (required) names a field id declared on one or more entry types. The filter covers that
  field on every type that declares it, or only the types listed in `types`.
- Every type the filter covers must declare the field with the same type, the same unit, and for a
  `choice` the same choices or a subset of one list; otherwise the declaration is refused.
- `match` applies to `text` only: `contains` (default, word match) or `equals` (exact value).
- Order is declaration order. The first three are shown on the Filter panel; the rest sit under
  "More filters" (the PRD owns that presentation).
- `filter` joins the enforced top-level keys; any other key in a `[[filter]]` table is refused, as
  every unknown key in a log file is.

### D2. Which field types can be filters, and how each compares

| Field type | Filter | Wire form | Phase |
|---|---|---|---|
| `choice` | any of the chosen values | `field.<id>=v` (repeat for any of) | 1 |
| `text`, `match = "equals"` | any of the given values, exact | `field.<id>=v` (repeat) | 1 |
| `text`, `match = "contains"` | contains every word given | `field.<id>.contains=words` | 1 |
| `number` | inclusive range, either end optional | `field.<id>.gte=`, `field.<id>.lte=` | 1 |
| `quantity` | inclusive range in a stated unit | as `number`, plus `field.<id>.unit=` | 1 |
| `attest_checkbox` | checked or not | `field.<id>=true` or `false` | 1 |
| `datetime` | inclusive range of instants | `field.<id>.gte=`, `.lte=` (ISO 8601) | 2 |
| `readings` | one instrument, inclusive range | `field.<id>@<instrument>.gte=`, `.lte=`, `.unit=` | 3 |
| `obligation_ref` | not a filter; the log's obligations view answers it | refused at declaration | |

Rules every comparison follows:

- **Exact, never float.** Numbers compare as decimals (`numeric`), as the change since the previous
  value already does (`changes.py`).
- **Unit-aware by declaration only.** A `quantity` bound is given in the field's declared unit or a
  unit the field's `convert` table names (#1801); the bound is converted exactly into the declared
  unit before the query. A bound in any other unit is refused (400 `filter`), never guessed. An
  entry stored in another unit is compared by the `converted` value it carries; an entry whose
  value has no comparable unit (the declaration's unit changed after it was signed) never matches a
  range, and doctor counts it.
- **Corrections count.** A filter matches an entry by its **current** value: the value restated by
  the latest signed correction of that field, else the signed value (the rule unit L7 already uses).
  The entry is returned, never the correction alone; its thread shows both values.
- **ANDed with everything.** Field filters AND with each other and with entry type, meaning,
  signer, time, interval and text. Several values for one equality filter are OR within that
  filter. There is no negation in phase 1.

### D3. One door: the records route, the skill and the exports read one parser

`filters.parse_filters` gains the `field.` keys and `RecordFilters` gains a tuple of field
predicates. Because the route `GET /{logbook}/records`, the skill `attest.show` (MCP
`axiom_attest__show`), the export selection and the export preview already share that parser and
`RecordFilters.apply`, they all gain field filters at once and give identical answers.

- **After the read check, never instead of it.** The door resolves the site from the session and
  checks `may_read` first, as today. A field filter adds WHERE clauses; it cannot name a site, a log
  or a label the caller could not already read.
- **Only declared filters.** A `field.<id>` key naming a field the log did not declare as a filter
  is refused with 400 `filter` and the words "`<id>` is not a filter of this log; its filters are
  a, b, c". It is never ignored: a filter that silently does nothing returns too many records,
  which reads as an answer. The same words fail the skill and exit the CLI with code 2.
- **The list modes** `unacknowledged=1` and `cosign=pending` stay separate lists, as today; field
  filters do not apply to them in phase 1.
- **The skill and MCP spelling.** `attest.show` and `attest.export` accept the same `field.<id>`
  keys and also a structured `where` list, `[{field, op, value, unit?}]`, with `op` one of `eq`,
  `contains`, `gte`, `lte`. Both spellings parse to the same predicates.
- **The CLI spelling.** `axi log <log> show` and `export` take `--where` (repeatable, ANDed):
  `--where area=North`, `--where 'asset_id=A-104'`, `--where 'pressure>=4 bar'`,
  `--where 'pressure<=450 kPa'`, `--where 'note~loose cover'`, `--where checked=yes`. The CLI turns
  each into the same keys; nothing is parsed twice in two ways.

### D4. The index: a typed, labelled, rebuildable projection beside the chain

A table `attest_field_values` in the attest schema (ADR-052), one row per (entry, field, and for
`readings` the instrument):

| Column | Holds |
|---|---|
| `site_id`, `logbook`, `entry_type`, `entry_seq`, `attestation_id` | which entry, and the labels the door checks (and, when ADR-251's record labels land, those labels copied from the record) |
| `field`, `instrument` | which value |
| `value_text` | the value as entered, for equality and suggestions |
| `value_words` | a `tsvector` (`simple`), for `contains` (the same configuration as migration 0010) |
| `value_num`, `value_unit` | for `number` and `quantity`: the comparable decimal and its unit, the `converted` value where the record carries one in the declared unit |
| `value_at` | for `datetime` |
| `source_attestation_id`, `source_seq`, `source_digest` | the record the value came from: the entry itself or the correction that restated it; the digest is the drift anchor (ADR-241 D5) |
| `current` | true for the value a filter compares (D2, "Corrections count") |

Indexes: btree on (`site_id`, `logbook`, `field`, `value_text`, `entry_seq`) and (`site_id`,
`logbook`, `field`, `value_unit`, `value_num`, `entry_seq`), and a GIN on `value_words`, each
partial on `current`. Every field filter is then an index-prefix predicate, and a filtered page is
`entry_seq IN (...)` semi-joined to the existing keyset on `attest_records`.

**Every scalar field of every entry is indexed, whether or not it is a filter today.** Records are
few and small (ADR-241 D7), so this is cheap, and it means declaring a new filter, or a site
upgrading an extension that adds one, works at once with no backfill. What a reader may filter on
is decided at the door (D3), not by what the index happens to hold.

**How rows get there: a projector by sequence watermark, with read-time catch-up.**

- One watermark per chain, `(site_id, logbook, indexed_seq)`. The projector reads the records after
  the watermark in `seq` order, types each value by the declaration the record names (its
  `logbook_version`; the current declaration when that version is not loaded), writes rows,
  applies a correction by moving `current` from the old row to the restated one, and advances the
  watermark, in one transaction under a per-chain advisory lock. Writing the same records again
  changes nothing.
- It runs after a record is appended (from the bus event the append already emits, never inside
  the signing transaction), and **a field-filtered read first brings the chain's index to its head**
  (a bounded statement over the few records past the watermark), so a person who has just signed an
  entry finds it with the filter they are using. No request writes a record and its projection in
  one step (ADR-241 D2).
- **Rebuild** is resetting the watermark and deleting the chain's rows: `axi log <log> reindex`
  (administration, recorded), or automatically when the projector's own version changes. It never
  touches a signed row.
- **A value the projector cannot type** (a `number` field holding text from an old declaration) is
  indexed as text only, never guessed into a number, and counted for doctor.

**When the gold view of records exists** (ADR-241 D6), the same projection is built from gold for
the gold door, so chat and reports that read gold first gain the same filters with the same words.
The Log page keeps reading the chain and this index (ADR-241 D3). One projector definition serves
both; no second, surface-private index is built (ADR-251 D4).

### D5. Suggestions come only from what the reader may read

One read skill, `attest.filters` (CLI completion, MCP `axiom_attest__filters`, agent tool, and
`GET /api/v1/attest/{logbook}/filters`), returns the log's declared filters: field, label, field
type, control, unit and the units it accepts, `match`, the covered entry types and, where values
are offered, the values.

- A `choice` filter offers its declared choices, plus any value present in readable entries that
  is no longer declared, marked as no longer offered, so old entries stay findable.
- A `text` filter with `match = "equals"` offers the most frequent values seen, at most 50, and a
  prefix lookup (`prefix=`) for more.
- `readings` (phase 3) offers the instruments the field's source lists for the reader's site
  (`field_sources.resolve`), the same list the entry form uses.
- `number`, `quantity` and `datetime` offer no values in phase 1. The lowest and highest values seen
  may be added later, under the same rule.
- **The rule:** every value comes from the index under the same site, log and label predicates as a
  records read by the same caller, after the same `may_read` check. A reader who may not read the
  log gets the same refusal as from the records door, and its refusal is recorded once (#1885). The
  read is recorded at the door as `attest.read.filters` (ADR-251 D8). No value is cached across
  callers. Suggestions carry no counts in phase 1.

### D6. Observed fields may be filters

A field with `observe = true` may be declared a filter. Observation constrains *writing*: no
software offers a value for the field before a person signs (ADR-144). Filtering reads values
people already signed, so it supplies nothing to anybody's entry. Two guards keep the line clear:

- the entry form, the proposal path and every presenter never call `attest.filters` or read
  `attest_field_values`; a test asserts that no presentation of an observed field carries a value
  that came from the index;
- suggestions for an observed `text` field come only from signed entries, as for any field, and are
  shown only on the Filter panel and in CLI completion for `--where`, never as a default.

### D7. Validation, doctor and lint

- **`axi log validate` refuses** a `[[filter]]` whose `field` no type declares; whose `types` names a
  type that does not exist or does not declare the field; whose field is `obligation_ref`; whose
  covered types disagree on the field's type, unit or choices (D1); with `match` on a field that is
  not `text`; two filters on the same field and types; and any unknown key. A `datetime` or
  `readings` filter loads before its phase ships and is listed under `not_yet_enforced`, the rule
  every later key follows, so nobody mistakes it for a filter in force.
- **`axi ext doctor`** (`logbook.declared`) lists each log's filters in its detail line, and a new
  check `logbook.filters` reports the index: watermark behind head beyond the catch-up bound,
  values indexed as text only, values in a unit a filter cannot compare, and rows whose
  `source_digest` no longer matches a record. Each finding names its fix (`reindex`, or the
  declaration change). A healthy index takes no row (DC-17).
- **`axi log <log> about`** and `GET /logbooks/{logbook}` list the declared filters.

## Options considered

**A flag on the field (`filter = true` in `[[type]].fields`).** The smallest change, and close to
the field it describes. It lost on four counts. A filter is a property of the log's Filter panel,
not of one type: the same field id on three types needs three flags that must agree, and nothing
says which to show first. It gives no place for order, a label for the filter or `match`, which
the panel needs for progressive disclosure. It mixes reading concerns into the field tables that
drive the entry form and the digest-bearing presentation. And a later reader of a long field list
cannot see the log's filters at a glance. The `[[filter]]` table costs one indirection, and
validation (D7) makes a broken reference fail at load.

**Filter in place on the record's JSON** (`record->'content'->'fields'->>'x'`, with a GIN
`jsonb_path_ops` index or expression indexes per declared filter). JSON containment answers
equality but not numeric ranges, units or corrections; an expression index per filter means DDL
driven by a pip-installed extension's TOML, run at install. Neither knows a field's type, since the
record does not carry it. Rejected.

**A database trigger on `attest_records` populating the index** (the 0010 pattern, extended). The
database would derive the rows atomically with the append. It cannot type the values: the
declaration, its units and its conversions live in files. A typeless index cannot do unit-aware
ranges, and teaching the trigger types would copy the declaration into the database, a second
source of truth. Rejected in favour of the watermark projector with read-time catch-up, which gives
read-your-writes without a dual write.

**Index only the declared filter fields.** Smaller, but every new or changed declaration would need
a backfill job, and a site upgrading an extension would see a filter that finds nothing until it
ran. Indexing all scalar fields costs little at a log's scale and removes that failure.

**Filter in the browser over the brief's `fields`** (the brief already carries them, LV-12). Works
for one page of records and fails for the log: a filter must see the whole chain, and paging would
silently hide matches. It also leaves MCP and the CLI without the feature. Rejected.

**Read from gold only.** Gold of the chain does not exist yet, and the Log page reads the chain by
ADR-241 D3. Building the projector once and pointing it at gold when gold exists keeps one design.

## Consequences

- An extension author adds a few lines to a log file and the log gains filters on the web page, in
  MCP and on the CLI, with no code. External extensions get it the same way as built-ins.
- The records door's contract grows: `field.<id>` keys, a `where` list, 400 `filter` for an
  undeclared field. The cross-door matrix (#1758, LD-1) gains rows for the new parameters and for
  the new `filters` door; a door added without them fails the build.
- The attest schema gains one projection table, a watermark table, a migration (expand only) and a
  projector the node runs; the backup excludes nothing new, because the index is rebuilt rather than
  restored.
- A log's declaration now carries reading concerns. A site cannot add filters to a log it does not
  author in phase 1; whether a site may choose which declared filters show first is left to a later
  phase.
- Follow-up work: the spec (`spec-attestation.md`, a "Field filters" section and the route table)
  is updated in the pull request that builds phase 1, as the three-document rule requires; appkit
  extends `LogbookFilters` and `RecordFilter` (no second filter component); the search provider
  accepts the same `field.` keys for `kinds=logbook` in phase 2.
