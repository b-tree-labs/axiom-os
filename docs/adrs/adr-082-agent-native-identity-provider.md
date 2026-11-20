# ADR-082: Agent-Native Identity Provider — OAuth 2.1 AS + OIDC + MCP Resource Server

**Status:** Accepted (2026-07-09)
**Deciders:** Benjamin Booth
**Related:** ADR-083 (OpenFGA authz substrate), ADR-084 (ActorContext identity
unification), ADR-085 (webauth ES256), ADR-086 (authenticated delegation),
ADR-055 (Unified Governance Fabric / GUARD), ADR-075 (SSO/OIDC delegated auth —
the OAuth *client* half), ADR-077 (local principal / progressive trust),
ADR-018 (public LLM endpoint), ADR-038 (builtin MCP server).

---

## Context

Axiom must serve, from one backend, a first-party web app, an imminent mobile
app, **and** agents / MCP clients. The industry has converged: every major agent
platform (Anthropic MCP, OpenAI Apps SDK, LangGraph, CrewAI, Cloudflare Agents,
Google A2A) authenticates on **OAuth 2.1 with audience-bound bearer tokens** and
**defers fine-grained authorization to the resource server** — which for us is
GUARD (ADR-055). The differentiated, hard part (deterministic, auditable,
per-call authorization) is already ours; the exposure is the *credential wire*.

Today: `webauth` mints first-party HS256 JWTs for its own sessions only; the
`auth` builtin is an OAuth **client** (relying party) to external IdPs
(ADR-075); there is no OAuth **server**. Our MCP auth story is stale — pre-shared
≥256-bit bearer tokens (ADR-038 D6; spec/prd-builtin-mcp-server "Phase 5"). The
docs also carry a Gen-1 commitment to Ory Kratos as the identity server
(spec-security §3, prd-security Part 1) that we are not building.

## Decision

Axiom becomes a **standards-compliant OAuth 2.1 Authorization Server + OIDC
Provider + MCP Resource Server**, in a **new `oauth` builtin extension** distinct
from the `auth` relying-party extension.

- **Naming split (explicit):** `auth` = RP (Axiom logs a user in *via* an
  external IdP). `oauth` = AS (Axiom *issues* tokens to first-party and agent
  clients). External IdPs federate in *upstream* through `auth`.
- **Endpoints** (mount on the `http` substrate as ordinary `MountSpec`s):
  `/authorize`, `/token`, `/revoke`, `/introspect`, `/userinfo`, `/oauth/register`,
  JWKS, and the public `/.well-known/{openid-configuration,oauth-authorization-server,
  oauth-protected-resource[/{path}]}` (discovery mounts declare `requires_authz=False`).
- **Flows:** Authorization Code + **PKCE S256** (mandatory), refresh, and
  client-credentials (headless agents). **Audience binding (RFC 8707)** so a
  token minted for resource A cannot be replayed at B. Client onboarding via
  **Client ID Metadata Documents** (preferred) + DCR fallback.
- **Consent** is routed through GUARD: `/authorize` builds an `ActionEnvelope`
  and calls `decide()`; `PROPOSE_TO_HUMAN`/`AWAIT_HUMAN` *is* the per-client
  consent primitive (also the confused-deputy defense).
- **Resource-server enforcement** folds into the `http` authz-hook: Protected
  Resource Metadata **auto-derives from the RouterRegistry**; audience + `iss` +
  `typ` validation; the token-passthrough **prohibition**; and RFC 9470
  step-up challenges (`WWW-Authenticate: … insufficient_user_authentication /
  insufficient_scope`). Tokens are ES256/JWKS-verified per ADR-085.

GUARD remains the sole authorization decision point; scopes are a coarse
transport claim, never an authorization bypass.

## Consequences

- Standard MCP/agent interoperability: any conformant MCP client discovers our
  endpoints and receives audience-restricted tokens. A future `/mcp` mount
  becomes OAuth-discoverable with zero extra code (PRM derives from the
  registry).
- **This wires the first production `AuthzHook` into `axi serve`.** Today none
  exists — `compose_app` fail-closes and served surfaces only work with
  `--insecure`; the webauth→ActorContext→GUARD middleware is the missing piece.
- New `oauth` extension to build (build phase P2/P3). Deferred, explicitly:
  DPoP sender-constrained tokens; device-flow *as AS* (the client side already
  exists in `auth`).

**Supersedes:** the Kratos identity-server commitment (spec-security §3;
prd-security Part 1), ADR-038 D6 MCP auth modes, and the Phase-5 pre-shared-token
plan in spec/prd-builtin-mcp-server. **Amends:** ADR-075 (re-scoped as the RP
half), prd-axiom-auth-sso, spec-serve / prd-serve (adds the oauth mounts +
token-resolution step), and ADR-018 (the AS provides the standards path for
off-node HTTP auth; Ed25519 signed-header stays for node↔node peer traffic).
