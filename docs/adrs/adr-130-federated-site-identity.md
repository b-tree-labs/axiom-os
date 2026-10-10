<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-130 — Federated site identity: joining a site delivers its sign-in

**Status:** Proposed (founder-initiated 2026-09-24)

## Context

Standing up sign-in on a new node today is manual IdP plumbing: copy the
tenant and client id from wherever the last deploy recorded them, mint a
client secret, register this machine's redirect URI in the IdP console,
and wire `AXIOM_GATE_OIDC_*`. We did exactly this for a local dev node
against the site app registration that already serves the site node's
webgate — and hit, live, the failure classes the manual path guarantees:

- IdP redirect URIs match as **exact strings** (`localhost` ≠
  `127.0.0.1` — AADSTS50011), so every machine/port/host variant is a
  console edit.
- Client secrets are per-registration; a careless
  `credential reset` without `--append` silently invalidates the
  production node's secret.
- Nothing distributes the config: each node re-discovers the tenant,
  client id, scopes, and claims mapping by archaeology.

The founder's observation: **a node that federates with (or attaches
to) a site should pick the site's sign-in up automatically** — "running
a local Axiom node federated with a shared resource like our installed
site node just reuses the OIDC gate." And further: *attach to a site's
shared resource* and *federate with a site* look like the same act.

Adjacent decisions this lands on, none of which owns it:

- **ADR-041** (identity acquisition; pluggable identity providers) —
  the site's IdP configuration is exactly a pluggable provider, but
  ADR-041 doesn't say how a provider's config **travels**.
- **ADR-028** (trust graph) — gives the trust edge a brokered identity
  assertion can ride, but says nothing about sign-in.
- **ADR-103** (directory seam) — group/membership resolution once an
  identity exists; not how it is established on a member node.
- The join-a-site onboarding lifecycle (working doc,
  `onboarding-lifecycle-local-to-site.md`) — flips the four planes
  local→served under a proven identity, but treats the identity plane's
  *configuration* as already present.
- The Site Topology Registry — the natural publication surface, today
  carrying peers and endpoints but no identity material.

## Decision

**A site publishes a *site identity profile*; joining the site consumes
it. "Attach to site" and "federate with site" converge on one act that
delivers scope, endpoints, and sign-in together.**

### 1. The site identity profile (the published thing)

A signed document in the site's topology-registry entry, containing
ONLY non-secret material:

```
site_identity_profile:
  site: <site id>
  issued_by: @<site-node>:<site>          # signed with the site's key (ADR-027 machinery)
  sign_in:
    - name: sso
      label: "Sign in with UT EID"
      flow: oidc_pkce | site_broker
      # oidc_pkce fields:
      issuer/tenant/client_id/scopes/subject_claim/roles_claim
      # site_broker fields:
      broker_url: https://<site-node>/gate
  updated_at / version
```

No client secret ever appears in a profile; a mechanism that would
require distributing one is out.

### 2. Mechanism A — `oidc_pkce` (ships first)

The member node's gate runs the auth-code + **PKCE** flow as a *public
client*: no client secret on member nodes at all. The IdP app
registration is marked public-client and given one **loopback**
redirect URI — under the loopback exemption (RFC 8252 §7.3; Entra
implements it) any port matches, so every dev machine works with zero
console edits and the exact-string-URI failure class disappears for
members. The site node itself stays a confidential client with its own
secret, unchanged.

Requires: `webgate.oidc.OidcSignIn` grows a secretless PKCE mode
(today a secret is mandatory); `axi site join` (and federation attach)
fetches the profile, verifies the site signature, and writes the gate
config; JIT account creation keyed on the immutable subject claim, as
already designed.

### 3. Mechanism B — `site_broker` (the federation feature)

The member node has **no IdP coupling at all**: sign-in delegates to
the *site's* gate (browser → `broker_url` → site's IdP → back), and the
member node accepts a short-lived, audience-bound **site identity
assertion** (signed with the site key already distributed for
federation, verified against it), minting its *own local session* from
the assertion. Properties:

- The site is the IdP for its members: IdP churn, revocation, role
  assignment all stay site-administered; members never touch Entra.
- The assertion rides the ADR-028 trust edge — accepting it *is* an
  edge policy (`accept_identity_from: @site-node:site`), so a node
  federated for data can decline identity, and vice versa.
