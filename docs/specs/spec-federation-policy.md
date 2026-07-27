# Federation Policy — Visibility, Classification, Trust, Gateway

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-04-25
**Scope:** Axiom framework — federation policy primitives that gate fragment outflow + inflow across cohorts and trust boundaries.
**Related:**
- ADR-027 (federated memory — addressing + propagation mechanics)
- ADR-028 (trust graph — peer trust relationships)
- ADR-029 (federation composition — meta-rules)
- ADR-033 (layered memory architecture — where these primitives sit)
- `spec-classification-boundary.md` (classification + export-control regimes)

---

## 1. Purpose

ADR-027 closed the *mechanics* layer of federation: addressing (`axiom://`), the cohort-sharded registry, propagation modes, failover. It did not specify **what content is allowed to flow** to which peers.

ADR-033 introduces a four-layer memory architecture that needs that policy surface to be uniform across every Axiom extension. This spec is the canonical reference for the four federation-policy primitives those layers consume:

1. **`VisibilityHorizon`** — per-fragment outflow intent set by the writer.
2. **`ClassificationStamp`** — per-fragment regulatory constraint (defined in `spec-classification-boundary.md`; consumed here).
3. **`TrustProfile`** — per-scope statement of which peers + horizons we accept.
4. **`FederationGateway`** — the runtime that composes the above when projecting to a peer or accepting from one.

Together these primitives answer "given this fragment, this scope, this peer, this requested projection — does any content flow, and what shape does it take?"

---

## 2. Why this is a separate spec

Three concerns kept getting tangled in earlier docs:

- **Mechanics** (ADR-027) — how to reach the right peers, how propagation auto-scales, how the registry survives a coordinator outage.
- **Trust** (ADR-028) — which peers we have a relationship with, what kind, how it evolves.
- **Policy** (this spec) — given that we *can* reach a peer and we *trust* them at some level, what content is *allowed* to flow.

ADR-027 does not gate content. ADR-028 does not stamp content. The policy primitives below sit between them and consume both: the gateway uses ADR-027 to find peers and ADR-028 to evaluate trust, then applies the rules here to decide outflow.

This spec also resolves the per-fragment vs per-cohort tension. ADR-027 propagates by cohort. ADR-028 trusts by peer. The fragment-level decision (does THIS fragment go to THIS peer?) was implicit; this spec makes it explicit and uniform.

---

## 3. `VisibilityHorizon` — writer's outflow intent

A per-fragment enum naming **how far this fragment is allowed to travel from its origin scope**, expressed independent of any extension's vocabulary:

```python
class VisibilityHorizon(Enum):
    SCOPE_INTERNAL    = "scope_internal"
    REQUEST_GATED     = "request_gated"
    PEERS_DECLARED    = "peers_declared"
    FEDERATION_BOUND  = "federation_bound"
    PUBLIC            = "public"
```

### Semantics

| Horizon | Outflow rule | Discovery rule | Typical use |
|---|---|---|---|
| `SCOPE_INTERNAL` | Never leaves origin scope. | Not advertised to any peer. | Personal drafts, conversation-only state, in-progress work, password vault content. |
| `REQUEST_GATED` | Peers may fetch by explicit reference; never sent unsolicited. | Not advertised; reference must come through an out-of-band channel (e.g., a citation in another fragment). | Pre-publication research findings, drafts under review, classified-pending-declassification content. |
| `PEERS_DECLARED` | Flows to peers present in the origin scope's `TrustProfile.declared_peers`. | Concept-level metadata advertised to declared peers; full content on their request. | Curriculum shared with partner classrooms, methodology shared with partner labs, agent-personality facts shared with sibling agents. |
| `FEDERATION_BOUND` | Flows through the trust graph; hop-bounded by `TrustProfile.federation_max_hops` (default 1, max 2). | Concept-level metadata propagates with the trust graph. | Cross-cohort benchmarks, federated knowledge bases, public-curated artifacts within a federation. |
| `PUBLIC` | Discoverable to any reachable node. | Listed in any cross-federation discovery index the origin scope opts into. | Published OER materials, published papers, public knowledge corpus. |

