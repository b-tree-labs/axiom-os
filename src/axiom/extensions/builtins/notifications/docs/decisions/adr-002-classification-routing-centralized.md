# ADR-002 — Classification routing is centralized in HERALD, adapters are opaque

**Status:** Accepted (2026-05-31)
**Scope:** `axiom.extensions.builtins.notifications` (HERALD)
**Locks:** spec-axiom-notifications §4

## Context

Every channel adapter has a classification ceiling. A `CONTROLLED` envelope
(ITAR / Part 810 / NSI) must never reach an `INTERNAL`-ceiling channel. The
check can live in:

1. Each adapter (defensive in depth, N call sites to keep in sync), or
2. HERALD's `send()` site, before adapter selection (one site).

## Decision

**HERALD's `send()` is the only classification-routing site.** Adapters
never see an envelope whose `classification > adapter.classification_ceiling`.
The site uses `axiom.governance.classification.classification_lte`, the same
helper PULSE + authz consume.

## Consequences

- One audit surface: the `routing_rationale` JSON column on
  `delivery_receipts` records the per-candidate decision and the rationale.
- Adapter authors cannot accidentally widen the ceiling — their code never
  runs above it.
- Fuzz testing covers a single function call site rather than N adapter
  implementations.
- The check is centralized but the rationale is per-candidate, so operators
  can replay why a channel was admitted or skipped.
- When **no** adapter is admitted, HERALD denies and emits a receipt with
  `outcome=denied`; the rationale records why. Phase-3 escalation to a
  `fallback_escalation_principal` is deferred (see spec §11 Q4).

## Alternatives considered

- **Per-adapter ceiling check** — rejected. Drift risk; double-audit; harder
  to fuzz.
- **Per-channel-adapter manifest declaration only (no runtime check)** —
  rejected. Lint catches malformed manifests; runtime check catches
  envelope-vs-ceiling mismatches that lint can't reason about.

## References

- spec-axiom-notifications §4
- spec-classification-boundary.md (tier definitions)
- `axiom.governance.classification` (the helper)
