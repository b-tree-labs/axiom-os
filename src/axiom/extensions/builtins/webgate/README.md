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
| `GET/POST /gate/keys` | A signed-in person mints their own key (see `self_key_route`). |
| `GET /gate/cli/authorize` | The command line's browser sign-in: sign-in buttons and an invitation box when signed out; one "Connect" click when signed in. |
| `POST /gate/cli/approve`, `POST /gate/cli/invitation` | Decide the key (the Keys page's own rules), store a one-time code, 303 to the listener on `127.0.0.1`. |
| `POST /gate/cli/token` | The CLI trades the one-time code and its PKCE verifier for the key, once (`no-store`). |

### The command line's browser sign-in (`/gate/cli/*`)

The loopback pattern (RFC 8252; `gh auth login`, `gcloud auth login`): `axi setup`
opens the authorize page with a `state`, an S256 `code_challenge` and a
`redirect_uri` on this computer. The rules, all in `cli_signin.py`:

- **Return address:** `http://127.0.0.1:<port>/callback` or `http://[::1]:<port>/callback`
  only, port 1024-65535, no query, fragment or user name. Not `localhost`. Anything
  else is refused with a page (400), never redirected.
- **The code:** 32 random bytes, single use (removed when presented, valid or
  not), two minutes, bound to the exact return address and the PKCE challenge.
- **At rest:** `cli-sign-ins.json` beside the keys file (0600, locked, so two
  workers or two blue/green slots share it) holds only a hash of each code. A
  signed-in grant keeps the decision (principal, roles, site); an invitation grant
  keeps the invitation sealed (Fernet) under the one-time code, which the node never
  stores.
- **The key** is minted at the exchange, so an abandoned sign-in leaves no key and
  spends no invitation. Scopes come from the session's roles, never the request.
- **Refusals** go back to the listener as `error=access_denied` with the node's
  sentence, so the reason is printed in the terminal.

No identity-provider change is needed: the IdP still redirects to the node's own
`/gate/oidc/callback`; the loopback leg is node to laptop.

### Access recourse — don't hand a turned-away viewer a dead end

A signed-in person with no role (or a role the site has not defined) cannot mint
a key, and the gate used to tell them to "ask whoever runs this site" — true, and
nothing they can act on. Set **`AXIOM_BRAND_SUPPORT`** (an email or an `http(s)`
URL) and the `/gate/keys` page renders it as an actionable link ("Request
access"), and the JSON path carries it too. Unset, the page keeps the honest
text with no empty link — the setting is the recourse, not a new wall. The value
is sanitised by `web_brand.support_href`: an email becomes `mailto:`, an http(s)
URL passes through, anything else (including `javascript:`/`data:`) renders no
link. It resolves from the environment per request, like the other
`AXIOM_BRAND_*` settings (`web_brand.WebBrand`).

### Three roles for the people who arrive (ADR-222)

| Role | Reaches | Scopes |
|---|---|---|
| `reader` | data, documents, chat | `*:read`, `gateway:invoke`, `chat:invoke` |
| `contributor` | a reader's, plus kit submissions and document uploads | reader's, plus `data_platform:invoke`, `library:invoke` |
| `operator` | a contributor's, plus governance (the kit review queue, and every declared governing route) | `*:read`, `*:invoke`, `*:govern` (ADR-151) |

A first SSO sign-in with no roles claim lands on `reader` (`AXIOM_GATE_JIT_ROLE`).
ADR-151's `owner`, `admin`, `creator`, `viewer` and `guest` stay as they were.

**The release check.** `python -m axiom.extensions.builtins.webgate.role_check` mints
a real self-service key for each role through `POST /gate/keys` and presents it to the
composed app's real authz middleware (handlers stubbed, so nothing is read or written),
then checks the matrix above. The publish workflow runs it. A job that stands up a node
calls the same module against it, one role at a time:

```sh
# sign in as an account holding the role; mints a self-service key on that node
python -m axiom.extensions.builtins.webgate.role_check --role contributor \
    --node http://127.0.0.1:8770 --email someone@example.org --password-env ROLE_PASSWORD
# or present a key already minted for the role
python -m axiom.extensions.builtins.webgate.role_check --role reader \
    --node http://127.0.0.1:8770 --key-env ROLE_KEY
```

Exit 0 means the role reached exactly what it should; `--json` prints the table. Every
probe a role may reach is one the node refuses before changing anything (an unknown verb
on the submit and review paths, an empty chat), and a door the node does not serve is
reported as `not served`, not as a pass. The key and the password are never printed.

### Every self-service key carries a site

A key's site is what a data kit's tenant, a site-held read and every per-site
check reads, so `/gate/keys` never mints one without it. The default is the
node's home site, **`AXIOM_SITE`**; with no home site set, no key is minted and
the page says why. A role may list more sites a holder can mint a key for (a
guest site this node hosts) in the roles file (`AXIOM_GATE_ROLES_FILE`):

```toml
[role.contributor]
scopes = ["*:read", "data_platform:invoke"]
sites = ["guest-loop"]        # besides this node's home site
```

`sites` widens no scope: the key holds the same scopes, held to the chosen
site's rows. The page offers exactly the home site plus the sites the account's
roles list (a picker when there is more than one), the API takes `{"site": ...}`
in the body, and any other site is refused (403, `site_not_allowed`). The
principal is the person bound to that site (`@name:<site>`).

A key minted before this with no site keeps working. The gate logs one warning
per such key per process, and `gate list api-keys` shows `NO SITE` for it and
says to mint a new one and revoke the old.

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
