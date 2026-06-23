# ADR-002 (schedule): Postgres storage, not Redis

**Status:** Accepted (2026-05-31)
**Locks:** spec-axiom-schedule §2
**Related:** ADR-052 (database tenancy), spec-governance-fabric §8.4

## Context

PULSE needs durable storage for schedule definitions, an idempotency log, a dead-letter trail, and a leader lease. Redis is the conventional pick for scheduler hot paths; the platform also already runs Postgres for every other governance primitive.

## Decision

PULSE-1 stores everything in the `schedule` Postgres schema (per ADR-052): three tables — `schedule_definition`, `schedule_fire_log`, `schedule_lease`. Redis is rejected as the primary backend.

## Consequences

- **Why not Redis:** (a) ADR-052 already picked Postgres as the install-wide OLTP; adding Redis grows the ops surface — extra process, extra backup story, extra failure mode — for no functional gain at PULSE-1's load. (b) The idempotency log, the lease, and the schedule definitions all need transactional storage; Postgres gives it for free. (c) Tick rate is bounded by the slowest registered cadence, not by hot-path throughput; "Redis is faster" applies above ~10 fires/sec which is well above PULSE-1 deployment targets.
- **Revisit trigger:** If a cohort exceeds 10 fires/sec on the hot tick path, ship a `firing.backend` provider and a new ADR. Not paying that complexity tax in v1.
- **Schema-per-extension:** Per ADR-052, all three tables live under the `schedule` schema; sessions go through `session_for("schedule")`; no `schema=` hardcoded on the SQLA models.
