# Event Bus v2 — Product Requirements

**Status:** Draft  •  **Owner:** Benjamin Booth  •  **Last updated:** 2026-04-25
**Audience:** Extension authors, integrators, anyone publishing or subscribing to platform events.
**Companion docs:** [`spec-event-bus.md`](../specs/spec-event-bus.md), [`prd-hooks.md`](prd-hooks.md), [`spec-hooks.md`](../specs/spec-hooks.md).

---

## 1. What problem this solves

Axiom has had an in-process event bus (`axiom.infra.orchestrator.bus.EventBus`) since the earliest signal-pipeline work. It runs synchronously, writes events to a durable JSONL log for replay, and accepts glob-pattern subscriptions. It is consumed by `hygiene/`, `diagnostics/`, and (soon) the platform-hooks observer surface.

It has three load-bearing limitations:

1. **Silently swallows subscriber exceptions.** A misbehaving handler is invisible. Cost meters that fail to record, audit hooks that throw — none of it surfaces.
2. **Synchronous dispatch only.** Long-running observers (webhook deliveries, telemetry exports, federation push) block the publishing thread.
3. **No transport abstraction.** The in-process JSONL implementation is fine for an Edge laptop and inadequate for a 10k–100k-node federation. The bus offers no clean swap point — replacing it later means rewriting every subscriber.

This PRD scopes a v2 of the event bus that preserves the existing simple surface while making the bus production-grade for the upcoming platform-hooks work and the federation tier we're moving toward.

The OSS-library evaluation that informs this PRD lives at `project_event_bus_oss_assessment.md` (memory). Headline: **no OSS library covers Edge-laptop-in-process AND 10k–100k-node distribution in one runtime**, so the right move is to harden our own bus and design its surface for clean swap to NATS or PG LISTEN/NOTIFY when federation actually ships.

## 2. Who uses the bus and why

| Persona | What the bus does for them |
|---|---|
| **Extension author** | Subscribe to lifecycle events (`tool.post_invoke`, `session.started`, etc.) without coupling to specific producers. Publish domain events without specifying who listens. |
| **Operator / sysadmin** | Drop a subscriber file at `$AXIOM_HOME/hooks/` to wire local audit, cost, or telemetry without writing an extension. |
| **Federation participant** | Receive `federation.post_accept` events with signed envelopes attached; treat them as first-class platform events. |
| **Classroom instructor** | Build cohort-wide signal extractors that observe `tool.post_invoke` events across a session for stuck-student / engagement patterns. |
| **Security reviewer** | Read the JSONL event log to reconstruct a session, including which handlers ran, which failed, and what they observed. |

## 3. Mental model

> **One logical bus, two dispatch modes, swappable transport.**

- **One logical bus.** Authors `publish` an event with a subject and payload; subscribers receive it. They don't think about which transport carries it.
- **Two dispatch modes.** Subscribers register as either *synchronous* (run inline on `publish`) or *asynchronous* (scheduled on an event loop, run concurrently). The publisher chooses neither — both modes deliver the same event.
- **Swappable transport.** The bus owns *what an event looks like and how subscribers see it*. The transport owns *where the event physically lives between publish and dispatch*. v2.0 ships one transport (in-process + JSONL durability — what we have today). When federation needs cross-node fan-out, we add transports (NATS + JetStream for Platform, PG LISTEN/NOTIFY for Server) without changing any subscriber code.

This is distinct from the **HookBus** primitive (see `prd-hooks.md`). HookBus is for *interceptors* — synchronous, ordered, may modify or deny the in-flight operation. EventBus is for *notifications* — fire-and-forget, no return value, observers don't block the producer. Both primitives share manifest discovery and fail-mode handling; both deliver against AEOS §4.7's hook contract; the difference is what the subscriber can DO with the event.

## 4. Subject syntax

Events are identified by dot-separated subjects following NATS conventions:

- `tool.post_invoke` — concrete subject
- `tool.*` — exactly one token after `tool` (matches `tool.post_invoke`, `tool.error`)
- `tool.>` — one or more tokens after `tool` (matches `tool.post_invoke`, `tool.classroom.quiz_submitted`)
- `*.ended` — one token before `ended` (matches `session.ended`, `cohort.ended`)

