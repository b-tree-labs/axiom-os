# ADR-077: Local Principal Authentication & Progressive Trust

**Status:** Proposed (2026-06-11)
**Deciders:** Benjamin Booth
**Related:** ADR-041 (Identity Acquisition & Verification), ADR-055 (Unified
Governance Fabric / KEEP), ADR-022 (Federation Identity Roots), ADR-075
(SSO/OIDC Delegated Auth), `prd-axiom-vault`, `spec-aeos` addendum (this set).
**Supersedes the placeholder:** `vault/capability_store.py`
`signature=b"\x00"*64` / `public_bytes=b"\x00"*32`.

---

## Context

Today the principal is **caller-asserted**: `issue_capability(subject: Principal, …)`
takes the subject as a parameter; the capability signature is a placeholder;
KEEP performs no signature/principal verification. The only thing authenticating
"me" is the **OS process boundary** — whoever holds the shell + venv is implicitly
me. For a single-user laptop that is acceptable; for a cross-project vault holding
real credentials (a shared HPC/API key, Box, Postgres, org resources) it is obfuscation, not
security. We have hit this 4–5 times.

But we have run the *entire* build so far in this zero-auth mode, productively.
Demanding authentication everywhere would destroy that velocity and is wrong for
solo/dev/air-gapped-single-purpose nodes. **The requirement is not "add auth" —
it is "make auth a posture," default-off, that specific nodes and specific
resources can demand, with the human principal introduced only at moments of
consequence.**

## Decision

### 1. Identity is a *posture*, not a boolean — the assurance ladder

Every node has an **identity posture**: the minimum assurance level it enforces.

| Posture | Principal source | `ctx.principal` | Friction | For |
|---|---|---|---|---|
| **`open`** (default) | implicit, from the OS session → `@local:<os-user>@<host>` | populated, **unproven** | zero | solo / dev / single-purpose / air-gapped nodes — *today's free-wheeling* |
| **`attested`** | local **Ed25519** keypair, private key in the **OS keychain**, unlocked at session start | populated, **cryptographically proven** | one unlock/session | shared machines, a cross-project vault with real creds |
| **`sso`** | external IdP (OIDC, ADR-075) — `id_token` → principal | proven + org-federated | one sign-in/session | org-network, multi-user, institutional resources |
| **`service`** | workload/managed identity or service-account key | proven, non-interactive | none (provisioned) | unattended server/CI nodes |

The ladder is monotonic: `open < attested < sso`/`service`. A node's posture is
its **floor**; it may always step *up*.

### 2. `ctx.principal` is always present (never None)

The runtime populates `SkillContext.principal` on every skill invocation, in
*every* posture. In `open` it is the implicit OS-derived principal (unproven, and
labelled as such, `assured=False`); in `attested`/`sso` it is proven
(`assured=True`). This means: **nothing in the codebase has to special-case "no
identity"** — there is always a principal; the only question is its assurance.
KEEP binds capabilities to it; `authz` authorizes against it; receipts name it.

### 3. Three floors: node, resource, **and federation**

- **Node posture** — `identity.posture` in settings; default `open`; set by the
  deployment/env (an org-network deploy ships `sso`).
- **Resource/capability posture floor** — a secret or a capability scope MAY
  declare a `min_posture`. Even on an `open` node, an operation that touches a
  floored resource triggers **step-up** to that posture.
- **Federation policy** — a cohort/federation MAY stipulate, as a
  `FederationPolicy`, both a **minimum posture** *and* an **allowed set of IdPs**
  (ADR-027/028): an institutional cohort might require `sso` **via that institution's Entra tenant
  specifically**; a high-assurance cohort `attested`+hardware. A peer that is below the
  posture floor *or* authenticated via a non-allowed IdP can't join the cohort or
  act on its federated resources. (`PrincipalContext.idp` records which IdP a
  principal authenticated through; `FederationPolicy.admits(principal)` decides.)

The effective requirement for an operation =
`max(node_posture, resource_floor, federation_floor)`. A free-wheeling node stays
frictionless until it reaches for something — a credential, a cohort — that
demands proof.

### 3b. Users — a user *is* a principal

The model distinguishes three things it had been conflating:

- **Node** — the machine/install (carries the node posture).
- **User** — a principal (`@name:context`) that *acts*. `open` = the implicit OS
  user (`@<os-user>:local`, unproven); `attested`/`sso` = an explicit user with
  its own keypair / IdP identity, its own KEEP credentials (the token store
  already keys by user).
- **Session** — a user acting on a node; `ctx.principal` is the **acting user**.

A node may host **multiple users** (a shared server, a multi-tenant deploy). The
single-OS-user derivation is only the `open` default; a multi-user node resolves
*which* user is acting from the session/login (not the OS), and each user's
credentials + attestation are independent. Federation peers are users in other
contexts (`@name:other-cohort`).

### 4. Step-up: when the human principal is introduced (the thoughtful part)

Identity is introduced **only at moments of consequence**, never pre-emptively.
The canonical step-up moments — each raises the assurance just-in-time, then
persists for the session:

1. **First dereference of a shared/real credential** through KEEP (a shared HPC/API key,
   Box, an org resource) — "you're about to use the shared HPC/API key; authenticate."
