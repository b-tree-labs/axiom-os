# ADR-001 (schedule): Single-node firing in PULSE-1, lease-row in Postgres

**Status:** Accepted (2026-05-31)
**Locks:** spec-axiom-schedule §1
**Context:** [prd-axiom-schedule §3, §5.4](../../../../../../docs/prds/) — relocated under axiom-governance-fabric.

## Context

The PRD's success metrics include "100% exactly-once across multi-node deployments." That bar requires either a Raft-style consensus library or a lease-with-handoff protocol on top of a shared transactional store. The platform already depends on Postgres (ADR-052); pulling in Raft for one primitive is a complexity bet we shouldn't take in v1.

## Decision

PULSE-1 fires from a single node, gated by a singleton row (`schedule.schedule_lease`) acquired via Postgres advisory lock and renewed every `lease_ttl_seconds / 3`. The engine ticks only while it holds the lease.

The same shape carries forward to PULSE-2's distributed mode: same lease row, plus on-failure handoff and a post-fire receipt-claim handshake. The v1 → v2 cutover is a `firing.mode = "distributed"` config flip, not a re-architecture.

## Consequences

- **Cost:** PULSE-1 is **not** exactly-once across multi-node deployments. With one engine running, this is moot; with two, the lease prevents simultaneous ticking but a node-death-mid-fire is undetectable until PULSE-2's claim handshake lands. We document this risk in the spec.
- **Benefit:** No new operational dependency. No consensus library. The lease is just a row.
- **Benefit:** The v2 cutover is testable in isolation — every other component is already exercised under v1.
- **Carries forward:** The PRD's "100% exactly-once" success metric becomes PULSE-2's bar; PULSE-1 ships single-node firing only.
