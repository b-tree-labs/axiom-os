# ADR-024: Federation Root Availability, Delegation, and Key Hygiene

**Status:** Proposed
**Date:** 2026-04-15
**Authors:** Benjamin Booth, Claude
**Related:** ADR-016 (multi-node federation), ADR-022 (identity roots + membership separation), ADR-023 (topology, lifecycle, propagation), planned ADR-025 (threat model), `spec-classification-boundary.md`.

---

## Context

ADR-022 established self-sovereign root identities and made
membership a TTL'd signed artifact. ADR-023 set the topologies
and propagation protocols. What remains: ensuring the federation
keeps functioning when the entity holding the root key is
unavailable, compromised, or simply rotating keys on schedule.

At the scale target (10k–100k nodes across universities, agencies,
consortiums) the failure modes we must tolerate are concrete:

- A federation root node goes offline for hours-to-days (hardware
  failure, network partition, operational outage, maintenance
  window). 50,000 nodes in the federation cannot be sitting on
  their hands until it's back.
- A root key is suspected compromised. Revocation must
  propagate quickly; recovery must not require every member to
  perform a manual re-ceremony.
- Scheduled key rotation (annual, per policy). Operators must be
  able to rotate without federation-wide re-bootstrap.
- A critical CVE discovered in Axiom itself; a patched version
  must reach 10k nodes without every node doing a synchronous
  upgrade dance.

This ADR decides the availability and key-hygiene mechanisms.
Per Ben's "deliver value per phase" rule, each phase must ship
a working improvement on its own.

---

## Decisions

### 1. Threshold-Signed Federation Roots (k-of-n) by Default for Institutional Federations

**Choice: FROST (Flexible Round-Optimised Schnorr Threshold
signatures) over MuSig2 or Shamir-split secrets.**

A federation root is not a single key held by a single node. It
is a **threshold quorum** of `n` signing parties, any `k` of
whom can produce a valid root signature. The federation
publishes a single public key; signing operations happen
collaboratively among the quorum.

**Why FROST:**
- Produces a standard Ed25519-compatible Schnorr signature —
  downstream verifiers don't need to know it was threshold-signed.
  Every existing ADR-022 verification path continues to work
  without change.
- Two-round protocol with known security proofs; well-studied
  cryptography with multiple production implementations.
- Supports dynamic quorum membership (can add/remove signers
  without re-keying) — load-bearing when a signing party leaves
  the organization or is compromised.
- Works offline between rounds — signers don't need to be
  simultaneously online to produce a signature (preprocessing
  phase can be done hours or days in advance).

**Why not MuSig2:** similar properties for n-of-n signing, but
poor fit for threshold (k < n) case; less flexible for dynamic
quorum membership.

**Why not Shamir-split single secret:** reconstructing the
secret even momentarily in one place is the compromise surface
we're avoiding. Threshold signing means the secret never exists
reassembled.

**Default threshold:** `k = ceil(2n/3)` where `n` is the quorum
size. At n=3 that's k=2 (2-of-3); at n=5 it's k=4 (4-of-5);
at n=9 it's k=6 (6-of-9). Stricter than simple majority to
resist network partition attacks; looser than unanimous to
tolerate one or two signers offline.

**Small federations can opt out:** federations with <10 nodes
may use a single root key if operators explicitly choose.
Default for institutional, long-running, and cross-sector
federations is threshold. Ephemeral classroom federations
typically use single-key (the class instructor is the
authority).

### 2. Intermediate Signing Keys with Short TTL

Day-to-day federation operations (membership manifest updates,
probation promotions, member admission) do NOT require the root
quorum to sign every action. The root signs **intermediate
signing keys** with a short TTL; intermediates sign ordinary
operations.

```
Root (threshold quorum) →  signs Intermediate (TTL: 7 days)  → signs Operations
                        ↓
                        Intermediate rotates; root re-signs with a new one
```

**Why:** root quorum ceremonies are expensive. Requiring k-of-n
signers to coordinate for every membership change does not
scale. Intermediate keys with 7-day TTL mean the quorum
convenes roughly weekly; daily operations happen unilaterally
under an intermediate.

