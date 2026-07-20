# ADR-085: Asymmetric Web-Session Tokens — webauth ES256 + JWKS

**Status:** Accepted (2026-07-09)
**Deciders:** Benjamin Booth
**Related:** ADR-082 (OAuth AS — the primary verifier of these tokens),
ADR-084 (ActorContext — the claim payload), ADR-080 (autonomous key rotation —
overlap-validity applies to signing keys), ADR-022 (KEEP / node Ed25519 identity
key — deliberately *not* reused here), ADR-076 (credential lifecycle).

---

## Context

`webauth` (the human/web-auth module lifted from SoilMetrix, committed
`cbfcac24`) currently signs JWTs with **HS256** — a symmetric secret. For an
OAuth AS (ADR-082) whose tokens are verified by third-party MCP clients and
resource servers, symmetric signing is **disqualifying**: every verifier must
hold the signing secret, so any verifier can **forge** tokens for any user.
`webauth` is otherwise undocumented (this ADR is its first record) and has no
JWKS, rotation, or revocation.

A tempting shortcut — sign with the node's existing **vega Ed25519 identity
key** (ADR-022) — is rejected below.

## Decision

`webauth` signs with **ES256 (ECDSA P-256)**.

- **Not HS256** (forgeable by verifiers) and **not the vega Ed25519 key.**
  Reusing the identity key would couple OAuth `kid` rotation to
  federation-identity rotation (cross-protocol key reuse), and EdDSA is not
  universally verifiable by third-party JOSE stacks in the wild. Ed25519 stays
  the *node-to-node* signer (ADR-022); ES256 is the *token* signer.
- **Algorithm agility** via `kid`-keyed keys. New `keys.py`: `SigningKey`
  (`kid` = RFC 7638 JWK thumbprint, `alg`, rotation state), **JWKS** published by
  the `oauth` extension, private keys stored via the Axiom secrets provider
  (`axiom.setup.secrets`).
- **Access tokens** = RFC 9068 `at+jwt` (typed, audience-bound, `iss`-stamped,
  short TTL). **Refresh tokens** = opaque + rotating, with **reuse detection →
  family invalidation** (overlap-validity per ADR-080).
- **Revocation (RFC 7009)** + **introspection (RFC 7662)** + a `jti` **denylist**
  so stateless JWTs are killable before expiry.
- **Migration:** a time-boxed **HS256 verify-only** compatibility flag for
  in-flight sessions; **HS256 minting is removed on day one** of the cutover.
  The public `webauth` function signatures are preserved by adding keyword args
  (`key` / `audience` / `issuer` / `require_typ`), so `cbfcac24`'s API and tests
  keep working.

## Consequences

- Standards-interoperable tokens any JOSE verifier can validate from JWKS with
  no shared secret.
- The HS256 core just committed (`cbfcac24`) is **upgraded in place** — this is
  build phase **P1, independently shippable**, and the first code step of the
  whole effort (nothing downstream needs to wait).

**Supersedes/amends:** spec-session-store (session tokens bind to ES256 bearer);
the `webapp/mount.py` docstring overclaim (per-route auth is not yet wired —
corrected as the enforcement lands).