### Default

Every fragment defaults to `SCOPE_INTERNAL` unless the extension's manifest declares a stronger default for the fragment type. Default-deny is the design posture: if the extension author doesn't think about visibility, the fragment doesn't leak.

### Extension specialization

Extensions specialize the abstract horizon in their own vocabulary. The horizon is the source of truth; the alias is the affordance:

| Extension | `SCOPE_INTERNAL` alias | `PEERS_DECLARED` alias | `PUBLIC` alias |
|---|---|---|---|
| classroom | `cohort-private` | `cohort-shared` | `public-curated` |
| research / CURIO | `pre-publication` | `team-shared` | `published` |
| chat / agent | `conversation-only` | `agent-shared` | `community-knowledge` |
| domain consumer | `enclave-only` | `partner-facility-shared` | `public-literature` |

Extensions register their alias map in `axiom-extension.toml`:

```toml
[extension.visibility_aliases]
cohort-private  = "scope_internal"
cohort-shared   = "peers_declared"
public-curated  = "public"
```

The CLI surface accepts either the abstract horizon or the alias; the underlying field on `MemoryFragment` is always the abstract enum.

### Immutability + change procedure

`MemoryFragment.visibility` is immutable. Promoting a fragment from `SCOPE_INTERNAL` to `PEERS_DECLARED` (e.g., research finding moves from pre-publication to team-shared) is performed by writing a new `VisibilityPromoted` event referencing the original; the original fragment is not mutated. The federation gateway uses the latest promotion event when computing outflow.

Demotion (e.g., accidental `PUBLIC` retracted to `SCOPE_INTERNAL`) writes a `VisibilityDemoted` event and additionally writes tombstones for any propagation that occurred under the previous horizon. Demotion does not unsend already-fetched content from peers — that requires a separate forget request to each peer that pulled it (best-effort; documented as a known limitation).

---

## 4. `ClassificationStamp` — regulatory constraint

The stamp is fully specified in `spec-classification-boundary.md §2.1`. This spec consumes it as the second per-fragment input to the gateway. Recap of the relevant fields:

```python
@dataclass(frozen=True)
class ClassificationStamp:
    level: str                              # "unclassified" | "cui" | "secret" | ...
    compartments: frozenset[str]            # SCI markings
    export_control: ExportControl           # ITAR / EAR / Part 810
    proprietary: ProprietaryRestriction
    original_classifier: PrincipalId
    classification_date: str
```

`MemoryFragment.classification` is immutable. Re-classification follows the validated-classification pattern (`project_validated_classification_pattern`): a periodic L3 projection (`ClassificationValidator`) emits proposed `ClassificationDelta` records; operator approval applies one as a `ReclassificationApplied` event; original stamp stays intact for audit; tombstones cover any sharing that occurred under the previous stamp.

---

## 5. `TrustProfile` — per-scope acceptance policy

Per-scope, declared explicitly by the scope owner (the cohort's coordinator, the conversation's owner, the project's lead — whoever holds the scope's governance token):

```python
@dataclass(frozen=True)
class TrustProfile:
    scope: ScopeId

    # Outbound: which horizons we'll send, and to whom
    declared_peers: frozenset[PeerId]               # for PEERS_DECLARED
    federation_max_hops: int = 1                    # for FEDERATION_BOUND
    public_discoverable: bool = False               # for PUBLIC

    # Inbound: what we'll accept from peers
    inbound_horizons: frozenset[VisibilityHorizon]  # which horizons we'll accept
    inbound_classification_max: str = "unclassified"  # cap on accepted classification
    inbound_per_peer: dict[PeerId, InboundOverride] = field(default_factory=dict)

    # Projection shape preference
    prefer_concepts_over_full: bool = True          # default-deny on raw content
```

### Semantics

