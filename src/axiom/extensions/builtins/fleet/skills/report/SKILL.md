---
name: fleet.report
description: Node-side push — gather a local snapshot and POST it out to the configured console (opt-in; no config means not enrolled)
allowed-tools: []
---

# fleet.report

The node side of the push model (ADR-119): gathers a local snapshot and
POSTs it OUT to the configured console. The console never reaches in.

## Params

- `push_url` (string, optional) — overrides `AXIOM_FLEET_PUSH_URL`.
- `push_token` (string, optional) — overrides `AXIOM_FLEET_PUSH_TOKEN`;
  a site-bound Bearer API key minted on the console.
- `node_id` (string, optional) — overrides `AXIOM_FLEET_NODE_ID`, else
  the hostname.
- `cadences` (object, optional) — `{kind: seconds}` declared push
  cadences; defaults declare heartbeat/service_health at 15 min and
  versions daily.

## Returns

- Not configured: `{enrolled: false}`, ok — absence of opt-in is a
  fact, not a warning.
- Pushed: `{enrolled: true, sent, accepted, notes}` where `notes` lists
  any collector that failed (a broken collector drops its kind, never
  the push).
- Push failed: ok=false with `error` — a failed push is reported as a
  failure, never as success.

## Behavior

Collectors: `heartbeat` (timestamp), `service_health` (the local
HealthChecker snapshot with observed latencies), `versions` (installed
platform package versions). Scheduling rides PULSE; this function is
the schedule's action.
