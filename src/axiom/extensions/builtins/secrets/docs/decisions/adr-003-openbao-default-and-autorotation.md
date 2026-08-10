# ADR-003 — OpenBao-default SecretStore, cloud provider options, capability-fronted autorotation

**Status:** Proposed · **Date:** 2026-07-14
**Extension:** `secrets`
**Builds on:** adr-001 (secrets-vs-KEEP split — SecretStore is the backend, KEEP is the capability layer), adr-002 (CSI default k8s path), platform ADR-055 (KEEP / `axi vault` — capability issuance + `outbound_call`), platform ADR-093 (StoreProvider seam + serving/analytics tiers — secrets is serving-tier infra)
**Supersedes the "SEC-6 wires rotation later" TODO** carried in `providers/openbao.py`, `gcp_secret_manager.py`.

## Context

Secret custody must not assume a cloud KMS. The production posture we are heading to —
an export-controlled / air-gapped enclave — **cannot reach AWS/GCP/Azure secret managers**,
so the platform needs a **self-hosted default it fully controls**, with the cloud managers
available as quick-config options for installs that live in one cloud.

Today the `secrets` extension registers five providers (`env`, `openbao`, `kubernetes`,
`gcp`, `aws`) but:
- **there is no default-selection concept** — `register_builtins()` registers all five and
  nothing declares which is active;
- **there is no Microsoft/Azure provider** — the "cloud parity" story is incomplete;
- **rotation is backend-native only** — `SecretStore.rotate(ref)` takes just a `SecretRef` and
  delegates to the backend; most providers stub it, and none can rotate a **third-party SaaS
  credential** (mint-new + revoke-old through the *vendor's* API).

The trigger is concrete: long-lived credentials (SaaS API keys and a PAT) leaked into git
history, with no vault custody and no rotation — exactly the standing risk a self-hosted,
capability-fronted, auto-rotating store exists to remove.

## Decision

### D1 — OpenBao is the **default** SecretStore backend
Self-hosted, `kv/v2`, no cloud dependency (the provider already speaks the HTTP API over
`urllib`, so no `hvac` runtime dep — air-gap-friendly). It is the out-of-the-box and
enclave-appropriate custody. A `[secrets] default = "openbao"` config selects the active
backend, with `env` as the explicit **dev-only** fallback. Registration gains a
default-selection step and a required `available()` preflight surfaced by `axi secrets
diagnose` (fail-closed if the default can't be reached).

### D2 — Cloud managers are first-class quick-config options
Backend parity for **AWS** Secrets Manager and **Google** Secret Manager (both present) and
**Microsoft Azure Key Vault** (*to build*, mirroring the `gcp`/`aws` provider shape). An
install selects its backend by posture in a few lines of config: enclave → `openbao`;
cloud-native → the matching manager. All satisfy the same `SecretStore` protocol, so consumers
and KEEP are backend-agnostic.

### D3 — Autorotation = a **RotationStrategy** layer over the backend
The value isn't backend-native rotation — it's rotating the *actual* credential at its source.
Introduce a per-credential-kind **`RotationStrategy`** (adapter) that knows how to **mint-new +
revoke-old** against a vendor API (SaaS key, PAT, DB password), with **overlap-window
(dual-valid)** semantics so no consumer sees an outage mid-rotation. A **schedule** drives it —
a PULSE cadence, or the backend's native scheduler where one exists (AWS SM's Lambda) — and
**`force-rotate`** triggers one immediately. The SecretStore keeps versions; KEEP capabilities
always resolve to *current*. `rotate(ref)` is widened to accept the ref's bound strategy, so a
backend without native rotation still rotates via its adapter.

### D4 — Everything stays capability-fronted (KEEP)
Consumers never read a raw secret. KEEP (`axi vault`, ADR-055) issues scoped, time-limited,
revocable capability tokens and mediates every outbound call, so rotation is transparent (the
capability resolves to the current version) and revocation is instant. The SecretStore is
*custody*; KEEP is *access*.

### D5 — Deployment
OpenBao ships as an install component: a self-hosted server, a `kv/v2` mount, and unseal/root-
token custody bound to the host (OS keychain today; hardware-attested — TPM2 / Secure Enclave —
later). It is IaC-managed and **replayable into an enclave with no cloud egress**, and it slots
into ADR-093's StoreProvider seam as serving-tier infrastructure. A cloud install swaps the
`default` and the provider config; no consumer code changes.

### D6 — Remediation sequence for the current leak
For the already-exposed credentials: **store in the vault → bind each a RotationStrategy +
schedule → `force-rotate`** (the initial rotation supersedes the leaked values) → **purge from
git history**. Force-rotate is what actually closes a leak; storing a leaked value does not
un-leak it. Capability-fronting means the consumers pick up the rotated value with no redeploy.

## Consequences
- One custody model spans enclave and cloud; the air-gapped enclave is not a second-class citizen.
- Rotation becomes a **property of a credential**, not a manual chore — leaked or long-lived keys
  stop being a standing risk, and a future leak is contained by revoke + rotate, not a fire drill.
- New surface, all additive: the Azure provider, the `RotationStrategy` layer + one adapter per
  vendor (written as needed), the `rotate()` signature widening, and an OpenBao deployment.
- `axi secrets diagnose` becomes an operational gate: an install with an unreachable default
  fails closed rather than silently falling back to `env`.

## Alternatives considered
- **Cloud KMS as default** — rejected: unreachable in the EC/air-gapped target; makes the enclave
  second-class. Cloud managers remain first-class *options* (D2), not the default.
- **Backend-native rotation only** — rejected: cannot rotate third-party SaaS keys; the whole
  point is the vendor-API adapter (D3).
- **Keychain / `axi cred` as the shared store** — rejected as default: local-only, can't serve a
  multi-host or enclave install; retained for local-dev personal credentials.
- **Store-then-hope (no force-rotate)** — rejected: storing a leaked value doesn't close the leak.
