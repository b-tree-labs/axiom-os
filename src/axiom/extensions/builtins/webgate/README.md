<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# `webgate` — a UI-agnostic forward-auth gate

Authenticate a browser user once, in Axiom, and let **any** UI (Open WebUI,
LibreChat, a future app) sit behind it. An edge proxy subrequests the gate for
every request; the gate allows or denies and hands the proxy a verified identity
to forward upstream. Auth lives in the platform, not in the UI, so swapping the
UI never touches authentication. See **ADR-003** (in `ut-triga-site`).

Distinct from its neighbours: `webauth` is the primitives (scrypt passwords,
ES256 sessions, the user store); `oauth` is the OAuth 2.1 AS / OIDC provider;
`webgate` is the thin browser-session gate that ties them to an edge proxy.

## Routes (mounted at `/gate`, public)

| Route | Purpose |
|---|---|
| `GET /gate/login` | Password form (honours `?next=` / `?return_to=`). |
| `POST /gate/login` | Authenticate → set the ES256 session cookie → 303 to `next` (local paths only — open-redirect safe). |
| `GET /gate/verify` | The forward-auth check: **200** + `X-Axiom-User-{Id,Email,Name,Roles}` when a valid session is present, else **401**. |
| `POST /gate/logout` | Clear the session. |

## Wiring

1. **Accounts** — provision the `webauth` user store (`set_user_store(...)`;
   config/DB provisioning is a follow-up).
2. **Edge proxy** — put a proxy in front of the UI that forward-auths to
   `/gate/verify`, copies the `X-Axiom-User-*` headers upstream on 200, and
   redirects to `/gate/login?next=<original>` on 401. Caddy sketch:

   ```
   handle {
     forward_auth localhost:8768 {
       uri /gate/verify
       copy_headers X-Axiom-User-Id X-Axiom-User-Email X-Axiom-User-Name X-Axiom-User-Roles
       @denied status 401
       handle_response @denied {
         redir https://{host}/gate/login?next={uri}
       }
     }
     reverse_proxy localhost:3001   # the UI (Open WebUI / LibreChat)
   }
   ```

3. **UI trust** — point the UI at the injected identity (Open WebUI
   `WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Axiom-User-Email`). Swapping OWUI→LibreChat
   only changes this one config.
4. **HTTPS** — session cookies are `Secure` by default (terminate TLS at the
   proxy). For a local http bring-up only, `AXIOM_GATE_INSECURE_COOKIE=1`.

## SSO — "Sign in with <your institution>" (`webgate.oidc`)

The gate can federate login to an upstream OIDC IdP (Entra, Google, or any
issuer with discovery) on the `auth` extension's PKCE + JWKS building blocks.
It ends in the **same** session cookie as the password form, so `/gate/verify`,
the `X-Axiom-User-*` headers, and the OAuth bridge below need no changes.
Configuration is environment-only; `build_webgate_router()` picks it up:

```sh
AXIOM_GATE_OIDC_PROVIDER=entra                 # entra | google | https://<issuer>
AXIOM_GATE_OIDC_TENANT=<tenant guid>           # entra only
AXIOM_GATE_OIDC_CLIENT_ID=<app (client) id>
AXIOM_GATE_OIDC_CLIENT_SECRET_FILE=~/.config/axiom/gate-oidc.secret   # mode 0600; omit for a public client
AXIOM_GATE_OIDC_LABEL="Sign in with your institution"    # button text
AXIOM_GATE_OIDC_JIT=1                          # create accounts on first sign-in (0 = pre-provisioned only)
AXIOM_GATE_OIDC_SUBJECT_CLAIM=oid              # default: oid for entra, sub otherwise
AXIOM_GATE_OIDC_ROLES_CLAIM=roles
```

Register `https://<gate host>/gate/oidc/callback` as the IdP redirect URI.

### Several providers at once (Portkey-style)

`AXIOM_GATE_OIDC_PROVIDERS` names additional buttons next to the primary one;
each name reads its own `AXIOM_GATE_OIDC_<NAME>_*` block (same keys), routes at
`/gate/oidc/<name>/login|callback`, and defaults its label to
"Continue with <Name>":

