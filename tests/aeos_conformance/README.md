# AEOS federation conformance fixtures

Golden vectors for the AEOS federation conformance kit
([spec-aeos-federation-conformance-kit](../../docs/specs/spec-aeos-federation-conformance-kit.md),
[ADR-276](../../docs/adrs/adr-276-agents-federate-over-a2a-1-0-with-one-signing-rule-one-address-and-a-gate-on-everything-received.md)).
`test_fixture_selfcheck.py` checks that the fixtures agree with the rules they
encode: every canonical vector, every signed object against the pinned keys, the
receipt chain's `prev` link, every refusal, and the one vector accepted only as
data. That proves the fixtures, not an implementation; the Python and Go runners
that test an implementation are a follow-up. `test_aeos_status.py` checks the
AEOS section status table (a section is proven only when a named check exists).

## Layout (`fixtures/v1/`)

| File | What it holds |
|---|---|
| `keys.json` | Two test nodes. The keys are RFC 8032 section 7.1 TEST 1 and TEST 2, published test vectors; never use them for a real node. `pinned` lists the node ids a runner treats as pinned. |
| `canonical.json` | RFC 8785 vectors under the AEOS restrictions: key order, U+2028 and U+2029, control escapes, a non-BMP character, NFC by the signer, integer limits, and four inputs that MUST be refused (float, integer beyond 2^53 - 1, non-ASCII member name, lone surrogate). |
| `signed/card.json` | A signed AEOS Agent Card (node-a). |
| `signed/message.json` | A signed A2A Message; the signature sits in `metadata["urn:aeos:ext:federation:1"].signatures`. |
| `signed/delegation.json` | A delegation token issued by node-b for `@claude:bob` acting for `@bob`. |
| `signed/workload-offer.json` | A workload offer from node-b. |
| `signed/receipt-chain.json` | Two receipts; the second's `prev` is the SHA-256 of the first's canonical bytes. |
| `negative.json` | Objects a verifier MUST refuse (`neg-hash-as-sig`, `neg-wrong-type`, `neg-tampered`, `neg-kid-unpinned`, `neg-alg-none`) and one it MUST accept only as data (`neg-instruction-in-content`). |

## How a signature is checked

1. Take the `signatures` entry (top level, or in the AEOS metadata entry for a
   message) and remove it from the object.
2. Canonicalise the rest with RFC 8785.
3. The JWS signing input is `protected + "." + base64url(canonical bytes)`
   (detached payload, RFC 7515 Appendix F).
4. The protected header must have `alg = "EdDSA"`, `typ = "aeos+jws"`, the
   expected `aeos` object type, and a `kid` that is a pinned node id.
5. Verify the Ed25519 signature with the pinned public key.

## Provenance

Generated 2026-11-23 from the rules in `spec-agent-accord-protocol` §3 and
cross-checked with an independent RFC 8785 implementation (the `rfc8785`
Python package): every accepted canonical vector matches byte for byte, every
signed object verifies, and every negative vector is refused. Ed25519 is
deterministic, so a correct implementation regenerates identical files.

Changing a vector is a breaking change to the kit: add `v2/`, never edit `v1/`.
