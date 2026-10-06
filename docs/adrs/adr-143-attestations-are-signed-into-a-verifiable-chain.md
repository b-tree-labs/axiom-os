# ADR-143: Attestations are signed into a verifiable chain with public-key signatures

**Status:** Accepted (2026-09-30)
**Related:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[ADR-126](adr-126-typed-decision-receipts.md), [ADR-020](adr-020-federation-identity-and-relationships.md)
(node identity keys), [spec-attestation.md](../specs/spec-attestation.md) §Integrity

## Context

The platform has an HMAC chain, `axiom.infra.state.TamperEvidentChain`, and
three audit chains built on `audit_log._compute_hmac`. All read their key from
an environment variable. That falls short for attestations in three ways:

1. **Symmetric keys let the verifier forge.** A regulator or auditor who
   verifies an HMAC chain must hold the key, and anyone holding the key can
   produce valid records. An independent verifier is exactly who must *not*
   be able to forge.
2. **Environment-variable keys break the secrets rule.** The vault's
   `vault.resolve` verb deliberately never returns a secret value, so
   there is no sanctioned way to hand a signing key to a process.
3. **Canonicalisation is platform-dependent.** `_canonical_json` uses
   `default=str`, so the bytes stamped for a datetime or decimal depend on the
   Python type that happened to be in the dict. An external verifier cannot
   reproduce that reliably.

Federation identity (ADR-020) already gives every node an Ed25519 identity key
verifiable against the federation directory, and receipts are content-addressed.
Together these solve the first two problems for machine records. The CLI's `attested`
posture already holds a per-person device keypair in the OS keychain.

## Decision

Attestations use **hash chaining plus public-key signatures**, never HMAC:

- **Canonical form.** A record is serialised by a specified canonicalisation:
  JSON with sorted keys, UTF-8, no insignificant whitespace, timestamps as UTC
  RFC 3339 with microseconds, and decimals as strings exactly as entered
  (never floats). The spec defines it precisely. A standalone verifier
  implements it in a few dozen lines of stdlib Python.
- **Content address.** `digest = sha256(canonical(record without signatures))`;
  the record's URI follows the receipt form, `axiom://attest/sha256:<digest>`.
- **Chain.** Each record carries `prev_digest`, the digest of the previous
  record in the same chain. There is one chain per (site, book), and `seq` is
  gapless within it.
- **Quantities keep their uncertainty.** A measured value in signed content is
  serialised in the `axiom.uncertainty` wire form, never as a bare number. A
  value someone read off a gauge with no stated uncertainty says so explicitly
  (`Unquantified`); it is never silently treated as exact.
- **Node signature.** The site node signs `digest` with its federation
  identity key (Ed25519). Verification needs only the public key from the
  federation directory. Signing happens through a key-custody signing
  operation, so the private key never enters the extension's process.
- **Personal signature (higher assurance).** Where a book requires it, the
  signer's own device key also signs `digest`: the CLI `attested` keypair, or
  a WebAuthn/passkey assertion in the browser.
- **Anchors.** Daily, and on every seal, the node publishes a Merkle root over
  all book heads for the site. It is registered as a receipt and printed on
  exported and printed reports. Rewriting history behind an anchor requires
  forging every later anchor, including paper.
- **Append-only guard.** The application role has INSERT and SELECT only. A
  trigger rejects UPDATE and DELETE, and TRUNCATE is revoked. A test asserts
  the guard at every migration head.
- **Verification runs where drift is already watched.** Chain and anchor
  verification is a hygiene `node_health` finding on the heartbeat, the one
  daemon every node already runs. A break surfaces as an oversight item on
  the brief, not in a separate job someone must remember to invoke.
- **Key rotation.** Every signature carries a `key_id`. Retired public keys
  stay in the directory for verification.

`TamperEvidentChain` remains for internal audit chains, where the verifier is
the platform itself. Its docstring's "Operations Log" consumer and
`AXIOM_OPS_LOG_HMAC_KEY` are withdrawn.

## Options considered

- **HMAC chain with a vault-held key** (the original Ops Log design). Lost:
  whoever verifies can forge. Also, a vault signing operation for HMAC would
  still leave third-party verification impossible.
- **Per-person signatures only.** Lost: many sites will not issue device keys
  on day one, and a record must be verifiable without them. The personal
  signature layers on top of the node signature.
- **Blockchain or ledger service** (proposed in a consumer layer's early
  design). Lost: operational weight for no property that signed anchors on
  paper and in receipts do not already give.
- **External timestamping authority (RFC 3161).** Deferred. It can be added to
  anchors later without changing records.

## Consequences

- Key custody needs a **sign** operation: `sign(key_ref, digest) → signature`,
  with the key never exported. This is an Axiom work item on the identity and
  vault side.
- Evidence packages ship `records.jsonl`, the public keys used, anchors, and
  `verify.py`. A regulator verifies without Axiom and without any secret.
- Any consumer design that planned an HMAC-chained logbook moves to this chain.
