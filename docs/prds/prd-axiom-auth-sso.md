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

## 11. Connector Identity Profiles — describing platform apps as a cohesive whole (ADR-110)

_Added 2026-09-14. Cross-cut with `connector` and `secrets`; the authoritative
decision is ADR-110._

A deployment holds several external app registrations at once — a service
identity, a staff sign-in app, a per-user automation app — and today an operator
sees them as an incoherent set with no shared vocabulary. They are in fact three
points in one space, `(auth-mode × tenancy × capability-scope)`. The platform
must name that space.

### 11.1 Requirement — one record per configured app

Every configured platform app is one `ConnectionProfile`: `connector` (vendor,
reverse-DNS), **`auth_mode ∈ {service, delegated_user, user_signin}`**, `tenancy
∈ {single, multi}`, `capability_scope`, `credential_ref` (into the secrets seam,
never inline), `owner`, `status`. `auth_mode` is first-class — the field that is
implicit today. `axi connector` lists and describes apps by these fields, so
"why do we have three Microsoft apps?" has a one-line answer per app.

### 11.2 Requirement — the IdP registry is real (landed)

§5.2's "IdP providers (registry, mirrors `secrets`)" is now true in code as of
ADR-110 Phase 0: `auth` uses `get_idp` / `register_idp` / `available_idps` like
the other connector families, and no code path defaults an unknown provider to a
specific vendor. Adding Okta, Cognito, or any OIDC issuer is one `register_idp`
call with no caller edits.

**The registry carries a generic entry, because open and empty is closed.** It
shipped two presets, Entra and Google, and the sentence above was true of the
mechanism and false of the experience: any other issuer meant a caller
importing `from_discovery` itself, which is the hand-rolled dispatch the
registry replaced. `oidc` is now a built-in entry taking an issuer URL, so an
institutional single sign-on, Okta, Cognito, Keycloak and a test issuer are all
selected the same way as the two presets. `register_idp` is unchanged and
remains how a provider that needs more than a URL arrives.

**Discovery keeps every endpoint `IdpConfig` can hold**, which until now
excluded `device_authorization_endpoint`. The device flow was therefore
unavailable to every issuer not covered by one of the two presets, and the
failure accused the issuer: "<name> has no device authorization endpoint", for
an issuer that published one. A field the loader drops is worse than a field
nobody wrote, because the error names the wrong system and the reader
investigates it. A guard now walks `IdpConfig`'s declared fields against a
discovery document, so the next field added to the dataclass fails this check
rather than going missing in production.

### 11.3 Requirement — cross-cloud parity

A `ConnectionProfile` names no cloud. The same three `auth_mode`s map onto every
backend: `service` = Entra app / Google service account / AWS IAM role;
`delegated_user` = Entra public client / Google OAuth client / AWS Cognito;
`user_signin` = any OIDC RP. Switching or adding a cloud is registering profiles,
not rewriting consumers.

### 11.4 Requirement — colleague onboarding for delegated automation

For a team automating their own M365/Google lives, one shared `delegated_user`
profile (multi-tenant, public-client) is registered once; each colleague signs
in **as themselves**, their refresh token caches per-user and per-machine, and
each acts only on their own data. No shared secret is ever distributed. The
higher-privilege delegated scopes are admin-consented once for the tenant; after
that, onboarding is a single per-person sign-in.

**For a node's own API keys, nobody mints somebody else's secret.** The sign-in
story above does not cover a scoped bearer key against a node, and the gap showed
up the first time three colleagues were onboarded at once (2026-10-01): an
administrator logged into the node, ran `gate issue api-key` three times, and
sent each person their key. Every key then existed in the administrator's
terminal, in their scrollback, and in whatever channel carried it. One reached a
chat paste and had to be rotated.

The administrator's decision is **who may have what**. The secret only has to
reach one machine, and it is not theirs. So:

- `gate invite --principal @name:site --role <role> [--expires 7d]` records the
  approved grant and prints a single-use code. It mints no key, and the command's
  output contains no key.
- `gate redeem <code>`, run by the holder, spends the invitation and mints the
  key on their machine. The plaintext exists once, there.
- An invitation expires. `--expires` is required to have a value and defaults to
  seven days, because a credential with no end is one nobody remembers to revoke,
  and an invitation is the easiest kind to forget since it does nothing until
  somebody uses it.
