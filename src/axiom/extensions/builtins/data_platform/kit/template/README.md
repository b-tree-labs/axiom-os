# Your data kit

This folder is how {{tenant}} shapes its own data on the shared platform: from
raw records to the objects, questions and charts your colleagues use.

| Folder | What you contribute | Form |
| --- | --- | --- |
| `conform/` | Raw records to canonical silver rows | A Python function, tested here |
| `silver/` | Building blocks: derived channels, roles | Declarations (TOML) |
| `gold/` | Published objects | One SQL SELECT each, plus a description |
| `verbs/` | Questions chat can answer | Declarations over a gold object |
| `charts/` | Saved views | Chart specs (scidisplay ChartSpec 1.1, JSON) |
| `automations/` | A verb on a schedule, and what to do with its rows | Declarations (TOML) |
| `reference/` | Named facts that are not a transform | TOML, every number with a unit |
| `samples/` | Raw records to try everything on | JSON lines |
| `notebooks/` | The walkthrough | A runnable notebook |

## The loop

```
axi data kit-up       # a sandbox (Postgres in Docker)
axi data kit-try      # every tier on your data, with a chart
axi data kit-check    # the gate CI runs before promotion
axi data kit-down     # drop the sandbox
```

`neut data kit-*` is the same thing. Each verb is also a tool your assistant
can call, so you can ask it to do any step with you.

## An automation: tell me when a value crosses a line

```
axi data kit-scaffold automation hot_rise --verb temperature_rise_by_day \
    --every 1h --column rise --above 30 --notify --annotate hot
```

writes `automations/hot_rise.toml`:

```toml
description = "Tell me when the daily rise runs above 30 degC."
every = "1h"                      # or: cron = "0 7 * * 1-5"; at least 5 minutes apart
verb = "temperature_rise_by_day"  # one of this kit's verbs
params = { since = "2026-09-01" } # optional: only the verb's own parameters
[when]                            # optional: without it, every run acts
column = "rise"
above = 30.0                      # or below = ...; or any_rows = true
[then]
notify = "submitter"              # a message to whoever promoted the kit
annotate = { label = "hot" }      # rows into gold_{{tenant_slug}}.annotations
```

Write the description, then `kit-try` runs it once on your sandbox and prints
exactly what it would do ("would notify the submitter: ...", "would annotate 2
row(s) 'hot'"). It sends nothing. Promote the kit, and once it is approved the
node runs it as your site's role; `axi data kit-automations` shows its schedule,
last run and outcome. To stop it, delete the file and promote again.

## What stays on your machine

Your normalizer's Python runs here, against your own files. Everything else is
a declaration the platform evaluates, and your queries run as a role that can
only see your site's rows. That is why a promoted contribution needs no one
to read your SQL to know it cannot touch another site's data: `kit-check`
proves it.
