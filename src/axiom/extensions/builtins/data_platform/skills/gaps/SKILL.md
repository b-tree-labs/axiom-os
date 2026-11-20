---
name: data.gaps
description: What one site would need to declare, ordered by how much each would fix.
---

# data.gaps

`axi data gaps --site senna-loop`

## Why it exists

A steward cannot declare what they cannot see. The detection for most of this
already existed and was scattered across four modules and a query somebody ran
once, and every gap found on the live node this week was found because a
person went looking. That is not a process.

A steward should **meet** the list. They should not discover it.

## What it reports

Each gap carries four things: what is wrong, the specific act that closes it,
what closing it would **do**, and who can do it.

- **declare** — a unit, a role, a fault word. The steward can close it.
- **retract** — a channel that has never varied. Per ADR-042 D10 a retraction
  is not a correction: nothing about those rows is incorrect, and declaring a
  replacement value would assert something false about every one of them.
- **reclassify** — the values are right and the *kind* is wrong, such as
  configuration stored as a reading.
- **producer** — nothing declarable closes it. A timestamp collision needs
  finer timestamps at the source.

## Two choices worth knowing

**Ordered by rows, not by severity.** A severity is a judgement made on a
site's behalf, and 19.1 million unitless rows and three unitless rows are the
same defect and nowhere near the same problem. A count is a fact and it sorts
correctly without anyone grading anything.

**Every gap states its effect in rows and direction.** A proposal without a
blast radius reads as safe. "Withholds 46,662 readings" and "adds a unit to
3,430,514 rows" are opposite kinds of change wearing the same word, and the
first one changes every chart and mean over those channels.

## What it does not do

It changes nothing. Closing a gap is `axi data rederive`, which counts before
it writes.

Timestamp collisions are off by default, because that query groups every row
the site has. Pass `--collisions` when you want it, and the report says when
it was skipped rather than leaving a silent hole.
