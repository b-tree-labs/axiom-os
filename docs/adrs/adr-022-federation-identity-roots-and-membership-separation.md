# ADR-022: Federation Identity Roots and Membership Separation

**Status:** Proposed — authority model confirmed (hybrid, multi-sector); data-model migration phasing under review
**Date:** 2026-04-15
**Authors:** Benjamin Booth, Claude
**Related:** ADR-016 (multi-node federation), ADR-020 (federation identity layers), ADR-021 / planned ADR-025 (threat model), planned ADR-023 (topology & lifecycle), planned ADR-024 (root availability & delegation)

---

## Context

Two decisions, left open in ADR-020, must now be made together because
they are inseparable in the data model:

1. **Which identity authority model do institutional federations use?**
   Self-sovereign only, InCommon/eduGAIN leverage, or a hybrid?
   (ADR-020 §"Open Questions" deferred this pending a UT pilot.)
2. **How are identity and federation membership represented in the
   runtime?** Today `KnownNode` conflates identity, reachability, and
   membership into a single struct with a flat `state` field — fine
   for a laptop-and-self-hosted-node pair, broken the moment a single node is a
   member of an ephemeral class federation, a long-running bilateral
   partnership, and an open public federation simultaneously.

The scale target — 10,000 to 100,000 nodes across multiple
universities, decade-long cohorts, and partner institutions — makes
the current conflation untenable. At that scale, every O(n²) property
in the federation becomes an outage, and conflated state is an
O(n²) property: any membership change forces reasoning about
identity, any identity rotation forces reasoning about every
membership. They must be orthogonal.

This ADR covers both: the authority model (decision §1–2) and the
data-model separation (decision §3–5). ADR-023 handles topology and
lifecycle; ADR-024 handles availability and key hygiene; ADR-025
formalizes the threat model.

## Decisions

### 1. Hybrid Identity Authority — Self-Sovereign Root with Optional Multi-Sector Anchoring

Every principal (person, node, organization) holds a self-sovereign
Ed25519 root keypair. Identity is owned by the principal, not by any
authority. This is the floor: federation works even when no external
identity provider is available.

Principals **may** additionally anchor their root into an external
identity authority. Anchoring is optional, sector-specific, and
recorded as a signed attestation on the self-sovereign root.

**The three sectors Axiom must support from day one:**

