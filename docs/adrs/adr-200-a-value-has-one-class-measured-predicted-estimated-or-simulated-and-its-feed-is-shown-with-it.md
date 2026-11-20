# ADR-200: A value has one class (measured, predicted, estimated or simulated), and its feed is shown with it

**Status:** Proposed (decision 3, derived handles, confirmed by the owner 2026-10-22)
**Date:** 2026-10-22
**Related:** [ADR-131](adr-131-there-is-always-a-medallion.md) (there is always a medallion), ADR-136 (uncertainty), [ADR-199](adr-199-a-bound-install-reaches-a-node-through-one-door-and-a-time-series-extension-is-a-view-over-data.md) (one door), [units and names](../conventions/units-and-names.md), [audit](../working/data-architecture-audit-2026-10-22.md)

## Context

A value in the platform is one of four kinds, and the platform has always stored
which: `source_class` is `measured`, `predicted`, `estimated` or `simulated`
(`SOURCE_CLASSES`, defined once in the ingest envelope). Three problems followed
from stopping there.

**The vocabulary is defined once in code and nowhere a person reads.** The
signal-ingest spec still says the class is *three*-valued (it predates
`simulated`); the glossary defines none of the four words, nor `feed`, `channel`
or `source_class`. The code spells the group "modelled" in one place and the
values it contains in the American spelling elsewhere. A person comparing a
reading with a forecast has to learn which word means which from the source.

**Nothing a person looks at says the class.** An audit of one production node's
feeds on 2026-10-22 found, by sampling each feed's class:

| Feed (names generalised) | Class | What the name suggests |
|---|---|---|
| `pkg.instrument` | measured (relayed by the package) | a model's output |
| `pkg.forecast` | predicted | |
| `pkg.state` | estimated | a model's output (it is a filter's) |
| `shadow.*` (three feeds) | predicted | |
| `plant.console`, `lab.epics`, `loop.sensors` | measured | |
| `demo` | simulated | |

`pkg.instrument` is the case that shows what the class alone cannot say. It is
a **pass-through**: the observations were made by an instrument, and the model
package's object hands them across unchanged. The rows are rightly `measured`;
the model made none of them. But the feed's name reads as model output, and
because the same observation can also arrive by another path (directly from the
instrument's own feed), a rollup over "all measured feeds" counts it twice.
Nothing marked the rows as relayed, nothing in a plot's legend or a picker said
so, and the name led with the package that carried them rather than the thing
that made them. Channel names inside feeds encode the class by habit
(`measured_cm`, `predicted_cm`), which duplicates the field that is supposed to
be authoritative and is not applied consistently.

**A comparison can quietly average across kinds.** `group_by source_class` makes
the comparison a first-class operation, but a plot, a legend or a CLI listing
that does not show the class lets two different kinds of thing be read as one.
The envelope's own note records the incident: synthetic flow-loop rows reached
silver stamped `measured` because no honest value was available.

## Decision

### 1. The four classes are a closed set with stated meanings

| Class | Means | Not |
|---|---|---|
| `measured` | an instrument's observation of a real system | a value computed from one |
| `predicted` | a model's statement about a real system's state, past or future | a measurement; not an estimator's output |
| `estimated` | an estimator's output that combines measurement and model (a filter, an observer) | a raw model run |
| `simulated` | values a generator produced with no referent in any real system | a worse prediction |

**A class describes the value; `derivation` describes the path.** A measurement
a model package merely hands across stays `measured`. Its `derivation` is
`relayed` (alongside the existing `raw` and `derived`, and NULL for unstated).
The producing bridge already says so at the door: it tags the rows
`origin=rom-relay` (the path that carried them) and `origin_hint=console-serial`
(where the observation was made), and its runbook calls the feed "measured,
relayed". Those tags live in bronze and are not carried into silver, so nothing
downstream can tell two paths for one observation apart. This ADR carries them
across, so that two paths for one observation are recognised as one. A rollup
across feeds counts a relayed row once, and refuses to count it again when its
origin is also in the set. Where the origin is unknown the row says so rather
than claiming `raw`.

`MODELED_CLASSES` (the three that must name what produced them) replaces the
current spelling, with the old name kept as an alias for one release.

### 2. One feed has one class

A feed's class is declared where the feed is registered and recorded with it. A
row whose `source_class` differs from its feed's declared class is a **finding**
(shown by `gaps` and `pipeline-proof`) in the first release and **refused at
conform** after one release of notice. A source that really produces two kinds
produces two feeds.

### 3. A feed is shown with its class, always, by a short handle derived from the field

The handle is `<code>:<feed>`:

| Class | Code | Example |
|---|---|---|
| measured | `meas` | `meas:lab.epics`, `meas:pkg.instrument` |
| predicted | `pred` | `pred:pkg.forecast` |
| estimated | `est` | `est:pkg.state` |
| simulated | `sim` | `sim:demo` |

The handle is **derived from `source_class` at display time**, never stored in
the feed's name. A rename forks data (the platform has paid for that more than
once), and a stored prefix would be a second place for the class to be wrong.
`axiom.data_platform.labels.handle(feed, source_class)` is the only place the
codes are written; every surface calls it: CLI listings, MCP results (as
`label`), pickers, and the legends, axis titles and series names of every plot.

