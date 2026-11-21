# ADR-237: Every agent runs on one harness: an agent is a contract, the loop is shared

**Status:** Proposed (2026-11-06)
**Amends on acceptance:** [ADR-231](adr-231-a-chain-of-hand-offs-has-executor-semantics.md), [ADR-232](adr-232-capabilities-may-be-agent-backed-and-dispatch-is-a-recorded-verdict.md), [ADR-233](adr-233-chat-spawns-subagents-through-one-manager-and-one-run-handle.md)
**Narrows on acceptance:** [ADR-061](adr-061-agent-call-protocol.md)
**Completes:** [ADR-230](adr-230-keel-is-the-one-node-agent.md), and renames the TIDY side of [ADR-046](adr-046-rivet-tidy-boundary.md) to KEEL
**Depends on:** ADR-236 (draft, one permission model), [ADR-185](adr-185-tools-declare-what-they-need-and-make-and-vetted-chains-are-published-as-recipes.md), ADR-234, [ADR-064](adr-064-rivet-fix-trivial.md)
**Program, phases and scorecard:** [prd-agent-backed-capabilities-and-subagents](../prds/prd-agent-backed-capabilities-and-subagents.md)

## The 30-second model

An agent is data: a **goal**, a **persona**, **AgentContracts**, **capabilities** and **targets**. The harness is code, and it is the same for every agent:

| Step | What happens | Provided by |
|---|---|---|
| 1. Trigger | An event, a schedule or a person starts the work | Event bus, PULSE, surfaces |
| 2. Admission | Is there room on the host and on the forge? | Resource verdicts (KEEL, RIVET), baton |
| 3. Dispatch | Lowest rung that can do it; unknowns escalate on evidence | ADR-232 verdict |
| 4. Gate | deny, ask, notify or allow | GUARD (ADR-236) |
| 5. Hand-off | Each call carries the baton; each long step a watchdog | Baton, process registry |
| 6. Run | Function, recipe, one model call, agent loop, remote agent | `RunHandle` |
| 7. Verify | A deterministic check reads the world, not the agent's account | Dispatcher |
| 8. Record and learn | Receipt, memory, eval case | Receipts, memory read path, evals |

Nothing specific to one agent lives in those steps. RIVET landing a pull request, KEEL reclaiming a disk and PLINTH resuming a copy all pass through the same eight. RIVET is the first worked example because its failures are the best documented. It is not the subject of this decision.

The test of the design: **a second, unrelated pairing works with a persona, contracts and capabilities only, and no runtime change.**

## Context

Three ADRs designed the parts of this harness, and each has been built in part. The work queued for one agent (stall detection, timeouts, storm calming, retry) overlaps what those parts are meant to do. Built as written, it would be an agent-private loop that the next agent cannot reuse.

A reading of the code at `40b86c581` shows the parts exist and are not joined. Only `node/skills/_handoff.py` calls `baton_for`, so RIVET's heartbeat holds no baton. `RunManager` and `register_poll_tools` have no production caller. `spawn_agent` and `SubagentManager` do not exist. `chain_ledger.resource_level` treats a missing verdict as room. `node.vitals` on macOS measures nothing and publishes `even keel`. The schedule engine stores `backoff` and retries at once. The PRD lists each with its evidence.

## Decision

**One harness serves every agent. New agent behaviour is a contract and capabilities; a change to the eight steps is a platform change that every agent inherits.**

