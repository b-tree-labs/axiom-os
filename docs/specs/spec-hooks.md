# Platform Hooks — Technical Specification

**Status:** Draft  •  **Owner:** Benjamin Booth  •  **Last updated:** 2026-04-24
**Companion PRD:** [`prd-hooks.md`](../prds/prd-hooks.md)

---

## 1. Goals

- Lift AEOS §4.7 hook semantics to platform scope: the harness fires a fixed taxonomy of lifecycle events, extensions and user-level hooks subscribe.
- Honor `priority` and `fail_mode` per declared hook (existing `EventBus` ignores both).
- Auto-discover hooks from `[[extension.provides]] kind = "hook"` manifest entries and `$AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)` filesystem drops — no manual `bus.subscribe()` calls in extension boot code.
- Distinguish *interceptors* (synchronous, ordered, may modify or deny) from *observers* (notification, may reorder/swallow). Two primitives, two contracts; one mental model.
- Reuse the existing `EventBus` for the observer side; add the minimum new code for the interceptor side.
- Land Bronze-conformance fixtures in `axiom-tests` so extension authors can test their hooks.

## 2. Out of v1 (queued, not punted)

These ship in subsequent commits per `prd-hooks.md` §11 follow-up list — they are real next-up tasks, not "deferred to someday":

- **#1: Async dispatch** (`async def` handlers + `subscribe_async`). Sequential observer order in v1; parallel fan-out lands when needed. Concurrency *safety* is in v1 — see §11.
- **#2: Cross-process delivery.** Hooks fire within one Python process in v1. Cross-process rides the federation transport when EventBus v2's Transport seam carries a JetStream/PG-LISTEN backend.
- **#3: Replay-as-test.** Half-supported by `EventBus.replay()` today; not reworked here.
- **#4: Hot-reload of `$AXIOM_HOME/hooks/`.** Restart-required in v1; watchdog-based reload comes after.
- **#7: Federation hook surface.** Specced in `prd-federation.md` and `spec-federation.md`'s federation-hooks section. `federation.pre_accept` and `federation.post_accept` ship in v1 (already in §4); the rest land with the federation hook work.

A new RPC or wire format is NOT on the queue — manifest declarations + Python function dispatch are sufficient for the foreseeable future.

## 3. Architecture

Two primitives, sharing manifest discovery + fail-mode + priority machinery:

```
                ┌──────────────────────────────────────────┐
                │ axiom.infra.hooks.HookRegistry            │
                │   • parses manifest + $AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)        │
                │   • validates HookSpec (events, priority,  │
                │     fail_mode, entry)                      │
                │   • routes each hook to HookBus or         │
                │     EventBus based on the event's tier     │
                └──────────────────────────────────────────┘
                         │                      │
              interceptors│                       │observers
                         ▼                      ▼
            ┌─────────────────────┐   ┌────────────────────────────┐
            │ HookBus (new)        │   │ EventBus (existing, +polish)│
            │ • synchronous         │   │ • synchronous (today)        │
            │ • priority-ordered    │   │ • priority-ordered (new)     │
            │ • returns HookResult  │   │ • fail_mode honored (new)    │
            │ • short-circuits on   │   │ • durable JSONL log          │
            │   deny or approval    │   │ • glob subscriptions          │
            └─────────────────────┘   └────────────────────────────┘
```

**Why two primitives, not one.** Interceptor return values and short-circuit semantics are load-bearing for permissions/RACI/classification. Folding them into an observer that ignores returns either loses the interception contract (bad) or surfaces it as a special opt-in flag (worse — confuses authors). Two clean primitives, each with the contract its consumers need.

**Why reuse `EventBus`.** It already handles glob patterns, has durable JSONL logging + replay, and is consumed by `hygiene/subscriber.py` and `diagnostics/subscriber.py`. The needed upgrades (priority, fail_mode) are additive.

## 4. The lifecycle event taxonomy

Closed at v1 — adding an event is a deliberate spec change.

### Interceptor events (HookBus)

| Event | Payload | Hook return | Fired from |
|---|---|---|---|
| `tool.pre_invoke` | `{tool_name, args, principal, classification, ext_origin}` | `allow()` / `allow_modified(args)` / `deny(reason)` / `request_approval(why)` | `axiom.infra.gateway` tool-dispatch entry point |
| `prompt.pre_submit` | `{messages, system_layers, principal, model_id}` | `allow()` / `allow_modified(messages, system_layers)` / `deny(reason)` | `axiom.infra.prompt_composer` flush before transport |
| `extension.pre_install` | `{name, version, manifest, signature, source_url}` | `allow()` / `deny(reason)` / `request_approval(why)` | `axi ext install` after signature verify, before unpack |
| `federation.pre_accept` | `{message, peer_principal, classification, signature_chain}` | `allow()` / `allow_modified(message)` / `deny(reason)` | `axiom.vega.federation.receive` before trust-graph commit |