**Compromise containment:** an intermediate that is leaked has
at most a 7-day window to do damage. Subsequent operations
require a fresh intermediate that the compromised key cannot
produce.

**Verification chain:** every operation signed by intermediate
X carries X's signature plus root's signature on X plus X's
not-expired-at timestamp. Peers verify the chain back to root
and refuse if the intermediate is past its expiry.

**Rotation cadence:** intermediates rotate every 7 days by
default; configurable per federation. Emergency rotation (e.g.
suspected intermediate compromise) is immediate — operators
invoke the root quorum, revoke the intermediate, issue a new
one.

### 3. Membership Manifest Caching with Grace-Period TTL

A federation's root publishes a membership manifest signed by
its current intermediate key. The manifest includes:

- Federation spec (topology, lifecycle, domain — from ADR-023)
- List of active members with per-member probation/active
  status
- Monotonic sequence number
- `issued_at` + `expires_at` (default gap: 7 days)
- `soft_expire_at` (default: 24h before `expires_at`)

**Grace period semantics:** peers accept a manifest after
`soft_expire_at` but emit a warning. Past `expires_at`, the
manifest is refused — operators must pull a fresher one. The
grace window exists to tolerate short root outages without
cascading federation failure.

**Root outage graceful degradation:** if the root quorum is
offline for a day, members continue operating on the
most-recently-accepted manifest until the soft expiry, with
warning logs. If the outage extends beyond the hard expiry (7d
by default), members enter a degraded-federation state: no new
members can be admitted, no probation→active transitions, but
existing members' memberships remain valid for the duration of
their own record's `expires_at`.

**Sequence replay:** every manifest carries a monotonic sequence
number. A peer that has accepted sequence N refuses any
manifest with sequence ≤N (replay of an older, attacker-served
manifest). Combined with expiry, this gives us replay + rollback
resistance.

**Delta sync:** manifests are content-addressed (hash included).
When a peer holds manifest seq=N and the root publishes seq=N+k,
the peer pulls only the delta (new/removed members, status
changes) rather than the full list. At 50k-member scale, delta
sync is a 100x–1000x bandwidth improvement over full-list pull.

### 4. Revocation Propagation — Signed Revocation Channel

Key revocation (compromised intermediate, kicked member, retired
node) uses a **revocation channel** distinct from the manifest
channel:

- Revocations are signed by a root-quorum intermediate (or the
  root quorum directly, for emergency revocation of an
  intermediate).
- Revocation records carry a monotonic sequence number and a
  short TTL (default: 90 days) — forcing periodic re-issuance
  so revocation lists don't grow without bound.
- Peers check the revocation channel during every manifest sync
  and on every peer verification attempt. Revoked principals
  are refused regardless of what the current manifest says.

**Propagation latency targets:**
- Intra-cluster (peers gossip within 10s): revocation visible to
  all cluster members within 30s.
- Intra-hierarchy: revocation propagates down the tree within
  one heartbeat cycle (default: 60s). Upper-bound: 5 minutes
  across a deep hierarchy.
- Cross-bridge: revocation is pull-based at bridge refresh
  cadence (default: daily). For urgent revocations, operators
  on both sides of a bridge can explicitly push.

These are **target** latencies; ADR-025 will formalize the
threat model including attacks that widen the revocation window.

### 5. Root Key Rotation Without Federation-Wide Re-Bootstrap

Root quorum rotation is an operator action, not a protocol
event. The ceremony:

1. Current root quorum signs an **attestation** binding the new
   root public key to the existing federation.
2. New root quorum's first act is to sign the same attestation
   in reverse — old → new AND new → old signatures exist.
3. Both old and new root public keys are valid for a **hand-off
   window** (default: 30 days). Manifests and intermediates
   may be signed by either during this window.
4. After the hand-off window, the old root is retired; manifests
   signed by the old root after retirement are refused.
5. Operators update the federation spec to reference the new
   root's public key. Peers who have cached the old federation
   spec pick up the new one on next manifest sync (the manifest
   contains the current root ref).

**Why both-direction signatures:** prevents an attacker who
compromises the old root from issuing a fake "new root" that
replaces the legitimate quorum. New root's signature on
attestation is required for peers to trust it.

