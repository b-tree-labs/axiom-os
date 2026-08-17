# Event Bus v2 — Technical Specification

**Status:** Draft  •  **Owner:** Benjamin Booth  •  **Last updated:** 2026-04-25
**Companion docs:** [`prd-event-bus.md`](../prds/prd-event-bus.md), [`prd-hooks.md`](../prds/prd-hooks.md), [`spec-hooks.md`](spec-hooks.md).

---

## 1. Goals

- Extract a `BusTransport` Protocol behind `EventBus.publish` / `subscribe`. The current in-process + JSONL implementation becomes one concrete transport; future transports (NATS JetStream, PG LISTEN/NOTIFY) implement the same Protocol with no subscriber-facing change.
- Tighten subject syntax to NATS subjects (`a.b.c` with `*` for one token, `>` for tail) so the eventual JetStream swap is a transport change, not a refactor. Migrate existing `fnmatch`-glob users with a one-shot deprecation shim.
- Add async dispatch (`subscribe_async`) so long-running observers (webhook delivery, telemetry export, federation push) don't block the publishing thread.
- Replace `bus.py:149`'s silent exception swallow with a structured `bus.errors` topic. Make subscriber failures observable.
- Honor `priority` and `fail_mode` per subscriber, matching AEOS §4.7's manifest contract and `spec-hooks.md`'s requirements.
- Reuse `HookRegistry` (from `spec-hooks.md`) as the manifest-driven subscriber-discovery layer. The bus and HookBus share discovery; they differ in dispatch semantics.
- Keep zero new external runtime dependencies. The bus is pure stdlib + existing internal modules.

## 2. Non-goals (v2.0)

- Cross-process delivery. The Transport Protocol *enables* it; no implementation ships in v2.0.
- Replay-as-test, schema versioning per subject, metrics introspection, rate-limiting. Each is its own follow-up PRD per `prd-event-bus.md` §10.
- Replacing the bus with an OSS library (NATS, blinker, pyee, etc.). The OSS evaluation in `project_event_bus_oss_assessment.md` ruled this out for v2.0; revisit when federation transport is needed.
- Hot-reload of user-level subscribers without a process restart.

## 3. Architecture

```
        ┌───────────────────────────────────────────────────┐
        │                EventBus  (sync surface)            │
        │   • publish(subject, payload, source)              │
        │   • subscribe(pattern, handler, priority,           │
        │                fail_mode)                           │
        │   • subscribe_async(pattern, handler, priority,     │
        │                     fail_mode)                      │
        │   • subscribers_for(pattern)  [diagnostic]          │
        └───────────────────────────────────────────────────┘
                              │
                              ▼
        ┌───────────────────────────────────────────────────┐
        │         BusTransport  (Protocol)                   │
        │   • accept(event: Event) -> None                   │
        │   • iter_subscribers(subject) -> Iterable[Sub]      │
        │   • durability_log_path() -> Path | None            │
        └───────────────────────────────────────────────────┘
                              │
        ┌──────────────────────┼─────────────────────────┐
        ▼                      ▼                         ▼
   InProcessTransport    NATSJetStreamTransport      PostgresLNTransport
   (v2.0 ships this)     (v2.1; future)              (v2.1; future)
```

**Why the Protocol seam.** The Edge profile (Bonsai laptop, offline-first) cannot afford a broker daemon. The Platform profile (a shared HPC cluster, 10k–100k nodes) cannot rely on JSONL replay. No single library bridges both. The Transport Protocol is what bridges them: each profile gets the transport it needs; subscribers don't care.

**Why subjects, not glob patterns.** NATS subjects are the future-broker syntax. Adopting them now is free (the matcher is ~30 lines) and means `EventBus("federation.peer.*.classification.>")` reads identically when later it runs over JetStream.

## 4. Data model

### 4a. `Event`

Existing `axiom.infra.orchestrator.bus.Event` is preserved. Add one optional field:

```python
@dataclass
class Event:
    subject: str               # was `topic` — renamed for NATS alignment
    payload: dict[str, Any]    # was `data`
    timestamp: str = ""        # ISO 8601, set in __post_init__
    source: str = ""           # publishing component name
    envelope: dict[str, Any] = field(default_factory=dict)  # NEW: signed envelope hook for federation transports
```

`topic` and `data` keep backward-compat aliases (read-only properties) through Axiom 0.16 with a `DeprecationWarning` on access.

### 4b. `Subscription`

