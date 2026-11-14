# fleet — the fleet console

Effect-checked status and release drift over nodes that **push reports
out**. The console never reaches into a node (ADR-119).

## Guarantees

- **Push-only.** Ingestion is `POST /api/v1/fleet/reports` behind a
  site-bound Bearer API key. Site attribution comes from the credential,
  never the payload; a key bound to one site cannot write another's
  nodes. The console holds no inbound credentials to any node.
- **GREEN cites evidence.** A row renders green only on an observed
  effect (dump bytes on disk, `restore_live` PASS, measured latency).
  A claim without evidence renders `unproven` — distinct from `failed`.
  Every green predicate has a test proving it can fail.
- **Silence is failure.** A report kind older than 3x its declared
  cadence renders `stale`, and a node's rollup can never be better than
  that. Judgment happens at read time, so it cannot itself go stale.
- **Opt-in per node.** A node joins by configuring its push target;
  nothing else enrolls it. No global directory.

## Surfaces

- `axi fleet status` — per-node status with evidence.
- `axi fleet report` — push this node's snapshot (rides PULSE on a
  schedule; unconfigured = not enrolled, exits ok).
- `GET /api/v1/fleet/status`, `POST /api/v1/fleet/reports`.

## Enablement (node side)

```
AXIOM_FLEET_PUSH_URL=https://<console>/api/v1/fleet/reports
AXIOM_FLEET_PUSH_TOKEN=<site-bound API key minted on the console>
AXIOM_FLEET_NODE_ID=<stable name; default hostname>
```

## Docs

ADR-119 (push model), `docs/prds/prd-fleet-console.md`,
`docs/specs/spec-fleet-console.md`. Report kinds v1: `heartbeat`,
`service_health`, `backup`, `backup_validate`, `canary`, `versions`.
Phase 2: Ed25519 envelope verification, canary HTTP sink, pins + drift
view, read-only MCP tool.