| Concern | Owner | State at `40b86c581` | Emblematic cases |
|---|---|---|---|
| Chain liveness (holder died) | Harness: baton lease, sweep | Built; sweep runs only when `agents.chains` lists; only KEEL holds a baton | RIVET heartbeat, PLINTH adoption |
| Hung step (holder alive, step stuck) | Harness: process registry `max_runtime_s`, `process.sweep` | **Gap:** deadlines are checked between hops; a stuck step is never reached | Hung push, stuck copy job |
| Chain timeout | Harness: baton deadline | Built | Any long chain |
| Repeated failure | Harness: no-progress rule | Built, within one chain | RIVET repair, KEEL reclaim that frees nothing |
| Retry | Harness: **gap**, new retry service on PULSE | Schedule retries at once and never reads `backoff` | Lost runner (RIVET), transient store error (PLINTH) |
| Rate sharing | Harness: **gap**, one limiter across processes | `ratelimit` and `rate_limiter` exist per process; the forge client uses neither | RIVET, CLERK and KEEL on one token |
| Host admission | KEEL verdict, harness baton | Built; blind by default, see decision 3 | Push under pressure, adoption under pressure |
| Forge admission (storm) | RIVET verdict, harness baton | **Gap:** ADR-231 knows one signal, the host | Merge burst, branch-update burst |
| Ordering under pressure | Harness: chain strategy | Built | Competing writers |
| Landing order | RIVET: `landing_order`, merge queue | Built; queue not applied | Stays with RIVET: it is a policy about pull requests, not chains |
| Failure diagnosis, CI | RIVET: `failure_patterns`, `failure_narrative` | Built at R1 and R3; no contract, no verify | Unknown CI failure |
| Failure diagnosis, host and data | KEEL (`node.doctor`), PLINTH (`data.diagnose`) | Built; TRIAGE's half unmoved | Disk floor; ingest stall |
| Gate | GUARD (ADR-236) | `decide()` built; `pr.*` not live | Merge, re-run, reclaim |
| Credentials, forge identity | KEEP | **Gap:** no GitHub App identity; workflows use `github.token` | RIVET worker, CLERK writes |
| Notifying | HERALD (`notifications.alert`) | Built | All |
| Follow-ups | CLERK (`program.followup_add`) | Built, not called by RIVET or KEEL | Owed fixes from a leak finding |
| Schedules | PULSE | Built, `one_shot` included | Ticks, retries, drills |
| Correlation | SCAN | **Gap:** `correlator.py` maps people and initiatives; no incident skill | One cause across repos or agents |
| Evals | CURIO | **Gap:** quality gates only; no capability that runs evals | Readiness, promotion of a pattern |
| Digest | PRESS over `release.ci_digest`, `node.report` | Built in parts | Weekly scorecard |
| Host upkeep, waste | KEEL | Plans only; worktree logic still in `hygiene` | Worktrees, volumes, processes |
| Memory | Harness: MR1 to MR4 | Write path built; read path not | Signatures, fixes, flakes |
| Readiness | Harness: **gap**, new check | None | Every agent |

### 1. An agent is data

Agent-specific code is limited to capabilities the agent owns. The eight steps read declarations. A pull request that adds a branch for one agent's name to a harness step is refused in review. AEOS gains the rule when the second pairing passes (decision 11).

### 2. Hand-offs are real and carry the baton

A declared `next_steps` entry is a suggestion to the caller. A hand-off is an `invoke_capability` call with a chain. Every heartbeat opens a chain. `delegate()` gains the baton and draws from the budget envelope, so `agents.ask` is visible to `agents.chains`. `MAX_DELEGATION_DEPTH` stays as a bound on model cost. It is not a termination rule; termination is the baton's.

### 3. Admission has named sources and fails blind, not open (amends ADR-231)

`resource_level` becomes a set of named verdicts: `host` from KEEL and `forge` from RIVET, with room for more. A verdict is `ok`, `pressure`, `critical` or `blind`. A missing, stale or empty reading is `blind`, never room. Under `blind`, reads run, background chains wait, other chains run with `admission: blind` on the receipt, and HERALD alerts once. A sensor that measured nothing may not report health: this is the rule RIVET already follows, that green requires having looked, made general.

### 4. Every step is bounded (amends ADR-231)

A deadline is checked between hops, so it cannot end a stuck step. Every subprocess a capability starts goes through `run_registered` with `max_runtime_s`. A PULSE tick runs `agents.chains` (the sweep) and `process.sweep`. Stopping a process goes through `verify_ours`. An action outside the harness, a `git push` from a hook, is bounded by its own wrapper and reads the same verdicts.

### 5. Retry is one service (amends ADR-232)

A capability may declare `retry` per failure class: `max_attempts`, `base_s`, `cap_s` and a budget per subject, for example per commit. Delay is exponential with full jitter: `uniform(0, min(cap_s, base_s * 2 ** attempt))`. Only classes a retry can fix are declared. Each attempt is a child chain with a PULSE `one_shot`, so ADR-231's no-progress rule stays exact: the same signature twice on the same subject escalates once and stops. A pass on re-run still records the flake. The service respects both verdicts and the shared limiter, and it replaces `retry = { backoff = ... }` in schedule manifests, which nothing reads.

