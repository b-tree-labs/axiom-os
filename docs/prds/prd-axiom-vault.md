# PRD: `axiom.vault` — Secret Storage + Capability Token Issuance (KEEP)

**Status:** Draft (2026-05-30)
**Owner:** Benjamin Booth
**Companion ADR:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md)
**Companion Spec:** [spec-governance-fabric.md](../specs/spec-governance-fabric.md) §2 (capability tokens), §3 (connector shape), §8.2 (vault schema), §9.2 (no-credential-outside-vault lint)
**Primitive class:** AEOS built-in extension (`axiom.extensions.builtins.vault`)
**Agent:** KEEP (Steward + Governor)

---

## 1. Elevator Pitch

KEEP holds every secret the platform has — API keys, OAuth refresh tokens, database passwords, signing keys, federation root keys — and never exposes them. Calling agents receive **capability tokens**: scoped, time-limited, revocable assertions that *this actor may do this action on this resource until then*. The vault dereferences the token to the underlying credential only inside the vault process itself; the agent never sees plaintext. Compromised agent → revoke the capability, not the credential. Federation peer asks for help → vend them a capability scoped to exactly what they need, signed by your KEEP, revocable in one operation across the trust graph. This is the property no peer harness has, and it is what makes Axiom credible for regulated work.

## 2. Problem / Opportunity

### What's broken today

- **Raw credentials are everywhere.** `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `BOX_API_KEY`, `POSTGRES_PASSWORD` live in env vars, `.env` files, shell rc files. Any agent code that runs on the host can read them. Every connector ships its own ad-hoc secret-loading. Plaintext credentials cross process boundaries with regularity.
- **No revocation story.** When a credential leaks (or might have leaked), the user's only option is to rotate at the vendor side. Every running agent that was holding the old credential needs to be informed; every cached copy needs to be purged. There is no platform mechanism for this — the user runs `pkill` and hopes.
- **No federation handoff for credentials.** A peer cohort needs to run a query against your data; the only way to authorize is to share a credential, which is unacceptable. There's no protocol for "peer X is allowed to do action Y on resource Z, but the secret stays mine."
- **No rotation discipline.** OAuth refresh tokens have lifetimes; API keys age out; signing keys rotate. There's no platform-level rotation cadence, no audit of which secrets have rotated when, no testing-via-rotation-simulation.
- **Static analysis is impossible.** Because credentials are referenced as free strings, "find every site that touches an API key" is grep, not a typed query. We can't enforce hygiene.
- **No attestation for secret-at-rest.** Secrets stored locally (in `.env`, in keychain, in plaintext config) are at the mercy of the host's posture. No hardware-attestation chain.

### Why now

- Per ADR-055 D3: capability tokens are *the* fabric-level differentiator. Without the vault, the differentiator doesn't exist.
- The notification primitive (axiom-os#278) needs vault for OAuth tokens on every outbound channel. **Vault blocks notifications.**
- The schedule primitive (axiom-os#277) needs vault for any scheduled job that invokes an authenticated external service. **Vault blocks schedule.**
- The connector shape (spec §3) is undefined without the vault binding declaration.
- The first major domain extension (Expman) is days away from needing operator notifications, which means OAuth-bound Slack/email/Teams, which means vault.
- Two recent incidents (the self-hosted-node-canary-no-auto-update finding 2026-05-30, the EC-content-routing finding 2026-05-26) both compound on the absence of capability tokens. If hosts could mint and revoke scoped capabilities, the operational risk of any single host's compromise would be containable.

## 3. Goals & Success Metrics

**Primary goal:** No agent on the platform ever sees a raw credential. Every authenticated outbound action is mediated by a capability token. Compromise of an agent is contained to the capability scope; the underlying credential remains the vault's exclusive property.

**Success metrics (post-implementation):**

| Metric | Target |
|---|---|
| Number of literal credential patterns (`api_key=`, `Bearer`, etc.) outside `axiom.vault.*` source | 0 (lint enforces, §9.2 of spec) |
| Capability token verification latency | p99 < 1 ms |
| Vault-mediated outbound HTTP overhead vs direct vendor call | < 10 ms p95 |
| Time from `axi vault revoke <capability-id>` to active session denials globally | < 1 s on a single node; < 1 heartbeat (5 s) across federation peers |
| OAuth-bound connector first-run UX (user clicks "connect", returns to Axiom) | < 30 s end-to-end |
| Rotation drill (rotate underlying secret without disrupting active capabilities) | 100% pass — zero in-flight action failures during simulated rotation |
| Hardware-attestation chain present for secrets-at-rest on supported platforms | 100% on TPM 2.0 / Secure Enclave hosts; documented fallback on unattested hosts |
| Federation handoff drill (peer presents a federation-bound capability) | 100% pass — peer never receives the underlying credential |

## 4. Key Users / Personas

| Persona | Primary tasks | Pain today |
|---|---|---|
| **Solo operator** | First-run: connect Slack, GitHub, Anthropic. Day-2: rotate the OpenAI key after a leak. | `.env` editing; manual rotation; uncertain whether agents are using the old key. |
| **PI of a regulated cohort** | Vend a capability to a federated peer's scheduled query, scoped to one Silver view, expiring in 7 days. | Impossible today — shares whole credential or nothing. |
| **Extension developer** | Add a Slack integration; declare the OAuth scopes; consume the vault's API for outbound posts. | Custom OAuth flow code + manual token storage + custom revocation; never gets revisited. |
| **Federation operator** | Admit a peer's WARDEN-signed federation-bound capability; verify the scope; permit the action. | No protocol; ad-hoc trust. |
| **Compliance auditor** | "Show me every action authorized by capability X over the last quarter." | Cross-system forensics; no integrated trail. |
| **Security responder** | Credentials suspected leaked. Revoke all dependent capabilities; rotate underlying secret; audit usage. | `pkill`; rotate; pray. |

## 5. Scope — Key Capabilities

### 5.1 The capability lifecycle API

```python
# axiom.extensions.builtins.vault.public_api

