# ADR-197: A switch is one runner with a gate, a lock budget, a soak and an explicit retire

**Status:** Proposed
**Date:** 2026-10-20
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (CLERK owns commitments and `program status`), [ADR-170](adr-170-a-release-is-proven-in-an-idle-slot-before-traffic-moves.md) (a release is proven in an idle slot before traffic moves), [ADR-182](adr-182-axiom-never-goes-down-from-its-own-causes.md) (never down from our own causes; amended here), [ADR-189](adr-189-a-time-series-table-is-declared-once-and-its-storage-follows.md) (time-series adoption; amended here), [ADR-193](adr-193-a-conform-pass-reads-only-what-it-has-not-read.md) (a durable, resumable ledger), [ADR-196](adr-196-a-follow-up-is-a-tracked-commitment-clerk-owns-it-the-scheduler-keeps-the-clock-and-herald-delivers.md) (follow-ups), [prd-switch-contract](../prds/prd-switch-contract.md)

## Context

On 2026-10-20 a table adoption on a production node reported success and left the
system worse than it found it. The tool renamed a partition of a partitioned table
to `<table>__old`, renamed a new hypertable to the partition's name, held an
exclusive lock for 62 s, and printed "next: verify, then drop_old". A hypertable
cannot be attached to a partitioned parent, so the parent's readers and writers
went on using the old table. The new table was never read by anyone. Nothing in the
tool knew that "the switch happened" and "readers now use the new thing" are
different claims. The next printed step would have dropped the only table anyone
was using.

That is two missing properties, and neither is specific to tables: **no health
gate** (the claim "switched" was never tested against what a reader sees) and **no
lock budget** (62 s was an outcome, not a limit). Reading the code for how Axiom
switches things shows the same skeleton written seven times, each with a different
subset of the properties:

| Implementation | Builds beside | Proves | Switch | Keeps old | Rolls back | Own lock | Change record |
|---|---|---|---|---|---|---|---|
| `infra/switch.py` (process supervisor) | new copy on the same socket | ready pipe | SIGTERM old after ready | draining copy only | none (a failed copy is stopped) | none | `deploy` |
| `infra/deploy/bluegreen.py` (compose, k8s slots) | idle slot | health status / rollout | rewrite upstream, `both` then `to` | the other slot | "the same switch in the other direction" | none | none |
| `update/swap.py` (node version) | new venv | pre-checks, post-checks | `current` pointer | previous venv | pointer back on post-check failure | `pending.json` | `update` |
| `data_platform/timeseries/adopt.py` | `<t>__adopt` | per-window digest | two renames in one transaction | `<t>__old` | rename back, carry rows | pg advisory (copy only) | `migration` |
| `rag/generation.py` | candidate generation | evaluation | `active_generation` update | prior generation | update back (no check) | none | none |
| `vega/federation/pack_lifecycle.py` | installed version | none | `active` file | prior versions | file write (refuses unknown version only) | none | none |
| a downstream collector overlap (separate repo) | new copy beside old | live proof | stop old | none | restart old | own | its own |

Around them sit four primitives that each solve one slice and do not know about each
other: an advisory-lock helper in the adoption code and a second in `rag/store.py`
(a different key scheme, a blocking acquire with its own timeout); the pending-claim
file primitive (ADR-182 D3a); the change-intent records (ADR-182 D5a); and at least
four hand-rolled readiness waits (`dev/node.py::wait_ready`, the supervisor's ready
pipe, the compose health loop, the k3d probes), each with its own timeout and its own
idea of "ready".

Only the process supervisor and the adoption have anything resembling a gate, and
the adoption's was a per-window row digest of the **copy**, which proves the copy
equals the source and says nothing about whether anything reads the copy. The
properties a switch needs are not remembered by the operator or the author. They are
held, if at all, by whichever implementation happened to be written carefully.

## Decision

**A switch is a `SwitchPlan` run by one runner. A domain supplies hooks; the runner
owns the lifecycle, the gate, the lock budget, the record and the lock. An operator
never has to remember the order, because the runner refuses the wrong one.**

### D1. The lifecycle: expand, prove, commit, soak, retire

| Phase | What happens | Reversible | Who may do it |
|---|---|---|---|
| prepare (expand) | Build the new beside the old. Bulk work (copying a table, installing a venv, warming a slot) happens here, outside any lock. | yes, by discarding the new | the runner, any number of resumable steps |
| prove | Run every declared `pre` probe against the new, in isolation from live traffic. | n/a | the runner |
| commit | One bounded switch under a **lock budget**. `in_commit` probes run inside the same transaction or step, so a failed probe undoes the commit for free. | yes, by `rollback` | the runner, only after prove passed |
| soak | A watch period. `post` probes run on a cadence; a latched fault from the signals Axiom already raises (PLINTH, heartbeat) triggers `rollback` automatically. | yes | the scheduler's cadence, no new daemon |
| retire (contract) | Remove the old. | **no, if it reclaims data** | a person, explicitly (D6) |

