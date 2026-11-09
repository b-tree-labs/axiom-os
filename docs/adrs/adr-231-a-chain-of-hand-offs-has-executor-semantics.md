# ADR-231: A chain of hand-offs has executor semantics

**Status:** Accepted (2026-10-31)
**Related:** ADR-185 (skill chaining), ADR-196 (follow-ups), ADR-230 (KEEL), AEOS 1.0 §6 (checks and balances) and §20 (added by this ADR), `src/axiom/infra/chain_ledger.py`, `src/axiom/infra/skill_chain.py`

## Context

Agents hand work to each other: a skill calls another agent's skill, which may
call a third, which may call the first again. On 2026-10-31, while giving KEEL
its hand-offs to HERALD, KEEP, PLINTH and RIVET, the questions an executor or a
thread pool answers came up one after another, and the platform answered none
of them:

- Can a chain loop forever? (Two skills that hand to each other alternate until
  something outside gives up.)
- A first answer, a hop limit, was wrong: a long job with many legitimate
  hand-offs is exactly the case to support.
- Who can see the chains in flight, and stop one?
- When ten chains want the same scarce disk, which go first, and by what rule?
- Can chains deadlock? Can one starve?
- What happens to a chain whose process died mid-step, and to the work it started?

These are the classic concerns of threads and executors: cancellation,
timeouts, priority, scheduling policy, admission control, starvation, deadlock,
livelock, orphaned work, structured concurrency and observability. An agent
platform that dispatches work between agents is an executor whether it says so
or not, and the semantics belong in the standard so every conforming runtime
answers them the same way.

## Decision

**Every chain of hand-offs carries a baton, and a conforming runtime gives the
baton executor semantics.** The baton is a small durable record per chain (the
ledger). Before each hand-off the dispatcher consults it and either runs the
call or refuses it **at once** with a reason; it never blocks.

| Executor concern | The baton's answer |
|---|---|
| **Termination** | No hop limit. A chain ends, deterministically, when it is stopped, passes its deadline, is orphaned, makes no progress (the same call returns the same result twice), or reaches the identical-call backstop (the same call started `MAX_IDENTICAL` times with no result to compare). |
| **Cancellation** | `agents.chain_stop`: the next hop is refused, so a chain ends between steps, never mid-write. Cooperative, like a cancellation token. |
| **Timeout** | Every chain has a deadline: the originator sets it, or the default applies (`chains.default_deadline_hours`, 6). Long jobs declare long deadlines. |
| **Priority** | `critical`, `high`, `normal`, `background`. Defaults by surface (a person waiting is `high`; schedules are `background`). Changeable while running (`agents.chain_priority`). |
| **Scheduling policy** | Under resource pressure a strategy orders the competing chains: `priority` (class, then oldest), `finish-first` (oldest, least wasted work), `interactive-first` (a person waiting, then newest). Setting: `chains.pressure_strategy`. |
| **Admission control / backpressure** | KEEL's vitals verdict is the signal. At `pressure`, background chains wait and at most `chains.pressure_writers` (default 2) chains keep writing. At `critical`, only `critical` chains write. Reads always run. A refused chain is **deferred**, not ended: it may try again. |
| **Starvation** | Aging: a deferred chain rises one class per 30 minutes waiting, up to `high`. Every chain still has its deadline. |
| **Deadlock** | Impossible by construction: a hand-off never blocks and no chain holds anything while it waits, so no two chains can wait on each other. The way out of pressure is never deferred: alerts, chain stops and KEEL's own upkeep are exempt. At least one non-background chain is always admitted. |
| **Livelock** | The no-progress rule: a call repeated with the same arguments and the same result stops the chain. |
| **Orphans** | The baton is a lease (5 minutes, renewed by each call and by long steps through `renew()`). A lapsed lease marks the chain orphaned on the next sweep. |
| **Structured concurrency** | A step that starts a separate job does so as a child chain (`Baton.child()`). A stopped, ended or orphaned parent ends its children at their next hop. |
| **Observability** | `agents.chains` lists every chain in flight: status, priority, age, deadline, hops, last call, holder, lease, parent. It sweeps for orphans before it lists. |

The baton's expiry and the chain's deadline are different things: the **lease**
(minutes, renewable) says the holder is alive; the **deadline** (hours, set
once) says the work must be finished.

## Options considered

- **A hop limit.** Shipped for an afternoon and withdrawn: it cuts off long
  legitimate work and does not catch a slow loop that stays under the limit.
- **Blocking waits under pressure** (a semaphore per resource). This is how
  deadlocks are made. Refusing at once with a reason, and letting the caller
  retry, keeps every chain free of held resources.
- **Preemption.** Interrupting a step mid-write corrupts what it was writing.
  Cancellation is cooperative and lands between steps; a step that runs long
  renews its lease and can check whether its chain is still running.
- **Leave it to each agent.** Each agent would answer differently, and the
  dangerous cases (loops across two agents, starvation across many) are
  exactly the ones no single agent can see.

## Consequences

- AEOS 1.0 gains §20, "Chains of hand-offs", normative for conforming runtimes.
- Every hand-off goes through `invoke_capability` with the baton; a direct
  `registry.invoke` between agents is a hand-off the baton cannot see, and the
  chaining lint should flag it (follow-up).
- KEEL's heartbeat is now load-bearing for admission control: its vitals
  verdict file is the pressure signal, and a stale verdict (older than 30
  minutes) is not evidence.
- Not built yet, named so it is not forgotten: per-chain resource quotas (a
  disk or token budget per chain, not only a node-wide verdict), joins (a
  parent waiting on several children, which must stay non-blocking), and rate
  limits per agent.
