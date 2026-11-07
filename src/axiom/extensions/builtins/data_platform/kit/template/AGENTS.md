# Working in this data kit (for coding assistants)

This folder is a tenant's data kit for a shared data platform. You help the
person here shape their own data, from raw records to the gold objects,
questions and charts their colleagues use. Read `README.md` for the layout.

## The loop, and the tools for it

| Step | CLI | Tool |
| --- | --- | --- |
| Start the local medallion | `axi data kit-up` | `data.kit_up` |
| Run every tier and show it | `axi data kit-try` | `data.kit_try` |
| The gate before promotion | `axi data kit-check` | `data.kit_check` |
| Remove the local medallion | `axi data kit-down` | `data.kit_down` |

After any edit, run `kit-try` and show the person what changed. Before
suggesting a pull request, run `kit-check` and report its result exactly. An
isolation result of `not run` is **not** a pass; say so.

## Making a change

- **Raw records → silver:** copy `conform/rig_frame.py`. One pure function per
  schema_ref; never set `site`, `schema_ref` or `row_hash`; skip an absent
  channel rather than raising; timestamps carry a timezone; give every channel
  a unit. Add a test beside it in `conform/tests/` and run it.
- **A building block:** a `[[derived]]` entry in `silver/derived.toml`
  (`<a> <op> <b>`), or a role in `silver/roles.toml`. Use a role somebody else
  already uses (`axi data roles`); an invented role groups nothing.
- **A gold object:** `gold/<name>.sql` is one SELECT over `silver.signals`,
  `gold.*` or this tenant's own schema. No `WHERE site = ...`: the query runs as
  the tenant's role and only ever sees its own rows. Write `gold/<name>.toml`
  with a description a colleague would understand; chat reads it.
- **A question for chat:** `verbs/<name>.toml` names one gold object and a
  query over `{object}` with `%(param)s` parameters, each declared with a
  default. The description is what an assistant reads to decide whether to
  call it, so state what it answers and in what unit.
- **An automation:** `automations/<name>.toml` runs one of this kit's verbs
  `every = "1h"` (or on a five-field `cron`, at least 5 minutes apart), with
  only that verb's declared `params`; an optional `[when]` (`column` plus
  `above` or `below`, or `any_rows = true`); and a `[then]` with
  `notify = "submitter"` and/or `annotate = { label = "..." }`. Start it with
  `kit-scaffold automation`. `kit-try` shows what one run would do and sends
  nothing; it only acts on the node, after promotion.
- **A chart:** `charts/<name>.json` is a scidisplay ChartSpec (schema 1.1) —
  the same document the shared app draws: `schema_version`, `kind`
  (`timeseries`), `site` (this kit's tenant), `channels`, an absolute
  `window` (`from`/`to` with offsets), `transform` (`bucket` like `1m`/`1h`,
  `agg` like `mean`), `title`. Name channels, never a table: which rows serve
  them is the resolver's job. One unit per chart. Unknown or reserved fields
  (`marks`, `device`) and invisible characters are refused — fix the file,
  do not strip and retry.

## What not to do

- Do not edit `samples/` to make a check pass. They are the person's data.
- Do not weaken `kit-check` findings into notes, or call a finding a flake. Fix
  the file it names, or tell the person what it would take.
- Do not put credentials anywhere in this folder.
- Do not invent units, roles or uncertainties. Ask the person; if nobody
  knows, say unknown (`unit = "unknown"`, `[uncertainty] posture`).