### 4. Plots distinguish class by mark as well as colour

When one plot carries more than one class, each series' legend entry leads with
its handle and its line style encodes the class: solid for `meas`, dashed for
`pred`, dash-dot for `est`, dotted for `sim`. Colour alone never carries the
class. An aggregate across classes is refused unless the caller groups by class.

### 5. The vocabulary has one definition and a guard

The closed set and the table in decision 1 live in the glossary; a test asserts
the glossary lists exactly `SOURCE_CLASSES` and that no document states a
different count. `feed`, `channel`, `site` and `source_class` are defined in the
same place. A new feed's name begins with the system that **made** the values
(the instrument or the model), not the one that carried them, and does not
contain a class word; the handle supplies the class. A feed that only relays is
named for its origin where it can be.

## Phases

| Phase | Delivers | Gate |
|---|---|---|
| **P1** Words | glossary entries; the ingest spec corrected to four values; `MODELED_CLASSES`; the guard test | the guard fails if the glossary and `SOURCE_CLASSES` disagree or a doc claims another count |
| **P2** Handle | `labels.handle`; CLI listings and MCP results carry `label`; `catalog` and `pipeline-proof` show handles | a test over every listing verb: no feed is printed without its handle |
| **P3** Plots | the shared chart components render handles, line styles and the cross-class refusal | a rendering test per class; a plot mixing two classes shows both handles and two styles |
| **P4a** Relayed | `derivation = relayed` and the relay tags (`origin`, `origin_hint`) carried from bronze into silver at conform; cross-feed rollups count a relayed row once; the existing pass-through feeds are declared | for a fixture where one observation arrives by two paths, a cross-feed aggregate counts it once; `gaps` lists any relayed feed without an origin |
| **P4** One class per feed | declaration at registration; mismatch findings in `gaps` and `pipeline-proof`; the audit's findings fixed on live nodes by declaring, not renaming | a node with a mixed feed reports it; none reported on the audited node after the fix |
| **P5** Refusal | conform refuses a mismatched row after the notice release | a row with a class that contradicts its feed is quarantined with the reason |

## Consequences

- A reader can tell a measurement from a forecast from the label alone, in a
  table, a legend or an agent's answer.
- Feed names stay as they are; nothing forks.
- A feed with mixed classes becomes visible and then impossible.
- Cost: every surface that prints a feed or draws a series is touched once, and
  plots gain a rule about line style. The channel-name habit (`measured_cm`) is
  left alone, as the units-and-names convention requires, and becomes redundant
  rather than wrong.

## Alternatives considered

- **Store the prefix in the feed name** (`meas.lab.epics`). Forks every existing
  feed, duplicates the class in a second place, and makes a mislabelled prefix
  indistinguishable from the truth. A new-feeds-only version leaves two naming
  schemes in one catalogue.
- **Keep the class a column nobody sees.** The status quo, and the cause of the
  audit's first finding.
- **Colour-code classes.** Fails for colour-blind readers and in greyscale
  print; line style carries it reliably.
- **Fold `estimated` into `predicted`.** A filter that uses measurements is not
  a raw model run, and conflating them overstates what a comparison against
  `measured` proves.

## Open questions

1. ~~Whether new feeds should also carry the code in their canonical name.~~ **Decided 2026-10-22: derived.** The handle is computed from `source_class` at display time and is never stored in a feed's name, for existing and new feeds alike.
2. Whether `estimated` needs a sub-handle for the producing estimator
   (`est:pkg.state`), or `model_ref` is enough. The proposal is `model_ref`.
3. The codes: `meas`, `pred`, `est`, `sim` are four to three letters and
   unambiguous; shorter (`m`, `p`, `e`, `s`) saves width on a narrow axis and
   costs legibility.
4. Whether the relay tags (`origin`, `origin_hint`) become two nullable columns
   or one reference carried in `derivation` itself. A column is queryable and indexable; the string is free.
   The proposal is a column, added with the expand-then-contract rule (ADR-182).
