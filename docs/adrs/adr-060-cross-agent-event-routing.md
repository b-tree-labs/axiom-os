# ADR-060: Cross-Agent Event Routing

**Status:** Accepted
**Date:** 2026-06-01
**Deciders:** Benjamin Booth
**Related:** ADR-055 (Governance Fabric), ADR-058 (Agent Standards Registry), ADR-059 (Connector-First Vendor Unification), `notifications/agent_bridge.py` (PR #363)

---

## Context

Today, when one Axiom agent needs to surface a signal to a human (or another agent), there is no consistent path:

- **RIVET** imports `TerminalNotificationProvider` from publishing and calls `.send()` directly with hardcoded SMTP config.
- **TIDY** publishes events on `axiom.infra.bus.EventBus` (per ADR-046) and TIDY subscribers + the `agent_bridge` (PR #363) route them.
- **HERALD** has a recipient-preferences primitive (PR #376) that fans out to channels per `(classification, priority)`.
- **PRESS** has no agent-to-human surface at all today; its CLI is direct.

This produces three failure modes:

1. **Adding a new channel requires touching every agent.** Want SMS for RIVET? Edit RIVET's code. Want SMS for TIDY? Edit TIDY's code. Want SMS for PRESS? PRESS doesn't even have a notification path.
2. **Recipient preferences don't compose.** `@bbooth`'s "high priority → SMS + Slack" profile applies to HERALD's `send()` calls but not to RIVET's direct provider import.
3. **Audit is bespoke.** RIVET writes JSONL heartbeat lines; HERALD writes `DeliveryReceipt`s; publishing writes nothing structured. There is no uniform "who got notified, when, why" query.

The pieces to fix this already exist after the recent work: `EventBus` (long-standing), `agent_bridge` (PR #363), recipient preferences (PR #376), HERALD's channel registry. This ADR ties them together as a binding rule.

## Decision

**No agent imports another agent's notification path. Agents publish events; the `agent_bridge` routes; HERALD delivers.**

### The contract

1. **Every agent that needs to surface information emits an event on the platform EventBus** with a stable subject name (`rivet.ci_recovered`, `tidy.escalation`, `publishing.review_ready`, etc.).
2. **`agent_bridge.default_routing()` is the single registry that maps event subjects to HERALD send shapes** (priority, classification, summary template, recipient).
3. **HERALD's `send()` is the single dispatch point.** It consults the `ChannelAdapterRegistry`, the recipient-preferences store, and the classification ceiling. No agent skips it.
4. **The receipt model is uniform.** Every routed event becomes a `DeliveryReceipt` (per spec-axiom-notifications §8) which is itself a memory fragment under the originating envelope.

### What this means concretely

- RIVET's `pr_check_responder.py` stops importing `TerminalNotificationProvider`. It publishes `rivet.ci_failed` on the bus. The bridge handles routing.
- Publishing's `engine.notify(...)` is retired (per ADR-059). Publishing emits `publishing.succeeded` etc.
- Chat's email tool stops instantiating SMTP directly. It publishes `chat.email_send_request` and HERALD's email channel delivers.
- A future Calendar primitive emits `calendar.event_invited`; the bridge routes per the operator's profile.

### Event-naming convention

`<originating-agent>.<event-name>` — lowercase, dot-separated, present tense. Wildcards (`*.escalation`, `*.failed`) are supported by the bridge's routing pattern matcher.

### Routing-rule shape (already implemented per PR #363)

```python
BridgeRule(
    subject_pattern="rivet.ci_failed",
    summary_template="⚠ CI failed on {repo} — {failing_jobs}",
    priority=Priority.HIGH,
    classification=Classification.INTERNAL,
    actor="@rivet",
    recipient="@operator",   # resolved to channels via the recipient profile
)
```

The bridge already exists. This ADR commits to using it everywhere.

## Consequences

### Positive

- **One place to change channel routing.** A new operator who wants `rivet.ci_failed` to land in Slack instead of inbox edits the bridge config — not RIVET, not publishing, not chat.
- **Recipient preferences apply uniformly.** Every agent's signals fan out per the operator's declared profile.
- **Receipts are queryable as memory fragments.** "Show me every notification @bbooth received about a publishing failure in May" becomes one memory query.
- **Adding a new agent is trivial.** Publish events with stable subjects; the bridge already routes wildcards.
- **The agent fabric becomes uniform.** No more "every agent has its own preferred sender" pattern.

### Negative

- **Synchronous error propagation goes away** for agents that previously called `send()` directly and inspected the return. The event publish is fire-and-forget; failures are observed via the bridge's failure-handling and surfaced on the channel-status surface (per ADR-057 connector observability).
- **Discoverability friction during the transition.** Operators who knew "RIVET sends via terminal" must learn the event surface. Mitigated by `axi notifications list` showing the bridge's routing table.
- **Standards execution receipts now thread through the bus.** This is intentional per ADR-055 + ADR-058 but it means observers care about ordering — partially mitigated by the EventBus's per-subscriber `fail_mode="warn"` semantics.

### Neutral

- **The agent_bridge already exists.** PR #363 shipped it tonight. This ADR is largely about disciplining usage rather than building new infrastructure.

## How to use this list

When an agent needs to surface a signal:

1. Pick a stable event subject (`<agent>.<event>`).
2. `bus.publish(subject, payload, source=f"agent.{name}")`.
3. If routing is novel, add a `BridgeRule` to `agent_bridge.default_routing()` (or override at deployment via a custom `BridgeRouting`).
4. Do **not** import another agent's send path.
5. Do **not** instantiate vendor adapters (SMTP, Slack, etc.) directly. Use HERALD via the bridge.

When an extension needs a different routing for a deployment:

1. Construct a custom `BridgeRouting(rules=...)`.
2. Pass it to `AgentBridge(send_ctx, routing=...)`.
3. The deployment owns the routing; the agent code doesn't need to know.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