| Sector | Example anchoring authorities | Typical scenario | Day-1 constraints |
|--------|------------------------------|------------------|-------------------|
| **Academic** | InCommon / eduGAIN (SAML), domain-based TLS | an institution ↔ a partner-university research federation, multi-university cohorts | Standard |
| **Government** | PIV / CAC / FICAM, agency-issued certs | Federal lab ↔ university research partnership (e.g. an institution ↔ a national lab); multi-agency collaboration | **Export control** (ITAR, EAR, NRC 10 CFR Part 810) — nationality-based access, auditable per-document approvals; **classified tiers** (SECRET+) as future capability — must not be foreclosed by the identity model |
| **Industry** | Consortium-issued PKI, vendor IDPs, bilateral CA chains | Multi-company R&amp;D consortium, vendor partnership | Standard plus IP-ownership attestation per context (aligns with ADR-020's context-scoped artifact model) |

**Anchoring provides two benefits regardless of sector:**
- Bootstrapping trust at onboarding — a peer with no bilateral
  history can verify a counterparty's root against their sector's
  authority without a manual key ceremony.
- Reputational rails — fraud detection and reputation scoring can
  reference anchoring attestations as one input without making the
  anchor itself authoritative.

**Cross-sector bridges** (edu ↔ gov, gov ↔ industry, etc.) are
designed explicitly in ADR-023 because they carry constraints not
present within a single sector. The governing principle: **the more
restrictive side's rules apply globally in the bridged federation**.
An institution ↔ national-lab bridge must honor the national lab's export-control obligations even
though the institution alone does not have them.

**Why hybrid and not self-sovereign only:** at the 10k-100k-node
scale across universities, agencies, and industry partners, pure
self-sovereign trust bootstrap is untenable — every new peer
relationship requires an out-of-band ceremony. Existing sector
authorities (InCommon, FICAM, consortium PKI) already solve the
identity-bootstrap problem for their sector; ignoring them would be
rebuilding what those sectors already have and raising the
integration barrier for every deployment.

**Why hybrid and not authority-required:** coupling Axiom to a
single identity authority (InCommon, FICAM, or any one consortium)
makes it unavailable to any deployment outside that authority's
reach — international partners, deployments inside classified
enclaves, ad-hoc research groups. Self-sovereign as the floor
preserves reach; sector anchoring as an add-on preserves bootstrap
convenience where applicable.

**Content-tier alignment.** Identity authority pairs with the
existing three-tier content model (public / restricted /
export_controlled — see `spec-rag-architecture.md` and the
two-dimensional content model). The authority layer decides
**who you are**; the tier layer decides **what you can see**.
Together they enforce export-control regimes without ad-hoc
checks sprinkled across the codebase.

**Classified information (SECRET and above)** is a deferred
capability — not day-1 — but the identity model must not foreclose
it. Specifically: the self-sovereign root must remain replaceable
by a hardware-backed (e.g. HSM, CAC-resident) keypair without
rewriting the trust chain; anchoring must accommodate authorities
whose metadata is not internet-reachable (air-gapped classified
enclaves).

**Hybrid implies:**
- Axiom never requires external identity-authority membership to
  function.
- A principal can flip anchoring on or off, and swap between
  authorities, without re-keying.
- Cross-sector trust uses cross-signed attestations independently
  of whether either side anchored. The bridge design (ADR-023)
  defines how the cross-signing ceremony handles sector-specific
  constraints.

### 2. Root Keys Are Global and Permanent; Memberships Are Scoped and Lifecycled

**Identity layer (global, permanent):**

Every principal has exactly one root identity, globally unique,
cryptographically owned. A person's `@ben.booth:axiom`, a node's
`@example-host:org`, an organization's `@org` — each is a
self-sovereign Ed25519 root key that survives employer changes,
institutional splits, retirement, and the passage of time. Root keys
can be rotated (see ADR-024) but the identity they signify is
durable.

**Membership layer (scoped, lifecycled):**

A `FederationMembership` is a **separate, signed, TTL-bearing record**
distinct from identity. It asserts: "identity X is a member of
federation F, admitted by admitter Y on date T₀, expiring on T₁ (or
'long-running' with renewal every R), with role R and capabilities
C, under relationship type K (Cluster / Partner / Federated)." Each
identity may hold many concurrent memberships in unrelated
federations; each federation may contain many identities; the Cartesian
product is exactly what today's `KnownNode` cannot express.

**Distinction matters because the lifecycles are different:**

| Property | Identity | Membership |
|----------|----------|------------|
| Lifetime | Decades | Hours to years |
| Scope | Global | Per-federation |
| Issuer | Self (self-sovereign) | Federation authority + admitter |
| Revocation | Almost never (compromise only) | Routinely (class ends, partnership sunsets, node removed) |
| Count per principal | 1 root, N rotated | Many concurrent, thousands over time |

### 3. KnownNode Refactored into Three Orthogonal Concepts

Today's `KnownNode` holds identity fields (`public_key`, `owner`,
`fingerprint`), transport fields (`url`, `ssh_user`, `ssh_host`), and
a flat `state` that encodes lifecycle across all three layers at
once. This ADR splits it:

```
Identity        — permanent; owned by the principal; one per root key
  node_id (= sha256(pubkey))
  public_key
  owner (display / human-friendly, not authoritative)
  fingerprint
  first_seen_at
  last_verified_at
  rotated_from: Optional[Identity]   # for key rotation chains

Reachability    — transient; operational; many per Identity
  identity_ref: node_id
  transport: "ssh" | "a2a" | "mdns" | ...
  url: transport-specific endpoint
  last_reachable_at
  health: "healthy" | "degraded" | "unreachable"

Membership      — scoped + signed; many per Identity per federation
  identity_ref: node_id
  federation_id
  relationship: "cluster" | "partner" | "federated"
  admitted_at
  admitted_by: identity_ref        # the admitter's identity
  expires_at: Optional[ISO-8601]   # null = long-running with renewal
  renewal_cadence: Optional[duration]
  last_renewed_at: Optional[ISO-8601]
  roles: list[str]
  capabilities: list[str]
  manifest_sequence: int           # monotonic; replay-protection
  signatures:
    - admitter_sig
    - federation_root_sig
```

**Migration:** existing `KnownNode` entries map one-to-one to an
`Identity` + a single `Reachability` + (in most cases) zero
`Membership` records. The refactor is additive for the laptop-and-self-hosted-node
case — nothing observable breaks — and unlocks the multi-federation
case we cannot express today.

### 4. Membership Manifests Are Signed, TTL'd, and Monotonic