- `declared_peers` is the only set the gateway will fan `PEERS_DECLARED` content out to. Adding a peer is an explicit operator action; lint warns if the set grows past 50 (a hint that `FEDERATION_BOUND` may be the right horizon instead).
- `federation_max_hops` caps how deep through the trust graph (ADR-028) `FEDERATION_BOUND` content travels. Default 1; max 2 enforced at the gateway. Anything beyond 2 requires a separate explicit policy.
- `inbound_classification_max` is the floor below which the scope refuses to accept content. A scope declared `unclassified` cannot accept CUI even if a peer offers it; the gateway rejects at acceptance.
- `prefer_concepts_over_full` is the default projection shape: even when full-content outflow is allowed, the gateway sends concept-level metadata first; full content requires a follow-up explicit fetch.

### Defaults

The scope-creation default is conservative:
- `declared_peers = frozenset()` (no one declared; `PEERS_DECLARED` content can't leave)
- `federation_max_hops = 1`
- `public_discoverable = False`
- `inbound_horizons = {SCOPE_INTERNAL}` (the scope accepts no inbound from peers by default)
- `inbound_classification_max = "unclassified"`

Every relaxation is an explicit operator action recorded as an L1 event. There is no "open by default" path.

---

## 6. `FederationGateway` — composition runtime

The gateway is the only runtime that reads `VisibilityHorizon`, `ClassificationStamp`, and `TrustProfile` together. It exposes two operations:

```python
class FederationGateway:
    def project_for_peer(
        self,
        projection: Projection,
        task: TaskSpec,
        peer_id: PeerId,
        *,
        max_hops: int = 1,
    ) -> SignedProjection: ...

    def accept_from_peer(
        self,
        incoming: SignedProjection,
        peer_id: PeerId,
    ) -> AcceptDecision: ...
```

### Outbound rules (`project_for_peer`)

For each fragment that would appear in the projection:

```
1. Compute fragment.effective_outflow:
     min(visibility.outflow_level, classification.allowed_outflow_level)

2. Compute peer.allowed_inflow_for_this_scope:
     min(trust_profile.outbound_for(peer), peer.declared_inbound_horizons)

3. If fragment.effective_outflow >= peer.allowed_inflow_for_this_scope:
     - If projection.shape == "concepts_only" or trust_profile.prefer_concepts_over_full:
         project concept-level metadata only (strip blob_refs, redact content body)
     - Else:
         include full fragment
   Else:
     skip fragment (it does not appear in the outbound projection)

4. If fragment classification has nationality/clearance constraints:
     evaluate peer.principal.nationality ∩ fragment.classification.export_control.authorized_nationalities
     evaluate peer.principal.clearance >= fragment.classification.level
     skip fragment if either fails

5. Sign the resulting projection with the scope's signing key
```

**Classification trumps visibility, always.** A fragment marked `PUBLIC` but classified `CUI` collapses to `SCOPE_INTERNAL` outflow at step 1. The gateway never lets a writer's optimistic horizon override a regulatory constraint.

### Inbound rules (`accept_from_peer`)

For each fragment in an incoming projection:

```
1. Verify the projection's signature against the peer's known public key
   (rejected at the door if signature invalid)

2. Verify peer is in trust_profile.declared_peers OR trust graph reachable
   within trust_profile.federation_max_hops

3. Verify fragment.classification.level <= trust_profile.inbound_classification_max

4. Verify fragment.visibility is in trust_profile.inbound_horizons
   (e.g., a scope that hasn't opted into PUBLIC inbound rejects PUBLIC fragments)

5. Apply per-peer inbound override if present (more restrictive than scope default)

6. Accept: write fragment to L1 with origin metadata preserved (provenance shows
   it came from peer X via signed projection at time T)
```

Acceptance is itself an L1 event — `FragmentAccepted` references the incoming fragment + the peer + the projection signature. This means inbound flows are auditable + replayable + retractable like every other write.

### Hop-bounded by default

No gateway operation iterates the full peer set. `FEDERATION_BOUND` traversal walks the trust graph (ADR-028) up to `max_hops` and rejects deeper requests. Concept-level federated queries use Bloom-filter probabilistic peer existence checks before fetching, keeping discovery cost O(log peers) at federation scale (10k–100k nodes per `project_federation_scale_target`).

---

## 7. Composition with existing federation primitives

| Concern | Where it lives | This spec's role |
|---|---|---|
| Naming + addressing | ADR-027 | Consumed: gateway uses `axiom://` URIs to identify fragments + peers |
| Cohort-sharded registry | ADR-027 | Consumed: gateway consults the registry to find peer scopes |
| Propagation mode auto-scale | ADR-027 | Consumed: gateway respects propagation mode but still gates per-fragment |
| Trust graph (peer relationships) | ADR-028 | Consumed: gateway queries trust graph for `FEDERATION_BOUND` traversal |
| Identity + signing | `axiom.vega.identity` + ADR-022 | Consumed: gateway signs outbound projections; verifies inbound |
| Memory fragment shape | ADR-033 + `axiom.memory.fragment` | Extended: `MemoryFragment.visibility` + `MemoryFragment.classification` are added per ADR-033 |
| MIRIX cognitive types | `axiom.memory.fragment` (existing) | Orthogonal; type doesn't affect outflow rules directly |
| Classification regimes | `spec-classification-boundary.md` | Consumed: the stamp + invariants enforce here |

---

## 8. Implementation surface

### 8.1 Module locations

- `axiom.vega.federation.policy` — `VisibilityHorizon` enum, `TrustProfile` dataclass, default profile factory.
- `axiom.vega.federation.gateway` — `FederationGateway` class (Stage 5 of ADR-033 migration).
- `axiom.memory.fragment` — `MemoryFragment.visibility: VisibilityHorizon` + `MemoryFragment.classification: ClassificationStamp` fields.

The enum lives in federation rather than memory because it is a federation-policy primitive that happens to attach to a memory field. Memory imports the enum from federation; federation does not import from memory. (The dependency direction is intentional: federation defines the policy vocabulary; memory carries the field.)

### 8.2 Backward compatibility

`MemoryFragment.visibility` and `MemoryFragment.classification` ship as optional fields with conservative defaults:

- `visibility: VisibilityHorizon = VisibilityHorizon.SCOPE_INTERNAL`
- `classification: ClassificationStamp = ClassificationStamp.unclassified()`

Existing fragments without these fields decode with the defaults applied. Migration helper restamps existing fragments through the L1 dual-write adapter (Stage 1 of ADR-033) by reading the writer's intent from the extension's manifest defaults.

### 8.3 Test surface

- Unit tests for `VisibilityHorizon` ordering + alias resolution.
- Unit tests for `TrustProfile` default-deny posture.
- `FederationGateway` outbound: per-horizon gating, classification-trumps-visibility, peer-not-in-declared-peers rejection, hop-bound enforcement.
- `FederationGateway` inbound: signature verification, classification-floor enforcement, per-peer override, FragmentAccepted event written.
- Replay test: serialize + deserialize a `MemoryFragment` with visibility + classification, confirm round-trip stability.

---

## 9. Open items deferred to follow-up

- **Group identity for peers** — current `PeerId` is per-node. For institution-scope trust ("share with anyone at INL"), we need a group identity that resolves to a peer set. Likely a thin layer over ADR-022's authority hierarchy; tracked as a follow-up.
- **Outbound rate limits per peer** — to defend against a misconfigured `FEDERATION_BOUND` flooding a peer, the gateway should rate-limit. Algorithm + defaults TBD.
- **Inbound provenance preservation across promotion** — when a peer-accepted fragment is then re-projected to another peer, do we surface the original provenance chain or just our acceptance event? Default proposal: surface both; revisit after first cross-cohort use case.
- **Discovery bootstrap** — how a scope joins a federation it has no prior peer relationships in. Out of scope for this spec; relates to ADR-022 root-availability.

---

## 10. Acceptance criteria

This spec is implementable when:

1. `VisibilityHorizon` enum + `TrustProfile` dataclass land in `axiom.vega.federation.policy`.
2. `MemoryFragment.visibility` + `MemoryFragment.classification` ship with conservative defaults + serialization round-trip tests.
3. `FederationGateway` is specified in code (skeleton OK) with the outbound + inbound rule sequences above.
4. ADR-033 Stage 5 picks up the rest of the implementation behind these primitives.