def issue_capability(
    parent: CapabilityToken,
    subject: Principal,
    intent_pattern: IntentPattern,
    resource_pattern: ResourcePattern,
    classification_ceiling: Classification,
    not_after: datetime,
    delegation_depth: int = 0,
) -> CapabilityToken:
    """Issue a child capability narrower than the parent."""

def get_capability(
    actor: Principal,
    intent: ActionIntent,
    resource: ResourceRef,
    classification: Classification,
) -> CapabilityToken:
    """Return a fresh-or-cached capability for the action."""

def revoke_capability(capability_id: str, reason: str) -> RevocationReceipt:
    """Revoke. Active sessions notified within one heartbeat; federation peers within two."""

async def outbound_call(
    capability: CapabilityToken,
    request: HttpRequest,
) -> HttpResponse:
    """The only place plaintext credentials touch outbound traffic."""

def rotate_secret(secret_ref: SecretRef) -> RotationReceipt:
    """Rotate the underlying secret; child capabilities auto-re-issue under the new secret."""

def list_secrets(actor: Principal) -> list[SecretMeta]:
    """List secrets visible to the actor — NEVER includes the secret values."""
```

Each is a single typed verb consumed by primitives + extensions; no leakage paths.

**Acceptance:** every API surface returns a typed object, never plaintext; fuzz tests verify the API cannot be coerced to leak; integration tests against the connector shape.

### 5.2 The CLI surface

```bash
axi vault connect <vendor>                # browser-based OAuth or paste-key flow
axi vault list                            # capabilities + secrets (no values)
axi vault inspect <capability-id>         # scope + expiry + remaining uses
axi vault rotate <secret-ref>             # initiate rotation cycle
axi vault revoke <capability-id> [--reason "<text>"]
axi vault delegate <capability-id> --to <peer-principal>     # federation handoff
axi vault audit --since 7d                # backed by receipts
axi vault break-glass --confirm           # tier-0 emergency disable (recorded as tamper-evident)
```

`connect` is the load-bearing first-run UX: it brokers OAuth so the user goes through one branded "Axiom is requesting access" page per vendor, rather than per-extension custom flows.

**Acceptance:** every subcommand has structured + human output; `connect` provides a standard pattern across all OAuth-bound connectors.

### 5.3 Encryption + at-rest storage

Secrets live in `vault.secrets` (per spec §8.2), encrypted with a **per-secret data-encryption key (DEK)** wrapped under the **vault master key (MK)**.

The MK's location:

- **TPM 2.0 hosts (Linux Workstation/Server)** — MK is TPM-sealed; `axi vault unlock` issues a `TPM2_Unseal` invocation. Vault process holds MK in volatile memory.
- **Secure Enclave (macOS)** — MK is Secure Enclave-bound; biometric/passcode unlock per Apple's Local Authentication framework.
- **Windows TPM** — Windows TBS API analogue.
- **Container/cloud hosts without hardware** — MK is PBKDF2-derived from a hardware-machine identity + a user-supplied passphrase; **degraded mode** flag in receipts indicates lower attestation tier; classification ceiling automatically lowered.
- **Federation hardening** — peer KEEPs cross-sign their MK fingerprints into the trust graph; significant MK rotation flagged in receipts.

**Acceptance:** the four platform paths each have integration tests; the degraded-mode classification-ceiling reduction is enforced.

### 5.4 OAuth flow ownership — vault as the OAuth client

Per ADR-055 D3 and spec §3.3: when an extension declares `oauth = { ... }` in its connector manifest, the vault — not the extension — owns the flow.

The user experience:

1. User runs `axi vault connect slack` (or follows a link in the chat REPL after the connector advertises its presence).
2. Vault opens a browser window to a standard Axiom-branded "Connect Slack" page hosted by an in-vault transient HTTP server (`localhost:31415`).
3. The page initiates the OAuth dance with Slack; refresh-token + access-token come back to the vault, never the connector.
4. Vault stores the refresh token encrypted; immediately mints a capability for the connector's declared scopes.
5. Browser redirect closes the loop; the CLI confirms the connection.

This is *one* connect UX across every OAuth-bound vendor (Slack, Teams, Discord, GitHub, GitLab, Google Drive, Box, Notion, OpenAI, Anthropic). One mental model for users; one well-trodden code path; one place to harden.

**Acceptance:** Slack + GitHub + Google Drive end-to-end OAuth flows verified at v1; the connector authors do not write OAuth code.

### 5.5 Federation handoff (cross-cohort capability)

The vault implements the federation-hop semantics per spec §7. Briefly:

- **Outbound delegation** — the local vault re-signs a capability bound to a peer's principal; the peer's KEEP exchanges it for a local capability at their cohort.
- **Inbound delegation** — a peer-presented capability is signature-verified against the peer's known root key (ADR-022), trust-score-checked (ADR-028), classification-checked against the local cohort policy, and admitted as a local capability.
- **Revocation propagation** — when a capability is revoked at the originator, the revocation record is published to a federation channel that peer vaults subscribe to. Worst-case propagation: one heartbeat per hop.

**Acceptance:** end-to-end federation drill: cohort A vends a 24-hour capability to cohort B; cohort B fires a scheduled action against cohort A's data; cohort A revokes mid-action; cohort B's next attempt is denied.

### 5.6 Rotation drill

The vault runs a quarterly self-test:

- Mint a synthetic secret + a synthetic capability bound to it.
- Initiate `rotate_secret` while a simulated workload presents the capability continuously.
- Verify: zero in-flight action failures; child capabilities re-issue transparently; the rotation receipt records the transition.

**Acceptance:** rotation drill scheduled via `axiom.schedule` (eating own dog food); test failures surface to operator via HERALD.

### 5.7 The no-credential-outside-vault lint

Per spec §9.2: a CI rule that fails the build when a credential-shaped literal (`api_key`, `Bearer`, `oauth_access_token`, etc.) appears as a local variable, struct field, or function parameter outside `axiom.vault.*` modules.

**Acceptance:** the lint runs in CI; explicit allowlist for `CredentialRef` / `KeyRef` typed references; PRs that introduce raw credentials fail.

### 5.8 Storage-backend adapter kind

A new AEOS capability kind, `secret_backend`:

```toml
[[extension.provides]]
kind = "secret_backend"
name = "macos-keychain"
entry = "axiom_ext_macos_keychain.backend:KeychainBackend"

