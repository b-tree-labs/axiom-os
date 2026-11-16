# axiom-cluster

The cluster base: layer 2 of ADR-215. It gives every Kubernetes size the names
the application chart (`axiom-node`) relies on, so a node is installed the same
way on K3D, k3s, RKE2 or a cloud's managed cluster.

| Name | What backs it |
|---|---|
| Storage classes `axiom-fast`, `axiom-standard` | local-path (k3s, K3D), OpenEBS LocalPV (RKE2), EBS, Persistent Disk or Azure Disk |
| Ingress class `axiom` | Traefik: bundled by k3s and RKE2 v1.36+, installed here on a cloud |
| Certificate issuer `axiom-letsencrypt` or `axiom-selfsigned` | cert-manager |
| Postgres operator | CloudNativePG |
| Secret store `axiom` | External Secrets: a namespace in the cluster by default; Secrets Manager, Secret Manager, Key Vault or OpenBao by provider values |

Each component has `install`. `false` means the platform already provides it,
and only the name is bound.

```bash
helm dependency build infra/charts/axiom-cluster
helm install base infra/charts/axiom-cluster -n axiom-system --create-namespace \
  -f infra/charts/axiom-cluster/values-small.yaml --wait        # or -medium, -large
# a cloud: -f values-large.yaml -f values-provider-aws.yaml (or -gcp, -azure)
python -m axiom.infra.deploy.cluster_contract                   # read-only; non-zero if a name is missing
```

`infra/smoke/cluster_base_smoke.py` installs the small size on a K3D cluster it
creates, checks the contract, and uses each name: a Postgres cluster on
`axiom-fast` and a secret through the store `axiom`.

`micro` has no cluster base: it is Compose rendered from `axiom-node`.