A federation's membership state is a **manifest**: a list of current
`Membership` records, signed by the federation root (or threshold
quorum per ADR-024), tagged with a monotonic `manifest_sequence`
integer and an `expires_at`. Peers refuse manifests that are older
than one they've already accepted (replay protection) or past their
expiry (staleness protection).

Manifests are **content-addressed** (each version has a hash) and
support **delta sync**: a peer holding manifest N can pull the
delta from N→N+k without re-downloading the full list. This is
load-bearing at 50k-member scale; full-list resync on every update
is the O(n²) failure mode.

Expiry plus monotonicity together give the **graceful root outage**
property: if the federation root is unreachable, peers continue
operating on their most recently accepted manifest until it expires.
Expiry windows for long-running federations are measured in days-to-
weeks, not seconds — enough tolerance for extended outages without
indefinite staleness. (ADR-024 details availability.)

### 5. Verification Chain for Every Federation Message

A message between federation peers is trustworthy only if **all
three** signature layers verify:

```
1. Node signature         — message was produced by the claimed node
                            (signed with the node's identity root key)
2. Membership attestation — the node is currently a member of the
                            federation the message is scoped to
                            (latest signed manifest contains the node)
3. Affiliation validity   — (for messages invoking an org's authority)
                            the node's affiliation with the org is
                            currently valid (not expired, not revoked)
```

This is a tightening of ADR-020 §"Verification Chain": the
affiliation layer was described there, this ADR makes the
membership layer first-class and distinct from affiliation.
Membership applies to everyone in a federation; affiliation applies
only to those acting under an organization's authority.

## Data Model Changes — Concrete

**Current** (`src/axiom/federation/discovery.py`):

```python
@dataclass
class KnownNode:
    node_id: str
    display_name: str
    url: str
    transport: str
    state: NodeState
    profile: str
    # identity (populated after fetch_identity_ssh)
    public_key: str
    owner: str
    fingerprint: str
    # ...
```

**Target** (new modules under `src/axiom/federation/`):

```python
# identity.py — Identity layer
@dataclass(frozen=True)
class Identity:
    node_id: str
    public_key: str
    owner: str
    fingerprint: str
    first_seen_at: str
    last_verified_at: str
    rotated_from: Optional[str] = None   # previous node_id

# reachability.py — Reachability layer
@dataclass
class Reachability:
    identity_ref: str    # Identity.node_id
    transport: str
    url: str
    last_reachable_at: str
    health: Literal["healthy", "degraded", "unreachable"]

# membership.py — Membership layer (new)
@dataclass(frozen=True)
class Membership:
    identity_ref: str
    federation_id: str
    relationship: Literal["cluster", "partner", "federated"]
    admitted_at: str
    admitted_by: str
    expires_at: Optional[str]
    renewal_cadence: Optional[str]
    last_renewed_at: Optional[str]
    roles: list[str]
    capabilities: list[str]
    manifest_sequence: int
    admitter_signature: bytes
    federation_root_signature: bytes
```

`KnownNode` is retained as a **view** — a dataclass that joins an
`Identity` with its currently-best `Reachability` and its relevant
`Membership` records for a given federation scope. Existing callers
continue to see a `KnownNode`-shaped object; internally the storage
is three orthogonal registries.

## Consequences

**Positive:**

- A single node can be in many federations without state contortions.
- Identity rotation does not force any membership change (only a
  rotation-chain entry on the Identity).
- A federation's membership list can be delta-synced and
  cryptographically replay-protected — load-bearing at 50k members.
- Root outage does not cascade — TTL'd manifests give graceful
  degradation (detail in ADR-024).
- Ephemeral federations (class periods, research sprints) are first-
  class; their memberships expire automatically.
- Cross-institutional trust becomes a data concern (cross-signed
  attestations) rather than a code concern (no special-case logic).
- The model is compatible with both self-sovereign and anchored
  identity roots — institutions choose without forking the code
  path.

**Negative:**

- Three registries instead of one — storage and in-memory model are
  larger.
- Migration of existing `KnownNode` entries is non-trivial (though
  additive — no breaking change for the laptop-and-self-hosted-node case).
- Membership manifests add a new signing+verification path every
  peer must implement. Without care this becomes a latency tax on
  every cross-node operation; mitigate via caching + delta sync.
- Hybrid identity authority introduces two trust-bootstrap paths
  (self-sovereign and anchored). Must test both paths at every
  release to prevent drift.

**Neutral:**

- Topology (flat mesh / hub-spoke / hierarchical / cross-root bridge)
  is orthogonal to this ADR and decided in ADR-023.
- Availability (threshold signing, delegated intermediate keys,
  manifest caching policy) is orthogonal and decided in ADR-024.
