---
name: observe.diagnose
description: Deterministic post-install health check for the observability substrate.
---

# observe.diagnose

Walks the helm release plus the four core workloads:

- Deployment `<release>-web`
- Deployment `<release>-worker`
- StatefulSet `<release>-postgres`
- StatefulSet `<release>-clickhouse`

Returns `ok=True` when every Deployment/StatefulSet has
`readyReplicas == replicas > 0`.