### 6. Hybrid dispatch is the default for operational agents

Known patterns stay deterministic at R1. A failure with no match returns `unmet: no_pattern`, an ADR-232 escalation, to R3 and then R4 under a budget. A verified diagnosis is proposed as a new pattern through a CURIO gate, so the same failure costs no model next time.

### 7. A worker that writes is a subagent with four boundaries (amends ADR-233)

It uses `spawn_agent` with `isolation: worktree`. The worktree comes from a lane with an owner and an expiry (decision 9), off the default branch rather than the parent's. It runs in a sandbox on a cloud runner, as a remote binding, under a forge identity KEEP issues per run. Verify runs in the dispatcher. S9 therefore moves ahead of the rest of P4. `RunState` keeps A0's names, which match A2A, and ADR-233's states map to them; `budget_exhausted` and `awaiting_approval` become causes on `failed` and `input_needed`.

### 8. KEEL finishes ADR-230 first and reports what development wastes

`hygiene`, `diagnostics` and `install_health` move into `node` and are deleted, with the TIDY and TRIAGE personas, before any new detection is written. KEEL owns host and environment diagnosis. RIVET owns CI failure diagnosis, because the pattern database and logs are its own. KEEL then runs on every developer machine as well as on nodes. `node.waste` inventories what agentic development leaves behind, attributes it to a session, agent or test run, alerts on growth as well as on a floor, and files each source as a fix through CLERK.

### 9. Scarce things are created through one governed path

A worktree comes from `lane.claim`, which records an owner and an expiry. A container or volume started for a test carries labels naming its run. What a harness creates by other means is adopted on sight and reported. Cleaning up is the backstop; the governed path is the control.

### 10. An agent is operational only by evidence (amends ADR-232 part 6)

Five tests: **deployed** (installed at a release that holds its code), **dispatched** (its trigger fired in the last two intervals), **hands off** (each declared hand-off has executed for real), **evaluated** (its evals pass) and **exercised** (it resolved a real or injected case). `axi agents readiness` computes them. A persona missing any of the five is reported as not operational.

### 11. Protocol stance (narrows ADR-061)

| Boundary | Binding | Built | Designed only |
|---|---|---|---|
| Inside a process | Registry skill through `invoke_capability`, with the baton | Yes | Adoption beyond KEEL |
| Harness or tool to platform | MCP | Yes | Poll tools registered; native tasks (H3) |
| Across processes on one node | Local control endpoint, `RunHandle` states (ADR-233 part 10) | State machine only | Endpoint, durable store |
| Across nodes or authorities | A2A as the binding behind a capability (ADR-232 part 7) | Agent cards, a named seam in `memory/sync/transport.py`, A2A-shaped state names | Task lifecycle, server, round trip |

A2A may serve any process boundary and is first needed at a node boundary. It is never a front door. ADR-061 is accepted as narrowed: decision 1 (the surface is the registry) and decision 6 (the audit tree) stand. Decision 2 (a signed envelope on every in-process call), the NATS lane of decision 3, decision 4 (WARDEN gates every call) and decision 5 (autonomy per edge) are withdrawn. GUARD through ADR-236 is the gate, and WARDEN gates only what crosses a node.

## Options considered

- **A loop for RIVET now, generalised later.** Fastest for one agent, and the later step never comes: it would be the sixth place that answers "may I retry".
- **Finish every phase of ADR-232 and 233 first.** The router prerequisites have no issues yet, and CI needs relief today. The PRD keeps deterministic rungs moving independently.
- **New detection in `hygiene`, collapse later.** Cheaper this week, and it writes twice the code into an extension ADR-230 deletes.
- **A2A for all agent calls, as ADR-061 said.** It adds signing and a verdict per in-process call, with no consumer and no task lifecycle, and it duplicates `decide()`.

## Consequences

- ADR-231's table changes in three rows: admission, timeout and orphans. Its model stands.
- `release.land` must split: it declares `risk = "read"` while `--act` merges, so the baton treats a merge as a read.
- Two ADR-236 points need an answer: a disposable resource has no undo, and `node.*` actions join the PR 12 registration. See D3 in the PRD.
- Net code falls in the first phase, by deletion, before anything new is added.
- A sensor that was silently optimistic becomes noisy for a while. That is correct.
- Agents shown as not operational will be most of the roster on first run. That number is the baseline.
