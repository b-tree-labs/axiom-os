# Terraform module — `axiom-observability`

IaC wrapper around the bundled helm chart (`../helm`). Use this when
the deployment story is pure-Terraform rather than operator-driven
`axi observe install` — both are equivalent paths to the same helm
release. The module owns namespace, credentials Secret, optional
static local PVs, and the `helm_release`; replaying it with different
variables is the intended migration path between hosts.

## Minimal use (external shared Postgres — the default)

```hcl
module "observability" {
  source          = "./deploy/terraform"
  kubeconfig_path = "~/.kube/config"
  pg_dsn          = var.shared_pg_dsn # schema=langfuse appended by the chart
}
```

Secrets (`salt`, `nextauth_secret`, `encryption_key`,
`clickhouse_password`) are minted via `random_password` when not
supplied and escrowed in the `<release>-credentials` Secret — read
minted values back from there.

## Isolated mode with node-pinned storage

```hcl
module "observability" {
  source          = "./deploy/terraform"
  kubeconfig_path = "~/.kube/config"
  postgres_mode   = "internal" # private Postgres StatefulSet
  node_name       = var.storage_node # pin PVs to one node...
  data_path       = "/data/observability" # ...on its data volume
  service_type    = "NodePort"
  node_port       = 30300
}
```

When `node_name` + `data_path` are both set the module creates a
no-provisioner StorageClass plus static local PVs (`clickhouse/`, and
`postgres/` in internal mode) under `data_path`, and points the
chart's PVCs at them. Leave both empty to use the cluster's default
StorageClass instead.

## Outputs

| Output | Meaning |
|---|---|
| `release_name` / `namespace` | Where the substrate landed. |
| `langfuse_host_hint` | In-cluster `LANGFUSE_HOST` for the env-driven trace provider. |
| `credentials_secret` | Secret escrowing minted credentials. |

Bind `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
on the Axiom process afterwards; `axiom.infra.tracing.env` picks the
`langfuse` backend automatically.
