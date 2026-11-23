---
name: data.tier_audit
description: Report the medallion's actual shape and anything ADR-128 did not expect.
---

# data.tier_audit

Reads `information_schema` across bronze, silver and gold and says what is
there: how many base tables and how many views in each tier, which tiers are
absent, and every object the rule did not expect.

## Why it exists

ADR-128 fixes the rule — bronze lands, silver conforms, gold serves — and a
static test holds the *code* to it. The database is where the drift actually
showed up, and no test can see the database. On one node gold held eleven base
tables that no platform code creates, while authored reference records of the
same kind sat in silver. Nothing made either visible.

## What it reports

- **An undeclared base table in gold.** Gold is views over silver plus the
  reference data somebody declared. An undeclared base table there is either
  authored data nobody wrote down (ADR-128 E1/E3) or a serving surface that
  quietly stopped being derived.
- **A gold view reading bronze.** That skips the conform tier entirely, so
  whatever it serves was never validated.
- **A gold view that transforms rather than derives.** Gold may select,
  filter, join, rename, bucket and aggregate — operations that change the
  shape of an answer. A unit conversion, a `coalesce` from a literal, a `CASE`
  repair or string cleaning changes what a record *means*, and that is
  silver's work. Reported for a person to judge, never failed: a rate really
  is a derivation even though it changes the unit, and no pattern can tell
  that from a conversion. What it can do is stop it being invisible — on one
  node, three gold views each converted watts to megawatts independently and
  nothing compared them.

Declare authored reference tables in `AXIOM_GOLD_REFERENCE_TABLES` as a
comma-separated list of bare table names.

## What it does not do

It changes nothing and fails nothing. A table that appeared in gold by hand is
a decision somebody made; the useful thing is that it stops being invisible.

```
axi data tier-audit
axi data tier-audit --schemas bronze,silver,gold --json
```
