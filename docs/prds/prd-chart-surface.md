# Product Requirements: The Chart Surface

**Product / Feature:** Chart, the platform's figure surface
**Owner:** Platform
**Status:** Active. Phase 1 built, Phase 2 specified below.
**Last updated:** 2026-09-28

Related: [chart-colour.md](../conventions/chart-colour.md) for the colour code,
[units-and-names.md](../conventions/units-and-names.md) for how a unit reaches a
figure, and the survey of how other products solve layout in appkit's
`docs/working/analytics-surface-study.md`.

---

## 1. What this is

One surface where a person picks readings from one or more sites and sees them
drawn, correctly, in a figure they could put in a paper.

"Correctly" is the whole product. Anyone can draw a line. The things that make
this worth building are the ones a chart library will not do for us: a unit on
every axis, a colour that means the same thing in every figure, a gap where a
reading is missing rather than a line drawn through it, a window sized to the
data rather than to a constant, and a figure that says out loud what it could
not do.

## 2. Why now

Two weeks of putting real readings through it produced a list of defects that
have one shape. Each is a case of the surface answering confidently when it did
not know, or answering quietly when it did.

- A figure opened on a fixed 24-hour window. A channel read once a day showed
  **one point**. The window was a constant and no constant is right for both a
  sensor sampled twice a second and one sampled once a day.
- The axis went to whichever series happened to be first, so a channel with no
  declared unit took the axis and every channel that **did** declare one was
  left out of the picture.
- A measurement and a model of it could not be drawn together at all, because
  the picker was scoped to one feed and they live in different feeds by
  construction. That is the one comparison the surface exists for.
- Provenance was read from the store and then dropped before drawing, so every
  model was drawn as a measurement.
- A window typed into a date picker was taken as given, so nine months could be
  requested over a record of twenty-five days.
- A window inside one afternoon carried no date anywhere on the axis.

None of these are drawing bugs. They are all the same bug: the surface knew
something and did not use it. That is what this brief is written against.

## 3. The model

Five nouns. Everything else is layout.

**A figure is a document.** A deterministic renderer draws it from a chart
spec, so the same request is the same picture in a terminal, in a chat, in an
export and on a page. There is exactly one renderer and it is the platform's.
A charting library in the browser would be a second renderer, it would have to
re-earn every promise in section 4, and it is the one nobody tests.

**A source answers for a set of readings.** It takes a request (which series,
which window, which way to nudge) and returns a drawn figure plus what it knows
about itself: the window it actually drew, how far the readings go, the bucket
it chose, and the notes.

**A panel is one figure in a layout.** It has a title and a source and a
selection. It has no window of its own.

**A facet is the dimension a grid is split on.** Site today. Feed, unit, and
channel are the obvious next ones. The grid is derived from the facet, never
assembled by hand.

**The window is one, and it belongs to the surface.** Not to a panel.

## 4. Principles

Each of these is load-bearing, each has already been paid for, and each is
checked by a test rather than asserted in a comment.

**4.1 A value without its unit is not a fact.** The unit lives in the `unit`
field and is never inferred from a name into the data. But a name that carries
a unit is evidence, and the surface says so: not "no unit" but "its own name
says cm and the map does not", which is a line somebody can go and write.

**4.2 A declaration outranks an absence.** When a figure must choose which unit
gets the axis, a declared unit beats an undeclared one, then the unit the most
series share, then the first. An undeclared unit is not a rival claim about the
quantity. It is the absence of one.

**4.3 Nothing is dropped quietly.** A series left out is named, with the reason
and the fix. A reading a log axis cannot place is counted. A window that was
clamped says so. A figure that drew less than it was asked for and looked fine
is the failure this surface exists to prevent.

**4.4 The window is sized by the data, never by a constant.** It opens on the
most recent stretch of the readings, anchored at the **last reading** and not at
the clock, and widened until it would hold about as many readings as the chart
aims to have marks. A window measured from the clock is empty the moment
ingestion stops, and an empty chart looks exactly like a working chart of a
quiet signal.

**4.5 A comparison opens where the series meet.** Over one feed the union of
the series is right. Over a comparison it is wrong, because the most recent
stretch of the union may be a span where one of them does not exist. Where
series never overlap, say so: "these were never recorded at the same time" is
the answer to the question rather than a failure to answer it.

**4.6 Moving the window is the source's arithmetic.** Zoom, pan and a typed
range are all clamped to the readings by the same rules and reported in the
same words, so a window means one thing however a reader arrived at it. The
browser never does the sum, which is what keeps a grid of panels in lockstep.

**4.7 Hue carries the quantity.** Every temperature in a figure is a shade of
one hue, from the unit, which is a physical fact and not a domain noun. Colour
is never the only carrier: every series is named at the end of its own line
with its last reading beside the name. A reader who separates none of the
colours still reads the figure correctly.

**4.8 Provenance is drawn.** A model is dashed and named as one, in the colour
of the thing it models. It comes from `source_class` in the store, never from a
pattern in a feed name.

**4.9 Fast is a feature and caching has four exits.** The catalogue is written
at conform time rather than counted on read. Caches are busted by time, by a
revision, explicitly, and by eviction, and a revision outranks the clock in
both directions. Measured: catalogue 8,935 ms to 2.7 ms, first draw 11.6 s to
1.8 s.

## 5. Which layer owns what

**Axiom** owns the chart spec, the renderer, the colour code, the unit rules,
the window arithmetic and the catalogue. Nothing here names a consumer.

**appkit** owns the surface around a figure: the picker, the window bar, the
grid, the states, and the rendering of the notes a figure carries. It shows
figures; it does not draw them. It names no domain either.

