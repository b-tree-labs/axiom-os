# PRD: `axiom.auth` — SSO & Delegated Auth (OIDC / OAuth2)

**Status:** Draft (2026-06-11)
**Owner:** Benjamin Booth
**Primitive class:** AEOS built-in extension (`axiom.extensions.builtins.auth`)
**Related:** `secrets` (token storage), `connector` (consumes tokens), `authz`
(authorization — distinct from authentication), the calendar providers (first
consumer), the "unified credential & secret fabric" direction.

---

## 1. Elevator Pitch

One sign-in with your organization's identity provider — "**Sign in with your
institutional account**", Google, Okta, any OIDC IdP — gives Axiom both a **platform identity**
(who you are) and **delegated tokens** (act on your behalf) for every connector.
Axiom runs the OAuth2 / OIDC Authorization-Code + PKCE flow once, stores the
refresh token in the secrets vault, and hands every connector a `token_source`
that always yields a fresh access token. Users authenticate as **themselves**
with their own institutional credentials; MFA (e.g. an authenticator app) is enforced by the IdP,
not reimplemented here.

## 2. Problem / Opportunity

- **OAuth is reinvented per connector.** `publishing/providers/box.py` and
  `onedrive_graph.py` each hand-roll token handling; the calendar providers would
  too. No shared flow, no shared refresh, no shared storage — bugs and drift.
- **No SSO.** To deploy on **an organization network**, people must authenticate with
  their **institutional SSO** (an institution's IdP, e.g. Microsoft Entra ID + MFA). Today there is no
  "sign in with your org" path; only app-only service-account auth exists.
- **App-only doesn't fit "folks use their own creds."** A central service account
  reads everyone's calendars under admin consent — wrong model when each user
  should grant access to *their own* resources with *their own* login.
- **Tokens aren't brokered.** Refresh tokens live wherever each provider stashes
  them; there's no single audited, vault-backed broker (the credential-fabric
  goal).
- **Identity and authorization are conflated.** `authz` answers "may this
  principal do X"; nothing establishes "who is this principal" via an external
  IdP. SSO closes that gap and feeds the principal to `authz`.

### Why now

An org-network deployment is imminent and **non-negotiably requires institutional
SSO**. The calendar connectors just made the need concrete (delegated calendar
access), and their provider seam already accepts a `token_source` — so the IdP
component is the missing half.

## 3. Goals & Success Metrics

**Primary goal:** A user signs in once with their IdP; Axiom obtains and refreshes
delegated tokens, stores the refresh token in the vault, exposes a `token_source`
per (user, provider, scope), and resolves the user's platform principal from the
`id_token` — with PKCE, state/nonce CSRF protection, and MFA delegated to the IdP.

| Metric | Target |
|---|---|
| Auth-code + PKCE flow against a conformant OIDC IdP | 100% in the fake-IdP suite |
| Access-token auto-refresh before expiry (no failed calls at the boundary) | 100% |
| Refresh token never written outside the secrets vault / never logged | 100% (lint + test) |
| IdP onboarding (Entra / Google / generic discovery) | `.well-known/openid-configuration` driven |
| MFA enforced by IdP, never handled in-process | by construction |
| `id_token` → platform principal (`@name:context`) | 100% |

## 4. Key Users / Personas

| Persona | Task | Pain today |
|---|---|---|
| **End user (org network)** | Sign in with my institutional account; connect my calendar/files | No SSO; can't use my own creds |
| **Operator** | Configure the org IdP once (issuer, client id) | Per-connector OAuth config |
| **Connector developer** | Consume a fresh token without touching OAuth | Hand-roll exchange + refresh |
| **Security officer** | Audit token lifecycle; revoke | Tokens scattered, unaudited |

## 5. Scope — Key Capabilities

### 5.1 The auth API

```python
# axiom.extensions.builtins.auth

def login(provider: str, *, scopes: list[str], user_hint: str | None = None) -> Session:
    """Run Authorization-Code + PKCE against the IdP; return a Session with the
    platform principal and a stored refresh token."""

def token_source(provider: str, *, user: str, scopes: list[str]) -> Callable[[], str]:
    """A callable returning a always-fresh access token (refreshing as needed).
    This is what connectors pass to a provider's config."""

def whoami(provider: str, *, user: str) -> Principal: ...
def logout(provider: str, *, user: str) -> None: ...   # revoke + drop the refresh token
```

### 5.2 IdP providers (registry, mirrors `secrets`)

`entra` (tenant-scoped), `google`, `okta`, and `generic` (any OIDC issuer via
`.well-known/openid-configuration` discovery). Each declares its
`authorization_endpoint`, `token_endpoint`, `jwks_uri`, and default scopes.

### 5.3 Flows

- **Authorization-Code + PKCE** (default; web + desktop with a loopback redirect).
- **Device-code** for headless / CLI / a server with no browser.
- **Refresh** — silent, before expiry, via the stored refresh token.

### 5.4 Token storage & the `token_source` seam

