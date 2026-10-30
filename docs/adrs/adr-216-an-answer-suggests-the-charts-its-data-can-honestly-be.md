# ADR-216: An answer suggests the charts its data can honestly be

**Status:** Proposed (2026-10-28)
**Related:** ADR-214 (ChartSpec 2.0, one renderer), ADR-039 D5 (chart selector authority), the chat dual-drive spec (appkit `docs/specs/spec-chat-dual-drive.md`), [spec-scientific-displays §9b](../specs/spec-scientific-displays.md)

## Context

Chat already draws a chart inline with the same component as the Charts page,
and ChartSpec 2.0 can name sixteen kinds. What it could not do was decide
which. A person who asked "how did the temperature behave last week" had to
know that a line answers "behave over time", that a box answers "how much did
it vary", and then ask for each. The model could be asked to pick, but a model
picks a pie for a time series, or a gauge for a value with no limit, and the
only check on it was the reader.

Some pictures of some data are simply wrong. A pie of negative values, a pie of
twelve slices or of a time axis, a heatmap with no second axis, a gauge with no
limit, a histogram of six readings, a dual axis with one unit: each draws, and
each misleads. Which kinds are honest for a result is a property of the
result, so it can be decided without asking anybody.

## Decision

**D1. A deterministic profile and compatibility table decide which kinds can
draw a result** (`scidisplay.suggest`). The profile reads values, never column
names: field roles (time, quantitative with unit, categorical with
cardinality, ordinal, grid, hierarchy), rows, series, units, sign, range,
cadence, sparsity, shared instants, and the reference a value is read against
(an explicit limit or target, or the 100 that a percentage implies). The table
below is the rule; `tests/test_a_chart_is_suggested_from_the_data.py` holds it
as a fixture × kind matrix, and a property test draws every suggested spec
through the real renderer.

| Chip (kind) | Can draw when |
|---|---|
| line | time or numeric x; at least 2 points; one unit; amounts, not labels |
| line-dual (line, y2) | as line, with exactly two units |
| line-panels (line, panels) | as line, with two units or more |
| bar | categories ≤ 40, or ≤ 40 instants; one unit |
| bar-horizontal (ranked) | one value per category, ≤ 40 |
| bar-stacked | ≥ 2 series of an additive, non-negative unit; or a hierarchy of 2 levels |
| scatter | two quantities paired: numeric x, or two series sharing ≥ 5 instants |
| area | as line; non-negative; an amount, not an intensity (not °C, %, ratio) |
| area-stacked | as area; ≥ 2 series over time |
| histogram | ≥ 20 values of one unit; not one value per category |
| box | ≥ 5 values per group; ≤ 24 groups; one unit |
| step | time x |
| pie, donut | one series of 2–8 named parts, all > 0, additive unit, no time axis |
| heatmap | a 2-D grid (two categorical axes), or time × ≥ 3 series of one unit |
| gauge, progress | one value with a maximum, limit or target |
| bullet | one value with a target or limit |
| kpi | ≤ 6 series over time; a lone value only with a reference |
| sparkline | time x; ≤ 6 series (each row has its own scale) |
| state | a label (≤ 12 distinct) over time |
| flame | a hierarchy of ≥ 2 levels, non-negative; offered once the kind is registered |
| mesh3d, surface, scatter3d, volume | never suggested from an answer: they draw a scene (mesh, grid or points), which a data read is not |

A chip for a kind the renderer has not registered is withheld: a rule for a
kind nothing draws is a promise.

There is one engine. `chart_choice.offer_for`, which `table_spec.tabulate`
calls to attach a chart offer to every table, keeps its 1.x half (the time,
value and series columns a query document needs to read the rows back from
the store) and takes the picture from `suggest`: `ChartOffer.suggested` is the
top chip and `suggestions` the row, filled even for a table with no time axis.

**D2. Ranking is by intent, deterministically, and a model can only reorder
within the valid set.** The question is classified into intents by cue (trend,
compare, part of whole, distribution, relationship, status against a limit,
ranking, hierarchy, composition over time); a question with no cue takes the
data's default (a time series is a trend, categories a ranking, a value a
status). Each intent prefers some chips. The top row takes the first two
intents' own picks, then the overall ranking, one chip per family (line-like,
bar-like, pie-like), so the row offers different pictures; two is enough when
a third would repeat the first. Every other valid chip is under "more".
`prefer` moves chips to the front and is ignored for any chip not valid, and
the response says which preferences it ignored.

**D3. The answer carries the data, bounded, and the first chart already
drawn.** At the end of a web turn that read data (`data.series`,
`data.show`, `data.aggregate`, …), the chat loop merges the reads into one
result and emits one `chart_suggestions` frame (at most one per answer). The
frame holds the profile, the suggestions with their reasons and specs, and the
data factored into `datasets` by digest, so kinds that draw the same numbers
carry them once. The data is decimated to the draw width (min and max per
bucket, so a peak survives; an even sample for kinds that count values). It is
capped at 96 KiB; past the cap the block is decimated harder, and past that its
datasets are held on the node and the block carries a `data_ref` fetched once
(`GET /api/v1/charts/data/{ref}`). This cap is separate from the 16 KiB
view-context cap. The top suggestion's SVG is in the frame, so the first chart
draws without a round trip; another kind is drawn by `POST /api/v1/charts/render`
from the data already held, with no model call and no re-read.

The data carried is what the chart draws, for the person who asked: the
decimated series the answer's prose quotes, not the raw tool result.

**D4. One capability per verb, every surface.** `charts.suggest` and
`charts.render` (the `charts` builtin) reach the CLI (`axi charts suggest |
render`), MCP, the chat tool loop and HTTP. The browser draws through
`/api/v1/charts/render`, the same renderer as everywhere.

**D5. Switching kind by asking is checked against the same table.** A chat
chart publishes the chips its data supports (`kinds`) and why the others are
refused (`refused`) in the view context. `update_chart` with a `kind` the
chart cannot be is refused with that reason, by the planner ("show that as a
box plot") and by the tool alike.

**D6. Nothing is written to the catalog until the person saves.** A suggested
chart lives in the message. One action, "Save", writes it to the per-account
catalog (the default "Chat" group, or one the person picks), find-or-create by
the spec's id so the same chart is not saved twice.

## Consequences

- The choice of chart is testable and explainable: every suggestion and every
  refusal has a reason a person can read, and the table is in one module.
- The model's part shrinks to answering. The prompt tells it the chart is
  attached and not to ask which one.
- An answer is larger by its chart data (bounded as above). A conversation
  reloaded from the server does not yet carry its chart blocks; they are
  emitted per turn.
- A bound turn whose reads run on a remote node returns no local tool results,
  so it carries no chart block until the node emits the frame itself.
- New kinds become suggestible by adding a rule and a row here, and the matrix
  test fails until both exist.
