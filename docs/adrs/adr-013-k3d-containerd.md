# ADR-013: Private-Server Server Environment Uses k3d + containerd

**Status:** Proposed
**Date:** 2026-03-20
**Decision Makers:** Ben, Team

## Context

Private-Server is a beefy physical server at the deploying org sized for self-hosted LLM experimentation. It provides GPU compute and real disk not available in a developer laptop environment. Private-Server sits behind an org VPN (a different VPN profile from any future PrivateCloud deployment). Private-Server is **not** an export-controlled (EC) authorized computing environment; that role belongs to PrivateCloud and is a future concern. Private-Server's purpose is to validate the restricted-tier (no-cloud LLM) architecture and serve as a staging environment before any PrivateCloud deployment is designed.

**Hardware:** NVIDIA RTX PRO 6000 Blackwell (97GB VRAM), 500GB RAM, 3.3TB `/home`, 19TB `/natura`.

**LLM runtime:** `llama-server` (llama.cpp) runs directly on the host — **not** in-cluster. It currently serves `unsloth/Qwen3.5-122B-A10B-GGUF:Q4_K_M` (122B MoE / ~10B active parameters, 256K context window) on port 41883 with TLS and API key authentication. The endpoint is OpenAI-compatible (`/v1`). The GPU is fully allocated to this process; no in-cluster Ollama is deployed.

The k3d cluster (this ADR) hosts everything **except** the LLM: PostgreSQL (pgvector), the axiom signal server, and future platform services. The axiom gateway reaches llama-server at `https://private-server.example.org:41883/v1` over the host network.

The existing local development environment deploys PostgreSQL, Ollama, and related services via k3d (k3s in Docker) with a Helm chart defined in `infra/environments/local/`. A third environment — a future PrivateCloud HPC deployment — is anticipated but not yet designed.

Four capabilities are blocked without a real server environment:

1. **Self-hosted LLM validation** — Qwen on Private-Server is the target deployment for the restricted tier (no-cloud) and for an external researcher's first deployment. The end-to-end path (VS Code → Neut → Qwen on Private-Server → RAG) has not been exercised on real hardware.
2. **GPU-backed Ollama** — Ollama 0.18.x has a Metal GPU backend bug on macOS that prevents using `nomic-embed-text` locally. GPU embedding development is blocked without real server hardware.
3. **Network-accessible service endpoints** — service endpoints accessible from a developer machine over the network cannot be validated from a local container environment.
4. **PVC provisioning** — real disk behaviour (storage classes, performance, failure modes) cannot be observed in a local container environment.

## Decision

The Private-Server EC staging environment will deploy the same Helm chart as the local development environment, using **k3d on containerd** (not Docker). A new Terraform target `infra/environments/private-server/` provides Private-Server-specific value overrides.

The k3d cluster on Private-Server is named `private-server-ec` to distinguish it from any local development clusters.

## Alternatives Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **k3d + containerd (this ADR)** | Reuses existing Helm chart; containerd is smaller attack surface; no Docker daemon socket; aligns with PrivateCloud runtime | One-time k3d + containerd setup on Private-Server (~2 hours) | ✅ Selected |
| **k3d + Docker** | Matches existing local dev exactly | Docker daemon socket is a privileged attack surface; diverges from PrivateCloud runtime; Docker not present on hardened Private-Server | ❌ |
| **Raw systemd services** | No container runtime to install | Entirely different deployment topology; diverges from local and PrivateCloud; duplicates all configuration | ❌ |
| **Docker Compose on Private-Server** | Simple, familiar | Same Docker daemon concerns; not path-compatible with PrivateCloud; diverges from Helm chart | ❌ |
| **Full k8s (kubeadm)** | Closer to production k8s | Substantial operational overhead for a single-node staging machine; not worth it for one server | ❌ |

## Consequences

### Positive

- **Environments stay converged.** Local, Private-Server, and PrivateCloud are three instantiations of the same Helm chart, not three separate designs. Private-Server-specific values are isolated to `infra/environments/private-server/`.
- **Private-Server becomes a real staging gate.** Issues discovered on Private-Server — storage class behaviour, network routing, Helm chart gaps — get fixed before any PrivateCloud deployment is attempted.
- **The external researcher's deployment path is validated.** The VS Code → Neut → Qwen (Private-Server) → RAG path exercises exactly what an external operator deployment looks like on real hardware.
- **GPU-backed Ollama unblocks embedding development.** `nomic-embed-text` runs on Private-Server GPU, working around the Ollama macOS Metal bug.
- **containerd aligns with PrivateCloud.** PrivateCloud environments use containerd or Podman, not Docker. Using containerd on Private-Server validates that assumption early without requiring PrivateCloud access.

### Negative

- Requires installing k3d and containerd on Private-Server — one-time setup, estimated ~2 hours.
- Adds `infra/environments/private-server/` directory with Helm values to maintain alongside local and future PrivateCloud targets.

## Implementation

### Directory Layout

```
infra/
├── environments/
│   ├── local/          # existing local dev target (unchanged)
│   ├── private-server/         # NEW — Private-Server EC staging (this ADR)
│   │   ├── main.tf
│   │   └── values.yaml # Helm overrides: node resources, storage class,
│   │                   #   network policy, GPU requests for Ollama
│   └── hpc/            # future third environment (stub; not yet designed)
```

### Private-Server-Specific Value Overrides (`infra/environments/private-server/values.yaml`)

| Parameter | Local value | Private-Server override |
|-----------|-------------|-----------------|
| Storage class | `local-path` (k3d default) | Private-Server SSD storage class |
| Ollama resource requests | CPU only | GPU resource request added |
| Service endpoints | localhost | org VPN-accessible endpoints (`vpn_profile = "org-private-server"`) |
| Node resources | Developer laptop limits | Private-Server server limits |

### Cluster Name

```bash
k3d cluster create private-server \
  --runtime containerd \
  ...
```

The cluster name `private-server` must be used consistently in kubeconfig, Terraform state, and CI references to prevent confusion with local clusters.

### Environment Progression

```
infra/environments/local/   (developer laptop, k3d + containerd)
  ↓  same Helm chart, Private-Server values override
infra/environments/private-server/  (physical server, k3d + containerd, GPU, Qwen)
  ↓  same Helm chart, PrivateCloud values override
infra/environments/hpc/     (future PrivateCloud HPC deployment)
```

Issues discovered at each stage are fixed before progressing. Private-Server is the mandatory staging gate before any PrivateCloud deployment is attempted.

## References

- [k3d documentation](https://k3d.io/)
- [k3d containerd runtime](https://k3d.io/v5.6.0/usage/runtimes/)
- ADR-012 (`adr-012-provider-identity.md`) — provider identity model used by services deployed in this environment
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
