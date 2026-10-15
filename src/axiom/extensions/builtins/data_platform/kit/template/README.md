# Your data kit

This folder is how {{tenant}} shapes its own data on the shared platform: from
raw records to the objects, questions and charts your colleagues use.

| Folder | What you contribute | Form |
| --- | --- | --- |
| `conform/` | Raw records to canonical silver rows | A Python function, tested here |
| `silver/` | Building blocks: derived channels, roles | Declarations (TOML) |
| `gold/` | Published objects | One SQL SELECT each, plus a description |
| `verbs/` | Questions chat can answer | Declarations over a gold object |
| `charts/` | Saved views | Chart specs (JSON) |
| `samples/` | Raw records to try everything on | JSON lines |
| `notebooks/` | The walkthrough | A runnable notebook |

## The loop

```
axi data kit-up       # a local medallion (Postgres in Docker)
axi data kit-try      # every tier on your data, with a chart
axi data kit-check    # the gate CI runs before promotion
axi data kit-down     # remove the local medallion
```

`neut data kit-*` is the same thing. Each verb is also a tool your assistant
can call, so you can ask it to do any step with you.

## What stays on your machine

Your normalizer's Python runs here, against your own files. Everything else is
a declaration the platform evaluates, and your queries run as a role that can
only see your site's rows. That is why a promoted contribution needs no one
to read your SQL to know it cannot touch another site's data: `kit-check`
proves it.
