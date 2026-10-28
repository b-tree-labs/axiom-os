---
name: data.rederive
description: Apply a site's declarations to the rows already written, so a correction reaches its own history.
---

# data.rederive

A site fills in a unit it had never stated, declares the fault word its
hardware emits, corrects a role. `axi data rederive --site <name>` applies that
to the data already in silver.

## Why it exists

The conform insert was `ON CONFLICT (row_hash, channel) DO NOTHING`, and
`row_hash` hashes the **source row** — which does not change when a channel map
does. So a declaration made today could only ever affect data that arrived
after it. The bronze retained precisely so the platform can re-derive, 544 GB
of it on one node, was unreachable.

That is the difference between an integration that costs a partner a meeting
per gap and one that costs them a single declaration. They should not have to
re-send data we already hold in order to benefit from a fact they just told us.

## What it changes

Only the fields conform computes from a declaration: `unit`, `quality`,
`value`, `source_class`, `schema_ref`, `model_ref`, `basis`, `uncertainty`,
`role`, `derivation`.

Never `site`, `feed`, `channel`, `ts` or `row_hash`. If a re-derive produced
different ones it would not be the same reading, and rewriting them would mask
a normalizer change rather than apply a declaration.

`value` is in the list because a quality verdict withholds it — a `bad` reading
has `value = NULL` per ADR-132. That makes a re-derive able to change a number,
which is why it counts before it writes.

## Dry run by default

Everything is executed and then rolled back, so the reported count is what
would actually have happened rather than a prediction. Pass `--apply` to write.

Rows that already match their declarations are not rewritten. Postgres writes a
new tuple for every row an UPDATE touches, even one set to the value it already
held, so without that guard re-deriving a 17-million-row site would rewrite all
17 million. With it, an unchanged site costs a read.

## Scoped

Re-deriving every site because one of them corrected a unit is how a
maintenance operation becomes an outage. A named site that maps to no connector
is reported rather than passed over in silence, because silence reads as
"nothing needed changing".

```
axi data rederive --site senna-loop            # count only
axi data rederive --site senna-loop --apply    # write
```