- Roles arrive as claims in the assertion, resolved at the site
  (ADR-103's seam), then mapped locally through role bundles.
- Offline/air-gap: a member node that can't reach the broker falls
  back to its local accounts (webgate's existing store) — sign-in
  degrades, existing sessions live out their TTL.

### 4. Convergence rule

`axi site join` = federation attach + profile consumption. One verb,
one recorded act (receipted), delivering: site scope grant, service
endpoints, and sign-in. A "shared resource" is precisely what a site
profile names; there is no second attachment concept.

### 5. Multi-site membership (founder question, 2026-09-24)

**Yes to both** — a person may join several sites, and a node may hold
several federation edges (the topology registry already models many
peers). The implications reach well past login, and the governing rule
is the one the codebase has been converging on everywhere: **site is an
explicit axis, never a merged one.** Memberships LINK under one
identity; they never UNION.

- **Identity: linking, not merging.** A person's memberships are a set
  of ``(site, principal, roles)`` bindings keyed to one IdP subject
  (the immutable ``oid``) or linked pairwise where sites use different
  IdPs. ``@ben:site-a`` and ``@ben:site-b`` are two principals with a
  recorded link — an account-linking record, not one principal with
  two hats. (Precedents already enforce the axis: magic links are
  site-bound at issuance; API keys are site-bound; chat conversations
  are principal-AND-site scoped.)
- **One active site per credential.** Every session, assertion, and
  API credential names exactly ONE site (audience-bound). "Which site
  am I acting in" is a first-class selection — the scope chip in the
  accepted mobile/desktop design is precisely this surface — and
  switching sites mints a new session context rather than mutating one.
  A broker assertion from site A is refusable-by-construction at
  site B: wrong audience, wrong signing key, wrong edge.
- **Scopes never union implicitly.** Reads are bounded by the active
  site's grant. A cross-site view (one person overseeing two reactors,
  one brief over two sites) is an EXPLICIT fan-out — query each site
  in its own scope, render with per-site marks — the same aggregation
  discipline as the oversight brief's source marks. Silent union is
  how a site-id split becomes a data leak.
- **Roles are site-scoped, never inherited.** ``operator`` at site A
  says nothing at site B. Role claims arrive per site (per assertion /
  per profile) and map through that site's role bundles only.
- **Classification boundaries hold across memberships.** A member of
  an export-controlled site and an open site is exactly the case the
  classification spec exists for: no credential, assertion, session,
  or cached datum minted under one site's tier may widen access under
  another. Federation edges carry classification posture; identity
  linking must not become a side channel between tiers.
- **Per-edge revocation.** Leaving / being removed from one site
  revokes that binding and its edge only; other memberships are
  untouched. A node's edges are likewise independently revocable, each
  with its own ``accept_identity_from`` policy.
- **Beyond login (named, deliberately out of scope here, each owned by
  its own surface):** the multi-site brief (fan-out + site marks — the
  oversight plane), multi-site chat scoping (conversation store is
  already site-keyed), vault/credential partitioning per site, and
  scheduling/PULSE acting under the correct site context. This ADR
  fixes the identity-and-membership substrate those build on.

## Consequences

- New member nodes get working sign-in with **zero IdP console work**
  (A) or **zero IdP contact** (B). The manual path remains possible and
  becomes the site-node bootstrap only.
- Secrets never travel: A is secretless by construction; B moves trust
  to the already-distributed federation keys.
- The topology registry becomes security-relevant: profiles must be
  signed by the site key and verified at join; a tampered profile is a
  sign-in redirect to an attacker, which is why profile verification is
  NOT optional and unsigned profiles are refused.
- Broker assertions add a token type to audit: short TTL, single
  audience (the member node), nonce-bound to the browser transaction —
  the same single-use discipline as the gate's reset/magic tokens.
- ADR-041's provider taxonomy gains a distribution story; ADR-028's
  trust edges gain an identity dimension; the onboarding lifecycle's
  identity plane becomes concrete. None are contradicted.
- Phasing: **A** with `axi site join` (config plumbing + PKCE mode in
  webgate); **B** as the follow-on federation feature (assertion
  format, edge policy, broker endpoint on the site gate).

## Alternatives considered

- **Distribute the client secret to members** — rejected: turns every
  member node into a credential holder for the site's confidential
  client; one leak revokes everyone; violates "the value never leaves
  the node."
- **Per-node app registrations** — rejected: N console registrations,
  N secrets, N redirect lists; exactly the toil being removed.
- **Wildcard/many redirect URIs on the site registration** — rejected:
  unbounded redirect lists on a production confidential client are an
  open-redirect surface and still require console edits per host.
