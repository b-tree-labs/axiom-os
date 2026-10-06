# Platform Hooks — Product Requirements

**Status:** Draft  •  **Owner:** Benjamin Booth  •  **Last updated:** 2026-04-24
**Audience:** Extension authors, integrators, anyone writing automation that needs to observe or interpose on Axiom's runtime.

---

## 1. What problem this solves

Today an Axiom extension can declare hooks in its manifest (AEOS §4.7), but the platform itself does not fire them. There is no way to:

- Audit every tool call (cost tracking, compliance)
- Deny or rewrite a tool call before it executes (permissions, classification)
- Run code on session start / end / pause
- Notify a webhook when a federation event happens
- Compose pre-prompt context (project rules, principal-specific overrides)

These are all things peer harnesses (Claude Code, Cursor's lifecycle scripts, LangGraph's middleware, OpenAI Agents' guardrails) already let users do — and that Axiom users currently can't do without forking the runtime.

**Platform hooks** are the missing harness-level interception + notification surface. They:

- Lift AEOS §4.7's *extension-scope* hook contract to *platform scope* — the harness itself fires them, not just individual extensions.
- Supply a fixed, named taxonomy of lifecycle events (the platform's API).
- Honor the AEOS-declared `priority` and `fail_mode` per hook.
- Discover hooks from manifests automatically — extension authors declare in TOML; nothing else.

## 2. Who this is for

| Persona | What hooks unlock |
|---|---|
| **Extension author** | Add cross-cutting behavior (audit, cost meter, prompt injection guard) that fires for every interaction without modifying the runtime. |
| **Operator / sysadmin** | Plug compliance, observability, and approval workflows in via local hooks at `$AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)` without writing an extension. |
| **Classroom instructor** | Wire student-progress signals: "fire this skill suggestion when a student asks the same question 3+ times in 10 minutes." |
| **Federation participant** | Validate, sign, or sideline incoming federation messages before they touch the local trust graph. |
| **Security reviewer** | Verify that the deterministic gates the Axiomatic Way mandates are actually in the data path — auditable hook chain instead of trust-by-assertion. |

## 3. Two flavors, one mental model

Hooks come in two flavors. Authors choose by what they want to do.

### 3a. Observer hooks (notifications)

> *"Tell me when something happened. I won't change it."*

Use for: audit log writes, cost meter increments, telemetry, cohort-wide pattern matchers, classroom signal extractors, downstream pipeline triggers.

Observer hooks subscribe to events on the existing `EventBus` (the platform pub/sub at `axiom.infra.orchestrator.bus`). Multiple observers may listen to the same event; they run in priority order; an observer that raises follows its declared `fail_mode`. Observers cannot change the event payload or block the operation that produced it.

### 3b. Interceptor hooks (gates)

> *"Let me see this BEFORE it runs. I might modify it, deny it, or pause for approval."*

Use for: permission gates, classification rewriters, secret scrubbers, RACI approval, slash-command argument expansion, federation message validation.

Interceptor hooks register with the new `HookBus` (this PRD's deliverable). They run synchronously in priority order. Each returns a `HookResult` saying: continue (with optional modified args), deny (with reason), or request approval (block on human signal). The first deny / approval-required wins — subsequent hooks in the chain don't run.

The shapes are intentionally different. Mixing them confuses authors; keeping them separate lets each contract be exactly what its consumers need.

## 4. The lifecycle event taxonomy

Names follow `<scope>.<verb>` lowercase-snake. The platform fires every event it declares; extensions and user-level hooks subscribe.

### Interceptor events (HookBus)

| Event | Fires before | Hook may | Typical use |
|---|---|---|---|
| `tool.pre_invoke` | Any tool call dispatched by an agent | Modify args, deny, request approval | Permissions, classification, RACI gating, secret scrubbing |
| `prompt.pre_submit` | Composed system prompt sent to a model | Modify prompt blocks, append context, deny | Project rules injection, prompt-injection scrubbing, principal-specific overrides |
| `extension.pre_install` | An extension is about to be installed | Deny, request approval | Trust profile enforcement, classified-content review |
| `federation.pre_accept` | An inbound federation message is about to enter the local trust graph | Deny, modify, request approval | Cohort policy enforcement, classification ceiling, anomaly detection |

### Observer events (EventBus)

| Event | Fires after | Typical use |
|---|---|---|
| `tool.post_invoke` | Any tool call completes (success or failure) | Cost meter, audit log, telemetry |
| `prompt.post_submit` | A model response returns | Cost meter, response logging |
| `session.started` | A new chat or agent session begins | Per-session state setup, identity binding |
| `session.ended` | A session closes (graceful or abort) | Per-session state flush, summary write |
| `extension.post_install` | An extension finished installing | Trust-graph update, federation announcement |
| `federation.post_accept` | A federation message was accepted | Cohort propagation, peer-state update |

The taxonomy is closed at v1 — adding a new event is a deliberate spec change, not an ad-hoc emit.

## 5. Authoring a hook

### 5a. Manifest declaration (the one-liner an extension adds)

```toml
[[extension.provides]]
kind = "hook"
events = ["tool.pre_invoke"]
entry = "diagnostics.hooks:rate_limit_check"
priority = 100
fail_mode = "abort"
description = "Per-principal rate limiting on all tool invocations"
```

`priority` runs lower numbers first (matches Linux nice / systemd convention). `fail_mode` is `abort` (exception bubbles, blocks the call), `warn` (logged + a `status("warn", ...)` line, call continues), or `ignore` (silently swallowed).

### 5b. The hook function

For interceptor hooks, the signature is:

```python
from axiom.infra.hooks import HookContext, HookResult, allow, deny, modify

def rate_limit_check(ctx: HookContext) -> HookResult:
    principal = ctx.principal
    if not _within_budget(principal, ctx.event):
        return deny(reason="principal exceeded daily budget")
    return allow()
```

For observer hooks, the signature mirrors the existing `EventBus.subscribe` callable:

```python
def cost_meter(topic: str, data: dict) -> None:
    if topic == "tool.post_invoke":
        _cost_ledger.record(data["principal"], data["model"], data["tokens"])
```

### 5c. User-level hooks (no extension required)

Drop a Python file at `$AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)<event>.py` and Axiom auto-registers it. This mirrors Claude Code's `.claude/hooks/` and `.claude/commands/` patterns — a single user can add an audit trail or cost meter without authoring an extension.

## 6. Trust and safety

Hooks shape behavior; they never grant capability (Axiomatic Way principle #4). Specifically:

- A hook can DENY or MODIFY a call within the bounds the principal is already authorized for.
- A hook cannot ELEVATE a principal's permissions, bypass the trust graph, or skip federation policy. Those are deterministic-code paths upstream of hook dispatch.
- A tampered hook produces *misbehavior* (incorrect denial, log noise), never *privilege escalation*. The threat model matches `spec-security.md §2.3`.

Discovery follows AEOS extension trust: hooks shipped by signed, trusted extensions run automatically; hooks at `$AXIOM_HOME/hooks/ (default ~/.axiom/hooks/)` are user-trusted (the user installed them). Federation cannot push hooks into a peer's runtime; that's outside the trust boundary.

## 7. What hooks are not

- Not a place to put core domain logic. Move that into a tool, service, or agent. Hooks are *cross-cutting*; if every hook for an event has the same body, the body belongs in the platform.
- Not a way to handle long-running work synchronously. An observer that needs to do real I/O should enqueue to a worker, not block the producing call.
- Not a substitute for the trust graph, the policy engine, or RACI. Hooks supplement; they don't replace.
- Not a webhook receiver — that's `event-driven triggers`, a separate parity gap, with HTTP plumbing the hook system doesn't need.

## 8. Limits in v1

- **Synchronous interceptors only.** A hook on `tool.pre_invoke` blocks the tool call until it returns. Long work must be offloaded.
- **In-process by default.** Hooks run inside the same Python process as the agent that triggered them. Cross-process delivery rides the federation transport when it ships (see §11 follow-up #2).
- **No async observer mode in v1.** Observers are sequential by default. The async surface is follow-up #1, not punted indefinitely.
- **No "session-pause" interceptor.** The interceptor surface is the four pre-events listed in §4. We don't expose mid-tool-call cancellation; that's an interrupt-streaming concern (a separate parity gap).

## 9. Concurrency safety

Sequential dispatch order is a v1 default; *thread-safety* is not negotiable. The implementation guarantees:

- The hook registry, the subscriber list, and the `bus.errors` topic all mutate under a lock. Registering a hook while events are firing is safe; the new hook either fires for the next event or doesn't, never partially.
- Each handler invocation runs in isolation — a handler that mutates shared state is responsible for its own synchronization, but the bus does not interleave mutations across handlers' frames.
- The JSONL durability log uses `axiom.infra.state.locked_append_jsonl` (filelocked across processes), so multiple agent processes writing to the same log don't corrupt it.
- Async observers (when added; see §11) run on isolated tasks within a structured task group; cancellation of one observer doesn't cascade to siblings.

The spec records concurrency invariants explicitly so they're verified by tests, not assumed by code review.

## 10. Real-life scenarios these unlock

Two of the follow-ups in §11 — cross-process delivery and replay-as-test — are abstract in the abstract. Concretely:

### 10a. Cross-process / federated hooks

- **Federated audit ledger.** A research consortium (UT, OSU, INL) requires every tool invocation across member institutions to be logged centrally for compliance. Each peer's local `tool.post_invoke` fires; the consortium-audit observer subscribes cross-federation and receives signed envelopes from each peer. One audit log; many runtimes; no special-case code per-peer.
- **Cross-institution rate limiting.** UT student logs in via federation from OSU. OSU's chat agent fires `tool.pre_invoke`. UT owns the principal, so UT's rate-limit interceptor subscribes cross-federation for principals it owns; the interceptor sees OSU's `tool.pre_invoke` and can deny if the student is over their daily LLM-call budget — *even though the call is happening on OSU's runtime*.
- **Cohort-wide classroom signal extraction.** Prague: 30 students, each on their own laptop, federated to the instructor's coordinator. Each laptop's chat agent fires `tool.post_invoke` locally. The coordinator's signal extractor subscribes cross-federation (with student consent expressed in the trust profile) and runs cohort-wide pattern matchers — "5 students stuck on the same topic in the last 10 minutes" — without each student running the matcher locally.
- **Federation-aware classification gate.** Inbound `federation.pre_accept` from a peer; a hook on the receiving side checks the message classification stamp against the local node's classification ceiling. If the ceiling is `"internal"` and the message is `"export-controlled"`, the hook returns `deny()` with a reason that propagates back to the sending peer.

### 10b. Replay-as-test

- **Refactor regression.** Refactoring the gateway. Want to verify the same hooks fire identically before and after. Replay the JSONL log against a fresh subscriber set, capture each handler's invocation sequence + return value, diff the two runs.
- **Reconstructing a session.** A user reports their session went off the rails. Take their JSONL log from `$AXIOM_HOME/runtime/events.jsonl`, replay against a debug-instrumented bus, see exactly which hooks fired in what order and what they returned. No guesswork.
- **Compliance evidence.** "Show us the audit trail for September 15." Replay that day's events, capture every hook invocation, produce a deterministic audit report that an inspector can independently verify by re-running the replay.

## 11. Follow-up tasks (queued, not punted)

These are real next-up tasks. Each lands in its own commit or short branch after v1 hooks ships. Calling them "v2" implies someday-maybe; they're queued.

1. **Async dispatch** — `async def hook(...)` and `subscribe_async` for observers. Pairs with the chat loop becoming asyncio-native.
2. **Cross-process hook delivery** — federation transport carries hook events between peers. Same Transport seam EventBus v2 already exposes (see `spec-event-bus.md`). See §10a for what this unlocks.
3. **Replay-as-test mode** — re-run all observer hooks against the JSONL log. See §10b.
4. **Hot-reload of `$AXIOM_HOME/hooks/`** — watchdog-based file-system reload without restarting the agent.
5. **Multi-modal payload typing** — first-class image / audio / file references in `tool.pre_invoke` and `prompt.pre_submit` payloads. See §13 schema versioning notes.
6. **Pluggable priority strategies beyond the two ships in v1** — see §12.
7. **Federation hook surface** — the full event taxonomy described in `prd-federation.md`'s federation-hooks section. Includes `federation.peer_joined`, `federation.trust_changed`, `federation.classification_violation`, etc.

## 12. Priority strategy

Priority resolves hook-execution order within an event. v1 ships **two strategies**; the active one is selectable via configuration:

- **`manifest_priority` (default).** Order by the `priority` field declared in each hook's manifest. Lowest number runs first. Manifest authority — what's declared is what runs.
- **`trust_weighted`.** Higher-trust extensions run before lower-trust ones for the same event; ties broken by manifest priority. Useful when a deployment wants signed-by-the-institution hooks to always pre-empt user-installed ones.

The strategy is a `PriorityStrategy` Protocol (see `spec-hooks.md` §6.5). Custom strategies are plug-in: a deployment can author its own (e.g., "alphabetical by extension name" for fully predictable ordering across forks).

## 13. Type-safety stance

Per-event payloads are typed-optional. Authors can write hooks against:

- **Loose dicts** — `def hook(ctx: HookContext) -> HookResult` where `ctx.payload: dict[str, Any]`. Fast to author, no friction. Forward-compatible (new keys appear, old keys stay).
- **TypedDicts** — `def hook(ctx: HookContext[ToolPreInvokePayload]) -> HookResult`. The platform ships TypedDicts per known event; authors opt in for static checking.
- **Custom types** — extension authors who wrap an event in their own dataclass can do so; the platform doesn't enforce a particular type. Open-ended structures accommodated.

**Multi-modal expansion is intentionally not blocked.** When `tool.pre_invoke` payloads grow image / audio / video / file references (post-v1, follow-up #5), hooks written against the v1 TypedDict still work; hooks that want the new keys opt into a v2 TypedDict. The runtime does not enforce a particular media-reference shape — it passes whatever's in the payload through to handlers.

## 14. Success criteria

- An audit log can be wired by writing one extension manifest entry + one Python function — no runtime patches.
- A cost meter ships in under 200 lines of total code.
- The same hook authored as a manifest declaration runs identically as a user-level `$AXIOM_HOME/hooks/` (default `~/.axiom/hooks/`) file.
- Existing `subscriber.py` consumers (`hygiene`, `diagnostics`) migrate to the new manifest-driven discovery without behavior change.
- All four interceptor events have at least one example in `docs/specs/spec-hooks.md` and at least one passing integration test.
- The Axiomatic Way doctrine gains a principle (or extends an existing one) covering platform hooks; the AEOS spec §4.7 is unchanged (it already defines the shape).

---

*Companion technical spec:* `docs/specs/spec-hooks.md`.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
