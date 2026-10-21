# PRD: The switch contract

**Status:** Draft (2026-10-20). **Decision:** [ADR-197](../adrs/adr-197-a-switch-is-one-runner-with-a-gate-a-lock-budget-a-soak-and-an-explicit-retire.md).
**Extends:** ADR-182 (never down from our own causes), ADR-189 (time-series adoption). **Related:** ADR-161, ADR-170, ADR-193, ADR-196.

## 1. Problem

Axiom changes things by building the new beside the old, switching, keeping the old
for a while, and being able to go back. It does this in at least seven places. Each
was written separately, each remembers a different subset of the steps that make the
pattern safe, and nothing makes the next author remember them. A switch can be
declared done when it only *happened*: the readers do not use the new thing, or the
lock held was long, or nobody was told the old one is still there and costs money or
confuses the next operator.

An operator should not have to remember the order of a switch, or that "switched" and
"working" are different claims. The platform should refuse the wrong order.

## 2. Evidence: the swap on 2026-10-20

A table adoption on a production node ran to its swap step and reported success.

- The target was a partition of a natively partitioned table. The tool renamed it to
  `<table>__old` and renamed a new hypertable to its name.
- **A hypertable cannot be attached to a partitioned parent.** The parent kept reading
  and writing the old table. The new one was never read.
- The exclusive lock was held **62 s**. The tail copy that caused it ran inside the
  lock because nothing said it must not.
- The tool then printed "next: verify, then drop_old". Followed, the next step would
  have dropped the only table anyone was using.
- Nothing was wrong with any single line. The per-window digest proved the copy equals
  the source. No check asked whether a read through the original name lands on the
  copy.

Reading the code for how else Axiom switches things found the same skeleton six more
times (table in the ADR). The properties are present in one implementation and absent
in another: the supervisor proves readiness and has no rollback; the pack lifecycle
rolls back and proves nothing; the generation rollback writes a number without
checking the generation exists; the slot switch has a health gate and no record; the
node update has a rollback and a record and no soak; four code paths wait for
"ready" with four timeouts.

## 3. Goals and non-goals

**Goals**

1. One runner owns the lifecycle: prepare, prove, commit, soak, retire.
2. A domain supplies four small hooks; it cannot skip a phase.
3. "Ready" and "healthy" are declared once, as data, and read by every consumer.
4. A commit that cannot meet its lock budget, or has no passing gate, or has no
   written change record, does not run.
5. Retiring the old is explicit when it is irreversible, and every retire has an
   owner and a due time that someone is reminded of.
6. The existing seven implementations migrate onto it, and a test fails when an eighth
   appears.
7. Operators see all switches in flight, their phase and their rollback in one place.

**Non-goals**

- Not a deploy system. Compose, Helm and Terraform stay as they are; the runner wraps
  their switch step (ADR-170).
- Not a scheduler or a notifier. The clock is PULSE, delivery is HERALD, the
  commitment is CLERK's (ADR-196). The runner records and asks; it does not remind.
- Not a migration framework. Expand/contract for schema (ADR-182 D4) stays; this makes
  its shape reusable.
- Not consumer-specific. A consumer implements hooks in its own repository against a
  published seam; Axiom ships none of theirs.
- No new daemon.

## 4. Users

- **An operator** running a swap, an update or a rollback, who needs the platform to
  say "refused, and here is why" instead of printing a next step.
- **An extension author** adding a mutation of shared state, who should write four
  functions and get the rest.
- **A reviewer / the maintainer** who wants "does this follow the contract?" to be a
  lint, not a memory.
- **An agent** driving a change, which needs the same refusals a person gets, in a
  form it can read.

## 5. Requirements

**R1. The runner.** `SwitchPlan` runs `prepare`, `prove`, `commit`, `soak`, `retire`
in that order. Each phase is separately invocable (a copy that takes days is many
`prepare` calls) and resumable from the ledger.

**R2. Hooks.** `prepare`, `probe`, `commit`, `rollback` are required; `retire` and
`tail_estimate` are optional. Hooks are idempotent. `rollback` is callable from any
phase from prepare to soak.

**R3. Declared probes.** Probes are data: `http`, `tcp`, `command`, `file`,
`relation_read`, `callable`; each has a `when` (`pre`, `in_commit`, `post`) and a
timeout. A probe that cannot be evaluated fails. The boot-order wait-for, the
supervisor and the gates read the same registry.