[extension.provides.secret_backend]
classification_ceiling = "internal"
attestation_tier = "secure_enclave"
```

The vault auto-discovers and chains backends: the highest-attestation backend available holds the MK; degraded fallbacks are documented and operator-confirmable.

Initial backends: macOS Keychain (Secure Enclave-backed where possible), Linux Secret Service / TPM2, Windows Credential Manager, HashiCorp Vault, AWS Secrets Manager, 1Password CLI.

**Acceptance:** each backend ships as its own AEOS-conformant extension; the vault dispatches transparently; per-backend integration tests.

## 6. Non-Functional / Constraints

- **Performance** — capability verification p99 < 1 ms; outbound call overhead < 10 ms; rotation cycle latency < 30 s.
- **Availability** — vault MUST be reachable for any outbound authenticated action; degraded mode (cached capabilities, stale-but-not-expired) permits read-side actions to continue during a brief vault outage.
- **No plaintext outside the vault process boundary** — enforced by lint + runtime assertions + the `outbound_call` chokepoint.
- **No vault state in memory snapshots** — secrets are mmap'd from sealed storage; process snapshots redact.
- **Cross-platform** — the four platform paths (TPM2 Linux, Secure Enclave macOS, Windows TBS, degraded-mode container/cloud) are first-class, not afterthoughts. Per `[[feedback_cross_platform_support_matrix]]`.
- **Federation neutrality** — local vault operates without peer reachability; federation-hop is opportunistic, not required.
- **Auditability** — every `issue` / `revoke` / `rotate` / `outbound_call` writes a vault receipt fragment classified per the secret's classification.

## 7. Timeline (high level)

| Phase | Scope | Target |
|---|---|---|
| Phase 0 | This PRD + spec sections cross-referenced; ADR-055 merged | 2026-06 |
| Phase 1 | Local-host vault (one backend: OS keychain); capability API; one connector retrofit (Anthropic LLM provider) | 2026-06 → 2026-07 |
| Phase 2 | Three more backends (TPM2 Linux, Secure Enclave macOS, Windows TBS); `axi vault connect` OAuth flow; first OAuth-bound connector (Slack) | 2026-07 → 2026-08 |
| Phase 3 | Lint enforcement; full migration of in-tree credential sites; rotation drill | 2026-08 → 2026-09 |
| Phase 4 | Federation handoff + WARDEN integration | 2026-09 → 2026-10 |
| Phase 5 | Remaining backends (HashiCorp Vault, AWS Secrets Manager, 1Password); break-glass; tamper-evident audit | 2026-10 → 2026-11 |

Each phase ships shippable value: Phase 1 lets one consumer migrate; Phase 2 unblocks notifications/schedule for Slack/email; Phase 3 makes the platform-wide no-plaintext property real; Phase 4 unlocks federation handoff; Phase 5 covers institutional environments (HashiCorp, AWS).

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Vault becomes a single point of failure for all authenticated actions | Cached capabilities + graceful degraded-mode reads; vault crash-restart < 5 s; runbook for vault outage |
| OAuth flow UX confuses users (one browser page per vendor) | Stable per-vendor branded page; consistent UX; documented user expectations |
| Rotation simulation reveals connector-specific edge cases (vendor lifecycle mismatches) | Per-connector rotation tests in Phase 2; runbook entries per vendor |
| Federation handoff cryptography subtly wrong | Phase 4 includes external review; before-Phase-5-cutover security audit |
| Degraded-mode hosts (no TPM, no Secure Enclave) silently weaken the platform | Receipts record `attestation_tier = degraded`; classification ceiling enforced; operator-visible alerts |
| `outbound_call` becomes a network bottleneck under high-throughput agent workloads | Connection pooling per vendor; horizontal scaling via shared Postgres state + read-replicas |
| Connector authors find the OAuth-flow-stays-in-vault model too restrictive | Phase 2 has documented "custom OAuth" escape hatch with elevated review; default discourages |

**Open questions:**

- (Phase 1) Should secrets-at-rest use AES-GCM or XChaCha20-Poly1305? Trade between hardware support and modern AEAD. **Default: AES-GCM** for hardware support; revisit at Phase 5.
- (Phase 2) The "Connect Vendor" branded page — fully local (localhost:31415) or routed through axiom.dev hosted? Local avoids cloud dependency; hosted simplifies redirect-URI registration. **Default: local with hosted fallback**.
- (Phase 4) Cross-cohort revocation latency — single heartbeat or eager push? Single heartbeat is simpler; eager push is more secure. **Default: heartbeat with explicit eager-push escape for high-classification revocations**.
- (Phase 5) Break-glass mechanism — what triggers tier-0? Hardware key + spoken passphrase? Multi-party? **Default: configured-at-install N-of-M with at least one out-of-band channel.**

## 9. Acceptance & Rollout

**Sign-off:**
- Engineering: Ben Booth
- Product: Ben Booth (B-Tree Labs)
- Security review: TBD external (mandatory before Phase 4)

**Rollout plan:**
1. Phase 0–1 land on `feat/governance-fabric-vault` branch.
2. Phase 1 cuts axiom 0.26.x with vault + Anthropic-connector retrofit.
3. Phase 2 cuts 0.27 with OAuth flow + Slack connector.
4. Phase 3 cuts 0.28 with lint + migration sweep.
5. Phase 4 cuts 0.29 with federation handoff.
6. Phase 5 cuts 0.30 with the institutional backends.

**Rollback criteria:**
- Capability verification latency degrades > 2× → throttle issuance; surface alert.
- Plaintext leak detected in any process other than the vault → emergency revert + post-mortem.
- Rotation drill fails (in-flight action failures during rotation) → halt rotation; emergency manual rotate runbook.
- Federation handoff cryptography flaw → halt Phase 5 cutover; security audit re-engagement.

## 10. Contacts & Links

- Product lead: Benjamin Booth — no-reply@axiom-os.ai
- Eng lead: Benjamin Booth
- Security reviewer: TBD external (Phase 4)
- ADR: [`adr-055-unified-governance-fabric.md`](../adrs/adr-055-unified-governance-fabric.md)
- Spec: [`spec-governance-fabric.md`](../specs/spec-governance-fabric.md) §2, §3, §8.2, §9.2
- Sibling PRDs: [authz](prd-axiom-authz.md), [notifications](prd-axiom-notifications.md), [schedule](prd-axiom-schedule.md)
- Related — ADR-026 ownership, ADR-022 federation root keys, ADR-028 trust graph, ADR-052 DatabaseProvider, `prd-identity-and-bindings.md` (binding subject resolution)

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