**A tenant** owns where its figures come from and what its sites and channels
are called. A tenant that finds itself rebuilding a control has found a gap in
appkit, and the fix is in appkit.

The test for a new piece of work is the standing one: if it reads a consumer's
artefact it is the tenant's, if it is about drawing it is Axiom's, and if it is
about what a person does around a drawing it is appkit's.

## 6. Scope

### Built (Phase 1)

1. Multi-select sites, each with its own block of feed and channel controls.
   Checking a second site adds a panel and changes nothing about the first.
2. Channels addressable across feeds, so a measurement and a model of it draw
   together. A channel may name its feed, and the catalogue decides what a feed
   is called so a channel name full of separators is not mangled.
3. One window above the grid governing every panel, with nudge, typed range,
   and named "last" spans that adapt to both the record and the view.
4. A column control yielding stacked, side by side and grid.
5. Units, colour, provenance, gaps, notes and the cadence-sized opening window
   as described in section 4.

### Next (Phase 2)

1. **Split by feed, unit or channel**, which is the same facet mechanism with a
   different dimension.
2. **A y-scale toggle: shared or free.** Shared scales compare magnitudes, free
   scales compare shapes. Both are right for different questions and a tool
   that silently picks one is answering a question nobody asked.
3. **The view is a URL.** A figure somebody is looking at should be a link they
   can send. This also gives back-button navigation for free.
4. **A saved view**, so a question asked weekly is asked once.
5. **Export** to PNG, SVG and the underlying rows, with the provenance line
   travelling with all three.
6. **Pointer zoom and brush selection**, keeping 4.6: the browser reports where
   the pointer is and the source does the arithmetic.
7. **A second axis on request**, already supported by the renderer and not yet
   reachable from the surface.

### Later

Annotations and shared cursors across panels. Small multiples within one panel
rather than one panel per facet value. A table view of the same selection,
since the tabular surface already exists and should share the picker.

### Not building

**Drag-and-drop placement, panel resizing, and per-panel chart types.** These
are real features in mature dashboard products and each of them presumes a
reader who already has something worth curating. We do not yet.

**A charting library in the browser.** See section 3.

**A second analytics stack.** An older PRD proposed dashboards built on Apache
Superset. Its substrate is superseded by this document as of 2026-09-28; its
requirements are inherited in §10. Two charting surfaces in one product is one
charting surface and an argument about which.

## 7. Non-functional

**Latency.** A catalogue read under 10 ms, a figure under 2 s cold and under
500 ms warm. Flipping between sites should be free, which is what the prefetch
is for.

**Determinism.** The same spec draws the same bytes. Anything the renderer
infers moves when a reader zooms, so anything that must not move is pinned in
the spec: units, colours and the window.

**Accessibility.** No figure requires colour to be read, because every series
is named at the end of its own line. Every drawn colour clears a WCAG contrast
ratio of 3.0 against white, which is the standard's bar for a graphical object,
and the palette is measured for dichromatic separation rather than asserted to
have been. Controls are reachable by keyboard and carry accessible names.

**Scale.** A figure of up to six series and roughly 800 marks. Beyond six
series on one axis a figure stops being readable, and the surface says so
rather than accepting a seventh click silently.

## 8. Open questions

1. **Where a shared y-scale is decided** when panels come from different sites
   whose units genuinely differ. Probably per unit rather than per grid.
2. **Whether a panel may hold more than one site.** Today a panel is a site
   because the lane is site-scoped. Cross-site comparison of the same quantity
   is a real question and would need the lane to accept a site per channel.
3. **How much of this belongs in the chat surface.** A figure in a conversation
   is the same document; the question is which controls travel with it.

## 9. Acceptance

A reader who has never seen the surface can, without being told anything:

1. Land on a site that is already showing something.
2. Put a measurement and a model of it on one figure and see which is which.
3. Add a second site and get a second chart without touching the first.
4. Change the window once and have every chart follow.
5. Read the unit off every axis, or read why it is missing and where to fix it.

---

## 10. Requirements inherited from the dashboards PRD

[prd-analytics-dashboards.md](prd-analytics-dashboards.md) proposed a Superset
substrate that we are not building. Its requirements came from interviews, not
from the tool, and they are owed here. None are built yet.

**10.1 Access tiers.** Nothing is public. A delayed tier serves readings older
than 24 hours to outside readers; a real-time tier serves current readings to
staff; an admin tier configures. The chart surface sits behind the platform
authz gate already, and the delayed tier is the piece that does not exist.

**10.2 Refresh.** Historical analysis tolerates a nightly batch. An operations
view needs about five minutes. A console view needs under a minute. The surface
does not poll today, so a live view is a feature, not a setting.

**10.3 Export, in four formats.** PDF for a report, PNG and SVG for one figure,
CSV for the rows behind it, and **plain text for archive**, which was asked for
by name and is the one a charting product would skip. Every one of them carries
the provenance line.

**10.4 The inventory is a list of questions people actually ask.** Operations
overview, log compliance and gap detection, component wear, inferred-state
inventory, and a shift handover summary. Read it as the demand signal for saved
views (§6, Phase 2) rather than as five dashboards to build.

**10.5 Model versus measurement.** Named there as an integration point and
built here in Phase 1. It is the one requirement this surface already answers,
and it is worth saying so: a model is drawn dashed, in the colour of the thing
it models, and named with the model that produced it.

---

## Appendix: vocabulary

A group of channels from one producer is a **feed**. Settled 2026-09-28. The
store said `stream`, the surface said "feed", and one thing with two names is
what the terminology ledger exists to prevent. `stream` is retired in this
sense across the platform; it keeps its other, unrelated meaning of an HTTP or
token stream, which is why the rename was done by sense and not by substitution.