Two refinements to the shape in the original proposal came from the code:

1. **Probes have a point in time, not just a name.** The adoption's decisive check
   has to run *inside* the commit transaction, after the renames and before the
   commit, because that is the only place a failure rolls back with nothing to undo.
   A single "prove" before commit cannot express it, and a single check after commit
   leaves a window where a wrong state is live. A probe declares `when`: `pre`,
   `in_commit` or `post`. The runner refuses `commit` if a plan declares no probe at
   all, and refuses a data swap that declares no `relation_read` probe (D3).
2. **Retire is not always irreversible.** For a process or a slot, "retire" is
   draining and stopping the old copy, and the old is reproducible from the release.
   For a venv it is pruning all but the previous. Those finish automatically after a
   passing soak, as they do today. For anything that **reclaims data that exists
   only in the old** (drop a table, delete a generation's index, remove a pack
   version that is the last installed) retire is explicit, approved and verified. A
   plan declares `retire = "release"` or `retire = "reclaim"`; `reclaim` is the
   default, so forgetting to declare it picks the safe one.

### D2. Four hooks, one declared-probe registry

A domain implements `SwitchHooks`:

- `prepare(ctx)`: expand. Must be idempotent and resumable; it reports progress into
  the ledger and returns when the new is ready to prove (or, for a long copy, when
  one bounded batch is done; the runner calls it again).
- `probe(ctx)`: returns the plan's **declared probes**.
- `commit(ctx)`: the one bounded switch. It receives a `CommitScope` with the lock
  budget and a `run_in_commit_probes()` callable, so a transactional kind runs the
  gate inside its own transaction and a pointer-flip kind runs it after the flip and
  before declaring success.
- `rollback(ctx)`: put back what `commit` replaced. Callable from any phase from
  prepare onward, and idempotent.

Optional: `retire(ctx)` and `tail_estimate(ctx)` (a number of seconds the commit is
expected to hold its lock, for the budget check in D4).

Probes are declared as data in one registry, `axiom.infra.switchplan.probes`:

| Kind | Declared as | Proves |
|---|---|---|
| `http` | URL, expected status, optional body match | the service answers as ready |
| `tcp` | host, port | something accepts connections |
| `command` | argv, expected exit code | an arbitrary check, bounded by timeout |
| `file` | path, optional content or mtime condition | a pointer or artifact is in place |
| `relation_read` | a name, a relation that must be among the relations a read through it scans, relations it must not scan | **a read through this name lands on the new relation** |
| `callable` | an in-process function | what the domain alone can see |

The same declarations are read by the boot-order wait-for, by the supervisor's
readiness and by the gates, so "ready" is defined once per thing. The existing
hand-rolled waits migrate to read the registry in P3; none is rewritten in P1.

### D3. The gate, and what it refuses

The runner refuses `commit` (raises `SwitchRefused`, changes nothing) when:

