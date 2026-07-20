# Terraform module — `axiom-secrets`

IaC wrapper around the bundled OpenBao helm chart (`../helm`). Use this
when the deployment story is pure-Terraform rather than operator-driven
`helm install` — both are equivalent paths to the same release. The
module owns the namespace and the `helm_release`; a values overlay
supplies the profile, and replaying the module with a different
`image_registry` / `values_file` is the intended migration path between
hosts (self-hosted → air-gapped enclave differs only by those two).

The module mints **no** secrets. OpenBao's unseal keys and root token are
produced by `bao operator init` inside the running server and escrowed
**outside** this cluster — Terraform never sees or state-persists them.

## Self-hosted (sealed, persistent — the default)

```hcl
module "secrets" {
  source          = "./deploy/terraform"
  kubeconfig_path = "~/.kube/config"
  values_file     = "${path.module}/deploy/helm/values-selfhosted.yaml"
  storage_size    = "5Gi"
}
```

After apply the server is **sealed**. Initialize + unseal once (see the
top-level `deploy/README.md` § Unseal & bootstrap-trust), then mint a
scoped token for the extension.

## Air-gapped enclave (image from a local mirror)

```hcl
module "secrets" {
  source          = "./deploy/terraform"
  kubeconfig_path = "~/.kube/config"
  values_file     = "${path.module}/deploy/helm/values-enclave.yaml"
  image_registry  = "registry.local"   # your mirror; no external egress
}
```

## Local / dev (ephemeral, auto-unsealed)

```hcl
module "secrets" {
  source          = "./deploy/terraform"
  kubeconfig_path = "~/.kube/config"
  values_file     = "${path.module}/deploy/helm/values-local.yaml"
  server_mode     = "dev"
  dev_root_token  = var.dev_root_token   # supply here, not in the overlay
}
```

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `kubeconfig_path` | — | Kubeconfig for the target cluster. |
| `namespace` / `release` | `axiom-secrets` | Where the substrate lands. |
| `chart_path` | `../helm` | Bundled chart path. |
| `values_file` | `""` | Profile overlay; empty = chart defaults (sealed). |
| `image_registry` | `docker.io` | Point at a local mirror for air-gap. |
| `image_repository` / `image_tag` | `openbao/openbao` / `2.2.0` | Image coordinates. |
| `server_mode` | `sealed` | `dev` (ephemeral) or `sealed` (persistent). |
| `storage_size` | `2Gi` | Data PVC size (sealed). |
| `service_type` | `ClusterIP` | Client service type. |
| `mount` | `kv` | kv/v2 mount → `AXIOM_OPENBAO_MOUNT`. |
| `dev_root_token` | `""` (sensitive) | dev-mode root token; ignored when sealed. |
| `extra_values` | `{}` | `--set`-style chart-value passthrough. |

## Outputs

| Output | Meaning |
|---|---|
| `release_name` / `namespace` | Where the substrate landed. |
| `openbao_url` | In-cluster address → `AXIOM_OPENBAO_URL` (https when TLS is enabled). |
| `openbao_mount` | kv/v2 mount → `AXIOM_OPENBAO_MOUNT`. |
| `dev_token_secret` | dev-mode Secret holding `AXIOM_OPENBAO_TOKEN` (empty in sealed mode). |

Bind `AXIOM_OPENBAO_URL` / `AXIOM_OPENBAO_TOKEN` / `AXIOM_OPENBAO_MOUNT`
on the Axiom process afterwards; the `openbao` SecretStoreProvider reads
them, and `axi secrets diagnose` fails closed if the store is unreachable.
