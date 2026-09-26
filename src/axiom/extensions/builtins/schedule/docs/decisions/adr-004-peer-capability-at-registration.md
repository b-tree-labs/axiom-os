# ADR-004 (schedule): Capability minting at peer registration, not at fire time

**Status:** Accepted (2026-05-31)
**Locks:** spec-axiom-schedule §4
**Related:** spec-governance-fabric §2 (capability tokens), §7 (federation hop), ADR-022 (root keys), ADR-028 (trust graph)

## Context

PULSE-2 / PULSE-3 will accept peer-defined schedules — a remote cohort registers a schedule that fires on our hardware. Two implementation paths:

1. **Mint at fire time** — at each fire, our PULSE calls the peer's KEEP to obtain a fresh capability.
2. **Mint at registration time** — the peer's KEEP issues a capability bound to the schedule at registration; our PULSE stores it; presents it at every fire.

## Decision

Mint at registration. The peer's KEEP issues a `ScheduleCapabilityEnvelope` bound to `(schedule_id, intent_pattern, resource_pattern, classification_ceiling, max_fires_per_window, window_seconds, not_before, not_after)`. PULSE stores it on `schedule_definition.capability_envelope`. At fire time PULSE presents the stored capability to our local `authz.decide()` as the actor's authority. Signature is verified once at registration (peer's root key) and re-verified at fire time (cheap; catches tampering).

## Consequences

- **No fire-time peer dependency.** A peer-defined schedule fires correctly even when the peer is unreachable. Critical for cross-tenant resilience.
- **Rate limit is enforceable locally.** `max_fires_per_window` is checked by our PULSE against `schedule_fire_log` — the peer doesn't get to lie about how often they're invoking.
- **Revocation has documented latency.** Peer revokes by rotating their KEEP root; our local cache of the peer's root key needs a refresh cadence (separate ADR in PULSE-3). At-fire-time signature re-verification doesn't catch revocation alone; the `not_after` window plus an active revocation-list pull does. Bounded by lease TTL + next-fire interval; acceptable for PULSE-2.
- **PULSE-1 scope:** Does not yet accept peer-defined schedules. The envelope shape is locked here so PULSE-2 ships it without renegotiation.
