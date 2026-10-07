# The colour code for charts

A colour that means one thing in one figure and another in the next is
decoration. Two promises make it a code instead:

**Related quantities look related.** Every temperature in a figure is a shade of
one hue, so a reader sees they are the same kind of thing before reading a
single label. The hue comes from the UNIT, which is a physical fact and not a
domain noun: the platform never learns what a channel measures, only what it is
measured in.

**The same channel is the same colour.** Exactly, when it is pinned or declared.
By hue always, whatever else is drawn beside it.

Every claim below is checked by a test rather than asserted in a comment. That
distinction has already earned its place once: an earlier palette carried a
comment saying it had been "checked for deuteranopia separation", and measured,
two of its colours sat **4.9** apart under deuteranopia, which for two thin
lines is the same colour. The comment was not written dishonestly. It was
simply never run.

## 1. Colour is never the only carrier

Every series is named at the end of its own line, with its last reading beside
the name. No legend, no lookup.

This is what makes everything below a quality bar rather than a correctness
one. A reader who separates none of these colours — on a photocopy, on a
projector, with any colour vision at all — still reads the figure correctly. A
chart that needs its colours to be read fails silently for about one reader in
twelve.

## 2. Hue carries the quantity

| quantity | anchor | status | why |
|---|---|---|---|
| temperature | `#953e33` | **convention** | hot is red; the most widely held colour convention there is |
| pressure | `#5c9358` | **convention** | the process-and-instrumentation habit |
| flow | `#2a8fbf` | **convention** | water is blue on every schematic ever drawn |
| radiation | `#8d3c73` | **convention** | ISO 361 and ANSI Z535 put the trefoil in magenta |
| power, energy | `#4a77c1` | allocated | no received convention; allocated so power channels group |
| electrical | `#834c16` | allocated | no received convention; allocated so electrical channels group |
| length, position | `#05765d` | allocated | no received convention; allocated so positions group |
| ratio, percent | `#7a7297` | allocated | a ratio is not a quantity of a thing, so it is drawn quietly |
| **no unit declared** | `#868686` | allocated | the unit was never declared, and the colour says so |

Four of these are conventions a reader already holds. The rest are **arbitrary
but stable**, and the module says which is which, because claiming a convention
for electrical potential would be inventing one and then citing it. What the
allocated families still buy is grouping, and that is the part worth having.

An SI prefix is stripped before the lookup: `kW` and `MW` land where `W` does,
because a prefix changes the size of a number and never what it measures.

A series whose unit was never declared is drawn grey. The figure looks as
uncertain as it is, which is the same thing the axis label already says in
words.

### Why the bands differ in lightness

Two of the four conventions are **red and green**, which is precisely the pair
about eight percent of men cannot separate by hue. Measured, a temperature and
a pressure sat 6.9 apart under deuteranopia, which is nothing.

Hue cannot carry that distinction and keep the convention, so **lightness
carries it**. Each quantity draws from its own lightness band, and lightness is
what a dichromacy leaves alone. That lifts the worst pair of quantities to 11.5
under normal vision, deuteranopia and protanopia alike. It is also where the
shades within a family come from, which is why each band is wide enough for
four.

This is a real limit, stated rather than papered over: a reader with
deuteranopia separating a red line from a green one is relying on the lightness
difference and on the labels, not on the hue.

## 3. The same channel is the same colour

Three tiers, highest wins.

| | where it lives | travels with the figure | overruled by |
|---|---|---|---|
| **declared** | the chart document's `series_colours` | yes | nothing |
| **preferred** | `~/.axi/chart-preferences.toml` | no | the document |
| **derived** | the unit and the channel's name | — | both |

```
neut daq chart --always-colour Power=blue      # every chart, from now on
neut daq chart --colour Power=blue             # this chart, in its document
neut daq chart --colours                       # what is pinned, and where
neut daq chart --forget-colour Power
```

What is guaranteed, and what is not, because the difference is the whole point
of pinning:

- **The hue always holds.** A channel measured in degrees is warm in every
  figure it appears in, whatever else is drawn beside it.
- **The shade holds while the company does.** Several channels of one quantity
  are spread across the family's band so each stays legible, and which shade a
  channel takes depends on how many share its quantity in that figure. Alone,
  it always takes the anchor.
- **A pinned channel never moves at all.** That is what pinning is for, and it
  is one command.

A preferences file that cannot be read stops the chart. Drawing in the derived
colours while somebody's own settings sit unread in a file is the quiet kind of
wrong: the figure looks fine, and it is not the figure they asked for.

Order comes from a stable hash of the channel name, never from `hash()`, which
is randomised per process — a figure drawn twice would have come out in
different colours — and never from the order a caller passed its series in.

## 4. Provenance is carried by the dash, not by the hue

**A model wears the colour of the thing it models, drawn dashed, and labelled
`(model)`.**

An earlier rule reserved one red for modelled output. It said "this is a model"
and lost *which* model: two channels and their two models drew both models in
the same red, and a reader could not pair them. Only one thing can own hue, and
the quantity owns it, because provenance has a better encoding available and
the quantity does not. The model now carries its code three ways over — same
hue, dashed stroke, and the word — where colour was only ever one.

## 5. When a family is too crowded, the figure says so

Past four channels of one quantity the shades stop separating. The names at the
ends of the lines still tell them apart, so the figure is readable; it just has
to say which crutch it is leaning on.

> more temperature channels than the colour band separates; the names at the
> ends of the lines tell them apart

## 6. Contrast

Every colour clears a WCAG contrast ratio of 3.0 against white, which is what
the standard asks of a graphical object (4.5 is the bar for text). A band that
would yield something too pale to see is darkened until it carries, rather than
drawing a line the reader cannot find.

## Where the rules live

- The families, the derivation and the three tiers:
  `src/axiom/extensions/builtins/scidisplay/chart_colour.py`
- What a person pinned: `.../scidisplay/colour_preferences.py`
- The measurement harness: `.../scidisplay/tests/_colour_vision.py`
- The checks: `.../scidisplay/tests/test_the_colour_code.py` and
  `.../tests/test_a_remembered_colour.py`

Raising a floor is welcome. Lowering one is a decision, and the test is where it
gets made rather than noticed.
