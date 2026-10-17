# ADR-083: Authorization Substrate Under GUARD — OpenFGA

**Status:** Accepted (2026-07-09) — substrate = OpenFGA; a Postgres Check-latency
benchmark gates fleet-wide consistency tuning (see Consequences).
**Deciders:** Benjamin Booth
**Related:** ADR-055 (Unified Governance Fabric / GUARD — the PDP this extends),
ADR-084 (ActorContext — supplies tenant/roles/attributes to the Check),
ADR-026 (ownership model — the four rights encode as relations), ADR-025
(federation threat model — deterministic gates), ADR-086 (delegation tuples).

---

## Context

GUARD (ADR-055) is our sole authorization decision point —
`decide(ActionEnvelope) → Verdict`, with a capability floor, a rule engine, and
RACI graduation. But the implemented engine has **no RBAC** (`Principal` carries
no roles; rule `actor_match` is literal-or-`*` — `authz/rules.py`), **no ReBAC**,
and **no pluggable multi-model composition** (one rule list, fixed
deny-overrides). We need combined **RBAC + ABAC + ReBAC** for a multi-tenant web
app plus agent-delegation graphs.

The docs have long *named* OpenFGA (spec-connections §8, spec-security §4) — but
as a **direct authorizer** checked at call sites, fed by Ory Kratos identity. That
is a Gen-1 pattern inconsistent with GUARD-as-sole-decision-point.

## Decision

Adopt **OpenFGA** as the fine-grained authorization **substrate that GUARD
calls** — never a direct authorizer.

- **GUARD stays the PDP / decision-site / audit spine.** The deterministic
  **capability floor stays *underneath* the substrate** and fail-closed. This is
  our "deterministic floors under judgment" rule.
- `decide()` gains a **`PolicySourceRegistry` + configurable combiner**
  (deny-overrides default; permit-overrides / first-applicable selectable). The
  capability floor runs first (short-circuits); then an `AuthzSubstrate` Check
  is the first *authoritative* source, alongside the existing rule and
  graduation sources. A `NullSubstrate` (fail-closed in prod, permit-all in dev)
  keeps GUARD safe when unwired.
- **Model mapping:** RBAC → roles-as-relations; ReBAC → relationship tuples;
  ABAC → CEL **conditions** + request `context` + contextual tuples. Un-modeled
  resources return **`ABSTAIN`**, falling through to graduation — so phased
  rollout (per-type authoritative flip) never breaks novel actions.
- **Deployment:** self-hosted OpenFGA on the Postgres we already run (ADR-052),
  with Okta/Auth0 FGA as a managed escape hatch. A thin `AuthzSubstrate` port
  keeps the engine swappable.
- **`SubjectContext`** (tenant / `fga_user` / attributes / contextual_tuples) is
  threaded onto `ActionEnvelope` at the identity boundary (ADR-084);
  `SCHEME_TO_FGA_TYPE` and `IntentRelationMap` registries map `ResourceRef` →
  FGA object and `ActionIntent` → relation.

**Substrate choice, decided:** OpenFGA's check cache is **off by default →
strong consistency**, so a revoked tuple is visible on the very next `Check`
with **no ZedToken bookkeeping**. SpiceDB's ZedTokens buy multi-region scale-out
we don't need, at the cost of threading snapshot tokens through every resource
row — so **SpiceDB is the documented fallback** only if read-after-revoke at
scale ever demands it. **Cedar/OPA are ruled out as primary** (stateless
evaluators over data you hand them — not storage-backed relationship graphs;
they push the hard part back onto us), though Cedar remains viable later as a
policy-overlay source.

## Consequences

- One decision engine, one audit trail (ADR-055 D2/D8 preserved). **Extensions
  never call OpenFGA directly** — always via `GUARD.decide()`.
- A new substrate service to operate. **Gate:** there is *no* published
  OpenFGA-on-Postgres benchmark (public numbers are Okta's managed DynamoDB), so
  we run our own Check-latency test before pinning `HIGHER_CONSISTENCY`
  fleet-wide with the read cache enabled. The decision path defaults to
  `HIGHER_CONSISTENCY`; `ListObjects` for UI may relax to `MINIMIZE_LATENCY`.

**Supersedes:** the direct-check role of OpenFGA in spec-connections §8 and
spec-security §4, and prd-security's authz framing. **Amends:** prd-axiom-authz
§5.2/§7, spec-governance-fabric §5.1,
prd/spec-federation OpenFGA mentions, prd-data-platform (RLS), and
prd-managed-infrastructure IAM-3. Roadmap phases in spec-connections §8.3 reslot
to "substrate under GUARD."
