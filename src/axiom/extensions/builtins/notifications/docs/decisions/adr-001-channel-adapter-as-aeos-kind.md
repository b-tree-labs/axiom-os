# ADR-001 — `channel_adapter` is the 8th AEOS capability kind

**Status:** Accepted (2026-05-31)
**Scope:** `axiom.extensions.builtins.notifications` (HERALD)
**Locks:** spec-axiom-notifications §2

## Context

HERALD dispatches to channels (inbox, email, Slack, Teams, …). AEOS currently
declares seven capability kinds (agent, tool, cmd, service, adapter, skill, hook).
A channel can be modeled as:

1. A subtype of `adapter` with HERALD-specific fields tacked on, or
2. A new top-level kind `channel_adapter` distinct from `adapter` and from
   `connector` (the fabric-§3 vendor-integration capability).

## Decision

**`channel_adapter` is the 8th AEOS capability kind.** It composes with
`connector` (via `connector_ref`) but is not a subtype of `adapter`.

## Consequences

- `axi ext lint --strict` gains a `channel_adapter` validator that enforces
  the declarations in spec §2 (direction, classification_ceiling, priority levels,
  threading + ack flags, optional webhook path, connector_ref resolves, etc.).
- HERALD never sees an `adapter` it has to interpret. The ceiling check (§4)
  and the registry (§7) work against one well-typed shape.
- AEOS bumps to `0.2.0` when this kind lands across the AEOS spec; the
  extension declares `aeos_version = "0.1.0"` until the bump merges.
- Storage adapters and channel adapters do not share contracts (they
  shouldn't). This avoids a future `adapter.subtype` discriminator.

## Alternatives considered

- **Subtype `adapter`** — rejected. Forces every storage adapter to either
  declare or ignore delivery semantics. Bloats the `adapter` contract.
- **Re-use `connector`** — rejected. A connector is the vendor integration
  (OAuth + rate-limit + retry). HERALD composes with one; a `channel_adapter`
  is a higher-level capability that names its `connector_ref`.

## References

- spec-axiom-notifications §2 — manifest shape + lint rules
- spec-governance-fabric §3 — connector capability shape
- AEOS 0.1 spec — `src/axiom/docs/specs/spec-aeos-0.1.md`