Refresh tokens are stored **only** via the `secrets` vault (OpenBao default),
keyed by `(provider, user, scope-set)`. `token_source()` returns a callable that
caches the access token and refreshes from the vault-held refresh token on
expiry. Calendar/storage providers already accept this callable.

### 5.5 Identity → principal

The verified `id_token` (signature checked against the IdP `jwks_uri`) yields the
platform principal (`sub`/`email`/`preferred_username` → `@name:context`), which
`authz` then authorizes. Authentication (this) and authorization (`authz`) stay
separate.

### 5.6 CLI surface (ADR-056)

```bash
axi auth login <provider> --scopes "<...>"     # run the flow, store the refresh token
axi auth whoami <provider>                      # show the resolved principal
axi auth logout <provider>                      # revoke + forget
axi auth providers                              # list configured IdPs + status
```

## 6. Non-Functional / Constraints

- **PKCE mandatory** (S256); never the implicit flow.
- **CSRF**: `state` + `nonce` validated on every flow.
- **Secrets discipline**: refresh tokens only in the vault; access tokens
  in-memory; **nothing token-bearing is logged** (lint guard).
- **MFA**: delegated to the IdP entirely (e.g. an authenticator app).
- **Cross-platform** loopback-redirect listener (macOS/Linux/Windows).
- **SAML fallback**: a SAML IdP adapter behind the same `token_source` seam for
  any legacy SAML IdP service; OIDC is the primary path.
- **Clock/skew**: `id_token` `exp`/`nbf` validated with leeway.

## 7. Timeline

| Phase | Scope |
|---|---|
| 1 | PKCE + auth-code flow + token exchange/refresh + `token_source`; Entra + Google + generic-discovery providers; vault storage; `id_token` claims |
| 2 | `id_token` JWKS signature verification; device-code flow; `axi auth` CLI |
| 3 | Connector-wizard integration ("sign in" front door); calendar/storage cutover to `token_source` |
| 4 | SAML fallback adapter; revocation + audit surface |

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Loopback redirect blocked on locked-down hosts | device-code flow fallback |
| Refresh-token leakage | vault-only storage + log lint + short-lived access tokens |
| IdP quirks (Entra `v2.0` issuer, Google scopes) | discovery-driven + per-provider tests |
| Token-source thundering refresh | single-flight refresh + cache with skew |

**Open questions:** per-user vs shared service principal for unattended schedule
firing (likely: delegated for user-owned resources, app-only for the central
scheduler — both supported); refresh-token rotation policy; multi-IdP per user.

## 9. Behavioral Requirements (normative)

Testable guarantees; **[1]** ships in Phase 1. Each converts to user docs.

- **AUTH-R1 [1].** Login MUST use Authorization-Code with **PKCE (S256)**; the
  `code_verifier` never leaves the process and the `code_challenge` is derived by
  SHA-256.
- **AUTH-R2 [1].** Every flow MUST generate and validate `state` (CSRF) and
  `nonce` (replay); a mismatch aborts the login.
- **AUTH-R3 [1].** Token exchange MUST yield access + refresh + id tokens; the
  **refresh token is persisted only through the `secrets` vault**, keyed by
  `(provider, user, scopes)` — never to disk or logs.
- **AUTH-R4 [1].** `token_source(provider, user, scopes)` MUST return a callable
  that yields a **non-expired** access token, refreshing silently from the stored
  refresh token when within the expiry skew.
- **AUTH-R5 [1].** IdP endpoints MUST be resolvable from a provider config or
  OIDC discovery (`.well-known/openid-configuration`); Entra is tenant-scoped,
  Google + generic are issuer-scoped.
- **AUTH-R6 [1].** The platform principal MUST be derived from `id_token` claims
  (`sub` + `email`/`preferred_username`); authentication is distinct from `authz`.
- **AUTH-R7 [2].** The `id_token` signature MUST be verified against the IdP's
  `jwks_uri`, with `iss`/`aud`/`exp`/`nbf` checked (leeway for skew).
- **AUTH-R8 [1].** MFA MUST be delegated to the IdP — the flow never collects a
  second factor in-process.
- **AUTH-R9 [2].** A **device-code** flow MUST be available for headless hosts
  with no browser/loopback.
- **AUTH-R10 [1].** Connectors MUST be able to authenticate by **delegated token
  source** (this) *or* app-only (service account / client-credentials); the
  calendar providers already accept both.

## 10. How It Works — Worked Examples

```bash
# A user on an org network signs in with their institutional IdP (e.g. Entra).
axi auth login entra --scopes "https://graph.microsoft.com/Calendars.ReadWrite offline_access"
#  → opens the IdP login (MFA happens there), stores the refresh token in the vault
axi auth whoami entra        # @user:example
```

```python
from axiom.extensions.builtins import auth
from axiom.extensions.builtins.schedule.calendar import get_provider

# Hand a connector a fresh-token callable — no OAuth code in the connector.
ts = auth.token_source("entra", user="user@example.org",
                       scopes=["https://graph.microsoft.com/Calendars.ReadWrite"])
cal = get_provider("m365", {"user_id": "user@example.org", "token_source": ts})
cal.list_events(start=..., end=...)   # acts as the user, via their SSO session
```

---

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
