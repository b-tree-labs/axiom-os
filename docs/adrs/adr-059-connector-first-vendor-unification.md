# ADR-059: Connector-First Vendor Unification

**Status:** Accepted
**Date:** 2026-06-01
**Deciders:** Benjamin Booth
**Related:** ADR-057 (Connector Primitive), ADR-058 (Agent Standards Registry), ADR-060 (Cross-Agent Event Routing), `docs/working/axiom-v0.30-unified-fabric-plan.md`

---

## Context

Today, Axiom has **two parallel notification stacks** that target the same vendors:

| Vendor | HERALD's path | Publishing's path |
|---|---|---|
| SMTP email | `SmtpEmailProvider` (`notifications/channels/email/smtp.py`, ~120 LOC) | `SMTPNotificationProvider` (`publishing/providers/notification/smtp.py`, ~100 LOC) |
| Terminal / inbox | `InboxChannelAdapter` | `TerminalNotificationProvider` |

Two implementations of the same vendor, two `Protocol`s, two registries (`ChannelAdapterRegistry` vs `PublisherFactory`), and consumers that pick one stack and stick with it:

- `release/_legacy_rivet_cli.py` imports `TerminalNotificationProvider` directly
- `chat/tools_ext/email.py` instantiates publishing's SMTP path directly
- HERALD's own `send()` uses its `ChannelAdapterRegistry`

Storage adapters tell the same story at the next altitude: `publishing/providers/storage/{local, sharepoint, box}.py` duplicate auth + OAuth lifecycle that the connector extension (ADR-057) is designed to own. Adding Microsoft 365 Graph next week (per the Austin/Radman blocker doc) would require building it twice under the status quo — once for HERALD email, once for publishing's notify-on-publish surface.

ADR-057 declared the connector extension as the home for cross-cutting vendor adapters. This ADR closes the loop by retiring the parallel stacks.

## Decision

**All vendor adapters live in the `connector/` extension. Consumer extensions reach in; nobody implements a vendor twice.**

### Specifically:

1. **Publishing's `NotificationProvider` ABC is retired.** Publishing no longer ships its own notification adapters.
2. **Publishing emits events** on the EventBus (`publishing.succeeded`, `publishing.failed`, `publishing.review_ready`, etc.) and the `agent_bridge` (PR #363) routes them through HERALD's `ChannelAdapterRegistry` per recipient preferences (PR #376).
3. **HERALD's `ChannelAdapter` Protocol stays** as the channel-shape (it carries classification ceiling, priority, threading, ack), and `notifications/channels/*` keeps the per-vendor adapter implementations *for the channel concern*.
4. **The `connector/` extension owns the cross-cutting registry, wizard, status, observability, and (eventually) per-vendor Protocol generalization.** ADR-061 (future) generalizes the adapter shape so HERALD's channel adapters, future storage connectors, and future calendar connectors all conform to a base `Connector` Protocol with specializations.
5. **Storage providers stay in `publishing/providers/storage/` for v0.30** (LocalStorage, SharePoint, Box) but their auth flows route through connector's `SecretBackendProvider` shape. Storage-adapter migration to `connector/` is queued as ADR-061.

### Migration plan (executed in M3 of the v0.30 arc)

| Step | Files affected | Effort |
|---|---|---|
| Add `publishing.*` to `agent_bridge.default_routing()` | `notifications/agent_bridge.py` | XS |
| Replace `engine.notify(...)` with `bus.publish("publishing.<event>", ...)` | `publishing/engine.py` | S |
| Replace RIVET's direct terminal import | `release/_legacy_rivet_cli.py` | S |
| Replace chat tool's direct SMTP path | `chat/tools_ext/email.py` | M |
| Delete `publishing/providers/notification/{smtp,terminal}.py` | publishing tree | XS |
| Remove `NotificationProvider` from `publishing/providers/base.py` | publishing tree | XS |
| Update `publishing/discovery.py` if it referenced the old shape | publishing tree | XS |
| Update tests: `publishing/tests/test_providers.py` etc. | publishing tests | M |

**Net delete: ~250 LOC. Net add: ~50 LOC of bus emission + routing config. Net: −200 LOC + one fewer concept to teach.**

## Consequences

### Positive

- **One M365 Graph adapter serves HERALD email + Calendar + publishing-notify.** Auth flow built once.
- **Adding a new channel never requires touching publishing.** Publishing emits events; HERALD figures out where they go.
- **Recipient preferences (PR #376) apply uniformly.** A `publishing.failed` event delivers to the same `@bbooth` profile that handles `tidy.escalation` — Slack, SMS, inbox, email per priority.
- **Auth lifecycle is uniform.** The Badge "Living Capabilities" feature (Axiom Cloud track) plugs in once as a `SecretBackendProvider` against the connector; every consumer benefits.
- **The "every Axiom connector..." marketing claim from `connector-quality-competitive-study-2026-06-01.md` becomes literally true.** Before this ADR it was aspirational because publishing had its own (lower-quality) adapter stack.

### Negative

- **One-time migration cost.** ~3 hours of focused work to land the M3 milestone. Tests that referenced the old `NotificationProvider` ABC must be rewritten.
- **Publishing's notification flow shifts from "direct send" to "publish event."** Callers that expected synchronous error propagation lose that — the event publishes, the channel-side failure is observed via the agent-bridge's normal handling.
- **Brief discoverability regression.** Operators who knew "publish → SMTP notify" must learn "publish → emits event → agent_bridge routes." Mitigated: the bridge's routing table is the single place to look, and ADR-060 documents it.

### Neutral

- **HERALD's per-vendor `ChannelAdapter` files (`channels/slack.py`, `channels/email/`, etc.) stay where they are.** Generalizing them to a `Connector` base Protocol is queued as ADR-061; this ADR commits only to retiring the *duplicate publishing* stack, not to the full Protocol generalization.

## How to use this list

When adding a new vendor adapter:

1. Build it in `notifications/channels/<vendor>/` under HERALD's existing `ChannelAdapter` Protocol.
2. Register a wizard handler in `connector/wizard.py` so `axi connector add <vendor>` works.
3. Emit `connector.{delivered, failed, reconnect_required}` events from the adapter's result-return path per ADR-057.
4. **Do not** build a parallel adapter in `publishing/providers/`. Publishing emits events; HERALD routes.

When adding a new consumer (e.g. Calendar):

1. Consume the connector registry directly. Do not invent a `CalendarNotificationProvider` ABC.
2. Emit events on the bus; the agent_bridge routes them per the recipient profile.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
