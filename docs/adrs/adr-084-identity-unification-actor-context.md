# ADR-084: Identity Unification — ActorContext

**Status:** Accepted (2026-07-09)
**Deciders:** Benjamin Booth
**Related:** ADR-055 (GUARD — consumes the actor), ADR-077 (local principal /
progressive-trust posture — folds in as Assurance), ADR-082 (OAuth AS — mints the
claims), ADR-083 (OpenFGA — consumes tenant/attributes), ADR-085 (webauth token —
carries the claims), ADR-086 (delegation — the actor chain), ADR-020 (handle
grammar).

---

## Context

Axiom carries **three principal representations that do not connect**:

1. `axiom.vega.identity.principal.Principal` — `handle` + `public_bytes`, the
   minimal identifier the `ActionEnvelope` actually carries into GUARD.
2. `axiom.infra.principal.PrincipalContext` — the posture / assurance ladder
   (`open < attested < sso/service`, ADR-077). **It never reaches `decide()`.**
3. The raw JWT claim bag at the HTTP edge (roles, tenant, `aal`, `acr`) —
   discarded once a bearer maps to a handle.

So `decide()` sees neither **roles**, **tenant**, nor **assurance**; RBAC, ABAC,
and step-up cannot even be *expressed* at the decision point. Separately, the
handle grammar contradicts across docs (ADR-020 single-`@user:host` vs several
docs' `@@`), though the code follows ADR-020.

## Decision

**Compose, don't merge.** Introduce a single governance actor and thread it end
to end.

- `vega.identity.Principal` stays the **minimal cryptographic identifier** — its
  `handle` + `public_bytes` are load-bearing in capability signatures; do not
  bloat it.
- New **`ActorContext`** (governance) = `principal` + `tenant` + `roles` +
  `attributes` + **`Assurance`** (`posture`, `aal`, `acr`, `amr`, `auth_time`).
  It becomes `ActionEnvelope.actor`, with a compatibility property that still
  exposes the bare principal so existing receipts and signatures are unchanged.
- New **`SubjectContext`** (`tenant` / `fga_user` / `attributes` /
  `contextual_tuples`) rides alongside for the substrate Check (ADR-083).
- **Resolution at the identity boundary:** a `resolve_actor` seam populates
  `ActorContext` **deterministically from verified token claims** (the
  ADR-085/082 token) — no lookups inside `decide()`.
- `infra.PrincipalContext` **re-bases on `ActorContext`** (keeps its public API);
  posture maps into `Assurance` via a **normative posture ↔ NIST AAL ↔ `acr`**
  table so `attested`/`sso` become expressible AAL levels.
- Rule matching is enriched: `actor_match` gains role / tenant / posture
  predicates (removing the literal-or-`*` limitation).
- New **`STEP_UP_REQUIRED`** verdict + `Challenge` payload (RFC 9470) — step-up
  now **originates from the decision**, not beside it. (Today `infra/stepup.py`
  raises an exception disconnected from GUARD.)
- Handle grammar **normalized to ADR-020** across the docs.

## Consequences

- `decide()` finally receives roles, tenant, and assurance — the precondition
  for ADR-083 (substrate), ADR-086 (delegation subject-assurance), and step-up.
- One actor model instead of three; the handle-grammar drift is closed (the code
  already complies, so this is a docs fix).
- **Honest code-gap flag:** spec-governance-fabric §1.2 references an
  `on_behalf_of` field on `Principal` that does not exist. It is *not* added to
  `Principal`; ADR-086 places delegation on `CapabilityToken` instead, and that
  spec section is corrected.

**Supersedes/amends:** ADR-055 D2 (actor type widens to `ActorContext`); ADR-077
(posture folds into `Assurance`; step-up wires into the verdict);
spec-governance-fabric §1; prd-axiom-authz §5.1/§5.2; spec-aeos-identity-addendum
§1 (handle grammar); spec-identity-acquisition.