### Observer events (EventBus)

| Event | Payload | Fired from |
|---|---|---|
| `tool.post_invoke` | `{tool_name, args, result, error, latency_ms, principal, model_id, tokens}` | `axiom.infra.gateway` tool-dispatch exit |
| `prompt.post_submit` | `{messages, response, latency_ms, principal, model_id, tokens, cost_usd}` | `axiom.infra.gateway` after model response |
| `cli.command_started` | `{command_path, args, principal, started_at}` | `axiom_cli.py:main` at top-level dispatch entry |
| `cli.command_ended` | `{command_path, exit_code, duration_ms, ended_at}` | `axiom_cli.py:main` at process exit |
| `extension.post_install` | `{name, version, install_path, manifest}` | `axi ext install` after pip + state record |
| `federation.post_accept` | `{message, peer_principal, accepted_at}` | `axiom.vega.federation.receive` after trust-graph commit |

#### Platform vs. extension event ownership

The taxonomy above is the **platform**'s closed set — events fired by runtime infra (`axiom.infra.*`) and ships-with-the-distribution-but-extension-agnostic CLI surfaces. They are stable, documented, and version-bounded.

**Extensions own their own event namespaces.** A built-in extension that has a session-like concept emits its own namespaced events; the platform does not extend its own taxonomy to cover them. Examples:

| Extension | Events it emits | Notes |
|---|---|---|
| `chat/` | `chat.session.started`, `chat.session.ended`, `chat.turn.started`, `chat.turn.ended` | The chat-loop lifecycle. AXI-rebranded consumer layers (e.g., a domain consumer's own agent) emit the same `chat.*` events; rebranding is a presentation concern, not an event-namespace concern (Axiomatic Way #7). |
| `research/` | `research.session.started`, `research.session.ended`, `research.cycle.completed` | CURIO's autoresearch loop. |
| `signals/` | `cadence.tick`, `cadence.run.started`, `cadence.run.ended`, `signals.extracted` | SCAN's cadence + signal extraction. |
| `release/` | `release.published`, `release.tagged`, `release.pipeline.failed` | RIVET's CI/CD events (when RIVET actually runs). |
| `classroom/` | `classroom.tutoring.started`, `classroom.tutoring.ended`, `classroom.quiz.broadcast`, `classroom.cohort.signal` | CHALKE's classroom engagements. Owned by Keplo extraction target. |
| `federation/` | `federation.peer.joined`, `federation.peer.left`, `federation.trust.changed`, `federation.consortium.member_added`, `federation.classification.crossing` | Vega's federation lifecycle. See `spec-federation.md` §19.5. (`federation.pre_accept`/`post_accept` are platform-listed in the table above because they fire from `axiom.vega.federation.receive` runtime code; the rest are extension-emitted. Pragmatic split — same code path doesn't matter for the taxonomy boundary.) |

**Why the split.** The platform's closed taxonomy is what every consumer can rely on across versions. Extensions evolve their event namespaces independently — a classroom v2 might rename `classroom.tutoring.started` to something better without touching the platform spec. Subscribers that want generic semantics (e.g., "log every session start anywhere") use NATS-style glob patterns: subscribe to `*.session.started` to match `chat.session.started`, `research.session.started`, `classroom.tutoring.started`, etc.

