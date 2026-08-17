# ADR-023: Federation Topology, Lifecycle, and Propagation

**Status:** Proposed
**Date:** 2026-04-15
**Authors:** Benjamin Booth, Claude
**Related:** ADR-016 (multi-node federation), ADR-020 (identity layers), ADR-022 (identity roots + membership separation), planned ADR-024 (availability + delegation), planned ADR-025 (threat model), `spec-classification-boundary.md` (classification + export-control), `spec-federation.md` (wire spec).

---

## Context

ADR-022 settled the *data model* (Identity / Reachability /
Membership) and the *authority model* (hybrid self-sovereign with
multi-sector anchoring). What ADR-022 deferred to this ADR:

- The **shape** a federation can take (flat mesh, hub-and-spoke,
  hierarchical, cross-sector bridge) and when each applies.
- The **lifecycle** a federation goes through (ephemeral class
  period, long-running institutional, open public research) and
  how membership behaves differently per type.
- The **propagation protocol** that lets federations scale:
  gossip inside a cluster, aggregated heartbeats upstream,
  pull-based delta sync across bridges.
- The **handshake sequence** for admitting a new member,
  including multi-source attestation to resist eclipse attacks
  at join time.

Scale target (from `project_federation_scale_target`): 10k–100k
nodes across universities, agencies, industry consortiums, and
cross-sector bridges, with few known scale or security
weakpoints. Anything O(n²) is an outage. Any single-root failure
that cascades to the federated branch is an outage.

Cross-sector rules from ADR-022 apply globally in this ADR: a
bridge between two federations carries the **more restrictive**
side's constraints. Classification + export-control rules from
`spec-classification-boundary.md` compose with everything here.

---

## Decisions

### 1. Four Topologies, Each With Explicit Scale Regime

| Topology | Node count | When it applies | Primary cost | Primary benefit |
|----------|------------|-----------------|--------------|-----------------|
| **Flat mesh** | 2–10 | Small collaborations, dev/test, small classes | O(n²) gossip | Zero coordinator overhead; simplest trust model |
| **Hub-and-spoke** | 10–1,000 | Single-institution federation, departmental/classroom, small consortium | Hub is SPoF without §3 availability protections | O(1) resource discovery; single trust anchor |
| **Hierarchical** | 1,000–100,000 | Multi-institution, multi-department, multi-cohort | Propagation latency along branches | Scales indefinitely; natural organizational alignment |
| **Cross-sector bridge** | Any size | Bilateral/multilateral partnerships across sectors (edu ↔ gov ↔ industry) | Bridge is a chokepoint (mitigated by multi-bridge) | Enables federation where common identity authority doesn't exist |

**Transition rule:** a federation declares its topology at
creation time. When a threshold is crossed (default: 10 nodes for
flat→hub, 1,000 for hub→hierarchical, explicit for bridges),
operators receive a recommendation via the validated-
classification pattern (`spec-security.md §2.6`) — the LM observes
that the federation has outgrown its declared topology and
proposes an upgrade. Operator-approved; never automatic.

### 2. Three Lifecycle Types, Each With Different Membership Rules

| Lifecycle | Example | Membership TTL | Renewal | End-of-life |
|-----------|---------|----------------|---------|-------------|
| **Ephemeral** | Class period, research sprint, task force | Hours to months (TTL declared at federation creation) | Not typically renewed — expires at the term | All memberships auto-expire at TTL; federation archived; knowledge promoted per alumni policy (see `prd-alumni.md`) |
| **Long-running** | Institutional federation, bilateral partnership, consortium | Years to indefinite | Renewed periodically; renewal cadence is federation policy | Federation dissolves via operator action or loss of admitter principals; graceful unwind |
| **Open** | Public research federation, open-source consortium | Per-member TTL, renewable | Self-service renewal; admission is lighter-touch | No end-of-life at federation level; individual members join/leave freely |

**Membership lifecycle per node:**
```
PROBATION (new-peer) → ACTIVE → (RENEWAL_DUE → ACTIVE | EXPIRED) → REVOKED | EXPIRED
```

Probation is ADR-020's quarantine concept made concrete: new
members operate read-only / low-privilege for a configurable
window (default: 1 week) before promotion to ACTIVE. This is the
deterministic gate for eclipse resistance — even a successful
Sybil join cannot immediately poison the federation.

### 3. Propagation Protocol

Three distinct propagation mechanisms, each matched to a scale
regime:

