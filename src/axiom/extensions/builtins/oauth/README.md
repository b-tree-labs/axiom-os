<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# `oauth` — first-party OAuth 2.1 AS + OIDC + MCP Resource Server

Axiom issues its own audience-bound tokens (ES256, from `axiom.webauth`) so a web
app, a mobile app, and agents / MCP clients all authenticate on one standards
surface. **GUARD** (`axiom.authz`) stays the authorization decision point — this
extension is the *credential wire*, not the policy engine. See **ADR-082**.

This is distinct from the `auth` extension (the relying-party that federates
*external* IdPs upstream, ADR-075). Naming: `auth` = log in *via* someone else;
`oauth` = Axiom *issues* the tokens.

## What ships so far

Two public mounts (`requires_authz = false`). Both must be reachable before a
client holds a token, and the token endpoint authenticates the *client* from the
request itself — not via GUARD.

**Discovery + JWKS** (`/.well-known`) — the front door every client fetches first:

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/jwks.json` | Public ES256 keys (RFC 7517) — verify tokens with no shared secret |
| `GET /.well-known/oauth-authorization-server` | AS metadata (RFC 8414) |
| `GET /.well-known/openid-configuration` | OIDC discovery |

**Authorize + token endpoints** (`/oauth`):

| Endpoint | Grant / role | Notes |
|---|---|---|
| `GET /oauth/authorize` | Authorization Code, code issuance | Validates the request, authenticates the resource owner (an injected/`set_subject_resolver` resolver — the login page is a webapp concern), issues a **PKCE-S256-bound**, single-use code and 302s it back. Untrusted `redirect_uri` errors render in place (no open-redirect); the rest bounce to `redirect_uri` with `error` + `state`. |
| `POST /oauth/token` `authorization_code` | code redemption | Exchanges the code for a token, gated by PKCE + exact `client_id`/`redirect_uri` binding. Public clients (SPA/mobile, `auth_method=none`) present only `client_id`; confidential clients authenticate. Issues a refresh token when `offline_access` was granted. |
| `POST /oauth/token` `refresh_token` | rotation | Rotates the refresh token (RFC 6749 §6): new access + new refresh in the same family, scope may narrow but never widen. **Reuse detection** — replaying a rotated-away token revokes the whole family (OAuth 2.1 stolen-token defence). |
| `POST /oauth/token` `client_credentials` | machine-to-machine | `client_secret_basic`; audience-bound ES256 token (RFC 8707). |

All tokens are audience-bound (RFC 8707 resource indicator, or the issuer as the
safe default), scope narrows within the client's ceiling, and the error surface
is RFC 6749 §4.1.2.1 / §5.2. Clients live in a `ClientRegistry`, codes in an
`AuthorizationCodeStore`, and rotating refresh tokens in a `RefreshTokenStore`
(all in-memory for now). The issuer URL derives from the request, or from
`OAUTH_ISSUER` behind a TLS-terminating proxy.

## What's next (later cuts)

- `private_key_jwt` client auth (RFC 7523) — the no-shared-secret path for agents;
  already advertised in `token_endpoint_auth_methods_supported`.
- Postgres-backed `ClientRegistry` / `AuthorizationCodeStore` / `RefreshTokenStore`
  (durability matters for reuse detection), and an `axi oauth client` verb.
- The webapp wires `set_subject_resolver` (session → subject) + serves the login
  and consent pages; a consent screen replaces first-party auto-consent.
- Resource-server enforcement: the first production `AuthzHook` into `axi serve`
  (bearer → `ActorContext` → `GUARD.decide`), Protected Resource Metadata
  auto-derived from the router registry, RFC 9470 step-up challenges (P3).
- RFC 8693 token-exchange delegation (P4).
