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

## The OIDC fast-follow (already seamed)

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