**Signer rotation within the quorum** (one signer leaves, one
joins) uses FROST's dynamic-membership support: a fresh DKG
(distributed key generation) produces a new shares without
changing the public key. No hand-off window required; operations
continue uninterrupted.

### 6. Emergency Upgrade Channel

A critical Axiom CVE requires fast patching at 10k+-node scale.
Ordinary `axi update` flow is fine for non-urgent releases; for
emergency releases, the federation provides a guarded fast-path.

**Mechanism:**
- The Axiom release pipeline signs emergency releases with a
  separate **emergency-channel** signing key (analogous to a
  Chromium-style canary channel but severity-gated).
- The federation root publishes a **security directive** as part
  of its manifest: "nodes running < axi X.Y.Z are
  federation-untrusted as of date T."
- Nodes automatically pull and install the emergency release
  when they see a security directive from their federation
  root, subject to operator policy:
  - `emergency_upgrade_policy: auto` — install on receipt.
  - `emergency_upgrade_policy: notify` — alert operator; require
    manual approval.
  - `emergency_upgrade_policy: manual` — no automation; operator
    handles.
- Default is `notify`. Deployments prioritizing availability
  over stability (class labs, consumer dev) can choose `auto`.
  Regulated / air-gapped deployments must use `manual` (air-gap
  precludes auto anyway).