**Agent-to-agent delegations** do NOT fire `*.session.started`. They fire as `tool.pre_invoke` / `tool.post_invoke` because the delegation runs through the tool-dispatch path (the called agent is a tool from the caller's perspective). If a future delegation pattern needs richer semantics, that's a separate event family; we don't conflate it with sessions.

#### Discoverability

Today, an extension's emitted events are documented in its README/spec but not formally declarable. A future AEOS addition (queued, not in v1) is an `[[extension.emits]]` manifest block:

```toml
[[extension.emits]]
event = "classroom.tutoring.started"
description = "CHALKE began a tutoring engagement"
payload_schema = "classroom.events:TutoringStartedPayload"  # optional TypedDict
```

That would enable `axi ext list-events` to enumerate every event the installed extension set might fire — a real DX improvement once the v1 hooks ship and people start writing subscribers.

#### Payloads + multi-modal

Payload schemas live in `src/axiom/infra/hooks/events.py` as TypedDicts, declared as `total=False` for forward compatibility (new keys may appear; old keys never disappear in the same major version). For multi-modal events:

```python
class ImageRef(TypedDict, total=False):
    uri: str          # axiom:// or file:// or https://
    inline_bytes: bytes
    media_type: str   # "image/png", "image/jpeg", etc.

class ToolPreInvokePayload(TypedDict, total=False):
    tool_name: str
    args: dict[str, Any]
    images: list[ImageRef]
    audio: list[AudioRef]
    files: list[FileRef]
    principal: str
    classification: str
    ext_origin: str
```

Authors who want static typing import the TypedDict; authors who want raw flexibility take `dict[str, Any]`. Both work against the same runtime. See PRD §13.

## 5. The `HookBus` interface

New module: `src/axiom/infra/hooks/__init__.py` (and friends).

```python
# src/axiom/infra/hooks/types.py
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

FailMode = Literal["abort", "warn", "ignore"]

@dataclass(frozen=True)
class HookContext:
    """Context passed to every interceptor hook."""
    event: str                     # e.g. "tool.pre_invoke"
    payload: dict[str, Any]        # event-specific dict (see §4)
    principal: str                 # who's running this
    cancellation_reason: str = ""  # populated by deny() chain

@dataclass(frozen=True)
class HookResult:
    """What an interceptor returns."""
    decision: Literal["allow", "modify", "deny", "approval_required"]
    modified_payload: dict[str, Any] | None = None
    reason: str = ""
    approval_token: str = ""

def allow() -> HookResult: ...
def allow_modified(**modifications: Any) -> HookResult: ...
def deny(*, reason: str) -> HookResult: ...
def request_approval(*, why: str) -> HookResult: ...
```

```python
# src/axiom/infra/hooks/hookbus.py
@dataclass(frozen=True)
class HookSpec:
    event: str
    entry: Callable[[HookContext], HookResult]
    priority: int = 100
    fail_mode: FailMode = "abort"
    source: str = ""  # extension name, "user", or "platform"

class HookBus:
    def register(self, spec: HookSpec) -> None: ...
    def fire(self, event: str, payload: dict[str, Any], principal: str) -> HookResult: ...
```

`HookBus.fire`:

1. Sort registered hooks for `event` using the active `PriorityStrategy` (see §5.5).
2. Call each hook in order with a `HookContext`.
3. If a hook raises:
   - `fail_mode = "abort"` → re-raise; the operation aborts. The caller turns this into the user-facing equivalent of "denied by hook".
   - `fail_mode = "warn"` → log structured warning, return `allow()` for this hook, continue.
   - `fail_mode = "ignore"` → log at debug, return `allow()`, continue.
4. If a hook returns `deny` or `request_approval`, short-circuit — don't run subsequent hooks. Return that result.
5. If a hook returns `allow_modified`, splice the modifications into the payload before calling the next hook (so subsequent hooks see the modified version).
6. If all hooks return `allow` (or `allow_modified`), return a final `allow()` (or `allow_modified` with the accumulated modifications).

Hook execution is synchronous and runs in the calling thread.

### 5.5. `PriorityStrategy` Protocol

Hook execution order is plug-in. v1 ships two strategies; the active one is selected via `axi config set hooks.priority_strategy <name>`:

```python
from typing import Protocol, Iterable

@runtime_checkable
class PriorityStrategy(Protocol):
    name: str

    def order(self, hooks: Iterable[HookSpec]) -> list[HookSpec]:
        """Return hooks in execution order. Lower-index = runs first."""
```

Two implementations live in `axiom.infra.hooks.priority`:

```python
class ManifestPriorityStrategy:
    """Order by manifest-declared priority. Default."""
    name = "manifest_priority"

    def order(self, hooks):
        return sorted(hooks, key=lambda h: (h.priority, h.source))

class TrustWeightedStrategy:
    """Higher-trust extensions run before lower-trust ones; ties by manifest priority."""
    name = "trust_weighted"

    def __init__(self, trust_lookup: Callable[[str], int]):
        self._trust = trust_lookup

    def order(self, hooks):
        return sorted(hooks, key=lambda h: (-self._trust(h.source), h.priority))
```

Custom strategies are plug-in via the `axiom.hooks.priority_strategies` entry-point group; a deployment registers `name = "alphabetical"` and points it at its own implementation. The `HookBus` resolves the active strategy at registration time and at every config-reload.

The strategy is consulted ONCE per hook registration (the registry caches an ordered list per event); re-ordering is cheap because hook registration is rare. Concurrent calls to `HookBus.fire` see a consistent ordering — the registry's mutex ensures the cached list is replaced atomically when a new hook registers.

## 6. Reused: `EventBus` upgrades

Existing `axiom.infra.orchestrator.bus.EventBus` keeps its public API. Two additive changes:

### 6a. Priority-ordered subscription

```python
def subscribe(
    self,
    pattern: str,
    handler: EventHandler,
    *,
    priority: int = 100,
    fail_mode: FailMode = "warn",
) -> None: ...
```

`_dispatch` sorts matching subscribers by priority before calling. Default `priority=100` matches AEOS §4.7's default. Existing call sites that don't pass `priority` get unchanged behavior.

### 6b. Fail-mode honored

The current `_dispatch` swallows ALL exceptions silently — that's wrong. Replace with:

```python
def _dispatch(self, event: Event) -> None:
    matching = [(p, h, prio, fm) for (p, h, prio, fm) in self._subscriptions if fnmatch.fnmatch(event.topic, p)]
    matching.sort(key=lambda t: t[2])  # by priority
    for pattern, handler, _prio, fail_mode in matching:
        try:
            handler(event.topic, event.data)
        except Exception as exc:
            if fail_mode == "abort":
                raise
            elif fail_mode == "warn":
                log.warning("event handler %r raised on %r: %s", handler, event.topic, exc)
            # ignore: do nothing
```

This is a behavior change — broken handlers no longer silently break. Default `fail_mode` is `"warn"` from day one. Subscribers that genuinely tolerate failure declare `fail_mode = "ignore"` explicitly. We don't ship a deprecation cycle for the silent-swallow behavior because that behavior was a real bug, not a contract.

## 7. Manifest-driven discovery

`HookRegistry` (new module) is invoked once at runtime startup. It:

1. Walks every installed extension via `axiom.extensions.discovery.discover_extensions()`.
2. For each extension's manifest, finds `[[extension.provides]] kind = "hook"` blocks.
3. For each block, imports the entry point (`module:symbol`), constructs a `HookSpec`, and routes it to either `HookBus.register` (interceptor events) or `EventBus.subscribe` (observer events) based on the event's known tier.
4. Walks `$AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)<event>.py` and `./.axiom/hooks/<event>.py` (project-local override). Each file may export `def hook(ctx)` (interceptor) or `def observer(topic, data)` (observer); the function name disambiguates.
5. Logs a structured summary: "Registered 14 hooks (8 interceptors, 6 observers)."

A discovered hook for an unknown event name is logged as a warning (not abort) — keeps forward compatibility when the runtime is older than the extension.

## 8. Wiring at call-sites

Three call-sites in this commit; the rest of the taxonomy can land later.

### 8a. `tool.pre_invoke` and `tool.post_invoke`

`axiom.infra.gateway.dispatch_tool` (or its current equivalent) wraps:

```python
ctx = HookContext(event="tool.pre_invoke", payload={...}, principal=...)
result = hookbus.fire("tool.pre_invoke", ctx.payload, ctx.principal)
if result.decision == "deny":
    raise ToolDenied(result.reason)
elif result.decision == "approval_required":
    raise ApprovalRequired(result.reason)
elif result.decision == "modify":
    args = result.modified_payload["args"]

# ... actually invoke the tool ...

eventbus.publish("tool.post_invoke", {..., "tokens": ..., "latency_ms": ...})
```

Identify the actual gateway entry point during implementation; update precisely once per call-site.

### 8b. `prompt.pre_submit` and `prompt.post_submit`

`axiom.infra.prompt_composer.PromptComposer.render_text` (or where the rendered prompt is shipped to the model) wraps similarly. The interceptor sees the rendered messages + system layers; modifications splice back into the composer state.

### 8c. `session.started` / `session.ended`

`chat/agent.py`'s session entry/exit. Observer-only — no decision to honor.

`extension.pre_install`, `extension.post_install`, `federation.pre_accept`, `federation.post_accept` are wired analogously when their consumer code is touched. Initial implementation may stub `extension.*` and `federation.*` to fire-but-no-op until those subsystems are ready.

## 9. Migration of existing subscribers

Two subscriber files exist today: `hygiene/subscriber.py` and `diagnostics/subscriber.py`. They manually call `bus.subscribe(...)` in extension boot code.

Migration path:

1. Each adds a `[[extension.provides]] kind = "hook"` block to its manifest declaring the events it consumes.
2. The subscribe-in-init code is removed; `HookRegistry` does it at runtime startup.
3. Behavior is unchanged from the user's perspective — the same handler runs on the same events.

This is a chore, not a feature commit. It can land alongside or after the HookBus introduction.

## 10. Trust model

- Hooks discovered from a signed extension run with the extension's trust profile.
- Hooks at `$AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)` run with user trust (the user installed them); a warning is emitted at startup naming each user-level hook so the user sees what they're running.
- Hooks at `./.axiom/hooks/` (project-local) run with project trust + an extra confirmation on first sight (`unknown project hook X — trust this run? [y/N]`).
- No hook can ELEVATE trust. Hooks shape behavior within already-granted permissions; the deterministic policy/classification engines upstream of HookBus dispatch are the trust boundary (Axiomatic Way #4).

A hook crashing with `fail_mode = "abort"` is reported as the call's denial reason, not as a runtime error — users see "denied by `<extension>:<hook>` — <message>" rather than a stack trace. Stack traces go to the diagnostic log.

## 10.5. Concurrency safety

Sequential dispatch order is v1's default; thread safety is non-negotiable. Verified by tests, not assumed.

### Invariants

1. **Registry mutation is atomic.** Adding, removing, or re-prioritizing a hook holds a single `threading.RLock` covering both the `HookBus` registry and the `EventBus` subscription list. Hooks fired during a registry mutation either see the pre-mutation set in full or the post-mutation set in full — never a partial view.
2. **Per-event ordered list is immutable in flight.** When `HookBus.fire(event, ...)` runs, it captures a snapshot of the event's ordered hook list under the registry lock, then releases the lock and iterates the snapshot. New registrations during the iteration affect the *next* fire, not this one.
3. **Handler-frame isolation.** The bus does not interleave handler executions for the same event. A handler that mutates shared state is responsible for its own synchronization, but the bus does not call other handlers' frames during that handler's execution.
4. **JSONL durability log is process-safe.** All writes go through `axiom.infra.state.locked_append_jsonl`, which uses `fcntl`-locked append-only writes. Multiple agent processes writing to the same log don't corrupt each other.
5. **`bus.errors` topic respects all of the above.** A subscriber failure publishes to `bus.errors` from within the same dispatch call; the registry lock is held for the publish; recursive `bus.errors` failures are demoted to `ignore` to prevent infinite loops.
6. **Async observers (when added; v2 follow-up #1) run on a structured `asyncio.TaskGroup`.** Cancellation of one async observer does not cascade to siblings. Async + sync observers for the same event can coexist; the sync ones run inline, the async ones are scheduled.

### What this means for handler authors

- Sync handlers run on the publisher's thread. If the publisher is the gateway calling out to a tool, that's the gateway's worker thread. Don't block on I/O; if you need to, schedule async (when async ships) or spawn a worker.
- Hooks read the same payload object that subsequent hooks see (post-modification). If your hook mutates the payload dict in-place (instead of returning `allow_modified`), other hooks see your changes — that's a contract foot-gun. Return `allow_modified` with explicit modifications instead.
- Two different events firing concurrently is normal — the bus does not serialize across events. If your handler is registered on both `tool.pre_invoke` and `prompt.pre_submit`, two threads may run it simultaneously. Your handler must be reentrant.

### Tests

Concurrency safety is enforced by `tests/infra/test_hookbus_concurrency.py`:

- 8 threads firing the same event simultaneously while a 9th thread registers and unregisters hooks; assert no exceptions, no missed firings, no double-firings.
- Hook handler that publishes to `bus.errors` from within its own failure path; assert no infinite loop.
- Async observer cancellation propagation (when async ships): cancelling task A doesn't cancel task B.

## 11. Tests

New under `packages/axiom-tests/src/axiom_tests/fixtures/hooks/` (the existing axiom-tests pattern):

- A `mock_hookbus` fixture that records every `fire()` call so extension tests can assert their hook fired with the expected payload.
- A `mock_eventbus_subscriber` fixture mirroring the same.
- A pytest marker `@pytest.mark.hook("tool.pre_invoke")` that auto-wires the mock for the test's scope.

New tests under `tests/infra/`:

- `test_hookbus.py` — priority ordering, fail_mode (all three), short-circuit on deny / request_approval, payload modification splicing.
- `test_hook_registry.py` — manifest discovery, user-dir discovery, project-dir discovery + first-sight confirmation, unknown-event-name warning, signature trust mapping.
- `test_eventbus_priority_failmode.py` — priority sort, fail_mode behavior on raises.
- `test_gateway_hook_wiring.py` — round-trip integration: a registered hook fires on a real tool call.

Tests are TDD-first per project convention.

## 12. Refactor cleanly — no compat shims

Per the project's no-shim rule (we have no external users yet, so backwards-compat shims are pure debt for nobody's benefit), the hooks v1 work updates every consumer in the same commit that introduces the new primitives.

- `EventBus` API gains `priority` and `fail_mode` kwargs. Existing call sites that don't pass them get defaults (`priority=100`, `fail_mode="warn"`) — no separate deprecation cycle for the kwargs themselves; they're just additive.
- `hygiene/subscriber.py` and `diagnostics/subscriber.py` boot-time subscribe code is **deleted** in the same commit that adds the manifest declarations. We don't ship the two paths in parallel.
- `bus.errors` topic replaces today's silent-swallow at `bus.py:149` immediately. No "still allow silent in some cases" toggle.

## 13. Implementation plan

Tight branch, TDD throughout, fast-merge on green.

| # | Step | Test gate |
|---|---|---|
| 1 | New `axiom.infra.hooks` package: `types.py` (HookContext, HookResult, factories), `hookbus.py`, `event_schemas.py` (TypedDicts). | `test_hookbus.py` (priority, fail_mode, short-circuit) |
| 2 | `EventBus` upgrades: priority + fail_mode kwargs in `subscribe`, dispatch uses both. | `test_eventbus_priority_failmode.py` |
| 3 | `HookRegistry` discovery: manifest-walking + user/project hook directory loading. | `test_hook_registry.py` |
| 4 | Wire `tool.pre_invoke` + `tool.post_invoke` at the gateway. | `test_gateway_hook_wiring.py` |
| 5 | Wire `prompt.pre_submit` + `prompt.post_submit` at the composer. | Integration test in `tests/infra/test_prompt_hook_wiring.py` |
| 6 | Wire `session.started` + `session.ended` at the chat agent. Sister-session-aware: chat/ is being evolved; this commit touches the loop entry/exit only and avoids deeper changes. | Integration test in `tests/infra/test_session_hook_wiring.py` |
| 7 | Migrate `hygiene/subscriber.py` and `diagnostics/subscriber.py` to manifest declarations. | Existing extension tests still pass |
| 8 | `axiom-tests` fixtures: `mock_hookbus`, `mock_eventbus_subscriber`, `@pytest.mark.hook`. | Self-test via the existing axiom-tests test suite |
| 9 | Doctrine update: extend Axiomatic Way principle #4 (or add #12) to call out platform hooks. AEOS §4.7 stays unchanged — already specifies the manifest shape. | n/a |

Estimated commits: 5–6 (steps 1–2 in one, 3 alone, 4–6 each, 7+8 in one, 9 in one).

## 14. Open questions

These need resolution before implementation, but resolution can land in the implementation commit messages rather than blocking the spec.

- *Where does session originate for `session.started`?* The chat agent emits today; if `axi research start` should also emit, it needs to call into the same hook path. Default v1: chat/ is the sole emitter; `research/` follows in v2.
- *Federation events firing locally.* `federation.pre_accept` fires on the receiving peer. If both peers want to observe their counterpart's accept, that's cross-process — explicitly out of scope (§2). v1: receiving peer only.
- *User-level hook hot-reload.* Today, restarting axi reloads `$AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)`. A watchdog-based hot-reload is appealing; not in v1.
- *Payload schema versioning.* Authors writing against `tool.pre_invoke` v1's payload schema — what happens when v2 adds a key? Default: payloads are forward-compatible (additive); breaking changes get a new event name. Documented in §4 and the per-event TypedDict docstrings.

## 15. Success criteria

This spec is right when:

- An extension author can implement audit logging with one manifest entry + one Python function — no runtime patches.
- A cost meter ships in <200 LOC including tests.
- `mirror_agent`-style content gating could be reimplemented as a single `tool.pre_invoke` hook — proving hooks subsume what dedicated retired-agent surfaces did.
- The `subscriber.py` files in `hygiene/` and `diagnostics/` are gone; their behavior is preserved via manifest hook declarations.
- Bronze AEOS conformance for an extension's hooks is verified by an `axiom-tests` fixture, not by reading code.

---

*Companion product requirements:* [`prd-hooks.md`](../prds/prd-hooks.md).
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