Tokens are lowercase ASCII letters, digits, and underscores. The `*` and `>` wildcards bind only on token boundaries.

The current bus uses Python's `fnmatch` glob, which differs in subtle ways (`*` matches anything including dots). We tighten to NATS subjects in v2.0 because it's the syntax the eventual JetStream transport speaks natively — no ambiguity in the swap. Existing subscribers are migrated by the bus during the upgrade; authors who used `*` to mean "any number of tokens" replace it with `>`.

## 5. Authoring

### 5a. Subscribe in code

Synchronous (today's API; `priority` and `fail_mode` are new optional kwargs):

```python
from axiom.infra.bus import bus

bus.subscribe(
    "tool.post_invoke",
    cost_meter,
    priority=100,
    fail_mode="warn",
)
```

Asynchronous (new):

```python
from axiom.infra.bus import bus

bus.subscribe_async(
    "federation.post_accept",
    notify_peer_dashboard,
    priority=200,
    fail_mode="warn",
)
```

A sync handler that takes too long is a bug; if you're doing real I/O, use async.

### 5b. Subscribe via manifest declaration (preferred for extensions)

```toml
[[extension.provides]]
kind = "hook"
events = ["tool.post_invoke"]
entry = "hygiene.observers:audit_record"
priority = 100
fail_mode = "warn"
description = "Append every tool invocation to the local audit ledger"
```

The bus's discovery layer (shared with HookBus per `spec-hooks.md`) auto-registers this at runtime startup. No `bus.subscribe()` call needed in the extension's boot code.

### 5c. Publish

```python
bus.publish(
    "tool.post_invoke",
    {"tool_name": "search", "args": {...}, "result": ..., "tokens": 412, "latency_ms": 1287},
    source="gateway",
)
```

Publishing is always synchronous from the publisher's perspective: the call returns once the in-process transport has accepted the event. Sync subscribers run before `publish` returns; async subscribers are scheduled and run on the event loop.

## 6. Structured errors

Subscriber exceptions are first-class events. When a handler raises:

- If `fail_mode = "abort"`: the exception bubbles to the publisher.
- If `fail_mode = "warn"` (default): the exception is published as a `bus.errors` event with `{handler, original_event, exception, traceback}` and a structured warning is logged.
- If `fail_mode = "ignore"`: the exception is published as a `bus.errors` event at debug level only.

`bus.errors` is itself a subscribable subject. A diagnostics extension can subscribe to `bus.errors` and surface a "subscriber X is failing on Y" status — turning today's invisible silent-swallow into an observable, dashboard-able signal.

## 7. Trust and safety

- The bus shapes behavior; it never grants capability (Axiomatic Way principle #4). A subscriber observes; it does not authorize.
- Subscribers from a signed extension run with the extension's trust profile.
- Subscribers at `$AXIOM_HOME/hooks/` run with user trust (the user installed them); a startup banner names each user-level subscriber.
- Subscribers at `./.axiom/hooks/` (project-local) require a first-sight confirmation, same model as user-level hooks per `prd-hooks.md` §6.
- Cross-process or federated transports (v2.1+) carry signed envelopes with classification stamps. The bus doesn't add trust; it preserves it.

## 8. What the bus is NOT

- **Not the HookBus.** Hooks intercept (modify/deny). The bus notifies. If you want to deny a tool call, register a hook on `tool.pre_invoke`; if you want to log it, subscribe on `tool.post_invoke`.
- **Not a queue.** Subscribers run as the events arrive (sync immediately, async soon). The bus is not a job queue with retries; if you need durable work-queue semantics, build a worker on top.
- **Not a broker.** v2.0 is in-process. Federation adds transports later; the bus surface stays the same.
- **Not a webhook receiver.** Inbound webhooks are an `event-driven triggers` parity-gap concern; they'd publish into the bus but the receiving HTTP plumbing isn't bus territory.

## 9. v2.0 scope (lands with hooks v1)

1. **Transport Protocol** — the `BusTransport` interface; the in-process JSONL implementation is the first concrete transport. No subscriber-facing changes.
2. **Async dispatch** — `subscribe_async` + an `AsyncEventBus` sibling. Sync dispatch is unchanged.
3. **`bus.errors` topic** — replaces today's silent exception swallow. Default `fail_mode` becomes `"warn"`.
4. **NATS-shape subjects** — `*` and `>` wildcards on token boundaries. Existing `fnmatch`-glob patterns are rewritten to NATS subjects in the same commit; no compat shim. Pre-public-launch we refactor cleanly per the project's no-shim rule.
5. **Manifest-driven discovery** — shared with `HookRegistry`. Extension subscribers move from manual `bus.subscribe()` to `[[extension.provides]] kind = "hook"`.
6. **Priority + fail_mode** — required by hooks; cheap to add here.

## 10. v2.1 scope (independent PRDs after hooks v1 lands)

These each warrant their own design pass:

- **Cross-process delivery** — NATS + JetStream for Platform tier; PG LISTEN/NOTIFY for Server. Federation events ride this transport. Each is a transport implementation behind the v2.0 Protocol.
- **Replay-as-test mode** — re-fire all events from the JSONL log against a fresh subscriber set; powerful regression tool for refactors.
- **Schema versioning** — TypedDict per subject + breaking-change discipline (additive-only, breaking changes get a new subject name).
- **Metrics + introspection** — built-in counters for events fired, handler latencies, fail-mode events; `bus.subscribers_for(pattern)` for diagnostics.
- **Rate-limiting + back-pressure** — for high-volume observer paths (telemetry spikes, cost meter under bursty load).
- **Hot-reload of user-level subscribers** — without restarting the agent.

## 11. Future (post-v2.1, captured to keep the v2.0 design swap-friendly)

- **Cross-language subscribers.** A Vyzier extension authored in TypeScript could subscribe over the JetStream transport. Out of scope for v2.x but the Transport seam doesn't preclude it.
- **Event sourcing** — full reconstruction of system state from the event log. Already half-supported by replay; would need durable consumer offsets to be production-grade.
- **Federated event schemas** — institutions agree on a shared subject namespace (`research.cohort.>`, `classroom.>`) so peers can subscribe across the federation.

## 12. Risk flags worth knowing

- **NATS BUSL/CNCF dispute** (April–May 2025) is parked, not resolved. Synadia attempted to relicense `nats-server` to BUSL and pull it from CNCF; settled with trademarks transferred to LF, Apache-2.0 retained. Could reignite. The Transport seam keeps us escapable.
- **Don't adopt Redis-the-server.** RSALv2/SSPL since 7.4, AGPL reintroduced 2025 — none Apache-2.0 compatible for our purposes. Use **Valkey** (Linux Foundation, BSD-3) if a Redis-shape store is needed.
- **Python 3.14 lag.** `nats.py`, `redis-py`, `asyncpg` don't yet advertise 3.14 wheels. We'd be early adopters.
- **Today's silent-swallow** is the most pressing latent bug. The platform-hooks PRD adds many subscribers; without the `bus.errors` topic, a misbehaving hook is invisible.

## 13. Success criteria

- Existing subscribers (`hygiene/subscriber.py`, `diagnostics/subscriber.py`) migrate to manifest declarations with no behavior change.
- A platform-hooks observer (cost meter, audit logger) ships in <200 LOC including tests.
- A failing subscriber surfaces as a `bus.errors` event observable by `axi diagnostics`, not as silent loss.
- The Transport Protocol is exercised by at least one alternate implementation in test fixtures (a `MockTransport` for in-memory test isolation).
- When NATS adoption time comes, swap is one new transport class — zero subscriber code changes.

## 14. Open questions

- *Default `fail_mode` choice.* Today is effectively `ignore` (silent swallow). v2 defaults to `warn` from day one. Some subscribers may now log noisily — that's signal, not noise; silent failure was a real bug. Subscribers that genuinely tolerate failure declare `fail_mode = "ignore"` explicitly.
- *Subject migration.* `fnmatch` `*` matched dots; NATS `*` doesn't. Existing patterns like `"sense.*"` continue to mean what they meant; patterns that relied on `*` matching multiple tokens (rare) need `>`. A one-shot scan of the codebase + deprecation warning covers it.
- *Async event loop ownership.* Who owns the loop the async subscribers run on? v2.0 default: a private bus-owned loop in a daemon thread. Authors who already have an async chat loop can pass it in. Confirm at implementation.

---

*Companion technical spec:* [`spec-event-bus.md`](../specs/spec-event-bus.md).
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
