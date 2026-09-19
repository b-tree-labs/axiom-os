# ADR-075: SSO / OIDC Delegated Authentication

**Status:** Proposed (2026-06-11)
**Deciders:** Benjamin Booth
**Related:** ADR-077 (Local Principal Auth & Progressive Trust — the `sso`
posture), ADR-041 (Identity Acquisition & Verification), ADR-055 (Governance
Fabric), `prd-axiom-auth-sso` (the PRD), `spec-aeos` addendum (this set).

---

## Context

To deploy on **an organization network**, people must authenticate with their
**institutional SSO** (an institution's IdP, e.g. **Microsoft Entra ID + MFA**). Today only app-only
service-account auth exists; OAuth is reinvented per connector
(`publishing/providers/box.py`, `onedrive_graph.py`). A central service account
reading everyone's calendars under admin consent is the wrong model when each
user should act as **themselves** with their **own login**. ADR-077 defines the
`sso` posture; this ADR defines how that posture is *established* and how
connectors *consume* the resulting delegated tokens.

Key leverage: when an institution's IdP **is** Entra, which **is** the M365 backend — so one OIDC
sign-in yields both the SSO identity (`id_token`) and delegated resource access
(`access_token`). AEOS already wraps MCP, whose auth spec is **OAuth 2.1 + PKCE**
— so our public subset is MCP-interoperable; the AEOS delta is the KEEP brokering.

## Decision

1. **A new `auth` AEOS built-in extension** (authentication; distinct from
   `authz`). Authorization-Code + **PKCE (S256)** is the only interactive flow;
   **device-code** for headless hosts. Implicit flow is forbidden.

2. **IdP providers are `adapter` kind** in a registry, mirroring `secrets`:
   `entra` (tenant-scoped — e.g. an institution's IdP), `google`, and `generic` (OIDC discovery
   via `.well-known/openid-configuration`). Endpoints come from the provider or
   discovery.

3. **Three auth modes, all behind one `token_source` seam:**
   - **delegated** (user OAuth/SSO) — establishes the `sso` posture; per-user
     consent.
   - **app-only** (service account / client-credentials) — the `service` posture;
     central unattended scheduler.
   - **device-code** — delegated on browserless hosts.
   Connectors accept a `token_source` callable (already wired into the calendar
   providers); they never hold OAuth logic.

4. **Token custody:** refresh tokens are stored **only** via the `secrets` vault
   (OpenBao default), keyed by `(provider, user, scopes)`. Access tokens stay
   in-memory, refreshed silently within an expiry skew. **Nothing token-bearing
   is logged** (lint-enforced). MFA (e.g. an authenticator app) is delegated entirely to the IdP.

5. **`id_token` → platform principal.** The verified `id_token` (signature checked
   against the IdP `jwks_uri`; `iss`/`aud`/`exp`/`nbf` with leeway) yields the
   `@name:context` principal that ADR-077 places on `ctx.principal` at the `sso`
   posture. Authentication (this) stays distinct from authorization (`authz`).

6. **Manifest-driven onboarding (AEOS):** an extension declares a
   `[[extension.consumes]]` credential need (IdP, scopes, `min_posture`); the
   connector wizard / runtime satisfies it by running the sign-in, storing the
   refresh token, and injecting the `token_source`. The wizard's job becomes
   "satisfy the manifest's credential declarations."

7. **SAML fallback** as a second IdP `adapter` behind the same `token_source`
   seam for any legacy SAML IdP service; OIDC is primary.

## Consequences

- One sign-in serves SSO identity *and* delegated calendar/file access (Entra).
- OAuth is centralized once; connectors are auth-agnostic (proven: the calendar
  adapters already take `token_source`).
- Public-standard subset (OAuth2.1+PKCE) is harness-interoperable; the KEEP
  delegated-token brokering is the AEOS delta.
- Refresh-token custody risk → vault-only + log lint + short-lived access tokens.
- IdP quirks (Entra v2.0 issuer, Google scope shapes) → discovery-driven +
  per-provider tests + JWKS verification.

## Status of implementation (refreshed 2026-08-26)

**Shipped** (`auth` builtin, ADR-075's RP half):

- Phase 1 — `pkce.py` (S256), `providers.py` (`entra(tenant)`, `google()`,
  `from_discovery(issuer)`), `flow.py` (authorization URL, code exchange, refresh,
  state + nonce), `token_source.py` (the three modes: delegated / app-only /
  device-code), `token_store.py` (keychain-backed, keyed `(provider, user, scopes)`).
- Phase 2 — `jwt_verify.py` (`id_token` JWKS signature + iss/aud/exp/nbf, AUTH-R7),
  `device_flow.py` + `login.py` (device-code sign-in, AUTH-R9), and the
  `axi auth {providers,login,whoami,logout}` CLI.
- The **browser sign-in** this ADR anticipated lives in the `webgate` extension:
  `webgate/oidc.py` runs the auth-code + PKCE flow against the configured IdP,
  verifies the `id_token` with `jwt_verify`, keys the account on the immutable
  subject (Entra `oid`), and ends in the same session cookie as the password
  form — one login for the gate, the OAuth AS (ADR-082) and the forward-auth
  headers. Configured by `AXIOM_GATE_OIDC_*` (PR #701).

**Remaining** per `prd-axiom-auth-sso`:

- AUTH-2 — an interactive loopback-redirect login for the *CLI* (`axi auth login`
  is device-code only today).
- AUTH-4 — vault-keyed refresh-token custody through `secrets` (the keychain
  store predates the vault seam).
- Phase 3 connector-wizard integration and the calendar/storage cutover onto
  `token_source`; Phase 4 SAML.

---

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
