---
name: observe.install
description: Provision the Langfuse trace+eval substrate on K8s.
---

# observe.install

Installs the bundled helm chart (Langfuse v3 + Postgres + ClickHouse).
Mints required secrets when not supplied. Surfaces the resolved
`LANGFUSE_*` env triple; binding those vars on the Axiom process turns
on the env-driven trace provider (`axiom.infra.tracing.env`).

## Inputs

- `namespace` (default: `axiom-observability`)
- `release` (default: `axiom-observability`)
- `kube_context` (default: active)
- `salt`, `nextauth_secret`, `encryption_key`, `postgres_password`,
  `clickhouse_password` (auto-minted when empty)
- `dry_run` (bool)
- `skip_diagnose` (bool)

## Returns

`SkillResult.value = {release, namespace, context, env, secrets}`