```python
FailMode = Literal["abort", "warn", "ignore"]

@dataclass(frozen=True)
class Subscription:
    pattern: str               # NATS subject pattern
    handler: Callable          # sync or async
    is_async: bool
    priority: int = 100        # lower runs first
    fail_mode: FailMode = "warn"
    source: str = ""           # extension name, "user", "platform", or ""
```

## 5. Subject syntax

Formal grammar:

```
subject     := token ('.' token)*
pattern     := pattern-token ('.' pattern-token)*  (no '>'  before the last token)
            |  pattern-prefix '.>'
pattern-prefix := token ('.' token)*  |  '*' ('.' token)*  |  …
token       := [a-z0-9_]+
pattern-token := token | '*'
```

Matching rules:

- `*` matches exactly one token, between dots.
- `>` matches one or more tokens; only legal as the final element.
- Subjects are case-sensitive. Tokens are lowercase ASCII letters, digits, underscores. Reject anything else with a clear error at `subscribe` time.

Implementation: a small `subject_matches(pattern: str, subject: str) -> bool` function in `axiom.infra.bus.subjects`. Roughly 30 lines, fully unit-tested.

### Migration from `fnmatch` glob

`fnmatch` differs from NATS in that `*` matches dots: `tool.*` matched `tool.post.invoke`. NATS `tool.*` only matches `tool.post`; you need `tool.>` to match `tool.post.invoke`.

Migration strategy:

1. At v2.0 release, the bus accepts BOTH styles for one deprecation cycle.
2. Patterns containing `*` that would match multi-token subjects are detected at `subscribe` time and emit a `DeprecationWarning` — "this pattern would change semantics under NATS subjects; replace with `tool.>` or `tool.*`".
3. A one-shot scan script (`scripts/migrate_bus_subjects.py`) rewrites the codebase. Hygiene + diagnostics + classroom + research subscribers all get audited.
4. Axiom 0.16 removes `fnmatch` fallback.

## 6. The `BusTransport` Protocol

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class BusTransport(Protocol):
    """Backend that physically delivers events between publish and dispatch."""

    def accept(self, event: Event) -> None:
        """Called by the bus on publish. Transport persists / forwards the event."""

    def attach_subscriber(self, subscription: Subscription) -> None:
        """Called by the bus when a subscriber is registered."""

    def detach_subscriber(self, subscription: Subscription) -> None:
        """Called by the bus when a subscriber is removed."""

    def iter_pending(self) -> Iterable[Event]:
        """Drain pending events queued by the transport. Used by replay tooling."""

    def durability_log_path(self) -> Path | None:
        """Path to the JSONL log if this transport is durable; None if ephemeral."""
```

The v2.0 `InProcessTransport` is the existing `EventBus._dispatch` machinery refactored behind this Protocol. It:

- Persists every accepted event to JSONL via `axiom.infra.state.locked_append_jsonl` (today's behavior).
- Walks registered subscriptions in priority order on `accept`, invoking sync handlers inline and scheduling async handlers on the bus's task group.
- Surfaces handler exceptions as `bus.errors` events per §7.

Future transports:

- **`NATSJetStreamTransport`** — `accept` calls `nc.publish(subject, payload)`; subscribers are bound NATS consumers; durability via JetStream streams. Loaded only when the Platform profile is selected and `nats.py` is installed (an optional extra).
- **`PostgresLNTransport`** — `accept` writes to an outbox table + emits `NOTIFY`; subscribers `LISTEN` and pull from the outbox. Useful for Server-tier deployments with Postgres but no NATS broker.

The bus chooses its transport at construction time via dependency injection. Default for built-in `EventBus()` is `InProcessTransport(log_path=$AXIOM_HOME/runtime/events.jsonl)`.

## 7. Structured errors

When a sync subscriber raises:

```python
def _dispatch_sync(self, subscription, event):
    try:
        subscription.handler(event.subject, event.payload)
    except Exception as exc:
        self._handle_subscriber_error(subscription, event, exc)

def _handle_subscriber_error(self, subscription, event, exc):
    error_event = Event(
        subject="bus.errors",
        payload={
            "handler": _qualname(subscription.handler),
            "original_subject": event.subject,
            "original_payload": event.payload,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
            "fail_mode": subscription.fail_mode,
        },
        source="bus",
    )
    self._transport.accept(error_event)  # itself an event; subscribed to like any other

    if subscription.fail_mode == "abort":
        raise
    elif subscription.fail_mode == "warn":
        log.warning("subscriber %s raised on %s: %s", error_event.payload["handler"], event.subject, exc)
    # "ignore" — no log; the bus.errors event is the only signal