**R4. Refusals.** The runner refuses commit when: the change record was not written;
the gate did not run or a required probe failed; no probe or no rollback is declared;
a data plan has no read-through-the-original-name probe; the lock budget cannot be
met; another run holds the single-flight lock. Each refusal changes nothing and names
the fix.

**R5. Lock budget.** Default 2 s, raised only by explicit declaration. Bulk work is
outside the commit; the kind runs catch-up passes until the estimated tail fits. The
measured hold is recorded; an overrun is a fault.

**R6. Change record first.** The ADR-182 D5a record is written before the first
mutating step, with `step` lines for each phase, additive to today's format.

**R7. One lock.** A single `SingleFlight` helper with an advisory backend and a file
lease backend replaces the two advisory-lock helpers for single-flight uses.

**R8. One ledger shape.** `SwitchRecord` defined once, stored in a file or in the
owning extension's schema; a transactional kind writes it in its commit's
transaction. The adoption ledger is the first store.

**R9. Soak and automatic rollback.** A soak window with `post` probes on a cadence; a
latched fault from the signals Axiom already raises (PLINTH, heartbeat) rolls back
with no person present, and the rollback is a recorded change.

**R10. Retire.** `reclaim` retire (the default) needs: soak passed or a written
override, a fresh verify, a named approver, an owner and a due time. It is never
scheduled. `release` retire runs on a passing soak.

**R11. Retire follow-up.** Commit proposes a follow-up (owner, due, attached
read-only check skill) through the ADR-196 seam (`program.followup_add`). Until CLERK's skills are present the task
is recorded in the ledger and shown by `axi switch` and `axi program status`.

**R12. Enforcement.** AEOS104/105/106 in `axi ext lint`; a conformance test with a
shrinking allowlist for `swap`/`rollback`/`switch`/`cutover` functions that bypass
the runner; the runtime refusals of R4 with no skip flag.

**R13. Proof per kind.** A CI zero-gap harness per switch kind: a load generator
during commit counting failures (bar: zero); for data, a writer running through the
switch with every row accounted for after commit and after rollback.

**R14. `axi switch`.** `list`, `show`, `rollback`, `retire --approve`. Output carries
phase, soak remaining, open retire task, owner, due, and the exact rollback command.

**R15. Defaults.** Reload-by-default for services that declare it; lock budgets
default small with loud refusal; every data swap has a read-through check.

## 6. Success metrics

| Metric | Target | Measured by |
|---|---|---|
| Requests refused during a commit | 0 | zero-gap harness, per kind, in CI |
| Rows lost by a writer through a data swap, or through its rollback | 0 | data-swap harness |
| Commits whose measured lock hold exceeded budget | 0 unexplained; every breach is a fault | ledger audit |
| Switches that committed without a passing gate | 0 (the runner cannot) | ledger audit: every `commit` step has a preceding passing `prove` |
| Retire steps with an owner and a due time | 100% | ledger audit |
| Retire tasks overdue without a delivered reminder | 0 | CLERK journal vs ledger |
| Functions named swap/rollback/switch outside the runner | allowlist shrinks every phase; 0 by P4 | conformance test |
| Distinct readiness-wait implementations | 1 (the registry) | grep in the conformance test |
| Distinct advisory-lock helpers for single-flight | 1 | grep in the conformance test |
| Time from "commit failed its probe" to "old is serving again" | seconds, no human | harness |

## 7. Phased delivery plan

Each phase ships on its own, behind tests that run against real Postgres/TimescaleDB
containers and real processes (no mocks of the thing being switched). A phase is not
done until its exit criteria are met and its docs are current.