- **An invitation can only shrink.** The approved scopes are recorded at
  invitation time, resolved again from the role bundles at redemption, and a
  redemption that would grant more is refused rather than widened. A role
  narrowed in between narrows the key, which is the direction safe to apply
  without asking. This is what makes an outstanding invitation safe to leave
  outstanding.
- Refusals are specific — already redeemed, expired, revoked, unrecognised — not
  uniform. The code carries 32 bytes of entropy so there is nothing to enumerate,
  and a colleague who cannot tell those apart has to go and ask somebody, which is
  the dead end this surface exists to remove.
- `invite` lists outstanding and expired invitations separately, because an
  administrator needs to tell "still waiting on them" from "they missed it and
  need another".

`gate issue` remains the right verb for a service principal, where there is no
person to redeem anything.

- `POST /gate/redeem` serves redemption over HTTP, so a colleague redeems from
  their own machine. It is on the gate's public mount by necessity: the
  invitation is what authenticates the holder, and the people redeeming one are
  exactly the people with no account on the node yet. Developers here get no SSH
  and no database credentials by design (ADR-002), so an on-node-only redemption
  would be unusable by the people it is for.

**Still open.** There is no rate limit on the redeem route. The code carries 32
bytes of entropy, so guessing one is infeasible and the exposure is a spray
costing an attacker a request per attempt against a node that refuses every one.
Worth adding with the rest of the gate's throttling rather than invented here for
one route. The device flow (ADR-075, `from_discovery` plus the generic `oidc`
registry entry) is the other half of this, for a holder with no browser session.

### 11.4.1 Requirement — forward-auth answers for a key, not only a session

`GET /gate/verify` is the forward-auth endpoint nginx calls to ask "who is this".
It read the session cookie and nothing else, so a service behind the gate could
authenticate a person and not an API key. That gap had a live consequence: a chat
face with no way to verify an API caller trusted a client-settable identity
header.

The library path (`build_bearer_resolver` + `build_authz_hook`) already verified
keys, but it needs the axiom package in the calling process and that face runs in
an environment without it. Vendoring key parsing and scrypt a second time is the
worse of the two options, so the gate answers for both families.

**The header contract**, because a front door is built against it. Every header
is present even when empty: a missing header and an empty one read differently to
a caller, and the absent one is what becomes a `KeyError` at three in the morning.

| header | session | API key |
|---|---|---|
| `X-Axiom-Auth` | `session` | `api-key` |
| `X-Axiom-User-Id` | the subject claim | the principal handle `@name:site` |
| `X-Axiom-User-Email` | the email claim | empty |
| `X-Axiom-User-Name` | the name claim | empty |
| `X-Axiom-User-Roles` | the roles claim | empty |
| `X-Axiom-User-Site` | the site claim | the key's bound site |
| `X-Axiom-User-Scopes` | empty | the verified scopes, comma-separated |
| `X-Axiom-Key-Id` | empty | the key id |

Three of those emptinesses are decisions rather than gaps:

- **Email** and **Name** are empty for a key rather than filled with something
  plausible. A principal handle is not an address, and a key's label says what the
  key is for rather than who holds it. Either rendered in the wrong field is a
  small lie something downstream will act on.
- **Roles** is empty because a role is resolved through the bundles *when the key
  is issued* and only the resulting scopes are stored. The node cannot say
  afterwards which role a key came from, and a guess would be worse than nothing.
  Callers gate on `Scopes`.

**A presented bearer never falls back to the cookie.** An invalid or revoked key
is 401 even when a valid session cookie rides along. The alternative is a caller
whose key was revoked continuing to work through a cookie they also hold, and
nobody looking at the revoked key would understand why. An `Authorization` header
of another scheme counts as presented: treating it as absent would fall through.

**Cheap by construction**, since a front door calls this on every request. The key
store is built once per router and caches its parse by mtime and its scrypt
verification per key. It still re-reads on mtime change, so revocation remains
immediate with no restart — both halves are asserted, because holding the store
for speed is exactly how stale records get served.

### 11.5 Requirement — mapping data is deployment config, not code

Vendor→IdP maps, default scopes, endpoints, and "which connector answers for
this vendor" are **deployment configuration**, shipped as editable config files
with data-file defaults, never as Python literals. An operator changes a
mapping, adds a vendor, or repoints an IdP by editing config in a **live
deployment — no code patch, no restart** (the config system's file-watcher
applies it). This is what makes the apparatus flexible: a new institution's
IdP, or a sovereign-cloud endpoint, is a config edit, not a release. _Landed
first for the calendar vendor map (ADR-110 §Decision-6)._

---

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