2. **Crossing a trust boundary** — federation to a peer, or acting on an
   org-owned resource (the id_token is required to cross).
3. **A consequential outbound side-effect** — sending comms *as a person*,
   writing to a calendar others see, irreversible/destructive ops — so the audit
   receipt's actor is a real principal.
4. **Deploying to a shared / multi-user / networked node** — posture is set at
   deploy; the human is introduced once at onboarding.
5. **Elevation** — RACI graduation (propose→autonomous) or a classification rise
   (CUI/EC) requires an assured principal for the audit chain.

Principle: **progressive disclosure of identity** — zero-auth until identity
actually matters, then one graceful step-up (keychain unlock / SSO sign-in) that
persists. This is how mature tools behave (`gh` lets you work locally, prompts
auth only on push).

### 5. The keystone mechanics (`attested`)

- A local Ed25519 `Keypair` (`vega.identity.keypair`) for the principal; the
  **private key is custodied in the OS keychain** (macOS Keychain / Windows
  DPAPI / Linux Secret-Service, via the `setup.secrets` keychain path), never on
  disk in the clear. Unlock at session start gates on the OS (Touch ID / login).
- `issue_capability` signs the capability's canonical bytes with the
  authenticated principal's key and records the **real** `signature` +
  `public_bytes` (retiring the placeholders).
- `outbound`/validation **verifies** the signature (`vega.identity.keypair.verify`)
  against the issuer before dereferencing any secret; a bad/missing signature is
  rejected in `attested`+ (advisory-only in `open`).

### 5b. Principal custody is pluggable; the device is a second factor

The `attested` principal's key need not live "in a keystore." Custody is a
pluggable **`adapter`** so the same posture works across very different security
models:

| Custody backend | Where the key lives | 2FA |
|---|---|---|
| **`keychain`** (default) | OS keychain; unlock gated by the OS (Touch ID / login / DPAPI) | the OS biometric/password is the second factor |
| **`badge`** | **nowhere at rest** — the key is *derived on-demand* from the user's biometric (Badge's privacy-preserving model); no secret to store, sync, or breach | biometric *is* the factor; nothing stored |
| **`hardware`** | a hardware token (YubiKey/TPM/Secure Enclave); key never extractable | possession + touch |

**2FA / device-as-2FA is part of assurance, not a bolt-on.** A posture carries a
factor count: `attested(1fa)` (session unlock) vs `attested(2fa)` (a *fresh*
biometric/possession confirmation). A credential/secret MAY require
`require_mfa = true` — releasing it demands a fresh second-factor confirmation at
*use* time (not just session start), e.g. a Touch ID / Badge tap to release the
shared HPC/API key even within an already-unlocked session. For `sso`, MFA (e.g. an authenticator app) is the
IdP's (ADR-075); for the local principal + credential *release*, the **device is
the 2FA**.

### 5c. KEEP as the personal credential fabric ("any credential, any system, anytime")

The same machinery is, from the user's seat, a **personal cross-system credential
vault** — the credential analogue of `axi mem`: *put* any credential for any
system, *get* it (or have an agent use it) anytime, from any node, with the local
principal gating release and the raw secret never spreading to the tool. KEEP +
`secrets` is the store; the local principal (this ADR) is the lock; capabilities
are the brokered release. Badge custody makes the lock secret-less; a per-credential
`require_mfa` makes high-value releases (a shared HPC/API key) demand a fresh tap. This is
the de-parking of the Unified Credential & Secret Fabric — surfaced as `axi cred`.

### 6. AEOS integration (see the spec addendum in this set)

- `SkillContext.principal` (+ its `assured`/`posture`) is added to the AEOS
  runtime contract.
- A capability/secret's `min_posture` rides the `[[extension.consumes]]`
  credential declaration.
- Conformance gains: *"every skill runs under a populated `ctx.principal`"* and
  *"a posture floor is enforced before a floored credential is dereferenced."*

## Consequences

**Positive**
- **Nothing we built breaks** — `open` is the default and is exactly today's
  behavior, now named and explicit.
- **Flexibility by construction** — per-node *and* per-resource posture; one env
  runs wide-open, another demands SSO, and a sensitive resource demands proof
  even on an open node.
- **Thoughtful human-introduction** — step-up only at consequence; no auth wall
  in front of free-wheeling work.
- **Audit becomes meaningful** at `attested`+ (the receipt actor is proven).
- The placeholder is retired; KEEP becomes a real broker.

**Negative / risks**
- `open`-mode receipts name an *unproven* principal — must be clearly labelled
  `assured=False` so nobody mistakes them for attested provenance.
- Step-up UX must be genuinely one-step or it will be hated; the keychain unlock
  / SSO sign-in must persist for the session and be cached.
- A node mis-postured `open` when it should be `sso` is a downgrade risk —
  deployment config must set posture explicitly for shared/networked nodes;
  resource floors are the backstop.

## Alternatives considered

- **Always require authentication.** Rejected — kills the free-wheeling default
  that's been productive; wrong for solo/air-gapped nodes.
- **Binary auth (on/off).** Rejected — no per-resource granularity; can't let a
  mostly-open node still protect one real credential.
- **Authenticate lazily but globally on first call.** Rejected — too blunt; the
  per-resource floor + step-up moments are more precise and less annoying.

---

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