```

Async subscriber failures publish the same `bus.errors` event. `abort` mode is meaningless for async (the publishing thread is already gone); async + abort logs an error and treats it as `warn`.

`bus.errors` events themselves are subscribable. A diagnostics extension can subscribe to `bus.errors` and surface "subscriber X failed Y times in the last hour" in `axi diagnostics`. Subscribers that themselves fail while handling a `bus.errors` event are demoted to `ignore` to prevent infinite-loop pathologies.

## 8. Async dispatch

```python
class EventBus:
    def __init__(self, transport: BusTransport, *, async_loop: asyncio.AbstractEventLoop | None = None):
        self._transport = transport
        self._loop = async_loop  # None → bus owns a private daemon-thread loop on first async use
        self._async_task_group: asyncio.TaskGroup | None = None

    def subscribe_async(self, pattern: str, handler: AsyncHandler, *, priority: int = 100, fail_mode: FailMode = "warn") -> Subscription:
        sub = Subscription(pattern=pattern, handler=handler, is_async=True, priority=priority, fail_mode=fail_mode)
        self._transport.attach_subscriber(sub)
        return sub

    def publish(self, subject: str, payload: dict, source: str = "") -> Event:
        event = Event(subject=subject, payload=payload, source=source)
        self._transport.accept(event)
        for sub in self._transport.iter_subscribers(subject):
            if sub.is_async:
                self._schedule_async(sub, event)
            else:
                self._dispatch_sync(sub, event)
        return event

    def _schedule_async(self, sub: Subscription, event: Event) -> None:
        loop = self._loop or self._ensure_private_loop()
        coro = sub.handler(event.subject, event.payload)
        future = asyncio.run_coroutine_threadsafe(self._wrap_async(sub, event, coro), loop)
        # We don't await the future — fire-and-forget is the observer contract.