| Phase | Scope | First users | Exit criteria |
|---|---|---|---|
| **P1** | The runner, the probe registry (`http`, `tcp`, `command`, `file`, `relation_read`, `callable`), the single `SingleFlight` helper (advisory and file lease), the `SwitchRecord` ledger (file store, with the PG store used by the first user), the refusals of R4, change-record steps. | Table adoption: its swap, rollback and drop-old become `SwitchPlan` hooks once the partition-guard fix has merged. | Unit tests for every refusal; a real-TimescaleDB test where a swap whose gate fails rolls back with the old still serving; a writer through a bounded-lock swap loses 0 rows; the measured hold is recorded and a deliberately oversize tail is refused before it takes the lock; two concurrent runs, one refused; a killed run resumes from the ledger; one lock helper replaces the adoption's. |
| **P2** | AEOS104/105/106 in `axi ext lint`; the conformance test with the ratchet allowlist; the runtime gate wired to refuse in production paths; the CI zero-gap harness for the process and data kinds; `axi switch list/show/rollback/retire`; `axi program status` lists open retire tasks; the retire task recorded in the ledger. | `axi ext lint` over all built-ins; the harness over the supervisor and the adoption | A new function named `swap` outside the runner fails CI; lint fails an extension that mutates shared state without probes; the harness runs in CI and fails when a request is refused during a commit; `axi switch list` shows an in-flight and a finished switch with retire owner and due. |
| **P3** | Migrate the process supervisor, the node update swap and the blue/green slot switch onto the runner; the hand-rolled readiness waits (`dev/node.py::wait_ready`, the supervisor's, compose and k3d probes) read the registry; `rag/store.py`'s DDL lock moves to the helper. | `infra/switch.py`, `update/swap.py`, `infra/deploy/bluegreen.py` | Each passes its existing zero-gap and rollback tests unchanged, through the runner; a failed post-check in the update swap rolls back through the runner's rollback hook and is a recorded change; the readiness-wait count is 1; the allowlist loses three entries. |
| **P4** | Corpus generation rollback and pack version rollback on the runner; the published seam (`SwitchHooks`, entry-point group `axiom.switch_kinds`, conformance fixtures) for a downstream collector overlap, implemented in that repository. | `rag/generation.py`, `vega/federation/pack_lifecycle.py`; the downstream repo | A rollback to a generation or pack version that does not exist is refused with the available ones listed; the seam has a documented example kind and a test fixture a downstream repo can import; the allowlist is empty. |
| **P5** | Retire follow-ups through CLERK/HERALD (ADR-196's skills replace the ledger-only default); soak auto-rollback wired to PLINTH and heartbeat faults; operator guide and runbook; docs index and glossary. | all kinds | A retired-later task is delivered to its owner and escalates per ADR-196 with the plan's check attached; a latched fault during soak rolls back with no person present, in a test with a real fault source; the operator guide covers every kind. |

**Dependency on in-flight work.** The partition guard, the post-swap read-through
check and the bounded-lock change for the adoption (PR #1347) are the first
implementation of the gate and the budget. P1 builds the runner so that work becomes
hooks; it does not re-implement it. If that PR merges first, P1 rebases onto it; if
P1 merges first, the owner of that PR is told. The ADR-196 follow-up skills (#1346) have landed; this plan consumes them through a
protocol and falls back to the ledger where they are absent.

## 8. Risks and open questions

- *Migration churn.* Moving a working supervisor onto a runner can regress it.
  Mitigation: P3 is gated on the existing zero-gap tests passing unchanged.
- *Probe sprawl.* Probes declared loosely give false confidence. Mitigation: a probe
  that cannot be evaluated fails; `relation_read` is mandatory for data plans.
- *The budget is a lower bound on pain, not a guarantee.* A lock wait behind a long
  reader is bounded by `wait_s` and retried; the budget limits the hold, and the
  attempts limit the wait.
- *Open:* soak duration derivation; handling a failed soak rollback after a
  `reclaim` retire (cannot occur by ordering today); non-relational read-through.
  See ADR-197 open questions.

## 9. Links

- Decision: [ADR-197](../adrs/adr-197-a-switch-is-one-runner-with-a-gate-a-lock-budget-a-soak-and-an-explicit-retire.md)
- [ADR-182](../adrs/adr-182-axiom-never-goes-down-from-its-own-causes.md),
  [ADR-189](../adrs/adr-189-a-time-series-table-is-declared-once-and-its-storage-follows.md),
  [ADR-161](../adrs/adr-161-program-tracking-is-a-composed-clerk.md),
  [ADR-170](../adrs/adr-170-a-release-is-proven-in-an-idle-slot-before-traffic-moves.md),
  [ADR-193](../adrs/adr-193-a-conform-pass-reads-only-what-it-has-not-read.md),
  [ADR-196](../adrs/adr-196-a-follow-up-is-a-tracked-commitment-clerk-owns-it-the-scheduler-keeps-the-clock-and-herald-delivers.md)
