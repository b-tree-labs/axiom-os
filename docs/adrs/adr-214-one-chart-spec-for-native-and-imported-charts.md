# ADR-214: One chart spec for native and imported charts, drawn by one renderer

**Status:** Proposed (2026-10-28)
**Related:** ADR-039 (scientific displays), ADR-203 (a kit is promoted as data), ADR-208 draft (conforming utilities as catalog objects), [spike report](../working/plotly-trame-charts-spike-2026-10-28.md)

## Context

The platform had two chart vocabularies and could draw one picture.

- **ChartSpec 1.x** (`scidisplay.chart_spec`) is a *query* document: site,
  channels, window, transform, drawn as a time series (`timeseries`,
  `comparison`, `state`).
- **The data-platform catalogue** (`data_platform.charts.ChartSpec`) is a
  *registry entry*: an id, a kind from thirteen names, a provenance claim, a
  group, roles and the gold verb that serves it. Most of its kinds had no
  renderer.

Consumers need pies, bars, gauges, progress bars, KPI tiles, histograms, box
plots, heatmaps, spectra on log axes, subplots, annotations and threshold
bands. Partners build dashboards in Python frameworks whose charts are Plotly
figures (a Trame app keeps each one as JSON in its server state), and they want
those charts beside native ones with no visible seam. The appkit figure seam
already forbids a second renderer in the browser.

## Decision

**D1. One document: ChartSpec 2.0** (`scidisplay.unified_spec`). Every chart
the platform draws, whoever made it, is one versioned document: `kind`,
`title`, `data` (exactly one of `query`, `verb`, `series`, `field`, `value`,
plus optional `frames`), `axes`, `encoding`, `marks`, `layout`, `controls`,
`catalog`, `display_units`, `series_colours`, `palette`, and `source`.
`validate` reports every problem at once; `json_schema()` publishes the outer
shape. Kinds are an open registry (21 ship: line, area, step, sparkline, bar,
bullet, pie, donut, progress, gauge, kpi, histogram, box, scatter, heatmap,
state, flame, mesh3d, surface, scatter3d, volume). Grouped, stacked and 100% bars, horizontal bars, dual axes, log-log
spectra and small multiples are encodings, axes and layout on those kinds, not
new kinds.

**D2. Provenance is `source`, and it is not content.** `source` is
`{"type": "native"}` or `{"type": "imported", "format": "plotly"|"trame",
"figure": <ref>, "digest": "sha256:…", "importer_version", "revision"}`. The
original figure is kept by reference and pinned by digest; a re-import keeps
the `id`, replaces `data` and increments `revision`. `spec_id` (the citable
content id) excludes `source`, so an imported chart and its native twin have
the same id.

**D3. One renderer, which never reads `source`.** `chart_kinds.render_spec`
draws every kind as SVG in the platform chrome (themed tokens, the serif for
numbers and sans for labels, units once per axis, colour by quantity from
`chart_colour`, categories from the one palette registry `palettes`, Cividis
for fields). A line over time goes to the existing `chart_svg.render_svg`, so
it is the Charts page's line. Marks (bands, rules, annotations) are drawn by
`chart_marks` for both. Every figure is `role="img"` with a title and a
description; data marks carry their value as `aria-label` and `<title>`, and up
to 240 are keyboard focusable (past that, one summary takes focus).

**D4. Controls are declared in the spec and drive state.** `slider`, `range`,
`dropdown`, `toggle`, `segmented`, `playback`, each bound to one target:
`frame`, `group`, `layer`, `window` or `param`. State is not in the document;
`resolve_state` merges a state into the defaults and answers, rather than
clamps, a value a control cannot take. Controls bound to one target are one
value. Chat dual drive gains `set_control` and `kind`/`spec` on create and
update, so the conversation can move a slider the reader sees.

**D5. Import is translation or refusal, never half.** `plotly_import`
translates scatter (lines, markers, step, fill, stack, numeric and log x), bar
(group, stack, percent, horizontal), pie and donut, heatmap, histogram, box and
indicator (number, gauge, bullet); subplots into panels; annotations and
shapes into marks; update menus into segmented, dropdown or toggle controls;
sliders and frames into a frame slider and playback; Trame widgets (`VSlider`,
`VRangeSlider`, `VSelect`, `VSwitch`, `VCheckbox`, `VBtnToggle`) into
controls, with recomputed figures sampled into frames. Text is stripped of
HTML. Anything else is refused whole with every reason, and only then goes to
the fenced plotly.js fallback, sanitised (`sanitize_figure`) and labelled.