```

The bus's private loop runs in a daemon thread, started lazily on first async use. Authors who already own a chat-loop event loop may pass it in via `EventBus(transport=..., async_loop=their_loop)`. The private loop is sufficient for v2.0; tighter integration with the chat loop is v2.1.

## 9. Discovery

Shared with `HookRegistry` (per `spec-hooks.md` §7). At runtime startup:

1. Walk every installed extension via `axiom.extensions.discovery.discover_extensions()`.
2. For each `[[extension.provides]] kind = "hook"` block whose `events[]` matches a known *observer* event (per `spec-hooks.md` §4 taxonomy), build a `Subscription` and call `bus.subscribe(...)` (sync) or `bus.subscribe_async(...)` per the entry point's signature.
3. Walk `$AXIOM_HOME/hooks/<subject>.py` and `./.axiom/hooks/<subject>.py`. Subscribe similarly. Project-local subscribers get a first-sight trust prompt.
4. Log the registration count. `bus.subscribers_for(pattern)` (new diagnostic API) returns the live subscriber list.

A subscriber declared for a *known interceptor* event (e.g. `tool.pre_invoke`) routes to `HookBus.register` instead. Same registry, two destinations. The dispatch table is `spec-hooks.md` §4's taxonomy.

## 10. Implementation plan

TDD-first per project convention. One feature branch (`feat/event-bus-v2`), commits per step, fast-merge to main.

| # | Step | Test gate |
|---|---|---|
| 1 | New `axiom.infra.bus` package: `subjects.py` (matcher), `types.py` (Event, Subscription, FailMode), `transport.py` (Protocol). | `tests/infra/bus/test_subjects.py` (matcher); `test_types.py` |
| 2 | `InProcessTransport` — refactor today's `EventBus._dispatch` + JSONL log behind the Protocol, no behavior change. | `tests/infra/bus/test_in_process_transport.py` |
| 3 | New `EventBus` class composing transport + subscriber registry. Backward-compat: keep `axiom.infra.orchestrator.bus.EventBus` as a deprecation shim re-exporting from the new location. | `tests/infra/bus/test_event_bus_sync.py` (covers existing semantics) |
| 4 | `subscribe_async` + private async loop + `_schedule_async`. | `tests/infra/bus/test_event_bus_async.py` |
| 5 | `bus.errors` topic + structured error event. Replace silent swallow. | `tests/infra/bus/test_error_topic.py` |
| 6 | Subject migration shim — accept `fnmatch` glob with `DeprecationWarning`; provide `subject_compatible(pattern)` validator. | `tests/infra/bus/test_subject_migration.py` |
| 7 | Manifest discovery in `HookRegistry` — already specced in `spec-hooks.md` §7; this step wires the EventBus side. | Integration test in `tests/infra/test_hook_registry_observer_routing.py` |
| 8 | Migrate `hygiene/subscriber.py` and `diagnostics/subscriber.py` to manifest declarations. | Existing extension tests pass |
| 9 | Doc updates — `spec-aeos-0.1.md` cross-references EventBus subject syntax in §4.7 hook events. | n/a |

Estimated 5–6 commits, ~3–4 hours focused work. Lands before the hooks v1 implementation begins (hooks rides on bus v2's API).

## 11. Refactor cleanly — no compat shims

Per the project's no-shim rule (we have no external users yet, so backwards-compat shims are pure debt for nobody's benefit), every rename in this spec is a clean refactor with all call sites updated in one commit. No deprecation cycles, no re-export shims, no read-only-property aliases.

Concrete refactors that land atomically:

- **Module move** — `axiom.infra.orchestrator.bus` → `axiom.infra.bus`. Every importer is updated in the same commit. The old path is deleted, not aliased.
- **Field rename** — `Event.topic` → `Event.subject`; `Event.data` → `Event.payload`. Every constructor call and field-access site is updated. Old field names disappear.
- **Subject syntax** — `fnmatch` glob is removed. Existing patterns that use `*` to mean multi-token (rare) are rewritten to `>` in the same commit. The matcher only parses NATS subjects.
- **Subscribe API** — `bus.subscribe(pattern, handler)` calls are updated to pass explicit `priority` and `fail_mode` where the new behavior matters. Default values mean most call sites need no change, but the old method signature is gone.
- **`fail_mode` default** — `warn` from day one. Today's effective `ignore` (silent swallow) was a real bug; we don't preserve it.

The migration script (`scripts/migrate_bus_subjects.py`) is a one-shot; it lives in the same PR as the bus refactor and is deleted in the next commit. We do not ship migration scripts as ongoing tooling.

A single feature branch carries the bus refactor + every consumer update. The pre-push hook validates the full test suite passes against the post-refactor codebase. If the parallel Keplo session has live work in flight, the prompt-handoff doc gets updated so they pick up the new names from the start; we don't paper over the rename to spare them a rebase.

## 12. Tests

New under `tests/infra/bus/`:

- **`test_subjects.py`** — matcher exhaustive cases: `*` boundaries, `>` only-at-tail, lowercase enforcement, error on bad pattern.
- **`test_in_process_transport.py`** — accept persists to JSONL, iter_subscribers honors priority, durability_log_path is correct.
- **`test_event_bus_sync.py`** — publish→deliver cycle with sync subscribers; priority ordering; matches glob today.
- **`test_event_bus_async.py`** — async subscriber receives event without blocking publisher; private loop bootstraps on first async subscribe; supplied loop is honored.
- **`test_error_topic.py`** — sync handler raises → `bus.errors` event published; abort/warn/ignore semantics; `bus.errors` is itself subscribable; cycle prevention (handler that fails on `bus.errors`).
- **`test_subject_migration.py`** — `tool.*` (fnmatch-style multi-token) emits DeprecationWarning; valid NATS pattern doesn't.
- **Integration: `tests/infra/test_hook_registry_observer_routing.py`** — manifest declares observer event → bus.subscribe is called; manifest declares interceptor event → HookBus.register is called.

Total: ~30 new tests. All TDD'd before implementation.

## 13. Open questions

These need a decision in the implementation commit messages:

- *Default `fail_mode`.* Today's effective behavior is `ignore` (silent). v2.0 default is `warn`. Acceptable noise increase, or ship `ignore` and tighten over a deprecation cycle? Default position: `warn` from day one.
- *Async loop ownership.* Bus owns a private daemon-thread loop in v2.0. When the chat loop is asyncio-native (eventually), do they share? Default position: bus's private loop is fine; chat loop uses `loop=`.
- *`bus.errors` recursion.* A handler that fails on `bus.errors` is demoted to `ignore` — is that the right rule, or should it be silently dropped without a record? Default position: still publish a `bus.errors` event with a marker indicating recursion-detected, but with the demoted handler removed for the rest of the session.
- *Should `subscribe_async` accept sync handlers and wrap them in `asyncio.to_thread`?* Convenience; consenting adults; default position: yes, with a one-line warning that the author probably meant `subscribe`.

---

*Companion product requirements:* [`prd-event-bus.md`](../prds/prd-event-bus.md).
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