**Intra-cluster (flat mesh, within a hub-spoke hub's peers):
gossip.**
Peers exchange membership-state deltas every 10 seconds.
O(n²) traffic is acceptable at n ≤ 10; above that, gossip is
constrained to a peer's immediate cluster.

**Intra-hierarchy: aggregated heartbeats upstream, signed
manifest pulls downstream.**
- Each node heartbeats to its parent every 10 seconds with a
  digest of its own state + a summary of children's states.
- Parents maintain a signed membership manifest with monotonic
  sequence numbers (ADR-022 §4).
- Children pull manifest deltas from parent on a longer cadence
  (default: 60 seconds) or on-demand when a lookup requires
  fresher data.
- Net traffic is O(log n) per node regardless of federation
  size.

**Cross-bridge: pull-based delta sync with explicit quiescence.**
- Bridges do NOT auto-propagate. A bridge node on side A holds a
  signed read-only mirror of side B's membership manifest,
  refreshed on policy (default: daily; on-demand during
  cross-bridge operations).
- The bridge node enforces the more-restrictive side's rules
  (export control, classification domain, access gates)
  deterministically on every cross-side request.
- If the bridge goes down, in-flight cross-side operations fail
  cleanly (explicit error) rather than silently succeeding with
  stale data.

**All manifest exchanges are cryptographically replay-
protected.** Monotonic sequence numbers + expiry + content-
addressed hashes. A peer refuses a manifest older than one it's
already accepted. A peer refuses a manifest past its expiry even
if it's the latest one seen (forces renewal).

### 4. Handshake: Multi-Source Attestation at Join

Joining a federation — especially for a new peer with no
bilateral history — is the highest-value moment for eclipse
attacks. An attacker controlling all the peers visible to a
joining node can present a false view of the federation.

**Mitigation: multi-source attestation.** A valid join requires
**three independent signatures** converging on the same federation
state:

1. **Invite-giver signature** — the existing member who invited
   the new node (ADR-016 §9).
2. **Federation root signature** — the federation's root key
   (or threshold quorum per ADR-024) attesting to the invite's
   validity and the current membership manifest hash.
3. **At least one additional member signature** — a third
   member, chosen by the joining node (not by the inviter), who
   attests to the federation's membership state.

A joining node verifies that all three signatures converge on
the same manifest sequence number and hash. Divergence is
diagnostic of an eclipse attempt and causes the join to abort
with a loud error (not a silent retry).

Joins in flat mesh topologies (≤ 10 nodes, bilateral trust)
can relax to two-source (invite-giver + self-attesting new
peer), because attacker-controlled membership becomes
operationally visible in a small set.

### 5. Topology + Classification Composition

When topology and classification interact, classification wins:

- A **cross-sector bridge** between an unclass and a classified
  federation is NOT a topology choice — it's a cross-domain
  transfer boundary (`spec-classification-boundary.md §S4/S5`).
  Ordinary cross-bridge propagation does not apply.
- A **hierarchical** federation spanning classification levels is
  not permitted. Each classification level operates as its own
  hierarchy; up-transfer and down-transfer are explicit,
  audited, human-gated operations across disconnected trees.
- A **hub-and-spoke** can operate at a single classification
  level; the hub inherits the highest classification its spokes
  hold.
- Cross-sector bridges between edu and gov where one side is
  export-restricted: the bridge enforces export regime
  deterministically on every transit, irrespective of membership
  status (`spec-classification-boundary.md §S6/S7`).

### 6. Node Profile via Validated Classification

Node profile (leaf / standard / provider / coordinator) is
*declared* at registration and *validated* over time per the
canonical [hybrid] pattern (`spec-security.md §2.6`). The
deterministic routing rule reads the validated profile; the
declaration is starting data only.

Observed signals for profile validation:
- Resource availability (CPU, GPU, RAM) over sustained windows
- Response latency and uptime (provider and coordinator
  profiles require durable liveness)
- Agent-call-volume served (providers host agents reachable by
  others)
- Corpus contribution rate (providers contribute to community
  corpora)

Confidence thresholds and proposed transitions are advisory;
operator approval (or explicit federation policy) is the
deterministic gate for profile change.

### 7. Ephemeral Federation Archival

When an ephemeral federation hits its TTL, its membership
manifest is sealed, cryptographically signed by the federation
root (or threshold quorum), and archived. Knowledge promoted
during the federation's life follows the alumni model
(`prd-alumni.md`, `project_classroom_alumni` memory) — cohort
members retain read access to their contributions; aggregated
insights optionally promote to long-running federations per
operator policy.

Archive is NOT deletion. The manifest, audit chain, and promoted
knowledge packs persist. Expired memberships simply lose write
privileges.

### 8. Operator-Visible Federation State

Every node publishes its view of every federation it's a member
of via `axi federation status --verbose`:

- Federation ID + topology + lifecycle type + domain
  classification
- This node's role (leaf / standard / provider / coordinator)
- Membership: active / probation / expired with TTL
- Latest accepted manifest sequence number + content hash
- Peer counts by profile
- Recent propagation latency summary (for hierarchical and
  bridge topologies)
- Any outstanding validated-classification advisory nudges

---

## Data Model Additions (beyond ADR-022)

ADR-022 defined `Identity`, `Reachability`, `Membership`. This
ADR adds:

```python
@dataclass(frozen=True)
class FederationSpec:
    id: str
    topology: Literal["flat", "hub_spoke", "hierarchical", "bridge"]
    lifecycle: Literal["ephemeral", "long_running", "open"]
    domain: str               # "unclassified" | "cui" | "secret" | ...
    compartments: list[str]
    export_regime: ExportRegimeSpec
    parent_federation: Optional[str]   # for hierarchical
    bridge_peers: list[str]   # federation_ids on other side(s)
    created_at: str
    expires_at: Optional[str]
    renewal_cadence: Optional[str]
    root_authority: str       # identity_ref of federation root (or quorum ref per ADR-024)
    probation_window_days: int

@dataclass(frozen=True)
class Manifest:
    federation_id: str
    sequence: int             # monotonic; replay protection
    content_hash: str         # content-addressed; enables delta sync
    members: list[str]        # identity_refs currently ACTIVE
    probation: list[str]      # identity_refs currently in probation
    issued_at: str
    expires_at: str
    root_signature: bytes
```

---

## Consequences

**Positive:**
- Topology choice is explicit, scale-appropriate, and operators
  are nudged toward upgrades when the declared topology outgrows
  reality.
- Lifecycle types separate the short-term and long-term
  membership semantics that tangled together in earlier designs.
- Propagation costs are bounded: O(log n) per node for
  hierarchy, acceptable constant-per-peer for flat mesh,
  explicit refresh cadence for bridges.
- Multi-source attestation at join resists eclipse at the
  highest-value moment.
- Classification composition is explicit — no hidden
  topology-vs-classification interactions.

**Negative:**
- More federation configuration surface (topology type,
  lifecycle type, probation window, renewal cadence, bridge
  policies). Operators must know what to pick.
- Hierarchical propagation introduces latency along branches;
  cross-branch lookups may traverse several hops.
- Cross-bridge sync cadence is a tradeoff between freshness and
  cost; wrong choice leads to stale manifests or bandwidth
  waste.
- Validated-classification advisories require ongoing LM cycles
  per federation — additional operational cost.

**Neutral:**
- This ADR does NOT decide root availability (threshold signing,
  intermediate keys, manifest caching policy) — that's ADR-024.
- Formal threat model enumeration is ADR-025.

---

## Open Questions (Deferred)

- **ADR-024:** threshold-signing scheme (FROST / MuSig2 / other),
  intermediate signing key TTLs, revocation propagation latency
  targets, emergency upgrade channel.
- **ADR-025:** formal threat model including eclipse, Sybil,
  replay, DoS, topology-level attacks.
- **Separately:** cross-bridge routing protocol details —
  authentication on every transit, caching policies, bandwidth
  caps.
- **Separately:** ephemeral federation declassification / archive
  export format — alignment with FOIA and institutional records
  requirements.

---

## Decisions Pending Ben Review

This ADR proceeds with defaults. Please confirm or redirect:

1. **Default thresholds** (flat→hub at 10, hub→hierarchical at
   1,000). These are conservative; reasonable objections include
   "hub scales further than that" (up to ~1,000 if the hub is a
   well-resourced provider) or "hierarchical should start
   sooner" (at a few hundred to minimize gossip cost).
2. **Probation window** (1 week default). Tradeoff: shorter
   windows reduce friction for legitimate members; longer
   windows reduce Sybil/eclipse exposure.
3. **Manifest refresh cadence** (60s hierarchical pull, 24h
   bridge pull). These should be policy knobs per federation;
   defaults here.
4. **Multi-source attestation requirement** (3 sigs outside flat
   mesh, 2 in flat mesh). Could argue for 3 everywhere
   (stricter) or scaling with federation size.

---

## Related Documents

- ADR-016 — federation architecture, original topology + trust
  bootstrap discussion.
- ADR-020 — identity layers (platform, node, affiliation,
  context).
- ADR-022 — data model (Identity / Reachability / Membership),
  authority model, migration phasing.
- `spec-classification-boundary.md` — classification, export
  control, cross-sector composition rules.
- `spec-federation.md` — wire-level specification (to be
  updated with propagation protocol once this ADR is accepted).
- `spec-security.md §2` — deterministic / model-mediated
  framework and validated-classification canonical pattern.
- `prd-federation.md §17` — install/upgrade scenarios the
  topology must handle.
- `prd-alumni.md` — ephemeral federation archival knowledge
  promotion.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
