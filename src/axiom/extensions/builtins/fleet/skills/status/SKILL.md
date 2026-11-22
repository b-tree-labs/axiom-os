---
name: fleet.status
description: Read-time fleet view — per-node per-kind effect-checked status with evidence and worst-wins rollup
allowed-tools: []
---

# fleet.status

The console view over the fleet store. Every judgment happens at read
time from stored reports, so the judgment cannot itself go stale.

## Params

- `sites` (list of strings, optional) — narrow to these sites. The HTTP
  surface resolves scope from the credential instead and never trusts
  the query; this param is for console-local CLI use.
- `include_archived` (bool, default false) — include archived nodes.

## Returns

`value.generated_at` — ISO timestamp of the evaluation.
`value.nodes[]` — one entry per node:

- `node_id`, `site`, `display_name`, `profile`
- `rollup` — worst-wins across kinds: `failed > stale > unproven >
  unknown > green`. A node with no reports rolls up `unknown`, never
  green.
- `kinds.{kind}` — `status`, `evidence` (the cited observed effect or
  the reason green was refused), `received_at`, `signature_state`.

## Behavior

GREEN requires cited observed-effect evidence (ADR-119): a dump's
`size_bytes > 0` with a fresh `created_at`; all backup-validate checks
PASS including `restore_live`; services healthy with observed latency.
A claim without evidence is `unproven` — distinct from `failed`.
Anything older than 3x its declared cadence is `stale`; staleness beats
content, so there is no last-known-green.