- the change record was not written (D5);
- the prove phase did not run for this plan, or a required probe failed or timed out;
- the plan declares no probes, or no rollback hook;
- for a data kind, the probes include no `relation_read` through the **original
  names** (the table's name, its parent if it has one, every dependent view);
- the lock budget cannot be met (D4);
- another run holds the single-flight lock (D7).

Every refusal names the missing property and the fix. A probe that cannot be
evaluated is a failure, never a pass ("a relation the plan names that we cannot
resolve is not 'nothing'").

### D4. The lock budget

`LockBudget(max_hold_s, wait_s, attempts)` with a default `max_hold_s` of **2 s**. A
plan can raise it only by naming a larger number in its declaration, so a long hold
is visible in review and in the ledger.

- *Before commit.* `tail_estimate` is compared with the budget; if the tail the commit
  would copy under the lock exceeds it, the runner refuses with the loud message
  ("the commit would hold the lock about 41 s; the budget is 2 s; run catch-up and
  try again"). The kind then runs **catch-up passes** in `prepare` (copy the
  newest rows again, outside the lock) until the estimate fits. This is the
  structural fix for the 62 s hold: bulk work is never inside the commit.
- *During commit.* The kind sets `lock_timeout` to `wait_s` for the acquire and
  `statement_timeout` to `max_hold_s` for the hold, so an overrun aborts the
  transaction and rolls back rather than running long. Pointer-flip kinds (symlink,
  rename, SIGHUP) have no lock to hold and record the measured duration.
- *After commit.* The measured hold is written to the ledger. A hold over budget
  that nevertheless completed is a recorded breach and a fault, never silent.

### D5. The change record is written before acting, and extended additively

The runner calls `record_change` (ADR-182 D5a) before the first mutating step of
`prepare`. The kind maps to the closed `CHANGE_KINDS` (`deploy`, `update`,
`migration`, `config`); the set does not grow. The ledger is extended additively with
`step` lines (`prepare`, `prove`, `commit`, `soak`, `rollback`, `retire`), which
today's readers already skip because they read only `intent` and `outcome`. The
change's `outcome` closes at the end of commit (`switched`, `rolled_back`, `failed`);
an automatic rollback during soak writes its own change that names the first. A
commit refused for want of a record is the one refusal that does not need the record
to exist, and it says so.

### D6. Retire is explicit for anything that cannot be undone

`retire` with `retire = "reclaim"` requires, in the runner, all of:

- the soak has finished without a fault, or a person overrides it in writing;
- a **verify** has passed since the commit and within a declared freshness (the
  adoption's "verified since the swap" rule, generalized);
- a named **approver** (a principal, `@name:context`), recorded in the ledger;
- the plan's retire task has an **owner and a due time** (D8).

It is never scheduled and never called by a cadence. A `release` retire runs on the
soak's pass and is recorded the same way, with the approver `system`.

### D7. One single-flight lock, two backends

`axiom.infra.switchplan.lock.SingleFlight(key)` replaces the adoption's advisory-lock
code and the schema-DDL lock in `rag/store.py` for the cases that are single-flight
(the DDL lock is a short serializer, not a switch; it moves to the same helper's
`blocking(timeout)` mode for one key scheme, one set of names, one grep). Two
backends behind one interface, because the kinds differ in what they can reach:

- *Advisory* (`pg_try_advisory_lock(hashtextextended(key, 0))`): held on a database
  connection, released with it, so a killed process releases it. For data kinds.
- *File lease* (the pending-claim primitive's atomic link, pid and start time, stale
  takeover): for kinds with no database (supervisor, update swap, pack lifecycle).

The pending claim of ADR-182 D3a remains. It answers a different question: *which
copy finishes this spooled item*. The single-flight lock answers *is anyone else
running this switch right now*.

### D8. The ledger, one shape, two stores

`SwitchRecord` (id, kind, subject, phase, probes and their last results, budget and
measured holds, approver, retire task, timestamps, notes) is defined once. Two
stores implement it: a JSON-lines file under the node's state directory (no
database needed) and a table in the owning extension's schema (ADR-052), which is
what the adoption ledger already is. A transactional kind writes its ledger row **in
the commit's own transaction** (the adoption does this today: the `swapped` phase
and the renames commit together), so the ledger cannot disagree with the world.

**The retire task.** At the end of a successful commit the runner proposes a
follow-up through the seam in ADR-196: an owner, a due time, and an attached
read-only check. ADR-196 D4 makes a check a registered skill that declares
`risk = "read"` and `side_effects = False`, plus arguments, so the plan names one
(`retire_check`, for example a data-platform table-status skill with the old table's
name); a `post` probe is not a skill and is not sent. The `post` probes remain the
verify the runner itself runs before a retire (D6). The seam is a protocol,
`FollowUps.propose(...)`. The CLERK follow-up skills (`program.followup_add`,
ADR-196, landed in #1346) back the CLERK implementation, selected by detection;
where they are not on the node the default implementation records the task in the
switch ledger, and `axi switch` and `axi program status` list every retire task that
is open, due or overdue, so it is not remembered by a chat session. Either way the
ledger keeps the task and, when there is one, the follow-up id. The runner
does not hold a clock, deliver a message or escalate; those are PULSE and HERALD
(ADR-196 D2 and D3), and duplicating them here is the failure this decision exists
to prevent.

### D9. Enforcement

- **AEOS contract and `axi ext lint`.** Anything that declares it mutates shared
  state must declare probes, a lock budget and a rollback in its
  `[extension.availability]` (ADR-182 D2). New codes: AEOS104 (no probe), AEOS105 (no
  lock budget for a lock-holding kind), AEOS106 (no rollback). Errors, as AEOS100.
- **Conformance test.** A function named `swap`, `rollback`, `switch` or `cutover`
  (or with that as a prefix) in `src/axiom` that does not use the runner fails the
  test, as the pending-claim lint does for `resume_pending`. Existing hits go into a
  **ratchet allowlist** with a reason each; the allowlist may only shrink (ADR-182
  D6), and the P3 and P4 migrations delete entries.
- **Runtime gate.** The runner refuses as in D3. There is no flag to skip the gate.
  A `--force` exists only for `retire`, and it is itself recorded with the approver.
- **CI zero-gap harness, one per kind.** A load generator runs during commit and
  counts failures; the bar is zero. For a data swap a writer runs through the switch
  and every row it wrote must be present after, and after a rollback. The harness is
  the proof that AEOS102 ("proof pending") names.
- **`axi switch`.** `axi switch list` shows switches in flight and recently finished:
  kind, subject, phase, soak remaining, the open retire task and its owner and due
  time, and the exact rollback command. `axi switch rollback <id>` and `axi switch
  retire <id> --approve @name` drive the same runner.

### D10. Defaults baked into infra behavior

- A service that declares `reload` (SIGHUP) in its availability declaration is
  reloaded by default on config change, never restarted.
- A lock budget defaults to 2 s with a loud refusal when it cannot be met.
- Every data swap needs a post-swap check that reads through the original names;
  there is no way to declare a data plan without one.

### What this amends

**ADR-182.**
- *D2* (declaration): adds probes, lock budget and rollback to what a service or a
  mutating extension declares, and the three lint codes above.
- *D3* (one switch): the Supervisor stays the mechanism for a served app, but it is
  no longer "the" switch. It becomes one **kind** of `SwitchPlan` (P3). D3's steps
  (start, wait ready, move, drain, stop) map to prepare, prove, commit, soak and
  release-retire.
- *D5a* (change records): adds `step` lines and the rule that a change is written
  before `prepare`, not only before the switch.
- *D6* (ratchet): extends the ratchet to the conformance allowlist.
- Unchanged: D1 (the rule), D3a (pending claims), D4 (expand/contract migrations;
  this ADR generalizes the shape and leaves D4's content alone), D5 (proof measured).

**ADR-189.**
- *D5 step 4* (swap): the swap becomes the `commit` hook under a lock budget. The
  tail is brought under the budget by catch-up passes outside the lock; the
  post-swap read-through check (the work in flight on the partition guard) is the
  first `relation_read` probe, `in_commit`.
- *D5 steps 5 and 6* (drop old, rollback): drop-old is a `reclaim` retire (D6 here),
  with an approver, a verify, and an owner and due time on its follow-up; rollback is
  the `rollback` hook, callable until retire.
- *D5 "Unattended"*: unchanged, with one clarification. A scheduled copy is
  `prepare`; the swap, the verify and the drop remain unscheduled, and now the runner
  enforces it instead of the skill's wording.
- The adoption ledger tables stay as the data store of D8 for this kind.

## Options considered

- **Keep each implementation and add a checklist to the docs.** This is what exists.
  The 2026-10-20 swap had careful code and a careful ADR and still printed the wrong
  next step. A checklist is remembered by whoever read it.
- **A framework for the lifecycle only, with no enforcement.** Cheaper, and
  migrations would take as long as people had time. Without the conformance test and
  the runtime refusal, the next author writes the eighth copy.
- **Make the process supervisor the one mechanism for everything.** It is the best of
  the seven, but it owns a socket and a child process; a table, a pointer file and a
  pack version have neither. The runner takes its *shape* (build beside, prove, switch,
  keep the old), not its mechanism.
- **Run the gate only after commit.** Simple, and leaves a window where a wrong
  state is live. For a transactional kind the in-transaction check is strictly
  better, so the probe point is a property of the probe (D1).
- **Hold a follow-up clock in the runner.** Rejected by ADR-196: a second clock and a
  second delivery path is how reminders get lost.

## Consequences

- Authoring a new switch is four hooks and a probe declaration; the lifecycle, lock,
  record, refusals and `axi switch` come with it.
- Each existing implementation migrates on its own phase (P3 and P4). Until it does,
  it sits in the ratchet allowlist with a reason, so the debt is a number that goes
  down, not a feeling.
- The change ledger grows `step` lines. Readers that ignore unknown phases are
  unaffected; the availability attribution reads `intent` and `outcome` only.
- `reclaim` as the default retire class makes forgetting safe and makes the common
  process-and-venv case declare `release` once.
- A downstream repository that owns a collector overlap implements the same hooks
  against the published seam (the `SwitchHooks` protocol and an entry-point group,
  `axiom.switch_kinds`). Axiom ships no consumer-specific kind. That migration is
  that repository's change, scheduled in P4.
- Follow-ups: the delivery plan and its exit criteria are in
  [prd-switch-contract](../prds/prd-switch-contract.md) section 7.

## Open questions

- Whether `soak` duration is declared per plan or derived from the kind's measured
  time-to-first-fault. Start declared; revisit with the first month of ledger data.
- Whether a failed soak rollback of a `reclaim`-retired plan (the old is gone) is
  refused outright or escalated. Today it cannot occur, because retire follows soak.
- Whether the read-through probe for non-relational names (a path, a route) needs its
  own kind or is covered by `command` and `http`.