**D6. The two vocabularies migrate onto 2.0** (`unified_migrate`): `from_v1`
(1.x queries), `from_catalog` (every catalogue kind maps), `from_saved_graph`
and `migrate_saved_view` (Charts page views keep all their state and gain a
`spec` per graph). 1.x documents keep parsing under `chart_spec`; nothing saved
stops opening.

**D7. Seamlessness is a test.** For every supported Plotly type, an imported
figure and its native-authored equivalent render to byte-identical SVG and
share a `spec_id` (`test_an_imported_chart_is_a_native_chart`).

**D8. A hierarchy is a `flame` chart** (amended 2026-10-28). `data.tree` holds
one of three forms that read as one tree (`flame_data`): a nested tree, folded
stacks (`a;b;c 42`, the format perf, py-spy and async-profiler write), or
parent-id rows (a query result). A node is a name and a value, children summing
to at most it, with an optional `self` share and `category`. Drawn as a flame
(root at the bottom) or an icicle (root at the top), width by value, labels
fitted or hidden; colour by category from the categorical palette or by a hash
of the name from the registry's `FLAME` set, never random. Hover and focus give
name, value, % of total and % of caller. Controls: a `path` control bound to
`zoom` (set by clicking a frame or a breadcrumb item, which carry
`data-axk-control`/`data-axk-value`), a `text` control bound to `search`
(matches highlighted, their total share reported, a match inside a match
counted once), and `encoding`-bound controls for `invert` (callers above the
work) and `direction`. Frames narrower than a pixel are folded per parent into
an `(other)` sliver at render, so 50,000 frames draw in well under a second.
Dual drive: `focus` takes a frame `path`. Plotly `icicle` imports onto `flame`;
`treemap` and `sunburst` are refused with the way forward (re-export as icicle).

**D9. Every series colour reads in both themes.** The light colour is the one
the colour code and the colour-vision checks are made on and the one exported;
where it is too dark for the dark panel, the renderer writes
`light-dark(#light,#dark)` with the same hue lifted only as far as it must
(`palettes.series_colour`). `flatten_theme` resolves it to the light colour on
export. A test holds every categorical entry and every derived family colour to
3:1 on both papers.

**D10. 3D is four kinds drawn twice: live in WebGL, static on the server**
(amended 2026-10-28). `mesh3d`, `surface`, `scatter3d` and `volume` take
`data.scene`: a `mesh` (points and polygons), `points`, a `grid` (a surface's z
over x, y, or a volume's values), or a `ref` for geometry past the inline bound
(250,000 points or cells); plus named `scalars` (per cell or per point, with a
unit), `elements` (one id per cell: what a pick reports and what an explode
moves) and a `length_unit`. Encoding: `colormap` (sequential, Cividis default),
`camera` (`iso`, `top`, `side`, `front`, or `{azimuth, elevation, zoom}`),
`opacity`, `edges`, `explode`, `cutaway` with `slice_axis` and `slice_at`,
`clip` planes, `scalar` (which field colours it), `iso` (volume isovalues) and
`pick`. Every one is a control target, so a slice slider, a scalar dropdown, a
camera picker, opacity and explode are ordinary controls, frames give time with
playback, and chat drives them with `set_control`, `set_camera` and `focus`
on an element.

The server draws the chrome (title, colour bar with unit, notes) and marks the
scene box; inside it, a deterministic orthographic projection of the same
resolved scene is the static view used for exports, chat thumbnails and a
browser without WebGL. It also returns the resolved scene (cut, exploded,
coloured by the same rules) that appkit draws with vtk.js, loaded only when a
3D chart is on screen, over that box. The live and static views therefore
cannot disagree about what is shown. Plotly `mesh3d`, `surface`, `scatter3d`,
`isosurface` and `volume` import onto these kinds (`cone` and `streamtube`
are refused: a vector field is not a kind yet), and a trame-vtk
`VtkLocalView` scene imports from its polydata and camera
(`import_vtk_scene`), with the original kept by reference and digest.
`spec_id` leaves out `id` as well as `source`.

## Consequences

- A chart from a partner's Trame app is a platform chart: it themes, exports,
  speaks its values, is drivable from chat, and promotes as data (ADR-203) like
  any other. Under ADR-208 the importer is a catalog utility.
- The renderer backlog is explicit and small: polar and ring geometry,
  map-shaped grids of small multiples, contour, vector fields.
- A 2.x reader refuses a 1.x document by name and points at `from_v1`; the
  1.x module stays until no saved view needs it.

## Open questions

1. Should `catalog` move into the registry only, leaving the document purely
   about the picture? (Kept together now so a kit chart is one file.)
2. A `param`-bound control needs a source that recomputes; a live Trame bridge
   could serve it. Today, imported widgets are sampled into frames.
