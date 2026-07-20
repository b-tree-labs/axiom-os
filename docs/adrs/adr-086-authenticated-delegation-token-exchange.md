# ADR-086: Authenticated Delegation — RFC 8693 Token Exchange on CapabilityToken

**Status:** Accepted (2026-07-09)
**Deciders:** Benjamin Booth
**Related:** ADR-055 (GUARD / CapabilityToken — the object extended), ADR-082
(OAuth AS — hosts the token-exchange grant), ADR-083 (OpenFGA — the double-check
delegation tuple), ADR-084 (ActorContext — subject vs actor), ADR-076 (KEEP /
credential lifecycle), ADR-035 (existing delegation_chain — reconciled).

---

## Context

The agent-native differentiator is an **agent acting on behalf of a human** with
authority that is **scoped, revocable, auditable, and monotonically narrowing**.
The standard vocabulary is **RFC 8693 token exchange** (subject = human, actor =
agent, `act` chain). But RFC 8693 **explicitly does not require narrowing or
cryptographic binding** — a compliant STS may issue a *broader*, unbound token.
That gap is exactly where confused-deputy and privilege-escalation attacks live.

GUARD's `CapabilityToken` already has `delegation_depth`, `parent_capability`,
and `signature` — but the **code enforces neither narrowing nor
proof-of-possession**, while spec-governance-fabric §2 *claims* it does. This is
a real doc-overclaims-code gap.

## Decision

Map RFC 8693 onto `CapabilityToken` and **close the standard's gap** by making
narrowing and binding mandatory.

- **Keep `CapabilityToken`** as the delegation object; add `on_behalf_of`,
  `act_chain`, `audience`, and `subject_assurance`. The `oauth` AS (ADR-082)
  hosts the token-exchange grant as the **STS**.
- **Enforce monotonic narrowing at issuance** on all five axes (scope,
  audience, TTL, delegation_depth, resource set) **and re-verify independently in
  `decide()`**: chain walk + signature per hop + monotonicity re-check +
  revocation-subtree check + **proof-of-possession** (the WIMSE Workload
  Proof-Token pattern). Issuance-time checks are convenience; the decision-time
  re-check is the security boundary.
- **Double-enforced** with an OpenFGA grant (`agent … with delegation_valid`
  CEL condition, ADR-083). **Effective authority = capability scope ∩ the
  delegator's *live* FGA permissions ∩ actor floors** — so revoking the human's
  access immediately starves every capability derived from it.
- Enterprise interop: **ID-JAG / cross-app-access assertions** are acceptable as
  the inbound `subject_token` (issuer-side, optional) but do not replace our
  narrowing enforcement.

## Consequences

- Provably monotonic, PoP-bound, revocable delegation — the differentiator RFC
  8693 alone does not give us.
- **Closes the doc-overclaims-code gap:** the narrowing/PoP that
  spec-governance-fabric §2 already promises becomes real (build phase P4; the
  vault `issue_capability` narrowing enforcement is the code fix).
- New token-exchange endpoint on the `oauth` AS.

**Supersedes/amends:** spec-governance-fabric §2 / §2.4 (narrowing + PoP now
normative and implemented); prd-axiom-vault §5.1/§5.5. **Reconciles** ADR-035:
`delegation_chain` stays the informational/audit trail; the new `act_chain` is
the **authorization-bearing** chain re-verified at decision time.