- Formal threat model for the layered design lives in ADR-025.

## Open Questions (Deferred to Subsequent ADRs)

- **ADR-023:** Exact topology thresholds (when to elect a
  coordinator, when to adopt hierarchical aggregation), handshake
  sequence, propagation protocol (gossip + delta manifest pull),
  ephemeral / long-running / open lifecycle semantics.
- **ADR-024:** Threshold-signing scheme (FROST / MuSig2 /
  something else), intermediate signing key TTLs, rotation
  protocol, manifest caching policy and grace periods, revocation
  propagation latency targets, emergency upgrade channel.
- **ADR-025:** Formal threat model. Adversary capabilities, impact
  per threat, mitigations from 022/023/024, residual risks,
  detection strategy. This ADR identifies threats in §"Negative"
  bullets but does not formalize them.
- **Separately from ADR chain:** cross-context data references
  (explicit consent protocol for personal → org data movement),
  mentioned in ADR-020, still deferred.

## Decisions Confirmed With Ben (2026-04-15)

1. **Authority model — hybrid, multi-sector.** Confirmed as
   drafted in §1: self-sovereign root with optional anchoring
   across edu / gov / industry sectors. Export-control and
   eventual classified support are day-1 design constraints
   (non-foreclosure), not deferred capabilities. The drafted §1
   text above reflects the broader scope.
2. **ADR-025 scope — standalone.** Formal threat model lives in
   its own ADR, cross-referenced from 022/023/024.
3. **Data-model migration — phased, with mandatory end-to-end
   value in phase 1.** Ben has been burned by phased plans where
   the critical late phase never ships. The phasing below is
   designed so phase 1 delivers working federation value on its
   own — no "plumbing first, feature later." If phase 2 or 3
   slips, phase 1 is still a shippable improvement.

## Data-Model Migration Plan (Phased, Phase-1-Valuable)

**Phase 1 — Add `Membership` as a first-class registry (one release).**

- Introduce `Membership` storage (`~/.axi/memberships.yaml`).
- Add `axi federation list-memberships`, `axi federation renew`,
  `axi federation leave` commands.
- `KnownNode` gains a read-only `memberships: list[Membership]`
  view computed from the new registry; its existing state field
  remains for backward compatibility.
- **Ships working value on its own:** users can manage multiple
  concurrent federation memberships with TTLs and renewals. Every
  other capability downstream (manifests, delta sync, root
  outage graceful degradation) depends on this registry existing,
  but phase 1 alone is shippable and useful.

**Phase 2 — Split `Identity` and `Reachability` (one release).**

- Create `Identity` and `Reachability` registries, populate from
  existing `KnownNode` entries.
- `KnownNode` becomes a pure view joining Identity +
  best-Reachability + relevant Memberships.
- **Ships working value:** identity rotation no longer touches
  membership state; operators can add multiple reachabilities
  per identity (e.g. example-host-via-ssh AND example-host-via-A2A).
- If phase 3 slips indefinitely, phases 1+2 together still
  deliver the intended federation model minus the deprecation
  cleanup.

**Phase 3 — Deprecate `KnownNode.state`, remove the backward-
compat shim (one release, on a sufficient adoption runway).**

- `KnownNode.state` is removed; callers that still read it get
  a clear error with migration guidance.
- **Value:** cleaner internal model, reduced state-space for
  bugs. This phase is *polish*, not *function*. Designed to be
  omittable.

**Phasing risk audit:**
- Phase 1 is **independently valuable** ✅
- Phases 1+2 together deliver **all intended federation semantics** ✅
- Phase 3 is **polish; slippage does not harm users** ✅

This matches the "deliver value per phase" rule. The design does
NOT hide the payoff in a late phase.

## Related Documents

- `adr-016-multi-node-federation.md` — foundational federation
  architecture; §8 (topology) and §9 (trust bootstrap) are
  referenced throughout.
- `adr-020-federation-identity-and-relationships.md` — identity
  layers (Platform / Node / Affiliation / Context) that this ADR
  refines into the three-registry data model.
- `adr-021-federation-threat-model.md` — precedent for ADR-025.
- `prd-federation.md` — §17 (Installation and Upgrade Across the
  Federation) enumerates the install/upgrade scenarios this data
  model must support.
- `spec-federation.md` — technical specification; will be updated
  with the concrete registry schemas once this ADR is accepted.
- `spec-security.md` — Trust Model section (new) documents the
  deterministic-vs-model-mediated boundary referenced here.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
