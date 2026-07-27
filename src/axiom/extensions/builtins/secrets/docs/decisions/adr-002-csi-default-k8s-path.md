# ADR-002 (secrets) — CSI Secret Store driver as the default K8s delivery path

**Status:** Accepted · **Date:** 2026-05-31 · **Owner:** Benjamin Booth

## Context

Operational secrets need to reach in-cluster workloads (Dagster
webserver + daemon, RAG embedder, future PLINTH skill pods) at boot
time. Two patterns dominate in the OpenBao / Vault ecosystem:

1. **CSI Secret Store driver** — a Kubernetes-native CSI volume that
   projects secrets onto pod-local tmpfs at mount time. Provider-agnostic:
   the same `SecretProviderClass` shape works against OpenBao,
   AWS Secrets Manager, Azure KV, and GCP Secret Manager.
2. **OpenBao agent-injector** — a mutating admission webhook that
   adds a sidecar to every annotated pod; the sidecar templates secrets
   onto a shared volume. OpenBao/Vault-specific; battle-tested.

Both deliver secrets to pods without the pod ever talking to the secret
backend directly. The choice shapes how `axi data install` writes its
helm values and how a future swap from OpenBao to a cloud KMS
plays out.

## Decision

**CSI Secret Store driver is the default.** Agent-injector is documented
as an escape hatch and reachable via `axi data install
--secret-delivery=injector`, but the CSI path is what the runbook
follows.

Why:

- **Provider-agnostic.** Same `SecretProviderClass` template works
  unchanged when a deployment swaps OpenBao for a cloud KMS — only the
  driver binary changes. With injector, the swap requires re-templating
  every annotated workload.
- **No sidecar tax.** Dagster, embedder, and future PLINTH pods stay
  single-container. Sidecars duplicate per-pod memory + restart blast
  radius.
- **Matches the spirit of the provider registry.** The CSI driver's
  per-backend providers parallel our `SecretStoreProvider` registry
  one-for-one.

## Consequences

- The `kubernetes` SecretStoreProvider (SEC-3) emits
  `SecretProviderClass` manifests and reads via mounted tmpfs paths.
- Operators wanting injector get a documented opt-in flag, not a
  silently-different code path.
- The CSI driver + OpenBao provider become a deploy-time prereq on the
  cluster — `axi data install` adds a preflight check.

## Alternatives considered

**Agent-injector as default.** Rejected — the lock-in to OpenBao-shaped
annotations makes a cloud-KMS migration harder than necessary.

**Native Kubernetes Secrets only (no external store).** Rejected — K8s
Secrets are not encrypted at rest by default, the operator has to manage
rotation by hand, and there is no audit stream. Adequate for `mode=dev`,
inadequate for a self-hosted node or a cloud-KMS enclave.
