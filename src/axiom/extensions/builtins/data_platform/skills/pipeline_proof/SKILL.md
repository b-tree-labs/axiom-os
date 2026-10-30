---
name: data.pipeline_proof
description: Prove data is flowing bronze to silver to gold per connector, and say whose problem it is when it is not.
---

# data.pipeline_proof

`axi data pipeline-proof [--connector NAME] [--observe-s 30] [--json]`

Read-only. Exits non-zero when any connector is in a pipeline fault.

## Verdicts, and who fixes each

| Verdict | Meaning | Whose problem |
|---|---|---|
| `FLOWING` | silver is current for the feed's own cadence, nothing waits in bronze, gold serves silver's latest | nobody |
| `SOURCE_SILENT` | nothing waits in bronze and silver stopped advancing: the producer stopped | the producer's owner |
| `BACKLOG` | bytes wait in bronze and the conform pass advanced recently: catching up | nobody yet; watch it shrink |
| `PIPELINE_STUCK` | bytes wait in bronze and the conform pass has not advanced in `stuck_after_s` | the platform |
| `NEVER_FLOWED` | a connector is configured and silver holds nothing for its site | whoever added the connector |
| `GOLD_DISAGREES` | gold does not serve silver's latest reading | the platform |

The report also says whether the schema stamp is current (so a deploy's schema
step will take no lock) and lists sessions idle in a transaction on
`silver.signals` with their age.

## When an agent should use it

Before telling anyone "the data is flowing", after any deploy, after adding a
connector or kit, and when a number looks old. `--observe-s` samples the conform
watermarks twice so "advancing" is observed, not inferred.

Do not read `SOURCE_SILENT` as a pipeline problem; do not call a pipeline
healthy because a row count is large.
