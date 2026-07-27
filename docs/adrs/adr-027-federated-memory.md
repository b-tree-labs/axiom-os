# ADR-027: Federated Memory — Addressing, Coordination, Propagation

**Status:** Accepted (as-implemented, MVP)
**Date:** 2026-04-17
**Authors:** Benjamin Booth, Claude
**Related:** ADR-016 (federation foundation), ADR-022/023/024/025 (federation decisions), ADR-026 (ownership), ADR-028 (trust graph), ADR-033 (layered memory architecture), `spec-federation-policy.md` (visibility horizon + classification + trust profile + gateway primitives that ride on this addressing/propagation layer).
**Implementation:**
- `src/axiom/memory/addressing.py` (9 tests)
- `src/axiom/memory/cohort_registry.py` (12 tests)
- Federation primitives already shipped in `axiom/extensions/builtins/classroom/classroom_federation.py` (#16–19)

---

## Purpose

Define how Axiom resolves fragment addresses, propagates writes,
and coordinates memory state across a federation. The earlier
federation ADRs (022–025) addressed identity, topology, and
threats. This ADR closes the *memory mechanics* layer:

1. How a node names a fragment that lives on another node.
2. How cohorts coordinate their registry without a global point of
   failure.
3. How writes reach the right peers at different cohort sizes.

---

## Decision

### URI scheme: `axiom://<node-id>/<fragment-id>`

Every cross-node fragment reference uses this scheme.

- `node-id`: federation-recognized identifier (DNS-like,
  `prague.axiom.eu`).
- `fragment-id`: UUID4 from `axiom.infra.identifiers.generate_id`.
- `parse_uri` / `format_uri` in `axiom/memory/addressing.py`.

The scheme works for both MVP (central coordinator resolves) and
a future DHT (Kademlia lookup on `fragment-id`). Clients never
change — the URI is the stable API.

### Cohort-sharded registry (not a global DHT)

Each cohort has exactly one coordinator. The coordinator maintains
an address index `{fragment_id → frozenset[node_ids]}`. Every
member keeps a **local snapshot** of the registry.

- **Reads** are served from the local snapshot — they never fail
  even if the coordinator is down.
- **Writes** require the coordinator. When unreachable, writes
  queue in `pending_writes`.
- **Failover** is an explicit election. On `elect_coordinator`,
  the queue drains into the new coordinator's index.

This gives us:
- No global point of failure (many coordinators, one per cohort).
- No coordination overhead between unrelated cohorts.
- Bounded complexity — sharding is one dimension, not a DHT network.

### When DHT is and isn't required

DHT is **deferred, not permanently rejected.** The right way to
think about it is by deployment shape:

| Deployment | Coordinator | DHT needed? |
|---|---|---|
| Single-node (Prague MVP, local dev, personal) | Local (trivial) | **No** |
| Single-org federation (UT only) | Institutional hub | **No** (IT owns the directory) |
| Air-gapped or classified | Local hub | **No** (by constraint) |
| Multi-org research federation | No single authority | **Yes** |
| Multi-org production at 10k+ | Per-cohort + DHT fallback | **Yes** |

DHT's job is *cross-boundary resolver-of-last-resort* — when the
local coordinator is unreachable or when a query crosses trust
boundaries where no single party is authoritative. At Prague
scale, neither condition is hit; our cohort-registry path suffices.

The `axiom://` scheme is DHT-shaped from day one so no refactor
is needed when multi-org deployments show up. DHT (libp2p /
Kademlia) joins in as a parallel resolver — clients try the
cohort coordinator first; on miss or cross-boundary lookup, fall
through to DHT.

### Propagation mode — auto-select by cohort size

| Size | Mode | Cost | Consistency |
|---|---|---|---|
| < 100 | `push` | O(N) per write | immediate |
| 100 – 9,999 | `pull` (topic subscription) | O(subscribers) per topic | near-real-time |
| ≥ 10,000 | `gossip` (epidemic) | O(log N) per round | eventual |

`propagation_mode_for_size(n)` computes the default. Coordinators
can override for unusual workloads (e.g., force gossip for stress
testing at small scale).

Emergency events (quarantine, revocation) always escalate to push
regardless of mode — safety overrides efficiency.

### Failover election

Coordinator failure detection + failover is an operational concern
(not fully specified here). The registry provides the **data
shape** needed for failover:
- `mark_coordinator_unreachable()` suspends writes
- Read cache survives
- `elect_coordinator(new_coordinator)` promotes + drains queue

A consensus protocol (Raft-light, Paxos, or simpler quorum
round) picks the new coordinator. MVP deployment may do it
manually ("the coordinator is whatever node has `--role=coordinator`
in its flags"); production deployments add automatic election.

### Content stays with the owner; registry only locates

**The registry never holds content.** Only `{fragment_id → nodes
that have it}`. Content lives at the owner's node (per ADR-026,
ownership travels with the fragment). When Node A wants content
from Node B, it:
1. Resolves the address in the local cache.
2. Fetches directly from one of the listed replicas.
3. Verifies signature (ADR-028 / `axiom.memory.attest`).
4. Optionally caches locally + registers itself as a replica.

---

## Rationale

### Why not a DHT (yet)

DHTs excel at global distributed indexes with millions of peers
and no trust anchors. Axiom's federation has trust anchors by
design — institutions vouch for their members. We get resilience
from cohort sharding instead of from structural decentralization.

DHT complexity (routing tables, churn handling, protocol
versioning) is non-trivial. Libraries like libp2p help but still
impose an operational burden. Keeping it out of the MVP buys us
months of runway.

### Why per-cohort sharding works

A classroom cohort (12 students) is its own registry. A
dept-wide federation (500 members) is another. The coordinator
of one has no relationship to the coordinator of another. Outage
blast radius = one cohort. This is already how
`ClassroomCohort` (#16) works; we generalized it.

### Why read-cache + write-queue

Coordinator availability is the single failure mode. By keeping
every member's read cache authoritative, coordinator outage
becomes a write-only degradation — users can still see content;
they just can't publish new replica registrations until failover.

This is good-enough availability for the MVP. "Always read, mostly
write" matches how classroom memory is used.

### Why auto-scale propagation

Ben's intuition (small = push; large = gossip) is the well-studied
right answer. Auto-scaling avoids forcing operators to pick a mode
they don't know how to reason about. Operator override is
preserved for unusual cases.

---

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| Global DHT for addressing | Complexity >> benefit at current scale |
| Registry + content replication | Scope creep; content-move semantics unclear |
| Always-push propagation | O(N) per write at 10k scale is untenable |
| Always-gossip propagation | Eventual consistency unnecessary at small scale |
| No read cache (coordinator-authoritative) | Coordinator outage = total read outage |
| No failover election | MVP but brittle; added for production readiness |

---

## Consequences

### Positive

- **Scales cleanly to 10k-node federations** via cohort sharding.
- **Outage blast radius is one cohort**, not the whole federation.
- **Read availability is near 100%** via local cache.
- **`axiom://` scheme is DHT-ready** for future upgrade.
- **Propagation mode matches cohort size** without operator
  expertise.

### Negative

- **No automatic failover in MVP** — operator picks coordinator
  manually. Production deployments add Raft-light.
- **Write availability degraded during coordinator outage.**
  Writes queue; not lost, but delayed until failover.
- **Cross-cohort references require cohort-discovery.** If Node
  A in Cohort-1 wants a fragment from Cohort-2, it needs to know
  Cohort-2's coordinator first. Hand-off via invitation flow
  (ClassroomCohort invite tokens already handle this for
  classrooms; generalize).

### Migration

Existing `ClassroomCohort` (#16) already implements this shape for
classrooms; the `CohortRegistry` generalization makes it available
for any federation topology. `axiom://` URIs can be adopted
incrementally — pre-URI code continues to work via local fragment
ids.

---

## Open items

- **Automatic coordinator election protocol.** Add for production
  deployments; Raft-light seems sufficient.
- **Cross-cohort fragment discovery.** How does a node in Cohort-1
  find a fragment it has heard about from Cohort-2? Federation
  invite handshake?
- **Registry replication.** Today the coordinator is the single
  source of truth for writes; a secondary "follower" would reduce
  election-time write loss. Deferred.
- **DHT upgrade path.** When federation exceeds ~10k nodes, add
  libp2p-based Kademlia as a parallel resolver. Clients try central
  first; fall back to DHT. Keeps performance good for trusted hubs,
  adds resilience for the tail.

---

## Test coverage

- `tests/memory/test_addressing.py`: 9 tests — URI format/parse
  round-trip, validation errors.
- `tests/memory/test_cohort_registry.py`: 12 tests — register,
  deregister, snapshot/restore, read-cache-during-failover,
  write-queue-during-failover, failover-election-drains-queue,
  propagation-mode auto-selection, operator override.

Related (already shipped in #16–19):
- `ClassroomCohort` (cohort trust primitives)
- `pack_distribution.py` (mid-course update propagation)
- `ec_gate.py` (cross-node classification filtering)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
