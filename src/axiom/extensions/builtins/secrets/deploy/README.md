# `secrets` extension — deploy

Deploy artifacts for a self-hosted **OpenBao** server — the default
operational SecretStore backend for the `secrets` extension per
[ADR-003](../docs/decisions/adr-003-openbao-default-and-autorotation.md).
OpenBao is the OSS, API-compatible fork of HashiCorp Vault (image
`openbao/openbao`, listens on `:8200`, health at `/v1/sys/health`).

Two equivalent install paths — pick one:

- **Helm** — `helm/` (chart `axiom-secrets`), driven directly or by an
  install verb.
- **Terraform** — `terraform/` wraps the same chart in a `helm_release`.
  See `terraform/README.md`.

Both are generic and domain-agnostic. A consumer layer supplies only a
values overlay + (for sealed installs) its own unseal-key custody.

## Profiles

| Overlay | For | Posture |
|---|---|---|
| `helm/values-local.yaml` | k3d / kind / docker-desktop, fast iteration | **dev mode** — in-memory, auto-unsealed, **ephemeral** (restart wipes all secrets). Never for real custody. |
| `helm/values-selfhosted.yaml` | single-node persistent install | **sealed** — PVC (file backend), starts sealed, TLS-ready. |
| `helm/values-enclave.yaml` | air-gapped / resource-constrained | **sealed** — image from a **local mirror**, no external egress, tight limits. |

## Install (Helm)

```sh
CHART=src/axiom/extensions/builtins/secrets/deploy/helm

# local / dev
helm install sec "$CHART" -f "$CHART/values-local.yaml"

# self-hosted (sealed, persistent)
helm install sec "$CHART" -f "$CHART/values-selfhosted.yaml"

# air-gapped enclave — set your mirror host first
helm install sec "$CHART" -f "$CHART/values-enclave.yaml" \
  --set image.registry=registry.local

# upgrade in place (any profile)
helm upgrade sec "$CHART" -f "$CHART/values-selfhosted.yaml"
```

## Extension wiring — `AXIOM_OPENBAO_*`

The `openbao` SecretStoreProvider reads three env vars (also honored by
`axi secrets diagnose`, which fails closed if the store is unreachable):

| Env | Value | Source |
|---|---|---|
| `AXIOM_OPENBAO_URL` | `http://<release>-axiom-secrets:8200` (in-cluster) | the chart's client Service (`https` when TLS is on) |
| `AXIOM_OPENBAO_TOKEN` | a scoped token | dev: the `<release>-axiom-secrets-token` Secret · sealed: minted by the operator (below) |
| `AXIOM_OPENBAO_MOUNT` | `kv` (the kv/v2 mount path) | `extension.mount` in values |

Equivalent provider config block (ADR-003):

```toml
[[secret_store_providers]]
kind  = "openbao"
name  = "primary"
url   = "http://sec-axiom-secrets:8200"   # AXIOM_OPENBAO_URL
mount = "kv"                              # AXIOM_OPENBAO_MOUNT
# token via AXIOM_OPENBAO_TOKEN env (do not inline)
```

Read the **dev** token back:

```sh
kubectl -n <ns> get secret sec-axiom-secrets-token \
  -o jsonpath='{.data.token}' | base64 -d
```

## Unseal & bootstrap-trust

OpenBao encrypts its storage with a master key that is itself protected
by unseal keys. On every start the server comes up **sealed** and cannot
read or serve any secret until unsealed. **This chart never writes an
unseal key or a root token to cluster storage.** How the seal is opened
depends on the profile:

### dev mode (`values-local.yaml`)

Auto-unsealed by OpenBao's `-dev` server, with a well-known root token
carried in a Secret purely for local wiring. **Ephemeral and insecure by
design** — in-memory storage, single root token, no seal. Local iteration
only.

### sealed mode (`values-selfhosted.yaml`, `values-enclave.yaml`)

One-time bootstrap after the pod is Running (it will be *not Ready* until
unsealed — that is correct):

```sh
# 1. Initialize once — prints the unseal keys + initial root token.
kubectl -n <ns> exec -it sec-axiom-secrets-0 -- bao operator init
```

**Custody of the `operator init` output is the trust root.** Store the
unseal keys (or, with a KMS seal, the recovery keys) and the initial root
token in a custody store **outside this cluster** — an operator secret
manager, a KMS, or hardware-attested storage (TPM2 / Secure Enclave, per
ADR-003 §D5). Never commit them, never put them in a values file or a
ConfigMap.

```sh
# 2a. Manual unseal — repeat with the threshold number of keys, and again
#     after every restart. Simple, but a human is in the restart path.
kubectl -n <ns> exec -it sec-axiom-secrets-0 -- bao operator unseal

# 2b. Auto-unseal (recommended for anything unattended) — set a seal
#     backend so the master key is wrapped by an external KMS/transit and
#     the server returns unsealed after a restart with no operator step:
#       server.seal.type   = awskms | gcpckms | azurekeyvault | transit
#       server.seal.config = { <backend-specific keys> }
#     `operator init` then returns recovery keys instead of unseal keys.
#     Air-gapped: no cloud KMS is reachable — use a "transit" seal backed
#     by a peer OpenBao inside the enclave, or accept manual unseal.

# 3. Enable the kv/v2 mount and mint a SCOPED token for the extension
#    (never hand the app the root token):
kubectl -n <ns> exec -it sec-axiom-secrets-0 -- sh -c '
  bao secrets enable -path=kv kv-v2
  bao token create -policy=axiom-read -period=768h'
#    Wire the printed token as AXIOM_OPENBAO_TOKEN.
```

### TLS (sealed profiles)

Serve HTTPS by mounting a k8s TLS secret (`tls.crt` / `tls.key`) and
flipping the listener:

```sh
--set server.listener.tlsDisable=false \
--set server.listener.tlsSecretName=openbao-tls
```

`AXIOM_OPENBAO_URL` then becomes `https://…`.

## Storage backend

`server.storage.backend` selects `file` (default — single-node, simplest)
or `raft` (integrated storage; single-node today, the seam for HA later).
Raft additionally advertises a cluster listener on `:8201` and sets
`node_id` / `cluster_addr` in the rendered HCL.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
