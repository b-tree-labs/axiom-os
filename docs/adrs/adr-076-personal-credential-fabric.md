# ADR-076: Personal Credential & Secret Fabric (`axi cred`)

**Status:** Proposed (2026-06-11)
**Deciders:** Benjamin Booth
**Supersedes/absorbs:** the parked "Unified Credential & Secret Fabric" (epic
axiom-os#463, doc PR #462).
**Related:** ADR-077 (Progressive Trust — the lock), ADR-055 (KEEP / governance
fabric — the capability machinery), ADR-075 (SSO — delegated tokens are fabric
tenants), `prd-axiom-vault`, the secrets extension (OpenBao).

---

## Context

The same friction recurs: a credential (an HPC key, Box, Postgres, Slack, an org
resource, an OIDC refresh token) is needed from many places, and today it lives
wherever each call site stashed it — env vars, per-provider OAuth caches, loose
files. There is no single, audited, user-scoped store with a real lock. The
"unified credential fabric" was scoped (#463) but parked. With ADR-077 the lock
now exists (an authenticated local principal), and `axi cred` (CRED-1) is built —
so the fabric is promoted from a parked epic to a decision of record.

## Decision

**KEEP + the secrets vault + the local principal together *are* the personal
credential & secret fabric.** `axi cred` is its surface — the credential analogue
of `axi mem`.

1. **Any credential, any system, anytime.** `axi cred put/get/list/rm` (+ an
   `issue` for agent use). Store any secret for any system under a name; retrieve
   or use it from any node, anytime.
2. **The principal is the lock (ADR-077).** Release is gated by the credential's
   **posture floor** and optional **`require_mfa`** fresh tap. `list` shows names
   + floors, never values.
3. **Capabilities broker *use* — the raw key never reaches the tool.** For agent
   use, `issue` mints a KEEP capability; the actual call goes through KEEP's
   outbound (the one plaintext site), audited under the authenticated principal.
   Direct `get` (the owner retrieving their own key) is allowed, posture-gated.
4. **Custody is pluggable (ADR-077 §5b).** `keychain` default; `hardware`;
   **`badge`** (key/credential released without a stored secret — the secret-less
   tier). So the fabric can be *no-secret-at-rest*, end to end.
5. **Cross-project / cross-machine.** The store is user-scoped and follows the
   user (vault-backed; OpenBao for multi-machine). With Badge custody, nothing
   secret syncs at all (FED-3).
6. **Two no-secret properties stack:** no secret at rest (Badge) × no secret in
   transit to tools (KEEP capability brokering) — the market differentiator vs
   1Password / OS keychains / cloud secret managers.

## Consequences

- One audited, posture-gated store replaces scattered credential stashing; the
  "re-hit the shared HPC/API key" pain is solved once.
- The fabric is an AEOS-standard surface: an extension `consumes` a secret/
  credential (spec-aeos-identity-addendum) and the runtime resolves it through
  the fabric — never raw keys in extension code.
- Risk: a single store is a single high-value target — mitigated by the principal
  lock + posture/MFA floors + capability brokering + (with Badge) no secret at
  rest. The `list`-hides-values rule + log-lint keep values from leaking.
- #463 is superseded; this ADR is the reference.

## Alternatives considered

- **External secret manager only** (Vault/cloud) — rejected as the *primary* UX:
  they authenticate via cloud IAM, not a local user principal, and don't broker
  capability-bound use to local tools. We integrate them as custody/storage
  backends, not as the fabric.
- **Per-tool credential handling** (status quo) — rejected: the scattering is the
  problem.

---

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
