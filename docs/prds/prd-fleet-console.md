# PRD: Fleet Console

**Status:** Living. **Owner:** platform. **ADR:** ADR-119. **Spec:** `docs/specs/spec-fleet-console.md`.

## Problem

An operator running more than zero nodes has no single place to answer:
are my nodes alive, did last night's backup actually restore, did the
canary go green, which versions are actually deployed, and does deployed
reality match what I declared? Today each answer is a hand-run command on
the right box — and several past incidents were reports that did not
match reality (a "armed" backup that could not resolve its DSN; heartbeats
that wrote while the agent was dead). The console exists to make fleet
state visible **without weakening the edge posture**: nodes push out,
nothing reaches in.

## Users

- A **tenant operator** watching their own site's nodes (the common case).
- A **fleet operator** granted several sites, comparing across them.
- **Agents** (TIDY et al.) reading the same views to propose remediation.

## Requirements

### R1 — Push-only report ingestion
A node pushes signed reports to one authenticated endpoint. Report kinds
v1: `heartbeat`, `service_health`, `backup`, `backup_validate`, `canary`,
`versions`. The console never initiates a connection to a node. Site
attribution comes from the pushing credential, never the payload.

### R2 — Fleet status view
One view (CLI first; API for the web tier) listing each node with: report
freshness per kind, worst-status rollup, and the evidence behind any
non-green state. Available per tenant; a multi-site grant sees its sites
and nothing else (404-not-403 beyond scope).

### R3 — Effect-checked rendering
Green requires cited observed-effect evidence. A claim without evidence
is UNPROVEN, visually distinct from FAILED. Every green predicate has a
test that proves it can fail.

### R4 — Silence is failure
Each node declares its push cadence per report kind at opt-in. A kind
stale past 3× its cadence renders STALE, and the node's rollup can never
be better than STALE. No last-known-green.

### R5 — Release channels and drift (surface 2)
Declared pins are recorded centrally: which platform/site versions each
scope should run, per channel. Nodes report deployed versions (`versions`
report). The console computes drift = declared minus deployed, per node,
and renders it in the same status view. No pin is ever pushed to a node
from the console — declaration here, actuation stays with the node's own
deploy path.

### R6 — Opt-in per node
A node joins the console by configuring its push target + credential;
removing that config removes it from the fleet (rows age into STALE, then
archive per retention). There is no console-side enrollment of a node
that did not push.

### R7 — Receipts
Every console-visible mutation (pin declared, node archived, credential
minted) carries an audit receipt id.

## Non-requirements (v1)

- No remote actuation of any kind (no restart/upgrade/rollback buttons).
- No global directory across tenants; no federation topology map.
- No metrics/time-series storage — the console keeps latest-per-kind plus
  a bounded report log; long-horizon telemetry stays with the data
  platform.
- No web UI in v1 — the API view is the contract; the web tier renders it
  later through the standard `/api/v1` surface.

## Success criteria (dogfood)

1. The standing chores disappear: backup-validate outcomes, canary
   status, and node liveness for our own tenancy are answered from one
   command with evidence attached.
2. The two-pin-locations drift that previously required manual diffing is
   a rendered view.
3. At least one real "report ≠ effect" defect is caught by an UNPROVEN or
   STALE rendering before a human notices it — the console's reason to
   exist, observed at least once.
