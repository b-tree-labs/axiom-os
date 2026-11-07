# Spec: Process registry and containment

Status: living. Code: `src/axiom/infra/process_registry.py`,
`src/axiom/infra/process_containment.py`, extension `process`.

## Problem

Subprocess tests and agent runs left orphaned children spinning at 100% CPU.
The only remedy on offer was `pkill -f`, which matches text and can hit a
developer's editor, another session or a running node.

## Model

Every process the harness starts writes one atomic JSON record under
`<state>/processes/records/<id>.json`: pid, **start time**, owner (principal,
agent, extension, session), purpose, parent, `max_runtime_s`, `cpu_budget_s`,
`rss_budget_mb`, singleton key, scrubbed command. The child also carries
`AXIOM_PROC_MARK=<scope>:<id>`, inherited by its descendants, so a process that
escaped registration is still attributable and a panic stop can find ours even
with a damaged registry. Only the mark is read back from an environment.

Registered at: background tasks (`TaskRunner`), agent heartbeats (the
dispatcher), the scidisplay compute worker, dev nodes. `run_registered(...)`
and `registered_process(...)` are the helpers for new spawn points.

## Safety rules (enforced in `terminate`)

1. Signal only a target whose record verifies against the live process: same
   uid and start time within 2 s. A pid whose start time disagrees is a reused
   pid and is never signalled.
2. Never this process, its ancestors, pid 0/1, or our own process group.
3. Signal the process GROUP only when the record led its own group at
   registration; otherwise the pid alone.
4. Dry-run unless asked. Every plan, refusal and action writes an
   action-ledger record (who, what, why, evidence); refusals included.
5. Escalation: SIGTERM, grace (`process.containment.grace_seconds`, 10), SIGKILL.

## Detection (read-only)

`overrun`, `cpu_over_budget`, `rss_over_budget`, `orphan` (parent gone, not
`detached`), `pid_reused`, `stale_record`, `duplicate_singleton`,
`unregistered` (carries our mark, no live record), and, from the action ledger
for agents that are currently enabled, `runaway_agent_rate` (more than
`process.runaway.max_actions`=120 in `window_seconds`=60) and
`runaway_agent_failures` (last `max_consecutive_failures`=10 all `failed`;
refusals are a guard working and do not count).

## Containment defaults, and why

`process.containment.auto` defaults to `orphans`: a child whose parent died
has nobody left to be surprised by its removal. Even then it applies only to
`tool`/`test` records, only past `process.containment.orphan_factor` (2x) their
declared max runtime, and only through `sweep --auto`. Overruns, budget
breaches, duplicates and runaway agents are reported and wait for a human
unless added to the setting (`orphans,overruns,runaway-agents`; `off` disables).
TIDY's node-health audit runs the detection in report mode only.

Agent containment removes the agent from the consent set (the mechanism the
dispatcher already honours), confirms by re-reading it, and records the pause so
`process resume <agent>` can undo exactly that. Failing to confirm reports
`failed`, never `paused`.

## Surfaces

`<cli> process ls|show|stop|sweep|panic|resume` (skills `process.*` through the
dispatch chokepoint; `stop`, `sweep --kill` and `panic --kill` act, everything
else reports). Chat: `/ps` (read-only) and `/stop <id>`, confined to
processes recorded for the current session. `panic` pauses every agent and
stops agent-owned processes; with a damaged registry it falls back to the mark
scan of this uid's processes, report-only without `--kill`.

## Not covered

Resource limits are detected after the fact (`ps` sampling), not enforced by
cgroups/rlimits. Processes started outside the registered spawn points are
visible only if they inherited the mark.
