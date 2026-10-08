# Steer — design sources

The artboards behind [prd-steer.md](../../../prds/prd-steer.md) and
[ADR-137](../../../adrs/adr-137-steer-is-the-agent-activity-verb.md). Eight
screens, committed so the PRD cites a file rather than a link that may move.

| File | What it specifies | Interactive |
|---|---|---|
| `Main.dc.html` | The stream: facets, liveness, coverage disclosure, the glance gauges, the in-row conversation and its grow control, the drawer edge, the three reveals | yes |
| `Case.dc.html` | One case: the derivation, the consequence gate, the decide flow with a verbatim reason and a standing condition, Discuss, the payload toggle, the breadcrumb | yes |
| `Drawer.dc.html` | One agent: log, timeline ranges, per-agent search, the exchange-excerpt rule for conversations several agents took part in, and a composer the agent answers for itself | yes |
| `Dials.dc.html` | Tuning: the four dials with reason and expiry, teaching by conversation with the read-back, and what an agent has been taught | yes |
| `Agent.dc.html` | One agent's profile: every skill with how it arrived and when, filterable by shipped, granted, taught or no-spec, beside the dated history of grants and instructions | yes |
| `Sources.dc.html` | Origin filtering and the per-source capability matrix, including the write-back-without-observation asymmetry | yes |
| `States.dc.html` | The states that decide whether the surface lies: empty done right against empty done wrong, a dark agent, a failed run, a false success, the widest content, coverage at three magnitudes | no |
| `Rail.dc.html` | The nav decision: three rail options, the artifact verb's label and the register rule, and the route-id question | no |

`canvas.json` is the index: each artboard's frame on the canvas, the ordering
and the captions between them.

## Viewing them

These are Design Component pages and need their runtime, so opening one straight
from disk shows markup rather than a screen. Read them as source, or open the
canvas, which renders all seven and runs the interactive ones.

The canvas is private to its owner. Ask for access rather than assuming a link
works.

## Conventions they follow

Colours are the `--theme-*` token canon, not a palette invented here, so a
screen lifted into appkit keeps its appearance. Every control is a real
`button`, `a` or `input` with a label. Anything that must be told apart differs
in lightness and carries a word, never hue alone and never a bare glyph. A
truncated chart axis states its range on the face of the chart.

No consumer layer, host or site is named. The agents, nodes and capabilities
shown are generic on purpose: this is the base surface, and a design that names
one consumer would be evidence the surface is not domain-agnostic.