**Federation membership consequences:** after the directive's
effective date, nodes on the vulnerable version are marked
untrusted in the manifest. Peers refuse to verify their
messages. This is both protective (contains blast radius) and
motivating (untrusted nodes can't participate).

**Bonsai LM narration:** per `spec-security.md §2.4`, Bonsai
narrates emergency-directive UX — "critical federation security
update available; [diff], [impact], [recommended action]". The
underlying deterministic action (version check, signature
verify, install) never depends on Bonsai being available.

### 7. Air-Gapped Deployments

For air-gapped classified federations (per
`spec-classification-boundary.md §S8`):

- Root quorum and intermediate signing happen entirely within
  the air-gap.
- Manifest transfer to/from an air-gapped federation is via
  controlled media (signed bundles carried across).
- Revocation channel exists but its latency target widens
  considerably (hours to days, not seconds to minutes).
- Emergency upgrade requires physical bundle delivery; the
  directive mechanism still works but relies on operator
  action rather than auto-pull.

Air-gap is NOT a degraded mode — it is a first-class deployment
mode with explicit different expectations around propagation
latency and operator involvement.

---

## Phasing (Value Per Phase)

Per the "deliver value per phase" rule — each phase is
independently valuable.

**Phase 1 (near): single-key root + intermediate + manifest TTL.**
Ships the intermediate signing model and grace-period manifest
caching. Threshold signing deferred. **Single-key root is
enough** for classroom/departmental federations today. The
intermediate + manifest TTL alone provides most of the
availability benefit: a root outage doesn't stop federation
operation for up to 7 days. This is shippable on its own.

**Phase 2: threshold root (FROST) + dynamic quorum membership.**
Enables institutional, long-running, cross-sector federations.
Backfills the existing single-key root deployments as an
operator-initiated upgrade (attestation ceremony: current
single-key signs the DKG public key; thenceforth quorum signs).
The manifest and intermediate formats don't change.

**Phase 3: revocation channel formalized; cross-bridge
propagation.** Revocation exists as part of intermediate
management today but is ad-hoc; Phase 3 gives it a formal
schema, sequence numbers, and measured propagation latency.

**Phase 4: emergency upgrade channel.** Requires the release
pipeline (ADR-017) to sign emergency releases with a separate
key and the federation root to publish security directives.
Last because it's the most operational and requires the
release infrastructure to be mature.

**Phasing risk audit:**
- Phase 1 is independently valuable (graceful root outage via
  intermediate+TTL).
- Phase 1+2 gives you production-grade institutional federation.
- Phase 3 is security-hardening polish; value is measurable but
  incremental.
- Phase 4 is operational — only matters after you have a live
  federation that needs patching.

Each phase survives as a shippable unit. None of the later
phases is load-bearing for the earlier phases' usefulness.

---

## Data Model Additions (beyond ADR-022 + ADR-023)

```python
@dataclass(frozen=True)
class IntermediateKey:
    key_id: str
    public_key: bytes
    federation_id: str
    issued_by_root: bytes           # threshold sig (or single root sig)
    issued_at: str
    expires_at: str
    revoked_at: Optional[str]

@dataclass(frozen=True)
class RevocationRecord:
    federation_id: str
    sequence: int
    target_type: Literal["intermediate", "member", "node"]
    target_id: str
    reason: str
    effective_at: str
    expires_at: str                  # TTL on the revocation itself
    signed_by_intermediate: bytes

@dataclass(frozen=True)
class SecurityDirective:
    federation_id: str
    sequence: int
    minimum_version: str             # "0.11.3"
    rationale: str                   # human-readable CVE ref
    effective_at: str
    expires_at: str
    signed_by_intermediate: bytes
```

---

## Consequences

**Positive:**
- A 7-day root outage doesn't cascade to 10k member nodes —
  graceful degradation via manifest TTL.
- Threshold root (Phase 2+) eliminates single-key compromise as
  a federation-wide vulnerability.
- Intermediate rotation contains intermediate-key compromise to
  a 7-day window.
- Emergency upgrade channel lets a critical patch reach every
  node in minutes (on `auto` policy) without operators doing
  synchronous work.
- Root rotation is a routine operator action, not a
  federation-wide re-bootstrap.
- Every action is replay- and rollback-protected via monotonic
  sequence numbers + expiries.

**Negative:**
- FROST implementation adds a non-trivial dependency (Python
  FROST libraries exist but are less mature than raw Ed25519).
- Intermediate management adds an operational surface — quorums
  must meet weekly to rotate intermediates; if a federation's
  quorum cannot, the federation degrades.
- Emergency upgrade channel creates an automatic code-execution
  path (install + run new version) — requires careful scoping
  of what a security directive can compel.
- Three signing key types (root quorum, intermediate, member)
  instead of one adds reasoning complexity for operators and
  for debugging.

**Neutral:**
- Air-gapped deployments get different latency SLAs for
  revocation and emergency upgrade but the same semantic
  model.
- The emergency upgrade channel is **opt-in to `auto`**; more
  conservative deployments use `notify` or `manual`.

---

## Open Questions (Deferred)

- **Specific FROST library choice** (several candidates —
  `frost-ed25519`, `frost-secp256k1` with Ed25519 compat layer,
  custom impl) — implementation-phase decision, not this ADR.
- **Security directive granularity** — should a directive be
  able to compel anything beyond version bump? (e.g. config
  change, extension disable). Proposal: no — too powerful; a
  directive only compels version. Broader operational changes
  go through ordinary operator workflow.
- **Cross-bridge intermediate trust** — does a bridge trust the
  other side's intermediate, or does it need its own bilateral
  intermediate per bridge? Deferred to Phase 3.
- **ADR-025 will cover:** formal threat model including key
  compromise recovery, split-brain during root outage,
  directive-channel abuse.

---

## Decisions Pending Ben Review

1. **FROST over MuSig2** for threshold signing — happy to switch
   if you have a preferred scheme.
2. **7-day intermediate TTL** — aggressive shorter (24h) or
   relaxed (30d) as alternative defaults?
3. **Emergency upgrade default = `notify`** (vs `auto` or
   `manual`) — classroom labs might want `auto`, regulated
   deployments definitely `manual`.
4. **Phase 1 scope as defined** — single-key root + intermediate
   + manifest TTL. Big enough to be valuable; small enough to
   ship standalone.

---

## Related Documents

- ADR-016 — federation architecture foundations.
- ADR-022 — identity roots, data model, authority model.
- ADR-023 — topology and propagation; this ADR's manifest
  semantics refine ADR-023's manifest mechanics.
- ADR-025 (planned) — formal threat model; covers adversarial
  scenarios against this ADR's mechanisms.
- `spec-classification-boundary.md` — air-gap operating mode
  requirements this ADR satisfies.
- `spec-security.md §2` — deterministic-vs-model-mediated
  framework; Bonsai narration of upgrade directives lives on
  model-mediated side.
- ADR-017 — release pipeline, supply chain integrity; emergency
  upgrade channel is a consumer of ADR-017's signing
  infrastructure.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
