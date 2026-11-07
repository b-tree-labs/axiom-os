# ADR-057: Connector as a Top-Level Platform Primitive

**Status:** Accepted
**Date:** 2026-06-01
**Deciders:** Benjamin Booth
**Related:** ADR-055 (Unified Governance Fabric), ADR-056 (Skill as Function), connector-quality-competitive-study-2026-06-01, agent-harness-competitive-analysis-2026-06-01, axiom-strategy-refinement-2026-06-01b

---

## Context

Until 2026-06-01, connector machinery (the `ChannelAdapterRegistry`, the install wizard, the status store, the observability publish surface, the reconnect skill) lived inside the `notifications` extension. The CLI noun was `axi notifications connector ...`. This was a path-of-least-resistance arrangement: the channel adapter registry first landed inside HERALD because that was the first consumer, and every adjacent piece accreted around it.

The operator-facing question "what am I missing?" surfaced 2026-06-01: connectors are conceptually cross-cutting, not HERALD-owned. The same OAuth-bound credential to Microsoft 365 Graph is needed by:

- HERALD email outbound + inbound reply ingest
- The upcoming Calendar primitive (Outlook calendar read/write)
- A future RAG ingest pipeline (OneDrive + SharePoint)
- A future agent that schedules meetings on behalf of a human

The connector-quality competitive study (2026-06-01) makes the same point: "Every Axiom connector is contract-tested..." — the claim only makes sense if connectors are a *thing*, visible and addressable, not buried inside one consumer.

The strategy refinement (2026-06-01) names connectors as the wedge: "Make our connector extension so good that people would want to use Axiom for this alone." A wedge needs its own home.

Three architectural states under consideration:

1. **Status quo (rejected):** keep `axi notifications connector ...`. Cheapest now, entrenches a wrong shape, forces every future consumer (Calendar, RAG) to either replicate connector machinery or import from HERALD.

2. **Two-extension model (deferred):** `axi connector` for the primitive + `axi notifications` for HERALD-specific concerns, but channel adapters stay in HERALD as per-vendor specializations. This is the shape this ADR adopts as v0.

3. **Full Provider Protocol generalization (future):** a single `Connector` Protocol with `ChannelConnector` / `StorageConnector` / `CalendarConnector` specializations under it. Vendor implementations move under `connector/vendors/`. Consumer extensions (HERALD / Calendar / RAG) consume the registry without owning vendor code. Right end state; too large for one PR.

## Decision

**`axi connector` becomes a top-level platform primitive, peer to GUARD / KEEP / HERALD / PULSE.** A new built-in extension `axiom.extensions.builtins.connector` owns:

- The cross-cutting registry concept (status observability, reconnect lifecycle, install wizard)
- `axi connector add | status | reconnect` CLI verbs
- The bus event surface (`connector.delivered` / `connector.failed` / `connector.reconnect_required`)
- The status store (in-memory v0; Postgres-backed in a follow-up alongside audit-log retention)

**No deprecation shim** — `axi notifications connector ...` is removed in the same PR. The CLI churn is acceptable because the connector machinery is days old and unused outside CI tests.

**Per-vendor channel adapters stay in `notifications/channels/` for now.** They are specializations of the channel-adapter shape (with `ChannelCapabilities`, classification ceiling, threading/ack, delivery SLA) that don't generalize cleanly to storage or calendar connectors. The future Protocol generalization is deferred to a follow-up ADR (see "Future direction" below).

**The existing `connect/` extension (495 LOC, legacy credentials wizard, `axi connect <name>`) is untouched in this ADR.** It serves an adjacent purpose (set-up-a-credentialed-connection workflow against `axiom.infra.connections`). A future ADR consolidates the relationship — likely by absorbing `connect/` into `connector/` as the `add` wizard's secret-resolution backend.

## Consequences

### Positive

- **Conceptual clarity.** Consumers (HERALD, Calendar, RAG, future) reason about "the connector for vendor X" once, not "HERALD's channel for X plus Calendar's connector for X plus RAG's adapter for X."
- **The wedge claim becomes literal.** The strategy doc + connector-quality bar can credibly say "every Axiom connector..." because there's one extension whose entire job is connectors.
- **Future Calendar primitive (next PRD) drops the M365 Graph connector into the registry that HERALD email already uses.** One credential, two consumers, one reconnect flow, one audit row.
- **Badge integration ("Living Capabilities," Axiom Cloud paid tier) plugs into the connector extension as the `SecretBackendProvider`** — every connector gets phishing-resistant ephemeral bearer derivation, not just channels.
- **The operator mental model matches the file layout.** `axi connector add slack` lives in `connector/`, not in `notifications/`.

### Negative

- **CLI break.** Anyone who scripted `axi notifications connector ...` between 2026-05-31 and 2026-06-01 has to update. Acceptable given the day-old surface and the lack of external users.
- **Channel adapter files are now physically separated from their wizard handlers** (adapters in `notifications/channels/`, wizard handlers in `connector/wizard.py`). The wizard handlers import the adapter Provider classes by absolute path. This is a coupling that the future Protocol generalization will resolve; in v0 it's a known seam.
- **Documentation churn.** The connector-quality study, the agent-harness analysis, the strategy doc, and CLAUDE.md all reference the old paths in passing. They get refreshed as they're touched.

### Neutral

- **Test count unchanged** (213 → 213) because the move was rename-and-relocate, not behavior change.
- **`axiom.infra.connections`** (the platform-level connection abstraction the legacy `connect/` extension uses) is unchanged. The new `connector/` extension does not reach into it.

## Future direction (NOT in this ADR; tracked separately)

1. **Generalize the adapter Protocol** to a `Connector` shape with `ChannelConnector` / `StorageConnector` / `CalendarConnector` specializations. Vendor implementations (Slack, Microsoft 365 Graph, Box, etc.) live under `connector/vendors/`. HERALD / Calendar / RAG become pure consumers of the registry.
2. **Absorb the legacy `connect/` extension** into `connector/` once the Protocol generalization lands. The `axi connect <name>` UX folds into `axi connector add <name>` with the wizard handling the credential capture.
3. **Postgres-backed status store** alongside the audit-log retention feature (Axiom Cloud paid track).
4. **Adapter result-side `publish_outcome` calls.** The observability machinery is in place; each adapter needs a one-line call at result return time. Small focused PR.
5. **`axi connector list`** verb (currently the manifest declares it but the skill isn't implemented). Lists every registered connector with its capabilities. Adjacent to the existing `axi notifications channels` which serves a similar purpose for the HERALD-channel subset.

## How to use this list

When adding a new connector type (Storage / Calendar / Identity / future), the right shape is:

1. Implement the vendor-specific Provider in its consumer extension (HERALD / Calendar / RAG).
2. Register a wizard handler in `connector/wizard.py` so `axi connector add <vendor>` works.
3. Emit `connector.{delivered,failed,reconnect_required}` events from the adapter's result-return path via `connector.publish_outcome`.
4. The status surface + reconnect flow surface it automatically; no per-vendor code needed.

The Protocol-generalization milestone collapses step 1 into "implement against `connector.Protocol`" and moves vendor code into `connector/vendors/`.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
