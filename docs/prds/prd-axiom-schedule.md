# PRD: `axiom.schedule` — App-Level Domain Scheduler (PULSE)

**Status:** Draft (2026-05-30; **§11 normative requirements added 2026-06-08** from the PULSE-1 build)
**Owner:** Benjamin Booth
**Companion ADR:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md)
**Companion Spec:** [spec-governance-fabric.md](../specs/spec-governance-fabric.md) §5.5 (schedule firing), §6 (idempotency, retries, dead-lettering), §8.4 (schedule schema)
**Primitive class:** AEOS built-in extension (`axiom.extensions.builtins.schedule`)
**Agent:** PULSE (Orchestrator)
**Tracking issue:** [axiom-os#277](https://github.com/b-tree-labs/axiom-os/issues/277)
**Sibling layer:** axiom-os#274 (`axi schedule install` host-unit lifecycle); PULSE is the *app-level domain event* scheduler; #274 is the *OS unit* scheduler. The two compose; neither replaces the other.

---

## 1. Elevator Pitch

PULSE is the platform's app-level scheduler: fire a domain event at time T or interval I, with retry, idempotency, capability-token authentication, classification-aware routing, RACI graduation, and federation handoff. Schedules consume the same action envelope every other primitive does, so a scheduled SLA timer ("lab order LO-4821 stuck in `processing` >24h → notify the lab manager") inherits provenance and classification automatically; the operator notification graduates from proposal to autonomous per RACI; the receipt is a queryable memory fragment. Peer-defined cadences can fire on our hardware under capability-bound authority granted by their KEEP. No peer harness has this.

## 2. Problem / Opportunity

### What's broken today

- **No app-level scheduler.** Cloud routines exist via `RemoteTrigger` (partial: cron + one-shot from any session, tracked by RIVET). But manifest-declared `[[agent.cadence]]` for user-built extensions doesn't ship. SCAN's watchers, TIDY's heartbeat-checks, RIVET's CI poll — all reinvent scheduling per agent.
- **Domain events have nowhere to fire.** "A work order stuck in a process step past its limit" → no platform mechanism. Each extension rolls its own background loop, badly.
- **Idempotency is per-call-site.** Every cron-style code path gets its own dedupe + retry; some get them wrong; failures look like silent skips.
- **No memory-context binding.** When a scheduled job fires, what memory context does it run under? Today: whichever context the registering caller happened to have. The receipts don't make this explicit.
- **No federation.** A peer cohort cannot schedule an action against our hardware (or vice versa). Federation's primitives are mature enough; nothing exercises them for schedules.
- **No RACI integration.** A novel scheduled action class has no default disposition; either it fires or it doesn't; the operator has no "approve once, autonomous thereafter" UX.
- **Cadence + retry + jitter + dead-letter are reinvented.** Each consumer codes their own. Some get retries wrong; some never dead-letter; failures pile up invisibly.

### Why now

- The first major domain extension lands in 2026-06; its Phase 2 needs SLA timers, recurring compliance windows, and predicted-vs-actual analytic cycles. All scheduled.
- TIDY hygiene heartbeats need to declarative (not hand-coded); the manifest-declared `[[agent.cadence]]` gap in the parity doc maps to this.
- RIVET CI watching is bespoke today; consolidating onto PULSE simplifies the agent's code.
- The host-unit primitive (axiom-os#274) just had its first concrete need (a consumer canary auto-update on an on-prem node); PULSE is its sibling so both land coherently.
- Notifications (axiom-os#278) are mostly scheduled — a recurring digest, a delayed escalation, a follow-up if not acknowledged. PULSE is HERALD's most common caller.

## 3. Goals & Success Metrics

**Primary goal:** Any consumer extension can declare a scheduled action — manifest or programmatic — and PULSE fires it under the right envelope, with retry + idempotency + classification + RACI + capability-token enforcement, producing a receipt. Federated peers can schedule against us under our cohort policy.

**Success metrics (post-implementation):**

| Metric | Target |
|---|---|
| Cadence precision (hourly) | ≤ 5 s drift |
| Cadence precision (daily) | ≤ 30 s drift |
| Schedule firing exactly-once across multi-node deployments | 100% in distributed-mode tests |
| Idempotency window — duplicate firings absorbed | 100% within configured window |
| Retry-then-dead-letter correctness | 100% in failure-injection suite |
| RACI graduation — novel schedule fires as proposal first, autonomous after N approvals | Working in fuzz |
| Federation-scheduled action lands on remote node with right cohort policy enforcement | 100% in cross-cohort drill |
| Manifest-declared `[[extension.schedule]]` cadences discovered at extension install | 100% |
| Schedule registration → first fire | < 1 cadence period |
| `axi schedule list` summary across all installed schedules | < 200 ms |

## 4. Key Users / Personas

| Persona | Primary tasks | Pain today |
|---|---|---|
| **Extension developer** | Declare a cadence in my extension's manifest; PULSE owns the firing. | Hand-code a background loop; reinvent retry/dedupe. |
| **Operator** | Approve a novel schedule pattern once; PULSE graduates to autonomous; pause/resume on demand. | No proposal UX; no graduation pathway; no operator-visible inventory. |
| **TIDY** (agent) | Declarative per-heartbeat health checks. | Hand-coded; not visible to the operator. |
| **RIVET** (agent) | CI poll cadences | Hand-coded session-watcher; cross-session continuity via memory rather than scheduler primitive. |
| **Federation operator** | Admit a peer-defined schedule that fires on my hardware. | No protocol. |
| **Compliance officer** | Query "every scheduled action that touched CUI in the last quarter, plus its receipts." | Cross-system forensics. |
| **Bench scientist** (domain extension) | Set a reminder to record a follow-up reading 7 days after a run. | Manual calendar entry; no platform-side. |

## 5. Scope — Key Capabilities

### 5.1 The schedule API

```python
# axiom.extensions.builtins.schedule.public_api

def register(
    envelope: ActionEnvelope,
    cadence: Cadence,
    action: CallableRef,
    retry_policy: RetryPolicy = RetryPolicy.default(),
    classification_ceiling: Optional[Classification] = None,
    description: str = "",
) -> ScheduleId:
    """Register a recurring or one-shot action."""

def pause(schedule_id: ScheduleId, reason: str) -> ScheduleReceipt: ...
def resume(schedule_id: ScheduleId) -> ScheduleReceipt: ...
def cancel(schedule_id: ScheduleId) -> ScheduleReceipt: ...

def list(filter: Optional[ScheduleFilter] = None) -> Iterator[ScheduleSummary]: ...
def status(schedule_id: ScheduleId) -> ScheduleStatus: ...
def fire_now(schedule_id: ScheduleId) -> ScheduleReceipt:
    """Manual fire (subject to authz)."""

@dataclass(frozen=True)
class Cadence:
    kind:        Literal["one_shot", "interval", "cron", "trigger"]
    interval:    Optional[timedelta] = None
    cron:        Optional[str] = None
    trigger:     Optional[TriggerSpec] = None
    not_before:  Optional[datetime] = None
    not_after:   Optional[datetime] = None
    randomized_delay: Optional[timedelta] = None
```

The four `kind`s cover the space: one-shot (run once at T), interval (every Δt), cron (cron expression), trigger (fire when external event Y happens — webhook, file watch, queue message).

**Acceptance:** every API call writes a receipt; integration tests exercise each cadence kind.

### 5.2 The manifest declaration

```toml
[[extension.schedule]]
name = "dispatch_sla_4h"
description = "Alert the duty manager when a work order has been in 'dispatched' state for >4h"
cadence = { kind = "interval", interval_seconds = 3600 }
action = "fieldservice.scheduled.check_dispatch_sla"
classification_ceiling = "internal"
raci_default = "propose_first"   # propose → autonomous after N approvals
retry = { max_attempts = 3, backoff = "exponential" }
dedup_window_hours = 24
```

PULSE discovers these at extension install (via the existing AEOS manifest discovery), registers them with PULSE's runtime, and fires them per their cadence. Removal at extension uninstall is automatic.

**Acceptance:** install → schedule appears in `axi schedule list`; uninstall → schedule removed; schema validation per `spec-aeos`.

### 5.3 The CLI surface

```bash
axi schedule list                              # all registered, status + next fire
axi schedule show <schedule-id>                # full details
axi schedule pause <schedule-id> --reason "<text>"
axi schedule resume <schedule-id>
axi schedule cancel <schedule-id>
axi schedule fire-now <schedule-id>            # manual fire (authz-gated)
axi schedule logs <schedule-id> --since 7d     # receipts for that schedule
axi schedule graduations                       # see RACI-graduation state per schedule
axi schedule register --cadence "interval:1h" \
    --action "<callable>" \
    --description "<text>"                     # ad-hoc registration
```

`axi schedule register` is the bridge for in-chat-REPL ad-hoc schedules; the manifest form is the path for extension-declared schedules.

**Acceptance:** every subcommand has structured + human output; receipts queryable via `axi audit list --primitive schedule`.

### 5.4 Exactly-once + idempotency

Per spec §6:

- **At-most-once for scheduled jobs** — distributed Postgres advisory lock keyed by `(schedule_id, fire_window)`; only one node holds the lock at fire time.
- **Idempotency window** — `dedup_key` derived from `(schedule_id, intended_fire_time)`; presented to the action; the action's downstream effects use the dedup_key for their own idempotency.
- **At-least-once dead-letter** — exhausted retries produce a dead-letter fragment surfaced to TIDY + HERALD.

The lock implementation uses `pg_advisory_xact_lock(hashtext(<schedule_id>::text))` per the ADR-052 shared connection pool.

**Acceptance:** multi-node distributed-firing test confirms exactly-once; idempotency injection test confirms duplicate firings within window absorbed; dead-letter integration confirms surfacing.

### 5.5 RACI graduation

Per ADR-045 + ADR-055 D7: a schedule's `raci_default` field declares the autonomy posture.

- `propose_first` (recommended for novel domain events) — first N firings are proposals to the human via HERALD; after N approvals the schedule fires autonomously.
- `autonomous` — fires without proposal (for low-risk schedules like hygiene heartbeats).
- `always_propose` — every firing requires explicit approval.

Graduation state lives in `authz.graduation` (shared with the authz primitive) keyed by `(schedule_id, originator_intent)`.

**Acceptance:** graduation fuzz test — novel schedule fires as proposal, graduates after N approvals, denial resets counter; receipts capture transitions.

### 5.6 Trigger-style schedules (event-driven)

A `Cadence(kind="trigger", trigger=...)` schedule fires when an external event matches:

- **Webhook** — PULSE exposes a uniform webhook endpoint per schedule; vendor-side configuration done at extension install.
- **File-watcher** — local filesystem path; uses `watchdog` per platform.
- **Queue consumer** — pulls from a named queue (rabbit / SQS / per-vendor).
- **Memory-event** — fires when a memory fragment matching a query lands in the composition service.

Each trigger has its own authz consultation, dedup, classification check.

**Acceptance:** each trigger kind has an integration test; receipts capture the trigger payload.

### 5.7 Federation handoff

Per spec §7:

- **Local-defined, remote-fire** — a schedule registered on cohort A fires `action` on cohort B's hardware. A presents a federation-hop capability; B's GUARD admits per cohort policy; B's PULSE runs the action; receipt is dual-classified and federated back.
- **Remote-defined, local-fire** — peer cohort B registers a schedule that fires on our hardware. We admit per our cohort policy and trust score; B's KEEP delegates the capability; we fire; receipt federates back.

**Acceptance:** end-to-end cross-cohort drill; trust-score regression test (peer score drops → autonomous schedules require human approval).

### 5.8 Operator inventory + hygiene

PULSE writes its inventory to `schedule.registrations`; TIDY (via its hygiene capability) periodically audits:

- Are all manifest-declared schedules from installed extensions present in the registry?
- Are any schedules in the registry pointing to uninstalled extensions?
- Are any schedules persistently in `dead_letter` state?

Findings surface via HERALD to the operator's inbox.

**Acceptance:** TIDY hygiene audit catches: registration missing, registration orphaned, persistent dead-letter; the operator sees the finding within one heartbeat.

### 5.9 Migration of existing schedule sites

Existing scheduler-like code in the codebase migrates per a documented order:

1. **TIDY heartbeat checks** — currently in-line in TIDY's main loop; cut over to manifest-declared `[[extension.schedule]]` entries.
2. **RIVET CI poll cadences** — currently bespoke session-watcher; cut over.
3. **A consumer extension's canary auto-update** — currently a systemd timer; cut over to PULSE when v1 ships (the systemd timer remains the floor for boot-time auto-update; PULSE handles in-session re-fire patterns).
4. **SCAN cadence watchers** — declared in manifest already in partial form; cut over to PULSE's runtime.
5. **MCP regeneration trigger** (per prd-builtin-mcp-server §5.4) — currently a post-install hook; cut over.

**Acceptance:** each migration is its own PR; the lint encourages `[[extension.schedule]]` declarations over hand-coded background loops.

## 6. Non-Functional / Constraints

- **Cadence precision** — per metrics in §3.
- **Distributed correctness** — Postgres advisory locks for multi-node deploys; per spec §6.
- **Federation neutrality** — local-only schedules fire without peer reachability.
- **Resource use** — PULSE process is long-running but lightweight; no per-schedule subprocesses; thread-pool with bounded concurrency.
- **Persistence** — schedule state survives node restart; in-flight retries resume.
- **Cross-platform** — schedules fire correctly on macOS, Linux, Windows; per `[[feedback_cross_platform_support_matrix]]`.
- **Time-source discipline** — PULSE consults a vendored NTP-tracked monotonic clock; clock skew between nodes detected and surfaced.
- **Time-zone discipline** — cron expressions accept `TZ` per spec; UTC-default with operator-configurable per-schedule overrides.

## 7. Timeline (high level)

| Phase | Scope | Target |
|---|---|---|
| Phase 0 | This PRD + spec sections + ADR-055 merged | 2026-06 |
| Phase 1 | `register / pause / resume / cancel / list` API; `[[extension.schedule]]` manifest; one-shot + interval + cron cadences; in-process firing on single node | 2026-07 |
| Phase 2 | Distributed locks; multi-node exactly-once; trigger-style schedules (webhook, file-watcher, memory-event); RACI graduation | 2026-08 |
| Phase 3 | Migration cutovers (TIDY heartbeats, RIVET CI poll, a consumer canary auto-update, SCAN watchers); inventory + hygiene loop | 2026-09 |
| Phase 4 | Federation handoff + WARDEN integration | 2026-10 |
| Phase 5 | Remaining trigger kinds (queue consumers); advanced cadence forms (sliding windows, batch schedules); first domain extension's SLA-timer cutover | 2026-11 |

Each phase ships shippable value: Phase 1 unblocks the first domain extension's SLA timers; Phase 2 supports multi-node on-prem GPU-node deployments; Phase 3 simplifies TIDY / RIVET / SCAN; Phase 4 unlocks federation; Phase 5 broadens trigger coverage.

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Distributed exactly-once breaks under network partition | Use Postgres advisory locks (committed transaction boundary handles partition); test under partition injection |
| Cadence drift accumulates on heavily loaded nodes | Vendored monotonic clock; schedule readjustment when drift detected; warn operators |
| Trigger-style schedules (webhooks) get spammed by hostile sources | Webhook-secret check; per-source rate-limit; dead-letter beyond N |
| RACI graduation thresholds wrong (proposes too often or too rarely) | Per-class override; operator-graduate manually; tune defaults after Phase 1 |
| Federation-hop schedule lands on a peer who's not running PULSE | Pre-register capability check; reject with documented retry-on-peer-readiness |
| In-flight retries on node restart double-fire | Postgres transactional state guarantees at-most-once-resume; integration test |
| Time-zone confusion in cron expressions | UTC default; explicit per-schedule TZ; surface in `axi schedule show`; operator alert on mismatched expectations |

**Open questions:**

- (Phase 2) Multi-node coordination mechanism — Postgres advisory locks (simple, depends on Postgres) vs Raft (complex, no single-point dependency). **Default: Postgres advisory locks per ADR-052's shared Postgres assumption.**
- (Phase 4) Federation peer running an older PULSE — how does v2 handle a v1-only peer's schedule? **Default: version-negotiated downgrade; reject if neither can satisfy the other's requirements.**
- (Phase 5) Sliding-window cadences (run if X events accumulate, vs run every T) — additional kind in `Cadence` or composable trigger? **Default: separate `kind="sliding_window"` to keep the trigger ontology clean.**

## 9. Acceptance & Rollout

**Sign-off:**
- Engineering: Ben Booth
- Product: Ben Booth (B-Tree Labs)

**Rollout plan:**
1. Phase 0–1 land on `feat/governance-fabric-schedule` branch.
2. Phase 1 cuts 0.27 with single-node schedules; SCAN cadence migration.
3. Phase 2 cuts 0.28 with distributed locks + triggers + RACI.
4. Phase 3 cuts 0.29 with the migration cutovers.
5. Phase 4 cuts 0.30 with federation handoff.
6. Phase 5 cuts 0.31 with the broader cadence set.

**Rollback criteria:**
- Cadence drift exceeds 2× target → throttle; surface alert via HERALD.
- Distributed exactly-once violation under partition → revert; investigate.
- Federation-hop schedule fires on peer when local cohort policy should have denied → emergency revert + security audit.

## 10. Contacts & Links

- Product lead: Benjamin Booth — user@example.org
- Eng lead: Benjamin Booth
- ADR: [`adr-055-unified-governance-fabric.md`](../adrs/adr-055-unified-governance-fabric.md)
- Spec: [`spec-governance-fabric.md`](../specs/spec-governance-fabric.md) §5.5, §6, §7, §8.4
- Sibling PRDs: [authz](prd-axiom-authz.md), [vault](prd-axiom-vault.md), [notifications](prd-axiom-notifications.md)
- Sibling layer: axiom-os#274 (`axi schedule install` host-unit lifecycle)
- Related — ADR-027 federated memory, ADR-028 trust graph, ADR-045 RACI, ADR-049 data platform, ADR-052 DatabaseProvider; `docs/working/competitive-parity-gaps.md`; axiom-os#277 (tracking issue); a consumer extension's canary auto-update (first migration consumer)

---

## 11. Behavioral Requirements (normative)

The nuanced, testable behaviors PULSE guarantees — the spec-of-record, and the
source these convert to user documentation from. **MUST** = guaranteed; **[1]**
= shipped in PULSE-1 (single-node); **[2]** = planned for PULSE-2 (distributed /
federation). Each requirement maps to a test; user docs paraphrase the
guarantee, not the mechanism.

### 11.1 Cadences & firing

- **SCHED-R1 [1].** A schedule MUST support cadence kinds `one_shot` (fire once
  at a time), `interval` (every Δt), `cron` (a cron expression with an optional
  timezone), and `rrule` (iCalendar RFC 5545 recurrence, computed via
  `dateutil.rrule` — the lossless calendar form, honouring `COUNT`/`UNTIL`).
  `trigger` (event-driven) is accepted by the API shape but
  rejected at registration until PULSE-2.
- **SCHED-R2 [1].** Every cadence collapses to a single computed `next_fire_at`;
  a schedule fires when `next_fire_at` is at or before the current time and the
  schedule is `active`. `paused` and `cancelled` schedules never fire.
- **SCHED-R3 [1].** `not_before` / `not_after` bound a schedule's active window;
  a fire computed past `not_after` ends the schedule (no further fires). An
  optional `randomized_delay` adds uniform jitter to spread load.
- **SCHED-R4 [1].** A schedule MUST advance to its next instant after every fire
  attempt (success, failure, or skip); it never stalls on a single instant
  except where the idempotency claim deliberately blocks a re-fire (SCHED-R6).

### 11.2 Exactly-once & idempotency (the safety linchpin)

- **SCHED-R5 [1].** Each fire claims an idempotency slot keyed on
  `(schedule_id, fire_time_bucket, params_hash)` against a unique constraint
  before executing. A losing claim is a no-op — **the same instant never
  executes twice**, even under duplicate ticks, retries, crashes, or (PULSE-2)
  concurrent nodes.
- **SCHED-R6 [1].** Exactly-once is a *safety* property, not a convenience: a
  crash between executing the action and recording its outcome leaves a claim
  behind, and on restart the persisted claim MUST prevent re-execution of that
  instant. (Domains where a double-fire is harmful — a command, a charge, a beam
  — depend on this.)
- **SCHED-R7 [2].** Across a multi-node deployment, exactly-once MUST hold during
  leader failover; the global idempotency key (SCHED-R5) is the guarantee even
  when two nodes briefly believe they hold the lease.

### 11.3 Authorization & preconditions at fire time

- **SCHED-R8 [1].** Before executing, PULSE MUST consult authorization with the
  schedule's capability envelope. A denial is **recorded, never executed**, and
  the schedule advances; the persona/LLM layer cannot override a deny.
- **SCHED-R9 [1].** A registered `pre_fire` precondition gate MAY veto a fire
  (e.g. "the resource is unavailable / a prerequisite is incomplete / no
  allocation"). The gate is **fail-closed**: a veto *or* an error in a
  precondition check skips the fire — PULSE does not fire under uncertainty.
- **SCHED-R10 [1].** A schedule's `classification_ceiling` MUST bound what an
  action may touch; a fire never exceeds the declared ceiling.

### 11.4 Retry & dead-letter

- **SCHED-R11 [1].** A failing action MUST be retried per the schedule's
  `retry_policy` (`max_attempts`); on exhaustion the fire terminates in the
  `dead_letter` outcome — **never a silent skip**.
- **SCHED-R12 [1].** Dead-lettered fires MUST be surfaced (via the
  `on_dead_letter` hook / bus event) for hygiene and operator visibility, and
  recoverable by an explicit `replay_dead_letter` once the cause is fixed — it
  re-arms the dead-lettered instant and re-fires it with a fresh claim.

### 11.5 Lifecycle hooks

- **SCHED-R13 [1].** PULSE MUST expose lifecycle hook points: a vetoing
  `pre_fire` gate (SCHED-R9) and observational `on_success`, `on_failure`,
  `on_dead_letter`, `on_register`, `on_cancel`, `on_reschedule`,
  `on_actual_recorded`, `on_conflict`. These let consumers and other agents plug
  in (calendar sync, escalation, dependency recompute) **without PULSE knowing
  their domain**.
- **SCHED-R14 [1].** Observational hooks MUST be isolated: a raising observer
  never breaks a fire. Every hook point MUST also emit on the platform event bus
  (ADR-060), best-effort — a missing or failing bus never breaks the fire.

### 11.6 Schedule mutation

- **SCHED-R15 [1].** Operators MUST have fine-grained control over a live
  schedule: `pause(reason)` / `resume` / `cancel` (terminal); `skip_next` skips
  only the next occurrence without pausing the series; `snooze(until | delay)`
  delays just the next fire (cadence unchanged); `fire_now` marks it due so the
  next tick fires it under the same authz + idempotency as any fire. Cancelling
  a **time slot** (`cancel_time_slot`) MUST cascade to its cadences — the
  planned-relative reminder and every actual-anchored timer.
- **SCHED-R16 [1].** `reschedule` MUST move a schedule in time **without losing
  its identity, history, or fire-log** (a new cadence re-times the series; an
  explicit time is a single move) and MUST emit `on_reschedule` so dependents
  (e.g. a calendar) follow.

### 11.7 Time slots & the consumer seam (planned-vs-actual)

- **SCHED-R17 [1].** PULSE MUST offer a domain-agnostic seam a consumer wraps:
  `register_time_slot` (reserve a window with **opaque** consumer metadata that
  PULSE stores and returns verbatim and **never interprets**), `register_cadence`
  (a reminder/timer riding PULSE, optionally linked to a slot), `record_actual`,
  and `time_slot_status`.
- **SCHED-R18 [1].** A time slot MUST carry both *planned* and *actual* times;
  the gap is the consumer's planned-vs-actual signal. PULSE keeps the record; it
  does not compute the gap's meaning.
- **SCHED-R19 [1].** `reschedule_time_slot` MUST move a slot **and shift its
  linked cadence by the same delta**, preserving the reminder/timer's relative
  offset to the slot (move the event, its reminder follows).
- **SCHED-R20 [1].** Recording the actual time MUST emit `on_actual_recorded`
  and recompute any **anchored** dependent cadences — the **anchor**: a cadence
  bound to a slot with `anchor_to` (`actual_start`/`actual_end`) + an offset sits
  dormant until the actual is recorded, then fires at `actual_<anchor_to> +
  offset` (e.g. "open a window 24h after the actual end" — counted from what
  actually happened, not the plan). An anchor whose required actual is not yet
  recorded stays dormant until it is.

### 11.8 Restart safety

- **SCHED-R21 [1].** Schedule state MUST survive process restart (it is durable
  in Postgres, ADR-052); on restart the engine resumes from the stored state —
  no schedule is lost.
- **SCHED-R22 [1].** Each schedule MUST carry a **misfire policy** governing
  instants missed while the engine was down: `fire_once` (default — fire one,
  shed the backlog), `fire_all` (catch up each missed instant), or `skip` (drop
  missed instants and resume at the next future instant).
- **SCHED-R23 [1].** On restart, an interrupted fire (a `pending` claim with no
  recorded outcome) MUST be reconciled: a **reentrant** action releases the claim
  to re-fire; a **non-reentrant** action is flagged `interrupted` for review
  *and* advanced past the wedged instant so the schedule cannot stall — never a
  silent double-run of a non-idempotent action.
- **SCHED-R24 [1, partial].** On graceful shutdown (SIGTERM) PULSE SHOULD finish
  the in-flight fire, persist, and release the lease, so the next start is clean;
  SIGKILL falls back to reconciliation (SCHED-R23). (Cross-platform graceful
  shutdown is the next increment.)

### 11.9 Clock discipline

- **SCHED-R25 [1].** The engine MUST treat all stored times as timezone-aware
  (UTC), normalizing backend differences, so comparisons are correct regardless
  of store.
- **SCHED-R26 [2].** `fire_time_bucket` MUST be anchored to a **shared clock**
  (the database clock), not a node's local clock, so federated nodes agree on
  "the same instant" despite skew. Cross-node clock skew is detected and surfaced.

### 11.10 Resilience & federation

- **SCHED-R27 [1].** PULSE-1 is single-node by construction: one engine holds an
  in-memory lease and is authoritative; the lease code path is what PULSE-2 runs
  distributed.
- **SCHED-R28 [2].** PULSE-2 MUST provide leader failover: on leader loss a
  standby acquires the lease and resumes; a quorum lease (cohort registry, trust
  graph) avoids split-brain on partition; SCHED-R5/R7 guarantee no double-fire
  during handoff.
- **SCHED-R29 [2].** Federation MAY admit a peer-defined schedule to fire on
  local hardware only under a signed, capability-bound envelope and local cohort
  policy; receipts federate back dual-classified.

### 11.11 Observability & testing

- **SCHED-R30 [1].** Every fire MUST produce a queryable record (outcome,
  attempt, receipt, error); `status` exposes last outcome, attempts, and
  dead-letter count per schedule.
- **SCHED-R31 [1].** Because the engine is fully dependency-injected (clock,
  authz, executor, fire-log, lease are Protocols), its invariants MUST be
  verifiable by **deterministic fault simulation** — a callable chaos library
  (fault-injecting doubles + a synthetic clock) drives crashes, clock jumps, and
  flaky actions and asserts exactly-once, no-double-execute-on-restart, and
  bounded catch-up hold.

### 11.12 Conflicts & approval-gated changes

- **SCHED-R32 [1].** When a time slot carries a `resource_key`, PULSE MUST detect
  conflicts — other non-cancelled slots on the **same** `resource_key` whose
  planned windows overlap — and surface them via `on_conflict`. `resource_key`
  is the *only* part of a slot PULSE compares; everything else stays opaque.
  `reject_on_conflict` raises instead of reserving.
- **SCHED-R33 [1].** A `fixed` slot is immovable: confirming a reschedule whose
  target window collides with a fixed slot MUST be refused — you reschedule
  *around* an immovable slot, never onto it.
- **SCHED-R34 [1 partial].** A slot's `priority` orders preemption (higher wins)
  and is surfaced in conflicts. (Automatic preemptive rescheduling of the loser
  is a planned increment; the ordering + surfacing ship now.)
- **SCHED-R35 [1].** Changes MAY be approval-gated (operator-veto): a requester
  `propose_reschedule` records a **pending** move and surfaces its conflicts
  **without applying it**; an operator `confirm_reschedule` applies it (subject
  to SCHED-R33) or `reject_reschedule` discards it.

### 11.13 Policy & safety windows

- **SCHED-R36 [1].** A schedule MAY declare a `compliance_window_seconds`. A fire
  later than the window is a compliance violation recorded as `out_of_window`
  (distinct from `dead_letter`) and surfaced — for protocol windows / queue-time
  limits / reporting deadlines. `compliance_action` selects `flag` (execute, but
  record the deviation) or `skip` (do not fire a late instant).
- **SCHED-R37 [1].** A **blackout window** MUST suppress fires whose instant
  falls inside it (a maintenance outage, holiday, market closure); the schedule
  resumes after the window rather than flooding. Blackouts are global or scoped
  to a `resource_key`.
- **SCHED-R38 [1].** Reservations MAY be gated by an **allocation gate** — a
  fail-closed `pre_register` check evaluated at reserve time (distinct from
  fire-time authz). A veto raises `AllocationError`; this is where quota /
  allocation / entitlement is enforced before a slot is ever held.

### 11.14 Format interoperability

- **SCHED-R39 [1].** PULSE MUST **read and write** the predominant schedule
  formats through a codec (`formats.parse` / `formats.serialize`), so schedules
  import from, and export to, the systems people already use. **cron** (5/6-field
  + `@daily`-style shortcuts) and **ISO-8601** durations / repeating intervals
  ship. Conversions are exact where the formats overlap and raise `FormatError`
  where they do not (a one-shot has no cron form; an irregular cron has no
  interval form). The dialect is auto-detected when not given.
- **SCHED-R40 [1].** **iCalendar RRULE** (RFC 5545) is a first-class cadence kind
  and codec dialect — read/write, lossless round-trip. This is the substrate for
  two-way calendar sync (a recurring calendar event *is* an RRULE); the calendar
  connector itself (bind event ↔ cadence over CalDAV / Google / M365) remains
  **[2]**. **systemd `OnCalendar`** is the remaining dialect, for the host-unit
  sibling (axiom-os#274).

---

## 12. How It Works — Worked Examples

Walkthroughs of the behaviors in §11, with the actual API. Examples are
domain-agnostic; substitute your own action and metadata. (These mirror the
test suite, so they run.)

### 12.1 A recurring SLA timer

The bread-and-butter case: fire an action on a cadence.

```python
from datetime import timedelta
from axiom.extensions.builtins.schedule.api import register, Cadence

# Every hour, check whether any work order has breached its SLA.
sched = register(
    envelope=my_capability_envelope,
    cadence=Cadence(kind="interval", interval=timedelta(hours=1)),
    action="fieldservice.scheduled.check_dispatch_sla",
    retry_policy={"max_attempts": 3},
    misfire_policy="fire_once",   # after downtime, fire one + skip the backlog
)
```

PULSE computes the first `next_fire_at`, fires under authz + idempotency, retries
on failure, and dead-letters on exhaustion (SCHED-R5/R8/R11). `cron` and
`one_shot` work the same way; only `Cadence.kind` differs.

### 12.2 Reserve a slot, remind before it, reconcile after — planned vs actual

The consumer seam composes a *time slot* (a reserved window with opaque metadata
PULSE never interprets) with cadences. This is what a domain CLI verb wraps.

```python
from axiom.extensions.builtins.schedule import seam
from axiom.extensions.builtins.schedule.api import Cadence

# 1. Reserve the slot (e.g. a procedure at 14:00). Metadata is yours.
slot = seam.register_time_slot(
    planned_start=appt_time,
    metadata={"ref": "appt-9931", "room": "C", "owner": "dr-lee"},
)

# 2. A reminder one hour before — "planned-relative": it moves with the slot.
seam.register_cadence(
    cadence=Cadence(kind="one_shot", not_before=appt_time - timedelta(hours=1)),
    action="clinic.scheduled.send_reminder",
    time_slot_id=slot,
)

# 3. When it actually happens, record it (often a few minutes off plan).
seam.record_actual(slot, actual_start=actual_time)

# 4. Read it back — planned vs actual is your signal; PULSE just keeps the record.
seam.time_slot_status(slot)
# {"planned_start": ..., "actual_start": ..., "metadata": {...}, "state": "active"}
```

### 12.3 The anchor — a window that opens off the *actual* time

Some downstream timers must count from what *actually* happened, not the plan
(a sample's count window after the actual run; a follow-up N hours after an
actual procedure; a fab queue-time deadline after the actual prior step). Bind a
cadence to the slot with `anchor_to` + an offset:

```python
# Dormant until the actual end is recorded, then fires 24h after it.
seam.register_cadence(
    cadence=Cadence(kind="one_shot"),
    action="lab.scheduled.open_count_window",
    time_slot_id=slot,
    anchor_to="actual_end",
    anchor_offset=timedelta(hours=24),
)
# Before record_actual:  next_fire_at is NULL (dormant).
# After record_actual(actual_end=E):  next_fire_at == E + 24h  (SCHED-R20).
```

If only the start is recorded so far, an `actual_end`-anchored timer stays
dormant until the end lands.

### 12.4 Moving things in time

```python
from axiom.extensions.builtins.schedule.api import reschedule

# Move one schedule without losing its id / history / fire-log.
reschedule(sched, next_fire_at=new_time)          # a single move
reschedule(sched, cadence=Cadence(kind="interval", interval=timedelta(hours=2)))  # re-time the series

# Move the slot — its planned-relative reminder follows by the same delta;
# actual-anchored timers do NOT move (they wait for the actual).
seam.reschedule_time_slot(slot, new_planned_start=appt_time + timedelta(hours=3))
```

Every move emits `on_reschedule`, so a calendar connector (or any subscriber)
mirrors it (SCHED-R16/R19).

### 12.5 Surviving a restart

State is durable; the engine resumes on restart. What happens to instants missed
while it was down is the schedule's **misfire policy**:

| `misfire_policy` | After an outage that missed N instants |
|---|---|
| `fire_once` (default) | Fire one, then jump to the next future instant (no flood). |
| `fire_all` | Catch up — fire each missed instant once. |
| `skip` | Drop the missed instants; resume at the next future instant. |

An action interrupted mid-fire (crash before its outcome was recorded) is
reconciled on startup: a **reentrant** action re-fires; a **non-reentrant** one
is flagged `interrupted` and advanced past, so it never wedges — and never
silently double-runs (SCHED-R6/R22/R23).

### 12.6 Operator control over a live schedule

```python
from axiom.extensions.builtins.schedule.api import (
    pause, resume, cancel, skip_next, snooze, fire_now, replay_dead_letter,
)

pause(sched, reason="maintenance window"); resume(sched)
skip_next(sched)                            # skip just the next occurrence
snooze(sched, delay=timedelta(minutes=30))  # or snooze(sched, until=some_time)
fire_now(sched)                             # run it now (still authz + idempotency gated)
replay_dead_letter(sched)                   # after fixing the cause, re-fire the dead-lettered instant

# Cancelling a slot cascades to all its cadences (reminder + anchored timers).
seam.cancel_time_slot(slot)
```

### 12.7 Plugging in behavior with hooks

Consumers and other agents extend the lifecycle without PULSE knowing their
domain. `pre_fire` is a fail-closed gate; the rest are observational.

```python
from axiom.extensions.builtins.schedule import hooks

# A precondition: don't fire if the resource isn't ready (a veto OR an error skips).
hooks.register(hooks.PRE_FIRE, lambda p: "skip" if tool_is_down(p) else True)

# React to lifecycle events (here: mirror reschedules onto an external calendar).
hooks.register(hooks.ON_RESCHEDULE, lambda p: calendar.move(p["schedule_id"], p))
hooks.register(hooks.ON_DEAD_LETTER, lambda p: escalate_to_oncall(p))
```

Every hook point also emits on the platform event bus (best-effort), so a
subscriber that lives in another extension can react without a direct call
(SCHED-R13/R14).

### 12.8 Conflicts and the operator-veto reschedule

When slots compete for a scarce resource, set a `resource_key`; PULSE detects
overlaps. Some slots are `fixed` (immovable). And a change can require an
operator's blessing rather than applying unilaterally.

```python
# Two reservations on the same resource; overlap is detected + surfaced.
a = seam.register_time_slot(planned_start=t0, planned_end=t0 + 2*h, resource_key="bay-3")
seam.register_time_slot(planned_start=t0 + h, planned_end=t0 + 3*h, resource_key="bay-3")
# -> on_conflict fires, naming slot `a`. Or pass reject_on_conflict=True to refuse.

# A fixed (immovable) maintenance window; other work reschedules *around* it.
seam.register_time_slot(planned_start=t0 + 4*h, planned_end=t0 + 6*h,
                        resource_key="bay-3", fixed=True)

# Operator-veto: a requester proposes a move; it is NOT applied yet.
proposal = seam.propose_reschedule(mover, new_planned_start=t0 + 4*h + 30*min)
proposal["conflicts"]            # surfaces the fixed-window collision

# The operator decides:
seam.confirm_reschedule(mover)   # -> raises ConflictError (can't move onto the fixed slot)
seam.reject_reschedule(mover)    # discard the proposal
seam.propose_reschedule(mover, new_planned_start=t0 + 7*h)
seam.confirm_reschedule(mover)   # around the fixed slot — applied (SCHED-R32..R35)
```

### 12.9 Policy & safety windows

```python
from axiom.extensions.builtins.schedule import blackout, hooks
from axiom.extensions.builtins.schedule.api import register, Cadence

# A compliance window: a fire more than 2h late is a recorded deviation.
register(
    envelope=env, cadence=Cadence(kind="cron", cron="0 8 * * *"),
    action="pharma.scheduled.administer_dose",
    compliance_window_seconds=2 * 3600,
    compliance_action="flag",   # still administer, but record out_of_window; or "skip"
)

# A blackout: suppress all fires during a maintenance outage; they resume after.
blackout.add_blackout(outage_start, outage_end, reason="quarterly PM")
# Scope it to one resource instead of globally:
blackout.add_blackout(start, end, resource_key="line-2")

# An allocation gate: refuse a reservation without quota (register-time, not fire-time).
hooks.register(hooks.PRE_REGISTER,
               lambda p: True if has_allocation(p) else "no allocation")
# seam.register_time_slot(...) now raises AllocationError when the gate vetoes.
```

### 12.10 Speaking other schedule formats

Import an existing cron job or ISO-8601 interval, and export a cadence back out.

```python
from axiom.extensions.builtins.schedule import formats
from axiom.extensions.builtins.schedule.api import register

# Read: a user brings their cron line or an ISO-8601 interval — dialect is
# auto-detected (or pass dialect="cron"/"iso8601" explicitly).
cadence = formats.parse("@daily")            # -> Cadence(kind="cron", cron="0 0 * * *")
cadence = formats.parse("PT15M")             # -> Cadence(kind="interval", 15 min)
cadence = formats.parse("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR")  # iCalendar recurrence (lossless)
register(envelope=env, cadence=cadence, action="...", now=event_start)  # now= anchors the RRULE

# Write: export a registered cadence to another system's format.
formats.serialize(cadence, dialect="rrule")    # "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"
formats.serialize(Cadence(kind="interval", interval=timedelta(hours=2)), dialect="cron")  # "0 */2 * * *"
# systemd OnCalendar is the remaining dialect (host-unit sibling #274).
```

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