```sh
AXIOM_GATE_OIDC_PROVIDERS=google,microsoft
AXIOM_GATE_OIDC_GOOGLE_PROVIDER=google
AXIOM_GATE_OIDC_GOOGLE_CLIENT_ID=<google oauth client id>
AXIOM_GATE_OIDC_GOOGLE_CLIENT_SECRET_FILE=~/.config/axiom/gate-google.secret
AXIOM_GATE_OIDC_MICROSOFT_PROVIDER=entra
AXIOM_GATE_OIDC_MICROSOFT_TENANT=common
AXIOM_GATE_OIDC_MICROSOFT_CLIENT_ID=<entra app id>
AXIOM_GATE_OIDC_MICROSOFT_CLIENT_SECRET_FILE=~/.config/axiom/gate-ms.secret
```

Register each provider's redirect URI (`…/gate/oidc/google/callback`, …). The
login card shows one button per provider (Google / Microsoft marks included),
then the password form. Under the form the gate says how an account comes to
exist: `LoginBrand.login_hint` wins; else `signup_url` renders "Create an
account"; else a JIT provider yields "First time here? Use <label> and your
account is created automatically."; else "ask your administrator".


Routes added when configured: `GET /gate/oidc/login?next=` (302 to the IdP,
signed transaction cookie) and `GET /gate/oidc/callback` (code → tokens →
verified `id_token` → account → session). The login page grows the button; the
password form stays as the fallback.

Accounts are keyed on the IdP's **immutable subject** (Entra `oid`), so a
rename never orphans anyone. A pre-existing password account with the same
email is *linked* (id and password kept, binding recorded in
`attributes.idp`), never duplicated. Roles follow the IdP's `roles` claim when
present (Entra app roles assigned by group); otherwise local roles are kept. An
SSO-only account has `password_hash: null` in the accounts file and can never
pass the password form. Refusals — state/nonce mismatch, bad signature, wrong
audience, disabled account, JIT off — all return the login page with a short
message; the detail goes to the log only.

### Popular issuers — recipes

Any OIDC IdP with a discovery document works via
`AXIOM_GATE_OIDC_<NAME>_PROVIDER=https://<issuer>`; these well-known ones are
recognized automatically (glyph, label, subject claim):

| IdP | `PROVIDER=` |
|---|---|
| AWS Cognito | `https://cognito-idp.<region>.amazonaws.com/<user-pool-id>` |
| Cloudflare Access | `https://<team>.cloudflareaccess.com` |
| Okta | `https://<org>.okta.com/oauth2/default` |
| Auth0 | `https://<tenant>.<region>.auth0.com` |
| Keycloak | `https://<host>/realms/<realm>` |
| GitLab | `https://gitlab.com` (or a self-hosted `gitlab.<domain>`) |
| Google Workspace | `google` (shortcut) or `https://accounts.google.com` |
| Microsoft Entra | `entra` + `TENANT=` (shortcut) |

GitHub is OAuth2-only (no OIDC discovery / id_token) and is not supported by
this surface.

## The OAuth bridge (already seamed)

`webgate.bridge.session_subject_resolver` turns the session cookie into the
`SubjectResolver` the `oauth` AS expects, so an already-logged-in browser passes
straight through `/oauth/authorize` — one login for cookie-session and OIDC:

```python
from axiom.extensions.builtins.oauth import set_subject_resolver
from axiom.extensions.builtins.webgate.bridge import session_subject_resolver
set_subject_resolver(session_subject_resolver)
```

Proven in `tests/test_oidc_bridge.py`: one `POST /gate/login` then
`GET /oauth/authorize` issues an authorization code with no second login.

## Deferred

Postgres-backed user store + an `axi` provisioning verb; CSRF token on the login
POST (SameSite=Lax covers the common case); API-client 401-vs-redirect
negotiation on `/gate/verify` (v1 returns a clean 401 and lets the proxy redirect).
